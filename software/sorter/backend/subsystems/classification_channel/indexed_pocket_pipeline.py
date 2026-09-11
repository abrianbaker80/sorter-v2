from __future__ import annotations

import time
from enum import Enum

from defs.known_object import ClassificationStatus, KnownObject, PieceStage, RecognitionImage
from subsystems.bus import StationId
from subsystems.classification_channel import crop_quality
from subsystems.classification_channel.five_sector_platter import C4FiveSectorPlatter
from subsystems.classification_channel.pocket_ledger import (
    PocketLedger,
    PocketLoad,
    PocketRoute,
)
from subsystems.classification_channel.simple_state_machine_rev01.base import Rev01BaseState
from subsystems.classification_channel.simple_state_machine_rev01.constants import (
    C4_EXIT_SECTOR_ADVANCE,
    C4_INDEXED_ROUTE_SECTOR_COUNT,
    C4_TRAVEL_SIGN,
)
from subsystems.classification_channel.simple_state_machine_rev01.context import (
    SimpleStateMachineRev01Context,
)


LOG_TAG = "[C4-INDEXED]"
_ALIGNMENT_TOLERANCE_STEPS = 1
_CAPTURE_ARRIVAL_TIMEOUT_S = 1.5
_RELEASE_CONFIRM_TIMEOUT_S = 3.0
_MOTION_TIMEOUT_S = 5.0
_DRAIN_IDLE_S = 8.0
_DRAIN_GATE_GUARD_S = 1.5


class _Phase(str, Enum):
    WAIT_INTAKE = "wait_intake"
    WAIT_ARRIVAL = "wait_arrival"
    CAPTURE_TAIL = "capture_tail"
    INDEX_ONE_POCKET = "index_one_pocket"


