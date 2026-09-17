"""Manual flap commands share the existing hardware lifecycle ownership."""

from functools import wraps
from inspect import signature

from fastapi import HTTPException

from defs.sorter_controller import SorterLifecycle
from server import shared_state
from subsystems.distribution.states import DistributionState


def ensure_manual_flap_control() -> None:
    """Called with hardware_lifecycle_lock held; never consumes ownership."""
    worker = shared_state.hardware_worker_thread
    if (worker is not None and worker.is_alive()) or shared_state.hardware_state in {
        "homing", "initializing",
    }:
        raise HTTPException(409, "Hardware recovery owns the flaps.")
    controller = shared_state.controller_ref
    if controller is None:
        return  # Setup may have an active IRL before a sorting controller exists.
    if controller.state not in {SorterLifecycle.PAUSED, SorterLifecycle.READY}:
        raise HTTPException(409, "Pause sorting before manual flap or bin movement.")
    coordinator = getattr(controller, "coordinator", None)
    distribution = getattr(coordinator, "distribution", None)
    if distribution is not None and distribution.current_state != DistributionState.IDLE:
        raise HTTPException(409, "A retained distribution transaction owns the flap/bin route.")
    shared = getattr(coordinator, "shared", None)
    transport = getattr(shared, "transport", None)
    if transport is not None and (
        transport.getPieceForDistributionPositioning() is not None
        or transport.getPieceForDistributionDrop() is not None
    ):
        raise HTTPException(409, "A retained distribution transaction owns the flap/bin route.")


def manual_flap_operation(function):
    """Protect dispatch and feedback as one operation, rejecting stale requests."""
    @wraps(function)
    def guarded(*args, **kwargs):
        if not shared_state.hardware_lifecycle_lock.acquire(blocking=False):
            raise HTTPException(409, "Another hardware operation is in progress.")
        try:
            ensure_manual_flap_control()
            return function(*args, **kwargs)
        finally:
            shared_state.hardware_lifecycle_lock.release()

    # Resolve postponed annotations in the endpoint's own module for FastAPI.
    guarded.__signature__ = signature(function, eval_str=True)
    return guarded
