# Fail-forward C4 migration

This is the durable scope record for the accepted architecture and Slices 2–4.
Production cutover and hardware qualification are separate future work.

## Locked geometry and model

C4 has ten fixed physical pockets and advances clockwise. P6 is intake/home.
A deposit follows P6 -> P5 -> P4 -> P3 -> P2 -> P1 -> P0 -> EXIT: six completed
advances leave it retained at P0; the seventh sweeps it into the chute. Ten
physical pocket IDs are distinct from the seven-advance loaded travel path.

Slice 1 (`cd60c808911bcb493b795ad7d0427ddd048d6c89`) added
[`physical_fifo.py`](../../software/sorter/backend/subsystems/classification_channel/physical_fifo.py).
It alone owns physical pocket state, generations and the confirmed rotor
boundary. Absolute targets survive retry/resume; only confirmed completion
advances, and duplicate completion cannot advance twice. No runtime cutover.

## Slice 2: demand and predictive chute core

[`demand_planner.py`](../../software/sorter/backend/subsystems/classification_channel/demand_planner.py)
adds FEED for a deposited P6 load, DRAIN for buffered loads with empty intake,
and HOLD otherwise. It prepositions for the earliest available future route.
P0 remains retained until the seventh advance. A release-producing index needs
an aligned chute or arrival prediction plus margin no later than release.
An expired ETA is not arrival feedback. Non-release indexes need no chute gate.

Pending routing deadlines carry pocket ID and generation; stale deadlines cannot
change a reused pocket. P0 is the latest routing deadline: remaining PENDING
becomes DISCARD. The planner consumes routes, not provider or Harvest identities.
Disposition cannot stall physical transport waiting for a response.

Callers supply monotonic time, fresh stopped/aligned chute feedback, reachable
travel estimates, and the index-start-to-release interval. Future hardware
adapters reuse Chute.getAngleForBin(), its calibrated geometry, servo readiness
and stepper.estimateMoveDegreesMs(); the planner never calls motion methods.
plan() prepares at most one index; while outstanding it returns HOLD. The
adapter retains FIFO.pending_index for an interrupted move. That target is not
resume authorization: before resuming, a later adapter must revalidate chute
readiness against the remaining physical distance to release. This slice does
not infer partial-index geometry; its active-index decision remains HOLD.
Completion requires
confirmed stopped-at-target evidence, not an ACK. Across model replacement,
callbacks must remain segregated by runtime epoch as specified in Slice 1.

Travel, physical release and gravity fall-clear are separate. The isolated
compatibility default is **1.5 seconds**, from normal Sending._settleMs() ->
CHUTE_SETTLE_MS in distribution/sending.py. That existing path additionally
uses a configured cooldown (default 0.8 s) and tracker checks; neither is copied.
The 0.4 s sample-collection setting and 8 s incident watchdog are also excluded.
No independent gravity calibration was found: 1.5 s is an existing normal-path
value, not newly measured evidence. Timing permits a calibrated override.
Fall-clear starts at reported physical release, or conservatively at confirmed
completion if release time is unavailable, never at predicted release. Chute
departure is allowed exactly at fall-clear; duplicate completion cannot extend it.

Production coordinator, feeders, distribution state machine and FIFO remain
unchanged. No hardware operation, deployment, active drain controls or adapters
are included. The later adapter must verify live calibration and release timing
before any physical use; simulated timing is not hardware qualification.

## Slice 3: intake and asynchronous routing bridge

[`intake_routing.py`](../../software/sorter/backend/subsystems/classification_channel/intake_routing.py)
exposes the explicitly constructed `C4IntakeRoutingBridge`; no production factory
or coordinator selects it. The owner calls `confirmed_deposit(boundary, now,
sample)` for a confirmed stationary C3->P6 arrival. `sample` is the exact paired
`(PieceObservation sequence, PerceptionFrame)` obtained from the existing
`PerceptionService.read_pieces_and_frame(4)` for that confirmation. The caller
establishes arrival/freshness and maps the calibrated DROP region to P6; this
bridge never infers confirmation from a detection or a transfer identity.
Only DROP observations contribute crops/cardinality. Missing imagery, repeated
frame timestamps, ambiguous occupancy and multidrops retain a DISCARD load.
Duplicate boundary confirmations are harmless; in-motion intake is forbidden.

A singleton creates one PENDING generation and copies its crop before dispatch.
[`pocket_recognition.py`](../../software/sorter/backend/subsystems/classification_channel/pocket_recognition.py)
reuses the existing Brickognize client, optional selected hosted color client,
and read-only sorting-profile category lookup. Explicit confidence/margin policy,
category-to-destination mapping and reachable destinations are supplied by the
integration; they are not new production settings. Provider errors, timeouts,
missing/ambiguous answers and unreachable routes mean DISCARD. No price lookup,
Harvest allocation/confirmation or legacy object/transport lifecycle is called.

