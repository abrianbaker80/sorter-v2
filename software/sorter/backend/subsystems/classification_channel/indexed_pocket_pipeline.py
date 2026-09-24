from __future__ import annotations

import time
import threading
from dataclasses import asdict
from defs.events import PauseCommandData, PauseCommandEvent
from subsystems.classification_channel.transfer_episode import TransferEpisode
from enum import Enum

from defs.known_object import ClassificationStatus, KnownObject, PieceStage, RecognitionImage, UNVERIFIED_C4_HANDOFF
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


from .c3_transfer_recovery import C3TransferRecovery


class IndexedPocketPipeline(C3TransferRecovery):
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
        # is normally admitted after fresh C4 intake evidence. Lost confirmation
        # can instead consume that reservation as an unverified reject pocket.
        # The coordinator always owns a TickBus; enabling it here makes
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
        self._canceled_route_uuid: str | None = None
        self._tail: PocketLoad | None = None
        self._last_release_mono = self._releaseTimestamp() or 0.0
        self._episode: TransferEpisode | None = None
        self._upstream_view = None
        self._terminal_recovery_lock = threading.Lock()
        self._terminal_recovery_request: tuple[str, int] | None = None
        self._resolved_failure_drain = False
        self._arrival_armed_at_mono = 0.0
        self._arrival_armed_at_wall = 0.0
        self._arrival_last_frame_ts = 0.0
        self._arrival_presence_streak = 0
        self._arrival_empty_streak = 0
        self._arrival_timed_out = False
        self._arrival_samples: list[tuple[list, object]] = []
        self._confirmed_recovery_samples = None
        self._last_intake_activity = time.monotonic()
        self._drain_armed_at = 0.0
        self._drain_active = False
        self._intake_deadline: float | None = None
        self._readmission_used = False
        self._boundary_completed_wall = time.time()
        self._empty_frame_ts = 0.0
        self._empty_streak = 0
        self._move_target_steps: int | None = None
        self._move_target_sector: int | None = None
        self._move_started_at = 0.0
        self._move_retries = 0
        self._paused_at_mono: float | None = None
        self._paused_at_wall: float | None = None
        self.last_progress_at = time.monotonic()
        stats = getattr(gc, "runtime_stats", None)
        if stats is not None:
            stats.setIndexedMode()
        self.logger.info(f"{LOG_TAG} absolute origin ready sector={boundary} position={position}")

    def phaseName(self) -> str:
        return self._phase.value

    def noteProgress(self) -> None:
        self.last_progress_at = time.monotonic()

    def step(self) -> None:
        now = time.monotonic()
        try:
            self._applyResults()
            if self._phase == _Phase.WAIT_INTAKE:
                self._waitIntake(now)
            elif self._phase == _Phase.WAIT_ARRIVAL:
                self._waitArrival(now)
            elif self._phase == _Phase.CAPTURE_TAIL:
                self._captureTail(now)
            else:
                self._indexOnePocket(now)
        finally:
            self._observeRuntime()

    def _observeRuntime(self) -> None:
        stats = getattr(self.gc, "runtime_stats", None)
        if stats is None:
            return
        phase = self.phaseName()
        if self._phase == _Phase.INDEX_ONE_POCKET:
            phase = "index_motion" if self._move_target_steps is not None else "route_wait"
        elif self._phase == _Phase.WAIT_ARRIVAL and self._arrival_timed_out:
            phase = "unconfirmed_transfer"
        elif self._phase == _Phase.WAIT_INTAKE and self._drain_armed_at and not self._drain_active:
            phase = "drain_guard"
        stats.observeState("classification", phase)
        stats.observeState("classification.occupancy", phase)
        # Retain ownership through ledger -> distribution handoff until the
        # existing confirmation marks the object terminal. Publish once, so
        # the broadcaster never observes an artificial unowned handoff gap.
        pieces = [load.payload.ctx.known_object for load in self._ledger.loads]
        pieces.extend((self.transport.getPieceForDistributionPositioning(),
                       self.transport.getPieceForDistributionDrop()))
        stats.setOwnedPieceUuids(
            str(obj.uuid) for obj in pieces
            if obj is not None and obj.stage != PieceStage.distributed
        )

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
            if self._resolved_failure_drain:
                # Observe during the existing guard, without extending it. This
                # makes the first legal boundary eligible for re-admission.
                self._readmissionEvidenceReady()
            if now - self._drain_armed_at >= _DRAIN_GATE_GUARD_S:
                self._resolved_failure_drain = False
                self._drain_active = True
                self._phase = _Phase.INDEX_ONE_POCKET
            return

        # Start aiming the chute as soon as a load reaches the pre-exit pocket,
        # while C3 is free to prepare the following intake piece.
        if due is not None:
            self._prepareDueLoad(due)

        if self._ledger.loads and self._intake_deadline is not None and now >= self._intake_deadline:
            self._drain_armed_at = now
            self._setGate(False, "arming idle C4 drain")
            return

        self._openIntake(now, "indexed intake pocket available")

    def _openIntake(self, now: float, reason: str) -> None:
        if self._intake_deadline is None:
            self._intake_deadline = now + _DRAIN_IDLE_S
        self._setGate(True, reason)

    def _tryReadmission(self, now: float) -> bool:
        # Reconsider only at an acknowledged boundary, while already waiting
        # for routing/readiness. Never add an unbounded camera wait to draining.
        if (not self._drain_active or self._readmission_used or not self._ledger.loads
                or not self._ledger.can_admit or self._move_target_steps is not None
                or self._tail is not None or self._arrival_armed_at_mono
                or getattr(self.shared, "c3_motion_pending", False)):
            return False
        if not self._readmissionEvidenceReady():
            return False
        expected = self._platter.sector_position_microsteps(self._ledger.boundary_index)
        if not self._stepper.stopped or abs(int(self._stepper.position) - expected) > _ALIGNMENT_TOLERANCE_STEPS:
            return False
        # Only confirmed admission renews this allowance. If the opportunity
        # fails, drain the remaining owned loads without offering it repeatedly.
        self._readmission_used = True
        self._drain_active = False
        self._drain_armed_at = 0.0
        self._intake_deadline = None
        self._phase = _Phase.WAIT_INTAKE
        self._openIntake(now, "bounded indexed re-admission")
        return True

    def _readmissionEvidenceReady(self) -> bool:
        perception = getattr(self.gc, "perception_service", None)
        if perception is None or not hasattr(perception, "read_states"):
            return False
        c3 = perception.read_states().get(3)
        wall = time.time()
        if (c3 is None or c3.n_pieces <= 0
                or not 0 <= wall - c3.ts <= _CAPTURE_ARRIVAL_TIMEOUT_S):
            self._empty_streak = 0
            return False
        sample = self._readDropPiecesAndFrame()
        pieces, frame = sample if sample is not None else ([], None)
        ts = float(getattr(frame, "timestamp", 0.0) or 0.0)
        if (ts <= self._boundary_completed_wall
                or not 0 <= wall - ts <= _CAPTURE_ARRIVAL_TIMEOUT_S):
            self._empty_streak = 0
            return False
        if ts > self._empty_frame_ts:
            self._empty_frame_ts = ts
            self._empty_streak = 0 if pieces else self._empty_streak + 1
        if self._empty_streak < max(1, int(self._config.presence_streak_to_start)):
            return False
        return True

    def _armArrival(self, now: float) -> None:
        if not self._ledger.can_admit:
            raise RuntimeError("C3 release attempted into an unavailable C4 pocket")
        if self._episode is not None and self._episode.unresolved:
            raise RuntimeError("cannot replace an unresolved C3 transfer episode")
        release_at = self._last_release_mono or now
        self._episode = TransferEpisode(
            self._ledger.boundary_index, self._ledger.intake_pocket_id, release_at,
            time.time() - max(0.0, now - release_at),
            leader_id=getattr(self.shared, "c3_release_leader_id", None),
        )
        self._episode.release_evidence = dict(getattr(self.shared, "c3_release_evidence", {}) or {})
        # Consume only imagery attached to this exact accepted release. It is
        # optional and never participates in the physical arrival predicates.
        view = getattr(self.shared, 'c3_release_view', None)
        self.shared.c3_release_view = None
        self._upstream_view = None
        if (isinstance(view, dict) and view.get('evidence') is
                getattr(self.shared, 'c3_release_evidence', None)
                and view.get('leader_id') == self._episode.leader_id):
            self._upstream_view = {k: v for k, v in view.items() if k != 'evidence'}
            self._upstream_view['episode_id'] = self._episode.episode_id
        self._episode.group_size_unknown = bool(self._episode.release_evidence.get('group_size_unknown'))
        self._episode.support_motion_deg = self._episode.release_evidence.get("motion_deg", 0.0)
        self.shared.c3_transfer_episode = self._episode
        self._observeEpisode()
        self._arrival_armed_at_mono = now
        self._arrival_armed_at_wall = time.time()
        self._arrival_last_frame_ts = 0.0
        self._arrival_presence_streak = 0
        self._arrival_empty_streak = 0
        self._arrival_timed_out = False
        if self._intake_deadline is None:
            self._intake_deadline = now + _DRAIN_IDLE_S
        self._arrival_samples = []
        self._phase = _Phase.WAIT_ARRIVAL
        self._setGate(False, "awaiting confirmed C4 intake arrival")
        self.noteProgress()

    def _observeEpisode(self) -> None:
        stats = getattr(self.gc, "runtime_stats", None)
        if stats is not None and self._episode is not None:
            stats.observeTransferEpisode(asdict(self._episode))

    def _unresolvedTransfer(self, reason: str) -> None:
        episode = self._episode
        if episode is None or episode.state == "unresolved" and not episode.terminal_recovery_active:
            return
        episode.terminal_recovery_active = False
        episode.state = "unresolved"
        if episode.recovery_started_mono is not None:
            episode.recovery_elapsed_s = max(0.0, time.monotonic()-episode.recovery_started_mono)
        episode.recovery_decision["final_outcome"] = "unresolved"
        episode.forced_reject_reason = episode.forced_reject_reason or "c3_transfer_unresolved"
        self._observeEpisode()
        self._setGate(False, "unresolved C3 transport; retained ownership")
        self.logger.warning(f"{LOG_TAG} episode={episode.episode_id} unresolved: {reason}")
        stats = getattr(self.gc, "runtime_stats", None)
        if stats is not None and stats.activeIncident() is None:
            stats.setActiveIncident({
                "kind": "classification_intake_request_timeout",
                "source_kind": "bounded_c3_transfer", "episode_id": episode.episode_id,
                "severity": "warning", "status": "waiting_for_operator",
                "awaiting_operator": True, "scope": "classification", "channel": "c4",
                "triggered_at": time.time(), "rule": "unresolved_c3_transfer",
                "operator_message": f"Automatic C3 recovery stopped: {reason}. "
                                    "Transfer ownership is retained.",
            })
        # A missing detector frame never authorizes clearing the reserved boundary.
        self._deps[-1].put(PauseCommandEvent(tag="pause", data=PauseCommandData()))

    def requestRetainedTransferRecovery(self, episode_id: str, boundary_index: int) -> None:
        """Queue exactly this retained reservation; mutation occurs on its owner thread."""
        with self._terminal_recovery_lock:
            ep = self._episode
            if (ep is None or ep.episode_id != episode_id or ep.boundary_index != boundary_index
                    or ep is not getattr(self.shared, 'c3_transfer_episode', None)
                    or ep.state != 'unresolved' or self._phase != _Phase.WAIT_ARRIVAL):
                raise ValueError('No matching retained unresolved C3 transfer')
            if (ep.terminal_recovery_attempted or self._terminal_recovery_request is not None
                    or ep.recovery_branch in ('exit_clearance', 'retained_piece_jitter') and ep.recovery_legs):
                raise ValueError('The bounded geometry recovery attempt is already spent')
            self._terminal_recovery_request = (episode_id, boundary_index)

    def applyRetainedTransferRecovery(self) -> bool:
        """Activate the same episode before the coordinator evaluates its incident hold."""
        with self._terminal_recovery_lock:
            request = self._terminal_recovery_request
            self._terminal_recovery_request = None
        if request is None:
            return False
        ep = self._episode
        stats = self.gc.runtime_stats
        incident = stats.activeIncident()
        def refuse(reason):
            self.logger.warning(f'{LOG_TAG} retained recovery refused: {reason}')
            if ep is not None and ep.episode_id == request[0]:
                ep.recovery_decision['terminal_recovery_request_error'] = reason
                self._observeEpisode()
            return False
        valid = (ep is not None and (ep.episode_id, ep.boundary_index) == request
                 and ep is getattr(self.shared, 'c3_transfer_episode', None)
                 and ep.state == 'unresolved' and not ep.terminal_recovery_attempted
                 and self._phase == _Phase.WAIT_ARRIVAL
                 and (incident is None or
                      (incident.get('source_kind') == 'bounded_c3_transfer'
                       and incident.get('episode_id') == ep.episode_id)))
        boundary = self._recoveryBoundary(time.time()) if valid else None
        if not valid or not all(boundary['predicates'][k] for k in
                                ('reserved_boundary', 'c4_available', 'c4_stopped_aligned')):
            return refuse('Retained recovery reservation or incident no longer matches')
        try:
            stopped = all(getattr(self.irl, f'c_channel_{ch}_rotor_stepper').stopped for ch in (2,3))
        except Exception as exc:
            return refuse(f'Motor completion feedback unavailable: {exc}')
        if getattr(self.shared, 'c3_motion_pending', False) or not stopped:
            return refuse('Retained recovery requires resolved existing motor ownership')
        if not stats.clearActiveIncidentIfMatches(incident, resolved_by='retained_transfer_recovery'):
            return refuse('The validated transfer incident was replaced')
        now, wall = time.monotonic(), time.time()
        ep.terminal_recovery_previous = {'decision': ep.recovery_decision,
            'elapsed_s': ep.recovery_elapsed_s, 'started_mono': ep.recovery_started_mono,
            'deadline_mono': ep.recovery_deadline_mono, 'branch': ep.recovery_branch}
        ep.terminal_recovery_attempted = ep.terminal_recovery_active = True
        ep.terminal_recovery_leg_offset = len(ep.recovery_legs)
        ep.recovery_started_mono, ep.recovery_deadline_mono = now, now+12.0
        ep.recovery_elapsed_s = 0.0
        ep.recovery_arrival = False
        ep.recovery_stage = 0
        ep.recovery_branch = 'exit_clearance'
        ep.recovery_decision = {}
        ep.release_evidence['completion_observed_wall'] = wall
        self._clearArrivalArm()
        self._arrival_armed_at_mono, self._arrival_armed_at_wall = now, wall
        self._arrival_timed_out = True
        self._setGate(False, 'explicit recovery of retained C3 transfer')
        self._observeEpisode()
        return True

    def _clearArrivalArm(self) -> None:
        self._confirmed_recovery_samples = None
        self._arrival_armed_at_mono = 0.0
        self._arrival_armed_at_wall = 0.0
        self._arrival_last_frame_ts = 0.0
        self._arrival_presence_streak = 0
        self._arrival_empty_streak = 0
        self._arrival_timed_out = False
        self._arrival_samples = []

    def _recoveryBoundary(self, frame_ts: float) -> dict:
        ep = self._episode
        expected = self._platter.sector_position_microsteps(ep.boundary_index)
        try:
            stopped, position = self._stepper.stopped, int(self._stepper.position)
        except Exception:
            stopped, position = False, None
        age = time.time()-frame_ts
        return {'boundary_index': self._ledger.boundary_index,
                'reserved_boundary': ep.boundary_index, 'pocket': ep.pocket_id,
                'owned_pockets': [p.pocket_id for p in self._ledger.loads],
                'position': position, 'expected_position': expected, 'stopped': stopped,
                'frame_ts': frame_ts, 'frame_age_s': age,
                'predicates': {
                    'reserved_boundary': self._ledger.boundary_index == ep.boundary_index and
                                         self._ledger.intake_pocket_id == ep.pocket_id,
                    'c4_available': self._ledger.can_admit,
                    'c4_stopped_aligned': stopped and position is not None and
                        abs(position-expected) <= _ALIGNMENT_TOLERANCE_STEPS and self._move_target_steps is None,
                    'c4_frame_fresh': frame_ts > self._arrival_armed_at_wall and 0 <= age <= _CAPTURE_ARRIVAL_TIMEOUT_S,
                    'c4_empty': self._arrival_empty_streak >= max(1, int(self._config.presence_streak_to_start)),
                }}

    def _recordRecoveryDecision(self) -> None:
        import json
        self._episode.recovery_decision['command_history'] = [dict(l) for l in self._episode.recovery_legs]
        self._episode.recovery_decision['episode_state'] = self._episode.state
        self.logger.info(f"{LOG_TAG} recovery_decision " + json.dumps(self._episode.recovery_decision, sort_keys=True))
        self._observeEpisode()

    def _waitArrival(self, now: float) -> None:
        if self._episode is not None and not self._episode.recovery_open:
            return
        self._setGate(False, "unconfirmed transfer awaiting physical resolution"
                      if self._arrival_timed_out else "awaiting confirmed C4 intake arrival")

        # A second release command while C4's gate is closed is a feeder race.
        # Consume its timestamp so it cannot arm a second logical pocket; actual
        # multi-piece evidence is still decided from the C4 camera below.
        release = self._releaseTimestamp()
        if release is not None and release > self._last_release_mono:
            self._last_release_mono = release
            if self._episode is not None:
                self._episode.group_size_unknown = True
            self._unresolvedTransfer("additional release while intake was reserved")
            return

        sample = self._readDropPiecesAndFrame()
        drop_pieces, frame = sample if sample is not None else ([], None)
        frame_ts = float(getattr(frame, "timestamp", 0.0) or 0.0)
        fresh = (
            frame is not None
            and frame_ts > self._arrival_last_frame_ts
            and frame_ts > self._arrival_armed_at_wall
            and 0 <= time.time() - frame_ts <= _CAPTURE_ARRIVAL_TIMEOUT_S
        )
        if now - self._arrival_armed_at_mono >= _RELEASE_CONFIRM_TIMEOUT_S:
            if self._episode is not None:
                self._episode.forced_reject_reason = "c3_arrival_unconfirmed"
        if fresh:
            self._arrival_last_frame_ts = frame_ts
            if drop_pieces:
                self._arrival_empty_streak = 0
                self._arrival_presence_streak += 1
                self._arrival_samples.append((list(drop_pieces), frame))
            else:
                self._arrival_empty_streak = (
                    0 if getattr(self.shared, "c3_motion_pending", False)
                    else self._arrival_empty_streak + 1
                )
                self._arrival_presence_streak = 0
                self._arrival_samples = []

            required = max(1, int(self._config.presence_streak_to_start))
            if self._arrival_presence_streak >= required or self._confirmed_recovery_samples:
                samples = self._confirmed_recovery_samples or list(self._arrival_samples[-required:])
                ep = self._episode
                request = getattr(self.shared, "request_c3_recovery", None)
                if ep is not None and ep.recovery_started_mono is not None and callable(request):
                    boundary = self._recoveryBoundary(frame_ts)
                    decision = request(ep, boundary)
                    ep.recovery_arrival = True
                    self._confirmed_recovery_samples = samples
                    decision['arrival'] = True
                    # Arrival cancels future legs, including on the deadline
                    # tick, but cannot waive ownership while motion settles.
                    self._advanceRecovery(now, boundary, decision)
                    if not ep.recovery_open:
                        return
                    if not decision['predicates']['motor_c3_resolved']:
                        decision['result'] = 'arrival_waiting_for_owned_completion'
                        self._recordRecoveryDecision()
                        return
                    if not all(decision['predicates'][k] for k in ('reserved_boundary', 'c4_available', 'c4_stopped_aligned')):
                        self._unresolvedTransfer("confirmed arrival with inconsistent receiving boundary")
                        self._recordRecoveryDecision()
                        return
                    if ep.recovery_legs:
                        ep.recovery_legs[-1]['completed_at_mono'] = ep.recovery_legs[-1]['completed_at_mono'] or now
                        ep.recovery_legs[-1].setdefault('completed_at_wall', time.time())
                    ep.recovery_success_stage = ep.recovery_stage
                    ep.recovery_elapsed_s = max(0.0, now-ep.recovery_started_mono)
                    decision['final_outcome'] = 'arrived_transport_reject'
                    self._recordRecoveryDecision()
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

        episode = self._episode
        if episode is None or not episode.recovery_open:
            return
        request = getattr(self.shared, "request_c3_recovery", None)
        timed_out = now-self._arrival_armed_at_mono >= _RELEASE_CONFIRM_TIMEOUT_S
        # Before recovery, only preserve C3 spatial continuity; normal arrival
        # must not add C4 motor queries for diagnostic boundary snapshots.
        boundary = (self._recoveryBoundary(frame_ts)
                    if timed_out or episode.recovery_started_mono is not None
                    else {'frame_ts': frame_ts, 'predicates': {}})
        observed = request(episode, boundary) if callable(request) else None
        if timed_out and not self._arrival_timed_out:
            self._arrival_timed_out = True
            self.logger.warning(f"{LOG_TAG} C3 release produced no confirmed C4 arrival; "
                f"episode={episode.episode_id} frame_ts={frame_ts:.6f} drop_count={len(drop_pieces)} "
                f"c3_motion_pending={getattr(self.shared, 'c3_motion_pending', False)}")
            stats = getattr(self.gc, "runtime_stats", None)
            if stats is not None:
                stats.observeBlockedReason("classification", "arrival_unconfirmed")
        if timed_out or episode.recovery_started_mono is not None:
            if not callable(request):
                episode.recovery_decision = {'failed_predicates': ['recovery_callback_available']}
                self._unresolvedTransfer("recovery_callback_available=false")
                self._recordRecoveryDecision()
                return
            self._advanceRecovery(now, boundary, observed)

    def _admit(self, admitted_at: float, *, initial_samples=None, unverified=False) -> None:
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
        episode = self._episode
        if episode is not None:
            obj.transfer_episode_id = episode.episode_id
            obj.physical_group_size_unknown = episode.group_size_unknown
            obj.transport_failure_reason = episode.forced_reject_reason
            obj.transfer_first_pass = episode.forced_reject_reason is None
            episode.piece_uuid = str(obj.uuid)
            episode.first_pass = obj.transfer_first_pass
            episode.state = "discard_bound" if unverified else "admitted"
            stats = getattr(self.gc, 'runtime_stats', None)
            incident = stats.activeIncident() if stats is not None else None
            if (incident and incident.get('source_kind') == 'bounded_c3_transfer'
                    and incident.get('episode_id') == episode.episode_id):
                stats.clearActiveIncidentIfMatches(incident,
                    resolved_by='discard_bound_handoff' if unverified else 'confirmed_c4_arrival')
            episode.terminal_recovery_active = False
            self._observeEpisode()
        worker.ctx.known_object = obj
        view = self._upstream_view
        if (view is not None and episode is not None
                and view.get('episode_id') == episode.episode_id
                and not episode.group_size_unknown and not obj.transport_failure_reason):
            # Match the worker generation as well as the physical episode.
            worker.ctx.owned_upstream_view = {**view, 'piece_uuid': str(obj.uuid),
                                             'cycle_id': worker.ctx.cycle_id}
        self._upstream_view = None
        worker.emitKnownObject()
        self._tail = self._ledger.admit(
            worker, admitted_at_mono=admitted_at
        )
        stats = getattr(self.gc, "runtime_stats", None)
        if stats is not None and not unverified:
            stats.observeChannelExit("c_channel_3", piece_uuid=str(obj.uuid),
                                     source="indexed_confirmed_arrival")
        self._observeRuntime()
        self._last_intake_activity = time.monotonic()
        self._intake_deadline = None
        self._readmission_used = False
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
        if worker.ctx.multi_feed_detected or any(len(pieces) > 1 for pieces, _ in (initial_samples or [])):
            obj.physical_group_size_unknown = True
        if obj.transport_failure_reason:
            self._forceReject(self._tail, obj.transport_failure_reason, category="transport")
            self._phase = _Phase.INDEX_ONE_POCKET
            self._resolved_failure_drain = not unverified
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
        started = time.perf_counter()
        sample = perception.read_pieces_and_frame(4)
        stats = getattr(self.gc, "runtime_stats", None)
        if stats is not None:
            stats.observePerfMs("classification.indexed.perception_read_ms",
                                (time.perf_counter() - started) * 1000.0)
        if sample is None:
            return None
        pieces, frame = sample
        frame_ts = float(getattr(frame, "timestamp", 0.0) or 0.0)
        if stats is not None and frame_ts > 0:
            stats.observePerfMs("classification.decision_frame_age_ms",
                                max(0.0, (time.time() - frame_ts) * 1000.0))
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
            if ctx.known_object is None:
                self._rejectOwnership(load, "missing C4 piece record")
                continue
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
                obj.reject_category = "provider" if obj.request_failed else "classification"
            self.noteProgress()

    def _forceReject(
        self, load: PocketLoad, reason: str, *, multi_piece: bool = False, category: str = "handling"
    ) -> None:
        worker: Rev01BaseState = load.payload
        obj = worker.ctx.known_object
        if obj is not None:
            obj.forced_reject_reason = obj.forced_reject_reason or reason
            obj.reject_category = obj.reject_category or category
            obj.physical_group_size_unknown = obj.physical_group_size_unknown or multi_piece
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
        if self.shared.classification_ready:
            self._setGate(False, "preparing indexed movement")
        release = self._releaseTimestamp()
        if (self._move_target_steps is not None and release is not None
                and release > self._last_release_mono):
            # The accepted attempt belongs to an uncertain physical boundary.
            # Keep the ledger intact and use the existing incident path.
            self._setGate(False, "C3 release during C4 index")
            raise RuntimeError("C3 release attempt overlapped an accepted C4 index")
        if self._move_target_steps is None:
            if release is not None and release > self._last_release_mono:
                self._last_release_mono = release
                self._armArrival(now)
                return
            # The feeder retains ownership for every accepted move. Only its
            # bounded, closed-boundary staging move can overlap this index;
            # release, follow-through, recovery and unknown purposes still wait.
            if (getattr(self.shared, "c3_motion_pending", False)
                    and not getattr(self.shared, "c3_safe_staging_pending", False)):
                self._setGate(False, "waiting for accepted C3 move completion")
                return
            if not self._ledger.loads:
                self._drain_active = False
                self._drain_armed_at = 0.0
                self._readmission_used = False
                self._phase = _Phase.WAIT_INTAKE
                self._openIntake(now, "indexed pipeline empty")
                return
            if self._tryReadmission(now):
                return
        due = self._ledger.due_to_exit_on_next_index()
        if due is not None and not self._prepareDueLoad(due):
            self._setGate(False, "waiting for indexed exit route readiness")
            return

        self._setGate(False, "indexing C4 one pocket")

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
            stats = getattr(self.gc, "runtime_stats", None)
            if stats is not None and obj.transport_failure_reason != UNVERIFIED_C4_HANDOFF:
                stats.observeChannelExit("classification_channel", piece_uuid=str(obj.uuid),
                                         source="indexed_transport_exit")
            self.logger.info(
                f"{LOG_TAG} exited pocket={outgoing.pocket_id} piece={obj.uuid[:8]} "
                f"route={outgoing.route.value}"
            )

        self._move_target_steps = None
        self._move_target_sector = None
        self._move_started_at = 0.0
        self._move_retries = 0
        self._tail = None
        self._boundary_completed_wall = time.time()
        self._empty_frame_ts = 0.0
        self._empty_streak = 0
        self._intake_deadline = None
        if self._resolved_failure_drain:
            self._phase = _Phase.WAIT_INTAKE
            self._drain_armed_at = now
            self._setGate(False, "guarding drain after resolved transport failure")
            self.noteProgress()
            return
        if self._drain_active and self._ledger.loads:
            # The gate was already guarded before this drain started. Keep it
            # closed and advance the remaining occupied pockets consecutively;
            # re-arming the same guard after every empty index added seconds of
            # dead time without increasing isolation.
            self._phase = _Phase.INDEX_ONE_POCKET
        else:
            self._drain_active = False
            self._drain_armed_at = 0.0
            if not self._ledger.loads:
                self._readmission_used = False
            self._phase = _Phase.WAIT_INTAKE
        self.noteProgress()

    def _rejectOwnership(self, load: PocketLoad, reason: str) -> None:
        ctx = load.payload.ctx
        if ctx.known_object is None:
            ctx.known_object = KnownObject()
        obj = ctx.known_object
        # Use the existing unverified-pocket retirement contract: no invented
        # arrival, quantity, normal delivery or Harvest credit.
        obj.transport_failure_reason = UNVERIFIED_C4_HANDOFF
        obj.physical_group_size_unknown = True
        load.route = PocketRoute.REJECT
        load.reject_reason = reason
        load.route_locked = True
        load.distribution_placed = False
        self._forceReject(load, reason, category="transport")

    def _prepareDueLoad(self, load: PocketLoad) -> bool:
        worker: Rev01BaseState = load.payload
        obj = worker.ctx.known_object
        if obj is None:
            self._rejectOwnership(load, "missing C4 piece record")
            obj = worker.ctx.known_object
        if self._canceled_route_uuid is not None:
            if self.transport.isCanceledPieceForDistribution(self._canceled_route_uuid):
                return False
            self._canceled_route_uuid = None
        if not load.route_locked:
            if worker.ctx.classification_applied and obj.part_id is not None:
                load.lock_route(PocketRoute.NORMAL)
            else:
                self._forceReject(load, load.reject_reason or "recognition deadline",
                                  category=obj.reject_category or "classification")
        positioned = self.transport.getPieceForDistributionPositioning()
        if positioned is not None and positioned.uuid != obj.uuid:
            # Cancel positioning, never simulate a physical drop. Distribution
            # acknowledges this through its existing READY/IDLE path before
            # the affected pocket can request an all-open reject route.
            self._rejectOwnership(load, "conflicting C4 positioning ownership")
            for other in self._ledger.loads:
                other_obj = other.payload.ctx.known_object
                if other is not load and other_obj is not None and other_obj.uuid == positioned.uuid:
                    self._rejectOwnership(other, "conflicting C4 positioning ownership")
            self.transport.cancelPieceForDistribution(positioned.uuid)
            self._canceled_route_uuid = positioned.uuid
            return False
        if load.distribution_placed and positioned is None:
            self._rejectOwnership(load, "missing C4 positioning ownership")
            self.transport.cancelPieceForDistribution(obj.uuid)
            self._canceled_route_uuid = obj.uuid
            return False
        if not load.distribution_placed:
            if positioned is None:
                if obj.transport_failure_reason == UNVERIFIED_C4_HANDOFF:
                    obj.stage = PieceStage.created
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
        stats = getattr(self.gc, "runtime_stats", None)
        if stats is not None:
            stats.observeState("classification.intake_gate", "open" if ready else f"closed: {reason}")

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

        if self._episode is not None and self._episode.recovery_started_mono is not None:
            self._episode.recovery_started_mono += paused_mono
            self._episode.recovery_deadline_mono += paused_mono
        self._last_intake_activity = shift(
            self._last_intake_activity, paused_mono
        )
        self._drain_armed_at = shift(self._drain_armed_at, paused_mono)
        if self._intake_deadline is not None:
            self._intake_deadline += paused_mono
        self._boundary_completed_wall = now_wall
        self._empty_frame_ts = 0.0
        self._empty_streak = 0
        self._move_started_at = shift(self._move_started_at, paused_mono)
        self._arrival_armed_at_mono = shift(
            self._arrival_armed_at_mono, paused_mono
        )
        # Episode timestamps describe actual commands and remain immutable;
        # only active confirmation deadlines exclude the paused interval.
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
            self._arrival_empty_streak = 0
            self._arrival_samples = []
            self._confirmed_recovery_samples = None
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
        stats = getattr(self.gc, "runtime_stats", None)
        if stats is not None:
            stats.setOwnedPieceUuids(())
            stats.endState("classification.occupancy")
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
        if self._episode is not None and self._episode.unresolved:
            self._episode.state = "unresolved"
            self._observeEpisode()
        self.shared.c3_transfer_episode = None
        self._clearArrivalArm()
        self._drain_armed_at = 0.0
        self._drain_active = False
        self._paused_at_mono = None
        self._paused_at_wall = None
        self._phase = _Phase.WAIT_INTAKE
        self._intake_deadline = None
        self._readmission_used = False
