"""Synthetic, inactive qualification of completed native follow-up dispositions."""

from __future__ import annotations

# The imported fixture isolates local-state paths before importing backend modules.
from test_smart_bins_completion_recovery import (
    context, followup, fresh_owner_blocker, late_hold, second_native_piece,
    settle_time,
)
from test_smart_bins_delivery import connect, prepared
from test_smart_bins_sending import _Progress

import json
import time
from dataclasses import replace

import pytest

import smart_bins_completion_recovery as recovery
import smart_bins_delivery as delivery
import smart_bins_followup_reconciliation as recon
from smart_bins_reconciliation import EvidenceReference


@pytest.fixture
def completed(context):
    ctx = context
    recon.initialize_schema()
    ctx.discharge()
    handoff = ctx.adapter.require_handoff(ctx.piece)
    ctx.adapter.complete(
        ctx.piece, runtime_run_id=ctx.gc.run_recorder.run_id,
        completed_at=time.time() + 5)
    assert followup(ctx)["state"] == "DELIVERY_PENDING_PUBLICATION"
    return ctx, handoff


def reference(name):
    return EvidenceReference(f"synthetic:{name}", "qualified_synthetic_owner_witness")


def request_for(ctx, handoff, *, effects=(), holds=(), resolutions=(), key="reconcile-one",
                actor="synthetic_owner_reviewer"):
    row = followup(ctx)
    with connect(ctx.path) as conn:
        machine_revision = conn.execute(
            "SELECT state_revision FROM smart_bin_machines WHERE machine_id='m'").fetchone()[0]
    request = recon.ReconciliationRequest(
        machine_id="m", followup_id=row["id"], reservation_id=row["reservation_id"],
        delivery_id=row["delivery_id"], request_key=key,
        expected_machine_revision=machine_revision,
        expected_followup_revision=row["row_revision"], actor=actor,
        reason="synthetic evidence disposition", original_attempt_id=row["release_attempt_id"],
        original_marker_evidence_ref=row["marker_evidence_ref"],
        original_completion_evidence_ref=json.loads(row["completion_json"])["completion_evidence_ref"],
        effects=effects, holds=holds, resolutions=resolutions)
    now = time.time()
    owner = recon.OwnerSnapshot(
        machine_id="m", followup_id=row["id"], reservation_id=row["reservation_id"],
        current_owner_incarnation="current-owner-separate-from-original",
        authority_revision="synthetic-current-owner-revision", observed_at=now,
        expires_at=now + 4, current_owner=True, quiescent=True,
        publication_attempt_inactive=True, dispatch_invalidated=True,
        gate_open=False, evidence=reference("fresh-owner-quiescence"))
    return request, owner


def claim(req, effect, status="APPLIED", *, ref=None, proof_kind="OWNER_OBSERVATION"):
    return recon.EffectEvidence(
        effect=effect, status=status, evidence=reference(ref or effect),
        proof_kind=proof_kind, followup_id=req.followup_id,
        delivery_id=req.delivery_id, original_attempt_id=req.original_attempt_id)


def propose_resolution(req, owner, obligation_id):
    first = recon.preview(req, owner)
    target = next(item for item in first["resolvable"] if item["id"] == obligation_id)
    return replace(req, resolutions=(recon.Resolution(
        obligation_id=obligation_id, source_digest=target["source_digest"],
        evidence_ref=req.effects[0].evidence.ref if req.effects else owner.evidence.ref),))


def apply_reviewed(req, owner):
    preview = recon.preview(req, owner)
    assert preview["code"] == "READY", preview
    return recon.apply(req, owner, expected_preview_hash=preview["preview_hash"])


def inventory(conn, reservation_id):
    return tuple(tuple(row) for row in conn.execute(
        "SELECT id,state,quantity,intended_kind,intended_slot_id,intended_cycle_id "
        "FROM smart_bin_reservations WHERE id=?", (reservation_id,)))


