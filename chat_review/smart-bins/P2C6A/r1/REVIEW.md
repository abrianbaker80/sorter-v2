# P2C6A r1 — Inactive Harvest integration journal

The local smart-bin ledger and Harvest use separate SQLite databases. This slice
adds durable request identities, exact read-back and local phase transitions
without a distributed transaction. Native reservation, delivery and contents
remain authoritative in local state; Harvest owns quota and project progress.

**Inactive, source and isolated-store qualification only.** No production
initialization, operational data access, provider call, service control, deployment,
hardware action, implementation commit or implementation push occurred.

## Baseline and patch scope

- Implementation: `sorting-flow-candidate` at
  `c4e298ebd1a153001355df3d3b0c41cc5446ac8f`; implementation remains uncommitted.
- Accepted predecessor: P2C5B r2,
  `a798d9c7a9225f69aabf77fed599515d6edea791`, reconstructed tree
  `9d8f68b33790d98c7f34584ade99be7b5a164b9a`.
- `P2C6A.patch` changes exactly five paths: new
  `harvest_integration_storage.py`, `smart_bins_harvest_integration.py` and
  `tests/test_smart_bins_harvest_integration.py`; bounded additions to
  `project_harvest_projects.py` and read-only projection in `smart_bins_delivery.py`.
  Paths are relative to `software/sorter/backend`.
- All 34 accepted-chain source blobs matched before editing. Relevant P2C5B r2
  physical hashes also matched. Reconstruction used separate temporary indexes;
  no predecessor patch was reapplied to the implementation checkout.
- The manifest records the complete ordered predecessor chain, source physical
  SHA-256 hashes, Git blob IDs, slice base/result trees and package hashes.
  Replaying that chain plus this patch reproduces the result tree, including
  all new files.
- Implementation HEAD, branch and empty index are preserved. Thirty-eight
  protected existing files and the three previously blocked temporary Git indexes
  are unchanged. Previous review packages are preserved.

## Actual Harvest capabilities inspected

Source: `HarvestProjectStore` in `project_harvest_projects.py`,
`project_harvest_runtime.py`, the Harvest router, `Positioning`, `Sending`, and
`PhysicalDistribution`. Harvest transactions use `_STORE_LOCK` then their own
SQLite connection. Local state uses a different connection/database.

| Operation | Existing identity/read-back | Limitation and treatment in this slice |
| --- | --- | --- |
| `create_project` | Reuses an identical draft/configuration; generated project ID; `get_project`, `list_projects` | No general caller request key. Project creation orchestration is deferred. |
| `propose_allocation` | Simulation only; unique `(project_id, piece_id)`; may cap quantity to remaining quota | Existing replay does not compare payload. Optional exact request receipt added; local adoption rejects a quantity mismatch. |
| `propose_live_allocation` | Active singleton supplies project and activation; generated allocation ID; unique project/piece; `runtime_id` stores activation; one planned allocation per active runtime | Existing replay can return an earlier row without validating input or activation. Tagged requests freeze full arguments, expected project/activation and revision, and record allocation plus receipt in the same Harvest transaction. |
| `_reserved_progress` | Confirmed live consumption across activations plus planned quota in the current activation | Unchanged. The exclusive planned/confirmed status avoids counting an allocation twice. |
| `confirm_allocation` | Project/allocation lookup; confirmed replay; stores exact `evidence_json` | Existing confirmed replay does not compare new evidence. The journal verifies stored evidence against exact operation, request hash, reservation, piece, activation and native delivery ID/hash before accepting confirmation. No existing confirmation semantics changed. |
| `undo_allocation` | Exact allocation lookup; undone status replays | Can undo confirmed rows; cannot establish physical safety. Tagged allocations are refused and require integration reconciliation. Untagged behavior is preserved. |
| `retire_c4_planned_allocations` | Active-runtime planned rows; optional piece list; count return; may retire all planned rows | No operation key, no local custody knowledge. Tagged rows are excluded from both blanket and piece-selected retirement. A separate exact, planned-only cancellation method is used for integration. |
| `activate_project` | Expected project revision, pre/post bin-state tokens; generated activation ID; audited assignment map | Router applies local category assignment before Harvest activation and attempts restoration on exceptions. No distributed atomicity or exact activation request receipt. Full activation orchestration is deferred. |
| `get_active_runtime`, `has_active_runtime` | Singleton active runtime; project/activation consistency checks | Not proof of an individual allocation or a past operation. |
| `stop_activation` | Rejects planned live allocations before stopping the runtime | Preserved. Stopping is not cancellation evidence. |
| `get_project` | Bounded recent allocation list (`MAX_RECENT_ALLOCATIONS`) | Absence in this list is not evidence of absence. New exact project/piece lookup has no recent-list limit. |
| `acceptance_allocations` | Exact acceptance runtime, confirmed allocations only | Does not prove a pending allocation, cancellation, or arbitrary live delivery. Preserved. |

