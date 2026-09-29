"""Dormant recovery persistence with synthetic historical evidence only."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from test_smart_bins_delivery import (
    prepared as prepared, connect, claim, prepare, confirm, completion_evidence, custody,
)
import smart_bins_completion_recovery as recovery
import smart_bins_delivery as delivery


def retained(path, *, piece="piece", pocket=0, key="complete"):
    from test_smart_bins_delivery import release_evidence, exit_evidence
    reserved = claim(piece, "reserve-" + key, pocket=pocket)
    intent = prepare(reserved, key="intent-" + key,
                     evidence=release_evidence(piece, pocket, boundary=17 + pocket))
    confirm(reserved, intent, key="exit-" + key,
            evidence=exit_evidence(intent["attempt_id"], piece=piece,
                                   pocket=pocket, boundary=17 + pocket))
    handoff = SimpleNamespace(
        followup_id="followup-" + key, request_key=key, machine_id="m",
        reservation_id=reserved["reservation_id"], attempt_id=intent["attempt_id"],
        piece=SimpleNamespace(uuid=piece), custody=custody(piece, pocket),
        marker_evidence_ref="marker-confirmation", target_boundary=17 + pocket,
        intended_kind="BIN", intended_slot_id="s0", intended_cycle_id="c0")
    receipt = recovery.stage_handoff(handoff)
    assert receipt["state"] == "RETAINED"
    return SimpleNamespace(path=path, handoff=handoff, piece=handoff.piece,
                           evidence=completion_evidence(intent["attempt_id"], piece=piece,
                                                        pocket=pocket, boundary=17 + pocket))


def followup(ctx):
    with connect(ctx.path) as conn:
        return recovery.read_on_connection(conn, "m", ctx.handoff.followup_id)


def complete_retained(ctx):
    h = ctx.handoff
    receipt = delivery.complete_native(
        "m", h.reservation_id, expected_reservation_revision=2,
        request_key=h.request_key, evidence=ctx.evidence, followup_id=h.followup_id)
    assert receipt["code"] == "OK"
    h.receipt = receipt
    return receipt


def advance(ctx, action):
    row = followup(ctx)
    return recovery.transition(
        "m", ctx.handoff.followup_id, action=action, expected_revision=row["row_revision"],
        request_key=ctx.handoff.request_key + ":" + action.lower().replace("_", "-"),
        delivery_id=row["delivery_id"])


def close(ctx):
    for action in ("PUBLICATION_ATTEMPT", "PUBLICATION_SUCCEEDED", "CLOSE"):
        advance(ctx, action)


def late_hold(ctx, handoff, *, kind="OWNERSHIP_LOST", key="late-hold", phase=None):
    return recovery.record_hold(
        "m", handoff.followup_id, kind=kind, reason="later synthetic witness",
        expected_revision=followup(ctx)["row_revision"], request_key=key, failure_phase=phase)


@pytest.fixture
def context(prepared):
    return retained(prepared)


@pytest.fixture
def completed(context):
    complete_retained(context)
    return context


def test_retention_exact_replay_and_restart_hold(context):
    original = followup(context)
    assert recovery.stage_handoff(context.handoff)["state"] == "RETAINED"
    assert followup(context) == original
    report = delivery.inspect_recovery("m")
    assert report["followup_blockers"][0]["state"] == "RETAINED"
    with connect(context.path) as conn:
        assert conn.execute("SELECT count(*) FROM smart_bin_deliveries").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM smart_bin_release_attempts").fetchone()[0] == 1


def test_link_failure_rolls_back_delivery_history_evidence_and_receipt(context, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("followup link failed")
    monkeypatch.setattr(recovery, "bind_delivery", fail)
    with pytest.raises(OSError, match="link failed"):
        complete_retained(context)
    assert followup(context)["state"] == "RETAINED"
    with connect(context.path) as conn:
        for name in ("smart_bin_deliveries", "piece_records"):
            assert conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0] == 0
        assert conn.execute("SELECT state FROM smart_bin_reservations").fetchone()[0] == "EXIT_CONFIRMED"
        assert conn.execute("SELECT completion_json FROM smart_bin_release_evidence").fetchone()[0] is None
    assert delivery.lookup_request(context.handoff.request_key) is None


def test_lost_completion_ack_replays_original_and_preserves_link(completed):
    original = completed.handoff.receipt
    assert complete_retained(completed) == original
    assert followup(completed)["delivery_id"] == original["delivery_id"]
    with connect(completed.path) as conn:
        assert conn.execute("SELECT count(*) FROM smart_bin_deliveries").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM piece_records").fetchone()[0] == 1


@pytest.mark.parametrize("steps,state", [
    ((), "DELIVERY_PENDING_PUBLICATION"),
    (("PUBLICATION_ATTEMPT",), "PUBLICATION_ATTEMPTED"),
    (("PUBLICATION_ATTEMPT", "PUBLICATION_SUCCEEDED"), "PUBLICATION_SUCCEEDED"),
])
def test_restart_preserves_missing_close_obligation(completed, steps, state):
    for action in steps:
        advance(completed, action)
    for _ in range(2):
        assert delivery.inspect_recovery("m")["followup_blockers"][0]["state"] == state
    with connect(completed.path) as conn:
        assert conn.execute("SELECT count(*) FROM smart_bin_release_attempts").fetchone()[0] == 1


def test_old_close_receipt_does_not_erase_later_hold(completed):
    close(completed)
    assert delivery.inspect_recovery("m")["followup_blockers"] == []
    h = completed.handoff
    late_hold(completed, h)
    replay = recovery.transition("m", h.followup_id, action="CLOSE", expected_revision=3,
                                 request_key=h.request_key + ":close",
                                 delivery_id=h.receipt["delivery_id"])
    assert replay["state"] == "CLOSED"
    assert delivery.inspect_recovery("m")["followup_blockers"][0]["ownership_loss_reason"]
    with pytest.raises(recovery.CompletionRecoveryError, match="conflict"):
        recovery.transition("m", h.followup_id, action="CLOSE", expected_revision=3,
                            request_key=h.request_key + ":close",
                            delivery_id=h.receipt["delivery_id"], reason="changed")


@pytest.mark.parametrize("phase", ["PUBLICATION_CALLBACKS", "GATE_OPEN", "ADMISSION_RELEASE", None])
def test_failure_phase_is_durable_and_replayed(completed, phase):
    result = late_hold(completed, completed.handoff, kind="SIDE_EFFECT_FAILED", phase=phase)
    with connect(completed.path) as conn:
        receipt = json.loads(conn.execute("SELECT result_json FROM smart_bin_completion_receipts "
                                          "WHERE request_key='late-hold'").fetchone()[0])
    assert receipt == result
    assert result.get("failure_phase") == phase
    assert delivery.inspect_recovery("m")["followup_blockers"][0]["failure_kind"] == "SIDE_EFFECT_FAILED"


def test_closed_missing_history_is_still_blocked(completed):
    close(completed)
    with connect(completed.path) as conn:
        conn.execute("DELETE FROM piece_records")
    assert delivery.inspect_recovery("m")["followup_blockers"][0]["missing_evidence"]


def test_successor_cannot_hide_closed_predecessor_hold(completed):
    close(completed)
    second = retained(completed.path, piece="second", pocket=1, key="second-complete")
    complete_retained(second)
    late_hold(completed, completed.handoff)
    with delivery._readonly() as conn:
        current, others = recovery.authorization_on_connection(conn, "m", second.handoff.followup_id)
    assert current["id"] == second.handoff.followup_id
    assert any(row["followup_id"] == completed.handoff.followup_id for row in others)


def test_conflicting_completion_payload_does_not_rewrite_history(completed):
    h = completed.handoff
    changed = replace(completed.evidence, runtime_run_id="changed")
    with pytest.raises(recovery.CompletionRecoveryError, match="replay lacks its follow-up"):
        delivery.complete_native("m", h.reservation_id, expected_reservation_revision=2,
                                 request_key=h.request_key, evidence=changed,
                                 followup_id=h.followup_id)
    assert complete_retained(completed) == h.receipt
    with connect(completed.path) as conn:
        assert conn.execute("SELECT run_id FROM piece_records").fetchone()[0] == "runtime-run"
