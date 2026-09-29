# RB05 r1 — final v0.3.0 source acceptance package

## Scope and verdict

**Source gate: PASS for review of the exact uncommitted tree** `c8c43b0261c1d581575fcd65785140b416dc6d44`. This is source/disposable-test evidence, not deployment or physical qualification. **Receiving-evidence source contract: READY. Physical receiving-evidence policy: NOT YET QUALIFIED.** The runtime supplies no qualified receiver; guarded continuous Smart Bin production remains blocked. P2C6B remains blocked.

Implementation `smart-bins-v030-integration` stayed at HEAD `8951f914eb34778139b0df42f59f3957abbc2f8a` with an empty index. An isolated index applied RB01 `8c83a0ce` to pristine tree `21e805b6` and yielded `ab7e7461`; RB02 `688778a6` with its declared zero-context mode yielded `df609a4d`; RB03 `95c0c215` yielded `b4cf20bd`; RB04 `84ed5b61` yielded `f388c5b9`. Every one of 1,605 reconstructed files matched the pre-edit working bytes. RB05's five-path context patch reapplied to `f388c5b9` in a second isolated index and yielded the exact result tree above. No implementation commit or push, live sorter, hardware, firmware, provider, Harvest, deployment or physical test action occurred. The old `sorting-flow-candidate-20260911` was read only.

## Instrumentation gap audit

Classification is for **later physical qualification**, not a claim that source tests measured physical performance. `EXISTING_NEEDS_LABEL/EXPORT` means the source contains a boundary, but an operator must aggregate, pair, or label it explicitly. `NOT_MEASURABLE_SOURCE_ONLY` means the required physical observation is absent; no metric was invented. Durations from profiler rows are call/step summaries, not independent per-piece physical evidence.

