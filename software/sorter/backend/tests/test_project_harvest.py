from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import requests

import project_harvest as harvest

REAL_SAMPLE = Path.home() / "Downloads" / "lego-21369.bsx"
REAL_SAMPLE_SHA256 = "2B107A7F0A50B1E7DE6BC15D22A0BE71E907063D7922A564A669A0D5839E096D"


def _item_xml(
    item_id: str,
    *,
    color_id: str = "1",
    quantity: str = "1",
    remarks: str = "Bag 1",
    item_type: str = "P",
    condition: str = "N",
    status: str | None = None,
    extra_field: str = "",
) -> str:
    status_xml = f"<Status>{status}</Status>" if status is not None else ""
    return (
        "<Item>"
        f"<ItemID>{item_id}</ItemID>"
        f"<ItemTypeID>{item_type}</ItemTypeID>"
        f"<ColorID>{color_id}</ColorID>"
        f"<Qty>{quantity}</Qty>"
        f"<Condition>{condition}</Condition>"
        f"{status_xml}"
        f"<Remarks>{remarks}</Remarks>"
        f"{extra_field}"
        "</Item>"
    )


def _bsx(*items: str, comment: str = "", doctype: bool = True) -> bytes:
    declaration = '<?xml version="1.0" encoding="UTF-8"?>'
    doctype_xml = "<!DOCTYPE BrickStoreXML>" if doctype else ""
    return (
        f"{declaration}{doctype_xml}<BrickStoreXML>{comment}<Inventory>"
        f"{''.join(items)}</Inventory></BrickStoreXML>"
    ).encode()


def _issue_codes(plan: dict[str, Any]) -> set[str]:
    return {issue["code"] for issue in plan["validation"]["issues"]}


def _assert_import_error(code: str, content: bytes) -> None:
    with pytest.raises(harvest.HarvestImportError) as exc_info:
        harvest.parse_bricksperbag_bsx(
            content, set_number="21369", filename="lego-21369.bsx"
        )
    assert exc_info.value.code == code


@pytest.mark.skipif(
    not REAL_SAMPLE.exists(),
    reason="local Bricks Per Bag acceptance sample is unavailable",
)
def test_real_21369_sample_acceptance() -> None:
    content = REAL_SAMPLE.read_bytes()
    plan = harvest.parse_bricksperbag_bsx(
        content,
        set_number="21369",
        filename=REAL_SAMPLE.name,
    )

    assert hashlib.sha256(content).hexdigest().upper() == REAL_SAMPLE_SHA256
    assert plan["source"]["sha256"].upper() == REAL_SAMPLE_SHA256
    assert plan["summary"] == {
        "source_row_count": 530,
        "source_total_quantity": 1477,
        "numbered_bag_count": 12,
        "numbered_quantity": 1475,
        "non_numbered_group_count": 1,
        "non_numbered_quantity": 2,
        "unique_item_ids": 248,
        "unique_part_color_pairs": 418,
        "duplicate_rows_merged": 0,
        "required_numbered_bins": 12,
        "additional_groups_requiring_policy": 1,
        "comments_total": 0,
        "skipped_comment_count": 0,
        "group_kinds": {
            "numbered": {"group_count": 12, "source_row_count": 529, "quantity": 1475},
            "unnumbered": {"group_count": 0, "source_row_count": 0, "quantity": 0},
            "extras": {"group_count": 1, "source_row_count": 1, "quantity": 2},
            "unassigned": {"group_count": 0, "source_row_count": 0, "quantity": 0},
        },
    }
    assert [group["quantity"] for group in plan["groups"]] == [
        108,
        104,
        114,
        126,
        103,
        159,
        97,
        183,
        155,
        86,
        139,
        101,
        2,
    ]
    assert plan["groups"][-1]["id"] == "extras"
    assert plan["groups"][-1]["parts"] == [
        {
            "item_type": "P",
            "item_id": "1749",
            "color_id": "12",
            "quantity": 2,
            "condition": "N",
            "status": "E",
            "source_rows": [530],
        }
    ]
    assert plan["validation"]["status"] == "unverified"
    assert plan["validation"]["activation_allowed"] is False
    assert len(plan["manifest_sha256"]) == 64
    assert plan == harvest.parse_bricksperbag_bsx(
        content,
        set_number="21369",
        filename=REAL_SAMPLE.name,
    )


