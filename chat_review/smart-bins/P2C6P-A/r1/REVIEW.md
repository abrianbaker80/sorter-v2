# P2C6P-A r1 — golden live baseline convergence review

**Analysis only. P2C6B remains blocked pending the source convergence and performance gates in [CONVERGENCE.md](CONVERGENCE.md).**

## Baselines verified

| Input | Verified result |
| --- | --- |
| Golden source | `basicallysource/sorter-v2@2c8269843c16282659c9edbfbf03e7c7042a15ce` |
| Golden parent | `367bbe593df561222635b806f323c9ac6903d4b7` |
| v0.2.9 delta | Only backend Tailscale router and its install test |
| Implementation branch | `sorting-flow-candidate` |
| Implementation HEAD | `c4e298ebd1a153001355df3d3b0c41cc5446ac8f` |
| Implementation index | Empty; captured index bytes preserved |
| Accepted package | `2952a9ac4d52ec9b5f0becd580c97f9512ce3853:chat_review/smart-bins/P2C6A/r1/` |
| Accepted result tree | `8b4d95a94d25d5c6931b57e210bf3e87de8b7e22` |
| Reconstruction | All 11 ordered patches applied/check-verified only in an isolated temporary index; all 38 accepted paths match implementation Git blobs |
| Historical candidate origin | Root snapshot `dca6ef67350942dd71a2e8a13ba7b1d0e3231ffc`; no Git merge base with golden source |

The comparison uses A (golden), B (clean HEAD) and C (accepted result). Exact path hashes and B→C slice attribution are in [comparison.json](comparison.json). CRLF differences are normalized for semantic comparison but retained in exact source blob/hash inventories. The complete changed production/configuration inventory assigns 140 paths, of which 136 count as runtime paths; counts are defined in the manifest and convergence report.

The installed stable version, 7–9 PPM continuous operation, firmware v0.8.1 / `8d560d26` and distribution-v1-2 are **user-reported protected references**. Source A was fetched and verified; no installation, operational database, live tuning, device or physical throughput was inspected. The short firmware hash was not resolvable in the isolated source objects, so no firmware-source compatibility claim is made from that hash. No firmware change is proposed.

## Major findings

1. **Ordinary mode semantics differ before Smart Bins.** Candidate TwoPiece and indexed selections both instantiate PhysicalC4Controller, with marker networking, ten-pocket custody and recovery requirements absent from golden TwoPiece. Preserve the required physical architecture through explicit selection; do not silently change what the golden configuration runs.
2. **Continuous-operation fixes are missing.** Candidate retention holds SQLite write transactions during filesystem deletion and directory enumeration. Its bus reader still uses pyserial read_until, while golden reads chunks. Upstream placed-head/READY/drop and clump-bucket invariants are missing. Candidate servo stop does not implement golden release behavior. These require targeted convergence, with genuine merges for ownership/routing paths.
3. **Candidate-only behavior is often intentional.** Retain physical custody, tracked command receipts, observed C3 recovery, full flap-route safety, provenance and atomic native completion. Candidate supervisor already satisfies the upstream SIGTERM intent with stronger restart fencing. Avoid wholesale file replacement or speculative firmware/tuning changes.
4. **Smart Bin cost has two different activation boundaries.** P1/P2B occupancy/eligibility work already runs in ordinary routing. The optional native bridge is not production-constructed; when injected, a successful reserved release has seven FULL write commits (eight including reservation), multiple readbacks and polling-dependent reads. Authorization scans all historical machine follow-ups, making growth a mandatory performance concern.
5. **P2C6A itself is inactive, but whole-candidate Harvest isolation is not complete.** B already constructs/queries legacy Harvest storage from ordinary routing and recovery. P2C6A adds no ordinary journal access, and empty-journal recovery requires no extension schema. Convergence must establish a real inactive boundary before claiming ordinary independence.
6. **Four recorded Harvest failures are stale contract expectations.** Exception-bin capacity, simulation/live progress separation and readiness/green-light behavior explain them. This is a source-grounded classification of previously recorded failures, not a fresh test run or broad Harvest qualification.

## Gate verdict and first slice

The read-only analysis is complete. Implementation, deployment, physical performance and P2C6B runtime integration are **not qualified** by this package. CONVERGENCE.md defines the source acceptance gates, ordered small slices, measurements and later separately authorized physical protocol. No physical test is required to complete this analysis.

Recommended first slice: **C01 — retention writer-lock and directory-enumeration convergence**, limited to `channel_crop_store.py` and `piece_image_store.py`, with upstream locking tests and focused crash/replay coverage. **GPT-6 Sol, high reasoning.** This addresses an evidenced shared-database stall mechanism without changing motion or firmware.

## Preservation and validation scope

- Captured and rechecked 868 existing tracked/untracked/instruction-file hashes, full status, HEAD, branch and implementation index bytes. All unchanged. The working modifications remain uncommitted and the index remains empty.
- The three previously blocked temporary index files were not opened, changed or deleted. This is a no-access statement, not a new hash-verification claim.
- Fetched golden into an isolated temporary repository. Candidate objects were available read-only through an alternate object directory. Reconstruction wrote only that repository's fresh index/object store.
- Read all required instruction files and source/contracts relevant to the comparison. Kept the report public-safe: no tuning values, credentials, operational records or private evidence paths.
- No backend imports, pytest run, schema initialization, operational DB connection, provider call, deployment, service control, firmware operation, hardware motion or physical benchmark. Static parsing and source/object/hash checks do not substitute for regression tests.
- Prior P2C6A results (236 passed in the requested regression group; 65 passed / 4 failed in the affected Harvest group; pre-slice 9 passed / 4 failed) are cited as historical evidence only. No new pass total is asserted. Independent agent review was not run; this package received source-backed self-review.

