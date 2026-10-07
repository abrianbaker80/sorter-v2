"""Inactive native exit through the real distributor, transport and Sending."""

from __future__ import annotations

# This fixture establishes temporary SQLite/config paths before backend imports.
from test_smart_bins_delivery import prepared, connect, qualification

import queue
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

import local_state
import piece_records
import smart_bins_delivery as delivery
import smart_bins_service as service
from defs.known_object import KnownObject, PieceStage
from piece_transport import ClassificationChannelTransport
from run_recorder import RunRecorder
from subsystems.bus import TickBus
from subsystems.classification_channel.physical_distribution import PhysicalDistribution
from subsystems.classification_channel.physical_fifo import PocketState
from subsystems.classification_channel.transfer_episode import TransferEpisode
from subsystems.distribution.sending import CHUTE_SETTLE_MS, Sending
from subsystems.distribution.states import DistributionState
from subsystems.shared_variables import SharedVariables
from smart_bins_native_completion import NativeCompletionAdapter
from test_distribution_sending import _GlobalConfig
from test_smart_bins_physical_bridge import Bench, Owner


class _RejectOwner(Owner):
    def release_evidence(self, binding, target, reservation_id):
        route, evidence = super().release_evidence(binding, target, reservation_id)
        return route, replace(evidence, destination_kind="REJECT", slot_id=None,
                              cycle_id=None)


class _Progress:
    def __init__(self):
        self.calls = 0
        self.fail = False

    def record(self, *identity):
        self.calls += 1
        if self.fail:
            raise RuntimeError("progress acknowledgement unavailable")


