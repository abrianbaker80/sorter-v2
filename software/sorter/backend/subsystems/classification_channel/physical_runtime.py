"""Single C4 custody/motion writer; asynchronous recognition returns data only.

The runtime composes the ten physical pockets, demand deadlines and the frozen
marker positioner. Bindings retain images/records, never another occupancy map.
Distribution owns chute positioning and accounting; only marker-confirmed EXIT
events are handed to it. All entry points run under the controller operation lock.
"""

from dataclasses import dataclass

from recognition_journey import RecognitionJourneys
from .demand_planner import C4DemandPlanner, Timing
from .marker_positioner import ConfirmedIndex, PositionError, StableMarkers, error_deg
from .physical_fifo import PhysicalC4FIFO, PocketState


REJECT = "reject"


@dataclass
class Binding:
    pocket_id: int
    generation: int
    piece: object
    episode: object
    journey: object = None
    admitted: bool = False
    result_applied: bool = False
    capture_until: float = 0.0
    landing_scene: object = None

    @property
    def key(self):
        return self.pocket_id, self.generation


class PhysicalC4Runtime:
    def __init__(
        self,
        fifo: PhysicalC4FIFO,
        positioner,
        *,
        speed: int,
        distribution,
        journeys=None,
        apply_result=None,
    ):
        if positioner.boundary != fifo.boundary or positioner.pending is not None:
            raise PositionError("FIFO requires an established marker boundary")
        self.fifo, self.positioner = fifo, positioner
        self.speed, self.distribution = speed, distribution
        # No unmeasured chute ETA is used: release requires stopped alignment.
        self.planner = C4DemandPlanner(fifo, Timing(0, 0), discard_destination=REJECT)
        self.journeys = journeys or RecognitionJourneys()
        self.apply_result = apply_result
        self.bindings: dict[tuple[int, int], Binding] = {}
        self.handoff: Binding | None = None
        self.paused = False
        self.recovering = False
        self.recovery_start = None
        self._verify = None
        self._verify_token = None
        self._verify_started = None
        self._pending = None

    @property
    def can_admit(self):
        return (
            not self.paused
            and not self.recovering
            and self._verify is None
            and self._pending is None
            and self.handoff is None
            and self.fifo.pockets[self.fifo.intake_pocket_id].state is PocketState.EMPTY
        )

    def reserve(self, piece, episode):
        if not self.can_admit:
            raise RuntimeError("C3 release conflicts with physical C4 custody")
        if (
            episode.boundary_index != self.fifo.boundary
            or episode.pocket_id != self.fifo.intake_pocket_id
        ):
            raise RuntimeError("C3 reservation does not match confirmed intake")
        pocket = self.fifo.deposit(
            {"journey_uuid": piece.uuid, "episode_id": episode.episode_id}
        )
        binding = Binding(pocket.pocket_id, pocket.generation, piece, episode)
        episode.piece_uuid = piece.uuid
        piece.c4_pocket_id, piece.c4_generation = binding.key
        self.bindings[binding.key] = self.handoff = binding
        return binding

    def finish_handoff(self, binding, *, arrived: bool, retained_on_c3: bool = False):
        if binding is not self.handoff:
            raise RuntimeError("handoff reservation changed")
        if retained_on_c3:
            return False
        binding.admitted = True
        binding.episode.state = "admitted" if arrived else "discard_bound"
        if not arrived:
            self.discard(binding, "C3 departed; C4 arrival unconfirmed")
        self.handoff = None
        return True

    def current(self, binding):
        p = self.fifo.pockets[binding.pocket_id]
        return (
            self.bindings.get(binding.key) is binding
            and p.generation == binding.generation
            and p.state is not PocketState.EMPTY
        )

    def discard(self, binding, reason):
        if not self.current(binding):
            return False
        changed = self.fifo.resolve(*binding.key, PocketState.DISCARD)
        if changed:
            self.distribution.reject(binding.piece, reason)
        return changed

    def accept_result(self, binding, result):
        if (
            not self.current(binding)
            or binding.result_applied
            or self.fifo.pockets[binding.pocket_id].state is not PocketState.PENDING
        ):
            return False
        binding.result_applied = True
        if (
            result.piece_uuid != binding.piece.uuid
            or result.episode_id != binding.episode.episode_id
            or result.generation != binding.generation
        ):
            self.discard(binding, "recognition association invalid")
            return False
        if result.error or self.apply_result is None:
            self.discard(binding, result.error or "recognition unavailable")
            return False
        self.apply_result(binding, result)
        if not binding.piece.part_id:
            self.discard(binding, "recognition unknown")
            return False
        return True

    def tick(self, now: float, *, feed: bool = False, allow_motion: bool = True):
        # A pause can finish an already accepted finite move, never start another.
        if self._pending is not None:
            confirmation = self.positioner.poll()
            if confirmation is not None:
                if (
                    not isinstance(confirmation, ConfirmedIndex)
                    or confirmation.boundary != self._pending.boundary
                ):
                    raise PositionError(
                        "marker confirmation does not match pending FIFO index"
                    )
                # FIFO microsteps are a logical coordinate, not a motor receipt.
                # Marker corrections must not rebase pocket IDs or generations.
                events = self.planner.complete_index(
                    self._pending,
                    confirmed_microsteps=self._pending.absolute_microsteps,
                    now=now,
                )
                self._pending = None
                for event in events:
                    binding = self.bindings.pop(
                        (event.pocket.pocket_id, event.pocket.generation), None
                    )
                    self.distribution.discharge(event, binding)
                    if binding and binding.journey:
                        self.journeys.close(binding.journey)
            return
        if self.paused:
            return
        if self._verify is not None:
            self.positioner.motor.check_token(self._verify_token)
            if now - self._verify_started > self.positioner.limits.timeout_s:
                raise PositionError("resume marker verification timed out")
            raw_sample = self.positioner.source.sample()
            validated_at = self.positioner.clock()
            sample = self._verify.add(raw_sample, validated_at)
            self.positioner.motor.check_token(self._verify_token)
            if sample is None:
                return
            if (
                abs(
                    error_deg(
                        self.positioner.mapping.phase(self.fifo.boundary),
                        sample.phase_deg,
                    )
                )
                > self.positioner.limits.tolerance_deg
            ):
                raise PositionError(
                    "retained C4 boundary differs from markers; complete recovery required"
                )
            self._verify = None
        for binding in tuple(self.bindings.values()):
            j = binding.journey
            if j is not None:
                with j.lock:
                    result = j.result
                if result is not None:
                    self.accept_result(binding, result)
        if self.recovering and self.fifo.boundary - self.recovery_start >= 10:
            if (
                self.fifo.boundary % 10 == 0
                and now >= self.planner.fall_clear_at
                and self.distribution.clear()
            ):
                if any(p.state is not PocketState.EMPTY for p in self.fifo.pockets):
                    raise RuntimeError("recovery sweep left physical FIFO occupied")
                self.distribution.reset()
                self.recovering = False
                return
        if self.handoff is not None:
            return
        self.distribution.observe(self, now)
        # The demand planner owns the final P0 deadline. Give it an observation
        # before preparing routes so expired enrichment cannot delay release.
        for p in self.fifo.pockets:
            if (
                p.state is PocketState.PENDING
                and self.fifo.station_of(p.pocket_id) == 0
            ):
                binding = self.bindings.get((p.pocket_id, p.generation))
                if binding:
                    self.discard(binding, "recognition deadline")
        chute = self.distribution.observe(self, now)
        if not allow_motion:
            return
        decision = self.planner.plan(
            now,
            chute,
            drain=not feed or self.recovering,
            sweep_empty=(
                self.recovering
                and self.distribution.clear()
                and chute.aligned_destination == REJECT
                and now >= self.planner.fall_clear_at
            ),
        )
        if decision.index is not None:
            self._pending = decision.index
        if self._pending is not None:
            self.positioner.request_index(self._pending.boundary, self.speed)

    def pause(self):
        self.paused = True

    def resume(self, now):
        if self._pending is not None:
            raise PositionError("consume the accepted marker index before resuming")
        self._verify_token = self.positioner.motor.stationary_token()
        self._verify = StableMarkers(
            self.positioner.source.fence(),
            self.positioner.mapping.geometry,
            self.positioner.limits,
        )
        self._verify_started = now
        self.paused = False

    def reestablish_for_recovery(self, confirmation, positioner):
        if (
            not isinstance(confirmation, ConfirmedIndex)
            or positioner.boundary != confirmation.boundary
            or positioner.pending is not None
        ):
            raise PositionError("recovery requires a newly confirmed marker boundary")
        self.fifo.reestablish_for_recovery(confirmation.boundary)
        self.positioner = positioner
        self._pending = self._verify = None
        self.planner = C4DemandPlanner(
            self.fifo, Timing(0, 0), discard_destination=REJECT
        )
        self.recover(unknown=False)

    def recover(self, *, unknown=True):
        if self._pending is not None:
            raise PositionError("finish the owned index before recovery")
        self.fifo.discard_all(include_unknown=unknown)
        self.handoff = None
        for binding in self.bindings.values():
            binding.episode.state = "discard_bound"
            self.distribution.reject(binding.piece, "complete C4 recovery")
            if binding.journey:
                self.journeys.close(binding.journey)
        self.distribution.begin_recovery()
        self.recovering, self.recovery_start = True, self.fifo.boundary
        self.paused = False
