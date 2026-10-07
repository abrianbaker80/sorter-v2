from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import project_harvest
import project_harvest_projects as projects


def _item(
    part_id: str,
    color_id: str,
    quantity: int,
    group: str,
    *,
    status: str | None = None,
) -> str:
    status_xml = f"<Status>{status}</Status>" if status else ""
    return (
        "<Item>"
        f"<ItemID>{part_id}</ItemID>"
        "<ItemTypeID>P</ItemTypeID>"
        f"<ColorID>{color_id}</ColorID>"
        f"<Qty>{quantity}</Qty>"
        "<Condition>N</Condition>"
        f"{status_xml}"
        f"<Remarks>{group}</Remarks>"
        "</Item>"
    )


def _bsx(*items: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<!DOCTYPE BrickStoreXML>"
        "<BrickStoreXML><Inventory>" + "".join(items) + "</Inventory></BrickStoreXML>"
    ).encode()


def _bom(
    *items: dict,
    part_namespace: str = "bricklink_item_number",
    color_namespace: str = "bricklink_color_id",
) -> bytes:
    return json.dumps(
        {
            "set_number": "21369",
            "namespace": {"part": part_namespace, "color": color_namespace},
            "items": list(items),
        }
    ).encode()


def _store_with_draft(tmp_path: Path, *, repeated_part: bool = False):
    if repeated_part:
        content = _bsx(
            _item("3001", "2", 1, "Bag 1"),
            _item("3001", "2", 2, "Bag 2"),
        )
    else:
        content = _bsx(
            _item("3001", "2", 3, "Bag 1"),
            _item("3002", "5", 1, "Bag 2"),
            _item("3003", "1", 1, "Extra Parts", status="E"),
        )
    draft = project_harvest.save_bsx_draft(
        tmp_path, set_number="21369", filename="21369.bsx", content=content
    )
    store = projects.HarvestProjectStore(tmp_path)
    return store, draft


def _resolved_policy(project: dict) -> dict:
    policy = dict(project["policy"])
    policy["group_actions"] = {
        group_id: ("exclude" if group_id == "extras" else "separate")
        for group_id in policy["group_actions"]
    }
    policy["extras_action"] = "exclude"
    return policy


def _bins(count: int, *, max_pieces: int | None = None) -> list[dict]:
    return [
        {
            "bin_id": f"bin-{index + 1}",
            "available": True,
            "reserved_by": [],
            "max_pieces": max_pieces,
        }
        for index in range(count)
    ]


class _RebrickableResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.content = json.dumps(payload).encode()

    def json(self) -> dict:
        return self._payload


def _rebrickable_row(
    part_num: str, color_id: int, quantity: int, *, is_spare: bool = False
) -> dict:
    return {
        "part": {"part_num": part_num, "name": f"Part {part_num}"},
        "color": {"id": color_id, "name": f"Color {color_id}"},
        "quantity": quantity,
        "is_spare": is_spare,
    }


def _rebrickable_set_metadata(
    set_number: str = "21369-1", *, image_url: str | None = None
) -> dict:
    return {
        "set_num": set_number,
        "name": "IDEAS Typewriter",
        "year": 2021,
        "theme_id": 576,
        "num_parts": 2079,
        "set_img_url": image_url
        or f"https://cdn.rebrickable.com/media/sets/{set_number}.jpg",
        "set_url": f"https://rebrickable.com/sets/{set_number}/ideas-typewriter/",
        "last_modified_dt": "2025-01-02T03:04:05Z",
    }


