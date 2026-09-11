"""Project Harvest source imports, guided validation, and audited live routing.

Source drafts remain immutable. The project layer adds frozen-BOM review,
capacity planning, simulation, an audited ledger, and an explicit paused-state
activation gate. Activation assigns destinations but never resumes motion.
"""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
from typing import Any
from uuid import UUID

import project_harvest
import project_harvest_projects
from blob_manager import getApiKeys
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from server import shared_state


router = APIRouter(prefix="/api/project-harvest", tags=["project-harvest"])


class CreateProjectRequest(BaseModel):
    draft_id: str
    name: str | None = None
    priority: int = Field(default=100, ge=0, le=10_000)
    match_policy: str = "exact"
    fallback_mode: str = "bag_plan"


class FreshCopyRequest(BaseModel):
    request_id: UUID
    expected_revision: int = Field(ge=1, strict=True)
    operator: str


class ReviewUpdateRequest(BaseModel):
    policy: dict[str, Any] | None = None
    mappings: dict[str, Any] | None = None
    priority: int | None = Field(default=None, ge=0, le=10_000)
    match_policy: str | None = None
    fallback_mode: str | None = None


class CapacityPlanRequest(BaseModel):
    bins: list[dict[str, Any]] | None = None
    assignment_overrides: list[dict[str, Any]] | None = None


class SimulationRequest(BaseModel):
    inventory: list[dict[str, Any]] | None = None


class AllocationRequest(BaseModel):
    piece_id: str
    part_id: str
    color_id: str
    quantity: int = Field(default=1, ge=1, le=1_000_000)
    mode: str = "simulation"


class ConfirmAllocationRequest(BaseModel):
    evidence: dict[str, Any]


class UndoAllocationRequest(BaseModel):
    reason: str


class AcceptanceRequest(BaseModel):
    evidence: dict[str, Any]


class StartAcceptanceRunRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    expected_bin_state_token: str
    test_id: str
    operator: str
    test_piece_limit: int = Field(default=10, ge=1, le=25)
    physical_bins_verified_empty: bool = False


class FinishAcceptanceRunRequest(BaseModel):
    acceptance_run_id: str
    operator: str
    result: str
    observed_destinations_match: bool = False
    notes: str = ""


class AbortAcceptanceRunRequest(BaseModel):
    acceptance_run_id: str
    operator: str
    reason: str


class ReopenAcceptanceRunRequest(BaseModel):
    acceptance_run_id: str
    operator: str
    reason: str


class BinClearanceRequest(BaseModel):
    bin_ids: list[str]
    expected_bin_state_token: str
    operator: str
    physical_bins_emptied: bool = False


class GreenLightRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    expected_bin_state_token: str
    operator: str
    reason: str
    physical_bins_verified_empty: bool = False


class ActivationRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    expected_bin_state_token: str
    operator: str
    reason: str


class StopActivationRequest(BaseModel):
    operator: str
    reason: str


class TransitionRequest(BaseModel):
    target: str
    reason: str


def _draft_directory() -> Path:
    gc = shared_state.gc_ref
    if gc is None:
        raise HTTPException(status_code=503, detail="Backend not ready.")
    configured = getattr(gc, "project_harvest_dir", None)
    if not isinstance(configured, str) or not configured.strip():
        raise HTTPException(
            status_code=503, detail="Project Harvest storage is not configured."
        )
    return Path(configured)


def _project_store() -> project_harvest_projects.HarvestProjectStore:
    return project_harvest_projects.HarvestProjectStore(_draft_directory())


def _project_error_status(exc: project_harvest_projects.HarvestProjectError) -> int:
    if exc.code in {
        "PROJECT_NOT_FOUND",
        "DRAFT_NOT_FOUND",
        "ALLOCATION_NOT_FOUND",
        "BOM_REVISION_MISSING",
        "REBRICKABLE_SET_NOT_FOUND",
    }:
        return 404
    if exc.code in {
        "DUPLICATE_BIN",
        "DRAFT_REVISION_MISMATCH",
        "NO_INCOMPLETE_QUOTA",
        "PROJECT_NOT_READY",
        "NOT_READY_FOR_ACCEPTANCE",
        "ALLOCATION_UNDONE",
        "EVIDENCE_DRIVEN_STATE",
        "INVALID_STATE_TRANSITION",
        "PROJECT_INCOMPLETE",
        "REBRICKABLE_API_KEY_REQUIRED",
        "REBRICKABLE_INVALID_API_KEY",
        "STALE_PROJECT_REVISION",
        "COPY_REQUEST_CONFLICT",
        "STALE_BIN_STATE",
        "BIN_CLEARANCE_REQUIRED",
        "NOT_READY_FOR_GREEN_LIGHT",
        "PHYSICAL_BIN_CONFIRMATION_REQUIRED",
        "CAPACITY_PLAN_REQUIRED",
        "NOT_READY_FOR_ACTIVATION",
        "ANOTHER_PROJECT_ACTIVE",
        "PROJECT_ACTIVE",
        "PROJECT_NOT_ACTIVE",
        "LIVE_ALLOCATION_RECOVERY_REQUIRED",
        "RUNTIME_STATE_MISMATCH",
        "LIVE_ASSIGNMENT_PLAN_MISMATCH",
        "CONTROLLED_ACCEPTANCE_REQUIRED",
        "PHYSICAL_OBSERVATION_REQUIRED",
        "ACCEPTANCE_OBSERVATION_NOT_READY",
        "ACCEPTANCE_PIECE_COUNT_MISMATCH",
        "RUNTIME_MODE_MISMATCH",
        "ACCEPTANCE_ABORT_REASON_REQUIRED",
    }:
        return 409
    if exc.code in {
        "BOM_TOO_LARGE",
        "EVENT_TOO_LARGE",
        "REBRICKABLE_RESPONSE_TOO_LARGE",
    }:
        return 413
    if exc.code == "REBRICKABLE_RATE_LIMITED":
        return 429
    if exc.code == "REBRICKABLE_TIMEOUT":
        return 504
    if exc.code in {
        "REBRICKABLE_UNAVAILABLE",
        "REBRICKABLE_HTTP_ERROR",
        "REBRICKABLE_INVALID_RESPONSE",
        "REBRICKABLE_PAGINATION_LIMIT",
        "REBRICKABLE_EMPTY_BOM",
    }:
        return 502
    return 400


