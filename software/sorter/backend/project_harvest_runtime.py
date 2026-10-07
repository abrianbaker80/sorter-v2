"""Narrow runtime bridge between the distribution pipeline and Harvest storage."""

from __future__ import annotations

import copy
from typing import Any
from functools import lru_cache

from project_harvest_projects import (
    HARVEST_EXCEPTION_GROUP_ID,
    HARVEST_EXCEPTION_GROUP_LABEL,
    HARVEST_LAYER_SIZE_REJECT_REASON,
    HarvestProjectError,
    HarvestProjectStore,
)


_CONFIRMATION_EVIDENCE_ERRORS = frozenset({
    "HARVEST_DESTINATION_MISSING",
    "ALLOCATION_NOT_FOUND",
    "ALLOCATION_UNDONE",
    "PROJECT_NOT_FOUND",
    "PROJECT_NOT_ACTIVE",
    "RUNTIME_STATE_MISMATCH",
    "PHYSICAL_EVIDENCE_REQUIRED",
    "HARVEST_CONFIRMATION_IDENTITY_MISMATCH",
    "INVALID_ALLOCATION_ID",
    "INVALID_PROJECT_ID",
})


def is_confirmation_evidence_failure(exc: Exception) -> bool:
    """Only explicit identity/evidence refusals permit automatic recovery."""
    return isinstance(exc, HarvestProjectError) and exc.code in _CONFIRMATION_EVIDENCE_ERRORS


@lru_cache(maxsize=4)
def _store_for_root(root: str) -> HarvestProjectStore:
    return HarvestProjectStore(root)


def _store(gc: Any) -> HarvestProjectStore:
    root = getattr(gc, "project_harvest_dir", None)
    if not isinstance(root, str) or not root.strip():
        raise HarvestProjectError(
            "HARVEST_STORAGE_UNAVAILABLE",
            "Project Harvest storage is not configured. Keep the sorter paused.",
        )
    return _store_for_root(root.strip())


def active_runtime(gc: Any) -> dict[str, Any] | None:
    return _store(gc).get_active_runtime()


def rehydrate_active_bin_assignments(gc: Any, layout: Any) -> dict[str, Any]:
    """Restore the exact active Harvest routing map after runtime recovery.

    The active runtime is authoritative, but an unrelated operator edit must
    never be overwritten.  Recovery is therefore allowed only when the live
    layout is already the activated map or is the exact pre-activation snapshot
    captured by the audited runtime.  Any third state fails closed.
    """

    from blob_manager import setBinCategories
    from irl.bin_layout import applyCategories, extractCategories, layoutMatchesCategories

    runtime = active_runtime(gc)
    if not isinstance(runtime, dict):
        return {"status": "inactive", "changed": False}
    if runtime.get("status") not in {"active", "awaiting_observation"}:
        raise HarvestProjectError(
            "RUNTIME_STATE_MISMATCH",
            "The active Harvest runtime has an invalid status. Keep the sorter paused.",
        )

    categories_before = runtime.get("categories_before")
    assignments = runtime.get("assignments")
    if (
        not isinstance(categories_before, list)
        or not layoutMatchesCategories(layout, categories_before)
        or not isinstance(assignments, list)
        or not assignments
    ):
        raise HarvestProjectError(
            "ACTIVE_BIN_ASSIGNMENT_RECOVERY_INVALID",
            "The active Harvest runtime cannot reconstruct its audited bin assignment. Keep the sorter paused.",
        )

    expected = copy.deepcopy(categories_before)
    normalized: list[tuple[int, int, int, str]] = []
    coordinates_seen: set[tuple[int, int, int]] = set()
    categories_seen: set[str] = set()
    for index, assignment in enumerate(assignments, start=1):
        if not isinstance(assignment, dict):
            raise HarvestProjectError(
                "ACTIVE_BIN_ASSIGNMENT_RECOVERY_INVALID",
                f"Active Harvest assignment {index} is invalid. Keep the sorter paused.",
            )
        try:
            coordinates = (
                int(assignment["layer_index"]),
                int(assignment["section_index"]),
                int(assignment["bin_index"]),
            )
            category_id = str(assignment["category_id"]).strip()
            existing_bin = expected[coordinates[0]][coordinates[1]][coordinates[2]]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise HarvestProjectError(
                "ACTIVE_BIN_ASSIGNMENT_RECOVERY_INVALID",
                f"Active Harvest assignment {index} does not match the live layout. Keep the sorter paused.",
            ) from exc
        if (
            any(value < 0 for value in coordinates)
            or not isinstance(existing_bin, list)
            or not category_id
            or coordinates in coordinates_seen
            or category_id in categories_seen
        ):
            raise HarvestProjectError(
                "ACTIVE_BIN_ASSIGNMENT_RECOVERY_INVALID",
                f"Active Harvest assignment {index} is not unique and valid. Keep the sorter paused.",
            )
        coordinates_seen.add(coordinates)
        categories_seen.add(category_id)
        normalized.append((*coordinates, category_id))

    for layer in expected:
        for section in layer:
            for bin_categories in section:
                bin_categories[:] = [
                    value for value in bin_categories if value not in categories_seen
                ]
    for layer_index, section_index, bin_index, category_id in normalized:
        expected[layer_index][section_index][bin_index] = [category_id]

    current = extractCategories(layout)

    def _semantic(categories: list[list[list[list[str]]]]) -> list[list[list[list[str]]]]:
        return [
            [[sorted(bin_categories) for bin_categories in section] for section in layer]
            for layer in categories
        ]

    if _semantic(current) == _semantic(expected):
        setBinCategories(copy.deepcopy(expected))
        return {"status": "already_applied", "changed": False}
    if _semantic(current) != _semantic(categories_before):
        raise HarvestProjectError(
            "ACTIVE_BIN_ASSIGNMENT_MISMATCH",
            "The bin assignment differs from both the audited pre-activation and active Harvest maps. Keep the sorter paused for review.",
        )

    applyCategories(layout, expected)
    setBinCategories(copy.deepcopy(expected))
    return {
        "status": "rehydrated",
        "changed": True,
        "activation_id": runtime.get("activation_id"),
        "assignment_count": len(normalized),
    }