def test_rebrickable_fetch_uses_header_pagination_and_preserves_spares(
    monkeypatch,
) -> None:
    calls: list[dict] = []
    pages = [
        _RebrickableResponse(_rebrickable_set_metadata()),
        _RebrickableResponse(
            {
                "count": 3,
                "next": "https://rebrickable.com/api/v3/lego/sets/21369-1/parts/?page=2",
                "previous": None,
                "results": [
                    _rebrickable_row("3001", 2, 3),
                    _rebrickable_row("3024", 1, 1, is_spare=True),
                ],
            }
        ),
        _RebrickableResponse(
            {
                "count": 3,
                "next": None,
                "previous": "https://rebrickable.com/api/v3/lego/sets/21369-1/parts/",
                "results": [_rebrickable_row("3002", 0, 2)],
            }
        ),
    ]

    def fake_get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return pages.pop(0)

    monkeypatch.setattr(projects.requests, "get", fake_get)
    source = projects.fetch_rebrickable_bom(
        set_number="21369", api_key="private-test-key"
    )
    envelope = json.loads(source["content"])

    assert source["provider"] == "rebrickable_api"
    assert envelope["set_number"] == "21369-1"
    assert envelope["summary"]["authoritative_quantity"] == 5
    assert envelope["summary"]["spare_quantity"] == 1
    assert envelope["set_metadata"]["name"] == "IDEAS Typewriter"
    assert [item["part_id"] for item in envelope["items"]] == ["3001", "3002"]
    assert envelope["spares"][0]["part_id"] == "3024"
    assert calls[0]["url"].endswith("/api/v3/lego/sets/21369-1/")
    assert calls[0]["params"] is None
    assert calls[1]["headers"]["Authorization"] == "key private-test-key"
    assert calls[1]["params"]["inc_minifig_parts"] == 1
    assert calls[1]["timeout"] == (5.0, 90.0)
    assert calls[2]["params"] is None
    assert all("private-test-key" not in call["url"] for call in calls)

    normalized = projects.parse_bom_upload(
        source["content"],
        filename=source["filename"],
        provider=source["provider"],
        default_set_number="21369",
    )
    assert normalized["summary"]["total_quantity"] == 5
    assert normalized["namespace"]["color"] == "rebrickable_color_id"
    assert normalized["set_metadata"]["official_piece_count"] == 2079
    assert normalized["set_metadata"]["image_url"].startswith(
        "https://cdn.rebrickable.com/"
    )


def test_rebrickable_fetch_rejects_unsafe_pagination(monkeypatch) -> None:
    responses = [
        _RebrickableResponse(_rebrickable_set_metadata()),
        _RebrickableResponse(
            {
                "count": 1,
                "next": "https://example.com/steal-key",
                "previous": None,
                "results": [_rebrickable_row("3001", 2, 1)],
            },
        ),
    ]
    monkeypatch.setattr(
        projects.requests, "get", lambda *_args, **_kwargs: responses.pop(0)
    )
    with pytest.raises(projects.HarvestProjectError) as exc_info:
        projects.fetch_rebrickable_bom(set_number="21369-1", api_key="secret")
    assert exc_info.value.code == "REBRICKABLE_INVALID_RESPONSE"


def test_rebrickable_fetch_rejects_unsafe_set_image(monkeypatch) -> None:
    monkeypatch.setattr(
        projects.requests,
        "get",
        lambda *_args, **_kwargs: _RebrickableResponse(
            _rebrickable_set_metadata(image_url="https://example.com/tracker.jpg")
        ),
    )
    with pytest.raises(projects.HarvestProjectError) as exc_info:
        projects.fetch_rebrickable_bom(set_number="21369-1", api_key="secret")
    assert exc_info.value.code == "REBRICKABLE_INVALID_RESPONSE"


def test_set_metadata_is_shared_by_projects_for_the_same_set(tmp_path: Path) -> None:
    store, draft = _store_with_draft(tmp_path)
    source = store.create_project(draft_id=draft["draft_id"])
    source = store.save_bom(
        source["project_id"],
        content=json.dumps(
            {
                "set_number": "21369-1",
                "namespace": {
                    "part": "rebrickable_part_number",
                    "color": "rebrickable_color_id",
                },
                "set_metadata": _rebrickable_set_metadata(),
                "items": [{"part_id": "3001", "color_id": "2", "quantity": 3}],
            }
        ).encode(),
        filename="rebrickable-21369-1.json",
        provider="rebrickable_api",
    )
    sibling = store.create_project(draft_id=draft["draft_id"], priority=101)

    assert source["set_metadata"]["name"] == "IDEAS Typewriter"
    assert sibling["set_metadata"]["official_piece_count"] == 2079
    assert sibling["set_metadata"]["source_bom_revision_id"] == source["bom"][
        "bom_revision_id"
    ]


def test_create_project_reuses_an_identical_draft_configuration(tmp_path: Path) -> None:
    store, draft = _store_with_draft(tmp_path)
    first = store.create_project(draft_id=draft["draft_id"])
    repeated = store.create_project(draft_id=draft["draft_id"])
    distinct = store.create_project(draft_id=draft["draft_id"], priority=101)

    assert repeated["project_id"] == first["project_id"]
    assert distinct["project_id"] != first["project_id"]
    assert len(store.list_projects()) == 2


