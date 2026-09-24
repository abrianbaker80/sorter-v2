"""Process-exit interlock for private occupied-recovery hardware ownership."""

from __future__ import annotations

from server import shared_state


def _checkpoint_exit_block(action: str) -> str | None:
    from subsystems.classification_channel.occupied_checkpoint import pending_checkpoint

    try:
        checkpoint = pending_checkpoint()
    except Exception as exc:
        return f"Cannot {action}: occupied C4 ownership state is unreadable ({exc})."
    if checkpoint is not None and checkpoint.get("phase") != "prepared":
        phase = str(checkpoint.get("phase") or "unresolved")
        return f"Cannot {action}: occupied C4 ownership remains unresolved ({phase})."
    return None


def _runtime_ownership_exit_block(action: str) -> str | None:
    controller = shared_state.controller_ref
    if controller is None:
        return None
    try:
        coordinator = controller.coordinator
        flow = getattr(coordinator.classification, "_two_piece", None)
        if flow is not None and (
            bool(getattr(flow, "_pieces", {}))
            or bool(getattr(flow, "hasSafetyHold", lambda: False)())
        ):
            return f"Cannot {action}: the controller still owns C4 pieces or a safety hold."

        transport = getattr(coordinator, "transport", None)
        if transport is not None:
            if transport.activePieces():
                return f"Cannot {action}: transport still owns active pieces."
            if transport.getPieceForDistributionPositioning() is not None:
                return f"Cannot {action}: the distribution positioning slot is occupied."
            if transport.getPieceForDistributionDrop() is not None:
                return f"Cannot {action}: the distribution handoff is unsettled."
            if transport.hasPendingClassifications():
                return f"Cannot {action}: classifications remain pending."
    except Exception as exc:
        return f"Cannot {action}: controller ownership could not be verified ({exc})."
    return None


def quiesce_occupied_recovery_for_exit(action: str, *, timeout: float = 14.0) -> str | None:
    """Cancel and join private recovery before process teardown.

    A completion or cancellation acknowledgement is published only after its
    lifecycle transition has won under ``hardware_lifecycle_lock``. An
    unverified stop leaves the process alive so its hardware owner is not torn
    down underneath the worker.
    """
    with shared_state.hardware_lifecycle_lock:
        worker = shared_state.hardware_worker_thread
        if worker is None or not worker.is_alive():
            if shared_state.hardware_state in {"homing", "initializing"}:
                return f"Cannot {action}: hardware lifecycle status has no live worker."
            ownership_block = _runtime_ownership_exit_block(action)
            if ownership_block:
                return ownership_block
            return _checkpoint_exit_block(action)

        cancel_event = shared_state.occupied_recovery_cancel_event
        cancel_complete_event = shared_state.occupied_recovery_cancel_complete_event
        complete_event = shared_state.occupied_recovery_complete_event
        if cancel_event is None or cancel_complete_event is None or complete_event is None:
            return f"Cannot {action} while a hardware lifecycle worker is active."

        if not complete_event.is_set():
            cancel_event.set()

    worker.join(timeout=max(0.0, timeout))
    if worker.is_alive():
        return f"Cannot {action}: occupied recovery is still stopping hardware."
    if complete_event.is_set() or cancel_complete_event.is_set():
        ownership_block = _runtime_ownership_exit_block(action)
        if ownership_block:
            return ownership_block
        return _checkpoint_exit_block(action)
    return f"Cannot {action}: occupied recovery ended without verified completion or cancellation."
