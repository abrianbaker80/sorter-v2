# RC01 r1 — immutable Smart Bins v0.3.0 RC1 source

## Materialization

The accepted RB01→RB05 source was materialized as commit `8ecf05c44877ddb9a45145b8dee511d6da5f1f43` on remote branch `smart-bins-v030-rc1`. Its single parent is pristine v0.3.0 `8951f914eb34778139b0df42f59f3957abbc2f8a`; its tree is the accepted RB05 tree `c8c43b0261c1d581575fcd65785140b416dc6d44`. GitHub's branch ref and commit API independently returned those exact values.

An isolated Git index applied the published RB01, RB02 (`--unidiff-zero`), RB03, RB04 and RB05 patches to the pristine parent. The intermediate trees were, in order, `ab7e7461a69efc1db74faac2c8e8f3318ca10341`, `df609a4d5c953f253737a927432ab3598647765e`, `b4cf20bd92855e2ccdb22748fcf952f19b169738`, `f388c5b91945e01a141100039f41f55d728a7331`, and `c8c43b0261c1d581575fcd65785140b416dc6d44`. All five matched their accepted result trees.

Every one of the final tree's 1,606 files matched the implementation checkout's working bytes. No implementation source was edited or staged in RC01. `git commit-tree` used that exact tree and one parent; `git update-ref` created a previously absent local RC1 branch, and a single non-force push created the remote RC1 branch. The pristine-to-RC1 comparison contains one commit and 66 changed paths, all under `software/sorter/backend/`. It contains no `chat_review`, firmware, `machine.toml`, generated evidence, or private/local paths. The count is provenance of the accepted delta, not a new source review. `git diff --check` passed. RB05's 197 tests, Ruff, pytest and builds were not rerun.

## Preservation

The integration checkout remained on `smart-bins-v030-integration` at HEAD `8951f914eb34778139b0df42f59f3957abbc2f8a`, with an empty real index and the exact RB05 working source bytes. The isolated index and plumbing commit did not change its branch, HEAD, index or files.

The old `sorting-flow-candidate-20260911` checkout was read only. Its observed branch is `sorting-flow-candidate`, HEAD is `c4e298ebd1a153001355df3d3b0c41cc5446ac8f`, and its real index is empty. Its working tree remains dirty by design.

**UNRESOLVED PRESERVATION METADATA:** RB03/RB04 recorded old-candidate inventory SHA-256 `d4ae072299bc0590115451435325d135ae22dc0b375d459b14827c45733af03e`; RB05 recorded `623b773072f5d76e81018229bedbb4759332d37c86c0e3230e3ab3d0016c79c1`. The available package records do not specify enough of both inventory serialization methods to establish whether the difference is algorithm, path set, canonical/filesystem bytes or mode inclusion. Current read-only inventory has 844 tracked and 25 untracked paths, consistent with the earlier recorded total of 869, but does not prove the two digest methods equivalent. Neither digest is used to qualify the RC source tree.

## Release gates

- **Source:** ACCEPTED / IMMUTABLY MATERIALIZED.
- **Ordinary v0.3.0 physical qualification:** NOT YET RUN.
- **Smart Bin physical receiving evidence:** NOT QUALIFIED.
- **Smart Bin continuous production:** BLOCKED.
- **P2C6B:** BLOCKED.

No deployment, live sorter access, schema initialization, hardware operation, firmware or `machine.toml` change, or physical qualification occurred. A separately authorized Stage 0 / Stage 1 task must preserve and verify the v0.2.9 baseline, run ordinary v0.3.0 smoke with Smart Bins absent, and only then run ordinary RC1 smoke with Smart Bins absent. Smart Bin activation additionally requires qualified physical receiving evidence and later guarded physical and continuous-operation acceptance. This commit grants no live activation authority.
