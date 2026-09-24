from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.routers.project_harvest as project_harvest_router
import project_harvest_projects
import piece_records
from server import shared_state
from server.routers import hardware
from server.routers.project_harvest import router


SAMPLE_BSX = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE BrickStoreXML>
<BrickStoreXML><Inventory>
<Item><ItemID>3001</ItemID><ItemTypeID>P</ItemTypeID><ColorID>2</ColorID><Qty>1</Qty><Condition>N</Condition><Remarks>Bag 1</Remarks></Item>
<Item><ItemID>3001</ItemID><ItemTypeID>P</ItemTypeID><ColorID>2</ColorID><Qty>2</Qty><Condition>N</Condition><Remarks>Bag 2</Remarks></Item>
</Inventory></BrickStoreXML>"""


def test_acceptance_evidence_recording_rejects_running_controller(monkeypatch) -> None:
    monkeypatch.setattr(
        shared_state,
        "controller_ref",
        SimpleNamespace(state=SimpleNamespace(value="running")),
    )
    monkeypatch.setattr(shared_state, "hardware_state", "standby")
    monkeypatch.setattr(shared_state, "hardware_worker_thread", None)
    monkeypatch.setattr(shared_state, "hardware_runtime_irl", None)

    assert project_harvest_router._acceptance_evidence_recording_allowed() is False


def test_acceptance_evidence_recording_rejects_hardware_worker(monkeypatch) -> None:
    monkeypatch.setattr(shared_state, "controller_ref", None)
    monkeypatch.setattr(shared_state, "hardware_state", "standby")
    monkeypatch.setattr(
        shared_state,
        "hardware_worker_thread",
        SimpleNamespace(is_alive=lambda: True),
    )
    monkeypatch.setattr(shared_state, "hardware_runtime_irl", None)

    assert project_harvest_router._acceptance_evidence_recording_allowed() is False


def _client(monkeypatch, tmp_path: Path) -> TestClient:
    monkeypatch.setattr(
        shared_state,
        "gc_ref",
        SimpleNamespace(project_harvest_dir=str(tmp_path / "harvest")),
    )
    monkeypatch.setattr(
        shared_state,
        "controller_ref",
        SimpleNamespace(state=SimpleNamespace(value="paused")),
    )
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _create_project(client: TestClient, *, priority: int = 100) -> dict:
    draft_response = client.post(
        "/api/project-harvest/drafts/bsx",
        params={"set_number": "21369", "filename": "21369.bsx"},
        content=SAMPLE_BSX,
    )
    assert draft_response.status_code == 200
    response = client.post(
        "/api/project-harvest/projects",
        json={"draft_id": draft_response.json()["draft_id"], "priority": priority},
    )
    assert response.status_code == 200
    return response.json()


def _complete_controlled_acceptance(
    client: TestClient,
    monkeypatch,
    tmp_path: Path,
    project: dict,
    configured_bins: list[dict],
    *,
    standby_before_finish: bool = False,
) -> dict:
    monkeypatch.setattr(
        project_harvest_router, "_configured_capacity_bins", lambda: configured_bins
    )

    def fake_apply(*, assignments: list[dict]) -> dict:
        return {
            "assignments": assignments,
            "categories_before": [[[[ ] for _ in range(len(configured_bins))]]],
            "categories_after": [],
        }

    monkeypatch.setattr(hardware, "apply_harvest_bin_assignments", fake_apply)
    monkeypatch.setattr(
        hardware,
        "restore_harvest_bin_assignments",
        lambda *, categories_before: None,
    )
    readiness = client.get(
        f"/api/project-harvest/projects/{project['project_id']}/bin-readiness"
    ).json()
    started_response = client.post(
        f"/api/project-harvest/projects/{project['project_id']}/acceptance-run/start",
        json={
            "expected_revision": project["revision"],
            "expected_bin_state_token": readiness["bin_state_token"],
            "test_id": "controlled-test",
            "operator": "owner",
            "test_piece_limit": 1,
            "physical_bins_verified_empty": True,
        },
    )
    assert started_response.status_code == 200, started_response.text
    started = started_response.json()
    runtime = client.get("/api/project-harvest/runtime").json()["active"]
    assert runtime["runtime_mode"] == "acceptance"
    assert runtime["status"] == "active"
    store = project_harvest_projects.HarvestProjectStore(tmp_path / "harvest")
    allocation = store.propose_live_allocation(
        piece_id=f"acceptance-{project['project_id']}", part_id="3001", color_id="2"
    )
    confirmed = store.confirm_allocation(
        project["project_id"],
        allocation["allocation_id"],
        evidence={
            "physical_drop_confirmed": True,
            "activation_id": allocation["activation_id"],
            "destination_bin": [
                allocation["destination"]["layer_index"],
                allocation["destination"]["section_index"],
                allocation["destination"]["bin_index"],
            ],
        },
    )
    assert confirmed["pause_required"] is True
    runtime = client.get("/api/project-harvest/runtime").json()["active"]
    assert runtime["status"] == "awaiting_observation"
    if standby_before_finish:
        monkeypatch.setattr(shared_state, "controller_ref", None)
        monkeypatch.setattr(shared_state, "hardware_state", "standby")
        monkeypatch.setattr(shared_state, "hardware_worker_thread", None)
        monkeypatch.setattr(shared_state, "hardware_runtime_irl", None)
        runtime_status = client.get("/api/project-harvest/runtime").json()
        assert runtime_status["sorter_state"] is None
        assert runtime_status["acceptance_evidence_recording_allowed"] is True
    finished = client.post(
        f"/api/project-harvest/projects/{project['project_id']}/acceptance-run/finish",
        json={
            "acceptance_run_id": runtime["activation_id"],
            "operator": "owner",
            "result": "passed",
            "observed_destinations_match": True,
            "notes": "Inspected test bins.",
        },
    )
    assert finished.status_code == 200, finished.text
    if standby_before_finish:
        monkeypatch.setattr(
            shared_state,
            "controller_ref",
            SimpleNamespace(state=SimpleNamespace(value="paused")),
        )
    return finished.json()


def test_project_routes_cover_bom_plan_simulation_acceptance_and_export(
    monkeypatch, tmp_path: Path
) -> None:
    with _client(monkeypatch, tmp_path) as client:
        project = _create_project(client)
        project_id = project["project_id"]

        bom = json.dumps(
            {
                "set_number": "21369",
                "namespace": {
                    "part": "bricklink_item_number",
                    "color": "bricklink_color_id",
                },
                "items": [{"part_id": "3001", "color_id": "2", "quantity": 3}],
            }
        ).encode()
        response = client.post(
            f"/api/project-harvest/projects/{project_id}/bom",
            params={"filename": "bom.json", "provider": "private_moc"},
            content=bom,
        )
        assert response.status_code == 200, response.text
        assert response.json()["reconciliation"]["status"] == "validated"

        response = client.post(
            f"/api/project-harvest/projects/{project_id}/capacity-plan",
            json={
                "bins": [
                    {
                        "bin_id": "bin-1",
                        "available": True,
                        "reserved_by": ["existing-category"],
                    },
                    {"bin_id": "bin-2", "available": True},
                    {"bin_id": "bin-3", "available": True},
                ],
                "assignment_overrides": [
                    {"group_id": "bag:1", "bin_id": "bin-2"},
                    {"group_id": "bag:2", "bin_id": "bin-1"},
                    {"group_id": "harvest-exception", "bin_id": "bin-3"},
                ],
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["capacity_plan"]["status"] == "ready"
        assert response.json()["capacity_plan"]["summary"]["reserved_bins_excluded"] == 0

        monkeypatch.setattr(
            project_harvest_router,
            "_configured_capacity_bins",
            lambda: [
                {
                    "bin_id": "bin-1",
                    "available": True,
                    "reserved_by": ["existing-category"],
                    "tracked_piece_count": 2,
                },
                {
                    "bin_id": "bin-2",
                    "available": True,
                    "reserved_by": [],
                    "tracked_piece_count": 0,
                },
                {
                    "bin_id": "bin-3",
                    "available": True,
                    "reserved_by": [],
                    "tracked_piece_count": 0,
                },
            ],
        )
        readiness = client.get(
            f"/api/project-harvest/projects/{project_id}/bin-readiness"
        )
        assert readiness.status_code == 200, readiness.text
        assert readiness.json()["status"] == "clearance_required"
        assert readiness.json()["suggested_bins_requiring_clearance"] == [
            {
                "bin_id": "bin-1",
                "reserved_by": ["existing-category"],
                "tracked_piece_count": 2,
            }
        ]
        assert readiness.json()["live_changes_allowed"] is False

        response = client.post(
            f"/api/project-harvest/projects/{project_id}/simulate", json={}
        )
        assert response.status_code == 200
        assert response.json()["simulation"]["status"] == "passed"

        legacy_acceptance = client.post(
            f"/api/project-harvest/projects/{project_id}/acceptance",
            json={
                "evidence": {
                    "test_id": "controlled-test",
                    "operator": "operator",
                    "controlled_piece_count": 3,
                    "result": "passed",
                }
            },
        )
        assert legacy_acceptance.status_code == 409
        assert legacy_acceptance.json()["detail"]["code"] == "CONTROLLED_ACCEPTANCE_REQUIRED"

        clear_bins = [
            {
                "bin_id": "bin-1",
                "layer_index": 0,
                "section_index": 0,
                "bin_index": 0,
                "available": True,
                "reserved_by": [],
                "tracked_piece_count": 0,
            },
            {
                "bin_id": "bin-2",
                "layer_index": 0,
                "section_index": 0,
                "bin_index": 1,
                "available": True,
                "reserved_by": [],
                "tracked_piece_count": 0,
            },
            {
                "bin_id": "bin-3",
                "layer_index": 0,
                "section_index": 0,
                "bin_index": 2,
                "available": True,
                "reserved_by": [],
                "tracked_piece_count": 0,
            },
        ]
        accepted = _complete_controlled_acceptance(
            client,
            monkeypatch,
            tmp_path,
            response.json(),
            clear_bins,
            standby_before_finish=True,
        )
        assert accepted["readiness"]["ready_for_green_light"] is True
        assert accepted["readiness"]["activation_eligible"] is False
        assert accepted["readiness"]["hardware_activation_allowed"] is False

        monkeypatch.setattr(
            piece_records,
            "getPieceSummaryByUuid",
            lambda _gc, uuid_val: {
                "uuid": uuid_val,
                "part_id": "3001",
                "part_name": "Brick 2 x 4",
                "color_id": "2",
                "color_name": "Tan",
                "confidence": 0.93,
                "bin": {"x": 0, "y": 0, "z": 1},
                "has_images": True,
            },
        )
        acceptance_pieces = client.get(
            f"/api/project-harvest/projects/{project_id}/acceptance-pieces"
        )
        assert acceptance_pieces.status_code == 200, acceptance_pieces.text
        acceptance_payload = acceptance_pieces.json()
        assert acceptance_payload["acceptance_run_id"] == accepted["acceptance"][
            "acceptance_run_id"
        ]
        assert acceptance_payload["piece_count"] == 1
        assert acceptance_payload["items"][0]["piece_id"] == f"acceptance-{project_id}"
        assert acceptance_payload["items"][0]["group_label"] == "Bag 1"
        assert acceptance_payload["items"][0]["bin_id"] == "bin-2"
        assert acceptance_payload["items"][0]["display_bin_id"] == "L1-S1-B2"
        assert acceptance_payload["items"][0]["summary"]["part_name"] == "Brick 2 x 4"

        clear_readiness = client.get(
            f"/api/project-harvest/projects/{project_id}/bin-readiness"
        ).json()
        green_light = client.post(
            f"/api/project-harvest/projects/{project_id}/green-light",
            json={
                "expected_revision": accepted["revision"],
                "expected_bin_state_token": clear_readiness["bin_state_token"],
                "operator": "operator",
                "reason": "controlled deployment approval",
                "physical_bins_verified_empty": True,
            },
        )
        assert green_light.status_code == 200, green_light.text
        assert green_light.json()["state"] == "approved"
        assert green_light.json()["readiness"]["activation_eligible"] is True
        assert green_light.json()["readiness"]["hardware_activation_allowed"] is True

        missing = client.get(
            f"/api/project-harvest/projects/{project_id}/missing-parts.csv"
        )
        assert missing.status_code == 200
        assert missing.headers["content-type"].startswith("text/csv")
        assert "bag:1" in missing.text

        verified = client.get("/api/project-harvest/projects/verify")
        assert verified.status_code == 200
        assert verified.json()["verified_projects"] == [project_id]


def test_allocation_routes_are_quantity_aware_idempotent_and_undoable(
    monkeypatch, tmp_path: Path
) -> None:
    with _client(monkeypatch, tmp_path) as client:
        project = _create_project(client)
        project_id = project["project_id"]

        proposed = client.post(
            f"/api/project-harvest/projects/{project_id}/allocations",
            json={"piece_id": "piece-1", "part_id": "3001", "color_id": "2"},
        )
        assert proposed.status_code == 200
        allocation = proposed.json()
        assert allocation["group_id"] == "bag:1"

        repeated = client.post(
            f"/api/project-harvest/projects/{project_id}/allocations",
            json={"piece_id": "piece-1", "part_id": "3001", "color_id": "2"},
        )
        assert repeated.json()["allocation_id"] == allocation["allocation_id"]

        confirmed = client.post(
            f"/api/project-harvest/projects/{project_id}/allocations/{allocation['allocation_id']}/confirm",
            json={"evidence": {"simulation": True}},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "confirmed"

        next_piece = client.post(
            f"/api/project-harvest/projects/{project_id}/allocations",
            json={"piece_id": "piece-2", "part_id": "3001", "color_id": "2"},
        )
        assert next_piece.json()["group_id"] == "bag:2"

        undone = client.post(
            f"/api/project-harvest/projects/{project_id}/allocations/{allocation['allocation_id']}/undo",
            json={"reason": "wrong physical pocket"},
        )
        assert undone.status_code == 200
        assert undone.json()["status"] == "undone"

        live = client.post(
            f"/api/project-harvest/projects/{project_id}/allocations",
            json={
                "piece_id": "live-piece",
                "part_id": "3001",
                "color_id": "2",
                "mode": "live",
            },
        )
        assert live.status_code == 400
        assert live.json()["detail"]["code"] == "LIVE_ROUTING_DISABLED"


def test_live_activation_is_paused_explicit_and_does_not_start_motion(
    monkeypatch, tmp_path: Path
) -> None:
    with _client(monkeypatch, tmp_path) as client:
        project = _create_project(client)
        project_id = project["project_id"]
        bom = json.dumps(
            {
                "set_number": "21369",
                "namespace": {
                    "part": "bricklink_item_number",
                    "color": "bricklink_color_id",
                },
                "items": [{"part_id": "3001", "color_id": "2", "quantity": 3}],
            }
        ).encode()
        assert client.post(
            f"/api/project-harvest/projects/{project_id}/bom",
            params={"filename": "bom.json", "provider": "private_moc"},
            content=bom,
        ).status_code == 200
        configured = [
            {
                "bin_id": f"L1-S1-B{index + 1}",
                "layer_index": 0,
                "section_index": 0,
                "bin_index": index,
                "available": True,
                "reserved_by": [],
                "tracked_piece_count": 0,
            }
            for index in range(3)
        ]
        monkeypatch.setattr(
            project_harvest_router, "_configured_capacity_bins", lambda: configured
        )
        planned = client.post(
            f"/api/project-harvest/projects/{project_id}/capacity-plan", json={}
        ).json()
        simulated = client.post(
            f"/api/project-harvest/projects/{project_id}/simulate", json={}
        ).json()
        accepted = _complete_controlled_acceptance(
            client, monkeypatch, tmp_path, simulated, configured
        )
        readiness = client.get(
            f"/api/project-harvest/projects/{project_id}/bin-readiness"
        ).json()
        approved_response = client.post(
            f"/api/project-harvest/projects/{project_id}/green-light",
            json={
                "expected_revision": accepted["revision"],
                "expected_bin_state_token": readiness["bin_state_token"],
                "operator": "owner",
                "reason": "activate exact physical plan",
                "physical_bins_verified_empty": True,
            },
        )
        assert approved_response.status_code == 200, approved_response.text
        approved = approved_response.json()

        applied: list[list[dict]] = []

        def fake_apply(*, assignments: list[dict]) -> dict:
            applied.append(assignments)
            return {
                "assignments": assignments,
                "categories_before": [[[[ ] for _ in range(3)]]],
                "categories_after": [],
            }

        monkeypatch.setattr(hardware, "apply_harvest_bin_assignments", fake_apply)
        monkeypatch.setattr(
            hardware,
            "restore_harvest_bin_assignments",
            lambda *, categories_before: None,
        )
        activated_response = client.post(
            f"/api/project-harvest/projects/{project_id}/activate",
            json={
                "expected_revision": approved["revision"],
                "expected_bin_state_token": readiness["bin_state_token"],
                "operator": "owner",
                "reason": "start live bag sorting",
            },
        )
        assert activated_response.status_code == 200, activated_response.text
        activated = activated_response.json()
        assert activated["state"] == "active"
        assert len(applied[0]) == 3
        assert any(
            item["group_id"] == "harvest-exception" for item in applied[0]
        )
        runtime = client.get("/api/project-harvest/runtime").json()
        assert runtime["active"]["project_id"] == project_id
        assert runtime["sorter_state"] == "paused"
        assert runtime["activation_starts_motion"] is False

        from defs.known_object import KnownObject
        from project_harvest_runtime import reserve_piece
        piece = KnownObject(part_id="3001", color_id="2", forced_reject_reason="c3_arrival_unconfirmed",
                            classification_item_candidates=[{"id": "3001", "score": 1.0}],
                            classification_color_candidates=[{"id": "2", "score": 1.0}])
        gc = SimpleNamespace(project_harvest_dir=str(tmp_path / "harvest"))
        rejected = reserve_piece(gc, piece)
        assert rejected["exception"] and rejected["group_id"] == "harvest-exception"
        assert reserve_piece(gc, piece)["allocation_id"] == rejected["allocation_id"]
        store = project_harvest_projects.HarvestProjectStore(tmp_path / "harvest")
        dest = rejected["destination"]
        store.confirm_allocation(project_id, rejected["allocation_id"], evidence={
            "physical_drop_confirmed": True, "activation_id": rejected["activation_id"],
            "destination_bin": [dest["layer_index"], dest["section_index"], dest["bin_index"]],
        })
        normal = reserve_piece(gc, KnownObject(part_id="3001", color_id="2", confidence=1.0, color_confidence=1.0))
        assert normal["exception"] is False
        dest = normal["destination"]
        store.confirm_allocation(project_id, normal["allocation_id"], evidence={
            "physical_drop_confirmed": True, "activation_id": normal["activation_id"],
            "destination_bin": [dest["layer_index"], dest["section_index"], dest["bin_index"]],
        })

        stopped = client.post(
            f"/api/project-harvest/projects/{project_id}/deactivate",
            json={"operator": "owner", "reason": "controlled stop"},
        )
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["state"] == "paused"
        assert client.get("/api/project-harvest/runtime").json()["active"] is None


def test_portfolio_allocation_uses_highest_priority_project(
    monkeypatch, tmp_path: Path
) -> None:
    with _client(monkeypatch, tmp_path) as client:
        low = _create_project(client, priority=10)
        high = _create_project(client, priority=500)

        response = client.post(
            "/api/project-harvest/portfolio/allocations",
            json={"piece_id": "portfolio-piece", "part_id": "3001", "color_id": "2"},
        )
        assert response.status_code == 200
        assert response.json()["project_id"] == high["project_id"]

        listed = client.get("/api/project-harvest/projects")
        assert [item["project_id"] for item in listed.json()["projects"]] == [
            high["project_id"],
            low["project_id"],
        ]

        summaries = client.get("/api/project-harvest/projects/summaries")
        assert summaries.status_code == 200
        assert [item["project_id"] for item in summaries.json()["projects"]] == [
            high["project_id"],
            low["project_id"],
        ]
        assert "allocations" not in summaries.json()["projects"][0]
        assert "effective_groups" not in summaries.json()["projects"][0]


def test_selected_bin_clearance_is_stale_safe_paused_and_audited(
    monkeypatch, tmp_path: Path
) -> None:
    with _client(monkeypatch, tmp_path) as client:
        project = _create_project(client)
        project_id = project["project_id"]
        bom = json.dumps(
            {
                "set_number": "21369",
                "namespace": {
                    "part": "bricklink_item_number",
                    "color": "bricklink_color_id",
                },
                "items": [{"part_id": "3001", "color_id": "2", "quantity": 3}],
            }
        ).encode()
        assert (
            client.post(
                f"/api/project-harvest/projects/{project_id}/bom",
                params={"filename": "bom.json", "provider": "private_moc"},
                content=bom,
            ).status_code
            == 200
        )

        state = {"reserved": ["existing-category"], "pieces": 2}

        def configured_bins() -> list[dict]:
            return [
                {
                    "bin_id": "L1-S1-B1",
                    "layer_index": 0,
                    "section_index": 0,
                    "bin_index": 0,
                    "available": True,
                    "reserved_by": list(state["reserved"]),
                    "tracked_piece_count": state["pieces"],
                }
            ]

        monkeypatch.setattr(
            project_harvest_router, "_configured_capacity_bins", configured_bins
        )
        planned = client.post(
            f"/api/project-harvest/projects/{project_id}/capacity-plan", json={}
        )
        assert planned.status_code == 200, planned.text
        readiness = client.get(
            f"/api/project-harvest/projects/{project_id}/bin-readiness"
        ).json()

        called: list[list[tuple[int, int, int]]] = []

        def fake_clear(*, selected_bins: list[tuple[int, int, int]]) -> dict:
            called.append(selected_bins)
            state["reserved"] = []
            state["pieces"] = 0
            return {
                "message": "cleared",
                "snapshot_id": "snapshot-1",
                "released_assignments": 1,
            }

        monkeypatch.setattr(hardware, "clear_selected_bins", fake_clear)

        stale = client.post(
            f"/api/project-harvest/projects/{project_id}/bin-clearance",
            json={
                "bin_ids": ["L1-S1-B1"],
                "expected_bin_state_token": "stale",
                "operator": "operator",
                "physical_bins_emptied": True,
            },
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "STALE_BIN_STATE"
        assert called == []

        cleared = client.post(
            f"/api/project-harvest/projects/{project_id}/bin-clearance",
            json={
                "bin_ids": ["L1-S1-B1"],
                "expected_bin_state_token": readiness["bin_state_token"],
                "operator": "operator",
                "physical_bins_emptied": True,
            },
        )
        assert cleared.status_code == 200, cleared.text
        assert called == [[(0, 0, 0)]]
        assert cleared.json()["bin_readiness"]["status"] == "clear"
        assert cleared.json()["project"]["events"][-1]["kind"] == "bin_clearance_recorded"


def test_invalid_project_and_bom_errors_are_structured(
    monkeypatch, tmp_path: Path
) -> None:
    with _client(monkeypatch, tmp_path) as client:
        invalid = client.get("/api/project-harvest/projects/not-a-project")
        assert invalid.status_code == 400
        assert invalid.json()["detail"]["code"] == "INVALID_PROJECT_ID"

        project = _create_project(client)
        bad_bom = client.post(
            f"/api/project-harvest/projects/{project['project_id']}/bom",
            params={"filename": "bad.json"},
            content=b"not-json",
        )
        assert bad_bom.status_code == 400
        assert bad_bom.json()["detail"]["code"] == "INVALID_BOM"


def test_rebrickable_route_requires_configured_key(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(project_harvest_router, "getApiKeys", lambda: {})
    monkeypatch.delenv("REBRICKABLE_API_KEY", raising=False)
    with _client(monkeypatch, tmp_path) as client:
        project = _create_project(client)
        response = client.post(
            f"/api/project-harvest/projects/{project['project_id']}/bom/rebrickable"
        )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "REBRICKABLE_API_KEY_REQUIRED"


def test_rebrickable_route_freezes_fetched_source(monkeypatch, tmp_path: Path) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        project_harvest_router, "getApiKeys", lambda: {"rebrickable": "saved-key"}
    )

    def fake_fetch(*, set_number: str, api_key: str) -> dict:
        captured.update({"set_number": set_number, "api_key": api_key})
        return {
            "content": json.dumps(
                {
                    "set_number": "21369-1",
                    "namespace": {
                        "part": "rebrickable_part_number",
                        "color": "rebrickable_color_id",
                    },
                    "items": [
                        {"part_id": "3001", "color_id": "2", "quantity": 3}
                    ],
                }
            ).encode(),
            "filename": "rebrickable-21369-1.json",
            "provider": "rebrickable_api",
        }

    monkeypatch.setattr(
        project_harvest_router.project_harvest_projects,
        "fetch_rebrickable_bom",
        fake_fetch,
    )
    with _client(monkeypatch, tmp_path) as client:
        project = _create_project(client)
        response = client.post(
            f"/api/project-harvest/projects/{project['project_id']}/bom/rebrickable"
        )

    assert response.status_code == 200
    assert response.json()["bom"]["provider"] == "rebrickable_api"
    assert response.json()["bom"]["summary"]["total_quantity"] == 3
    assert captured == {"set_number": "21369", "api_key": "saved-key"}
