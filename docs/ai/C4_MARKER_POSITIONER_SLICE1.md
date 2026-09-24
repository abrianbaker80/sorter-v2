# Slice 1: blue-marker C4 positioning

Status: Slice 1 COMPLETE and frozen after authorized empty-rotor qualification.
The circuit completed after P1 recovery; maximum final residual was 0.415504
degrees. Production-controller integration remains outside this slice.
[Signed policy, deployment and live evidence](../../../analysis_artifacts/c4-positioner-signed-20260922/RESULT.md).
No FIFO migration or recognition changes are part of this slice.

## Ownership and deployment baseline

The inspected deployed backend uses `ClassificationChannelStateMachine` ->
`TwoPieceClassificationChannel._startTrackedOutputMove()` ->
`StepperMotor.start_tracked_move()`; `_pollMotion()` polls
`tracked_move_complete()`. These receipts validate motor identity, command
generation, enabled/stall state, stopped feedback and firmware pulse counter.
They do not establish rotor angle. The deployed passive marker module observes
these receipts but never controls the rotor.

This differs from the older indexed-pipeline source in this candidate checkout.
The exact deployed source was captured for inspection. No older local hardware,
recognition, C3, or controller files were copied over the deployed baseline.

The new layer is a callable C4 positioner. It is not wired into the currently
deployed two-piece controller: that controller requests variable sweeps using
piece geometry, whereas this API accepts the next physical index. Converting
its track-based custody into FIFO custody is explicitly outside Slice 1.

## Position evidence

`blue_markers.py` reuses the installed passive detector's blue hub annulus,
component grouping and unique dashed-spoke assignment. It fits absolute phase
from at least seven independently identified solid spokes. Missing/ambiguous
dashed identity or insufficient corroboration produces no usable phase. Motor
commands and LEGO tracks never choose a 36-degree phase alias.

`BridgeMarkerSource` reads the existing camera bridge's JPEG APP15 v1 identity:
source epoch, sequence, capture monotonic nanoseconds and wall nanoseconds.
It does not open another camera, alter settings, or change perception.
After stopped motor feedback, a health request establishes a source-clock
fence. The admitted maximum frame age is added to that fence. Verification
requires later captures plus a 200ms settle interval, at least three distinct
captures spanning 100ms, and no more than 0.25 degrees of spread. The window
works at 30, 60 and 120 FPS; it does not depend on slow polling.

Each JPEG is checked against current bridge health to reject stale snapshots,
restarts, mismatched geometry and invalid identities. Local monotonic time
bounds request age and operation deadlines. Host clocks are not compared.
This uses the bridge's source-capture metadata contract. Its sensor-exposure
stamping implementation has not been independently verified; neither HTTP nor
OpenCV retrieval time is described as sensor exposure time.

## One-time marker-to-pocket calibration

There need not be existing pocket numbers. With the rotor empty and correctly
aligned to receive from C3, designate that receiving physical pocket as pocket
0 at station P6. Record a durable description of its relation to the dashed
marker. A stationary image cannot establish correct intake alignment or motor
polarity by itself. Establish the configured command sign for clockwise rotor
travel through bounded physical qualification before accepting the mapping.

`calibrate_mapping()` collects stationary marker evidence for the explicitly
known boundary without moving any hardware. The dashed reference and solid
constellation define `zero_phase_deg`; index n targets zero + n * 36 degrees.
Physical labels stay fixed when tracks appear, disappear or change identity.

`MarkerMapping.save_new(path)` atomically publishes a checksummed mapping and
refuses to overwrite an existing one. `load(path, geometry, motor_coordinates)`
rejects corruption or incompatible camera geometry/motor coordinates. The
fingerprint covers camera identity/center/resolution/detector version and motor
inversion, gearing, logical and driver microstep settings. A physical camera,
rotor or marker remount also requires recalibration; software cannot detect an
unreported mechanical remount from an unchanged configuration hash.

