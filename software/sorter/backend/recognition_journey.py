"""Recognition only: KnownObject UUID, evidence-scoped aliases and one request.

No transport, pocket, motor, incident or controller dependency. Scene publishers
must supply complete detections and an epoch that changes on camera/tracker
restart. A track generation changes on reuse; raw numeric IDs are never keys.
The caller owns scheduling; no operation waits for more photographs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from functools import wraps
import math
import threading
from typing import Callable

import numpy as np

from recognition_views import distinct_indices, valid_bgr
from subsystems.classification_channel import crop_quality


class Stage(StrEnum):
    C3_READY = "c3_ready"
    C3_HISTORY = "c3_history"
    C3_EXIT = "c3_exit"
    FALL = "carousel_freefall"
    LANDING = "c4_landing"
    SETTLED = "c4_settled"


@dataclass(frozen=True)
class TrackAlias:
    channel: int
    epoch: str  # camera + tracker incarnation; never inferred from timestamp
    track_id: int
    generation: int


@dataclass(frozen=True)
class Detection:
    alias: TrackAlias
    bbox: tuple[int, int, int, int]


@dataclass(frozen=True)
class Scene:
    channel: int
    epoch: str
    sequence: int
    timestamp: float
    bgr: np.ndarray = field(repr=False, compare=False)
    detections: tuple[Detection, ...] = ()
    complete: bool = True

    @property
    def key(self):
        return self.channel, self.epoch, self.sequence


@dataclass(frozen=True)
class Observation:
    stage: Stage
    alias: TrackAlias
    frame_key: tuple
    timestamp: float
    bgr: np.ndarray = field(repr=False, compare=False)
    evidence: str = "isolated_detection"


@dataclass(frozen=True)
class RecognitionRequest:
    piece_uuid: str
    episode_id: str
    generation: int
    images: tuple[Observation, ...]


@dataclass(frozen=True)
class RecognitionResult:
    piece_uuid: str
    episode_id: str
    generation: int
    response: object = None
    error: str | None = None


@dataclass
class Journey:
    piece: object
    episode_id: str
    generation: int
    c3_alias: TrackAlias | None
    started_at: float
    aliases: dict[TrackAlias, tuple] = field(default_factory=dict)
    observations: list[Observation] = field(default_factory=list)
    crossing_confirmed: bool = False
    crossing_rejected: bool = False
    request: RecognitionRequest | None = None
    result: RecognitionResult | None = None
    submitted: bool = False
    closed: bool = False
    lock: object = field(default_factory=threading.RLock, repr=False)
    seen: dict[TrackAlias, tuple[int, float]] = field(default_factory=dict)
    _uuid: str = field(init=False)

    def __post_init__(self):
        self._uuid = self.piece.uuid

    @property
    def uuid(self):
        return self._uuid


@dataclass
class _Crossing:
    journey: Journey
    release: Scene
    baseline: Scene
    c3_region: tuple
    c4_region: tuple
    deadline: float
    frames: list[Scene] = field(default_factory=list)
    reservation: tuple | None = None


def _overlap(a, b):
    return max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])


def _inside(a, b):
    return b[0] <= a[0] < a[2] <= b[2] and b[1] <= a[1] < a[3] <= b[3]


def _valid(scene):
    try:
        return _valid_scene(scene)
    except (TypeError, ValueError, AttributeError, IndexError):
        return False


def _valid_scene(scene):
    if (
        not isinstance(scene, Scene)
        or not scene.complete
        or not scene.epoch
        or scene.channel not in (3, 4)
        or type(scene.sequence) is not int
        or scene.sequence < 0
        or not math.isfinite(scene.timestamp)
        or not valid_bgr(scene.bgr)
    ):
        return False
    bounds = (0, 0, scene.bgr.shape[1], scene.bgr.shape[0])
    return all(
        isinstance(d, Detection)
        and isinstance(d.alias, TrackAlias)
        and len(d.bbox) == 4
        and all(type(v) is int for v in d.bbox)
        and d.alias.channel == scene.channel
        and d.alias.epoch == scene.epoch
        and type(d.alias.generation) is int
        and d.alias.generation >= 0
        and type(d.alias.track_id) is int
        and _inside(d.bbox, bounds)
        for d in scene.detections
    )


def _optional(default=False):
    """Malformed/unavailable recognition evidence is omission, never control flow."""

    def decorate(fn):
        @wraps(fn)
        def call(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception:
                return default

        return call

    return decorate


def _same_detection(detections):
    return (
        bool(detections)
        and len(detections) <= 2
        and len({d.bbox for d in detections}) == 1
        and len({d.alias for d in detections}) == len(detections)
        and len({d.alias.track_id for d in detections}) == len(detections)
    )


def _crop(scene, detection):
    box = detection.bbox
    # Do not pad across another detected piece. This is a tight original crop.
    if any(d is not detection and _overlap(box, d.bbox) for d in scene.detections):
        return None
    x1, y1, x2, y2 = box
    crop = scene.bgr[y1:y2, x1:x2].copy()
    correct = getattr(scene, "correct_pixels", None)
    return correct(crop, "recognition_crop_color_ms") if correct is not None else crop


class RecognitionJourneys:
    """Single recognition owner; live controllers are deliberately not wired here.

    Call start_c3 at selection, before release. begin_crossing/burst_frame/land
    consume already-available paired observations; their return values affect
    optional imagery only. Any missing proof leaves the C4-only request usable.
    """

    def __init__(self):
        self.journeys: dict[str, Journey] = {}
        self._closed_uuids: set[str] = (
            set()
        )  # small once-only tombstones, no image arrays
        self._owners: dict[TrackAlias, str] = {}
        self._crossing: _Crossing | None = None
        self._epochs: dict[int, str] = {}
        self._latest: dict[tuple, int] = {}

    def set_epoch(self, channel, epoch):
        """Publisher lifecycle notification, not an inference from track motion."""
        if self._epochs.get(channel) not in (None, epoch) and self._crossing:
            self._reject_crossing()
        self._epochs[channel] = epoch

    def _scene(self, scene):
        return _valid(scene) and self._epochs.get(scene.channel) == scene.epoch

    def _bind(self, j, alias, scene):
        if j.closed or j.submitted or j.request is not None:
            return False
        owner = self._owners.get(alias)
        slot = (alias.channel, alias.epoch, alias.track_id)
        previous = self._latest.get(slot, -1)
        if owner not in (None, j.uuid) or alias.generation < previous:
            return False
        self._owners[alias] = j.uuid
        self._latest[slot] = alias.generation
        j.aliases[alias] = scene.key
        return True

    def _owned(self, j, alias):
        return (
            self._epochs.get(alias.channel) == alias.epoch
            and self._latest.get((alias.channel, alias.epoch, alias.track_id))
            == alias.generation
            and self._owners.get(alias) == j.uuid
            and alias in j.aliases
        )

    @_optional(None)
    def start_c3(self, piece, *, episode_id, generation, scene, alias):
        """Reuses the KnownObject supplied at C3; never creates a C4 identity."""
        if (
            not getattr(piece, "uuid", None)
            or piece.uuid in self._closed_uuids
            or not episode_id
            or type(generation) is not int
            or generation < 0
            or not self._scene(scene)
            or alias.channel != 3
        ):
            return None
        matches = [d for d in scene.detections if d.alias == alias]
        if len(matches) != 1 or _crop(scene, matches[0]) is None:
            return None
        if piece.uuid in self.journeys:
            j = self.journeys[piece.uuid]
            return (
                j
                if (
                    j.piece is piece
                    and j.episode_id == episode_id
                    and j.generation == generation
                    and j.c3_alias == alias
                    and not j.closed
                )
                else None
            )
        j = Journey(piece, episode_id, generation, alias, scene.timestamp)
        if not self._bind(j, alias, scene):
            return None
        self.journeys[j.uuid] = j
        self.observe(j, scene, alias, Stage.C3_READY)
        return j

    def _retain(self, j, observation):
        with j.lock:
            if j.closed or j.submitted or j.request is not None:
                return False
            if any(
                o.frame_key == observation.frame_key and o.alias == observation.alias
                for o in j.observations
            ):
                return False
            observation.bgr.setflags(write=False)
            same = [o for o in j.observations if o.stage == observation.stage]
            if len(same) >= 8:
                # Bounded candidates, replacing the weakest only when useful.
                pool = same + [observation]
                keep = crop_quality.selectBurstIndices(
                    [crop_quality.scoreCrop(o.bgr) for o in pool], 8
                )
                if 8 not in keep:
                    return False
                j.observations = [
                    o for o in j.observations if not any(o is old for old in same)
                ]
                j.observations.extend(pool[i] for i in keep)
            else:
                j.observations.append(observation)
            return True

    def observe(self, j, scene, alias, stage):
        """Capture a known alias. Optional failures return False, never motion effects."""
        try:
            if (
                not self._scene(scene)
                or not self._owned(j, alias)
                or scene.timestamp < j.started_at
                or stage not in Stage
                or stage == Stage.FALL
                or (alias.channel == 3)
                != (stage in (Stage.C3_READY, Stage.C3_HISTORY, Stage.C3_EXIT))
            ):
                return False
            previous = j.seen.get(alias)
            if previous and (
                scene.sequence <= previous[0] or scene.timestamp <= previous[1]
            ):
                return False
            matches = [d for d in scene.detections if d.alias == alias]
            if len(matches) != 1:
                return False
            crop = _crop(scene, matches[0])
            if crop is None:
                return False
            j.seen[alias] = (scene.sequence, scene.timestamp)
            return self._retain(
                j, Observation(stage, alias, scene.key, scene.timestamp, crop)
            )
        except Exception:
            return False

    def _reject_crossing(self):
        if self._crossing:
            self._crossing.journey.crossing_rejected = True
        # Keep the rejected crossing reserved until close(); an overlapping
        # release cannot make a third release appear exclusive.

    @_optional()
    def begin_crossing(self, j, *, release, empty_c4, c3_region, c4_region, reservation=None):
        """One exclusive crossing, empty receiving ROI, exact C3 leader evidence."""
        if self._crossing:
            self._reject_crossing()
            j.crossing_rejected = True
            return False
        if (
            j.closed
            or j.crossing_rejected
            or j.crossing_confirmed
            or not self._scene(release)
            or not self._scene(empty_c4)
            or release.channel != 3
            or empty_c4.channel != 4
            or release.timestamp < j.started_at
            or not 0 <= release.timestamp - empty_c4.timestamp <= 0.5
            or j.c3_alias is None
            or not self._owned(j, j.c3_alias)
        ):
            return False
        leaders = [d for d in release.detections if _overlap(d.bbox, c3_region)]
        if (
            len(leaders) != 1
            or (reservation is None and len(release.detections) != 1)
            or leaders[0].alias != j.c3_alias
            or not _inside(leaders[0].bbox, c3_region)
            or any(_overlap(d.bbox, c4_region) for d in empty_c4.detections)
            or (reservation is None and empty_c4.detections)
            or (reservation is not None and
                (len(reservation) != 3 or reservation[0] != j.episode_id
                 or reservation[2] != j.generation))
        ):
            return False
        self.observe(j, release, j.c3_alias, Stage.C3_EXIT)
        self._crossing = _Crossing(
            j, release, empty_c4, c3_region, c4_region, release.timestamp + 3.0,
            reservation=reservation
        )
        return True

    @_optional()
    def burst_frame(self, scene):
        """Original pre-trigger buffer scenes; not yet assigned to any journey."""
        c = self._crossing
        if (
            c is None
            or c.journey.crossing_rejected
            or not self._scene(scene)
            or scene.channel != 4
            or scene.epoch != c.baseline.epoch
            or not c.release.timestamp < scene.timestamp <= c.deadline
            or scene.sequence <= c.baseline.sequence
        ):
            return False
        if any(
            scene.sequence <= f.sequence or scene.timestamp <= f.timestamp
            for f in c.frames
        ):
            return False
        relevant = [d for d in scene.detections if _overlap(d.bbox, c.c4_region)]
        if c.reservation is None and len(scene.detections) != len(relevant):
            self._reject_crossing()
            return False
        if len(relevant) > 1 and not _same_detection(relevant):
            self._reject_crossing()
            return False
        if not relevant or not _inside(relevant[0].bbox, c.c4_region):
            return False
        # Do not reinterpret a resident/previous journey as this new arrival.
        if any(
            self._owners.get(d.alias) not in (None, c.journey.uuid) for d in relevant
        ):
            self._reject_crossing()
            return False
        c.frames.append(
            Scene(
                scene.channel,
                scene.epoch,
                scene.sequence,
                scene.timestamp,
                scene.bgr.copy(),
                scene.detections,
                scene.complete,
            )
        )
        c.frames = c.frames[-12:]
        return True

    @_optional()
    def land(self, j, *, scene, c3_after):
        c = self._crossing
        if (
            c is None
            or c.journey is not j
            or j.crossing_rejected
            or not self._scene(scene)
            or not self._scene(c3_after)
            or scene.channel != 4
            or scene.epoch != c.baseline.epoch
            or c3_after.epoch != c.release.epoch
            or c3_after.channel != 3
            or c3_after.sequence <= c.release.sequence
            or scene.sequence <= c.baseline.sequence
            or not c.release.timestamp < scene.timestamp <= c.deadline
            or not scene.timestamp <= c3_after.timestamp <= scene.timestamp + 0.5
            or (c.reservation is None and c3_after.detections)
            or any(d.alias == j.c3_alias for d in c3_after.detections)
        ):
            self._reject_crossing()
            return False
        matches = [d for d in scene.detections if _overlap(d.bbox, c.c4_region)]
        if (
            len(matches) != 1
            or (c.reservation is None and len(scene.detections) != 1)
            or not _inside(matches[0].bbox, c.c4_region)
            or _crop(scene, matches[0]) is None
            or not self._bind(j, matches[0].alias, scene)
        ):
            self._reject_crossing()
            return False
        j.crossing_confirmed = True
        # A fall label is not a piece identity. Connect it to the landing label
        # only through explicit same-frame/same-detection alias bridges.
        connected = {matches[0].alias}
        bridges = []
        for frame in c.frames:
            labels = [d for d in frame.detections if _overlap(d.bbox, c.c4_region)]
            if (
                frame.timestamp < scene.timestamp
                and len(labels) == 2
                and _same_detection(labels)
            ):
                bridges.append({d.alias for d in labels})
        for _ in bridges:
            for bridge in bridges:
                if connected & bridge:
                    connected.update(bridge)
        for frame in c.frames:
            if frame.timestamp >= scene.timestamp:
                continue
            d = next(d for d in frame.detections if _overlap(d.bbox, c.c4_region))
            if d.alias not in connected:
                continue
            crop = _crop(frame, d)
            if crop is not None and self._bind(j, d.alias, frame):
                self._retain(
                    j,
                    Observation(
                        Stage.FALL,
                        d.alias,
                        frame.key,
                        frame.timestamp,
                        crop,
                        f"exclusive_crossing:{j.episode_id}:{j.generation}",
                    ),
                )
        self._crossing = None
        self.observe(j, scene, matches[0].alias, Stage.LANDING)
        return True

    @_optional()
    def bind_same_detection(self, j, *, old_alias, new_alias, scene):
        """Explicit alias bridge: both labels on the SAME frame and exact bbox.

        A nearby replacement detection or time-window match is insufficient.
        Publishers without this evidence simply lose optional new-track views.
        """
        if (
            not self._scene(scene)
            or scene.channel != 4
            or not self._owned(j, old_alias)
        ):
            return False
        previous = j.seen.get(old_alias)
        if previous and (
            scene.sequence <= previous[0] or scene.timestamp <= previous[1]
        ):
            return False
        old = [d for d in scene.detections if d.alias == old_alias]
        new = [d for d in scene.detections if d.alias == new_alias]
        if (
            len(old) != 1
            or len(new) != 1
            or old_alias == new_alias
            or old[0].bbox != new[0].bbox
            or old_alias.track_id == new_alias.track_id
            or any(
                d.alias not in (old_alias, new_alias) and _overlap(d.bbox, old[0].bbox)
                for d in scene.detections
            )
        ):
            return False
        return self._bind(j, new_alias, scene)

    @_optional(None)
    def c4_only(self, piece, *, episode_id, generation, scene, alias):
        """Independent isolated C4 identity when upstream association is absent.

        Cannot graft an uncertain C4 observation onto a C3 journey. Uses the
        caller's existing KnownObject, with no new or competing UUID.
        """
        if (
            piece.uuid in self.journeys
            or piece.uuid in self._closed_uuids
            or not self._scene(scene)
            or scene.channel != 4
            or not episode_id
            or type(generation) is not int
            or generation < 0
        ):
            return None
        matches = [d for d in scene.detections if d.alias == alias]
        if len(matches) != 1 or _crop(scene, matches[0]) is None:
            return None
        j = Journey(piece, episode_id, generation, None, scene.timestamp)
        if not self._bind(j, alias, scene):
            return None
        self.journeys[j.uuid] = j
        self.observe(j, scene, alias, Stage.SETTLED)
        return j

    def start_reserved(self, piece, *, episode_id, generation, scene=None, alias=None):
        """Retain the UUID before motion even when optional C3 imagery is absent.

        Runtime owns the physical reservation; absence of an alias gives no
        upstream image permission. This does not reserve or move any pocket.
        """
        if scene is not None and alias is not None:
            j = self.start_c3(piece, episode_id=episode_id, generation=generation,
                              scene=scene, alias=alias)
            if j is not None:
                return j
        if piece.uuid in self.journeys or piece.uuid in self._closed_uuids:
            return None
        j = Journey(piece, episode_id, generation, None, 0.0)
        self.journeys[j.uuid] = j
        return j

    @_optional()
    def observe_reserved_c4(self, j, *, scene, alias, region, reservation):
        """C4-only fallback for the sole stationary reserved intake generation.

        Caller must establish physical arrival into that reservation. Full scene
        crop isolation remains mandatory. This cannot authorize C3/fall images;
        an unproven crossing retains them for evidence but excludes them from
        the request. Track aliases still have global exclusive generation owners.
        """
        if (reservation[0] != j.episode_id or reservation[2] != j.generation
                or not self._scene(scene) or scene.channel != 4):
            return False
        labels = [d for d in scene.detections if _overlap(d.bbox, region)]
        if (len(labels) != 1 or labels[0].alias != alias
                or not _inside(labels[0].bbox, region) or _crop(scene, labels[0]) is None):
            return False
        if not self._bind(j, alias, scene):
            return False
        return self.observe(j, scene, alias, Stage.SETTLED)

    @_optional(None)
    def prepare_request(self, j):
        with j.lock:
            if j.closed:
                return None
            if j.request is not None:
                return j.request
            candidates = tuple(j.observations)
            include_upstream = j.crossing_confirmed and not j.crossing_rejected
        selected = select_views(candidates, include_upstream=include_upstream)
        with j.lock:
            if j.closed:
                return None
            if j.request is not None:
                return j.request
            if not selected:
                return None  # no valid C4; no waiting, no incident or transport action
            j.request = RecognitionRequest(
                j.uuid, j.episode_id, j.generation, tuple(selected)
            )
            return j.request

    def submit(self, j, provider: Callable, *, launch=None):
        """At most one provider call, even on error, repeated calls or concurrent ticks.

        provider(request) runs off-loop. Result is identity-scoped data only;
        no mutation of KnownObject classification, custody or transport.
        """
        with j.lock:
            if (
                j.closed
                or j.submitted
                or not any(
                    o.stage in (Stage.LANDING, Stage.SETTLED) and valid_bgr(o.bgr)
                    for o in j.observations
                )
            ):
                return False
            j.submitted = True

        def work():
            request = None
            try:
                # Quality ranking and dedup, as well as provider I/O, stay off
                # the caller's control tick. submitted freezes the candidate set.
                request = self.prepare_request(j)
                if request is None:
                    raise ValueError("no valid C4 image")
                response = provider(request)
                result = RecognitionResult(j.uuid, j.episode_id, j.generation, response)
            except Exception as exc:
                result = RecognitionResult(
                    j.uuid, j.episode_id, j.generation, error=str(exc)
                )
            with j.lock:
                if not j.closed and j.request is request:
                    j.result = result

        try:
            if launch is None:
                threading.Thread(
                    target=work, daemon=True, name="journey-brickognize"
                ).start()
            else:
                launch(work)
        except Exception as exc:
            with j.lock:
                j.result = RecognitionResult(
                    j.uuid, j.episode_id, j.generation, error=str(exc)
                )
        return True

    def close(self, j):
        with j.lock:
            j.closed = True
            self._closed_uuids.add(j.uuid)
            self.journeys.pop(j.uuid, None)
            if self._crossing and self._crossing.journey is j:
                self._reject_crossing()
                self._crossing = None


def select_views(observations, *, include_upstream):
    """Existing quality ranking + pHash dedup; up to four available distinct views."""
    groups = [[], [], [], []]  # settled, C3, fall, landing/bounce
    for o in observations:
        if not valid_bgr(o.bgr):
            continue
        try:
            quality = crop_quality.scoreCrop(o.bgr)
            if quality.fft_hf_ratio <= 0 and quality.lap_var_piece_norm <= 0:
                continue  # featureless/empty crop, not another view
        except Exception:
            continue
        group = (
            0
            if o.stage == Stage.SETTLED
            else 3
            if o.stage == Stage.LANDING
            else 2
            if o.stage == Stage.FALL
            else 1
        )
        if group in (1, 2) and not include_upstream:
            continue
        groups[group].append(o)
    if not groups[0] and not groups[3]:
        return []
    ranked = []
    for group in groups:
        pool = list(group)
        ordered = []
        while pool:
            best = crop_quality.bestIndex([crop_quality.scoreCrop(o.bgr) for o in pool])
            if best is None:
                break
            ordered.append(pool.pop(best))
        ranked.append(ordered)
    primary = ranked[0] or ranked[3]
    preferred = [primary[0]]
    preferred += [g[0] for g in ranked[1:] if g and g[0] is not primary[0]]
    preferred += [o for g in ranked for o in g if not any(o is p for p in preferred)]
    # Reuse existing dedup, additionally reject simple in-plane quarter turns.
    selected = []
    for o in preferred:
        if any(
            any(
                len(distinct_indices([p.bgr, np.rot90(o.bgr, k).copy()])) == 1
                for k in range(4)
            )
            for p in selected
        ):
            continue
        selected.append(o)
        if len(selected) == 4:
            break
    return selected