def reserve_piece(gc: Any, piece: Any) -> dict[str, Any] | None:
    root = getattr(gc, "project_harvest_dir", None)
    if not isinstance(root, str) or not root.strip():
        return None
    store = _store(gc)
    if not store.has_active_runtime():
        return None
    if bool(getattr(piece, "too_big", False)):
        raise HarvestProjectError(
            "HARVEST_OVERSIZE_REQUIRES_MANUAL_HANDLING",
            "An oversized piece cannot safely enter an activated bag bin. Remove it manually.",
        )
    forced_reject = bool(getattr(piece, "forced_reject_reason", None))
    allocation = store.propose_live_allocation(
        piece_id=str(piece.uuid),
        part_id=str(piece.part_id) if piece.part_id is not None and not forced_reject else None,
        color_id=str(piece.color_id) if piece.part_id is not None and not forced_reject else None,
        item_candidates=[] if forced_reject else list(
            getattr(piece, "classification_item_candidates", None) or []
        ),
        color_candidates=[] if forced_reject else list(
            getattr(piece, "classification_color_candidates", None) or []
        ),
        classification_attempts=[] if forced_reject else [
            {
                "part_id": getattr(attempt, "part_id", None),
                "part_name": getattr(attempt, "part_name", None),
                "item_score": getattr(attempt, "confidence", None),
                "color_id": getattr(attempt, "color_id", None),
                "color_name": getattr(attempt, "color_name", None),
                "color_score": getattr(attempt, "color_confidence", None),
                "strategy": str(getattr(attempt, "strategy", "")),
                "applied": bool(getattr(attempt, "applied", False)),
            }
            for attempt in (getattr(piece, "classification_attempts", None) or [])
        ],
    )
    stats = getattr(gc, "runtime_stats", None)
    if stats is not None and hasattr(stats, "observeHarvestReservation"):
        stats.observeHarvestReservation(allocation)
    return allocation


