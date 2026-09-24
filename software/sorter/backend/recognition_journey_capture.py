"""Adapters for existing in-memory crops/history/buffer and Brickognize.

No legacy bare global-ID/time-window association is upgraded to proof. Old
history lacking paired complete observations contributes nothing. Everything
here is optional enrichment; transport never consumes a return value.
"""

from dataclasses import dataclass

import numpy as np

from recognition_journey import Observation, Scene, Stage, _overlap
from recognition_views import capture_release_view, valid_bgr


def retain_ready_history(journeys, journey, service, evidence, scene, paired_scene):
    """Reuse exact-frame, same-incarnation ChannelCropCollector C3 history.

    capture_release_view enforces the collector's continuous generation and
    isolated crop. Each older frame additionally needs its original complete
    generation-scoped scene. The collector's numeric ID alone is insufficient.
    Snapshot only before release; no asynchronous lookup later.
    """
    try:
        alias = journey.c3_alias
        if (
            alias is None
            or not journeys._owned(journey, alias)
            or not journeys._scene(scene)
            or scene.channel != 3
            or scene.timestamp != evidence.get("ts")
            or journey.closed
        ):
            return 0
        matches = [d for d in scene.detections if d.alias == alias]
        if len(matches) != 1:
            return 0
        material = evidence.get("material", [])
        same = [p for p in material if p.get("id") == alias.track_id]
        if len(same) != 1 or tuple(same[0]["bbox"]) != matches[0].bbox:
            return 0
        # The paired service sample must still be the exact scene; no newest
        # frame or nearest-time substitution is permitted.
        paired = service.read_pieces_and_frame(3)
        if (
            paired is None
            or paired[1].timestamp != scene.timestamp
            or not np.array_equal(paired[1].bgr, scene.bgr)
        ):
            return 0
        view = capture_release_view(
            service, evidence, alias.track_id, now=scene.timestamp
        )
        if view is None:
            return 0
        count = 0
        for image in view.get("views", ()):
            stamp, bgr = image.get("frame_ts"), image.get("bgr")
            if not valid_bgr(bgr) or not 0 <= scene.timestamp - stamp <= 1.5:
                continue
            proof = paired_scene(image)
            if (
                not journeys._scene(proof)
                or proof.channel != 3
                or proof.epoch != scene.epoch
                or proof.timestamp != stamp
                or proof.sequence > scene.sequence
            ):
                continue
            labels = [d for d in proof.detections if d.alias == alias]
            if len(labels) != 1:
                continue
            pad = service.channel_crop_collector._cfg.crop_pad_px
            x1, y1, x2, y2 = labels[0].bbox
            box = (
                max(0, x1 - pad),
                max(0, y1 - pad),
                min(proof.bgr.shape[1], x2 + pad),
                min(proof.bgr.shape[0], y2 + pad),
            )
            if any(
                d is not labels[0] and _overlap(d.bbox, box) for d in proof.detections
            ) or not np.array_equal(bgr, proof.bgr[box[1] : box[3], box[0] : box[2]]):
                continue
            count += journeys._retain(
                journey,
                Observation(
                    Stage.C3_HISTORY,
                    alias,
                    (3, scene.epoch, "ready_history", proof.sequence),
                    stamp,
                    bgr.copy(),
                    f"exact_c3_anchor:{scene.sequence}",
                ),
            )
        return count
    except Exception:
        return 0


def retain_rolling_fall(journeys, rolling_buffer, paired_scene):
    """Reuse DropZoneBurstCollector.rolling_buffer including pre-trigger frames.

    paired_scene(buffered_frame) supplies original, complete detection evidence
    for that EXACT frame, including camera/tracker incarnation and generation.
    It must be a memory lookup, not inference or a wait. Legacy detected=True
    without such evidence is deliberately not enough to associate a piece.
    """
    count = 0
    try:
        frames = rolling_buffer.snapshot()
    except Exception:
        return count
    for frame in frames:
        try:
            scene = paired_scene(frame)
            if (
                isinstance(scene, Scene)
                and scene.timestamp == frame.timestamp
                and np.array_equal(scene.bgr, frame.raw)
            ):
                count += journeys.burst_frame(scene)
        except Exception:
            continue
    return count


@dataclass(frozen=True)
class HistoryReference:
    """Existing tracked_global_id history locator, not another piece identity."""

    alias: object
    tracked_global_id: int
    created_at: float


def retain_track_history(journeys, journey, detail, reference, paired_scene):
    """Reuse C4 sector snapshots/drop_zone_burst with exact scene evidence.

    A history ID or pre/post label alone cannot identify a physical piece.
    paired_scene takes the original record, not just its timestamp. Fall records
    still go through the one active exclusive crossing; settled records require
    an already-proven alias. Late/previous generations are omitted.
    """
    count = 0
    try:
        if (
            detail.get("global_id") != reference.tracked_global_id
            or detail.get("created_at") != reference.created_at
            or not journeys._owned(journey, reference.alias)
        ):
            return 0
        for segment in detail.get("segments", ()):
            if segment.get("source_role") != "carousel":
                continue
            for snap in segment.get("sector_snapshots", ()):
                scene = paired_scene(snap)
                if isinstance(scene, Scene) and scene.timestamp == snap.get(
                    "captured_ts"
                ):
                    count += journeys.observe(
                        journey, scene, reference.alias, Stage.SETTLED
                    )
        for record in detail.get("drop_zone_burst", ()):
            try:
                scene = paired_scene(record)
                if (
                    not record.get("detected")
                    or not isinstance(scene, Scene)
                    or scene.timestamp != record.get("timestamp")
                ):
                    continue
                if record.get("phase") == "pre":
                    count += journeys.burst_frame(scene)
                else:
                    count += journeys.observe(
                        journey, scene, reference.alias, Stage.LANDING
                    )
            except Exception:
                continue
    except Exception:
        pass
    return count


def brickognize_provider(gc=None):
    """Reuse the original multipart/JPEG request construction, exactly once."""
    from classification.brickognize import _classifyImages

    def call(request):
        return _classifyImages(
            gc,
            [o.bgr for o in request.images],
            piece_uuid=request.piece_uuid,
            dump_label="journey",
        )

    return call
