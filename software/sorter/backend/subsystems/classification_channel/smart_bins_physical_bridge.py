"""Optional, inactive native smart-bin fence for the existing C4 owner.

The caller holds the lifecycle and controller operation locks throughout each
entry point. The bridge records one prepared target; FIFO and positioner remain
the only occupancy and motor owners. Constructing either without this bridge
keeps the legacy path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from uuid import uuid4

import smart_bins_delivery as delivery
from .marker_positioner import ConfirmedIndex, PositionError
from .physical_fifo import IndexTarget, Pocket, PocketState


@dataclass
class _Index:
    target: IndexTarget
    owner_incarnation: str
    pocket: Pocket
    binding: object | None
    custody: delivery.CustodyRef | None
    reservation_id: str | None
    qualification: object | None
    release_evidence: delivery.ReleaseEvidence | None
    intent_key: str | None
    motor_identity: str
    motor_token: object
    stage: str = "prepared"
    attempt_id: str | None = None
    first_dispatch_consumed: bool = False
    first_receipt: object | None = None
    confirmation: ConfirmedIndex | None = None
    exit_key: str | None = None
    exit_evidence: delivery.ExitEvidence | None = None
    error: str | None = None
    failure: Exception | None = None
    uncertainty_key: str | None = None
    uncertainty_evidence: delivery.UncertaintyEvidence | None = None
    uncertainty_result: dict | None = None
    uncertainty_error: str | None = None


class PhysicalNativeBridge:
    """Narrow owner interface for simulated native release.

    owner supplies owner_incarnation and assert_locked(), native_qualified(),
    reservation_for(), release_evidence(), current_route(), and
    empty_route_ready(). Its route methods report current physical readiness;
    the ledger separately validates reservation and policy facts.
    """

    def __init__(self, machine_id: str, owner):
        if not machine_id or not getattr(owner, "owner_incarnation", None):
            raise ValueError("guarded bridge needs machine and owner identity")
        for name in ("assert_locked", "native_qualified", "reservation_for",
                     "release_evidence", "current_route", "empty_route_ready"):
            if not callable(getattr(owner, name, None)):
                raise ValueError(f"guarded bridge needs owner.{name}")
        self.machine_id, self.owner = machine_id, owner
        self.runtime = None
        self.positioner = None
        self.index: _Index | None = None
        self.recovery_blocker: str | None = None
        self.refused_target: tuple[IndexTarget, Pocket, object | None] | None = None
        self._completion_guard = None
        self._completion_waiting: tuple[str, str] | None = None

    def attach_completion_guard(self, guard) -> None:
        """Couple a single downstream completion owner to C3/C4 admission."""
        if (self._completion_guard is not None and self._completion_guard is not guard
            or getattr(guard, "bridge", None) is not self
            or not callable(getattr(guard, "releases_admission", None))):
            raise PositionError("guarded completion owner is missing or changed")
        self._completion_guard = guard

    def attach(self, runtime, positioner) -> None:
        self.owner.assert_locked()
        if self.runtime is not None or positioner.pending is not None:
            raise PositionError("guarded owner must attach to an idle positioner once")
        if any(p.state is not PocketState.EMPTY for p in runtime.fifo.pockets):
            raise PositionError("guarded attach requires empty FIFO and explicit custody inspection")
        if not callable(getattr(positioner, "install_dispatch_guard", None)):
            raise PositionError("positioner lacks the required dispatch hook")
        inspection = delivery.inspect_recovery(self.machine_id)
        if inspection["unresolved_discrepancies"]:
            self.recovery_blocker = "unresolved durable discrepancy"
        for claim in inspection["claims"]:
            if claim["state"] != "RESERVED" or claim["owner_incarnation"] != self.owner.owner_incarnation:
                self.recovery_blocker = "outstanding durable custody needs reconciliation"
        if inspection["followup_blockers"]:
            self.recovery_blocker = "unresolved native completion follow-up"
        positioner.install_dispatch_guard(self)
        self.runtime, self.positioner = runtime, positioner

    @property
    def blocks_admission(self) -> bool:
        if (self.recovery_blocker is not None or self.index is not None
            or self.refused_target is not None):
            return True
        if self._completion_waiting is None:
            return False
        guard = self._completion_guard
        if guard is None:
            return True
        try:
            return not guard.releases_admission(*self._completion_waiting)
        except Exception:
            return True

    def fence_refused_target(self, target: IndexTarget) -> None:
        """Retain a planner target that could not acquire its guarded evidence."""
        if self.index is None and self.refused_target is None:
            pocket = self._exit_pocket(target)
            self.refused_target = (
                target, pocket,
                self.runtime.bindings.get((pocket.pocket_id, pocket.generation)))

    @property
    def first_dispatch_consumed(self) -> bool:
        return self.index is not None and self.index.first_dispatch_consumed

    def admission_reservation(self, piece, episode, pocket: Pocket) -> str:
        self.owner.assert_locked()
        if self.blocks_admission:
            raise PositionError(self.recovery_blocker or "guarded index outstanding")
        if not self.owner.native_qualified(piece, episode):
            raise PositionError("native admission is unqualified or Harvest-bound")
        custody = delivery.CustodyRef(
            piece.uuid, self.owner.owner_incarnation, episode.episode_id,
            pocket.pocket_id, pocket.generation + 1)
        reservation_id = self.owner.reservation_for(piece, episode, custody)
        if not reservation_id:
            raise PositionError("native admission needs an existing reservation")
        code = delivery.validate_physical_claim(
            self.machine_id, reservation_id, custody, state="RESERVED", revision=0)
        if code != "OK":
            raise PositionError(f"native admission refused: {code}")
        return reservation_id

    def _exit_pocket(self, target: IndexTarget) -> Pocket:
        fifo = self.runtime.fifo
        if fifo.pending_index != target or target.boundary != fifo.boundary + 1:
            raise PositionError("prepared FIFO target changed")
        return next(p for p in fifo.pockets if fifo.station_of(p.pocket_id) == 0)

    def prepare_target(self, target: IndexTarget) -> None:
        self.owner.assert_locked()
        if self.recovery_blocker:
            raise PositionError(self.recovery_blocker)
        if self.refused_target is not None:
            raise PositionError("prepared target refused; owner reconciliation required")
        if self.index is not None:
            self._assert_index_owner(self.index)
            if self.index.target != target:
                raise PositionError("another guarded target is outstanding")
            self._retry_intent()
            return
        if self.runtime.recovering:
            raise PositionError("guarded recovery sweep has no durable permit")
        owner_incarnation = self.owner.owner_incarnation
        pocket = self._exit_pocket(target)
        binding = self.runtime.bindings.get((pocket.pocket_id, pocket.generation))
        custody = None
        reservation_id = None
        qualification = None
        evidence = None
        key = None
        if pocket.state is PocketState.EMPTY:
            if binding is not None or not self.owner.empty_route_ready(target, pocket):
                raise PositionError("empty exit proof is unavailable")
        else:
            if binding is None or not binding.admitted or not binding.reservation_id:
                raise PositionError("exiting load lacks native custody")
            if not self.runtime.current(binding):
                raise PositionError("exiting binding changed")
            custody = delivery.CustodyRef(
                binding.piece.uuid, owner_incarnation,
                binding.episode.episode_id, binding.pocket_id, binding.generation)
            reservation_id = binding.reservation_id
            qualified = self.owner.release_evidence(binding, target, reservation_id)
            if qualified is None or len(qualified) != 2:
                raise PositionError("native route is unqualified")
            qualification, evidence = qualified
            if (not isinstance(evidence, delivery.ReleaseEvidence)
                or evidence.custody != custody or evidence.target_boundary != target.boundary
                or not evidence.current_owner or not evidence.route_ready
                or evidence.harvest_allocation_ref is not None or evidence.harvest_metadata):
                raise PositionError("owner release evidence does not match custody")
            if ((pocket.state is PocketState.ROUTED and evidence.destination_kind != "BIN")
                or (pocket.state is PocketState.DISCARD and evidence.destination_kind != "REJECT")
                or pocket.state is PocketState.PENDING):
                raise PositionError("physical route and reserved destination disagree")
            key = str(uuid4())
        motor = self.positioner.motor
        state = _Index(target, owner_incarnation, pocket, binding, custody, reservation_id,
                       qualification, evidence, key, motor.coordinate_identity(),
                       motor.stationary_token())
        self.index = state
        self._retry_intent()

    def _assert_index_owner(self, state: _Index) -> None:
        if self.owner.owner_incarnation != state.owner_incarnation:
            reason = "guarded index owner incarnation changed"
            if state.stage in ("prepared", "armed"):
                state.stage = "blocked"
                state.error = reason
            raise PositionError(reason)

    def _retry_intent(self) -> None:
        state = self.index
        if state is None:
            raise PositionError("no prepared index")
        self.owner.assert_locked()
        self._assert_index_owner(state)
        if state.stage == "armed":
            return
        if state.stage != "prepared":
            raise PositionError(state.error or "guarded index cannot be rearmed")
        if self.runtime._pending is not None or self.positioner.pending is not None:
            raise PositionError("lost intent acknowledgement needs proof of no motor attempt")
        self.positioner.motor.check_token(state.motor_token)
        if self.positioner.motor.coordinate_identity() != state.motor_identity:
            raise PositionError("motor ownership changed during intent")
        if self._exit_pocket(state.target) != state.pocket:
            raise PositionError("exiting pocket changed during intent")
        if state.reservation_id is not None:
            result = delivery.prepare_release(
                self.machine_id, state.reservation_id,
                expected_reservation_revision=0, request_key=state.intent_key,
                qualification=state.qualification, evidence=state.release_evidence)
            if result.get("code") != "OK" or result.get("target_boundary") != state.target.boundary:
                state.error = f"release intent refused: {result.get('code')}"
                state.stage = "blocked"
                raise PositionError(state.error)
            state.attempt_id = result["attempt_id"]
        state.stage = "armed"

    def before_request(self, boundary: int, positioner) -> None:
        self.owner.assert_locked()
        state = self.index
        if state is not None:
            self._assert_index_owner(state)
        if (state is None or state.stage != "armed" or state.target.boundary != boundary
            or positioner is not self.positioner or self.runtime._pending != state.target):
            raise PositionError("missing exact guarded index authorization")

    def _live(self, positioner) -> _Index:
        self.owner.assert_locked()
        state = self.index
        if state is not None:
            self._assert_index_owner(state)
        if (state is None or state.stage != "armed" or positioner is not self.positioner
            or positioner.pending != state.target.boundary
            or self.runtime._pending != state.target
            or self.runtime.recovering
            or (self.runtime.paused and not state.first_dispatch_consumed)
            or positioner.motor.coordinate_identity() != state.motor_identity
            or self.runtime.fifo.pending_index != state.target):
            raise PositionError("guarded owner or target changed")
        positioner.motor.check_token(positioner._token)
        if state.first_receipt is None and positioner._token != state.motor_token:
            raise PositionError("motor ownership token changed")
        pocket = self._exit_pocket(state.target)
        if pocket != state.pocket:
            raise PositionError("exiting pocket changed")
        if state.reservation_id is None:
            if pocket.state is not PocketState.EMPTY or not self.owner.empty_route_ready(
                state.target, pocket
            ):
                raise PositionError("empty exit proof became stale")
        else:
            binding = state.binding
            if (not self.runtime.current(binding) or binding.reservation_id != state.reservation_id
                or not self.owner.native_qualified(binding.piece, binding.episode)):
                raise PositionError("native custody changed")
            current_route = self.owner.current_route(binding, state.target, state.reservation_id)
            if current_route is None:
                raise PositionError("native route no longer ready")
            if replace(
                current_route,
                machine_state_revision=state.qualification.machine_state_revision
            ) != state.qualification:
                raise PositionError("native route qualification changed")
            code = delivery.validate_physical_claim(
                self.machine_id, state.reservation_id, state.custody,
                state="RELEASE_INTENT", revision=1,
                attempt_id=state.attempt_id, target_boundary=state.target.boundary,
                qualification=current_route)
            if code != "OK":
                raise PositionError(f"native dispatch refused: {code}")
        return state

    def before_first_dispatch(self, positioner) -> None:
        state = self._live(positioner)
        if state.first_dispatch_consumed or state.first_receipt is not None:
            raise PositionError("first dispatch permission already consumed")
        state.first_dispatch_consumed = True

    def before_trim(self, positioner) -> None:
        state = self._live(positioner)
        if (not state.first_dispatch_consumed or state.first_receipt is None
            or positioner._receipt is None):
            raise PositionError("trim lacks the accepted tracked index")

    def after_motor_start(self, positioner, receipt, *, first: bool) -> None:
        state = self.index
        if state is None or (first and not state.first_dispatch_consumed):
            raise PositionError("motor receipt has no guarded attempt")
        if first:
            state.first_receipt = receipt

    def _retain_uncertainty(self, exc: Exception, reason: str) -> None:
        """Fence in memory only; fault/stop must run before persistence."""
        state = self.index
        if state is None or state.stage == "uncertain":
            return
        state.stage = "uncertain"
        state.failure = exc
        state.error = f"{reason}: {exc}"
        if state.reservation_id is None:
            return
        state.uncertainty_key = str(uuid4())
        state.uncertainty_evidence = delivery.UncertaintyEvidence(
            state.custody.piece_uuid, state.custody.owner_incarnation,
            state.error,
            f"motor-start-uncertain:{state.uncertainty_key}", state.attempt_id)

    def motor_start_failed(self, exc: Exception) -> None:
        self._retain_uncertainty(exc, "tracked motor start acceptance is uncertain")

    def motion_failed(self, exc: Exception) -> None:
        """An accepted move without marker confirmation remains uncertain."""
        state = self.index
        if state is not None and state.first_dispatch_consumed:
            self._retain_uncertainty(exc, "accepted tracked index lacks marker confirmation")

    def persist_uncertainty(self) -> None:
        """One post-stop write or explicit same-payload retry; never motion."""
        self.owner.assert_locked()
        state = self.index
        if (state is None or state.stage != "uncertain" or state.reservation_id is None
            or state.uncertainty_result is not None):
            return
        if state.uncertainty_key is None or state.uncertainty_evidence is None:
            raise PositionError("frozen uncertainty evidence is unavailable")
        try:
            result = delivery.mark_uncertain(
                self.machine_id, state.reservation_id,
                expected_reservation_revision=1, request_key=state.uncertainty_key,
                evidence=state.uncertainty_evidence)
            if result.get("code") != "OK":
                raise PositionError(f"uncertainty record refused: {result.get('code')}")
        except Exception as record_exc:
            state.uncertainty_error = str(record_exc)
            raise
        state.uncertainty_result = result
        state.uncertainty_error = None

    def capture_confirmation(self, confirmation: ConfirmedIndex) -> None:
        self.owner.assert_locked()
        state = self.index
        if (state is None or state.stage != "armed"
            or not state.first_dispatch_consumed or state.first_receipt is None
            or confirmation.boundary != state.target.boundary):
            raise PositionError("marker confirmation lacks the guarded dispatch")
        state.confirmation = confirmation
        state.stage = "confirmed"
        if state.reservation_id is not None:
            state.exit_key = str(uuid4())
            marker_ref = (f"marker:{confirmation.source_epoch}:"
                          f"{confirmation.source_sequence}:"
                          f"{confirmation.source_capture_ns}")
            state.exit_evidence = delivery.ExitEvidence(
                state.custody, state.attempt_id, state.target.boundary,
                True, marker_ref, time.time())

    def persist_exit(self) -> None:
        self.owner.assert_locked()
        state = self.index
        if state is None or state.stage not in ("confirmed", "exit_persisted"):
            raise PositionError("no retained marker confirmation")
        if state.stage == "exit_persisted":
            return
        if state.reservation_id is not None:
            result = delivery.confirm_exit(
                self.machine_id, state.reservation_id,
                expected_reservation_revision=1, request_key=state.exit_key,
                evidence=state.exit_evidence)
            if result.get("code") != "OK" or result.get("attempt_id") != state.attempt_id:
                state.error = f"exit persistence refused: {result.get('code')}"
                raise PositionError(state.error)
        state.stage = "exit_persisted"

    def begin_completion(self, target: IndexTarget) -> None:
        self.owner.assert_locked()
        if self.index is None or self.index.target != target or self.index.stage != "exit_persisted":
            raise PositionError("FIFO completion requires durable exit")
        self.index.stage = "completion_attempted"

    def begin_handoff(self, events) -> None:
        state = self.index
        if state is None or state.stage != "completion_attempted":
            raise PositionError("guarded completion identity lost")
        expected = () if state.reservation_id is None else (state.binding.key,)
        actual = tuple((e.pocket.pocket_id, e.pocket.generation) for e in events)
        if actual != expected:
            state.error = "FIFO discharge differs from guarded exit"
            state.stage = "handoff_ambiguous"
            raise PositionError(state.error)
        state.stage = "handoff"

    def handoff_failed(self, exc: Exception) -> None:
        if self.index is not None:
            self.index.stage = "handoff_ambiguous"
            self.index.error = f"downstream handoff ambiguous: {exc}"

    def finish(self) -> None:
        if self.index is None or self.index.stage != "handoff":
            raise PositionError("guarded handoff incomplete")
        if self.index.reservation_id is not None:
            self._completion_waiting = (self.index.reservation_id,
                                        self.index.attempt_id)
        self.index = None

    def pause(self) -> None:
        state = self.index
        if (state is not None and state.stage in ("prepared", "armed")
            and not state.first_dispatch_consumed):
            state.stage = "paused_unissued"
            state.error = "paused before first motor dispatch"
