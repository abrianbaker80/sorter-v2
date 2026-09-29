# P2C6P-C01 r1 — media retention writer-lock convergence

**C01 is source-qualified. P2C6B remains BLOCKED.** The two retention stores now finish their selection read before deleting files, attempt each non-root parent directory once without listing it, and tombstone the selected rows in one short write transaction. Piece-link retention also runs when the main image table is within its independent cap.

No Positioning, distribution, feeder, physical C4, Harvest, Smart Bin custody, schema, synchronous-policy, firmware, machine.toml or live-installation source/configuration was changed. No deployment, service restart, live database access, provider call, hardware operation or physical performance test occurred. The user-observed continuous roughly 7–9 PPM is not requalified by this source result.

## Verified baseline and result

| Item | Exact reference |
| --- | --- |
| Implementation branch | `sorting-flow-candidate` |
| Implementation HEAD, unchanged | `c4e298ebd1a153001355df3d3b0c41cc5446ac8f` |
| Accepted implementation package | `2952a9ac4d52ec9b5f0becd580c97f9512ce3853:chat_review/smart-bins/P2C6A/r1/` |
| Accepted pre-C01 tree | `8b4d95a94d25d5c6931b57e210bf3e87de8b7e22` |
| Accepted analysis | `bc15b9a8007173b8ec49cccada7c992a85637def:chat_review/smart-bins/P2C6P-A/r1/` |
| Golden upstream | `basicallysource/sorter-v2@2c8269843c16282659c9edbfbf03e7c7042a15ce` |
| C01 result tree | `b604851c01253880d331525884d0dc280a478485` |
| Implementation index | Empty; byte hash unchanged |

Before editing, all 38 accepted P2C6A working paths matched their reconstructed Git blobs. Both retention files matched state C in the published P2C6P-A comparison inventory, including canonical SHA-256 and blob identity. The pre-fix harness used an isolated archive of the exact accepted tree, never a rollback or patch application to the implementation checkout.

Protected physical references remain SorterOS v0.2.9, firmware v0.8.1 / `8d560d26`, distribution-v1-2 and the existing live tuning. This slice did not inspect or alter the installation.

## Exact changed paths

1. `software/sorter/backend/channel_crop_store.py` — retention selection/unlink/tombstone boundaries and bounded parent cleanup.
2. `software/sorter/backend/piece_image_store.py` — the same boundaries through a shared private table helper; separate main/link cap evaluation.
3. `software/sorter/backend/tests/test_retention_sweep_locking.py` — new deterministic regression module, 23 cases.

`tests/test_piece_image_store.py` was run unchanged. Search found no separate existing channel-crop retention test module. The existing ready-channel-crop tests exercise capture/lookup rather than the changed retention routines and were not added to the affected run.

## Upstream behavior ported

- `c95543a470a0f3678f95705743aa771f8e326301`: short selection connection, filesystem work after close, then one batched tombstone write. Only retention blocks were ported. Its **Positioning persistence change was excluded**; that remains C08 work.
- `7ce8c1d3d25ca89ecceba3a4e23d8b918c189326`: collect parents while unlinking; attempt `rmdir` directly once per unique non-root parent after all unlinks. No per-file `iterdir`, listing or glob.

The piece store uses the golden private `_sweepTable` shape with fixed internal table/order arguments. Returning early for the main table no longer returns from the outer `_retentionSweep`, so the link table gets its own retention pass. No worker, dependency, schema or cross-filesystem transaction mechanism was added. Whole files were not replaced. An AST comparison confirms every non-retention production definition and top-level setting is unchanged.

Existing policy is preserved:

| Table | Byte cap | Victim priority | Maximum selected rows per pass |
| --- | ---: | --- | ---: |
| channel_crops | 512 MiB | synced first, then oldest within each tier | 1000 |
| piece_images | 500 MiB | synced first, then oldest within each tier | 500 |
| piece_link_images | 200 MiB | oldest first | 500 |

