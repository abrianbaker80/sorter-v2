# P2C5B r1 - Completed native follow-up evidence reconciliation

Implemented inactive read-only preview and transactional disposition for an
existing native follow-up with a completed reservation and matching durable
delivery and piece history. The original attempt, intended and actual
destination, delivered quantity, original history, holds and old receipts stay
unchanged. The only durable changes are reconciliation evidence, named
resolution links, audit, revisions and an action-qualified receipt.

## Baseline and reconstruction

Implementation branch `sorting-flow-candidate` remains at
`c4e298ebd1a153001355df3d3b0c41cc5446ac8f` with an empty index. Accepted
P2C5A r1 is `63c2ca9842e0f3ad461d558ce20513b131504044`. The 32 pre-existing
dirty/untracked files were hashed before edits. All 31 files outside this
slice remained byte-identical, as did the three blocked old temporary indexes.
The slice adds two previously untracked files and changes one previously
untracked P2C5A file. `P2C5B.patch` contains exactly these three paths.

The manifest records the complete P1 through P2C5A chain, accepted and result
trees, source SHA-256 and blob IDs, and the new patch. Two isolated indexes
reconstructed the accepted tree from implementation HEAD and the full predecessor
chain, then reproduced this slice's result tree and all three source blobs.
Source diff whitespace validation passed.
The patch artifact's Git whitespace check flags two single-space blank context
lines required by unified-diff syntax; patch application and reconstruction pass.

## Evidence contract and supported resolution

- The request binds machine, follow-up, reservation, delivery, original release
  attempt and marker/completion references; it requires expected machine and
  follow-up revisions, actor/reason and a fresh current-owner snapshot. The
  current owner and reconciliation actor remain distinct from original custody.
- Preview verifies completed reservation, matching durable delivery and piece
  history, original source evidence, and absence of Harvest/external operations.
  Missing, pending, mismatched or malformed evidence defers. Preview separates
  each producer effect and event-consumer outcome as applied, not applied,
  unknown or proven inapplicable. An event queue write never proves consumption.
- Original publication-attempt receipts establish only that publication began.
  A publication-success receipt proves mandatory bounded producer callbacks
  returned, but old records do not say whether the optional tracker existed;
  those progress effects remain unknown without specific evidence. Synthetic
  trusted-owner witnesses may establish per-effect outcomes; paired proof of
  original tracker absence is required to mark both optional effects inapplicable.
- Only explicitly named, digest-matched, evidence-eligible publication or hold
  obligations receive resolution links. A gate observation cannot explain an
  attempted callback failure; gate resolution requires durable publication and
  closure receipts. Proven non-application and unknown callbacks remain a named
  publication blocker. The normal close step, discrepancies, later holds and
  owner requalification remain separate blockers.

`smart_bins_followup_reconciliation.py` exposes explicit `initialize_schema`,
`preview`, `apply` and `lookup_request`. A version-1 additive extension keeps
immutable dispositions, named links and receipts. Initialization is explicit;
the inactive API does not migrate operational state. Preview uses read-only
SQLite; apply rechecks evidence, revisions and the preview digest under the
existing FULL-synchronous `BEGIN IMMEDIATE` transaction. Exact payload replay
returns the original acknowledgement before stale checks; changed payload
conflicts. An old acknowledgement is never current owner authorization.

The shared completion-recovery inspector projects both raw attempted/failed
history and audited resolution for restart and current-process assessment. The
live handoff is not adopted or rearmed. The trusted owner collector, operator
UI and production injection remain unwired. The caller must hold the established
outer owner/controller/service locks while collecting the snapshot and applying;
the database transaction itself acquires none of those locks and invokes no
callbacks, providers, gates or hardware.

## Validation and remaining boundary

Frozen Python 3.12 and real temporary SQLite were used, with database/config
paths set before imports, controlled clocks and keeper cleanup.

```text
uv run --frozen python -m pytest tests/test_smart_bins_followup_reconciliation.py -q
15 passed in 5.06s

uv run --frozen python -m pytest tests/test_smart_bins_followup_reconciliation.py tests/test_smart_bins_completion_recovery.py tests/test_smart_bins_sending.py tests/test_smart_bins_reconciliation.py tests/test_smart_bins_delivery.py tests/test_smart_bins_physical_bridge.py -q
223 passed in 32.77s
```

Tests cover ambiguous, partial, not-applied, unknown and tracker-absent effects;
identity, owner, stale preview, competing request and replay boundaries; missing
history, pending delivery and external operation deferral; named gate and later
owner/predecessor holds; exact preservation of inventory, delivery, history,
attempt and old receipts; rollback after disposition/link/audit/revision writes;
and shared restart/current-process assessment. Sending's in-memory synthetic
callbacks were exercised; no operational callbacks or stores were used.

Quantity effect: **zero**. Evidence disposition does not grant admission,
resume, permit recreation, deployment, live migration or physical recovery.
No implementation commit, push or merge occurred. No subagents were used.

## Publication

Only `REVIEW.md`, `P2C5B.patch` and `manifest.json` are committed and pushed
from the existing isolated `chat-review` publisher with a literal allowlist.
The repository reported zero GitHub workflows and zero webhooks at publication
review; no automation settings were changed. Earlier packages remain intact.
Remote ref and artifact-byte readback are required after push.
