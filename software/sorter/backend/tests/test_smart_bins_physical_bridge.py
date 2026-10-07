"""Isolated native C4 bridge qualification with the real FIFO and marker rig."""

from __future__ import annotations

# The imported fixture sets temporary SQLite/config paths before backend imports.
import time
from fractions import Fraction
from threading import RLock

import pytest

from test_smart_bins_delivery import prepared, qualification, connect
import smart_bins_delivery as delivery
import smart_bins_service as service
from defs.known_object import KnownObject
from subsystems.classification_channel.marker_positioner import PositionError
from subsystems.classification_channel.physical_fifo import (
    PhysicalC4FIFO, Pocket, PocketState,
)
from subsystems.classification_channel.physical_runtime import PhysicalC4Runtime
from subsystems.classification_channel.smart_bins_physical_bridge import PhysicalNativeBridge
from subsystems.classification_channel.transfer_episode import TransferEpisode
from test_marker_positioner import Rig
from test_physical_c4_runtime import Chute


class Owner:
    owner_incarnation = "owner"

    def __init__(self):
        self.lock = RLock()
        self.reservations = {}
        self.route_ready = True
        self.empty_ready = True
        self.native = True

    def assert_locked(self):
        if not self.lock._is_owned():
            raise RuntimeError("lifecycle/controller owner locks are required")

    def native_qualified(self, piece, episode):
        self.assert_locked()
        return self.native and not getattr(piece, "harvest_allocation_ref", None)

    def reservation_for(self, piece, episode, custody):
        self.assert_locked()
        return self.reservations.get(piece.uuid)

    def release_evidence(self, binding, target, reservation_id):
        self.assert_locked()
        now = time.time()
        custody = delivery.CustodyRef(
            binding.piece.uuid, self.owner_incarnation, binding.episode.episode_id,
            binding.pocket_id, binding.generation)
        return qualification(), delivery.ReleaseEvidence(
            custody, target.boundary, "BIN", "s0", "c0", True, self.route_ready,
            "current-owner", "aligned-route", now, now + 4)

    def current_route(self, binding, target, reservation_id):
        self.assert_locked()
        return qualification() if self.route_ready else None

    def empty_route_ready(self, target, pocket):
        self.assert_locked()
        return self.empty_ready


class Bench:
    def __init__(self, owner, *, gains=()):
        self.owner = owner
        self.rig = Rig(gains=gains)
        self.rig.bind()
        self.chute = Chute()
        self.bridge = PhysicalNativeBridge("m", owner)
        with owner.lock:
            self.runtime = PhysicalC4Runtime(
                PhysicalC4FIFO(microsteps_per_revolution=Fraction(52000, 3)),
                self.rig.p, speed=500, distribution=self.chute,
                native_bridge=self.bridge)
        self.binding = None
        self.reservation_id = None

    def add_native_piece(self):
        piece = KnownObject()
        ep = TransferEpisode(
            self.runtime.fifo.boundary, self.runtime.fifo.intake_pocket_id,
            self.rig.clock(), time.time(), leader_id=42)
        pocket = self.runtime.fifo.pockets[self.runtime.fifo.intake_pocket_id]
        request = service.ReservationRequest(
            piece_uuid=piece.uuid, route_attempt="first", sorting_session_id="session",
            group_key_id="A", owner_incarnation=self.owner.owner_incarnation,
            episode_id=ep.episode_id, pocket_index=pocket.pocket_id,
            pocket_generation=pocket.generation + 1)
        with self.owner.lock:
            route = qualification()
            preview = service.preview(request, route)
            assert preview["code"] == "OK"
            claim = service.reserve(
                request, route, expected_state_revision=preview["state_revision"],
                expected_qualification_hash=preview["qualification_hash"],
                request_key=f"reserve-{piece.uuid}")
            assert claim["code"] == "OK"
            self.owner.reservations[piece.uuid] = claim["reservation_id"]
            binding = self.runtime.reserve(piece, ep)
            self.runtime.finish_handoff(binding, arrived=True)
            assert self.runtime.fifo.resolve(
                *binding.key, PocketState.ROUTED, destination="s0")
        self.binding = binding
        self.reservation_id = claim["reservation_id"]
        return binding

    def tick(self, *, allow_motion=True):
        self.rig.clock.advance()
        with self.owner.lock:
            self.runtime.tick(self.rig.clock(), allow_motion=allow_motion)

    def advance(self):
        old = self.runtime.fifo.boundary
        for _ in range(180):
            self.tick()
            if self.runtime.fifo.boundary > old:
                return
        pytest.fail("index did not complete")

    def at_native_exit(self):
        assert self.binding is not None
        for _ in range(6):
            self.advance()
        assert self.runtime.fifo.boundary == 6
        assert self.runtime.fifo.station_of(self.binding.pocket_id) == 0

    def claim(self):
        return service.lookup_reservation("m", self.reservation_id)


