"""Capture collector for unlabeled C2/C3 channel bbox crops.

Polls each upstream channel's latest ``(pieces, frame)`` from the perception
service, decides per piece whether to grab a crop (a cheap cadence gate keyed
on the piece's advisory ByteTrack id + how far its center-of-mass has travelled
toward the exit since its last capture), crops + JPEG-encodes off the hot path,
and hands the bytes to ``channel_crop_store`` for durable, size-capped storage.

The cadence is deliberately zone-aware so the crops we keep are dense exactly
where the same-piece heuristic needs them — many right at the C3 exit (a piece
about to fall onto C4), fewer in the drop zone, and just a couple per pass on
C2. All thresholds are plain constants so they can be tuned against a real run.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import cv2
import numpy as np

import channel_crop_store


# Region codes from perception.arcs._region_lookup / PieceObservation.zone_code.
_ZONE_NONE = 0
_ZONE_DROP = 1
_ZONE_EXIT_ONLY = 2
_ZONE_PRECISE = 3


@dataclass(frozen=True)
class _ZoneCadence:
    # Capture again once the piece's COM has advanced this many output degrees
    # toward the exit since its last capture for this track.
    advance_deg: float
    # Hard cap on crops kept per track while it sits in this zone band.
    max_captures: int


@dataclass
class ChannelCropCaptureConfig:
    enabled: bool = True
    channels: tuple[int, ...] = (2, 3)
    # Poll faster than the ~30 Hz camera; the per-frame-ts dedup + cadence gate
    # do the real rate limiting.
    poll_interval_s: float = 0.03
    crop_pad_px: int = 6
    jpeg_quality: int = 80
    # Never take two crops of one track closer together in time than this, even
    # if it jumped zones — guards against duplicate near-identical frames.
    min_capture_interval_s: float = 0.05
    # Forget a track this long after we last saw it (a new pass reuses the id).
    track_ttl_s: float = 4.0
    # Zone-aware cadence for C3 (the interesting channel). Exit/precise get dense
    # coverage; drop and between-zone get sparse.
    c3_cadence: dict[int, _ZoneCadence] = field(
        default_factory=lambda: {
            _ZONE_PRECISE: _ZoneCadence(advance_deg=3.0, max_captures=4),
            _ZONE_EXIT_ONLY: _ZoneCadence(advance_deg=3.0, max_captures=4),
            _ZONE_DROP: _ZoneCadence(advance_deg=12.0, max_captures=2),
            _ZONE_NONE: _ZoneCadence(advance_deg=18.0, max_captures=2),
        }
    )
    # C2 is far upstream. advance_deg is ~30% tighter than the original 25.0 so
    # captures recur sooner; up to 5 crops of a single piece as it crosses C2
    # (most won't travel far enough to hit that ceiling).
    c2_cadence: _ZoneCadence = _ZoneCadence(advance_deg=19.2, max_captures=5)


@dataclass
class _TrackState:
    last_capture_ts: float
    last_capture_deg: Optional[float]
    count: int
    last_seen_ts: float


@dataclass(frozen=True)
class _ReadyC3Run:
    generation: object
    last_ts: float
    bbox: tuple[int, int, int, int]
    crops: tuple = ()


class ChannelCropCollector:
    def __init__(self, *, perception_service: Any, logger: Any = None,
                 config: Optional[ChannelCropCaptureConfig] = None) -> None:
        self._service = perception_service
        self._logger = logger
        self._cfg = config or ChannelCropCaptureConfig()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # (channel_id, track_key) -> _TrackState
        self._tracks: dict[tuple[int, Any], _TrackState] = {}
        self._last_frame_ts: dict[int, float] = {}
        self._last_prune = 0.0
        self._captured_total = 0
        # Recognition can reuse already-created crops without waiting for disk
        # or the collector. This optional cache never changes capture cadence.
        self._clearReadyC3()

    def _clearReadyC3(self) -> None:
        self._ready_incarnation = None
        self._ready_last_ts = None
        self._ready_runs: dict[int, _ReadyC3Run] = {}
        self._ready_observations: tuple = ()
        self._ready_published: tuple = (None, frozenset(), ())

    def _c3Incarnation(self):
        try:
            worker = self._service.workers().get(3)
            tracker = worker._tracker._tracker
            return (worker, tracker) if tracker is not None else None
        except Exception:
            return None

    @staticmethod
    def _sameIncarnation(left, right) -> bool:
        return (left is not None and right is not None
                and left[0] is right[0] and left[1] is right[1])

    def _observeReadyC3(self, pieces, frame, incarnation) -> bool:
        """Record only continuous, isolated observations; never track a piece."""
        ts = float(frame.timestamp)
        if incarnation is None:
            self._clearReadyC3()
            return False
        if not self._sameIncarnation(incarnation, self._ready_incarnation):
            self._clearReadyC3()
            self._ready_incarnation = incarnation
            # A worker may still publish its old sample after tracker replacement.
            self._ready_last_ts = ts
            return False
        if self._ready_last_ts is not None and ts <= self._ready_last_ts:
            if ts < self._ready_last_ts:
                self._clearReadyC3()
                self._ready_incarnation = incarnation
                self._ready_last_ts = ts
            return False
        previous = self._ready_runs
        # Reuse the existing fresh-image horizon; inference need not be 30 Hz.
        if self._ready_last_ts is not None and ts - self._ready_last_ts > 1.5:
            previous = {}
        self._ready_last_ts = ts
        bgr = getattr(frame, "bgr", None)
        if bgr is None or not getattr(bgr, "size", 0):
            self._ready_runs = {}
            self._ready_observations = ()
            self._ready_published = (incarnation, frozenset(), ())
            return False
        h, w = bgr.shape[:2]
        boxes = []
        for piece in pieces:
            try:
                box = tuple(int(v) for v in piece.bbox)
                if len(box) != 4:
                    raise ValueError("invalid bbox")
            except (AttributeError, TypeError, ValueError):
                # An unlocatable neighbor makes isolation unknowable.
                boxes = []
                pieces = ()
                break
            boxes.append(box)
        ids = [getattr(piece, "sv_bt_track_id", None) for piece in pieces]
        runs = {}
        pad = self._cfg.crop_pad_px
        for index, (piece, box) in enumerate(zip(pieces, boxes)):
            track_id = ids[index]
            if (not isinstance(track_id, int) or isinstance(track_id, bool)
                    or track_id <= 0 or ids.count(track_id) != 1):
                continue
            x1, y1, x2, y2 = box
            if not (0 <= x1 < x2 <= w and 0 <= y1 < y2 <= h):
                continue
            padded = (max(0, x1-pad), max(0, y1-pad), min(w, x2+pad), min(h, y2+pad))
            if any(j != index and max(padded[0], other[0]) < min(padded[2], other[2])
                   and max(padded[1], other[1]) < min(padded[3], other[3])
                   for j, other in enumerate(boxes)):
                continue
            old = previous.get(track_id)
            runs[track_id] = _ReadyC3Run(
                old.generation if old else object(), ts, box,
                old.crops if old else (),
            )
            if len(runs) >= 32:
                break
        self._ready_runs = runs
        active = frozenset((tid, run.generation) for tid, run in runs.items())
        self._ready_observations = tuple(obs for obs in self._ready_observations
                                         if (obs[1], obs[3]) in active)
        # Publish invalidation before any optional crop encoding can be busy.
        self._ready_published = (incarnation, active, self._ready_observations)
        return True

    def _publishReadyC3(self, incarnation) -> None:
        if (self._stop.is_set()
                or not self._sameIncarnation(incarnation, self._c3Incarnation())):
            self._clearReadyC3()
            return
        active = frozenset((tid, run.generation) for tid, run in self._ready_runs.items())
        observations = tuple(obs for obs in self._ready_observations
                             if (obs[1], obs[3]) in active)
        observations += tuple((run.last_ts, tid, run.bbox, run.generation, run.crops)
                              for tid, run in self._ready_runs.items())
        self._ready_observations = observations[-32:]
        # One immutable publication: readers neither lock nor wait for a tick.
        self._ready_published = (incarnation, active, self._ready_observations)

    def ready_c3_views(self, frame_ts, leader_id, bbox) -> list[dict]:
        """Return up to four ready crops bound to this exact release observation.

        Recognition chooses its smaller distinct subset without delaying C4.
        """
        incarnation, active, observations = self._ready_published
        if not self._sameIncarnation(incarnation, self._c3Incarnation()):
            return []
        try:
            box = tuple(int(v) for v in bbox)
            ts = float(frame_ts)
        except (TypeError, ValueError):
            return []
        for observed_ts, tid, observed_box, generation, crops in reversed(observations):
            if (observed_ts != ts or tid != leader_id or observed_box != box
                    or (tid, generation) not in active):
                continue
            eligible = [crop for crop in crops if 0 <= ts-crop[0] <= 1.5]
            if not self._sameIncarnation(incarnation, self._c3Incarnation()):
                return []
            return [{"frame_ts": stamp, "bgr": bgr, "source": "c3_history"}
                    for stamp, bgr in eligible]
        return []

    # --- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="channel-crop-collector"
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None
        self._clearReadyC3()

    def stats(self) -> dict[str, Any]:
        return {"captured_total": self._captured_total, "tracked": len(self._tracks)}

    # --- gating ---------------------------------------------------------

    def _isSorting(self) -> bool:
        # Only collect while actively sorting (RUNNING) — pieces aren't flowing
        try:
            from server import shared_state
            from defs.sorter_controller import SorterLifecycle

            controller = shared_state.controller_ref
            if controller is None:
                return False
            return controller.state == SorterLifecycle.RUNNING
        except Exception:
            return False

    # --- loop -----------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            cfg = self._cfg
            if not cfg.enabled or not self._isSorting():
                self._clearReadyC3()
                self._stop.wait(0.5)
                continue
            for channel_id in cfg.channels:
                try:
                    self._collectChannel(channel_id, cfg)
                except Exception as exc:
                    _log(self._logger, "warning",
                         f"[channel-crop] collect ch{channel_id} failed: {exc}")
            self._maybePrune()
            self._stop.wait(cfg.poll_interval_s)

    def _collectChannel(self, channel_id: int, cfg: ChannelCropCaptureConfig) -> None:
        incarnation = self._c3Incarnation() if channel_id == 3 else None
        res = self._service.read_pieces_and_frame(channel_id)
        if res is None:
            return
        pieces, frame = res
        ready_frame = (self._observeReadyC3(pieces, frame, incarnation)
                       if channel_id == 3 else False)
        if not pieces:
            if ready_frame:
                self._publishReadyC3(incarnation)
            return
        ts = float(frame.timestamp)
        if self._last_frame_ts.get(channel_id) == ts:
            return
        self._last_frame_ts[channel_id] = ts
        bgr = frame.bgr
        if bgr is None or bgr.size == 0:
            return
        h, w = bgr.shape[:2]
        now = time.time()
        for piece in pieces:
            crop = self._considerPiece(channel_id, piece, bgr, w, h, ts, now, cfg)
            if ready_frame and crop is not None:
                tid = getattr(piece, "sv_bt_track_id", None)
                run = self._ready_runs.get(tid)
                if run is not None:
                    crop["bgr"].flags.writeable = False
                    self._ready_runs[tid] = _ReadyC3Run(
                        run.generation, run.last_ts, run.bbox,
                        (run.crops + ((crop["frame_ts"], crop["bgr"]),))[-4:],
                    )
        if ready_frame:
            self._publishReadyC3(incarnation)

    def _cadenceFor(self, channel_id: int, zone_code: int,
                    cfg: ChannelCropCaptureConfig) -> _ZoneCadence:
        if channel_id == 2:
            return cfg.c2_cadence
        return cfg.c3_cadence.get(zone_code, cfg.c3_cadence[_ZONE_NONE])

    def _considerPiece(self, channel_id: int, piece: Any, bgr: np.ndarray,
                       w: int, h: int, ts: float, now: float,
                       cfg: ChannelCropCaptureConfig) -> Optional[dict]:
        zone_code = int(getattr(piece, "zone_code", 0) or 0)
        deg = getattr(piece, "com_forward_to_exit_deg", None)
        deg_f = float(deg) if isinstance(deg, (int, float)) else None
        cadence = self._cadenceFor(channel_id, zone_code, cfg)

        track_id = getattr(piece, "sv_bt_track_id", None)
        # Without a stable id we can't associate a piece across frames, so bucket
        # by coarse COM section (~4 deg) as a stand-in identity for the gate.
        if track_id is not None:
            track_key: Any = ("t", int(track_id))
        else:
            section = int(getattr(piece, "com_section", 0) or 0)
            track_key = ("p", section // 4)
        key = (channel_id, track_key)

        state = self._tracks.get(key)
        if state is not None:
            state.last_seen_ts = now
            if now - state.last_capture_ts < cfg.min_capture_interval_s:
                return
            if state.count >= cadence.max_captures:
                return
            # Advance gate: only re-capture once the piece has travelled far
            # enough toward the exit (smaller deg = more forward). If either deg
            # is unknown, fall through and capture (time gate already passed).
            if deg_f is not None and state.last_capture_deg is not None:
                if state.last_capture_deg - deg_f < cadence.advance_deg:
                    return

        bbox = getattr(piece, "bbox", None)
        crop = _cropBbox(bgr, bbox, cfg.crop_pad_px, w, h)
        if crop is None or crop.size == 0:
            return
        ok, buf = cv2.imencode(
            ".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), int(cfg.jpeg_quality)]
        )
        if not ok:
            return
        jpeg = buf.tobytes()
        meta = {
            "channel": channel_id,
            "ts": ts,
            "created_at": now,
            "track_id": int(track_id) if track_id is not None else None,
            "com_forward_to_exit_deg": deg_f,
            "com_section": int(getattr(piece, "com_section", 0) or 0),
            "zone_code": zone_code,
            "sharpness": _sharpness(crop),
            "bbox": tuple(int(v) for v in bbox) if bbox else None,
        }
        channel_crop_store.enqueue(jpeg, meta)
        self._captured_total += 1

        if state is None:
            self._tracks[key] = _TrackState(
                last_capture_ts=now, last_capture_deg=deg_f, count=1, last_seen_ts=now
            )
        else:
            state.last_capture_ts = now
            state.last_capture_deg = deg_f
            state.count += 1
        return {"bgr": crop, "frame_ts": ts}

    def _maybePrune(self) -> None:
        now = time.time()
        if now - self._last_prune < 1.0:
            return
        self._last_prune = now
        ttl = self._cfg.track_ttl_s
        stale = [k for k, s in self._tracks.items() if now - s.last_seen_ts > ttl]
        for k in stale:
            self._tracks.pop(k, None)


def _cropBbox(bgr: np.ndarray, bbox: Any, pad: int, w: int, h: int) -> Optional[np.ndarray]:
    if not bbox:
        return None
    try:
        x1, y1, x2, y2 = float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
    except (TypeError, ValueError, IndexError):
        return None
    xa = max(0, int(round(min(x1, x2))) - pad)
    ya = max(0, int(round(min(y1, y2))) - pad)
    xb = min(w, int(round(max(x1, x2))) + pad)
    yb = min(h, int(round(max(y1, y2))) + pad)
    if xb <= xa or yb <= ya:
        return None
    # Copy — the capture thread reuses the frame buffer once we return.
    return bgr[ya:yb, xa:xb].copy()


def _sharpness(crop: np.ndarray) -> Optional[float]:
    try:
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())
    except Exception:
        return None


def _log(logger: Any, level: str, message: str) -> None:
    if logger is None:
        return
    try:
        getattr(logger, level)(message)
    except Exception:
        pass
