# P0B — Smart bins

**PROPOSED CONTRACTS / NOT IMPLEMENTED — 2026-09-27**

Changed path: [P0-CONTRACTS.md](P0-CONTRACTS.md).

## Baseline and scope

One HEAD/status check confirmed implementation checkout, branch `sorting-flow-candidate`, HEAD `c4e298ebd1a153001355df3d3b0c41cc5446ac8f`, clean index/worktree/nonignored untracked list. P0A-BASELINE.md (local baseline note; not included in this package) remains the baseline record. No current installed-machine state was inspected.

## 1. Authority and minimum schema

The existing `local_state` SQLite database owns delivery, contents, assignments and reservations. One shared bin service validates and writes them. `PhysicalC4Controller`/`PhysicalC4Runtime` retain physical custody and all motion decisions.

IDs are stable text keys; quantities are nonnegative integers; references are foreign keys:

| Record | Required fields and constraints |
| --- | --- |
| Slot/assignment | `slot_id`, machine, layout revision, coordinates, routing revision, qualified group keys. Coordinates are unique within a layout. Existing geometry, enabled flags, pool and limits remain configuration inputs with revisions; no second writable configuration copy. |
| Optional container; fill cycle | Container ID and optional label; cycle ID, nullable container ID/current slot, opened/closed times, close reason, provenance. At most one attached open cycle per slot and one open cycle per known container. Anonymous containers use cycle identity. Detachment preserves contents. |
| Run and policy | Reuse sorting-session/run IDs. Immutable policy revision identifies compiled artifact hash, grouping/mixing rules and existing routing settings. Group keys contain kind and namespace; preserve raw exact part/color IDs and source namespaces, including unknown provenance. |
| Reservation | ID, machine/piece UUID, route attempt, run, slot/cycle or explicit reject destination, quantity, group/policy/routing revisions, owner incarnation, episode, pocket/generation, state, row revision, timestamps. Unique request key and `(machine, piece, route_attempt)`; one nonterminal reservation per machine/piece. |
| Delivery and cycle contents | Delivery ID, unique reservation and machine/piece UUID, immutable destination/cycle, identity, quantity, policy and evidence references. Totals are keyed by cycle; aggregates by `(cycle, qualified item/group key)`. Both derive from deliveries plus opening balances. Retain the existing atomic event/aggregate/count operation, keyed to cycle rather than copied session ownership. |
| Audit/recovery | Transition ID, request key, payload hash, before/after revisions, actor/reason/evidence; opening-balance quantity/provenance/coverage; discrepancy records; durable external-operation status/reference/result. One monotonic machine `state_revision` covers every relevant write. |

Balances never invent piece UUIDs. Delivery uniqueness survives run changes; completed journeys cannot reserve again.

