# RB03 r1 — Native custody, durable release intent, and route truth

## Result and scope

RB03 adds an explicit native custody extension and a command-free adapter around the existing v0.3.0 physical owners. The final focused run passed **252 tests** with four existing FastAPI deprecation warnings. The implementation is uncommitted. This package is for source review; it provides no deployment, live-device, firmware, or physical qualification.

The allowed Waveshare root fix is included. Both servo backends now distinguish a requested target from controller observations. Unknown feedback cannot qualify a Smart Bin release.

Guarded operation is deliberately conservative: unknown/resident staging, clumps and stall sweeps are held for reconciliation. After even a normal inferred exit, the reservation remains at RELEASE_INTENT with capacity retained. RB03 therefore does not establish continuous Smart Bin delivery operation. Receiving completion and the authority to resolve these claims remain RB04; RB04 has not begun. P2C6B remains blocked.

## Exact baseline and preservation

- Implementation branch: `smart-bins-v030-integration`; HEAD `8951f914eb34778139b0df42f59f3957abbc2f8a`.
- Pristine v0.3.0 tree: `21e805b60da575ff7468fcc3570a517d80f01cd0`.
- RB01 review `8c83a0ceb261f2e071b0b4dccff6765b0fb12e1f`, result `ab7e7461a69efc1db74faac2c8e8f3318ca10341`.
- RB02 review `688778a6f11222207e64a419a2dd64322b74b061`, result and patch base `df609a4d5c953f253737a927432ab3598647765e`.
- P2C6P-B architecture reference: `d451afdb5a93d3e3b7bc8d84c57a15f005b23f7b`.
- Old semantic reference: `615cb66d5d78e4f5737564f3e81b82a1c47ee9cf`.
- Before editing, RB01 + RB02 reconstructed exactly and all 1,591 accepted working files matched RB02. Final packaging repeats reconstruction and checks the saved before inventory.
- Real index remains empty and byte-identical: `18c7474d1a3a7fc2b9b8b894f6340ca5e8e607d78b0de3a154156c3c08b1027f`. HEAD and branch unchanged. Old candidate inventory/HEAD/index/status unchanged.
- RB01 `hardware/bus.py`, RB02 storage/service/delivery, `machine_platform/servo_controller.py`, Sending, runtime statistics, recorder and event contracts remain byte-identical. No native completion module was added.

## Native physical ownership and adapter proof

Native pulse-perception, two-piece classification and Distribution still issue physical commands. No old physical controller, FIFO, pocket/marker planner, alternate runtime or Harvest provider was introduced. Coordinator preserves Distribution → Classification → Feeder order under one reentrant ownership lock.

AST inspection of `smart_bins_native_adapter.py` confirms no calls to stepper moves, servo open/close, chute moves, or transport advance. Its chute reachability call is a kinematic query. It reserves through RB02, commits custody, grants/refuses permission, and records observations. Driver guards and manual endpoints use the same ownership fence. SQLite transactions close before every physical side effect; a test opens a separate immediate writer inside the dispatch scope.

## Native custody and release sequence

Version 1 is explicitly installed through `initialize_native_schema(machine_id, policy_id, namespace, routing_revision)` after the RB02 service schema. Startup only inspects; absent files/schemas are not created. A present but partial/unsupported native extension fails closed. Existing held reservations, discrepancies and old completion followups are inspected before startup motion even when native settings are absent.

Identity contains machine, piece UUID, reservation, runtime incarnation, native head generation, route revision and unique release attempt. Claims have forward-only status transitions; identity/attempt are immutable; evidence is append-only. No marker, pocket, confirmed index or actual receiving destination is fabricated.

1. Positioning holds ownership, checks native C4/C3 stopped, and asks RB02 to select/reserve the route against policy hash, current session, category, capacity, layout and configuration digest.
2. Reservation and native binding commit before the native route scope commands doors/chute. Intended destination remains separate from any future actual destination.
3. The native route waits for chute controller state and every relevant flap. Ordinary native positioned UUID and destination tuple remain intact.
4. READY verifies exact head/UUID/generation and the full flap path. It commits ROUTE_VERIFIED and RELEASE_INTENT before creating an in-memory permit and opening `distribution_ready`.
5. `startOutputMove` revalidates and consumes the exact permit durably before the single native C4 finite command. A failed commit leaves zero release movement. The permit is never reconstructed on restart.
6. Command outcome is persisted separately. Neither ACK, software promotion, elapsed time nor Sending creates Smart Bin completion.