def test_bom_reconciliation_capacity_simulation_and_acceptance_gates(
    tmp_path: Path,
) -> None:
    store, draft = _store_with_draft(tmp_path)
    project = store.create_project(draft_id=draft["draft_id"], name="Test build")
    assert project["reconciliation"]["status"] == "unverified"
    assert project["readiness"]["hardware_activation_allowed"] is False

    project = store.save_bom(
        project["project_id"],
        content=_bom(
            {"part_id": "3001", "color_id": "2", "quantity": 3},
            {"part_id": "3002", "color_id": "5", "quantity": 1},
        ),
        filename="rebrickable-21369.json",
        provider="private_moc",
    )
    assert project["reconciliation"]["status"] == "incomplete"
    assert project["bom"]["source_sha256"] == project["bom"]["source_sha256"]

    project = store.update_review(
        project["project_id"], policy=_resolved_policy(project)
    )
    assert project["reconciliation"]["status"] == "validated"

    project = store.plan_capacity(project["project_id"], bins=_bins(2))
    assert project["capacity_plan"]["status"] == "ready"
    assert project["capacity_plan"]["execution_mode"] == "single_wave"

    project = store.simulate(project["project_id"])
    assert project["simulation"]["status"] == "passed"
    assert project["readiness"]["ready_for_physical_acceptance"] is True
    assert project["readiness"]["activation_eligible"] is False
    assert project["readiness"]["live_integration_enabled"] is False

    project = store.record_acceptance(
        project["project_id"],
        evidence={
            "test_id": "controlled-test-1",
            "operator": "test-operator",
            "controlled_piece_count": 4,
            "result": "passed",
        },
    )
    assert project["state"] == "accepted"
    assert project["readiness"]["activation_eligible"] is True
    assert project["readiness"]["hardware_activation_allowed"] is False
    assert project["events"][-1]["kind"] == "physical_acceptance_recorded"

    with pytest.raises(projects.HarvestProjectError) as exc_info:
        store.transition(
            project["project_id"], target="completed", reason="premature completion"
        )
    assert exc_info.value.code == "PROJECT_INCOMPLETE"

    project = store.transition(
        project["project_id"], target="paused", reason="controlled pause"
    )
    assert project["state"] == "paused"
    assert project["acceptance"]["result"] == "passed"
    assert project["readiness"]["activation_eligible"] is False
    project = store.transition(
        project["project_id"], target="review", reason="review after pause"
    )
    assert project["acceptance"] is None

    project = store.record_acceptance(
        project["project_id"],
        evidence={
            "test_id": "controlled-test-2",
            "operator": "test-operator",
            "controlled_piece_count": 4,
            "result": "passed",
        },
    )
    project = store.plan_capacity(project["project_id"], bins=_bins(2))
    assert project["state"] == "ready"
    assert project["acceptance"] is None
    assert project["readiness"]["activation_eligible"] is False
    assert store.verify_all()["count"] == 1


def test_namespace_mapping_is_required_and_reconciles_different_sources(
    tmp_path: Path,
) -> None:
    store, draft = _store_with_draft(tmp_path)
    project = store.create_project(draft_id=draft["draft_id"])
    project = store.save_bom(
        project["project_id"],
        content=_bom(
            {"part_id": "RB-3001", "color_id": "RB-2", "quantity": 3},
            {"part_id": "RB-3002", "color_id": "RB-5", "quantity": 1},
            part_namespace="rebrickable_part_number",
            color_namespace="rebrickable_color_id",
        ),
        filename="rebrickable.json",
        provider="rebrickable",
    )
    project = store.update_review(
        project["project_id"], policy=_resolved_policy(project)
    )
    assert project["reconciliation"]["status"] == "incomplete"
    assert project["reconciliation"]["unmapped"]

    project = store.update_review(
        project["project_id"],
        mappings={
            "namespace": {
                "parts": {
                    "3001": "RB-3001",
                    "3002": "RB-3002",
                    "3003": "RB-3003",
                },
                "colors": {"1": "RB-1", "2": "RB-2", "5": "RB-5"},
            },
            "substitutions": [],
        },
    )
    assert project["reconciliation"]["status"] == "validated"
    assert project["reconciliation"]["unmapped"] == []


