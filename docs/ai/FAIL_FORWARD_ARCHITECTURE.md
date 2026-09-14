# Fail-forward C4 migration

This is the durable scope record for the accepted architecture and Slices 2-5.
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

## Slice 5: opt-in physical binding and calibration

[`physical_binding.py`](../../software/sorter/backend/subsystems/classification_channel/physical_binding.py)
adds `PhysicalC4Binding`, requiring the explicit mode
`fail-forward-physical-experimental`. No production selector imports it. It takes
already initialized C4 `StepperMotor`, `Chute`, every physical layer's existing
servo, a destination-to-`BinAddress` map, recognition callable and calibration.
Construction performs no device I/O or motion. The caller must establish exclusive
hardware ownership, empty transport and a verified stationary P6 origin before
use. This is not a resume, homing, enabling or deployment interface.

`binding.tick(now)` supplies one monotonic clock to the accepted runtime and a
checked motor wrapper. Existing `StepperMotor.move_steps`, direction conversion,
acceleration, command ACK, `stopped` and fresh `position` reads do the work. FIFO
absolute targets (rounded once per boundary) produce relative command deltas;
there is no separate motor stack or ten-position modulo reset. A successful
submission ACK alone never advances the FIFO. Acknowledged submission followed
by stopped-at-target feedback advances once; old/duplicate runtime-epoch command
callbacks retain Slice 4 idempotency. Repeated ticks never resend a relative move.
Missing/rejected command ACK, disabled/stalled hardware, feedback exceptions or
completion timeout latch FAULTED and prohibit further C4 commands. A stopped-short
index remains outstanding until timeout; no automatic retry/resume is provided.
The existing explicit completion callback can record verified physical completion
after a fault but cannot authorize more motion. Never use a transport ACK as that
callback's stopped/position evidence.

`CalibratedChute` uses the existing `Chute.getAngleForBin()` aiming geometry and
`moveToBin(require_ack=True)` command path. The opt-in keyword checks the existing
stepper ACK that the legacy ETA-returning API previously discarded. It changes
no production caller's default behavior. The expected integer target uses the same
stepper degree conversion as that command, and alignment requires fresh stopped
and target-position feedback plus all doors at their calibrated targets.
`ServoMotor.command_door/door_at_target` and the corresponding Waveshare methods
reuse their existing endpoints, speed setup, command transport and feedback.
Waveshare verification does not accept its legacy cached-position fallback.
PWM position reports the firmware motion profile, not independent flap sensing;
physical qualification must establish what that evidence supports.

Destination mappings are copied, validated against the existing layout and filtered
for reachability before worker results enter Slice 3. Missing/ambiguous answers,
unknown destinations and recognition/provider/advisory exceptions become DISCARD.
No live allocation, reservation, ownership, PieceTransport, tracking, Harvest or
accounting API enters the adapter. All-layer-open discard passthrough retains the
current chute azimuth, as in existing distribution; it is not an invented bin angle.
Accounting consumes Slice 4's outbox separately. Runtime device ownership also
requires keeping layout, calibration, motor settings and destination mapping fixed
for its lifetime; changes require a new reconciled runtime.

`C4Calibration` exposes these explicit, offline-supplied parameters:

- Exact output `microsteps_per_revolution`, logical `clockwise_sign` and
  `origin_microsteps`. One index remains exactly one tenth of a revolution;
  ten pockets and seven P6-to-EXIT advances remain locked by Slice 1.
- `release_fraction` locates P0 release within the seventh sweep in motor
  coordinates (`release_microsteps`). `release_after_start_s` independently
  supplies the conservative start-to-release interval to Slice 2. Neither is
  inferred from a gravity delay or used as actual release evidence; physical
  measurement must establish both under the selected motion profile.
- Existing `stepper.estimateMoveDegreesMs()` at the chute's configured operating
  speed supplies rotational ETA. `door_travel_s` bounds concurrent door travel;
  `chute_eta_scale` (at least one) and `chute_eta_allowance_s` adjust their maximum.
  `arrival_margin_s` is Slice 2's separate readiness margin. In-flight ETA is fixed;
  an expired prediction never becomes alignment and is not refreshed by ticks.
- `index_timeout_s` and `chute_timeout_s` bound missing completion evidence.
- `fall_clear_s` defaults to **1.5 seconds only for compatibility**, not calibrated
  truth. An explicitly reported physical release time starts fall-clear; absent
  that evidence, confirmed index completion starts it conservatively. No predicted
  release or release-coordinate crossing is treated as a measured release.

Local qualification runs the Slice 1–4 regression files, `test_physical_binding.py`,
existing chute aiming, stepper estimation and servo tests. Simulated device/bus
fixtures establish software contracts only. Before the first separately approved
controlled physical qualification, establish clear hardware/exclusive ownership,
verify actual gearing/sign/origin and calibrated door/bin endpoints, and measure
release timing, travel ETA/margins and gravity fall-clear. No hardware operation,
production cutover, configuration write, deployment or push belongs to Slice 5.
Software validation on Python 3.12.12: 153 tests passed across that surface.
Pyright reported zero errors for the Slice 1-5 modules and chute. Expanded
hardware-file type checking reported three unchanged baseline errors (stepper
status bitmask typing, Waveshare calibration possibly-unbound position and
missing recalibrate return). Three broader stepper endpoint tests also fail
unchanged at starting commit `f66e5da` because fixtures lack `enable_force`.
These baseline issues were reproduced separately; no failures were hidden or
converted to skips. Independent review's torque-ACK finding was corrected and
re-reviewed without remaining findings.

This slice supersedes the former roadmap item numbered 5.

## Remaining migration slices

6. First-class C1 Exit camera, physical-state sensing and shared UI parity.
7. Concurrent vision-driven C1/C2/C3 speed control and physical handoff sensing.
8. Bounded adaptive jam recovery and operator Resume without historical locks.
9. Four drain scopes and sustained-empty confidence, including Drain All.
10. Explicit runtime cutover, removal of obsolete transport dependencies, and
   separately approved hardware qualification.

Do not bridge the replacement through KnownObject, PieceTransport, UUID matching,
transfer episodes, reservation/reconciliation or Harvest ownership. Retain
historical records and legacy machine support outside this runtime.