def preserved(conn, req):
    return {
        "reservation": inventory(conn, req.reservation_id),
        "delivery": tuple(tuple(row) for row in conn.execute(
            "SELECT * FROM smart_bin_deliveries WHERE id=?", (req.delivery_id,))),
        "history": tuple(tuple(row) for row in conn.execute(
            "SELECT * FROM piece_records WHERE uuid=(SELECT piece_uuid FROM "
            "smart_bin_completion_followups WHERE id=?)", (req.followup_id,))),
        "attempt": tuple(tuple(row) for row in conn.execute(
            "SELECT * FROM smart_bin_release_evidence WHERE attempt_id=?",
            (req.original_attempt_id,))),
        "old_receipts": tuple(tuple(row) for row in conn.execute(
            "SELECT * FROM smart_bin_completion_receipts ORDER BY request_key")),
    }


def test_ambiguous_attempt_resolves_only_with_attributed_producer_evidence(completed):
    ctx, handoff = completed
    ctx.adapter.begin_publication(handoff)
    req, owner = request_for(ctx, handoff)
    assert recon.preview(req, owner)["effects"]["EVENT_ENQUEUED"]["status"] == "UNKNOWN"
    req = replace(req, effects=tuple(claim(req, effect) for effect in recon.PRODUCER_EFFECTS))
    req = propose_resolution(req, owner, "PUBLICATION")
    preview = recon.preview(req, owner)
    assert [item["id"] for item in preview["remaining"]] == [
        "CLOSURE_PENDING", "OWNER_REQUALIFICATION"]
    with connect(ctx.path) as conn:
        before = preserved(conn, req)
    result = apply_reviewed(req, owner)
    assert result["code"] == "OK" and result["quantity_effect"] == 0
    assert [item["id"] for item in result["remaining"]] == [
        "CLOSURE_PENDING", "OWNER_REQUALIFICATION"]
    assert [item["obligation_id"] for item in delivery.inspect_recovery("m")["followups"][0]
            ["resolved_obligations"]] == ["PUBLICATION"]
    live = delivery.inspect_recovery("m")["followups"][0]
    with delivery._readonly() as conn:
        current = recovery.assess_on_connection(conn, "m", handoff.followup_id)
    assert current["resolved_obligations"] == live["resolved_obligations"]
    assert current["remaining_obligations"] == live["remaining_obligations"]
    assert live["state"] == "PUBLICATION_ATTEMPTED"
    assert [item["id"] for item in live["remaining_obligations"]] == [
        "CLOSURE_PENDING", "OWNER_REQUALIFICATION"]
    assert live["effect_inventory"]["EVENT_CONSUMED"]["status"] == "UNKNOWN"
    assert fresh_owner_blocker() == "unresolved native completion follow-up"
    assert ctx.adapter.blocks_admission()
    assert ctx.events.empty() and not handoff.admission_released
    with connect(ctx.path) as conn:
        assert preserved(conn, req) == before
    assert recon.lookup_request(req.request_key)["result"] == result


def test_partial_not_applied_and_unknown_effects_keep_publication_blocked(completed):
    ctx, handoff = completed
    ctx.adapter.begin_publication(handoff)
    req, owner = request_for(ctx, handoff)
    req = replace(req, effects=(claim(req, "EVENT_ENQUEUED"),
                                claim(req, "RUN_RECORDER_ADOPTED", "NOT_APPLIED"),
                                claim(req, "PROGRESS_RECORDED", "UNKNOWN")))
    view = recon.preview(req, owner)
    assert view["code"] == "READY"
    assert view["resolvable"] == []
    assert view["effects"]["RUN_RECORDER_ADOPTED"]["status"] == "NOT_APPLIED"
    assert "PUBLICATION" in [item["id"] for item in view["remaining"]]
    assert apply_reviewed(req, owner)["code"] == "OK"
    restarted = delivery.inspect_recovery("m")
    assert "PUBLICATION" in [item["id"] for item in restarted["followup_blockers"][0]
                             ["remaining_obligations"]]
    assert restarted["followups"][0]["effect_inventory"]["RUN_RECORDER_ADOPTED"]["status"] == "NOT_APPLIED"