def test_capacity_waves_reserved_bins_and_overflow(tmp_path: Path) -> None:
    store, draft = _store_with_draft(tmp_path)
    project = store.create_project(draft_id=draft["draft_id"])
    project = store.save_bom(
        project["project_id"],
        content=_bom(
            {"part_id": "3001", "color_id": "2", "quantity": 3},
            {"part_id": "3002", "color_id": "5", "quantity": 1},
        ),
        filename="bom.json",
        provider="private_moc",
    )
    project = store.update_review(
        project["project_id"], policy=_resolved_policy(project)
    )
    project = store.plan_capacity(project["project_id"], bins=_bins(1))
    assert project["capacity_plan"]["execution_mode"] == "two_stage"
    assert project["capacity_plan"]["summary"]["wave_count"] == 2

    project = store.plan_capacity(
        project["project_id"],
        bins=[
            {"bin_id": "reserved", "available": True, "reserved_by": ["other"]},
            {"bin_id": "small", "available": True, "reserved_by": [], "max_pieces": 2},
        ],
    )
    assert project["capacity_plan"]["status"] == "blocked"
    assert project["capacity_plan"]["overflow"][0]["group_id"] == "bag:1"


def test_quantity_aware_allocation_confirmation_and_undo_are_durable(
    tmp_path: Path,
) -> None:
    store, draft = _store_with_draft(tmp_path, repeated_part=True)
    project = store.create_project(draft_id=draft["draft_id"])
    project = store.save_bom(
        project["project_id"],
        content=_bom({"part_id": "3001", "color_id": "2", "quantity": 3}),
        filename="bom.json",
        provider="private_moc",
    )
    project_id = project["project_id"]

    first = store.propose_allocation(
        project_id,
        piece_id="piece-1",
        part_id="3001",
        color_id="2",
    )
    assert first["group_id"] == "bag:1"
    repeated = store.propose_allocation(
        project_id,
        piece_id="piece-1",
        part_id="3001",
        color_id="2",
    )
    assert repeated["allocation_id"] == first["allocation_id"]
    second = store.propose_allocation(
        project_id,
        piece_id="piece-2",
        part_id="3001",
        color_id="2",
    )
    assert second["group_id"] == "bag:2"
    store.confirm_allocation(
        project_id, first["allocation_id"], evidence={"simulation": True}
    )
    store.confirm_allocation(
        project_id, second["allocation_id"], evidence={"simulation": True}
    )
    snapshot = store.get_project(project_id)
    assert snapshot["progress"]["summary"]["confirmed_quantity"] == 2

    undone = store.undo_allocation(
        project_id, first["allocation_id"], reason="test correction"
    )
    assert undone["status"] == "undone"
    reopened = store.propose_allocation(
        project_id,
        piece_id="piece-3",
        part_id="3001",
        color_id="2",
    )
    assert reopened["group_id"] == "bag:1"
    assert store.get_project(project_id)["events"][-1]["kind"] == "allocation_proposed"

    with pytest.raises(projects.HarvestProjectError) as exc_info:
        store.propose_allocation(
            project_id,
            piece_id="live-piece",
            part_id="3001",
            color_id="2",
            mode="live",
        )
    assert exc_info.value.code == "LIVE_ROUTING_DISABLED"


def test_substitute_policy_and_portfolio_priority(tmp_path: Path) -> None:
    store, draft = _store_with_draft(tmp_path, repeated_part=True)
    store.create_project(
        draft_id=draft["draft_id"], priority=10, match_policy="exact", name="Low"
    )
    high = store.create_project(
        draft_id=draft["draft_id"], priority=100, match_policy="substitute", name="High"
    )
    store.update_review(
        high["project_id"],
        mappings={
            "namespace": {"parts": {}, "colors": {}},
            "substitutions": [
                {
                    "kind": "substitute",
                    "input": {"part_id": "ALT-3001", "color_id": "2"},
                    "target": {"part_id": "3001", "color_id": "2"},
                }
            ],
        },
    )
    allocation = store.propose_allocation(
        high["project_id"],
        piece_id="substitute-piece",
        part_id="ALT-3001",
        color_id="2",
    )
    assert allocation["match_kind"] == "substitute"
    assert [item["name"] for item in store.list_projects()] == ["High", "Low"]


