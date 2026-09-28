# P2C2 r1 — guarded native C4 dispatch and durable exit

**Review pending. Implementation remains uncommitted and inactive.** `P2C2.patch` is the slice-only delta from accepted [P2C1 r2](../../P2C1/r2/REVIEW.md) at commit `ea92beb833f15f500a9b3bcb7cb8343ee8aeb555`. It changes `smart_bins_delivery.py`, `marker_positioner.py`, `physical_runtime.py`, and adds `smart_bins_physical_bridge.py` plus `test_smart_bins_physical_bridge.py`. The accepted P1/P2A1/P2A2/P2B chain is recorded in the manifest.

## Guarded behavior

The bridge is explicitly injected into `PhysicalC4Runtime`; no production controller constructs it. Its owner interface supplies a unique `owner_incarnation`, an outer-lock assertion, existing reservation identity, native qualification, release evidence, current route qualification, and empty-route readiness. Guarded admission validates the piece/episode/pocket generation against a real `RESERVED` claim. Startup inspects outstanding claims and discrepancies; an unresolved, foreign-owned, or non-reserved claim blocks admission. A nonempty FIFO cannot be attached as fresh custody.

After the existing planner prepares a target, the bridge retains that target and exiting binding. A native exit commits `prepare_release` before runtime pending state or `request_index`; an exception retains the frozen request, target and custody. An ambiguous acknowledgement replays the same request key only while the same owner proves no motor attempt. A proven EMPTY exit pocket receives a distinct, owner-bound no-discharge authorization tied to its boundary and generation; unknown occupancy and recovery/maintenance paths refuse.

`MarkerPositioner._move` checks the single-use permit immediately before `motor.start`, consuming it first. The guard rechecks owner, exact target, motor token/coordinates, physical route, reservation/attempt state, policy/configuration and unresolved journey evidence under the caller's outer locks; its SQLite read ends before the motor call. Bounded trims stay on the accepted tracked attempt. Unknown command acceptance or accepted motion without marker confirmation retains the index and records native uncertainty. Pausing an unissued index fences dispatch; an already accepted finite move can finish.

The exact `ConfirmedIndex` stays in owner memory. `confirm_exit` commits using a frozen marker-source epoch/sequence/capture reference and separate wall timestamp before FIFO completion, binding removal or discharge. Failed/ambiguous exit acknowledgement retries only persistence with the same payload and key. A downstream callback failure retains handoff identity and blocks automatic replay. Successful handoff leaves the reservation at `EXIT_CONFIRMED`; this slice does not call `complete_native`.

## Validation

- Focused development: `uv run --frozen python -m pytest tests/test_smart_bins_physical_bridge.py -q` → **20 passed**. Earlier runs had **15 passed, 2 failed**, then **16 passed, 1 failed** from test assertions about prior empty-index motor calls and a resume error string; those assertions were corrected. An intermediate run passed **19** before the final failure-path test was added.
- Final affected group: `uv run --frozen python -m pytest tests/test_smart_bins_physical_bridge.py tests/test_physical_c4_runtime.py tests/test_marker_positioner.py tests/test_smart_bins_delivery.py -q` → **160 passed**.

The tests use the real runtime, FIFO, planner, positioner and temporary SQLite lifecycle; only external observations and motor hardware are faked. Temporary database/configuration paths precede backend imports, and test connections/keepers close through the existing isolated fixtures. No full-backend, frontend, device or physical qualification ran.

## Remaining boundary

The owner interface and lock discipline must be supplied by a future production integration; this package does not wire startup, activate smart bins, reconcile interrupted claims or permit recovery motion. Sending completion, Harvest's separate-store route, legacy-writer fencing and physical bin-entry sensing remain later work. A simulated motor log proves ordering, not hardware custody or landing.