def _raise_project_error(exc: project_harvest_projects.HarvestProjectError) -> None:
    detail: dict[str, Any] = {"code": exc.code, "message": str(exc)}
    if exc.details is not None:
        detail["details"] = exc.details
    raise HTTPException(status_code=_project_error_status(exc), detail=detail) from exc


async def _read_bounded_bom(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError:
            declared_size = None
        if (
            declared_size is not None
            and declared_size > project_harvest_projects.MAX_BOM_BYTES
        ):
            raise project_harvest_projects.HarvestProjectError(
                "BOM_TOO_LARGE",
                f"The BOM upload exceeds {project_harvest_projects.MAX_BOM_BYTES // 1024 // 1024} MiB.",
            )
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > project_harvest_projects.MAX_BOM_BYTES:
            raise project_harvest_projects.HarvestProjectError(
                "BOM_TOO_LARGE",
                f"The BOM upload exceeds {project_harvest_projects.MAX_BOM_BYTES // 1024 // 1024} MiB.",
            )
        content.extend(chunk)
    return bytes(content)


def _configured_capacity_bins() -> list[dict[str, Any]]:
    """Return an inert view of enabled bins and their current reservations."""

    from blob_manager import getBinCategories
    from irl.bin_layout import getBinLayout
    from local_state import get_current_bin_piece_counts

    layout = getBinLayout()
    categories = getBinCategories()
    tracked_piece_counts = get_current_bin_piece_counts()
    result: list[dict[str, Any]] = []
    for layer_index, layer in enumerate(layout.layers):
        section_enabled = layer.section_enabled or [True] * len(layer.sections)
        for section_index, section in enumerate(layer.sections):
            enabled = bool(layer.enabled) and bool(
                section_enabled[section_index]
                if section_index < len(section_enabled)
                else True
            )
            for bin_index, _size in enumerate(section):
                reserved_by: list[str] = []
                try:
                    raw_reserved = categories[layer_index][section_index][bin_index]
                    if isinstance(raw_reserved, list):
                        reserved_by = [str(value) for value in raw_reserved]
                except (TypeError, IndexError):
                    pass
                result.append(
                    {
                        "bin_id": f"L{layer_index + 1}-S{section_index + 1}-B{bin_index + 1}",
                        "layer_index": layer_index,
                        "section_index": section_index,
                        "bin_index": bin_index,
                        "available": enabled,
                        "reserved_by": reserved_by,
                        "max_pieces": layer.max_pieces_per_bin,
                        "tracked_piece_count": tracked_piece_counts.get(
                            (layer_index, section_index, bin_index), 0
                        ),
                    }
                )
    return result


def _bin_state_token(bins: list[dict[str, Any]]) -> str:
    evidence = [
        {
            "bin_id": item["bin_id"],
            "available": bool(item.get("available", True)),
            "reserved_by": sorted(str(value) for value in item.get("reserved_by", [])),
            "tracked_piece_count": int(item.get("tracked_piece_count", 0)),
        }
        for item in sorted(bins, key=lambda item: str(item["bin_id"]))
    ]
    return hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _planned_bin_ids(project: dict[str, Any]) -> set[str]:
    plan = project.get("capacity_plan")
    if not isinstance(plan, dict) or plan.get("status") != "ready":
        raise project_harvest_projects.HarvestProjectError(
            "CAPACITY_PLAN_REQUIRED",
            "Generate a ready capacity plan before checking bin readiness.",
        )
    return {
        assignment["bin_id"]
        for wave in plan.get("waves", [])
        if isinstance(wave, dict)
        for assignment in wave.get("assignments", [])
        if isinstance(assignment, dict) and isinstance(assignment.get("bin_id"), str)
    }


def _bin_readiness_payload(
    project: dict[str, Any], bins: list[dict[str, Any]]
) -> dict[str, Any]:
    planned_bin_ids = _planned_bin_ids(project)
    suggested_bins = [
        {
            "bin_id": item["bin_id"],
            **(
                {
                    "layer_index": item["layer_index"],
                    "section_index": item["section_index"],
                    "bin_index": item["bin_index"],
                }
                if all(
                    item.get(name) is not None
                    for name in ("layer_index", "section_index", "bin_index")
                )
                else {}
            ),
            "reserved_by": item["reserved_by"],
            "tracked_piece_count": item.get("tracked_piece_count", 0),
        }
        for item in bins
        if item["bin_id"] in planned_bin_ids
        and (item["reserved_by"] or item.get("tracked_piece_count", 0) > 0)
    ]
    all_bins_requiring_clearance = [
        {
            "bin_id": item["bin_id"],
            **(
                {
                    "layer_index": item["layer_index"],
                    "section_index": item["section_index"],
                    "bin_index": item["bin_index"],
                }
                if all(
                    item.get(name) is not None
                    for name in ("layer_index", "section_index", "bin_index")
                )
                else {}
            ),
            "reserved_by": item["reserved_by"],
            "tracked_piece_count": item.get("tracked_piece_count", 0),
        }
        for item in bins
        if item["reserved_by"] or item.get("tracked_piece_count", 0) > 0
    ]
    controller = shared_state.controller_ref
    controller_state = getattr(getattr(controller, "state", None), "value", None)
    return {
        "status": "clear" if not suggested_bins else "clearance_required",
        "project_id": project["project_id"],
        "planning_assumption": "enabled_bins_assumed_empty",
        "planned_bin_ids": sorted(planned_bin_ids),
        "suggested_bin_count": len(planned_bin_ids),
        "suggested_bins_requiring_clearance": suggested_bins,
        "all_bins_requiring_clearance": all_bins_requiring_clearance,
        "bin_state_token": _bin_state_token(bins),
        "recorded_state_only": True,
        "physical_verification_required": True,
        "record_clearance_allowed": controller_state == "paused",
        "live_changes_allowed": False,
    }


def _require_sorter_paused() -> None:
    controller = shared_state.controller_ref
    state = getattr(getattr(controller, "state", None), "value", None)
    if state != "paused":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SORTER_MUST_BE_PAUSED",
                "message": "Pause the sorter before physically clearing bins or recording a green light.",
            },
        )


