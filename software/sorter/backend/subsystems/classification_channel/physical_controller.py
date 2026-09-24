"""Production wiring for the single physical C4 runtime (Slice 3)."""

from fractions import Fraction
from dataclasses import fields
from copy import deepcopy
import queue
import json
from pathlib import Path
import time

from defs.known_object import KnownObject, UNVERIFIED_C4_HANDOFF
from recognition_journey import Stage
from recognition_journey_capture import (
    brickognize_provider,
    retain_ready_history,
    retain_rolling_fall,
    retain_track_history,
)
from perception.journey_scenes import zone_region
from .five_sector_platter import C4FiveSectorPlatter
from .marker_positioner import (
    MarkerMapping,
    MarkerPositioner,
    PositionError,
    StableMarkers,
    TrackedStepperIndexMotor,
    error_deg,
)
from .marker_source import BridgeMarkerSource
from .physical_fifo import PhysicalC4FIFO
from .physical_runtime import PhysicalC4Runtime
from .physical_distribution import PhysicalDistribution
from .physical_handoff import PhysicalHandoff
from .simple_state_machine_rev01.context import SimpleStateMachineRev01Context
from .simple_state_machine_rev01.base import Rev01BaseState
from .transfer_episode import TransferEpisode


def create_positioner(irl, config, on_fault):
    root = Path(__file__).resolve().parents[2] / "c4_marker_positioning"
    settings = json.loads((root / "reference.json").read_text())
    source = BridgeMarkerSource(
        settings["base_url"],
        center=tuple(settings["center"]),
        shape=tuple(settings["shape"]),
        camera_id=settings["camera_id"],
    )
    motor = TrackedStepperIndexMotor(
        irl.carousel_stepper, C4FiveSectorPlatter.from_irl_config(config)
    )
    mapping = MarkerMapping.load(
        root / "mapping.json", source.geometry, motor.coordinate_identity()
    )
    return MarkerPositioner(motor, source, mapping, on_fault)


class _BootstrapMarkers(StableMarkers):
    """Stationary startup evidence uses source chronology, not consumer cadence.

    The newest fit must be fresh to complete. Earlier valid fits may support
    stability even when processing made them stale at validation. Continuity
    permits skipped source frames at the bridge-reported capture period, plus
    the existing freshness budget; missing cadence uses the conservative gap.
    The controller's existing startup deadline bounds the entire history.
    """

    def __init__(self, fence, geometry, limits, *, capture_period_s=None):
        super().__init__(fence, geometry, limits)
        self.capture_period_s = capture_period_s

    def add(self, sample, now):
        import math
        import statistics
        if sample is None:
            self.samples.clear()
            return None
        if sample.epoch != self.fence.epoch or sample.geometry != self.geometry:
            self.samples.clear()
            raise PositionError("camera epoch or geometry changed during bootstrap")
        # This is a source-frame fence, not an image region or marker filter.
        if sample.sequence <= self.fence.sequence or sample.captured_ns <= self.fence.captured_ns:
            if self.samples:
                self.samples.clear()
            return None
        if sample.sequence <= self.last_sequence or sample.captured_ns <= self.last_capture:
            self.samples.clear()
            self.last_sequence, self.last_capture = sample.sequence, sample.captured_ns
            return None
        if self.samples:
            previous = self.samples[-1]
            elapsed = (sample.captured_ns - previous.captured_ns) / 1e9
            skipped = sample.sequence - previous.sequence - 1
            expected = skipped * (self.capture_period_s or 0.0)
            if elapsed - expected > self.limits.max_sample_age_s:
                self.samples.clear()
        self.last_sequence, self.last_capture = sample.sequence, sample.captured_ns
        age = now - sample.received_mono
        if not math.isfinite(age) or age < 0:
            return None
        if not math.isfinite(sample.phase_deg) or not 0 <= sample.phase_deg < 360:
            self.samples.clear()
            return None
        if sample.captured_ns <= self.fence.captured_ns + self.limits.settle_s * 1e9:
            return None
        self.samples.append(sample)
        offsets = [error_deg(s.phase_deg, self.samples[0].phase_deg) for s in self.samples]
        if max(offsets) - min(offsets) > self.limits.stable_spread_deg:
            self.samples = [sample]
            return None
        if len(self.samples) > 32:
            self.samples = self.samples[:1] + self.samples[-31:]
        if (age > self.limits.max_sample_age_s or len(self.samples) < 3
                or (sample.captured_ns - self.samples[0].captured_ns) / 1e9 < self.limits.stable_span_s):
            return None
        offsets = [error_deg(s.phase_deg, self.samples[0].phase_deg) for s in self.samples]
        return type(sample)(sample.epoch, sample.sequence, sample.captured_ns,
                            sample.received_mono,
                            (self.samples[0].phase_deg + statistics.median(offsets)) % 360,
                            sample.geometry)