The shared gate refuses attempts to reopen without a live permit. Native READY still waits for the matching drop-slot UUID. Withheld/changed track identity retains custody; track loss alone never authorizes safe cancellation.

## Complete motion-entry coverage

| Entry | Source | Enforcement |
|---|---|---|
| Startup/discovery | main.main, _initialize_hardware, _home_hardware | Attach before API startup; inspect held claims before mkIRLInterface can restore/calibrate/open hardware. No automatic schema initialization. |
| Coordinator | Coordinator.__init__, step | Shared ownership lock spans native Distribution -> Classification -> Feeder order. Guarded distribution gate starts closed. |
| Route preparation | Positioning.step/_nativeCustodyStep | C4 and C3 stopped check; authoritative RB02 reservation commits before native flap/chute commands. Route scope only while RESERVED. |
| READY | Ready.step; SharedVariables.set_distribution_gate | Full flap observations, exact positioned UUID/head generation, durable intent commit, then one live permit and gate. |
| Normal eject | two_piece.base.startOutputMove | Consume exact permit before native finite C4 move; record accepted/rejected/ambiguous receipt separately; never retry. |
| Initial/post-eject staging | two_piece.base.startOutputMove | Same entry fence. Unknown/resident staging creates durable hold and refuses; consumed/unresolved prior release cannot authorize staging. |
| Multi-drop | two_piece.flow and Positioning | Existing bucket hold cycles retained; unknown quantity produces snapshot/hold before route or release. No one-piece invention. |
| Stall recovery | two_piece.flow.attemptStallAutoClear | Persist affected-claim snapshot/uncertainty before native pre-promotion, door opening or sweep; guarded recovery refuses motion. |
| Channel clear / spoke home | channel_clear.clearChannelByAdvancing; spoke_home.maybeRunSpokeHome | Held claims or unknown/occupied perception refuse sweep. Fresh already-clear observation records no exact identity/destination; unsuccessful purge blocks alignment. |
| C3 admission | pulse_perception.flow._move | Before pulse speed or move; C1/C2 have no Smart Bin DB query. |
| Manual/test/calibration | routers.steppers/hardware; StepperMotor/Waveshare/PCA command entries | HTTP 409 before endpoint motion when held; common driver lock closes dispatch races, including background sweeps and calibration. Stops/disables remain available. |
| Route configuration | steppers._guard_route_configuration and hardware configuration endpoints | Ownership lock spans parameter/layout/calibration edits; held custody returns HTTP 409 before side effects. |
| Waveshare service/direct bus | WaveshareBusService.move_to/set_torque/calibrate_servo; ScServoBus.move_to/set_torque | Ownership lock precedes existing serial lock; no second serial lock, polling worker, resend or recovery-threshold change. |

Stop and torque/PWM release remain available. Configuration routes now hold ownership through their edits, so calibration/layout changes cannot race a reserved or released claim. Finite receipts acquire ownership before their local receipt lock. Waveshare service motion acquires ownership before the existing serial lock, avoiding reversed lock order.

## Typed evidence and retained capacity

| Evidence | Meaning and limit |
|---|---|
| ROUTE_VERIFIED / ROUTE_UNCERTAIN | Intended path observed or unavailable; never landing evidence. |
| RELEASE_INTENT / DISPATCH_CONSUMED | Committed authorization and single-use consumption; never a replayable command. |
| MOTOR_ACCEPTED | Positive finite-command ACK; no encoder or piece-displacement proof. |
| MOTOR_REJECTED | Valid PCA/MCU explicit rejection; distinct from transport ambiguity. |
| MOTOR_AMBIGUOUS | Unconfirmed transaction; reservation becomes UNCERTAIN and retains capacity. |
| INFERRED_EXIT | Fresh owned track absence after accepted dispatch, bound to the current attempt/generation; no marker or receiving proof. |
| EJECT_TIMEOUT_VISIBLE | Fifteen-second eject timeout while visible; UNCERTAIN, no second release or staging. |
| TRACK_LOST_REIDENTIFIED | Lost/reassociated head; retained/uncertain claim, no automatic cancellation. |
| SOFTWARE_SLOT_PROMOTION | Association movement only; never physical proof. |
| STALL_PRE_CLEAR_PROMOTION | Recovery-request context persisted before any pre-promotion; guarded recovery is refused. |
| CHANNEL_CLEAR_OBSERVED | Fresh channel-empty observation; exact identity, quantity and actual destination remain null. |
| UNKNOWN_MATERIAL | Durable hold with affected reservation snapshot and unknown quantity. |

