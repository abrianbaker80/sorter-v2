from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
import threading
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException

PARSER_VERSION = 3
MAX_BSX_BYTES = 16 * 1024 * 1024
MAX_LOCABRIQUES_BYTES = 16 * 1024 * 1024
MAX_ITEM_ROWS = 100_000
MAX_TOTAL_QUANTITY = 1_000_000

_LOCABRIQUES_API_ORIGIN = "https://locabriques.fr"
_LOCABRIQUES_INVENTORIES_URL = "https://locabriques.fr/api/inventories/"
_LOCABRIQUES_ACCEPT_LANGUAGE = "en"
_SET_NUMBER_RE = re.compile(r"^[0-9]{3,7}(?:-[0-9]+)?$")
_DRAFT_ID_RE = re.compile(r"^[0-9a-z-]+-[0-9a-f]{16}$")
_BAG_RE = re.compile(r"^bag\s+([0-9]+)$", re.IGNORECASE)
_GROUP_RE = re.compile(r"^group\s+([0-9]+)$", re.IGNORECASE)
_EXTRA_RE = re.compile(r"^extra\s+parts$", re.IGNORECASE)
_XML_COMMENT_RE = re.compile(r"<!--(?P<text>.*?)-->", re.DOTALL)
_SKIPPED_COMMENT_HINT_RE = re.compile(
    r"\b(?:skip(?:ped)?|not\s+included|unmapped|no\s+BrickLink\s+mapping|not\s+found|unsupported)\b",
    re.IGNORECASE,
)
_SKIPPED_MAPPING_RE = re.compile(
    r"<!--\s*(?P<count>[0-9]+)\s+part\(s\)\s+not\s+included\s+"
    r"\(no\s+BrickLink\s+mapping\s+yet\):\s*(?P<ids>.*?)\s*-->",
    re.IGNORECASE | re.DOTALL,
)
_ALLOWED_ITEM_FIELDS = {
    "ItemID",
    "ItemTypeID",
    "ColorID",
    "Qty",
    "Condition",
    "Status",
    "Remarks",
}
_DRAFT_LOCK = threading.RLock()


class HarvestImportError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def normalize_set_number(value: str) -> str:
    normalized = (value or "").strip()
    if not _SET_NUMBER_RE.fullmatch(normalized):
        raise HarvestImportError(
            "INVALID_SET_NUMBER",
            "Set number must contain 3-7 digits with an optional variant suffix (for example 21369 or 21369-1).",
        )
    return normalized


def bricksperbag_set_url(set_number: str) -> str:
    normalized = normalize_set_number(set_number)
    return f"https://bricksperbag.com/set/{normalized.split('-', 1)[0]}"


def _issue(code: str, severity: str, message: str, **details: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"code": code, "severity": severity, "message": message}
    if details:
        result["details"] = details
    return result


def _safe_filename(filename: str | None) -> str:
    name = os.path.basename((filename or "").strip())
    return name[:255] if name else "project-harvest.bsx"


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _manifest_hash_payload(plan: dict[str, Any]) -> dict[str, Any]:
    """Return the immutable normalized plan represented by a draft manifest.

    Storage metadata is deliberately excluded.  This makes the manifest digest
    deterministic for the same source bytes, set binding, and parser version,
    while still allowing the first-save timestamp and on-disk filename to be
    recorded in the persisted draft.
    """

    payload = json.loads(json.dumps(plan, ensure_ascii=False))
    payload.pop("draft_id", None)
    payload.pop("created_at", None)
    payload.pop("manifest_sha256", None)
    source = payload.get("source")
    if isinstance(source, dict):
        source.pop("stored_filename", None)
    return payload


def _calculate_manifest_sha256(plan: dict[str, Any]) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(_manifest_hash_payload(plan))
    ).hexdigest()


def _normalize_locabriques_detail_url(value: str) -> str:
    candidate = (value or "").strip()
    if candidate.startswith("/"):
        candidate = f"{_LOCABRIQUES_API_ORIGIN}{candidate}"
    parsed = urlparse(candidate)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "locabriques.fr"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/api/inventories/[0-9]+/", parsed.path)
    ):
        raise HarvestImportError(
            "LOCABRIQUES_INVALID_DETAIL_URL",
            "LocaBriques returned an inventory URL outside its trusted public API.",
        )
    return candidate


def _validate_xml_envelope(content: bytes) -> None:
    if not content:
        raise HarvestImportError("EMPTY_FILE", "The BSX upload is empty.")
    if len(content) > MAX_BSX_BYTES:
        raise HarvestImportError(
            "FILE_TOO_LARGE",
            f"The BSX upload exceeds the {MAX_BSX_BYTES // (1024 * 1024)} MiB limit.",
        )
    if content.startswith((b"PK\x03\x04", b"\x1f\x8b")):
        raise HarvestImportError(
            "COMPRESSED_UPLOAD", "Upload the uncompressed .bsx file."
        )
    if b"\x00" in content:
        raise HarvestImportError(
            "INVALID_ENCODING", "Bricks Per Bag BSX files must be UTF-8 XML."
        )

    lowered = content.lower()
    if b"<!entity" in lowered or b"<![cdata[" in lowered or b"<xi:include" in lowered:
        raise HarvestImportError(
            "UNSAFE_XML", "XML entities, CDATA, and XInclude are not allowed."
        )
    if re.search(rb"\sxmlns(?::[a-z_][a-z0-9_.-]*)?\s*=", lowered):
        raise HarvestImportError(
            "UNSAFE_XML", "XML namespaces are not allowed in a Bricks Per Bag import."
        )
    if re.search(rb"<!doctype\s+[^>]*(?:system|public|\[)", lowered):
        raise HarvestImportError(
            "UNSAFE_XML", "External or internal DTD declarations are not allowed."
        )

    doctypes = re.findall(rb"<!doctype\s+[^>]+>", lowered)
    if len(doctypes) > 1 or (
        doctypes and not re.fullmatch(rb"<!doctype\s+brickstorexml\s*>", doctypes[0])
    ):
        raise HarvestImportError(
            "UNSAFE_XML", "Only the standard BrickStoreXML doctype is allowed."
        )

    processing_instructions = re.findall(rb"<\?\s*([a-z_][a-z0-9_.:-]*)", lowered)
    if (
        any(target != b"xml" for target in processing_instructions)
        or len(processing_instructions) > 1
    ):
        raise HarvestImportError(
            "UNSAFE_XML", "XML processing instructions are not allowed."
        )

    declaration = re.search(
        rb"<\?xml\s+[^?]*encoding\s*=\s*['\"]([^'\"]+)['\"]",
        content,
        re.IGNORECASE,
    )
    if declaration and declaration.group(1).strip().lower() not in {b"utf-8", b"utf8"}:
        raise HarvestImportError(
            "INVALID_ENCODING", "Bricks Per Bag BSX files must declare UTF-8 encoding."
        )


