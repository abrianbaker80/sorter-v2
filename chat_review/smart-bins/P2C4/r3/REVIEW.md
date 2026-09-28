# P2C4 r3 — closure retry and replaced-handoff blockers

**Implementation uncommitted; review pending. The guarded native path remains inactive in production.** This corrects [P2C4 r2](../r2/REVIEW.md), submitted at `99524b6cfb4a48d46638b65403ad2128a432b1b2`. The accepted implementation predecessor remains [P2C3 r3](../../P2C3/r3/REVIEW.md). `P2C4.patch` reconstructs the complete P2C4 slice from that predecessor; `P2C4-r2-to-r3.patch` contains only this correction from the exact r2 result tree. Earlier packages are unchanged.

## Corrections

- Sending now treats CLOSE write/readback separately from the external gate operation. A failure before CLOSE commits, or a lost acknowledgement after it commits, fences gate and admission without inventing `SIDE_EFFECT_FAILED`. The surviving process retries the same frozen CLOSE key, revision and delivery reference. A committed but unacknowledged CLOSE is read back through its exact receipt. Callbacks, delivery, history and audit are not repeated. A genuine gate-open failure still records a durable side-effect hold after the local gate is fenced.
- Current authorization now uses the recovery inspector's evidence predicates for the current follow-up **and every other outstanding obligation on the same machine in one SQLite read snapshot**. The current handoff may be intentionally open in its expected lifecycle state; another open, lost, failed, missing-evidence or unlinked obligation blocks admission, gate release, callbacks and successor retention. Other machines are outside that assessment. A predecessor hold before a new publication attempt leaves the new delivery pending; a replayable receipt for the current attempt can still be returned, but its historical acknowledgement does not authorize callbacks when a current hold exists.

These checks are point-in-time under the existing owner/lock and revision boundaries. SQLite transactions are not held across callbacks or gate operations. No schema, motion authority, runtime injection or production activation changed.

## Validation

Commands ran from `software/sorter/backend` using the frozen Python 3.12 environment, temporary SQLite/configuration paths before imports, the real guarded runtime/distribution/Sending fixtures, controlled clocks and reliable fixture cleanup.

- Against submitted r2, `uv run --frozen python -m pytest tests/test_smart_bins_completion_recovery.py -q -k 'close_write_failure_retries or real_gate_failure_retains or replaced_predecessor_late_hold or predecessor_hold_before_second_publication or other_machine_followup_hold or shared_authorization_read_failure'` returned **6 failed, 2 passed, 29 deselected**. The failures reproduced closure failure misclassification, replaced-predecessor admission and callback gaps, and the missing shared-assessment read fence. The genuine gate-failure and unrelated-machine controls passed.
- After the initial correction, the full targeted module returned **36 passed, 1 failed**: a prior test showed that checking the current hold before a lost attempt receipt replay suppressed the historical acknowledgement. The precheck now considers predecessor blockers; the post-transition check enforces current callback authorization. Final `uv run --frozen python -m pytest tests/test_smart_bins_completion_recovery.py -q` — **37 passed**.
- Final `uv run --frozen python -m pytest tests/test_smart_bins_completion_recovery.py tests/test_smart_bins_sending.py tests/test_smart_bins_delivery.py tests/test_smart_bins_physical_bridge.py tests/test_smart_bins_reservations.py tests/test_smart_bins_storage.py tests/test_smart_bins_migration.py -q` — **232 passed**.

Regressions cover CLOSE failure before commit and lost acknowledgement after commit, exact same-key retry without duplicate effects, genuine gate failure, both kinds of late hold on the first of two completed pieces, a predecessor hold before the second piece's callbacks, independent-machine scope and read failure. Both packaged patches were applied to their recorded base trees in an isolated Git index and reconstructed the same r3 result tree.

## Remaining boundaries and cleanup

The path is still explicitly initialized and inactive in production. Callback failure or lost callback acknowledgement remains ambiguous for operator reconciliation; there is no automatic callback replay, fresh-owner custody reconstruction, Harvest bridge, recovery motion, deployment or physical qualification. A hold first witnessed while SQLite is unavailable cannot become durable until its original write succeeds. Software closure is not sensor-confirmed bin entry.

Three earlier temporary Git index files remain outside the implementation checkout because their deletion was blocked; cleanup is unresolved and was not retried:

- `C:/Users/abria/AppData/Local/Temp/sorter-p2c4-package.index`
- `C:/Users/abria/AppData/Local/Temp/sorter-p2c4-verify.index`
- `C:/Users/abria/AppData/Local/Temp/sorter-p2c4-chain-verify.index`
