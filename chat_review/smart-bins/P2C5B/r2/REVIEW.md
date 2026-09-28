# P2C5B r2 - Match failed-operation evidence and retain closure

This correction fixes two evidence errors in P2C5B r1. A side-effect hold now
records which operation failed in the existing durable completion receipt.
Reconciliation uses that original operation phase, rather than a proposed
cause alone, when deciding whether a named hold is eligible for resolution.
Closure is now an independent obligation whenever a completed delivery lacks
a valid normal CLOSE receipt, including after publication is reconciled on an
unchanged `PUBLICATION_ATTEMPTED` row.

## Baseline and two patch paths

The uncommitted implementation remains on `sorting-flow-candidate` at
`c4e298ebd1a153001355df3d3b0c41cc5446ac8f` with an empty index.
Reviewed r1 is `0d0b97438daac956f823dd2212412b7338c3bde0`; accepted
P2C5A r1 is `63c2ca9842e0f3ad461d558ce20513b131504044`.

`P2C5B.patch` is the complete five-path P2C5B slice from the accepted P2C5A
tree. `P2C5B-r1-to-r2.patch` is the five-path correction from the exact
submitted r1 tree. Separate isolated indexes reconstructed implementation
HEAD through the complete accepted predecessor chain, then reached the same
r2 tree through either the cumulative patch or r1 plus correction patch.
All five resulting source blobs, original r1 package bytes, protected source
hashes and the three blocked old index hashes were checked. The manifest has
the exact trees, paths and SHA-256 hashes.
Source diff whitespace checks pass. Artifact-level Git flags 12 and 19
single-space blank context lines in the two patch files; these are required
unified-diff syntax, and both patch chains apply and reconstruct correctly.

## Corrected evidence behavior

- Sending records `PUBLICATION_CALLBACKS`, `GATE_OPEN` or
  `ADMISSION_RELEASE` as the original failure phase in its action-qualified
  completion hold receipt. Callback and gate operations remain outside the
  database transaction. The original reason, audit and old receipts remain.
  Older phase-unattributed holds report `UNKNOWN` and cannot be resolved by
  a caller's cause label alone. No schema conversion was added.
- Successful producer callbacks can resolve only a hold attributed to
  `PUBLICATION_CALLBACKS`. They cannot prove gate opening. A fresh gate-open
  observation can resolve only a specifically attributed `GATE_OPEN` hold
  with durable publication and close receipts. It cannot prove callback
  completion; `ADMISSION_RELEASE` remains unsupported here.
- A valid CLOSE receipt is checked against machine, follow-up, request key and
  CLOSED result. Without it, the shared inspector and preview retain
  `CLOSURE_PENDING` independently of the publication state. Reconciled
  publication leaves both `CLOSURE_PENDING` and `OWNER_REQUALIFICATION`.
  No normal close is fabricated or invoked by reconciliation.
- Delivery, history, original reservation, quantity and destinations remain
  untouched. A queued event still does not prove its consumer completed.
  Later, predecessor and unrelated holds retain their own blockers.

## Reproduction and verification

The two new regressions were run against exact r1 source before correction:

```text
test_ambiguous_attempt_resolves_only_with_attributed_producer_evidence
  FAILED: r1 returned only OWNER_REQUALIFICATION after publication resolution;
  CLOSURE_PENDING was absent.
test_real_gate_failure_rejects_successful_callback_claim
  FAILED: r1 marked the real Sending gate-failure hold resolvable from
  CALLBACKS_APPLIED while gate_open was false.
2 failed in 7.56s

uv run --frozen python -m pytest tests/test_smart_bins_followup_reconciliation.py -q
18 passed in 6.90s

uv run --frozen python -m pytest tests/test_smart_bins_followup_reconciliation.py tests/test_smart_bins_completion_recovery.py tests/test_smart_bins_sending.py tests/test_smart_bins_reconciliation.py tests/test_smart_bins_delivery.py tests/test_smart_bins_physical_bridge.py -q
226 passed in 44.96s
```

The focused cases include a real synthetic Sending gate-opening failure after
producer callbacks, an observed gate resolution of only its named hold, a
callback-phase failure, old phase-unknown evidence, and consistent preview,
apply, restart and current-process closure obligations. Existing replay,
stale-preview, rollback, inventory preservation and physical-bridge tests pass.
These tests use isolated temporary SQLite and in-memory fixtures.

The trusted owner collector and production injection remain unwired. This
evidence disposition does not authorize admission, deployment, live migration,
gate retry, callback retry or physical recovery. No operational store, service,
provider or hardware was used. No implementation commit or push occurred.

## Publication

Only this r2 package's four public-safe artifacts are committed and pushed
from the existing isolated `chat-review` publisher with an exact allowlist.
R1 and all earlier packages remain unchanged. GitHub automation and remote
artifact-byte readback are checked at publication.
