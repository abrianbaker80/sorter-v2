"""Shared interlocks for manual motion while occupied C4 ownership is pending."""

from __future__ import annotations

import inspect
from functools import wraps
from typing import Any, Callable

from fastapi import HTTPException

from server import shared_state
from subsystems.classification_channel.occupied_checkpoint import pending_checkpoint


def _ensure_occupied_checkpoint_clear(action: str) -> None:
    try:
        checkpoint = pending_checkpoint()
    except Exception as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot {action}: the occupied C4 checkpoint is unreadable; "
                "inspect and reconcile it before moving hardware."
            ),
        ) from exc

    if checkpoint is not None:
        phase = str(checkpoint.get("phase") or "unresolved")
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot {action}: the occupied C4 checkpoint is {phase}; "
                "reconcile it before moving hardware."
            ),
        )


def occupied_checkpoint_motion_guard(
    action: str,
    *,
    when: Callable[[dict[str, Any]], bool] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Serialize a motion request against occupied-checkpoint lifecycle work.

    ``when`` can exempt non-motion modes on a shared endpoint, such as a C4
    sector-move plan with ``execute=False``. The lifecycle lock stays held
    through dispatch so checkpoint preparation cannot race between this guard
    and the hardware command.
    """

    def decorate(endpoint: Callable[..., Any]) -> Callable[..., Any]:
        signature = inspect.signature(endpoint)

        @wraps(endpoint)
        def guarded(*args: Any, **kwargs: Any) -> Any:
            bound = signature.bind_partial(*args, **kwargs)
            arguments = dict(bound.arguments)
            if when is not None and not when(arguments):
                return endpoint(*args, **kwargs)
            admission_lock = shared_state.occupied_checkpoint_transition_lock
            if not admission_lock.acquire(blocking=False):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Cannot {action}: occupied C4 checkpoint preparation "
                        "is in progress."
                    ),
                )
            try:
                with shared_state.hardware_lifecycle_lock:
                    _ensure_occupied_checkpoint_clear(action)
                    return endpoint(*args, **kwargs)
            finally:
                admission_lock.release()

        return guarded

    return decorate


def recovery_related_stepper(arguments: dict[str, Any]) -> bool:
    """Whether a manual stepper operation can disturb occupied recovery."""

    return arguments.get("stepper") in {
        "c_channel_2",
        "c_channel_3",
        "c_channel_4",
        "carousel",
        "chute",
    }
