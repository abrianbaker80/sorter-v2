# RB02 r1 — dormant Smart Bin persistence on v0.3.0

## Result and qualification

The expanded RB02 source/database contract slice is implemented in the existing integration checkout. The exact result tree is `df609a4d5c953f253737a927432ab3598647765e`; patch base is accepted RB01 tree `ab7e7461a69efc1db74faac2c8e8f3318ca10341`. The patch contains **13 production paths and 11 test/support paths**, and no runtime wiring.

The final bounded frozen Python 3.12.12 run passed **256 tests**. These use disposable SQLite databases and synthetic historical custody/evidence records. This establishes source/database behavior only. Historical marker/pocket fields and names such as `complete_native` do not establish qualified native v0.3.0 physical evidence. Native v0.3.0 remains the sole physical owner, with no Smart Bin service installed in its runtime.

Ruff ran on all 24 changed/new Python paths. There are **zero new findings** and **one preserved upstream E741** in `local_state.get_current_bin_contents_snapshot`: `sum(int(l["piece_count"] or 0) for l in layers)`. Pristine B reports the identical diagnostic at line 1970; the insertion moves it to line 2007. No rule is suppressed, and the raw Ruff exit code is 1. The unrelated upstream function is unchanged, consistent with the port-only requirement. Both working diff and exact patch-tree `git diff --check` pass.

## Bases and B-versus-D semantic port

- Pristine v0.3.0 commit: `8951f914eb34778139b0df42f59f3957abbc2f8a`, tree `21e805b60da575ff7468fcc3570a517d80f01cd0`.
- Accepted RB01 review: `8c83a0ceb261f2e071b0b4dccff6765b0fb12e1f`, result `ab7e7461a69efc1db74faac2c8e8f3318ca10341`.
- Old pre-Smart-Bin C tree: `8d5b04fc56682b0d0a6daab131cd039eedf08aeb`.
- Accepted old semantic/test donor through C02: `615cb66d5d78e4f5737564f3e81b82a1c47ee9cf`.
- Accepted P2C6P-B r1 architecture rebaseline: `d451afdb5a93d3e3b7bc8d84c57a15f005b23f7b`.

Before the initial port, all working blobs plus repository file modes reconstructed exact RB01. The continuation rechecked the stopped partial inventory, all three RB01 files, HEAD/branch/index and old candidate before editing. No reset, stash, clean, or re-port of completed files occurred.

| Allowlisted module | B versus D / accepted delta | Dependencies and decision |
| --- | --- | --- |
| local_state | C to D adds only get_current_bin_occupancy_evidence | Insert that function into B; retain every existing B function unchanged. |
| piece_records | C to D extracts recordPieceOnConnection and adds initialize_piece_records | Extract B's own writer SQL and values; retain all B schema and other APIs. |
| smart_bins_eligibility | Absent in B/C; accepted pure classification and capacity rules | stdlib only; retain. |
| smart_bins_storage | Absent in B/C; explicit v2 ledger, FULL/FK critical transactions | local_state._connect exists in B. Critical window overrides NORMAL, then restores supplied connections. |
| smart_bins_migration | Absent in B/C; explicit deterministic opening balances and provenance | local_state path and allowlisted storage only. |
| smart_bins_service | Absent in B/C; dormant preview/reserve/cancel/receipt contracts | B irl.bin_layout._parseLayersDict is retained. Its eager package initializer required the explicitly authorized IRL repair below. Other direct dependencies are allowlisted. |
| smart_bins_delivery | Absent in B/C; historical custody/exit/completion data, atomic history | Allowlisted modules only. Marker-form fields remain historical contracts, with no claim of native v0.3.0 evidence qualification. |
| smart_bins_completion_recovery | Absent in B/C; receipt, follow-up, hold and phase persistence | Allowlisted service/storage and lazy follow-up projection only. No native-completion adapter import. |
| smart_bins_reconciliation | Absent in B/C; observed delivery/removal/uncertainty data decisions | Allowlisted modules only; no physical bridge. |
| smart_bins_followup_reconciliation | Absent in B/C; evidence attribution and retained obligations | Allowlisted modules only. Preserve historical scans; no RB04 optimization. |
| smart_bins_harvest_integration | Absent in B/C; local operation journal, observations and receipts | Local modules and canonical/hash helpers only; no Harvest application call. |
| harvest_integration_storage | Absent in B/C; canonical/hash plus optional separate-store helpers | Three project_harvest_projects imports are inside explicitly called separate-store helpers. Retain them lazy and uncalled; B intentionally lacks that application. |
| irl/__init__.py | B eagerly exports four config and two motor symbols | Authorized continuation adds lazy real-object exports; no config/bin-layout/hardware source change. |

