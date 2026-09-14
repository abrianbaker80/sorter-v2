import ast
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from perception.capture import PerceptionFrame
from perception.inference import InferenceWorker
from perception.service import PerceptionService
from perception.state import PieceObservation
from global_config import GlobalConfig, Timeouts
from subsystems.classification_channel import intake_routing as bridge_module
from subsystems.classification_channel import pocket_recognition as recognition_module
from subsystems.classification_channel.demand_planner import (
    AdvanceKind, C4DemandPlanner, ChuteObservation, Timing,
)
from subsystems.classification_channel.intake_routing import C4IntakeRoutingBridge
from subsystems.classification_channel.physical_fifo import PhysicalC4FIFO, PocketState
from subsystems.classification_channel.pocket_recognition import PocketRecognitionRouter


def sample(timestamp=100.0, count=1, source="carousel"):
    # A second load elsewhere on C4 must never count as a P6 multidrop or
    # contribute pixels. No tracker identity is needed for either observation.
    pieces = [PieceObservation(100, 30, 1, (2 + 8*i, 2, 8 + 8*i, 8)) for i in range(count)]
    pieces.append(PieceObservation(0, 180, 2, (20, 20, 28, 28)))
    return pieces, PerceptionFrame(source, timestamp, np.zeros((32, 32, 3), dtype=np.uint8))


@pytest.fixture
def workers(monkeypatch):
    pending = []

    class Worker:
        def __init__(self, *, target, daemon, name):
            assert daemon is True
            self.run = target

        def start(self):
            pending.append(self)

    monkeypatch.setattr(bridge_module, "Thread", Worker)
    return pending


def make(recognize=lambda crop: "A", timeout=5, max_workers=4):
    fifo = PhysicalC4FIFO(microsteps_per_revolution=1000)
    bridge = C4IntakeRoutingBridge(fifo, recognize=recognize, routing_timeout_s=timeout, max_workers=max_workers)
    return fifo, bridge


def deposit(bridge, now=0, evidence=None):
    load = bridge.confirmed_deposit(boundary=bridge.fifo.boundary, now=now,
                                   sample=evidence if evidence is not None else sample(100 + bridge.fifo.boundary))
    assert load is not None
    return load


def advance(fifo, count=1):
    for _ in range(count):
        target = fifo.prepare_index()
        fifo.complete_index(target, confirmed_microsteps=target.absolute_microsteps)


def test_singleton_paired_perception_intake_creates_one_pending_generation(workers):
    fifo, bridge = make()
    evidence = sample()
    # Exercise the actual existing paired read accessor; no camera is started.
    service = PerceptionService(channels={}, captures={}, runtimes={}, slots={}, workers={})
    worker = InferenceWorker.__new__(InferenceWorker)
    worker._latest_pieces_frame = (tuple(evidence[0]), evidence[1])
    service._workers[4] = worker
    load = bridge.confirmed_deposit(boundary=0, now=1, sample=service.read_pieces_and_frame(4))
    assert load is not None
    assert load == fifo.pockets[0]
    assert load.pocket_id == 0 and load.generation == 1
    assert load.state is PocketState.PENDING and load.metadata == ()
    assert sum(p.state is not PocketState.EMPTY for p in fifo.pockets) == 1
    assert len(workers) == 1
    assert bridge.poll(1)[0].at == 6


def test_result_routes_generation_after_motion_only_on_owner_poll(workers):
    fifo, bridge = make()
    load = deposit(bridge)
    advance(fifo, 3)
    workers[0].run()
    assert fifo.pockets[0] == load  # Worker has no FIFO write authority.
    assert bridge.poll(1) == ()
    assert fifo.pockets[0].state is PocketState.ROUTED
    assert fifo.pockets[0].destination == "A"
    assert fifo.pockets[0].generation == load.generation


def test_stale_result_after_physical_pocket_reuse_is_ignored(workers):
    fifo, bridge = make()
    old = deposit(bridge)
    advance(fifo, 10)
    current = deposit(bridge, now=1)
    assert current.pocket_id == old.pocket_id
    assert current.generation == old.generation + 1
    workers[0].run()
    assert not bridge.post_result(bridge.key_for(old), "wrong")
    bridge.poll(2)
    assert fifo.pockets[0] == current
    workers[1].run()
    bridge.poll(3)
    assert fifo.pockets[0].destination == "A"