def test_duplicate_rows_merge_only_inside_the_same_source_group() -> None:
    plan = harvest.parse_bricksperbag_bsx(
        _bsx(
            _item_xml("3001", quantity="1", remarks="Bag 1"),
            _item_xml("3001", quantity="2", remarks="Bag 1"),
            _item_xml("3001", quantity="4", remarks="Bag 2"),
        ),
        set_number="21369",
        filename="lego-21369.bsx",
    )

    assert [group["id"] for group in plan["groups"]] == ["bag:1", "bag:2"]
    assert plan["groups"][0]["parts"][0]["quantity"] == 3
    assert plan["groups"][0]["parts"][0]["source_rows"] == [1, 2]
    assert plan["groups"][1]["parts"][0]["quantity"] == 4
    assert plan["summary"]["source_row_count"] == 3
    assert plan["summary"]["duplicate_rows_merged"] == 1
    assert plan["summary"]["unique_part_color_pairs"] == 1


def test_bag_and_group_labels_keep_distinct_identity_and_block_activation() -> None:
    plan = harvest.parse_bricksperbag_bsx(
        _bsx(
            _item_xml("3001", remarks="Bag 1"),
            _item_xml("3002", remarks="Group 1"),
        ),
        set_number="21369",
    )

    assert [group["id"] for group in plan["groups"]] == ["bag:1", "group:1"]
    assert plan["summary"]["required_numbered_bins"] == 2
    assert "MIXED_NUMBERED_GROUP_SCHEMES" in _issue_codes(plan)
    assert plan["validation"]["status"] == "incomplete"
    assert plan["validation"]["activation_status"] == "blocked"
    assert plan["validation"]["activation_allowed"] is False


def test_group_labels_normalize_whitespace_without_changing_source_identity() -> None:
    plan = harvest.parse_bricksperbag_bsx(
        _bsx(_item_xml("3001", remarks="  Bag   12  ")),
        set_number="21369",
    )
    assert plan["groups"][0]["id"] == "bag:12"
    assert plan["groups"][0]["source_labels"] == ["Bag 12"]


def test_unrecognized_label_is_preserved_as_unassigned_and_blocks_activation() -> None:
    plan = harvest.parse_bricksperbag_bsx(
        _bsx(_item_xml("3001", remarks="Instruction step 7")),
        set_number="21369",
    )
    assert plan["groups"][0]["id"] == "unassigned"
    assert plan["groups"][0]["source_labels"] == ["Instruction step 7"]
    assert plan["summary"]["group_kinds"]["unassigned"]["quantity"] == 1
    assert "UNRECOGNIZED_BAG_LABEL" in _issue_codes(plan)
    assert plan["validation"]["activation_allowed"] is False


def test_source_reported_skipped_comment_is_structured_and_blocking() -> None:
    comment = "<!-- 2 part(s) not included (no BrickLink mapping yet): 123, 456 -->"
    plan = harvest.parse_bricksperbag_bsx(
        _bsx(_item_xml("3001"), comment=comment),
        set_number="21369",
    )
    issue = next(
        issue
        for issue in plan["validation"]["issues"]
        if issue["code"] == "SOURCE_REPORTED_SKIPPED_ITEM"
    )
    assert issue["severity"] == "error"
    assert issue["details"]["skipped_part_ids"] == ["123", "456"]
    assert plan["summary"]["comments_total"] == 1
    assert plan["summary"]["skipped_comment_count"] == 1
    assert plan["validation"]["status"] == "incomplete"
    assert plan["validation"]["activation_allowed"] is False


