"""Restart boundaries for inactive guarded native completion follow-up."""

from __future__ import annotations

# The imported fixture sets temporary SQLite/config paths before backend imports.
from test_smart_bins_delivery import (
    prepared, connect, claim, prepare, confirm, complete,
)

import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

import smart_bins_completion_recovery as recovery
import smart_bins_delivery as delivery
from subsystems.distribution.sending import CHUTE_SETTLE_MS
from subsystems.distribution.states import DistributionState
from smart_bins_native_completion import (
    CurrentFollowupHeld, NativeCompletionError, NativeHandoff,
)
from defs.known_object import PieceStage
from subsystems.classification_channel.physical_fifo import PocketState
from subsystems.distribution.sending import Sending
from test_smart_bins_physical_bridge import Bench, Owner
from test_smart_bins_sending import _Context, _Progress


@pytest.fixture
def context(prepared, monkeypatch):
    ctx = _Context(prepared)
    import subsystems.distribution.sending as sending_module
    monkeypatch.setattr(sending_module, "time", SimpleNamespace(time=lambda: ctx.clock[0]))
    return ctx


def followup(ctx):
    handoff = ctx.adapter.require_handoff(ctx.piece)
    with connect(ctx.path) as conn:
        return dict(conn.execute(
            "SELECT * FROM smart_bin_completion_followups WHERE id=?",
            (handoff.followup_id,)).fetchone())


def fresh_owner_blocker():
    bench = Bench(Owner())
    assert len(bench.rig.commands) == 0
    return bench.bridge.recovery_blocker


def settle_time(ctx):
    ctx.clock[0] = time.time() + 2
    assert ctx.sending.step() is None
    ctx.clock[0] += CHUTE_SETTLE_MS / 1000 + 0.1


def second_native_piece(ctx):
    """Admit and discharge another piece through the same guarded owner."""
    previous = ctx.piece
    previous_handoff = ctx.adapter.require_handoff(previous)
    piece, episode = ctx.next_admission()
    piece.category_id = "A"
    piece.destination_bin = (0, 0, 0)
    piece.stage = PieceStage.distributing
    piece.drop_snapshot = "second-synthetic-complete-scene"
    with ctx.owner.lock:
        binding = ctx.bench.runtime.reserve(piece, episode)
        ctx.bench.runtime.finish_handoff(binding, arrived=True)
        assert ctx.bench.runtime.fifo.resolve(
            *binding.key, PocketState.ROUTED, destination="bin:0:0:0")
    ctx.bench.binding = binding
    ctx.bench.reservation_id = ctx.owner.reservations[piece.uuid]
    for _ in range(10):
        if ctx.bench.runtime.fifo.station_of(binding.pocket_id) == 0:
            break
        ctx.bench.advance()
    assert ctx.bench.runtime.fifo.station_of(binding.pocket_id) == 0
    ctx.bench.advance()
    ctx.piece = piece
    assert ctx.shared.transport.getPieceForDistributionDrop() is piece
    assert ctx.adapter.require_handoff(piece).followup_id != previous_handoff.followup_id
    ctx.sending.cleanup()
    ctx.sending = Sending(SimpleNamespace(), ctx.gc, ctx.shared, ctx.events,
                          native_completion=ctx.adapter)
    return previous, previous_handoff


def late_hold(ctx, handoff, *, kind="OWNERSHIP_LOST", key="late-hold"):
    return recovery.record_hold(
        "m", handoff.followup_id, kind=kind, reason="later durable witness",
        expected_revision=handoff.followup_revision, request_key=key)


def other_machine_held_followup(path):
    """Seed a valid independent exit and retain its open follow-up."""
    with connect(path) as conn:
        conn.execute("INSERT INTO smart_bin_machines(machine_id) VALUES('other-machine')")
        conn.execute("INSERT INTO smart_bin_policy_revisions "
                     "VALUES('other-policy','other-machine','artifact','{}',1)")
        conn.execute("INSERT INTO sorting_sessions(id,machine_id,started_at,status) "
                     "VALUES('other-session','other-machine',1,'active')")
        conn.execute("INSERT INTO smart_bin_slots "
                     "(id,machine_id,layout_revision,layer_index,section_index,bin_index) "
                     "VALUES('other-slot','other-machine','layout',0,0,0)")
        conn.execute("INSERT INTO smart_bin_cycles "
                     "(id,machine_id,slot_id,opened_at,provenance) "
                     "VALUES('other-cycle','other-machine','other-slot',1,'observed')")
        conn.execute(
            "INSERT INTO smart_bin_reservations "
            "(id,machine_id,piece_uuid,route_attempt,request_key,run_id,"
            "policy_revision_id,group_key_id,routing_revision,quantity,intended_kind,"
            "intended_slot_id,intended_cycle_id,owner_incarnation,episode_id,"
            "pocket_index,pocket_generation,state,row_revision,created_at,updated_at) "
            "VALUES('other-reservation','other-machine','other-piece','first',"
            "'other-reserve','other-session','other-policy','A',0,1,'BIN',"
            "'other-slot','other-cycle','other-owner','other-episode',0,1,"
            "'EXIT_CONFIRMED',2,1,1)")
        conn.execute("INSERT INTO smart_bin_release_attempts "
                     "VALUES('other-attempt','other-reservation','other-owner','17',1)")
        conn.execute("INSERT INTO smart_bin_release_evidence "
                     "(attempt_id,reservation_id,intent_json,exit_json) VALUES(?,?,?,?)",
                     ("other-attempt", "other-reservation", "{}",
                      json.dumps({"marker_evidence_ref": "other-marker"})))
    handoff = NativeHandoff(
        SimpleNamespace(uuid="other-piece"), "other-machine", "other-reservation",
        delivery.CustodyRef("other-piece", "other-owner", "other-episode", 0, 1),
        "other-attempt", 17, "other-marker", "BIN", "other-slot", "other-cycle",
        "other-session", (0, 0, 0), request_key="other-retain",
        followup_id="other-followup")
    assert recovery.stage_handoff(handoff)["state"] == "RETAINED"
    assert recovery.record_hold(
        "other-machine", handoff.followup_id, kind="OWNERSHIP_LOST",
        reason="independent owner loss", expected_revision=0,
        request_key="other-loss")["state"] == "RETAINED"
    with connect(path) as conn:
        report = recovery.inspect_on_connection(conn, "other-machine")
    assert len(report["followup_blockers"]) == 1


