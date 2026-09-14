import ast
from pathlib import Path

import numpy as np
import pytest

from perception.capture import PerceptionFrame
from perception.state import PieceObservation
from subsystems.classification_channel import intake_routing
from subsystems.classification_channel import fail_forward_runtime as runtime_module
from subsystems.classification_channel.demand_planner import (
    AdvanceKind, C4DemandPlanner, ChuteObservation, Timing,
)
from subsystems.classification_channel.fail_forward_runtime import (
    C4RuntimeCoordinator, IndexCommand, Lifecycle,
)
from subsystems.classification_channel.intake_routing import C4IntakeRoutingBridge
from subsystems.classification_channel.physical_fifo import PhysicalC4FIFO, PocketState


class Motor:
    def __init__(self):
        self.position = 0
        self.stopped = True
        self.commands = []
        self.target = 0
        self.accept = True

    def move_steps(self, steps):
        self.commands.append(steps)
        if not self.accept:
            return False
        self.target = self.position + steps
        self.stopped = False
        return True

    def finish(self):
        self.position = self.target
        self.stopped = True


class Chute:
    def __init__(self):
        self.aligned = "A"
        self.pending = None
        self.commands = []

    def observe(self, now):
        if self.pending and now >= self.pending.arrive_at:
            self.aligned = self.pending.destination
            self.pending = None
        return ChuteObservation(
            self.aligned, {"A": .2, "B": .2, "bin-discard": .2},
            self.pending.destination if self.pending else None,
            self.pending.arrive_at if self.pending else None,
        )

    def move(self, move):
        self.commands.append(move)
        self.pending = move


@pytest.fixture
def rig(monkeypatch):
    workers = []

    class Worker:
        def __init__(self, *, target, **kwargs):
            self.run = target

        def start(self):
            workers.append(self)

    monkeypatch.setattr(intake_routing, "Thread", Worker)
    fifo = PhysicalC4FIFO(microsteps_per_revolution=1000)
    bridge = C4IntakeRoutingBridge(fifo, recognize=lambda crop: "A", routing_timeout_s=20)
    motor, chute = Motor(), Chute()
    runtime = C4RuntimeCoordinator(bridge, C4DemandPlanner(
        fifo, Timing(1, .1, 1.5), discard_destination="bin-discard",
    ), motor, chute)
    return runtime, motor, chute, workers


def deposit(runtime, now, destination: str | None = "A"):
    frame = PerceptionFrame("carousel", 100 + runtime.fifo.boundary,
                            np.zeros((16, 16, 3), dtype=np.uint8))
    pocket = runtime.confirmed_deposit(
        boundary=runtime.fifo.boundary, now=now,
        sample=([PieceObservation(100, 30, 1, (2, 2, 8, 8))], frame),
    )
    assert pocket is not None
    if destination != "pending":
        assert runtime.bridge.post_result(runtime.bridge.key_for(pocket), destination)
    return pocket


def finish(runtime, motor, now):
    command = runtime.active_index
    assert command is not None
    motor.finish()
    assert runtime.acknowledge_index(command, stopped=motor.stopped,
                                     position=motor.position, now=now)
    return command


@pytest.mark.parametrize("destination, state", [("A", PocketState.ROUTED),
                                                 (None, PocketState.DISCARD)])