def _acceptance_evidence_recording_allowed() -> bool:
    """Return whether acceptance evidence can be recorded without live motion.

    A normally completed controlled test leaves the controller paused. If the
    backend exits after the final confirmed drop, however, the supervisor
    deliberately restarts it in hardware standby. That state has no controller,
    no hardware worker, and no active hardware runtime, so restoring the audited
    pre-test bin categories and recording the operator's observation are safe.
    """

    controller = shared_state.controller_ref
    controller_state = getattr(getattr(controller, "state", None), "value", None)
    if controller_state == "paused":
        return True
    if controller is not None:
        return False

    with shared_state.hardware_lifecycle_lock:
        worker = shared_state.hardware_worker_thread
        worker_active = worker is not None and worker.is_alive()
        return (
            shared_state.hardware_state == "standby"
            and not worker_active
            and shared_state.hardware_runtime_irl is None
        )


def _require_acceptance_evidence_recording_allowed() -> None:
    if _acceptance_evidence_recording_allowed():
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": "SORTER_MUST_BE_STATIONARY",
            "message": (
                "Pause the sorter before recording the controlled-test inspection. "
                "Recording is also allowed when the backend is safely stopped in hardware standby."
            ),
        },
    )


def _runtime_assignments(
    project: dict[str, Any], bins: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    plan = project.get("capacity_plan") or {}
    planned = [
        assignment
        for wave in plan.get("waves", [])
        for assignment in wave.get("assignments", [])
    ]
    bins_by_id = {item["bin_id"]: item for item in bins}
    assignments: list[dict[str, Any]] = []
    for assignment in planned:
        bin_info = bins_by_id.get(assignment.get("bin_id"))
        if bin_info is None or not all(
            isinstance(bin_info.get(field), int)
            for field in ("layer_index", "section_index", "bin_index")
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "LIVE_ASSIGNMENT_BIN_MISSING",
                    "message": "A planned Harvest bin no longer exists in the live layout.",
                },
            )
        group_id = str(assignment["group_id"])
        assignments.append(
            {
                "group_id": group_id,
                "group_label": str(assignment.get("group_label") or group_id),
                "bin_id": str(assignment["bin_id"]),
                "category_id": f"harvest:{project['project_id'][-8:]}:{group_id}",
                "layer_index": int(bin_info["layer_index"]),
                "section_index": int(bin_info["section_index"]),
                "bin_index": int(bin_info["bin_index"]),
            }
        )
    if not assignments:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CAPACITY_PLAN_REQUIRED",
                "message": "Generate a ready capacity plan before starting physical routing.",
            },
        )
    return assignments


def _error_detail(exc: project_harvest.HarvestImportError) -> dict[str, str]:
    return {"code": exc.code, "message": str(exc)}


def _error_status(exc: project_harvest.HarvestImportError) -> int:
    if exc.code in {"FILE_TOO_LARGE", "LOCABRIQUES_RESPONSE_TOO_LARGE"}:
        return 413
    if exc.code in {"LOCABRIQUES_NOT_FOUND", "DRAFT_NOT_FOUND"}:
        return 404
    if exc.code == "LOCABRIQUES_AMBIGUOUS":
        return 409
    if exc.code in {"LOCABRIQUES_TIMEOUT", "LOCABRIQUES_DETAIL_TIMEOUT"}:
        return 504
    if exc.code.startswith("LOCABRIQUES_") and exc.code not in {
        "LOCABRIQUES_SET_MISMATCH",
        "LOCABRIQUES_INVALID_DETAIL_URL",
    }:
        return 502
    return 400


def _draft_response(draft: dict[str, Any]) -> dict[str, Any]:
    """Add stable API aliases without mutating the immutable stored manifest."""
    result = dict(draft)

    source = draft.get("source")
    if isinstance(source, dict):
        source_out = dict(source)
        source_out.setdefault("source_kind", source_out.get("provider"))
        result["source"] = source_out

    validation = draft.get("validation")
    if isinstance(validation, dict):
        validation_out = dict(validation)
        validation_out.setdefault(
            "activation_allowed",
            validation_out.get("activation_status") == "allowed",
        )
        result["validation"] = validation_out

    return result


def _draft_summary_response(draft: dict[str, Any]) -> dict[str, Any]:
    result = _draft_response(draft)
    result.pop("groups", None)
    return result