def test_retained_before_transport_and_restart_block(context):
    ctx = context
    observed = []
    original_advance = ctx.shared.transport.advanceTransport

    def advance():
        observed.append((followup(ctx)["state"], ctx.shared.distribution_ready))
        return original_advance()

    ctx.shared.transport.advanceTransport = advance
    ctx.discharge()
    assert observed == [("RETAINED", False)]
    assert delivery.inspect_recovery("m")["followup_blockers"][0]["state"] == "RETAINED"
    assert fresh_owner_blocker() == "unresolved native completion follow-up"
    assert ctx.counts() == (0, 0, 0)


def test_stage_write_failure_refuses_transport(context, monkeypatch):
    ctx = context
    ctx.bench.at_native_exit()
    original = recovery.stage_handoff
    monkeypatch.setattr(recovery, "stage_handoff", lambda *a: (_ for _ in ()).throw(
        OSError("follow-up storage unavailable")))
    with pytest.raises(Exception, match="follow-up storage unavailable"):
        ctx.bench.advance()
    assert ctx.shared.transport.getPieceForDistributionDrop() is None
    assert ctx.bench.claim()["state"] == "EXIT_CONFIRMED"
    monkeypatch.setattr(recovery, "stage_handoff", original)
    handoff = ctx.adapter._handoff
    assert handoff is not None and handoff.request_key
    ctx.adapter.persist_retained_handoff()
    assert followup(ctx)["completion_request_key"] == handoff.request_key
    assert ctx.shared.transport.getPieceForDistributionDrop() is None
    assert ctx.counts() == (0, 0, 0)


def test_delivery_and_publication_states_block_fresh_owner(context):
    ctx = context
    ctx.discharge()
    handoff = ctx.adapter.require_handoff(ctx.piece)
    now = time.time() + 5
    ctx.adapter.complete(ctx.piece, runtime_run_id=ctx.gc.run_recorder.run_id,
                         completed_at=now)
    assert followup(ctx)["state"] == "DELIVERY_PENDING_PUBLICATION"
    assert ctx.counts()[:2] == (1, 1)
    assert fresh_owner_blocker() == "unresolved native completion follow-up"
    ctx.adapter.begin_publication(handoff)
    assert followup(ctx)["state"] == "PUBLICATION_ATTEMPTED"
    assert fresh_owner_blocker() == "unresolved native completion follow-up"
    ctx.adapter.record_published(handoff)
    assert followup(ctx)["state"] == "PUBLICATION_SUCCEEDED"
    assert fresh_owner_blocker() == "unresolved native completion follow-up"
    assert ctx.counts() == (1, 1, 0)


def test_closed_followup_is_inspectable_and_not_own_blocker(context):
    ctx = context
    ctx.discharge()
    settle_time(ctx)
    assert ctx.sending.step() is DistributionState.IDLE
    report = delivery.inspect_recovery("m")
    assert len(report["followups"]) == 1
    assert report["followups"][0]["state"] == "CLOSED"
    assert report["followup_blockers"] == []
    assert fresh_owner_blocker() is None
    assert ctx.shared.distribution_ready
    assert ctx.counts() == (1, 1, 0)


def test_completion_rollback_keeps_open_obligation_and_frozen_retry(context, monkeypatch):
    ctx = context
    ctx.discharge()
    settle_time(ctx)
    original = delivery.recordPieceOnConnection
    monkeypatch.setattr(delivery, "recordPieceOnConnection",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("history write failed")))
    assert ctx.sending.step() is None
    handoff = ctx.adapter.require_handoff(ctx.piece)
    first_key, first_evidence = handoff.request_key, handoff.evidence
    assert followup(ctx)["state"] == "RETAINED"
    assert ctx.counts() == (0, 0, 0)
    assert fresh_owner_blocker() == "unresolved native completion follow-up"
    monkeypatch.setattr(delivery, "recordPieceOnConnection", original)
    assert ctx.sending.step() is DistributionState.IDLE
    assert (handoff.request_key, handoff.evidence) == (first_key, first_evidence)
    assert followup(ctx)["state"] == "CLOSED"
    assert ctx.counts() == (1, 1, 0)


