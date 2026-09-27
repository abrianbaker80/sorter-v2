# Smart bins P1 — eligibility review

**Status:** implementation uncommitted; review pending. This package publishes review artifacts only. It is the first submission (`r1`), with no predecessor review package. `P1.patch` contains the P1 source and test changes relative to `sorting-flow-candidate` at `c4e298ebd1a153001355df3d3b0c41cc5446ac8f`; it is not an implementation commit. The index, worktree, and nonignored untracked set were clean at the start of P1.

## Behavior submitted

The allocator now excludes full matching, unassigned, and shared bins; checks enabled/reachable slots, pool, recorded contents, and the current piece's existing layer dimension limit before selection or assignment; and tries another fitting destination before using the existing layer oversize passthrough and metadata. Empty routing labels do not imply empty contents. Occupied bins require reconciling count and aggregate category evidence. Unknown or inconsistent evidence is skipped, and a read failure propagates. A failed assignment write does not change the in-memory labels. Matching preference and random selection, normal/not-in-inventory separation, MISC virtual rejection, global oversize, and Harvest's exact reserved destination and exception path remain in place.

Changed implementation paths:

- `software/sorter/backend/subsystems/distribution/positioning.py`
- `software/sorter/backend/local_state.py`
- `software/sorter/backend/tests/test_bin_eligibility.py` (new)
- `software/sorter/backend/tests/test_local_state.py` (Windows temporary SQLite keeper teardown)

The included `P0-CONTRACTS.md` is a public-safe copy of the proposed P0B contract, including the supervisor clarifications. It documents later work; P1 does not implement those contracts.

## Validation reused from P1

Commands ran from `software/sorter/backend` with Python 3.12 via the frozen environment. Each run set fresh temporary `LOCAL_STATE_DB_PATH` and `MACHINE_SPECIFIC_PARAMS_PATH` values before imports. No tests were rerun for publication.

1. `uv run --frozen python -m pytest tests/test_bin_eligibility.py -q` — 14 passed on the initial focused pass; 17 passed after adding step and Harvest cases.
2. `uv run --frozen python -m pytest tests/test_bin_eligibility.py tests/test_local_state.py::LocalStateMigrationTests::test_sorting_sessions_persist_current_bin_state_and_recent_pieces tests/test_local_state.py::LocalStateMigrationTests::test_bin_snapshots_accumulate_layers_and_close_on_all_clear -q` — first run: 20 passed, 2 teardown failures on Windows because the existing fixture left its temporary SQLite WAL keeper open. The fixture was corrected. The same command then passed 22 tests twice, including after the final allocator signature change.
3. `uv run --frozen python -m pytest tests/test_distribution_rehome.py tests/test_flap_routing_integrity.py tests/test_unverified_handoff.py tests/test_project_harvest_distribution_runtime.py -q` — 76 passed twice, including after the final signature change.

`git diff --check` and `git apply --cached --check` passed for the submitted patch. No full backend or frontend suite ran.

## Limits and follow-on work

P1 does not reserve capacity for in-flight pieces, establish concurrency safety, prove physical emptiness or historical pool provenance, fence alternate assignment writers, or activate the later ledger. P2 must address those gates and verify lock entry paths. The P0A local baseline note is not included; the implementation base is recorded here and in the manifest. No deployment or physical qualification occurred. Independent review remains pending.