def _single_text(
    item: ET.Element, field: str, row_number: int, *, required: bool
) -> str:
    nodes = item.findall(field)
    if len(nodes) > 1:
        raise HarvestImportError(
            "DUPLICATE_FIELD",
            f"Item row {row_number} contains more than one {field} field.",
        )
    value = (nodes[0].text or "").strip() if nodes else ""
    if required and not value:
        raise HarvestImportError(
            "MISSING_FIELD", f"Item row {row_number} is missing {field}."
        )
    if any(ord(char) < 32 and char not in "\t\r\n" for char in value):
        raise HarvestImportError(
            "CONTROL_CHARACTER", f"Item row {row_number} contains control characters."
        )
    if len(value) > 255:
        raise HarvestImportError(
            "FIELD_TOO_LONG", f"Item row {row_number} has an overlong {field} value."
        )
    return value


def _validate_element_shape(element: ET.Element, *, location: str) -> None:
    if (
        not isinstance(element.tag, str)
        or element.tag.startswith("{")
        or ":" in element.tag
    ):
        raise HarvestImportError(
            "UNSAFE_XML", f"XML namespaces are not allowed at {location}."
        )
    if element.attrib:
        raise HarvestImportError(
            "UNEXPECTED_ATTRIBUTE", f"XML attributes are not allowed at {location}."
        )
    if element.tail and element.tail.strip():
        raise HarvestImportError(
            "UNEXPECTED_TEXT", f"Unexpected text follows {location}."
        )


def _normalized_group_label(value: str) -> str:
    return " ".join(value.split())


def _group_from_item(
    remarks: str,
    status: str,
    row_number: int,
) -> tuple[str, str, str | None, int | None, str]:
    remarks = _normalized_group_label(remarks)
    if status.upper() == "E":
        return "extras", "Extra / unnumbered", None, None, remarks or "Extra parts"

    bag_match = _BAG_RE.fullmatch(remarks)
    if bag_match:
        number = int(bag_match.group(1))
        if number == 0:
            return "unnumbered", "Bag 0 / unnumbered", "bag", 0, remarks
        return "numbered", f"Bag {number}", "bag", number, remarks

    group_match = _GROUP_RE.fullmatch(remarks)
    if group_match:
        number = int(group_match.group(1))
        if number == 0:
            return "unnumbered", "Group 0 / unnumbered", "group", 0, remarks
        return "numbered", f"Group {number}", "group", number, remarks

    if _EXTRA_RE.fullmatch(remarks):
        return "extras", "Extra / unnumbered", None, None, remarks

    return "unassigned", "Unassigned", None, None, remarks