@pytest.mark.parametrize(
    ("code", "content"),
    [
        (
            "UNSAFE_XML",
            b'<?xml version="1.0"?><!DOCTYPE BrickStoreXML SYSTEM "https://evil.test/x"><BrickStoreXML><Inventory/></BrickStoreXML>',
        ),
        (
            "UNSAFE_XML",
            b'<?xml version="1.0"?><!DOCTYPE BrickStoreXML [<!ENTITY x "boom">]><BrickStoreXML><Inventory/></BrickStoreXML>',
        ),
        (
            "UNSAFE_XML",
            b'<?xml version="1.0"?><?harvest run?><BrickStoreXML><Inventory/></BrickStoreXML>',
        ),
        (
            "UNSAFE_XML",
            b'<?xml version="1.0"?><BrickStoreXML xmlns="urn:evil"><Inventory/></BrickStoreXML>',
        ),
        (
            "UNSAFE_XML",
            b'<?xml version="1.0"?><BrickStoreXML><Inventory><![CDATA[bad]]></Inventory></BrickStoreXML>',
        ),
        (
            "INVALID_INVENTORY",
            b'<?xml version="1.0"?><BrickStoreXML><Inventory/><Other/></BrickStoreXML>',
        ),
        (
            "UNEXPECTED_ELEMENT",
            b'<?xml version="1.0"?><BrickStoreXML><Inventory><Folder/></Inventory></BrickStoreXML>',
        ),
        (
            "UNEXPECTED_ITEM_FIELD",
            _bsx(_item_xml("3001", extra_field="<Price>1.00</Price>")),
        ),
    ],
)
def test_rejects_unsafe_namespaced_or_unknown_xml_structure(
    code: str, content: bytes
) -> None:
    _assert_import_error(code, content)


@pytest.mark.parametrize("quantity", ["0", "-1", "1.5", "1e3", ""])
def test_quantity_must_be_a_strictly_positive_integer(quantity: str) -> None:
    _assert_import_error(
        "INVALID_QUANTITY" if quantity != "" else "MISSING_FIELD",
        _bsx(_item_xml("3001", quantity=quantity)),
    )


def test_enforces_file_row_and_total_quantity_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _bsx(_item_xml("3001"))
    monkeypatch.setattr(harvest, "MAX_BSX_BYTES", len(content) - 1)
    _assert_import_error("FILE_TOO_LARGE", content)

    monkeypatch.setattr(harvest, "MAX_BSX_BYTES", 16 * 1024 * 1024)
    monkeypatch.setattr(harvest, "MAX_ITEM_ROWS", 1)
    _assert_import_error("TOO_MANY_ITEMS", _bsx(_item_xml("3001"), _item_xml("3002")))

    monkeypatch.setattr(harvest, "MAX_ITEM_ROWS", 100_000)
    monkeypatch.setattr(harvest, "MAX_TOTAL_QUANTITY", 2)
    _assert_import_error("QUANTITY_LIMIT", _bsx(_item_xml("3001", quantity="3")))


def test_normalized_manifest_hash_is_deterministic_and_covers_plan_data() -> None:
    content = _bsx(_item_xml("3001", quantity="2"))
    first = harvest.parse_bricksperbag_bsx(
        content, set_number="21369", filename="lego-21369.bsx"
    )
    second = harvest.parse_bricksperbag_bsx(
        content, set_number="21369", filename="lego-21369.bsx"
    )
    assert first == second
    assert first["manifest_sha256"] == harvest._calculate_manifest_sha256(first)

    changed = harvest.parse_bricksperbag_bsx(
        _bsx(_item_xml("3001", quantity="3")),
        set_number="21369",
        filename="lego-21369.bsx",
    )
    assert changed["manifest_sha256"] != first["manifest_sha256"]


