# P2C3 r1 — guarded native completion at Sending

**Implementation uncommitted; review pending. The guarded path remains inactive in production.** This slice follows [accepted P2C2 r2](../../P2C2/r2/REVIEW.md) at `bc206ae58d4b24e014547568ddb299a8c4faef69`. Its patch applies to that package's result tree after the recorded P1 through P2C2 predecessor chain.

## Injected path

An isolated caller constructs `NativeCompletionAdapter(bridge)` and injects the same adapter into `PhysicalDistribution(..., native_completion=adapter)` and `Sending(..., native_completion=adapter)` (or `DistributionStateMachine(..., native_completion=adapter)`). No production constructor call was changed. With no adapter, ordinary unguarded distribution and Sending keep their legacy behavior. A native reservation with no matching adapter refuses before transport advancement.

At C4 discharge, the adapter compares the retained bridge custody, attempt, marker boundary and exit evidence with the durable `EXIT_CONFIRMED` reservation and release record. It also checks the exact piece, reserved slot/cycle or virtual reject, sorting session, owner incarnation and Harvest metadata. The distributor retains that handoff before advancing the existing transport slot. Missing or foreign metadata and Harvest-bound pieces hold the route. The bridge can then clear its completed index without losing completion identity.

Sending keeps its existing settle and physical gate checks. On the first settled attempt it freezes one completion request key, timestamp, runtime run ID and `CompletionEvidence` history payload. It calls `complete_native` with the reserved destination and reads back the committed delivery and piece history before adopting `distributed`, publishing an event or reopening the gate. A failed write or lost acknowledgement retains that same request and evidence for a persistence-only retry. A refusal or mismatched result holds the gate; no extra drop, invented sensor-entry claim or replacement `UNCERTAIN` transition is made.

Native delivery remains the sole durable accounting writer. `RunRecorder.recordCommittedNativePiece` verifies the delivery and adopts the piece in memory without its legacy history upsert. Native event fields carry machine, reservation and delivery IDs; `RuntimeStatsCollector` verifies them against the ledger and retains the provenance across partial updates and lookup eviction, suppressing its legacy bin writer. Ordinary unguarded, Harvest and sample behavior uses the prior path. If an after-commit event, recorder or progress callback fails, Sending retains the committed receipt and a closed gate, publishes its existing incident/pause controls, and does not blindly repeat an incrementing callback.

## Validation

Commands ran from `software/sorter/backend` with Python **3.12.12**, frozen dependencies, temporary SQLite/configuration paths established before imports, simulated hardware, controlled settle clocks and fixture cleanup.

- `uv run --frozen python -m pytest tests/test_smart_bins_sending.py -q` — **17 passed**.
- `uv run --frozen python -m pytest tests/test_smart_bins_sending.py tests/test_distribution_sending.py tests/test_distribution_rehome.py tests/test_runtime_stats.py tests/test_smart_bins_physical_bridge.py tests/test_smart_bins_delivery.py -q` — **112 passed**.

The new suite drives a real guarded index through durable exit, `PhysicalDistribution`, transport, settle and completion for a bin and virtual reject. It checks commit-before-publication, before-commit failure, lost acknowledgement, repeated ticks/re-entry, unverified flags, wrong or missing drop identity, foreign/missing evidence, Harvest refusal, legacy-write suppression, after-commit callback hold, corrections and distinct session/runtime run IDs. An early test draft had one fixture failure because it replaced transport after the original Sending instance cached its piece; the wrong-piece case now creates a fresh Sending instance. No production change was made for that test correction.

`P2C3.patch` is the slice-only source/test delta from the accepted P2C2 r2 tree. The predecessor chain, patch reconstruction, ten changed paths and hashes are in `manifest.json`. Earlier packages are preserved.

## Remaining boundaries

This is isolated software simulation. Production startup injection, restart reconstruction, operator reconciliation, Harvest's separate-store bridge, legacy-writer fencing and physical qualification remain pending. The existing asynchronous event consumer has no new durable notification outbox; a synchronous after-commit callback failure holds for explicit reconciliation rather than replaying an ambiguous increment. A committed software delivery is not sensor-confirmed bin entry.