def parse_bricksperbag_bsx(
    content: bytes, *, set_number: str, filename: str | None = None
) -> dict[str, Any]:
    normalized_set = normalize_set_number(set_number)
    safe_filename = _safe_filename(filename)
    _validate_xml_envelope(content)

    try:
        source_text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HarvestImportError(
            "INVALID_ENCODING", "Bricks Per Bag BSX files must be UTF-8 XML."
        ) from exc

    try:
        root = DefusedET.fromstring(content)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise HarvestImportError(
            "INVALID_XML", f"Could not parse BrickStore XML: {exc}"
        ) from exc

    if root.tag != "BrickStoreXML":
        raise HarvestImportError("INVALID_ROOT", "The XML root must be BrickStoreXML.")
    _validate_element_shape(root, location="BrickStoreXML")
    if root.text and root.text.strip():
        raise HarvestImportError(
            "UNEXPECTED_TEXT", "BrickStoreXML contains unexpected text."
        )
    inventories = root.findall("Inventory")
    if len(inventories) != 1 or len(list(root)) != 1:
        raise HarvestImportError(
            "INVALID_INVENTORY", "BrickStoreXML must contain exactly one Inventory."
        )
    inventory = inventories[0]
    _validate_element_shape(inventory, location="Inventory")
    if inventory.text and inventory.text.strip():
        raise HarvestImportError(
            "UNEXPECTED_TEXT", "Inventory contains unexpected text."
        )

    item_nodes = list(inventory)
    for child in item_nodes:
        _validate_element_shape(child, location="Inventory child")
        if child.tag != "Item":
            raise HarvestImportError(
                "UNEXPECTED_ELEMENT",
                f"Inventory contains unsupported element {child.tag!r}; only Item is allowed.",
            )

    issues: list[dict[str, Any]] = []
    comments = list(_XML_COMMENT_RE.finditer(source_text))
    skipped_matches = list(_SKIPPED_MAPPING_RE.finditer(source_text))
    skipped_part_ids: list[str] = []
    for match in skipped_matches:
        skipped_part_ids.extend(
            part.strip() for part in match.group("ids").split(",") if part.strip()
        )
    skipped_comments = [
        " ".join(match.group("text").split())[:500]
        for match in comments
        if _SKIPPED_COMMENT_HINT_RE.search(match.group("text"))
    ]
    if skipped_comments:
        issues.append(
            _issue(
                "SOURCE_REPORTED_SKIPPED_ITEM",
                "error",
                "The source reports skipped or unmapped parts; the bag plan is incomplete until reconciled.",
                skipped_comment_count=len(skipped_comments),
                skipped_comments=skipped_comments[:20],
                skipped_part_ids=skipped_part_ids[:100],
            )
        )

    groups: dict[str, dict[str, Any]] = {}
    row_count = 0
    source_total_quantity = 0
    duplicate_rows_merged = 0
    global_pairs: set[tuple[str, str]] = set()
    global_item_ids: set[str] = set()
    numbering_schemes: set[str] = set()
    unassigned_rows: list[int] = []
    extra_status_bag_rows: list[int] = []
    extra_label_without_status_rows: list[int] = []
    unexpected_statuses: dict[str, list[int]] = {}
    unexpected_conditions: dict[str, list[int]] = {}

    for row_count, item in enumerate(item_nodes, start=1):
        if row_count > MAX_ITEM_ROWS:
            raise HarvestImportError(
                "TOO_MANY_ITEMS",
                f"The BSX contains more than {MAX_ITEM_ROWS} item rows.",
            )

        if item.text and item.text.strip():
            raise HarvestImportError(
                "UNEXPECTED_TEXT", f"Item row {row_count} contains unexpected text."
            )
        for field_node in list(item):
            _validate_element_shape(field_node, location=f"Item row {row_count} field")
            if field_node.tag not in _ALLOWED_ITEM_FIELDS:
                raise HarvestImportError(
                    "UNEXPECTED_ITEM_FIELD",
                    f"Item row {row_count} contains unsupported field {field_node.tag!r}.",
                )
            if list(field_node):
                raise HarvestImportError(
                    "UNEXPECTED_ELEMENT",
                    f"Item row {row_count} field {field_node.tag} must contain text only.",
                )

        item_id = _single_text(item, "ItemID", row_count, required=True)
        item_type = _single_text(item, "ItemTypeID", row_count, required=True)
        color_text = _single_text(item, "ColorID", row_count, required=True)
        quantity_text = _single_text(item, "Qty", row_count, required=True)
        condition = _single_text(item, "Condition", row_count, required=False)
        status = _single_text(item, "Status", row_count, required=False)
        remarks = _single_text(item, "Remarks", row_count, required=True)

        if item_type != "P":
            raise HarvestImportError(
                "UNSUPPORTED_ITEM_TYPE",
                f"Item row {row_count} uses unsupported BrickLink item type {item_type!r}; only parts are sortable.",
            )
        if not re.fullmatch(r"[0-9A-Za-z._+:/-]+", item_id):
            raise HarvestImportError(
                "INVALID_ITEM_ID", f"Item row {row_count} has an invalid ItemID."
            )
        if not re.fullmatch(r"[0-9]+", color_text):
            raise HarvestImportError(
                "INVALID_COLOR_ID", f"Item row {row_count} has an invalid ColorID."
            )
        if not re.fullmatch(r"[0-9]+", quantity_text):
            raise HarvestImportError(
                "INVALID_QUANTITY", f"Item row {row_count} has a non-integer Qty."
            )

        color_number = int(color_text)
        if color_number > 2_147_483_647:
            raise HarvestImportError(
                "INVALID_COLOR_ID", f"Item row {row_count} ColorID is out of range."
            )

        quantity = int(quantity_text)
        if quantity <= 0:
            raise HarvestImportError(
                "INVALID_QUANTITY", f"Item row {row_count} Qty must be positive."
            )
        source_total_quantity += quantity
        if source_total_quantity > MAX_TOTAL_QUANTITY:
            raise HarvestImportError(
                "QUANTITY_LIMIT",
                f"The BSX total quantity exceeds {MAX_TOTAL_QUANTITY}.",
            )

        color_id = str(color_number)
        normalized_remarks = _normalized_group_label(remarks)
        normalized_status = status.upper()
        if normalized_status and normalized_status != "E":
            unexpected_statuses.setdefault(status, []).append(row_count)
        if condition and condition.upper() != "N":
            unexpected_conditions.setdefault(condition, []).append(row_count)
        if normalized_status == "E" and (
            _BAG_RE.fullmatch(normalized_remarks)
            or _GROUP_RE.fullmatch(normalized_remarks)
        ):
            extra_status_bag_rows.append(row_count)
        if normalized_status != "E" and _EXTRA_RE.fullmatch(normalized_remarks):
            extra_label_without_status_rows.append(row_count)

        kind, label, numbering_scheme, number, source_label = _group_from_item(
            normalized_remarks,
            normalized_status,
            row_count,
        )
        if numbering_scheme is not None:
            numbering_schemes.add(numbering_scheme)
        if kind == "unassigned":
            unassigned_rows.append(row_count)
        if kind == "numbered":
            group_key = f"{numbering_scheme}:{number}"
        elif kind == "unnumbered" and numbering_scheme is not None:
            group_key = f"{numbering_scheme}:0"
        else:
            group_key = kind
        group = groups.setdefault(
            group_key,
            {
                "id": group_key,
                "kind": kind,
                "numbering_scheme": numbering_scheme,
                "bag_number": number,
                "label": label,
                "source_labels": [],
                "source_row_count": 0,
                "quantity": 0,
                "parts_by_key": {},
            },
        )
        if source_label not in group["source_labels"]:
            group["source_labels"].append(source_label)
        group["source_row_count"] += 1
        group["quantity"] += quantity

        part_key = f"{item_type}|{item_id}|{color_id}"
        existing = group["parts_by_key"].get(part_key)
        if existing is not None:
            existing["quantity"] += quantity
            existing["source_rows"].append(row_count)
            duplicate_rows_merged += 1
        else:
            group["parts_by_key"][part_key] = {
                "item_type": item_type,
                "item_id": item_id,
                "color_id": color_id,
                "quantity": quantity,
                "condition": condition or None,
                "status": status or None,
                "source_rows": [row_count],
            }
        global_pairs.add((item_id, color_id))
        global_item_ids.add(item_id)

    if row_count == 0:
        raise HarvestImportError(
            "EMPTY_INVENTORY", "The BSX Inventory has no Item rows."
        )

    if duplicate_rows_merged:
        issues.append(
            _issue(
                "DUPLICATE_ROWS_MERGED",
                "info",
                "Duplicate rows within the same bag were merged by part and color.",
                merged_rows=duplicate_rows_merged,
            )
        )

    if len(numbering_schemes) > 1:
        issues.append(
            _issue(
                "MIXED_NUMBERED_GROUP_SCHEMES",
                "error",
                "The source mixes Bag N and Group N labels. They remain separate, but activation is blocked until the source is reconciled.",
                numbering_schemes=sorted(numbering_schemes),
            )
        )
    if unassigned_rows:
        issues.append(
            _issue(
                "UNRECOGNIZED_BAG_LABEL",
                "error",
                "Some item rows do not have a recognized Bag N, Group N, or Extra parts label.",
                source_rows=unassigned_rows[:100],
            )
        )
    if extra_status_bag_rows:
        issues.append(
            _issue(
                "EXTRA_STATUS_OVERRIDES_NUMBERED_LABEL",
                "warning",
                "Status E overrides a numbered source label; those rows are kept in Extras.",
                source_rows=extra_status_bag_rows[:100],
            )
        )
    if extra_label_without_status_rows:
        issues.append(
            _issue(
                "EXTRA_LABEL_WITHOUT_STATUS",
                "warning",
                "Rows labeled Extra parts do not carry BrickStore status E; the label was preserved as Extras.",
                source_rows=extra_label_without_status_rows[:100],
            )
        )
    if unexpected_statuses:
        issues.append(
            _issue(
                "UNEXPECTED_ITEM_STATUS",
                "warning",
                "The source contains item statuses other than E; status does not affect matching.",
                statuses={
                    key: rows[:100] for key, rows in sorted(unexpected_statuses.items())
                },
            )
        )
    if unexpected_conditions:
        issues.append(
            _issue(
                "UNEXPECTED_ITEM_CONDITION",
                "warning",
                "The source contains conditions other than N; condition does not affect part and color matching.",
                conditions={
                    key: rows[:100]
                    for key, rows in sorted(unexpected_conditions.items())
                },
            )
        )

    extra_group = groups.get("extras")
    if extra_group is not None:
        issues.append(
            _issue(
                "EXTRA_OR_UNNUMBERED_GROUP",
                "warning",
                "The source includes an Extra / unnumbered group. Confirm whether it needs a separate project bin.",
                quantity=extra_group["quantity"],
            )
        )
    unnumbered_groups = [
        group for group in groups.values() if group["kind"] == "unnumbered"
    ]
    if unnumbered_groups:
        issues.append(
            _issue(
                "BAG_ZERO_REQUIRES_REVIEW",
                "warning",
                "Bag 0 / unnumbered parts require an explicit routing policy.",
                group_ids=[group["id"] for group in unnumbered_groups],
            )
        )

    filename_number = re.search(
        r"(?<![0-9])([0-9]{3,7})(?:-[0-9]+)?(?![0-9])", safe_filename
    )
    if filename_number and filename_number.group(1) != normalized_set.split("-", 1)[0]:
        issues.append(
            _issue(
                "FILENAME_SET_MISMATCH",
                "warning",
                "The filename appears to reference a different set number. The filename is not trusted as identity.",
                filename=safe_filename,
            )
        )

    issues.extend(
        [
            _issue(
                "SET_IDENTITY_BOUND_EXTERNALLY",
                "warning",
                "BSX does not contain an authoritative set number; this upload is bound to the entered project set.",
            ),
            _issue(
                "AUTHORITATIVE_BOM_REQUIRED",
                "warning",
                "The bag totals must be reconciled against a frozen set or MOC bill of materials before activation.",
            ),
        ]
    )

    def group_sort_key(group: dict[str, Any]) -> tuple[int, int, str, str]:
        if group["kind"] == "numbered":
            return (
                0,
                int(group["bag_number"]),
                str(group.get("numbering_scheme") or ""),
                group["label"],
            )
        if group["kind"] == "unnumbered":
            return (1, 0, str(group.get("numbering_scheme") or ""), group["label"])
        if group["kind"] == "extras":
            return (2, 0, "", group["label"])
        return (3, 0, "", group["label"])

    normalized_groups: list[dict[str, Any]] = []
    for group in sorted(groups.values(), key=group_sort_key):
        parts = sorted(
            group["parts_by_key"].values(),
            key=lambda part: (part["item_id"], int(part["color_id"])),
        )
        normalized_groups.append(
            {
                key: value
                for key, value in {
                    **group,
                    "source_labels": sorted(group["source_labels"], key=str.casefold),
                    "distinct_elements": len(parts),
                    "parts": parts,
                }.items()
                if key != "parts_by_key"
            }
        )

    numbered_groups = [
        group for group in normalized_groups if group["kind"] == "numbered"
    ]
    numbered_quantity = sum(group["quantity"] for group in numbered_groups)
    non_numbered_quantity = source_total_quantity - numbered_quantity
    source_sha256 = hashlib.sha256(content).hexdigest()
    error_issues = [issue for issue in issues if issue["severity"] == "error"]
    group_kind_summary = {
        kind: {
            "group_count": sum(
                1 for group in normalized_groups if group["kind"] == kind
            ),
            "source_row_count": sum(
                group["source_row_count"]
                for group in normalized_groups
                if group["kind"] == kind
            ),
            "quantity": sum(
                group["quantity"]
                for group in normalized_groups
                if group["kind"] == kind
            ),
        }
        for kind in ("numbered", "unnumbered", "extras", "unassigned")
    }

    plan = {
        "schema_version": 1,
        "parser_version": PARSER_VERSION,
        "set_number": normalized_set,
        "source": {
            "provider": "bricksperbag_manual_bsx",
            "filename": safe_filename,
            "source_url": bricksperbag_set_url(normalized_set),
            "identifier_namespace": {
                "part": "bricklink_item_number",
                "color": "bricklink_color_id",
            },
            "sha256": source_sha256,
            "size_bytes": len(content),
        },
        "validation": {
            "status": "incomplete" if error_issues else "unverified",
            "structural_status": "blocked" if error_issues else "valid",
            "bom_status": "unverified",
            "activation_status": "blocked",
            "activation_allowed": False,
            "issues": issues,
        },
        "summary": {
            "source_row_count": row_count,
            "source_total_quantity": source_total_quantity,
            "numbered_bag_count": len(numbered_groups),
            "numbered_quantity": numbered_quantity,
            "non_numbered_group_count": len(normalized_groups) - len(numbered_groups),
            "non_numbered_quantity": non_numbered_quantity,
            "unique_item_ids": len(global_item_ids),
            "unique_part_color_pairs": len(global_pairs),
            "duplicate_rows_merged": duplicate_rows_merged,
            "required_numbered_bins": len(numbered_groups),
            "additional_groups_requiring_policy": len(normalized_groups)
            - len(numbered_groups),
            "comments_total": len(comments),
            "skipped_comment_count": len(skipped_comments),
            "group_kinds": group_kind_summary,
        },
        "groups": normalized_groups,
    }
    plan["manifest_sha256"] = _calculate_manifest_sha256(plan)
    return plan