All ten new Smart Bin/local Harvest modules retain the donor's function/class ASTs. The delivery module docstring explicitly disclaims native v0.3.0 qualification; the service drops one unused typing import. No old physical module is introduced. The existing `toml_config -> server.config_helpers` dependency is a pre-existing inert serialization helper used by B local_state; its import performs no configuration write or router registration. It remains unchanged. No new forbidden runtime dependency remains.

### Exact local_state delta

Insert only `get_current_bin_occupancy_evidence` immediately before `get_current_bin_contents_snapshot`. It opens one explicit read transaction, reads active-session counts and all aggregate categories, includes aggregate-only coordinates with a zero recorded count, and preserves count/category disagreement. Invalid non-positive aggregate rows remain refusal evidence. It creates no piece UUID, receiving destination or Smart Bin schema. It retains the accepted call to the ordinary legacy initializer when explicitly invoked.

Removing this one new function from the parsed module yields B's exact whole-module AST. Centralized `machine_toml_path`, WAL keeper, local-state schema v5, power-stress tables/APIs, existing migrations, NORMAL ordinary synchronization and 5-second connection/busy timeout remain unchanged. Tests confirm those settings and use a concurrent WAL writer between the two occupancy SELECTs to prove the returned evidence belongs to one snapshot.

### Exact piece_records delta

Add `initialize_piece_records`, which explicitly delegates to B's `_ensureInitialized`. Extract B's own `recordPiece` upsert into `recordPieceOnConnection(conn, ...)`. The helper opens no connection, initializes no schema, and commits nothing. `recordPiece` retains its public signature and ordinary committing behavior by calling the helper through its existing connection context. The existing schema initializer and every other original function remain unchanged.

The helper's SQL call and bound-value AST match B exactly. No old-candidate transfer or Harvest history columns were ported. Tests cover caller transaction visibility, commit, explicit rollback, exception rollback, correction-field preservation, original ordinary writer use, and no second connection while the helper runs.

### Deliberately rejected donor divergence

Do not port C's alternate machine-params resolver, path-aware keeper/replacement cleanup API, assignment-selection clearing API, machine-params cleanup changes, initialization/cache changes, or removal of B power-stress functions. Test cleanup closes B's private keeper only in test support.

Do not port old piece-record `transfer_episode_id`, `transfer_first_pass`, `transport_failure_reason`, `forced_reject_reason`, `reject_category`, `physical_group_size_unknown`, or `harvest_exception` schema/upsert/read changes. Do not port old aggregate/value cache rewrites. Keep B's schema and metadata/correction behavior.

## IRL package import boundary

Pre-fix chain: `smart_bins_service -> irl.bin_layout -> irl/__init__.py -> irl.config -> hardware.bus`. A fresh pre-fix reproduction with the hardware guard raised exactly `AssertionError: RB02 forbidden runtime import: hardware`. The initial source review had checked the parser's direct imports but missed package initialization; the continuation explicitly authorized only `irl/__init__.py` as an added production path.

`_parseLayersDict` itself needs no hardware. The initializer now declares the same six exports in `__all__`, maps four config exports to `irl.config` and two motor exports to `hardware.sorter_interface`, and implements PEP 562 `__getattr__`. Explicit access imports the real defining module and caches the exact object in module globals. Unknown attributes raise `AttributeError`. `__dir__` exposes the compatibility names without resolving them.

