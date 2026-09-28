# P2C3 r3 — first-observation ownership hold

**Implementation uncommitted; review pending. The guarded path remains inactive in production.** This correction follows [P2C3 r2](../r2/REVIEW.md) at `360bb64cb5dfabb6258c809979f0d2d4b40e0de1`. The accepted implementation predecessor remains [P2C2 r2](../../P2C2/r2/REVIEW.md).

## Correction

The retained `NativeCompletionAdapter` now compares the current staged transport and exact drop object with its original handoff before `Sending` caches any piece. A missing or replaced transport, missing or wrong drop, or failed ownership read sets `ownership_lost` on that handoff before an early return or foreign-object identity check. The same adapter check is reused at the existing completion and gate boundaries.

A missing Sending adapter uses the shared retained guard for this check. Missing injection by itself holds without inventing ownership loss when the original transport and drop still match. A witnessed loss remains held after software restaging, a new Sending instance, or restored injection. Original custody, attempt and any committed receipt stay available; the hold does not create a delivery, cancellation, retry or reconciliation. The bridge admission gate and distribution gate remain closed.

Before any handoff, waiting for a drop does not mark ownership lost. Once a guarded handoff has been fully published and released, a later ordinary drop can still use unguarded Sending. Matching guarded completion and same-key persistence retries retain their r2 behavior. No motor authorization, fault-stop ordering, schema, startup injection or production routing changed.

## Validation

Commands ran from `software/sorter/backend` with Python 3.12.12, frozen dependencies, temporary SQLite/configuration paths before imports, simulated C4 transport, controlled clocks and fixture cleanup.

- Against r2: `uv run --frozen python -m pytest tests/test_smart_bins_sending.py::test_first_observed_missing_drop_latches_original_handoff tests/test_smart_bins_sending.py::test_first_observed_wrong_drop_survives_fresh_sending tests/test_smart_bins_sending.py::test_missing_adapter_latches_missing_drop_across_restoration tests/test_smart_bins_sending.py::test_waiting_without_native_handoff_does_not_latch_loss -q` — **3 failed, 1 passed**. The missing and wrong first-observed drops did not latch.
- Expanded r2 demonstration: `uv run --frozen python -m pytest tests/test_smart_bins_sending.py::test_first_observed_missing_drop_latches_original_handoff tests/test_smart_bins_sending.py::test_first_ownership_read_failure_latches_handoff tests/test_smart_bins_sending.py::test_first_observed_wrong_drop_survives_fresh_sending tests/test_smart_bins_sending.py::test_missing_adapter_latches_missing_drop_across_restoration tests/test_smart_bins_sending.py::test_waiting_without_native_handoff_does_not_latch_loss -q` — **5 failed, 2 passed**. The added read failure also escaped Sending. After the shared check, this selection passed **7** cases.
- A positive control caught an interim released-guard regression: `uv run --frozen python -m pytest tests/test_smart_bins_sending.py::test_released_native_guard_does_not_claim_later_ordinary_drop -q` — **1 failed** before limiting the no-adapter shared check to unresolved handoffs.
- Final targeted: `uv run --frozen python -m pytest tests/test_smart_bins_sending.py -q` — **41 passed**. An intermediate run before the last positive control passed 40.
- Final affected group: `uv run --frozen python -m pytest tests/test_smart_bins_sending.py tests/test_distribution_sending.py tests/test_distribution_rehome.py tests/test_runtime_stats.py tests/test_smart_bins_physical_bridge.py tests/test_smart_bins_delivery.py tests/test_physical_c4_runtime.py -q` — **182 passed**.

`P2C3.patch` contains the complete P2C3 slice from the accepted P2C2 r2 result tree after the P1–P2C2 chain. `P2C3-r2-to-r3.patch` contains only this three-file correction from exact r2. Both reconstruct the same submitted tree; earlier packages remain intact.

## Remaining limits

This is software simulation, not production injection, deployment or physical qualification. A witnessed ownership loss, restart reconstruction and ambiguous after-commit callbacks still require explicit reconciliation in later work. The asynchronous event consumer has no new durable outbox. A committed software delivery does not prove sensor-confirmed bin entry.
