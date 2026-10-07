# Project policy and historical state

[Brian's authoritative project boundaries](../../AGENTS.md) and
[the active workflow](../CODEX_WORKFLOW.md) govern current work. The dated entries
below record historical implementation, deployment and validation snapshots;
they are not live machine state, current authorization or additional recovery
and qualification gates. Their contents and accepted evidence are preserved.
The default reject-and-continue policy is documented only; this instruction
update implements no runtime change and does not authorize one.

## Physical C4 runtime Slice 3 COMPLETE LOCALLY - 2026-09-23

PhysicalC4Controller/PhysicalC4Runtime now compose the ten-pocket FIFO,
frozen marker positioner and recognition journeys for both former TwoPiece
and indexed runtime selections. Admission reserves UUID/episode/pocket/generation
before C3 release; only ConfirmedIndex advances custody. Uncertain arrivals and
recognition become DISCARD. Existing C3 recovery policy is extracted unchanged.
Original C3 history and carousel snapshot/freefall/burst capture remain optional,
with complete-frame provenance, asynchronous recognition and one request per UUID.

Pause retains custody and journeys; resume verifies markers before handoff work.
Complete recovery freshly establishes markers, drains all ten positions to Reject,
retires generations and planned Harvest allocations, and returns empty at P0.
Restart with unknown custody uses that recovery before admission. Confirmed
Harvest history remains intact. Normal motion bypasses TwoPiece, track ownership
and the generic C4 watchdog; physical/control faults retain custody and pause.

Independent review approved; its final targeted run passed 189 tests. The final
new runtime suite passes 45 tests. Expanded regression runs: 477 passed / 7 baseline
failures; perception: 76 passed / 1 baseline failure. All eight failures reproduced
using baseline modules without changing the checkout. These are scoped local
results, not a full-backend or physical qualification claim. Frozen Slice 1 and
Slice 2 capture helpers retain their captured hashes; Slice 2's journey API has
only the physical-reservation integration contract extensions.

No deployment, hardware operation, provider calls or production migration.
Changes remain uncommitted in the existing dirty checkout. Source inventory and
hashes: [manifest](../../software/sorter/backend/analysis_artifacts/slice3-local-validation/source-manifest.json).
Validation, baseline failures and review: [results](../../software/sorter/backend/analysis_artifacts/slice3-local-validation/results.json).

## Recognition Journey Slice 2 local contract COMPLETE - 2026-09-22

KnownObject UUID now has an isolated recognition journey contract starting on
C3, with generation-scoped observation aliases, copied views, exclusive crossing
proof, bounded diverse selection and one asynchronous Brickognize request.
Original crop collector, drop buffer/history, quality/dedup and request builder
are reused. Invalid/uncertain optional views are omitted without transport effects.
88 focused tests passed; independent review approved and passed 16 regressions.
One unchanged crop-quality baseline test failure is documented in the report.

No existing software changed: production controller wiring, physical FIFO,
transport and frozen Slice 1 are untouched. No deployment, live sorting or
Slice 3 work. Future integration must supply original complete scene, epoch,
generation and alias-bridge evidence; this is not live/loaded qualification.

[Contract and evidence](RECOGNITION_JOURNEY_SLICE2.md).

## C4 marker positioning Slice 1 COMPLETE; frozen - 2026-09-22

Signed residual correction and the supported maintenance adapter are deployed.
50 targeted positioner tests passed; independent review approved the bounded
policy. P1 recovered with one -0.709576-degree trim, then all nine remaining
forward indexes passed through P0. Every target was marker-confirmed within
0.5 degrees; P0 returned at 195.356520 against unchanged 195.772024 degrees.
No live forward trim was needed; forward convergence passed targeted tests.
Positive steps are proven clockwise. Limits remain two trims, 4 degrees each,
6 degrees absolute total, the original deadline and existing fault owner.

Two files deployed with rollback retained; 409 protected source files, machine
configuration, mapping and prior failure evidence remained unchanged. Backend
PID 175268 is initialized and stopped without hardware error. No FIFO, TwoPiece,
recognition, C3, firmware or geometry changes. Slice 2 not started.

[Deployment hashes, complete move table and evidence](../../../analysis_artifacts/c4-positioner-signed-20260922/RESULT.md).

## C4 ColorChecker24 correction overlay deployed; C4 static profile applied - 2026-09-21

The six-file C4-only ColorChecker24 correction overlay is installed and exact-hash verified (manifest SHA-256 30f0cb730211b14c93adb736503dfb2538d2e868321956099670e5df64a40c3b). Its 26 offline deployment-runner tests passed, with compile, Ruff, and manifest checks also passing. The post-install runner defect was corrected to use the verified pre-stop tuning snapshot; the already-installed candidate then started through one supervisor request without repeating installation or stop. Backend PID 76263 is healthy.

One C4 color calibration request completed with HTTP 200. The saved profile is enabled and active for classification_channel only, from all 24 patches of the Calibrite ColorChecker Passport Photo 2 (standard Classic 24-patch target). The response reports applied_live=true, active=true, global gate=true, and camera_settings_changed=false. Fit errors are 4.7858 mean / 11.5338 max; six-fold validation errors are 5.4462 mean / 12.8083 max CIE76. Post-GETs confirm the saved reference, matrix, and all other configured camera-role profiles remain identity/disabled.

The system reports hardware ready with no error, runtime paused with is_running=false, zero piece counters, and no active incident. All cameras are online and the supervisor remains healthy. C4 source and picture settings are unchanged. Exposure 250, ISO 300, contrast 20, brightness 50, saturation 50, white balance 6500 K, auto exposure/WB off, power-line frequency 2, sharpness 50, focus 60, autofocus off. The C4 LED is assigned only to distribution:6 at 100%; C2/C3 are unassigned and the operator confirmed the LEDs are on.

C2-C4 are operator-confirmed clear. No motor command, database change, or Hive access occurred; UUID/database evidence remains preserved. This completes static C4 color calibration under the current chart/lighting/settings only. The earlier offline moved/rotated capture 03 score (DeltaE00 mean 6.9173 to 3.2811, 23/24 patches improved) belongs to that offline profile evaluation, not a separate holdout score for the newly saved live matrix. C4 mechanical sorting/routing and other illumination remain unqualified.

[Deployment, current profile, live-state and offline evidence](../../../analysis_artifacts/c4-color-correction-qualification-20260921/qualification-status.md).

## Terminal refusal refresh deployed; RUNNING - 2026-09-19

94642 stale feeder tick plus zero safe sweep no longer immediately terminalizes.
Recovery waits newer paired frames/current feeder tick, then checks originalidentity
and forward/jitter envelope. Existing deadline/discard/sweep guards unchanged.
197 targeted tests passed; independent128 approved. Three files deployed,675hashes
verified/672protected unchanged. Authorized C4drain swept10/reconciled7; no manual
clearing/C3jog. Oldepisode not replayable in place after spent recovery.
Live82417609 refreshed then confirmed arrival;94a839a3 became discard-bound and
FIFOcontinued. Final13episodes/6completed/zero unresolved/noincident; allfeeders
and auto restored, RUNNING. Oldepisode freshnumeric envelope was not measured.

[Exact scope, review, hashes and live evidence](../../../analysis_artifacts/c3-refusal-refresh-20260920/result.md).

## Retained C3 observed-position recovery deployed; RUNNING - 2026-09-19

Fresh unique original identity now permits finite owned jitter/observed-position
continuation toward the operator-confirmed far end of the saved exit sector.
Every move rechecks follower clearance and C4 alignment; rotor travel is not
piece progress. Existing terminal identity/discard/admission helpers unchanged.
186 targeted tests passed; independent final review approved with54 tests.
Complete C4 Recovery swept10/reconciled3 records; no manual clearing or C3 jog.
Current labeled frame could not prove old track1; no recovery was issued for it.
Three approved files deployed;675 hashes verified/672 protected unchanged.
Normal home/Start/all feeders/auto restored. Ordinary sample13 confirmed arrivals,
6 completed loads, zero unresolved/no incident. No live retained event yet (0/0).

[Exact changes, current-frame limits, hashes and live results](../../../analysis_artifacts/c3-retained-path-20260919/result.md).

## Transfer finalization deployed; new physical retention BLOCKED - 2026-09-19

Budget/progress refusal now preserves existing observation, with no further motion;
terminal spatial continuity alone cannot veto discard. Explicit pre-dispatch audit
keeps slow-preparation refusal distinct from motor rejection. Motion rules unchanged.
196 targeted tests passed; independent107 approved. Complete C4 Recovery swept10,
cleared7 retained records/old4f4f incident; C1/C2 preserved, no manual clearing.
Two approved files deployed,675 hashes verified/673 protected unchanged. Restart,
home and Start succeeded, all feeders/auto restored. Two ordinary arrivals confirmed.
New0831f011 originaltrack1 remains atC3 after12.174s; genuine retention guard faults.
Paused with ownership intact; no hardwareerror or further scope changes.

[Exact scope, hashes, validation and physical fault evidence](../../../analysis_artifacts/c3-finalization-20260919/result.md).

## Terminal physical-evidence correction deployed; RUNNING � 2026-09-19

Episode ad8481 lost original350 but unrelated tracks reseeded its support;
the expired-budget fallback incorrectly treated that envelope as retention.
One-file correction uses original identity or unbroken pre-loss continuity,
prioritizes confirmed C4 arrival, and waits for fresh evidence without adding
motion/time. Genuine retained-piece and motor/ownership faults remain protected.
188 targeted tests passed; independent review passed40 checks and approved.
Authorized Complete C4 Recovery cleared occupied restart boundary without manual
clearing; C1/C2 preserved. Payload242d0223...056c73d deployed;673 hashes verified,
672 protected unchanged. Normal home/Start and all feeders restored.
Ordinary sample22 transfers,14 completed loads,3 physical recoveries confirmed,
zero unresolved/no incident. No natural discard-bound event after this deployment.
Left normal RUNNING.

[Cause, exact fix, validation and live evidence](../../../analysis_artifacts/c3-terminal-evidence-20260919/result.md).

## Waiting-for-operator correction deployed; production RUNNING � 2026-09-19

Explicitly authorized Complete C4 Recovery swept10/10 positions to reject,
cleared7retainedrecords/associatedincident,homedREADY;C4cameraempty.C1/C2material
preserved,no manualclearing. Exactapprovedpipelinehashf6d392a2...625871d7e
installedunchanged.All673hashesverified,672protectedunchanged.Prior89tests and
independent54testpass reused.Normalhome/Start,C1/C2/C3true,auto_channels.
Ordinarysample14transfers,7completedloads,0unresolved/noincident/hardwarefault.
No naturallyoccurringdiscard-bound handoff in sample.LeftnormalRUNNING.

[Deployment, physical boundary and production evidence](../../../analysis_artifacts/c3-terminal-deployment-20260919/result.md).

## Lost-handoff terminal veto corrected locally; occupied deployment BLOCKED � 2026-09-19

Current79bc5297 differspriorbrownstall: originalC3trackabsent,58freshmissingframes;
unknownobjects inoldcorridor vetoeddiscard thenraisedgenericoperatorincident.
One-file correction removesonlyunknowncorridorveto,keepspositiveowned-ID and
freshness/motion/alignmentguards; clearsmatchingincidentonresolution.
89targetedtests pass;independentreview approved(54rerun).NOTDEPLOYED.
Existing retained-recovery invokedonce;installedveto recurred. SixotherC4loads
remainowned. No restart,blanketdrain,manualclear orownershipdeletion; userexcludes
blanketdrainforsinglehandoff. Preparedfixblockedpending supportedrestartboundary.

[Exact cause, hashes, validation and live boundary](../../../analysis_artifacts/c3-terminal-fallthrough-20260919/result.md).

## Bounded owned C3 jitter deployed; physical sweep BLOCKED � 2026-09-19

Three-file jitter recovery deployed;232 targeted tests and independent review pass.
Original firmware single-cycle jitter, max3 within unchanged12s, positive original
C3 identity and both sweep checks. All673 hashes verified;670protected unchanged.
Accepted completeC4 drain/reset prepared deployment, no manual clearing. Home/Start
succeeded; first retained brown-piece transfer still blocked by following material
crossing forward_sweep_clear. Zero live jitter commands; effectiveness not yet
qualified. Paused with episode051805f0 ownership preserved; no hardware error.
Recognition/C4 recovery/flaps/masks/configuration unchanged.

[Exact scope, tests, hashes and live blocked result](../../../analysis_artifacts/c3-jitter-20260919/result.md).

## Complete C4 recovery — deployed/live PASS, 2026-09-18

Explicit destructive operator recovery is installed: main-header **Drain C4 to Reject & Reset**, POST `/api/system/c4-drain-reset`. Full10-pocket reject circuit plus normal homing succeeded against retained current episode; fresh camera view showed C4 empty. Normal Start restored C1/C2/C3 auto sorting. Latest captured sample:21 transfers,14 completed,zero unresolved, no active incident/hardware fault; left RUNNING. No manual C1/C2/C3/C4 clearing. 231 tests passed; independent review approved;11 payloads hash verified,662 protected files unchanged. Recognition work stopped; accepted recognition/flap/motion/configuration preserved. Full frontend formatting retains baseline failures (136 files); check/build pass.

[Scope, validation, exact manifests and live result](../../../analysis_artifacts/complete-c4-recovery-20260918/result.md). Earlier blocked/clearance instructions below are historical and superseded within this explicitly authorized destructive recovery scope.

# Current Development State

## Normal sorting restored; live C4-only and discard path verified, 2026-09-18

Brian replied "clear" to C4/chute status check. Supported home reachedREADY,
C1/C2/C3 enabled, auto_channels resumed. Both reviewed fixes deployed unchanged,
all323 source hashes verified. No additional manual removal requested. Original
597225 ownership audit was archived during authorized restart, not naturally
discarded; do not claim otherwise.

Ten actual outbound provider requests:0/10 cross-piece C3+C4 mixes,10/10 C4-only,
0/10 proven C3 inclusions, one combined call per classified record. No trustworthy
cross-camera linkage available; ready-history multi-view is NOT claimed restored.
Some C4 images show guide/spoke hardware; no out-of-scope detector/mask edits.
Final classify-success7/36=19.44%, status ratio not accuracy.

Natural new lost-handoff0168d2dc... became c3_handoff_unverified, progressed in
FIFO, rejected and retired as unverified_pocket_cleared while following loads
continued. Counter1, no unresolved transfers, no active incident, hardwareREADY.
Left normal production RUNNING, no observation pause or artificial limits.

[Deployment, live payload audit, natural reject retirement and final status](../../../analysis_artifacts/authorized-association-restart-20260918/result.md).


## Both fixes deployed through explicitly authorized restart, 2026-09-18

User explicitly directed "apply the fix with a restart". Both reviewed payloads
installed unchanged: recognitionca8c8f22...01d3d38 and pipeline6413cbad...fb198372.
PID63876 ->71507; bounded startup passed; all323 hashes verified (321 protected).
Seven previous ownership records archived with physical dispositionUNKNOWN, no
fabricated delivery/reject/physical-clearance credit. No natural drain occurred.

HardwareSTANDBY/no error; controller stateinitializing, feeders still disabled.
No homing/start or new live provider sample yet. Asked ONLY observation of whether
C4/chute empty or still loaded, without moving material; awaiting answer. No manual
clearing requested. Status: restarted successfully, production not yet resumed.

[Deployment authorization, exact hashes, audit and receipts](../../../analysis_artifacts/authorized-association-restart-20260918/result.md).


## Current597225 evaluated without clearing; missing-incident fix prepared, 2026-09-18

User revoked manual reconciliation request. Fresh C3 production image shows retained
exit region clear; old material observation is stale. No physical retention claim
from the terminal snapshot. Supported exact retained-recovery API queued, but owner
refused `Retained recovery reservation or incident no longer matches`: current
incident is null. No motion/attempt spent. `_waitArrival` terminal early return
prevents refreshing support and evaluating discard. Ownership remains intact.

Only missing-incident predicate corrected locally (allow absence or matching bounded
incident, retaining atomic compare-and-clear).32 focused tests pass; independent
review approves. No added retry/timer/motion, no transport redesign. Exact accepted
recognition payload unchanged. Current process cannot load changed method through
an established supported control path; restart loses occupied ledger. No live source
changes, restart, reset, injection or manual request. All323 live hashes unchanged.

Paused, hardwareREADY, feeders disabled; six admitted owners plus unresolved reserved
pocket remain. No natural drain or post-deployment sample. SORTING STATUS: BLOCKED.

[Fresh evidence, exact API refusal, predicate patch, tests and execution boundary](../../../analysis_artifacts/current-transfer-597225-20260918/result.md).


## Strict image association correction ready; restart boundary pending, 2026-09-18

Three recorded cross-piece requests traced to fresh c3_transfer crops. Pipeline
stamped the receiving UUID/worker cycle onto a C3-local crop, then treated those
copied fields as physical identity proof. No independent cross-camera identity
exists in current indexed records. Original collector captured226 C3 crops;
most earlier same-track crops exceeded unchanged1.5s horizon. No basis to force
history into provider requests or claim restored positive cross-camera linkage.

User explicitly authorized excluding C3 when proof is unavailable. One-file
recognition correction now rejects unproven indexed C3 candidates immediately,
consumes optional candidate once, preserves C4 selection and one provider request.
70 focused recognition/capture tests pass in exact-installed replay. Independent
review approved payload ca8c8f22cb05026d564e186d0afe8e2ba25eaf6d6164a3881efc74d5b01d3d38.
Transport/recovery/motion/flaps/masks/thresholds/serializer unchanged.

Deployment NOT executed yet. Runtime had independently resumed and subsequently
terminalized new episode597225bb094b4d74b7c2f84034704229, boundary49, local C3 ID444,
with forward_sweep_clear=false and positive_safe_travel=false. Six other C4 owners
remain. Drain attempt precondition caught changed lifecycle before issuing motion.
Paused via supported API, feeder flags disabled, chute stopped. No ownership clear,
reset, or new recovery commands. Controlled restart needed for loaded Python module;
occupied ledger cannot survive it. Minimum downstream C3-exit/transfer/C4/chute
REJECT reconciliation requested under user's restart exception; C1/C2 untouched.
Waiting for fresh physical confirmation. Frozen installer ready, no post-fix live
sample yet. SORTING STATUS: BLOCKED.

[Trace, exact patch, focused tests, review, hashes and deployment preparation](../../../analysis_artifacts/strict-image-association-20260918/result.md).


## Both fixes deployed; real multi-view association fault found, 2026-09-18

Latest one-time reconciliation authorization superseded the earlier blocked
checkpoint below. Brian moved retained C3-exit/C4/chute material to REJECT and
confirmed transfer path/C4/chute clear, hands clear; C1/C2 stayed loaded/untouched.
Both approved fixes deployed together with all4 exact approved hashes and all319
other installed module hashes preserved. Combined installed replay193 passed.
Reset/restart/home/start succeeded; normal feeders and auto_channels restored.

Actual outbound TLS payloads:23 requests,1-4 images each, one combined call per
classified record;8 C3-transfer+C4 and15 C4-only. No ready c3_history crop observed.
Three requests demonstrably combine different pieces (yellow C3/black C4 or the
reverse), so multi-view live acceptance FAILED. Five other C3/C4 pairs appear
compatible and show different perspectives. No claim of ground-truth accuracy.

Paused via supported /pause for this real software fault. HardwareREADY/no error;
ownership preserved, no extra clearing/restart/source edits.24 successful normal
arrivals,17 completed loads, zero unresolved handoffs; no natural recovery episode.
Classify-success17/24=70.83%, a status ratio, not accuracy. C1/C2/C3 remain enabled,
auto_channels configured, controller PAUSED. SORTING STATUS: BLOCKED.

[Deployment, exact hashes, receipts, fault and actual payload evidence](../../../analysis_artifacts/combined-reconciliation-deploy-20260918/result.md).


## Occupied recovery attempt — exact API blocker, narrow correction prepared, 2026-09-18

Latest user authorizes supported recovery of b252e637, natural C4 drain and exact
reviewed multi-view deployment, without manual clearing, ledger reset or new retries.
Live C3 images still show material at the retained exit location; recorded support
IDs300/332/346, missing-frame count0 and location_lost=false do not qualify for
discard-bound conversion. The exact retained-transfer recovery API returned409:
"The bounded geometry recovery attempt is already spent" because the installed
exit_clearance branch already has a recovery leg. No motion was issued.

Found and locally corrected the premature observation termination: the combined
refusal contained stale feeder_tick_fresh plus no progress/C4 nonempty. Adding
feeder_tick_fresh to the existing observation-only exception preserves the original
12-second deadline and permits the existing discard fallback only after fresh
disappearance. No new moves, retries, gates or budgets. One runtime file changed
locally, eight regression variants; **134 exact-installed replay tests pass**.
New correction is not deployed or separately independently reviewed.

Current terminal episode cannot reopen using installed controls; restart loses
occupied ownership. Six C4 owners plus the original reservation remain intact.
No manual clearance requested/performed, incident clear, reset or restart. Existing
idle C4 drain remains available after resolution but was not entered. The approved
three-file multi-view payload and all its hashes are unchanged, still not deployed.
All323 live source hashes/config unchanged; no new live provider sample.
C1/C2/C3 enabled, auto_channels, hardwareREADY; controller labelRUNNING but actual
flowBLOCKED. Classify-success99/186=53.23%; post-restoration rate unavailable.

[Evidence, exact live API refusal, correction diff and tests](../../../analysis_artifacts/occupied-recovery-deploy-20260918/result.md).

## Original multi-view restoration — prepared, deployment blocked, 2026-09-18

History distinguishes the older DYNAMIC global-track/fall crops from the later
REV01 C2/C3 collector + learned matcher. The installed matcher is disabled
(`enabled=false`, algorithm empty); the recent release-snapshot path also bypassed
its call. The legacy fall collector is not compatible with indexed-pocket identity
and uses polygon bypass/lower detection confidence, so it was not re-enabled.

Three-file recognition-only payload restores ready original C3 collector pixels
through exact release-frame/track/bbox and continuous tracker-incarnation binding,
then the existing episode/piece/cycle envelope. At most two C3/four total images,
one combined provider request, immediate C4 fallback. Capture cadence, configuration,
models, thresholds, transport, ownership, recovery, flaps and masks are unchanged.
Two focused test files added. Exact 323-module installed replay: **158 passed**;
independent review: **59 recognition tests passed**, final payload approved.

Deployment has NOT occurred. At this task's first live read, new episode
`b252e6370d014f599f81d553349fe8a2` was already unresolved with six other C4 owners.
The existing recovery decision reports `c4_empty`, `feeder_tick_fresh`, and
`observed_forward_progress` blocked. Restart would lose occupied ownership;
transport changes are expressly outside this request. No runtime command,
reconciliation, manual-clear request, restart or deployment was issued.

All 323 installed Python hashes and machine config remain unchanged. Hardware is
READY/no error; controller label RUNNING but actual flow BLOCKED by that incident.
Before classify-success is **99/186 = 53.23%**, not accuracy. After is unavailable.
The prior installed-version outbound capture's last ten requests show four truly
different C3/C4 pairs, C3 transmitted for all six eligible pieces, one call/piece.
These are BEFORE evidence, not a fabricated post-restoration live sample.

[History, exact files, tests, hashes, prior 10-piece table and blocker](../../../analysis_artifacts/original-multiview-restoration-20260918/result.md),
[payload manifest](../../../analysis_artifacts/original-multiview-restoration-20260918/manifest.json),
[independent approval](../../../analysis_artifacts/original-multiview-restoration-20260918/review.md).

## Brickognize outbound payload verification — 2026-09-18

Read-only, PID/Host-scoped TLS-write capture verified 13 real production HTTP
multipart requests with 31 JPEG payloads: image counts 1,2,1,3,1,4,4,2,2,1,3,3,4.
All had one completed combined provider call and a Brickognize listing receipt.
Six eligible C3 crops were selected; all six appeared in the wire payload. Visual
review found six requests with useful differing faces/perspectives, including four
C3+C4 requests; the other two C3+C4 pairs showed substantially similar faces.
Some C4 frames differ only in blur/framing. Distinct hashes alone do not prove
distinct angles. No production code change, restart or transport change was made.
Capture detached normally, with zero lost events/read errors. Normal resume was
used from the initially paused, fault-free runtime; production remains RUNNING.

The dashboard percentage is classified/(classified+unknown+not_found+multi_drop_fail)
over the runtime's retained piece records, excluding unfinished classifications.
The initial 59/(59+53+1+2)=51.3% is an accepted-classification outcome rate, not
ground-truth accuracy. The end-of-sample value was 75/(75+65+1+3)=52.08%; this is
additional production data, not improvement attributable to a new fix.

[Per-image payload hashes, timestamps, provenance and visual review](../../../analysis_artifacts/brickognize-wire-20260918/image-audit.csv),
[capture summary](../../../analysis_artifacts/brickognize-wire-20260918/summary.json).

## Optional multi-view and lost-handoff correction — deployed, RUNNING, 2026-09-18

Brian verified C4 physically empty and explicitly authorized retiring stale C4
ownership through minimum supported reset/restart. The seven stale episode records
were audited without normal delivery credit; C1/C2 material remained in place.
Six-file tested payload deployed atomically, PID47737 -> PID53444; all 323 source
checks passed. Mask/geometry remained identical. Normal recovery/homing and resume
completed; C1/C2/C3 are enabled and automatic feeding is active.

Final live sample: **27 transfers, 20 completed discharges, seven normal in-flight
loads, zero unresolved transfers, no active incident, hardware READY/no error**.
Eleven classified pieces each issued exactly one combined Brickognize request:
eight used optional C3 plus C4 imagery, three used immediate C4-only fallback;
requests contained 2-4 selected images. One additional multi-drop load correctly
skipped classification. No new lost-handoff case occurred; fallback/FIFO/reject
retirement remains covered by the accepted targeted tests, not a manufactured
live failure. All six deployed hashes reverified. Sorter left RUNNING.

[Deployment, reconciliation and live receipts](../../../analysis_artifacts/multiview-handoff-20260918/receipts/result.json),
[per-piece provider sample](../../../analysis_artifacts/multiview-handoff-20260918/receipts/live-sample.json),
[final production state](../../../analysis_artifacts/multiview-handoff-20260918/receipts/final-live.json).
The deployment constraint in the prior checkpoint below is now resolved.

## Optional multi-view and lost-handoff correction — tested, deployment blocked, 2026-09-18

Six-file payload is prepared against the exact installed baseline. It adds a
ready-only, release/episode/piece/generation-bound C3 crop, deduplicates available
C3/C4 views to at most four, and removes the parallel single-image provider call.
C4 alone remains sufficient. Known detached C3 followers no longer become owned
material through overlapping detection boxes. No-progress/nonempty-intake checks
retain the existing arrival-observation budget rather than immediately making
the episode terminal. Existing discard-bound FIFO/reject/accounting remains intact.

Focused self-review and exact-installed-source replay: **207 passed, one existing
crop-quality failure**, independently reproduced on unmodified installed source.
No classifier threshold/model or protected geometry changes. All **322** installed
application hashes verified unchanged; payload has **not been deployed**.

Current episode `ead8473fbaf54ad38a64b19c514cc0af`, boundary21/pocket1, remains
unresolved after the installed code absorbed known followers and stopped arrival
observation at 2.44 seconds. The existing retained-transfer recovery API was tried
and returned **409: The bounded geometry recovery attempt is already spent**.
It cannot selectively convert this terminal episode. Restart/reset would erase
six unrelated C4 owners; no supported live source reload or ledger rehydration
exists. Current instructions forbid that ownership loss and broad manual clearing.
No runtime injection, reset, restart, manual-clear request or ownership deletion.

Final observation: hardware READY/no error, controller label RUNNING, automatic
feeding and C1/C2/C3 enabled; actual flow **BLOCKED** by the unresolved reservation.
Seven pending episodes (six other loads), 15 completed, zero unverified retirements.
No post-deployment production sample is possible yet. A supported way to cross
this occupied-process deployment boundary remains unresolved; do not reuse prior
clearance authorization for this new episode.

[Payload hashes](../../../analysis_artifacts/multiview-handoff-20260918/manifest.json),
[exact-baseline test results](../../../analysis_artifacts/multiview-handoff-20260918/candidate-tests.txt),
[live API refusal and preserved ownership](../../../analysis_artifacts/multiview-handoff-20260918/runtime-blocker.json).

## Lost-handoff discard continuation — deployed and production resumed, 2026-09-18

Five-file installed-baseline correction implements the requested narrow
unconfirmed handoff -> original FIFO pocket discard-bound transition. Fresh
post-motion absence and intact receiving boundary are required; visible stalled
material and real faults retain existing recovery. Following admissions continue;
the uncertain pocket exits via existing reject/flap checks and retires without
invented physical-piece/Harvest credit. 211 affected tests pass; independent review
approved and independently validated the payload.

Brian explicitly authorized restart/reset/rehome and recorded the six cleared C4
loads as manually discarded, resolving the one-time deployment constraint.
The six episodes plus removed arch are preserved in a reconciliation receipt with
no automatic distribution credit. C1/C2 remained loaded. Exact five-file payload
deployed to PID47737; all 322 source checks passed. Normal homing completed and
C1/C2/C3 were re-enabled; `/resume` succeeded. Final ordinary production sample: 20
first-pass arrivals, 13 completed discharges, no incident or unresolved transfer.
Following pockets continue. No new lost-confirmation case yet in that sample;
unverified reject retirement remains software-tested pending natural occurrence.
Seven ordinary loads remain in flight; all 322 final source checks pass.
SORTING STATUS: RUNNING.

[Exact scope, tests, deployed payload and reconciliation/live receipts](../../../analysis_artifacts/lost-handoff-20260918/result.md).

## C3 observed-progress transfer — deployed and live verified, 2026-09-18

Frictional slip is established physical behavior. Three-file correction replaces
the single full-endpoint recovery restriction with fresh observed-progress
continuations capped by current follower clearance and the existing active budget.
C4 confirmation remains the sole admission proof; original episode/pocket remain.
Detached followers cannot become owned material merely by entering an old commanded
sweep corridor. Sweep geometry, overlap, C2 scheduling and flap repair are unchanged.

137 targeted tests pass; independent source review approved and independently
passed 97 focused tests. Brian confirmed the authorized one-time physical
reconciliation to reject. Three-file payload deployed/hash-verified in PID43395;
normal homing/startup restored C1/C2/C3 and RUNNING. At final verification:
25 admitted transfers (24 first-pass, one successful continuation), 19 completed
loads, zero unresolved transfers or active incident. Live slip case: 21.48 degrees
rotor travel versus 17.57 observed center progress; continuation capped to 19.42
degrees inside 19.43 follower clearance, then C4 confirmed once. 322 final source
checks pass. Multi-continuation behavior passed synthetic tests, not needed live.
No fixed-angle retries or scheduling guard added. Complete C4 Drain stays separate.

[Scope, tests, exact payload and live evidence](../../../analysis_artifacts/c3-observed-progress-20260918/result.md).

## Current recovery-envelope diagnosis — no source change, 2026-09-18

Captured episode 4f0406a2f698485194784c7406c84538 began its normal release with
2.09 degrees of recovery headroom. During 18.33 degrees of rotor motion, leader
COM advanced 7.50 degrees versus follower COM 18.84 degrees; reserve became
-8.62 degrees. Active-transfer staging was already prohibited. Limiting shared
rotor staging cannot increase relative spacing. A full recovery would put the
follower footprint 8.62 degrees into the exit opening; the far edge is not a
proven earliest-fall boundary. Neither requested software defect is established.
Independent review agrees; ten focused existing tests pass. No source changes,
motion, deployment, reconciliation request, or ownership clearance in this slice.
Paused occupied state below remains; complete C4 drain remains separately open.

[Exact geometric evidence and limits](../../../analysis_artifacts/recovery-envelope-20260918/diagnosis.md).

## Retained-transfer recovery deployed — production paused at real sweep obstruction, 2026-09-18

Brian confirmed the authorized ONE-TIME DEPLOYMENT RECONCILIATION to reject.
The exact seven reviewed files are deployed and hash-verified in PID 37015;
normal homing established empty ownership before feeders and sorting resumed.
Unchanged software evidence: 197 passing tests and focused review approval.
Ordinary production produced eight first-pass arrivals, two automatic single-move
geometry recoveries (42.97 and 45.27 degrees), and four downstream completed loads.
A subsequent transfer 4f0406a2f698485194784c7406c84538 is unresolved at boundary 10:
50.83 degrees required, following material at 42.21 degrees, forward_sweep_clear=false.
Paused with ownership preserved: six admitted C4 loads and the unresolved reservation.
No protection bypass or new manual-clearance request. COMPLETE C4 DRAIN remains a
separate open defect. Earlier snapshots below are historical.

[Deployment hashes, reconciliation and live results](../../../analysis_artifacts/retained-transfer-20260918/deployment-reconciliation/result.md).

## Flap routing integrity — local only, 2026-09-17

Addresses the four independent-review blockers in the uncommitted flap repair.
Manual flap/bin, servo setup, and sample-mode controls share the existing lifecycle
lock and reject RUNNING sorting or retained distribution/transport ownership.
READY and POSITIONING retain their original destination across refused manual
commands. Explicit POSITIONING resume retries only that retained flap route.

Automatic normal routing validates and verifies every configured flap: destination
closed, all others open. Sample, oversize, and no-bin pass-through retain
POSITIONING until all passage flaps settle. Required unavailable, uncalibrated,
rejected, failed, or unknown flaps hold release through the existing distribution
incident/gate paths. READY rechecks the complete flap path before authorizing C4.

Python 3.12.12 validation: **248 passed**, four existing FastAPI deprecation warnings.
Includes 42 new production-path cases in
software/sorter/backend/tests/test_flap_routing_integrity.py and all 46 unchanged
original flap cases. The affected suite also covers READY, POSITIONING/rehome,
SENDING, sample collection/speeds, servo/bus, chute aiming, lifecycle/coordinator,
indexed pause/pipeline, and Harvest distribution. Frontend unchanged; no frontend
or type-check rerun. Scope/hash and diff-whitespace checks passed.

Ready for independent re-review; not yet accepted for hardware qualification.
No deployment, hardware operation, calibration/assignment/geometry changes,
staging, or commit. The 8.8125-degree discrepancy and unresolved 443 evidence
remain unchanged. The prior repair report below is historical for this checkpoint.

## Bin-page flap repair — local only, 2026-09-16

Manual layer selection now matches automatic intercept/pass-through semantics.
PCA commands are serialized/deduplicated, rejected commands do not become reached
positions, and flap failures propagate to the API and block distribution completion.
153 affected backend tests pass; frontend check/build pass with existing warnings.
Existing formatting/type-check baseline failures are recorded in the report.
Uncommitted and not deployed; no hardware operation. Independent review and
controlled physical qualification remain pending. Bin assignments, geometry,
calibration, the separate 8.8125-degree discrepancy and unresolved 443 evidence
are unchanged. Earlier operational snapshots below remain historical evidence.

[Repair scope, exact checks and deferred evidence](../../../analysis_artifacts/bin-assignment-repair-20260916/repair-summary.md).

## C4 arrival accepted PASS; C3 removal reported, selective reconciliation unavailable

Brian accepts focused C4 arrival confirmation as **PASSED** on candidate `d62ca834927a48ab…`; do not repeat stress or reopen the implementation without a new missed physical arrival. The final non-arrival was a C3 exit transport blockage, not C4 confirmation failure.

Brian removed and retained that C3 exit blockage separately. This physical disposition is recorded against episode `9c087a5efe284d35842d9138fc6aee41`, but its live reservation is still unresolved. Fresh observation confirms healthy PID14703, paused runtime and six admitted C4 loads preserved. No motion or runtime mutation in this continuation.

The existing fallback clear API only clears the warning and does not reconcile an indexed bounded-transfer episode; terminal WAIT_ARRIVAL cannot resume it. No supported selective reconciliation exists in the installed process. Do not clear the warning as a substitute, inject code, or restart with occupied paths. Further physical clearance of remaining C2/C3/C4/chute material is needed for supported clean-controller recovery. No motion-guard defect established: the prior independent exit load blocked the forward sweep, and unknown grouping precluded reverse recovery. Later stages remain unexercised.

[Current removal evidence and precise reconciliation limitation](../../../analysis_artifacts/c3-exit-reconciliation-20260912/status.md). Sections below are historical; their statement that C4 arrival qualification is incomplete is superseded by Brian's acceptance.

## C4 fix deployed — focused stress blocked at C3 exit, 2026-09-12 local

Brian physically cleared C2/C3/C4/chute and explicitly superseded all pre-crash ownership. The tested arrival fix is now installed and hash-verified as archive `d62ca834927a48abaa9513c6f45fd7075af07b2c41a44649ac08b2e221bb4b12`; backend PID14703 healthy. Corrected atomic deployment/startup and clean zero-ownership initialization passed. No debugger or service-interrupting instrumentation was used in this continuation.

Focused stress: **35 attempted, 34 confirmed arrivals, 28 discharged/completed, six admitted loads still in C4**. One delayed confirmation succeeded and normal flow continued. No false confirmation observed. The final transfer `9c087a5efe284d35842d9138fc6aee41` remains unresolved at boundary42/pocket2: receiving pocket visibly empty, C3 exit material blocks recovery sweep. Motion was correctly refused with `forward_sweep_clear=false`; no recovery motor leg issued. This is an unarrived/physically blocked transfer, not a demonstrated repeat of the C4 merged-box false negative.

Current: READY hardware, PAUSED controller, feeders disabled, motors stopped. C3 retains material; C4 pockets6,7,8,9,0,1 retain six admitted loads and pocket2 has unresolved reservation. Scene/ownership preserved. **Focused arrival qualification incomplete; not yet qualified for continued testing until physical obstruction and transfer ownership are resolved.** Fix remains deployed. No broad throughput or later-stage qualification run.

[Current focused stress evidence](../../../analysis_artifacts/c4-arrival-stress-20260912/status.md). Earlier implementation's 126 relevant passing tests remain accepted. The following sections are historical snapshots and do not describe current deployment or ownership.

## C4 arrival repair — validated locally, occupied hardware after diagnostic crash 2026-09-12

**Current state supersedes the prior paused snapshot below.** During diagnosis, the primary agent's live debugger attachment caused backend PID 5588 to crash. Supervisor restarted PID 11398; verified healthy with hardware standby and feeders disabled. The live pocket ledger was lost. C3 material and C4's six admitted loads plus red intake remain physically retained. No reset, home, clear or resume was commanded. Do not initialize or resume with these occupied paths and missing runtime ownership.

Before the crash, paired images/state identified the red brick as episode `a3e2779e6b0f4d02b8a3020a1ca32098`, reserved pocket 3. It was not reconciled. C4 merged its box with a neighboring load across the diagonal divider, moving the merged COM outside DROP; the arrival consumer consequently saw no qualifying intake. A second issue made missing C3 support terminal at the initial recovery check instead of allowing bounded stationary confirmation retries.

Local repair preserves intake attribution during C4 bbox merging, allows bounded no-motion reobservation with existing freshness/two-frame/deadline guards, and prevents terminal episodes silently admitting later. **126 focused tests passed**, including geometry and delayed-arrival/false-positive regressions; independent review found no findings. Four extra reverse-direction tests fail identically against baseline due to existing clockwise configuration versus reverse-test assumptions. No unrelated configuration changes, full-suite rerun, staging or commit.

**Repair NOT deployed; C3-C4 NOT fully qualified.** Remaining stages 2/3 and sustained qualification are pending. Runtime has no supported snapshot rehydration interface. Brian must remove and retain material from C2/C3/C4 and chute and confirm clear before corrected deployment/initialization can proceed. This new physical blocker arose from the agent's diagnostic restart, not ambiguous red-brick identity. Evidence was captured first and remains preserved.

[Repair evidence, tests and diagnostic error](../../../analysis_artifacts/c4-arrival-repair-20260912/status.md). Prior state is preserved in that directory. Follow the existing corrected deployment procedure after physical clearance; do not repeat debugger attachment.

## Prior qualification snapshot — historical, before diagnostic restart

Candidate archive `bff3a4ae5079db9f2860f9e019fd480735e001860b6842966ebe7bd369ad1f7c` remains installed; backend PID 5588 healthy. No redeployment, test-suite rerun or tuning in this continuation. Earlier 11/11 and +3-degree qualification remain accepted as their separate cohort.

The authorized remaining recovery observation ran **527.349 seconds**, completing **41 new loads / 4.665 loads per minute**, including **28 known useful singletons / 3.186 per minute** and nine additional useful-route groups of unknown size. Two natural +3-degree recoveries confirmed arrival; later stages 2/3 were not exercised. The separate sustained qualification phase was not reached. No valid physical discard percentage: twelve completed groups have unknown size and seven episodes remain pending.

At 21:51:41 UTC, episode `a3e2779e6b0f4d02b8a3020a1ca32098` lost C3 association while C4 arrival telemetry reported empty. Paired images show the released red brick in C4's receiving region. Recovery refused motion and the qualification observer issued supported pause. Final verified state: hardware READY, controller PAUSED, feeders disabled, all four channel motors and chute stopped. C3 retains material; C4 has six admitted loads (pockets 7,8,9,0,1,2) plus unresolved reserved intake pocket 3. Ownership/history preserved. No automatic reset, clear, home or resume after this blocker.

**Not fully qualified for normal sorting.** Current target: reconcile and diagnose the C4 arrival-confirmation discrepancy for the visibly landed red brick. This is a current unresolved ownership blocker, not a blanket stop for recovered rejects. Hive remains external/read-only.

[Current qualification report and evidence](../../../analysis_artifacts/c3-c4-remaining-qualification-20260912/status.md). [Prior accepted deployment and bounded qualification](../../../analysis_artifacts/c3-c4-cleared-deployment-20260912/status.md). Previous state text is retained in the current evidence directory.

Follow [CODEX_WORKFLOW.md](../CODEX_WORKFLOW.md) and AGENTS.local.md. No new production code, tests, staging or commit in this qualification continuation.
