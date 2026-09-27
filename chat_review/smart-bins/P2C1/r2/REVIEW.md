# P2C1 r2 — journey contradiction and destination-reference correction

**Review pending. Implementation remains uncommitted and inactive.** `P2C1.patch` is the complete P2C1 slice from accepted P2B r2; `P2C1-r1-to-r2.patch` changes only the submitted P2C1 r1 source. Both patches include `software/sorter/backend/smart_bins_delivery.py`, `smart_bins_service.py`, and `tests/test_smart_bins_delivery.py`. P2C1 r1 and every predecessor package remain intact.

## Findings addressed

The shared reservation service now checks unresolved discrepancies across every reservation for the **same machine and piece** on the active transaction connection. Preview and new reserve return `UNRESOLVED_DELIVERY` before candidate selection, so a cancelled reservation's terminal contradiction cannot be bypassed by offering another cycle. `prepare_release` uses the same check on the current reservation's piece; an already-reserved successor cannot start a new release after its predecessor receives contradictory evidence. The check does not confuse the successor's own held capacity with a new admission, block unrelated pieces on unaffected cycles, alter terminal outcomes, or discard evidence. Exact committed reserve, cancellation, contradiction and intent receipts still replay before new checks; an intent receipt still grants no dispatch permission.

`complete_native` now validates the supplied actual BIN slot and cycle as existing references on the same machine **before** linking either to a discrepancy or delivery. Invalid, nonexistent and cross-machine references return `UNQUALIFIED_DESTINATION` without a false association, state change, credit, audit or receipt. The same reference check is shared with terminal contradiction recording. A valid same-machine different destination still creates a discrepancy and retains an `UNCERTAIN` hold without crediting either cycle. Historical cycles need not remain attached or open to preserve truthful evidence.

No schema, service version, migration, history helper, runtime hook, Harvest bridge or physical-owner path changed.

## Validation

- Before correction: `uv run --frozen python -m pytest tests/test_smart_bins_delivery.py -q -k r2_` → **3 failed, 1 passed, 19 deselected**. The failures reproduced alternative-cycle retry, successor release, and invalid destination association.
- After correction: the same targeted command → **4 passed, 19 deselected**.
- Final affected group: `uv run --frozen python -m pytest tests/test_smart_bins_delivery.py tests/test_smart_bins_reservations.py tests/test_smart_bins_storage.py tests/test_smart_bins_migration.py -q` → **121 passed**.

The Python 3.12 frozen tests use temporary database/configuration paths before imports and real, closed SQLite connections. They assert unchanged claims, contents, revisions, attempts, evidence and receipts on refusal; an undisputed linked retry and unrelated admission still work. Earlier history, local-state and P1 evidence was reused because those paths and fixtures did not change. No full-backend, frontend or physical test ran.

## Remaining boundary

This is source qualification only. Physical-owner wiring, single-use dispatch permits, recovery motion, operator reconciliation, Harvest's separate-store bridge, legacy-writer fencing, deployment and sensor-confirmed bin entry remain pending. Review publication does not accept or commit the implementation or authorize activation.
