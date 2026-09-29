# RB05 later physical qualification protocol — plan only

No stage in this document was executed in RB05. The candidate source tree is `c8c43b0261c1d581575fcd65785140b416dc6d44`. Source receiving-evidence contract: **READY**. Physical receiving-evidence policy: **NOT YET QUALIFIED**. Continuous Smart Bin production cannot be qualified until Stage 4 passes. A programmatic test seam is not an installed receiver.

## Authority and records

Run only after a separate deployment and physical-test authorization with an operator present. Record operator, machine, source/firmware identities, configuration hash, material lot, UTC start/end, profiler setting, enabled mode, incidents, stops, interventions, and exact immutable data extracts. Use aggregate counts and bounded reason categories in the shared report; keep piece identities, machine secrets, and private history in controlled local evidence. Do not treat repeated log lines as distinct events. Use durable claim/attempt/incident IDs to deduplicate locally before aggregation.

## Stage 0 — rollback and readiness

1. Record the exact installed source commit/tag and tree, firmware version/commit/variant, and current service state. The supplied baseline is `sorter/stable/v0.2.9` at `2c8269843c16282659c9edbfbf03e7c7042a15ce`, firmware `v0.8.1` / `8d560d26` / `distribution-v1-2`; verify these on the machine in the later authorized run.
2. Take restorable, access-controlled backups of source checkout, service definition, `machine.toml`, local SQLite database including consistent WAL state, and relevant calibration/configuration. Record hashes, backup location, and a restoration check. Preserve a known-working source revision and exact service start procedure.
3. Inspect durable Smart Bin claims and current obligations before activation. Require zero unresolved claims or an explicitly approved reconciliation disposition. Do not silently clear UNCERTAIN claims.
4. Define rollback trigger before motion: any safety halt below, schema failure/corruption, inability to restore native ordinary behavior, or an unaccepted performance/quality result. Stop admission and motion under operator control, preserve incident evidence, then restore source/config/database as one consistent set. Never restore an older database over newer unresolved custody without a reviewed reconciliation.
5. Keep firmware and `machine.toml` tuning unchanged during comparison. A concrete incompatibility needs separate review before changing either.

## Stage 1 — ordinary v0.3.0 smoke

Deploy the clean v0.3.0 base first with Smart Bins absent. Verify startup, homing, servo truth, C3→C4 handoff, chute/bin routing, multi-drop bucket safety, stall auto-clear, and manual controls under direct observation. Confirm no Smart Bin schema or Harvest store is required or created. Record ordinary successful deliveries, MISC/rejects, stalls and interventions. Halt for any safety condition below. This establishes the contemporaneous native behavior on this machine before the Smart Bin source is considered.

## Stage 2 — instrumentation overhead

With the same firmware, tuning, similar material and observed operating conditions, compare native ordinary runs with profiler disabled and enabled. Collect `snapshotRows()` only at the existing report cadence and optional `SORTER_PROFILE_BUS=1` logs with a fixed minimum threshold. Compare active/wall PPM and state dwell; inspect for materially changed queue depth or timing. No performance result is predeclared. If instrumentation itself changes behavior materially, investigate before using profiler-on measurements as qualification evidence. Keep the profiler-off run as the principal throughput comparison.

## Stage 3 — Smart Bin dry and guard verification

Under approved operator conditions, explicitly initialize RB02 storage/service, RB03 native custody, then RB04 completion extension on a new or reviewed database. Verify exact schema versions, zero stale claims, reservation before route, RELEASE_INTENT before release, one-shot dispatch, route/servo truth and current-obligation fences. Do not fabricate receiving evidence. Missing receiver evidence must hold without delivery, bin credit, history completion, progress credit, gate release or another physical release. Do not induce unsafe jams to exercise guards. Verify absence of Harvest application calls.

## Stage 4 — receiving-evidence policy qualification

This is the decisive unresolved physical gate. A future separately authorized source implementation is required; the RB05 tree has no installed qualified receiver. Define and separately accept an installed observation source and policy that identifies the **specific piece**, **actual receiver**, and **release attempt/incarnation/generation**, with a documented false-positive risk and failure behavior. Test correct delivery, wrong destination, no delivery, delayed/stale observation, duplicate observation and receiver ambiguity against direct physical observation. Route selection, track disappearance, motor acceptance, and settle are not receiving proof. Possible future sensor or inference approaches are options only; none is selected or implemented by RB05. Continuous Smart Bin production remains blocked until a source/policy passes this gate.

## Stage 5 — bounded guarded lot

After Stage 4 acceptance, run a small observed lot with one operator maintaining a physical destination tally. Reconcile each qualified observation, actual receiving bin, durable delivery, canonical history, and effect receipt. Required zero-tolerance events: unintended sorted-bin drop, duplicate release after ambiguity, duplicate canonical completion, or silent freeing of an UNCERTAIN claim. Stop for any safety halt below. Expand lot size only after the small lot closes with zero unresolved claims and an operator-accepted discrepancy report.

