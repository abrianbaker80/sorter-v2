from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import project_harvest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import shared_state
from server.routers.project_harvest import router


SAMPLE_BSX = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE BrickStoreXML>
<BrickStoreXML>
 <Inventory>
  <Item>
   <ItemID>3001</ItemID>
   <ItemTypeID>P</ItemTypeID>
   <ColorID>2</ColorID>
   <Qty>3</Qty>
   <Condition>N</Condition>
   <Remarks>Bag 1</Remarks>
  </Item>
  <Item>
   <ItemID>3002</ItemID>
   <ItemTypeID>P</ItemTypeID>
   <ColorID>5</ColorID>
   <Qty>1</Qty>
   <Condition>N</Condition>
   <Remarks>Bag 2</Remarks>
  </Item>
 </Inventory>
</BrickStoreXML>
"""


def _client(monkeypatch, tmp_path: Path) -> TestClient:
    monkeypatch.setattr(
        shared_state,
        "gc_ref",
        SimpleNamespace(project_harvest_dir=str(tmp_path / "harvest")),
    )
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_raw_bsx_import_is_immutable_and_available_from_list_and_load(
    monkeypatch, tmp_path: Path
) -> None:
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/project-harvest/drafts/bsx",
            params={"set_number": "21369", "filename": "lego-21369.bsx"},
            content=SAMPLE_BSX,
            headers={"content-type": "application/xml"},
        )
        assert response.status_code == 200
        draft = response.json()
        assert draft["set_number"] == "21369"
        assert draft["summary"]["numbered_bag_count"] == 2
        assert draft["summary"]["source_total_quantity"] == 4
        assert draft["source"]["source_kind"] == "bricksperbag_manual_bsx"
        assert draft["validation"]["activation_allowed"] is False

        repeated = client.post(
            "/api/project-harvest/drafts/bsx",
            params={"set_number": "21369", "filename": "renamed.bsx"},
            content=SAMPLE_BSX,
        )
        assert repeated.status_code == 200
        assert repeated.json()["draft_id"] == draft["draft_id"]

        listed = client.get("/api/project-harvest/drafts")
        assert listed.status_code == 200
        assert [item["draft_id"] for item in listed.json()["drafts"]] == [
            draft["draft_id"]
        ]

        loaded = client.get(f"/api/project-harvest/drafts/{draft['draft_id']}")
        assert loaded.status_code == 200
        assert loaded.json() == draft


def test_import_rejects_invalid_bsx_with_structured_detail(
    monkeypatch, tmp_path: Path
) -> None:
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/project-harvest/drafts/bsx",
            params={"set_number": "21369", "filename": "bad.bsx"},
            content=b"not xml",
        )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_XML"


def test_import_rejects_declared_oversize_before_parsing(
    monkeypatch, tmp_path: Path
) -> None:
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/project-harvest/drafts/bsx",
            params={"set_number": "21369", "filename": "large.bsx"},
            content=b"small",
            headers={"content-length": str(project_harvest.MAX_BSX_BYTES + 1)},
        )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "FILE_TOO_LARGE"


def test_load_rejects_invalid_id_and_reports_missing_draft(
    monkeypatch, tmp_path: Path
) -> None:
    with _client(monkeypatch, tmp_path) as client:
        invalid = client.get("/api/project-harvest/drafts/not-valid")
        missing = client.get("/api/project-harvest/drafts/21369-0123456789abcdef")

    assert invalid.status_code == 400
    assert invalid.json()["detail"]["code"] == "INVALID_DRAFT_ID"
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "DRAFT_NOT_FOUND"


def test_locabriques_lookup_contract_and_invalid_set(
    monkeypatch, tmp_path: Path
) -> None:
    def fake_lookup(set_number: str) -> dict:
        return {
            "set_number": set_number,
            "status": "unavailable",
            "message": "source timed out",
            "fallback": {"requires_manual_download": True},
        }

    monkeypatch.setattr(project_harvest, "lookup_locabriques", fake_lookup)
    with _client(monkeypatch, tmp_path) as client:
        unavailable = client.get("/api/project-harvest/sources/locabriques/21369")

    assert unavailable.status_code == 200
    assert unavailable.json()["status"] == "unavailable"
    assert unavailable.json()["error"] == "source timed out"

    # Restore the real validator for the route-level 400 contract.
    monkeypatch.undo()
    monkeypatch.setattr(
        shared_state,
        "gc_ref",
        SimpleNamespace(project_harvest_dir=str(tmp_path / "harvest")),
    )
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        invalid = client.get("/api/project-harvest/sources/locabriques/not-a-set")
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["code"] == "INVALID_SET_NUMBER"


def test_locabriques_draft_endpoint_is_inert_and_maps_upstream_failures(
    monkeypatch, tmp_path: Path
) -> None:
    def fake_save(directory: Path, *, set_number: str) -> dict:
        assert directory == tmp_path / "harvest"
        return {
            "schema_version": 1,
            "parser_version": project_harvest.PARSER_VERSION,
            "draft_id": "10294-1-0123456789abcdef",
            "created_at": "2026-08-30T00:00:00+00:00",
            "set_number": "10294-1",
            "manifest_sha256": "0" * 64,
            "source": {
                "provider": "locabriques_api",
                "filename": "locabriques-10294-1-inventory-22.json",
                "sha256": "0" * 64,
                "size_bytes": 100,
            },
            "validation": {
                "structural_status": "valid",
                "bom_status": "unverified",
                "activation_status": "blocked",
                "activation_allowed": False,
                "issues": [],
            },
            "summary": {"numbered_bag_count": 47, "source_total_quantity": 9092},
            "groups": [],
        }

    monkeypatch.setattr(project_harvest, "save_locabriques_draft", fake_save)
    with _client(monkeypatch, tmp_path) as client:
        response = client.post("/api/project-harvest/drafts/locabriques/10294")
    assert response.status_code == 200
    assert response.json()["source"]["source_kind"] == "locabriques_api"
    assert response.json()["validation"]["activation_allowed"] is False

    def fake_timeout(directory: Path, *, set_number: str) -> dict:
        raise project_harvest.HarvestImportError(
            "LOCABRIQUES_DETAIL_TIMEOUT",
            "source timed out",
        )

    monkeypatch.setattr(project_harvest, "save_locabriques_draft", fake_timeout)
    with _client(monkeypatch, tmp_path) as client:
        timeout = client.post("/api/project-harvest/drafts/locabriques/10294")
    assert timeout.status_code == 504
    assert timeout.json()["detail"]["code"] == "LOCABRIQUES_DETAIL_TIMEOUT"
