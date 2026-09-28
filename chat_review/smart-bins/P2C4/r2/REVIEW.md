# P2C4 r2 — guarded completion succession and current durable holds

**Implementation uncommitted; review pending. The guarded native path remains inactive in production.** This corrects [P2C4 r1](../r1/REVIEW.md), submitted at `425e508886c2551ff238da2a30ace123146e5aac`. The accepted implementation predecessor remains [P2C3 r3](../../P2C3/r3/REVIEW.md). `P2C4.patch` is the complete P2C4 slice from that predecessor; `P2C4-r1-to-r2.patch` is only this correction from the exact r1 result tree. All previous packages remain intact.

## Corrections

- The same adapter can retain a later real C4 handoff after the prior piece has published, closed durably and released admission. It checks the current follow-up, its exit/completion/delivery/history evidence and the same staged transport under the existing owner lock before replacing the in-memory handoff. Pending, failed, lost, missing or contradicted predecessors remain held. Previous delivery, history, audit and receipts stay in SQLite. A replayed retention receipt retains its historical revision and cannot rewind a progressed handoff.
- Follow-up transition receipts remain exact idempotent acknowledgements, including replay before a stale-state check. They no longer serve as current permission. The adapter and recovery inspector share one evidence assessment. Before publication callbacks, final gate opening, admission release and successor retention, the adapter reads the current durable row. New loss or failure evidence, missing evidence and read failure block progress even when closure or publication flags are cached. The bridge rechecks durable eligibility for owner admission. An observed existing hold does not create a second side-effect failure record.
- Sending closes and verifies the follow-up before opening its local gate, then admission release checks current eligibility again. Genuine callback or gate failures still follow the existing stop, hold and pause path. No callback or physical operation is replayed merely to settle bookkeeping.

These records establish software delivery and publication state; they do not prove physical bin entry. No schema version, motor authorization, restart motion or production activation changed.

## Validation

Commands ran from `software/sorter/backend` using the frozen Python 3.12 environment, temporary SQLite/configuration paths set before imports, simulated physical transport, controlled clocks and fixture cleanup.

- Before the correction, `uv run --frozen python -m pytest tests/test_smart_bins_completion_recovery.py -q -k 'closed_handoffs_succeed or unresolved_predecessor_rejects or late_hold_after_closed_fences or lost_attempt_ack_replays or old_retention_receipt or followup_eligibility_read_failure'` returned **8 failed, 1 passed, 19 deselected**. Five failures reproduced the r1 source findings (successor rejection, cached late-hold admission, callback after held attempt-receipt replay, retention revision rewind and read-failure admission). Three negative-control failures were caused by a test copy inheriting the original transport reference; the test fixture was corrected before assessing that control.
- The first full targeted run after the source fix returned **27 passed, 1 failed** because the existing stale-close test expected its prior error wording. The `CurrentFollowupHeld` error now preserves that wording. Final `uv run --frozen python -m pytest tests/test_smart_bins_completion_recovery.py -q` — **29 passed**.
- Final `uv run --frozen python -m pytest tests/test_smart_bins_completion_recovery.py tests/test_smart_bins_sending.py tests/test_smart_bins_delivery.py tests/test_smart_bins_physical_bridge.py tests/test_smart_bins_reservations.py tests/test_smart_bins_storage.py tests/test_smart_bins_migration.py -q` — **224 passed**. This affected group also passed once before the final small identity/lint cleanup; the stated result is the rerun against the packaged bytes.

Regressions exercise two consecutive pieces on one runtime, adapter and transport; blocked succession; late ownership and side-effect holds after closure; lost attempt acknowledgement with and without a hold; original receipt replay; missing/read-failed evidence; and single delivery, history, callback, progress, audit and closure effects. Both patches were applied to their recorded base trees in an isolated Git index and reconstructed the same r2 result tree.

## Remaining limits and cleanup

The path is still explicitly initialized and inactive in production. Callback failure or lost callback acknowledgement remains ambiguous for operator reconciliation; there is no automatic callback replay, fresh-owner custody reconstruction, Harvest bridge, recovery motion, deployment or physical qualification. Current authorization reads are point-in-time; this slice does not make an external concurrent hold and a callback one database transaction. Owner admission rechecks the current durable row.

Three earlier temporary Git index files remain outside the implementation checkout because their deletion was blocked; cleanup is unresolved and was not retried:

- `C:/Users/abria/AppData/Local/Temp/sorter-p2c4-package.index`
- `C:/Users/abria/AppData/Local/Temp/sorter-p2c4-verify.index`
- `C:/Users/abria/AppData/Local/Temp/sorter-p2c4-chain-verify.index`