def test_singleton_seven_advances_one_physical_discharge(rig, destination, state):
    runtime, motor, chute, _ = rig
    pocket = deposit(runtime, 0, destination)
    runtime.start_drain(0)
    command = None
    for boundary in range(1, 8):
        decision = runtime.tick(boundary - 1)
        assert decision.kind is (AdvanceKind.FEED if boundary == 1 else AdvanceKind.DRAIN)
        assert runtime.fifo.boundary == boundary - 1  # ACK is not completion.
        command = finish(runtime, motor, boundary)
        assert runtime.fifo.boundary == boundary
        if boundary < 7:
            assert runtime.take_discharge_events() == ()
            assert runtime.fifo.station_of(pocket.pocket_id) == 6 - boundary
    events = runtime.take_discharge_events()
    assert len(events) == 1
    assert (events[0].pocket_id, events[0].generation, events[0].boundary) == (0, 1, 7)
    assert events[0].state is state
    assert events[0].destination == (destination or "bin-discard")
    assert events[0].metadata == ()
    assert motor.commands == [100] * 7
    assert command is not None
    assert not runtime.acknowledge_index(command, stopped=True, position=700, now=7)
    assert runtime.take_discharge_events() == ()
    assert runtime.planner.fall_clear_at == 8.5
    runtime.tick(8.49)
    assert runtime.lifecycle is Lifecycle.DRAINING
    runtime.tick(8.5)
    assert runtime.lifecycle is Lifecycle.DRAINED
    runtime.close(8.5)
    assert runtime.lifecycle is Lifecycle.CLOSED


def test_multiple_buffered_fifo_and_predictive_successive_destinations(rig):
    runtime, motor, chute, _ = rig
    pockets = []
    now = 0
    for destination in ("A", "B", None):
        pockets.append(deposit(runtime, now, destination))
        assert runtime.tick(now).kind is AdvanceKind.FEED
        now += 1
        finish(runtime, motor, now)
    assert sum(p.state is not PocketState.EMPTY for p in runtime.fifo.pockets) == 3
    runtime.start_drain(now)
    events = []
    for _ in range(60):
        runtime.tick(now)
        now += .5
        if runtime.active_index:
            finish(runtime, motor, now)
        events.extend(runtime.take_discharge_events())
        if runtime.lifecycle is Lifecycle.DRAINED:
            break
    assert runtime.lifecycle is Lifecycle.DRAINED
    assert [(e.pocket_id, e.generation) for e in events] == [(p.pocket_id, p.generation) for p in pockets]
    assert [e.boundary for e in events] == [7, 8, 9]
    assert [e.destination for e in events] == ["A", "B", "bin-discard"]
    assert [m.destination for m in chute.commands] == ["B", "bin-discard"]
    assert len(motor.commands) == 9  # Six empty P6 indexes, no invented loads.
    assert all(p.state is PocketState.EMPTY for p in runtime.fifo.pockets)


def test_classification_completes_during_other_pocket_motion(rig):
    runtime, motor, _, workers = rig
    first = deposit(runtime, 0, "pending")
    runtime.tick(0)
    finish(runtime, motor, 1)
    deposit(runtime, 1, "B")
    assert runtime.tick(1).kind is AdvanceKind.FEED
    workers[0].run()
    assert runtime.fifo.pockets[first.pocket_id].state is PocketState.PENDING
    assert runtime.tick(1.1).kind is AdvanceKind.HOLD  # Only the index is outstanding.
    assert runtime.fifo.pockets[first.pocket_id].destination == "A"
    assert len(motor.commands) == 2
    finish(runtime, motor, 2)


@pytest.mark.parametrize("timeout", [.5, 20])
def test_timeout_or_p0_deadline_discards_without_waiting(rig, timeout):
    runtime, motor, _, workers = rig
    # A separate bridge supplies the intended timeout through its public API.
    runtime = C4RuntimeCoordinator(C4IntakeRoutingBridge(
        runtime.fifo, recognize=lambda crop: "A", routing_timeout_s=timeout,
    ), runtime.planner, motor, runtime.chute)
    pocket = deposit(runtime, 0, "pending")
    runtime.start_drain(0)
    for i in range(7):
        assert runtime.tick(i).kind is not AdvanceKind.HOLD
        finish(runtime, motor, i + 1)
    event, = runtime.take_discharge_events()
    assert event.destination == "bin-discard"
    workers[0].run()
    runtime.tick(7)
    assert runtime.fifo.pockets[pocket.pocket_id].state is PocketState.EMPTY