Workers post at most one result using `RoutingKey(epoch, pocket_id, generation)`.
The opaque epoch is local to this bridge instance and cannot be persisted or
reused across model replacement. Only the owner applies results: call
`deadlines = bridge.poll(now)` immediately before `planner.plan(...,
deadlines=deadlines)`. Use the same monotonic clock for both; camera timestamps
are a separate domain. Results applied at/after their deadline are ignored and
Slice 2 converts the pending load to DISCARD. Its P0 deadline remains the final
fallback. A result can never revise a resolved/discharged/reused generation.

Worker slots are bounded with no backlog; saturation discards the new intake.
An expired request retains its worker slot until the provider actually returns,
preventing unbounded threads during a provider outage. Neither transport nor
`close(now)` joins workers. Close retires pending routes to DISCARD and rejects
late callbacks. Physical motion, calibrated timing and runtime cutover remain
future adapter responsibilities. Slice 4 can consume pocket/generation routes
and the existing once-only `Discharge` snapshots for advisory routing/accounting.

## Slice 4: integrated runtime coordinator

[`fail_forward_runtime.py`](../../software/sorter/backend/subsystems/classification_channel/fail_forward_runtime.py)
adds explicitly constructed `C4RuntimeCoordinator(bridge, planner, motor, chute)`.
The bridge and planner must share an idle FIFO. This is the opt-in integration
seam; the production coordinator and all production selectors remain unchanged.
Slices 1–3 retain their existing interfaces and behavior.

One owner calls `confirmed_deposit` with Slice 3's confirmed paired sample and
`tick(now)` on the same monotonic clock. Each tick consumes stopped-at-target
motor feedback, polls asynchronous routing, evaluates FEED/DRAIN/HOLD, submits
predictive chute positioning, then submits at most one logical pocket index.
Classification is never joined. `IndexMotor` structurally reuses the existing
`StepperMotor.move_steps`, `stopped`, and `position` surface: the relative command
is computed once from the FIFO's absolute boundary targets, with no new motor
stack, speed settings or position reset. Only stopped-at-target confirmation
advances the FIFO. Submission ACKs and repeated ticks cannot advance/reissue it.

`ChuteIO.observe(now)` supplies fresh stopped/aligned feedback including doors
and reachable travel estimates. `move(ChuteMove)` submits immediately or raises
on rejection/uncertainty. An in-flight directive retains its original ETA until
aligned feedback, preventing replay or inferred arrival from elapsed time alone.
A future physical binding must reuse existing `Chute.getAngleForBin/moveToBin`,
servo readiness and `stepper.estimateMoveDegreesMs`; this slice exercises an
injected simulator and does not provide a production hardware binding.

`acknowledge_index(IndexCommand, stopped, position, now, physical_release_at)`
is the owner-thread callback seam, alternatively driven by tick's motor polling.
The command includes an opaque runtime epoch and absolute target; old-runtime,
duplicate and older-index confirmations cannot affect a new outstanding index.
Only the FIFO's P0->EXIT completion creates a `PhysicalDischargeEvent`, keyed by
`(epoch, pocket_id, generation)`, with final destination, state, boundary and
immutable optional metadata. DISCARD uses the configured discard destination
through precisely the same motion/discharge path. `take_discharge_events()`
drains an in-memory outbox without invoking consumers or gating motion. Events
are once-only within this runtime; crash-durable accounting is future work.

RUNNING accepts intake; `start_drain(now)` closes intake and enters DRAINING,
continuing empty-P6 indexes until every retained load physically exits. DRAINED
is reached after the final gravity fall-clear, with no further indexes; `close`
then retires the bridge without waiting for providers. After each discharge,
only Slice 2's fall-clear holds chute departure; no additional cooldown applies.
The callback can supply measured physical release time; otherwise completion
starts the conservative timer. Duplicate confirmation never extends it.

Command rejection, uncertain submission or unexpected C4 position/motion at an
idle confirmed boundary latches FAULTED. An active index stopped short remains
HOLD with its target retained. No automatic relative-command retry
or interrupted-motion resume is authorized. A matching physical completion may
still be recorded after a fault without re-enabling motion. Resume needs a later
remaining-distance/release-readiness contract. All callbacks are single-writer;
recognition workers retain the Slice 3 mailbox boundary.

This Slice 4 supersedes the former roadmap item numbered 4. Advisory accounting,
physical bindings/calibration and runtime cutover remain future scope. Simulation
proves composition, not live geometry, arrival freshness or gravity calibration.

## Remaining migration slices

5. First-class C1 Exit camera, physical-state sensing and shared UI parity.
6. Concurrent vision-driven C1/C2/C3 speed control and physical handoff sensing.
7. Bounded adaptive jam recovery and operator Resume without historical locks.
8. Four drain scopes and sustained-empty confidence, including Drain All.
9. Explicit runtime cutover, removal of obsolete transport dependencies, and
   separately approved hardware qualification.

Do not bridge the replacement through KnownObject, PieceTransport, UUID matching,
transfer episodes, reservation/reconciliation or Harvest ownership. Retain
historical records and legacy machine support outside this runtime.