def confirm_piece_drop(gc: Any, piece: Any) -> dict[str, Any] | None:
    allocation_id = getattr(piece, "harvest_allocation_id", None)
    project_id = getattr(piece, "harvest_project_id", None)
    activation_id = getattr(piece, "harvest_activation_id", None)
    destination = getattr(piece, "destination_bin", None)
    marker_owned = getattr(piece, "c4_marker_exit_boundary", None) is not None
    if not allocation_id and not project_id:
        if (marker_owned and not bool(getattr(piece, "c4_discard", False))
            and any(getattr(piece, name, None) for name in (
                "harvest_activation_id", "harvest_group_id", "harvest_group_label", "harvest_exception",
            ))):
            raise HarvestProjectError(
                "HARVEST_CONFIRMATION_IDENTITY_MISMATCH",
                "The discharged Harvest piece has lost its original allocation identity.",
            )
        return None
    confirmation_scope = {}
    if marker_owned:
        if not all(isinstance(value, str) and value.strip() for value in (
            allocation_id, project_id, activation_id, getattr(piece, "uuid", None),
        )):
            raise HarvestProjectError(
                "HARVEST_CONFIRMATION_IDENTITY_MISMATCH",
                "The discharged piece lacks its original Harvest confirmation identity.",
            )
        confirmation_scope = dict(expected_piece_id=piece.uuid, expected_activation_id=activation_id)
    elif not allocation_id or not project_id:
        return None
    if not isinstance(destination, tuple) or len(destination) != 3:
        if bool(getattr(piece, "harvest_exception", False)) and bool(
            getattr(piece, "too_big_for_layer", False)
        ):
            result = _store(gc).confirm_allocation(
                str(project_id),
                str(allocation_id),
                evidence={
                    "physical_drop_confirmed": True,
                    "activation_id": str(activation_id),
                    "destination_bin": "bottom_reject",
                    "bottom_reject": True,
                    "reject_reason": HARVEST_LAYER_SIZE_REJECT_REASON,
                    "reserved_group_id": getattr(piece, "harvest_group_id", None),
                    "piece_id": str(piece.uuid),
                    "piece_max_dimension_mm": getattr(piece, "max_dimension_mm", None),
                    "intended_layer_index": getattr(piece, "intended_layer_index", None),
                },
                **confirmation_scope,
            )
            piece.harvest_group_id = HARVEST_EXCEPTION_GROUP_ID
            piece.harvest_group_label = HARVEST_EXCEPTION_GROUP_LABEL
            piece.harvest_exception = True
            return result
        raise HarvestProjectError(
            "HARVEST_DESTINATION_MISSING",
            "The physically dropped Harvest piece has no recorded destination.",
        )
    if marker_owned:
        try:
            destination_values = [int(value) for value in destination]
        except (TypeError, ValueError, OverflowError) as exc:
            raise HarvestProjectError(
                "HARVEST_DESTINATION_MISSING", "The discharged piece has invalid destination evidence.",
            ) from exc
    else:
        destination_values = [int(value) for value in destination]
    return _store(gc).confirm_allocation(
        str(project_id),
        str(allocation_id),
        evidence={
            "physical_drop_confirmed": True,
            "activation_id": str(activation_id),
            "destination_bin": destination_values,
            "piece_id": str(piece.uuid),
        },
        **confirmation_scope,
    )


def retire_c4_planned_allocations(gc, *, piece_ids=None):
    root = getattr(gc, "project_harvest_dir", None)
    if not isinstance(root, str) or not root.strip():
        return 0
    return _store(gc).retire_c4_planned_allocations(piece_ids=piece_ids)


def retire_unconfirmed_piece_drop(gc: Any, piece: Any, *, reason: str) -> int:
    """Retire only this piece's original, unconfirmed Harvest reservation.

    Missing identity cannot authorize retiring another activation's allocation.
    A zero result preserves absent, completed, or independently owned records.
    """
    identity = (
        getattr(piece, "harvest_project_id", None),
        getattr(piece, "harvest_allocation_id", None),
        getattr(piece, "harvest_activation_id", None),
        getattr(piece, "uuid", None),
    )
    if not all(isinstance(value, str) and value.strip() for value in identity):
        return 0
    project_id, allocation_id, activation_id, piece_id = identity
    return _store(gc).retire_c4_planned_allocations(
        piece_ids=[piece_id], project_id=project_id,
        allocation_id=allocation_id, activation_id=activation_id, reason=reason,
    )