Runtime crossings remain unchanged: `reserve_piece` calls live quota reservation;
`confirm_piece_drop` calls confirmation from Sending; the two
`PhysicalDistribution` retirement paths call the existing store wrapper.
`rehydrate_active_bin_assignments` reads Harvest then persists local categories.
There is no local journal wiring into any of those runtime paths.

## Explicit schema and authority boundaries

Both extensions are version 1 and require explicit initialization. Neither
initializes on import, during existing startup, or in `HarvestProjectStore.__init__`.
No migration of operational stores was performed. Unsupported or incomplete
versions fail closed.

Local state reuses `smart_bin_external_operations` as the operation journal.
Companion tables add immutable request/local evidence, observations, reservation
integration links and action receipts. They contain references and the evidence
needed for coordination, without a copy of the Harvest project model.
Triggers preserve identity, request payloads, observations, links and receipts;
external phases cannot regress or skip. Every journal mutation updates machine
and operation revisions and an audit/receipt in one local transaction.

Harvest adds `harvest_integration_requests` only through
`initialize_integration_schema()`. Optional `integration_request` arguments on
the existing proposal methods atomically commit the exact request, allocation
snapshot, activation destination and original acknowledgement with the allocation.
The ordinary allocation/quota logic is reused. Exact tagged retries return the
original acknowledgement before project revision/runtime checks; mismatched
operation IDs or keys conflict. Existing untagged rows cannot acquire fabricated
request provenance.

`lookup_integration_allocation(project_id, piece_id=...)` returns a query-only
snapshot of the exact row and immutable receipts. It neither assembles the
project nor calls a provider. A missing row remains unresolved: a read snapshot
cannot prove that an earlier in-flight request will never commit.

Legacy stores without the extension remain usable by existing calls. Their
untagged records do not satisfy the integration proof. Activation has no new
exact request/read-back protocol in this slice and cannot be treated as confirmed
by inference from an active project.

## Phases and APIs

| Journey | Forward phases | Completion condition |
| --- | --- | --- |
| Allocation | `LOCAL_INTENT` → `REMOTE_CONFIRMED` → `COMPLETE` | Exact read-back, then immutable local link while reservation remains `RESERVED`. A proven cancellation can instead close an unlinked allocation. |
| Cancellation | `CANCEL_INTENT` → `CANCEL_CONFIRMED` → `COMPLETE` | Local reservation is already safely cancelled, with no release attempt or native delivery; exact tagged remote cancellation is read back. |
| Delivery confirmation | `DELIVERY_CONFIRM_PENDING` → `DELIVERY_CONFIRM_REMOTE_CONFIRMED` → `COMPLETE` | Canonical native delivery and local link remain intact; remote stored confirmation evidence matches exactly. |

`readback_required` and `reconciliation_required` are independent durable flags.
They retain uncertainty without regressing an already recorded forward fact.
A contradiction creates an explicit discrepancy and remains blocking even if
later evidence looks correct. This slice has no automatic discrepancy resolver.

Public local APIs:

- `initialize_schema`, `begin_allocation_operation`, `begin_allocation_cancel`.
- `record_remote_allocation_result`, `record_remote_cancel_result`, and
  `record_delivery_confirmation_result`: append detached response data only.
- `advance_remote_result`: accepts only the latest exact `READBACK`; ACKs,
  timeouts, absence and missing provenance cannot advance the phase.
- `link_local_reservation`, `complete_operation`: check local evidence again
  before completing integration. They never modify native reservation/delivery
  truth or recorded contents.
- `stage_delivery_confirmation_on_connection`: caller-owned FULL-synchronous,
  foreign-key-enforced transaction; no internal commit or second connection.
  It freezes native delivery evidence and raises on refusal so future callers
  cannot inadvertently commit a delivery without its required journal record.
  The normal delivery transaction does not call this helper yet.
- `lookup_operation`, `inspect_integration_recovery`, and the existing
  `smart_bins_delivery.inspect_recovery` projection: local read-only recovery.

Every operation has a stable ID, action-qualified key, canonical request hash,
machine/reservation/piece/project/activation references, known allocation/delivery
references, expected local/project revisions, timestamps and audit history.
Exact successful local request replay returns its saved acknowledgement before
stale checks. Reusing a key with different data returns `IDEMPOTENCY_CONFLICT`.

The service mutex is `integration_lock`; callers hold lifecycle/physical-owner
locks outside it. Local SQLite is always innermost. No local API calls Harvest,
a callback, hardware, or a provider. Future callers must commit and close local
transactions before invoking Harvest, and close Harvest transactions before
calling local state. No worker or retry dispatcher was added.

## Recovery and remaining runtime work

Inspection reports every operation's kind/phase, reservation/piece/project/
allocation/activation/delivery references, local record presence, known remote
confirmation, read-back and cancellation obligations, and affected admission
scope. `physical_recovery_authorized` is always false. Missing/contradictory
evidence still blocks an operation marked `COMPLETE`; missing schema/provenance
can require a machine-wide block. Existing P2C5 discrepancies and follow-up
blockers remain in the combined projection.

