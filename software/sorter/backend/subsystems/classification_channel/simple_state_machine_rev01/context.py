import threading
from typing import Optional

import numpy as np

from classification.providers import DEFAULT_COLOR_PROVIDER, DEFAULT_MOLD_PROVIDER
from defs.known_object import (
    ClassificationAttempt,
    ClassificationAttemptStrategy,
    KnownObject,
)

from subsystems.classification_channel.crop_quality import CropQuality

from .rev01_config import Rev01Config, configFromDict


def _loadConfig() -> Rev01Config:
    try:
        from toml_config import getClassificationChannelRev01Config

        return configFromDict(getClassificationChannelRev01Config())
    except Exception:
        return Rev01Config()


class SimpleStateMachineRev01Context:
    """Mutable state shared across the rev01 state classes for one run."""

    def __init__(self) -> None:
        # Monotonically identifies the piece cycle that owns asynchronous
        # classification work.  reset() advances it before clearing the shared
        # fields, so a late worker from an abandoned cycle cannot publish into
        # the next piece's context.
        self.cycle_id: int = 0
        self.config: Rev01Config = _loadConfig()
        self.captured_crops: list[np.ndarray] = []
        self.captured_crop_timestamps: list[float] = []
        # Laplacian-variance sharpness of each captured crop (higher = sharper),
        # index-aligned with captured_crops. Informational only (logs + the
        # per-image `sharpness` field synced to Hive); selection uses
        # captured_crop_quality.
        self.captured_crop_sharpness: list[float] = []
        # Crop-quality metrics per captured crop, index-aligned with
        # captured_crops. Drives which burst frames ship to Brickognize
        # (crop_quality.selectBurstIndices) and the anchor/thumbnail choice.
        self.captured_crop_quality: list[CropQuality] = []
        # The subset actually submitted to Brickognize — selected by CAPTURING
        # when it spawns the classify thread, read by AWAITING_DISTRIBUTION when
        # it dumps the burst artifacts (the spawn/apply split spans two states).
        self.selected_captures: list[np.ndarray] = []
        # Per-attempt Brickognize record and the winning strategy, produced by the
        # classify thread's retry runner and applied onto the KnownObject when the
        # result lands (the spawn/apply split spans two states).
        self.classification_attempts: list[ClassificationAttempt] = []
        self.classification_strategy: Optional[ClassificationAttemptStrategy] = None
        self.last_capture_frame_ts: float = 0.0
        self.capturing_started_at: float = 0.0
        self.rotating_started_at: float = 0.0
        self.classify_started_at: float = 0.0
        self.discharging_started_at: float = 0.0
        self.classification_result: object = None
        self.classification_error: Optional[str] = None
        # Main-thread ownership markers for the result -> distribution handoff.
        # MOVING_TO_PRECISE may complete this handoff while C4 is still moving;
        # AWAITING_DISTRIBUTION uses the same markers if recognition finishes
        # later, so neither result application nor transport placement can repeat.
        self.classification_applied: bool = False
        self.distribution_placed: bool = False
        # Which service actually produced the color/mold applied to this piece.
        # color_provider is only the configured provider if that provider
        # answered in time — a hosted-provider timeout falls back to Brickognize
        # and is recorded as such (see base._resolveHostedColor). hosted_color
        # carries the remote (color_id, color_name) to override Brickognize's,
        # and hosted_color_confidence that provider's own score for it — kept
        # apart from the mold score so neither is mistaken for the other.
        self.color_provider: str = DEFAULT_COLOR_PROVIDER
        self.mold_provider: str = DEFAULT_MOLD_PROVIDER
        self.hosted_color: Optional[tuple[str, str]] = None
        self.hosted_color_confidence: Optional[float] = None
        self.classify_thread: Optional[threading.Thread] = None
        self.classify_lock = threading.Lock()
        self.known_object: Optional[KnownObject] = None
        self.owned_upstream_view: dict | None = None
        # Absolute indexed C4 route for this piece. IDLE admits a piece only at a
        # pocket boundary; MOVING_TO_PRECISE derives both downstream targets from
        # that origin so per-move rounding and camera COM estimates cannot drift.
        self.c4_cycle_start_sector: Optional[int] = None
        self.c4_safe_staging_target_steps: Optional[int] = None
        self.c4_exit_target_steps: Optional[int] = None
        # Causal C4 handoff evidence. MOVING_TO_PRECISE records a settled frame
        # proving the piece remains on C4 at the indexed safe staging point.
        # DISCHARGING refuses to move or credit a piece without that link.
        self.precise_staged: bool = False
        self.precise_staged_frame_ts: float = 0.0
        self.discharge_armed_frame_ts: float = 0.0
        # A recoverable pre-discharge failure hands the same owned piece to the
        # existing bottom-reject flow.  Keeping the request on the shared cycle
        # context lets the state transition reuse one recovery implementation
        # instead of duplicating motion and distribution ownership logic.
        self.auto_reject_reason: Optional[str] = None
        self.auto_reject_multi_piece: bool = False
        # Latched True once >=2 pieces are confirmed on the channel (over several
        # distinct frames) during a cycle. A multi-feed: classification can't be
        # trusted, so the piece is routed to MISC and the discharge clears every
        # piece off. Debounced via observeMultiFeed so a single noisy frame (one
        # piece split into two boxes, or a spurious detection) can't trip it.
        self.multi_feed_detected: bool = False
        self._multi_feed_streak: int = 0
        self._multi_feed_last_ts: float = -1.0

    def observeMultiFeed(self, n_pieces: int, frame_ts: float, threshold: int) -> bool:
        # Count consecutive DISTINCT frames with >=2 on-channel pieces; latch
        # only after the streak reaches the threshold. Dedup by frame ts because
        # the state machine ticks faster than perception produces frames, so the
        # same slot is read many times — counting ticks would let one frame
        # inflate the streak. Returns True only on the tick the latch first trips.
        if self.multi_feed_detected:
            return False
        if frame_ts == self._multi_feed_last_ts:
            return False
        self._multi_feed_last_ts = frame_ts
        if n_pieces >= 2:
            self._multi_feed_streak += 1
        else:
            self._multi_feed_streak = 0
        if self._multi_feed_streak >= max(1, int(threshold)):
            self.multi_feed_detected = True
            return True
        return False

    def reset(self) -> None:
        # Classification workers commit under this same lock.  Advancing the
        # generation and clearing all cycle-owned fields is therefore atomic
        # from their point of view.
        with self.classify_lock:
            self.cycle_id += 1
            self.config = _loadConfig()
            self.captured_crops = []
            self.captured_crop_timestamps = []
            self.captured_crop_sharpness = []
            self.captured_crop_quality = []
            self.selected_captures = []
            self.classification_attempts = []
            self.classification_strategy = None
            self.last_capture_frame_ts = 0.0
            self.capturing_started_at = 0.0
            self.rotating_started_at = 0.0
            self.classify_started_at = 0.0
            self.discharging_started_at = 0.0
            self.classification_result = None
            self.classification_error = None
            self.classification_applied = False
            self.distribution_placed = False
            self.color_provider = DEFAULT_COLOR_PROVIDER
            self.mold_provider = DEFAULT_MOLD_PROVIDER
            self.hosted_color = None
            self.hosted_color_confidence = None
            self.classify_thread = None
            self.known_object = None
            self.owned_upstream_view = None
            self.c4_cycle_start_sector = None
            self.c4_safe_staging_target_steps = None
            self.c4_exit_target_steps = None
            self.precise_staged = False
            self.precise_staged_frame_ts = 0.0
            self.discharge_armed_frame_ts = 0.0
            self.auto_reject_reason = None
            self.auto_reject_multi_piece = False
            self.multi_feed_detected = False
            self._multi_feed_streak = 0
            self._multi_feed_last_ts = -1.0