def test_private_moc_csv_non_sortable_and_adaptive_fallback(tmp_path: Path) -> None:
    store, draft = _store_with_draft(tmp_path)
    project = store.create_project(
        draft_id=draft["draft_id"], fallback_mode="adaptive_part"
    )
    content = (
        "part_id,color_id,quantity,item_type,sortable\n"
        "3001,2,3,P,true\n"
        "3002,5,1,P,true\n"
        "sticker,0,1,S,false\n"
    ).encode()
    project = store.save_bom(
        project["project_id"],
        content=content,
        filename="private-moc.csv",
        provider="private_moc",
    )
    assert project["bom"]["summary"]["non_sortable_quantity"] == 1
    assert len(project["effective_groups"]) == 2
    assert all(
        group["kind"] == "adaptive_part" for group in project["effective_groups"]
    )
    assert project["reconciliation"]["status"] == "fallback"
    project = store.plan_capacity(
        project["project_id"],
        bins=[
            {"bin_id": "bin-1", "available": True, "max_pieces": 10},
            {"bin_id": "bin-2", "available": True, "max_pieces": 10},
        ],
    )
    project = store.simulate(project["project_id"])
    assert project["simulation"]["status"] == "passed"
    assert project["readiness"]["ready_for_physical_acceptance"] is True


def test_bom_and_audit_tampering_are_detected(tmp_path: Path) -> None:
    store, draft = _store_with_draft(tmp_path)
    project = store.create_project(draft_id=draft["draft_id"])
    project = store.save_bom(
        project["project_id"],
        content=_bom({"part_id": "3001", "color_id": "2", "quantity": 3}),
        filename="bom.json",
        provider="private_moc",
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE harvest_bom_revisions SET source_blob = ? WHERE project_id = ?",
            (b"tampered", project["project_id"]),
        )
        conn.commit()
    with pytest.raises(projects.HarvestProjectError) as exc_info:
        store.get_project(project["project_id"])
    assert exc_info.value.code == "BOM_SOURCE_HASH_MISMATCH"

    manifest_root = tmp_path / "manifest"
    manifest_store, manifest_draft = _store_with_draft(manifest_root)
    manifest = manifest_store.create_project(draft_id=manifest_draft["draft_id"])
    manifest = manifest_store.save_bom(
        manifest["project_id"],
        content=_bom({"part_id": "3001", "color_id": "2", "quantity": 3}),
        filename="bom.json",
        provider="private_moc",
    )
    with sqlite3.connect(manifest_store.db_path) as conn:
        normalized = json.loads(
            conn.execute(
                "SELECT normalized_json FROM harvest_bom_revisions WHERE project_id = ?",
                (manifest["project_id"],),
            ).fetchone()[0]
        )
        normalized["items"][0]["quantity"] = 999
        conn.execute(
            "UPDATE harvest_bom_revisions SET normalized_json = ? WHERE project_id = ?",
            (json.dumps(normalized), manifest["project_id"]),
        )
        conn.commit()
    with pytest.raises(projects.HarvestProjectError) as exc_info:
        manifest_store.get_project(manifest["project_id"])
    assert exc_info.value.code == "BOM_MANIFEST_HASH_MISMATCH"

    other_root = tmp_path / "audit"
    other_store, other_draft = _store_with_draft(other_root)
    other = other_store.create_project(draft_id=other_draft["draft_id"])
    with sqlite3.connect(other_store.db_path) as conn:
        conn.execute(
            "UPDATE harvest_events SET payload_json = ? WHERE project_id = ?",
            ('{"tampered":true}', other["project_id"]),
        )
        conn.commit()
    with pytest.raises(projects.HarvestProjectError) as exc_info:
        other_store.get_project(other["project_id"])
    assert exc_info.value.code == "AUDIT_CHAIN_MISMATCH"


def test_missing_parts_csv_contains_group_quotas(tmp_path: Path) -> None:
    store, draft = _store_with_draft(tmp_path, repeated_part=True)
    project = store.create_project(draft_id=draft["draft_id"])
    payload = projects.missing_parts_csv(project).decode()
    assert "project_id,set_number,group_id" in payload
    assert "bag:1" in payload
    assert "bag:2" in payload