def test_tracker_absence_requires_one_attributed_pair(completed):
    ctx, handoff = completed
    ctx.adapter.begin_publication(handoff)
    req, owner = request_for(ctx, handoff)
    mandatory = tuple(claim(req, name) for name in recon.PRODUCER_EFFECTS[:2])
    one_optional = claim(req, "PROGRESS_RECORDED", "NOT_REQUIRED",
                         ref="tracker-absent-at-original-attempt",
                         proof_kind="ORIGINAL_TRACKER_ABSENT")
    req = replace(req, effects=mandatory + (one_optional,))
    assert "ORIGINAL_TRACKER_ABSENCE_NOT_PROVEN" in recon.preview(req, owner)["blockers"]
    second_optional = claim(req, "PROGRESS_SYNC_NOTIFIED", "NOT_REQUIRED",
                            ref="tracker-absent-at-original-attempt",
                            proof_kind="ORIGINAL_TRACKER_ABSENT")
    req = replace(req, effects=mandatory + (one_optional, second_optional))
    assert [item["id"] for item in recon.preview(req, owner)["resolvable"]] == ["PUBLICATION"]
    req = propose_resolution(req, owner, "PUBLICATION")
    assert apply_reviewed(req, owner)["code"] == "OK"
    live = delivery.inspect_recovery("m")["followups"][0]
    assert live["effect_inventory"]["PROGRESS_RECORDED"]["status"] == "NOT_REQUIRED"
    assert live["effect_inventory"]["EVENT_CONSUMED"]["status"] == "UNKNOWN"


def test_publication_succeeded_still_requires_normal_closure(completed):
    ctx, handoff = completed
    ctx.adapter.begin_publication(handoff)
    recovery.transition("m", handoff.followup_id, action="PUBLICATION_SUCCEEDED",
                        expected_revision=followup(ctx)["row_revision"],
                        request_key=f"{handoff.request_key}:publication-succeeded",
                        delivery_id=handoff.receipt["delivery_id"])
    req, owner = request_for(ctx, handoff)
    view = recon.preview(req, owner)
    assert "CLOSURE_PENDING" in [item["id"] for item in view["remaining"]]
    assert all(item["id"] != "CLOSURE_PENDING" for item in view["resolvable"])
    restarted = delivery.inspect_recovery("m")["followup_blockers"][0]
    assert "CLOSURE_PENDING" in [item["id"] for item in restarted["remaining_obligations"]]


def test_owner_or_gate_evidence_cannot_replace_callback_evidence(completed):
    ctx, handoff = completed
    ctx.adapter.begin_publication(handoff)
    late_hold(ctx, handoff, kind="SIDE_EFFECT_FAILED")
    req, owner = request_for(ctx, handoff)
    with connect(ctx.path) as conn:
        hold = conn.execute("SELECT id FROM smart_bin_audit_events WHERE request_key='late-hold'").fetchone()[0]
    req = replace(req, holds=(recon.HoldEvidence(hold, "GATE_OPEN", reference("gate-open")),))
    owner = replace(owner, gate_open=True)
    view = recon.preview(req, owner)
    assert view["resolvable"] == []
    assert "PUBLICATION" in [item["id"] for item in view["remaining"]]
    assert "HOLD:" + hold in [item["id"] for item in view["remaining"]]
    assert apply_reviewed(req, owner)["code"] == "OK"
    live = delivery.inspect_recovery("m")["followups"][0]
    assert not live["resolved_obligations"]
    assert "PUBLICATION" in [item["id"] for item in live["remaining_obligations"]]


