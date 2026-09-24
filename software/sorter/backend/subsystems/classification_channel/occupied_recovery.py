"""Bounded recovery of checkpointed owners without any feeder advancement."""
import math
import time
import uuid

from defs.known_object import PieceStage
from .occupied_checkpoint import (
    CheckpointStore, RecoveryError, configuration_fingerprint, match_owners,
    verify_delivery_records, verify_unrecorded,
)


class RecoveryCancelled(RecoveryError):
    """The operator requested a stop while occupied recovery was active."""


def _check_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise RecoveryCancelled("Occupied recovery cancellation requested by operator")


def _request_c4_reject(gc, reason: str) -> None:
    stats = getattr(gc, "runtime_stats", None)
    request = getattr(stats, "requestC4Reject", None)
    if callable(request):
        request(str(reason))


def _recovery_stops_verified(stops):
    if not isinstance(stops, dict):
        return False
    required = (
        "c_channel_2_rotor_stepper",
        "c_channel_3_rotor_stepper",
        "carousel_stepper",
        "chute_stepper",
        "servos_stopped",
    )
    return all(stops.get(name) is True for name in required)


def _unverified_recovery_stops():
    return {
        "c_channel_2_rotor_stepper": False,
        "c_channel_3_rotor_stepper": False,
        "carousel_stepper": False,
        "chute_stepper": False,
        "servos_stopped": False,
    }


def prepare_controller_checkpoint(controller, *, store=None):
    from server import shared_state

    admission_lock = shared_state.occupied_checkpoint_transition_lock
    if not admission_lock.acquire(blocking=False):
        raise RecoveryError("Manual motion dispatch is active; retry checkpoint preparation")
    try:
        return _prepare_controller_checkpoint(controller, store=store)
    finally:
        admission_lock.release()


def _prepare_controller_checkpoint(controller, *, store=None):
    from defs.known_object import ClassificationStatus
    from utils.event import knownObjectToEvent

    if controller.state.value != "paused":
        raise RecoveryError("Checkpoint requires an already-paused controller")
    coordinator = controller.coordinator
    flow = getattr(coordinator.classification, "_two_piece", None)
    if flow is None or flow.hasSafetyHold():
        raise RecoveryError("Checkpoint requires unambiguous retained two-piece ownership")
    drop = coordinator.transport.getPieceForDistributionDrop()
    if drop is not None:
        distribution = getattr(coordinator, "distribution", None)
        if distribution is None:
            raise RecoveryError("Distribution status is unavailable for the retained chute handoff")
        state = getattr(distribution, "current_state", None)
        state_value = getattr(state, "value", state)
        settled_uuid = getattr(distribution, "last_settled_drop_uuid", None)
        drop_uuid = getattr(drop, "uuid", None)
        drop_stage = getattr(drop, "stage", None)
        distributed_at = getattr(drop, "distributed_at", None)
        fully_settled = (
            drop_stage == PieceStage.distributed
            and isinstance(distributed_at, (int, float))
            and math.isfinite(float(distributed_at))
            and isinstance(drop_uuid, str)
            and bool(drop_uuid)
            and settled_uuid == drop_uuid
            and state_value == "idle"
            and getattr(coordinator.shared, "distribution_ready", False) is True
        )
        if not fully_settled:
            raise RecoveryError(
                "The chute handoff has not fully settled; it cannot be omitted from C4 ownership"
            )
    _ensure_checkpoint_motion_quiescent(controller)
    perception = controller.gc.perception_service
    position = verify_intake(controller.irl, perception)
    deadline = time.monotonic() + 6.0
    while flow.reconcilingPause() and time.monotonic() < deadline:
        if verify_intake(controller.irl, perception) != position:
            raise RecoveryError("C3 moved during checkpoint preparation")
        state, _ = fresh_pair(perception)
        flow._reconcilePause(state, time.monotonic())
        if flow.hasSafetyHold():
            raise RecoveryError("Paused ownership could not be reconciled")
        time.sleep(0.1)
    if flow.reconcilingPause() or not controller.irl.carousel_stepper.stationary_verified():
        raise RecoveryError("C4 has not settled for checkpoint preparation")
    state, size = fresh_pair(perception)
    if state.tracker_generation != flow._tracker_generation or len(state.pieces) != len(flow._pieces):
        raise RecoveryError("C4 owner set differs from current perception")
    owners = []
    for tp in flow._pieces.values():
        if not tp.visible or tp.known_object is None:
            raise RecoveryError("A visible physical owner has no recorded identity")
        data = knownObjectToEvent(tp.known_object).data.model_dump(mode="json")
        recognized = data["classification_status"] == ClassificationStatus.classified.value and data["part_id"] is not None
        if not recognized:
            data.update(part_id=None, category_id=None, destination_bin=None,
                        classification_status="unknown", moving_avg_price=None,
                        high_value_routed=False, not_in_inventory=False)
        owners.append({"object": data, "bbox": list(tp.bbox),
                       "disposition": "recognized" if recognized else "reject"})
    plan = {"version": 1, "operation_id": str(uuid.uuid4()), "provenance": "paused_controller",
            "confirmation": "Retained controller ownership reconciled with stationary fresh perception",
            "config_fingerprint": configuration_fingerprint(controller.gc), "frame_size": list(size), "owners": owners}
    matched = match_owners(plan, state, size, time.time())
    expected = {tp.known_object.uuid: tp.track_id for tp in flow._pieces.values()}
    if any(expected[entry["object"]["uuid"]] != obs.sv_bt_track_id for entry, obs in matched):
        raise RecoveryError("Checkpoint would reassign a retained owner's identity")
    verify_parked(plan, perception)
    verify_unrecorded(plan)
    (store or CheckpointStore()).prepare(plan)
    coordinator.shared.set_classification_gate(False, reason="occupied_checkpoint")
    coordinator.shared.set_distribution_gate(False, reason="occupied_checkpoint")
    return plan["operation_id"]