## Commands and checks performed

Commands below use `$impl`, `$isolated`, `$publisher` for the implementation checkout, disposable source repository and existing chat-review publisher respectively. These labels intentionally omit private absolute locations; exact public Git refs and path arguments are retained. Native file reads used `Get-Content -LiteralPath`, `rg -n` and `rg --files`; consulted source paths are exhaustively inventoried in comparison.json.

```text
git --no-optional-locks rev-parse HEAD
git --no-optional-locks branch --show-current
git --no-optional-locks diff --cached --name-only
git --no-optional-locks status --porcelain=v1 -z
git --no-optional-locks ls-files -z --cached --others --exclude-standard
git worktree list --porcelain
git remote -v
git show 2952a9ac4d52ec9b5f0becd580c97f9512ce3853:chat_review/smart-bins/P2C6A/r1/manifest.json
git show 2952a9ac4d52ec9b5f0becd580c97f9512ce3853:chat_review/smart-bins/P2C6A/r1/REVIEW.md
git init -q                         # only in $isolated
git fetch --no-tags https://github.com/basicallysource/sorter-v2.git 2c8269843c16282659c9edbfbf03e7c7042a15ce
git read-tree c4e298ebd1a153001355df3d3b0c41cc5446ac8f
git show <manifest-chain-commit>:<manifest-package>/<patch>
git apply --cached --check -        # fresh isolated index only, for each patch
git apply --cached -                # same isolated index; never implementation
git write-tree                     # checked at every reconstructed stage
git hash-object --path=<accepted-path> --stdin
git rev-parse 8b4d95a94d25d5c6931b57e210bf3e87de8b7e22:<accepted-path>
git ls-tree -r <A-or-B-or-C>
git cat-file blob <enumerated-blob>
git diff --no-ext-diff <A-or-B> <B-or-C> -- software/sorter/backend
git diff --ignore-space-at-eol --numstat 2c826984 c4e298eb -- software/sorter/backend <scope-exclusions>
git diff --ignore-space-at-eol --unified=1 2c826984 c4e298eb -- <inspected-production-paths>
git show -s --format='%H %P %s' 2c8269843c16282659c9edbfbf03e7c7042a15ce
git diff-tree --no-commit-id --name-only -r 2c8269843c16282659c9edbfbf03e7c7042a15ce
git log --format='%h %ad %s' --date=short -55 2c8269843c16282659c9edbfbf03e7c7042a15ce
git log --format='%h %ad %s' --date=short -12 c4e298ebd1a153001355df3d3b0c41cc5446ac8f
git merge-base c4e298ebd1a153001355df3d3b0c41cc5446ac8f 2c8269843c16282659c9edbfbf03e7c7042a15ce
git show -s --format=full dca6ef6
git show --format='%h %s' --stat 042752f6 88ed1218 eebd0f0a c95543a4 7ce8c1d3 1549037e c2c09888 94622df0 1236bc94
git cat-file -t 8d560d26             # not available; firmware not fetched/modified
gh api repos/abrianbaker80/sorter-v2/git/ref/heads/chat-review --jq .object.sha
git -C $publisher add -- chat_review/smart-bins/P2C6P-A/r1/REVIEW.md chat_review/smart-bins/P2C6P-A/r1/CONVERGENCE.md chat_review/smart-bins/P2C6P-A/r1/comparison.json chat_review/smart-bins/P2C6P-A/r1/manifest.json
git -C $publisher diff --cached --name-only
git -C $publisher diff --cached --check
git -C $publisher diff --cached --stat
git -C $publisher commit -m "Review golden SorterOS convergence and performance gates"
git -C $publisher push origin HEAD:chat-review
gh api repos/abrianbaker80/sorter-v2/commits/<published-SHA>
gh api repos/abrianbaker80/sorter-v2/contents/chat_review/smart-bins/P2C6P-A/r1/<each-of-four-files>?ref=<published-SHA>
```

Python standard-library scripts performed SHA-256 inventory, normalized diffs, AST-only syntax checks and exact manifest/allowlist validation. They did not import backend modules. Reconstruction initially failed because Windows text writing added CRLF to the disposable Git alternates file; writing that file with LF corrected the isolated setup. An initial upstream object lookup in the implementation repository correctly reported it absent. A few exploratory relative-path/commit lookups used the wrong source directory and were repeated in the verified isolated repository; no failed lookup was treated as evidence that a production component is absent.

## Publication

Only four new files under `chat_review/smart-bins/P2C6P-A/r1/` are eligible for commit/push: REVIEW.md, CONVERGENCE.md, comparison.json, manifest.json. No implementation patch is included. The existing isolated chat-review publisher is used with an explicit allowlist and a normal fast-forward push. The remote ref, commit path list and every file are read back and byte/hash compared; the final response supplies the published SHA and commit-pinned links. The manifest hashes the other three package files; its own hash is verified in the publication receipt to avoid a self-referential hash.
