# RB04 r1 — Canonical native completion and bounded current authorization

## Result and scope

RB04 adds explicit, versioned receiving evidence and an atomic Smart Bin delivery/history/receipt transaction around the native v0.3.0 identity. It changes no physical motion owner. The source default has **no qualified receiving observation**: MOTOR_ACCEPTED, INFERRED_EXIT, transport association, route/flap position and Sending settle never establish actual bin receipt. A guarded piece without qualified evidence remains held; it earns no delivery, history, bin count or set-progress credit and cannot cause another release.

The implementation remains uncommitted on `smart-bins-v030-integration` at `8951f914eb34778139b0df42f59f3957abbc2f8a`; its index is empty. This is source and disposable-test evidence only. There was no sorter, serial, firmware, deployment, provider, external Harvest, or physical qualification action. RB05 has not started; P2C6B remains blocked.

Patch base: accepted RB03 result tree `b4cf20bd92855e2ccdb22748fcf952f19b169738`. RB04 result tree: `f388c5b91945e01a141100039f41f55d728a7331`. The normal context patch reconstructs that tree in a separate Git index and object directory. The old candidate was read only and its inventory digest remained `d4ae072299bc0590115451435325d135ae22dc0b375d459b14827c45733af03e`.

## Receiving-evidence contract

`ReceivingEvidence` version 1 binds immutable evidence ID, machine, reservation, piece UUID, release attempt, native incarnation, head generation, intended route revision, actual destination kind/slot/cycle, evidence type and policy version, evidence reference, observed time, source and qualifier identities, and immutable revision. It requires a physically qualified receiver type and an observation after the accepted release command. The actual slot/cycle must exist and be open at observation time. No public endpoint or automatic runtime source supplies this record. The narrow programmatic seam is for a separately qualified future source and disposable tests.

The row and payload hash are immutable. An identical evidence ID/payload replays exactly; the same ID with different payload or a second receiver for one reservation conflicts. Actual destination comes only from this record. Intended reservation/delivery fields remain frozen. A qualified misroute credits the observed receiver and retains an unresolved discrepancy. A reject/bucket receipt requires the same qualified observation; “all doors open” is not receipt proof.

## Native completion and publication

The adapter binds `native_machine_id` and `native_reservation_id` to the KnownObject immediately after durable RB03 custody binding. `native_delivery_id` is assigned only after completion commits and receipt readback verifies machine, reservation, piece, attempt, incarnation, generation, route, actual evidence, delivery, history run/destination and receipt hash. Events carry only these three IDs, and old event shapes remain valid.

On exact qualified evidence, `complete_native` checks current same-machine obligations and release state inside one critical transaction. It inserts actual delivery, writes `piece_records` through the caller-owned connection, transitions the reservation to COMPLETED, records a misroute discrepancy when needed, stages all publication obligations, and inserts the canonical receipt before one commit. A failed step rolls the entire set back. A lost commit acknowledgement is resolved by exact receipt readback and replay, never another physical release.

Sending preserves its ordinary v0.3.0 branch when no native reservation exists. For a verified guarded claim it emits a truthful pending event once, without an actual destination or legacy accounting, then checks at most once per second. On verified completion it publishes an event, adopts already-written history into RunRecorder memory, records progress, notifies progress sync, persists CLOSE, and only then checks current blockers for gate/admission effects. Each nontransactional effect has a persisted ATTEMPTED barrier and a separate success receipt; an ambiguous attempt is held for reconciliation instead of replayed. RunRecorder never issues a second piece-record upsert for a canonical native delivery.

RuntimeStats retains ledger-verified machine/reservation/delivery provenance across partial events and detail lookup eviction. Pending native provenance suppresses `record_piece_distribution`; verified completion also suppresses that legacy writer because the Smart Bin delivery already credits contents. A forged event identity cannot suppress ordinary accounting. A later event cannot replace established native identity.