def test_save_is_idempotent_and_load_verifies_the_immutable_draft(
    tmp_path: Path,
) -> None:
    content = _bsx(_item_xml("3001", quantity="2"))
    first = harvest.save_bsx_draft(
        tmp_path,
        set_number="21369",
        filename="lego-21369.bsx",
        content=content,
    )
    second = harvest.save_bsx_draft(
        tmp_path,
        set_number="21369",
        filename="renamed-download.bsx",
        content=content,
    )

    assert second == first
    assert second["source"]["filename"] == "lego-21369.bsx"
    assert harvest.load_bsx_draft(tmp_path, first["draft_id"]) == first
    assert harvest.list_bsx_drafts(tmp_path) == [first]
    assert (tmp_path / first["source"]["stored_filename"]).read_bytes() == content


def test_load_rejects_manifest_tampering_and_idempotent_save_does_not_hide_it(
    tmp_path: Path,
) -> None:
    content = _bsx(_item_xml("3001"))
    draft = harvest.save_bsx_draft(
        tmp_path,
        set_number="21369",
        filename="lego-21369.bsx",
        content=content,
    )
    manifest_path = tmp_path / f"{draft['draft_id']}.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["groups"][0]["quantity"] = 999
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(harvest.HarvestImportError) as exc_info:
        harvest.load_bsx_draft(tmp_path, draft["draft_id"])
    assert exc_info.value.code == "DRAFT_MANIFEST_HASH_MISMATCH"

    with pytest.raises(harvest.HarvestImportError) as exc_info:
        harvest.save_bsx_draft(
            tmp_path,
            set_number="21369",
            filename="lego-21369.bsx",
            content=content,
        )
    assert exc_info.value.code == "DRAFT_MANIFEST_HASH_MISMATCH"


def test_load_rejects_source_hash_tampering(tmp_path: Path) -> None:
    draft = harvest.save_bsx_draft(
        tmp_path,
        set_number="21369",
        filename="lego-21369.bsx",
        content=_bsx(_item_xml("3001")),
    )
    source_path = tmp_path / draft["source"]["stored_filename"]
    source_path.write_bytes(_bsx(_item_xml("3002")))

    with pytest.raises(harvest.HarvestImportError) as exc_info:
        harvest.load_bsx_draft(tmp_path, draft["draft_id"])
    assert exc_info.value.code == "DRAFT_SOURCE_HASH_MISMATCH"


def test_load_rejects_a_rehashed_manifest_that_disagrees_with_its_source(
    tmp_path: Path,
) -> None:
    draft = harvest.save_bsx_draft(
        tmp_path,
        set_number="21369",
        filename="lego-21369.bsx",
        content=_bsx(_item_xml("3001")),
    )
    manifest_path = tmp_path / f"{draft['draft_id']}.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["groups"][0]["quantity"] = 999
    manifest["manifest_sha256"] = harvest._calculate_manifest_sha256(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(harvest.HarvestImportError) as exc_info:
        harvest.load_bsx_draft(tmp_path, draft["draft_id"])
    assert exc_info.value.code == "DRAFT_MANIFEST_SOURCE_MISMATCH"


class _FakeResponse:
    def __init__(
        self, payload: Any, *, status_code: int = 200, invalid_json: bool = False
    ):
        self.payload = payload
        self.status_code = status_code
        self.invalid_json = invalid_json
        self.content = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode()
        self.headers = {"content-length": str(len(self.content))}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def json(self) -> Any:
        if self.invalid_json:
            raise ValueError("invalid upstream JSON")
        return self.payload

    def iter_content(self, chunk_size: int) -> Any:
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]


def _loca_entry(set_num: str, inventory_id: int = 22) -> dict[str, Any]:
    return {
        "id": inventory_id,
        "set": {
            "set_num": set_num,
            "name": "Example",
            "year": 2026,
            "img_url": "image",
        },
        "bag_count": 12,
        "total_part_count": 1477,
        "url": f"/api/inventories/{inventory_id}/",
        "website_url": f"/inventories/{inventory_id}/",
    }