def _ensure_checkpoint_motion_quiescent(controller):
    from server import shared_state

    relevant_stepper_keys = {
        "c_channel_2",
        "c_channel_3",
        "c_channel_4",
        "carousel",
        "chute",
    }
    active_stepper_moves = sorted(
        name
        for name, lock in shared_state.pulse_locks.items()
        if name in relevant_stepper_keys and lock.locked()
    )
    if active_stepper_moves:
        raise RecoveryError(
            "Manual stepper motion is still in flight: " + ", ".join(active_stepper_moves)
        )

    from subsystems.distribution.chute_stress import getActiveChuteStressRunner
    from subsystems.power_stress import getActivePowerStressRunner

    for name, getter in (
        ("chute stress", getActiveChuteStressRunner),
        ("power stress", getActivePowerStressRunner),
    ):
        runner = getter()
        if runner is not None and runner.isActive():
            raise RecoveryError(f"Cannot checkpoint while {name} is active")

    for attr in (
        "c_channel_2_rotor_stepper",
        "c_channel_3_rotor_stepper",
        "carousel_stepper",
        "chute_stepper",
    ):
        motor = getattr(controller.irl, attr, None)
        if motor is None or not motor.stationary_verified():
            raise RecoveryError(f"{attr} is not verified stationary for checkpoint preparation")

    for servo in getattr(controller.irl, "servos", ()):
        if getattr(servo, "stopped", None) is not True:
            raise RecoveryError("All flap servos must be stopped for checkpoint preparation")


def fresh_pair(perception):
    state = perception.read_state(4)
    pair = perception.read_pieces_and_frame(4)
    if pair is None or pair[1].timestamp != state.ts or tuple(pair[0]) != state.pieces:
        raise RecoveryError("Waiting for paired C4 observations")
    frame = pair[1].bgr
    return state, (int(frame.shape[1]), int(frame.shape[0]))