@pytest.fixture
def bench(prepared):
    owner = Owner()
    rig = Bench(owner)
    rig.add_native_piece()
    rig.at_native_exit()
    return rig


def test_intent_precedes_first_motor_and_exit_precedes_fifo_and_discharge(bench):
    seen = []
    original_start = bench.rig.start
    original_discharge = bench.chute.discharge

    def start(degrees, speed, token):
        row = bench.claim()
        assert row["state"] == "RELEASE_INTENT"
        assert bench.runtime.fifo.boundary == 6
        seen.append("intent-before-motor")
        return original_start(degrees, speed, token)

    def discharge(event, binding):
        assert bench.claim()["state"] == "EXIT_CONFIRMED"
        assert bench.runtime.fifo.boundary == 7
        seen.append("exit-before-discharge")
        original_discharge(event, binding)

    bench.rig.start = start
    bench.chute.discharge = discharge
    bench.advance()
    assert seen == ["intent-before-motor", "exit-before-discharge"]
    assert [binding for _, binding in bench.chute.exits] == [bench.binding]
    assert bench.binding.key not in bench.runtime.bindings
    assert bench.bridge.index is None
    assert bench.claim()["state"] == "EXIT_CONFIRMED"


def test_intent_failure_and_ambiguous_ack_keep_exact_target_without_motion(
    bench, prepared, monkeypatch,
):
    actual = delivery.prepare_release
    key_seen = []

    def lose_ack(*args, **kwargs):
        result = actual(*args, **kwargs)
        key_seen.append(kwargs["request_key"])
        raise OSError("lost intent acknowledgement")

    monkeypatch.setattr(delivery, "prepare_release", lose_ack)
    motor_calls = len(bench.rig.commands)
    with pytest.raises(OSError, match="lost intent"):
        bench.tick()
    state = bench.bridge.index
    assert state.stage == "prepared"
    assert bench.runtime._pending is None
    assert bench.runtime.planner._active.index == state.target
    assert bench.runtime.fifo.pending_index == state.target
    assert bench.runtime.current(bench.binding)
    assert not bench.runtime.can_admit
    assert len(bench.rig.commands) == motor_calls
    assert bench.claim()["state"] == "RELEASE_INTENT"
    monkeypatch.setattr(delivery, "prepare_release", actual)
    bench.tick()
    assert state.intent_key == key_seen[0] and state.stage == "armed"
    assert bench.runtime._pending == state.target
    bench.advance()
    assert bench.claim()["state"] == "EXIT_CONFIRMED"
    with connect(prepared) as conn:
        count = conn.execute(
            "SELECT count(*) FROM smart_bin_release_attempts WHERE reservation_id=?",
            (bench.reservation_id,)).fetchone()[0]
    assert count == 1