def test_followup_link_failure_rolls_back_delivery_and_history(context, monkeypatch):
    ctx = context
    ctx.discharge()
    settle_time(ctx)

    def fail_link(*args, **kwargs):
        raise OSError("follow-up link write failed")

    monkeypatch.setattr(recovery, "bind_delivery", fail_link)
    assert ctx.sending.step() is None
    assert ctx.counts() == (0, 0, 0)
    assert ctx.bench.claim()["state"] == "EXIT_CONFIRMED"
    assert followup(ctx)["state"] == "RETAINED"
    assert fresh_owner_blocker() == "unresolved native completion follow-up"


def test_retention_lost_ack_refuses_advance_but_blocks_restart(context, monkeypatch):
    ctx = context
    ctx.bench.at_native_exit()
    actual = recovery.stage_handoff

    def lose_ack(handoff):
        actual(handoff)
        raise OSError("retention acknowledgement lost")

    monkeypatch.setattr(recovery, "stage_handoff", lose_ack)
    with pytest.raises(Exception, match="retention acknowledgement lost"):
        ctx.bench.advance()
    assert ctx.shared.transport.getPieceForDistributionDrop() is None
    handoff = ctx.adapter._handoff
    assert handoff is not None
    key, followup_id = handoff.request_key, handoff.followup_id
    monkeypatch.setattr(recovery, "stage_handoff", actual)
    ctx.adapter.persist_retained_handoff()
    assert (handoff.request_key, handoff.followup_id) == (key, followup_id)
    assert followup(ctx)["completion_request_key"] == key
    assert ctx.shared.transport.getPieceForDistributionDrop() is None
    assert delivery.inspect_recovery("m")["followup_blockers"]
    assert fresh_owner_blocker() == "unresolved native completion follow-up"


def test_lost_completion_ack_replays_same_delivery_and_followup(context, monkeypatch):
    ctx = context
    ctx.discharge()
    settle_time(ctx)
    original = delivery.complete_native
    calls = []

    def lose_once(*args, **kwargs):
        calls.append((kwargs["request_key"], kwargs["evidence"]))
        result = original(*args, **kwargs)
        if len(calls) == 1:
            raise OSError("completion acknowledgement lost")
        return result

    monkeypatch.setattr(delivery, "complete_native", lose_once)
    assert ctx.sending.step() is None
    row = followup(ctx)
    assert row["state"] == "DELIVERY_PENDING_PUBLICATION"
    assert ctx.counts() == (1, 1, 0)
    assert ctx.sending.step() is DistributionState.IDLE
    assert calls[0][0] == calls[1][0] and calls[0][1] is calls[1][1]
    assert followup(ctx)["state"] == "CLOSED"
    assert ctx.counts() == (1, 1, 0)
    assert ctx.events.qsize() == 1