async def _read_bounded_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError:
            declared_size = None
        if declared_size is not None and declared_size > project_harvest.MAX_BSX_BYTES:
            raise project_harvest.HarvestImportError(
                "FILE_TOO_LARGE",
                f"The BSX upload exceeds the {project_harvest.MAX_BSX_BYTES // (1024 * 1024)} MiB limit.",
            )

    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > project_harvest.MAX_BSX_BYTES:
            raise project_harvest.HarvestImportError(
                "FILE_TOO_LARGE",
                f"The BSX upload exceeds the {project_harvest.MAX_BSX_BYTES // (1024 * 1024)} MiB limit.",
            )
        content.extend(chunk)
    return bytes(content)


@router.get("/sources/locabriques/{set_number}")
def lookup_locabriques_source(set_number: str) -> dict[str, Any]:
    try:
        result = project_harvest.lookup_locabriques(set_number)
    except project_harvest.HarvestImportError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(exc)) from exc

    # The source service returns a human-readable message for both misses and
    # outages.  Preserve it and provide the stable ``error`` alias expected by
    # API clients for the unavailable case.
    if result.get("status") == "unavailable" and result.get("message"):
        result = {**result, "error": result["message"]}
    return result


@router.get("/drafts")
def list_drafts() -> dict[str, Any]:
    try:
        drafts = project_harvest.list_bsx_drafts(_draft_directory())
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "DRAFT_LIST_FAILED",
                "message": "Could not read Project Harvest drafts.",
            },
        ) from exc
    return {"drafts": [_draft_response(draft) for draft in drafts]}


@router.get("/drafts/summaries")
def list_draft_summaries() -> dict[str, Any]:
    try:
        drafts = project_harvest.list_bsx_drafts(_draft_directory())
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "DRAFT_LIST_FAILED",
                "message": "Could not read Project Harvest drafts.",
            },
        ) from exc
    return {"drafts": [_draft_summary_response(draft) for draft in drafts]}


@router.post("/drafts/locabriques/{set_number}")
def import_locabriques_draft(set_number: str) -> dict[str, Any]:
    directory = _draft_directory()
    try:
        draft = project_harvest.save_locabriques_draft(
            directory,
            set_number=set_number,
        )
    except project_harvest.HarvestImportError as exc:
        raise HTTPException(
            status_code=_error_status(exc), detail=_error_detail(exc)
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "DRAFT_WRITE_FAILED",
                "message": "Could not store the Project Harvest draft.",
            },
        ) from exc
    return _draft_response(draft)


@router.get("/drafts/{draft_id}")
def load_draft(draft_id: str) -> dict[str, Any]:
    try:
        draft = project_harvest.load_bsx_draft(_draft_directory(), draft_id)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "DRAFT_NOT_FOUND",
                "message": "Project Harvest draft not found.",
            },
        ) from exc
    except project_harvest.HarvestImportError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(exc)) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "DRAFT_READ_FAILED",
                "message": "Could not read the Project Harvest draft.",
            },
        ) from exc
    return _draft_response(draft)


@router.post("/drafts/bsx")
async def import_bsx_draft(
    request: Request,
    set_number: str,
    filename: str | None = None,
) -> dict[str, Any]:
    directory = _draft_directory()
    try:
        content = await _read_bounded_body(request)
        draft = project_harvest.save_bsx_draft(
            directory,
            set_number=set_number,
            filename=filename,
            content=content,
        )
    except project_harvest.HarvestImportError as exc:
        raise HTTPException(
            status_code=_error_status(exc), detail=_error_detail(exc)
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "DRAFT_WRITE_FAILED",
                "message": "Could not store the Project Harvest draft.",
            },
        ) from exc
    return _draft_response(draft)


# ---------------------------------------------------------------------------
# Audited project planning. These routes never mutate sorter bin assignments,
# sorting profiles, distribution state, or hardware.
# ---------------------------------------------------------------------------


@router.get("/capacity")
def configured_capacity() -> dict[str, Any]:
    bins = _configured_capacity_bins()
    return {
        "bins": bins,
        "summary": {
            "total_bins": len(bins),
            "available_bins": sum(
                1 for item in bins if item["available"] and not item["reserved_by"]
            ),
            "reserved_bins": sum(1 for item in bins if item["reserved_by"]),
            "disabled_bins": sum(1 for item in bins if not item["available"]),
        },
        "read_only": True,
    }


@router.post("/projects")
def create_project(payload: CreateProjectRequest) -> dict[str, Any]:
    try:
        return _project_store().create_project(**payload.model_dump())
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)
    except project_harvest.HarvestImportError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(exc)) from exc


@router.get("/projects")
def list_projects() -> dict[str, Any]:
    try:
        projects = _project_store().list_projects()
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)
    return {"projects": projects}


@router.get("/projects/summaries")
def list_project_summaries() -> dict[str, Any]:
    try:
        projects = _project_store().list_project_summaries()
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)
    return {"projects": projects}


@router.get("/projects/verify")
def verify_project_store() -> dict[str, Any]:
    try:
        return _project_store().verify_all()
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)


@router.post("/portfolio/allocations")
def propose_portfolio_allocation(payload: AllocationRequest) -> dict[str, Any]:
    store = _project_store()
    errors: list[dict[str, Any]] = []
    try:
        projects = store.list_projects()
        for project in projects:
            if project["state"] in {"paused", "completed"}:
                continue
            try:
                allocation = store.propose_allocation(
                    project["project_id"], **payload.model_dump()
                )
                return {"project_id": project["project_id"], "allocation": allocation}
            except project_harvest_projects.HarvestProjectError as exc:
                if exc.code != "NO_INCOMPLETE_QUOTA":
                    raise
                errors.append({"project_id": project["project_id"], "code": exc.code})
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)
    raise HTTPException(
        status_code=409,
        detail={
            "code": "NO_PORTFOLIO_QUOTA",
            "message": "No active Harvest project accepts this part and color.",
            "projects_checked": errors,
        },
    )