def verify_parked(plan, perception, *, timeout=6.0, cancel_event=None):
    deadline = time.monotonic() + timeout
    previous = None
    last_error = None
    while time.monotonic() < deadline:
        _check_cancelled(cancel_event)
        try:
            state, size = fresh_pair(perception)
            matches = match_owners(plan, state, size, time.time())
            identities = tuple(observation.sv_bt_track_id for _, observation in matches)
            if previous is not None and state.ts > previous[0] and state.tracker_generation == previous[1] and identities == previous[2]:
                return state, size
            previous = (state.ts, state.tracker_generation, identities)
        except RecoveryError as exc:
            previous = None
            last_error = exc
        _check_cancelled(cancel_event)
        time.sleep(0.1)
    _check_cancelled(cancel_event)
    raise RecoveryError(f"C4 checkpoint could not be verified: {last_error or 'no distinct stable frames'}")


def verify_intake(irl, perception):
    for attr in ("c_channel_2_rotor_stepper", "c_channel_3_rotor_stepper"):
        if not getattr(irl, attr).stationary_verified():
            raise RecoveryError("Feeder motion prevents occupied recovery")
    state = perception.read_state(3)
    if not math.isfinite(state.ts) or not 0 <= time.time() - state.ts <= 1.0:
        raise RecoveryError("C3 observation is not fresh")
    if state.n_pieces != len(state.pieces):
        raise RecoveryError("C3 occupancy is unresolved")
    for piece in state.pieces:
        clearance = piece.clearance_to_exit_deg
        if clearance is None or not math.isfinite(clearance) or clearance <= 0:
            raise RecoveryError("C3 has a piece at its exit")
    return irl.c_channel_3_rotor_stepper.position