def test_publication_bookkeeping_lost_ack_retries_without_callbacks(context, monkeypatch):
    ctx = context
    ctx.gc.set_progress_tracker = _Progress()
    ctx.discharge()
    settle_time(ctx)
    original = recovery.transition
    calls = []

    def lose_published(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs["action"] == "PUBLICATION_SUCCEEDED":
            calls.append(kwargs["request_key"])
            if len(calls) == 1:
                raise OSError("publication bookkeeping acknowledgement lost")
        return result

    monkeypatch.setattr(recovery, "transition", lose_published)
    assert ctx.sending.step() is None
    assert followup(ctx)["state"] == "PUBLICATION_SUCCEEDED"
    assert ctx.events.qsize() == 1
    assert ctx.gc.set_progress_tracker.calls == 1
    assert ctx.sending.step() is DistributionState.IDLE
    assert calls[0] == calls[1]
    assert ctx.events.qsize() == 1
    assert ctx.gc.set_progress_tracker.calls == 1
    assert followup(ctx)["state"] == "CLOSED"


def test_callback_success_before_outcome_write_blocks_restart(context, monkeypatch):
    ctx = context
    ctx.gc.set_progress_tracker = _Progress()
    ctx.discharge()
    settle_time(ctx)
    actual = recovery.transition
    attempts = []

    def fail_before_outcome(*args, **kwargs):
        if kwargs["action"] == "PUBLICATION_SUCCEEDED":
            attempts.append(kwargs["request_key"])
            if len(attempts) == 1:
                raise OSError("outcome write unavailable")
        return actual(*args, **kwargs)

    monkeypatch.setattr(recovery, "transition", fail_before_outcome)
    assert ctx.sending.step() is None
    assert followup(ctx)["state"] == "PUBLICATION_ATTEMPTED"
    assert ctx.events.qsize() == 1
    assert ctx.gc.set_progress_tracker.calls == 1
    assert fresh_owner_blocker() == "unresolved native completion follow-up"
    assert ctx.sending.step() is DistributionState.IDLE
    assert attempts[0] == attempts[1]
    assert ctx.events.qsize() == 1
    assert ctx.gc.set_progress_tracker.calls == 1
    assert followup(ctx)["state"] == "CLOSED"


def test_callback_failure_attempt_is_ambiguous_after_restart(context, monkeypatch):
    ctx = context
    ctx.discharge()
    settle_time(ctx)
    original_put = ctx.events.put
    calls = []

    def lost_ack(event):
        calls.append(event)
        original_put(event)
        raise OSError("event acknowledgement lost")

    monkeypatch.setattr(ctx.events, "put", lost_ack)
    assert ctx.sending.step() is None
    assert followup(ctx)["state"] == "PUBLICATION_ATTEMPTED"
    assert followup(ctx)["failure_kind"] == "SIDE_EFFECT_FAILED"
    assert fresh_owner_blocker() == "unresolved native completion follow-up"
    assert ctx.sending.step() is None
    assert len(calls) == 1
    assert ctx.counts() == (1, 1, 0)


def test_ownership_loss_fences_before_failed_sqlite_write(context, monkeypatch):
    ctx = context
    ctx.discharge()
    ctx.shared.transport = None
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(1)
        assert not ctx.shared.distribution_ready
        raise OSError("SQLite unavailable")

    monkeypatch.setattr(recovery, "record_hold", unavailable)
    assert ctx.sending.step() is None
    handoff = ctx.adapter.require_handoff(ctx.piece)
    assert handoff.ownership_lost
    assert not ctx.shared.distribution_ready
    assert ctx.events.empty()
    assert followup(ctx)["state"] == "RETAINED"
    assert fresh_owner_blocker() == "unresolved native completion follow-up"
    assert calls


def test_stale_close_cannot_clear_loss_and_late_loss_remains_visible(context):
    ctx = context
    ctx.discharge()
    handoff = ctx.adapter.require_handoff(ctx.piece)
    ctx.adapter.complete(ctx.piece, runtime_run_id=ctx.gc.run_recorder.run_id,
                         completed_at=time.time() + 5)
    ctx.adapter.begin_publication(handoff)
    ctx.adapter.record_published(handoff)
    with pytest.raises(recovery.CompletionRecoveryError):
        recovery.transition(
            "m", handoff.followup_id, action="CLOSE", expected_revision=1,
            request_key="stale-close", delivery_id=handoff.receipt["delivery_id"])
    observed = recovery.record_hold(
        "m", handoff.followup_id, kind="OWNERSHIP_LOST", reason="witnessed loss",
        expected_revision=1, request_key="loss")
    assert observed["stale_observation"] is True
    with pytest.raises(NativeCompletionError, match="not ready to close"):
        ctx.adapter.close(handoff)
    assert delivery.inspect_recovery("m")["followup_blockers"]
    assert ctx.counts() == (1, 1, 0)


def test_late_loss_after_closed_keeps_completed_delivery_and_blocks(context):
    ctx = context
    ctx.discharge()
    settle_time(ctx)
    assert ctx.sending.step() is DistributionState.IDLE
    handoff = ctx.adapter.require_handoff(ctx.piece)
    assert followup(ctx)["state"] == "CLOSED"
    observed = recovery.record_hold(
        "m", handoff.followup_id, kind="OWNERSHIP_LOST", reason="late contradiction",
        expected_revision=3, request_key="late-loss")
    assert observed["stale_observation"] is True
    row = followup(ctx)
    assert row["state"] == "CLOSED"
    assert row["ownership_loss_reason"] == "late contradiction"
    assert fresh_owner_blocker() == "unresolved native completion follow-up"
    assert ctx.counts() == (1, 1, 0)


def test_closed_followup_does_not_waive_independent_discrepancy(context):
    ctx = context
    ctx.discharge()
    settle_time(ctx)
    assert ctx.sending.step() is DistributionState.IDLE
    with connect(ctx.path) as conn:
        conn.execute(
            "INSERT INTO smart_bin_discrepancies "
            "(id,machine_id,reservation_id,kind,status,details_json,created_at) "
            "VALUES('independent','m',?,'LATE_OBSERVATION','open','{}',1)",
            (ctx.bench.reservation_id,))
    assert delivery.inspect_recovery("m")["followup_blockers"] == []
    assert fresh_owner_blocker() == "unresolved durable discrepancy"


def test_missing_required_history_evidence_blocks_closed_row(context):
    ctx = context
    ctx.discharge()
    settle_time(ctx)
    assert ctx.sending.step() is DistributionState.IDLE
    handoff = ctx.adapter.require_handoff(ctx.piece)
    with connect(ctx.path) as conn:
        conn.execute(
            "DELETE FROM piece_records WHERE uuid=?", (handoff.piece.uuid,))
    report = delivery.inspect_recovery("m")
    assert report["followup_blockers"][0]["missing_evidence"] is True
    assert fresh_owner_blocker() == "unresolved native completion follow-up"
    assert not ctx.adapter.releases_admission(handoff.reservation_id, handoff.attempt_id)
    assert not ctx.bench.runtime.can_admit


def test_exact_transition_replay_precedes_stale_check_and_payload_conflicts(context):
    ctx = context
    ctx.discharge()
    handoff = ctx.adapter.require_handoff(ctx.piece)
    ctx.adapter.complete(ctx.piece, runtime_run_id=ctx.gc.run_recorder.run_id,
                         completed_at=time.time() + 5)
    key = f"{handoff.request_key}:publication-attempt"
    ctx.adapter.begin_publication(handoff)
    first = recovery.transition(
        "m", handoff.followup_id, action="PUBLICATION_ATTEMPT",
        expected_revision=1, request_key=key,
        delivery_id=handoff.receipt["delivery_id"])
    assert first["state"] == "PUBLICATION_ATTEMPTED"
    with pytest.raises(recovery.CompletionRecoveryError, match="idempotency"):
        recovery.transition(
            "m", handoff.followup_id, action="PUBLICATION_ATTEMPT",
            expected_revision=2, request_key=key,
            delivery_id=handoff.receipt["delivery_id"])
    assert followup(ctx)["state"] == "PUBLICATION_ATTEMPTED"


def test_standalone_completed_delivery_is_explicit_recovery_blocker(prepared):
    claimed = claim()
    intent = prepare(claimed)
    confirm(claimed, intent)
    assert complete(claimed, intent)["code"] == "OK"
    report = delivery.inspect_recovery("m")
    assert report["followups"] == []
    assert report["followup_blockers"][0]["reservation_id"] == claimed["reservation_id"]
    assert fresh_owner_blocker() == "unresolved native completion follow-up"


def test_incompatible_followup_extension_is_rejected(prepared):
    with connect(prepared) as conn:
        conn.execute("UPDATE smart_bin_completion_versions SET version=2")
    with pytest.raises(recovery.CompletionRecoveryError, match="unsupported"):
        delivery.inspect_recovery("m")
    with pytest.raises(recovery.CompletionRecoveryError, match="unsupported"):
        recovery.initialize_schema()


def test_closed_handoffs_succeed_in_one_runtime_adapter_and_transport(context):
    ctx = context
    progress = _Progress()
    ctx.gc.set_progress_tracker = progress
    transport = ctx.shared.transport
    runtime = ctx.bench.runtime
    adapter = ctx.adapter
    ctx.discharge()
    settle_time(ctx)
    assert ctx.sending.step() is DistributionState.IDLE
    first, first_handoff = second_native_piece(ctx)
    assert ctx.shared.transport is transport
    assert ctx.bench.runtime is runtime
    assert ctx.adapter is adapter
    assert first_handoff.closure_durable and first_handoff.admission_released
    first_revision = first_handoff.followup_revision
    with connect(ctx.path) as conn:
        first_row = dict(conn.execute(
            "SELECT * FROM smart_bin_completion_followups WHERE id=?",
            (first_handoff.followup_id,)).fetchone())
    assert first_row["state"] == "CLOSED"
    ctx.clock[0] += 2
    assert ctx.sending.step() is None
    ctx.clock[0] += CHUTE_SETTLE_MS / 1000 + 0.1
    assert ctx.sending.step() is DistributionState.IDLE
    second_handoff = ctx.adapter.require_handoff(ctx.piece)
    with connect(ctx.path) as conn:
        rows = conn.execute(
            "SELECT id,state,row_revision FROM smart_bin_completion_followups "
            "ORDER BY created_at,id").fetchall()
        assert len(rows) == 2
        assert {row["id"] for row in rows} == {
            first_handoff.followup_id, second_handoff.followup_id}
        assert all(row["state"] == "CLOSED" for row in rows)
        assert conn.execute("SELECT count(*) FROM smart_bin_deliveries").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM piece_records WHERE uuid IN (?,?)",
                            (first.uuid, ctx.piece.uuid)).fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM smart_bin_audit_events WHERE "
                            "request_key IN (?,?)", (f"{first_handoff.request_key}:close",
                            f"{second_handoff.request_key}:close")).fetchone()[0] == 2
    assert first_handoff.followup_revision == first_revision
    assert ctx.events.qsize() == progress.calls == 2
    assert len(ctx.gc.run_recorder.pieces) == 2
    assert delivery.inspect_recovery("m")["followup_blockers"] == []