class PhysicalC4Controller:
    physical_c4_authority = True

    def __init__(
        self,
        irl,
        irl_config,
        gc,
        shared,
        transport,
        vision,
        event_queue,
        *,
        positioner=None,
    ):
        if getattr(shared, "c4_runtime_owner", None) is not None:
            raise RuntimeError("C4 already has a runtime motion owner")
        self.irl, self.irl_config, self.gc = irl, irl_config, gc
        self.shared, self.vision, self.events = shared, vision, event_queue
        self.perception = gc.perception_service
        self.transport = transport
        self.config = SimpleStateMachineRev01Context().config
        self._deps = (irl, irl_config, gc, shared, transport, vision, event_queue)
        self.positioner = positioner
        self.runtime = None
        self.handoff = None
        self._paused = False
        self.fault = None
        self._paused_at = None
        self._reestablishing = False
        self._bootstrap = None
        self._bootstrap_started = None
        self._bootstrap_token = None
        self._reject_opened = False
        self._seen = {}
        self._regions = {}
        self._last_admission = 0.0
        self.last_progress_at = time.monotonic()
        self.shared.c4_runtime_owner = self
        self.shared.reserve_c4_transfer = self.reserve_transfer
        self.shared.c4_reject_path_ready = self.reject_path_ready
        self.gc.use_channel_bus = True
        self.shared.set_classification_gate(
            False, reason="establishing C4 markers and recovery"
        )
        gc.runtime_stats.setIndexedMode()

    def phaseName(self):
        if self.runtime is None:
            return "establishing_marker_boundary"
        return (
            "paused"
            if self._paused
            else "reject_recovery"
            if self.runtime.recovering
            else "handoff"
            if self.handoff
            else "physical_fifo"
        )

    def noteProgress(self):
        self.last_progress_at = time.monotonic()

    def _fault(self, reason):
        self.shared.set_classification_gate(False, reason=reason)
        # Existing hardware fault owner; a fault never clears FIFO custody.
        from server.routers.steppers import _halt_stepper

        _halt_stepper(self.irl.carousel_stepper, force=True)

    def reject_path_ready(self):
        from subsystems.distribution.flap_path import validate_flaps, flap_path_settled

        servos = list(self.irl.servos)
        if not self._reject_opened:
            validate_flaps(servos, None)
            # Finish the existing finite flap command before requesting its
            # opposite. A busy door is not a rejected recovery operation.
            if not all(servo.stopped for servo in servos):
                return False
            for servo in servos:
                if servo.open() is False:
                    raise RuntimeError("Reject flap command rejected")
            self._reject_opened = True
        return flap_path_settled(servos, None)

    def _establish(self, now):
        if not self.reject_path_ready():
            return
        if self.positioner is None:
            self.positioner = create_positioner(self.irl, self.irl_config, self._fault)
        p = self.positioner
        if self._bootstrap is None:
            self._bootstrap_token = p.motor.stationary_token()
            fence = p.source.fence()
            self._bootstrap = _BootstrapMarkers(
                fence, p.mapping.geometry, p.limits,
                capture_period_s=getattr(p.source, "capture_period_s", None),
            )
            self._bootstrap_started = now
        if p.pending is None and p.boundary is None:
            p.motor.check_token(self._bootstrap_token)
            if now - self._bootstrap_started > p.limits.timeout_s:
                raise PositionError("Cannot establish C4 marker phase")
            observation = p.source.sample()
            sample = self._bootstrap.add(observation, time.monotonic())
            p.motor.check_token(self._bootstrap_token)
            if sample is None:
                return
            boundary = (
                round(((sample.phase_deg - p.mapping.zero_phase_deg) % 360) / 36) % 10
            )
            if (
                abs(error_deg(p.mapping.phase(boundary), sample.phase_deg))
                > p.limits.max_correction_deg
            ):
                raise PositionError(
                    "C4 phase is outside supported marker recovery trim"
                )
            # Unknown startup custody, all-open reject path, explicitly observed
            # nearest physical target. Frozen maintenance trim limits apply.
            if self.runtime is not None:
                old_boundary = self.runtime.fifo.boundary
                boundary = old_boundary + (boundary - old_boundary) % 10
            p.begin_target_trim(
                boundary, int(self.config.precise_converge_speed_usteps_per_s)
            )
            return
        confirmation = p.poll() if p.pending is not None else None
        if p.pending is not None:
            return
        if self._reestablishing:
            self.runtime.reestablish_for_recovery(confirmation, p)
            self._reestablishing = False
            return
        platter = C4FiveSectorPlatter.from_irl_config(self.irl_config)
        steps = (
            Fraction(str(platter.gear_ratio))
            * platter.microsteps
            * platter.motor_steps_per_revolution
        )
        fifo = PhysicalC4FIFO(
            microsteps_per_revolution=steps, initial_boundary=p.boundary
        )
        self.runtime = PhysicalC4Runtime(
            fifo,
            p,
            speed=int(self.config.precise_converge_speed_usteps_per_s),
            distribution=PhysicalDistribution(self.shared, self.events, self.gc),
            apply_result=self._apply_result,
        )
        self.runtime.recover(unknown=True)

    def _scenes(self, channel):
        read = getattr(self.perception, "read_journey_scenes", None)
        scenes = read(channel) if callable(read) else ()
        if scenes and self.runtime:
            self.runtime.journeys.set_epoch(channel, scenes[-1].epoch)
        return scenes

    def _region(self, channel):
        definition = getattr(self.perception, "_channels", {}).get(channel)
        if definition is None:
            return None
        key = channel, id(definition)
        if key not in self._regions:
            self._regions = {k: v for k, v in self._regions.items() if k[0] != channel}
            sections = (
                definition.drop_sections if channel == 4 else definition.exit_sections
            )
            self._regions[key] = zone_region(definition, sections)
        return self._regions[key]

    def reserve_transfer(self, state, evidence):
        """Called by the C3 motor owner immediately BEFORE the release command."""
        r = self.runtime
        if r is None or not r.can_admit or self.handoff is not None:
            return False
        read = getattr(self.perception, "read_pieces_and_frame", None)
        paired = read(4) if callable(read) else None
        if paired and 0 <= time.time() - paired[1].timestamp <= 1.5:
            # Physical admission uses calibrated DROP attribution. The larger
            # rectangle for optional crop isolation also covers adjacent pockets.
            if any(piece.zone_code == 1 for piece in paired[0]):
                # Material already in the intake is not the C3 piece. Reserve
                # it as unknown reject custody and leave the original on C3.
                ep = TransferEpisode(
                    r.fifo.boundary,
                    r.fifo.intake_pocket_id,
                    time.monotonic(),
                    time.time(),
                )
                unknown = r.reserve(KnownObject(reject_on_routing_failure=True), ep)
                r.finish_handoff(unknown, arrived=False)
                self.shared.set_classification_gate(
                    False, reason="rejecting unknown intake material"
                )
                return False
        leader = next(iter(state.pieces), None)
        leader_id = getattr(leader, "sv_bt_track_id", None)
        now, wall = time.monotonic(), time.time()
        ep = TransferEpisode(
            r.fifo.boundary, r.fifo.intake_pocket_id, now, wall, leader_id=leader_id
        )
        ep.release_evidence = evidence
        ep.group_size_unknown = bool(evidence.get("group_size_unknown"))
        ep.support_motion_deg = evidence.get("motion_deg", 0.0)
        piece = KnownObject(
            reject_on_routing_failure=True, transfer_episode_id=ep.episode_id
        )
        binding = r.reserve(piece, ep)
        self.shared.c3_transfer_episode = ep
        self.handoff = PhysicalHandoff(self, binding)
        self.shared.set_classification_gate(False, reason="reserved physical C4 intake")
        c3s, c4s = self._scenes(3), self._scenes(4)
        scene = next((s for s in reversed(c3s) if s.timestamp == state.ts), None)
        alias = (
            next(
                (d.alias for d in scene.detections if d.alias.track_id == leader_id),
                None,
            )
            if scene
            else None
        )
        j = r.journeys.start_reserved(
            piece,
            episode_id=ep.episode_id,
            generation=binding.generation,
            scene=scene,
            alias=alias,
        )
        binding.journey = j
        if j and scene and alias:
            retain_ready_history(
                r.journeys,
                j,
                self.perception,
                evidence,
                scene=scene,
                paired_scene=lambda record: next(
                    (s for s in c3s if s.timestamp == record.get("frame_ts")), None
                ),
            )
            if c4s and self._region(3) and self._region(4):
                r.journeys.begin_crossing(
                    j,
                    release=scene,
                    empty_c4=c4s[-1],
                    c3_region=self._region(3),
                    c4_region=self._region(4),
                    reservation=(ep.episode_id, *binding.key),
                )
        self.handoff._observeEpisode()
        return True

    def finish_handoff(self, binding, now, *, arrived):
        r = self.runtime
        if not arrived:
            binding.piece.transport_failure_reason = UNVERIFIED_C4_HANDOFF
        if binding.episode.group_size_unknown:
            r.discard(binding, "ambiguous intake group")
        r.finish_handoff(binding, arrived=arrived)
        binding.capture_until = now + 1.5
        self.handoff._observeEpisode()
        self.handoff = None
        self._last_admission = now
        self._capture(now)
        if arrived:
            self.gc.runtime_stats.observeChannelExit(
                "c_channel_3",
                piece_uuid=binding.piece.uuid,
                source="physical_fifo_confirmed_arrival",
            )
        self.noteProgress()

    def recovery_boundary(self, binding, frame_ts):
        r, p = self.runtime, self.positioner
        ep = binding.episode
        aligned = p.pending is None and p.boundary == r.fifo.boundary
        if aligned:
            # Verify motor custody, not the commanded position as rotor truth.
            p.motor.check_token(p._token)
        return {
            "boundary_index": r.fifo.boundary,
            "reserved_boundary": ep.boundary_index,
            "pocket": ep.pocket_id,
            "frame_ts": frame_ts,
            "frame_age_s": time.time() - frame_ts,
            "predicates": {
                "reserved_boundary": r.handoff is binding and r.current(binding),
                "c4_available": r.handoff is binding,
                "c4_stopped_aligned": aligned,
                "c4_frame_fresh": frame_ts > ep.started_at_wall
                and 0 <= time.time() - frame_ts <= 1.5,
                "c4_empty": self.handoff._arrival_empty_streak
                >= max(1, self.config.presence_streak_to_start),
            },
        }

    def _capture(self, now):
        try:
            self._capture_views(now)
        except Exception:
            # Optional image capture cannot hold physical transport. The same
            # generation remains PENDING until its normal route deadline.
            self.gc.logger.exception("C4 optional journey enrichment unavailable")

    def _capture_views(self, now):
        r = self.runtime
        c3s, c4s = self._scenes(3), self._scenes(4)
        read_buffer = getattr(self.perception, "read_journey_buffer", None)
        buffer = read_buffer(4) if callable(read_buffer) else None
        if buffer is not None:
            retain_rolling_fall(
                r.journeys,
                buffer,
                lambda frame: next(
                    (s for s in c4s if s.timestamp == frame.timestamp), None
                ),
            )
        burst = getattr(self.vision, "_drop_zone_burst_collector", None)
        if burst is not None:
            retain_rolling_fall(
                r.journeys,
                burst.rolling_buffer,
                lambda frame: next(
                    (s for s in c4s if s.timestamp == frame.timestamp), None
                ),
            )
        for scene in c4s:
            if scene.sequence > self._seen.get(scene.epoch, -1):
                r.journeys.burst_frame(scene)
                self._seen = {scene.epoch: scene.sequence}
        for b in tuple(r.bindings.values()):
            j = b.journey
            if j is None or j.submitted or j.closed:
                continue
            if b.admitted and c4s:
                region = self._region(4)
                if (
                    region
                    and r.fifo.intake_pocket_id == b.pocket_id
                    and r._pending is None
                ):
                    scene = c4s[-1]
                    if (
                        scene.timestamp > b.episode.started_at_wall
                        and 0 <= time.time() - scene.timestamp <= 1.5
                    ):
                        b.landing_scene = scene
                        for d in scene.detections:
                            if d.alias in j.aliases:
                                r.journeys.observe(j, scene, d.alias, Stage.LANDING)
                            else:
                                r.journeys.observe_reserved_c4(
                                    j,
                                    scene=scene,
                                    alias=d.alias,
                                    region=region,
                                    reservation=(b.episode.episode_id, *b.key),
                                )
                # Paired C3 inference can arrive after indexing begins. Its
                # proof refers to the retained stationary landing frame.
                landing = b.landing_scene
                if landing is not None and not j.crossing_confirmed:
                    after = next(
                        (
                            s
                            for s in c3s
                            if landing.timestamp
                            <= s.timestamp
                            <= landing.timestamp + 0.5
                        ),
                        None,
                    )
                    if after is not None:
                        r.journeys.land(j, scene=landing, c3_after=after)
                for scene in c4s:
                    for d in scene.detections:
                        if d.alias in j.aliases:
                            r.journeys.observe(j, scene, d.alias, Stage.SETTLED)
            history = getattr(self.perception, "read_journey_history", None)
            if callable(history):
                for alias in tuple(j.aliases):
                    captured = history(alias)
                    if captured is None:
                        continue
                    reference, detail = captured
                    retain_track_history(
                        r.journeys,
                        j,
                        detail,
                        reference,
                        lambda record: next(
                            (
                                s
                                for s in c4s
                                if s.timestamp
                                == record.get("captured_ts", record.get("timestamp"))
                            ),
                            None,
                        ),
                    )
            if b.admitted and now >= b.capture_until:
                if not r.journeys.submit(j, self._provider_for(b)):
                    r.discard(b, "no valid recognition image")

    def _provider_for(self, binding):
        # Worker owns a private record; optional metadata I/O never holds the
        # controller operation lock and cannot race physical custody mutation.
        private_piece = deepcopy(binding.piece)
        provider = brickognize_provider(self.gc)

        def work(request):
            response = provider(request)
            worker = Rev01BaseState(
                *self._deps[:-1], queue.Queue(), SimpleStateMachineRev01Context()
            )
            worker.ctx.known_object = private_piece
            worker.ctx.captured_crops = [o.bgr for o in request.images]
            from defs.known_object import RecognitionImage

            private_piece.recognition_image_set = [
                RecognitionImage(
                    image=worker.encodeFrame(o.bgr),
                    source=o.stage.value,
                    used=True,
                    ts=o.timestamp,
                    channel=o.alias.channel,
                    created_at=o.timestamp,
                )
                for o in request.images
            ]
            worker.updateKnownObjectWithResult(response, None)
            return private_piece

        return work

    def _apply_result(self, binding, result):
        record = result.response
        if not isinstance(record, KnownObject) or record.uuid != binding.piece.uuid:
            return
        # Apply the worker's classification data only after the runtime checks
        # UUID/episode/generation. Physical/route fields stay with this writer.
        protected = {
            "uuid",
            "created_at",
            "updated_at",
            "stage",
            "destination_bin",
            "forced_reject_reason",
            "reject_on_routing_failure",
            "aborted",
        }
        for field in fields(record):
            name = field.name
            if name in protected or name.startswith(
                ("c4_", "transfer_", "transport_", "physical_", "distribut", "harvest_")
            ):
                continue
            setattr(binding.piece, name, getattr(record, name))
        binding.piece.updated_at = time.time()
        from utils.event import knownObjectToEvent

        self.events.put(knownObjectToEvent(binding.piece))

    def step(self):
        try:
            self._step()
        except Exception as exc:
            self._latch_fault(exc)

    def _latch_fault(self, exc):
        self.fault = str(exc)
        self._paused = True
        self.shared.set_classification_gate(False, reason=self.fault)
        from server import shared_state

        shared_state.setHardwareStatus(state="error", error=self.fault)
        from defs.events import PauseCommandData, PauseCommandEvent

        shared_state.command_queue.put(
            PauseCommandEvent(tag="pause", data=PauseCommandData())
        )
        self.gc.logger.error("Physical C4 stopped with custody retained: %s", exc)

    def _step(self):
        now = time.monotonic()
        if self._paused:
            return
        if self.runtime is None or self._reestablishing:
            self._establish(now)
            return
        r = self.runtime
        if r._verify is not None:
            r.tick(now, allow_motion=False)
            if r._verify is not None:
                return
        self._capture(now)
        if self.handoff:
            self.handoff.tick(now)
        feed = now - self._last_admission < 8
        unsafe_c3 = (
            self.shared.c3_motion_pending and not self.shared.c3_safe_staging_pending
        )
        r.tick(now, feed=feed, allow_motion=not unsafe_c3 or r.recovering)
        self.shared.set_classification_gate(
            r.can_admit, reason="physical C4 intake availability"
        )
        self.gc.runtime_stats.setOwnedPieceUuids(
            b.piece.uuid for b in r.bindings.values()
        )
        self.gc.runtime_stats.observeState("classification", self.phaseName())

    def pause(self):
        self.shared.set_classification_gate(False, reason="paused physical C4")
        self._paused = True
        if self._paused_at is None:
            self._paused_at = time.monotonic()
        if self.runtime:
            self.runtime.pause()
            # Consume the single accepted finite request under the same owner;
            # pausing cannot discard its receipt or manufacture its completion.
            while (
                self.runtime._pending is not None
                and not self.fault
                and not self.positioner.fault
            ):
                try:
                    self.runtime.tick(time.monotonic())
                except Exception as exc:
                    self._latch_fault(exc)
                    break
                time.sleep(0.05)

    def resume(self):
        if self.fault:
            return
        now = time.monotonic()
        if self._paused_at is not None:
            duration = now - self._paused_at
            if self.handoff:
                self.handoff._arrival_armed_at_mono += duration
                ep = self.handoff._episode
                if ep.recovery_deadline_mono is not None:
                    ep.recovery_deadline_mono += duration
            if self.runtime:
                for b in self.runtime.bindings.values():
                    if b.capture_until:
                        b.capture_until += duration
            self._paused_at = None
        try:
            if self.runtime:
                self.runtime.resume(now)
            self._paused = False
        except Exception as exc:
            self._latch_fault(exc)

    def begin_recovery(self):
        self.shared.set_classification_gate(False, reason="complete C4 recovery")
        if not all(
            getattr(self.irl, f"c_channel_{n}_rotor_stepper").stopped for n in (1, 2, 3)
        ):
            raise PositionError(
                "upstream motion must finish before complete C4 recovery"
            )
        interrupted = self.fault or (self.positioner and self.positioner.fault)
        if not interrupted:
            self.pause()
        old = self.positioner
        stepper = self.irl.carousel_stepper
        if interrupted or self.fault:
            self._fault("stopping interrupted C4 request for complete recovery")
        if not stepper.stationary_verified():
            raise PositionError("C4 request is not physically stopped")
        if not stepper.enabled:
            stepper.enabled = True
        # Every explicit drain establishes current physical phase, including
        # displacement that has not yet been diagnosed by a resume attempt.
        if old is None:
            self.positioner = create_positioner(self.irl, self.irl_config, self._fault)
        else:
            old.motor.stationary_token()
            self.positioner = MarkerPositioner(
                old.motor,
                old.source,
                old.mapping,
                self._fault,
                limits=old.limits,
                clock=old.clock,
            )
        self._bootstrap = None
        self._reestablishing = self.runtime is not None
        if self.runtime:
            for binding in self.runtime.bindings.values():
                self.runtime.distribution.reject(binding.piece, "complete C4 recovery")
                binding.episode.state = "discard_bound"
                if binding.journey:
                    self.runtime.journeys.close(binding.journey)
        self.handoff = None
        self.shared.c3_transfer_episode = None
        self._reject_opened = False
        self.fault = None
        self._paused = False
        self._paused_at = None

    def cleanup(self):
        # Stop/teardown must never claim C4 empty. New process/controller state
        # starts UNKNOWN and completes the same reject sweep before admission.
        self.pause()
