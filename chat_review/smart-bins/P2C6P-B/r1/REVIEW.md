# P2C6P-B r1 — architecture rebaseline review

## Recommendation

Use **OPTION 2**: a new clean integration base from exact v0.3.0. Use physical
owner **A**: native pulse-perception/two-piece sorting as the sole physical owner,
with a narrow durable Smart Bin adapter. This recommendation is conditional on
accepted native release/receiving evidence and recovery contracts; unmodified
v0.3.0 does not already satisfy them.

The first implementation slice is **RB01 — clean v0.3.0 base and MCU safety port**,
assigned to **GPT-6 Sol, High**. Its only production edit is
`software/sorter/backend/hardware/bus.py`. Preserve B's chunk reader; port D's
no-resend, validation and unresolved-address semantics. C01 behavior is already
upstream. See [ARCHITECTURE.md](ARCHITECTURE.md) for the complete RB01–RB05 plan.

## Baseline verification

| State | Exact ref | Exact tree |
| --- | --- | --- |
| A | `2c8269843c16282659c9edbfbf03e7c7042a15ce` | `26269348312cd4154140828a82c6f5e0a7325bd7` |
| B | `8951f914eb34778139b0df42f59f3957abbc2f8a` | `21e805b60da575ff7468fcc3570a517d80f01cd0` |
| C | `c4e298ebd1a153001355df3d3b0c41cc5446ac8f` | `8d5b04fc56682b0d0a6daab131cd039eedf08aeb` |
| D | `615cb66d5d78e4f5737564f3e81b82a1c47ee9cf` | `615cb66d5d78e4f5737564f3e81b82a1c47ee9cf` |

Candidate remains `sorting-flow-candidate` at C with an empty index. All **869**
tracked/nonignored untracked working files match the accepted D result after Git
canonicalization; all original raw bytes are unchanged throughout this analysis.
The complete 13-patch accepted chain was reconstructed from C using a new isolated
Git index. Every published patch SHA-256 and intermediate result tree matched,
including P2C6A, C01 and C02. No patches touched the working checkout.

Four whole-tree inventories contain A=1449,
B=1569, C=844,
D=869 blob paths. The relevant comparison covers
**77 paths**, all **29 accepted production changes**, **24 custom modules**
plus one inherited legacy helper, and renamed native-flow paths. Full inventory
digests, exact hashes and detailed classifications are in
[comparison.json](comparison.json) and [manifest.json](manifest.json).

## Major findings

1. **The native drop slot is not physical proof.** Transport advances after a
   350 ms track absence, a 15-second timeout even if still visible, or before a
   stall-clear sweep starts. The latter two branches already existed in A. B
   Sending then marks software completion after settling; there is no inspected
   receiving sensor. A slot change or timer must not mint Smart Bin exit/delivery.
2. **The old physical topology predates Smart Bins.** C already selects
   PhysicalC4Controller for both old classification modes. Preserve only needed
   semantics, not its ten-pocket/marker architecture for its own sake. Counts:
   REQUIRED_NOW **2**, USEFUL_BUT_SIMPLIFIABLE **7**, DEFERRED_EXPERIMENT **9**,
   OBSOLETE_FOR_CURRENT_ARCHITECTURE **3**, SMART_BIN_ADAPTER_ONLY **3**. Required
   residue is narrow flap-route checking and truthful accepted/rejected/unknown
   hardware command state. Controller state still is not a receiving sensor.
3. **A native adapter needs new typed evidence.** D requires marker/pocket proof
   and its completion adapter derives actual fields from intended route plus
   marker/settle. Do not fabricate those fields on B. Retain capacity on ambiguous
   release or receiving evidence, preserve original intent separately, and never
   redispatch motion to repair persistence.
4. **Release coverage begins before Coordinator.** Startup doors/purge/homing,
   staging, manual motion and stall clearing can affect held pieces. Inspect
   durable claims before these actions and use one physical-owner fence. Keep
   B's positioned-UUID READY race and matching drop-identity protections.
5. **Clean-base porting is smaller.** B/C differ in 98 shared production Python
   paths, with 136 candidate-only paths. The C→D accepted work changes only 29
   production paths. C01 is already AST-equivalent upstream; C02 safety needs a
   bounded merge. Do not mechanically replay old patches onto B.
6. **Harvest is optional custom application code, absent from both A and B.**
   The inactive local P2C6A journal can survive, while its optional store API
   integration remains deferred. Ordinary sorting must require no Harvest
   application/database/provider; its bundled local journal/hash helper is not
   an external store dependency. No P2C6B wiring is proposed here.
7. **Platform compatibility is manageable but must retain durability.** B keeps
   WAL/NORMAL/5-second busy timeout, adds power-stress tables and retains the
   centralized TOML resolver. Preserve Smart Bin FULL/FK commits and atomic
   history delivery. Legacy mode blocks are ignored; pulse keys have explicit
   in-memory migration; split cameras and carousel aliases remain supported.
