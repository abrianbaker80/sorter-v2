"""Destructive operator drain using the installed C4 and reject-path primitives."""

import time

from subsystems.classification_channel.five_sector_platter import C4FiveSectorPlatter
from subsystems.classification_channel.simple_state_machine_rev01.constants import C4_TRAVEL_SIGN
from subsystems.distribution.flap_path import flap_path_settled, validate_flaps
from subsystems.distribution.sending import CHUTE_SETTLE_MS
from subsystems.classification_channel.simple_state_machine_rev01.context import SimpleStateMachineRev01Context


def drain_all_pockets(irl, config, *, progress=lambda message: None,
                      clock=time.monotonic, sleep=time.sleep):
    """Sweep a full revolution from any starting phase; stop on real failures."""
    stepper = irl.carousel_stepper
    platter = C4FiveSectorPlatter.from_irl_config(config)
    servos = list(irl.servos)

    def wait(predicate, timeout, message):
        deadline = clock() + timeout
        while not predicate():
            if clock() >= deadline:
                raise RuntimeError(message)
            sleep(0.05)

    def wait_stepper_stopped(timeout, stall_message, timeout_message):
        deadline = clock() + timeout
        while not bool(stepper.stopped):
            if bool(getattr(stepper, "stalled", False)):
                raise RuntimeError(stall_message)
            if clock() >= deadline:
                raise RuntimeError(timeout_message)
            sleep(0.05)
        if bool(getattr(stepper, "stalled", False)):
            raise RuntimeError(stall_message)

    def upstream_stopped():
        return all(bool(getattr(irl, f"c_channel_{n}_rotor_stepper").stopped)
                   for n in (1, 2, 3))

    try:
        wait(upstream_stopped, 5.0, "Upstream motor did not stop for C4 drain")
        if getattr(stepper, "software_disabled", False):
            raise RuntimeError("C4 motor is disabled")
        if bool(getattr(stepper, "stalled", False)):
            raise RuntimeError("C4 motor is stalled")
        wait_stepper_stopped(5.0, "C4 motor stalled before reject drain",
                             "C4 motor did not stop")
        validate_flaps(servos, None)
        for servo in servos:
            if servo.open() is False:
                raise RuntimeError("Reject flap command was rejected")
        wait(lambda: flap_path_settled(servos, None), 6.0,
             "Reject flap route did not settle")
        sleep(CHUTE_SETTLE_MS / 1000.0)
        origin = int(stepper.position)
        stepper.set_speed_limits(16, max(16, int(
            SimpleStateMachineRev01Context().config.precise_converge_speed_usteps_per_s)))
        for index in range(1, platter.sector_count + 1):
            if not upstream_stopped() or not flap_path_settled(servos, None):
                raise RuntimeError("Drain lost stationary intake or settled reject route")
            if bool(getattr(stepper, "stalled", False)):
                raise RuntimeError("C4 motor stalled during reject drain")
            target = origin + platter.sector_position_microsteps(index * C4_TRAVEL_SIGN)
            delta = target - int(stepper.position)
            if not delta or not bool(stepper.move_steps(delta)):
                raise RuntimeError("C4 drain index command rejected")
            wait_stepper_stopped(5.0, "C4 motor stalled during reject drain",
                                 "C4 drain index timed out")
            if abs(int(stepper.position) - target) > 1:
                raise RuntimeError("C4 drain index stopped off target")
            sleep(CHUTE_SETTLE_MS / 1000.0)
            if bool(getattr(stepper, "stalled", False)):
                raise RuntimeError("C4 motor stalled during reject drain")
            progress(f"Reject drain: {index}/{platter.sector_count} pockets swept")
        return {"pockets_swept": platter.sector_count, "route": "reject",
                "physical_basis": "full circuit with settled open flaps and normal fall-clear"}
    except Exception:
        stepper.move_at_speed(0)
        raise

def drain_controller(controller, *, progress=lambda message: None,
                     clock=time.monotonic, sleep=time.sleep):
    """Complete recovery through the retained single C4 motion owner.

    Called only with the lifecycle/operation locks held. Finite upstream moves
    finish; new releases stay closed. The full sweep does not inspect identities.
    """
    from .marker_positioner import PositionError
    owner = controller.coordinator.classification._delegate
    if not getattr(owner, "physical_c4_authority", False):
        raise RuntimeError("physical C4 controller required")
    owner.shared.set_classification_gate(False, reason="complete C4 recovery")
    controller.coordinator.feeder.hold_motion()
    deadline = clock() + 12
    while not all(getattr(owner.irl, f'c_channel_{n}_rotor_stepper').stopped for n in (1, 2, 3)):
        if clock() >= deadline:
            raise RuntimeError("upstream motor did not complete before C4 recovery")
        sleep(.05)
    # A generation already discharged belongs to the existing downstream
    # transaction. Let its finite fall/settle and accounting finish first.
    from defs.known_object import PieceStage
    while True:
        drop = owner.transport.getPieceForDistributionDrop()
        if drop is None or drop.stage is PieceStage.distributed:
            break
        if clock() >= deadline:
            raise RuntimeError("downstream discharge did not complete before C4 recovery")
        controller.coordinator.distribution.step()
        sleep(.05)
    owner.begin_recovery()
    started = clock()
    while owner.runtime is None or owner.runtime.recovering or owner._reestablishing:
        if clock() - started > 240:
            raise RuntimeError("complete marker recovery did not finish")
        owner.step()
        if owner.fault:
            raise PositionError(owner.fault)
        if owner.runtime:
            progress(f"Reject sweep boundary {owner.runtime.fifo.boundary}")
        sleep(.05)
    boundary = owner.runtime.fifo.boundary
    owner.pause()
    perception = getattr(controller.gc, "perception_service", None)
    if perception is not None:
        perception.reset_tracker_generation(4)
    controller.gc.runtime_stats.reconcileC4Drain()
    controller.shared_c4_recovery_complete = True
    return {"route": "reject", "pockets_swept": boundary-owner.runtime.recovery_start,
            "boundary": boundary, "physical_basis": "marker-confirmed full circuit at P0"}