class IndexedPocketPipeline:
    """Ten-pocket C4 bucket brigade with reject as the default route.

    Pocket identity comes only from acknowledged absolute stepper indexes. The
    camera supplies an intake crop and a debounced count; it never steers C4 or
    re-associates a piece after admission.
    """

    def __init__(self, irl, irl_config, gc, shared, transport, vision, event_queue):
        self.irl = irl
        self.irl_config = irl_config
        self.gc = gc
        self.shared = shared
        self.transport = transport
        self.logger = gc.logger
        # This mode uses a C3 release attempt only to arm C4. A logical pocket
        # is admitted after fresh C4 intake evidence confirms the physical
        # handoff. The coordinator always owns a TickBus; enabling it here makes
        # both Go-to-Angle and this controller publish/read the handshake.
        gc.use_channel_bus = True
        self._deps = (irl, irl_config, gc, shared, transport, vision, event_queue)
        self._platter = C4FiveSectorPlatter.from_irl_config(irl_config)
        if self._platter.sector_count != C4_INDEXED_ROUTE_SECTOR_COUNT:
            raise ValueError(
                "indexed pocket mode requires the configured ten-pocket C4 rotor"
            )
        self._stepper = getattr(irl, "carousel_stepper", None)
        if self._stepper is None:
            raise ValueError("indexed pocket mode requires the C4 carousel stepper")
        self._config = SimpleStateMachineRev01Context().config
        self._travel_sign = 1 if C4_TRAVEL_SIGN >= 0.0 else -1
        self._phase = _Phase.WAIT_INTAKE
        position = int(self._stepper.position)
        boundary = self._platter.nearest_sector_index(position)
        target = self._platter.sector_position_microsteps(boundary)
        if abs(position - target) > _ALIGNMENT_TOLERANCE_STEPS:
            raise RuntimeError(
                "optical home did not leave C4 on an absolute pocket boundary "
                f"(position={position}, target={target})"
            )
        self._ledger = PocketLedger(
            sector_count=self._platter.sector_count,
            exit_advance=C4_EXIT_SECTOR_ADVANCE,
            travel_sign=self._travel_sign,
            boundary_index=boundary,
        )
        self._tail: PocketLoad | None = None
        self._last_release_mono = self._releaseTimestamp() or 0.0
        self._arrival_armed_at_mono = 0.0
        self._arrival_armed_at_wall = 0.0
        self._arrival_last_frame_ts = 0.0
        self._arrival_presence_streak = 0
        self._arrival_samples: list[tuple[list, object]] = []
        self._last_intake_activity = time.monotonic()
        self._drain_armed_at = 0.0
        self._drain_active = False
        self._move_target_steps: int | None = None
        self._move_target_sector: int | None = None
        self._move_started_at = 0.0
        self._move_retries = 0
        self._paused_at_mono: float | None = None
        self._paused_at_wall: float | None = None
        self.last_progress_at = time.monotonic()
        self.logger.info(f"{LOG_TAG} absolute origin ready sector={boundary} position={position}")

    def phaseName(self) -> str:
        return self._phase.value

    def noteProgress(self) -> None:
        self.last_progress_at = time.monotonic()

    def step(self) -> None:
        now = time.monotonic()
        self._applyResults()
        if self._phase == _Phase.WAIT_INTAKE:
            self._waitIntake(now)
        elif self._phase == _Phase.WAIT_ARRIVAL:
            self._waitArrival(now)
        elif self._phase == _Phase.CAPTURE_TAIL:
            self._captureTail(now)
        else:
            self._indexOnePocket(now)

    # --------------------------------------------------------------- intake

    def _waitIntake(self, now: float) -> None:
        release = self._releaseTimestamp()
        if release is not None and release > self._last_release_mono:
            self._last_release_mono = release
            self._armArrival(now)
            return

        due = self._ledger.due_to_exit_on_next_index()
        if not self._ledger.can_admit:
            self._setGate(False, "indexed C4 at capacity")
            self._phase = _Phase.INDEX_ONE_POCKET
            return

        if self._drain_armed_at > 0.0:
            self._setGate(False, "guarding C3 before C4 drain index")
            if now - self._drain_armed_at >= _DRAIN_GATE_GUARD_S:
                self._drain_active = True
                self._phase = _Phase.INDEX_ONE_POCKET
            return

        # Start aiming the chute as soon as a load reaches the pre-exit pocket,
        # while C3 is free to prepare the following intake piece.
        if due is not None:
            self._prepareDueLoad(due)

        if self._ledger.loads and now - self._last_intake_activity >= _DRAIN_IDLE_S:
            self._drain_armed_at = now
            self._setGate(False, "arming idle C4 drain")
            return

        self._setGate(True, "indexed intake pocket available")

    def _armArrival(self, now: float) -> None:
        if not self._ledger.can_admit:
            raise RuntimeError("C3 release attempted into an unavailable C4 pocket")
        self._arrival_armed_at_mono = now
        self._arrival_armed_at_wall = time.time()
        self._arrival_last_frame_ts = 0.0
        self._arrival_presence_streak = 0
        self._arrival_samples = []
        self._phase = _Phase.WAIT_ARRIVAL
        self._setGate(False, "awaiting confirmed C4 intake arrival")
        self.noteProgress()

    def _clearArrivalArm(self) -> None:
        self._arrival_armed_at_mono = 0.0
        self._arrival_armed_at_wall = 0.0
        self._arrival_last_frame_ts = 0.0
        self._arrival_presence_streak = 0
        self._arrival_samples = []

    def _waitArrival(self, now: float) -> None:
        self._setGate(False, "awaiting confirmed C4 intake arrival")

        # A second release command while C4's gate is closed is a feeder race.
        # Consume its timestamp so it cannot arm a second logical pocket; actual
        # multi-piece evidence is still decided from the C4 camera below.
        release = self._releaseTimestamp()
        if release is not None and release > self._last_release_mono:
            self._last_release_mono = release
            self.logger.warning(
                f"{LOG_TAG} additional C3 release attempt while awaiting arrival"
            )

        sample = self._readDropPiecesAndFrame()
        drop_pieces, frame = sample if sample is not None else ([], None)
        frame_ts = float(getattr(frame, "timestamp", 0.0) or 0.0)
        fresh = (
            frame is not None
            and frame_ts > self._arrival_last_frame_ts
            and frame_ts > self._arrival_armed_at_wall
        )
        if fresh:
            self._arrival_last_frame_ts = frame_ts
            if drop_pieces:
                self._arrival_presence_streak += 1
                self._arrival_samples.append((list(drop_pieces), frame))
            else:
                self._arrival_presence_streak = 0
                self._arrival_samples = []

            required = max(1, int(self._config.presence_streak_to_start))
            if self._arrival_presence_streak >= required:
                samples = list(self._arrival_samples[-required:])
                self._clearArrivalArm()
                self._admit(now, initial_samples=samples)
                notifier = getattr(self.shared, "publish_piece_delivered", None)
                if callable(notifier):
                    notifier(
                        source=StationId.C3,
                        target=StationId.CLASSIFICATION,
                        delivered_at_mono=now,
                    )
                return

        if now - self._arrival_armed_at_mono >= _RELEASE_CONFIRM_TIMEOUT_S:
            self.logger.warning(
                f"{LOG_TAG} C3 release produced no confirmed C4 arrival; "
                "retrying without creating a pocket"
            )
            self._clearArrivalArm()
            self._phase = _Phase.WAIT_INTAKE
            self._setGate(True, "release unconfirmed; C3 retry allowed")
            self.noteProgress()

    def _admit(self, admitted_at: float, *, initial_samples=None) -> None:
        if not self._ledger.can_admit:
            raise RuntimeError("C3 delivered into an unavailable C4 intake pocket")
        worker = Rev01BaseState(*self._deps, SimpleStateMachineRev01Context())
        worker.ctx.reset()
        obj = KnownObject(
            stage=PieceStage.created,
            classification_status=ClassificationStatus.pending,
            first_carousel_seen_ts=time.time(),
            reject_on_routing_failure=True,
        )
        worker.ctx.known_object = obj
        worker.emitKnownObject()
        self._tail = self._ledger.admit(
            worker, admitted_at_mono=admitted_at
        )
        self._last_intake_activity = time.monotonic()
        self._drain_armed_at = 0.0
        self._drain_active = False
        self._phase = _Phase.CAPTURE_TAIL
        self._setGate(False, "capturing indexed intake pocket")
        if initial_samples:
            ctx = worker.ctx
            ctx.capturing_started_at = admitted_at
            for pieces, frame in initial_samples:
                frame_ts = float(getattr(frame, "timestamp", 0.0) or 0.0)
                if frame_ts > 0.0 and ctx.observeMultiFeed(
                    len(pieces), frame_ts, ctx.config.multi_feed_confirm_reads
                ):
                    ctx.multi_feed_detected = True
                self._captureDropCrop(worker, pieces, frame)
        self.noteProgress()
        self.logger.info(
            f"{LOG_TAG} admitted pocket={self._tail.pocket_id} piece={obj.uuid[:8]}"
        )

    # --------------------------------------------------------------- capture

    def _captureTail(self, now: float) -> None:
        self._setGate(False, "capturing indexed intake pocket")
        load = self._tail
        if load is None:
            raise RuntimeError("capture phase has no owned intake pocket")
        worker: Rev01BaseState = load.payload
        ctx = worker.ctx

        # A second C3 delivery while the gate is closed belongs to this same
        # physical pocket. Treat the pocket as a clump and reject it as one load.
        release = self._releaseTimestamp()
        if release is not None and release > self._last_release_mono:
            self._last_release_mono = release
            ctx.multi_feed_detected = True

        sample = self._readDropPiecesAndFrame()
        drop_pieces, frame = sample if sample is not None else ([], None)
        frame_ts = float(getattr(frame, "timestamp", 0.0) or 0.0)
        multi = ctx.multi_feed_detected
        if frame_ts > 0.0 and ctx.observeMultiFeed(
            len(drop_pieces), frame_ts, ctx.config.multi_feed_confirm_reads
        ):
            multi = True
        multi = multi or ctx.multi_feed_detected

        if drop_pieces and frame is not None:
            if ctx.capturing_started_at == 0.0:
                ctx.capturing_started_at = now
            self._captureDropCrop(worker, drop_pieces, frame)

        if multi:
            self._forceReject(load, "multiple pieces in one pocket", multi_piece=True)
            finished = True
        elif ctx.capturing_started_at > 0.0:
            done, _reason = worker.burstCaptureComplete(ctx, now)
            # The burst owns every valid crop accumulated for this pocket.  A
            # terminal detector blink must not discard that evidence; only a
            # current multi-object frame waits for the debounce to resolve.
            if done and len(drop_pieces) <= 1:
                ctx.classify_started_at = now
                worker.spawnClassifyThread(list(ctx.captured_crops))
                finished = True
            elif now - load.admitted_at_mono >= _CAPTURE_ARRIVAL_TIMEOUT_S:
                self._forceReject(load, "intake evidence vanished during capture")
                finished = True
            else:
                finished = False
        elif now - load.admitted_at_mono >= _CAPTURE_ARRIVAL_TIMEOUT_S:
            self._forceReject(load, "no intake crop before capture deadline")
            finished = True
        else:
            finished = False

        if finished:
            self._phase = _Phase.INDEX_ONE_POCKET
            self.noteProgress()

    def _readDropPiecesAndFrame(self):
        perception = getattr(self.gc, "perception_service", None)
        if perception is None or not hasattr(perception, "read_pieces_and_frame"):
            return None
        sample = perception.read_pieces_and_frame(4)
        if sample is None:
            return None
        pieces, frame = sample
        return [piece for piece in pieces if int(piece.zone_code) == 1], frame

    def _captureDropCrop(self, worker, pieces, frame) -> None:
        ctx = worker.ctx
        frame_ts = float(frame.timestamp)
        if frame.bgr is None or frame_ts <= ctx.last_capture_frame_ts:
            return
        piece = max(
            pieces,
            key=lambda p: max(0, int(p.bbox[2]) - int(p.bbox[0]))
            * max(0, int(p.bbox[3]) - int(p.bbox[1])),
        )
        bbox = tuple(int(v) for v in piece.bbox)
        crop = worker.cv.cropBbox(frame.bgr, bbox, ctx.config.crop_padding_px)
        if crop is None:
            return
        sharp = worker.sharpness(crop)
        quality = crop_quality.scoreCrop(crop)
        ctx.captured_crops.append(crop)
        ctx.captured_crop_timestamps.append(frame_ts)
        ctx.captured_crop_sharpness.append(sharp)
        ctx.captured_crop_quality.append(quality)
        ctx.last_capture_frame_ts = frame_ts
        obj = ctx.known_object
        encoded = worker.encodeFrame(crop)
        if obj is not None and encoded is not None:
            obj.latest_captured_crop = encoded
            obj.latest_captured_crop_ts = frame_ts
            obj.recognition_image_set.append(
                RecognitionImage(
                    image=encoded,
                    source="c4_burst",
                    used=False,
                    ts=frame_ts,
                    channel=4,
                    created_at=frame_ts,
                    sharpness=sharp,
                )
            )
            worker.emitKnownObject()

    # ---------------------------------------------------------- recognition

    def _applyResults(self) -> None:
        for load in self._ledger.loads:
            worker: Rev01BaseState = load.payload
            ctx = worker.ctx
            if load.route_locked or ctx.classification_applied:
                continue
            if ctx.classify_started_at <= 0.0:
                continue
            with ctx.classify_lock:
                result = ctx.classification_result
                error = ctx.classification_error
            if result is None and error is None:
                continue
            worker.updateKnownObjectWithResult(result, error)
            worker.dumpBurstCaptureArtifacts(
                list(ctx.captured_crops),
                list(ctx.selected_captures),
                result=result,
                error=error,
            )
            ctx.classification_applied = True
            obj = ctx.known_object
            if obj is not None and obj.part_id is not None:
                load.route = PocketRoute.NORMAL
                load.reject_reason = None
            else:
                load.route = PocketRoute.REJECT
                load.reject_reason = error or "recognition returned no routable part"
            self.noteProgress()

    def _forceReject(
        self, load: PocketLoad, reason: str, *, multi_piece: bool = False
    ) -> None:
        worker: Rev01BaseState = load.payload
        obj = worker.ctx.known_object
        if obj is not None:
            obj.part_id = None
            obj.part_name = None
            obj.part_category = None
            obj.category_id = None
            obj.destination_bin = None
            obj.classification_status = (
                ClassificationStatus.multi_drop_fail
                if multi_piece
                else ClassificationStatus.unknown
            )
            obj.reject_on_routing_failure = True
            worker.emitKnownObject()
        worker.ctx.classification_applied = True
        load.lock_route(PocketRoute.REJECT, reason)
        self.logger.warning(
            f"{LOG_TAG} pocket={load.pocket_id} -> bottom reject ({reason})"
        )

    # --------------------------------------------------------------- motion

    def _indexOnePocket(self, now: float) -> None:
        self._setGate(False, "indexing C4 one pocket")
        due = self._ledger.due_to_exit_on_next_index()
        if due is not None and not self._prepareDueLoad(due):
            return

        if self._move_target_steps is None:
            current = int(self._stepper.position)
            target_sector = self._ledger.boundary_index + self._travel_sign
            target_steps = self._platter.sector_position_microsteps(target_sector)
            self._startMove(current, target_steps, target_sector, now)
            return

        if not bool(self._stepper.stopped):
            if now - self._move_started_at <= _MOTION_TIMEOUT_S:
                return
            self._retryMove(now, "motion timeout")
            return

        current = int(self._stepper.position)
        error = current - int(self._move_target_steps)
        if abs(error) > _ALIGNMENT_TOLERANCE_STEPS:
            self._retryMove(now, f"settled {error} microsteps off target")
            return

        exited = self._ledger.advance_to(int(self._move_target_sector))
        if len(exited) > 1:
            raise RuntimeError("one pocket index produced multiple logical exits")
        if exited:
            outgoing = exited[0]
            if due is None or outgoing is not due:
                raise RuntimeError("logical exit did not match the prepared pocket")
            advanced = self.transport.advanceTransport()
            dropped = advanced.piece_for_distribution_drop
            obj = outgoing.payload.ctx.known_object
            if obj is None or dropped is None or dropped.uuid != obj.uuid:
                raise RuntimeError("distribution did not accept the indexed exit pocket")
            self.logger.info(
                f"{LOG_TAG} exited pocket={outgoing.pocket_id} piece={obj.uuid[:8]} "
                f"route={outgoing.route.value}"
            )

        self._move_target_steps = None
        self._move_target_sector = None
        self._move_started_at = 0.0
        self._move_retries = 0
        self._tail = None
        if self._drain_active and self._ledger.loads:
            # The gate was already guarded before this drain started. Keep it
            # closed and advance the remaining occupied pockets consecutively;
            # re-arming the same guard after every empty index added seconds of
            # dead time without increasing isolation.
            self._phase = _Phase.INDEX_ONE_POCKET
        else:
            self._drain_active = False
            self._drain_armed_at = 0.0
            self._phase = _Phase.WAIT_INTAKE
        self.noteProgress()

    def _prepareDueLoad(self, load: PocketLoad) -> bool:
        worker: Rev01BaseState = load.payload
        obj = worker.ctx.known_object
        if obj is None:
            raise RuntimeError("exit pocket has no owned piece record")
        if not load.route_locked:
            if worker.ctx.classification_applied and obj.part_id is not None:
                load.lock_route(PocketRoute.NORMAL)
            else:
                self._forceReject(load, load.reject_reason or "recognition deadline")
        if not load.distribution_placed:
            positioned = self.transport.getPieceForDistributionPositioning()
            if positioned is not None and positioned.uuid != obj.uuid:
                return False
            if positioned is None:
                self.transport.placePieceForDistribution(obj)
            load.distribution_placed = True
            self.logger.info(
                f"{LOG_TAG} preparing chute for pocket={load.pocket_id} "
                f"route={load.route.value}"
            )
        positioned = self.transport.getPieceForDistributionPositioning()
        return bool(
            positioned is not None
            and positioned.uuid == obj.uuid
            and obj.stage == PieceStage.distributing
            and self.shared.distribution_ready
        )

    def _startMove(self, current: int, target: int, target_sector: int, now: float) -> None:
        delta = int(target) - int(current)
        if delta == 0:
            raise RuntimeError("one-pocket C4 index resolved to zero movement")
        self._stepper.set_speed_limits(
            16, max(16, int(self._config.precise_converge_speed_usteps_per_s))
        )
        if not bool(self._stepper.move_steps(delta)):
            raise RuntimeError("C4 rejected the absolute one-pocket move")
        self._move_target_steps = int(target)
        self._move_target_sector = int(target_sector)
        self._move_started_at = now

    def _retryMove(self, now: float, reason: str) -> None:
        if self._move_retries >= 1:
            raise RuntimeError(f"C4 absolute index failed after bounded retry: {reason}")
        try:
            self._stepper.move_at_speed(0)
        except Exception:
            pass
        current = int(self._stepper.position)
        target = int(self._move_target_steps)
        delta = target - current
        if delta == 0:
            return
        self._move_retries += 1
        self._move_started_at = now
        if not bool(self._stepper.move_steps(delta)):
            raise RuntimeError(f"C4 rejected bounded index retry: {reason}")
        self.logger.warning(f"{LOG_TAG} retrying absolute index once ({reason})")

    def _releaseTimestamp(self) -> float | None:
        getter = getattr(self.shared, "latest_piece_release_attempt_mono", None)
        if not callable(getter):
            return None
        return getter(source=StationId.C3, target=StationId.CLASSIFICATION)

    def _setGate(self, ready: bool, reason: str) -> None:
        self.shared.set_classification_gate(bool(ready), reason=reason)

    def pause(self) -> None:
        """Freeze logical ownership while an ordinary machine pause is active.

        Finite stepper commands are allowed to settle, but no ledger, transport,
        or distribution state is discarded.  A full stop/re-home still calls
        ``cleanup()`` and performs the existing teardown-and-purge path.
        """
        if self._paused_at_mono is not None:
            return
        self._paused_at_mono = time.monotonic()
        self._paused_at_wall = time.time()
        self._setGate(False, "indexed pipeline paused")
        self.logger.info(
            f"{LOG_TAG} paused with {len(self._ledger.loads)} owned pocket(s)"
        )

    def resume(self) -> None:
        """Resume the exact indexed transaction without counting paused time."""
        if self._paused_at_mono is None:
            return

        now_mono = time.monotonic()
        now_wall = time.time()
        paused_mono = max(0.0, now_mono - self._paused_at_mono)
        paused_wall = max(
            0.0,
            now_wall - float(self._paused_at_wall or now_wall),
        )

        def shift(value: float, delta: float) -> float:
            return value + delta if value > 0.0 else value

        self._last_intake_activity = shift(
            self._last_intake_activity, paused_mono
        )
        self._drain_armed_at = shift(self._drain_armed_at, paused_mono)
        self._move_started_at = shift(self._move_started_at, paused_mono)
        self._arrival_armed_at_mono = shift(
            self._arrival_armed_at_mono, paused_mono
        )
        self.last_progress_at = shift(self.last_progress_at, paused_mono)
        for load in self._ledger.loads:
            load.admitted_at_mono = shift(load.admitted_at_mono, paused_mono)
            ctx = load.payload.ctx
            for field in (
                "capturing_started_at",
                "rotating_started_at",
                "classify_started_at",
                "discharging_started_at",
            ):
                setattr(ctx, field, shift(float(getattr(ctx, field, 0.0)), paused_mono))

        if self._phase == _Phase.WAIT_ARRIVAL:
            # Require a complete post-resume physical confirmation.  A frame
            # sampled before the pause must not combine with a later frame to
            # manufacture an arrival while the controller was stopped.
            self._arrival_armed_at_wall = now_wall
            self._arrival_last_frame_ts = 0.0
            self._arrival_presence_streak = 0
            self._arrival_samples = []
        else:
            self._arrival_armed_at_wall = shift(
                self._arrival_armed_at_wall, paused_wall
            )

        self._paused_at_mono = None
        self._paused_at_wall = None
        self.noteProgress()
        self.logger.info(
            f"{LOG_TAG} resumed phase={self._phase.value} "
            f"with {len(self._ledger.loads)} owned pocket(s)"
        )

    def cleanup(self) -> None:
        self._setGate(False, "indexed pipeline stopped")
        try:
            self._stepper.move_at_speed(0)
        except Exception:
            pass
        for load in self._ledger.clear():
            worker: Rev01BaseState = load.payload
            worker.abandonInFlightObject("indexed pipeline teardown")
            worker.ctx.reset()
        self._tail = None
        self._clearArrivalArm()
        self._drain_armed_at = 0.0
        self._drain_active = False
        self._paused_at_mono = None
        self._paused_at_wall = None
        self._phase = _Phase.WAIT_INTAKE
