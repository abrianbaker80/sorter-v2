# P2A2 review: explicit inactive legacy-bin backfill

Implementation remains uncommitted; review pending. This **P2A2-only** patch follows accepted [P1 r2](../../P1/r2/REVIEW.md) (`ea86ac3d8be4a942730ffe1f44c61c0bcf3b9ff2`) and [P2A1 r2](../../P2A1/r2/REVIEW.md) (`dd2eb3edfa7f2b0f8b07d256b7daaed674ef6dfe`). Apply their cumulative patches to implementation HEAD `c4e298ebd1a153001355df3d3b0c41cc5446ac8f`, then `P2A2.patch`. A temporary Git index reconstructed all nine current P1/P2A1/P2A2 source and test blobs in that order.

## Interfaces and schema

- New `software/sorter/backend/smart_bins_migration.py` provides `plan_legacy_backfill(db_path=None)`, a read-only coherent legacy snapshot and deterministic plan; `initialize_migration_schema()`, an **explicit** additive extension installer; `apply_legacy_backfill(plan)`, which re-plans under P2A1's FULL-synchronous `BEGIN IMMEDIATE` transaction before one atomic commit; and `read_recorded_contents(cycle_id, db_path=None)`, an inactive cycle projection.
- The accepted core smart-bin schema stays **version 2**. The extension has its own version 1 table, import/anchor/group-source/historical-contribution tables, and cross-origin triggers. It does not relabel v2 or convert rejected experimental v1. Extension installation is rerunnable and required before apply. Core reservation/delivery guards remain unchanged.
- An import records detached cycles, source cutoffs, provenance, linked event/history contributions, group-level opening balances, blocking discrepancies, one import receipt and machine audit/revision. Deterministic migration IDs identify records, never invented pieces. `opened_at` on a migration cycle is labeled as a source anchor, **not** a measured last-empty time. Existing legacy rows, settings and correction fields are not rewritten.
- The reader sums native deliveries at the evidenced actual cycle, proven historical contributions and opening balances. It returns group breakdown, raw legacy identity context, coverage, source fingerprint/machine revision and discrepancies for P2B/P2E. No legacy API or routing code calls it yet.

## Evidence limits

Cross-session linkage requires adjacent supported session close/start evidence, matching machine/profile artifact, one unchanged saved layout, coordinates/epoch, and count plus item-aggregate carry-forward arithmetic. A missing link retains `historical_unknown` coverage; it never attaches contents to a guessed current slot. Clear snapshots are cutoffs, not new inventory; repeated identical rows count once. Import-marked or history-unmatched piece IDs and synthetic times do not become contributions. An attributable legacy event must match per-piece history and its group; that proves software lineage, not sensor-confirmed bin entry.

Trustworthy aggregate residuals become explicit opening balances, including a named unidentified group when membership is absent. Conflicting identities, native-delivery duplicates, unsupported group membership, negative residuals, conflicting totals and missing clear evidence create blocking discrepancies. Hard conflicts are not credited. A prior successful import blocks a changed source until explicit reconciliation; no automatic re-import or operational migration is provided. The source fingerprint covers relevant legacy rows and configuration, excluding this extension's output.

## Validation

Python 3.12 frozen environment; temporary `LOCAL_STATE_DB_PATH` and `MACHINE_SPECIFIC_PARAMS_PATH` were established before backend imports. All stores and connection cleanup were synthetic and isolated.

| Command, from `software/sorter/backend` | Result |
| --- | --- |
| `uv run --frozen python -m pytest tests/test_smart_bins_migration.py -q` | **15 passed** on final source. |
| `uv run --frozen python -m pytest tests/test_smart_bins_migration.py tests/test_smart_bins_storage.py tests/test_local_state.py -q` | **50 passed** on final source. |

Tests cover session carry, repeated snapshots, clear boundaries, importer-generated IDs, aggregate-only and unknown groups, raw identity context, group residuals/conflicts, duplicate pieces, native/historical double-credit guards, read-only planning, explicit extension initialization, exact replay, stale-plan rejection, injected full rollback, and unchanged representative legacy rows/settings/corrections. P1 and unrelated history suites were not rerun.

The ledger remains inactive. Before cutover, P2B/P2E must consume the reader and blockers, prove slot attachment and source lineage where possible, quiesce/fence every legacy writer, and revalidate the source snapshot. No deployed, physical or sensor qualification is claimed.