Fresh absence must follow the accepted dispatch timestamp and be no older than 0.5 seconds. Stale/unknown perception cannot authorize a startup purge. Native timeout/association maintenance remains available, but it cannot discharge custody. Unknown material creates a hold rather than an invented one-piece reservation. A failed hold commit still blocks motion in memory and does not perform a sweep.

## Waveshare flap evidence root fix

The preserved before source reproduces five defects: (1) failed open plus missing feedback looks successful; (2) failed close plus missing feedback looks successful; (3) accepted movement with missing feedback looks stopped after elapsed time; (4) unconfirmed finite movement updates the shadow target; (5) a missing moving-register read and an observed stopped register both return False. The original four-case script/log, original source and fifth-case result remain in local `rb03_evidence`; `reproduce_saved_before.py` reruns all five against saved pre-fix source.

`ScServoBus.is_moving` now returns True for 1, False for 0, and None for missing/invalid data. `WaveshareBusService.is_moving` is a public pass-through through its existing `_execute` serialization. No private service bus access, extra serial lock, worker, retry loop or recovery-threshold change was introduced.

Waveshare keeps pending target separate from confirmed position. Positive transport acknowledgement is ACCEPTED. False and exceptions are AMBIGUOUS because this protocol conflates missing/malformed/error replies; False does not prove no motion. Exceptions retain their original semantics. Open/close return the command bool. Neither failure nor acceptance immediately confirms the target.

Position performs a live read; unavailable feedback returns None or preserves the existing raised I/O error. `stopped` uses the moving register and a fresh position read. Accepted + stopped + observed target within nine raw counts clears the pending target, matching the native calibration's existing less-than-ten-count criterion. Wrong position stays pending. Unknown is neither open nor closed. `feedback` preserves useful fields and adds explicit validity, command outcome, pending target and observation times; API feedback callers accept null/error output.

Torque release occurs on positively observed stop. Ambiguous release commands attempt protective torque-off immediately. Existing polling also cuts torque after 3.5 seconds if observations remain unavailable/moving; this protection never reports stopped or target success. On a custody fault native Positioning releases other route servos, with unacknowledged release retained for the next normal tick. Communication failure cannot establish that physical torque is off. There is no background polling thread.

Stored EEPROM limits/calibration and inversion remain native. Initialization/recalibration obtain fresh position without seeding a guessed target. Inversion changes endpoint meaning without inventing feedback. `is_calibrated` requires valid completed calibration; factory/unknown limits fail route qualification.

## PCA, finite receipts, and full flap path

| Surface | Accepted | Rejected | Ambiguous |
|---|---|---|---|
| PCA servo | Pending until stopped and fresh controller position | Explicit False; never confirmed target | Invalid/failed I/O invalidates certainty |
| Waveshare servo | Pending until moving=False and fresh raw position | Protocol cannot prove explicit rejection | False or exception; never target fallback |
| Finite MCU stepper | Monotonic generation and requested steps, ACCEPTED | Valid zero payload gives REJECTED | Bus/invalid response gives AMBIGUOUS; no resend |

Stepper bool callers remain compatible; the additive receipt API states `encoder_present=False` and `proves_piece_displacement=False`. Zero/suppressed commands are NOT_DISPATCHED. The native-through-RB01 fake-serial test proves one write after a lost reply and refusal of a second dispatch.

The narrow `flap_path.qualify` checks every configured route layer: available, calibrated, acknowledged command, valid stopped observation, valid position, no pending target, and expected closed target/open pass-through state. Both PCA and Waveshare pass the same contract. Missing/disabled/unqualified flaps keep the gate closed. PCA observations are firmware/PWM state without a physical encoder; they do not establish physical landing or mechanical calibration accuracy. Source tests are not physical qualification.

## Ordinary mode and performance

Schema-absent routing follows the original native branches. The driver truth corrections and required failed-purge alignment fix also apply in ordinary mode. Existing handoff, multi-drop, spoke-home, servo and manual endpoint tests pass. Smart Bin strict route policy is only active for explicit native settings; legacy unresolved claims still fail closed at startup. No Harvest application/store/provider dependency enters this path.