@router.get("/projects/{project_id}")
def load_project(project_id: str) -> dict[str, Any]:
    try:
        return _project_store().get_project(project_id)
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)


@router.post("/projects/{project_id}/fresh-copy")
def create_fresh_copy(project_id: str, payload: FreshCopyRequest) -> dict[str, Any]:
    try:
        return _project_store().create_fresh_copy(
            project_id,
            request_id=str(payload.request_id),
            expected_revision=payload.expected_revision,
            operator=payload.operator,
        )
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)
        raise  # _raise_project_error always raises an HTTPException.
    except project_harvest.HarvestImportError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(exc)) from exc


@router.post("/projects/{project_id}/bom")
async def import_project_bom(
    project_id: str,
    request: Request,
    filename: str | None = None,
    provider: str = "private_moc",
) -> dict[str, Any]:
    try:
        content = await _read_bounded_bom(request)
        return _project_store().save_bom(
            project_id,
            content=content,
            filename=filename,
            provider=provider,
        )
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)


@router.post("/projects/{project_id}/bom/rebrickable")
def fetch_project_bom_from_rebrickable(project_id: str) -> dict[str, Any]:
    try:
        saved = getApiKeys() or {}
        api_key = str(
            saved.get("rebrickable") or os.environ.get("REBRICKABLE_API_KEY", "")
        ).strip()
        if not api_key:
            raise project_harvest_projects.HarvestProjectError(
                "REBRICKABLE_API_KEY_REQUIRED",
                "Configure a Rebrickable API key in Settings before fetching the BOM.",
            )
        store = _project_store()
        project = store.get_project(project_id, include_events=False)
        source = project_harvest_projects.fetch_rebrickable_bom(
            set_number=project["set_number"], api_key=api_key
        )
        return store.save_bom(
            project_id,
            content=source["content"],
            filename=source["filename"],
            provider=source["provider"],
        )
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)


@router.patch("/projects/{project_id}/review")
def update_project_review(
    project_id: str, payload: ReviewUpdateRequest
) -> dict[str, Any]:
    try:
        return _project_store().update_review(
            project_id, **payload.model_dump(exclude_unset=True)
        )
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)


@router.post("/projects/{project_id}/capacity-plan")
def create_capacity_plan(
    project_id: str, payload: CapacityPlanRequest
) -> dict[str, Any]:
    try:
        bins = payload.bins if payload.bins is not None else _configured_capacity_bins()
        return _project_store().plan_capacity(
            project_id,
            bins=bins,
            assignment_overrides=payload.assignment_overrides,
        )
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)


@router.get("/projects/{project_id}/bin-readiness")
def get_project_bin_readiness(project_id: str) -> dict[str, Any]:
    """Compare the draft's suggested bins with current recorded bin state.

    This is deliberately read-only. Draft execution cannot clear contents,
    remove category assignments, reserve bins, or move hardware.
    """

    try:
        project = _project_store().get_project(project_id, include_events=False)
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)
    try:
        return _bin_readiness_payload(project, _configured_capacity_bins())
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)


@router.post("/projects/{project_id}/bin-clearance")
def clear_project_bins(
    project_id: str, payload: BinClearanceRequest
) -> dict[str, Any]:
    if not payload.physical_bins_emptied:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PHYSICAL_BIN_CONFIRMATION_REQUIRED",
                "message": "Physically empty every selected bin before marking its records clear.",
            },
        )
    _require_sorter_paused()
    store = _project_store()
    try:
        project = store.get_project(project_id, include_events=False)
        bins_before = _configured_capacity_bins()
        readiness_before = _bin_readiness_payload(project, bins_before)
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)
    if payload.expected_bin_state_token != readiness_before["bin_state_token"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "STALE_BIN_STATE",
                "message": "Bin assignments or contents changed. Check the bins again before clearing records.",
            },
        )

    selected_ids = sorted({value.strip() for value in payload.bin_ids if value.strip()})
    allowed_by_id = {
        item["bin_id"]: item
        for item in readiness_before["all_bins_requiring_clearance"]
    }
    invalid = [value for value in selected_ids if value not in allowed_by_id]
    if not selected_ids or invalid:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_BIN_CLEARANCE_SELECTION",
                "message": "Select only bins currently reported as requiring clearance.",
                "invalid_bin_ids": invalid,
            },
        )
    coordinates = [
        (
            int(allowed_by_id[bin_id]["layer_index"]),
            int(allowed_by_id[bin_id]["section_index"]),
            int(allowed_by_id[bin_id]["bin_index"]),
        )
        for bin_id in selected_ids
    ]

    from server.routers.hardware import clear_selected_bins

    clearance = clear_selected_bins(selected_bins=coordinates)
    refreshed_project = store.get_project(project_id, include_events=False)
    readiness_after = _bin_readiness_payload(
        refreshed_project, _configured_capacity_bins()
    )
    try:
        updated = store.record_bin_clearance(
            project_id,
            actor=payload.operator,
            evidence={
                "bin_ids": selected_ids,
                "physical_bins_emptied": True,
                "before_bin_state_token": readiness_before["bin_state_token"],
                "after_bin_state_token": readiness_after["bin_state_token"],
                "snapshot_id": clearance.get("snapshot_id"),
                "released_assignments": clearance.get("released_assignments", 0),
                "motion_performed": False,
            },
        )
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)
    return {
        "project": updated,
        "bin_readiness": readiness_after,
        "clearance": clearance,
    }