def test_classifying_one_pocket_cannot_mutate_another(workers):
    fifo, bridge = make()
    first = deposit(bridge)
    advance(fifo)
    second = deposit(bridge, now=1)
    before = fifo.pockets
    workers[0].run()
    bridge.poll(2)
    assert fifo.pockets[first.pocket_id].state is PocketState.ROUTED
    assert fifo.pockets[second.pocket_id] == second
    assert fifo.pockets[1:] == before[1:]


@pytest.mark.parametrize("count", [0, 2, 3])
def test_ambiguous_or_multidrop_intake_is_discard_without_dispatch(workers, count):
    fifo, bridge = make()
    load = deposit(bridge, evidence=sample(count=count))
    assert load.state is PocketState.DISCARD
    assert len(workers) == 0
    assert len([p for p in fifo.pockets if p.state is not PocketState.EMPTY]) == 1


@pytest.mark.parametrize("evidence", [None, sample(source="c_channel_3"), sample(timestamp=float("nan"))])
def test_missing_or_wrong_camera_imagery_does_not_lose_confirmed_load(workers, evidence):
    fifo, bridge = make()
    bridge.confirmed_deposit(boundary=0, now=0, sample=evidence)
    assert fifo.pockets[0].state is PocketState.DISCARD
    assert not workers


def test_reused_frame_is_not_sent_for_a_different_pocket(workers):
    fifo, bridge = make()
    deposit(bridge, evidence=sample())
    advance(fifo)
    second = deposit(bridge, now=1, evidence=sample())
    assert second.state is PocketState.DISCARD
    assert len(workers) == 1


@pytest.mark.parametrize("exception", [RuntimeError("provider error"), TimeoutError("provider timeout")])
def test_provider_failure_discards_without_incident_or_transport_dependency(workers, exception):
    def recognize(crop):
        raise exception

    fifo, bridge = make(recognize)
    deposit(bridge)
    workers[0].run()
    bridge.poll(1)
    assert fifo.pockets[0].state is PocketState.DISCARD
    assert fifo.pending_index is None
    advance(fifo, 7)
    assert fifo.pockets[0].state is PocketState.EMPTY


@pytest.mark.parametrize("timeout", [0.5, 100])
def test_deadline_and_p0_fallback_transport_without_provider_completion(workers, timeout):
    fifo, bridge = make(timeout=timeout)
    planner = C4DemandPlanner(fifo, Timing(0.1, 0), discard_destination="reject")
    deposit(bridge)
    events = ()
    for i in range(7):
        now = float(i)
        decision = planner.plan(now, ChuteObservation("reject", {}), drain=True, deadlines=bridge.poll(now))
        assert decision.kind is (AdvanceKind.FEED if i == 0 else AdvanceKind.DRAIN)
        assert decision.index is not None
        events = planner.complete_index(decision.index, confirmed_microsteps=decision.index.absolute_microsteps,
                                        now=now + 0.1)
    assert len(events) == 1 and events[0].pocket.state is PocketState.DISCARD
    workers[0].run()
    bridge.poll(7)
    assert fifo.pockets[0].state is PocketState.EMPTY


def test_queued_success_cannot_beat_expired_deadline(workers):
    fifo, bridge = make(timeout=1)
    planner = C4DemandPlanner(fifo, Timing(0.1, 0), discard_destination="reject")
    deposit(bridge)
    workers[0].run()
    deadlines = bridge.poll(1)
    assert fifo.pockets[0].state is PocketState.PENDING
    planner.plan(1, ChuteObservation("reject", {}), deadlines=deadlines)
    assert fifo.pockets[0].state is PocketState.DISCARD


def test_duplicate_callback_and_duplicate_deposit_are_harmless(workers):
    fifo, bridge = make()
    load = deposit(bridge)
    key = bridge.key_for(load)
    assert bridge.confirmed_deposit(boundary=0, now=0, sample=sample()) is None
    assert len(workers) == 1
    assert bridge.post_result(key, "A")
    assert not bridge.post_result(key, "B")
    bridge.poll(1)
    assert not bridge.post_result(key, None)
    workers[0].run()
    assert fifo.pockets[0].destination == "A"