Nine fresh-process tests show that `import irl` and `import irl.bin_layout` succeed with config/hardware/machine-platform imports forbidden. Explicit requests for IRLConfig, IRLInterface, mkIRLConfig, mkIRLInterface, StepperMotor and ServoMotor each return the identical real defining-module object. Repeated access is stable and does not call the lazy loader again. Compatibility-test child processes prohibit SQLite/network I/O and hardware/controller construction; they import real definitions but operate no hardware.

No `irl.config`, `irl.bin_layout`, machine-platform, or hardware production source was changed by RB02. The inherited RB01 hardware change remains byte-identical. Native modules still import their defining modules directly. No import-loader bypass, parser duplication, proxy class, or runtime ordering change was introduced.

## Durable contracts and evidence

### Schema/init and critical transactions

Smart Bin ledger version 2 is separate from local-state version 5. Migration/service/recovery/follow-up/local-journal extensions retain their accepted explicit version checks. Initialization is an explicit call requiring previously initialized local state; imports and reads never install Smart Bin schema. Unknown/incomplete versions refuse without repair.

Critical transactions use the same local-state SQLite authority, FK ON, synchronous FULL, and BEGIN IMMEDIATE. The caller commits explicitly; exceptions and uncommitted exits roll back. Nested use refuses without committing or rolling back the caller's active transaction. Supplied connections remain open and restore their original synchronous/FK values, including an originally disabled FK pragma. Tests trace BEGIN IMMEDIATE and check restoration after both commit and injected failure. Ordinary local_state remains NORMAL/WAL.

### Reservations, capacity, migration

Capacity counts recorded contents plus RESERVED, RELEASE_INTENT, EXIT_CONFIRMED and UNCERTAIN claims; COMPLETED and CANCELLED no longer hold additional capacity. Tests cover each held state, last-unit competition, fill-cycle identity, cross-policy/group claims, assignment sharing/reset without loss of contents, request replay and payload conflict. Session/profile changes preserve physical contents.

The migration suite represents aggregate-only history as explicit opening balances, with no invented piece/reservation/delivery identity or receiving evidence. Deterministic planning and exact replay are checked, as are stale plans, missing/conflicting history, aggregate/count mismatches, carry-forward lineage, duplicates across native/historical origins, and unchanged legacy rows/settings/corrections.

### Atomic history and delivery

Delivery, reservation transition, durable evidence, canonical receipt and history use one critical connection. Synthetic failure injection at intent, exit, history and final receipt writes proves rollback of all effects. A follow-up link failure also rolls back delivery/history/completion evidence/receipt. Independent readers cannot observe staged history/ledger effects; successful outer commit persists both. Exact completion replay does not duplicate delivery or capacity.

Intended and actual destinations remain separate. Mismatch records a discrepancy and UNCERTAIN hold without credit; reconciliation credits only an explicitly evidenced actual destination. Missing/stale/wrong identity evidence cannot free capacity. A changed completion payload with a linked follow-up retains the donor's refusal exception (`CompletionRecoveryError`); the original receipt remains replayable and history unchanged.

### Reconciliation and recovery/follow-up

The dormant reconciliation suite covers COMPLETED/CANCELLED/UNCERTAIN decisions, affirmed non-arrival before cancellation, immutable original intent, actual-destination credit, stale revisions/previews, contradictory evidence, terminal contradictions, and atomic receipts/history. No timeout/restart/missing RAM state is treated as proof of emptiness or delivery. Cancellation does not subtract fabricated contents.

Recovery tests directly persist synthetic handoffs and transitions. No Physical C4, FIFO, marker positioner, native completion adapter, Sending state machine or physical bridge is imported. Retained, delivery-pending, attempted-publication and publication-succeeded records remain restart obligations until valid closure. Missing history and later holds still block closed records. An old receipt cannot clear newer contradictory evidence or a predecessor obligation. Restart/readback creates no additional release attempt.

Follow-up tests preserve PUBLICATION_CALLBACKS, GATE_OPEN, ADMISSION_RELEASE and UNKNOWN phase attribution, exact receipt suffixes, producer-effect evidence, missing-close obligations, current contradiction rules and owner-requalification requirements. Gate observations cannot substitute for callback proof; historical unknown phases cannot be relabeled. Evidence-only reconciliation preserves quantities, delivery/history and physical-release prohibition. Historical scans are retained; RB04 performance work is not started.