def _store_with_retirement_allocations(tmp_path: Path):
    store, draft = _store_with_draft(tmp_path, repeated_part=True)
    project = store.create_project(draft_id=draft["draft_id"])
    project = store.save_bom(
        project["project_id"],
        content=_bom({"part_id": "3001", "color_id": "2", "quantity": 3}),
        filename="bom.json", provider="private_moc",
    )
    original = store.propose_allocation(
        project["project_id"], piece_id="original-piece", part_id="3001", color_id="2",
    )
    current = store.propose_allocation(
        project["project_id"], piece_id="current-piece", part_id="3001", color_id="2",
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE harvest_allocations SET mode='live',runtime_id='original-activation' WHERE allocation_id=?",
            (original["allocation_id"],),
        )
        conn.execute(
            "UPDATE harvest_allocations SET mode='live',runtime_id='current-activation' WHERE allocation_id=?",
            (current["allocation_id"],),
        )
        conn.execute(
            "INSERT INTO harvest_runtime_state VALUES(1,?,?,?,?)",
            (project["project_id"], "current-activation",
             json.dumps({"activation_id": "current-activation", "status": "active", "runtime_mode": "live",
                         "assignments": [{"group_id": current["group_id"], "layer_index": 0,
                                          "section_index": 0, "bin_index": 1}]}),
             projects._now()),
        )
    return store, project["project_id"], original, current


def test_exact_confirmation_retirement_is_idempotent_and_preserves_current_runtime(tmp_path: Path) -> None:
    store, project_id, original, current = _store_with_retirement_allocations(tmp_path)
    arguments = dict(
        piece_ids=[original["piece_id"]], project_id=project_id,
        allocation_id=original["allocation_id"], activation_id="original-activation",
        reason="Harvest confirmation uncredited (PHYSICAL_EVIDENCE_REQUIRED)",
    )
    assert store.retire_c4_planned_allocations(**arguments) == 1
    assert store.retire_c4_planned_allocations(**arguments) == 0
    with sqlite3.connect(store.db_path) as conn:
        rows = dict(conn.execute("SELECT allocation_id,status FROM harvest_allocations"))
        assert rows == {original["allocation_id"]: "undone", current["allocation_id"]: "planned"}
        assert conn.execute("SELECT COUNT(*) FROM harvest_allocations WHERE confirmed_at IS NOT NULL").fetchone()[0] == 0
    project = store.get_project(project_id)
    retirements = [event for event in project["events"] if event["kind"] == "c4_unconfirmed_allocation_retired"]
    assert len(retirements) == 1
    assert retirements[0]["payload"]["reason"] == arguments["reason"]
    # The existing default API still retires only the currently active scope.
    assert store.retire_c4_planned_allocations() == 1


@pytest.mark.parametrize("case", [
    "confirmed", "undone", "missing", "wrong_project", "wrong_allocation",
    "wrong_piece", "wrong_activation", "integrated", "simulation",
])
def test_exact_confirmation_retirement_preserves_other_states(tmp_path: Path, case: str) -> None:
    store, project_id, original, _current = _store_with_retirement_allocations(tmp_path)
    arguments = dict(
        piece_ids=[original["piece_id"]], project_id=project_id,
        allocation_id=original["allocation_id"], activation_id="original-activation",
        reason="Harvest confirmation uncredited",
    )
    with sqlite3.connect(store.db_path) as conn:
        if case in {"confirmed", "undone"}:
            conn.execute("UPDATE harvest_allocations SET status=? WHERE allocation_id=?",
                         (case, original["allocation_id"]))
        elif case == "missing":
            conn.execute("DELETE FROM harvest_allocations WHERE allocation_id=?", (original["allocation_id"],))
        elif case == "simulation":
            conn.execute("UPDATE harvest_allocations SET mode='simulation' WHERE allocation_id=?",
                         (original["allocation_id"],))
        elif case == "integrated":
            from harvest_integration_storage import DDL
            for statement in DDL:
                conn.execute(statement)
            conn.execute("INSERT INTO harvest_integration_versions VALUES(1,1)")
            conn.execute("INSERT INTO harvest_integration_requests VALUES(?,?,?,?,?,?,?,?,?)",
                         ("operation", "request", "test", "{}", original["allocation_id"],
                          "{}", None, "{}", projects._now()))
        before = list(conn.execute("SELECT * FROM harvest_allocations ORDER BY allocation_id"))
        events_before = list(conn.execute("SELECT * FROM harvest_events ORDER BY sequence"))
    if case == "wrong_project":
        arguments["project_id"] = "other-project"
    elif case == "wrong_allocation":
        arguments["allocation_id"] = "other-allocation"
    elif case == "wrong_piece":
        arguments["piece_ids"] = ["current-piece"]
    elif case == "wrong_activation":
        arguments["activation_id"] = "current-activation"
    assert store.retire_c4_planned_allocations(**arguments) == 0
    with sqlite3.connect(store.db_path) as conn:
        assert list(conn.execute("SELECT * FROM harvest_allocations ORDER BY allocation_id")) == before
        assert list(conn.execute("SELECT * FROM harvest_events ORDER BY sequence")) == events_before


