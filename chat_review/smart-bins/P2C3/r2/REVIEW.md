# P2C3 r2 — guarded completion and admission correction

**Implementation uncommitted; review pending. The guarded path remains inactive in production.** This correction follows [P2C3 r1](../r1/REVIEW.md) at `91e8b972f1d6814e439d3d7ca3ac587e38c27363`; the accepted implementation predecessor remains [P2C2 r2](../../P2C2/r2/REVIEW.md).

## Corrections

- `PhysicalDistribution` exposes the retained completion guard to `Sending`. With no Sending adapter, a guarded drop holds even if its native tags were cleared or the drop slot is missing. Incomplete or foreign tags also hold. It cannot enter Harvest confirmation, legacy recording, distributed publication or gate reopening. Ordinary unguarded Sending still completes.
- A guarded handoff retains its staged transport instance. The same Sending instance compares the current transport and exact drop object before completion, after durable completion, before publication, and before gate reopening. Missing, replaced or foreign ownership creates a sticky hold; restaging the same object in software cannot clear it. A completion that committed before ownership loss keeps its verified receipt and history; repeated ticks do not create another delivery, event, recorder entry or progress increment.
- The physical bridge retains the reservation and attempt after its index clears. `NativeCompletionAdapter` binds explicitly to that bridge. `PhysicalC4Runtime.can_admit` and `reserve` now see the bridge's completion hold even while a queued pause is unprocessed or unavailable. Missing coupling fails closed. Only the matching handoff's verified receipt, completed publication and successful gate release remove this hold. Existing pause, incident, ownership and recovery blockers remain in effect; an ambiguous incrementing callback stays held.

No schema, production startup injection, motor permit, Harvest routing or runtime activation changed.

## Validation

Commands ran from `software/sorter/backend` with Python 3.12.12, frozen dependencies, temporary SQLite/configuration paths before imports, simulated hardware, controlled clocks and fixture cleanup.

- Before correction: `uv run --frozen python -m pytest tests/test_smart_bins_sending.py -q` — **9 failed, 18 passed**. The failures reproduced legacy fallback, stale cached-drop completion and open admission during unresolved native completion.
- Intermediate targeted runs of the same command passed **27**, **29**, then **30** cases as regressions were added. A source review found the cleared-tag, missing-drop and software-restaging gaps. `uv run --frozen python -m pytest tests/test_smart_bins_sending.py::test_missing_sending_adapter_holds_guarded_drop tests/test_smart_bins_sending.py::test_missing_adapter_and_drop_does_not_reopen_guarded_gate tests/test_smart_bins_sending.py::test_guarded_drop_loss_cannot_be_repaired_by_restaging_object -q` then showed **3 failed, 3 passed** before the added correction.
- Final targeted: `uv run --frozen python -m pytest tests/test_smart_bins_sending.py -q` — **33 passed**. The suite covers ordinary unguarded completion, exact current-drop ownership on the same Sending instance, loss during and after commit, sticky ownership hold, missing guard coupling, failed write, lost acknowledgement, blocked new admission, same-key recovery and sticky callback failure.
- Affected group: `uv run --frozen python -m pytest tests/test_smart_bins_sending.py tests/test_distribution_sending.py tests/test_distribution_rehome.py tests/test_runtime_stats.py tests/test_smart_bins_physical_bridge.py tests/test_smart_bins_delivery.py tests/test_physical_c4_runtime.py -q` — **174 passed**. Its earlier 171-pass run preceded the last three regressions and was superseded by this run.

`P2C3.patch` is the complete P2C3 slice from the accepted P2C2 r2 result tree after the recorded P1–P2C2 chain. `P2C3-r1-to-r2.patch` is only the five-file correction from exact r1. Both reconstruct the same submitted tree. Earlier packages remain intact.

## Remaining limits

This is isolated software simulation, not production injection, deployment or physical qualification. Restart reconstruction, operator reconciliation, Harvest's separate-store bridge and legacy-writer fencing remain later work. A witnessed ownership loss and an ambiguous after-commit callback require explicit reconciliation; neither is repaired by software restaging or callback replay. The asynchronous event consumer has no new durable notification outbox. A committed software delivery does not prove sensor-confirmed entry into a bin.