def _loca_detail(set_num: str = "10294-1") -> dict[str, Any]:
    return {
        "url": "https://locabriques.fr/api/inventories/22/",
        "set": {"set_num": set_num, "name": "Example", "year": 2026},
        "total_part_count": 10,
        "bag_count": 2,
        "author_username": "example-author",
        "external_source_url": None,
        "website_url": "https://locabriques.fr/inventaires/example/",
        "published_on": "2026-08-01T00:00:00Z",
        "bag_list": [
            {
                "bag_number": "2",
                "content": {"15": {"3001": 2}, "1": {"3002": 3}},
            },
            {
                "bag_number": "1",
                "content": {"15": {"3001": 4}, "1": {"3003": 1}},
            },
        ],
    }


def test_locabriques_detail_normalizes_to_an_inert_draft_plan() -> None:
    response = _FakeResponse(_loca_detail())
    plan = harvest.parse_locabriques_inventory(
        response.content,
        set_number="10294",
        source_url="https://locabriques.fr/api/inventories/22/",
    )

    assert plan["set_number"] == "10294-1"
    assert plan["source"]["provider"] == "locabriques_api"
    assert plan["source"]["identifier_namespace"] == {
        "part": "rebrickable_part_number",
        "color": "rebrickable_color_id",
    }
    assert plan["summary"]["source_total_quantity"] == 10
    assert plan["summary"]["numbered_bag_count"] == 2
    assert [group["id"] for group in plan["groups"]] == ["bag-1", "bag-2"]
    assert plan["validation"]["structural_status"] == "valid"
    assert plan["validation"]["bom_status"] == "unverified"
    assert plan["validation"]["activation_allowed"] is False


def test_locabriques_zero_quantity_placeholders_are_audited_and_ignored() -> None:
    detail = _loca_detail()
    detail["bag_list"][0]["content"]["25"] = {"96874": 0}
    plan = harvest.parse_locabriques_inventory(
        _FakeResponse(detail).content,
        set_number="10294",
        source_url="https://locabriques.fr/api/inventories/22/",
    )

    assert plan["validation"]["structural_status"] == "valid"
    assert plan["summary"]["source_total_quantity"] == 10
    assert plan["summary"]["ignored_zero_quantity_rows"] == 1
    assert "LOCABRIQUES_ZERO_QUANTITY_IGNORED" in _issue_codes(plan)
    assert all(
        part["item_id"] != "96874"
        for group in plan["groups"]
        for part in group["parts"]
    )


@pytest.mark.parametrize("invalid_quantity", [-1, True, 1.5, "0"])
def test_locabriques_invalid_quantities_remain_blocked(
    invalid_quantity: Any,
) -> None:
    detail = _loca_detail()
    detail["bag_list"][0]["content"]["25"] = {"96874": invalid_quantity}

    with pytest.raises(harvest.HarvestImportError) as exc_info:
        harvest.parse_locabriques_inventory(
            _FakeResponse(detail).content,
            set_number="10294",
            source_url="https://locabriques.fr/api/inventories/22/",
        )
    assert exc_info.value.code == "LOCABRIQUES_INVALID_QUANTITY"


def test_locabriques_detail_is_saved_idempotently_and_verified(tmp_path: Path) -> None:
    detail = _loca_detail()

    def fake_get(url: str, **kwargs: Any) -> _FakeResponse:
        assert kwargs["headers"]["Accept-Language"] == "en"
        if url == "https://locabriques.fr/api/inventories/":
            return _FakeResponse({"results": [_loca_entry("10294-1")]})
        assert url == "https://locabriques.fr/api/inventories/22/"
        return _FakeResponse(detail)

    draft = harvest.save_locabriques_draft(tmp_path, set_number="10294", get=fake_get)
    repeated = harvest.save_locabriques_draft(
        tmp_path, set_number="10294", get=fake_get
    )

    assert repeated == draft
    assert draft["draft_id"].startswith("10294-1-")
    assert draft["source"]["stored_filename"].endswith(".locabriques.json")
    assert harvest.load_bsx_draft(tmp_path, draft["draft_id"]) == draft
    assert [item["draft_id"] for item in harvest.list_bsx_drafts(tmp_path)] == [
        draft["draft_id"]
    ]

    source_path = tmp_path / draft["source"]["stored_filename"]
    source_path.write_bytes(source_path.read_bytes() + b" ")
    with pytest.raises(harvest.HarvestImportError) as exc_info:
        harvest.load_bsx_draft(tmp_path, draft["draft_id"])
    assert exc_info.value.code == "DRAFT_SOURCE_HASH_MISMATCH"