def test_confirmation_retirement_wrapper_uses_original_piece_identity() -> None:
    from project_harvest_runtime import retire_unconfirmed_piece_drop

    piece = SimpleNamespace(
        uuid="original-piece", harvest_project_id="original-project",
        harvest_allocation_id="original-allocation", harvest_activation_id="original-activation",
    )
    store = Mock()
    store.retire_c4_planned_allocations.return_value = 1
    with patch("project_harvest_runtime._store", return_value=store):
        assert retire_unconfirmed_piece_drop(object(), piece, reason="unconfirmed") == 1
    store.retire_c4_planned_allocations.assert_called_once_with(
        piece_ids=["original-piece"], project_id="original-project",
        allocation_id="original-allocation", activation_id="original-activation", reason="unconfirmed",
    )
    piece.harvest_activation_id = None
    with patch("project_harvest_runtime._store") as lookup:
        assert retire_unconfirmed_piece_drop(object(), piece, reason="unconfirmed") == 0
        lookup.assert_not_called()


@pytest.mark.parametrize("case", ["missing_project", "missing_allocation", "missing_activation", "missing_ownership_ids", "invalid_destination"])
def test_marker_confirmation_rejects_incomplete_identity_and_destination(case: str) -> None:
    from project_harvest_runtime import confirm_piece_drop

    piece = SimpleNamespace(
        uuid="original-piece", harvest_project_id="original-project",
        harvest_allocation_id="original-allocation", harvest_activation_id="original-activation",
        c4_marker_exit_boundary=21, destination_bin=(0, 0, 1), harvest_exception=False,
    )
    if case == "invalid_destination":
        piece.destination_bin = (0, None, 1)
    elif case == "missing_ownership_ids":
        piece.harvest_project_id = None
        piece.harvest_allocation_id = None
    else:
        field = {"missing_project": "harvest_project_id", "missing_allocation": "harvest_allocation_id",
                 "missing_activation": "harvest_activation_id"}[case]
        setattr(piece, field, None)
    with patch("project_harvest_runtime._store") as lookup:
        with pytest.raises(projects.HarvestProjectError) as raised:
            confirm_piece_drop(object(), piece)
        expected = "HARVEST_DESTINATION_MISSING" if case == "invalid_destination" else "HARVEST_CONFIRMATION_IDENTITY_MISMATCH"
        assert raised.value.code == expected
        lookup.assert_not_called()


def test_marker_confirmation_wrapper_preserves_ordinary_and_legacy_paths() -> None:
    from project_harvest_runtime import confirm_piece_drop

    piece = SimpleNamespace(
        uuid="original-piece", harvest_project_id=None, harvest_allocation_id=None,
        harvest_activation_id="retained-reject-activation", harvest_group_id="retained-group",
        c4_marker_exit_boundary=21, c4_discard=True, destination_bin=None,
    )
    with patch("project_harvest_runtime._store") as lookup:
        assert confirm_piece_drop(object(), piece) is None
        lookup.assert_not_called()
    piece.c4_discard = False
    piece.harvest_activation_id = None
    piece.harvest_group_id = None
    with patch("project_harvest_runtime._store") as lookup:
        assert confirm_piece_drop(object(), piece) is None
        lookup.assert_not_called()
    piece.harvest_project_id = "original-project"
    piece.harvest_allocation_id = "original-allocation"
    piece.harvest_activation_id = "original-activation"
    piece.destination_bin = (0, 0, 1)
    store = Mock()
    store.confirm_allocation.return_value = {"status": "confirmed"}
    with patch("project_harvest_runtime._store", return_value=store):
        confirm_piece_drop(object(), piece)
    assert store.confirm_allocation.call_args.kwargs["expected_piece_id"] == piece.uuid
    assert store.confirm_allocation.call_args.kwargs["expected_activation_id"] == piece.harvest_activation_id
    piece.c4_marker_exit_boundary = None
    store.reset_mock()
    with patch("project_harvest_runtime._store", return_value=store):
        confirm_piece_drop(object(), piece)
    assert set(store.confirm_allocation.call_args.kwargs) == {"evidence"}


