"""Explicit, inactive legacy-bin backfill for the smart-bin v2 ledger.

Planning reads a coherent legacy snapshot. Applying requires a preinstalled
migration extension and re-plans under the caller-owned critical transaction.
Neither operation is wired into startup or runtime routing.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from local_state import local_state_db_path
from smart_bins_storage import check_schema_version, critical_transaction


EXTENSION_VERSION = 2
_LEGACY_TABLES = (
    "sorting_sessions", "bin_state_current", "bin_item_aggregates", "piece_events",
    "bin_events", "bin_snapshots", "bin_snapshot_layers", "bin_snapshot_items",
    "bin_layouts",
)
_EXT_DDL = (
    "CREATE TABLE smart_bin_migration_versions ("
    "singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL, "
    "installed_at REAL NOT NULL)",
    "CREATE TABLE smart_bin_migration_imports ("
    "id TEXT NOT NULL PRIMARY KEY, source_fingerprint TEXT NOT NULL UNIQUE, "
    "scope_key TEXT NOT NULL UNIQUE, plan_hash TEXT NOT NULL, created_at REAL NOT NULL, "
    "result_json TEXT NOT NULL)",
    "CREATE TABLE smart_bin_migration_anchors ("
    "id TEXT NOT NULL PRIMARY KEY, import_id TEXT NOT NULL, cycle_id TEXT NOT NULL UNIQUE, "
    "machine_id TEXT NOT NULL, source_ref TEXT NOT NULL, session_id TEXT NOT NULL, "
    "layer_index INTEGER NOT NULL, section_index INTEGER NOT NULL, bin_index INTEGER NOT NULL, "
    "bin_epoch INTEGER NOT NULL, legacy_total INTEGER, coverage TEXT NOT NULL, "
    "linked_sessions_json TEXT NOT NULL, "
    "FOREIGN KEY(import_id) REFERENCES smart_bin_migration_imports(id), "
    "FOREIGN KEY(cycle_id) REFERENCES smart_bin_cycles(id), "
    "FOREIGN KEY(machine_id) REFERENCES smart_bin_machines(machine_id), "
    "FOREIGN KEY(session_id) REFERENCES sorting_sessions(id))",
    "CREATE TABLE smart_bin_migration_group_sources ("
    "group_key_id TEXT NOT NULL PRIMARY KEY, legacy_item_key TEXT, category_id TEXT, "
    "classification_status TEXT, source_context TEXT NOT NULL, "
    "FOREIGN KEY(group_key_id) REFERENCES smart_bin_group_keys(id))",
    "CREATE TABLE smart_bin_historical_contributions ("
    "id TEXT NOT NULL PRIMARY KEY, import_id TEXT NOT NULL, cycle_id TEXT NOT NULL, "
    "machine_id TEXT NOT NULL, piece_uuid TEXT NOT NULL, group_key_id TEXT NOT NULL, "
    "quantity INTEGER NOT NULL CHECK(quantity=1), source_event_ids_json TEXT NOT NULL, "
    "runtime_run_id TEXT NOT NULL, sorting_session_id TEXT NOT NULL, "
    "source_distributed_at REAL NOT NULL, "
    "source_ref TEXT NOT NULL, provenance TEXT NOT NULL, "
    "UNIQUE(machine_id,piece_uuid), "
    "FOREIGN KEY(import_id) REFERENCES smart_bin_migration_imports(id), "
    "FOREIGN KEY(cycle_id) REFERENCES smart_bin_cycles(id), "
    "FOREIGN KEY(machine_id) REFERENCES smart_bin_machines(machine_id), "
    "FOREIGN KEY(group_key_id) REFERENCES smart_bin_group_keys(id), "
    "FOREIGN KEY(sorting_session_id) REFERENCES sorting_sessions(id))",
    "CREATE TRIGGER smart_bin_migration_no_native_duplicate_insert "
    "BEFORE INSERT ON smart_bin_historical_contributions WHEN EXISTS ("
    "SELECT 1 FROM smart_bin_deliveries d WHERE d.machine_id=NEW.machine_id "
    "AND d.piece_uuid=NEW.piece_uuid) "
    "BEGIN SELECT RAISE(ABORT,'piece already credited by native delivery'); END",
    "CREATE TRIGGER smart_bin_migration_no_native_duplicate_update "
    "BEFORE UPDATE OF machine_id,piece_uuid ON smart_bin_historical_contributions WHEN EXISTS ("
    "SELECT 1 FROM smart_bin_deliveries d WHERE d.machine_id=NEW.machine_id "
    "AND d.piece_uuid=NEW.piece_uuid) "
    "BEGIN SELECT RAISE(ABORT,'piece already credited by native delivery'); END",
    "CREATE TRIGGER smart_bin_migration_no_historical_duplicate_insert "
    "BEFORE INSERT ON smart_bin_deliveries WHEN EXISTS ("
    "SELECT 1 FROM smart_bin_historical_contributions h WHERE h.machine_id=NEW.machine_id "
    "AND h.piece_uuid=NEW.piece_uuid) "
    "BEGIN SELECT RAISE(ABORT,'piece already credited by historical contribution'); END",
    "CREATE TRIGGER smart_bin_migration_no_historical_duplicate_update "
    "BEFORE UPDATE OF machine_id,piece_uuid ON smart_bin_deliveries WHEN EXISTS ("
    "SELECT 1 FROM smart_bin_historical_contributions h WHERE h.machine_id=NEW.machine_id "
    "AND h.piece_uuid=NEW.piece_uuid) "
    "BEGIN SELECT RAISE(ABORT,'piece already credited by historical contribution'); END",
)


class MigrationConflict(RuntimeError):
    """The legacy source or an earlier import conflicts with the requested plan."""


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _id(kind: str, *parts: Any) -> str:
    return f"migration-{kind}-{_digest(parts)[:32]}"


def _rows(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cursor = conn.execute(query, params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _check_extension(conn: sqlite3.Connection, *, required: bool) -> bool:
    if not _table_exists(conn, "smart_bin_migration_versions"):
        if required:
            raise MigrationConflict("migration extension is not initialized")
        return False
    version = conn.execute("SELECT singleton,version FROM smart_bin_migration_versions").fetchall()
    if len(version) != 1 or tuple(version[0]) != (1, EXTENSION_VERSION):
        raise MigrationConflict("unsupported migration extension version")
    for name in ("smart_bin_migration_imports", "smart_bin_migration_anchors",
                 "smart_bin_migration_group_sources", "smart_bin_historical_contributions"):
        if not _table_exists(conn, name):
            raise MigrationConflict(f"incomplete migration extension: {name}")
    for name in ("smart_bin_migration_no_native_duplicate_insert",
                 "smart_bin_migration_no_native_duplicate_update",
                 "smart_bin_migration_no_historical_duplicate_insert",
                 "smart_bin_migration_no_historical_duplicate_update"):
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
                        (name,)).fetchone() is None:
            raise MigrationConflict(f"incomplete migration extension: {name}")
    return True


def initialize_migration_schema() -> int:
    """Explicit additive extension install; core smart-bin schema remains v2."""
    with critical_transaction() as conn:
        if not _check_extension(conn, required=False):
            for ddl in _EXT_DDL:
                conn.execute(ddl)
            conn.execute("INSERT INTO smart_bin_migration_versions VALUES(1,?,?)",
                         (EXTENSION_VERSION, time.time()))
        conn.commit()
    return EXTENSION_VERSION


def _legacy_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    if check_schema_version(conn) != 2:
        raise MigrationConflict("smart-bin v2 schema is required")
    snapshot: dict[str, Any] = {}
    for table in _LEGACY_TABLES:
        if not _table_exists(conn, table):
            raise MigrationConflict(f"missing legacy table: {table}")
        # Canonical JSON sorting below makes row order and SQLite query plans irrelevant.
        records = _rows(conn, f"SELECT * FROM {table}")
        snapshot[table] = sorted(records, key=lambda row: json.dumps(row, sort_keys=True))
    snapshot["metadata"] = _rows(conn, "SELECT key,value FROM metadata WHERE key IN "
                                      "('active_sorting_session_id','open_bin_snapshot_id') ORDER BY key")
    snapshot["state_entries"] = _rows(conn, "SELECT key,json_value,updated_at FROM state_entries "
                                           "WHERE key IN ('machine_id','bin_layout','bin_categories',"
                                           "'sorting_profile_sync') ORDER BY key")
    snapshot["piece_records"] = (_rows(conn, "SELECT * FROM piece_records")
                                  if _table_exists(conn, "piece_records") else None)
    return snapshot


def _coords(row: dict[str, Any]) -> tuple[int, int, int]:
    return (row["layer_index"], row["section_index"], row["bin_index"])


def _item_identity(row: dict[str, Any]) -> tuple[Any, ...]:
    return (row.get("item_key"), row.get("part_id"), row.get("color_id"),
            row.get("category_id"), row.get("classification_status"))


def _event_item_identity(row: dict[str, Any]) -> tuple[Any, ...]:
    return ("|".join(str(row.get(name) or "") for name in
                     ("part_id", "color_id", "category_id", "classification_status")),
            row.get("part_id"), row.get("color_id"), row.get("category_id"),
            row.get("classification_status"))


def _clear_matches(event: dict[str, Any], layer: dict[str, Any]) -> bool:
    if event["session_id"] != layer["session_id"] or event["created_at"] != layer["flushed_at"]:
        return False
    kind = event["event_type"]
    if kind == "all_cleared":
        return True
    if kind == "layer_cleared":
        return event["layer_index"] == layer["layer_index"]
    if kind == "bin_cleared":
        return _coords(event) == _coords(layer)
    if kind == "selection_cleared":
        try:
            details = json.loads(event["details_json"] or "{}")
            return any(_coords(item) == _coords(layer) for item in details.get("bins", []))
        except (TypeError, ValueError, KeyError):
            return False
    return False


def _source_items(source: dict[str, Any], session_id: str,
                  coordinates: tuple[int, int, int]) -> list[dict[str, Any]]:
    return [row for row in source["bin_item_aggregates"]
            if row["session_id"] == session_id and _coords(row) == coordinates]


def _session_events(source: dict[str, Any], session_id: str,
                    coordinates: tuple[int, int, int], epoch: int) -> list[dict[str, Any]]:
    return [row for row in source["piece_events"] if row["session_id"] == session_id
            and _coords(row) == coordinates and row["bin_epoch"] == epoch]


def _layout_evidenced(source: dict[str, Any], older: dict[str, Any],
                      newer: dict[str, Any]) -> bool:
    if not older["profile_id"] or older["profile_id"] != newer["profile_id"]:
        return False
    if not older["artifact_hash"] or older["artifact_hash"] != newer["artifact_hash"]:
        return False
    # Activation has no legacy journal. A competing saved layout or a later
    # mutable layout write removes this narrow proof of unchanged geometry.
    layouts = source["bin_layouts"]
    mutable_layout = next((row for row in source["state_entries"]
                           if row["key"] == "bin_layout"), None)
    if len(layouts) != 1 or (mutable_layout is not None and
                             mutable_layout["updated_at"] > older["started_at"]):
        return False
    layout = layouts[0]
    return (layout["profile_id"] == older["profile_id"] and layout["is_active"] == 1
            and layout["created_at"] <= older["started_at"]
            and layout["updated_at"] <= older["started_at"])


def _lineage(source: dict[str, Any], anchor: dict[str, Any],
             sessions: dict[str, dict[str, Any]]) -> tuple[list[str], bool]:
    """Trace only the specific carry-forward shape produced by local_state."""
    chain = [anchor["session_id"]]
    current = sessions[chain[0]]
    coords = tuple(anchor["coordinates"])
    epoch = anchor["epoch"]
    count = anchor["total"]
    items = {row["item_key"]: row["count"] for row in anchor["items"]}
    unsupported = False
    while True:
        older = [row for row in sessions.values() if row["machine_id"] == current["machine_id"]
                 and row["id"] != current["id"] and row["ended_at"] is not None
                 and row["ended_at"] <= current["started_at"]]
        if not older:
            return chain, unsupported
        older.sort(key=lambda row: row["ended_at"], reverse=True)
        previous = older[0]
        # A gap, overlap, missing start event or competing predecessor is not a
        # proven session handoff. Equal coordinates/epochs alone are insufficient.
        starts = [event for event in source["bin_events"] if event["session_id"] == current["id"]
                  and event["event_type"] == "session_started"]
        ambiguous = len(older) > 1 and older[1]["ended_at"] == previous["ended_at"]
        gap = current["started_at"] - previous["ended_at"]
        if (ambiguous or not 0 <= gap <= 1 or len(starts) != 1
                or starts[0]["created_at"] != current["started_at"]
                or previous["reason"] != current["reason"]
                or not _layout_evidenced(source, previous, current)
                or any(event["session_id"] == current["id"] and
                       (event["event_type"] == "snapshot_imported" or
                        event["event_type"].endswith("_cleared"))
                       for event in source["bin_events"])):
            return chain, True
        prior_rows = [row for row in source["bin_state_current"]
                      if row["session_id"] == previous["id"] and _coords(row) == coords]
        if len(prior_rows) != 1 or prior_rows[0]["bin_epoch"] != epoch:
            return chain, True
        new_events = _session_events(source, current["id"], coords, epoch)
        residual = dict(items)
        for event in new_events:
            key = _event_item_identity(event)[0]
            residual[key] = residual.get(key, 0) - 1
        prior_items = {row["item_key"]: row["count"] for row in
                       _source_items(source, previous["id"], coords)}
        if (not isinstance(count, int) or count - len(new_events) != prior_rows[0]["piece_count"]
                or {key: value for key, value in residual.items() if value != 0} !=
                {key: value for key, value in prior_items.items() if value != 0}):
            return chain, True
        chain.append(previous["id"])
        current = previous
        count = prior_rows[0]["piece_count"]
        items = prior_items
        if count == 0:
            return chain, unsupported


def _history_matches(event: dict[str, Any], history: dict[str, Any] | None,
                     session: dict[str, Any]) -> bool:
    if history is None:
        return False
    return (isinstance(history["run_id"], str) and bool(history["run_id"].strip())
            and history["machine_id"] == session["machine_id"]
            and history["dead"] == 0
            and (history["bin_x"], history["bin_y"], history["bin_z"]) == _coords(event)
            and history["recorded_at"] == event["distributed_at"]
            and event["distributed_at"] >= session["started_at"]
            and (session["ended_at"] is None
                 or event["distributed_at"] <= session["ended_at"])
            and all(history[name] == event[name]
                    for name in ("part_id", "color_id", "category_id",
                                 "classification_status")))


def _clear_between(source: dict[str, Any], sessions: dict[str, dict[str, Any]],
                   anchor: dict[str, Any], earlier_at: float) -> bool:
    """A recorded clear is the only supported exclusion for earlier bin stock."""
    for layer in source["bin_snapshot_layers"]:
        session = sessions.get(layer["session_id"])
        if (session is not None and session["machine_id"] == anchor["machine_id"]
                and _coords(layer) == tuple(anchor["coordinates"])
                and earlier_at <= layer["flushed_at"] < anchor["anchor_time"]
                and any(_clear_matches(event, layer) for event in source["bin_events"])):
            return True
    return False


def _group(item: dict[str, Any] | None) -> dict[str, Any]:
    if item is None:
        identity = (None, None, None, None, None)
    else:
        identity = _item_identity(item)
    return {
        "id": _id("group", identity),
        "kind": "legacy_item" if item is not None else "unidentified",
        "namespace": "legacy_unqualified",
        "part_id": identity[1], "color_id": identity[2],
        "category_id": identity[3], "classification_status": identity[4],
        "legacy_item_key": identity[0],
        "source_context": "legacy row; part/color/category namespaces unverified",
    }


def _build_plan(conn: sqlite3.Connection) -> dict[str, Any]:
    _check_extension(conn, required=False)
    source = _legacy_snapshot(conn)
    fingerprint = _digest(source)
    sessions = {row["id"]: row for row in source["sorting_sessions"]}
    active_id = next((row["value"] for row in source["metadata"]
                      if row["key"] == "active_sorting_session_id"), None)
    active = sessions.get(active_id)
    if active is not None and active["status"] != "active":
        active = None
    history = {row["uuid"]: row for row in source["piece_records"] or []}
    imported_sessions = {row["session_id"] for row in source["bin_events"]
                         if row["event_type"] == "snapshot_imported"}
    native_rows = _rows(conn, "SELECT d.id,d.machine_id,d.piece_uuid,d.actual_kind,"
                        "d.delivered_at,s.layer_index,s.section_index,s.bin_index "
                        "FROM smart_bin_deliveries d LEFT JOIN smart_bin_slots s "
                        "ON s.id=d.actual_slot_id ORDER BY d.id")
    native = {(row["machine_id"], row["piece_uuid"]) for row in native_rows}
    raw_anchors: list[dict[str, Any]] = []
    global_blockers: list[dict[str, Any]] = []
    state_keys = {(row["session_id"], *_coords(row)) for row in source["bin_state_current"]}
    for item in source["bin_item_aggregates"]:
        if (item["session_id"], *_coords(item)) not in state_keys and item["count"] != 0:
            global_blockers.append({"kind": "orphan_positive_aggregate",
                                    "source_ref": f"bin_item_aggregates:{item['session_id']}:"
                                                  f"{_coords(item)}:{item['item_key']}",
                                    "count": item["count"]})
    if active is None:
        occupied_states = [row for row in source["bin_state_current"] if row["piece_count"] != 0]
        occupied_items = [row for row in source["bin_item_aggregates"] if row["count"] != 0]
        if occupied_states or occupied_items:
            global_blockers.append({"kind": "unowned_current_occupancy",
                                    "active_session_id": active_id,
                                    "state_refs": sorted(f"{row['session_id']}:{_coords(row)}"
                                                         for row in occupied_states),
                                    "aggregate_refs": sorted(
                                        f"{row['session_id']}:{_coords(row)}:{row['item_key']}"
                                        for row in occupied_items)})
    else:
        for state in source["bin_state_current"]:
            if state["session_id"] == active_id:
                coords = _coords(state)
                items = _source_items(source, active_id, coords)
                if state["piece_count"] == 0 and not any(item["count"] != 0 for item in items):
                    continue
                raw_anchors.append({"source_ref": f"current:{active_id}:{coords}:{state['bin_epoch']}",
                                    "source_refs": [f"bin_state_current:{active_id}:{coords}"],
                                    "session_id": active_id, "coordinates": coords,
                                    "epoch": state["bin_epoch"], "total": state["piece_count"],
                                    "anchor_time": state["updated_at"], "closed_at": None,
                                    "items": items})
    layers: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for layer in source["bin_snapshot_layers"]:
        layers[(layer["session_id"], *_coords(layer), layer["bin_epoch"])].append(layer)
    snapshot_items = source["bin_snapshot_items"]
    for key, repeated in sorted(layers.items()):
        repeated.sort(key=lambda row: row["id"])
        first = repeated[0]
        items = [row for row in snapshot_items if row["snapshot_layer_id"] == first["id"]]
        raw_anchors.append({"source_ref": f"snapshot:{first['id']}",
                            "source_refs": [f"bin_snapshot_layers:{row['id']}" for row in repeated],
                            "session_id": first["session_id"], "coordinates": _coords(first),
                            "epoch": first["bin_epoch"], "total": first["piece_count"],
                            "anchor_time": first["flushed_at"], "closed_at": first["flushed_at"],
                            "items": items,
                            "repeat_conflict": any(row["piece_count"] != first["piece_count"] or
                                                   sorted(((_item_identity(item), item["count"])
                                                           for item in snapshot_items if
                                                           item["snapshot_layer_id"] == row["id"]),
                                                          key=repr) !=
                                                   sorted(((_item_identity(item), item["count"])
                                                           for item in items), key=repr)
                                                   for row in repeated[1:]),
                            "clear_proven": any(_clear_matches(event, first) for event in source["bin_events"])})
    raw_anchors.sort(key=lambda anchor: anchor["source_ref"])
    anchors: list[dict[str, Any]] = []
    events_by_uuid: dict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for raw in raw_anchors:
        session = sessions.get(raw["session_id"])
        if session is None:
            global_blockers.append({"kind": "orphan_snapshot_session",
                                    "source_ref": raw["source_ref"],
                                    "session_id": raw["session_id"]})
            continue
        anchor = {key: value for key, value in raw.items() if key != "items"}
        anchor["machine_id"] = session["machine_id"]
        anchor["cycle_id"] = _id("cycle", session["machine_id"], raw["source_ref"])
        anchor["id"] = _id("anchor", raw["source_ref"])
        chain, uncertain = _lineage(source, raw, sessions)
        anchor["linked_sessions"] = chain
        anchor["coverage"] = "historical_unknown" if uncertain or len(chain) == 1 else "bounded_lineage"
        anchor["blocked"] = False
        anchor["items"] = raw["items"]
        anchor["events"] = []
        for session_id in chain:
            for event in _session_events(source, session_id, tuple(raw["coordinates"]), raw["epoch"]):
                if raw["closed_at"] is not None and event["distributed_at"] > raw["closed_at"]:
                    continue
                anchor["events"].append(event)
                events_by_uuid[(session["machine_id"], event["piece_uuid"])].append(
                    (anchor["source_ref"], event))
        anchors.append(anchor)
    mapped_event_ids = {event["id"] for occurrences in events_by_uuid.values()
                        for _, event in occurrences}
    for event in source["piece_events"]:
        session = sessions.get(event["session_id"])
        if session is not None and event["id"] not in mapped_event_ids:
            events_by_uuid[(session["machine_id"], event["piece_uuid"])].append(
                (f"unmapped:{event['session_id']}", event))
    groups: dict[str, dict[str, Any]] = {}
    contributions: list[dict[str, Any]] = []
    balances: list[dict[str, Any]] = []
    discrepancies: list[dict[str, Any]] = []
    def problem(anchor: dict[str, Any], kind: str, detail: Any) -> None:
        ref = anchor["source_ref"]
        discrepancies.append({"id": _id("discrepancy", ref, kind, detail),
                              "machine_id": anchor["machine_id"], "cycle_id": anchor["cycle_id"],
                              "kind": kind, "source_ref": ref, "detail": detail, "blocking": True})
        anchor["blocked"] = True
    for anchor in anchors:
        if anchor.get("repeat_conflict"):
            problem(anchor, "conflicting_repeated_snapshot", anchor["source_refs"])
        if anchor["source_ref"].startswith("snapshot:") and not anchor.get("clear_proven"):
            problem(anchor, "missing_clear_evidence", anchor["source_refs"])
        total = anchor["total"]
        if not isinstance(total, int) or total < 0:
            problem(anchor, "unknown_or_negative_quantity", total)
            continue
        item_counts: Counter[tuple[Any, ...]] = Counter()
        items_by_identity: dict[tuple[Any, ...], dict[str, Any]] = {}
        for item in anchor["items"]:
            identity = _item_identity(item)
            if not isinstance(item["count"], int) or item["count"] < 0:
                problem(anchor, "unknown_or_negative_group_quantity", identity)
                continue
            item_counts[identity] += item["count"]
            items_by_identity[identity] = item
        if item_counts and sum(item_counts.values()) != total:
            problem(anchor, "count_group_conflict", {"total": total, "groups": sum(item_counts.values())})
        unidentified = not item_counts and total > 0
        if unidentified:
            problem(anchor, "unidentified_group", total)
        if anchor["machine_id"] == "unknown-machine":
            problem(anchor, "unknown_machine_identity", anchor["session_id"])
        if anchor["session_id"] in imported_sessions:
            problem(anchor, "imported_aggregate_lineage_uncertain", anchor["session_id"])
        linked_counts: Counter[tuple[Any, ...]] = Counter()
        candidate_contributions: list[dict[str, Any]] = []
        checked_native_duplicates: set[str] = set()
        for (machine_id, uuid), occurrences in sorted(events_by_uuid.items()):
            own = [(ref, event) for ref, event in occurrences if ref == anchor["source_ref"]]
            if not own:
                continue
            distinct = {(ref, event["distributed_at"], _event_item_identity(event),
                         tuple(_coords(event)), event["bin_epoch"])
                        for ref, event in occurrences}
            if len(distinct) != 1:
                problem(anchor, "conflicting_piece_identity", {"piece_uuid": uuid,
                         "event_ids": sorted(event["id"] for _, event in occurrences)})
                continue
            event = own[0][1]
            if any(row["session_id"] in imported_sessions for _, row in own):
                problem(anchor, "import_generated_or_unverified_piece", uuid)
                continue
            if (machine_id, uuid) in native:
                problem(anchor, "native_delivery_duplicate", uuid)
                checked_native_duplicates.add(uuid)
                continue
            if not any(_history_matches(row, history.get(uuid),
                                        sessions[row["session_id"]]) for _, row in own):
                problem(anchor, "unverified_piece_history", uuid)
                continue
            identity = _event_item_identity(event)
            if identity not in item_counts:
                problem(anchor, "unsupported_piece_group", uuid)
                continue
            linked_counts[identity] += 1
            group = _group(items_by_identity[identity])
            groups[group["id"]] = group
            candidate_contributions.append({"id": _id("contribution", machine_id, uuid, anchor["cycle_id"]),
                                            "cycle_id": anchor["cycle_id"], "machine_id": machine_id,
                                            "piece_uuid": uuid, "group_key_id": group["id"],
                                            "quantity": 1,
                                            "source_event_ids": sorted(row["id"] for _, row in own),
                                            "runtime_run_id": history[uuid]["run_id"],
                                            "sorting_session_id": event["session_id"],
                                            "source_distributed_at": event["distributed_at"],
                                            "source_ref": anchor["source_ref"]})
        for identity, count in item_counts.items():
            if linked_counts[identity] > count:
                problem(anchor, "negative_group_residual", {"group": identity,
                         "recorded": count, "linked": linked_counts[identity]})
        if sum(linked_counts.values()) > total:
            problem(anchor, "negative_total_residual", {"recorded": total,
                    "linked": sum(linked_counts.values())})
        if total > sum(linked_counts.values()):
            for delivery in native_rows:
                if (delivery["machine_id"] != anchor["machine_id"]
                        or delivery["piece_uuid"] in checked_native_duplicates):
                    continue
                prior_occurrences = [(ref, event) for ref, event in events_by_uuid.get(
                    (anchor["machine_id"], delivery["piece_uuid"]), [])
                    if _coords(event) == tuple(anchor["coordinates"])
                    and event["bin_epoch"] == anchor["epoch"]
                    and event["distributed_at"] <= anchor["anchor_time"]]
                prior_events = [event for _, event in prior_occurrences]
                same_destination = (
                    delivery["actual_kind"] == "BIN"
                    and (delivery["layer_index"], delivery["section_index"],
                         delivery["bin_index"]) == tuple(anchor["coordinates"])
                    and delivery["delivered_at"] <= anchor["anchor_time"]
                    and not _clear_between(source, sessions, anchor, delivery["delivered_at"]))
                if prior_events or same_destination:
                    basis = ("unverified_anchor_event"
                             if any(ref == anchor["source_ref"] for ref, _ in prior_occurrences)
                             else "unlinked_event" if prior_events else "same_bin_before_anchor")
                    problem(anchor, "potential_native_balance_overlap",
                            {"native_delivery_id": delivery["id"],
                             "legacy_event_ids": sorted(row["id"] for row in prior_events),
                             "basis": basis})
        hard_blockers = {"conflicting_repeated_snapshot", "missing_clear_evidence",
                         "unknown_or_negative_quantity", "unknown_or_negative_group_quantity",
                         "count_group_conflict", "unknown_machine_identity",
                         "conflicting_piece_identity", "native_delivery_duplicate",
                         "potential_native_balance_overlap",
                         "unsupported_piece_group", "negative_group_residual",
                         "negative_total_residual"}
        unsafe_credit = any(issue["cycle_id"] == anchor["cycle_id"] and
                            issue["kind"] in hard_blockers for issue in discrepancies)
        # Uncertain identity is carried by opening balance, not a fabricated piece.
        if not unsafe_credit:
            contributions.extend(candidate_contributions)
            if item_counts:
                for identity, count in sorted(item_counts.items(), key=lambda pair: repr(pair[0])):
                    residual = count - linked_counts[identity]
                    if residual > 0:
                        group = _group(items_by_identity[identity])
                        groups[group["id"]] = group
                        balances.append({"id": _id("balance", anchor["cycle_id"], group["id"]),
                                         "cycle_id": anchor["cycle_id"], "group_key_id": group["id"],
                                         "quantity": residual, "coverage": anchor["coverage"],
                                         "source_ref": anchor["source_ref"]})
        if unidentified and not unsafe_credit:
            # Trust the total only. Unknown membership is explicit and blocks activation.
            group = _group(None)
            groups[group["id"]] = group
            balances.append({"id": _id("balance", anchor["cycle_id"], group["id"]),
                             "cycle_id": anchor["cycle_id"], "group_key_id": group["id"],
                             "quantity": total, "coverage": "unknown",
                             "source_ref": anchor["source_ref"]})
        anchor.pop("items")
        anchor.pop("events")
    plan = {"source_fingerprint": fingerprint, "scope_key": "legacy-bin-backfill-v2",
            "anchors": anchors, "groups": sorted(groups.values(), key=lambda item: item["id"]),
            "contributions": sorted(contributions, key=lambda item: item["id"]),
            "balances": sorted(balances, key=lambda item: item["id"]),
            "discrepancies": sorted(discrepancies, key=lambda item: item["id"]),
            "global_blockers": global_blockers}
    plan["plan_hash"] = _digest(plan)
    return plan


def plan_legacy_backfill(db_path: Path | None = None) -> dict[str, Any]:
    """Read-only plan from one legacy SQLite snapshot; never creates a database."""
    path = Path(db_path) if db_path is not None else local_state_db_path()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5.0)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN")
        plan = _build_plan(conn)
        conn.rollback()
        return plan
    finally:
        conn.close()


def _write_plan(conn: sqlite3.Connection, plan: dict[str, Any]) -> dict[str, Any]:
    import_id = _id("import", plan["source_fingerprint"])
    now = time.time()
    result = {"import_id": import_id, "source_fingerprint": plan["source_fingerprint"],
              "cycles": len(plan["anchors"]), "contributions": len(plan["contributions"]),
              "opening_balances": len(plan["balances"]),
              "blocking_discrepancies": len(plan["discrepancies"]), "replayed": False}
    conn.execute("INSERT INTO smart_bin_migration_imports "
                 "(id,source_fingerprint,scope_key,plan_hash,created_at,result_json) "
                 "VALUES(?,?,?,?,?,?)",
                 (import_id, plan["source_fingerprint"], plan["scope_key"], plan["plan_hash"],
                  now, json.dumps(result, sort_keys=True)))
    machine_ids = sorted({row["machine_id"] for collection in
                          (plan["anchors"], plan["discrepancies"]) for row in collection})
    for machine_id in machine_ids:
        conn.execute("INSERT INTO smart_bin_machines(machine_id) VALUES(?) "
                     "ON CONFLICT(machine_id) DO NOTHING", (machine_id,))
    for group in plan["groups"]:
        conn.execute("INSERT INTO smart_bin_group_keys "
                     "(id,kind,namespace,part_id,color_id,provenance) "
                     "VALUES(?,?,?,?,?,'unknown')",
                     (group["id"], group["kind"], group["namespace"],
                      group["part_id"], group["color_id"]))
        conn.execute("INSERT INTO smart_bin_migration_group_sources "
                     "(group_key_id,legacy_item_key,category_id,classification_status,source_context) "
                     "VALUES(?,?,?,?,?)",
                     (group["id"], group["legacy_item_key"], group["category_id"],
                      group["classification_status"], group["source_context"]))
    for anchor in plan["anchors"]:
        # opened_at is explicitly a source cutoff/anchor, not an observed empty.
        conn.execute("INSERT INTO smart_bin_cycles "
                     "(id,machine_id,opened_at,closed_at,close_reason,provenance) "
                     "VALUES(?,?,?,?,?,?)",
                     (anchor["cycle_id"], anchor["machine_id"], anchor["anchor_time"],
                      anchor["closed_at"],
                      "legacy_clear_snapshot" if anchor["closed_at"] is not None else None,
                      "migration_anchor_not_observed_empty"))
        conn.execute("INSERT INTO smart_bin_migration_anchors "
                     "(id,import_id,cycle_id,machine_id,source_ref,session_id,layer_index,"
                     "section_index,bin_index,bin_epoch,legacy_total,coverage,linked_sessions_json) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (anchor["id"], import_id, anchor["cycle_id"], anchor["machine_id"],
                      anchor["source_ref"], anchor["session_id"], *anchor["coordinates"],
                      anchor["epoch"], anchor["total"], anchor["coverage"],
                      json.dumps(anchor["linked_sessions"])))
    for contribution in plan["contributions"]:
        conn.execute("INSERT INTO smart_bin_historical_contributions "
                     "(id,import_id,cycle_id,machine_id,piece_uuid,group_key_id,quantity,"
                     "source_event_ids_json,runtime_run_id,sorting_session_id,"
                     "source_distributed_at,source_ref,provenance) "
                     "VALUES(?,?,?,?,?,?,1,?,?,?,?,?,?)",
                     (contribution["id"], import_id, contribution["cycle_id"],
                      contribution["machine_id"], contribution["piece_uuid"],
                      contribution["group_key_id"], json.dumps(contribution["source_event_ids"]),
                      contribution["runtime_run_id"], contribution["sorting_session_id"],
                      contribution["source_distributed_at"],
                      contribution["source_ref"], "matched_legacy_event_and_history"))
    for balance in plan["balances"]:
        conn.execute("INSERT INTO smart_bin_opening_balances "
                     "(id,cycle_id,group_key_id,quantity,provenance,coverage,created_at) "
                     "VALUES(?,?,?,?,?,?,?)",
                     (balance["id"], balance["cycle_id"], balance["group_key_id"],
                      balance["quantity"], f"legacy_migration:{import_id}:{balance['source_ref']}",
                      balance["coverage"], now))
    for issue in plan["discrepancies"]:
        conn.execute("INSERT INTO smart_bin_discrepancies "
                     "(id,machine_id,cycle_id,kind,status,evidence_ref,details_json,created_at) "
                     "VALUES(?,?,?,?,?,?,?,?)",
                     (issue["id"], issue["machine_id"], issue["cycle_id"], issue["kind"],
                      "open", issue["source_ref"],
                      json.dumps({"import_id": import_id, "detail": issue["detail"]}, sort_keys=True),
                      now))
    for machine_id in machine_ids:
        before = conn.execute("SELECT state_revision FROM smart_bin_machines WHERE machine_id=?",
                              (machine_id,)).fetchone()[0]
        conn.execute("UPDATE smart_bin_machines SET state_revision=? WHERE machine_id=?",
                     (before + 1, machine_id))
        conn.execute("INSERT INTO smart_bin_audit_events "
                     "(id,machine_id,request_key,payload_hash,before_revision,after_revision,actor,"
                     "reason,evidence_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (_id("audit", import_id, machine_id), machine_id,
                      f"migration:{import_id}:{machine_id}", plan["plan_hash"], before,
                      before + 1, "legacy_backfill", "explicit_inactive_import",
                      plan["source_fingerprint"], now))
    return result


def apply_legacy_backfill(plan: dict[str, Any]) -> dict[str, Any]:
    """Atomically apply an unchanged plan once; exact replay reads its result."""
    with critical_transaction() as conn:
        _check_extension(conn, required=True)
        current = _build_plan(conn)
        if current != plan:
            raise MigrationConflict("stale or modified legacy backfill plan")
        if plan["global_blockers"]:
            raise MigrationConflict("legacy source has unowned blockers; import not applied")
        existing = conn.execute("SELECT source_fingerprint,plan_hash,result_json FROM "
                                "smart_bin_migration_imports WHERE scope_key=?",
                                (plan["scope_key"],)).fetchone()
        if existing is not None:
            if existing[0] != plan["source_fingerprint"] or existing[1] != plan["plan_hash"]:
                raise MigrationConflict("legacy source changed after an earlier import; reconcile explicitly")
            result = json.loads(existing[2])
            result["replayed"] = True
            conn.commit()
            return result
        result = _write_plan(conn, plan)
        conn.commit()
        return result


def read_recorded_contents_on_connection(conn: sqlite3.Connection, cycle_id: str) -> dict[str, Any]:
    """Canonical cycle projection on the caller's existing transaction snapshot."""
    if not conn.in_transaction:
        raise RuntimeError("recorded contents require a caller-owned transaction")
    if check_schema_version(conn) != 2:
        raise MigrationConflict("smart-bin v2 schema is required")
    _check_extension(conn, required=True)
    cycle = conn.execute("SELECT machine_id FROM smart_bin_cycles WHERE id=?", (cycle_id,)).fetchone()
    if cycle is None:
        raise KeyError(cycle_id)
    totals: dict[str | None, dict[str, int]] = defaultdict(lambda: {"native": 0,
                                                                     "historical": 0,
                                                                     "opening_balance": 0})
    for row in conn.execute("SELECT group_key_id,quantity FROM smart_bin_deliveries "
                            "WHERE actual_cycle_id=?", (cycle_id,)):
        totals[row[0]]["native"] += row[1]
    for row in conn.execute("SELECT group_key_id,quantity FROM smart_bin_historical_contributions "
                            "WHERE cycle_id=?", (cycle_id,)):
        totals[row[0]]["historical"] += row[1]
    for row in conn.execute("SELECT group_key_id,quantity FROM smart_bin_opening_balances "
                            "WHERE cycle_id=?", (cycle_id,)):
        totals[row[0]]["opening_balance"] += row[1]
    groups = {row["id"]: row for row in _rows(conn,
              "SELECT g.id,g.kind,g.namespace,g.part_id,g.part_namespace,g.color_id,"
              "g.color_namespace,g.provenance,s.legacy_item_key,s.category_id,"
              "s.classification_status,s.source_context FROM smart_bin_group_keys g "
              "LEFT JOIN smart_bin_migration_group_sources s ON s.group_key_id=g.id")}
    breakdown = [{"group_key_id": group_id, "identity": groups.get(group_id),
                  **quantities, "total": sum(quantities.values())}
                 for group_id, quantities in sorted(totals.items(), key=lambda pair: str(pair[0]))]
    issues = _rows(conn, "SELECT id,kind,status,evidence_ref,details_json,resolved_at FROM "
                   "smart_bin_discrepancies WHERE cycle_id=? ORDER BY id", (cycle_id,))
    if _table_exists(conn, "smart_bin_reconciliation_resolutions"):
        resolutions = {row["discrepancy_id"]: row for row in _rows(conn,
            "SELECT r.* FROM smart_bin_reconciliation_resolutions r "
            "JOIN smart_bin_discrepancies d ON d.id=r.discrepancy_id WHERE d.cycle_id=?",
            (cycle_id,))}
        for issue in issues:
            issue["resolution"] = resolutions.get(issue["id"])
    active_issues = [issue for issue in issues if issue["resolved_at"] is None]
    anchor = conn.execute("SELECT coverage,import_id,legacy_total FROM smart_bin_migration_anchors "
                          "WHERE cycle_id=?", (cycle_id,)).fetchone()
    fingerprint = None
    if anchor is not None:
        fingerprint = conn.execute("SELECT source_fingerprint FROM smart_bin_migration_imports "
                                   "WHERE id=?", (anchor[1],)).fetchone()[0]
    revision = conn.execute("SELECT state_revision FROM smart_bin_machines WHERE machine_id=?",
                            (cycle[0],)).fetchone()[0]
    result = {"cycle_id": cycle_id, "machine_id": cycle[0],
              "total": sum(item["total"] for item in breakdown), "breakdown": breakdown,
              "coverage": anchor[0] if anchor is not None else "native_only",
              "legacy_total": anchor[2] if anchor is not None else None,
              "source_revision": {"machine": revision, "legacy_fingerprint": fingerprint},
              "discrepancies": issues,
              "active_discrepancies": active_issues,
              "blocked": bool(active_issues) or (anchor is not None and anchor[0] == "blocked")}
    return result


def read_recorded_contents(cycle_id: str, db_path: Path | None = None) -> dict[str, Any]:
    """Canonical inactive projection for later P2B/P2E; no legacy API cutover."""
    path = Path(db_path) if db_path is not None else local_state_db_path()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5.0)
    try:
        conn.execute("BEGIN")
        result = read_recorded_contents_on_connection(conn, cycle_id)
        conn.rollback()
        return result
    finally:
        conn.close()
