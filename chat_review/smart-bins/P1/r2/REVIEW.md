# Smart bins P1 — occupied unlabeled bin correction (r2)

**Status:** implementation uncommitted; review pending. This revision follows [r1](../r1/REVIEW.md) at commit `a42151f26e15774713fd483e2b3a0cf6bf841001`. The unchanged [P0 contracts and supervisor clarifications](../r1/P0-CONTRACTS.md) remain the proposal for later slices.

## Findings addressed

- A bin with empty routing labels and complete recorded `B` contents can now enter the **shared fallback** for incoming `A` when the existing broad-sharing policy permits it. Normal bins require `allow_multiple_categories_per_bin`; the not-in-inventory pool retains its existing overlap behavior. When selected, the persisted labels preserve `B` and add `A`. With sharing disabled, the normal bin remains ineligible.
- A genuinely empty eligible bin is selected before introducing new mixing into an occupied unlabeled bin. Existing eligible labeled matches retain their preference; occupied unlabeled bins already containing the incoming category can restore their known labels without new mixing.
- Any recorded MISC contents exclude that physical bin from reuse, including the normal and not-in-inventory pools with sharing enabled. Contents evidence is left intact; no physical assignment containing MISC is restored from that evidence.

Production correction is confined to `software/sorter/backend/subsystems/distribution/positioning.py`. New regressions are in `software/sorter/backend/tests/test_bin_eligibility.py`. The read-only occupancy reader and Windows test-fixture change from r1 are unchanged.

## Patches and bases

- `P1.patch` is the **complete cumulative P1** source/test patch, including the untracked test file, against published `origin/sorting-flow-candidate` commit `c4e298ebd1a153001355df3d3b0c41cc5446ac8f`.
- `P1-r1-to-r2.patch` is **only the correction** from the exact r1 submitted source. The r1 source was reconstructed from its patch and checked against its manifest hashes before generating this delta. Both patches were applied to isolated Git indexes and verified against the current source/test file hashes.

## Validation

Commands ran from `software/sorter/backend` in the frozen Python 3.12 environment. Every run set fresh temporary `LOCAL_STATE_DB_PATH` and `MACHINE_SPECIFIC_PARAMS_PATH` paths before imports.

1. Before the allocator fix: `uv run --frozen python -m pytest tests/test_bin_eligibility.py -q -k 'unlabeled_other_category or genuinely_empty_bin or recorded_misc'` — **4 failed, 2 passed, 20 deselected**. The two permitted-sharing cases were rejected and both MISC cases restored `["A", "misc"]`; the no-sharing and empty-bin preference cases passed.
2. After the fix: `uv run --frozen python -m pytest tests/test_bin_eligibility.py -q` — **26 passed**.
3. Once stable: `uv run --frozen python -m pytest tests/test_distribution_rehome.py tests/test_flap_routing_integrity.py tests/test_unverified_handoff.py tests/test_project_harvest_distribution_runtime.py -q` — **76 passed**.

The unchanged r1 local-state cases previously passed (22 with the focused suite); they were not rerun because neither `local_state.py` nor its fixture changed in r2. No full-backend or frontend run.

## Remaining limits

P1 still does not reserve in-flight capacity, establish concurrency safety, prove physical emptiness or historical pool provenance, fence alternate writers, or activate P2 ledger and recovery contracts. No implementation commit, deployment, hardware operation, or physical qualification occurred. Independent review of r2 is pending.
