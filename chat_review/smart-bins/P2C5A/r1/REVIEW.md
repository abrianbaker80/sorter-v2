# P2C5A r1 - Explicit native reservation reconciliation

Implemented an inactive read-only preview and transactional apply for native
`RELEASE_INTENT`, `EXIT_CONFIRMED` and `UNCERTAIN` claims without a completion
follow-up. This is synthetic source qualification. Production evidence collection,
operator UI and recovery commands remain unwired.

## Accepted baseline and patch

Implementation branch: `sorting-flow-candidate`; HEAD:
`c4e298ebd1a153001355df3d3b0c41cc5446ac8f`. Its index remains empty. The accepted
P2C4 r3 package is at `4af9cefe9a249a334a43ad16f9ba54d43708f2a1`.
All 30 existing dirty/untracked predecessor files matched its reconstructed tree
before edits. The manifest records the complete accepted P1 through P2C4 chain.

`P2C5A.patch` contains only this slice's five files, including both new/untracked
files. It applies after the complete accepted P2C4 r3 tree. An isolated index
reconstructed the entire recorded chain from implementation HEAD, applied this
patch, and reproduced the result tree and all five source blobs. Raw source hashes
and predecessor hashes are in `manifest.json`. Earlier packages are unchanged.
Source diff and report/manifest whitespace checks pass. The artifact-level Git
whitespace check flags four single-space blank context lines in the patch; these
are required unified-diff syntax. Patch application and reconstruction pass.

## Evidence and supported outcomes

- Delivery requires the original piece/custody identity, evidence references and
  provenance for the entire stored quantity, an evidenced event time, and an
  explicit actual BIN slot/cycle or REJECT. BIN proof includes attributable cycle
  membership covering that event; coordinates alone are insufficient. References
  must belong to the same machine. Historical detachment or closure after delivery
  does not invalidate the evidence.
- Delivery preserves intended destination, group, policy, quantity, original owner,
  and release/exit evidence. It credits only the actual cycle and writes canonical
  delivery plus piece history on the same FULL-synchronous transaction connection.
  No marker, sensor-entry claim, runtime run ID or timestamp is synthesized.
- Cancellation accepts affirmative non-dispatch or removal-before-contents evidence
  for the entire claim, plus current owner quiescence and dispatch invalidation.
  Evidence must establish that the claim never became recorded contents and cannot
  still arrive. Cancellation releases only the hold. Confirmed exit contradicts a
  non-dispatch claim. Removal of credited contents is deferred to a contents action.
- Weak, partial, stale, cross-machine or conflicting evidence refuses without a
  write. Completion follow-ups, Harvest/external operation ownership, existing
  delivery/history credit and ambiguous legacy/opening-balance overlap defer.
  Migration anchors and positive affected opening balances are conservatively
  deferred because this slice has no non-overlap qualification workflow.

The accepted delivery schema requires an event time: absent/unknown delivery time
is an explicit blocker. Unknown source IDs, namespaces, runtime run and creation
time remain null/unknown. Supplied raw metadata needs its own evidence/provenance;
category/group IDs never substitute for raw part IDs. Namespace provenance is
retained in immutable reconciliation evidence. History correction fields survive.

## Interfaces, authority and schema

`smart_bins_reconciliation.py` provides `initialize_schema`, `preview`, `apply`
and `lookup_request`, with typed request, disposition, cycle membership, raw source
metadata, discrepancy resolution and current owner snapshot inputs. The recovery
actor and current owner are distinct from immutable original reservation custody.

The caller holds `hardware_lifecycle_lock -> controller _operation_lock -> service
mutex`, collects a fresh owner snapshot, and retains those locks through apply.
The collector is a trusted owner boundary; evidence references are supplied by it,
not fetched or authenticated by this inactive service. The five-second snapshot
window is checked again in the transaction. No database transaction acquires an
owner/controller/motor/provider/Harvest lock or invokes a physical callback.

Preview uses SQLite read-only mode and reports quantity effects, affected cycles,
addressed discrepancies and remaining blockers. Apply repeats the assessment under
`BEGIN IMMEDIATE`, checks both expected revisions and the complete preview digest,
including relevant source rows/configuration, and atomically commits the outcome,
audit/evidence, reservation and machine revisions, and an action-qualified receipt.
Exact request/payload replay returns its original result before freshness checks;
changed payload conflicts. Other requests cannot rewrite terminal/credited facts.
The existing `record_contradiction` boundary retains later terminal contradictions.