### Dormant Harvest local journal

Only the local journal and canonical/hash helper modules are introduced. Tests use detached, explicitly synthetic readback payloads, never a Harvest application or external store. They cover deterministic strict hashes, durable intent, exact receipts, payload conflicts, stale/ambiguous acknowledgements, latest-readback requirements, contradiction holds, atomic local rollback and caller-owned FULL/FK completion-journal staging. A second writer can acquire the local database after intent returns: no distributed transaction is assumed.

The optional Harvest-side imports inside `harvest_integration_storage` remain lazy and uncalled. The local test fixture asserts no project_harvest module was imported and no separate Harvest schema appeared. No provider, project runtime, application endpoint or UI integration was ported or tested.

### Import side effects and isolation

A fresh-process import test imports all 12 persistence modules with SQLite connections and network I/O prohibited, hardware/config/machine-platform imports prohibited, and project/physical runtime imports prohibited. It passes without initialization. The original guarded storage wrapper still rejects hardware and external Harvest imports; only its Windows file-URI decoding was corrected to canonicalize disposable paths correctly. A separate guarded collection run collects 100 storage/migration/reservation cases and confirms no SQLite database was created.

All database connections in the bounded parent process are audited to remain beneath a freshly created `sorter-rb02-tests-*` temporary directory (or in-memory). No default production DB path, live database or machine configuration was used. Exact guard source, command outputs and disposable paths are embedded in the manifest.

## Validation

Working directory: `software/sorter/backend`. Frozen v0.3.0 Python 3.12.12 environment; UV offline and automatic pytest plugin loading disabled. The evidence wrapper invokes pytest under network, runtime-import and SQLite-path guards. No dependency or lockfile changed.

```text
uv run --frozen --offline python "C:/Users/abria/OneDrive/Documents/ChatGPT/Sorter Bin Assignment/rb02_evidence/run_tests.py" tests/test_irl_import_boundary.py -q
```

**9 passed**; isolated real-export identity checks and hardware-free package/parser imports.

```text
uv run --frozen --offline python "C:/Users/abria/OneDrive/Documents/ChatGPT/Sorter Bin Assignment/rb02_evidence/run_tests.py" tests/test_smart_bins_storage.py tests/test_smart_bins_migration.py tests/test_smart_bins_reservations.py -q
```

**98 passed**; final set adds two supplied-connection restoration cases.

```text
uv run --frozen --offline python "C:/Users/abria/OneDrive/Documents/ChatGPT/Sorter Bin Assignment/rb02_evidence/run_tests.py" tests/test_smart_bins_delivery.py -q
```

**23 passed**.

```text
uv run --frozen --offline python "C:/Users/abria/OneDrive/Documents/ChatGPT/Sorter Bin Assignment/rb02_evidence/run_tests.py" tests/test_smart_bins_completion_recovery.py tests/test_smart_bins_reconciliation.py tests/test_smart_bins_followup_reconciliation.py -q --tb=short
```

**107 passed**.

```text
uv run --frozen --offline python "C:/Users/abria/OneDrive/Documents/ChatGPT/Sorter Bin Assignment/rb02_evidence/run_tests.py" tests/test_smart_bins_harvest_local.py -q --tb=short
```

**11 passed**; no external Harvest application.

```text
uv run --frozen --offline python "C:/Users/abria/OneDrive/Documents/ChatGPT/Sorter Bin Assignment/rb02_evidence/run_tests.py" tests/test_smart_bins_v030_contracts.py -q --tb=short
```

**6 passed**.

```text
uv run --frozen --offline python "C:/Users/abria/OneDrive/Documents/ChatGPT/Sorter Bin Assignment/rb02_evidence/run_tests.py" tests/test_smart_bins_storage.py tests/test_smart_bins_migration.py tests/test_smart_bins_reservations.py --collect-only -q
```

**100 collected**, no SQLite databases created.