| Category / measurement | Classification | Existing or added source; limitation |
|---|---|---|
| A successful distributions | EXISTING_SUFFICIENT | RuntimeStats unique distributed lifecycle; guarded success requires canonical RB04 receipt and actual receiver. |
| A active sorting duration | EXISTING_SUFFICIENT | RuntimeStats monotonic `running_time_s`. |
| A PPM | EXISTING_SUFFICIENT | RuntimeStats `overall_ppm`; later paired active/wall calculations defined in `QUALIFICATION.md`. |
| B classification latency | EXISTING_SUFFICIENT | KnownObject `created_at`/`classified_at`, RuntimeStats `created_to_classified_s`. |
| B C3→C4 handoff latency | NOT_MEASURABLE_SOURCE_ONLY | C3 dispense has a monotonic event; C4 piece timestamps are wall clock and no durable paired per-piece boundary exists. Require later synchronized observation; do not subtract unlike clocks. |
| B C4 release dispatch timing | EXISTING_NEEDS_LABEL/EXPORT | RB05 `dispatch_consume_ms` covers durable permission; RB01 bus blocking log covers command round trip. These do not prove physical landing. |
| B Positioning dwell | EXISTING_SUFFICIENT | `state_duration_ms.distribution.positioning`, RuntimeStats state timeline. |
| B Ready dwell | EXISTING_SUFFICIENT | `state_duration_ms.distribution.ready`, RuntimeStats state timeline. |
| B Sending dwell | EXISTING_SUFFICIENT | `state_duration_ms.distribution.sending` and `distribution.state_machine.state_step_ms.sending`; held receiver wait must be separated in analysis. |
| C reservation latency | EXISTING_NEEDS_LABEL/EXPORT | Added `smart_bins.reserve_ms` around service reserve call. |
| C release-intent commit latency | EXISTING_NEEDS_LABEL/EXPORT | Added `smart_bins.release_intent_ms` around custody arm/commit. |
| C dispatch-consume latency | EXISTING_NEEDS_LABEL/EXPORT | Added `smart_bins.dispatch_consume_ms` around one-shot consume/commit. |
| C receiving-evidence insert latency | NOT_MEASURABLE_SOURCE_ONLY | RB04 has a programmatic insert seam but no qualified production receiver. Future policy must supply trustworthy observation and insertion boundaries. |
| C canonical completion/history commit latency | EXISTING_NEEDS_LABEL/EXPORT | RB04 atomic receipt and Sending step give operational observation→verified completion latency and an inclusive step upper bound. No isolated production commit-only profiler span exists; do not label these as commit-only. |
| C current-authorization latency | EXISTING_NEEDS_LABEL/EXPORT | Added `smart_bins.current_authorization_ms` around indexed RB04 blocker call in native adapter. |
| C publication/effect latency | EXISTING_NEEDS_LABEL/EXPORT | RB04 durable phases/updated times and Sending step bound operational completion→CLOSE; no isolated callback-only profiler span. |
| D SQLite transaction wait/elapsed | EXISTING_NEEDS_LABEL/EXPORT | New reserve/arm/consume spans include connection, lock wait and transaction elapsed; wait alone is not separately resolved. |
| D busy/lock refusal | EXISTING_NEEDS_LABEL/EXPORT | SQLite errors/refusals propagate and can be categorized from incident/held results; no silent retry metric. |
| D bounded statement/row behavior | EXISTING_SUFFICIENT | RB04 current-obligation projection, indexed `LIMIT 1` lookups; accepted generated-history evidence: 10 authorization SQL statements at 1, 51 and 201 closed follow-ups, zero current rows, covering machine index. Large history test not rerun. |
| D current outstanding obligation count | EXISTING_NEEDS_LABEL/EXPORT | Read-only aggregate over `smart_bin_current_obligations` and current claims/holds; count off the release hot path. |
| E UNCERTAIN claims | EXISTING_SUFFICIENT | Durable reservation/native status and evidence. |
| E held claims | EXISTING_SUFFICIENT | Native holds and current-obligation projection. |
| E unresolved completion obligations | EXISTING_SUFFICIENT | Current-obligation projection plus incomplete RB04 effects/receipts. |
| E duplicate release refusal | EXISTING_SUFFICIENT | One-shot permit/attempt uniqueness and durable refusal; distinguish attempted duplicate from repeated motor command. |
| E duplicate completion refusal | EXISTING_SUFFICIENT | Exact receipt replay/conflict and unique canonical rows. |
| E route qualification failure reason | EXISTING_NEEDS_LABEL/EXPORT | Bounded service refusal codes and durable native hold kinds; group free-text exceptions offline, never use them as dynamic metric labels. |
| E receiving-evidence reason/type | NOT_MEASURABLE_SOURCE_ONLY | Contract fields exist, but no production qualified receiver supplies them. Future accepted policy must define bounded types/reasons. |
| F MCU bus round trip/blocking | EXISTING_SUFFICIENT | RB01 optional `SORTER_PROFILE_BUS` and `SORTER_PROFILE_BUS_MIN_MS` log, with threshold censoring documented. |
| G stalls | EXISTING_SUFFICIENT | RuntimeStats/incident records, unique episode IDs and auto-clear disposition. |
| G interventions | EXISTING_NEEDS_LABEL/EXPORT | Durable incident resolution/operator action must be deduplicated by episode in later run. |
| G native recovery/refusals | EXISTING_SUFFICIENT | Durable custody/reconciliation statuses and held/refusal reasons. |

The only missing low-overhead source spans needed now were the four added native adapter calls. Exact C3→C4 physical handoff, receiving insert, commit-only and callback-only timings need boundaries outside the permitted RB05 hot paths or a future qualified receiver; `QUALIFICATION.md` explicitly reports them unavailable rather than treating proxies as exact. The adapter retains the existing `gc.profiler` from `attach(gc)`. Disabled mode uses only a null context with no clock read or profiler row; enabled mode uses constant names, no identity labels, network, file scan or SQLite write for telemetry. Timing contexts close before physical motion is yielded. `snapshotRows()` already exports bounded aggregate duration rows.

## Configuration, firmware and isolation