The additive reconciliation extension is version 1: one version table,
`smart_bin_reconciliations`, `smart_bin_reconciliation_receipts`, and
`smart_bin_reconciliation_resolutions`, with immutable evidence/receipt/link
triggers. Core ledger and service versions and their consistency/immutability
guards are unchanged. Initialization is explicit; apply never creates schemas or
converts accepted data. Piece history must be initialized separately beforehand.

Shared changes are limited to cross-action request-key collision refusal,
resolved-versus-active discrepancy projection (including its resolution audit
link), and explicit reconciliation origin/requalification in recovery inspection.
Only identified, digest-matched native uncertainty or matching destination-mismatch
discrepancies may resolve; their original detail/evidence is retained. Unaddressed,
unrelated and later blockers remain effective.

## Capacity and downstream boundary

Evidenced delivery is recorded even into an over-limit, disabled, incompatible or
historically detached actual cycle. Every reconciled BIN delivery retains an
explicit allocation-requalification discrepancy because this service has no current
route/group qualification authority; known over-capacity, disabled, detached and
group risks are included. Assignments/policy and the physical route never change.

No normal completion follow-up, publication or marker exit is fabricated. Recovery
inspection retains an explicit owner-requalification blocker for reconciled
completion and cancellation. Existing physical holds, FIFO targets, permit state,
restart blockers and independent follow-up obligations remain intact. Callback
reconciliation, allocation requalification and physical recovery are later work.

**This publication does not authorize resume, admission, permit recreation,
deployment, live migration or physical recovery.**

## Validation

Commands ran from `software/sorter/backend` with frozen Python **3.12.12**, real
temporary SQLite, configuration/database paths set before backend imports, reliable
connection/keeper cleanup and controlled clocks. No arbitrary sleeps or hardware.

```text
uv run --frozen python -m pytest tests/test_smart_bins_reconciliation.py -q
74 passed in 12.75s

uv run --frozen python -m pytest tests/test_smart_bins_reconciliation.py tests/test_smart_bins_delivery.py tests/test_smart_bins_reservations.py tests/test_smart_bins_storage.py tests/test_smart_bins_migration.py tests/test_smart_bins_completion_recovery.py -q
232 passed in 33.24s
```

Coverage includes all three starting states and intended/other/REJECT outcomes,
full quantity, historical membership, over-capacity/group blocking, cancellation,
weak/stale/cross-machine/conflicting evidence, workflow deferrals, exact replay,
key collisions, terminal contradiction and duplicate credit, concurrent opposing
requests, FULL/FK connection assertions, late evidence/configuration/history,
injected rollback through history/state/machine/receipt writes (including audit and
discrepancy resolution), correction/namespace/run provenance, retained discrepancy
history, and real guarded-owner/marker-rig checks with no command or handoff.

Development found and corrected a test's stale helper name and a timestamp-only
configuration test assumption (the existing configuration digest intentionally
tracks values). Final self-review corrected preservation of a pre-existing
allocation blocker; its regression passes. No weakened assertions or skipped
failures remain. Independent supervisory review is pending; no subagents were used.
Accepted unchanged P1/physical evidence is reused; no full-backend/frontend or live
physical qualification is claimed.

## Publication and preservation

Only this package's three public-safe artifacts are staged/committed/pushed from
the existing isolated `chat-review` publisher, using a literal path allowlist.
GitHub Actions is enabled but the repository reports zero workflows and zero
webhooks; the publisher tree has no `.github` workflows. No automation settings
are changed. Publication requires remote ref and committed-artifact hash readback.

Implementation HEAD/index and all 27 unrelated/predecessor files outside the three
modified predecessor files are preserved. The three earlier blocked temporary
index files were hash-checked and left untouched; no deletion was retried. Prior
packages remain intact. No implementation commit/push/merge, force push, production
database access, configuration change, provider/SSH call, service control,
deployment or hardware operation occurred. Stop after P2C5A r1 publication.
