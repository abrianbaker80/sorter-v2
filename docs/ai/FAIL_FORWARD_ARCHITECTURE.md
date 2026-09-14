# Fail-forward C4 migration

This is the durable scope record for the accepted architecture and Slice 2.
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

## Remaining migration slices

3. Bounded recognition/capture adapter with pocket-generation results and discard.
4. Advisory Harvest routing and asynchronous, idempotent discharge accounting;
   no transport pauses, reservations or persistence gates.
5. First-class C1 Exit camera, physical-state sensing and shared UI parity.
6. Concurrent vision-driven C1/C2/C3 speed control and physical handoff sensing.
7. Bounded adaptive jam recovery and operator Resume without historical locks.
8. Four drain scopes and sustained-empty confidence, including Drain All.
9. Explicit runtime cutover, removal of obsolete transport dependencies, and
   separately approved hardware qualification.

Do not bridge the replacement through KnownObject, PieceTransport, UUID matching,
transfer episodes, reservation/reconciliation or Harvest ownership. Retain
historical records and legacy machine support outside this runtime.
