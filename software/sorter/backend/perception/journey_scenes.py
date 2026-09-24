"""Bounded original-frame provenance for optional recognition views.

Published on the inference worker, before consumers filter detections to zones.
Camera handle and tracker instance lifetimes define epochs; a disappearing
numeric label gets a new observation generation on reappearance.
"""

from collections import deque
from dataclasses import asdict
from uuid import uuid4

from recognition_journey import Detection, Scene, TrackAlias


class JourneyScenes:
    def __init__(self, channel):
        self.channel = channel
        self.records = ()
        from vision.tracking.drop_zone_burst import RollingFrameBuffer

        self.rolling_buffer = RollingFrameBuffer(maxlen=16)
        self._history = deque(maxlen=16)
        self._lifetimes = None
        self._epoch = uuid4().hex
        self._sequence = 0
        self._generations = {}
        self._present = set()
        self.capture_tracker = None
        self._capture_camera = None
        self.details = {}

    def publish_c4(self, frame, bboxes, scores, *, camera, channel):
        """Reuse original carousel snapshots/freefall/burst as observation only.

        The private history has no disk store, delivery observer or automatic
        recognition callback. Normal control tracking and custody are separate.
        """
        if self.capture_tracker is None or camera is not self._capture_camera:
            import numpy as np
            from vision.tracking.polar_tracker import PolarFeederTracker
            from vision.tracking.handoff import PieceHandoffManager
            from vision.tracking.history import PieceHistoryBuffer
            from vision.tracking.drop_zone_burst import DropZoneBurstCollector

            self._capture_camera = camera
            self.capture_history = PieceHistoryBuffer(max_entries=32)
            self.capture_tracker = PolarFeederTracker(
                "carousel",
                PieceHandoffManager(handoff_chain={}),
                history=self.capture_history,
            )
            ys, xs = np.nonzero(channel.mask)
            radii = np.hypot(xs - channel.center[0], ys - channel.center[1])
            if len(radii):
                self.capture_tracker.set_channel_geometry(
                    channel.center,
                    float(radii.min()),
                    float(radii.max()),
                    sector_count=10,
                )
            self.burst = DropZoneBurstCollector(self.capture_history)
            self.burst.rolling_buffer = self.rolling_buffer
            self._burst_started = set()
            self.details = {}
        tracker = self.capture_tracker
        tracks = tracker.update(bboxes, scores, frame.timestamp, frame_bgr=frame.bgr)
        ids = {
            t.bbox: t.global_id
            for t in tracks
            if not t.coasting and t.last_seen_ts == frame.timestamp
        }
        scene = self.publish(frame, bboxes, ids, camera=camera, tracker=tracker)
        from recognition_journey_capture import HistoryReference

        for detection in scene.detections:
            alias = detection.alias
            with tracker._lock:
                live = next(
                    (
                        t
                        for t in tracker._tracks.values()
                        if t.global_id == alias.track_id
                    ),
                    None,
                )
                if live is None:
                    continue
                created = live.first_seen_ts
                snapshots = [asdict(s) for s in live.sector_snapshots]
            detail = self.capture_history.get_detail(alias.track_id) or {}
            detail = {
                **detail,
                "global_id": alias.track_id,
                "created_at": created,
                "segments": [
                    {"source_role": "carousel", "sector_snapshots": snapshots}
                ],
            }
            self.details[alias] = (
                HistoryReference(alias, alias.track_id, created),
                detail,
            )
            if alias not in self._burst_started:
                self._burst_started.add(alias)
                self.burst.trigger(
                    alias.track_id,
                    lambda raw, a=alias: self._burst_detection(raw, a),
                    self._latest_burst_frame,
                )
        present = {d.alias for s in self.records for d in s.detections}
        self.details = {a: d for a, d in self.details.items() if a in present}
        self._burst_started.intersection_update(present)
        return scene

    def _burst_detection(self, raw, alias):
        import numpy as np

        for scene in reversed(self.records):
            if np.array_equal(scene.bgr, raw):
                same = [d for d in scene.detections if d.alias == alias]
                if len(same) == 1:
                    return same[0].bbox, 1.0
                break
        return None, None

    def _latest_burst_frame(self):
        records = self.records
        return (records[-1].bgr, records[-1].timestamp) if records else None

    def publish(self, frame, bboxes, track_ids, *, camera, tracker):
        lifetimes = (camera, tracker)
        if self._lifetimes is None or any(
            a is not b for a, b in zip(lifetimes, self._lifetimes)
        ):
            self._lifetimes = lifetimes
            self._epoch = uuid4().hex
            self._present.clear()
            self._generations.clear()
        self._sequence += 1
        present = set()
        detections = []
        for index, box in enumerate(bboxes):
            tid = track_ids.get(tuple(box))
            # Untracked observations cannot silently disappear from complete
            # scene evidence. They get an ephemeral alias, never custody.
            tid = int(tid) if tid is not None else -(self._sequence * 10000 + index + 1)
            if tid not in self._present:
                self._generations[tid] = self._generations.get(tid, 0) + 1
            present.add(tid)
            alias = TrackAlias(self.channel, self._epoch, tid, self._generations[tid])
            detections.append(Detection(alias, tuple(int(v) for v in box)))
        self._present = present
        # Bound ephemeral aliases as well as pixels.
        self._generations = {
            tid: gen
            for tid, gen in self._generations.items()
            if tid > 0 or tid in present
        }
        raw = frame.bgr
        if self.channel == 4:
            self.rolling_buffer.push(raw, frame.timestamp)
            raw = self.rolling_buffer.snapshot()[-1].raw
        scene = Scene(
            self.channel,
            self._epoch,
            self._sequence,
            frame.timestamp,
            raw,
            tuple(detections),
        )
        self._history.append(scene)
        while self._history and scene.timestamp - self._history[0].timestamp > 1.5:
            self._history.popleft()
        self.records = tuple(self._history)
        return scene


def zone_region(channel, sections):
    """Conservative rectangle enclosing the existing saved arc mask."""
    import numpy as np

    ys, xs = np.nonzero(channel.mask)
    if not len(xs):
        return None
    angles = (
        np.degrees(np.arctan2(ys - channel.center[1], xs - channel.center[0]))
        - channel.radius1_angle_image
    ) % 360
    selected = np.isin(angles.astype(int), tuple(sections))
    xs, ys = xs[selected], ys[selected]
    if not len(xs):
        return None
    return int(xs.min()), int(ys.min()), int(xs.max() + 1), int(ys.max() + 1)