Stats and existing log messages are emitted only after a successful tombstone commit. Freed-byte and eviction counts remain metadata-based. As in both the accepted and golden implementations, expected unlink/rmdir errors are tolerated and selected rows are tombstoned; an undeletable file can remain on disk. C01 does not change that established error policy or claim the stats measure successful OS deletions.

## Reproduction before editing

The new regression module was first placed only in the isolated accepted-tree archive. It seeded disposable SQLite tables and media directories, with database/storage paths redirected before imports. At each real `Path.unlink`, a second real SQLite connection attempted `BEGIN IMMEDIATE` with `timeout=0` and rolled back immediately on success.

| Accepted pre-C01 behavior | Observed result |
| --- | --- |
| channel_crops, two victims | write-lock probes `[True, False]`; two parent-directory listings |
| piece_images, two victims | write-lock probes `[True, False]`; two parent-directory listings |
| piece_link_images, two victims | write-lock probes `[True, False]`; two parent-directory listings |
| Main image table empty, link table over cap | link victims not tombstoned |
| Main image table exactly at cap, link table over cap | link victims not tombstoned |

The first unlink occurs before the old loop's first UPDATE and therefore has a free write lock. The second unlink occurs inside that UPDATE's transaction and fails the zero-timeout second-connection probe. This identifies the actual interleaved write/unlink mechanism, rather than merely detecting an open connection.

Pre-fix result: **5 failed, 18 deselected**, as expected. The three locking failures and two independent-cap failures were assertion failures against exact accepted bytes; collection/setup succeeded.

## Final verification

Python **3.12.12**, `uv run --frozen`, existing frozen environment, `UV_OFFLINE=1`. Ruff **0.15.21**. No dependency or lock-file change was needed.

| Command, from backend unless stated | Result |
| --- | --- |
| `uv run --frozen python -m pytest tests/test_retention_sweep_locking.py -q -p no:cacheprovider --basetemp <temporary-root>/pytest -s -k "writer_lock or link_cap"` — isolated pre-C01 tree | 5 failed, 18 deselected; expected reproduction |
| `uv run --frozen python -m pytest tests/test_retention_sweep_locking.py -q -p no:cacheprovider --basetemp <temporary-root>/pytest` | **23 passed** |
| `uv run --frozen python -m pytest tests/test_retention_sweep_locking.py tests/test_piece_image_store.py -q -p no:cacheprovider --basetemp <temporary-root>/pytest` | **30 passed** |
| `ruff check channel_crop_store.py piece_image_store.py tests/test_retention_sweep_locking.py` | **All checks passed** |
| Isolated `git apply --cached --check`, apply, `git write-tree` | Exact result tree above reconstructed |
| `git diff --check <accepted-tree> <result-tree>` | Clean |
| AST comparison excluding only the four retention/helper function names | All other production code unchanged |

Each pytest invocation used a new temporary root. `LOCAL_STATE_DB_PATH`, `TEMP`, `TMP` and Python import paths were set before launch; media directories derive from that temporary database path. Plugin autoload and bytecode writes were disabled. A temporary interpreter audit hook rejected database connections outside the test root, network connection/DNS attempts, subprocess launches and shell commands. The frozen environment was reused via `UV_PROJECT_ENVIRONMENT`; no backend service or worker was started. These restrictions belong to the private test runner, not production code.

The new tests establish:

- **All three tables:** a second connection acquires the writer lock at every unlink with no busy wait.
- **Stronger connection boundary:** both unlink and rmdir see zero active retention connection contexts, including read contexts. SQL tracing records exactly one BEGIN/COMMIT and four tombstone updates for a four-victim pass.
- **Bounded cleanup:** enumeration methods raise if called during cleanup; two parents shared by four victims receive one rmdir each, after all unlinks. A file directly in the retention root does not cause that root to be removed.
- **Crash/replay:** a test-only connection proxy raises at `commit()` after both files have been removed and the tombstone updates are pending. The real SQLite connection closes and rolls back. All row metadata stays unchanged; stats/logs do not advance. Rerun observes both files already missing, commits both tombstones, preserves the remaining metadata and counts two evictions once. A further sweep is a no-op. No invasive production hook is required.
- **Policy and limits:** synced/oldest priority, independent link cap, at-cap no-op, 1000/500 selection bounds, retained metadata, missing files and expected filesystem errors remain covered.