def test_epoch_segregation_and_nonblocking_close(workers):
    fifo, old = make()
    previous = deposit(old)
    new_fifo, new = make()
    current = deposit(new)
    assert previous == current
    assert not new.post_result(old.key_for(previous), "wrong epoch")
    old.close(1)
    workers[0].run()
    assert fifo.pockets[0].state is PocketState.DISCARD
    assert new_fifo.pockets[0].state is PocketState.PENDING
    with pytest.raises(RuntimeError, match="closed"):
        deposit(old, now=1)


def test_capacity_stays_bounded_across_expired_jobs_and_recovers(workers):
    fifo, bridge = make(max_workers=1)
    deposit(bridge)
    advance(fifo, 10)
    assert deposit(bridge, now=10).state is PocketState.DISCARD
    assert len(workers) == 1  # Old request still physically running.
    workers[0].run()
    advance(fifo)
    assert deposit(bridge, now=11).state is PocketState.PENDING
    assert len(workers) == 2


def test_crop_is_copied_from_paired_frame_before_async_dispatch(workers):
    received = []
    fifo, bridge = make(lambda crop: received.append(crop.copy()) or "A")
    evidence = sample()
    evidence[1].bgr[2:8, 2:8] = 17
    deposit(bridge, evidence=evidence)
    evidence[1].bgr[:] = 255
    workers[0].run()
    assert received[0].shape == (6, 6, 3)
    assert np.all(received[0] == 17)


def test_wrong_boundary_and_outstanding_index_cannot_assign_intake(workers):
    fifo, bridge = make()
    with pytest.raises(ValueError, match="boundary"):
        bridge.confirmed_deposit(boundary=1, now=0, sample=sample())
    fifo.prepare_index()
    with pytest.raises(RuntimeError, match="index"):
        deposit(bridge)
    assert all(p.generation == 0 for p in fifo.pockets)


def test_real_worker_cannot_block_transport_or_write_fifo():
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def recognize(crop):
        started.set()
        try:
            try:
                bridge.poll(0)
            except RuntimeError as exc:
                errors.append(str(exc))
            assert release.wait(5)
            return "A"
        finally:
            finished.set()

    fifo, bridge = make(recognize, max_workers=1)
    try:
        deposit(bridge)
        assert started.wait(5)
        assert fifo.pockets[0].state is PocketState.PENDING
        advance(fifo, 7)
        bridge.close(1)
        assert fifo.pockets[0].state is PocketState.EMPTY
    finally:
        release.set()
        assert finished.wait(5)
    assert errors == ["FIFO bridge requires its single owner thread"]


def response(items=None, colors=None):
    return {"items": items if items is not None else [{"id": "3001", "score": 0.95}],
            "colors": colors if colors is not None else [{"id": "5", "score": 0.98}]}


def router(**kwargs):
    # The lookup is deliberately the only interface exposed to recognition.
    class Profile:
        def getCategoryIdForPart(self, part_id: str, color_id: str = "any_color") -> str:
            return f"{color_id}-{part_id}"

    return PocketRecognitionRouter(gc=provider_config(), profile=Profile(), category_destinations={"5-3001": "A"},
                                   reachable_destinations=frozenset({"A"}), min_score=0.8,
                                   ambiguity_margin=0.05, **kwargs)


def provider_config():
    config = GlobalConfig.__new__(GlobalConfig)
    config.brickognize_dump_root = None
    config.timeouts = Timeouts()
    return config