## Stage 6 — sustained comparison

Use the same machine and firmware, unchanged tuning, similar material and matched run mode. Run at least **60 active sorting minutes** per comparable condition; extend toward two hours if safe and material permits. Report active PPM and wall-clock PPM for contemporaneous native ordinary and guarded Smart Bin runs, plus quality, interventions and latency. The historical measured successful-distribution reference is 36/5 min = 7.20, 109/15 = 7.27, 220/30 = 7.33, 431/60 = 7.18, and 893/120 = 7.44 PPM; approximately 7.2–7.4 active PPM is an empirical reference, **not** a confidence interval. These are overlapping windows and must not be treated as independent samples. A candidate within roughly 5% of a paired contemporary native baseline is a **provisional comparison only — USER APPROVAL REQUIRED BEFORE PHYSICAL TEST**. The user sets the final allowable degradation and quality gate before the run.

## Measurement definitions

Use a single run interval and a consistent inclusion rule in both modes. `N` is the count of unique successful physical distributions with an accepted actual destination; for guarded mode require a verified canonical completion receipt. A command receipt or pending event is not success. `A` is accumulated seconds in the runtime's active running state, excluding stopped/paused intervals. `W` is elapsed wall seconds from first admission to final stop, including holds and interventions. Active PPM = `60 × N / A`; wall-clock PPM = `60 × N / W`. Report `A`, `W`, `N`, and zero-denominator cases. Count processed pieces by unique piece identity entering the run's disposition path, with MISC/reject separately; do not count repeated lifecycle events.

| Measure | Exact later-run rule/source |
|---|---|
| Stalls per 100 processed | `100 × distinct stall incident episodes / processed pieces`; deduplicate incident IDs and auto-clear episodes. |
| Operator interventions per 100 | `100 × distinct manual intervention episodes / processed pieces`; record start/end and reason. |
| Unintended destination | Physically observed piece in a receiver other than the intended qualified route; count unique pieces. |
| MISC/reject | Unique pieces with final MISC/reject physical disposition; report each category separately. |
| UNCERTAIN and unresolved claims | Unique durable claims entering UNCERTAIN; separately count current unresolved claims at end, including held completion/effect obligations. |
| Duplicate release/completion attempts | Unique durable attempt/receipt identities that tried a second action or were refused; distinguish refusal from any repeated physical command or duplicate canonical row. |
| Reserve, release-intent, dispatch-consume, current-authorization latency | Existing profiler `smart_bins.reserve_ms`, `smart_bins.release_intent_ms`, `smart_bins.dispatch_consume_ms`, `smart_bins.current_authorization_ms`; report count, mean, min, max and profiler setting. These are call durations, not separate SQLite lock-wait measurements. |
| Completion latency | Operational interval from qualified observation time to verified canonical receipt/readback time, including at most one-second Sending check cadence. Report separately from `distribution.state_machine.state_step_ms.sending`, which is an inclusive step duration and not a commit-only metric. The current source does not expose a separate production commit-only span. |
| Receiving insert/publication effect latency | Derive only if the later qualified receiver/effect source records trustworthy boundaries. Current source does not expose distinct production profiler spans; do not invent values from log spacing. |
| MCU bus blocking | RB01 `SORTER_PROFILE_BUS` round-trip log durations above configured threshold; include threshold and censored fast-call count limitation. |
| Classification latency | KnownObject `created_at` → `classified_at`, summarized by RuntimeStats `created_to_classified_s`; state the exact start/end semantics. |
| C3→C4 handoff latency | Obtain only from a later synchronized, paired physical/source observation. C3 emits a monotonic dispense event and C4 has wall-clock piece timestamps; current source does not retain a directly subtractable per-piece pair. Report unavailable until that observation is qualified. |
| Positioning, Ready, Sending dwell | `state_duration_ms.distribution.*` profiler rows and RuntimeStats state timeline, with entry/exit accounting. Sending can include an intentional held Smart Bin claim; report that segment separately. |

For latency, report sample count and distribution summaries from the existing bounded profiler snapshot, with a run identifier kept outside metric names. No piece UUID, reservation, delivery, machine, category or bin ID belongs in a metric name or exported aggregate. Preserve raw trace only in controlled local evidence if needed for forensic reconciliation.

## Immediate safety halt

Stop immediately for unintended sorted-bin delivery; repeated physical release after ambiguous command; evidence attributed to the wrong piece; falsely credited actual destination; silently freed unresolved claim; motion while durable custody fence blocks; servo route-truth contradiction; database corruption/schema mismatch; or runaway/stuck unsafe motion. Preserve source and durable state for diagnosis. Ordinary recoverable MISC routing alone is not automatically a halt.