The whole backend suite was not run. This is affected source verification, not full application, deployment, storage-device or physical throughput qualification.

## Independent review

The repository-required independent reviewer inspected the exact pre/post production bytes, the new test module and the reproduction/final logs. **Approved with no actionable findings.** The reviewer did not edit files or rerun tests. Approval is limited to the bounded source change and its evidence.

## Preservation and publication

- Captured 868 existing tracked/untracked/instruction-file hashes before editing. Exactly the two allowed production files changed; **866 unrelated files remain byte-identical**. The regression module is the only new implementation path.
- All 38 previously accepted Smart Bin paths remain unchanged. No Positioning, motion, Harvest or Smart Bin custody source changed.
- HEAD, branch and implementation index bytes are unchanged; index remains empty. Existing unrelated status entries are unchanged. Implementation changes remain uncommitted.
- The three previously blocked temporary Git index files were not opened, changed or deleted. Only fresh indexes in the isolated source repository were used.
- Operational database bytes were not read or hashed. No private configuration values, credentials, operational records or private evidence locations appear in this package.

`P2C6P-C01.patch` contains only the three listed paths and is relative to the accepted P2C6A tree. A fresh isolated index was loaded from that tree, the patch check/applied, and its resulting tree compared exactly. The manifest records before/after working-byte SHA-256 values and canonical Git blob IDs, accounting for Windows line endings.

Only `REVIEW.md`, `P2C6P-C01.patch` and `manifest.json` are committed from the existing chat-review publisher, whose parent is the accepted analysis commit. Publication uses an exact allowlist and a normal fast-forward push. The remote commit parent/path list and all three files are read back and byte/hash verified before returning the final SHA. The manifest hashes REVIEW and patch; its own hash is held in the separate publication receipt to avoid self-reference.

The initial publisher-wide staged whitespace check reported 12 single-space lines inside the new patch file. These are required unified-diff context markers for unchanged blank lines, not trailing spaces added to source. The actual accepted-to-result source diff passes `git diff --check`; REVIEW and manifest also pass their scoped staged check. The patch retains valid context and its exact reconstruction is verified independently.

### Git/object checks

```text
git --no-optional-locks rev-parse HEAD
git --no-optional-locks branch --show-current
git --no-optional-locks diff --cached --name-only
git --no-optional-locks status --porcelain=v1 -z
git --no-optional-locks ls-files -z --cached --others --exclude-standard
git show bc15b9a8007173b8ec49cccada7c992a85637def:chat_review/smart-bins/P2C6P-A/r1/comparison.json
git hash-object --path=<accepted-or-C01-path> --stdin
git show -s --format="%H %s" c95543a470a0f3678f95705743aa771f8e326301 7ce8c1d3d25ca89ecceba3a4e23d8b918c189326
git archive 8b4d95a94d25d5c6931b57e210bf3e87de8b7e22 software/sorter/backend
git show 2c8269843c16282659c9edbfbf03e7c7042a15ce:<each-retention-path>
git read-tree 8b4d95a94d25d5c6931b57e210bf3e87de8b7e22
git hash-object -w --stdin
git update-index --add --cacheinfo 100644 <C01-blob> <each-allowlisted-path>
git write-tree
git diff --binary --full-index --no-ext-diff <accepted-tree> <result-tree> -- <three-allowlisted-paths>
git apply --cached --check -
git apply --cached -
git diff --check <accepted-tree> <result-tree>
```

The archive/object mutations and all read-tree/update-index/apply commands ran only in the isolated source area with fresh indexes. Read-only Git/object/hash operations verified the implementation. Standard-library scripts recorded hashes, validated the literal allowlist and performed the AST comparison. Publication commands are recorded in the manifest.
