# P2C2 r2 — fault-stop ordering and empty-index ownership

**Implementation uncommitted; review pending. Guarded mode remains inactive.**

This corrects [P2C2 r1](../r1/REVIEW.md), published at `8c96ecd420baec9cf305edc4215ac0a9bc9cbc16`. Current source matched all five r1 manifest hashes before editing. The correction changes only `software/sorter/backend/subsystems/classification_channel/marker_positioner.py`, `smart_bins_physical_bridge.py` in that directory, and `software/sorter/backend/tests/test_smart_bins_physical_bridge.py`.

## Corrections

**Fault callback before uncertainty persistence.** The `motor.start` exception handler now retains the original exception in memory and returns it to the existing failure path. `_fail` latches the positioner fault and calls memory-only bridge hooks to fence the index and freeze uncertainty evidence. Guard-bookkeeping exceptions are retained separately. The existing `on_fault` callback runs in a `finally` block, before `persist_uncertainty` or diagnostic reporting. Both unknown motor acceptance and accepted motion followed by marker failure use this ordering. There is no additional stop mechanism, worker or recovery loop.

The bridge retains the target, binding, custody, attempt, original exception, request key and immutable uncertainty payload. A write failure or lost acknowledgement leaves that payload available for an explicit `persist_uncertainty()` retry under the owner locks. The retry uses the original key/revision/evidence; a committed receipt is replayed without another discrepancy. Persistence errors do not replace the original fault or restore dispatch permission. If a memory-bookkeeping hook itself fails, the positioner retains the original fault and hook error, keeps the motor path latched, and preserves the outstanding index/custody. It does not invent missing evidence.

**Frozen owner identity for every index.** Each prepared index now stores its own `owner_incarnation`, including an EMPTY exit pocket with no reservation. Preparation, retry, request and live dispatch validate that identity. A change blocks the original permit even when motor ownership and FIFO evidence still match; restoring the old identity does not rearm the blocked permit. Valid same-owner empty indexes still complete without creating piece IDs, reservations or release attempts.

Intent-before-request, retained marker confirmation, durable exit-before-FIFO-removal, handoff protection, native owner checks, pause behavior and bounded trims remain covered. Runtime glue, delivery/storage schemas and production construction did not change in this correction.

## Tests

All commands ran from `software/sorter/backend` using Python **3.12.12** and the frozen environment.

- Against r1: `uv run --frozen python -m pytest tests/test_smart_bins_physical_bridge.py -q -k r2_ --tb=short` → **12 failed, 1 passed, 20 deselected**. Six cases observed persistence before stop, two exposed a suppressed stop callback after bookkeeping failure, and four accepted a replacement owner. The valid empty-index control passed.
- After correction: the same targeted command → **13 passed, 20 deselected**.
- Final affected group: `uv run --frozen python -m pytest tests/test_smart_bins_physical_bridge.py tests/test_physical_c4_runtime.py tests/test_marker_positioner.py tests/test_smart_bins_delivery.py -q` → **173 passed**.

An initial test draft reported 13 failures: its positive control asserted the new internal owner field, and its injected failure hook omitted an existing keyword argument. Those test setup details were corrected before the r1 reproduction above. Behavioral assertions were retained.

Tests use the real runtime/positioner/bridge and temporary SQLite, instrumenting the fault callback and persistence boundary. Import isolation and connection/keeper cleanup reuse the existing fixtures. No sleeps, hardware, operational stores, full-backend run or frontend build were used.

## Package and remaining limits

`P2C2.patch` is the complete P2C2 slice from accepted P2C1 r2 and its recorded predecessor chain. `P2C2-r1-to-r2.patch` is only this correction from the exact submitted r1 tree. Both chains were reconstructed and their five resulting source/test files matched the current working bytes. Earlier packages and unchanged contracts remain in place.

This is simulated source qualification. Callback ordering does not prove a physical halt or bin landing. Production startup wiring, Sending completion, recovery motion/reconciliation, Harvest, legacy-writer fencing and activation remain later work. Uncertainty persistence retries are explicit bookkeeping calls; faults never trigger automatic redispatch.