The adapter's hot checks query indexed current reservations, unresolved holds and discrepancies with LIMIT 1. Startup performs the schema/legacy-followup inspection once; RB03 does not create completion followups. Route qualification reads current slots/configuration once per reservation and rechecks the configuration digest at arming/dispatch. It does not scan delivery, journal or piece history each coordinator tick. C1/C2 perform no Smart Bin DB work. Servo observations occur at existing Positioning/READY boundaries; no additional periodic servo scanner or busy loop was added. Repeated held-state ticks do not append duplicate holds.

## Validation

Final combined result: **252 passed, 4 warnings in 14.06s**. Warnings are existing FastAPI `on_event` deprecations. The final run includes all four new RB03 files, existing Waveshare/controller/stop/fatal tests, native C4 handoff/multi-drop/spoke-home/manual safety, the required RB02 storage/reservation/delivery set, and RB01 bus ambiguity tests.

Earlier focused checkpoints passed: driver group 64; native baseline 34; native runtime group 48; bounded RB02 group 88; final configuration/native-boundary group 25. These overlap and are not added together. The manifest records exact commands and output, including the later combined result that supersedes those intermediate source snapshots.

Tests use frozen offline Python, fake hardware/perception and disposable SQLite/configuration. The runner denies network connections, real serial creation and SQLite paths outside its disposable root. No backend-wide suite, live startup, device, firmware, deployment, Harvest provider or physical test was run.

Changed-file Ruff comparison: **zero new findings**, 39 unchanged accepted-baseline findings matched by code/message/source line; no suppressions. `git diff --check` passes. The artifact-level staged check identifies 126 whitespace-only context lines inside the normal unified patch. Each diagnostic was verified to be a required context line; the source-tree diff and the two surrounding documents pass their own whitespace checks. No rule was suppressed and no patch context was removed. AST adapter command-free check passes. Protected and old-candidate bytes pass. The normal-context patch applies in an isolated index to the exact result tree.

## Patch and changed-file hashes

- Base: `df609a4d5c953f253737a927432ab3598647765e`
- Result: `b4cf20bd92855e2ccdb22748fcf952f19b169738`
- Patch SHA-256: `c5140ffb048f373fddb9439b75db5375fc6bd23cebf59ca9ab751305cd837881`
- Format: three-line context, full blob IDs, binary-capable; `149143` bytes.
- Only the 23 paths below changed relative to accepted RB02 (19 production, four tests). No implementation commit or implementation push was made. Publication contains only REVIEW.md, RB03.patch and manifest.json on chat-review.