@pytest.mark.parametrize("predecessor", ["pending", "failed", "lost"])
def test_unresolved_predecessor_rejects_successor_without_transport_change(context,
                                                                          predecessor):
    ctx = context
    ctx.discharge()
    first = ctx.adapter.require_handoff(ctx.piece)
    if predecessor == "failed":
        ctx.gc.set_progress_tracker = _Progress()
        ctx.gc.set_progress_tracker.fail = True
        settle_time(ctx)
        assert ctx.sending.step() is None
        assert first.publication_state == "failed"
    elif predecessor == "lost":
        late_hold(ctx, first)
    attempted = replace(first, followup_id="other-followup", request_key="other-request",
                        transport=None)
    before = ctx.shared.transport.getPieceForDistributionDrop()
    row_before = followup(ctx)
    with ctx.owner.lock, pytest.raises(
        NativeCompletionError, match="previous native completion remains held"
    ):
        ctx.adapter.retain_handoff(attempted, transport=ctx.shared.transport)
    assert ctx.shared.transport.getPieceForDistributionDrop() is before
    assert ctx.adapter.require_handoff(ctx.piece) is first
    assert followup(ctx) == row_before
    assert attempted.transport is None


@pytest.mark.parametrize("kind", ["OWNERSHIP_LOST", "SIDE_EFFECT_FAILED"])
def test_late_hold_after_closed_fences_live_owner_and_replayed_close(context, kind):
    ctx = context
    ctx.discharge()
    settle_time(ctx)
    assert ctx.sending.step() is DistributionState.IDLE
    handoff = ctx.adapter.require_handoff(ctx.piece)
    closed_revision = handoff.followup_revision
    observed = late_hold(ctx, handoff, kind=kind)
    assert observed["row_revision"] == closed_revision + 1
    assert fresh_owner_blocker() == "unresolved native completion follow-up"
    assert not ctx.adapter.releases_admission(handoff.reservation_id, handoff.attempt_id)
    assert ctx.adapter.blocks_admission()
    assert not ctx.bench.runtime.can_admit
    for _ in range(2):
        assert ctx.sending.step() is None
        assert not ctx.shared.distribution_ready
    with pytest.raises(NativeCompletionError):
        ctx.adapter.release_admission(ctx.piece)
    replay = recovery.transition(
        "m", handoff.followup_id, action="CLOSE", expected_revision=3,
        request_key=f"{handoff.request_key}:close",
        delivery_id=handoff.receipt["delivery_id"])
    assert replay["state"] == "CLOSED" and replay["row_revision"] == closed_revision
    row = followup(ctx)
    assert row["row_revision"] == observed["row_revision"]
    assert row["ownership_loss_reason"] == (
        "later durable witness" if kind == "OWNERSHIP_LOST" else None)
    assert row["failure_kind"] == (
        "SIDE_EFFECT_FAILED" if kind == "SIDE_EFFECT_FAILED" else None)
    attempted = replace(handoff, followup_id="other-followup", request_key="other-request",
                        transport=None)
    with ctx.owner.lock, pytest.raises(
        NativeCompletionError, match="previous native completion remains held"
    ):
        ctx.adapter.retain_handoff(attempted, transport=ctx.shared.transport)
    assert ctx.adapter.require_handoff(ctx.piece) is handoff
    assert attempted.transport is None
    assert ctx.events.qsize() == 1 and ctx.counts()[:2] == (1, 1)