@pytest.mark.parametrize("case", ["wrong_piece", "wrong_activation", "confirmed_mismatched_evidence"])
def test_exact_marker_confirmation_preserves_foreign_or_confirmed_allocation(tmp_path: Path, case: str) -> None:
    store, project_id, _original, current = _store_with_retirement_allocations(tmp_path)
    evidence = {"physical_drop_confirmed": True, "activation_id": "current-activation",
                "piece_id": current["piece_id"], "destination_bin": [0, 0, 1]}
    expected_piece_id = current["piece_id"]
    expected_activation_id = "current-activation"
    if case == "wrong_piece":
        expected_piece_id = "foreign-piece"
        evidence["piece_id"] = expected_piece_id
    elif case == "wrong_activation":
        expected_activation_id = "foreign-activation"
        evidence["activation_id"] = expected_activation_id
    with sqlite3.connect(store.db_path) as conn:
        if case == "confirmed_mismatched_evidence":
            conn.execute("UPDATE harvest_allocations SET status='confirmed',confirmed_at=?,evidence_json=? WHERE allocation_id=?",
                         (projects._now(), json.dumps(evidence), current["allocation_id"]))
            evidence["destination_bin"] = [0, 0, 2]
        before = list(conn.execute("SELECT * FROM harvest_allocations ORDER BY allocation_id"))
        events_before = list(conn.execute("SELECT * FROM harvest_events ORDER BY sequence"))
    with pytest.raises(projects.HarvestProjectError) as raised:
        store.confirm_allocation(
            project_id, current["allocation_id"], evidence=evidence,
            expected_piece_id=expected_piece_id, expected_activation_id=expected_activation_id,
        )
    expected_code = "PHYSICAL_EVIDENCE_REQUIRED" if case == "confirmed_mismatched_evidence" else "HARVEST_CONFIRMATION_IDENTITY_MISMATCH"
    assert raised.value.code == expected_code
    with sqlite3.connect(store.db_path) as conn:
        assert list(conn.execute("SELECT * FROM harvest_allocations ORDER BY allocation_id")) == before
        assert list(conn.execute("SELECT * FROM harvest_events ORDER BY sequence")) == events_before
    if case == "confirmed_mismatched_evidence":
        # Existing API callers retain their historical idempotent receipt behavior.
        assert store.confirm_allocation(project_id, current["allocation_id"], evidence=evidence)["status"] == "confirmed"


def test_exact_marker_confirmation_success_and_replay_do_not_duplicate_credit(tmp_path: Path) -> None:
    store, project_id, _original, current = _store_with_retirement_allocations(tmp_path)
    arguments = dict(
        evidence={"physical_drop_confirmed": True, "activation_id": "current-activation",
                  "piece_id": current["piece_id"], "destination_bin": [0, 0, 1]},
        expected_piece_id=current["piece_id"], expected_activation_id="current-activation",
    )
    result = store.confirm_allocation(project_id, current["allocation_id"], **arguments)
    assert result["status"] == "confirmed"
    with sqlite3.connect(store.db_path) as conn:
        before = list(conn.execute("SELECT * FROM harvest_allocations ORDER BY allocation_id"))
        events_before = list(conn.execute("SELECT * FROM harvest_events ORDER BY sequence"))
    assert store.confirm_allocation(project_id, current["allocation_id"], **arguments)["status"] == "confirmed"
    with sqlite3.connect(store.db_path) as conn:
        assert list(conn.execute("SELECT * FROM harvest_allocations ORDER BY allocation_id")) == before
        assert list(conn.execute("SELECT * FROM harvest_events ORDER BY sequence")) == events_before
