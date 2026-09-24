"""Bridge to the existing distributor's route selection, flaps and accounting."""

import time

from defs.known_object import ClassificationStatus, PieceStage
from utils.event import knownObjectToEvent
from .demand_planner import ChuteObservation
from .physical_fifo import PocketState
from .physical_runtime import REJECT


def destination(piece):
    return (
        REJECT
        if piece.destination_bin is None
        else "bin:" + ":".join(map(str, piece.destination_bin))
    )


class PhysicalDistribution:
    def __init__(self, shared, event_queue, gc=None):
        self.shared, self.events = shared, event_queue
        self.gc = gc
        self.transport = shared.transport
        self.recovering = False

    def reject(self, piece, reason):
        if self.transport.getPieceForDistributionPositioning() is piece:
            self.transport.cancelPieceForDistribution(piece.uuid)
        if piece.harvest_allocation_id and piece.c4_marker_exit_boundary is None:
            from project_harvest_runtime import retire_c4_planned_allocations

            retire_c4_planned_allocations(self.gc, piece_ids=[piece.uuid])
            piece.harvest_allocation_id = None
            piece.harvest_project_id = None
        piece.c4_discard = True
        piece.forced_reject_reason = reason
        piece.reject_on_routing_failure = True
        piece.part_id = None
        piece.destination_bin = None
        piece.classification_status = ClassificationStatus.unknown
        piece.stage = PieceStage.created
        self.events.put(knownObjectToEvent(piece))

    def clear(self):
        drop = self.transport.getPieceForDistributionDrop()
        return self.shared.distribution_ready and (
            drop is None or drop.stage is PieceStage.distributed
        )

    def observe(self, runtime, now):
        if self.recovering:
            return ChuteObservation(
                REJECT if self.shared.c4_reject_path_ready() else None, {}
            )
        loads = sorted(
            (p for p in runtime.fifo.pockets if p.state is not PocketState.EMPTY),
            key=lambda p: (runtime.fifo.station_of(p.pocket_id) + 1) % 10,
        )
        # The EXIT pocket is at station 0, followed by stations 1..9.
        loads.sort(key=lambda p: runtime.fifo.station_of(p.pocket_id))
        for p in loads:
            binding = runtime.bindings.get((p.pocket_id, p.generation))
            if binding is None and self.recovering:
                continue  # complete recovery uses the all-open path below
            if binding is None:
                raise RuntimeError("physical load has no generation binding")
            obj = binding.piece
            if p.state is PocketState.PENDING and not binding.result_applied:
                return ChuteObservation(None, {})
            positioned = self.transport.getPieceForDistributionPositioning()
            if positioned is None:
                if (
                    self.transport.isCanceledPieceForDistribution(obj.uuid)
                    or not self.clear()
                ):
                    return ChuteObservation(None, {})
                self.transport.placePieceForDistribution(obj)
                return ChuteObservation(None, {})
            if positioned is not obj:
                return ChuteObservation(None, {})
            route = destination(obj)
            selected = (
                obj.destination_bin is not None or obj.stage is PieceStage.distributing
            )
            if selected and p.state is PocketState.PENDING:
                runtime.fifo.resolve(
                    *binding.key,
                    PocketState.ROUTED if route != REJECT else PocketState.DISCARD,
                    destination=route if route != REJECT else None,
                )
            if obj.stage is PieceStage.distributing and self.shared.distribution_ready:
                # Installed positioning can publish READY before all opposing
                # flaps settle. The physical C4 owner verifies the entire path.
                if self.gc is not None and not self.gc.disable_servos:
                    from subsystems.distribution.flap_path import flap_path_settled
                    target = obj.destination_bin[0] if obj.destination_bin is not None else None
                    if not flap_path_settled(self.shared.c4_runtime_owner.irl.servos, target):
                        return ChuteObservation(None, {})
                return ChuteObservation(route, {})
            return ChuteObservation(None, {})
        if self.recovering:
            # The lifecycle/controller recovery code establishes and keeps this
            # physical route using the unchanged distributor flap primitives.
            if self.shared.c4_reject_path_ready():
                return ChuteObservation(REJECT, {})
        return ChuteObservation(None, {})

    def discharge(self, event, binding):
        if self.recovering:
            if binding:
                binding.piece.aborted = True
                self.events.put(knownObjectToEvent(binding.piece))
            return
        if (
            binding is None
            or self.transport.getPieceForDistributionPositioning() is not binding.piece
        ):
            raise RuntimeError(
                "confirmed C4 exit has inconsistent distribution ownership"
            )
        obj = binding.piece
        obj.c4_marker_exit_boundary = event.boundary
        obj.c4_pocket_id, obj.c4_generation = binding.key
        obj.updated_at = time.time()
        self.transport.advanceTransport()
        self.shared.set_distribution_gate(False, reason="marker-confirmed C4 discharge")

    def begin_recovery(self):
        self.recovering = True
        self.shared.c4_reset_distribution()

    def reset(self):
        from importlib.util import find_spec
        if find_spec("project_harvest_runtime") is not None:
            from project_harvest_runtime import retire_c4_planned_allocations
            retire_c4_planned_allocations(self.gc)
        self.shared.c4_reset_distribution()
        self.recovering = False