def test_lookahead_prepositions_before_release(rig):
    runtime, motor, chute, _ = rig
    deposit(runtime, 0, "B")
    runtime.tick(0)
    assert chute.commands[0].destination == "B"
    assert chute.commands[0].depart_at == 0
    assert runtime.fifo.boundary == 0
    assert runtime.active_index is not None


def test_chute_departure_exactly_at_fall_clear(rig):
    runtime, motor, chute, _ = rig
    deposit(runtime, 0, "A")
    runtime.tick(0)
    finish(runtime, motor, 1)
    deposit(runtime, 1, "B")
    runtime.start_drain(1)
    for i in range(1, 7):
        runtime.tick(i)
        finish(runtime, motor, i + 1)
    assert runtime.take_discharge_events()[0].destination == "A"
    assert runtime.tick(8.49).kind is AdvanceKind.HOLD
    assert chute.commands == []
    decision = runtime.tick(8.5)
    assert decision.kind is AdvanceKind.DRAIN
    assert chute.commands[0].depart_at == 8.5
    assert chute.commands[0].destination == "B"


def test_ack_poll_retries_and_epoch_are_idempotent(rig):
    runtime, motor, _, _ = rig
    deposit(runtime, 0)
    runtime.tick(0)
    command = runtime.active_index
    assert command is not None
    for t in (.1, .2, .3):
        runtime.tick(t)
        assert not runtime.acknowledge_index(command, stopped=False, position=100, now=t)
    assert runtime.fifo.boundary == 0
    assert motor.commands == [100]
    assert not runtime.acknowledge_index(IndexCommand(object(), command.target),
                                         stopped=True, position=100, now=.3)
    motor.finish()
    runtime.start_drain(1)
    runtime.tick(1)  # Poll confirms first index and issues the next one.
    assert runtime.fifo.boundary == 1
    assert runtime.active_index != command
    assert not runtime.acknowledge_index(command, stopped=True, position=100, now=1)
    assert runtime.fifo.boundary == 1
    assert motor.commands == [100, 100]


def test_stale_generation_result_after_full_rotor_turn(rig):
    runtime, motor, _, workers = rig
    old = deposit(runtime, 0, "pending")
    old_key = runtime.bridge.key_for(old)
    now = 0
    # Feed successive pockets to turn through one full revolution.
    for boundary in range(10):
        if boundary:
            deposit(runtime, now)
            workers[-1].run()
        while runtime.tick(now).kind is AdvanceKind.HOLD:
            now += 2
        now += 2
        finish(runtime, motor, now)
    new = deposit(runtime, now, "pending")
    assert new.pocket_id == old.pocket_id and new.generation == old.generation + 1
    assert not runtime.bridge.post_result(old_key, "B")
    runtime.tick(now)
    assert runtime.fifo.pockets[new.pocket_id].state is PocketState.PENDING


def test_empty_drain_and_intake_lifecycle(rig):
    runtime, motor, _, _ = rig
    with pytest.raises(RuntimeError, match="drain"):
        runtime.close(0)
    assert runtime.tick(0).kind is AdvanceKind.HOLD
    runtime.start_drain(0)
    with pytest.raises(RuntimeError, match="intake"):
        runtime.confirmed_deposit(boundary=0, now=0, sample=None)
    runtime.tick(0)
    runtime.tick(1)
    assert runtime.lifecycle is Lifecycle.DRAINED
    assert motor.commands == []
    assert runtime.take_discharge_events() == ()


@pytest.mark.parametrize("failure", ["reject", "uncertain", "origin"])
def test_motor_failure_retains_state_and_never_reissues(rig, failure, monkeypatch):
    runtime, motor, _, _ = rig
    deposit(runtime, 0)
    if failure == "reject":
        motor.accept = False
    elif failure == "origin":
        motor.position = 1
    else:
        def uncertain(steps):
            motor.commands.append(steps)
            raise OSError("lost command receipt")
        monkeypatch.setattr(motor, "move_steps", uncertain)
    with pytest.raises((RuntimeError, OSError)):
        runtime.tick(0)
    count = len(motor.commands)
    assert runtime.lifecycle is Lifecycle.FAULTED
    assert runtime.fifo.boundary == 0
    runtime.tick(1)
    assert len(motor.commands) == count
    assert runtime.fifo.pockets[0].state is PocketState.ROUTED