@pytest.mark.parametrize("intervening_hold", [False, True])
def test_lost_attempt_ack_replays_receipt_but_checks_current_hold(context,
                                                                  monkeypatch,
                                                                  intervening_hold):
    ctx = context
    progress = _Progress()
    ctx.gc.set_progress_tracker = progress
    ctx.discharge()
    settle_time(ctx)
    actual = recovery.transition
    attempts = []

    def lose_attempt(*args, **kwargs):
        result = actual(*args, **kwargs)
        if kwargs["action"] == "PUBLICATION_ATTEMPT":
            attempts.append(result)
            if len(attempts) == 1:
                if intervening_hold:
                    late_hold(ctx, ctx.adapter.require_handoff(ctx.piece))
                raise OSError("publication attempt acknowledgement lost")
        return result

    monkeypatch.setattr(recovery, "transition", lose_attempt)
    assert ctx.sending.step() is None
    handoff = ctx.adapter.require_handoff(ctx.piece)
    assert followup(ctx)["state"] == "PUBLICATION_ATTEMPTED"
    assert ctx.events.empty() and progress.calls == 0
    assert ctx.sending.step() is (None if intervening_hold else DistributionState.IDLE)
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert ctx.events.qsize() == progress.calls == (0 if intervening_hold else 1)
    assert followup(ctx)["state"] == ("PUBLICATION_ATTEMPTED" if intervening_hold else "CLOSED")
    assert ctx.counts()[:2] == (1, 1)
    if intervening_hold:
        assert not ctx.shared.distribution_ready
        assert not ctx.bench.runtime.can_admit
        assert followup(ctx)["ownership_loss_reason"] == "later durable witness"
        assert followup(ctx)["failure_kind"] is None
    else:
        assert ctx.shared.distribution_ready and handoff.admission_released
        assert ctx.sending.step() is DistributionState.IDLE
        assert ctx.events.qsize() == progress.calls == 1
        with connect(ctx.path) as conn:
            assert conn.execute(
                "SELECT count(*) FROM smart_bin_audit_events WHERE request_key IN (?,?)",
                (f"{handoff.request_key}:publication-attempt",
                 f"{handoff.request_key}:close")).fetchone()[0] == 2


def test_old_retention_receipt_does_not_reset_progressed_revision(context):
    ctx = context
    ctx.discharge()
    handoff = ctx.adapter.require_handoff(ctx.piece)
    ctx.adapter.complete(ctx.piece, runtime_run_id=ctx.gc.run_recorder.run_id,
                         completed_at=time.time() + 5)
    assert handoff.followup_revision == 1
    ctx.adapter.persist_retained_handoff()
    assert handoff.followup_revision == 1
    assert followup(ctx)["state"] == "DELIVERY_PENDING_PUBLICATION"


def test_followup_eligibility_read_failure_keeps_admission_held(context, monkeypatch):
    ctx = context
    ctx.discharge()
    settle_time(ctx)
    assert ctx.sending.step() is DistributionState.IDLE
    handoff = ctx.adapter.require_handoff(ctx.piece)
    monkeypatch.setattr(delivery, "_readonly", lambda: (_ for _ in ()).throw(
        OSError("eligibility read failed")))
    assert not ctx.adapter.releases_admission(handoff.reservation_id, handoff.attempt_id)
    assert ctx.adapter.blocks_admission()
    assert not ctx.bench.runtime.can_admit
    assert ctx.sending.step() is None
    assert not ctx.shared.distribution_ready
    assert ctx.events.qsize() == 1