[piece_records.py:24,199](../../../../software/sorter/backend/piece_records.py#L24) opens the same database as local state. Refactor its upsert into a connection-taking helper without internal commit; initialize schema beforehand. Completion atomically writes delivery, event/count/aggregate updates, reservation state and piece history, preserving correction fields. Notifications and runtime statistics follow commit and cannot independently credit contents. No history outbox.

[local_state.py:130](../../../../software/sorter/backend/local_state.py#L130) currently sets WAL `synchronous=NORMAL`. Critical reservation, release-intent, completion and reconciliation transactions require connections configured `synchronous=FULL` before `BEGIN IMMEDIATE`. Storage must honor synchronization; acknowledgement without durable commit never permits release.

## 2. Reservation and fill-cycle lifecycle

Named states and transitions:

- `RESERVED → RELEASE_INTENT → EXIT_CONFIRMED → COMPLETED`.
- `RESERVED → CANCELLED` only after the physical owner confirms that no release is pending.
- `RELEASE_INTENT → CANCELLED` requires affirmative non-dispatch evidence and invalidation of that owner's pending attempt.
- Any nonterminal state may become `UNCERTAIN`. Reconcile to `COMPLETED` with delivery evidence or `CANCELLED` with proven non-delivery/removal; otherwise retain uncertainty. Record actual observed destination separately from immutable intended destination; truthful over-limit reconciliation blocks further allocation rather than rejecting the evidence.

Every transition checks reservation revision and records evidence. Repeating an identical request key/payload returns the stored result before stale-revision checks; changed payload under the same key returns `IDEMPOTENCY_CONFLICT`. Terminal results cannot regress. Late contradictory evidence creates a discrepancy and blocks reuse.

Reservation commit freezes destination, cycle, group, policy, routing revision and known custody references. Missing custody references block release. Reassignment requires safe cancellation and a new reservation linked to its predecessor. Release adds an immutable attempt ID and unique `(owner_incarnation, target_boundary)` index key, preventing restart collisions.

`used_capacity = recorded_cycle_quantity + held_reservation_quantity`. Recorded quantity includes opening balances. `RESERVED`, `RELEASE_INTENT`, `EXIT_CONFIRMED` and `UNCERTAIN` hold capacity; completion replaces the hold with recorded quantity in one transaction. Cancellation removes only the hold. Reserve rechecks eligibility and this sum under `BEGIN IMMEDIATE`; two connections cannot claim the last unit. Null limits remain unlimited. Unknown quantities or conflicting totals block affected allocation. Timeout/restart never releases a claim.

Empty closes the old cycle after explicit physical-empty confirmation and opens a zero-balance cycle. Reset additionally clears assignment. Exchange detaches the old cycle with its contents and attaches an identified existing cycle or an explicitly confirmed empty new cycle. Unknown replacement contents require reconciliation. Profile/session changes do none of these.

## 3. Release hooks and crash recovery

The exact candidate runtime hook is [PhysicalC4Runtime.tick:233](../../../../software/sorter/backend/subsystems/classification_channel/physical_runtime.py#L233). After the planner returns its prepared target, identify the load at station zero and durably commit its intent **before setting the runtime pending request and calling `request_index()`**. Planning prepares a logical target without motion.

[MarkerPositioner.poll:771](../../../../software/sorter/backend/subsystems/classification_channel/marker_positioner.py#L771) later invokes `_move`; `_move:679` calls `motor.start`, and `TrackedStepperIndexMotor.start:882` uses the existing motion lock/tracked move. Attach the committed permit to this index and require it before the first motor dispatch. Existing trims retain that attempt and motion policy. Recovery requires a separate durable operation permit before `PhysicalC4Controller._establish:178` calls `begin_target_trim()`, as that trim precedes the indexed sweep. Record reject/unknown-pocket evidence without fabricated identities; prior uncertain deliveries remain unresolved.

On marker confirmation, persist `EXIT_CONFIRMED` **before** `planner.complete_index()` removes the exiting pocket/binding (`physical_runtime.py:160–170`). Keep the returned confirmation in owner memory until persisted; do not poll it away. Retain `PhysicalDistribution.discharge:102` and existing settling. `Sending.step:95` becomes the atomic completion hook. If persistence fails, the owner holds the route and subsequent admissions; it may finish its owned safe stop, never redispatch to repair persistence.

| Failure point | Required result |
| --- | --- |
| Intent fails before dispatch | No motor call; retain prepared logical target/custody. Resolve any ambiguous commit by reading its key. |
| Acceptance unknown, or accepted motion lacks marker confirmation | Retain intent/claim as uncertain; existing owner handles stopping/recovery. |
| Exit confirmed, persistence or completion fails | Retry evidence/completion using the same IDs; hold conflicting use. A crash before evidence persistence remains uncertain. |
| Duplicate completion | Return original result; no count, history or motion repetition. |
| Restart | Reconstruct durable claims before admission. No automatic motion replay. Fresh markers establish position, not past delivery. |

Draining C4 does not prove earlier bin receipt. Resolve it using attributable evidence or audited operator reconciliation; otherwise retain the block. Label marker-confirmed exit and software-recorded delivery separately from sensor-confirmed bin entry.

## 4. Interfaces, fencing and writer coverage

Minimum service surface: `preview(piece, revisions)`, `reserve(piece, expected_state_revision, request_key)`, `prepare_release(reservation, owner_token, target)`, `confirm_exit`, `complete`, `cancel`, `reconcile`, and `mutate_or_activate(plan, expected_state_revision, request_key)`. Transitions also require expected reservation revision. Results include IDs, new revisions and reason codes: `STALE_REVISION`, `NO_ELIGIBLE_BIN`, `IN_FLIGHT`, `OCCUPIED_INCOMPATIBLE`, `UNRESOLVED_DELIVERY`, `PERSISTENCE_FAILED`.

Eligibility checks enabled/reachable slots, pool, grouping compatibility, count and existing global/layer fit **before assignment**. Preserve unknown-dimension behavior and imported mixing/routing policy. Commit reevaluates previews.

Obtain physical quiescence from the current owner, then revalidate under lock order: lifecycle lock → bin-service mutex → SQLite transaction. Never acquire lifecycle/motor/Harvest locks from inside SQLite. Release database locks before hardware/provider waits. The committed intent and owner permit fence the dispatch gap. Cache adoption follows commit; failed adoption blocks resume.

| Writers from P0A | Required fence |
| --- | --- |
| Manual assign; empty/reset/exchange; Harvest clear | Reject affected claims, uncertain outcomes and incompatible occupied contents. “Paused” alone does not establish clearance. |
| Pool, limits, enabled sections/layers, geometry; bin-assignment settings | Use the same service/revision check. Reject limits below contents plus claims; preserve cycle/slot identity. Geometry changes cannot silently relocate contents. |
| History assignment and rule preassignment | Produce a proposal, then use shared eligibility and mutation checks; no privileged direct array writes. |
| Profile apply/reload; saved layouts | Stage/validate immutable artifact/configuration first. Global activation requires no retained C3/C4 custody, outstanding release/claim or unresolved integration. Commit active policy/run/assignments together without emptying cycles. Legacy empty/rules modes require separate explicit empty actions. |
| Harvest assignment/rehydration | Check exact audited map and revisions, plus shared capacity/occupancy gates; no overwrite of an intervening edit. |

Also cover `hardware.py:2627,2850,3498,3633`. Unknown occupancy compatibility fails closed. Hash-verify immutable staged files before committing the active reference. Legacy TOML/file writes become staged inputs or compatibility projections. Runtime/startup must adopt the committed revision before admission; saved geometry retains its restart requirement.

## 5. Harvest's separate transaction boundary

[HarvestProjectStore:1092](../../../../software/sorter/backend/project_harvest_projects.py#L1092) uses `harvest_projects.sqlite3`; its commits cannot join the local-state transaction. Use a durable integration record:

1. Record local operation intent, then request Harvest quota using its project/piece identity. Read back after ambiguous responses.
2. Commit the exact bin reservation with allocation/activation references. Release requires both records to agree. Failure leaves a recoverable quota claim until an explicit idempotent cancellation completes.
3. Commit local delivery plus a pending Harvest-confirmation operation atomically. Retry Harvest confirmation from that record; verify payload consistency before accepting replay. Failure blocks further affected-project admission, never repeats the drop.

Activation likewise journals phases across both stores while admission is fenced. Resume by read-back; compensate only when revisions still match and no release occurred. Existing blanket planned-allocation retirement must exclude uncertain release claims. Never hold either store's transaction while calling the other.

## 6. Migration and exports

Migration is additive, versioned and rerunnable with writers stopped. Preserve original rows/references; prohibit old writers after cutover. Backfill cycles from proven physical continuity across sessions. Legacy APIs read cycle-backed projections; run changes never copy canonical contents.

Link historical events only where session succession, clear/import history, coordinates and epochs prove continuity. Matching coordinates/epochs alone is insufficient. Count repeated carry-forward snapshots once; deduplicate proven identical piece events. Preserve ambiguous rows as historical evidence without assigning them guessed current membership.

For each trustworthy current aggregate, subtract proven linked deliveries to obtain an explicit opening balance. Negative residuals or conflicting totals become discrepancies, not fabricated adjustments. [The legacy importer:2228](../../../../software/sorter/backend/local_state.py#L2228) synthesizes UUIDs/timestamps; such rows do not automatically establish observed lineage.

At one read revision: `total = linked delivery quantities + opening balances`; grouped totals must equal total, using an unknown group where necessary. Per-piece exports contain proven identities only and report opening-balance quantity, unresolved claims, discrepancies and coverage. Snapshots reference cycle contents at their cutoff; they are never additional inventory.

## 7. Serial implementation and verification

| Slice | Consumes / limits |
| --- | --- |
| P1 eligibility | Shared eligibility semantics; no schema or in-flight capacity guarantee. |
| P2A schema/migration | IDs, revisions, cycle accounting, same-connection history helper, legacy provenance; test-only until complete cutover. |
| P2B reservations | Transactional capacity and replay rules; cannot claim physical crash safety. |
| P2C runtime recovery | Durable pre-dispatch permit, exit evidence, restart/reconciliation and Harvest bridge; simulated qualification first. |
| P2D bin actions | All mutation fences and explicit cycle transitions; close alternate-writer bypasses. |
| P2E exports | Canonical cycle ledger and migration coverage; reconcile all output forms. |
| P4B safe activation | Revisioned staging/adoption and store recovery; preserves retained ownership and occupied contents. |

Implement serially in that order. Do not enable the new runtime while any legacy writer can bypass its fences.

Proposed tests below extend **existing P0A targets**; none were run:

| Existing targets | New failure/concurrency/migration cases |
| --- | --- |
| `test_distribution_rehome.py`, Harvest distribution suite | Full unassigned bin; fit before assignment; alternate fitting slot; pool/disabled checks; unchanged unknown-dimension behavior. |
| `test_local_state.py`, `test_runtime_stats.py` | Concurrent final-unit claims; rollback including history; duplicate keys; cross-session lineage; opening balances/conflicts; rerun migration. |
| `test_physical_c4_runtime.py`, `test_distribution_sending.py`, `test_distribution_rehome.py` | Crash at each release boundary; ambiguous acceptance; retained pause; duplicate completion; restart never redrops. |
| `test_bin_reset_actions.py` | Every writer racing reserve/complete; stale revision; occupied exchange; unknown replacement; atomic multi-bin changes. |
| Harvest route/runtime suites | Failure between either store's commits; orphan quota; idempotent confirmation; uncertain retirement; activation/adoption recovery. |

Use Python 3.12 and P0A's frozen commands. Set temporary `LOCAL_STATE_DB_PATH`, `MACHINE_SPECIFIC_PARAMS_PATH` and Harvest roots before imports; reset connection/schema caches between stores. Use fake hardware/providers and the marker rig. Global `conftest.py` isolates models only. Exercise real temporary SQLite transactions with injected failures.

**RECOMMENDATIONS:** exact variants remain separate; new strict modes never silently mix overflow groups; visible container labels are optional; new high-value handling remains unconfigured. Preserve existing/imported behavior. Variant/strict-overflow product choices affect P3/P5/P6; labels affect P2D/P6 presentation; price and sensor choices belong to P7/D2. They do not block this ledger contract.

Remaining evidence gaps are legacy lineage quality and current installed integration identity. Conflicting migration evidence blocks affected-bin activation; source/physical qualification blocks later deployment. No product answer blocks P1/P2A. Only this memo was created; no tests, implementation or operational actions occurred. Stop after P0B.

## Supervisor clarifications

- When jointly acquired, use `hardware_lifecycle_lock` → controller `_operation_lock` → new service/database locks. P2C must verify every entry path and prohibit reverse acquisition.
- Assignment-only reset preserves contents and cycle. Empty-and-reset has distinct semantics and requires explicit physical-empty confirmation.
- Reconciliation preserves the intended destination, but credits only an evidenced receiving cycle. Unknown attribution stays unresolved; a reservation alone never credits the intended bin.
