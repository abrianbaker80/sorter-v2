"""Opt-in Harvest receipts. No initialization, local-state access or runtime calls.

Helpers run on the Harvest caller's transaction only. Allocation business logic
stays in HarvestProjectStore. Existing, untagged allocations cannot be adopted.
"""

from __future__ import annotations

import hashlib
import json


SCHEMA_VERSION = 1
DDL = (
    "CREATE TABLE harvest_integration_versions (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
    "version INTEGER NOT NULL)",
    "CREATE TABLE harvest_integration_requests (operation_id TEXT PRIMARY KEY NOT NULL, "
    "request_key TEXT UNIQUE NOT NULL, payload_hash TEXT NOT NULL, request_json TEXT NOT NULL, "
    "allocation_id TEXT NOT NULL REFERENCES harvest_allocations(allocation_id), allocation_json TEXT NOT NULL, destination_json TEXT, "
    "acknowledgement_json TEXT NOT NULL, "
    "created_at TEXT NOT NULL)",
    "CREATE TRIGGER harvest_integration_requests_update BEFORE UPDATE ON harvest_integration_requests "
    "BEGIN SELECT RAISE(ABORT,'Harvest request is immutable'); END",
    "CREATE TRIGGER harvest_integration_requests_delete BEFORE DELETE ON harvest_integration_requests "
    "BEGIN SELECT RAISE(ABORT,'Harvest request is immutable'); END",
)
REQUIRED = {ddl.split()[2] for ddl in DDL}


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def check_schema(conn):
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'harvest_integration_%'"
        )
    }
    if names != REQUIRED or [
        tuple(r)
        for r in conn.execute(
            "SELECT singleton,version FROM harvest_integration_versions"
        )
    ] != [(1, SCHEMA_VERSION)]:
        raise RuntimeError(
            "Harvest integration schema absent or unsupported; explicit initialization required"
        )


def is_integrated(conn, allocation_id):
    """Legacy retirement cannot infer physical safety for an integrated claim."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name LIKE 'harvest_integration_%'"
    ).fetchone():
        return False
    check_schema(conn)
    return (
        conn.execute(
            "SELECT 1 FROM harvest_integration_requests WHERE allocation_id=?",
            (allocation_id,),
        ).fetchone()
        is not None
    )


def check_request(conn, request, *, kind, project_id, activation_id, arguments):
    """Return exact replay before revision checks, or None for a new operation."""
    from project_harvest_projects import HarvestProjectError

    check_schema(conn)
    required = {
        "operation_id",
        "request_key",
        "machine_id",
        "reservation_id",
        "piece_uuid",
        "project_id",
        "activation_id",
        "allocation_id",
        "delivery_id",
        "parent_operation_id",
        "operation_kind",
        "expected_reservation_revision",
        "expected_machine_revision",
        "expected_project_revision",
        "request_payload",
    }
    if (
        not isinstance(request, dict)
        or set(request) != required
        or request["operation_kind"] != kind
        or request["project_id"] != project_id
        or request["activation_id"] != activation_id
        or canonical(request["request_payload"]) != canonical(arguments)
        or request["piece_uuid"] != arguments["piece_id"]
        or any(
            type(request[k]) is not int or request[k] < 0
            for k in (
                "expected_reservation_revision",
                "expected_machine_revision",
                "expected_project_revision",
            )
        )
        or not all(
            isinstance(request[k], str) and request[k].strip()
            for k in (
                "operation_id",
                "request_key",
                "machine_id",
                "reservation_id",
                "project_id",
            )
        )
    ):
        raise HarvestProjectError(
            "INTEGRATION_IDENTITY_MISMATCH",
            "Frozen request differs from Harvest arguments.",
        )
    key = "harvest:" + kind + ":" + request["request_key"]
    existing = conn.execute(
        "SELECT * FROM harvest_integration_requests WHERE request_key=? OR operation_id=?",
        (key, request["operation_id"]),
    ).fetchall()
    if existing:
        if len(existing) != 1 or existing[0]["payload_hash"] != digest(request):
            raise HarvestProjectError(
                "IDEMPOTENCY_CONFLICT",
                "Harvest request key or operation ID was reused.",
            )
        return dict(existing[0])
    row = conn.execute(
        "SELECT revision FROM harvest_projects WHERE project_id=?", (project_id,)
    ).fetchone()
    if row is None or row[0] != request["expected_project_revision"]:
        raise HarvestProjectError(
            "STALE_PROJECT_REVISION", "Harvest project revision changed."
        )
    return None


def save_request(conn, request, allocation_id, created_at):
    from project_harvest_projects import HarvestProjectStore

    row = conn.execute(
        "SELECT * FROM harvest_allocations WHERE allocation_id=?", (allocation_id,)
    ).fetchone()
    destination = None
    acknowledgement = HarvestProjectStore._allocation_dict(row)
    if request["operation_kind"] == "ALLOCATE" and row["runtime_id"] is not None:
        runtime = conn.execute(
            "SELECT activation_json FROM harvest_runtime_state WHERE project_id=? AND activation_id=?",
            (row["project_id"], row["runtime_id"]),
        ).fetchone()
        if runtime is None:
            raise RuntimeError("matching active Harvest runtime required")
        matches = [
            a
            for a in json.loads(runtime[0])["assignments"]
            if a["group_id"] == row["group_id"]
        ]
        if len(matches) != 1:
            raise RuntimeError("unique Harvest destination required")
        destination = canonical(matches[0])
        from project_harvest_projects import HARVEST_EXCEPTION_GROUP_ID

        acknowledgement.update(
            destination=matches[0],
            activation_id=row["runtime_id"],
            exception=row["group_id"] == HARVEST_EXCEPTION_GROUP_ID,
        )
    conn.execute(
        "INSERT INTO harvest_integration_requests VALUES(?,?,?,?,?,?,?,?,?)",
        (
            request["operation_id"],
            "harvest:" + request["operation_kind"] + ":" + request["request_key"],
            digest(request),
            canonical(request),
            allocation_id,
            canonical(HarvestProjectStore._allocation_dict(row)),
            destination,
            canonical(acknowledgement),
            created_at,
        ),
    )