def test_real_provider_client_and_profile_integrate_without_ownership(workers, monkeypatch):
    from classification import brickognize
    from sorting_profile import JsonSortingProfile

    requests = []

    def post(url, **kwargs):
        requests.append((url, kwargs))
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"items": [{"id": "3001", "score": 0.95, "category": "brick"}],
                                             "colors": [{"id": "5", "score": 0.98}]})

    # Stub only HTTP. Exercise JPEG encoding, existing provider decoding and
    # the actual category lookup, then apply through the single-writer bridge.
    monkeypatch.setattr(brickognize.requests, "post", post)
    profile = JsonSortingProfile.__new__(JsonSortingProfile)
    profile.part_to_category = {"5-3001": "bricks"}
    profile.default_category_id = "misc"
    recognize = PocketRecognitionRouter(gc=provider_config(), profile=profile, category_destinations={"bricks": "A"},
                                         reachable_destinations=frozenset({"A"}), min_score=0.8, ambiguity_margin=0.05)
    fifo, bridge = make(recognize)
    deposit(bridge)
    workers[0].run()
    bridge.poll(1)
    assert fifo.pockets[0].destination == "A"
    assert len(requests) == 1
    assert len(requests[0][1]["files"]) == 1
    assert requests[0][1]["files"][0][1][1].getvalue().startswith(b"\xff\xd8")


@pytest.mark.parametrize("payload", [None, {}, response(items=[]), response(colors=[]),
    response(items=[{"id": "3001", "score": 0.79}]),
    response(items=[{"id": "3001", "score": 0.95}, {"id": "3002", "score": 0.94}]),
    response(colors=[{"id": "5", "score": 0.95}, {"id": "6", "score": 0.95}]),
    response(items=[{"id": "3001", "score": float("nan")}]),
    response(items=[{"id": "3001", "score": True}]),
    response(items=[{"id": "", "score": 0.9}]),
    response(items=[{"id": "unmapped", "score": 0.99}]),
])
def test_ambiguous_malformed_or_unmapped_provider_result_is_discard(workers, monkeypatch, payload):
    monkeypatch.setattr(recognition_module, "_classifyImages", lambda gc, images: payload)
    fifo, bridge = make(router())
    deposit(bridge)
    workers[0].run()
    bridge.poll(1)
    assert fifo.pockets[0].state is PocketState.DISCARD


def test_unreachable_route_is_discard(workers, monkeypatch):
    monkeypatch.setattr(recognition_module, "_classifyImages", lambda gc, images: response())
    recognize = router()
    recognize._reachable = frozenset()
    fifo, bridge = make(recognize)
    deposit(bridge)
    workers[0].run()
    bridge.poll(1)
    assert fifo.pockets[0].state is PocketState.DISCARD


@pytest.mark.parametrize("color,expected", [(None, PocketState.DISCARD),
    ({"color_id": 5, "confidence": 0.99}, PocketState.ROUTED),
    ({"color_id": 5}, PocketState.DISCARD)])
def test_selected_color_provider_is_reused_without_fallback(workers, monkeypatch, color, expected):
    calls = []
    monkeypatch.setattr(recognition_module, "_classifyImages", lambda gc, images: response())

    def predict(gc, images, channels):
        calls.append((images, channels))
        return color

    monkeypatch.setattr(recognition_module.basically_services, "predictColor", predict)
    fifo, bridge = make(router(color_provider="hive_basically"))
    deposit(bridge)
    workers[0].run()
    bridge.poll(1)
    assert fifo.pockets[0].state is expected
    assert calls[0][1] == [4]
    assert calls[0][0][0].startswith(b"\xff\xd8")


def test_new_hot_path_has_no_legacy_ownership_or_runtime_activation():
    forbidden = ("known_object", "piece_transport", "pocket_ledger", "transfer_episode",
                 "project_harvest", "coordinator", "runtime_stats", "shared_variables",
                 "state_machine", "recognition.ClassificationChannelRecognizer")
    for module in (bridge_module, recognition_module):
        assert module.__file__ is not None
        tree = ast.parse(Path(module.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not any(word in (node.module or "") for word in forbidden)
            if isinstance(node, (ast.Name, ast.Attribute)):
                name = node.id if isinstance(node, ast.Name) else node.attr
                assert not any(word in name.lower() for word in ("uuid", "reserv", "transfer", "harvest", "reconcil", "pause"))
    backend = Path(bridge_module.__file__).parents[2]
    for runtime in (backend / "coordinator.py", backend / "subsystems/classification_channel/state_machine.py"):
        source = runtime.read_text()
        assert "intake_routing" not in source and "pocket_recognition" not in source