def test_real_gate_failure_rejects_successful_callback_claim(completed, monkeypatch):
    ctx, handoff = completed
    progress = _Progress()
    ctx.gc.set_progress_tracker = progress
    actual_gate = ctx.shared.set_distribution_gate
    gate_attempts = []

    def fail_gate_open(ready, *, reason):
        if ready:
            gate_attempts.append(reason)
            raise OSError("synthetic gate opening failure")
        return actual_gate(ready, reason=reason)

    monkeypatch.setattr(ctx.shared, "set_distribution_gate", fail_gate_open)
    settle_time(ctx)
    assert ctx.sending.step() is None
    row = followup(ctx)
    assert row["state"] == "CLOSED" and row["failure_kind"] == "SIDE_EFFECT_FAILED"
    assert len(gate_attempts) == ctx.events.qsize() == progress.calls == 1
    assert not ctx.shared.distribution_ready and not handoff.admission_released
    with connect(ctx.path) as conn:
        hold_id = conn.execute(
            "SELECT id FROM smart_bin_audit_events WHERE request_key=?",
            (f"{handoff.request_key}:side-effect-failed",)).fetchone()[0]
    req, owner = request_for(ctx, handoff)
    req = replace(req, effects=tuple(claim(req, effect) for effect in recon.PRODUCER_EFFECTS),
                  holds=(recon.HoldEvidence(
                      hold_id, "CALLBACKS_APPLIED", reference("callbacks-returned")),))
    view = recon.preview(req, owner)
    assert "HOLD:" + hold_id not in [item["id"] for item in view["resolvable"]]
    assert "HOLD:" + hold_id in [item["id"] for item in view["remaining"]]


def test_named_closed_gate_hold_resolves_but_new_owner_hold_survives(completed, monkeypatch):
    ctx, handoff = completed
    actual_gate = ctx.shared.set_distribution_gate

    def fail_gate_open(ready, *, reason):
        if ready:
            raise OSError("synthetic gate opening failure")
        return actual_gate(ready, reason=reason)

    monkeypatch.setattr(ctx.shared, "set_distribution_gate", fail_gate_open)
    settle_time(ctx)
    assert ctx.sending.step() is None
    raw = followup(ctx)
    assert raw["state"] == "CLOSED" and raw["failure_kind"] == "SIDE_EFFECT_FAILED"
    req, owner = request_for(ctx, handoff)
    with connect(ctx.path) as conn:
        gate_id = conn.execute("SELECT id FROM smart_bin_audit_events WHERE request_key=?",
                               (f"{handoff.request_key}:side-effect-failed",)).fetchone()[0]
        recorded = json.loads(conn.execute(
            "SELECT result_json FROM smart_bin_completion_receipts WHERE request_key=?",
            (f"{handoff.request_key}:side-effect-failed",)).fetchone()[0])
    assert recorded["failure_phase"] == "GATE_OPEN"
    req = replace(req, holds=(recon.HoldEvidence(gate_id, "GATE_OPEN", reference("gate")),))
    owner = replace(owner, gate_open=True)
    req = propose_resolution(req, owner, "HOLD:" + gate_id)
    result = apply_reviewed(req, owner)
    live = delivery.inspect_recovery("m")["followups"][0]
    assert "synthetic gate opening failure" in live["failure_reason"]
    assert live["effect_inventory"]["PROGRESS_RECORDED"]["status"] == "UNKNOWN"
    assert [item["id"] for item in live["remaining_obligations"]] == ["OWNER_REQUALIFICATION"]
    assert live["resolved_obligations"][0]["obligation_id"] == "HOLD:" + gate_id
    recovery.record_hold("m", handoff.followup_id, kind="OWNERSHIP_LOST",
                         reason="later contradictory owner evidence",
                         expected_revision=raw["row_revision"], request_key="later-owner-hold")
    restarted = delivery.inspect_recovery("m")["followups"][0]
    assert any(item["kind"] == "OWNERSHIP_LOST" for item in restarted["remaining_obligations"])
    assert recon.apply(req, owner, expected_preview_hash=result["preview_hash"]) == result
    assert any(item["kind"] == "OWNERSHIP_LOST" for item in
               delivery.inspect_recovery("m")["followup_blockers"][0]["remaining_obligations"])
    assert ctx.adapter.blocks_admission()
    assert not ctx.adapter.releases_admission(handoff.reservation_id, handoff.attempt_id)