The operator confirmed that C3 is exactly halfway between two rotor fins. A
fresh stationary capture records that receiving pocket as pocket 0 at P6 and
the fitted zero phase as 195.772024 degrees in the current image coordinates.
The physical reference is saved with capture identity in the slice evidence.
The mapping was subsequently persisted after positive steps were proven clockwise.
Its zero phase remains exactly 195.772024 degrees.

## Positioner API and integration contract

Construct `TrackedStepperIndexMotor(stepper, platter)` with the deployed tracked
motor implementation, then `MarkerPositioner(motor, source, mapping, on_fault)`.
The caller supplies its existing fault/stop handler and retains motion admission,
pause/recovery, discharge preparation, fall-clear and all physical custody.
The layer does not introduce an operator-safety framework.

1. `begin_bind(boundary)` verifies the stationary phase at the caller's durable
   boundary. It cannot infer full revolution count or change an occupied ledger.
2. `poll()` returns `None` while pending, raises `PositionError` on failure, or
   returns one `ConfirmedIndex` containing boundary, measured phase, residual,
   correction count and source capture identity.
3. `request_index(boundary + 1, configured_speed)` rechecks the old physical
   boundary, requests approximately 36 degrees, awaits the original motor
   receipt, fences fresh marker captures, and verifies the target.
4. Only after consuming that confirmation may a later single-writer FIFO
   runtime commit its pending boundary. No FIFO is imported or mutated here.

An adjacent clockwise index is the only normal move. Duplicate/skipped targets,
unbound instances, concurrent requests and rebasing are rejected. Confirmation
is consumed once. Idle rotor displacement, driver faults, replaced receipts,
camera restarts and motor-coordinate changes cannot become confirmed indexes.
The existing caller fault handler runs once; the positioner remains faulted and
cannot retry uncertain motion. Integrators must not reset the instance to bypass
that condition. Keep the single motion owner through receipt consumption/FIFO
commit, including against manual motion.

Limits: 0.5-degree target tolerance; eight seconds for the entire index;
at most two signed corrections, each <=4 degrees absolute, total <=6 degrees
absolute travel. Each correction must reduce absolute error by more than 0.1
degrees before confirmation or another trim. A reverse trim corrects the same
pending target; normal logical indexing stays forward-only. Excessive residual,
nonconvergence or missing evidence faults without blind retry. No firmware,
speed, acceleration or stall defaults changed.

`begin_target_trim(boundary, speed)` permits an unbound maintenance instance to
observe and trim a caller-attributed target within this same envelope. It never
sends an index-sized recovery or rebases a bound positioner. Confirmation alone
establishes the maintenance boundary; it does not commit production ownership.

## Validation and remaining physical work

Targeted Python 3.12 tests cover real synthetic blue-marker images, dashed/solid
ambiguity, phase wrap, camera identities/age/epochs, persistence, all ten indexes,
ACK-with-slip, bounded correction/nonconvergence, large-overshoot refusal,
fault/receipt ownership, high-FPS stability and motor-coordinate drift.
Independent review found two defects (high-FPS window and newly adopted motor
coordinates); both were corrected and independently rechecked.

A bounded read-only live observation produced 16/16 valid phases, 13 stable
windows, 0.051465 degrees range and 0.013047 degrees standard deviation while
the motor was stationary. No motion command, restart, home, drain, provider call,
incident change, production source change or mapping write occurred.

The subsequent authorized live circuit passed: P1 recovered using one
-0.709576-degree trim to residual -0.061392 degrees; nine remaining forward
indexes needed no corrections. P0 returned at 195.356520 degrees, within 0.5
degrees of the unchanged reference. Forward correction convergence passed
targeted tests; no live forward trim was needed. 50 targeted positioner tests
passed and independent review approved the policy and owner adapter.
This qualifies empty-rotor positioning, not loaded sorting or FIFO custody.
Slice 1 is frozen. Slice 2 has not begun.