def _json_object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HarvestImportError(
                "LOCABRIQUES_DUPLICATE_JSON_KEY",
                "LocaBriques returned JSON with a duplicate object key.",
            )
        result[key] = value
    return result


def _load_locabriques_json(content: bytes) -> dict[str, Any]:
    if not content:
        raise HarvestImportError(
            "LOCABRIQUES_EMPTY_RESPONSE", "LocaBriques returned an empty inventory."
        )
    if len(content) > MAX_LOCABRIQUES_BYTES:
        raise HarvestImportError(
            "LOCABRIQUES_RESPONSE_TOO_LARGE",
            f"The LocaBriques inventory exceeds the {MAX_LOCABRIQUES_BYTES // (1024 * 1024)} MiB limit.",
        )
    try:
        payload = json.loads(
            content.decode("utf-8"), object_pairs_hook=_json_object_without_duplicates
        )
    except HarvestImportError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarvestImportError(
            "LOCABRIQUES_INVALID_JSON",
            "LocaBriques returned inventory data that was not valid UTF-8 JSON.",
        ) from exc
    if not isinstance(payload, dict):
        raise HarvestImportError(
            "LOCABRIQUES_INVALID_RESPONSE",
            "LocaBriques returned an unexpected inventory response shape.",
        )
    return payload


