# P2C4 r1 — durable native completion follow-up

**Implementation uncommitted; review pending. The guarded path remains inactive in production.** This slice follows accepted [P2C3 r3](../../P2C3/r3/REVIEW.md) at `dc5104db0b300ef91179d296bf78fb14a6b1ebc9`. `P2C4.patch` contains only this slice from the P2C3 r3 result tree; the earlier uncommitted slices remain in the predecessor chain recorded in `manifest.json`.

## Behavior and boundaries

- An explicitly initialized completion extension (schema version 1, alongside the unchanged ledger v2) stores the original machine, owner, reservation, release attempt, custody, route, marker reference and frozen completion request key. The adapter retains that identity and closes the local gate before the pre-transport write. A failed or ambiguous write refuses transport advancement; an explicit retry uses the same identity. Missing, partial and incompatible extension schemas are rejected without conversion.
- Adapter-managed `complete_native` links the follow-up to the delivery and serialized completion evidence in the **same SQLite transaction** as the reservation, delivery and history commit. Its original receipt and payload remain stable; replay cannot reset an attempted, held or closed follow-up. Standalone completions remain distinct and appear as restart blockers without tracked follow-up evidence.
- Sending commits a publication-attempt record before callbacks. A failed or lost callback acknowledgement leaves an ambiguous attempted state and is not automatically replayed. After known callback success, the surviving process can retry only the publication bookkeeping with the same key. Durable publication success remains an open obligation until matching delivery, ownership and gate checks permit a separate durable closure. The bridge releases its in-memory hold only after that closure.
- Ownership loss latches and fences gate/admission before the SQLite hold write. The first reason and key stay frozen across ticks; an unavailable write leaves the earlier open record as a restart blocker. A stale or late loss observation is retained as a blocker even after software closure. State changes use action-qualified receipts, revisions, audit events and the existing machine revision.
- `inspect_recovery` reads claims, discrepancies and all completion follow-ups in one read transaction, including completed reservations. It checks referenced exit/completion evidence, delivery and matching piece history; open, contradictory, missing or standalone-completed follow-ups block fresh owner attachment. Closed rows remain visible and add no blocker when their evidence is intact. Inspection does not write, call callbacks or reconstruct custody or motion authority.

The delivery row is durable software accounting. Publication status is a separate software side effect. Closure acknowledges that follow-up and local admission checks; it does **not** prove physical halt or bin entry, and no persisted gate-open flag is restored on restart.

## Validation

Commands ran from `software/sorter/backend` with Python 3.12.12, frozen dependencies, temporary SQLite/configuration paths before imports, simulated C4 transport, controlled clocks and fixture cleanup.

- `uv run --frozen python -m pytest tests/test_smart_bins_completion_recovery.py -q` — **19 passed** on the final targeted run. Cases cover each restart boundary, fresh-owner blocking, closed and independently blocked owners, rollback, lost write acknowledgement, callback ambiguity, no duplicate credit, ownership-loss write failure, stale/late observations, legacy standalone completion and incompatible schema.
- `uv run --frozen python -m pytest tests/test_smart_bins_completion_recovery.py tests/test_smart_bins_sending.py tests/test_smart_bins_delivery.py tests/test_smart_bins_physical_bridge.py tests/test_smart_bins_reservations.py tests/test_smart_bins_storage.py tests/test_smart_bins_migration.py -q` — **214 passed** on the final affected run.

The first targeted run failed **9/12** because the accepted service receipt table restricts action names; a separate versioned follow-up receipt table fixed that schema conflict without changing service v2. An interim blocker-precedence failure was corrected so the bridge reports the completion hold, and an interim bookkeeping retry failure was corrected by clearing only the matching native incident after successful retry. A later test expected the wrong exception class for a correctly rejected stale close; its assertion was corrected. The final commands above passed after those fixes.

`P2C4.patch` reconstructs the recorded result tree from the exact P2C3 r3 result tree, including both new files. No implementation commit, production initialization, deployment, hardware operation or operational data access occurred.

## Remaining limits

Production construction and activation are still absent. No automatic callback replay, operator reconciliation, restart custody reconstruction, Harvest bridge, recovery motion or physical qualification is included. A late contradiction first witnessed after durable closure can only survive process loss if its follow-up write succeeds; a failed write is held in the current process but cannot create evidence in an unavailable database. A committed delivery or closed software follow-up is not sensor-confirmed bin entry.
