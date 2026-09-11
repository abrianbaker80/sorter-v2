"""Transactional Project Harvest planning and draft-execution model.

Immutable source parsing lives in :mod:`project_harvest`.  This module owns the
mutable, auditable layer built on top of those sources: frozen BOM revisions,
review policy, reconciliation, capacity plans, simulation, and confirmed piece
allocations.  It deliberately has no dependency on the motion or distribution
subsystems.  The draft release can prove a routing decision without being able
to actuate hardware.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import sqlite3
import threading
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote, urlparse

import project_harvest
import requests


PROJECT_SCHEMA_VERSION = 3
MAX_BOM_BYTES = 16 * 1024 * 1024
MAX_BOM_ROWS = 100_000
MAX_EVENT_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_PROJECT_NAME_LENGTH = 120
REBRICKABLE_API_ORIGIN = "https://rebrickable.com"
REBRICKABLE_MAX_PAGES = 20
REBRICKABLE_PAGE_SIZE = 1000
REBRICKABLE_TIMEOUT = (5.0, 90.0)

_PROJECT_ID_RE = re.compile(r"^harvest-[0-9a-f]{32}$")
_BOM_REVISION_ID_RE = re.compile(r"^bom-[0-9a-f]{32}$")
_ALLOCATION_ID_RE = re.compile(r"^allocation-[0-9a-f]{32}$")
_ACTIVATION_ID_RE = re.compile(r"^activation-[0-9a-f]{32}$")
_IDENTIFIER_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,120}$")
_ALLOWED_MATCH_POLICIES = {"exact", "compatible", "substitute"}
_ALLOWED_FALLBACK_MODES = {"bag_plan", "adaptive_part", "inventory_first"}
_ALLOWED_PROJECT_STATES = {
    "draft",
    "review",
    "ready",
    "simulated",
    "accepted",
    "approved",
    "active",
    "paused",
    "completed",
}
HARVEST_EXCEPTION_GROUP_ID = "harvest-exception"
HARVEST_EXCEPTION_GROUP_LABEL = "Exception pieces"
HARVEST_LAYER_SIZE_REJECT_REASON = "destination_layer_size_limit"
MAX_RECENT_ALLOCATIONS = 100
MAX_RECENT_EVENTS = 100
HARVEST_RESOLVER_VERSION = 2
HARVEST_MIN_ITEM_SCORE = 0.45
HARVEST_MIN_COLOR_SCORE = 0.25
HARVEST_MIN_PAIR_MARGIN = 0.05
HARVEST_MAX_RECOGNITION_CANDIDATES = 16
# Brickognize cannot distinguish these functionally compatible molds from the
# available views reliably. Keep the bridge deliberately small and reviewed;
# routing still requires the same color and a remaining frozen-BOM quota.
HARVEST_MOLD_EQUIVALENTS: dict[str, tuple[str, ...]] = {
    "3660": ("76959",),
    "76959": ("3660",),
}
_ALLOWED_GROUP_ACTIONS = {"separate", "include", "exclude", "review"}
_ALLOWED_NON_SORTABLE_ACTIONS = {"exclude", "include", "review"}
_MATCH_POLICY_RANK = {"exact": 0, "compatible": 1, "substitute": 2}
_STORE_LOCK = threading.RLock()
_FRESH_COPY_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "sorter-harvest:fresh-copy:v1")


class HarvestProjectError(ValueError):
    def __init__(self, code: str, message: str, *, details: Any | None = None):
        super().__init__(message)
        self.code = code
        self.details = details


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _decode_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


def _json_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HarvestProjectError(
                "DUPLICATE_JSON_KEY", f"Duplicate JSON key: {key}."
            )
        result[key] = value
    return result


def _identifier(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise HarvestProjectError(
            "INVALID_IDENTIFIER", f"{field} must be 1-120 printable characters."
        )
    return normalized


def _namespace(value: Any, field: str) -> str:
    normalized = _identifier(value, field).lower().replace(" ", "_")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", normalized):
        raise HarvestProjectError(
            "INVALID_NAMESPACE", f"{field} is not a supported namespace name."
        )
    return normalized


def _positive_int(value: Any, field: str, *, maximum: int = 1_000_000) -> int:
    if isinstance(value, bool):
        raise HarvestProjectError("INVALID_QUANTITY", f"{field} must be an integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise HarvestProjectError(
            "INVALID_QUANTITY", f"{field} must be an integer."
        ) from exc
    if str(value).strip() != str(parsed) and not isinstance(value, int):
        raise HarvestProjectError("INVALID_QUANTITY", f"{field} must be an integer.")
    if parsed <= 0 or parsed > maximum:
        raise HarvestProjectError(
            "INVALID_QUANTITY", f"{field} must be between 1 and {maximum}."
        )
    return parsed


def _boolean(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    raise HarvestProjectError("INVALID_BOOLEAN", f"{field} must be true or false.")


def _recognition_score(value: Any) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(score) or score < 0.0 or score > 1.0:
        return None
    return score


def _recognition_candidates(value: Any, *, id_key: str = "id") -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    best_by_id: dict[str, float] = {}
    for raw in value[:HARVEST_MAX_RECOGNITION_CANDIDATES]:
        if not isinstance(raw, dict):
            continue
        identifier = str(raw.get(id_key) or "").strip()
        score = _recognition_score(raw.get("score"))
        if not _IDENTIFIER_RE.fullmatch(identifier) or score is None:
            continue
        best_by_id[identifier] = max(score, best_by_id.get(identifier, -1.0))
    result = [
        {"id": identifier, "score": score}
        for identifier, score in best_by_id.items()
    ]
    result.sort(key=lambda item: (-item["score"], item["id"]))
    return result


def _rebrickable_public_url(value: Any, field: str, *, error_code: str) -> str | None:
    if value in {None, ""}:
        return None
    if not isinstance(value, str) or len(value) > 2_048:
        raise HarvestProjectError(error_code, f"Rebrickable returned an invalid {field}.")
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise HarvestProjectError(
            error_code, f"Rebrickable returned an invalid {field}."
        ) from exc
    if (
        parsed.scheme != "https"
        or (host != "rebrickable.com" and not host.endswith(".rebrickable.com"))
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise HarvestProjectError(error_code, f"Rebrickable returned an unsafe {field}.")
    return value


def _normalize_rebrickable_set_metadata(
    payload: Any, *, expected_set_number: str, error_code: str
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HarvestProjectError(error_code, "Rebrickable set metadata is invalid.")
    try:
        set_number = project_harvest.normalize_set_number(str(payload.get("set_num") or ""))
    except project_harvest.HarvestImportError as exc:
        raise HarvestProjectError(error_code, "Rebrickable set metadata has an invalid set number.") from exc
    if set_number != expected_set_number:
        raise HarvestProjectError(error_code, "Rebrickable set metadata does not match the project set.")
    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 240 or any(ord(character) < 32 for character in name):
        raise HarvestProjectError(error_code, "Rebrickable set metadata has an invalid name.")
    year = payload.get("year")
    theme_id = payload.get("theme_id")
    official_piece_count = payload.get("num_parts")
    if (
        isinstance(year, bool)
        or not isinstance(year, int)
        or not 1949 <= year <= 2100
        or isinstance(theme_id, bool)
        or not isinstance(theme_id, int)
        or theme_id <= 0
        or isinstance(official_piece_count, bool)
        or not isinstance(official_piece_count, int)
        or not 0 <= official_piece_count <= project_harvest.MAX_TOTAL_QUANTITY
    ):
        raise HarvestProjectError(error_code, "Rebrickable set metadata has invalid catalog fields.")
    last_modified = payload.get("last_modified_dt")
    if last_modified not in {None, ""}:
        if (
            not isinstance(last_modified, str)
            or len(last_modified) > 80
            or any(ord(character) < 32 for character in last_modified)
        ):
            raise HarvestProjectError(
                error_code, "Rebrickable set metadata has an invalid modification date."
            )
    return {
        "set_number": set_number,
        "name": name,
        "year": year,
        "theme_id": theme_id,
        "official_piece_count": official_piece_count,
        "image_url": _rebrickable_public_url(
            payload.get("set_img_url"), "set image URL", error_code=error_code
        ),
        "set_url": _rebrickable_public_url(
            payload.get("set_url"), "set URL", error_code=error_code
        ),
        "last_modified_at": last_modified or None,
    }


def _normalize_bom_item(row: dict[str, Any], row_number: int) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise HarvestProjectError(
            "INVALID_BOM_ROW", f"BOM row {row_number} must be an object."
        )
    part_id = _identifier(
        row.get("part_id", row.get("part_num", row.get("ItemID"))),
        f"BOM row {row_number} part_id",
    )
    color_id = _identifier(
        row.get("color_id", row.get("color", row.get("ColorID", "0"))),
        f"BOM row {row_number} color_id",
    )
    quantity = _positive_int(
        row.get("quantity", row.get("qty", row.get("Qty"))),
        f"BOM row {row_number} quantity",
    )
    item_type = str(row.get("item_type", row.get("ItemTypeID", "P")) or "P").strip()
    if item_type not in {"P", "M", "S", "N"}:
        raise HarvestProjectError(
            "INVALID_ITEM_TYPE",
            f"BOM row {row_number} item_type must be P, M, S, or N.",
        )
    sortable_raw = row.get("sortable")
    sortable = (
        item_type == "P"
        if sortable_raw in {None, ""}
        else _boolean(sortable_raw, f"BOM row {row_number} sortable")
    )
    return {
        "part_id": part_id,
        "color_id": color_id,
        "quantity": quantity,
        "item_type": item_type,
        "sortable": sortable,
        "description": str(row.get("description") or row.get("name") or "")[:255],
    }


def _normalize_runtime_aliases(
    value: Any,
    *,
    namespace: dict[str, str],
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate the frozen BrickLink-to-BOM identifier bridge used at runtime."""

    target_quantities = {
        (item["part_id"], item["color_id"]): int(item["quantity"])
        for item in items
        if item.get("sortable", True)
    }
    entries: list[dict[str, Any]] = []
    seen_inputs: dict[tuple[str, str], tuple[str, str]] = {}

    if value is None and namespace == {
        "part": "bricklink_item_number",
        "color": "bricklink_color_id",
    }:
        value = {
            "input_namespace": namespace,
            "target_namespace": namespace,
            "entries": [
                {
                    "input": {
                        "part_id": part_id,
                        "color_id": color_id,
                    },
                    "target": {
                        "part_id": part_id,
                        "color_id": color_id,
                    },
                }
                for part_id, color_id in sorted(target_quantities)
            ],
        }

    if not isinstance(value, dict):
        value = {}
    raw_entries = value.get("entries", [])
    if not isinstance(raw_entries, list) or len(raw_entries) > MAX_BOM_ROWS * 8:
        raise HarvestProjectError(
            "INVALID_RUNTIME_ALIASES", "Runtime identifier aliases are invalid."
        )
    input_namespace = value.get("input_namespace") or {}
    target_namespace = value.get("target_namespace") or namespace
    if not isinstance(input_namespace, dict) or not isinstance(target_namespace, dict):
        raise HarvestProjectError(
            "INVALID_RUNTIME_ALIASES", "Runtime identifier namespaces are invalid."
        )
    normalized_input_namespace = {
        "part": _namespace(
            input_namespace.get("part") or "bricklink_item_number",
            "runtime input part namespace",
        ),
        "color": _namespace(
            input_namespace.get("color") or "bricklink_color_id",
            "runtime input color namespace",
        ),
    }
    normalized_target_namespace = {
        "part": _namespace(
            target_namespace.get("part") or namespace["part"],
            "runtime target part namespace",
        ),
        "color": _namespace(
            target_namespace.get("color") or namespace["color"],
            "runtime target color namespace",
        ),
    }
    if normalized_input_namespace != {
        "part": "bricklink_item_number",
        "color": "bricklink_color_id",
    } or normalized_target_namespace != namespace:
        raise HarvestProjectError(
            "INVALID_RUNTIME_ALIASES",
            "Runtime aliases must map BrickLink identifiers to the frozen BOM namespace.",
        )

    for index, raw in enumerate(raw_entries, start=1):
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("input"), dict)
            or not isinstance(raw.get("target"), dict)
        ):
            raise HarvestProjectError(
                "INVALID_RUNTIME_ALIASES", f"Runtime alias {index} is invalid."
            )
        input_key = (
            _identifier(raw["input"].get("part_id"), f"runtime alias {index} input part_id"),
            _identifier(raw["input"].get("color_id"), f"runtime alias {index} input color_id"),
        )
        target_key = (
            _identifier(raw["target"].get("part_id"), f"runtime alias {index} target part_id"),
            _identifier(raw["target"].get("color_id"), f"runtime alias {index} target color_id"),
        )
        if target_key not in target_quantities:
            raise HarvestProjectError(
                "INVALID_RUNTIME_ALIASES",
                f"Runtime alias {index} targets an element outside the frozen BOM.",
            )
        previous = seen_inputs.get(input_key)
        if previous is not None and previous != target_key:
            raise HarvestProjectError(
                "AMBIGUOUS_RUNTIME_ALIAS",
                "A BrickLink part and color maps to more than one frozen BOM element.",
                details={"part_id": input_key[0], "color_id": input_key[1]},
            )
        if previous is not None:
            continue
        seen_inputs[input_key] = target_key
        entries.append(
            {
                "input": {"part_id": input_key[0], "color_id": input_key[1]},
                "target": {"part_id": target_key[0], "color_id": target_key[1]},
            }
        )

    covered_targets = set(seen_inputs.values())
    covered_quantity = sum(
        quantity for key, quantity in target_quantities.items() if key in covered_targets
    )
    total_quantity = sum(target_quantities.values())
    entries.sort(
        key=lambda item: (
            item["input"]["part_id"],
            item["input"]["color_id"],
            item["target"]["part_id"],
            item["target"]["color_id"],
        )
    )
    return {
        "status": "ready" if covered_quantity == total_quantity else "incomplete",
        "input_namespace": normalized_input_namespace,
        "target_namespace": normalized_target_namespace,
        "entries": entries,
        "summary": {
            "alias_count": len(entries),
            "covered_quantity": covered_quantity,
            "missing_quantity": max(0, total_quantity - covered_quantity),
            "sortable_quantity": total_quantity,
        },
    }


def parse_bom_upload(
    content: bytes,
    *,
    filename: str | None,
    provider: str,
    default_set_number: str,
) -> dict[str, Any]:
    """Parse a canonical/private-MOC/Rebrickable-style JSON or CSV BOM."""

    if not content:
        raise HarvestProjectError("EMPTY_BOM", "The BOM upload is empty.")
    if len(content) > MAX_BOM_BYTES:
        raise HarvestProjectError(
            "BOM_TOO_LARGE",
            f"The BOM upload exceeds {MAX_BOM_BYTES // 1024 // 1024} MiB.",
        )
    if b"\x00" in content:
        raise HarvestProjectError("INVALID_BOM_ENCODING", "The BOM must be UTF-8 text.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HarvestProjectError(
            "INVALID_BOM_ENCODING", "The BOM must be UTF-8 text."
        ) from exc

    safe_filename = Path(filename or "project-harvest-bom.json").name[:255]
    provider_name = _namespace(provider or "private_moc", "BOM provider")
    lower_filename = safe_filename.lower()
    if lower_filename.endswith(".csv"):
        is_csv = True
    elif lower_filename.endswith(".json"):
        is_csv = False
    else:
        is_csv = text.lstrip()[:1] not in {"{", "["}
    namespace: dict[str, str]
    set_number = project_harvest.normalize_set_number(default_set_number)
    rows: list[dict[str, Any]]
    set_metadata: dict[str, Any] | None = None
    runtime_aliases_raw: Any = None

    if is_csv:
        try:
            reader = csv.DictReader(io.StringIO(text, newline=""))
            if reader.fieldnames is None:
                raise HarvestProjectError(
                    "INVALID_BOM", "The BOM CSV has no header row."
                )
            rows = [dict(row) for row in reader]
        except csv.Error as exc:
            raise HarvestProjectError(
                "INVALID_BOM", "The BOM CSV is malformed."
            ) from exc
        namespace = {
            "part": "rebrickable_part_number"
            if "rebrickable" in provider_name
            else "bricklink_item_number",
            "color": "rebrickable_color_id"
            if "rebrickable" in provider_name
            else "bricklink_color_id",
        }
    else:
        try:
            payload = json.loads(text, object_pairs_hook=_json_without_duplicates)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HarvestProjectError(
                "INVALID_BOM", "The BOM JSON is malformed."
            ) from exc
        if isinstance(payload, list):
            rows = payload
            raw_namespace: Any = {}
        elif isinstance(payload, dict):
            raw_rows = payload.get("items", payload.get("results"))
            if not isinstance(raw_rows, list):
                raise HarvestProjectError(
                    "INVALID_BOM", "The BOM JSON must contain an items array."
                )
            rows = raw_rows
            raw_namespace = (
                payload.get("namespace") or payload.get("identifier_namespace") or {}
            )
            runtime_aliases_raw = payload.get("runtime_aliases")
            payload_set = payload.get("set_number") or payload.get("set_num")
            if payload_set is not None:
                normalized_payload_set = project_harvest.normalize_set_number(
                    str(payload_set)
                )
                if (
                    normalized_payload_set.split("-", 1)[0]
                    != set_number.split("-", 1)[0]
                ):
                    raise HarvestProjectError(
                        "BOM_SET_MISMATCH",
                        "The BOM set identity does not match the Harvest project.",
                    )
                set_number = normalized_payload_set
            raw_set_metadata = payload.get("set_metadata")
            if raw_set_metadata is not None:
                expected_metadata_set = (
                    set_number if "-" in set_number else f"{set_number}-1"
                )
                set_metadata = _normalize_rebrickable_set_metadata(
                    raw_set_metadata,
                    expected_set_number=expected_metadata_set,
                    error_code="INVALID_BOM_METADATA",
                )
        else:
            raise HarvestProjectError(
                "INVALID_BOM", "The BOM JSON must be an object or array."
            )
        if not isinstance(raw_namespace, dict):
            raise HarvestProjectError(
                "INVALID_NAMESPACE", "The BOM namespace must be an object."
            )
        namespace = {
            "part": _namespace(
                raw_namespace.get("part")
                or (
                    "rebrickable_part_number"
                    if "rebrickable" in provider_name
                    else "bricklink_item_number"
                ),
                "BOM part namespace",
            ),
            "color": _namespace(
                raw_namespace.get("color")
                or (
                    "rebrickable_color_id"
                    if "rebrickable" in provider_name
                    else "bricklink_color_id"
                ),
                "BOM color namespace",
            ),
        }

    if not rows:
        raise HarvestProjectError("EMPTY_BOM", "The BOM contains no item rows.")
    if len(rows) > MAX_BOM_ROWS:
        raise HarvestProjectError(
            "TOO_MANY_BOM_ROWS", "The BOM contains too many rows."
        )

    merged: dict[tuple[str, str, str, bool], dict[str, Any]] = {}
    total_quantity = 0
    for index, row in enumerate(rows, start=1):
        normalized = _normalize_bom_item(row, index)
        total_quantity += normalized["quantity"]
        if total_quantity > project_harvest.MAX_TOTAL_QUANTITY:
            raise HarvestProjectError(
                "BOM_QUANTITY_LIMIT", "The BOM quantity limit was exceeded."
            )
        key = (
            normalized["part_id"],
            normalized["color_id"],
            normalized["item_type"],
            normalized["sortable"],
        )
        if key in merged:
            merged[key]["quantity"] += normalized["quantity"]
        else:
            merged[key] = normalized
    items = sorted(
        merged.values(),
        key=lambda item: (item["part_id"], item["color_id"], item["item_type"]),
    )
    source_sha256 = hashlib.sha256(content).hexdigest()
    normalized_payload = {
        "schema_version": PROJECT_SCHEMA_VERSION,
        "set_number": set_number,
        "provider": provider_name,
        "filename": safe_filename,
        "source_sha256": source_sha256,
        "source_size_bytes": len(content),
        "namespace": namespace,
        "summary": {
            "source_row_count": len(rows),
            "distinct_elements": len(items),
            "total_quantity": sum(item["quantity"] for item in items),
            "sortable_quantity": sum(
                item["quantity"] for item in items if item["sortable"]
            ),
            "non_sortable_quantity": sum(
                item["quantity"] for item in items if not item["sortable"]
            ),
        },
        "items": items,
    }
    normalized_payload["runtime_aliases"] = _normalize_runtime_aliases(
        runtime_aliases_raw,
        namespace=namespace,
        items=items,
    )
    if set_metadata is not None:
        normalized_payload["set_metadata"] = set_metadata
    normalized_payload["normalized_sha256"] = hashlib.sha256(
        _canonical_json(normalized_payload).encode("utf-8")
    ).hexdigest()
    return normalized_payload