def test_latched_chute_submission_is_not_replayed_or_inferred_arrived(rig, monkeypatch):
    runtime, motor, chute, _ = rig
    monkeypatch.setattr(chute, "observe", lambda now: ChuteObservation("A", {"B": .2}))
    deposit(runtime, 0, "B")
    runtime.start_drain(0)
    for i in range(6):
        runtime.tick(i)
        finish(runtime, motor, i + 1)
    assert runtime.tick(6).kind is AdvanceKind.HOLD
    assert runtime.tick(7).kind is AdvanceKind.HOLD
    assert len(chute.commands) == 1
    assert runtime.fifo.boundary == 6


def test_uncertain_motor_receipt_can_record_completion_without_resuming(rig, monkeypatch):
    runtime, motor, _, _ = rig
    deposit(runtime, 0)
    submit = motor.move_steps

    def uncertain(steps):
        submit(steps)
        raise OSError("receipt lost after submission")

    monkeypatch.setattr(motor, "move_steps", uncertain)
    with pytest.raises(OSError):
        runtime.tick(0)
    assert runtime.fifo.pending_index is not None
    finish(runtime, motor, 1)
    assert runtime.fifo.boundary == 1
    assert runtime.lifecycle is Lifecycle.FAULTED
    runtime.tick(2)
    assert motor.commands == [100]


def test_chute_submission_failure_cannot_launch_release(rig, monkeypatch):
    runtime, motor, chute, _ = rig
    deposit(runtime, 0, "B")

    def reject(move):
        raise OSError("chute rejected")

    monkeypatch.setattr(chute, "move", reject)
    with pytest.raises(OSError):
        runtime.tick(0)
    assert motor.commands == []
    assert runtime.lifecycle is Lifecycle.FAULTED
    runtime.tick(1)
    assert motor.commands == []


def test_optional_metadata_and_measured_release_survive_discharge(rig):
    runtime, motor, _, _ = rig
    # The accepted FIFO permits optional opaque metadata; coordinator emits
    # that immutable snapshot without interpreting it as transport authority.
    pocket = runtime.fifo.deposit({"category": "brick"})
    runtime.fifo.resolve(pocket.pocket_id, pocket.generation, PocketState.ROUTED,
                         destination="A")
    runtime.start_drain(0)
    for i in range(6):
        runtime.tick(i)
        finish(runtime, motor, i + 1)
    runtime.tick(6)
    command = runtime.active_index
    assert command is not None
    motor.finish()
    assert runtime.acknowledge_index(command, stopped=True, position=700,
                                     now=8, physical_release_at=7)
    assert runtime.planner.fall_clear_at == 8.5
    event, = runtime.take_discharge_events()
    assert event.metadata == (("category", "brick"),)
    assert not runtime.acknowledge_index(command, stopped=True, position=700, now=9)
    assert runtime.planner.fall_clear_at == 8.5


def test_new_runtime_imports_only_accepted_layers_and_no_production_selection():
    source = Path(runtime_module.__file__).read_text()
    tree = ast.parse(source)
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imports == {"collections", "dataclasses", "enum", "math", "threading", "typing",
                       "perception.capture", "perception.state", "demand_planner",
                       "intake_routing", "physical_fifo"}
    forbidden = ("PieceTransport", "KnownObject", "piece_uuid", "reservation",
                 "reconciliation", "distribution_ready", "harvest", "tracker")
    assert not any(word.lower() in source.lower() for word in forbidden)
    backend = Path(runtime_module.__file__).parents[2]
    assert "fail_forward_runtime" not in (backend / "coordinator.py").read_text()