class _Context:
    def __init__(self, path, *, reject=False):
        self.path = path
        self.owner = _RejectOwner() if reject else Owner()
        self.bench = Bench(self.owner)
        self.gc = _GlobalConfig()
        piece_records.initialize_piece_records()
        self.gc.machine_id = "m"
        self.gc.run_id = "runtime-run"
        self.gc.sorting_profile_path = None
        self.gc.run_recorder = RunRecorder(self.gc)
        self.gc.runtime_stats.setLifecycleState("running")
        self.shared = SharedVariables(gc=self.gc, bus=TickBus())
        self.shared.transport = ClassificationChannelTransport()
        self.events = queue.Queue()
        self.adapter = NativeCompletionAdapter(self.bench.bridge)
        self.distribution = PhysicalDistribution(
            self.shared, self.events, self.gc, native_completion=self.adapter)
        self.bench.runtime.distribution = self.distribution
        self.piece = KnownObject(
            part_id="raw-3001", color_id="raw-5", category_id="A",
            destination_bin=None if reject else (0, 0, 0),
            stage=PieceStage.distributing)
        self.piece.drop_snapshot = "synthetic-complete-scene"
        if reject:
            with connect(path) as conn:
                conn.execute("INSERT INTO smart_bin_group_keys "
                             "(id,kind,namespace,part_id,provenance) "
                             "VALUES('misc','category','sorter','misc','known')")
        ep = TransferEpisode(
            self.bench.runtime.fifo.boundary,
            self.bench.runtime.fifo.intake_pocket_id,
            self.bench.rig.clock(), time.time(), leader_id=42)
        pocket = self.bench.runtime.fifo.pockets[self.bench.runtime.fifo.intake_pocket_id]
        request = service.ReservationRequest(
            piece_uuid=self.piece.uuid, route_attempt="first",
            sorting_session_id="session", group_key_id="misc" if reject else "A",
            owner_incarnation=self.owner.owner_incarnation,
            episode_id=ep.episode_id, pocket_index=pocket.pocket_id,
            pocket_generation=pocket.generation + 1)
        with self.owner.lock:
            route = qualification()
            preview = service.preview(request, route)
            assert preview["code"] == "OK"
            claim = service.reserve(
                request, route, expected_state_revision=preview["state_revision"],
                expected_qualification_hash=preview["qualification_hash"],
                request_key=f"reserve-{self.piece.uuid}")
            assert claim["code"] == "OK"
            self.owner.reservations[self.piece.uuid] = claim["reservation_id"]
            binding = self.bench.runtime.reserve(self.piece, ep)
            self.bench.runtime.finish_handoff(binding, arrived=True)
            assert self.bench.runtime.fifo.resolve(
                *binding.key,
                PocketState.DISCARD if reject else PocketState.ROUTED,
                destination=None if reject else "bin:0:0:0")
        self.bench.binding = binding
        self.bench.reservation_id = claim["reservation_id"]
        self.clock = [time.time()]
        self.sending = Sending(
            SimpleNamespace(), self.gc, self.shared, self.events,
            native_completion=self.adapter)

    def discharge(self):
        self.bench.at_native_exit()
        self.bench.advance()
        assert self.bench.claim()["state"] == "EXIT_CONFIRMED"
        assert self.bench.bridge.index is None
        assert self.shared.transport.getPieceForDistributionDrop() is self.piece
        assert not self.shared.distribution_ready
        assert self.adapter.require_handoff(self.piece).attempt_id

    def settle(self):
        assert self.sending.step() is None
        self.clock[0] += CHUTE_SETTLE_MS / 1000 + 0.1
        return self.sending.step()

    def counts(self):
        with connect(self.path) as conn:
            return (
                conn.execute("SELECT count(*) FROM smart_bin_deliveries").fetchone()[0],
                conn.execute("SELECT count(*) FROM piece_records WHERE uuid=?",
                             (self.piece.uuid,)).fetchone()[0],
                conn.execute("SELECT count(*) FROM piece_events WHERE piece_uuid=?",
                             (self.piece.uuid,)).fetchone()[0],
            )

    def next_admission(self):
        piece = KnownObject(part_id="raw-3001", color_id="raw-5", category_id="A")
        runtime = self.bench.runtime
        episode = TransferEpisode(
            runtime.fifo.boundary, runtime.fifo.intake_pocket_id,
            self.bench.rig.clock(), time.time(), leader_id=43)
        pocket = runtime.fifo.pockets[runtime.fifo.intake_pocket_id]
        request = service.ReservationRequest(
            piece_uuid=piece.uuid, route_attempt="next",
            sorting_session_id="session", group_key_id="A",
            owner_incarnation=self.owner.owner_incarnation,
            episode_id=episode.episode_id, pocket_index=pocket.pocket_id,
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
        return piece, episode


@pytest.fixture
def context(prepared, monkeypatch):
    ctx = _Context(prepared)
    import subsystems.distribution.sending as sending_module

    monkeypatch.setattr(sending_module, "time", SimpleNamespace(time=lambda: ctx.clock[0]))
    return ctx


@pytest.mark.parametrize("reject", [False, True])
def test_native_exit_settles_before_publication_and_gate_open(prepared, monkeypatch, reject):
    ctx = _Context(prepared, reject=reject)
    import subsystems.distribution.sending as sending_module

    monkeypatch.setattr(sending_module, "time", SimpleNamespace(time=lambda: ctx.clock[0]))
    ctx.discharge()
    original_put = ctx.events.put
    publication_states = []

    def observe_publication(event):
        publication_states.append((ctx.counts()[0], ctx.bench.claim()["state"],
                                   ctx.piece.stage, ctx.shared.distribution_ready))
        return original_put(event)

    monkeypatch.setattr(ctx.events, "put", observe_publication)
    ctx.clock[0] = time.time() + 2
    assert ctx.sending.step() is None
    assert ctx.piece.stage is PieceStage.distributing
    assert ctx.counts()[0] == 0
    assert ctx.events.empty()
    ctx.clock[0] += CHUTE_SETTLE_MS / 1000 + 0.1
    assert ctx.sending.step() is DistributionState.IDLE
    assert ctx.piece.stage is PieceStage.distributed
    assert ctx.piece.native_delivery_id
    assert ctx.counts()[:2] == (1, 1)
    assert ctx.shared.distribution_ready
    assert ctx.adapter.verified_piece(ctx.piece)
    assert publication_states == [(1, "COMPLETED", PieceStage.distributed, False)]
    assert ctx.counts()[2] == 0
    event = ctx.events.get_nowait()
    assert event.data.native_reservation_id == ctx.bench.reservation_id
    assert event.data.native_delivery_id == ctx.piece.native_delivery_id
    assert event.data.drop_snapshot == "synthetic-complete-scene"
    with connect(prepared) as conn:
        row = conn.execute("SELECT actual_kind,actual_slot_id,actual_cycle_id "
                           "FROM smart_bin_deliveries").fetchone()
        assert row[:] == (("REJECT", None, None) if reject else ("BIN", "s0", "c0"))
        history = conn.execute("SELECT run_id,part_id,color_id,bin_x,bin_y,bin_z "
                               "FROM piece_records WHERE uuid=?", (ctx.piece.uuid,)).fetchone()
        assert history[:3] == ("runtime-run", "raw-3001", "raw-5")
        assert history[3:] == ((None, None, None) if reject else (0, 0, 0))


def test_completion_failure_and_lost_ack_retry_only_frozen_write(context, monkeypatch):
    ctx = context
    ctx.discharge()
    ctx.clock[0] = time.time() + 2
    assert ctx.sending.step() is None
    ctx.clock[0] += CHUTE_SETTLE_MS / 1000 + 0.1
    original = delivery.complete_native
    calls = []
    mode = ["before"]

    def fail_or_ack(*args, **kwargs):
        calls.append((kwargs["request_key"], kwargs["evidence"]))
        if mode[0] == "before":
            raise OSError("write unavailable")
        result = original(*args, **kwargs)
        if mode[0] == "lost":
            raise OSError("ack lost")
        return result

    monkeypatch.setattr(delivery, "complete_native", fail_or_ack)
    assert ctx.sending.step() is None
    assert ctx.piece.stage is PieceStage.distributing
    assert not ctx.shared.distribution_ready
    assert ctx.counts()[0] == 0
    handoff = ctx.adapter.require_handoff(ctx.piece)
    assert handoff.completion_error is not None
    motor_calls = len(ctx.bench.rig.commands)
    mode[0] = "lost"
    assert ctx.sending.step() is None
    assert ctx.counts()[0] == 1
    assert ctx.piece.stage is PieceStage.distributing
    mode[0] = "ok"
    assert ctx.sending.step() is DistributionState.IDLE
    assert len(ctx.bench.rig.commands) == motor_calls
    assert ctx.counts()[:2] == (1, 1)
    assert len(calls) == 3
    assert all(key == calls[0][0] and evidence is calls[0][1]
               for key, evidence in calls)
    assert ctx.bench.claim()["state"] == "COMPLETED"


def test_unverified_flag_wrong_piece_and_missing_drop_hold(context):
    ctx = context
    ctx.discharge()
    ctx.clock[0] = time.time() + 2
    ctx.piece.stage = PieceStage.distributed
    assert ctx.settle() is None
    assert ctx.counts()[0] == 0
    assert not ctx.shared.distribution_ready
    ctx.piece.stage = PieceStage.distributing
    wrong = KnownObject()
    ctx.shared.transport = ClassificationChannelTransport()
    ctx.shared.transport.placePieceForDistribution(wrong)
    ctx.shared.transport.advanceTransport()
    ctx.sending = Sending(SimpleNamespace(), ctx.gc, ctx.shared, ctx.events,
                          native_completion=ctx.adapter)
    assert ctx.sending.step() is None
    assert ctx.counts()[0] == 0
    ctx.shared.transport = ClassificationChannelTransport()
    ctx.sending = Sending(SimpleNamespace(), ctx.gc, ctx.shared, ctx.events,
                          native_completion=ctx.adapter)
    assert ctx.sending.step() is None
    ctx.clock[0] += 2
    assert ctx.sending.step() is None
    assert not ctx.shared.distribution_ready


def test_native_event_and_partial_updates_cannot_recredit_legacy(context, monkeypatch):
    ctx = context
    ctx.discharge()
    ctx.clock[0] = time.time() + 2
    monkeypatch.setattr(piece_records, "recordPiece", lambda *a, **k: pytest.fail(
        "native completion invoked legacy history upsert"))
    assert ctx.settle() is DistributionState.IDLE
    event = ctx.events.get_nowait().data.model_dump()
    import local_state as state

    calls = []
    monkeypatch.setattr(state, "record_piece_distribution", lambda value: calls.append(value))
    ctx.gc.runtime_stats.observeKnownObject(event)
    ctx.gc.runtime_stats.observeKnownObject({"uuid": ctx.piece.uuid,
                                             "part_id": "corrected-part",
                                             "distributed_at": ctx.piece.distributed_at,
                                             "native_machine_id": None,
                                             "native_reservation_id": None,
                                             "native_delivery_id": None})
    assert calls == []
    assert ctx.counts()[2] == 0
    assert ctx.gc.runtime_stats.lookupKnownObject(ctx.piece.uuid)["native_delivery_id"] == ctx.piece.native_delivery_id
    assert len(ctx.gc.run_recorder.pieces) == 1
    assert ctx.counts()[0] == 1
    from smart_bins_native_completion import NativeCompletionError

    with pytest.raises(NativeCompletionError):
        ctx.gc.runtime_stats.observeKnownObject({
            "uuid": "foreign", "stage": "distributed",
            "distributed_at": ctx.piece.distributed_at,
            "destination_bin": [0, 0, 0],
            "native_machine_id": "m",
            "native_reservation_id": ctx.bench.reservation_id,
            "native_delivery_id": ctx.piece.native_delivery_id,
        })
    assert calls == []


def test_native_provenance_survives_paused_lookup_eviction(context, monkeypatch):
    ctx = context
    ctx.discharge()
    ctx.clock[0] = time.time() + 2
    assert ctx.settle() is DistributionState.IDLE
    event = ctx.events.get_nowait().data.model_dump()
    collector = ctx.gc.runtime_stats
    collector.setLifecycleState("paused")
    import runtime_stats

    monkeypatch.setattr(runtime_stats, "MAX_KNOWN_OBJECT_LOOKUP_ENTRIES", 1)
    collector.observeKnownObject(event)
    collector.observeKnownObject({"uuid": "evictor", "stage": "created"})
    assert collector.lookupKnownObject(ctx.piece.uuid) is None
    collector.setLifecycleState("running")
    writes = []
    monkeypatch.setattr(local_state, "record_piece_distribution",
                        lambda value: writes.append(value))
    collector.observeKnownObject({
        "uuid": ctx.piece.uuid, "stage": "distributed",
        "destination_bin": (0, 0, 0), "distributed_at": ctx.piece.distributed_at,
        "native_machine_id": None, "native_reservation_id": None,
        "native_delivery_id": None,
    })
    assert writes == []
    assert collector.lookupKnownObject(ctx.piece.uuid)["native_delivery_id"] == ctx.piece.native_delivery_id


def test_progress_ack_failure_holds_without_second_increment(context, monkeypatch):
    ctx = context
    progress = _Progress()
    progress.fail = True
    ctx.gc.set_progress_tracker = progress
    ctx.discharge()
    ctx.clock[0] = time.time() + 2
    assert ctx.settle() is None
    assert ctx.counts()[0] == 1
    assert progress.calls == 1
    assert not ctx.shared.distribution_ready
    assert ctx.adapter.require_handoff(ctx.piece).publication_state == "failed"
    assert ctx.sending.step() is None
    ctx.sending = Sending(SimpleNamespace(), ctx.gc, ctx.shared, ctx.events,
                          native_completion=ctx.adapter)
    ctx.sending.start_time = ctx.clock[0] - 3
    assert ctx.sending.step() is None
    assert progress.calls == 1
    assert ctx.counts()[0] == 1


def test_repeat_ticks_and_sending_reentry_reuse_receipt(context):
    ctx = context
    ctx.discharge()
    ctx.clock[0] = time.time() + 2
    assert ctx.settle() is DistributionState.IDLE
    first_receipt = ctx.adapter.require_handoff(ctx.piece).receipt
    first_key = ctx.adapter.require_handoff(ctx.piece).request_key
    motor_calls = len(ctx.bench.rig.commands)
    assert ctx.sending.step() is DistributionState.IDLE
    ctx.sending = Sending(SimpleNamespace(), ctx.gc, ctx.shared, ctx.events,
                          native_completion=ctx.adapter)
    ctx.sending.start_time = ctx.clock[0] - 3
    assert ctx.sending.step() is DistributionState.IDLE
    handoff = ctx.adapter.require_handoff(ctx.piece)
    assert handoff.receipt is first_receipt
    assert handoff.request_key == first_key
    assert ctx.events.qsize() == 1
    assert len(ctx.gc.run_recorder.pieces) == 1
    assert len(ctx.bench.rig.commands) == motor_calls
    assert ctx.counts()[:2] == (1, 1)


def test_reentry_with_foreign_delivery_receipt_holds(context):
    ctx = context
    ctx.discharge()
    ctx.clock[0] = time.time() + 2
    assert ctx.settle() is DistributionState.IDLE
    ctx.piece.native_delivery_id = "foreign-delivery"
    ctx.sending = Sending(SimpleNamespace(), ctx.gc, ctx.shared, ctx.events,
                          native_completion=ctx.adapter)
    ctx.sending.start_time = ctx.clock[0] - 3
    assert ctx.sending.step() is None
    assert not ctx.shared.distribution_ready
    assert ctx.counts()[:2] == (1, 1)
    assert ctx.events.qsize() == 1


def test_claimed_completion_without_durable_delivery_holds(context, monkeypatch):
    ctx = context
    ctx.discharge()
    ctx.clock[0] = time.time() + 2
    assert ctx.sending.step() is None
    ctx.clock[0] += CHUTE_SETTLE_MS / 1000 + 0.1

    def fabricated(*args, **kwargs):
        h = ctx.adapter.require_handoff(ctx.piece)
        return {"code": "OK", "attempt_id": h.attempt_id,
                "delivery_id": "missing", "actual_kind": h.intended_kind,
                "actual_slot_id": h.intended_slot_id,
                "actual_cycle_id": h.intended_cycle_id}

    monkeypatch.setattr(delivery, "complete_native", fabricated)
    assert ctx.sending.step() is None
    assert ctx.piece.stage is PieceStage.distributing
    assert ctx.piece.distributed_at is None
    assert ctx.counts()[0] == 0
    assert ctx.events.empty()
    assert not ctx.shared.distribution_ready


def test_guarded_exit_without_adapter_refuses_before_transport(context):
    ctx = context
    ctx.bench.at_native_exit()
    ctx.distribution.native_completion = None
    with pytest.raises(RuntimeError, match="completion adapter"):
        ctx.bench.advance()
    assert ctx.shared.transport.getPieceForDistributionDrop() is None
    assert ctx.bench.claim()["state"] == "EXIT_CONFIRMED"
    assert ctx.counts()[0] == 0


def test_lost_marker_evidence_refuses_before_transport(context):
    ctx = context
    ctx.bench.at_native_exit()
    original = ctx.distribution.discharge

    def lost_evidence(event, binding):
        ctx.bench.bridge.index.exit_evidence = None
        return original(event, binding)

    ctx.distribution.discharge = lost_evidence
    with pytest.raises(Exception, match="matching handoff"):
        ctx.bench.advance()
    assert ctx.shared.transport.getPieceForDistributionDrop() is None
    assert ctx.bench.claim()["state"] == "EXIT_CONFIRMED"
    assert ctx.counts()[0] == 0


@pytest.mark.parametrize("field,value", [
    ("native_machine_id", "foreign-machine"),
    ("native_reservation_id", "foreign-reservation"),
    ("native_delivery_id", "fabricated-delivery"),
])
def test_foreign_handoff_metadata_refuses_before_transport(context, field, value):
    ctx = context
    ctx.bench.at_native_exit()
    setattr(ctx.piece, field, value)
    with pytest.raises(Exception, match="foreign or completed native metadata"):
        ctx.bench.advance()
    assert ctx.shared.transport.getPieceForDistributionDrop() is None
    assert ctx.bench.claim()["state"] == "EXIT_CONFIRMED"
    assert ctx.counts()[0] == 0


def test_harvest_bound_handoff_refuses_before_transport(context):
    ctx = context
    ctx.bench.at_native_exit()
    ctx.piece.harvest_allocation_id = "harvest-claim"
    with pytest.raises(Exception, match="Harvest-bound"):
        ctx.bench.advance()
    assert ctx.shared.transport.getPieceForDistributionDrop() is None
    assert ctx.bench.claim()["state"] == "EXIT_CONFIRMED"
    assert ctx.counts()[0] == 0


def test_history_corrections_and_separate_run_identity(context):
    ctx = context
    piece_records.initialize_piece_records()
    with connect(ctx.path) as conn:
        conn.execute("INSERT INTO piece_records(uuid,machine_id,run_id,part_correct,"
                     "color_corrected_id) VALUES(?,?,?,?,?)",
                     (ctx.piece.uuid, "m", "runtime-run", 0, "corrected-color"))
    ctx.discharge()
    ctx.clock[0] = time.time() + 2
    assert ctx.settle() is DistributionState.IDLE
    with connect(ctx.path) as conn:
        history = conn.execute("SELECT run_id,part_correct,color_corrected_id "
                               "FROM piece_records WHERE uuid=?", (ctx.piece.uuid,)).fetchone()
        reservation = conn.execute("SELECT run_id FROM smart_bin_reservations "
                                   "WHERE id=?", (ctx.bench.reservation_id,)).fetchone()
    assert history[:] == ("runtime-run", 0, "corrected-color")
    assert reservation[0] == "session"


@pytest.mark.parametrize("identity", ["valid", "incomplete", "foreign", "cleared"])
def test_missing_sending_adapter_holds_guarded_drop(context, identity):
    ctx = context
    ctx.discharge()
    if identity == "incomplete":
        ctx.piece.native_reservation_id = None
    elif identity == "foreign":
        ctx.piece.native_reservation_id = "foreign-reservation"
    elif identity == "cleared":
        ctx.piece.native_machine_id = None
        ctx.piece.native_reservation_id = None
    ctx.sending = Sending(SimpleNamespace(), ctx.gc, ctx.shared, ctx.events)
    ctx.clock[0] = time.time() + 2
    assert ctx.sending.step() is None
    ctx.clock[0] += CHUTE_SETTLE_MS / 1000 + 0.1
    assert ctx.sending.step() is None
    assert ctx.piece.stage is PieceStage.distributing
    assert ctx.counts() == (0, 0, 0)
    assert ctx.events.empty()
    assert not ctx.gc.run_recorder.pieces
    assert not ctx.shared.distribution_ready
    assert ctx.bench.claim()["state"] == "EXIT_CONFIRMED"
    assert not ctx.bench.runtime.can_admit
    if identity == "valid":
        assert not ctx.adapter.require_handoff(ctx.piece).ownership_lost


def test_missing_adapter_and_drop_does_not_reopen_guarded_gate(context):
    ctx = context
    ctx.discharge()
    ctx.shared.transport.advanceTransport()
    ctx.sending = Sending(SimpleNamespace(), ctx.gc, ctx.shared, ctx.events)
    ctx.clock[0] = time.time() + 2
    assert ctx.sending.step() is None
    ctx.clock[0] += 2
    assert ctx.sending.step() is None
    assert ctx.counts() == (0, 0, 0)
    assert ctx.events.empty()
    assert not ctx.shared.distribution_ready
    assert not ctx.bench.runtime.can_admit


@pytest.mark.parametrize("loss", ["drop", "transport_absent", "transport_replaced"])
def test_first_observed_missing_drop_latches_original_handoff(context, loss):
    ctx = context
    ctx.discharge()
    handoff = ctx.adapter.require_handoff(ctx.piece)
    progress = _Progress()
    ctx.gc.set_progress_tracker = progress
    original_transport = ctx.shared.transport
    if loss == "drop":
        original_transport.advanceTransport()
    elif loss == "transport_absent":
        ctx.shared.transport = None
    else:
        replacement = ClassificationChannelTransport()
        replacement.placePieceForDistribution(ctx.piece)
        replacement.advanceTransport()
        ctx.shared.transport = replacement
    assert ctx.sending.piece is None
    ctx.clock[0] = time.time() + 2
    assert ctx.sending.step() is None
    assert handoff.ownership_lost
    ctx.shared.transport = original_transport
    if loss == "drop":
        original_transport.placePieceForDistribution(ctx.piece)
        original_transport.advanceTransport()
    ctx.clock[0] += CHUTE_SETTLE_MS / 1000 + 0.1
    assert ctx.sending.step() is None
    assert ctx.adapter.require_handoff(ctx.piece) is handoff
    assert ctx.bench.claim()["state"] == "EXIT_CONFIRMED"
    assert ctx.counts() == (0, 0, 0)
    assert ctx.events.empty() and not ctx.gc.run_recorder.pieces
    assert progress.calls == 0
    assert not ctx.shared.distribution_ready
    assert not ctx.bench.runtime.can_admit


def test_first_ownership_read_failure_latches_handoff(context, monkeypatch):
    ctx = context
    ctx.discharge()
    handoff = ctx.adapter.require_handoff(ctx.piece)
    transport = ctx.shared.transport
    original = transport.getPieceForDistributionDrop

    def unavailable():
        raise OSError("drop sensor read unavailable")

    monkeypatch.setattr(transport, "getPieceForDistributionDrop", unavailable)
    ctx.clock[0] = time.time() + 2
    assert ctx.sending.step() is None
    assert handoff.ownership_lost
    monkeypatch.setattr(transport, "getPieceForDistributionDrop", original)
    ctx.clock[0] += CHUTE_SETTLE_MS / 1000 + 0.1
    assert ctx.sending.step() is None
    assert ctx.adapter.require_handoff(ctx.piece) is handoff
    assert ctx.counts() == (0, 0, 0)
    assert ctx.events.empty() and not ctx.gc.run_recorder.pieces
    assert not ctx.shared.distribution_ready
    assert not ctx.bench.runtime.can_admit


def test_first_observed_wrong_drop_survives_fresh_sending(context):
    ctx = context
    ctx.discharge()
    handoff = ctx.adapter.require_handoff(ctx.piece)
    progress = _Progress()
    ctx.gc.set_progress_tracker = progress
    ctx.shared.transport.placePieceForDistribution(KnownObject())
    ctx.shared.transport.advanceTransport()
    assert ctx.sending.piece is None
    ctx.clock[0] = time.time() + 2
    assert ctx.sending.step() is None
    assert handoff.ownership_lost
    ctx.shared.transport.placePieceForDistribution(ctx.piece)
    ctx.shared.transport.advanceTransport()
    ctx.sending = Sending(SimpleNamespace(), ctx.gc, ctx.shared, ctx.events,
                          native_completion=ctx.adapter)
    assert ctx.sending.step() is None
    ctx.clock[0] += CHUTE_SETTLE_MS / 1000 + 0.1
    assert ctx.sending.step() is None
    assert ctx.adapter.require_handoff(ctx.piece) is handoff
    assert ctx.bench.claim()["state"] == "EXIT_CONFIRMED"
    assert ctx.counts() == (0, 0, 0)
    assert ctx.events.empty() and not ctx.gc.run_recorder.pieces
    assert progress.calls == 0
    assert not ctx.shared.distribution_ready
    assert not ctx.bench.runtime.can_admit


def test_missing_adapter_latches_missing_drop_across_restoration(context):
    ctx = context
    ctx.discharge()
    handoff = ctx.adapter.require_handoff(ctx.piece)
    ctx.shared.transport.advanceTransport()
    ctx.sending = Sending(SimpleNamespace(), ctx.gc, ctx.shared, ctx.events)
    ctx.clock[0] = time.time() + 2
    assert ctx.sending.step() is None
    assert handoff.ownership_lost
    ctx.shared.transport.placePieceForDistribution(ctx.piece)
    ctx.shared.transport.advanceTransport()
    ctx.sending.native_completion = ctx.adapter
    ctx.clock[0] += CHUTE_SETTLE_MS / 1000 + 0.1
    assert ctx.sending.step() is None
    assert ctx.adapter.require_handoff(ctx.piece) is handoff
    assert ctx.bench.claim()["state"] == "EXIT_CONFIRMED"
    assert ctx.counts() == (0, 0, 0)
    assert ctx.events.empty() and not ctx.gc.run_recorder.pieces
    assert not ctx.shared.distribution_ready
    assert not ctx.bench.runtime.can_admit


def test_waiting_without_native_handoff_does_not_latch_loss(context):
    ctx = context
    ctx.clock[0] = time.time() + 2
    assert ctx.adapter._handoff is None
    assert ctx.sending.step() is None
    assert ctx.adapter._handoff is None
    assert not ctx.adapter.blocks_admission()
    ctx.shared.set_distribution_gate(True, reason=None)
    ctx.sending = Sending(SimpleNamespace(), ctx.gc, ctx.shared, ctx.events,
                          native_completion=ctx.adapter)
    ctx.discharge()
    assert not ctx.adapter.require_handoff(ctx.piece).ownership_lost
    assert ctx.settle() is DistributionState.IDLE
    assert ctx.counts()[:2] == (1, 1)


def test_released_native_guard_does_not_claim_later_ordinary_drop(context):
    ctx = context
    ctx.discharge()
    ctx.clock[0] = time.time() + 2
    assert ctx.settle() is DistributionState.IDLE
    handoff = ctx.adapter.require_handoff(ctx.piece)
    assert handoff.admission_released and not handoff.ownership_lost
    ordinary = KnownObject(part_id="raw-3001", color_id="raw-5",
                           category_id="A", stage=PieceStage.distributing)
    ctx.shared.transport.placePieceForDistribution(ordinary)
    ctx.shared.transport.advanceTransport()
    sender = Sending(SimpleNamespace(), ctx.gc, ctx.shared, ctx.events)
    assert sender.step() is None
    ctx.clock[0] += CHUTE_SETTLE_MS / 1000 + 0.1
    assert sender.step() is DistributionState.IDLE
    assert ordinary.stage is PieceStage.distributed
    assert not handoff.ownership_lost


def test_unguarded_drop_still_uses_legacy_sending(context):
    ctx = context
    ordinary = KnownObject(part_id="raw-3001", color_id="raw-5",
                           category_id="A", stage=PieceStage.distributing)
    ctx.shared.transport.placePieceForDistribution(ordinary)
    ctx.shared.transport.advanceTransport()
    sender = Sending(SimpleNamespace(), ctx.gc, ctx.shared, ctx.events)
    ctx.clock[0] = time.time() + 2
    assert sender.step() is None
    ctx.clock[0] += CHUTE_SETTLE_MS / 1000 + 0.1
    assert sender.step() is DistributionState.IDLE
    assert ordinary.stage is PieceStage.distributed
    assert ctx.shared.distribution_ready
    assert ctx.events.qsize() == 1


@pytest.mark.parametrize("replacement", ["missing", "wrong_piece", "new_transport"])
def test_cached_native_piece_rechecks_current_drop_before_commit(context, replacement):
    ctx = context
    ctx.discharge()
    ctx.clock[0] = time.time() + 2
    assert ctx.sending.step() is None
    assert ctx.sending.piece is ctx.piece
    if replacement == "wrong_piece":
        ctx.shared.transport.placePieceForDistribution(KnownObject())
    if replacement == "new_transport":
        ctx.shared.transport = ClassificationChannelTransport()
        ctx.shared.transport.placePieceForDistribution(ctx.piece)
    ctx.shared.transport.advanceTransport()
    if replacement == "new_transport":
        assert ctx.shared.transport.getPieceForDistributionDrop() is ctx.piece
    else:
        assert ctx.shared.transport.getPieceForDistributionDrop() is not ctx.piece
    ctx.clock[0] += CHUTE_SETTLE_MS / 1000 + 0.1
    assert ctx.sending.step() is None
    assert ctx.piece.stage is PieceStage.distributing
    assert ctx.counts() == (0, 0, 0)
    assert ctx.events.empty()
    assert not ctx.shared.distribution_ready
    assert not ctx.bench.runtime.can_admit


def test_cached_native_piece_loss_after_commit_keeps_receipt(context, monkeypatch):
    ctx = context
    progress = _Progress()
    ctx.gc.set_progress_tracker = progress
    notifications = []
    monkeypatch.setattr(
        "server.set_progress_sync.getSetProgressSyncWorker",
        lambda: SimpleNamespace(notify=lambda: notifications.append("sent")))
    ctx.discharge()
    ctx.clock[0] = time.time() + 2
    assert ctx.sending.step() is None
    incident = {"kind": "external_hold", "piece_uuid": "other", "channel": "c4"}
    ctx.gc.runtime_stats.setActiveIncident(incident)
    incident = ctx.gc.runtime_stats.activeIncident()
    ctx.clock[0] += CHUTE_SETTLE_MS / 1000 + 0.1
    assert ctx.sending.step() is None
    handoff = ctx.adapter.require_handoff(ctx.piece)
    receipt = handoff.receipt
    assert receipt and handoff.publication_state == "complete"
    assert ctx.counts()[:2] == (1, 1)
    assert ctx.events.qsize() == progress.calls == len(notifications) == 1
    assert not ctx.shared.distribution_ready
    assert not ctx.bench.runtime.can_admit
    assert ctx.gc.runtime_stats.clearActiveIncidentIfMatches(
        incident, resolved_by="test_external_hold_cleared")
    ctx.shared.transport.advanceTransport()
    assert ctx.sending.step() is None
    assert handoff.receipt is receipt
    assert ctx.counts()[:2] == (1, 1)
    assert ctx.events.qsize() == progress.calls == len(notifications) == 1
    assert not ctx.shared.distribution_ready
    assert not ctx.bench.runtime.can_admit


def test_drop_loss_during_durable_completion_skips_publication(context, monkeypatch):
    ctx = context
    ctx.discharge()
    ctx.clock[0] = time.time() + 2
    assert ctx.sending.step() is None
    ctx.clock[0] += CHUTE_SETTLE_MS / 1000 + 0.1
    original = delivery.complete_native

    def commit_then_lose_drop(*args, **kwargs):
        result = original(*args, **kwargs)
        ctx.shared.transport.advanceTransport()
        return result

    monkeypatch.setattr(delivery, "complete_native", commit_then_lose_drop)
    assert ctx.sending.step() is None
    handoff = ctx.adapter.require_handoff(ctx.piece)
    receipt = handoff.receipt
    assert receipt and ctx.counts()[:2] == (1, 1)
    assert ctx.events.empty() and not ctx.gc.run_recorder.pieces
    assert not ctx.shared.distribution_ready
    assert not ctx.bench.runtime.can_admit
    assert ctx.sending.step() is None
    assert handoff.receipt is receipt
    assert ctx.counts()[:2] == (1, 1)
    assert ctx.events.empty() and not ctx.gc.run_recorder.pieces


def test_guarded_drop_loss_cannot_be_repaired_by_restaging_object(context):
    ctx = context
    ctx.discharge()
    ctx.clock[0] = time.time() + 2
    assert ctx.sending.step() is None
    ctx.shared.transport.advanceTransport()
    ctx.clock[0] += CHUTE_SETTLE_MS / 1000 + 0.1
    assert ctx.sending.step() is None
    assert ctx.counts() == (0, 0, 0)
    ctx.shared.transport.placePieceForDistribution(ctx.piece)
    ctx.shared.transport.advanceTransport()
    assert ctx.shared.transport.getPieceForDistributionDrop() is ctx.piece
    assert ctx.sending.step() is None
    assert ctx.counts() == (0, 0, 0)
    assert ctx.events.empty()
    assert not ctx.shared.distribution_ready
    assert not ctx.bench.runtime.can_admit


@pytest.mark.parametrize("failure", ["before", "lost_ack"])
def test_unsettled_native_completion_blocks_owner_admission(context, monkeypatch, failure):
    ctx = context
    ctx.discharge()
    next_piece, episode = ctx.next_admission()
    ctx.clock[0] = time.time() + 2
    assert ctx.sending.step() is None
    ctx.clock[0] += CHUTE_SETTLE_MS / 1000 + 0.1
    original = delivery.complete_native

    def fail(*args, **kwargs):
        if failure == "before":
            raise OSError("write unavailable")
        result = original(*args, **kwargs)
        raise OSError("ack lost")

    monkeypatch.setattr(delivery, "complete_native", fail)
    assert ctx.sending.step() is None
    assert ctx.counts()[0] == (0 if failure == "before" else 1)
    assert not ctx.bench.runtime.can_admit
    with ctx.owner.lock, pytest.raises(RuntimeError, match="C3 release conflicts"):
        ctx.bench.runtime.reserve(next_piece, episode)
    assert ctx.bench.runtime.handoff is None
    assert ctx.bench.runtime.fifo.pockets[episode.pocket_id].state is PocketState.EMPTY
    monkeypatch.setattr(delivery, "complete_native", original)
    assert ctx.sending.step() is DistributionState.IDLE
    assert ctx.bench.runtime.can_admit
    assert ctx.counts()[:2] == (1, 1)
    ctx.bench.runtime.pause()
    assert not ctx.bench.runtime.can_admit


def test_failed_after_commit_callback_keeps_owner_admission_held(context):
    ctx = context
    progress = _Progress()
    progress.fail = True
    ctx.gc.set_progress_tracker = progress
    ctx.discharge()
    next_piece, episode = ctx.next_admission()
    ctx.clock[0] = time.time() + 2
    assert ctx.settle() is None
    assert ctx.counts()[:2] == (1, 1)
    assert ctx.adapter.require_handoff(ctx.piece).publication_state == "failed"
    assert not ctx.bench.runtime.can_admit
    with ctx.owner.lock, pytest.raises(RuntimeError, match="C3 release conflicts"):
        ctx.bench.runtime.reserve(next_piece, episode)
    assert ctx.bench.runtime.handoff is None
    assert progress.calls == 1
    assert ctx.sending.step() is None
    assert progress.calls == 1
    assert not ctx.bench.runtime.can_admit


def test_missing_completion_coupling_fails_closed_after_handoff(context):
    ctx = context
    ctx.discharge()
    next_piece, episode = ctx.next_admission()
    ctx.bench.bridge._completion_guard = None
    assert not ctx.bench.runtime.can_admit
    with ctx.owner.lock, pytest.raises(RuntimeError, match="C3 release conflicts"):
        ctx.bench.runtime.reserve(next_piece, episode)
    assert ctx.bench.runtime.handoff is None
    assert ctx.bench.runtime.fifo.pockets[episode.pocket_id].state is PocketState.EMPTY