Synthetic TOML verified that old `[machine_setup]` selection keys and `[feeder_go_to_angle]` / `[feeder_constant_movement]` cannot select removed flows: native constants remain `classification_channel`, `pulse_perception_rev01`, `two_piece_state_machine_rev01`. `[feeder_pulse_perception]` still parses; retired speed/clamp values migrate in memory while explicit per-channel values win. `c_channel_2`, `c_channel_3` and `classification_channel` camera roles remain accepted. `carousel` is the historical C4 camera alias and the C4 rotor binding. Canonical `classification_channel` wins a conflicting dual camera definition; the later operator preflight must detect and resolve such a conflict. `machine_toml_path()` is centralized, cwd independent, accepts absolute overrides and resolves relative overrides from the backend directory. No live `machine.toml` was read or edited.

The v0.3.0 firmware subtree and backend hardware protocol sources are unchanged from firmware commit `8d560d26b60b09145d0c7c62ac81f2fb3c22a60c` at pristine v0.3.0; RB01/RB03 backend fencing changes do not introduce a firmware command. Source check found **no firmware change required** for the supplied `v0.8.1` / `distribution-v1-2` identity. It is not proof of byte-identical flash or physical correctness.

Ordinary absent mode imports/constructs the native adapter without a Smart Bin or Harvest database and does not create their schemas. Explicit RB02 storage/service, RB03 custody and RB04 completion initialization is required for guarded mode; partial or mismatched extensions fail closed. Startup calls `attach` and `require_startup`, not schema initializers. Fresh-process test guards `project_harvest_*`, dormant local Harvest modules, network and import-time SQLite; no ordinary import or absent adapter construction crosses that boundary. The dormant journal remains unwired. No Harvest DB, provider, network or UI integration is required for native sorting.

## Static hot-path check

Native adapter C1/C2 motion returns before any Smart Bin DB read. Reservation and arm commit before route/release motion; dispatch consume commits before its one-use motor scope; no SQLite transaction spans servo/motor wait. No Harvest application call, provider/network call, directory enumeration, or file scan occurs in those release transactions. No telemetry key contains a piece, reservation, delivery, machine, category or bin ID. No servo/bus polling loop was added. Sending still checks missing receiver at most once per second. Current authorization reads indexed, current same-machine projection/claims/holds/discrepancies with `LIMIT 1`; it does not scan full history in the normal path. RB04's generated-history query evidence is reused, with no query change in RB05.

## Focused validation

The selected group crosses the final boundary: `test_mcu_bus_read` (RB01 no-resend/unresolved address), `test_smart_bins_storage` and `test_smart_bins_reservations` (RB02 atomicity/capacity), `test_smart_bins_native_custody` and `test_smart_bins_native_runtime` (RB03 guard/dispatch and new spans), `test_smart_bins_native_completion`, `test_smart_bins_native_projection`, `test_smart_bins_sending`, `test_smart_bins_runtime_stats` (RB04 completion/projection/Sending/provenance), `test_distribution_sending` and `test_c4_distribution_handoff` (native ordinary), `test_machine_toml`, `test_setup_wizard`, `test_pulse_perception_config`, and `test_rb05_configuration_isolation` (synthetic config/camera/Harvest boundary). Route/servo truth files were not rerun because those production surfaces were not edited. No full suite or old 256/252/237-test group was rerun.

Final frozen/offline Python 3.12 run under a disposable SQLite root, external-network/real-serial guards and no bytecode: **197 passed, 42 subtests passed, 4 existing FastAPI deprecation warnings**. The first run exposed a Unix-only absolute-path assertion and the RB04 runner's overly narrow Windows temp/loopback guard; the second exposed an existing test teardown retaining the process-lifetime WAL keeper. The review patch fixes the two test defects, and the final isolated run passes. Changed-path Ruff: **0 findings**, matching RB04's zero-findings changed-path baseline; **0 new findings**. The implementation's `git diff --check` and isolated source-patch `git diff --cached --check` passed. The review branch's outer check reports 13 inherent blank context lines inside the nested `RB05.patch`; the other four staged artifacts pass its check.

## Next action and gates

Accept/review this source package, then separately authorize an exact-tree deployment. Follow `DEPLOYMENT.md` and `QUALIFICATION.md` with operator control. P2C6B remains **BLOCKED** until RB05 source acceptance, explicit deployment authorization, native v0.3.0 ordinary physical smoke, receiving-evidence policy qualification, guarded Smart Bin physical qualification, and performance/continuous-operation acceptance. No physical result is inferred from this review.
