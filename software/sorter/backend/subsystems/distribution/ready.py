from typing import Optional
import time
import math
import server.shared_state as shared_state
from defs.known_object import PieceStage
from states.base_state import BaseState
from subsystems.shared_variables import SharedVariables
from .states import DistributionState
from .flap_path import flap_path_settled
from .chute import BinAddress, GEAR_RATIO
from irl.config import IRLInterface
from global_config import GlobalConfig


class Ready(BaseState):
    def __init__(self, irl: IRLInterface, gc: GlobalConfig, shared: SharedVariables):
        super().__init__(irl, gc)
        self.shared = shared
        self.signaled = False
        self._signaled_at: float = 0.0
        self._positioned_uuid: str | None = None
        self._positioned_destination: tuple[int, int, int] | None = None

    def step(self) -> Optional[DistributionState]:
        if self.gc.runtime_stats.activeIncident() is not None:
            self.shared.set_distribution_gate(False, reason="incident:active_incident")
            return None
        transport = self.shared.transport
        if not self.signaled:
            # Remember which piece we positioned so we can tell, durably, when it
            # has actually been flung — independent of any gate flag.
            self._positioned_uuid = None
            if transport is not None:
                positioned = transport.getPieceForDistributionPositioning()
                self._positioned_uuid = (
                    positioned.uuid if positioned is not None else None
                )
                self._positioned_destination = (
                    positioned.destination_bin if positioned is not None else None
                )
            if transport is not None and self._positioned_uuid is None:
                # Cancellation can land after POSITIONING returned READY but
                # before this state gets its first tick. Consume that durable
                # marker here as well as in the signaled path below, otherwise
                # classification cannot safely requeue the same object for an
                # automatic reject.
                transport.consumeCanceledPieceForDistribution()
                self.logger.info("Ready: positioned piece was canceled -> IDLE")
                self.shared.set_distribution_gate(True, reason=None)
                return DistributionState.IDLE
            if transport is not None and not self._flapsReady(positioned):
                self.shared.set_distribution_gate(False, reason="required_flap_path_unsettled")
                return None
            self.logger.info("Ready: distribution positioned, signaling ready")
            self.shared.set_distribution_gate(True, reason="ready_chute_aimed")
            self.signaled = True
            self._signaled_at = time.monotonic()

        # Durable drop detection: the piece we positioned has left the
        # positioning slot, i.e. classification flung it into the chute via
        # advanceTransport. This does NOT depend on classification flipping
        # distribution_ready, so the READY -> SENDING transition can't be lost
        # in a gate race (the bug that froze distribution in READY forever).
        # The gate is kept only as a fallback for the other (dynamic/legacy)
        # classification paths that still drive it.
        if (
            transport is not None
            and self._positioned_uuid is not None
            and transport.consumeCanceledPieceForDistribution(self._positioned_uuid)
        ):
            self.logger.info("Ready: positioned piece canceled -> IDLE")
            self.shared.set_distribution_gate(True, reason=None)
            return DistributionState.IDLE

        piece_advanced = False
        if transport is not None and self._positioned_uuid is not None:
            current = transport.getPieceForDistributionPositioning()
            if current is None or current.uuid != self._positioned_uuid:
                piece_advanced = True

        gate_only_drop = self._positioned_uuid is None and not self.shared.distribution_ready
        if piece_advanced or gate_only_drop:
            wait_ms = (time.monotonic() - self._signaled_at) * 1000
            self.logger.info(
                f"Ready: piece dropped -> SENDING (waited={wait_ms:.0f}ms, "
                f"advanced={piece_advanced}, gate_ready={self.shared.distribution_ready})"
            )
            return DistributionState.SENDING

        if transport is not None and self._positioned_uuid is not None and not self._flapsReady(current):
            self.shared.set_distribution_gate(False, reason="required_flap_path_unsettled")
            return None

        if (not self.shared.distribution_ready
                and transport is not None
                and self._positioned_uuid is not None
                and self._canRestoreReadiness(current)):
            # An incident can close the gate without ending this transaction.
            # Republish readiness only; never reset the UUID/drop latch or
            # replay positioning, allocation, or distribution completion.
            self.shared.set_distribution_gate(True, reason="ready_chute_aimed")

        if hasattr(self.gc, "runtime_stats"):
            self.gc.runtime_stats.observeBlockedReason(
                "distribution", "waiting_piece_drop"
            )

        return None

    def _flapsReady(self, piece) -> bool:
        if self.gc.disable_servos:
            return True
        try:
            target = piece.destination_bin[0] if piece.destination_bin is not None else None
            return flap_path_settled(self.irl.servos, target)
        except Exception:
            # Keep the existing readiness hold on failed/unknown route feedback.
            return False

    def _canRestoreReadiness(self, piece) -> bool:
        if (piece.stage != PieceStage.distributing
                or piece.destination_bin != self._positioned_destination
                or shared_state.hardware_state != "ready"
                or shared_state.hardware_error is not None
                or self.shared.chute_move_in_progress):
            return False
        try:
            if not self.gc.disable_chute:
                chute = self.irl.chute
                if not chute.homed or not chute.stepper.stopped:
                    return False
                if piece.destination_bin is not None:
                    if piece.distribution_positioned_at is None:
                        return False
                    target = chute.getAngleForBin(BinAddress(*piece.destination_bin))
                    current = chute.current_angle
                    tolerance = abs(chute.stepper.degrees_for_microsteps(1)) / GEAR_RATIO
                    if (target is None or not all(map(math.isfinite, (target, current, tolerance)))
                            or tolerance <= 0 or abs(current - target) > tolerance):
                        return False
            if not self._flapsReady(piece):
                return False
        except Exception:
            # Missing/invalid feedback cannot authorize a retained load.
            return False
        return self.gc.runtime_stats.activeIncident() is None

    def cleanup(self) -> None:
        super().cleanup()
        self.signaled = False
        self._signaled_at = 0.0
        self._positioned_uuid = None
        self._positioned_destination = None