def _locabriques_group_identity(
    label: str, index: int
) -> tuple[str, str, int | None, str | None]:
    normalized = label.strip()
    if normalized.isdigit():
        number = int(normalized)
        if number > 0:
            return f"bag-{number}", "numbered", number, "bag"
        return "bag-0", "unnumbered", 0, "bag"
    if _EXTRA_RE.fullmatch(normalized) or normalized.casefold() in {
        "extra",
        "extras",
        "spares",
    }:
        return "extras", "extras", None, None
    return f"unassigned-{index}", "unassigned", None, None


def parse_locabriques_inventory(
    content: bytes,
    *,
    set_number: str,
    source_url: str,
) -> dict[str, Any]:
    requested_set = normalize_set_number(set_number)
    normalized_source_url = _normalize_locabriques_detail_url(source_url)
    payload = _load_locabriques_json(content)

    set_info = payload.get("set")
    if not isinstance(set_info, dict):
        raise HarvestImportError(
            "LOCABRIQUES_INVALID_RESPONSE",
            "The LocaBriques inventory is missing set identity.",
        )
    source_set = normalize_set_number(str(set_info.get("set_num") or ""))
    requested_base = requested_set.split("-", 1)[0]
    source_base = source_set.split("-", 1)[0]
    if source_set != requested_set and (
        "-" in requested_set or requested_base != source_base
    ):
        raise HarvestImportError(
            "LOCABRIQUES_SET_MISMATCH",
            "The LocaBriques inventory does not match the requested set number.",
        )

    bags = payload.get("bag_list")
    if not isinstance(bags, list):
        raise HarvestImportError(
            "LOCABRIQUES_INVALID_RESPONSE",
            "The LocaBriques inventory is missing its bag list.",
        )

    groups: list[dict[str, Any]] = []
    seen_group_ids: set[str] = set()
    global_item_ids: set[str] = set()
    global_pairs: set[tuple[str, str]] = set()
    source_row_count = 0
    source_total_quantity = 0
    ignored_zero_quantity_rows = 0
    issues: list[dict[str, Any]] = []

    for bag_index, bag in enumerate(bags, start=1):
        if not isinstance(bag, dict):
            raise HarvestImportError(
                "LOCABRIQUES_INVALID_BAG",
                f"LocaBriques bag entry {bag_index} is not an object.",
            )
        label = str(bag.get("bag_number") or "").strip()
        if not label:
            raise HarvestImportError(
                "LOCABRIQUES_INVALID_BAG",
                f"LocaBriques bag entry {bag_index} has no bag number.",
            )
        group_id, kind, bag_number, numbering_scheme = _locabriques_group_identity(
            label, bag_index
        )
        if group_id in seen_group_ids:
            raise HarvestImportError(
                "LOCABRIQUES_DUPLICATE_BAG",
                f"LocaBriques returned the bag group {label!r} more than once.",
            )
        seen_group_ids.add(group_id)

        raw_content = bag.get("content")
        if not isinstance(raw_content, dict):
            raise HarvestImportError(
                "LOCABRIQUES_INVALID_BAG",
                f"LocaBriques bag {label!r} has no part content object.",
            )

        parts: list[dict[str, Any]] = []
        group_quantity = 0
        for raw_color_id, raw_parts in raw_content.items():
            color_id = str(raw_color_id).strip()
            if not re.fullmatch(r"-?[0-9]+", color_id) or not isinstance(
                raw_parts, dict
            ):
                raise HarvestImportError(
                    "LOCABRIQUES_INVALID_PART",
                    f"LocaBriques bag {label!r} contains an invalid color group.",
                )
            for raw_item_id, raw_quantity in raw_parts.items():
                item_id = str(raw_item_id).strip()
                if (
                    not item_id
                    or len(item_id) > 100
                    or not re.fullmatch(r"[0-9A-Za-z._-]+", item_id)
                ):
                    raise HarvestImportError(
                        "LOCABRIQUES_INVALID_PART",
                        f"LocaBriques bag {label!r} contains an invalid part identifier.",
                    )
                if (
                    isinstance(raw_quantity, bool)
                    or not isinstance(raw_quantity, int)
                    or raw_quantity < 0
                ):
                    raise HarvestImportError(
                        "LOCABRIQUES_INVALID_QUANTITY",
                        f"LocaBriques bag {label!r} contains a negative or non-integer quantity.",
                    )
                if raw_quantity == 0:
                    ignored_zero_quantity_rows += 1
                    issues.append(
                        _issue(
                            "LOCABRIQUES_ZERO_QUANTITY_IGNORED",
                            "info",
                            "A zero-quantity LocaBriques placeholder was ignored.",
                            group_id=group_id,
                            source_label=label,
                            item_id=item_id,
                            color_id=color_id,
                        )
                    )
                    continue
                source_row_count += 1
                source_total_quantity += raw_quantity
                group_quantity += raw_quantity
                if source_row_count > MAX_ITEM_ROWS:
                    raise HarvestImportError(
                        "TOO_MANY_ITEMS",
                        f"The LocaBriques inventory contains more than {MAX_ITEM_ROWS} part rows.",
                    )
                if source_total_quantity > MAX_TOTAL_QUANTITY:
                    raise HarvestImportError(
                        "QUANTITY_LIMIT",
                        f"The LocaBriques inventory exceeds {MAX_TOTAL_QUANTITY} pieces.",
                    )
                global_item_ids.add(item_id)
                global_pairs.add((item_id, color_id))
                parts.append(
                    {
                        "item_type": "P",
                        "item_id": item_id,
                        "color_id": color_id,
                        "quantity": raw_quantity,
                        "condition": "N",
                        "status": None,
                        "source_rows": [source_row_count],
                    }
                )

        parts.sort(key=lambda part: (part["item_id"], int(part["color_id"])))
        group: dict[str, Any] = {
            "id": group_id,
            "kind": kind,
            "label": f"Bag {bag_number}"
            if kind in {"numbered", "unnumbered"}
            else label,
            "source_labels": [label],
            "source_row_count": len(parts),
            "quantity": group_quantity,
            "distinct_elements": len(parts),
            "parts": parts,
        }
        if bag_number is not None:
            group["bag_number"] = bag_number
        if numbering_scheme is not None:
            group["numbering_scheme"] = numbering_scheme
        groups.append(group)

    def group_sort_key(group: dict[str, Any]) -> tuple[int, int, str]:
        order = {"numbered": 0, "unnumbered": 1, "extras": 2, "unassigned": 3}
        return (order[group["kind"]], int(group.get("bag_number") or 0), group["label"])

    groups.sort(key=group_sort_key)
    numbered_groups = [group for group in groups if group["kind"] == "numbered"]
    numbered_quantity = sum(group["quantity"] for group in numbered_groups)
    non_numbered_groups = [group for group in groups if group["kind"] != "numbered"]

    declared_bag_count = payload.get("bag_count")
    if isinstance(declared_bag_count, bool) or not isinstance(declared_bag_count, int):
        issues.append(
            _issue(
                "LOCABRIQUES_DECLARED_BAG_COUNT_INVALID",
                "error",
                "LocaBriques did not provide a valid declared bag count.",
            )
        )
    elif declared_bag_count != len(bags):
        issues.append(
            _issue(
                "LOCABRIQUES_BAG_COUNT_MISMATCH",
                "error",
                "The LocaBriques declared bag count does not match its bag list.",
                declared=declared_bag_count,
                parsed=len(bags),
            )
        )

    declared_total = payload.get("total_part_count")
    if isinstance(declared_total, bool) or not isinstance(declared_total, int):
        issues.append(
            _issue(
                "LOCABRIQUES_DECLARED_TOTAL_INVALID",
                "error",
                "LocaBriques did not provide a valid declared part total.",
            )
        )
    elif declared_total != source_total_quantity:
        issues.append(
            _issue(
                "LOCABRIQUES_TOTAL_MISMATCH",
                "error",
                "The LocaBriques declared part total does not match its bag contents.",
                declared=declared_total,
                parsed=source_total_quantity,
            )
        )

    for group in non_numbered_groups:
        issues.append(
            _issue(
                "NON_NUMBERED_GROUP_REQUIRES_REVIEW",
                "warning" if group["kind"] != "unassigned" else "error",
                "A non-numbered LocaBriques group requires an explicit project policy.",
                group_id=group["id"],
                source_label=group["source_labels"][0],
            )
        )
    issues.append(
        _issue(
            "AUTHORITATIVE_BOM_REQUIRED",
            "warning",
            "The LocaBriques bag totals must be reconciled against a frozen set bill of materials before activation.",
        )
    )

    parsed_url = urlparse(normalized_source_url)
    inventory_id = parsed_url.path.rstrip("/").rsplit("/", 1)[-1]
    source_sha256 = hashlib.sha256(content).hexdigest()
    error_issues = [issue for issue in issues if issue["severity"] == "error"]
    group_kind_summary = {
        kind: {
            "group_count": sum(1 for group in groups if group["kind"] == kind),
            "source_row_count": sum(
                group["source_row_count"] for group in groups if group["kind"] == kind
            ),
            "quantity": sum(
                group["quantity"] for group in groups if group["kind"] == kind
            ),
        }
        for kind in ("numbered", "unnumbered", "extras", "unassigned")
    }
    plan = {
        "schema_version": 1,
        "parser_version": PARSER_VERSION,
        "set_number": source_set,
        "source": {
            "provider": "locabriques_api",
            "filename": f"locabriques-{source_set}-inventory-{inventory_id}.json",
            "source_url": normalized_source_url,
            "website_url": payload.get("website_url"),
            "external_source_url": payload.get("external_source_url"),
            "published_on": payload.get("published_on"),
            "author_username": payload.get("author_username"),
            "identifier_namespace": {
                "part": "rebrickable_part_number",
                "color": "rebrickable_color_id",
            },
            "sha256": source_sha256,
            "size_bytes": len(content),
        },
        "validation": {
            "status": "incomplete" if error_issues else "unverified",
            "structural_status": "blocked" if error_issues else "valid",
            "bom_status": "unverified",
            "activation_status": "blocked",
            "activation_allowed": False,
            "issues": issues,
        },
        "summary": {
            "source_row_count": source_row_count,
            "source_total_quantity": source_total_quantity,
            "numbered_bag_count": len(numbered_groups),
            "numbered_quantity": numbered_quantity,
            "non_numbered_group_count": len(non_numbered_groups),
            "non_numbered_quantity": source_total_quantity - numbered_quantity,
            "unique_item_ids": len(global_item_ids),
            "unique_part_color_pairs": len(global_pairs),
            "duplicate_rows_merged": 0,
            "required_numbered_bins": len(numbered_groups),
            "additional_groups_requiring_policy": len(non_numbered_groups),
            "comments_total": 0,
            "skipped_comment_count": 0,
            "ignored_zero_quantity_rows": ignored_zero_quantity_rows,
            "group_kinds": group_kind_summary,
        },
        "groups": groups,
    }
    plan["manifest_sha256"] = _calculate_manifest_sha256(plan)
    return plan


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _save_parsed_draft(
    directory: str | Path,
    *,
    plan: dict[str, Any],
    content: bytes,
    source_suffix: str,
) -> dict[str, Any]:
    draft_id = f"{plan['set_number'].lower()}-{plan['source']['sha256'][:16]}"
    root = Path(directory)
    source_path = root / f"{draft_id}{source_suffix}"
    manifest_path = root / f"{draft_id}.json"

    with _DRAFT_LOCK:
        if manifest_path.exists():
            existing = load_bsx_draft(root, draft_id)
            if (
                existing.get("source", {}).get("sha256") != plan["source"]["sha256"]
                or existing.get("set_number") != plan["set_number"]
                or existing.get("source", {}).get("provider")
                != plan["source"]["provider"]
            ):
                raise HarvestImportError(
                    "DRAFT_COLLISION",
                    "An immutable Harvest draft id collision occurred.",
                )
            return existing

        if source_path.exists():
            if source_path.is_symlink():
                raise HarvestImportError(
                    "DRAFT_COLLISION",
                    "The immutable Harvest source path is not a regular file.",
                )
            existing_content = source_path.read_bytes()
            existing_hash = hashlib.sha256(existing_content).hexdigest()
            if existing_hash != plan["source"]["sha256"] or existing_content != content:
                raise HarvestImportError(
                    "DRAFT_COLLISION",
                    "An immutable Harvest source id collision occurred.",
                )
        else:
            _atomic_write(source_path, content)

        plan["draft_id"] = draft_id
        plan["created_at"] = datetime.now(UTC).isoformat()
        plan["source"]["stored_filename"] = source_path.name
        manifest = json.dumps(
            plan, indent=2, sort_keys=True, ensure_ascii=False
        ).encode("utf-8")
        _atomic_write(manifest_path, manifest)
        return load_bsx_draft(root, draft_id)