def test_unattributed_old_failure_phase_refuses_both_cause_labels(completed):
    ctx, handoff = completed
    settle_time(ctx)
    ctx.sending.step()
    assert followup(ctx)["state"] == "CLOSED"
    recovery.record_hold("m", handoff.followup_id, kind="SIDE_EFFECT_FAILED",
                         reason="old ambiguous side-effect failure",
                         expected_revision=followup(ctx)["row_revision"],
                         request_key="old-ambiguous-hold")
    with connect(ctx.path) as conn:
        hold_id = conn.execute("SELECT id FROM smart_bin_audit_events "
                               "WHERE request_key='old-ambiguous-hold'").fetchone()[0]
    req, owner = request_for(ctx, handoff)
    owner = replace(owner, gate_open=True)
    effects = tuple(claim(req, effect) for effect in recon.PRODUCER_EFFECTS)
    for cause in ("GATE_OPEN", "CALLBACKS_APPLIED"):
        candidate = replace(req, effects=effects,
                            holds=(recon.HoldEvidence(hold_id, cause, reference(cause)),))
        view = recon.preview(candidate, owner)
        assert view["code"] == "READY"
        assert "HOLD:" + hold_id not in [item["id"] for item in view["resolvable"]]
        assert any(item["id"] == "HOLD:" + hold_id and
                   item["failure_phase"] == "UNKNOWN" for item in view["remaining"])


def test_callback_phase_rejects_gate_evidence_and_accepts_callback_proof(completed,
                                                                          monkeypatch):
    ctx, handoff = completed
    progress = _Progress()
    ctx.gc.set_progress_tracker = progress
    import server.set_progress_sync as sync

    class NotifyThenLoseAck:
        calls = 0

        def notify(self):
            self.calls += 1
            raise OSError("synthetic notification acknowledgement lost")

    worker = NotifyThenLoseAck()
    monkeypatch.setattr(sync, "getSetProgressSyncWorker", lambda: worker)
    settle_time(ctx)
    assert ctx.sending.step() is None
    assert progress.calls == worker.calls == ctx.events.qsize() == 1
    row = followup(ctx)
    assert row["state"] == "PUBLICATION_ATTEMPTED"
    assert row["failure_kind"] == "SIDE_EFFECT_FAILED"
    with connect(ctx.path) as conn:
        hold_id = conn.execute("SELECT id FROM smart_bin_audit_events WHERE request_key=?",
                               (f"{handoff.request_key}:side-effect-failed",)).fetchone()[0]
    req, owner = request_for(ctx, handoff)
    owner = replace(owner, gate_open=True)
    effects = tuple(claim(req, effect) for effect in recon.PRODUCER_EFFECTS)
    gate = replace(req, effects=effects,
                   holds=(recon.HoldEvidence(hold_id, "GATE_OPEN", reference("gate")),))
    gate_view = recon.preview(gate, owner)
    assert "HOLD:" + hold_id not in [item["id"] for item in gate_view["resolvable"]]
    callbacks = replace(req, effects=effects, holds=(recon.HoldEvidence(
        hold_id, "CALLBACKS_APPLIED", reference("callbacks")),))
    callback_view = recon.preview(callbacks, owner)
    assert "HOLD:" + hold_id in [item["id"] for item in callback_view["resolvable"]]
    assert "CLOSURE_PENDING" in [item["id"] for item in callback_view["remaining"]]


def test_wrong_identity_stale_preview_and_untrusted_actor_refuse(completed):
    ctx, handoff = completed
    ctx.adapter.begin_publication(handoff)
    req, owner = request_for(ctx, handoff)
    assert "FOLLOWUP_NOT_FOUND" in recon.preview(replace(req, machine_id="wrong"), owner)["blockers"]
    assert "FOLLOWUP_IDENTITY_MISMATCH" in recon.preview(
        replace(req, delivery_id="wrong"), owner)["blockers"]
    assert "FOLLOWUP_IDENTITY_MISMATCH" in recon.preview(
        replace(req, original_marker_evidence_ref="wrong"), owner)["blockers"]
    with pytest.raises(ValueError):
        recon.preview(replace(req, actor=""), owner)
    assert "CURRENT_OWNER_QUIESCENCE_REQUIRED" in recon.preview(
        req, replace(owner, evidence=EvidenceReference("x", "unknown")))["blockers"]
    view = recon.preview(req, owner)
    assert recon.apply(req, owner, expected_preview_hash="stale")["code"] == "REFUSED"
    assert recon.apply(replace(req, expected_followup_revision=999), owner,
                       expected_preview_hash=view["preview_hash"])["code"] == "REFUSED"