```text
uv run --frozen --offline python "C:/Users/abria/OneDrive/Documents/ChatGPT/Sorter Bin Assignment/rb02_evidence/run_tests.py" tests/test_irl_import_boundary.py tests/test_smart_bins_storage.py tests/test_smart_bins_migration.py tests/test_smart_bins_reservations.py tests/test_smart_bins_delivery.py tests/test_smart_bins_completion_recovery.py tests/test_smart_bins_reconciliation.py tests/test_smart_bins_followup_reconciliation.py tests/test_smart_bins_harvest_local.py tests/test_smart_bins_v030_contracts.py -q --tb=short
```

**256 passed in 39.22s**, final combined affected set run once.

The first guarded rerun collected successfully but had 41 test failures caused by the wrapper interpreting Windows `file:///C:/...` URIs as UNC paths (57 passed). Correcting URI decoding retained the same containment check and yielded 98 passed. New recovery fixtures initially had two expectation/key-format errors; corrected fixtures yielded 107 passed. New preservation fixtures initially omitted `distributed_at`, omitted proxy delegation, and over-broadly banned B's inert config serializer; corrected fixtures yielded 6 passed. These were test integration corrections, not suppressed production failures. Full records remain in the manifest.

After the final combined pass, only an extra blank line at EOF in the new follow-up test was removed for exact-tree diff-check; executable code was unchanged. No full backend suite, old Harvest endpoint suite, hardware/device test or deployment validation ran.

## Exact changed paths and SHA-256

Paths below are relative to `software/sorter/backend/`. Before hashes are accepted RB01 canonical blobs; after hashes are working bytes. Canonical Git blob hashes and SHA-256 are also in the manifest.

| Path | Before SHA-256 | After working SHA-256 |
| --- | --- | --- |
| `local_state.py` | `5b512cfd2e6bd7b1a40091c79cad2edb1d86d33853085f32050cb94bad5f3427` | `fb38bf20456b065a2195bd27cb2b1e5c7368b742d372555750c419945aaa707d` |
| `piece_records.py` | `fbced7a503c9c53b6fcde3fdcb8d60f53625bd04a0b05207a74c15523224585b` | `4ae7741184b84a5b1034cd5d92c72490229f92c965495329b0a0c6a551fd821f` |
| `smart_bins_eligibility.py` | `absent` | `c7dd4984d2a1cff480167ecee1e964e8c2979dd87a798531879395eda4dd3341` |
| `smart_bins_storage.py` | `absent` | `923b07906a19c9e730cb9511aaa51b9a774caaff2fb6b299a2bd4ccb07514373` |
| `smart_bins_migration.py` | `absent` | `3f746cc59cee04e1c2d82f0c6b903a7dbc9db5f7822635ad4a11c282840516b9` |
| `smart_bins_service.py` | `absent` | `860bf13a3f11fb1c26fc4db3006b8b9c4efe0a0011863b7b6839f8f2e40b399b` |
| `smart_bins_delivery.py` | `absent` | `bd1d927242e25810922bbaa7974184f53e7c951cf0b366ab953685695331844f` |
| `smart_bins_completion_recovery.py` | `absent` | `62a8dc7b27dbdd9df0e1bfb01bc26e317e868cedc4980dcd20ef8e31ec81eb29` |
| `smart_bins_reconciliation.py` | `absent` | `326c4f3014cf7b20b37ed1433e576e3b4fff413e2bf126c06a7640327027f53b` |
| `smart_bins_followup_reconciliation.py` | `absent` | `55589796207d45935250aab44c5dfde6bf3974683f7268eff3d6e4325925f8f0` |
| `smart_bins_harvest_integration.py` | `absent` | `5831e136cf16a5b29a76e4edacf637ff5fd8be2bb7a3d7e26bab307e7635d00f` |
| `harvest_integration_storage.py` | `absent` | `a4d0c6d9ada86842ceb8ea7c10bbead8bcd21d4dc562f5764cfc424a59b313c0` |
| `irl/__init__.py` | `aac3850ccbbd639f24c6764cd25951d32c74f89efa21b4218ce94e7f67e4eb45` | `8cfe4cd1a00bbcbabfa1078f6ed9a7fec75399b29bf35cd1f8302f81e56cf1f1` |
| `tests/smart_bins_test_support.py` | `absent` | `740bd2cd215bf1c30ebf011735f85602e85b7b28887e0d6eeedb39ba5a516a9f` |
| `tests/test_irl_import_boundary.py` | `absent` | `68e6385150c659cf739e5d34a444a09e989f37d5592da3dcac24aecac4198736` |
| `tests/test_smart_bins_storage.py` | `absent` | `1995e071cbd8c02a6c51bf84696fb8b7cdb5825e446181973a185cd0e1300e8f` |
| `tests/test_smart_bins_migration.py` | `absent` | `83ab86e2457a930abfa53b91737e37ab3e13aa4dd24e859d83cb1d503cc91ac1` |
| `tests/test_smart_bins_reservations.py` | `absent` | `a63d5339c6c50344e083e6f12e6a776c1241e1d1a03de51f7cb2beac72210b39` |
| `tests/test_smart_bins_delivery.py` | `absent` | `ba5e02fca8753da1762ebd6e78b29b94914490d4077b5e45631319367abb7883` |
| `tests/test_smart_bins_completion_recovery.py` | `absent` | `34f6c0b05320083ec51da85a395b795bc98f0ece78bd5d3a6b97cfb7780915ee` |
| `tests/test_smart_bins_reconciliation.py` | `absent` | `49b9071d06cccbef707291f5cea6d24e9fcd338c8d8ba7c109a86473f59232b6` |
| `tests/test_smart_bins_followup_reconciliation.py` | `absent` | `575386eb1ef91378cc3b2744b0436d52a3562e1a37d5486c0f0e479d0d03e1e5` |
| `tests/test_smart_bins_harvest_local.py` | `absent` | `a3d82d005598c5f67cdd98679d684b25fb83099a161dedfb8bfe241fcfb19b77` |
| `tests/test_smart_bins_v030_contracts.py` | `absent` | `2f3d02fc4a7209b329cbe3f9cdd306a0a0297b481696cdfdda5bd5ccdef308da` |

