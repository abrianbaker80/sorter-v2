"""C4 index positioning only. No pieces, tracks, gates, or pocket-ledger writes.

The caller owns motion admission and the existing hardware fault/stop path.
Only a ConfirmedIndex can authorize its later FIFO boundary commit. A process
restart requires an explicit bind to the caller's boundary; a camera phase can
identify a pocket modulo ten, never the number of completed revolutions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import tempfile
import time
from typing import Callable, Protocol
from uuid import uuid4


def error_deg(target: float, actual: float) -> float:
    return (target - actual + 180.0) % 360.0 - 180.0


class PositionError(RuntimeError):
    pass


@dataclass(frozen=True)
class MarkerSample:
    epoch: str
    sequence: int
    captured_ns: int  # camera-host monotonic clock; never compared to host time
    received_mono: float
    phase_deg: float  # dashed-assigned, fitted rotor phase, image-clockwise
    geometry: str


@dataclass(frozen=True)
class FrameFence:
    epoch: str
    sequence: int
    captured_ns: int


class MarkerSource(Protocol):
    def fence(self) -> FrameFence: ...
    def sample(self) -> MarkerSample | None: ...


class IndexMotor(Protocol):
    def coordinate_identity(self) -> str: ...
    def stationary_token(self) -> object: ...
    def check_token(self, token: object) -> None: ...
    def start(self, degrees: float, speed: int, token: object) -> object: ...
    def complete(self, receipt: object) -> bool: ...


@dataclass(frozen=True)
class MarkerMapping:
    zero_phase_deg: float
    motor_sign: int
    geometry: str
    motor_coordinates: str
    reference: str  # operator's physical marker/pocket alignment description
    version: int = 1
    pocket_count: int = 10
    pocket_zero_station: int = 6

    def __post_init__(self):
        if (
            self.version != 1
            or self.pocket_count != 10
            or self.pocket_zero_station != 6
            or not math.isfinite(self.zero_phase_deg)
            or not 0 <= self.zero_phase_deg < 360
            or type(self.motor_sign) is not int
            or self.motor_sign not in (-1, 1)
            or not self.geometry
            or not self.motor_coordinates
            or not self.reference.strip()
        ):
            raise ValueError("invalid ten-pocket marker mapping")

    def phase(self, boundary: int) -> float:
        if type(boundary) is not int:
            raise ValueError("boundary must be an integer")
        return (self.zero_phase_deg + (boundary % 10) * 36.0) % 360.0

    def save_new(self, path: Path) -> None:
        """Atomic, no-clobber publication; recalibration cannot silently rebase owners."""
        payload = asdict(self)
        encoded = json.dumps(payload, sort_keys=True).encode()
        document = json.dumps(
            {"mapping": payload, "sha256": hashlib.sha256(encoded).hexdigest()},
            indent=2,
        )
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
        try:
            with os.fdopen(fd, "w", encoding="utf8") as stream:
                stream.write(document)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, path)  # fails if a persisted mapping already exists
        finally:
            os.unlink(temporary)

    @classmethod
    def load(cls, path: Path, geometry: str, motor_coordinates: str) -> MarkerMapping:
        document = json.loads(Path(path).read_text(encoding="utf8"))
        encoded = json.dumps(document["mapping"], sort_keys=True).encode()
        if hashlib.sha256(encoded).hexdigest() != document["sha256"]:
            raise PositionError("marker mapping checksum mismatch")
        mapping = cls(**document["mapping"])
        if (
            mapping.geometry != geometry
            or mapping.motor_coordinates != motor_coordinates
        ):
            raise PositionError(
                "camera/rotor or motor coordinates changed; mapping is invalid"
            )
        return mapping


@dataclass(frozen=True)
class PositionLimits:
    tolerance_deg: float = 0.5
    stable_spread_deg: float = 0.25
    settle_s: float = 0.20
    stable_span_s: float = 0.10
    max_sample_age_s: float = 0.5
    timeout_s: float = 8.0
    max_corrections: int = 2
    max_correction_deg: float = 4.0
    max_total_correction_deg: float = 6.0

    def __post_init__(self):
        values = (
            self.tolerance_deg,
            self.stable_spread_deg,
            self.settle_s,
            self.stable_span_s,
            self.max_sample_age_s,
            self.timeout_s,
            self.max_correction_deg,
            self.max_total_correction_deg,
        )
        if (
            any(not math.isfinite(v) or v <= 0 for v in values)
            or not 0 < self.stable_spread_deg <= self.tolerance_deg <= 1
            or not self.tolerance_deg < self.max_correction_deg <= 4
            or not self.max_correction_deg <= self.max_total_correction_deg <= 6
            or type(self.max_corrections) is not int
            or not 0 <= self.max_corrections <= 2
        ):
            raise ValueError("invalid bounded positioning limits")


class StableMarkers:
    """Three distinct source captures strictly after the stopped-camera fence."""

    def __init__(self, fence: FrameFence, geometry: str, limits: PositionLimits):
        self.fence = fence
        self.geometry = geometry
        self.limits = limits
        self.samples: list[MarkerSample] = []
        self.last_sequence = fence.sequence
        self.last_capture = fence.captured_ns

    def add(self, sample: MarkerSample | None, now: float) -> MarkerSample | None:
        if sample is None:
            self.samples.clear()
            return None
        if sample.epoch != self.fence.epoch or sample.geometry != self.geometry:
            raise PositionError("camera epoch or geometry changed during positioning")
        if (
            sample.sequence <= self.last_sequence
            or sample.captured_ns <= self.last_capture
        ):
            return None
        self.last_sequence, self.last_capture = sample.sequence, sample.captured_ns
        if (
            not math.isfinite(sample.phase_deg)
            or not 0 <= sample.phase_deg < 360
            or not 0 <= now - sample.received_mono <= self.limits.max_sample_age_s
        ):
            self.samples.clear()
            return None
        if sample.captured_ns <= self.fence.captured_ns + self.limits.settle_s * 1e9:
            return None
        # Each observation passed freshness at its own validation above.
        # Retain that stationary history across a continuous stream instead of
        # expiring it again while the next image is being decoded/fitted. A gap
        # in source captures still breaks the evidence chain; old observations
        # cannot bridge camera outages or sparse polling to manufacture stability.
        if self.samples and (
            sample.captured_ns - self.samples[-1].captured_ns
        ) / 1e9 > self.limits.max_sample_age_s:
            self.samples.clear()
        self.samples.append(sample)
        offsets = [
            error_deg(s.phase_deg, self.samples[0].phase_deg) for s in self.samples
        ]
        if max(offsets) - min(offsets) > self.limits.stable_spread_deg:
            self.samples = [sample]
            return None
        # Keep the start of the stable interval. Last-three truncation could
        # never span 100ms when called at the camera's 30/60 FPS rate.
        if len(self.samples) > 32:
            self.samples = self.samples[:1] + self.samples[-31:]
        if (
            len(self.samples) < 3
            or (sample.captured_ns - self.samples[0].captured_ns) / 1e9
            < self.limits.stable_span_s
        ):
            return None
        offsets = [
            error_deg(s.phase_deg, self.samples[0].phase_deg) for s in self.samples
        ]
        phase = (self.samples[0].phase_deg + statistics.median(offsets)) % 360
        return MarkerSample(
            sample.epoch,
            sample.sequence,
            sample.captured_ns,
            sample.received_mono,
            phase,
            sample.geometry,
        )


class SourceContinuityMarkers(StableMarkers):
    """Stopped-position evidence follows source captures, not poll timing.

    Older valid fits support stability across skipped captures. Only a fresh
    newest fit may confirm position, and source discontinuity still resets the
    window. This is the same rule used by the frozen startup bootstrap.
    """

    def __init__(self, fence, geometry, limits, *, capture_period_s=None):
        super().__init__(fence, geometry, limits)
        self.capture_period_s = capture_period_s
        self.last_reason = None
        self.last_reset_reason = None

    def add(self, sample: MarkerSample | None, now: float) -> MarkerSample | None:
        self.last_reason = None
        self.last_reset_reason = None
        if sample is None:
            self.samples.clear()
            self.last_reason = self.last_reset_reason = "no_marker_fit"
            return None
        if sample.epoch != self.fence.epoch or sample.geometry != self.geometry:
            self.samples.clear()
            self.last_reason = self.last_reset_reason = "camera_epoch_or_geometry_changed"
            raise PositionError("camera epoch or geometry changed during positioning")
        if sample.sequence <= self.fence.sequence or sample.captured_ns <= self.fence.captured_ns:
            if self.samples:
                self.samples.clear()
                self.last_reset_reason = "before_source_frame_fence"
            self.last_reason = "before_source_frame_fence"
            return None
        if sample.sequence <= self.last_sequence or sample.captured_ns <= self.last_capture:
            self.samples.clear()
            self.last_sequence, self.last_capture = sample.sequence, sample.captured_ns
            self.last_reason = self.last_reset_reason = "source_sequence_or_capture_not_advancing"
            return None
        if self.samples:
            previous = self.samples[-1]
            elapsed = (sample.captured_ns - previous.captured_ns) / 1e9
            skipped = sample.sequence - previous.sequence - 1
            expected = skipped * (self.capture_period_s or 0.0)
            if elapsed - expected > self.limits.max_sample_age_s:
                self.samples.clear()
                self.last_reset_reason = "source_capture_discontinuity"
        self.last_sequence, self.last_capture = sample.sequence, sample.captured_ns
        age = now - sample.received_mono
        if not math.isfinite(age) or age < 0:
            self.last_reason = "invalid_or_future_validation_age"
            return None
        if not math.isfinite(sample.phase_deg) or not 0 <= sample.phase_deg < 360:
            self.samples.clear()
            self.last_reason = self.last_reset_reason = "invalid_marker_phase"
            return None
        if sample.captured_ns <= self.fence.captured_ns + self.limits.settle_s * 1e9:
            self.last_reason = "inside_post_fence_settle"
            return None
        self.samples.append(sample)
        offsets = [error_deg(s.phase_deg, self.samples[0].phase_deg) for s in self.samples]
        if max(offsets) - min(offsets) > self.limits.stable_spread_deg:
            self.samples = [sample]
            self.last_reason = self.last_reset_reason = "unstable_marker_phase"
            return None
        if len(self.samples) > 32:
            self.samples = self.samples[:1] + self.samples[-31:]
        if (
            age > self.limits.max_sample_age_s
            or len(self.samples) < 3
            or (sample.captured_ns - self.samples[0].captured_ns) / 1e9
            < self.limits.stable_span_s
        ):
            self.last_reason = (
                "stale_newest_history_retained" if age > self.limits.max_sample_age_s
                else "insufficient_stable_samples" if len(self.samples) < 3
                else "insufficient_stable_span"
            )
            return None
        offsets = [error_deg(s.phase_deg, self.samples[0].phase_deg) for s in self.samples]
        phase = (self.samples[0].phase_deg + statistics.median(offsets)) % 360
        self.last_reason = "stable_fresh_fit"
        return MarkerSample(
            sample.epoch,
            sample.sequence,
            sample.captured_ns,
            sample.received_mono,
            phase,
            sample.geometry,
        )


@dataclass(frozen=True)
class ConfirmedIndex:
    boundary: int
    phase_deg: float
    residual_deg: float
    corrections: int
    source_epoch: str
    source_sequence: int
    source_capture_ns: int


def calibrate_mapping(
    motor: IndexMotor,
    source: MarkerSource,
    *,
    geometry: str,
    known_boundary: int,
    motor_sign: int,
    physical_reference: str,
    clock=time.monotonic,
    wait=time.sleep,
) -> MarkerMapping:
    """Measure a stationary, explicitly identified physical boundary; no motion.

    At boundary zero the physically designated pocket 0 must be at P6. The
    reference describes that alignment and the confirmed clockwise motor sign.
    This function cannot discover pocket labels or sign from stationary pixels.
    Persist the returned mapping with save_new only after that physical check.
    """
    if type(known_boundary) is not int:
        raise ValueError("known boundary must be an integer")
    motor_coordinates = motor.coordinate_identity()
    MarkerMapping(0, motor_sign, geometry, motor_coordinates, physical_reference)
    limits = PositionLimits()
    token = motor.stationary_token()
    window = StableMarkers(source.fence(), geometry, limits)
    started = clock()
    while clock() - started <= limits.timeout_s:
        motor.check_token(token)
        sample = window.add(source.sample(), clock())
        motor.check_token(token)
        if motor.coordinate_identity() != motor_coordinates:
            raise PositionError("motor coordinates changed during calibration")
        if clock() - started > limits.timeout_s:
            raise PositionError("stationary marker calibration timed out")
        if sample is not None:
            return MarkerMapping(
                (sample.phase_deg - 36 * (known_boundary % 10)) % 360,
                motor_sign,
                geometry,
                motor_coordinates,
                physical_reference,
            )
        wait(0.05)
    raise PositionError("stationary marker calibration timed out")


class MarkerPositioner:
    """Single-owner, tick-driven index API for the existing tracked motor adapter.

    begin_bind(boundary) -> poll() -> ConfirmedIndex establishes position only.
    request_index(boundary + 1, speed) -> poll() -> ConfirmedIndex performs one
    normal clockwise index, then bounded corrections. poll returns None while
    pending. Caller must consume each result once and retains all FIFO ownership.
    Every failure latches this instance and invokes the existing fault callback.
    Never retry an uncertain accepted command with a new positioner instance.
    """

    def __init__(
        self,
        motor: IndexMotor,
        source: MarkerSource,
        mapping: MarkerMapping,
        on_fault: Callable[[str], None],
        *,
        limits: PositionLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
        diagnostic_sink: Callable[[dict], None] | None = None,
    ):
        self.motor, self.source, self.mapping = motor, source, mapping
        if motor.coordinate_identity() != mapping.motor_coordinates:
            raise PositionError("motor coordinates differ from persisted mapping")
        self.on_fault, self.clock = on_fault, clock
        self.limits = limits or PositionLimits()
        self.boundary: int | None = None
        self.pending: int | None = None
        self.fault: str | None = None
        self._receipt = None
        self._token = None
        self._window = None
        self._binding = False
        self._preparing = False
        self._corrections = 0
        self._total_correction = 0.0
        self._epoch = None
        self._last_error = None
        if diagnostic_sink is None:
            logger = getattr(getattr(getattr(motor, "stepper", None), "_gc", None), "logger", None)
            if callable(getattr(logger, "_log", None)):
                diagnostic_sink = lambda row: logger._log(
                    "INFO", "[C4-POSITIONER] " + json.dumps(row, separators=(",", ":"))
                )
        self._diagnostic_sink = diagnostic_sink
        self._diagnostic_started = False
        self._diagnostic_active = False
        self._diagnostic_id = None
        self._first_post_fence_frame = None

    def _record(self, event: str, **fields):
        """Best-effort passive evidence; never participates in control decisions."""
        if not self._diagnostic_active or self._diagnostic_sink is None:
            return
        try:
            self._diagnostic_sink({
                "attempt_id": self._diagnostic_id,
                "event": event,
                "recorded_wall_s": time.time(),
                "bound_boundary": self.boundary,
                "pending_boundary": self.pending,
                **fields,
            })
        except Exception:
            pass

    def _history_evidence(self):
        return [
            {"sequence": s.sequence, "source_capture_monotonic_ns": s.captured_ns,
             "phase_deg": s.phase_deg}
            for s in getattr(self._window, "samples", ())
        ]

    def _record_observation(self, raw, validated_at, accepted):
        if not self._diagnostic_active:
            return
        source_row = getattr(self.source, "last_observation", None)
        source_row = source_row if isinstance(source_row, dict) else {}
        if raw is not None and (
            source_row.get("sequence") != raw.sequence
            or source_row.get("source_capture_monotonic_ns") != raw.captured_ns
        ):
            source_row = {}
        epoch = raw.epoch if raw is not None else source_row.get("epoch")
        sequence = raw.sequence if raw is not None else source_row.get("sequence")
        capture = raw.captured_ns if raw is not None else source_row.get("source_capture_monotonic_ns")
        retrieval = raw.received_mono if raw is not None else source_row.get("retrieval_monotonic_s")
        phase = raw.phase_deg if raw is not None else source_row.get("phase_deg")
        fence = self._window.fence
        past_fence = (
            epoch == fence.epoch and sequence is not None and capture is not None
            and sequence > fence.sequence
            and capture > fence.captured_ns + self.limits.settle_s * 1e9
        )
        stage = "post_motion" if self._receipt is not None else "pre_motion"
        evidence = {
            "stage": stage,
            "source_epoch": epoch,
            "source_sequence": sequence,
            "source_capture_monotonic_ns": capture,
            "retrieval_monotonic_s": retrieval,
            "retrieval_wall_s": source_row.get("retrieval_wall_s"),
            "validation_monotonic_s": validated_at,
            "age_s": validated_at - retrieval if retrieval is not None else None,
            "raw_phase_deg": phase,
            "stable_phase_deg": accepted.phase_deg if accepted is not None else None,
            "past_stopped_frame_fence": past_fence,
            "window_reason": getattr(self._window, "last_reason", None),
            "history_reset_reason": getattr(self._window, "last_reset_reason", None),
            "history": self._history_evidence(),
        }
        if stage == "post_motion" and past_fence and self._first_post_fence_frame is None:
            self._first_post_fence_frame = {k: evidence[k] for k in (
                "source_epoch", "source_sequence", "source_capture_monotonic_ns",
                "retrieval_monotonic_s", "raw_phase_deg",
            )}
            self._record("first_post_fence_frame", **self._first_post_fence_frame)
        self._record("marker_observation", **evidence)

    def _fail(self, exc: Exception):
        self.fault = str(exc)
        try:
            self._record("failure", reason=self.fault,
                         first_post_fence_frame=self._first_post_fence_frame,
                         history=self._history_evidence())
        except Exception:
            pass
        self._diagnostic_active = False
        try:
            self.on_fault(self.fault)
        finally:
            raise PositionError(self.fault) from exc

    def _available(self):
        if self.fault is not None or self.pending is not None:
            raise PositionError("positioner faulted or already has an owned request")

    def _observe_stopped(self):
        self._token = self.motor.stationary_token()
        if self.motor.coordinate_identity() != self.mapping.motor_coordinates:
            raise PositionError("motor coordinates differ from persisted mapping")
        fence_request_wall = time.time()
        fence_request_monotonic = time.monotonic()
        fence = self.source.fence()
        self.motor.check_token(self._token)
        if self._epoch is not None and fence.epoch != self._epoch:
            raise PositionError("camera restarted during index")
        self._epoch = fence.epoch
        self._window = SourceContinuityMarkers(
            fence,
            self.mapping.geometry,
            self.limits,
            capture_period_s=getattr(self.source, "capture_period_s", None),
        )
        self._record("stopped_frame_fence",
                     stage="post_motion" if self._receipt is not None else "pre_motion",
                     fence_request_wall_s=fence_request_wall,
                     fence_request_monotonic_s=fence_request_monotonic,
                     fence={"epoch": fence.epoch, "sequence": fence.sequence,
                            "source_capture_monotonic_ns": fence.captured_ns},
                     source_health=getattr(self.source, "last_fence_health", None),
                     capture_period_s=getattr(self.source, "capture_period_s", None))

    def begin_bind(self, boundary: int) -> None:
        self._available()
        self.mapping.phase(boundary)
        if self.boundary is not None:
            raise PositionError("already bound; cannot rebase a running positioner")
        self.pending = boundary
        self._binding = True
        self._started = self.clock()
        try:
            self._observe_stopped()
        except Exception as exc:
            self._fail(exc)

    def request_index(self, boundary: int, speed: int) -> None:
        self._available()
        if (
            self.boundary is None
            or type(boundary) is not int
            or boundary != self.boundary + 1
        ):
            raise PositionError("normal index must be exactly the next bound boundary")
        if type(speed) is not int or speed < 16:
            raise ValueError("speed must be a positive configured motor speed")
        self.pending, self._speed = boundary, speed
        self._binding, self._preparing = False, True
        self._corrections, self._total_correction = 0, 0.0
        self._last_error = None
        self._started = self.clock()
        if not self._diagnostic_started and self._diagnostic_sink is not None:
            self._diagnostic_started = self._diagnostic_active = True
            self._diagnostic_id = uuid4().hex
            self._first_post_fence_frame = None
            self._record("index_requested", request_start_monotonic_s=self._started,
                         request_start_wall_s=time.time(), speed=speed)
        try:
            # Verify idle custody of the motor, then re-observe the old physical
            # boundary before sending approximate travel. No stale bind reuse.
            self.motor.check_token(self._token)
            self._observe_stopped()
        except Exception as exc:
            self._fail(exc)

    def begin_target_trim(self, boundary: int, speed: int) -> None:
        """Maintenance only: observe/trim a caller-attributed target, then bind.

        Requires an unbound instance and external attribution of this physical
        target. Never sends an index-sized move or rebases a bound instance.
        The same correction envelope, deadline and confirmation rules apply.
        """
        self._available()
        self.mapping.phase(boundary)
        if self.boundary is not None:
            raise PositionError("already bound; cannot trim-bind a running positioner")
        if type(speed) is not int or speed < 16:
            raise ValueError("speed must be a positive configured motor speed")
        self.pending, self._speed = boundary, speed
        self._binding, self._preparing = False, False
        self._corrections, self._total_correction = 0, 0.0
        self._last_error = None
        self._started = self.clock()
        try:
            self._observe_stopped()
        except Exception as exc:
            self._fail(exc)

    def _move(self, degrees: float):
        requested_degrees = degrees * self.mapping.motor_sign
        self._record("motor_request_start", request_start_wall_s=time.time(),
                     request_start_monotonic_s=time.monotonic(),
                     requested_degrees=requested_degrees)
        self._receipt = self.motor.start(
            requested_degrees, self._speed, self._token
        )
        start = getattr(self._receipt, "start_position", None)
        target = getattr(self._receipt, "target_position", None)
        steps = ((target - start + 2**31) % 2**32) - 2**31 if start is not None and target is not None else None
        self._record("motor_receipt", motor_id=getattr(self._receipt, "motor_id", None),
                     generation=getattr(self._receipt, "generation", None),
                     start_position=start, requested_steps=steps,
                     target_position=target, requested_degrees=requested_degrees)
        self._window = None

    def poll(self) -> ConfirmedIndex | None:
        if self.fault is not None:
            raise PositionError(self.fault)
        if self.pending is None:
            return None
        try:
            if self.clock() - self._started > self.limits.timeout_s:
                raise PositionError("marker index deadline exceeded")
            if self._receipt is not None:
                if not self.motor.complete(self._receipt):
                    return None
                # Keep the receipt and recheck it on every observation tick;
                # an intervening stop/replacement must invalidate confirmation.
                if self._window is None:
                    self._record("motor_completion_verified",
                                 completion_wall_s=time.time(),
                                 completion_monotonic_s=time.monotonic(),
                                 stopped=True,
                                 final_reported_step_counter=getattr(self._receipt, "target_position", None),
                                 counter_source="tracked_move_complete verified MCU counter")
                    self._observe_stopped()
            self.motor.check_token(self._token)
            raw = self.source.sample()
            validated_at = self.clock()
            try:
                sample = self._window.add(raw, validated_at)
            except Exception:
                try:
                    self._record_observation(raw, validated_at, None)
                except Exception:
                    pass
                raise
            try:
                self._record_observation(raw, validated_at, sample)
            except Exception:
                pass
            self.motor.check_token(self._token)
            if sample is None:
                if self.clock() - self._started > self.limits.timeout_s:
                    raise PositionError("marker index deadline exceeded during observation")
                return None
            target = self.boundary if self._preparing else self.pending
            residual = error_deg(self.mapping.phase(target), sample.phase_deg)
            if self._preparing:
                if self.clock() - self._started > self.limits.timeout_s:
                    raise PositionError("marker index deadline exceeded during observation")
                if abs(residual) > self.limits.tolerance_deg:
                    raise PositionError("idle rotor moved off the bound marker target")
                self._record("pre_motion_fit", source_epoch=sample.epoch,
                             source_sequence=sample.sequence,
                             source_capture_monotonic_ns=sample.captured_ns,
                             retrieval_monotonic_s=sample.received_mono,
                             retrieval_wall_s=(getattr(self.source, "last_observation", None) or {}).get("retrieval_wall_s"),
                             phase_deg=sample.phase_deg, residual_deg=residual)
                self._preparing = False
                self._move(36.0 + residual)
                return None
            if (
                self._last_error is not None
                and abs(residual) >= self._last_error - 0.1
            ):
                raise PositionError(
                    f"marker correction did not converge: residual {residual:.3f} degrees"
                )
            if abs(residual) <= self.limits.tolerance_deg:
                # Retrieval on this clock follows source capture, so a frame
                # retrieved by the deadline was necessarily captured in time.
                if sample.received_mono > self._started + self.limits.timeout_s:
                    raise PositionError("marker index deadline exceeded during observation")
                result = ConfirmedIndex(
                    self.pending,
                    sample.phase_deg,
                    residual,
                    self._corrections,
                    sample.epoch,
                    sample.sequence,
                    sample.captured_ns,
                )
                self.boundary, self.pending = self.pending, None
                self._receipt = None
                self._record("confirmed", source_epoch=sample.epoch,
                             source_sequence=sample.sequence,
                             source_capture_monotonic_ns=sample.captured_ns,
                             phase_deg=sample.phase_deg, residual_deg=residual,
                             first_post_fence_frame=self._first_post_fence_frame,
                             history=self._history_evidence())
                self._diagnostic_active = False
                return result
            if self.clock() - self._started > self.limits.timeout_s:
                raise PositionError("marker index deadline exceeded during observation")
            if (
                self._binding
                or self._corrections >= self.limits.max_corrections
                or abs(residual) > self.limits.max_correction_deg
                or self._total_correction + abs(residual)
                > self.limits.max_total_correction_deg
            ):
                raise PositionError(
                    f"marker target not converged: residual {residual:.3f} degrees"
                )
            # Signed physical trim of the SAME pending target. Logical indexing
            # stays forward-only; only final marker confirmation changes boundary.
            self._last_error = abs(residual)
            self._total_correction += abs(residual)
            self._corrections += 1
            self._move(residual)
            return None
        except Exception as exc:
            self._fail(exc)


class TrackedStepperIndexMotor:
    """Wrap the deployed StepperMotor without changing its hardware fault rules."""

    def __init__(self, stepper, platter):
        self.stepper, self.platter = stepper, platter

    def coordinate_identity(self):
        s, p = self.stepper, self.platter
        return hashlib.sha256(
            json.dumps(
                {
                    "direction_inverted": s.direction_inverted,
                    "motor_steps_per_revolution": p.motor_steps_per_revolution,
                    "microsteps": p.microsteps,
                    "gear_ratio": p.gear_ratio,
                    "driver_microsteps": s._microsteps,
                    "driver_steps_per_revolution": s.steps_per_revolution,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def stationary_token(self):
        s = self.stepper
        with s._motion_lock:
            if (
                s.software_disabled
                or not s.enabled
                or s.stalled
                or not s.stationary_verified()
            ):
                raise PositionError("C4 motor disabled, stalled, or not stationary")
            return (
                id(s),
                s._motion_generation,
                int(s.position),
                self.coordinate_identity(),
            )

    def check_token(self, token):
        if self.stationary_token() != token:
            raise PositionError("C4 motor command ownership changed")

    def start(self, degrees, speed, token):
        s = self.stepper
        with s._motion_lock:
            self.check_token(token)
            steps = self.platter.output_degrees_to_motor_microsteps(degrees)
            if not steps:
                raise PositionError("marker correction quantized to zero")
            return s.start_tracked_move(steps, speed)

    def complete(self, receipt):
        return self.stepper.tracked_move_complete(receipt)