def test_missing_history_defers(completed):
    ctx, handoff = completed
    req, owner = request_for(ctx, handoff)
    with connect(ctx.path) as conn:
        conn.execute("DELETE FROM piece_records WHERE uuid=?", (ctx.piece.uuid,))
    assert "COMPLETED_MATCHING_DELIVERY_HISTORY_REQUIRED" in recon.preview(req, owner)["blockers"]
    with connect(ctx.path) as conn:
        # The history test is isolated; the external case uses a fresh fixture below.
        assert conn.execute("SELECT count(*) FROM piece_records WHERE uuid=?",
                            (ctx.piece.uuid,)).fetchone()[0] == 0


def test_pending_delivery_defers(context):
    ctx = context
    recon.initialize_schema()
    ctx.discharge()
    handoff = ctx.adapter.require_handoff(ctx.piece)
    # RETAINED has no delivery, so a manufactured request still defers.
    row = followup(ctx)
    with connect(ctx.path) as conn:
        rev = conn.execute("SELECT state_revision FROM smart_bin_machines WHERE machine_id='m'").fetchone()[0]
    req = recon.ReconciliationRequest("m", row["id"], row["reservation_id"],
                                      "missing-delivery", "pending", rev, row["row_revision"],
                                      "actor", "reason", row["release_attempt_id"],
                                      row["marker_evidence_ref"], "missing-completion", ())
    now = time.time()
    owner = recon.OwnerSnapshot("m", row["id"], row["reservation_id"], "fresh-owner", "r1",
                                now, now + 4, True, True, True, True, False, reference("owner"))
    assert "COMPLETED_MATCHING_DELIVERY_HISTORY_REQUIRED" in recon.preview(req, owner)["blockers"]


def test_external_workflow_defers(completed):
    ctx, handoff = completed
    req, owner = request_for(ctx, handoff)
    with connect(ctx.path) as conn:
        conn.execute("INSERT INTO smart_bin_external_operations VALUES("
                     "'external','m',?,'harvest','quota','external-key','hash',NULL,"
                     "'pending',NULL,1,1)", (req.reservation_id,))
    assert "HARVEST_OR_EXTERNAL_OPERATION_DEFERRED" in recon.preview(req, owner)["blockers"]


def test_exact_replay_changed_payload_and_stale_competitor(completed):
    ctx, handoff = completed
    ctx.adapter.begin_publication(handoff)
    req, owner = request_for(ctx, handoff)
    result = apply_reviewed(req, owner)
    assert recon.apply(req, owner, expected_preview_hash=result["preview_hash"]) == result
    assert recon.apply(replace(req, reason="changed"), owner,
                       expected_preview_hash=result["preview_hash"])["code"] == "IDEMPOTENCY_CONFLICT"
    competing = replace(req, request_key="competing")
    view = recon.preview(competing, owner)
    assert "STALE_REVISION" in view["blockers"]


def test_new_discrepancy_invalidates_preview_without_revision_change(completed):
    ctx, handoff = completed
    ctx.adapter.begin_publication(handoff)
    req, owner = request_for(ctx, handoff)
    view = recon.preview(req, owner)
    with connect(ctx.path) as conn:
        conn.execute("INSERT INTO smart_bin_discrepancies "
                     "(id,machine_id,reservation_id,kind,status,details_json,created_at) "
                     "VALUES('new-discrepancy','m',?,'OWNER_CONFLICT','open','{}',1)",
                     (req.reservation_id,))
    result = recon.apply(req, owner, expected_preview_hash=view["preview_hash"])
    assert result["code"] == "REFUSED" and "STALE_PREVIEW" in result["blockers"]
    assert "DISCREPANCY:new-discrepancy" in [item["id"] for item in result["remaining"]]