@router.post("/projects/{project_id}/simulate")
def simulate_project(project_id: str, payload: SimulationRequest) -> dict[str, Any]:
    try:
        return _project_store().simulate(project_id, inventory=payload.inventory)
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)


@router.post("/projects/{project_id}/allocations")
def propose_project_allocation(
    project_id: str, payload: AllocationRequest
) -> dict[str, Any]:
    try:
        return _project_store().propose_allocation(project_id, **payload.model_dump())
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)


@router.post("/projects/{project_id}/allocations/{allocation_id}/confirm")
def confirm_project_allocation(
    project_id: str,
    allocation_id: str,
    payload: ConfirmAllocationRequest,
) -> dict[str, Any]:
    try:
        return _project_store().confirm_allocation(
            project_id, allocation_id, evidence=payload.evidence
        )
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)


@router.post("/projects/{project_id}/allocations/{allocation_id}/undo")
def undo_project_allocation(
    project_id: str,
    allocation_id: str,
    payload: UndoAllocationRequest,
) -> dict[str, Any]:
    try:
        return _project_store().undo_allocation(
            project_id, allocation_id, reason=payload.reason
        )
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)


@router.get("/projects/{project_id}/acceptance-pieces")
def get_project_acceptance_pieces(
    project_id: str, acceptance_run_id: str | None = None
) -> dict[str, Any]:
    """Return exact piece evidence for the active or latest controlled test."""

    import piece_records

    store = _project_store()
    try:
        project = store.get_project(project_id, include_events=False)
        evidence = store.acceptance_allocations(
            project_id, acceptance_run_id=acceptance_run_id
        )
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)

    run_id = evidence["acceptance_run_id"]
    activation = project.get("activation")
    acceptance = project.get("acceptance")
    route_source: dict[str, Any] | None = None
    if (
        isinstance(activation, dict)
        and activation.get("runtime_mode") == "acceptance"
        and activation.get("activation_id") == run_id
    ):
        route_source = activation
    elif (
        isinstance(acceptance, dict)
        and acceptance.get("acceptance_run_id") == run_id
    ):
        route_source = acceptance

    group_labels = {
        str(group.get("id")): str(group.get("label") or group.get("id"))
        for group in project.get("effective_groups", [])
        if isinstance(group, dict) and group.get("id")
    }
    bin_ids: dict[str, str] = {}
    if isinstance(route_source, dict):
        for route in [
            *(route_source.get("assignments") or []),
            *(route_source.get("observed_routes") or []),
        ]:
            if not isinstance(route, dict) or not route.get("group_id"):
                continue
            group_id = str(route["group_id"])
            if route.get("group_label"):
                group_labels[group_id] = str(route["group_label"])
            if route.get("bin_id"):
                bin_ids[group_id] = str(route["bin_id"])

    items: list[dict[str, Any]] = []
    for sequence, allocation in enumerate(evidence["allocations"], start=1):
        stored_evidence = allocation.get("evidence")
        stored_evidence = stored_evidence if isinstance(stored_evidence, dict) else {}
        raw_destination = stored_evidence.get("destination_bin")
        destination: list[int] | None = None
        if isinstance(raw_destination, (list, tuple)) and len(raw_destination) == 3:
            try:
                destination = [int(value) for value in raw_destination]
            except (TypeError, ValueError):
                destination = None
        display_bin_id = (
            f"L{destination[0] + 1}-S{destination[1] + 1}-B{destination[2] + 1}"
            if destination is not None
            else None
        )
        group_id = str(allocation["group_id"])
        summary = piece_records.getPieceSummaryByUuid(
            shared_state.gc_ref, str(allocation["piece_id"])
        )
        items.append(
            {
                "sequence": sequence,
                "allocation_id": allocation["allocation_id"],
                "piece_id": allocation["piece_id"],
                "group_id": group_id,
                "group_label": group_labels.get(group_id, group_id),
                "bin_id": bin_ids.get(group_id) or display_bin_id,
                "display_bin_id": display_bin_id,
                "destination_bin": destination,
                "confirmed_at": allocation.get("confirmed_at"),
                "exception": group_id
                == project_harvest_projects.HARVEST_EXCEPTION_GROUP_ID,
                "summary": summary
                or {
                    "uuid": allocation["piece_id"],
                    "part_id": allocation.get("part_id"),
                    "color_id": allocation.get("color_id"),
                    "has_images": False,
                },
            }
        )
    return {
        "project_id": project_id,
        "acceptance_run_id": run_id,
        "piece_count": len(items),
        "items": items,
    }


@router.post("/projects/{project_id}/acceptance")
def record_project_acceptance(
    project_id: str, payload: AcceptanceRequest
) -> dict[str, Any]:
    try:
        return _project_store().record_acceptance(project_id, evidence=payload.evidence)
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)