def test_locabriques_detail_blocks_source_disagreement_and_untrusted_urls() -> None:
    detail = _loca_detail()
    detail["total_part_count"] = 11
    response = _FakeResponse(detail)
    plan = harvest.parse_locabriques_inventory(
        response.content,
        set_number="10294-1",
        source_url="https://locabriques.fr/api/inventories/22/",
    )
    assert "LOCABRIQUES_TOTAL_MISMATCH" in _issue_codes(plan)
    assert plan["validation"]["structural_status"] == "blocked"

    with pytest.raises(harvest.HarvestImportError) as exc_info:
        harvest.parse_locabriques_inventory(
            response.content,
            set_number="10294-1",
            source_url="https://example.com/api/inventories/22/",
        )
    assert exc_info.value.code == "LOCABRIQUES_INVALID_DETAIL_URL"


def test_locabriques_found_status_is_structured() -> None:
    def fake_get(*args: Any, **kwargs: Any) -> _FakeResponse:
        assert kwargs["headers"]["Accept-Language"] == "en"
        return _FakeResponse({"results": [_loca_entry("21369-1")]})

    result = harvest.lookup_locabriques(
        "21369-1",
        get=fake_get,
    )
    assert result["status"] == "found"
    assert result["code"] == "LOCABRIQUES_FOUND"
    assert result["retryable"] is False
    assert result["inventory"]["inventory_id"] == 22
    assert result["inventory"]["set_num"] == "21369-1"
    assert result["fallback"]["requires_manual_download"] is True


def test_locabriques_not_found_and_ambiguous_statuses_are_structured() -> None:
    not_found = harvest.lookup_locabriques(
        "21369",
        get=lambda *args, **kwargs: _FakeResponse(
            {"results": [_loca_entry("99999-1")]}
        ),
    )
    assert (not_found["status"], not_found["code"], not_found["retryable"]) == (
        "not_found",
        "LOCABRIQUES_NOT_FOUND",
        False,
    )

    ambiguous = harvest.lookup_locabriques(
        "21369",
        get=lambda *args, **kwargs: _FakeResponse(
            {"results": [_loca_entry("21369-2", 2), _loca_entry("21369-1", 1)]}
        ),
    )
    assert ambiguous["status"] == "ambiguous"
    assert ambiguous["code"] == "LOCABRIQUES_AMBIGUOUS"
    assert [candidate["set_num"] for candidate in ambiguous["candidates"]] == [
        "21369-1",
        "21369-2",
    ]


@pytest.mark.parametrize(
    ("get", "expected_code", "expected_retryable"),
    [
        (
            lambda *args, **kwargs: (_ for _ in ()).throw(requests.Timeout()),
            "LOCABRIQUES_TIMEOUT",
            True,
        ),
        (
            lambda *args, **kwargs: _FakeResponse({}, status_code=503),
            "LOCABRIQUES_HTTP_ERROR",
            True,
        ),
        (
            lambda *args, **kwargs: _FakeResponse({}, invalid_json=True),
            "LOCABRIQUES_INVALID_JSON",
            True,
        ),
        (
            lambda *args, **kwargs: _FakeResponse({"unexpected": []}),
            "LOCABRIQUES_INVALID_RESPONSE",
            True,
        ),
    ],
)
def test_locabriques_unavailable_statuses_are_structured(
    get: Any,
    expected_code: str,
    expected_retryable: bool,
) -> None:
    result = harvest.lookup_locabriques("21369", get=get)
    assert result["status"] == "unavailable"
    assert result["code"] == expected_code
    assert result["retryable"] is expected_retryable
    assert result["fallback"]["url"] == "https://bricksperbag.com/set/21369"