def test_predecessor_hold_remains_after_successor_disposition(completed):
    ctx, first_handoff = completed
    settle_time(ctx)
    ctx.sending.step()
    _, first_handoff = second_native_piece(ctx)
    second_handoff = ctx.adapter.require_handoff(ctx.piece)
    ctx.adapter.complete(ctx.piece, runtime_run_id=ctx.gc.run_recorder.run_id,
                         completed_at=time.time() + 5)
    late_hold(ctx, first_handoff, kind="OWNERSHIP_LOST", key="older-owner-hold")
    req, owner = request_for(ctx, second_handoff, key="successor-disposition")
    assert apply_reviewed(req, owner)["code"] == "OK"
    report = delivery.inspect_recovery("m")
    assert len(report["followup_blockers"]) == 2
    older = next(item for item in report["followup_blockers"]
                 if item["followup_id"] == first_handoff.followup_id)
    assert any(item["kind"] == "OWNERSHIP_LOST" for item in older["remaining_obligations"])
    assert ctx.adapter.blocks_admission()


def test_conflicting_later_effect_claim_does_not_erase_original(completed):
    ctx, handoff = completed
    ctx.adapter.begin_publication(handoff)
    req, owner = request_for(ctx, handoff)
    req = replace(req, effects=(claim(req, "EVENT_ENQUEUED", "NOT_APPLIED"),))
    assert apply_reviewed(req, owner)["code"] == "OK"
    later, fresh_owner = request_for(ctx, handoff, key="later-contradiction")
    later = replace(later, effects=(claim(later, "EVENT_ENQUEUED", "APPLIED"),))
    view = recon.preview(later, fresh_owner)
    assert "CONFLICTING_EFFECT_EVIDENCE" in view["blockers"]
    assert recon.apply(later, fresh_owner, expected_preview_hash=view["preview_hash"])["code"] == "REFUSED"
    report = delivery.inspect_recovery("m")["followups"][0]
    assert report["effect_inventory"]["EVENT_ENQUEUED"]["status"] == "NOT_APPLIED"
    assert any(item["id"] == "PUBLICATION" for item in report["remaining_obligations"])


def test_transaction_failure_rolls_back_every_record(completed, monkeypatch):
    ctx, handoff = completed
    ctx.adapter.begin_publication(handoff)
    req, owner = request_for(ctx, handoff)
    req = replace(req, effects=tuple(claim(req, effect) for effect in recon.PRODUCER_EFFECTS))
    req = propose_resolution(req, owner, "PUBLICATION")
    view = recon.preview(req, owner)
    with connect(ctx.path) as conn:
        before = preserved(conn, req)
        revs = tuple(conn.execute("SELECT state_revision FROM smart_bin_machines WHERE machine_id='m'").fetchone())
    # Force failure at the final receipt insert, after audit, disposition and revisions.
    import smart_bins_storage
    original = smart_bins_storage.critical_transaction
    class FailingConnection:
        def __init__(self, conn): self.conn = conn
        def __getattr__(self, key): return getattr(self.conn, key)
        def execute(self, sql, params=()):
            if sql.startswith("INSERT INTO smart_bin_followup_reconciliation_receipts"):
                raise OSError("synthetic receipt write failure")
            return self.conn.execute(sql, params)
    from contextlib import contextmanager
    @contextmanager
    def failed_transaction():
        with original() as conn:
            yield FailingConnection(conn)
    monkeypatch.setattr(recon, "critical_transaction", failed_transaction)
    with pytest.raises(OSError, match="synthetic receipt"):
        recon.apply(req, owner, expected_preview_hash=view["preview_hash"])
    with connect(ctx.path) as conn:
        assert preserved(conn, req) == before
        assert tuple(conn.execute("SELECT state_revision FROM smart_bin_machines WHERE machine_id='m'").fetchone()) == revs
        for table in ("smart_bin_followup_reconciliation_dispositions",
                      "smart_bin_followup_reconciliation_links",
                      "smart_bin_followup_reconciliation_receipts"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM smart_bin_audit_events WHERE request_key=?",
                            (req.request_key,)).fetchone()[0] == 0