@router.post("/projects/{project_id}/acceptance-run/start")
def start_project_acceptance_run(
    project_id: str, payload: StartAcceptanceRunRequest
) -> dict[str, Any]:
    _require_sorter_paused()
    if not payload.physical_bins_verified_empty:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PHYSICAL_BIN_CONFIRMATION_REQUIRED",
                "message": "Physically empty the planned test bins and confirm that check before arming the controlled test.",
            },
        )
    store = _project_store()
    try:
        project = store.get_project(project_id, include_events=False)
        bins_before = _configured_capacity_bins()
        readiness = _bin_readiness_payload(project, bins_before)
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)
    if payload.expected_bin_state_token != readiness["bin_state_token"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "STALE_BIN_STATE",
                "message": "Bin assignments or contents changed. Check the test bins again before arming.",
            },
        )
    if readiness["status"] != "clear":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "BIN_CLEARANCE_REQUIRED",
                "message": "Every planned controlled-test bin must be physically clear before arming.",
                "bins": readiness["suggested_bins_requiring_clearance"],
            },
        )
    assignments = _runtime_assignments(project, bins_before)
    from server.routers.hardware import (
        apply_harvest_bin_assignments,
        restore_harvest_bin_assignments,
    )

    applied = apply_harvest_bin_assignments(assignments=assignments)
    try:
        bins_after = _configured_capacity_bins()
        return store.start_acceptance_run(
            project_id,
            expected_revision=payload.expected_revision,
            test_id=payload.test_id,
            operator=payload.operator,
            test_piece_limit=payload.test_piece_limit,
            pre_acceptance_bin_state_token=readiness["bin_state_token"],
            post_acceptance_bin_state_token=_bin_state_token(bins_after),
            assignments=applied["assignments"],
            categories_before=applied["categories_before"],
        )
    except project_harvest_projects.HarvestProjectError as exc:
        restore_harvest_bin_assignments(
            categories_before=applied["categories_before"]
        )
        _raise_project_error(exc)
    except Exception:
        restore_harvest_bin_assignments(
            categories_before=applied["categories_before"]
        )
        raise


@router.post("/projects/{project_id}/acceptance-run/finish")
def finish_project_acceptance_run(
    project_id: str, payload: FinishAcceptanceRunRequest
) -> dict[str, Any]:
    _require_acceptance_evidence_recording_allowed()
    store = _project_store()
    try:
        runtime = store.get_active_runtime()
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)
    if (
        not isinstance(runtime, dict)
        or runtime.get("project_id") != project_id
        or runtime.get("runtime_mode") != "acceptance"
        or runtime.get("activation_id") != payload.acceptance_run_id
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "RUNTIME_STATE_MISMATCH",
                "message": "The requested controlled test is not the active Harvest session.",
            },
        )
    if runtime.get("status") != "awaiting_observation":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ACCEPTANCE_OBSERVATION_NOT_READY",
                "message": "The controlled test must reach its limit and auto-pause before it can be evaluated.",
            },
        )
    runtime_progress = runtime.get("runtime_progress") or {}
    if (
        int(runtime_progress.get("planned_piece_count") or 0) != 0
        or int(runtime_progress.get("confirmed_piece_count") or 0)
        != int(runtime.get("test_piece_limit") or 0)
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ACCEPTANCE_PIECE_COUNT_MISMATCH",
                "message": "Controlled-test routing evidence is incomplete. Keep the sorter paused.",
            },
        )
    if payload.result not in {"passed", "failed"}:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_ACCEPTANCE",
                "message": "Acceptance result must be passed or failed.",
            },
        )
    if payload.result == "passed" and not payload.observed_destinations_match:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PHYSICAL_OBSERVATION_REQUIRED",
                "message": "Inspect the bins and confirm that every test piece landed in the displayed destination.",
            },
        )
    from server.routers.hardware import restore_harvest_bin_assignments

    restore_harvest_bin_assignments(
        categories_before=runtime.get("categories_before") or []
    )
    try:
        return store.finish_acceptance_run(
            project_id,
            acceptance_run_id=payload.acceptance_run_id,
            operator=payload.operator,
            result=payload.result,
            observed_destinations_match=payload.observed_destinations_match,
            notes=payload.notes,
            post_restore_bin_state_token=_bin_state_token(_configured_capacity_bins()),
        )
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)


@router.post("/projects/{project_id}/acceptance-run/reopen-after-correction")
def reopen_project_acceptance_run_after_correction(
    project_id: str, payload: ReopenAcceptanceRunRequest
) -> dict[str, Any]:
    _require_sorter_paused()
    try:
        return _project_store().reopen_acceptance_after_correction(
            project_id,
            acceptance_run_id=payload.acceptance_run_id,
            operator=payload.operator,
            reason=payload.reason,
        )
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)


@router.post("/projects/{project_id}/acceptance-run/abort")
def abort_project_acceptance_run(
    project_id: str, payload: AbortAcceptanceRunRequest
) -> dict[str, Any]:
    _require_sorter_paused()
    store = _project_store()
    try:
        runtime = store.get_active_runtime()
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)
    if (
        not isinstance(runtime, dict)
        or runtime.get("project_id") != project_id
        or runtime.get("runtime_mode") != "acceptance"
        or runtime.get("activation_id") != payload.acceptance_run_id
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "RUNTIME_STATE_MISMATCH",
                "message": "The requested controlled test is not the active Harvest session.",
            },
        )
    if int((runtime.get("runtime_progress") or {}).get("planned_piece_count") or 0) != 0:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "LIVE_ALLOCATION_RECOVERY_REQUIRED",
                "message": "Resolve the in-flight controlled-test allocation before stopping it.",
            },
        )
    if not payload.reason.strip():
        raise HTTPException(
            status_code=400,
            detail={
                "code": "ACCEPTANCE_ABORT_REASON_REQUIRED",
                "message": "A reason is required to stop the controlled test.",
            },
        )
    from server.routers.hardware import restore_harvest_bin_assignments

    restore_harvest_bin_assignments(
        categories_before=runtime.get("categories_before") or []
    )
    try:
        return store.abort_acceptance_run(
            project_id,
            acceptance_run_id=payload.acceptance_run_id,
            operator=payload.operator,
            reason=payload.reason,
            post_restore_bin_state_token=_bin_state_token(_configured_capacity_bins()),
        )
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)