def _rebrickable_next_url(value: Any, *, set_number: str) -> str | None:
    if value in {None, ""}:
        return None
    if not isinstance(value, str):
        raise HarvestProjectError(
            "REBRICKABLE_INVALID_RESPONSE",
            "Rebrickable returned an invalid pagination link.",
        )
    parsed = urlparse(value)
    expected_path = f"/api/v3/lego/sets/{quote(set_number, safe='-')}/parts/"
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "rebrickable.com"
        or parsed.path != expected_path
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise HarvestProjectError(
            "REBRICKABLE_INVALID_RESPONSE",
            "Rebrickable returned an unsafe pagination link.",
        )
    return value


def _rebrickable_get_json(
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, int] | None,
    set_number: str,
    context: str,
) -> tuple[dict[str, Any], int]:
    try:
        response = requests.get(
            url,
            params=params,
            headers=headers,
            timeout=REBRICKABLE_TIMEOUT,
        )
    except requests.Timeout as exc:
        raise HarvestProjectError(
            "REBRICKABLE_TIMEOUT",
            f"Rebrickable did not respond before the {context} request timed out.",
        ) from exc
    except requests.RequestException as exc:
        raise HarvestProjectError(
            "REBRICKABLE_UNAVAILABLE",
            "The Rebrickable inventory service could not be reached.",
        ) from exc
    if response.status_code in {401, 403}:
        raise HarvestProjectError(
            "REBRICKABLE_INVALID_API_KEY",
            "Rebrickable rejected the configured API key.",
        )
    if response.status_code == 404:
        raise HarvestProjectError(
            "REBRICKABLE_SET_NOT_FOUND",
            f"Rebrickable does not have catalog data for set {set_number}.",
        )
    if response.status_code == 429:
        raise HarvestProjectError(
            "REBRICKABLE_RATE_LIMITED",
            "Rebrickable rate-limited the request. Wait a moment and try again.",
        )
    if response.status_code >= 400:
        raise HarvestProjectError(
            "REBRICKABLE_HTTP_ERROR",
            f"Rebrickable returned HTTP {response.status_code} while fetching {context}.",
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise HarvestProjectError(
            "REBRICKABLE_INVALID_RESPONSE",
            "Rebrickable returned malformed JSON.",
        ) from exc
    if not isinstance(payload, dict):
        raise HarvestProjectError(
            "REBRICKABLE_INVALID_RESPONSE",
            "Rebrickable returned an invalid response.",
        )
    return payload, len(response.content)


def _rebrickable_bom_item(row: Any, row_number: int) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise HarvestProjectError(
            "REBRICKABLE_INVALID_RESPONSE",
            f"Rebrickable part row {row_number} is not an object.",
        )
    part = row.get("part")
    color = row.get("color")
    if not isinstance(part, dict) or not isinstance(color, dict):
        raise HarvestProjectError(
            "REBRICKABLE_INVALID_RESPONSE",
            f"Rebrickable part row {row_number} is missing part or color details.",
        )
    part_number = part.get("part_num")
    color_id = color.get("id")
    if part_number is None or color_id is None or isinstance(color_id, bool):
        raise HarvestProjectError(
            "REBRICKABLE_INVALID_RESPONSE",
            f"Rebrickable part row {row_number} has an invalid part or color identifier.",
        )
    is_spare = row.get("is_spare")
    if not isinstance(is_spare, bool):
        raise HarvestProjectError(
            "REBRICKABLE_INVALID_RESPONSE",
            f"Rebrickable part row {row_number} has an invalid spare flag.",
        )
    item = {
        "part_id": _identifier(
            part_number, f"Rebrickable row {row_number} part number"
        ),
        "color_id": _identifier(
            str(color_id), f"Rebrickable row {row_number} color id"
        ),
        "quantity": _positive_int(
            row.get("quantity"), f"Rebrickable row {row_number} quantity"
        ),
        "item_type": "P",
        "sortable": True,
        "description": str(part.get("name") or "")[:255],
    }
    return {"item": item, "is_spare": is_spare}


def _rebrickable_external_ids(value: Any, provider: str) -> list[str]:
    if not isinstance(value, dict):
        return []
    raw = value.get(provider)
    if isinstance(raw, dict):
        raw = raw.get("ext_ids")
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for item in raw:
        candidate = str(item).strip()
        if candidate and candidate not in result and _IDENTIFIER_RE.fullmatch(candidate):
            result.append(candidate)
    return result


def _rebrickable_runtime_alias_candidates(row: Any) -> list[dict[str, Any]]:
    if not isinstance(row, dict) or row.get("is_spare") is not False:
        return []
    part = row.get("part")
    color = row.get("color")
    if not isinstance(part, dict) or not isinstance(color, dict):
        return []
    target_part = str(part.get("part_num") or "").strip()
    target_color = str(color.get("id") if color.get("id") is not None else "").strip()
    if not target_part or not target_color:
        return []
    part_ids = _rebrickable_external_ids(part.get("external_ids"), "BrickLink")
    color_ids = _rebrickable_external_ids(color.get("external_ids"), "BrickLink")
    return [
        {
            "input": {"part_id": part_id, "color_id": color_id},
            "target": {"part_id": target_part, "color_id": target_color},
        }
        for part_id in part_ids
        for color_id in color_ids
    ]


def fetch_rebrickable_bom(*, set_number: str, api_key: str) -> dict[str, Any]:
    """Fetch and canonicalize one official set inventory from Rebrickable.

    Authentication is sent only in the Authorization header. Raw response
    objects are retained in the frozen source envelope, while spare rows are
    preserved as evidence but excluded from the authoritative build quantity.
    """

    key = str(api_key or "").strip()
    if not key:
        raise HarvestProjectError(
            "REBRICKABLE_API_KEY_REQUIRED",
            "Configure a Rebrickable API key in Settings before fetching the BOM.",
        )
    normalized_set = project_harvest.normalize_set_number(set_number)
    if "-" not in normalized_set:
        normalized_set = f"{normalized_set}-1"
    encoded_set = quote(normalized_set, safe="-")
    set_details_url = f"{REBRICKABLE_API_ORIGIN}/api/v3/lego/sets/{encoded_set}/"
    first_url = f"{REBRICKABLE_API_ORIGIN}/api/v3/lego/sets/{encoded_set}/parts/"
    next_url: str | None = first_url
    params: dict[str, int] | None = {
        "page_size": REBRICKABLE_PAGE_SIZE,
        "inc_minifig_parts": 1,
        "inc_part_details": 1,
        "inc_color_details": 1,
    }
    headers = {
        "Authorization": f"key {key}",
        "Accept": "application/json",
        "User-Agent": "SorterV2-ProjectHarvest/1",
    }
    set_metadata_source, response_bytes = _rebrickable_get_json(
        set_details_url,
        headers=headers,
        params=None,
        set_number=normalized_set,
        context="set details",
    )
    _normalize_rebrickable_set_metadata(
        set_metadata_source,
        expected_set_number=normalized_set,
        error_code="REBRICKABLE_INVALID_RESPONSE",
    )
    raw_pages: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    spares: list[dict[str, Any]] = []
    runtime_alias_candidates: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    source_row_count = 0

    while next_url is not None:
        if len(raw_pages) >= REBRICKABLE_MAX_PAGES:
            raise HarvestProjectError(
                "REBRICKABLE_PAGINATION_LIMIT",
                "Rebrickable returned more inventory pages than the safety limit allows.",
            )
        if next_url in seen_urls:
            raise HarvestProjectError(
                "REBRICKABLE_INVALID_RESPONSE",
                "Rebrickable returned a repeated pagination link.",
            )
        seen_urls.add(next_url)
        payload, page_bytes = _rebrickable_get_json(
            next_url,
            headers=headers,
            params=params,
            set_number=normalized_set,
            context="the set inventory",
        )
        params = None
        response_bytes += page_bytes
        if response_bytes > MAX_BOM_BYTES:
            raise HarvestProjectError(
                "REBRICKABLE_RESPONSE_TOO_LARGE",
                f"The Rebrickable response exceeds {MAX_BOM_BYTES // 1024 // 1024} MiB.",
            )
        if not isinstance(payload.get("results"), list):
            raise HarvestProjectError(
                "REBRICKABLE_INVALID_RESPONSE",
                "Rebrickable returned an invalid inventory response.",
            )
        raw_pages.append(payload)
        for row in payload["results"]:
            source_row_count += 1
            normalized = _rebrickable_bom_item(row, source_row_count)
            destination = spares if normalized["is_spare"] else items
            destination.append(normalized["item"])
            runtime_alias_candidates.extend(_rebrickable_runtime_alias_candidates(row))
        next_url = _rebrickable_next_url(
            payload.get("next"), set_number=normalized_set
        )

    if not items:
        raise HarvestProjectError(
            "REBRICKABLE_EMPTY_BOM",
            f"Rebrickable returned no non-spare parts for set {normalized_set}.",
        )
    envelope = {
        "schema_version": PROJECT_SCHEMA_VERSION,
        "set_number": normalized_set,
        "provider": "rebrickable_api",
        "source_url": first_url,
        "set_metadata": set_metadata_source,
        "namespace": {
            "part": "rebrickable_part_number",
            "color": "rebrickable_color_id",
        },
        "policy": {
            "minifig_parts_included": True,
            "spares": "preserved_as_evidence_and_excluded_from_authoritative_quantity",
        },
        "summary": {
            "source_page_count": len(raw_pages),
            "source_row_count": source_row_count,
            "authoritative_row_count": len(items),
            "authoritative_quantity": sum(item["quantity"] for item in items),
            "spare_row_count": len(spares),
            "spare_quantity": sum(item["quantity"] for item in spares),
        },
        "items": items,
        "runtime_aliases": {
            "input_namespace": {
                "part": "bricklink_item_number",
                "color": "bricklink_color_id",
            },
            "target_namespace": {
                "part": "rebrickable_part_number",
                "color": "rebrickable_color_id",
            },
            "entries": runtime_alias_candidates,
        },
        "spares": spares,
        "source_pages": raw_pages,
    }
    content = _canonical_json(envelope).encode("utf-8")
    if len(content) > MAX_BOM_BYTES:
        raise HarvestProjectError(
            "REBRICKABLE_RESPONSE_TOO_LARGE",
            f"The canonical Rebrickable source exceeds {MAX_BOM_BYTES // 1024 // 1024} MiB.",
        )
    return {
        "content": content,
        "filename": f"rebrickable-{normalized_set}.json",
        "provider": "rebrickable_api",
        "source_summary": envelope["summary"],
    }


def default_review_policy(draft: dict[str, Any]) -> dict[str, Any]:
    group_actions: dict[str, str] = {}
    for group in draft.get("groups", []):
        kind = group.get("kind")
        group_actions[str(group.get("id"))] = (
            "separate" if kind == "numbered" else "review"
        )
    return {
        "group_actions": group_actions,
        "non_sortable_action": "exclude",
        "extras_action": "review",
        "unknown_classification_action": "manual_review",
        "physical_confirmation_required": True,
    }


def _validate_policy(policy: Any, draft: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(policy, dict):
        raise HarvestProjectError("INVALID_POLICY", "Review policy must be an object.")
    known_group_ids = {str(group.get("id")) for group in draft.get("groups", [])}
    raw_actions = policy.get("group_actions", {})
    if not isinstance(raw_actions, dict):
        raise HarvestProjectError("INVALID_POLICY", "group_actions must be an object.")
    actions: dict[str, str] = {}
    for group_id in sorted(known_group_ids):
        action = str(raw_actions.get(group_id, "review"))
        if action not in _ALLOWED_GROUP_ACTIONS:
            raise HarvestProjectError(
                "INVALID_POLICY", f"Unsupported action for {group_id}."
            )
        actions[group_id] = action
    non_sortable_action = str(policy.get("non_sortable_action", "exclude"))
    if non_sortable_action not in _ALLOWED_NON_SORTABLE_ACTIONS:
        raise HarvestProjectError(
            "INVALID_POLICY", "non_sortable_action must be exclude, include, or review."
        )
    return {
        "group_actions": actions,
        "non_sortable_action": non_sortable_action,
        "extras_action": str(policy.get("extras_action", "review")),
        "unknown_classification_action": str(
            policy.get("unknown_classification_action", "manual_review")
        ),
        "physical_confirmation_required": bool(
            policy.get("physical_confirmation_required", True)
        ),
    }


def _validate_mappings(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HarvestProjectError("INVALID_MAPPING", "Mappings must be an object.")
    namespace_raw = value.get("namespace", {})
    if not isinstance(namespace_raw, dict):
        raise HarvestProjectError(
            "INVALID_MAPPING", "namespace mappings must be an object."
        )
    normalized_namespace: dict[str, dict[str, str]] = {}
    for kind in ("parts", "colors"):
        raw_map = namespace_raw.get(kind, {})
        if not isinstance(raw_map, dict):
            raise HarvestProjectError(
                "INVALID_MAPPING", f"namespace.{kind} must be an object."
            )
        normalized_namespace[kind] = {
            _identifier(source, f"{kind} source"): _identifier(target, f"{kind} target")
            for source, target in sorted(raw_map.items())
        }
    substitutions_raw = value.get("substitutions", [])
    if not isinstance(substitutions_raw, list):
        raise HarvestProjectError("INVALID_MAPPING", "substitutions must be an array.")
    substitutions: list[dict[str, Any]] = []
    for index, item in enumerate(substitutions_raw, start=1):
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("input"), dict)
            or not isinstance(item.get("target"), dict)
        ):
            raise HarvestProjectError(
                "INVALID_MAPPING", f"Substitution {index} is invalid."
            )
        kind = str(item.get("kind", "compatible"))
        if kind not in {"compatible", "substitute"}:
            raise HarvestProjectError(
                "INVALID_MAPPING", f"Substitution {index} kind is invalid."
            )
        substitutions.append(
            {
                "kind": kind,
                "input": {
                    "part_id": _identifier(
                        item["input"].get("part_id"), "substitution input part_id"
                    ),
                    "color_id": _identifier(
                        item["input"].get("color_id"), "substitution input color_id"
                    ),
                },
                "target": {
                    "part_id": _identifier(
                        item["target"].get("part_id"), "substitution target part_id"
                    ),
                    "color_id": _identifier(
                        item["target"].get("color_id"), "substitution target color_id"
                    ),
                },
            }
        )
    substitutions.sort(
        key=lambda item: (
            _MATCH_POLICY_RANK[item["kind"]],
            item["input"]["part_id"],
            item["input"]["color_id"],
        )
    )
    return {"namespace": normalized_namespace, "substitutions": substitutions}


class HarvestProjectStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.db_path = self.root / "harvest_projects.sqlite3"
        self.root.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with _STORE_LOCK, self._connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS harvest_projects (
                    project_id TEXT PRIMARY KEY,
                    draft_id TEXT NOT NULL,
                    draft_manifest_sha256 TEXT NOT NULL,
                    set_number TEXT NOT NULL,
                    name TEXT NOT NULL,
                    state TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    match_policy TEXT NOT NULL,
                    fallback_mode TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    policy_json TEXT NOT NULL,
                    mapping_json TEXT NOT NULL,
                    selected_bom_revision_id TEXT,
                    capacity_plan_json TEXT,
                    simulation_json TEXT,
                    acceptance_json TEXT,
                    green_light_json TEXT,
                    activation_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS harvest_bom_revisions (
                    bom_revision_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES harvest_projects(project_id),
                    source_sha256 TEXT NOT NULL,
                    normalized_sha256 TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    source_blob BLOB NOT NULL,
                    normalized_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id, source_sha256)
                );
                CREATE TABLE IF NOT EXISTS harvest_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL REFERENCES harvest_projects(project_id),
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS harvest_allocations (
                    allocation_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES harvest_projects(project_id),
                    runtime_id TEXT,
                    piece_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    part_id TEXT NOT NULL,
                    color_id TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    match_kind TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    resolution_json TEXT,
                    evidence_json TEXT,
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    undone_at TEXT,
                    UNIQUE(project_id, piece_id)
                );
                CREATE TABLE IF NOT EXISTS harvest_runtime_state (
                    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
                    project_id TEXT NOT NULL REFERENCES harvest_projects(project_id),
                    activation_id TEXT NOT NULL,
                    activation_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_harvest_projects_priority
                    ON harvest_projects(priority DESC, created_at ASC);
                CREATE INDEX IF NOT EXISTS idx_harvest_events_project
                    ON harvest_events(project_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_harvest_allocations_project
                    ON harvest_allocations(project_id, status, group_id);
                """
            )
            project_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(harvest_projects)").fetchall()
            }
            if "green_light_json" not in project_columns:
                conn.execute(
                    "ALTER TABLE harvest_projects ADD COLUMN green_light_json TEXT"
                )
            if "activation_json" not in project_columns:
                conn.execute(
                    "ALTER TABLE harvest_projects ADD COLUMN activation_json TEXT"
                )
            allocation_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(harvest_allocations)").fetchall()
            }
            if "runtime_id" not in allocation_columns:
                conn.execute(
                    "ALTER TABLE harvest_allocations ADD COLUMN runtime_id TEXT"
                )
            if "resolution_json" not in allocation_columns:
                conn.execute(
                    "ALTER TABLE harvest_allocations ADD COLUMN resolution_json TEXT"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_harvest_allocations_runtime "
                "ON harvest_allocations(project_id, runtime_id, mode, status)"
            )
            conn.commit()

    def _project_row(self, conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
        if not _PROJECT_ID_RE.fullmatch(project_id):
            raise HarvestProjectError(
                "INVALID_PROJECT_ID", "Invalid Harvest project id."
            )
        row = conn.execute(
            "SELECT * FROM harvest_projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise HarvestProjectError("PROJECT_NOT_FOUND", "Harvest project not found.")
        return row

    @staticmethod
    def _ensure_not_active(row: sqlite3.Row) -> None:
        if str(row["state"]) == "active":
            raise HarvestProjectError(
                "PROJECT_ACTIVE",
                "Stop the live Harvest run before changing its BOM, policy, plan, or evidence.",
            )

    def _append_event(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        actor: str = "operator",
    ) -> dict[str, Any]:
        payload_json = _canonical_json(payload)
        if len(payload_json.encode("utf-8")) > MAX_EVENT_PAYLOAD_BYTES:
            raise HarvestProjectError(
                "EVENT_TOO_LARGE", "The audit event is too large."
            )
        previous = conn.execute(
            "SELECT sequence, event_hash FROM harvest_events WHERE project_id = ? ORDER BY sequence DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        sequence = int(previous["sequence"]) + 1 if previous else 1
        previous_hash = str(previous["event_hash"]) if previous else "0" * 64
        created_at = _now()
        hash_payload = "|".join(
            [
                previous_hash,
                project_id,
                str(sequence),
                kind,
                actor,
                created_at,
                payload_json,
            ]
        )
        event_hash = hashlib.sha256(hash_payload.encode("utf-8")).hexdigest()
        cursor = conn.execute(
            "INSERT INTO harvest_events(project_id, sequence, kind, actor, payload_json, previous_hash, event_hash, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                sequence,
                kind,
                actor,
                payload_json,
                previous_hash,
                event_hash,
                created_at,
            ),
        )
        return {
            "event_id": int(cursor.lastrowid),
            "sequence": sequence,
            "kind": kind,
            "actor": actor,
            "payload": payload,
            "previous_hash": previous_hash,
            "event_hash": event_hash,
            "created_at": created_at,
        }

    def _events(
        self, conn: sqlite3.Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM harvest_events WHERE project_id = ? ORDER BY sequence",
            (project_id,),
        ).fetchall()
        events: list[dict[str, Any]] = []
        expected_previous = "0" * 64
        for row in rows:
            payload = _decode_json(row["payload_json"], {})
            hash_payload = "|".join(
                [
                    expected_previous,
                    project_id,
                    str(row["sequence"]),
                    row["kind"],
                    row["actor"],
                    row["created_at"],
                    row["payload_json"],
                ]
            )
            expected_hash = hashlib.sha256(hash_payload.encode("utf-8")).hexdigest()
            if (
                row["previous_hash"] != expected_previous
                or row["event_hash"] != expected_hash
            ):
                raise HarvestProjectError(
                    "AUDIT_CHAIN_MISMATCH",
                    "The Project Harvest audit chain has changed.",
                )
            expected_previous = row["event_hash"]
            events.append(
                {
                    "event_id": int(row["event_id"]),
                    "sequence": int(row["sequence"]),
                    "kind": row["kind"],
                    "actor": row["actor"],
                    "payload": payload,
                    "previous_hash": row["previous_hash"],
                    "event_hash": row["event_hash"],
                    "created_at": row["created_at"],
                }
            )
        return events

    def create_project(
        self,
        *,
        draft_id: str,
        name: str | None = None,
        priority: int = 100,
        match_policy: str = "exact",
        fallback_mode: str = "bag_plan",
    ) -> dict[str, Any]:
        draft = project_harvest.load_bsx_draft(self.root, draft_id)
        project_name = str(name or f"Set {draft['set_number']}").strip()
        if not project_name or len(project_name) > MAX_PROJECT_NAME_LENGTH:
            raise HarvestProjectError(
                "INVALID_PROJECT_NAME", "Project name is invalid."
            )
        if match_policy not in _ALLOWED_MATCH_POLICIES:
            raise HarvestProjectError(
                "INVALID_MATCH_POLICY", "Unsupported match policy."
            )
        if fallback_mode not in _ALLOWED_FALLBACK_MODES:
            raise HarvestProjectError(
                "INVALID_FALLBACK_MODE", "Unsupported fallback mode."
            )
        if (
            isinstance(priority, bool)
            or not isinstance(priority, int)
            or not 0 <= priority <= 10_000
        ):
            raise HarvestProjectError(
                "INVALID_PRIORITY", "Priority must be between 0 and 10000."
            )
        created_at = _now()
        policy = default_review_policy(draft)
        mappings = {"namespace": {"parts": {}, "colors": {}}, "substitutions": []}
        with _STORE_LOCK, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT project_id FROM harvest_projects WHERE draft_id = ? AND draft_manifest_sha256 = ? AND name = ? AND priority = ? AND match_policy = ? AND fallback_mode = ? ORDER BY created_at DESC LIMIT 1",
                (
                    draft_id,
                    draft["manifest_sha256"],
                    project_name,
                    priority,
                    match_policy,
                    fallback_mode,
                ),
            ).fetchone()
            if existing is not None:
                project_id = str(existing["project_id"])
                conn.rollback()
                return self.get_project(project_id)
            project_id = f"harvest-{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO harvest_projects(project_id, draft_id, draft_manifest_sha256, set_number, name, state, priority, match_policy, fallback_mode, revision, policy_json, mapping_json, created_at, updated_at) VALUES(?, ?, ?, ?, ?, 'draft', ?, ?, ?, 1, ?, ?, ?, ?)",
                (
                    project_id,
                    draft_id,
                    draft["manifest_sha256"],
                    draft["set_number"],
                    project_name,
                    priority,
                    match_policy,
                    fallback_mode,
                    _canonical_json(policy),
                    _canonical_json(mappings),
                    created_at,
                    created_at,
                ),
            )
            self._append_event(
                conn,
                project_id,
                "project_created",
                {
                    "draft_id": draft_id,
                    "draft_manifest_sha256": draft["manifest_sha256"],
                    "priority": priority,
                    "match_policy": match_policy,
                    "fallback_mode": fallback_mode,
                },
            )
            conn.commit()
        return self.get_project(project_id)

    def create_fresh_copy(
        self,
        project_id: str,
        *,
        request_id: str,
        expected_revision: int,
        operator: str,
    ) -> dict[str, Any]:
        """Copy saved setup atomically; a replay returns the copy as it is now."""
        if not _PROJECT_ID_RE.fullmatch(project_id):
            raise HarvestProjectError("INVALID_PROJECT_ID", "Invalid Harvest project id.")
        try:
            normalized_request_id = str(uuid.UUID(str(request_id)))
        except (ValueError, AttributeError) as exc:
            raise HarvestProjectError(
                "INVALID_COPY_REQUEST_ID", "Copy request_id must be a UUID."
            ) from exc
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise HarvestProjectError(
                "INVALID_PROJECT_REVISION", "Expected project revision is invalid."
            )
        normalized_operator = _identifier(operator, "operator")
        request = {
            "source_project_id": project_id,
            "request_id": normalized_request_id,
            "expected_revision": expected_revision,
            "operator": normalized_operator,
        }
        request_fingerprint = hashlib.sha256(
            _canonical_json(request).encode("utf-8")
        ).hexdigest()
        copy_id = "harvest-" + uuid.uuid5(
            _FRESH_COPY_NAMESPACE, f"{project_id}:{normalized_request_id}"
        ).hex
        try:
            with _STORE_LOCK, self._connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT * FROM harvest_projects WHERE project_id = ?", (copy_id,)
                ).fetchone()
                if existing is not None:
                    # Read the complete verified chain: creation may no longer be
                    # inside the ordinary response's recent-events window.
                    events = self._events(conn, copy_id)
                    provenance = (
                        events[0]["payload"].get("fresh_copy", {}) if events else {}
                    )
                    if (
                        not events
                        or events[0]["kind"] != "project_created"
                        or provenance.get("request") != request
                        or provenance.get("request_fingerprint") != request_fingerprint
                    ):
                        raise HarvestProjectError(
                            "COPY_REQUEST_CONFLICT",
                            "This copy request ID is already bound to different parameters.",
                        )
                    result = self._assemble(conn, existing, include_events=True)
                    conn.commit()
                    return result

                row = self._project_row(conn, project_id)
                if row["revision"] != expected_revision:
                    raise HarvestProjectError(
                        "STALE_PROJECT_REVISION", "The source project revision has changed."
                    )
                draft = project_harvest.load_bsx_draft(self.root, row["draft_id"])
                if (
                    draft["manifest_sha256"] != row["draft_manifest_sha256"]
                    or draft["set_number"] != row["set_number"]
                ):
                    raise HarvestProjectError(
                        "DRAFT_REVISION_MISMATCH", "The project draft revision has changed."
                    )
                bom = self._selected_bom(conn, row)
                if bom is None:
                    raise HarvestProjectError(
                        "BOM_REVISION_MISSING", "Select a frozen BOM before creating a copy."
                    )
                policy = json.loads(
                    row["policy_json"], object_pairs_hook=_json_without_duplicates
                )
                mappings = json.loads(
                    row["mapping_json"], object_pairs_hook=_json_without_duplicates
                )
                if _validate_policy(policy, draft) != policy:
                    raise HarvestProjectError(
                        "INVALID_POLICY", "Saved policy is not normalized."
                    )
                if _validate_mappings(mappings) != mappings:
                    raise HarvestProjectError(
                        "INVALID_MAPPING", "Saved mappings are not normalized."
                    )
                if row["match_policy"] not in _ALLOWED_MATCH_POLICIES:
                    raise HarvestProjectError(
                        "INVALID_MATCH_POLICY", "Unsupported match policy."
                    )
                if row["fallback_mode"] not in _ALLOWED_FALLBACK_MODES:
                    raise HarvestProjectError(
                        "INVALID_FALLBACK_MODE", "Unsupported fallback mode."
                    )
                if (
                    not isinstance(row["priority"], int)
                    or not 0 <= row["priority"] <= 10_000
                ):
                    raise HarvestProjectError(
                        "INVALID_PRIORITY", "Priority must be between 0 and 10000."
                    )
                if not row["name"].strip() or len(row["name"]) > MAX_PROJECT_NAME_LENGTH:
                    raise HarvestProjectError(
                        "INVALID_PROJECT_NAME", "Project name is invalid."
                    )
                bom_row = conn.execute(
                    "SELECT * FROM harvest_bom_revisions WHERE bom_revision_id = ? AND project_id = ?",
                    (row["selected_bom_revision_id"], project_id),
                ).fetchone()
                # Hashes protect frozen content; also bind its embedded identity
                # to the owning row without reparsing or rewriting that content.
                if any(
                    bom[key] != bom_row[key]
                    for key in ("source_sha256", "normalized_sha256", "provider", "filename")
                ):
                    raise HarvestProjectError(
                        "INVALID_BOM", "Frozen BOM identity disagrees with its row."
                    )
                configuration = {
                    key: row[key]
                    for key in (
                        "draft_id", "draft_manifest_sha256", "set_number", "name",
                        "priority", "match_policy", "fallback_mode",
                    )
                }
                configuration.update(
                    policy=policy,
                    mappings=mappings,
                    bom={
                        key: bom_row[key]
                        for key in (
                            "source_sha256", "normalized_sha256", "provider", "filename"
                        )
                    },
                )
                configuration_fingerprint = hashlib.sha256(
                    _canonical_json(configuration).encode("utf-8")
                ).hexdigest()
                suffix = f" (copy {copy_id.removeprefix('harvest-')})"
                prefix_limit = MAX_PROJECT_NAME_LENGTH - len(suffix)
                name = row["name"][:prefix_limit].rstrip() + suffix
                if name == row["name"]:
                    name = row["name"][:prefix_limit - 1].rstrip() + suffix
                bom_id = f"bom-{uuid.uuid4().hex}"
                created_at = _now()
                # Only saved setup is inserted. Every downstream evidence column
                # remains NULL; no allocations or runtime rows are touched.
                conn.execute(
                    "INSERT INTO harvest_projects(project_id, draft_id, draft_manifest_sha256, "
                    "set_number, name, state, priority, match_policy, fallback_mode, revision, "
                    "policy_json, mapping_json, selected_bom_revision_id, created_at, updated_at) "
                    "VALUES(?, ?, ?, ?, ?, 'review', ?, ?, ?, 1, ?, ?, ?, ?, ?)",
                    (
                        copy_id, row["draft_id"], row["draft_manifest_sha256"],
                        row["set_number"], name, row["priority"], row["match_policy"],
                        row["fallback_mode"], row["policy_json"], row["mapping_json"],
                        bom_id, created_at, created_at,
                    ),
                )
                conn.execute(
                    "INSERT INTO harvest_bom_revisions(bom_revision_id, project_id, source_sha256, "
                    "normalized_sha256, provider, filename, source_blob, normalized_json, created_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        bom_id, copy_id, bom_row["source_sha256"], bom_row["normalized_sha256"],
                        bom_row["provider"], bom_row["filename"], bom_row["source_blob"],
                        bom_row["normalized_json"], created_at,
                    ),
                )
                self._append_event(
                    conn, copy_id, "project_created",
                    {
                        "draft_id": row["draft_id"],
                        "draft_manifest_sha256": row["draft_manifest_sha256"],
                        "priority": row["priority"],
                        "match_policy": row["match_policy"],
                        "fallback_mode": row["fallback_mode"],
                        "fresh_copy": {
                            "request": request,
                            "request_fingerprint": request_fingerprint,
                            "source_project_id": project_id,
                            "source_revision": row["revision"],
                            "source_draft_hash": row["draft_manifest_sha256"],
                            "source_bom_revision_id": row["selected_bom_revision_id"],
                            "source_bom_sha256": bom_row["source_sha256"],
                            "source_bom_normalized_sha256": bom_row["normalized_sha256"],
                            "configuration_fingerprint": configuration_fingerprint,
                            "source_project_created_at": row["created_at"],
                            "source_project_updated_at": row["updated_at"],
                            "source_draft_created_at": draft.get("created_at"),
                            "source_bom_created_at": bom_row["created_at"],
                            "bom_revision_id": bom_id,
                        },
                    },
                    actor=normalized_operator,
                )
                result = self._assemble(
                    conn, self._project_row(conn, copy_id), include_events=True
                )
                conn.commit()
                return result
        except FileNotFoundError as exc:
            raise HarvestProjectError(
                "DRAFT_NOT_FOUND", "Project Harvest draft not found."
            ) from exc
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
            raise HarvestProjectError(
                "INVALID_FROZEN_CONFIGURATION", "The saved Harvest configuration is malformed."
            ) from exc

    def list_projects(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            ids = [
                str(row["project_id"])
                for row in conn.execute(
                    "SELECT project_id FROM harvest_projects ORDER BY priority DESC, created_at"
                ).fetchall()
            ]
        return [
            self.get_project(project_id, include_events=False) for project_id in ids
        ]

    def list_project_summaries(self) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for project in self.list_projects():
            summaries.append(
                {
                    "project_id": project["project_id"],
                    "draft_id": project["draft_id"],
                    "set_number": project["set_number"],
                    "name": project["name"],
                    "state": project["state"],
                    "priority": project["priority"],
                    "revision": project["revision"],
                    "created_at": project["created_at"],
                    "updated_at": project["updated_at"],
                    "draft_source": project.get("draft_source"),
                    "set_metadata": project.get("set_metadata"),
                    "bom": (
                        {
                            "provider": project["bom"]["provider"],
                            "summary": project["bom"]["summary"],
                        }
                        if project.get("bom")
                        else None
                    ),
                    "reconciliation": {
                        "status": project["reconciliation"]["status"],
                        "summary": project["reconciliation"].get("summary"),
                        "issue_count": len(project["reconciliation"].get("issues", [])),
                        "unmapped_count": len(project["reconciliation"].get("unmapped", [])),
                    },
                    "capacity_plan": (
                        {
                            "status": project["capacity_plan"]["status"],
                            "summary": project["capacity_plan"].get("summary"),
                        }
                        if project.get("capacity_plan")
                        else None
                    ),
                    "progress": project["progress"]["summary"],
                    "readiness": project["readiness"],
                }
            )
        return summaries

    def _selected_bom(
        self, conn: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any] | None:
        revision_id = row["selected_bom_revision_id"]
        if revision_id is None:
            return None
        bom_row = conn.execute(
            "SELECT * FROM harvest_bom_revisions WHERE bom_revision_id = ? AND project_id = ?",
            (revision_id, row["project_id"]),
        ).fetchone()
        if bom_row is None:
            raise HarvestProjectError(
                "BOM_REVISION_MISSING", "The selected BOM revision is missing."
            )
        content = bytes(bom_row["source_blob"])
        if hashlib.sha256(content).hexdigest() != bom_row["source_sha256"]:
            raise HarvestProjectError(
                "BOM_SOURCE_HASH_MISMATCH", "The frozen BOM source has changed."
            )
        normalized = _decode_json(bom_row["normalized_json"], {})
        claimed_manifest_hash = normalized.get("normalized_sha256")
        normalized_without_hash = dict(normalized)
        normalized_without_hash.pop("normalized_sha256", None)
        actual_manifest_hash = hashlib.sha256(
            _canonical_json(normalized_without_hash).encode("utf-8")
        ).hexdigest()
        if (
            claimed_manifest_hash != bom_row["normalized_sha256"]
            or actual_manifest_hash != claimed_manifest_hash
        ):
            raise HarvestProjectError(
                "BOM_MANIFEST_HASH_MISMATCH", "The frozen BOM manifest has changed."
            )
        return {
            "bom_revision_id": bom_row["bom_revision_id"],
            "source_sha256": bom_row["source_sha256"],
            "provider": bom_row["provider"],
            "filename": bom_row["filename"],
            "created_at": bom_row["created_at"],
            **normalized,
        }

    def _set_metadata(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        selected_bom: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        if selected_bom is not None:
            candidates.append(selected_bom)
        other_rows = conn.execute(
            "SELECT * FROM harvest_projects WHERE set_number = ? AND project_id != ? AND selected_bom_revision_id IS NOT NULL ORDER BY updated_at DESC",
            (row["set_number"], row["project_id"]),
        ).fetchall()
        for other_row in other_rows:
            other_bom = self._selected_bom(conn, other_row)
            if other_bom is not None:
                candidates.append(other_bom)
        for candidate in candidates:
            metadata = candidate.get("set_metadata")
            if isinstance(metadata, dict):
                return {
                    **metadata,
                    "source_bom_revision_id": candidate["bom_revision_id"],
                    "snapshot_created_at": candidate["created_at"],
                }
        return None

    def save_bom(
        self,
        project_id: str,
        *,
        content: bytes,
        filename: str | None,
        provider: str,
    ) -> dict[str, Any]:
        with _STORE_LOCK, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._project_row(conn, project_id)
            self._ensure_not_active(row)
            parsed = parse_bom_upload(
                content,
                filename=filename,
                provider=provider,
                default_set_number=row["set_number"],
            )
            existing = conn.execute(
                "SELECT bom_revision_id FROM harvest_bom_revisions WHERE project_id = ? AND source_sha256 = ?",
                (project_id, parsed["source_sha256"]),
            ).fetchone()
            if existing:
                revision_id = str(existing["bom_revision_id"])
            else:
                revision_id = f"bom-{uuid.uuid4().hex}"
                conn.execute(
                    "INSERT INTO harvest_bom_revisions(bom_revision_id, project_id, source_sha256, normalized_sha256, provider, filename, source_blob, normalized_json, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        revision_id,
                        project_id,
                        parsed["source_sha256"],
                        parsed["normalized_sha256"],
                        parsed["provider"],
                        parsed["filename"],
                        content,
                        _canonical_json(parsed),
                        _now(),
                    ),
                )
            changed = row["selected_bom_revision_id"] != revision_id
            if changed:
                conn.execute(
                    "UPDATE harvest_projects SET selected_bom_revision_id = ?, revision = revision + 1, state = 'review', capacity_plan_json = NULL, simulation_json = NULL, acceptance_json = NULL, green_light_json = NULL, activation_json = NULL, updated_at = ? WHERE project_id = ?",
                    (revision_id, _now(), project_id),
                )
                self._append_event(
                    conn,
                    project_id,
                    "bom_revision_selected",
                    {
                        "bom_revision_id": revision_id,
                        "source_sha256": parsed["source_sha256"],
                        "normalized_sha256": parsed["normalized_sha256"],
                        "provider": parsed["provider"],
                    },
                )
            conn.commit()
        return self.get_project(project_id)

    def update_review(
        self,
        project_id: str,
        *,
        policy: Any | None = None,
        mappings: Any | None = None,
        priority: int | None = None,
        match_policy: str | None = None,
        fallback_mode: str | None = None,
    ) -> dict[str, Any]:
        with _STORE_LOCK, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._project_row(conn, project_id)
            self._ensure_not_active(row)
            draft = project_harvest.load_bsx_draft(self.root, row["draft_id"])
            next_policy = (
                _decode_json(row["policy_json"], {})
                if policy is None
                else _validate_policy(policy, draft)
            )
            next_mappings = (
                _decode_json(row["mapping_json"], {})
                if mappings is None
                else _validate_mappings(mappings)
            )
            next_priority = int(row["priority"]) if priority is None else priority
            next_match = (
                str(row["match_policy"]) if match_policy is None else match_policy
            )
            next_fallback = (
                str(row["fallback_mode"]) if fallback_mode is None else fallback_mode
            )
            if (
                isinstance(next_priority, bool)
                or not isinstance(next_priority, int)
                or not 0 <= next_priority <= 10_000
            ):
                raise HarvestProjectError(
                    "INVALID_PRIORITY", "Priority must be between 0 and 10000."
                )
            if next_match not in _ALLOWED_MATCH_POLICIES:
                raise HarvestProjectError(
                    "INVALID_MATCH_POLICY", "Unsupported match policy."
                )
            if next_fallback not in _ALLOWED_FALLBACK_MODES:
                raise HarvestProjectError(
                    "INVALID_FALLBACK_MODE", "Unsupported fallback mode."
                )
            update_payload = {
                "policy": next_policy,
                "mappings": next_mappings,
                "priority": next_priority,
                "match_policy": next_match,
                "fallback_mode": next_fallback,
            }
            current_payload = {
                "policy": _decode_json(row["policy_json"], {}),
                "mappings": _decode_json(row["mapping_json"], {}),
                "priority": int(row["priority"]),
                "match_policy": str(row["match_policy"]),
                "fallback_mode": str(row["fallback_mode"]),
            }
            if _canonical_json(update_payload) == _canonical_json(current_payload):
                conn.rollback()
                return self.get_project(project_id)

            routing_changed = any(
                update_payload[name] != current_payload[name]
                for name in ("policy", "mappings", "match_policy", "fallback_mode")
            )
            if routing_changed:
                conn.execute(
                    "UPDATE harvest_projects SET policy_json = ?, mapping_json = ?, priority = ?, match_policy = ?, fallback_mode = ?, state = 'review', revision = revision + 1, capacity_plan_json = NULL, simulation_json = NULL, acceptance_json = NULL, green_light_json = NULL, activation_json = NULL, updated_at = ? WHERE project_id = ?",
                    (
                        _canonical_json(next_policy),
                        _canonical_json(next_mappings),
                        next_priority,
                        next_match,
                        next_fallback,
                        _now(),
                        project_id,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE harvest_projects SET priority = ?, revision = revision + 1, updated_at = ? WHERE project_id = ?",
                    (next_priority, _now(), project_id),
                )
            self._append_event(
                conn,
                project_id,
                "review_updated",
                {
                    **update_payload,
                    "downstream_evidence_invalidated": routing_changed,
                },
            )
            conn.commit()
        return self.get_project(project_id)

    @staticmethod
    def _source_namespace(draft: dict[str, Any]) -> dict[str, str]:
        raw = draft.get("source", {}).get("identifier_namespace")
        if (
            isinstance(raw, dict)
            and isinstance(raw.get("part"), str)
            and isinstance(raw.get("color"), str)
        ):
            return {"part": raw["part"], "color": raw["color"]}
        return {"part": "bricklink_item_number", "color": "bricklink_color_id"}

    @staticmethod
    def _canonical_element(
        part: dict[str, Any],
        source_namespace: dict[str, str],
        bom_namespace: dict[str, str],
        mappings: dict[str, Any],
    ) -> tuple[str, str] | None:
        part_id = str(part.get("item_id") or part.get("part_id") or "")
        color_id = str(part.get("color_id") or "")
        if source_namespace["part"] != bom_namespace["part"]:
            part_id = mappings.get("namespace", {}).get("parts", {}).get(part_id, "")
        if source_namespace["color"] != bom_namespace["color"]:
            color_id = mappings.get("namespace", {}).get("colors", {}).get(color_id, "")
        if not part_id or not color_id:
            return None
        return part_id, color_id

    @staticmethod
    def _finalize_effective_group(group: dict[str, Any]) -> dict[str, Any]:
        """Coalesce one group's routable rows by canonical part and color."""

        parts: list[dict[str, Any]] = []
        by_canonical: dict[tuple[str, str], dict[str, Any]] = {}
        for contributor in group.get("parts", []):
            key = (str(contributor["part_id"]), str(contributor["color_id"]))
            quantity = int(contributor["quantity"])
            existing = by_canonical.get(key)
            if existing is None:
                effective_part = {
                    **contributor,
                    "part_id": key[0],
                    "color_id": key[1],
                    "quantity": quantity,
                }
                source_rows = contributor.get("source_rows")
                if isinstance(source_rows, list):
                    effective_part["source_rows"] = list(source_rows)
                by_canonical[key] = effective_part
                parts.append(effective_part)
                continue

            existing["quantity"] += quantity
            source_rows = contributor.get("source_rows")
            if isinstance(source_rows, list):
                existing.setdefault("source_rows", []).extend(source_rows)

        return {
            **group,
            "quantity": sum(int(part["quantity"]) for part in parts),
            "parts": parts,
        }

    def _effective_groups(
        self,
        row: sqlite3.Row,
        draft: dict[str, Any],
        bom: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        policy = _decode_json(row["policy_json"], {})
        mappings = _decode_json(row["mapping_json"], {})
        source_namespace = self._source_namespace(draft)
        bom_namespace = (
            bom.get("namespace", source_namespace) if bom else source_namespace
        )
        fallback = row["fallback_mode"]
        if fallback in {"adaptive_part", "inventory_first"} and bom is not None:
            groups: list[dict[str, Any]] = []
            groups_by_id: dict[str, dict[str, Any]] = {}
            for item in bom["items"]:
                if (
                    not item.get("sortable", True)
                    and policy.get("non_sortable_action") != "include"
                ):
                    continue
                group_id = f"part:{item['part_id']}:{item['color_id']}"
                group = groups_by_id.get(group_id)
                if group is None:
                    group = {
                        "id": group_id,
                        "label": f"{item['part_id']} / color {item['color_id']}",
                        "kind": "adaptive_part"
                        if fallback == "adaptive_part"
                        else "inventory_first",
                        "bag_number": None,
                        "parts": [],
                    }
                    groups_by_id[group_id] = group
                    groups.append(group)
                group["parts"].append(
                    {
                        "part_id": item["part_id"],
                        "color_id": item["color_id"],
                        "quantity": item["quantity"],
                        "item_type": item["item_type"],
                    }
                )
            return [self._finalize_effective_group(group) for group in groups]

        effective: list[dict[str, Any]] = []
        actions = policy.get("group_actions", {})
        for group in draft.get("groups", []):
            action = actions.get(str(group.get("id")), "review")
            if action in {"exclude", "review"}:
                continue
            parts: list[dict[str, Any]] = []
            for part in group.get("parts", []):
                canonical = self._canonical_element(
                    part, source_namespace, bom_namespace, mappings
                )
                if canonical is None:
                    continue
                parts.append(
                    {
                        "part_id": canonical[0],
                        "color_id": canonical[1],
                        "quantity": int(part["quantity"]),
                        "item_type": str(part.get("item_type") or "P"),
                        "source_rows": list(part.get("source_rows") or []),
                    }
                )
            effective.append(
                self._finalize_effective_group(
                    {
                        "id": str(group["id"]),
                        "label": str(group.get("label") or group["id"]),
                        "kind": str(group.get("kind") or "unknown"),
                        "bag_number": group.get("bag_number"),
                        "parts": parts,
                    }
                )
            )
        return effective

    def _reconcile(
        self,
        row: sqlite3.Row,
        draft: dict[str, Any],
        bom: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if bom is None:
            return {
                "status": "unverified",
                "issues": [{"code": "AUTHORITATIVE_BOM_REQUIRED", "severity": "error"}],
                "missing": [],
                "overages": [],
                "unmapped": [],
            }
        policy = _decode_json(row["policy_json"], {})
        if row["fallback_mode"] in {"adaptive_part", "inventory_first"}:
            non_sortable_items = [
                item for item in bom["items"] if not item.get("sortable", True)
            ]
            action = policy.get("non_sortable_action", "exclude")
            issues: list[dict[str, Any]] = []
            if non_sortable_items and action == "review":
                issues.append(
                    {
                        "code": "NON_SORTABLE_POLICY_REQUIRED",
                        "severity": "error",
                        "count": len(non_sortable_items),
                    }
                )
            included_items = [
                item
                for item in bom["items"]
                if item.get("sortable", True) or action == "include"
            ]
            return {
                "status": "incomplete" if issues else "fallback",
                "bom_revision_id": bom["bom_revision_id"],
                "source_namespace": self._source_namespace(draft),
                "bom_namespace": bom["namespace"],
                "fallback_mode": row["fallback_mode"],
                "issues": issues,
                "missing": [],
                "overages": [],
                "extras": [],
                "unmapped": [],
                "summary": {
                    "bom_quantity": sum(
                        int(item["quantity"]) for item in included_items
                    ),
                    "planned_quantity": sum(
                        int(item["quantity"]) for item in included_items
                    ),
                    "extras_quantity": 0,
                    "missing_quantity": 0,
                    "overage_quantity": 0,
                    "non_sortable_excluded_quantity": sum(
                        int(item["quantity"])
                        for item in non_sortable_items
                        if action == "exclude"
                    ),
                },
            }
        mappings = _decode_json(row["mapping_json"], {})
        source_namespace = self._source_namespace(draft)
        bom_namespace = bom["namespace"]
        actions = policy.get("group_actions", {})
        bag_totals: dict[tuple[str, str, str], int] = defaultdict(int)
        extras_totals: dict[tuple[str, str, str], int] = defaultdict(int)
        unmapped: list[dict[str, Any]] = []
        review_groups: list[str] = []
        for group in draft.get("groups", []):
            group_id = str(group.get("id"))
            action = actions.get(group_id, "review")
            if action == "review":
                review_groups.append(group_id)
                continue
            if action == "exclude":
                continue
            for part in group.get("parts", []):
                canonical = self._canonical_element(
                    part, source_namespace, bom_namespace, mappings
                )
                if canonical is None:
                    unmapped.append(
                        {
                            "group_id": group_id,
                            "part_id": str(part.get("item_id")),
                            "color_id": str(part.get("color_id")),
                            "quantity": int(part.get("quantity") or 0),
                        }
                    )
                    continue
                key = (canonical[0], canonical[1], str(part.get("item_type") or "P"))
                target = extras_totals if group.get("kind") == "extras" else bag_totals
                target[key] += int(part["quantity"])
        bom_totals = {
            (item["part_id"], item["color_id"], item["item_type"]): int(
                item["quantity"]
            )
            for item in bom["items"]
            if item.get("sortable", True)
        }
        missing: list[dict[str, Any]] = []
        overages: list[dict[str, Any]] = []
        all_keys = sorted(set(bom_totals) | set(bag_totals))
        for key in all_keys:
            expected = bom_totals.get(key, 0)
            planned = bag_totals.get(key, 0)
            delta = planned - expected
            record = {
                "part_id": key[0],
                "color_id": key[1],
                "item_type": key[2],
                "bom_quantity": expected,
                "planned_quantity": planned,
                "delta": delta,
            }
            if delta < 0:
                missing.append(record)
            elif delta > 0:
                overages.append(record)
        extras = [
            {
                "part_id": key[0],
                "color_id": key[1],
                "item_type": key[2],
                "quantity": quantity,
            }
            for key, quantity in sorted(extras_totals.items())
        ]
        issues: list[dict[str, Any]] = []
        if review_groups:
            issues.append(
                {
                    "code": "GROUP_POLICY_REQUIRED",
                    "severity": "error",
                    "groups": review_groups,
                }
            )
        if unmapped:
            issues.append(
                {
                    "code": "NAMESPACE_MAPPING_REQUIRED",
                    "severity": "error",
                    "count": len(unmapped),
                }
            )
        if missing:
            issues.append(
                {
                    "code": "BOM_ITEMS_MISSING_FROM_PLAN",
                    "severity": "error",
                    "count": len(missing),
                }
            )
        if overages:
            issues.append(
                {
                    "code": "PLAN_EXCEEDS_BOM",
                    "severity": "error",
                    "count": len(overages),
                }
            )
        if review_groups or unmapped:
            status = "incomplete"
        elif missing:
            status = "incomplete"
        elif overages:
            status = "conflicting"
        elif extras:
            status = "validated_with_extras"
        else:
            status = "validated"
        return {
            "status": status,
            "bom_revision_id": bom["bom_revision_id"],
            "source_namespace": source_namespace,
            "bom_namespace": bom_namespace,
            "issues": issues,
            "missing": missing,
            "overages": overages,
            "extras": extras,
            "unmapped": unmapped,
            "summary": {
                "bom_quantity": sum(bom_totals.values()),
                "planned_quantity": sum(bag_totals.values()),
                "extras_quantity": sum(extras_totals.values()),
                "missing_quantity": sum(-item["delta"] for item in missing),
                "overage_quantity": sum(item["delta"] for item in overages),
            },
        }

    def plan_capacity(
        self,
        project_id: str,
        *,
        bins: list[dict[str, Any]],
        assignment_overrides: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(bins, list) or not bins:
            raise HarvestProjectError(
                "NO_BINS", "At least one candidate bin is required."
            )
        normalized_bins: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(bins, start=1):
            if not isinstance(raw, dict):
                raise HarvestProjectError("INVALID_BIN", f"Bin {index} is invalid.")
            bin_id = _identifier(raw.get("bin_id"), f"bin {index} id")
            if bin_id in seen:
                raise HarvestProjectError("DUPLICATE_BIN", f"Duplicate bin {bin_id}.")
            seen.add(bin_id)
            capacity = raw.get("max_pieces")
            if capacity is not None:
                capacity = _positive_int(capacity, f"bin {bin_id} max_pieces")
            tracked_piece_count = raw.get("tracked_piece_count", 0)
            if (
                isinstance(tracked_piece_count, bool)
                or not isinstance(tracked_piece_count, int)
                or tracked_piece_count < 0
            ):
                raise HarvestProjectError(
                    "INVALID_BIN", f"Bin {bin_id} tracked_piece_count is invalid."
                )
            normalized_bins.append(
                {
                    "bin_id": bin_id,
                    "available": bool(raw.get("available", True)),
                    "reserved_by": [str(value) for value in raw.get("reserved_by", [])]
                    if isinstance(raw.get("reserved_by", []), list)
                    else [],
                    "max_pieces": capacity,
                    "layer_index": raw.get("layer_index"),
                    "section_index": raw.get("section_index"),
                    "bin_index": raw.get("bin_index"),
                    "tracked_piece_count": tracked_piece_count,
                }
            )
        with _STORE_LOCK, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._project_row(conn, project_id)
            self._ensure_not_active(row)
            draft = project_harvest.load_bsx_draft(self.root, row["draft_id"])
            bom = self._selected_bom(conn, row)
            groups = self._effective_groups(row, draft, bom)
            plan_groups = [
                *groups,
                {
                    "id": HARVEST_EXCEPTION_GROUP_ID,
                    "label": HARVEST_EXCEPTION_GROUP_LABEL,
                    "kind": "exception",
                    "quantity": 0,
                    "parts": [],
                },
            ]
            # Draft planning intentionally treats every enabled bin as physically
            # empty. Existing category assignments and tracked contents are
            # preserved below as execution-clearance evidence; they must not make
            # an otherwise valid draft impossible to simulate.
            available = [item for item in normalized_bins if item["available"]]
            original_bin_order = {
                item["bin_id"]: index for index, item in enumerate(normalized_bins)
            }
            available.sort(
                key=lambda item: (
                    item["tracked_piece_count"],
                    1 if item["reserved_by"] else 0,
                    original_bin_order[item["bin_id"]],
                )
            )
            if not groups:
                plan = {
                    "status": "blocked",
                    "execution_mode": "unavailable",
                    "issues": [{"code": "NO_EFFECTIVE_GROUPS", "severity": "error"}],
                    "waves": [],
                    "bins": normalized_bins,
                }
            elif not available:
                plan = {
                    "status": "blocked",
                    "execution_mode": "unavailable",
                    "issues": [{"code": "NO_AVAILABLE_BINS", "severity": "error"}],
                    "waves": [],
                    "bins": normalized_bins,
                }
            else:
                waves: list[dict[str, Any]] = []
                for wave_index, offset in enumerate(
                    range(0, len(plan_groups), len(available)), start=1
                ):
                    assignments: list[dict[str, Any]] = []
                    for group, bin_info in zip(
                        plan_groups[offset : offset + len(available)], available
                    ):
                        assignments.append(
                            {
                                "group_id": group["id"],
                                "group_label": group["label"],
                                "quantity": group["quantity"],
                                "bin_id": bin_info["bin_id"],
                            }
                        )
                    waves.append({"wave": wave_index, "assignments": assignments})

                override_map: dict[str, str] = {}
                if assignment_overrides is not None:
                    if not isinstance(assignment_overrides, list):
                        raise HarvestProjectError(
                            "INVALID_CAPACITY_ASSIGNMENTS",
                            "assignment_overrides must be a list.",
                        )
                    known_groups = {group["id"] for group in plan_groups}
                    known_bins = {item["bin_id"]: item for item in available}
                    for index, override in enumerate(assignment_overrides, start=1):
                        if not isinstance(override, dict):
                            raise HarvestProjectError(
                                "INVALID_CAPACITY_ASSIGNMENTS",
                                f"Assignment override {index} is invalid.",
                            )
                        group_id = _identifier(
                            override.get("group_id"),
                            f"assignment override {index} group_id",
                        )
                        bin_id = _identifier(
                            override.get("bin_id"),
                            f"assignment override {index} bin_id",
                        )
                        if group_id not in known_groups:
                            raise HarvestProjectError(
                                "UNKNOWN_CAPACITY_GROUP",
                                f"Unknown capacity group {group_id}.",
                            )
                        if bin_id not in known_bins:
                            raise HarvestProjectError(
                                "UNAVAILABLE_CAPACITY_BIN",
                                f"Bin {bin_id} is not enabled for this plan.",
                            )
                        if group_id in override_map:
                            raise HarvestProjectError(
                                "DUPLICATE_CAPACITY_GROUP",
                                f"Group {group_id} has more than one assignment override.",
                            )
                        override_map[group_id] = bin_id

                    for wave in waves:
                        for assignment in wave["assignments"]:
                            assignment["bin_id"] = override_map.get(
                                assignment["group_id"], assignment["bin_id"]
                            )

                for wave in waves:
                    used_bins: set[str] = set()
                    for assignment in wave["assignments"]:
                        bin_id = assignment["bin_id"]
                        if bin_id in used_bins:
                            raise HarvestProjectError(
                                "DUPLICATE_CAPACITY_BIN_ASSIGNMENT",
                                f"Bin {bin_id} is assigned more than once in wave {wave['wave']}.",
                            )
                        used_bins.add(bin_id)

                bin_by_id = {item["bin_id"]: item for item in available}
                overflow: list[dict[str, Any]] = []
                planned_bin_ids: set[str] = set()
                for wave in waves:
                    for assignment in wave["assignments"]:
                        bin_info = bin_by_id[assignment["bin_id"]]
                        planned_bin_ids.add(bin_info["bin_id"])
                        if (
                            bin_info["max_pieces"] is not None
                            and assignment["quantity"] > bin_info["max_pieces"]
                        ):
                            overflow.append(
                                {
                                    "group_id": assignment["group_id"],
                                    "quantity": assignment["quantity"],
                                    "bin_id": bin_info["bin_id"],
                                    "capacity": bin_info["max_pieces"],
                                }
                            )

                clearance_bins = [
                    {
                        "bin_id": item["bin_id"],
                        "reserved_by": item["reserved_by"],
                        "tracked_piece_count": item["tracked_piece_count"],
                    }
                    for item in normalized_bins
                    if item["bin_id"] in planned_bin_ids
                    and (item["reserved_by"] or item["tracked_piece_count"] > 0)
                ]
                issues: list[dict[str, Any]] = []
                if overflow:
                    issues.append(
                        {
                            "code": "BIN_CAPACITY_EXCEEDED",
                            "severity": "error",
                            "count": len(overflow),
                        }
                    )
                if clearance_bins:
                    issues.append(
                        {
                            "code": "BIN_CLEARANCE_REQUIRED",
                            "severity": "warning",
                            "count": len(clearance_bins),
                        }
                    )
                plan = {
                    "status": "blocked" if overflow else "ready",
                    "execution_mode": "single_wave" if len(waves) == 1 else "two_stage",
                    "planning_assumption": "enabled_bins_assumed_empty",
                    "selection_strategy": "least_recorded_contents",
                    "issues": issues,
                    "summary": {
                        "required_groups": len(plan_groups),
                        "bag_destinations": len(groups),
                        "exception_destinations": 1,
                        "available_bins": len(available),
                        "wave_count": len(waves),
                        "reserved_bins_excluded": 0,
                        "reserved_bins_observed": sum(
                            1 for item in normalized_bins if item["reserved_by"]
                        ),
                        "planned_bins_requiring_clearance": len(clearance_bins),
                    },
                    "clearance": {
                        "status": "required" if clearance_bins else "clear",
                        "bins": clearance_bins,
                    },
                    "assignment_overrides": [
                        {"group_id": group_id, "bin_id": bin_id}
                        for group_id, bin_id in sorted(override_map.items())
                    ],
                    "overflow": overflow,
                    "waves": waves,
                    "bins": normalized_bins,
                    "exception_destination": next(
                        (
                            assignment
                            for wave in waves
                            for assignment in wave["assignments"]
                            if assignment["group_id"] == HARVEST_EXCEPTION_GROUP_ID
                        ),
                        None,
                    ),
                }
            reconciliation = self._reconcile(row, draft, bom)
            ready_statuses = {"validated", "validated_with_extras", "fallback"}
            next_state = (
                "ready"
                if plan["status"] == "ready"
                and reconciliation["status"] in ready_statuses
                else "review"
            )
            conn.execute(
                "UPDATE harvest_projects SET capacity_plan_json = ?, state = ?, revision = revision + 1, simulation_json = NULL, acceptance_json = NULL, green_light_json = NULL, activation_json = NULL, updated_at = ? WHERE project_id = ?",
                (_canonical_json(plan), next_state, _now(), project_id),
            )
            self._append_event(conn, project_id, "capacity_planned", plan)
            conn.commit()
        return self.get_project(project_id)

    @staticmethod
    def _substitution_target(
        part_id: str,
        color_id: str,
        match_policy: str,
        mappings: dict[str, Any],
    ) -> tuple[str, str, str]:
        for item in mappings.get("substitutions", []):
            if item["input"] == {"part_id": part_id, "color_id": color_id}:
                if _MATCH_POLICY_RANK[item["kind"]] <= _MATCH_POLICY_RANK[match_policy]:
                    return (
                        item["target"]["part_id"],
                        item["target"]["color_id"],
                        item["kind"],
                    )
        return part_id, color_id, "exact"

    def simulate(
        self, project_id: str, *, inventory: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        with _STORE_LOCK, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._project_row(conn, project_id)
            self._ensure_not_active(row)
            draft = project_harvest.load_bsx_draft(self.root, row["draft_id"])
            bom = self._selected_bom(conn, row)
            if bom is None:
                raise HarvestProjectError(
                    "AUTHORITATIVE_BOM_REQUIRED", "Freeze a BOM before simulation."
                )
            groups = self._effective_groups(row, draft, bom)
            if inventory is None:
                policy = _decode_json(row["policy_json"], {})
                inventory = [
                    {
                        "part_id": item["part_id"],
                        "color_id": item["color_id"],
                        "quantity": item["quantity"],
                    }
                    for item in bom["items"]
                    if item.get("sortable", True)
                    or policy.get("non_sortable_action") == "include"
                ]
            if not isinstance(inventory, list) or len(inventory) > MAX_BOM_ROWS:
                raise HarvestProjectError(
                    "INVALID_INVENTORY", "Simulation inventory is invalid."
                )
            available: dict[tuple[str, str], int] = defaultdict(int)
            mappings = _decode_json(row["mapping_json"], {})
            for index, item in enumerate(inventory, start=1):
                if not isinstance(item, dict):
                    raise HarvestProjectError(
                        "INVALID_INVENTORY", f"Inventory row {index} is invalid."
                    )
                source_part = _identifier(
                    item.get("part_id"), f"inventory row {index} part_id"
                )
                source_color = _identifier(
                    item.get("color_id"), f"inventory row {index} color_id"
                )
                part_id, color_id, _ = self._substitution_target(
                    source_part, source_color, row["match_policy"], mappings
                )
                available[(part_id, color_id)] += _positive_int(
                    item.get("quantity", 1), f"inventory row {index} quantity"
                )
            routes: list[dict[str, Any]] = []
            missing: list[dict[str, Any]] = []
            for group in groups:
                for part in group["parts"]:
                    key = (part["part_id"], part["color_id"])
                    requested = int(part["quantity"])
                    routed = min(requested, available.get(key, 0))
                    if routed:
                        routes.append(
                            {
                                "group_id": group["id"],
                                "part_id": key[0],
                                "color_id": key[1],
                                "quantity": routed,
                            }
                        )
                        available[key] -= routed
                    if routed < requested:
                        missing.append(
                            {
                                "group_id": group["id"],
                                "part_id": key[0],
                                "color_id": key[1],
                                "quantity": requested - routed,
                            }
                        )
            surplus = [
                {"part_id": key[0], "color_id": key[1], "quantity": quantity}
                for key, quantity in sorted(available.items())
                if quantity > 0
            ]
            capacity = _decode_json(row["capacity_plan_json"], None)
            reconciliation = self._reconcile(row, draft, bom)
            passed = (
                not missing
                and reconciliation["status"]
                in {"validated", "validated_with_extras", "fallback"}
                and isinstance(capacity, dict)
                and capacity.get("status") == "ready"
            )
            simulation = {
                "run_id": f"simulation-{uuid.uuid4().hex}",
                "status": "passed" if passed else "failed",
                "created_at": _now(),
                "summary": {
                    "routed_quantity": sum(item["quantity"] for item in routes),
                    "missing_quantity": sum(item["quantity"] for item in missing),
                    "surplus_quantity": sum(item["quantity"] for item in surplus),
                    "route_count": len(routes),
                },
                "missing": missing,
                "surplus": surplus,
                "routes": routes,
            }
            next_state = "simulated" if passed else "review"
            conn.execute(
                "UPDATE harvest_projects SET simulation_json = ?, acceptance_json = NULL, green_light_json = NULL, activation_json = NULL, state = ?, revision = revision + 1, updated_at = ? WHERE project_id = ?",
                (_canonical_json(simulation), next_state, _now(), project_id),
            )
            self._append_event(conn, project_id, "simulation_completed", simulation)
            conn.commit()
        return self.get_project(project_id)

    def _confirmed_progress(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        *,
        mode: str = "live",
        runtime_id: str | None = None,
    ) -> dict[tuple[str, str, str], int]:
        params: list[Any] = [project_id, mode]
        runtime_clause = ""
        if runtime_id is not None:
            runtime_clause = " AND runtime_id = ?"
            params.append(runtime_id)
        rows = conn.execute(
            "SELECT group_id, part_id, color_id, SUM(quantity) AS quantity "
            "FROM harvest_allocations WHERE project_id = ? AND mode = ? "
            f"AND status = 'confirmed'{runtime_clause} "
            "GROUP BY group_id, part_id, color_id",
            params,
        ).fetchall()
        return {
            (str(row["group_id"]), str(row["part_id"]), str(row["color_id"])): int(
                row["quantity"]
            )
            for row in rows
        }

    def _reserved_progress(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        *,
        mode: str,
        runtime_id: str | None = None,
    ) -> dict[tuple[str, str, str], int]:
        params: list[Any] = [project_id, mode]
        if mode == "live" and runtime_id is not None:
            # Confirmed live drops are durable project consumption across
            # activations. Only an outstanding plan from this runtime reserves
            # additional quota; the row's exclusive status prevents double use
            # while it transitions from planned to confirmed.
            status_clause = (
                "AND (status = 'confirmed' OR (status = 'planned' AND runtime_id = ?))"
            )
            params.append(runtime_id)
        else:
            runtime_clause = ""
            if runtime_id is not None:
                runtime_clause = " AND runtime_id = ?"
                params.append(runtime_id)
            status_clause = f"AND status IN ('planned', 'confirmed'){runtime_clause}"
        rows = conn.execute(
            "SELECT group_id, part_id, color_id, SUM(quantity) AS quantity "
            "FROM harvest_allocations WHERE project_id = ? AND mode = ? "
            f"{status_clause} "
            "GROUP BY group_id, part_id, color_id",
            params,
        ).fetchall()
        return {
            (str(row["group_id"]), str(row["part_id"]), str(row["color_id"])): int(
                row["quantity"]
            )
            for row in rows
        }

    def propose_allocation(
        self,
        project_id: str,
        *,
        piece_id: str,
        part_id: str,
        color_id: str,
        quantity: int = 1,
        mode: str = "simulation",
    ) -> dict[str, Any]:
        if mode != "simulation":
            raise HarvestProjectError(
                "LIVE_ROUTING_DISABLED",
                "This draft release accepts allocation events only in simulation mode.",
            )
        piece_key = _identifier(piece_id, "piece_id")
        requested_quantity = _positive_int(quantity, "quantity")
        with _STORE_LOCK, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._project_row(conn, project_id)
            self._ensure_not_active(row)
            existing = conn.execute(
                "SELECT * FROM harvest_allocations WHERE project_id = ? AND piece_id = ?",
                (project_id, piece_key),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return self._allocation_dict(existing)
            draft = project_harvest.load_bsx_draft(self.root, row["draft_id"])
            bom = self._selected_bom(conn, row)
            groups = self._effective_groups(row, draft, bom)
            mappings = _decode_json(row["mapping_json"], {})
            target_part, target_color, match_kind = self._substitution_target(
                _identifier(part_id, "part_id"),
                _identifier(color_id, "color_id"),
                row["match_policy"],
                mappings,
            )
            progress = self._reserved_progress(
                conn, project_id, mode="simulation"
            )
            target: dict[str, Any] | None = None
            remaining = 0
            for group in groups:
                for part in group["parts"]:
                    if (
                        part["part_id"] != target_part
                        or part["color_id"] != target_color
                    ):
                        continue
                    key = (group["id"], target_part, target_color)
                    quota_remaining = int(part["quantity"]) - progress.get(key, 0)
                    if quota_remaining > 0:
                        target = group
                        remaining = quota_remaining
                        break
                if target is not None:
                    break
            if target is None:
                raise HarvestProjectError(
                    "NO_INCOMPLETE_QUOTA",
                    "No incomplete Harvest quota accepts this part and color.",
                    details={"part_id": target_part, "color_id": target_color},
                )
            allocation_quantity = min(requested_quantity, remaining)
            allocation_id = f"allocation-{uuid.uuid4().hex}"
            created_at = _now()
            conn.execute(
                "INSERT INTO harvest_allocations(allocation_id, project_id, piece_id, group_id, part_id, color_id, quantity, match_kind, mode, status, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?)",
                (
                    allocation_id,
                    project_id,
                    piece_key,
                    target["id"],
                    target_part,
                    target_color,
                    allocation_quantity,
                    match_kind,
                    mode,
                    created_at,
                ),
            )
            self._append_event(
                conn,
                project_id,
                "allocation_proposed",
                {
                    "allocation_id": allocation_id,
                    "piece_id": piece_key,
                    "group_id": target["id"],
                    "part_id": target_part,
                    "color_id": target_color,
                    "quantity": allocation_quantity,
                    "match_kind": match_kind,
                    "mode": mode,
                },
            )
            conn.commit()
            stored = conn.execute(
                "SELECT * FROM harvest_allocations WHERE allocation_id = ?",
                (allocation_id,),
            ).fetchone()
            assert stored is not None
            return self._allocation_dict(stored)

    def propose_live_allocation(
        self,
        *,
        piece_id: str,
        part_id: str | None,
        color_id: str | None,
        item_candidates: list[dict[str, Any]] | None = None,
        color_candidates: list[dict[str, Any]] | None = None,
        classification_attempts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Atomically reserve the next bag quota for one physical piece."""

        piece_key = _identifier(piece_id, "piece_id")
        with _STORE_LOCK, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            runtime_row = conn.execute(
                "SELECT * FROM harvest_runtime_state WHERE singleton_id = 1"
            ).fetchone()
            if runtime_row is None:
                raise HarvestProjectError(
                    "NO_ACTIVE_HARVEST_PROJECT", "No Harvest project is active."
                )
            project_id = str(runtime_row["project_id"])
            activation = _decode_json(runtime_row["activation_json"], {})
            runtime_mode = str(activation.get("runtime_mode") or "live")
            row = self._project_row(conn, project_id)
            if row["state"] != "active" or activation.get("status") != "active":
                raise HarvestProjectError(
                    "RUNTIME_STATE_MISMATCH",
                    "Harvest runtime state is inconsistent. Keep the sorter paused.",
                )
            existing = conn.execute(
                "SELECT * FROM harvest_allocations WHERE project_id = ? AND piece_id = ?",
                (project_id, piece_key),
            ).fetchone()
            assignments = {
                item["group_id"]: item for item in activation.get("assignments", [])
            }
            if existing is not None:
                destination = assignments.get(str(existing["group_id"]))
                if destination is None:
                    raise HarvestProjectError(
                        "LIVE_ASSIGNMENT_MISSING",
                        "The existing allocation has no active physical destination.",
                    )
                conn.commit()
                return {
                    **self._allocation_dict(existing),
                    "destination": destination,
                    "activation_id": activation["activation_id"],
                    "exception": existing["group_id"] == HARVEST_EXCEPTION_GROUP_ID,
                }
            if runtime_mode not in {"live", "acceptance"}:
                raise HarvestProjectError(
                    "RUNTIME_STATE_MISMATCH",
                    "Harvest runtime mode is invalid. Keep the sorter paused.",
                )
            runtime_id = str(activation.get("activation_id") or "")
            stale = conn.execute(
                "SELECT piece_id, allocation_id FROM harvest_allocations "
                "WHERE project_id = ? AND runtime_id = ? AND mode = ? "
                "AND status = 'planned' ORDER BY created_at LIMIT 1",
                (project_id, runtime_id, runtime_mode),
            ).fetchone()
            if stale is not None:
                raise HarvestProjectError(
                    "LIVE_ALLOCATION_RECOVERY_REQUIRED",
                    "A prior physical allocation is still unconfirmed. Keep the sorter paused for recovery.",
                    details={
                        "piece_id": stale["piece_id"],
                        "allocation_id": stale["allocation_id"],
                    },
                )

            bom = self._selected_bom(conn, row)
            if bom is None or bom["bom_revision_id"] != activation.get("bom_revision_id"):
                raise HarvestProjectError(
                    "ACTIVE_BOM_MISMATCH",
                    "The active Harvest BOM no longer matches its activation.",
                )
            draft = project_harvest.load_bsx_draft(self.root, row["draft_id"])
            groups = self._effective_groups(row, draft, bom)
            progress = self._reserved_progress(
                conn,
                project_id,
                mode=runtime_mode,
                runtime_id=runtime_id,
            )
            alias_map = {
                (
                    item["input"]["part_id"],
                    item["input"]["color_id"],
                ): (
                    item["target"]["part_id"],
                    item["target"]["color_id"],
                )
                for item in bom.get("runtime_aliases", {}).get("entries", [])
            }
            available_groups: dict[tuple[str, str], dict[str, Any]] = {}
            configured_targets: set[tuple[str, str]] = set()
            for group in groups:
                for part in group["parts"]:
                    target_key = (str(part["part_id"]), str(part["color_id"]))
                    configured_targets.add(target_key)
                    if (
                        target_key not in available_groups
                        and int(part["quantity"])
                        > progress.get((str(group["id"]), *target_key), 0)
                    ):
                        available_groups[target_key] = group

            applied_part = (
                _identifier(part_id, "part_id") if part_id is not None else None
            )
            applied_color = (
                _identifier(color_id, "color_id") if color_id is not None else None
            )
            normalized_items = _recognition_candidates(item_candidates)
            normalized_colors = _recognition_candidates(color_candidates)
            candidate_evidence = bool(normalized_items and normalized_colors)
            candidate_pairs: list[dict[str, Any]] = []
            if candidate_evidence:
                for item in normalized_items:
                    for color in normalized_colors:
                        candidate_pairs.append(
                            {
                                "part_id": item["id"],
                                "color_id": color["id"],
                                "item_score": item["score"],
                                "color_score": color["score"],
                                "source": "applied_request_candidates",
                            }
                        )
            elif applied_part is not None and applied_color is not None:
                candidate_pairs.append(
                    {
                        "part_id": applied_part,
                        "color_id": applied_color,
                        "item_score": 1.0,
                        "color_score": 1.0,
                        "source": "applied_result_legacy",
                    }
                )

            for attempt in (classification_attempts or [])[
                :HARVEST_MAX_RECOGNITION_CANDIDATES
            ]:
                if not isinstance(attempt, dict):
                    continue
                attempt_part = str(attempt.get("part_id") or "").strip()
                attempt_color = str(attempt.get("color_id") or "").strip()
                item_score = _recognition_score(attempt.get("item_score"))
                color_score = _recognition_score(attempt.get("color_score"))
                if (
                    not _IDENTIFIER_RE.fullmatch(attempt_part)
                    or not _IDENTIFIER_RE.fullmatch(attempt_color)
                    or item_score is None
                    or color_score is None
                ):
                    continue
                candidate_evidence = True
                candidate_pairs.append(
                    {
                        "part_id": attempt_part,
                        "color_id": attempt_color,
                        "item_score": item_score,
                        "color_score": color_score,
                        "source": f"attempt:{str(attempt.get('strategy') or 'unknown')[:32]}",
                    }
                )

            eligible: dict[tuple[str, str], dict[str, Any]] = {}
            compatible_but_exhausted = False
            compatible_but_low_confidence = False
            for candidate in candidate_pairs:
                input_key = (candidate["part_id"], candidate["color_id"])
                target = alias_map.get(input_key)
                alias_kind = "runtime_alias"
                equivalent_part: str | None = None
                if target is None:
                    for equivalent in HARVEST_MOLD_EQUIVALENTS.get(
                        candidate["part_id"], ()
                    ):
                        target = alias_map.get((equivalent, candidate["color_id"]))
                        if target is not None:
                            alias_kind = "runtime_mold_equivalent"
                            equivalent_part = equivalent
                            break
                if target is None:
                    continue
                target_key = (str(target[0]), str(target[1]))
                target_group = available_groups.get(target_key)
                if target_group is None:
                    if target_key in configured_targets:
                        compatible_but_exhausted = True
                    continue
                if candidate_evidence and (
                    candidate["item_score"] < HARVEST_MIN_ITEM_SCORE
                    or candidate["color_score"] < HARVEST_MIN_COLOR_SCORE
                ):
                    compatible_but_low_confidence = True
                    continue
                resolved = {
                    **candidate,
                    "target_part_id": target_key[0],
                    "target_color_id": target_key[1],
                    "target_group": target_group,
                    "alias_kind": alias_kind,
                    "equivalent_part_id": equivalent_part,
                    "pair_score": round(
                        candidate["item_score"] * candidate["color_score"], 9
                    ),
                }
                previous = eligible.get(target_key)
                if previous is None or (
                    resolved["pair_score"],
                    resolved["item_score"],
                    resolved["color_score"],
                ) > (
                    previous["pair_score"],
                    previous["item_score"],
                    previous["color_score"],
                ):
                    eligible[target_key] = resolved

            ranked = sorted(
                eligible.values(),
                key=lambda item: (
                    -item["pair_score"],
                    -item["item_score"],
                    -item["color_score"],
                    item["target_part_id"],
                    item["target_color_id"],
                ),
            )
            selected = ranked[0] if ranked else None
            ambiguous = bool(
                candidate_evidence
                and selected is not None
                and len(ranked) > 1
                and selected["pair_score"] - ranked[1]["pair_score"]
                < HARVEST_MIN_PAIR_MARGIN
            )
            if ambiguous:
                selected = None

            resolution: dict[str, Any] = {
                "resolver_version": HARVEST_RESOLVER_VERSION,
                "strategy": "bom_constrained_candidates",
                "applied_input": {
                    "part_id": applied_part,
                    "color_id": applied_color,
                },
                "candidate_evidence": candidate_evidence,
                "candidate_pair_count": len(candidate_pairs),
                "eligible_target_count": len(ranked),
                "thresholds": {
                    "min_item_score": HARVEST_MIN_ITEM_SCORE,
                    "min_color_score": HARVEST_MIN_COLOR_SCORE,
                    "min_pair_margin": HARVEST_MIN_PAIR_MARGIN,
                },
                "alternatives": [
                    {
                        "input": {
                            "part_id": item["part_id"],
                            "color_id": item["color_id"],
                        },
                        "target": {
                            "part_id": item["target_part_id"],
                            "color_id": item["target_color_id"],
                        },
                        "item_score": item["item_score"],
                        "color_score": item["color_score"],
                        "pair_score": item["pair_score"],
                        "source": item["source"],
                        "alias_kind": item["alias_kind"],
                    }
                    for item in ranked[:5]
                ],
            }
            target_group: dict[str, Any] | None = None
            target_part: str | None = None
            target_color: str | None = None
            if selected is not None:
                target_group = selected["target_group"]
                target_part = selected["target_part_id"]
                target_color = selected["target_color_id"]
                match_kind = selected["alias_kind"]
                if (selected["part_id"], selected["color_id"]) != (
                    applied_part,
                    applied_color,
                ):
                    match_kind = (
                        "runtime_candidate_mold_equivalent"
                        if selected["alias_kind"] == "runtime_mold_equivalent"
                        else "runtime_candidate"
                    )
                resolution["decision"] = "bag"
                resolution["selected_input"] = {
                    "part_id": selected["part_id"],
                    "color_id": selected["color_id"],
                }
                resolution["selected_target"] = {
                    "part_id": target_part,
                    "color_id": target_color,
                }
                resolution["selected_scores"] = {
                    "item": selected["item_score"],
                    "color": selected["color_score"],
                    "pair": selected["pair_score"],
                }
                group_id = str(target_group["id"])
                stored_part = str(target_part)
                stored_color = str(target_color)
            else:
                group_id = HARVEST_EXCEPTION_GROUP_ID
                stored_part = str(applied_part or "unidentified")
                stored_color = str(applied_color or "unidentified")
                match_kind = (
                    "ambiguous_candidate_exception"
                    if ambiguous
                    else "surplus_exception"
                    if compatible_but_exhausted
                    else "low_confidence_exception"
                    if compatible_but_low_confidence
                    else "unmapped_exception"
                    if applied_part is not None and applied_color is not None
                    else "unidentified_exception"
                )
                resolution["decision"] = "exception"
                resolution["reason"] = match_kind

            destination = assignments.get(group_id)
            if destination is None:
                raise HarvestProjectError(
                    "LIVE_ASSIGNMENT_MISSING",
                    f"No active physical destination is assigned to {group_id}.",
                )
            allocation_id = f"allocation-{uuid.uuid4().hex}"
            created_at = _now()
            conn.execute(
                "INSERT INTO harvest_allocations(allocation_id, project_id, runtime_id, piece_id, group_id, part_id, color_id, quantity, match_kind, mode, status, resolution_json, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 'planned', ?, ?)",
                (
                    allocation_id,
                    project_id,
                    runtime_id,
                    piece_key,
                    group_id,
                    stored_part,
                    stored_color,
                    match_kind,
                    runtime_mode,
                    _canonical_json(resolution),
                    created_at,
                ),
            )
            self._append_event(
                conn,
                project_id,
                "acceptance_allocation_reserved"
                if runtime_mode == "acceptance"
                else "live_allocation_reserved",
                {
                    "allocation_id": allocation_id,
                    "piece_id": piece_key,
                    "group_id": group_id,
                    "part_id": stored_part,
                    "color_id": stored_color,
                    "match_kind": match_kind,
                    "bin_id": destination["bin_id"],
                    "runtime_id": runtime_id,
                    "runtime_mode": runtime_mode,
                    "resolution": resolution,
                },
                actor="sorter",
            )
            conn.commit()
            stored = conn.execute(
                "SELECT * FROM harvest_allocations WHERE allocation_id = ?",
                (allocation_id,),
            ).fetchone()
            assert stored is not None
            return {
                **self._allocation_dict(stored),
                "destination": destination,
                "activation_id": activation["activation_id"],
                "exception": group_id == HARVEST_EXCEPTION_GROUP_ID,
            }

    @staticmethod
    def _allocation_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "allocation_id": row["allocation_id"],
            "project_id": row["project_id"],
            "runtime_id": row["runtime_id"],
            "piece_id": row["piece_id"],
            "group_id": row["group_id"],
            "part_id": row["part_id"],
            "color_id": row["color_id"],
            "quantity": int(row["quantity"]),
            "match_kind": row["match_kind"],
            "mode": row["mode"],
            "status": row["status"],
            "resolution": _decode_json(row["resolution_json"], None),
            "evidence": _decode_json(row["evidence_json"], None),
            "created_at": row["created_at"],
            "confirmed_at": row["confirmed_at"],
            "undone_at": row["undone_at"],
        }

    def confirm_allocation(
        self, project_id: str, allocation_id: str, *, evidence: dict[str, Any]
    ) -> dict[str, Any]:
        if not _ALLOCATION_ID_RE.fullmatch(allocation_id):
            raise HarvestProjectError("INVALID_ALLOCATION_ID", "Invalid allocation id.")
        if not isinstance(evidence, dict):
            raise HarvestProjectError(
                "PHYSICAL_EVIDENCE_REQUIRED",
                "Allocation confirmation evidence is required.",
            )
        with _STORE_LOCK, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            project_row = self._project_row(conn, project_id)
            row = conn.execute(
                "SELECT * FROM harvest_allocations WHERE project_id = ? AND allocation_id = ?",
                (project_id, allocation_id),
            ).fetchone()
            if row is None:
                raise HarvestProjectError(
                    "ALLOCATION_NOT_FOUND", "Allocation not found."
                )
            if row["status"] == "undone":
                raise HarvestProjectError(
                    "ALLOCATION_UNDONE", "An undone allocation cannot be confirmed."
                )
            if row["status"] == "confirmed":
                conn.commit()
                return self._allocation_dict(row)
            bottom_reject = False
            if row["mode"] == "simulation":
                if evidence.get("simulation") is not True:
                    raise HarvestProjectError(
                        "SIMULATION_EVIDENCE_REQUIRED",
                        "Simulation allocations require explicit simulation evidence.",
                    )
            elif row["mode"] in {"live", "acceptance"}:
                runtime_row = conn.execute(
                    "SELECT * FROM harvest_runtime_state WHERE singleton_id = 1 AND project_id = ?",
                    (project_id,),
                ).fetchone()
                if runtime_row is None:
                    raise HarvestProjectError(
                        "PROJECT_NOT_ACTIVE",
                        "The live Harvest allocation cannot be confirmed without its active runtime.",
                    )
                activation = _decode_json(runtime_row["activation_json"], {})
                runtime_mode = str(activation.get("runtime_mode") or "live")
                if (
                    runtime_mode != row["mode"]
                    or activation.get("activation_id") != row["runtime_id"]
                    or activation.get("status") != "active"
                ):
                    raise HarvestProjectError(
                        "RUNTIME_STATE_MISMATCH",
                        "The physical allocation does not belong to the active Harvest session.",
                    )
                destination = next(
                    (
                        item
                        for item in activation.get("assignments", [])
                        if item.get("group_id") == row["group_id"]
                    ),
                    None,
                )
                observed_destination = evidence.get("destination_bin")
                expected_destination = (
                    [
                        destination["layer_index"],
                        destination["section_index"],
                        destination["bin_index"],
                    ]
                    if isinstance(destination, dict)
                    else None
                )
                bottom_reject = bool(
                    evidence.get("physical_drop_confirmed") is True
                    and evidence.get("activation_id")
                    == activation.get("activation_id")
                    and observed_destination == "bottom_reject"
                    and evidence.get("bottom_reject") is True
                    and evidence.get("reject_reason")
                    == HARVEST_LAYER_SIZE_REJECT_REASON
                    and evidence.get("reserved_group_id") == row["group_id"]
                    and evidence.get("piece_id") == row["piece_id"]
                    and isinstance(destination, dict)
                    and evidence.get("intended_layer_index")
                    == destination.get("layer_index")
                )
                if bottom_reject:
                    resolution = _decode_json(row["resolution_json"], {})
                    if not isinstance(resolution, dict):
                        resolution = {}
                    resolution["physical_reject"] = {
                        "reason": HARVEST_LAYER_SIZE_REJECT_REASON,
                        "reserved_group_id": row["group_id"],
                        "destination_bin": expected_destination,
                        "piece_max_dimension_mm": evidence.get(
                            "piece_max_dimension_mm"
                        ),
                    }
                    conn.execute(
                        "UPDATE harvest_allocations SET group_id = ?, match_kind = ?, "
                        "resolution_json = ? WHERE allocation_id = ?",
                        (
                            HARVEST_EXCEPTION_GROUP_ID,
                            "physical_reject",
                            _canonical_json(resolution),
                            allocation_id,
                        ),
                    )
                    row = conn.execute(
                        "SELECT * FROM harvest_allocations WHERE allocation_id = ?",
                        (allocation_id,),
                    ).fetchone()
                    assert row is not None
                elif (
                    evidence.get("physical_drop_confirmed") is not True
                    or evidence.get("activation_id")
                    != activation.get("activation_id")
                    or observed_destination != expected_destination
                ):
                    raise HarvestProjectError(
                        "PHYSICAL_EVIDENCE_REQUIRED",
                        "Live confirmation must prove the physical drop into the activated destination.",
                    )
            else:
                raise HarvestProjectError(
                    "INVALID_ALLOCATION_MODE", "The allocation mode is invalid."
                )
            confirmed_at = _now()
            conn.execute(
                "UPDATE harvest_allocations SET status = 'confirmed', evidence_json = ?, confirmed_at = ? WHERE allocation_id = ?",
                (_canonical_json(evidence), confirmed_at, allocation_id),
            )
            self._append_event(
                conn,
                project_id,
                "acceptance_allocation_rejected"
                if bottom_reject and row["mode"] == "acceptance"
                else "live_allocation_rejected"
                if bottom_reject and row["mode"] == "live"
                else "acceptance_allocation_confirmed"
                if row["mode"] == "acceptance"
                else "live_allocation_confirmed"
                if row["mode"] == "live"
                else "allocation_confirmed",
                {"allocation_id": allocation_id, "evidence": evidence},
                actor="sorter"
                if row["mode"] in {"live", "acceptance"}
                else "operator",
            )
            project_completed = False
            pause_required = False
            if row["mode"] == "acceptance":
                runtime_row = conn.execute(
                    "SELECT * FROM harvest_runtime_state WHERE singleton_id = 1 AND project_id = ?",
                    (project_id,),
                ).fetchone()
                assert runtime_row is not None
                activation = _decode_json(runtime_row["activation_json"], {})
                confirmed_count = int(
                    conn.execute(
                        "SELECT COALESCE(SUM(quantity), 0) AS quantity "
                        "FROM harvest_allocations WHERE project_id = ? "
                        "AND runtime_id = ? AND mode = 'acceptance' "
                        "AND status = 'confirmed'",
                        (project_id, row["runtime_id"]),
                    ).fetchone()["quantity"]
                )
                test_piece_limit = int(activation.get("test_piece_limit") or 0)
                if test_piece_limit <= 0:
                    raise HarvestProjectError(
                        "RUNTIME_STATE_MISMATCH",
                        "The controlled test has no valid piece limit. Keep the sorter paused.",
                    )
                if confirmed_count >= test_piece_limit:
                    destination_rows = conn.execute(
                        "SELECT group_id, COUNT(*) AS piece_count "
                        "FROM harvest_allocations WHERE project_id = ? "
                        "AND runtime_id = ? AND mode = 'acceptance' "
                        "AND status = 'confirmed' GROUP BY group_id ORDER BY group_id",
                        (project_id, row["runtime_id"]),
                    ).fetchall()
                    assignments = {
                        str(item.get("group_id")): item
                        for item in activation.get("assignments", [])
                        if isinstance(item, dict)
                    }
                    observed_routes = [
                        {
                            "group_id": str(item["group_id"]),
                            "group_label": str(
                                assignments.get(str(item["group_id"]), {}).get(
                                    "group_label", item["group_id"]
                                )
                            ),
                            "bin_id": assignments.get(str(item["group_id"]), {}).get(
                                "bin_id"
                            ),
                            "piece_count": int(item["piece_count"]),
                        }
                        for item in destination_rows
                    ]
                    activation.update(
                        {
                            "status": "awaiting_observation",
                            "routing_completed_at": _now(),
                            "confirmed_piece_count": confirmed_count,
                            "observed_routes": observed_routes,
                        }
                    )
                    encoded_activation = _canonical_json(activation)
                    conn.execute(
                        "UPDATE harvest_projects SET activation_json = ?, "
                        "revision = revision + 1, updated_at = ? WHERE project_id = ?",
                        (encoded_activation, _now(), project_id),
                    )
                    conn.execute(
                        "UPDATE harvest_runtime_state SET activation_json = ?, "
                        "updated_at = ? WHERE singleton_id = 1",
                        (encoded_activation, _now()),
                    )
                    self._append_event(
                        conn,
                        project_id,
                        "physical_acceptance_routing_completed",
                        {
                            "acceptance_run_id": activation.get("activation_id"),
                            "confirmed_piece_count": confirmed_count,
                            "test_piece_limit": test_piece_limit,
                            "observed_routes": observed_routes,
                        },
                        actor="sorter",
                    )
                    pause_required = True
            if row["mode"] == "live" and row["group_id"] != HARVEST_EXCEPTION_GROUP_ID:
                draft = project_harvest.load_bsx_draft(self.root, project_row["draft_id"])
                bom = self._selected_bom(conn, project_row)
                groups = self._effective_groups(project_row, draft, bom)
                progress = self._progress(conn, project_row, groups)
                if progress["summary"]["missing_quantity"] == 0:
                    runtime_row = conn.execute(
                        "SELECT * FROM harvest_runtime_state WHERE singleton_id = 1 AND project_id = ?",
                        (project_id,),
                    ).fetchone()
                    if runtime_row is not None:
                        activation = _decode_json(runtime_row["activation_json"], {})
                        activation.update(
                            {
                                "status": "completed",
                                "completed_at": _now(),
                                "completion_allocation_id": allocation_id,
                            }
                        )
                        conn.execute(
                            "UPDATE harvest_projects SET activation_json = ?, state = 'completed', revision = revision + 1, updated_at = ? WHERE project_id = ?",
                            (_canonical_json(activation), _now(), project_id),
                        )
                        conn.execute(
                            "DELETE FROM harvest_runtime_state WHERE singleton_id = 1"
                        )
                        self._append_event(
                            conn,
                            project_id,
                            "live_sort_completed",
                            {
                                "activation_id": activation.get("activation_id"),
                                "completion_allocation_id": allocation_id,
                                "confirmed_quantity": progress["summary"]["confirmed_quantity"],
                            },
                            actor="sorter",
                        )
                        project_completed = True
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM harvest_allocations WHERE allocation_id = ?",
                (allocation_id,),
            ).fetchone()
            assert updated is not None
            return {
                **self._allocation_dict(updated),
                "project_completed": project_completed,
                "pause_required": pause_required,
            }

    def _reopen_acceptance_after_correction(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        activation: dict[str, Any],
        *,
        actor: str,
        reason: str,
    ) -> bool:
        """Restore an auto-paused acceptance run when evidence drops below its limit."""

        if activation.get("runtime_mode") != "acceptance":
            return False
        run_id = str(activation.get("activation_id") or "")
        confirmed_count = int(
            conn.execute(
                "SELECT COALESCE(SUM(quantity), 0) AS quantity "
                "FROM harvest_allocations WHERE project_id = ? AND runtime_id = ? "
                "AND mode = 'acceptance' AND status = 'confirmed'",
                (project_id, run_id),
            ).fetchone()["quantity"]
        )
        test_piece_limit = int(activation.get("test_piece_limit") or 0)
        if (
            activation.get("status") != "awaiting_observation"
            or test_piece_limit <= 0
            or confirmed_count >= test_piece_limit
        ):
            return False
        activation.update(
            {
                "status": "active",
                "confirmed_piece_count": confirmed_count,
                "reopened_at": _now(),
            }
        )
        activation.pop("routing_completed_at", None)
        activation.pop("observed_routes", None)
        encoded_activation = _canonical_json(activation)
        conn.execute(
            "UPDATE harvest_projects SET activation_json = ?, revision = revision + 1, "
            "updated_at = ? WHERE project_id = ?",
            (encoded_activation, _now(), project_id),
        )
        conn.execute(
            "UPDATE harvest_runtime_state SET activation_json = ?, updated_at = ? "
            "WHERE singleton_id = 1 AND project_id = ?",
            (encoded_activation, _now(), project_id),
        )
        self._append_event(
            conn,
            project_id,
            "physical_acceptance_reopened_after_correction",
            {
                "acceptance_run_id": run_id,
                "confirmed_piece_count": confirmed_count,
                "test_piece_limit": test_piece_limit,
                "replacement_piece_count": test_piece_limit - confirmed_count,
                "reason": reason,
            },
            actor=actor,
        )
        return True

    def reopen_acceptance_after_correction(
        self,
        project_id: str,
        *,
        acceptance_run_id: str,
        operator: str,
        reason: str,
    ) -> dict[str, Any]:
        """Repair a legacy acceptance mismatch without changing allocation evidence."""

        normalized_run_id = _identifier(acceptance_run_id, "acceptance_run_id")
        normalized_operator = _identifier(operator, "operator")
        normalized_reason = str(reason or "").strip()
        if not normalized_reason or len(normalized_reason) > 500:
            raise HarvestProjectError(
                "REOPEN_REASON_REQUIRED", "A concise correction reason is required."
            )
        with _STORE_LOCK, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._project_row(conn, project_id)
            runtime_row = conn.execute(
                "SELECT * FROM harvest_runtime_state WHERE singleton_id = 1 AND project_id = ?",
                (project_id,),
            ).fetchone()
            if runtime_row is None:
                raise HarvestProjectError(
                    "PROJECT_NOT_ACTIVE", "This controlled Harvest test is not active."
                )
            activation = _decode_json(runtime_row["activation_json"], {})
            if (
                activation.get("runtime_mode") != "acceptance"
                or activation.get("activation_id") != normalized_run_id
            ):
                raise HarvestProjectError(
                    "RUNTIME_STATE_MISMATCH",
                    "The requested controlled test is not the active Harvest session.",
                )
            if not self._reopen_acceptance_after_correction(
                conn,
                project_id,
                activation,
                actor=normalized_operator,
                reason=normalized_reason,
            ):
                raise HarvestProjectError(
                    "ACCEPTANCE_REOPEN_NOT_REQUIRED",
                    "This controlled test does not have an evidence mismatch that can be reopened.",
                )
            conn.commit()
        return self.get_project(project_id)

    def undo_allocation(
        self, project_id: str, allocation_id: str, *, reason: str
    ) -> dict[str, Any]:
        if not _ALLOCATION_ID_RE.fullmatch(allocation_id):
            raise HarvestProjectError("INVALID_ALLOCATION_ID", "Invalid allocation id.")
        normalized_reason = str(reason or "").strip()
        if not normalized_reason or len(normalized_reason) > 500:
            raise HarvestProjectError(
                "UNDO_REASON_REQUIRED", "A concise undo reason is required."
            )
        with _STORE_LOCK, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._project_row(conn, project_id)
            row = conn.execute(
                "SELECT * FROM harvest_allocations WHERE project_id = ? AND allocation_id = ?",
                (project_id, allocation_id),
            ).fetchone()
            if row is None:
                raise HarvestProjectError(
                    "ALLOCATION_NOT_FOUND", "Allocation not found."
                )
            activation: dict[str, Any] | None = None
            if row["status"] != "undone" and row["mode"] == "acceptance":
                runtime_row = conn.execute(
                    "SELECT * FROM harvest_runtime_state WHERE singleton_id = 1 AND project_id = ?",
                    (project_id,),
                ).fetchone()
                if runtime_row is None:
                    raise HarvestProjectError(
                        "ACCEPTANCE_EVIDENCE_IMMUTABLE",
                        "Finalized controlled-test evidence cannot be undone.",
                    )
                activation = _decode_json(runtime_row["activation_json"], {})
                if (
                    activation.get("runtime_mode") != "acceptance"
                    or activation.get("activation_id") != row["runtime_id"]
                    or activation.get("status") not in {"active", "awaiting_observation"}
                ):
                    raise HarvestProjectError(
                        "ACCEPTANCE_EVIDENCE_IMMUTABLE",
                        "This allocation is not part of the active controlled test.",
                    )
            if row["status"] != "undone":
                conn.execute(
                    "UPDATE harvest_allocations SET status = 'undone', undone_at = ? WHERE allocation_id = ?",
                    (_now(), allocation_id),
                )
                self._append_event(
                    conn,
                    project_id,
                    "allocation_undone",
                    {"allocation_id": allocation_id, "reason": normalized_reason},
                )
                if activation is not None:
                    self._reopen_acceptance_after_correction(
                        conn,
                        project_id,
                        activation,
                        actor="operator",
                        reason=normalized_reason,
                    )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM harvest_allocations WHERE allocation_id = ?",
                (allocation_id,),
            ).fetchone()
            assert updated is not None
            return self._allocation_dict(updated)

    def record_acceptance(
        self, project_id: str, *, evidence: dict[str, Any]
    ) -> dict[str, Any]:
        raise HarvestProjectError(
            "CONTROLLED_ACCEPTANCE_REQUIRED",
            "Manual acceptance records are disabled. Run the guided controlled physical test so routing evidence is captured by the sorter.",
        )

    def start_acceptance_run(
        self,
        project_id: str,
        *,
        expected_revision: int,
        test_id: str,
        operator: str,
        test_piece_limit: int,
        pre_acceptance_bin_state_token: str,
        post_acceptance_bin_state_token: str,
        assignments: list[dict[str, Any]],
        categories_before: list[list[list[list[str]]]],
    ) -> dict[str, Any]:
        """Arm a bounded physical-routing test without starting motion."""

        normalized_test_id = _identifier(test_id, "test_id")
        normalized_operator = _identifier(operator, "operator")
        normalized_limit = _positive_int(
            test_piece_limit, "test_piece_limit", maximum=25
        )
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise HarvestProjectError(
                "INVALID_PROJECT_REVISION", "Expected project revision is invalid."
            )
        pre_token = _identifier(
            pre_acceptance_bin_state_token, "pre-acceptance bin token"
        )
        post_token = _identifier(
            post_acceptance_bin_state_token, "post-acceptance bin token"
        )
        if not isinstance(assignments, list) or not assignments:
            raise HarvestProjectError(
                "INVALID_LIVE_ASSIGNMENTS", "Controlled-test assignments are required."
            )
        if not isinstance(categories_before, list):
            raise HarvestProjectError(
                "INVALID_LIVE_ASSIGNMENTS",
                "The pre-test bin snapshot is invalid.",
            )

        normalized_assignments: list[dict[str, Any]] = []
        seen_groups: set[str] = set()
        seen_bins: set[str] = set()
        seen_categories: set[str] = set()
        for index, assignment in enumerate(assignments, start=1):
            if not isinstance(assignment, dict):
                raise HarvestProjectError(
                    "INVALID_LIVE_ASSIGNMENTS",
                    f"Controlled-test assignment {index} is invalid.",
                )
            group_id = _identifier(
                assignment.get("group_id"), f"assignment {index} group_id"
            )
            bin_id = _identifier(
                assignment.get("bin_id"), f"assignment {index} bin_id"
            )
            category_id = _identifier(
                assignment.get("category_id"), f"assignment {index} category_id"
            )
            coordinates: list[int] = []
            for field in ("layer_index", "section_index", "bin_index"):
                value = assignment.get(field)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise HarvestProjectError(
                        "INVALID_LIVE_ASSIGNMENTS",
                        f"Controlled-test assignment {index} has invalid {field}.",
                    )
                coordinates.append(value)
            if (
                group_id in seen_groups
                or bin_id in seen_bins
                or category_id in seen_categories
            ):
                raise HarvestProjectError(
                    "DUPLICATE_LIVE_ASSIGNMENT",
                    "Each controlled-test destination, category, and physical bin must be unique.",
                )
            seen_groups.add(group_id)
            seen_bins.add(bin_id)
            seen_categories.add(category_id)
            normalized_assignments.append(
                {
                    "group_id": group_id,
                    "group_label": str(
                        assignment.get("group_label") or group_id
                    )[:120],
                    "bin_id": bin_id,
                    "category_id": category_id,
                    "layer_index": coordinates[0],
                    "section_index": coordinates[1],
                    "bin_index": coordinates[2],
                }
            )

        with _STORE_LOCK, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._project_row(conn, project_id)
            if int(row["revision"]) != expected_revision:
                raise HarvestProjectError(
                    "STALE_PROJECT_REVISION",
                    "The project changed after it was reviewed. Refresh and check it again.",
                    details={
                        "expected_revision": expected_revision,
                        "actual_revision": int(row["revision"]),
                    },
                )
            runtime_row = conn.execute(
                "SELECT * FROM harvest_runtime_state WHERE singleton_id = 1"
            ).fetchone()
            if runtime_row is not None:
                raise HarvestProjectError(
                    "ANOTHER_PROJECT_ACTIVE",
                    "Stop the current Harvest session before arming a controlled test.",
                    details={"project_id": runtime_row["project_id"]},
                )
            current = self._assemble(conn, row, include_events=False)
            if not current["readiness"]["ready_for_physical_acceptance"]:
                raise HarvestProjectError(
                    "NOT_READY_FOR_ACCEPTANCE",
                    "Reconciliation, capacity planning, and simulation must pass first.",
                )
            plan = current.get("capacity_plan") or {}
            planned = {
                item["group_id"]: item
                for wave in plan.get("waves", [])
                for item in wave.get("assignments", [])
            }
            assigned_bins = {item["group_id"]: item["bin_id"] for item in normalized_assignments}
            if set(planned) != seen_groups or any(
                planned[group_id].get("bin_id") != assigned_bins.get(group_id)
                for group_id in planned
            ):
                raise HarvestProjectError(
                    "LIVE_ASSIGNMENT_PLAN_MISMATCH",
                    "Controlled-test assignments do not exactly match the simulated bin plan.",
                )
            activation = {
                "activation_id": f"activation-{uuid.uuid4().hex}",
                "runtime_mode": "acceptance",
                "status": "active",
                "project_id": project_id,
                "project_revision": expected_revision,
                "test_id": normalized_test_id,
                "test_piece_limit": normalized_limit,
                "confirmed_piece_count": 0,
                "bom_revision_id": current["bom"]["bom_revision_id"],
                "simulation_run_id": current["simulation"]["run_id"],
                "operator": normalized_operator,
                "pre_acceptance_bin_state_token": pre_token,
                "post_acceptance_bin_state_token": post_token,
                "assignments": normalized_assignments,
                "categories_before": categories_before,
                "armed_at": _now(),
                "motion_started": False,
            }
            encoded_activation = _canonical_json(activation)
            conn.execute(
                "UPDATE harvest_projects SET acceptance_json = NULL, "
                "green_light_json = NULL, activation_json = ?, state = 'active', "
                "revision = revision + 1, updated_at = ? WHERE project_id = ?",
                (encoded_activation, _now(), project_id),
            )
            conn.execute(
                "INSERT INTO harvest_runtime_state(singleton_id, project_id, activation_id, activation_json, updated_at) "
                "VALUES(1, ?, ?, ?, ?)",
                (
                    project_id,
                    activation["activation_id"],
                    encoded_activation,
                    _now(),
                ),
            )
            self._append_event(
                conn,
                project_id,
                "physical_acceptance_test_armed",
                {key: value for key, value in activation.items() if key != "categories_before"},
                actor=normalized_operator,
            )
            conn.commit()
        return self.get_project(project_id)

    def finish_acceptance_run(
        self,
        project_id: str,
        *,
        acceptance_run_id: str,
        operator: str,
        result: str,
        observed_destinations_match: bool,
        notes: str,
        post_restore_bin_state_token: str,
    ) -> dict[str, Any]:
        """Close a completed controlled test using only sorter-captured evidence."""

        normalized_run_id = _identifier(acceptance_run_id, "acceptance_run_id")
        normalized_operator = _identifier(operator, "operator")
        if result not in {"passed", "failed"}:
            raise HarvestProjectError(
                "INVALID_ACCEPTANCE", "Acceptance result must be passed or failed."
            )
        if result == "passed" and observed_destinations_match is not True:
            raise HarvestProjectError(
                "PHYSICAL_OBSERVATION_REQUIRED",
                "Confirm that every test piece landed in the displayed physical destination.",
            )
        restore_token = _identifier(
            post_restore_bin_state_token, "post-restore bin token"
        )
        normalized_notes = str(notes or "").strip()[:2000]
        with _STORE_LOCK, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._project_row(conn, project_id)
            runtime_row = conn.execute(
                "SELECT * FROM harvest_runtime_state WHERE singleton_id = 1 AND project_id = ?",
                (project_id,),
            ).fetchone()
            if runtime_row is None:
                raise HarvestProjectError(
                    "PROJECT_NOT_ACTIVE", "This controlled Harvest test is not active."
                )
            activation = _decode_json(runtime_row["activation_json"], {})
            if (
                activation.get("runtime_mode") != "acceptance"
                or activation.get("activation_id") != normalized_run_id
                or activation.get("status") != "awaiting_observation"
            ):
                raise HarvestProjectError(
                    "ACCEPTANCE_OBSERVATION_NOT_READY",
                    "The controlled test must auto-pause at its piece limit before it can be evaluated.",
                )
            pending = conn.execute(
                "SELECT allocation_id, piece_id FROM harvest_allocations "
                "WHERE project_id = ? AND runtime_id = ? AND mode = 'acceptance' "
                "AND status = 'planned' ORDER BY created_at LIMIT 1",
                (project_id, normalized_run_id),
            ).fetchone()
            if pending is not None:
                raise HarvestProjectError(
                    "LIVE_ALLOCATION_RECOVERY_REQUIRED",
                    "Resolve the in-flight controlled-test allocation before recording a result.",
                    details=dict(pending),
                )
            allocation_rows = conn.execute(
                "SELECT * FROM harvest_allocations WHERE project_id = ? "
                "AND runtime_id = ? AND mode = 'acceptance' AND status = 'confirmed' "
                "ORDER BY confirmed_at, allocation_id",
                (project_id, normalized_run_id),
            ).fetchall()
            confirmed_count = sum(int(item["quantity"]) for item in allocation_rows)
            test_piece_limit = int(activation.get("test_piece_limit") or 0)
            if confirmed_count != test_piece_limit:
                raise HarvestProjectError(
                    "ACCEPTANCE_PIECE_COUNT_MISMATCH",
                    "The controlled test evidence does not match its configured piece limit.",
                    details={
                        "confirmed_piece_count": confirmed_count,
                        "test_piece_limit": test_piece_limit,
                    },
                )
            acceptance = {
                "acceptance_id": f"acceptance-{uuid.uuid4().hex}",
                "acceptance_run_id": normalized_run_id,
                "test_id": activation.get("test_id"),
                "operator": normalized_operator,
                "controlled_piece_count": confirmed_count,
                "test_piece_limit": test_piece_limit,
                "result": result,
                "observed_destinations_match": observed_destinations_match is True,
                "notes": normalized_notes,
                "simulation_run_id": activation.get("simulation_run_id"),
                "bom_revision_id": activation.get("bom_revision_id"),
                "observed_routes": activation.get("observed_routes", []),
                "allocation_ids": [str(item["allocation_id"]) for item in allocation_rows],
                "post_restore_bin_state_token": restore_token,
                "recorded_at": _now(),
            }
            conn.execute(
                "UPDATE harvest_projects SET acceptance_json = ?, green_light_json = NULL, "
                "activation_json = NULL, state = ?, revision = revision + 1, updated_at = ? "
                "WHERE project_id = ?",
                (
                    _canonical_json(acceptance),
                    "accepted" if result == "passed" else "review",
                    _now(),
                    project_id,
                ),
            )
            conn.execute("DELETE FROM harvest_runtime_state WHERE singleton_id = 1")
            self._append_event(
                conn,
                project_id,
                "physical_acceptance_recorded",
                acceptance,
                actor=normalized_operator,
            )
            conn.commit()
        return self.get_project(project_id)

    def abort_acceptance_run(
        self,
        project_id: str,
        *,
        acceptance_run_id: str,
        operator: str,
        reason: str,
        post_restore_bin_state_token: str,
    ) -> dict[str, Any]:
        normalized_run_id = _identifier(acceptance_run_id, "acceptance_run_id")
        normalized_operator = _identifier(operator, "operator")
        normalized_reason = str(reason or "").strip()
        if not normalized_reason or len(normalized_reason) > 500:
            raise HarvestProjectError(
                "ACCEPTANCE_ABORT_REASON_REQUIRED",
                "A concise reason is required to stop the controlled test.",
            )
        restore_token = _identifier(
            post_restore_bin_state_token, "post-restore bin token"
        )
        with _STORE_LOCK, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._project_row(conn, project_id)
            runtime_row = conn.execute(
                "SELECT * FROM harvest_runtime_state WHERE singleton_id = 1 AND project_id = ?",
                (project_id,),
            ).fetchone()
            if runtime_row is None:
                raise HarvestProjectError(
                    "PROJECT_NOT_ACTIVE", "This controlled Harvest test is not active."
                )
            activation = _decode_json(runtime_row["activation_json"], {})
            if (
                activation.get("runtime_mode") != "acceptance"
                or activation.get("activation_id") != normalized_run_id
            ):
                raise HarvestProjectError(
                    "RUNTIME_STATE_MISMATCH", "The controlled test runtime does not match."
                )
            pending = conn.execute(
                "SELECT allocation_id, piece_id FROM harvest_allocations "
                "WHERE project_id = ? AND runtime_id = ? AND mode = 'acceptance' "
                "AND status = 'planned' ORDER BY created_at LIMIT 1",
                (project_id, normalized_run_id),
            ).fetchone()
            if pending is not None:
                raise HarvestProjectError(
                    "LIVE_ALLOCATION_RECOVERY_REQUIRED",
                    "Resolve the in-flight controlled-test allocation before stopping it.",
                    details=dict(pending),
                )
            conn.execute(
                "UPDATE harvest_projects SET acceptance_json = NULL, green_light_json = NULL, "
                "activation_json = NULL, state = 'simulated', revision = revision + 1, "
                "updated_at = ? WHERE project_id = ?",
                (_now(), project_id),
            )
            conn.execute("DELETE FROM harvest_runtime_state WHERE singleton_id = 1")
            self._append_event(
                conn,
                project_id,
                "physical_acceptance_test_aborted",
                {
                    "acceptance_run_id": normalized_run_id,
                    "reason": normalized_reason,
                    "confirmed_piece_count": int(
                        activation.get("confirmed_piece_count") or 0
                    ),
                    "post_restore_bin_state_token": restore_token,
                },
                actor=normalized_operator,
            )
            conn.commit()
        return self.get_project(project_id)

    def record_bin_clearance(
        self, project_id: str, *, evidence: dict[str, Any], actor: str
    ) -> dict[str, Any]:
        """Attach a successful, non-motion bin-clearance record to the audit chain."""

        normalized_actor = _identifier(actor, "operator")
        if not isinstance(evidence, dict):
            raise HarvestProjectError(
                "INVALID_BIN_CLEARANCE", "Bin-clearance evidence must be an object."
            )
        with _STORE_LOCK, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._project_row(conn, project_id)
            self._ensure_not_active(row)
            conn.execute(
                "UPDATE harvest_projects SET revision = revision + 1, updated_at = ? WHERE project_id = ?",
                (_now(), project_id),
            )
            self._append_event(
                conn,
                project_id,
                "bin_clearance_recorded",
                evidence,
                actor=normalized_actor,
            )
            conn.commit()
        return self.get_project(project_id)

    def record_green_light(
        self,
        project_id: str,
        *,
        expected_revision: int,
        operator: str,
        reason: str,
        physical_bins_verified_empty: bool,
        bin_state_token: str,
        bin_ids: list[str],
    ) -> dict[str, Any]:
        """Record an operator green light without enabling live motion."""

        if not physical_bins_verified_empty:
            raise HarvestProjectError(
                "PHYSICAL_BIN_CONFIRMATION_REQUIRED",
                "Confirm that the planned physical bins are empty before green-lighting the project.",
            )
        normalized_operator = _identifier(operator, "operator")
        normalized_reason = str(reason or "").strip()
        if not normalized_reason or len(normalized_reason) > 500:
            raise HarvestProjectError(
                "GREEN_LIGHT_REASON_REQUIRED",
                "A green-light reason of 1 to 500 characters is required.",
            )
        normalized_token = _identifier(bin_state_token, "bin_state_token")
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise HarvestProjectError(
                "INVALID_PROJECT_REVISION", "Expected project revision is invalid."
            )
        normalized_bins = sorted({_identifier(value, "bin id") for value in bin_ids})
        if not normalized_bins:
            raise HarvestProjectError(
                "GREEN_LIGHT_BINS_REQUIRED",
                "At least one planned bin is required for green-light approval.",
            )

        with _STORE_LOCK, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._project_row(conn, project_id)
            self._ensure_not_active(row)
            if int(row["revision"]) != expected_revision:
                raise HarvestProjectError(
                    "STALE_PROJECT_REVISION",
                    "The project changed after it was reviewed. Refresh and check it again.",
                    details={
                        "expected_revision": expected_revision,
                        "actual_revision": int(row["revision"]),
                    },
                )
            current = self._assemble(conn, row, include_events=False)
            if not current["readiness"]["ready_for_green_light"]:
                raise HarvestProjectError(
                    "NOT_READY_FOR_GREEN_LIGHT",
                    "The frozen BOM, review, capacity plan, simulation, and physical acceptance must all pass first.",
                )
            acceptance = current.get("acceptance") or {}
            simulation = current.get("simulation") or {}
            normalized = {
                "green_light_id": f"green-light-{uuid.uuid4().hex}",
                "operator": normalized_operator,
                "reason": normalized_reason,
                "physical_bins_verified_empty": True,
                "bin_state_token": normalized_token,
                "bin_ids": normalized_bins,
                "project_revision": expected_revision,
                "acceptance_id": acceptance.get("acceptance_id"),
                "simulation_run_id": simulation.get("run_id"),
                "recorded_at": _now(),
                "motion_enabled": False,
            }
            conn.execute(
                "UPDATE harvest_projects SET green_light_json = ?, state = 'approved', revision = revision + 1, updated_at = ? WHERE project_id = ?",
                (_canonical_json(normalized), _now(), project_id),
            )
            self._append_event(
                conn,
                project_id,
                "green_light_recorded",
                normalized,
                actor=normalized_operator,
            )
            conn.commit()
        return self.get_project(project_id)

    def activate_project(
        self,
        project_id: str,
        *,
        expected_revision: int,
        operator: str,
        reason: str,
        pre_activation_bin_state_token: str,
        post_activation_bin_state_token: str,
        assignments: list[dict[str, Any]],
        categories_before: list[list[list[list[str]]]],
    ) -> dict[str, Any]:
        """Bind an approved revision to exact physical bins for live routing.

        Activation is intentionally separate from controller resume. The router
        requires the sorter to be paused and installs the category labels first;
        this transaction then makes the immutable assignment usable by the
        distribution pipeline across backend restarts.
        """

        normalized_operator = _identifier(operator, "operator")
        normalized_reason = str(reason or "").strip()
        if not normalized_reason or len(normalized_reason) > 500:
            raise HarvestProjectError(
                "ACTIVATION_REASON_REQUIRED", "A live-activation reason is required."
            )
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise HarvestProjectError(
                "INVALID_PROJECT_REVISION", "Expected project revision is invalid."
            )
        pre_token = _identifier(pre_activation_bin_state_token, "pre-activation bin token")
        post_token = _identifier(post_activation_bin_state_token, "post-activation bin token")
        if not isinstance(assignments, list) or not assignments:
            raise HarvestProjectError(
                "INVALID_LIVE_ASSIGNMENTS", "Live assignments are required."
            )
        if not isinstance(categories_before, list):
            raise HarvestProjectError(
                "INVALID_LIVE_ASSIGNMENTS", "The pre-activation bin snapshot is invalid."
            )

        normalized_assignments: list[dict[str, Any]] = []
        seen_groups: set[str] = set()
        seen_bins: set[str] = set()
        seen_categories: set[str] = set()
        for index, assignment in enumerate(assignments, start=1):
            if not isinstance(assignment, dict):
                raise HarvestProjectError(
                    "INVALID_LIVE_ASSIGNMENTS", f"Live assignment {index} is invalid."
                )
            group_id = _identifier(assignment.get("group_id"), f"assignment {index} group_id")
            bin_id = _identifier(assignment.get("bin_id"), f"assignment {index} bin_id")
            category_id = _identifier(
                assignment.get("category_id"), f"assignment {index} category_id"
            )
            coordinates: list[int] = []
            for field in ("layer_index", "section_index", "bin_index"):
                value = assignment.get(field)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise HarvestProjectError(
                        "INVALID_LIVE_ASSIGNMENTS",
                        f"Live assignment {index} has invalid {field}.",
                    )
                coordinates.append(value)
            if group_id in seen_groups or bin_id in seen_bins or category_id in seen_categories:
                raise HarvestProjectError(
                    "DUPLICATE_LIVE_ASSIGNMENT",
                    "Each Harvest destination, category, and physical bin must be unique.",
                )
            seen_groups.add(group_id)
            seen_bins.add(bin_id)
            seen_categories.add(category_id)
            normalized_assignments.append(
                {
                    "group_id": group_id,
                    "group_label": str(assignment.get("group_label") or group_id)[:120],
                    "bin_id": bin_id,
                    "category_id": category_id,
                    "layer_index": coordinates[0],
                    "section_index": coordinates[1],
                    "bin_index": coordinates[2],
                }
            )

        with _STORE_LOCK, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._project_row(conn, project_id)
            runtime_row = conn.execute(
                "SELECT * FROM harvest_runtime_state WHERE singleton_id = 1"
            ).fetchone()
            if runtime_row is not None:
                active = _decode_json(runtime_row["activation_json"], {})
                if runtime_row["project_id"] == project_id and active.get("status") == "active":
                    conn.commit()
                    return self.get_project(project_id)
                raise HarvestProjectError(
                    "ANOTHER_PROJECT_ACTIVE",
                    "Stop the currently active Harvest project before activating another one.",
                    details={"project_id": runtime_row["project_id"]},
                )
            if int(row["revision"]) != expected_revision:
                raise HarvestProjectError(
                    "STALE_PROJECT_REVISION",
                    "The project changed after it was reviewed. Refresh and check it again.",
                    details={
                        "expected_revision": expected_revision,
                        "actual_revision": int(row["revision"]),
                    },
                )
            current = self._assemble(conn, row, include_events=False)
            if not current["readiness"]["activation_eligible"]:
                raise HarvestProjectError(
                    "NOT_READY_FOR_ACTIVATION",
                    "The exact green-lit revision is not ready for live Harvest activation.",
                )
            green_light = current.get("green_light") or {}
            if green_light.get("bin_state_token") != pre_token:
                raise HarvestProjectError(
                    "STALE_BIN_STATE",
                    "Bin assignments or contents changed after the green light.",
                )
            plan = current.get("capacity_plan") or {}
            planned = {
                item["group_id"]: item
                for wave in plan.get("waves", [])
                for item in wave.get("assignments", [])
            }
            if set(planned) != seen_groups or any(
                planned[group_id].get("bin_id")
                != next(
                    item["bin_id"]
                    for item in normalized_assignments
                    if item["group_id"] == group_id
                )
                for group_id in planned
            ):
                raise HarvestProjectError(
                    "LIVE_ASSIGNMENT_PLAN_MISMATCH",
                    "Live assignments do not exactly match the green-lit bin plan.",
                )
            activation = {
                "activation_id": f"activation-{uuid.uuid4().hex}",
                "runtime_mode": "live",
                "status": "active",
                "project_id": project_id,
                "project_revision": expected_revision,
                "green_light_id": green_light.get("green_light_id"),
                "bom_revision_id": current["bom"]["bom_revision_id"],
                "simulation_run_id": current["simulation"]["run_id"],
                "acceptance_id": current["acceptance"]["acceptance_id"],
                "operator": normalized_operator,
                "reason": normalized_reason,
                "pre_activation_bin_state_token": pre_token,
                "post_activation_bin_state_token": post_token,
                "assignments": normalized_assignments,
                "categories_before": categories_before,
                "activated_at": _now(),
                "motion_started": False,
            }
            conn.execute(
                "UPDATE harvest_projects SET activation_json = ?, state = 'active', revision = revision + 1, updated_at = ? WHERE project_id = ?",
                (_canonical_json(activation), _now(), project_id),
            )
            conn.execute(
                "INSERT INTO harvest_runtime_state(singleton_id, project_id, activation_id, activation_json, updated_at) VALUES(1, ?, ?, ?, ?)",
                (
                    project_id,
                    activation["activation_id"],
                    _canonical_json(activation),
                    _now(),
                ),
            )
            self._append_event(
                conn,
                project_id,
                "live_activation_started",
                {key: value for key, value in activation.items() if key != "categories_before"},
                actor=normalized_operator,
            )
            conn.commit()
        return self.get_project(project_id)

    def get_active_runtime(self) -> dict[str, Any] | None:
        with self._connection() as conn:
            runtime_row = conn.execute(
                "SELECT * FROM harvest_runtime_state WHERE singleton_id = 1"
            ).fetchone()
            if runtime_row is None:
                return None
            row = self._project_row(conn, str(runtime_row["project_id"]))
            activation = _decode_json(runtime_row["activation_json"], {})
            project_activation = _decode_json(row["activation_json"], {})
            if (
                row["state"] != "active"
                or activation.get("status") not in {"active", "awaiting_observation"}
                or project_activation.get("activation_id") != activation.get("activation_id")
            ):
                raise HarvestProjectError(
                    "RUNTIME_STATE_MISMATCH",
                    "Harvest runtime state does not match the active project. Keep the sorter paused.",
                )
            project = self._assemble(conn, row, include_events=False)
            runtime_counts = {
                str(item["status"]): int(item["quantity"] or 0)
                for item in conn.execute(
                    "SELECT status, COALESCE(SUM(quantity), 0) AS quantity "
                    "FROM harvest_allocations WHERE project_id = ? AND runtime_id = ? "
                    "GROUP BY status",
                    (row["project_id"], activation.get("activation_id")),
                ).fetchall()
            }
            runtime_exception_counts = {
                str(item["status"]): int(item["quantity"] or 0)
                for item in conn.execute(
                    "SELECT status, COALESCE(SUM(quantity), 0) AS quantity "
                    "FROM harvest_allocations WHERE project_id = ? AND runtime_id = ? "
                    "AND group_id = ? GROUP BY status",
                    (
                        row["project_id"],
                        activation.get("activation_id"),
                        HARVEST_EXCEPTION_GROUP_ID,
                    ),
                ).fetchall()
            }
            confirmed_piece_count = runtime_counts.get("confirmed", 0)
            exception_confirmed_piece_count = runtime_exception_counts.get(
                "confirmed", 0
            )
            return {
                **activation,
                "set_number": project["set_number"],
                "name": project["set_metadata"]["name"]
                if project.get("set_metadata")
                else project["name"],
                "progress": project["progress"],
                "allocation_summary": project["allocation_summary"],
                "runtime_progress": {
                    "planned_piece_count": runtime_counts.get("planned", 0),
                    "confirmed_piece_count": confirmed_piece_count,
                    "bag_confirmed_piece_count": max(
                        0, confirmed_piece_count - exception_confirmed_piece_count
                    ),
                    "exception_confirmed_piece_count": exception_confirmed_piece_count,
                    "exception_planned_piece_count": runtime_exception_counts.get(
                        "planned", 0
                    ),
                    "test_piece_limit": activation.get("test_piece_limit"),
                },
            }

    def has_active_runtime(self) -> bool:
        with self._connection() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM harvest_runtime_state WHERE singleton_id = 1"
                ).fetchone()
                is not None
            )

    def stop_activation(
        self,
        project_id: str,
        *,
        operator: str,
        reason: str,
    ) -> dict[str, Any]:
        normalized_operator = _identifier(operator, "operator")
        normalized_reason = str(reason or "").strip()
        if not normalized_reason or len(normalized_reason) > 500:
            raise HarvestProjectError(
                "ACTIVATION_STOP_REASON_REQUIRED", "A reason is required to stop live sorting."
            )
        with _STORE_LOCK, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._project_row(conn, project_id)
            runtime_row = conn.execute(
                "SELECT * FROM harvest_runtime_state WHERE singleton_id = 1"
            ).fetchone()
            if runtime_row is None or runtime_row["project_id"] != project_id:
                raise HarvestProjectError(
                    "PROJECT_NOT_ACTIVE", "This Harvest project is not active."
                )
            activation = _decode_json(runtime_row["activation_json"], {})
            if str(activation.get("runtime_mode") or "live") != "live":
                raise HarvestProjectError(
                    "RUNTIME_MODE_MISMATCH",
                    "Use the controlled-test controls to stop an acceptance session.",
                )
            pending = conn.execute(
                "SELECT allocation_id, piece_id FROM harvest_allocations "
                "WHERE project_id = ? AND runtime_id = ? AND mode = 'live' "
                "AND status = 'planned' ORDER BY created_at LIMIT 1",
                (project_id, activation.get("activation_id")),
            ).fetchone()
            if pending is not None:
                raise HarvestProjectError(
                    "LIVE_ALLOCATION_RECOVERY_REQUIRED",
                    "Resolve the in-flight physical allocation before stopping live Harvest routing.",
                    details={
                        "allocation_id": pending["allocation_id"],
                        "piece_id": pending["piece_id"],
                    },
                )
            activation.update(
                {
                    "status": "stopped",
                    "stopped_at": _now(),
                    "stopped_by": normalized_operator,
                    "stop_reason": normalized_reason,
                }
            )
            conn.execute(
                "UPDATE harvest_projects SET activation_json = ?, state = 'paused', revision = revision + 1, updated_at = ? WHERE project_id = ?",
                (_canonical_json(activation), _now(), project_id),
            )
            conn.execute("DELETE FROM harvest_runtime_state WHERE singleton_id = 1")
            self._append_event(
                conn,
                project_id,
                "live_activation_stopped",
                {
                    "activation_id": activation.get("activation_id"),
                    "reason": normalized_reason,
                },
                actor=normalized_operator,
            )
            conn.commit()
        return self.get_project(project_id)

    def transition(
        self, project_id: str, *, target: str, reason: str
    ) -> dict[str, Any]:
        if target not in _ALLOWED_PROJECT_STATES:
            raise HarvestProjectError(
                "INVALID_PROJECT_STATE", "Unsupported project state."
            )
        if target in {"ready", "simulated", "accepted"}:
            raise HarvestProjectError(
                "EVIDENCE_DRIVEN_STATE",
                "Ready, simulated, and accepted states are reached only through their evidence-producing workflows.",
            )
        normalized_reason = str(reason or "").strip()
        if not normalized_reason or len(normalized_reason) > 500:
            raise HarvestProjectError(
                "TRANSITION_REASON_REQUIRED", "A transition reason is required."
            )
        with _STORE_LOCK, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._project_row(conn, project_id)
            self._ensure_not_active(row)
            snapshot = self._assemble(conn, row, include_events=False)
            previous = str(row["state"])
            allowed: dict[str, set[str]] = {
                "draft": {"review", "paused"},
                "review": {"paused"},
                "ready": {"review", "paused"},
                "simulated": {"review", "paused"},
                "accepted": {"review", "paused", "completed"},
                "approved": {"review", "paused", "completed"},
                "paused": {"review"},
                "completed": {"review"},
            }
            if target not in allowed.get(previous, set()):
                raise HarvestProjectError(
                    "INVALID_STATE_TRANSITION",
                    f"Project state cannot transition from {previous} to {target}.",
                )
            if (
                target == "completed"
                and snapshot["progress"]["summary"]["missing_quantity"] != 0
            ):
                raise HarvestProjectError(
                    "PROJECT_INCOMPLETE",
                    "All required quantities must be confirmed before completing the project.",
                    details={
                        "missing_quantity": snapshot["progress"]["summary"][
                            "missing_quantity"
                        ]
                    },
                )
            if target == "review":
                conn.execute(
                    "UPDATE harvest_projects SET state = ?, acceptance_json = NULL, green_light_json = NULL, activation_json = NULL, revision = revision + 1, updated_at = ? WHERE project_id = ?",
                    (target, _now(), project_id),
                )
            else:
                conn.execute(
                    "UPDATE harvest_projects SET state = ?, revision = revision + 1, updated_at = ? WHERE project_id = ?",
                    (target, _now(), project_id),
                )
            self._append_event(
                conn,
                project_id,
                "state_transitioned",
                {"from": previous, "to": target, "reason": normalized_reason},
            )
            conn.commit()
        return self.get_project(project_id)

    def _progress(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        groups: list[dict[str, Any]],
    ) -> dict[str, Any]:
        confirmed = self._confirmed_progress(conn, row["project_id"])
        group_progress: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        total_required = 0
        total_confirmed = 0
        for group in groups:
            required = sum(int(part["quantity"]) for part in group["parts"])
            found = 0
            part_progress: list[dict[str, Any]] = []
            for part in group["parts"]:
                key = (group["id"], part["part_id"], part["color_id"])
                part_required = int(part["quantity"])
                allocated = min(part_required, confirmed.get(key, 0))
                part_missing = max(0, part_required - allocated)
                found += allocated
                part_progress.append(
                    {
                        "part_id": part["part_id"],
                        "color_id": part["color_id"],
                        "required": part_required,
                        "confirmed": allocated,
                        "missing": part_missing,
                    }
                )
                if part_missing > 0:
                    missing.append(
                        {
                            "group_id": group["id"],
                            "group_label": group["label"],
                            "part_id": part["part_id"],
                            "color_id": part["color_id"],
                            "required": part_required,
                            "confirmed": allocated,
                            "missing": part_missing,
                        }
                    )
            total_required += required
            total_confirmed += found
            group_progress.append(
                {
                    "group_id": group["id"],
                    "label": group["label"],
                    "required": required,
                    "confirmed": found,
                    "complete": required > 0 and found >= required,
                    "parts": part_progress,
                }
            )
        return {
            "summary": {
                "required_quantity": total_required,
                "confirmed_quantity": total_confirmed,
                "missing_quantity": max(0, total_required - total_confirmed),
                "complete_groups": sum(
                    1 for group in group_progress if group["complete"]
                ),
                "group_count": len(group_progress),
            },
            "groups": group_progress,
            "missing": missing,
        }

    def _readiness(
        self,
        row: sqlite3.Row,
        draft: dict[str, Any],
        bom: dict[str, Any] | None,
        reconciliation: dict[str, Any],
        capacity: dict[str, Any] | None,
        simulation: dict[str, Any] | None,
        acceptance: dict[str, Any] | None,
        green_light: dict[str, Any] | None,
        activation: dict[str, Any] | None,
    ) -> dict[str, Any]:
        validation_issues = draft.get("validation", {}).get("issues", [])
        reviewable_source_codes = {
            "NON_NUMBERED_GROUP_REQUIRES_REVIEW",
            "AUTHORITATIVE_BOM_REQUIRED",
        }
        blocking_source_issues = [
            issue
            for issue in validation_issues
            if isinstance(issue, dict)
            and issue.get("severity") == "error"
            and issue.get("code") not in reviewable_source_codes
        ]
        gates = {
            "source_structurally_valid": not blocking_source_issues,
            "authoritative_bom_frozen": bom is not None,
            "runtime_identifiers_ready": isinstance(bom, dict)
            and isinstance(bom.get("runtime_aliases"), dict)
            and bom["runtime_aliases"].get("status") == "ready",
            "bom_reconciled": reconciliation.get("status")
            in {"validated", "validated_with_extras", "fallback"},
            "review_policies_resolved": not any(
                issue.get("code") == "GROUP_POLICY_REQUIRED"
                for issue in reconciliation.get("issues", [])
            ),
            "capacity_plan_ready": isinstance(capacity, dict)
            and capacity.get("status") == "ready"
            and capacity.get("execution_mode") == "single_wave"
            and isinstance(capacity.get("exception_destination"), dict),
            "simulation_passed": isinstance(simulation, dict)
            and simulation.get("status") == "passed",
            "physical_acceptance_passed": isinstance(acceptance, dict)
            and acceptance.get("result") == "passed"
            and isinstance(simulation, dict)
            and acceptance.get("simulation_run_id") == simulation.get("run_id"),
            "green_light_recorded": isinstance(green_light, dict)
            and green_light.get("physical_bins_verified_empty") is True
            and green_light.get("motion_enabled") is False
            and isinstance(acceptance, dict)
            and green_light.get("acceptance_id") == acceptance.get("acceptance_id")
            and isinstance(simulation, dict)
            and green_light.get("simulation_run_id") == simulation.get("run_id"),
            "live_activation_active": isinstance(activation, dict)
            and str(activation.get("runtime_mode") or "live") == "live"
            and activation.get("status") == "active",
        }
        draft_ready = all(
            gates[name]
            for name in (
                "source_structurally_valid",
                "authoritative_bom_frozen",
                "runtime_identifiers_ready",
                "bom_reconciled",
                "review_policies_resolved",
                "capacity_plan_ready",
            )
        )
        ready_for_green_light = (
            draft_ready
            and gates["simulation_passed"]
            and gates["physical_acceptance_passed"]
        )
        green_light_passed = ready_for_green_light and gates["green_light_recorded"]
        return {
            "gates": gates,
            "draft_ready": draft_ready,
            "ready_for_physical_acceptance": draft_ready and gates["simulation_passed"],
            "ready_for_green_light": ready_for_green_light,
            "green_light_approved": green_light_passed,
            "activation_eligible": row["state"] == "approved"
            and green_light_passed,
            "live_integration_enabled": True,
            "hardware_activation_allowed": row["state"] == "approved"
            and green_light_passed,
            "release_gate": (
                "live_sorting_active"
                if gates["live_activation_active"]
                else "ready_to_activate_live_sorting"
                if green_light_passed
                else "operator_green_light_required"
            ),
        }

    def _assemble(
        self, conn: sqlite3.Connection, row: sqlite3.Row, *, include_events: bool
    ) -> dict[str, Any]:
        draft = project_harvest.load_bsx_draft(self.root, row["draft_id"])
        if draft["manifest_sha256"] != row["draft_manifest_sha256"]:
            raise HarvestProjectError(
                "DRAFT_REVISION_MISMATCH", "The project draft revision has changed."
            )
        bom = self._selected_bom(conn, row)
        set_metadata = self._set_metadata(conn, row, bom)
        policy = _decode_json(row["policy_json"], {})
        mappings = _decode_json(row["mapping_json"], {})
        reconciliation = self._reconcile(row, draft, bom)
        capacity = _decode_json(row["capacity_plan_json"], None)
        simulation = _decode_json(row["simulation_json"], None)
        acceptance = _decode_json(row["acceptance_json"], None)
        green_light = _decode_json(row["green_light_json"], None)
        activation = _decode_json(row["activation_json"], None)
        groups = self._effective_groups(row, draft, bom)
        progress = self._progress(conn, row, groups)
        allocation_rows = conn.execute(
            "SELECT * FROM harvest_allocations WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
            (row["project_id"], MAX_RECENT_ALLOCATIONS),
        ).fetchall()
        allocations = [
            self._allocation_dict(item)
            for item in reversed(allocation_rows)
        ]
        allocation_counts = {
            str(item["status"]): int(item["count"])
            for item in conn.execute(
                "SELECT status, COUNT(*) AS count FROM harvest_allocations WHERE project_id = ? GROUP BY status",
                (row["project_id"],),
            ).fetchall()
        }
        exception_counts = conn.execute(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN status = 'confirmed' THEN 1 ELSE 0 END) AS confirmed FROM harvest_allocations WHERE project_id = ? AND group_id = ?",
            (row["project_id"], HARVEST_EXCEPTION_GROUP_ID),
        ).fetchone()
        result = {
            "schema_version": PROJECT_SCHEMA_VERSION,
            "project_id": row["project_id"],
            "draft_id": row["draft_id"],
            "draft_manifest_sha256": row["draft_manifest_sha256"],
            "set_number": row["set_number"],
            "name": row["name"],
            "state": row["state"],
            "priority": int(row["priority"]),
            "match_policy": row["match_policy"],
            "fallback_mode": row["fallback_mode"],
            "revision": int(row["revision"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "draft_source": {
                "provider": draft.get("source", {}).get("provider"),
                "source_kind": draft.get("source", {}).get("source_kind")
                or draft.get("source", {}).get("provider"),
                "filename": draft.get("source", {}).get("filename"),
                "created_at": draft.get("created_at"),
            },
            "policy": policy,
            "mappings": mappings,
            "bom": bom,
            "set_metadata": set_metadata,
            "reconciliation": reconciliation,
            "capacity_plan": capacity,
            "simulation": simulation,
            "acceptance": acceptance,
            "green_light": green_light,
            "activation": activation,
            "effective_groups": groups,
            "progress": progress,
            "allocations": allocations,
            "allocation_summary": {
                "total": sum(allocation_counts.values()),
                "by_status": allocation_counts,
                "returned": len(allocations),
                "exception_total": int(exception_counts["total"] or 0),
                "exception_confirmed": int(exception_counts["confirmed"] or 0),
            },
        }
        result["readiness"] = self._readiness(
            row,
            draft,
            bom,
            reconciliation,
            capacity,
            simulation,
            acceptance,
            green_light,
            activation,
        )
        if include_events:
            events = self._events(conn, row["project_id"])
            result["events"] = events[-MAX_RECENT_EVENTS:]
            result["event_summary"] = {
                "total": len(events),
                "returned": min(len(events), MAX_RECENT_EVENTS),
            }
        return result

    def get_project(
        self, project_id: str, *, include_events: bool = True
    ) -> dict[str, Any]:
        with self._connection() as conn:
            row = self._project_row(conn, project_id)
            return self._assemble(conn, row, include_events=include_events)

    def acceptance_allocations(
        self, project_id: str, *, acceptance_run_id: str | None = None
    ) -> dict[str, Any]:
        """Return the durable confirmed allocations for one controlled test.

        This intentionally queries by the acceptance runtime id instead of using
        the project's bounded recent-allocation list.  The evidence therefore
        remains exact even after later live sorting adds thousands of records.
        """

        with self._connection() as conn:
            row = self._project_row(conn, project_id)
            activation = _decode_json(row["activation_json"], None)
            acceptance = _decode_json(row["acceptance_json"], None)
            resolved_run_id = str(acceptance_run_id or "").strip()
            if not resolved_run_id and isinstance(activation, dict):
                if activation.get("runtime_mode") == "acceptance":
                    resolved_run_id = str(activation.get("activation_id") or "").strip()
            if not resolved_run_id and isinstance(acceptance, dict):
                resolved_run_id = str(
                    acceptance.get("acceptance_run_id") or ""
                ).strip()
            if not resolved_run_id:
                return {"acceptance_run_id": None, "allocations": []}
            normalized_run_id = _identifier(resolved_run_id, "acceptance_run_id")
            rows = conn.execute(
                "SELECT * FROM harvest_allocations WHERE project_id = ? "
                "AND runtime_id = ? AND mode = 'acceptance' AND status = 'confirmed' "
                "ORDER BY confirmed_at, allocation_id",
                (project_id, normalized_run_id),
            ).fetchall()
            return {
                "acceptance_run_id": normalized_run_id,
                "allocations": [self._allocation_dict(item) for item in rows],
            }

    def verify_all(self) -> dict[str, Any]:
        projects = self.list_projects()
        verified: list[str] = []
        with self._connection() as conn:
            for project in projects:
                project_id = project["project_id"]
                self._events(conn, project_id)
                row = self._project_row(conn, project_id)
                self._selected_bom(conn, row)
                verified.append(project_id)
        return {"status": "ok", "verified_projects": verified, "count": len(verified)}


def portfolio_candidate(
    projects: list[dict[str, Any]], *, part_id: str, color_id: str
) -> dict[str, Any] | None:
    """Choose the highest-priority project with an incomplete matching quota."""

    candidates: list[tuple[int, str, dict[str, Any], dict[str, Any]]] = []
    for project in projects:
        for group_index, group in enumerate(project.get("effective_groups", [])):
            for part in group.get("parts", []):
                if part["part_id"] == part_id and part["color_id"] == color_id:
                    candidates.append(
                        (
                            -int(project["priority"]),
                            f"{group_index:08d}",
                            project,
                            group,
                        )
                    )
                    break
    if not candidates:
        return None
    _, _, project, group = sorted(
        candidates, key=lambda item: (item[0], item[1], item[2]["created_at"])
    )[0]
    return {"project_id": project["project_id"], "group_id": group["id"]}


def missing_parts_csv(project: dict[str, Any]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "project_id",
            "set_number",
            "group_id",
            "group_label",
            "part_id",
            "color_id",
            "required",
            "confirmed",
            "missing",
        ],
        lineterminator="\n",
    )
    writer.writeheader()
    for item in project.get("progress", {}).get("missing", []):
        writer.writerow(
            {
                "project_id": project["project_id"],
                "set_number": project["set_number"],
                **item,
            }
        )
    return output.getvalue().encode("utf-8")