8. **Firmware is unchanged.** B's entire firmware subtree equals firmware
   `8d560d26b60b09145d0c7c62ac81f2fb3c22a60c`:
   `06181d87b2258fcce1581145cea672daaa2b5702`. No flash is indicated.
9. **Hot-path cost is unmeasured.** Eligibility projects growing cycle/group
   history; completion authorization scans historical followups repeatedly, with
   reconciliation/blocker amplification. The plan replaces unbounded per-piece
   scans without removing current blockers or FULL durability. No PPM impact is
   claimed. Supplied 7.18–7.44 PPM windows remain the protected empirical golden.

## Gate verdict

**Analysis-only publication complete; RB01 is proposed next.** Source convergence,
native adapter/receiving-evidence acceptance, recovery and separately authorized
physical qualification are still required. P2C6B remains **BLOCKED**. The old
C03–C13 sequence is superseded. No implementation changes, schema initialization,
runtime test, deployment, service action, firmware operation, live access or
hardware motion occurred. No implementation patch is included.

## Exact checks and command forms

The immutable source, not imported runtime code, was inspected. Commands used:

```text
git --no-optional-locks rev-parse HEAD
git --no-optional-locks branch --show-current
git --no-optional-locks diff --cached --name-only
git --no-optional-locks status --porcelain=v1 -z
git --no-optional-locks ls-files -z --cached --others --exclude-standard
git --no-optional-locks ls-tree -r -z <each A/B/C/D ref>
git --no-optional-locks show <exact ref>:<explicit path>
git --no-optional-locks cat-file --batch
git --no-optional-locks hash-object --path=<path> --stdin
git --no-optional-locks read-tree c4e298ebd1a153001355df3d3b0c41cc5446ac8f
git --no-optional-locks apply --cached -
git --no-optional-locks write-tree
git --no-optional-locks diff <A> <B> -- <explicit subsystem/config paths>
git --no-optional-locks diff <C> <D> -- <explicit accepted paths>
git grep -n -E <pattern> <ref> -- <explicit paths>
git show -s --format=%H%n%P%n%s <four integration commits>
rg -n <symbol pattern> <isolated source paths>
```

The read-tree/apply/write-tree commands used only the newly allocated isolated
index. The three previously blocked indexes were not accessed. Python stdlib
scripts hashed source bytes, compared inventories/ASTs, checked JSON and citation
ranges, and assembled these four analysis files; they did not import the backend,
run pytest or initialize SQLite. C02's 24/56 tests are historical source evidence,
not new results or acceptance of the proposed architecture. No network source
fetch was needed during this analysis; upstream objects were already isolated.

Verified integration commits: `0c543c7ceb28444de39360c3baacaf9ea36fad9a`
(single feeder/classification flow), `ea6a4c647b682dff3253d833b45536a6cf86db50`
(single setup), `102f787aa51d651967b48e3039063f9cad4e1a44`
(legacy perception/camera removal), and B merge `8951f914...` (PR #867).
Final publication uses the existing isolated chat-review publisher, an exact
four-file allowlist, normal fast-forward push, and remote ref/parent/file-byte
readback. Implementation HEAD, branch, empty index and all 869 original file
hashes are checked again after publication.

Independent final reviews passed for native flow/evidence, custom component and
accepted-change coverage, and platform/Harvest/privacy. All 51 structured native
citation ranges, 104 custom citation ranges and 64 platform source-object checks
passed. No backend tests were run by these reviews.

Publication checks use these exact command forms in the isolated publisher:

```text
git --no-optional-locks add -- chat_review/smart-bins/P2C6P-B/r1/REVIEW.md chat_review/smart-bins/P2C6P-B/r1/ARCHITECTURE.md chat_review/smart-bins/P2C6P-B/r1/comparison.json chat_review/smart-bins/P2C6P-B/r1/manifest.json
git --no-optional-locks diff --cached --name-only
git --no-optional-locks diff --cached --check
git --no-optional-locks show :<each allowlisted path>
git --no-optional-locks commit -m "Document v0.3.0 architecture rebaseline and minimum Smart Bin adapter"
git --no-optional-locks diff-tree --no-commit-id --name-only -r <published SHA>
git --no-optional-locks push origin HEAD:chat-review
gh api repos/<owner>/<repository>/git/ref/heads/chat-review
gh api repos/<owner>/<repository>/commits/<published SHA>
gh api repos/<owner>/<repository>/contents/<each allowlisted path>?ref=<published SHA>
```

Each remote content response is base64-decoded and compared byte-for-byte to the
reviewed local artifact. The manifest hashes its three sibling files; the private
publication receipt hashes all four, including the manifest itself.