New allocation journal requests are refused while their project has an unresolved
integration obligation. Production admission, native release gating, source
collection and normal completion staging remain unwired. A completed allocation
record is not a release permit. Later wiring must enforce agreement at dispatch
and hold the owner across revision checks; the two stores cannot fence one
another with a SQLite transaction.

Confirmation staging currently supports normal BIN deliveries with a linked
physical activation. Reject remapping, unknown delivery attribution, activation
recovery, broad legacy writer fencing and operational migration are deferred.
Native completion and contents are never rewritten to repair Harvest persistence.
Tagged uncertain quota is protected from legacy retirement/undo, but no physical
recovery or redelivery is authorized.

## Verification

Python 3.12.12 and frozen dependencies. Tests use two real temporary SQLite
stores, isolated roots before imports, reset local caches and controlled clocks.
The Harvest activation in new tests is a synthetic store fixture; no activation
or physical runtime is invoked. Socket connections are forbidden in these tests.

The 51 focused cases cover intent durability, crashes before/after remote commit,
lost acknowledgements, exact replay/conflicts, identity/payload/destination
mismatches, local and remote rollback, safe cancellation including failure before
linking, tagged blanket-retirement refusal, delivery/contents preservation,
confirmation ambiguity, staging rollback, missing evidence on completed records,
project admission scopes, read-only recovery, existing P2C5 blockers, explicit
schema initialization and absence of runtime wiring.

Development command:

```text
uv run --frozen python -m pytest tests/test_smart_bins_harvest_integration.py -q
46 passed in 11.69s before the five final retirement regressions.
```

Final requested six-module group: **236 passed in 49.77s**. Final affected
Harvest group (new integration tests, store tests and distribution-runtime tests):
**65 passed, 4 baseline failures in 16.71s**. Exact commands and scopes are
recorded in `manifest.json`. Four existing Harvest
store failures were also run against the preserved pre-slice module without
replacing checkout files. Baseline result: **9 passed, 4 failed in 1.20s**.
The same failures are: expected single wave vs two-stage plan; expected two vs
three waves; expected two simulation deliveries in live progress vs zero; and
expected physical-acceptance readiness vs false. These expectations and source
behavior are unchanged by P2C6A. No test was weakened or skipped.

Ruff passes on the three new modules. Source whitespace checks and exact patch
reconstruction pass. Patch-file single-space blank context lines are unified-diff
syntax; source whitespace is checked separately. Self-review included the
receipt/result binding, destination checks, cancellation-before-link closure,
terminal-evidence checks and tagged retirement guards. Independent review was
not run because the user prohibited subagents; it remains unclaimed.

## Publication

Only `REVIEW.md`, `P2C6A.patch` and `manifest.json` in this package are committed
and pushed on the existing `chat-review` publisher using a literal allowlist.
GitHub reports no Actions workflows, rulesets or branch protection for this
publication branch. Remote ref, commit paths and artifact bytes are read back
after the push. This publication is a review package, not an implementation
commit, deployment, live migration or physical qualification.

## Golden live reference and succession gate

The user-provided golden sorting/performance reference is live SorterOS stable
v0.2.9 at `2c8269843c16282659c9edbfbf03e7c7042a15ce`, reported to run
continuously at roughly 7–9 pieces/minute. The v0.2.9 change is the Tailscale
reauthentication correction; it does not materially change sorting/runtime
behavior. Its firmware remains v0.8.1, commit `8d560d26`, distribution-v1-2.
Harvest is not integrated into that proven live version.

This inactive slice did not modify or physically requalify that 7–9 PPM live
baseline. These live details are supplied reference information, not a fresh
live inspection or a candidate performance result. No runtime performance claim
is made. Ordinary non-Harvest sorting remains unwired from this journal: no
production startup initialization, per-piece Harvest dependency, C3/C4 change,
Sending integration, physical dispatch, or background worker was added.

After P2C6A acceptance, the next slice must be a separate live-baseline
convergence / performance-preservation review against that v0.2.9 reference.
P2C6B runtime Harvest integration must not begin until that review is complete.
Publication ends this slice; it does not authorize the next slice.

### Documentation follow-up verification

The initial P2C6A package at
`45a97ce428b0a82833ae3ac8cf9f2f7efd7ef010` was read back from GitHub and all
three artifact hashes matched. This follow-up changes only REVIEW.md and
manifest.json to record the explicit live reference and succession gate.
The original P2C6A.patch and all five implementation/test files are unchanged.
The accepted predecessor chain plus P2C6A.patch was reconstructed again and
matched the recorded result tree and all five source blob IDs/SHA-256 hashes.
The test totals above are reused recorded results for those unchanged bytes;
no tests were rerun for this documentation-only follow-up. The implementation
HEAD, empty index, 38 protected files and three blocked indexes were reverified.