@pytest.mark.parametrize("failure", ["before_commit", "lost_ack"])
def test_close_write_failure_retries_original_request_without_side_effect_hold(
    context, monkeypatch, failure
):
    ctx = context
    progress = _Progress()
    ctx.gc.set_progress_tracker = progress
    ctx.discharge()
    settle_time(ctx)
    actual_transition = recovery.transition
    actual_gate = ctx.shared.set_distribution_gate
    close_calls = []
    gate_opens = []

    def observe_gate(ready, *, reason):
        if ready:
            gate_opens.append(reason)
        return actual_gate(ready, reason=reason)

    def interrupt_close(*args, **kwargs):
        if kwargs["action"] == "CLOSE":
            close_calls.append((kwargs["request_key"], kwargs["expected_revision"],
                                kwargs["delivery_id"]))
            if len(close_calls) == 1:
                if failure == "lost_ack":
                    actual_transition(*args, **kwargs)
                raise OSError("closure acknowledgement unavailable")
        return actual_transition(*args, **kwargs)

    monkeypatch.setattr(ctx.shared, "set_distribution_gate", observe_gate)
    monkeypatch.setattr(recovery, "transition", interrupt_close)
    assert ctx.sending.step() is None
    handoff = ctx.adapter.require_handoff(ctx.piece)
    assert not gate_opens and not ctx.shared.distribution_ready
    assert not handoff.admission_released and not ctx.bench.runtime.can_admit
    assert followup(ctx)["state"] == (
        "PUBLICATION_SUCCEEDED" if failure == "before_commit" else "CLOSED")
    assert followup(ctx)["failure_kind"] is None
    assert ctx.events.qsize() == progress.calls == 1
    assert ctx.counts()[:2] == (1, 1)
    with connect(ctx.path) as conn:
        before_close_audits = conn.execute(
            "SELECT count(*) FROM smart_bin_audit_events WHERE request_key=?",
            (f"{handoff.request_key}:close",)).fetchone()[0]
    assert before_close_audits == (0 if failure == "before_commit" else 1)

    assert ctx.sending.step() is DistributionState.IDLE
    assert len(close_calls) == 2 and close_calls[0] == close_calls[1]
    assert len(gate_opens) == 1 and ctx.shared.distribution_ready
    assert handoff.admission_released and handoff.closure_durable
    assert followup(ctx)["state"] == "CLOSED"
    assert followup(ctx)["failure_kind"] is None
    assert ctx.events.qsize() == progress.calls == 1
    assert ctx.counts()[:2] == (1, 1)
    with connect(ctx.path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM smart_bin_audit_events WHERE request_key=?",
            (f"{handoff.request_key}:close",)).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM smart_bin_completion_receipts WHERE request_key=?",
            (f"{handoff.request_key}:close",)).fetchone()[0] == 1


def test_real_gate_failure_retains_side_effect_hold(context, monkeypatch):
    ctx = context
    progress = _Progress()
    ctx.gc.set_progress_tracker = progress
    ctx.discharge()
    settle_time(ctx)
    actual_gate = ctx.shared.set_distribution_gate
    gate_opens = []

    def fail_open(ready, *, reason):
        if ready:
            gate_opens.append(reason)
            raise OSError("distribution gate failed to open")
        return actual_gate(ready, reason=reason)

    monkeypatch.setattr(ctx.shared, "set_distribution_gate", fail_open)
    assert ctx.sending.step() is None
    handoff = ctx.adapter.require_handoff(ctx.piece)
    assert len(gate_opens) == 1
    assert followup(ctx)["state"] == "CLOSED"
    assert followup(ctx)["failure_kind"] == "SIDE_EFFECT_FAILED"
    assert not ctx.shared.distribution_ready and not handoff.admission_released
    assert not ctx.bench.runtime.can_admit
    assert ctx.events.qsize() == progress.calls == 1
    assert ctx.sending.step() is None
    assert len(gate_opens) == 1 and ctx.events.qsize() == progress.calls == 1