| Durable phase | Current behavior |
|---|---|
| No qualified receiver | RELEASE_INTENT/held; no completion, accounting, progress or new release. |
| Atomic DB failure | No delivery/history/receipt/COMPLETED transition; current claim remains. |
| Commit readback ambiguity | Verify exact committed receipt; hold if verification fails; never repeat motion. |
| Publication PENDING | Persist ATTEMPTED before callback. |
| Publication ATTEMPTED or callback failure | Retain action-qualified phase and failure evidence; do not replay uncertain callback. |
| Producer effects SUCCEEDED | Persist CLOSE separately before gate/admission. |
| Gate or admission failure | Attribute GATE_OPEN or ADMISSION_RELEASE separately; current obligation remains. |
| New contradiction after old close | Current facts override the old receipt; admission stays held. |

## Current recovery and bounded authorization

The explicit RB04 extension maintains `smart_bin_current_obligations` in the same SQLite transactions as follow-up, source, delivery, history, discrepancy, reconciliation, close-receipt and native effect writes. A rollback rolls the projection back too. CLOSED predecessors reappear as blockers on late holds, missing history or CLOSE receipt, contradictory source/reconciliation and owner requalification evidence. A new native receipt with missing history is also blocking. Operator inspection may still scan history; live authorization queries only indexed current obligations and current held reservations/holds/discrepancies. Machine B cannot block machine A.

Generated closed-history sizes of 1, 51 and 201 follow-ups all matched the full recovery inspector's clear result. Current authorization executed **10 SQL statements at each size** and returned zero current-obligation rows. `EXPLAIN QUERY PLAN` showed a covering search on `smart_bin_current_obligations_machine (machine_id=?)`. Separate cases cover a late hold, history deletion, a late reconciliation, another machine and transactional rollback. The live query count is constant in this measured range; this is deterministic source/test evidence, not a wall-clock benchmark or a proof of every possible historical disposition.

The extension is version 1, installed only by explicit `initialize_schema()`. Import performs no DDL. The initializer explicitly installs local piece-history, completion-recovery, reconciliation and follow-up-reconciliation prerequisites, then creates RB04 tables/indexes/triggers and backfills current facts. Ordinary startup does not migrate. Active native custody with a missing RB04 extension fails closed in the adapter before guarded release; Smart Bin absent mode remains ordinary native v0.3.0. No Harvest application/store/provider import or call is on the ordinary path.

## Fault injection and validation

| Injection | Observed durable result |
|---|---|
| Receiving insert | No qualified evidence row. |
| Delivery insert, history insert, reservation COMPLETED update, effect-obligation insert, receipt insert | Entire completion transaction rolls back; no delivery/history/receipt and RELEASE_INTENT remains. |
| Delivery readback after committed transaction | Exact receipt replay recovers one delivery and one history row. |
| Publication-attempt persistence | No callback or gate action; phase remains PENDING. |
| Callback | ATTEMPTED with PUBLICATION_CALLBACKS failure; no replay. |
| Publication-success persistence | ATTEMPTED retained; no gate opening. |
| CLOSE persistence | CLOSE stays PENDING; no gate opening. |
| Gate effect / admission effect | Separate GATE_OPEN / ADMISSION_RELEASE failure attribution; current obligation remains. |
| Projection write in rolled-back savepoint | No ahead-of-ledger blocker survives rollback. |

Final combined directly affected run: **237 passed** using the frozen/offline v0.3.0 environment, disposable SQLite under `C:\Users\abria\AppData\Local\Temp\sorter-rb03-tests-8pe6ku3z`, no network/real serial. It included RB04 completion/projection/fault/Sending/runtime/recorder/event tests; RB02 delivery/completion-recovery/reconciliation/follow-up tests; RB03 native custody/runtime; and ordinary `test_distribution_sending.py` plus `test_c4_distribution_handoff.py`. The prior focused baseline of RB02/RB03/ordinary tests passed **193 tests**. Ruff on every RB04-changed/new Python path found **zero current and zero new findings** against RB03. Source-tree `git diff --check` passed. No full backend, device or physical qualification is claimed.

The implementation HEAD/branch/index and every non-RB04 path remain unchanged; the exact path and hash list is in `manifest.json`. The accepted RB03 physical ownership, one-shot dispatch, RELEASE_INTENT-before-motion, no resend/replay, route/flap and controller-truth fences were not edited. This review package contains only `REVIEW.md`, `RB04.patch` and `manifest.json`.