@router.post("/projects/{project_id}/green-light")
def record_project_green_light(
    project_id: str, payload: GreenLightRequest
) -> dict[str, Any]:
    _require_sorter_paused()
    store = _project_store()
    try:
        project = store.get_project(project_id, include_events=False)
        readiness = _bin_readiness_payload(project, _configured_capacity_bins())
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)
    if payload.expected_bin_state_token != readiness["bin_state_token"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "STALE_BIN_STATE",
                "message": "Bin assignments or contents changed. Check the bins again before green-lighting.",
            },
        )
    if readiness["status"] != "clear":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "BIN_CLEARANCE_REQUIRED",
                "message": "Every planned bin must be physically verified and recorded clear before green-lighting.",
                "bins": readiness["suggested_bins_requiring_clearance"],
            },
        )
    try:
        return store.record_green_light(
            project_id,
            expected_revision=payload.expected_revision,
            operator=payload.operator,
            reason=payload.reason,
            physical_bins_verified_empty=payload.physical_bins_verified_empty,
            bin_state_token=readiness["bin_state_token"],
            bin_ids=readiness["planned_bin_ids"],
        )
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)


@router.get("/runtime")
def get_harvest_runtime() -> dict[str, Any]:
    try:
        active = _project_store().get_active_runtime()
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)
    controller = shared_state.controller_ref
    controller_state = getattr(getattr(controller, "state", None), "value", None)
    return {
        "active": active,
        "sorter_state": controller_state,
        "acceptance_evidence_recording_allowed": _acceptance_evidence_recording_allowed(),
        "activation_requires_paused_sorter": True,
        "activation_starts_motion": False,
    }


@router.post("/projects/{project_id}/activate")
def activate_project(
    project_id: str, payload: ActivationRequest
) -> dict[str, Any]:
    _require_sorter_paused()
    store = _project_store()
    try:
        project = store.get_project(project_id, include_events=False)
        bins_before = _configured_capacity_bins()
        readiness = _bin_readiness_payload(project, bins_before)
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)
    if payload.expected_bin_state_token != readiness["bin_state_token"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "STALE_BIN_STATE",
                "message": "Bin assignments or contents changed after the green light.",
            },
        )
    if readiness["status"] != "clear":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "BIN_CLEARANCE_REQUIRED",
                "message": "Every planned bin must still be physically clear before activation.",
                "bins": readiness["suggested_bins_requiring_clearance"],
            },
        )
    plan = project.get("capacity_plan") or {}
    planned = [
        assignment
        for wave in plan.get("waves", [])
        for assignment in wave.get("assignments", [])
    ]
    bins_by_id = {item["bin_id"]: item for item in bins_before}
    assignments: list[dict[str, Any]] = []
    for assignment in planned:
        bin_info = bins_by_id.get(assignment.get("bin_id"))
        if bin_info is None or not all(
            isinstance(bin_info.get(field), int)
            for field in ("layer_index", "section_index", "bin_index")
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "LIVE_ASSIGNMENT_BIN_MISSING",
                    "message": "A planned Harvest bin no longer exists in the live layout.",
                },
            )
        group_id = str(assignment["group_id"])
        assignments.append(
            {
                "group_id": group_id,
                "group_label": str(assignment.get("group_label") or group_id),
                "bin_id": str(assignment["bin_id"]),
                "category_id": f"harvest:{project_id[-8:]}:{group_id}",
                "layer_index": int(bin_info["layer_index"]),
                "section_index": int(bin_info["section_index"]),
                "bin_index": int(bin_info["bin_index"]),
            }
        )

    from server.routers.hardware import (
        apply_harvest_bin_assignments,
        restore_harvest_bin_assignments,
    )

    applied = apply_harvest_bin_assignments(assignments=assignments)
    try:
        bins_after = _configured_capacity_bins()
        activated = store.activate_project(
            project_id,
            expected_revision=payload.expected_revision,
            operator=payload.operator,
            reason=payload.reason,
            pre_activation_bin_state_token=readiness["bin_state_token"],
            post_activation_bin_state_token=_bin_state_token(bins_after),
            assignments=applied["assignments"],
            categories_before=applied["categories_before"],
        )
    except project_harvest_projects.HarvestProjectError as exc:
        restore_harvest_bin_assignments(
            categories_before=applied["categories_before"]
        )
        _raise_project_error(exc)
    except Exception:
        restore_harvest_bin_assignments(
            categories_before=applied["categories_before"]
        )
        raise
    return activated


@router.post("/projects/{project_id}/deactivate")
def deactivate_project(
    project_id: str, payload: StopActivationRequest
) -> dict[str, Any]:
    _require_sorter_paused()
    try:
        return _project_store().stop_activation(
            project_id, operator=payload.operator, reason=payload.reason
        )
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)


@router.post("/projects/{project_id}/transition")
def transition_project(project_id: str, payload: TransitionRequest) -> dict[str, Any]:
    try:
        return _project_store().transition(
            project_id, target=payload.target, reason=payload.reason
        )
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)


@router.get("/projects/{project_id}/missing-parts.csv")
def export_missing_parts(project_id: str) -> Response:
    try:
        project = _project_store().get_project(project_id, include_events=False)
        content = project_harvest_projects.missing_parts_csv(project)
    except project_harvest_projects.HarvestProjectError as exc:
        _raise_project_error(exc)
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="harvest-{project["set_number"]}-missing-parts.csv"'
        },
    )