@pytest.mark.parametrize("kind", ["OWNERSHIP_LOST", "SIDE_EFFECT_FAILED"])
def test_replaced_predecessor_late_hold_fences_live_owner(context, kind):
    ctx = context
    progress = _Progress()
    ctx.gc.set_progress_tracker = progress
    ctx.discharge()
    settle_time(ctx)
    assert ctx.sending.step() is DistributionState.IDLE
    first, first_handoff = second_native_piece(ctx)
    ctx.clock[0] += 2
    assert ctx.sending.step() is None
    ctx.clock[0] += CHUTE_SETTLE_MS / 1000 + 0.1
    assert ctx.sending.step() is DistributionState.IDLE
    second = ctx.piece
    second_handoff = ctx.adapter.require_handoff(second)
    assert ctx.events.qsize() == progress.calls == 2
    with connect(ctx.path) as conn:
        delivered_before = [tuple(row) for row in conn.execute(
            "SELECT id,piece_uuid,evidence_ref FROM smart_bin_deliveries ORDER BY id")]
        history_before = [tuple(row) for row in conn.execute(
            "SELECT uuid,run_id FROM piece_records WHERE uuid IN (?,?) ORDER BY uuid",
            (first.uuid, second.uuid))]
        audit_before = conn.execute(
            "SELECT count(*) FROM smart_bin_audit_events").fetchone()[0]
    assert len(delivered_before) == len(history_before) == 2
    hold = late_hold(ctx, first_handoff, kind=kind, key=f"predecessor-{kind}")
    assert hold["row_revision"] == first_handoff.followup_revision + 1
    assert fresh_owner_blocker() == "unresolved native completion follow-up"
    with pytest.raises(CurrentFollowupHeld):
        ctx.adapter.require_current_followup(second_handoff, states=("CLOSED",))
    assert not ctx.adapter.releases_admission(
        second_handoff.reservation_id, second_handoff.attempt_id)
    assert ctx.adapter.blocks_admission() and not ctx.bench.runtime.can_admit
    assert ctx.sending.step() is None
    assert not ctx.shared.distribution_ready
    assert ctx.sending.step() is None
    assert ctx.events.qsize() == progress.calls == 2
    attempted = replace(second_handoff, followup_id="third-followup",
                        request_key="third-request", transport=None)
    with ctx.owner.lock, pytest.raises(
        NativeCompletionError, match="previous native completion remains held"
    ):
        ctx.adapter.retain_handoff(attempted, transport=ctx.shared.transport)
    assert attempted.transport is None
    assert ctx.adapter.require_handoff(second) is second_handoff
    for handoff in (first_handoff, second_handoff):
        replay = recovery.transition(
            "m", handoff.followup_id, action="CLOSE", expected_revision=3,
            request_key=f"{handoff.request_key}:close",
            delivery_id=handoff.receipt["delivery_id"])
        assert replay["state"] == "CLOSED"
    with connect(ctx.path) as conn:
        delivered_after = [tuple(row) for row in conn.execute(
            "SELECT id,piece_uuid,evidence_ref FROM smart_bin_deliveries ORDER BY id")]
        assert delivered_after == delivered_before
        assert [tuple(row) for row in conn.execute(
            "SELECT uuid,run_id FROM piece_records WHERE uuid IN (?,?) ORDER BY uuid",
            (first.uuid, second.uuid))] == history_before
        assert conn.execute("SELECT count(*) FROM smart_bin_audit_events").fetchone()[0] == audit_before + 1
    assert ctx.counts()[:2] == (2, 1)


def test_predecessor_hold_before_second_publication_blocks_callbacks(context, monkeypatch):
    ctx = context
    progress = _Progress()
    ctx.gc.set_progress_tracker = progress
    ctx.discharge()
    settle_time(ctx)
    assert ctx.sending.step() is DistributionState.IDLE
    first, first_handoff = second_native_piece(ctx)
    ctx.clock[0] += 2
    assert ctx.sending.step() is None
    ctx.clock[0] += CHUTE_SETTLE_MS / 1000 + 0.1
    actual_complete = delivery.complete_native

    def complete_then_hold(*args, **kwargs):
        receipt = actual_complete(*args, **kwargs)
        late_hold(ctx, first_handoff, key="predecessor-before-publication")
        return receipt

    monkeypatch.setattr(delivery, "complete_native", complete_then_hold)
    assert ctx.sending.step() is None
    second_handoff = ctx.adapter.require_handoff(ctx.piece)
    assert ctx.piece.native_delivery_id == second_handoff.receipt["delivery_id"]
    assert followup(ctx)["state"] == "DELIVERY_PENDING_PUBLICATION"
    assert followup(ctx)["failure_kind"] is None
    assert ctx.events.qsize() == progress.calls == 1
    assert len(ctx.gc.run_recorder.pieces) == 1
    assert not ctx.shared.distribution_ready and not ctx.bench.runtime.can_admit
    assert ctx.sending.step() is None
    assert ctx.events.qsize() == progress.calls == 1
    assert ctx.counts()[:2] == (2, 1)
    assert first.uuid != ctx.piece.uuid


def test_other_machine_followup_hold_does_not_block_publication(context):
    ctx = context
    ctx.discharge()
    other_machine_held_followup(ctx.path)
    assert delivery.inspect_recovery("other-machine")["followup_blockers"]
    settle_time(ctx)
    assert ctx.sending.step() is DistributionState.IDLE
    assert ctx.events.qsize() == 1 and ctx.shared.distribution_ready
    assert ctx.bench.runtime.can_admit
    assert delivery.inspect_recovery("m")["followup_blockers"] == []


def test_shared_authorization_read_failure_fences_closed_owner(context, monkeypatch):
    ctx = context
    ctx.discharge()
    settle_time(ctx)
    assert ctx.sending.step() is DistributionState.IDLE
    handoff = ctx.adapter.require_handoff(ctx.piece)
    monkeypatch.setattr(recovery, "inspect_on_connection", lambda *args: (
        _ for _ in ()).throw(OSError("recovery assessment unavailable")))
    assert not ctx.adapter.releases_admission(handoff.reservation_id, handoff.attempt_id)
    assert not ctx.bench.runtime.can_admit
    assert ctx.sending.step() is None
    assert not ctx.shared.distribution_ready
