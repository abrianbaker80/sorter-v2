# P2B r2 — admission and held-group correction

**Review pending. Implementation remains uncommitted and inactive.** This revision changes only `smart_bins_service.py` and `tests/test_smart_bins_reservations.py` from P2B r1. `P2B-r1-to-r2.patch` is that correction; `P2B.patch` is the complete P2B slice from accepted P1 r2 + P2A1 r2 + P2A2 r3. Earlier packages remain intact.

## Findings addressed

Preview and new reserve attempts now use the same context, journey and candidate evaluation. Preview refuses a machine/piece already credited by native or historical delivery, any in-flight claim, and duplicate or unlinked cancelled route attempts. A valid new route attempt linked to the latest cancelled reservation can still reserve. An **exact committed request-key/payload replay** is handled before stale-revision and journey checks and returns its original receipt. On `STALE_REVISION`, preview returns the current database `state_revision` and separately names the stale `qualification_state_revision`.

Cycle selection now reads each held reservation's quantity, group, policy, routing revision and intended destination on the same `BEGIN IMMEDIATE` transaction connection as recorded contents. Held quantity contributes to capacity **and** compatibility. A hold from another policy/routing revision, an incompatible or unqualified held group, or a mismatched intended slot refuses that candidate; the service does not infer a cross-policy mapping. For compatible holds under the qualified policy, missing assignment labels do not make the cycle empty. A matching group may reuse the cycle; a different group needs the existing explicit sharing allowance. Another eligible cycle can still be selected. `UNCERTAIN` and unresolved-delivery blocking remain intact.

The test fixture now writes temporary layout/pool configuration explicitly in `configure()`; `qualification()` only reads current revisions and builds an input. Correction assertions therefore do not silently rewrite configuration. No schema, migration, shared P1 helper, legacy allocator or runtime path changed.

## Evidence

- Before the source correction, `uv run --frozen python -m pytest tests/test_smart_bins_reservations.py -q -k r2_` produced **6 failed, 1 passed, 25 deselected**. The failures reproduced preview admitting credited/in-flight/invalid retries, reporting the stale revision, and treating cross-policy or unlabeled holds as empty.
- After correction, `uv run --frozen python -m pytest tests/test_smart_bins_reservations.py -q` produced **33 passed**.
- Final affected group: `uv run --frozen python -m pytest tests/test_smart_bins_reservations.py tests/test_smart_bins_storage.py tests/test_smart_bins_migration.py -q` produced **98 passed**.

The tests use isolated Python 3.12/frozen temporary SQLite and configuration paths, real transactions and closed connections. They verify unchanged assignments, reservations, contents, receipts and revisions on refusal; same-policy matching and explicit sharing; alternative-cycle selection; stored replay after reconnect; and the retained final-unit race. The P1 helper and allocator are unchanged, so r1's **26 passing `tests/test_bin_eligibility.py` cases** were reused without rerunning them. No full backend or frontend suite ran.

## Qualification and P2C boundary

The caller still supplies a freshly qualified reachability/settings snapshot and current-owner evidence under outer locks. The service checks existing local-state configuration, policy, slot, cycle, contents and machine revisions transactionally, but it is not wired to production. Legacy manual/profile/layout/history/Harvest writers are not fenced. P2C must bind active policy and external revisions to the physical owner, implement release intent and dispatch permits, exit/completion/recovery and reconciliation, and adopt committed routes only after durable commit. Imported cycles remain unqualified. This package makes no runtime-readiness, deployment or physical crash-safety claim.

`manifest.json` records both exact patch bases, reconstruction, path hashes and packaged-file hashes. Publication is review evidence, not source acceptance.