| Path | Before SHA-256 | After SHA-256 |
|---|---|---|
| `software/sorter/backend/coordinator.py` | `002a3210760b6e05ef6d3ed7d15bd201adfd4ab4841e299ebbd3280a9ffd50d0` | `2f6141a2046a0d9200b0d42abff034da00180ff6af1be674b13d78958cfc4533` |
| `software/sorter/backend/hardware/sorter_interface.py` | `085d9a2befe2fab0f0289693d6b7fc3f2c3e6bcb17d4ec7ee49e23f6f52000f5` | `fefee138b0b5bc205d6de3490e0723f2146282da35087d2fc71f6b476f272377` |
| `software/sorter/backend/hardware/waveshare_bus_service.py` | `7044f14b42a05157587faffb4f0b9766ed1d70dd681a458326fc560df1b2cec3` | `cfd367233ff41ed4cbe662e81b8ed378ccd2b87194b537c58e27637adaa4c9a2` |
| `software/sorter/backend/hardware/waveshare_servo.py` | `76ecea0be881b7b61d2efd355415488f7451152c21ba6bd92d301faa6aca83e2` | `2a6ff8e797bdcc44b151eb6a3b7581d2074986fc442e4920279e049383d7b8eb` |
| `software/sorter/backend/main.py` | `dcf35e8389f8f7b030011e6d995939f4c067057d00c1a9452d358787a747cc05` | `5372c8879a4137e721be1c8ff83ed518a93466f53751d95c9277b721b6c36c4a` |
| `software/sorter/backend/piece_transport.py` | `39810a409fdc0ee205cc202fd8873a81124d1ecc7e275c155cf883b445cbd3e9` | `26ec32abc5b2e707028e8c10fd91938d830158f054fedf081b7b970fba6c0961` |
| `software/sorter/backend/server/routers/hardware.py` | `34434f9364271e946c9b738241fe91bb08688d17149887682afbba6db7edf116` | `3136c9b57ddca25b94a2ee90ea59f2d2c5f92520a3751f0be1d9f044e18b708c` |
| `software/sorter/backend/server/routers/steppers.py` | `9253f1bf3cb1b5c7a2c1fdff77edfbf9ebdc74d6e57d0f9ec9f39da7cf28d6c0` | `5e8e8a5040deb9d04c5c1f013321a3c18514deb0860eb23413b64fd5e1dde881` |
| `software/sorter/backend/smart_bins_native_custody.py` | `new` | `76a79f51481e41a31c0d0e42c1557c5b1be845e9d4b5cd47910ad1bce2306356` |
| `software/sorter/backend/subsystems/classification_channel/smart_bins_native_adapter.py` | `new` | `b12365c20a1b8e5517f110a9ad3616a37ee58f26a3d54f8b5fcf7da9419260a6` |
| `software/sorter/backend/subsystems/classification_channel/two_piece/base.py` | `ef26af57eb460ff56a3ac54594b80da8c2cbc5651abe8025ff48506e840b223e` | `8ebbb15b4b74867df9e0a1fa3a83bc7209f0f4dcb3582577cdc3662e88b39e3e` |
| `software/sorter/backend/subsystems/classification_channel/two_piece/channel_clear.py` | `0fa530ce61250f48aadbd3ec4441171dfadc971e8e97d1e38fc0f9b6580dd458` | `023301f13dbab2c9ba0626012a07de4298473e20a854b354c616fed2a0b28445` |
| `software/sorter/backend/subsystems/classification_channel/two_piece/flow.py` | `4e1fa6d5c0455e5bb1d9d8b28818104945c4517e927e90f77b1def99759c39ff` | `258b8a17afa97cc31a95d13424a17b669454f4e073c0752629e8c0fbac1946c5` |
| `software/sorter/backend/subsystems/classification_channel/two_piece/spoke_home.py` | `577732b799bc4aec3a84b7fa429d9c186c54461ecfedcc2e07d533adeef00ce1` | `cad1426905a24f3d57af4e8db72c693ca68eb7911216efae1cef34ea60820f44` |
| `software/sorter/backend/subsystems/distribution/flap_path.py` | `new` | `aa46cbdfbcc33836f1fab6194a714caaaf6acb4e831bc4997f5846713b80588a` |
| `software/sorter/backend/subsystems/distribution/positioning.py` | `0f4a354a5025a00c6396fc069044012c2a0962ac8ac8513172bc26a1e934f632` | `0c92bddb0ddfcae827d18cb31762b61d0d8a84c708e75d86e25f14f9e0ef22bf` |
| `software/sorter/backend/subsystems/distribution/ready.py` | `6870c19d5edf90ff5e446d5f79a43c1d0c880f5b1bad87bbd29a1a1d82703508` | `7bbe144eaf2040c61ba8dcd288efb94b2e4ed4d471116c660653a7bca323ee32` |
| `software/sorter/backend/subsystems/feeder/pulse_perception/flow.py` | `c4ff3417f612cc66b209462e2c71aa42096b1147521784b50f847066cdcb41ac` | `5e1a07280b8c1fb91d481814fc09c40eabf5e446598bac2ae293960294ef3c8a` |
| `software/sorter/backend/subsystems/shared_variables.py` | `59718592f9f1280a47503ec9554752e75dd9ffba18ed4c4c16f3be31ad6b3367` | `0a63d9cc20395aa1a70b37bf211042882b09014521635193bdd07b30979dfe0a` |
| `software/sorter/backend/tests/test_native_motion_truth.py` | `new` | `8a2b6e19ac6823d1f46e2c0a8984407a9c75aa6c45612f609989a7f4126c7d37` |
| `software/sorter/backend/tests/test_smart_bins_native_custody.py` | `new` | `95fba31767c56808d377539968f416311407a4899184c3c739bd6d88bfc7a950` |
| `software/sorter/backend/tests/test_smart_bins_native_runtime.py` | `new` | `59a5d3b56d240c7bd61df5fab1cdbed4fe2ea6300b3d766ec5b2bc154f88f63e` |
| `software/sorter/backend/tests/test_waveshare_servo_truth.py` | `new` | `35dfa944a701f3d59452c5a7cbccf666441947e80c97b2283102af1f80ca7362` |

**Stop boundary:** RB03 review publication only. Receiving completion remains RB04. P2C6B remains blocked.