## Patch reconstruction, preservation and publication

`RB02.patch` is 513953 bytes, SHA-256 `8ed857cbab1fdc311ae66614e74589582ffd9c1aa254e83ea1f4178d5988de9d`. It is a zero-context unified patch; apply with `git apply --unidiff-zero --cached` after `git read-tree ab7e7461a69efc1db74faac2c8e8f3318ca10341` in an isolated index. This standard representation avoids whitespace-only context lines in the review artifact. An isolated Git index read accepted RB01, applied the exact patch with these options, and wrote result `df609a4d5c953f253737a927432ab3598647765e`. Changed paths equal the explicit 24-path allowlist. Neither reconstruction nor publication uses the implementation index. The three previously blocked temporary indexes were not accessed.

Implementation remains uncommitted on `smart-bins-v030-integration`, HEAD `8951f914eb34778139b0df42f59f3957abbc2f8a`. The index is empty and byte-identical, SHA-256 `18c7474d1a3a7fc2b9b8b894f6340ca5e8e607d78b0de3a154156c3c08b1027f`. The three RB01 files remain accepted bytes. Every other pre-existing implementation file is unchanged except the three authorized existing production edits (local_state, piece_records, IRL initializer).

Old candidate remains on `sorting-flow-candidate`, HEAD `c4e298ebd1a153001355df3d3b0c41cc5446ac8f`. All 869 tracked/nonignored files, status and index match the initial snapshot. Inventory SHA-256 `d4ae072299bc0590115451435325d135ae22dc0b375d459b14827c45733af03e`; index SHA-256 `f9b14e028a87a6722fb563af357225d867cb8b5cf701d77603f113e9d94dc7d6`. The old working checkout was never edited or used to run tests.

Publish only `chat_review/smart-bins/RB02/r1/REVIEW.md`, `RB02.patch`, and `manifest.json` through the existing isolated clean chat-review publisher, followed by remote parent/path/ref and byte-for-byte artifact readback. No implementation commit or push is authorized or performed. Publication receipts are retained locally after the remote checks.

**NO runtime wiring. NO physical release authority. NO deployment/hardware/live access.**

Stop after RB02 publication. RB03 is not started. P2C6B remains **BLOCKED**.