def test_uncommitted_intent_failure_retains_target_and_custody(bench, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("SQLite write failed")

    monkeypatch.setattr(delivery, "prepare_release", fail)
    before = len(bench.rig.commands)
    with pytest.raises(OSError, match="SQLite write failed"):
        bench.tick()
    assert len(bench.rig.commands) == before
    assert bench.claim()["state"] == "RESERVED"
    assert bench.runtime.fifo.pending_index == bench.bridge.index.target
    assert bench.runtime.current(bench.binding) and not bench.runtime.can_admit


@pytest.mark.parametrize("change", [
    "missing", "consumed", "owner", "target", "route", "contradiction",
])
def test_late_or_missing_first_dispatch_authorization_never_calls_motor(
    bench, prepared, change,
):
    bench.tick()  # intent committed; request_index only fences a stopped rotor
    state = bench.bridge.index
    assert state.stage == "armed" and not state.first_dispatch_consumed
    before = len(bench.rig.commands)
    if change == "missing":
        bench.bridge.index = None
    elif change == "consumed":
        state.first_dispatch_consumed = True
    elif change == "owner":
        bench.owner.owner_incarnation = "replacement"
    elif change == "target":
        bench.rig.p.pending += 1
    elif change == "route":
        bench.owner.route_ready = False
    else:
        with connect(prepared) as conn:
            conn.execute(
                "INSERT INTO smart_bin_discrepancies "
                "(id,machine_id,reservation_id,cycle_id,kind,status,evidence_ref,"
                "details_json,created_at) VALUES('late','m',?,'c0','NATIVE_UNCERTAIN',"
                "'open','late-contradiction','{}',?)",
                (bench.reservation_id, time.time()))
    with pytest.raises(PositionError):
        for _ in range(20):
            bench.tick()
    assert len(bench.rig.commands) == before
    assert bench.runtime.fifo.boundary == 6
    assert bench.runtime.current(bench.binding)


def test_unknown_motor_acceptance_consumes_first_permission_and_records_uncertainty(
    bench,
):
    original = bench.rig.start

    def ack_then_raise(degrees, speed, token):
        original(degrees, speed, token)
        raise OSError("ACK lost after command acceptance")

    bench.rig.start = ack_then_raise
    bench.tick()
    with pytest.raises(PositionError, match="ACK lost"):
        for _ in range(20):
            bench.tick()
    assert bench.bridge.index.first_dispatch_consumed
    assert bench.bridge.index.stage == "uncertain"
    assert bench.claim()["state"] == "UNCERTAIN"
    before = len(bench.rig.commands)
    for _ in range(3):
        bench.tick()
    assert len(bench.rig.commands) == before == 7
    assert bench.runtime.fifo.boundary == 6


def test_accepted_move_without_marker_confirmation_records_uncertainty(bench):
    bench.tick()
    for _ in range(20):
        bench.tick()
        if bench.bridge.first_dispatch_consumed:
            break
    assert bench.bridge.first_dispatch_consumed
    bench.rig.marker_missing = True
    with pytest.raises(PositionError, match="deadline"):
        for _ in range(180):
            bench.tick()
    assert bench.bridge.index.stage == "uncertain"
    assert bench.claim()["state"] == "UNCERTAIN"
    assert bench.runtime.fifo.boundary == 6
    assert len(bench.rig.commands) == 7


def test_bounded_trim_uses_original_attempt_and_no_second_first_permit(
    prepared,
):
    owner = Owner()
    b = Bench(owner, gains=(1, 1, 1, 1, 1, 1, 0.95, 1))
    b.add_native_piece()
    b.at_native_exit()
    b.advance()
    assert len(b.rig.commands) == 8
    with connect(prepared) as conn:
        attempts = conn.execute(
            "SELECT count(*) FROM smart_bin_release_attempts WHERE reservation_id=?",
            (b.reservation_id,)).fetchone()[0]
    assert attempts == 1
    assert b.claim()["state"] == "EXIT_CONFIRMED"


def test_exit_ack_loss_retains_marker_then_retries_once_without_motion(
    bench, monkeypatch,
):
    actual = delivery.confirm_exit
    keys = []

    def ack_lost(*args, **kwargs):
        result = actual(*args, **kwargs)
        keys.append(kwargs["request_key"])
        raise OSError("exit ACK lost")

    monkeypatch.setattr(delivery, "confirm_exit", ack_lost)
    bench.tick()
    with pytest.raises(OSError, match="exit ACK lost"):
        for _ in range(20):
            bench.tick()
    state = bench.bridge.index
    confirmation = state.confirmation
    before = len(bench.rig.commands)
    assert state.stage == "confirmed"
    assert bench.runtime.fifo.boundary == 6
    assert bench.runtime.current(bench.binding)
    assert not bench.chute.exits and not bench.runtime.can_admit
    monkeypatch.setattr(delivery, "confirm_exit", actual)
    bench.tick()
    assert state.exit_key == keys[0] and state.confirmation is confirmation
    assert len(bench.rig.commands) == before
    assert bench.runtime.fifo.boundary == 7
    assert len(bench.chute.exits) == 1
    bench.tick(allow_motion=False)
    assert len(bench.chute.exits) == 1


def test_downstream_callback_failure_retains_identity_without_repeating(
    bench,
):
    def fail(event, binding):
        raise OSError("handoff failed after exit")

    bench.chute.discharge = fail
    bench.tick()
    with pytest.raises(OSError, match="handoff failed"):
        for _ in range(20):
            bench.tick()
    state = bench.bridge.index
    assert state.stage == "handoff_ambiguous"
    assert state.confirmation is not None
    assert state.binding is bench.binding
    assert bench.claim()["state"] == "EXIT_CONFIRMED"
    assert bench.runtime.fifo.boundary == 7
    assert bench.binding.key in bench.runtime.bindings
    motor_calls = len(bench.rig.commands)
    bench.tick()
    assert len(bench.rig.commands) == motor_calls
    assert not bench.runtime.can_admit


def test_pausing_unissued_index_fences_it_but_accepted_move_completes(bench):
    bench.tick()
    before = len(bench.rig.commands)
    with bench.owner.lock:
        bench.runtime.pause()
    for _ in range(4):
        bench.tick()
    assert len(bench.rig.commands) == before
    assert bench.bridge.index.stage == "paused_unissued"
    with bench.owner.lock, pytest.raises(PositionError, match="owned marker index"):
        bench.runtime.resume(bench.rig.clock())


def test_pause_after_accepted_motor_allows_finite_completion(bench):
    bench.tick()
    for _ in range(20):
        bench.tick()
        if bench.bridge.first_dispatch_consumed:
            break
    assert bench.bridge.first_dispatch_consumed
    with bench.owner.lock:
        bench.runtime.pause()
    bench.advance()
    assert bench.runtime.fifo.boundary == 7
    assert bench.claim()["state"] == "EXIT_CONFIRMED"


def test_new_owner_does_not_reconstruct_permit_from_outstanding_intent(
    bench,
):
    bench.tick()
    assert bench.claim()["state"] == "RELEASE_INTENT"
    other = Owner()
    other.owner_incarnation = "new-process"
    fresh = Bench(other)
    assert fresh.bridge.recovery_blocker
    assert not fresh.runtime.can_admit
    with other.lock, pytest.raises(PositionError, match="missing exact"):
        fresh.rig.p.request_index(1, 500)
    assert not fresh.rig.commands


def test_proven_empty_index_and_stale_empty_proof(prepared):
    owner = Owner()
    b = Bench(owner)
    b.add_native_piece()
    b.tick()
    assert b.bridge.index.reservation_id is None
    assert b.bridge.index.pocket.state is PocketState.EMPTY
    owner.empty_ready = False
    with pytest.raises(PositionError, match="empty exit proof"):
        for _ in range(20):
            b.tick()
    assert not b.rig.commands and b.runtime.fifo.boundary == 0


def test_unknown_exit_and_guarded_recovery_refuse_before_motion(prepared):
    owner = Owner()
    b = Bench(owner)
    b.add_native_piece()
    with owner.lock:
        b.runtime.fifo._pockets[4] = Pocket(
            4, 1, PocketState.DISCARD, deposited_boundary=0,
            metadata=(("unknown", "true"),))
        with pytest.raises(PositionError, match="durable permit"):
            b.runtime.recover()
        with pytest.raises(PositionError, match="durable permit"):
            b.rig.p.begin_target_trim(1, 500)
    with pytest.raises(PositionError, match="lacks native custody"):
        b.tick()
    assert not b.rig.commands
    assert b.bridge.refused_target is not None
    assert not b.runtime.can_admit
    b.tick()
    assert not b.rig.commands


def test_missing_hook_refuses_guarded_construction_and_legacy_still_indexes(
    prepared,
):
    owner = Owner()
    rig = Rig()
    rig.bind()
    rig.p.install_dispatch_guard = None
    with owner.lock, pytest.raises(PositionError, match="lacks the required"):
        PhysicalC4Runtime(
            PhysicalC4FIFO(microsteps_per_revolution=Fraction(52000, 3)),
            rig.p, speed=500, distribution=Chute(),
            native_bridge=PhysicalNativeBridge("m", owner))
    legacy_rig = Rig()
    legacy_rig.bind()
    legacy = PhysicalC4Runtime(
        PhysicalC4FIFO(microsteps_per_revolution=Fraction(52000, 3)),
        legacy_rig.p, speed=500, distribution=Chute())
    piece = KnownObject()
    episode = TransferEpisode(0, 0, legacy_rig.clock(), time.time(), leader_id=42)
    binding = legacy.reserve(piece, episode)
    legacy.finish_handoff(binding, arrived=True)
    for _ in range(160):
        legacy_rig.clock.advance()
        legacy.tick(legacy_rig.clock())
        if legacy.fifo.boundary == 1:
            break
    assert legacy.fifo.boundary == 1 and len(legacy_rig.commands) == 1


def _r2_inject_motion_failure(bench, monkeypatch, phase):
    """Drive the real positioner to an unknown start or a post-move sample fault."""
    failure = OSError(f"original {phase} failure")
    bench.tick()
    if phase == "start":
        original_start = bench.rig.start

        def accepted_without_ack(degrees, speed, token):
            original_start(degrees, speed, token)
            raise failure

        monkeypatch.setattr(bench.rig, "start", accepted_without_ack)
    else:
        for _ in range(20):
            bench.tick()
            if bench.bridge.first_dispatch_consumed:
                break
        assert bench.bridge.first_dispatch_consumed

        def sample_failed():
            raise failure

        monkeypatch.setattr(bench.rig, "sample", sample_failed)
    return failure


@pytest.mark.parametrize("phase", ["start", "marker"])
@pytest.mark.parametrize("write", ["success", "unavailable", "ack_lost"])
def test_r2_stop_precedes_uncertainty_and_failed_recording_replays_frozen_payload(
    bench, prepared, monkeypatch, phase, write,
):
    failure = _r2_inject_motion_failure(bench, monkeypatch, phase)
    state = bench.bridge.index
    identity = (state.target, state.binding, state.custody, state.attempt_id)
    actual = delivery.mark_uncertain
    events, requests = [], []
    fail_recording = write != "success"

    def record(*args, **kwargs):
        events.append("persist")
        requests.append((args, kwargs.copy()))
        if fail_recording and write == "unavailable":
            raise OSError("uncertainty database unavailable")
        result = actual(*args, **kwargs)
        if fail_recording and write == "ack_lost":
            raise OSError("uncertainty acknowledgement lost")
        return result

    def stop(reason):
        events.append("stop")
        bench.rig.failures.append(reason)

    monkeypatch.setattr(delivery, "mark_uncertain", record)
    monkeypatch.setattr(bench.rig.p, "on_fault", stop)
    with pytest.raises(PositionError, match=f"original {phase} failure"):
        for _ in range(20):
            bench.tick()
    assert events == ["stop", "persist"]
    assert bench.rig.failures == [str(failure)]
    assert bench.rig.p.fault_exception is failure
    assert state.failure is failure
    assert state.first_dispatch_consumed and state.stage == "uncertain"
    assert (state.target, state.binding, state.custody, state.attempt_id) == identity
    assert state.uncertainty_key == requests[0][1]["request_key"]
    assert state.uncertainty_evidence is requests[0][1]["evidence"]
    assert str(failure) in state.uncertainty_evidence.reason
    assert bench.runtime.current(bench.binding)
    assert not bench.runtime.can_admit
    original_error = state.error
    before = len(bench.rig.commands)
    for _ in range(3):
        bench.tick()
    assert len(bench.rig.commands) == before == 7
    if fail_recording:
        assert state.uncertainty_error
        fail_recording = False
        with bench.owner.lock:
            bench.bridge.persist_uncertainty()
        assert requests[1] == requests[0]
        assert requests[1][1]["evidence"] is requests[0][1]["evidence"]
        assert state.uncertainty_error is None
    with bench.owner.lock:
        bench.bridge.persist_uncertainty()
    assert len(requests) == (1 if write == "success" else 2)
    assert state.error == original_error and state.failure is failure
    assert bench.claim()["state"] == "UNCERTAIN"
    with connect(prepared) as conn:
        count = conn.execute(
            "SELECT count(*) FROM smart_bin_discrepancies WHERE reservation_id=?",
            (bench.reservation_id,)).fetchone()[0]
    assert count == 1


@pytest.mark.parametrize("phase", ["start", "marker"])
def test_r2_guard_bookkeeping_exception_cannot_suppress_original_stop(
    bench, monkeypatch, phase,
):
    failure = _r2_inject_motion_failure(bench, monkeypatch, phase)
    state = bench.bridge.index
    bookkeeping_failure = RuntimeError("guard bookkeeping failed")

    def broken_bookkeeping(exc, **kwargs):
        raise bookkeeping_failure

    hook = "motor_start_failed" if phase == "start" else "motion_failed"
    monkeypatch.setattr(bench.bridge, hook, broken_bookkeeping)
    with pytest.raises(Exception) as caught:
        for _ in range(20):
            bench.tick()
    assert bench.rig.failures == [str(failure)]
    assert isinstance(caught.value, PositionError)
    assert bench.rig.p.fault_exception is failure
    assert bookkeeping_failure in bench.rig.p.fault_bookkeeping_errors
    assert state.first_dispatch_consumed
    assert state.binding is bench.binding and state.attempt_id
    assert bench.runtime.current(bench.binding)
    assert not bench.runtime.can_admit
    before = len(bench.rig.commands)
    with pytest.raises(PositionError, match=f"original {phase} failure"):
        bench.tick()
    assert len(bench.rig.commands) == before == 7


@pytest.mark.parametrize("boundary", ["preparation", "retry", "request", "dispatch"])
def test_r2_empty_index_owner_change_refuses_original_authorization(
    prepared, monkeypatch, boundary,
):
    owner = Owner()
    b = Bench(owner)
    b.add_native_piece()
    if boundary == "preparation":
        original_empty_ready = owner.empty_route_ready

        def replace_during_preparation(target, pocket):
            proof = original_empty_ready(target, pocket)
            owner.owner_incarnation = "replacement"
            return proof

        monkeypatch.setattr(owner, "empty_route_ready", replace_during_preparation)
        with pytest.raises(PositionError, match="owner"):
            b.tick()
    elif boundary == "request":
        original_request = b.rig.p.request_index

        def replace_before_request(target, speed):
            owner.owner_incarnation = "replacement"
            original_request(target, speed)

        monkeypatch.setattr(b.rig.p, "request_index", replace_before_request)
        with pytest.raises(PositionError, match="owner"):
            b.tick()
    else:
        b.tick()
        state = b.bridge.index
        target, token, pocket = state.target, b.rig.token, state.pocket
        owner.owner_incarnation = "replacement"
        if boundary == "retry":
            with owner.lock, pytest.raises(PositionError, match="owner"):
                b.bridge.prepare_target(target)
        else:
            with pytest.raises(PositionError, match="owner"):
                for _ in range(20):
                    b.tick()
        assert b.rig.token == token
        assert b.runtime.fifo.pending_index == target
        assert b.runtime.fifo.pockets[pocket.pocket_id] == pocket
    state = b.bridge.index
    assert state.owner_incarnation == "owner"
    assert state.reservation_id is None and state.attempt_id is None
    assert not b.rig.commands
    assert not b.runtime.can_admit
    owner.owner_incarnation = "owner"
    with owner.lock, pytest.raises(PositionError):
        b.bridge.prepare_target(state.target)
    assert not b.rig.commands


def test_r2_same_owner_empty_index_completes_without_fabricated_claim(prepared):
    owner = Owner()
    b = Bench(owner)
    b.add_native_piece()
    b.tick()
    state = b.bridge.index
    assert state.reservation_id is None
    b.advance()
    assert b.runtime.fifo.boundary == 1
    assert len(b.rig.commands) == 1 and not b.chute.exits
    assert b.bridge.index is None
    assert b.claim()["state"] == "RESERVED"
    with connect(prepared) as conn:
        assert conn.execute("SELECT count(*) FROM smart_bin_reservations").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM smart_bin_release_attempts").fetchone()[0] == 0