def stop_recovery_hardware(irl):
    """Bounded stop-only cleanup; never address C1 or trust a stop ACK."""
    targets = {name: getattr(irl, name, None) for name in (
        "c_channel_2_rotor_stepper", "c_channel_3_rotor_stepper", "carousel_stepper", "chute_stepper")}
    verified = {name: False for name in targets}
    attempts = {name: 0 for name in targets}
    started = time.monotonic()
    next_attempt = started
    while time.monotonic() - started < 6.0:
        now = time.monotonic()
        for name, motor in targets.items():
            if motor is None:
                continue
            try:
                verified[name] = bool(motor.stationary_verified())
            except Exception:
                verified[name] = False
            if not verified[name] and attempts[name] < 3 and now >= next_attempt:
                attempts[name] += 1
                try:
                    motor.move_at_speed(0, force=True)
                except Exception:
                    verified[name] = False
        if all(verified.values()):
            break
        if now >= next_attempt:
            next_attempt = now + 0.5
        time.sleep(0.1)
    servos = list(getattr(irl, "servos", []))
    servos_stopped = True
    for servo in servos:
        stop = getattr(servo, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                servos_stopped = False
    servo_deadline = time.monotonic() + 2.0
    while servos_stopped and time.monotonic() < servo_deadline:
        moving = False
        for servo in servos:
            try:
                if not bool(getattr(servo, "stopped", True)):
                    moving = True
            except Exception:
                servos_stopped = False
                break
        if not moving or not servos_stopped:
            break
        time.sleep(0.1)
    if servos_stopped:
        try:
            servos_stopped = all(bool(getattr(servo, "stopped", True)) for servo in servos)
        except Exception:
            servos_stopped = False
    verified["servos_stopped"] = servos_stopped
    return verified


def finalize_recovery_distribution(coordinator, owners, *, before_ack=None):
    """Clear the final drop only after the normal delivery contract settles."""
    if not owners or any(owner.stage != PieceStage.distributed for owner in owners):
        return False

    distribution = getattr(coordinator, "distribution", None)
    state = getattr(distribution, "current_state", None)
    if getattr(state, "value", state) != "idle":
        return False
    shared = getattr(coordinator, "shared", None)
    if shared is None or not bool(getattr(shared, "distribution_ready", False)):
        return False

    transport = getattr(coordinator, "transport", None)
    if transport is None:
        raise RecoveryError("Recovery transport is unavailable at final delivery")
    if transport.getPieceForDistributionPositioning() is not None:
        raise RecoveryError("A positioning slot remains occupied after the final delivery")
    if transport.activePieces():
        raise RecoveryError("An active transport slot remains occupied after the final delivery")
    if transport.hasPendingClassifications():
        raise RecoveryError("A classification remains pending after the final delivery")

    drop = transport.getPieceForDistributionDrop()
    if drop is None:
        return False
    drop_uuid = str(getattr(drop, "uuid", ""))
    owner_uuids = {str(owner.uuid) for owner in owners}
    if not drop_uuid or drop_uuid not in owner_uuids:
        raise RecoveryError("Final distribution drop does not belong to a recovered owner")
    if drop.stage != PieceStage.distributed or drop.distributed_at is None:
        raise RecoveryError("Final distribution drop has not been committed")
    if getattr(distribution, "last_settled_drop_uuid", None) != drop_uuid:
        return False

    if before_ack is not None:
        before_ack()

    acknowledge = getattr(transport, "acknowledgeCompletedDistributionDrop", None)
    if not callable(acknowledge):
        raise RecoveryError("Recovery transport cannot clear a completed distribution drop")
    try:
        acknowledge(drop_uuid)
    except Exception as exc:
        raise RecoveryError("Final distribution drop could not be acknowledged safely") from exc

    if (
        transport.getPieceForDistributionDrop() is not None
        or transport.getPieceForDistributionPositioning() is not None
        or transport.activePieces()
        or transport.hasPendingClassifications()
    ):
        raise RecoveryError("Transport slots did not clear after final delivery acknowledgement")
    return True


def recover_occupied(*, gc, build_runtime, publish_runtime, store=None,
                     cancel_event=None, cancel_complete_event=None,
                     complete_event=None):
    """Called under the exclusive hardware lifecycle worker, never on startup."""
    store = store or CheckpointStore()
    consumed = False
    try:
        _check_cancelled(cancel_event)
        checkpoint = store.read()
        _check_cancelled(cancel_event)
        if checkpoint is None or checkpoint["phase"] != "prepared":
            raise RecoveryError("No unused prepared checkpoint; interrupted recovery needs reconciliation")
        plan = checkpoint["plan"]
        try:
            fingerprint = configuration_fingerprint(gc)
            _check_cancelled(cancel_event)
            if fingerprint != plan["config_fingerprint"]:
                raise RecoveryError("Routing or machine configuration changed after checkpoint")
            verify_parked(plan, gc.perception_service, cancel_event=cancel_event)
            _check_cancelled(cancel_event)
            verify_unrecorded(plan)
            _check_cancelled(cancel_event)
        except RecoveryCancelled:
            raise
        except RecoveryError as exc:
            _request_c4_reject(
                gc, f"Occupied C4 owner binding is uncertain; reject all retained material: {exc}"
            )
            return

        # Pause and recovery agree on one winner. If Pause sets cancellation
        # first, preserve the prepared checkpoint and never initialize hardware;
        # if this transition wins, Pause cancels the consumed recovery instead.
        from server import shared_state

        with shared_state.hardware_lifecycle_lock:
            _check_cancelled(cancel_event)
            store.transition(plan["operation_id"], "prepared", "running", {})
            consumed = True
    except RecoveryCancelled:
        # No motion has been started and the one-shot checkpoint is still
        # prepared, so this is a safe terminal cancellation without hardware
        # stop commands. This event does not claim the machine was physically
        # paused; it only tells Pause the private worker has exited cleanly.
        if not consumed and cancel_complete_event is not None:
            cancel_complete_event.set()
        raise

    controller = None
    owners = []
    uncertain_owner_reason = None
    try:
        controller = build_runtime()
        _check_cancelled(cancel_event)
        coordinator = controller.coordinator
        irl = controller.irl
        flow = getattr(coordinator.classification, "_two_piece", None)
        if flow is None:
            raise RecoveryError("Occupied recovery requires the two-piece controller")
        if getattr(coordinator.shared, "sample_collection_mode", False):
            raise RecoveryError("Sample-collection passthrough cannot preserve recognized destinations")
        for attr in ("c_channel_2_rotor_stepper", "c_channel_3_rotor_stepper", "carousel_stepper"):
            if not getattr(irl, attr).stationary_verified():
                raise RecoveryError(f"{attr} is moving; recovery cannot begin")
        from subsystems.feeder.go_to_angle.flow import CLASSIFICATION_PENDING_ADMISSION_MS
        intake_position = verify_intake(irl, gc.perception_service)
        settle_until = time.monotonic() + CLASSIFICATION_PENDING_ADMISSION_MS / 1000.0
        while time.monotonic() < settle_until:
            _check_cancelled(cancel_event)
            if verify_intake(irl, gc.perception_service) != intake_position:
                raise RecoveryError("C3 changed position during intake settling")
            time.sleep(0.1)
        try:
            state, size = verify_parked(plan, gc.perception_service, cancel_event=cancel_event)
            owners = flow.restoreOccupiedCheckpoint(plan, state, size)
        except RecoveryCancelled:
            raise
        except RecoveryError as exc:
            uncertain_owner_reason = (
                f"Occupied C4 owner binding is uncertain; reject all retained material: {exc}"
            )
            _request_c4_reject(gc, uncertain_owner_reason)
            raise
        coordinator.shared.retained_recovery_routes = {
            owner.uuid: owner.destination_bin for owner in owners if owner.destination_bin is not None
        }
        # C1/C2/C3 are never enabled or advanced by this worker. Normal Resume
        # stays blocked until the checkpoint has completed.
        if irl.carousel_stepper.software_disabled or irl.chute_stepper.software_disabled:
            raise RecoveryError("A recovery motor is software-disabled")
        irl.carousel_stepper.enabled = True
        irl.chute_stepper.enabled = True
        for servo in irl.servos:
            _check_cancelled(cancel_event)
            if not getattr(servo, "available", False) or not getattr(servo, "is_calibrated", False):
                raise RecoveryError("Recovery flap is unavailable or uncalibrated")
            servo.open()
        flap_deadline = time.monotonic() + 6.0
        while not all(servo.stopped and servo.isOpen() for servo in irl.servos):
            _check_cancelled(cancel_event)
            if time.monotonic() >= flap_deadline:
                raise RecoveryError("Recovery flaps did not settle open")
            time.sleep(0.1)
        _check_cancelled(cancel_event)
        if not irl.chute.home():
            raise RecoveryError("Recovery chute homing failed")
        _check_cancelled(cancel_event)
        try:
            verify_parked(plan, gc.perception_service, cancel_event=cancel_event)
        except RecoveryCancelled:
            raise
        except RecoveryError as exc:
            uncertain_owner_reason = (
                f"Occupied C4 owner visibility became uncertain; reject all retained material: {exc}"
            )
            _request_c4_reject(gc, uncertain_owner_reason)
            raise
        if not irl.carousel_stepper.stationary_verified() or irl.carousel_stepper.position != flow._motor_position:
            raise RecoveryError("C4 moved during chute initialization")
        deadline = time.monotonic() + 60.0 * len(owners)
        known_ids = set(flow._pieces)
        while time.monotonic() < deadline:
            _check_cancelled(cancel_event)
            if gc.runtime_stats.activeIncident() is not None or flow.hasSafetyHold():
                raise RecoveryError("Recovery encountered a machine incident")
            if verify_intake(irl, gc.perception_service) != intake_position:
                raise RecoveryError("C3 moved during occupied recovery")
            observed = gc.perception_service.read_state(4)
            if any(piece.sv_bt_track_id not in known_ids for piece in observed.pieces):
                uncertain_owner_reason = "An uncheckpointed piece appeared on C4; reject all retained material"
                _request_c4_reject(gc, uncertain_owner_reason)
                raise RecoveryError("An uncheckpointed piece appeared on C4")
            coordinator.distribution.step()
            _check_cancelled(cancel_event)
            flow.step()
            _check_cancelled(cancel_event)
            coordinator.shared.set_classification_gate(False, reason="occupied_recovery_no_intake")
            if all(owner.stage == PieceStage.distributed for owner in owners) and not flow._pieces:
                _check_cancelled(cancel_event)
                if not irl.carousel_stepper.stationary_verified():
                    raise RecoveryError("C4 completion is not stationary")
                if not finalize_recovery_distribution(
                    coordinator,
                    owners,
                    before_ack=lambda: verify_delivery_records(plan),
                ):
                    time.sleep(0.02)
                    continue
                from server import shared_state

                with shared_state.hardware_lifecycle_lock:
                    _check_cancelled(cancel_event)
                    store.transition(plan["operation_id"], "running", "complete", {
                        "owners": [{"uuid": obj.uuid, "destination_bin": obj.destination_bin,
                                    "distributed_at": obj.distributed_at} for obj in owners],
                    })
                    if complete_event is not None:
                        complete_event.set()
                del coordinator.shared.retained_recovery_routes
                controller.start()  # Paused, not normal sorting.
                publish_runtime(controller)
                return
            time.sleep(0.02)
        raise RecoveryError("Occupied recovery exceeded its bounded deadline")
    except Exception as exc:
        cancelled = isinstance(exc, RecoveryCancelled)
        reject_requested = bool(
            getattr(getattr(gc, "runtime_stats", None), "c4RejectRequested", lambda: False)()
        )
        stops = None
        if controller is not None:
            flow = getattr(controller.coordinator.classification, "_two_piece", None)
            if cancelled:
                if flow is not None:
                    try:
                        flow._hold("Occupied recovery cancelled by operator")
                    except Exception:
                        pass
                # Verify every owned axis after the flow's C4 hold hook has
                # had a chance to issue its stop-only request.
                try:
                    stops = stop_recovery_hardware(controller.irl)
                except Exception:
                    stops = _unverified_recovery_stops()
            else:
                try:
                    stops = stop_recovery_hardware(controller.irl)
                except Exception:
                    stops = _unverified_recovery_stops()
                if flow is not None:
                    try:
                        if reject_requested:
                            flow._hold_for_reject(
                                uncertain_owner_reason or
                                f"Occupied recovery left uncertain C4 ownership: {exc}"
                            )
                        else:
                            flow._hold(f"Occupied recovery failed: {exc}")
                    except Exception:
                        pass
                stop_deadline = time.monotonic() + 6.5
                while flow is not None and time.monotonic() < stop_deadline and not flow._hold_stop_verified:
                    try:
                        flow.serviceSafetyHold()
                    except Exception:
                        break
                    time.sleep(0.1)
        stops_verified = _recovery_stops_verified(stops)
        store.transition(plan["operation_id"], "running", "failed", {
            "error": str(exc),
            "cancelled_by_operator": cancelled,
            "stops_verified": stops,
            "reject_requested": reject_requested,
            "uncertain_owner_reason": uncertain_owner_reason,
        })
        if controller is not None and stops_verified:
            controller.start()
            publish_runtime(controller)
        if cancelled and _recovery_stops_verified(stops) and cancel_complete_event is not None:
            cancel_complete_event.set()
        if reject_requested and not cancelled and stops_verified:
            return
        raise