def save_bsx_draft(
    directory: str | Path,
    *,
    set_number: str,
    filename: str | None,
    content: bytes,
) -> dict[str, Any]:
    plan = parse_bricksperbag_bsx(content, set_number=set_number, filename=filename)
    return _save_parsed_draft(
        directory,
        plan=plan,
        content=content,
        source_suffix=".bsx",
    )


def _bounded_locabriques_response(response: Any) -> bytes:
    content_length = None
    headers = getattr(response, "headers", None)
    if headers is not None:
        content_length = headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except (TypeError, ValueError):
            declared_size = None
        if declared_size is not None and declared_size > MAX_LOCABRIQUES_BYTES:
            raise HarvestImportError(
                "LOCABRIQUES_RESPONSE_TOO_LARGE",
                f"The LocaBriques inventory exceeds the {MAX_LOCABRIQUES_BYTES // (1024 * 1024)} MiB limit.",
            )

    chunks: list[bytes] = []
    total = 0
    iterator = getattr(response, "iter_content", None)
    if callable(iterator):
        source_chunks = iterator(chunk_size=64 * 1024)
    else:
        source_chunks = [getattr(response, "content", b"")]
    for chunk in source_chunks:
        if not chunk:
            continue
        if not isinstance(chunk, bytes):
            raise HarvestImportError(
                "LOCABRIQUES_INVALID_RESPONSE",
                "LocaBriques returned an invalid response body.",
            )
        total += len(chunk)
        if total > MAX_LOCABRIQUES_BYTES:
            raise HarvestImportError(
                "LOCABRIQUES_RESPONSE_TOO_LARGE",
                f"The LocaBriques inventory exceeds the {MAX_LOCABRIQUES_BYTES // (1024 * 1024)} MiB limit.",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def save_locabriques_draft(
    directory: str | Path,
    *,
    set_number: str,
    get: Callable[..., Any] = requests.get,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    lookup = lookup_locabriques(set_number, get=get, timeout_seconds=timeout_seconds)
    if lookup.get("status") != "found":
        raise HarvestImportError(
            str(lookup.get("code") or "LOCABRIQUES_LOOKUP_FAILED"),
            str(
                lookup.get("message")
                or "LocaBriques did not return a unique published inventory."
            ),
        )
    inventory = lookup.get("inventory")
    detail_url = inventory.get("api_url") if isinstance(inventory, dict) else None
    if not isinstance(detail_url, str):
        raise HarvestImportError(
            "LOCABRIQUES_INVALID_RESPONSE",
            "LocaBriques did not provide a detail URL for the matched inventory.",
        )
    normalized_detail_url = _normalize_locabriques_detail_url(detail_url)

    try:
        response = get(
            normalized_detail_url,
            headers={
                "Accept": "application/json",
                "Accept-Language": _LOCABRIQUES_ACCEPT_LANGUAGE,
                "User-Agent": "SorterV2-ProjectHarvest/2",
            },
            timeout=timeout_seconds,
            stream=True,
        )
        response.raise_for_status()
        content = _bounded_locabriques_response(response)
    except requests.Timeout as exc:
        raise HarvestImportError(
            "LOCABRIQUES_DETAIL_TIMEOUT",
            "LocaBriques did not return the matched bag inventory before the timeout.",
        ) from exc
    except requests.RequestException as exc:
        raise HarvestImportError(
            "LOCABRIQUES_DETAIL_HTTP_ERROR",
            "LocaBriques could not provide the matched bag inventory.",
        ) from exc

    plan = parse_locabriques_inventory(
        content,
        set_number=set_number,
        source_url=normalized_detail_url,
    )
    return _save_parsed_draft(
        directory,
        plan=plan,
        content=content,
        source_suffix=".locabriques.json",
    )


def list_bsx_drafts(directory: str | Path) -> list[dict[str, Any]]:
    root = Path(directory)
    if not root.exists():
        return []
    drafts: list[dict[str, Any]] = []
    for path in root.glob("*.json"):
        if not _DRAFT_ID_RE.fullmatch(path.stem):
            continue
        try:
            draft = load_bsx_draft(root, path.stem)
        except (OSError, HarvestImportError):
            continue
        drafts.append(draft)
    drafts.sort(key=lambda draft: str(draft.get("created_at") or ""), reverse=True)
    return drafts


def load_bsx_draft(directory: str | Path, draft_id: str) -> dict[str, Any]:
    if not _DRAFT_ID_RE.fullmatch(draft_id):
        raise HarvestImportError("INVALID_DRAFT_ID", "Invalid Harvest draft id.")
    root = Path(directory)
    path = root / f"{draft_id}.json"
    if not path.exists():
        raise FileNotFoundError(draft_id)
    if path.is_symlink():
        raise HarvestImportError(
            "INVALID_DRAFT", "The Harvest draft manifest is not a regular file."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarvestImportError(
            "INVALID_DRAFT", "The Harvest draft manifest is unreadable."
        ) from exc
    if not isinstance(data, dict) or data.get("draft_id") != draft_id:
        raise HarvestImportError(
            "INVALID_DRAFT", "The Harvest draft manifest is invalid."
        )

    if data.get("schema_version") != 1 or data.get("parser_version") != PARSER_VERSION:
        raise HarvestImportError(
            "INVALID_DRAFT", "The Harvest draft uses an unsupported manifest version."
        )
    set_number = data.get("set_number")
    source = data.get("source")
    manifest_sha256 = data.get("manifest_sha256")
    if not isinstance(set_number, str) or not isinstance(source, dict):
        raise HarvestImportError(
            "INVALID_DRAFT", "The Harvest draft is missing source identity."
        )
    source_sha256 = source.get("sha256")
    source_filename = source.get("filename")
    if not isinstance(source_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", source_sha256
    ):
        raise HarvestImportError(
            "INVALID_DRAFT", "The Harvest draft source hash is invalid."
        )
    expected_draft_id = (
        f"{normalize_set_number(set_number).lower()}-{source_sha256[:16]}"
    )
    if expected_draft_id != draft_id:
        raise HarvestImportError(
            "INVALID_DRAFT",
            "The Harvest draft id does not match its immutable source identity.",
        )
    provider = source.get("provider")
    if provider == "bricksperbag_manual_bsx":
        expected_source_filename = f"{draft_id}.bsx"
    elif provider == "locabriques_api":
        expected_source_filename = f"{draft_id}.locabriques.json"
    else:
        raise HarvestImportError(
            "INVALID_DRAFT", "The Harvest draft source provider is unsupported."
        )
    if source.get("stored_filename") != expected_source_filename:
        raise HarvestImportError(
            "INVALID_DRAFT", "The Harvest draft source filename is invalid."
        )
    if not isinstance(source_filename, str):
        raise HarvestImportError(
            "INVALID_DRAFT", "The Harvest draft original filename is invalid."
        )
    if not isinstance(manifest_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", manifest_sha256
    ):
        raise HarvestImportError(
            "INVALID_DRAFT", "The Harvest normalized manifest hash is invalid."
        )
    calculated_manifest_hash = _calculate_manifest_sha256(data)
    if not hmac.compare_digest(manifest_sha256, calculated_manifest_hash):
        raise HarvestImportError(
            "DRAFT_MANIFEST_HASH_MISMATCH",
            "The immutable Harvest manifest has changed.",
        )

    source_path = root / expected_source_filename
    if not source_path.exists() or source_path.is_symlink():
        raise HarvestImportError(
            "DRAFT_SOURCE_MISSING", "The immutable Harvest source is missing."
        )
    try:
        source_content = source_path.read_bytes()
    except OSError as exc:
        raise HarvestImportError(
            "DRAFT_SOURCE_MISSING", "The immutable Harvest source is unreadable."
        ) from exc
    actual_source_hash = hashlib.sha256(source_content).hexdigest()
    if not hmac.compare_digest(source_sha256, actual_source_hash):
        raise HarvestImportError(
            "DRAFT_SOURCE_HASH_MISMATCH", "The immutable Harvest source has changed."
        )
    if source.get("size_bytes") != len(source_content):
        raise HarvestImportError(
            "DRAFT_SOURCE_HASH_MISMATCH",
            "The immutable Harvest source size has changed.",
        )

    if provider == "bricksperbag_manual_bsx":
        reparsed = parse_bricksperbag_bsx(
            source_content,
            set_number=set_number,
            filename=source_filename,
        )
    else:
        source_url = source.get("source_url")
        if not isinstance(source_url, str):
            raise HarvestImportError(
                "INVALID_DRAFT", "The Harvest draft source URL is invalid."
            )
        reparsed = parse_locabriques_inventory(
            source_content,
            set_number=set_number,
            source_url=source_url,
        )
    if not hmac.compare_digest(
        manifest_sha256, reparsed["manifest_sha256"]
    ) or _manifest_hash_payload(data) != _manifest_hash_payload(reparsed):
        raise HarvestImportError(
            "DRAFT_MANIFEST_SOURCE_MISMATCH",
            "The immutable Harvest manifest no longer matches its BSX source.",
        )
    return data


def _locabriques_fallback(normalized_set: str) -> dict[str, Any]:
    return {
        "provider": "bricksperbag_manual_bsx",
        "url": bricksperbag_set_url(normalized_set),
        "requires_manual_download": True,
    }


def _locabriques_status(
    normalized_set: str,
    *,
    status: str,
    code: str,
    message: str,
    retryable: bool,
    **details: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "set_number": normalized_set,
        "status": status,
        "code": code,
        "retryable": retryable,
        "message": message,
        "fallback": _locabriques_fallback(normalized_set),
    }
    result.update(details)
    return result


def _locabriques_candidate(entry: dict[str, Any]) -> dict[str, Any] | None:
    set_info = entry.get("set")
    if not isinstance(set_info, dict):
        return None
    set_num = str(set_info.get("set_num") or set_info.get("id") or "").strip()
    if not set_num:
        return None
    return {
        "inventory_id": entry.get("id"),
        "set_num": set_num,
        "name": set_info.get("name"),
        "year": set_info.get("year"),
        "image_url": set_info.get("img_url"),
        "bag_count": entry.get("bag_count"),
        "total_part_count": entry.get("total_part_count"),
        "api_url": entry.get("url"),
        "website_url": entry.get("website_url"),
    }


def lookup_locabriques(
    set_number: str,
    *,
    get: Callable[..., Any] = requests.get,
    timeout_seconds: float = 6.0,
) -> dict[str, Any]:
    normalized_set = normalize_set_number(set_number)
    try:
        response = get(
            _LOCABRIQUES_INVENTORIES_URL,
            params={"search": normalized_set, "page_size": 20},
            headers={
                "Accept": "application/json",
                "Accept-Language": _LOCABRIQUES_ACCEPT_LANGUAGE,
                "User-Agent": "SorterV2-ProjectHarvest/1",
            },
            timeout=timeout_seconds,
        )
    except requests.Timeout:
        return _locabriques_status(
            normalized_set,
            status="unavailable",
            code="LOCABRIQUES_TIMEOUT",
            message="LocaBriques did not respond before the lookup timeout.",
            retryable=True,
        )
    except requests.RequestException as exc:
        http_status = exc.response.status_code if exc.response is not None else None
        return _locabriques_status(
            normalized_set,
            status="unavailable",
            code="LOCABRIQUES_HTTP_ERROR"
            if http_status is not None
            else "LOCABRIQUES_NETWORK_ERROR",
            message="LocaBriques could not be reached."
            if http_status is None
            else "LocaBriques rejected the lookup request.",
            retryable=http_status is None or http_status >= 500 or http_status == 429,
            error={"http_status": http_status},
        )

    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        http_status = (
            exc.response.status_code
            if exc.response is not None
            else getattr(response, "status_code", None)
        )
        return _locabriques_status(
            normalized_set,
            status="unavailable",
            code="LOCABRIQUES_HTTP_ERROR",
            message="LocaBriques rejected the lookup request.",
            retryable=http_status is None or http_status >= 500 or http_status == 429,
            error={"http_status": http_status},
        )

    try:
        payload = response.json()
    except ValueError:
        return _locabriques_status(
            normalized_set,
            status="unavailable",
            code="LOCABRIQUES_INVALID_JSON",
            message="LocaBriques returned data that was not valid JSON.",
            retryable=True,
        )

    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return _locabriques_status(
            normalized_set,
            status="unavailable",
            code="LOCABRIQUES_INVALID_RESPONSE",
            message="LocaBriques returned an unexpected response shape.",
            retryable=True,
        )

    query_base = normalized_set.split("-", 1)[0]
    exact_matches: list[dict[str, Any]] = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        candidate = _locabriques_candidate(entry)
        if candidate is None:
            continue
        result_set = candidate["set_num"]
        if result_set == normalized_set or (
            "-" not in normalized_set and result_set.split("-", 1)[0] == query_base
        ):
            exact_matches.append(candidate)

    if not exact_matches:
        return _locabriques_status(
            normalized_set,
            status="not_found",
            code="LOCABRIQUES_NOT_FOUND",
            message="No published LocaBriques bag inventory matched this set.",
            retryable=False,
        )

    exact_matches.sort(
        key=lambda candidate: (
            str(candidate.get("set_num") or ""),
            str(candidate.get("inventory_id") or ""),
        )
    )
    if len(exact_matches) > 1:
        return _locabriques_status(
            normalized_set,
            status="ambiguous",
            code="LOCABRIQUES_AMBIGUOUS",
            message="More than one published LocaBriques inventory matched this set number.",
            retryable=False,
            candidates=exact_matches,
        )

    return _locabriques_status(
        normalized_set,
        status="found",
        code="LOCABRIQUES_FOUND",
        message="A published LocaBriques bag inventory matched this set.",
        retryable=False,
        inventory=exact_matches[0],
    )
