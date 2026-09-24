"""Durable, single-use checkpoint for an occupied, planned C4 restart.

This is not a crash-replay log: an interrupted recovery remains blocked. Old
motor acknowledgements, tracker IDs and chute timers are never restored.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from dataclasses import fields
from pathlib import Path

from defs.events import KnownObjectData
from defs.known_object import (
    ClassificationAttempt, ClassificationAttemptStrategy, ClassificationStatus,
    KnownObject, PieceStage, RecognitionImage,
)
from local_state import local_state_db_path


class RecoveryError(RuntimeError):
    pass


def checkpoint_path() -> Path:
    return local_state_db_path().with_name("c4_occupied_recovery.sqlite3")


def configuration_fingerprint(gc) -> str:
    from toml_config import _read_toml
    keys = ("bin_categories", "not_in_inventory_bins", "channel_polygons",
            "classification_polygons", "bin_layout", "servo_channel_calibration",
            "sorting_profile_sync")
    with sqlite3.connect(local_state_db_path().resolve().as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute("SELECT key,json_value FROM state_entries ORDER BY key").fetchall()
    profile = hashlib.sha256(Path(gc.sorting_profile_path).read_bytes()).hexdigest()
    return fingerprint({"machine": _read_toml(), "profile": profile,
                        "state": {key: json.loads(value) for key, value in rows if key in keys}})


def verify_unrecorded(plan: dict) -> None:
    """Reject any owner already present in a durable delivery ledger.

    Both ledgers are required and queried from one read-only SQLite snapshot.
    A missing table, unreadable database, or schema mismatch is uncertainty,
    not evidence that an owner is safe to replay.
    """
    try:
        database_uri = local_state_db_path().resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(database_uri, uri=True, timeout=5.0) as db:
            db.execute("BEGIN")
            for entry in plan["owners"]:
                piece_uuid = entry["object"]["uuid"]
                if db.execute(
                    "SELECT 1 FROM piece_records WHERE uuid=? LIMIT 1",
                    (piece_uuid,),
                ).fetchone():
                    raise RecoveryError(
                        "Checkpoint owner already has a durable piece record"
                    )
                if db.execute(
                    "SELECT 1 FROM piece_events WHERE piece_uuid=? LIMIT 1",
                    (piece_uuid,),
                ).fetchone():
                    raise RecoveryError("Checkpoint owner already has a durable bin event")
    except sqlite3.Error as exc:
        raise RecoveryError(
            "Durable delivery ledgers could not be read; recovery is blocked"
        ) from exc


def verify_delivery_records(plan: dict) -> None:
    """Require each delivered owner in at least one durable delivery ledger."""
    try:
        database_uri = local_state_db_path().resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(database_uri, uri=True, timeout=5.0) as db:
            db.execute("BEGIN")
            for entry in plan["owners"]:
                piece_uuid = entry["object"]["uuid"]
                piece_record = db.execute(
                    "SELECT 1 FROM piece_records WHERE uuid=? LIMIT 1",
                    (piece_uuid,),
                ).fetchone()
                piece_event = db.execute(
                    "SELECT 1 FROM piece_events WHERE piece_uuid=? LIMIT 1",
                    (piece_uuid,),
                ).fetchone()
                if piece_record is None and piece_event is None:
                    raise RecoveryError(
                        "Delivered checkpoint owner is missing from durable delivery ledgers"
                    )
    except sqlite3.Error as exc:
        raise RecoveryError(
            "Durable delivery ledgers could not be read; recovery is blocked"
        ) from exc


def pending_checkpoint() -> dict | None:
    checkpoint = CheckpointStore().read()
    return checkpoint if checkpoint is not None and checkpoint["phase"] != "complete" else None


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def decode_owner(payload: dict) -> KnownObject:
    data = KnownObjectData.model_validate(payload).model_dump(mode="json")
    allowed = {field.name for field in fields(KnownObject)}
    values = {key: value for key, value in data.items() if key in allowed}
    values["stage"] = PieceStage(data["stage"])
    values["classification_status"] = ClassificationStatus(data["classification_status"])
    for key in ("recognition_image_set", "link_match_image_set"):
        if key in values:
            values[key] = [RecognitionImage(**image) for image in values[key]]
    if "classification_attempts" in values:
        values["classification_attempts"] = [
            ClassificationAttempt(**{**attempt, "strategy": ClassificationAttemptStrategy(attempt["strategy"])})
            for attempt in values["classification_attempts"]
        ]
    if values.get("destination_bin") is not None:
        values["destination_bin"] = tuple(values["destination_bin"])
    obj = KnownObject(**values)
    if obj.stage == PieceStage.distributed or obj.distributed_at is not None:
        raise RecoveryError("Delivered objects cannot be restored onto C4")
    return obj


def validate_plan(plan: dict) -> None:
    if plan.get("version") != 1 or not isinstance(plan.get("operation_id"), str):
        raise RecoveryError("Unsupported recovery checkpoint")
    if plan.get("provenance") not in {"paused_controller", "operator_confirmed_legacy"}:
        raise RecoveryError("Recovery identity evidence is missing")
    if not isinstance(plan.get("confirmation"), str) or not plan["confirmation"].strip():
        raise RecoveryError("Checkpoint must record its identity evidence")
    if not isinstance(plan.get("config_fingerprint"), str) or len(plan["config_fingerprint"]) != 64:
        raise RecoveryError("Missing configuration fingerprint")
    size = plan.get("frame_size", [])
    if len(size) != 2 or any(type(value) is not int or value <= 0 for value in size):
        raise RecoveryError("Missing source frame dimensions")
    owners = plan.get("owners")
    if not isinstance(owners, list) or not 1 <= len(owners) <= 10:
        raise RecoveryError("Checkpoint must contain every occupied C4 owner")
    ids = set()
    boxes = []
    for entry in owners:
        obj = decode_owner(entry["object"])
        if obj.uuid in ids:
            raise RecoveryError("Duplicate physical owner")
        ids.add(obj.uuid)
        box = entry.get("bbox", [])
        if len(box) != 4 or not all(isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v) for v in box):
            raise RecoveryError("Missing owner geometry")
        x1, y1, x2, y2 = box
        if not (0 <= x1 < x2 <= size[0] and 0 <= y1 < y2 <= size[1]):
            raise RecoveryError("Owner geometry is outside the source frame")
        for a, b, c, d in boxes:
            if max(a, x1) < min(c, x2) and max(b, y1) < min(d, y2):
                raise RecoveryError("Overlapping owner anchors are ambiguous")
        boxes.append(box)
        if entry.get("disposition") not in {"recognized", "reject"}:
            raise RecoveryError("Missing per-piece disposition")
        if entry["disposition"] == "recognized":
            if obj.classification_status != ClassificationStatus.classified or not obj.part_id:
                raise RecoveryError("Recognized owner has no accepted recognition")
            if obj.destination_bin is not None and (len(obj.destination_bin) != 3 or any(type(v) is not int or v < 0 for v in obj.destination_bin)):
                raise RecoveryError("Invalid retained destination")
        elif obj.part_id is not None or obj.destination_bin is not None:
            raise RecoveryError("Reject owner cannot inherit a recognized route")


class CheckpointStore:
    def __init__(self, path: Path | None = None):
        self.path = (path or checkpoint_path()).resolve()

    def read(self) -> dict | None:
        if not self.path.exists():
            return None
        with sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True) as db:
            row = db.execute("SELECT payload, digest, phase FROM checkpoint WHERE singleton=1").fetchone()
        if row is None:
            raise RecoveryError("Recovery store is empty or incomplete")
        payload, digest, phase = row
        # A completed reject sweep supersedes the saved owner binding. Keep the
        # original payload as evidence, but do not let a now-irrelevant damaged
        # owner payload continue to block normal operation.
        if phase == "complete":
            return {"plan": None, "phase": phase}
        if hashlib.sha256(payload.encode()).hexdigest() != digest:
            raise RecoveryError("Recovery checkpoint checksum mismatch")
        plan = json.loads(payload)
        validate_plan(plan)
        return {"plan": plan, "phase": phase}

    def prepare(self, plan: dict) -> None:
        validate_plan(plan)
        payload = _json(plan)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS checkpoint (singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL, digest TEXT NOT NULL, phase TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY, operation_id TEXT NOT NULL, at REAL NOT NULL, phase TEXT NOT NULL, detail TEXT NOT NULL)")
            row = db.execute("SELECT phase FROM checkpoint WHERE singleton=1").fetchone()
            if row is not None and row[0] != "complete":
                raise RecoveryError("An occupied checkpoint already owns the machine")
            db.execute("INSERT OR REPLACE INTO checkpoint VALUES (1, ?, ?, 'prepared')",
                       (payload, hashlib.sha256(payload.encode()).hexdigest()))
            db.execute("INSERT INTO events(operation_id,at,phase,detail) VALUES (?,?,?,?)",
                       (plan["operation_id"], time.time(), "prepared", payload))

    def transition(self, operation_id: str, expected: str, phase: str, detail: dict) -> None:
        if (expected, phase) not in {("prepared", "running"), ("running", "complete"), ("running", "failed")}:
            raise RecoveryError("Invalid recovery transition")
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload,phase,digest FROM checkpoint WHERE singleton=1").fetchone()
            if row is None or row[1] != expected or json.loads(row[0])["operation_id"] != operation_id:
                raise RecoveryError("Recovery is stale, consumed, or belongs to another operation")
            if hashlib.sha256(row[0].encode()).hexdigest() != row[2]:
                raise RecoveryError("Recovery checkpoint checksum mismatch")
            db.execute("UPDATE checkpoint SET phase=? WHERE singleton=1", (phase,))
            db.execute("INSERT INTO events(operation_id,at,phase,detail) VALUES (?,?,?,?)",
                       (operation_id, time.time(), phase, _json(detail)))

    def complete_by_reject_drain(self, detail: dict | None = None) -> bool:
        """Retire an unusable owner binding only after a verified full C4 reject sweep.

        The owner payload and digest stay untouched as historical evidence. The
        completion event records the prior phase and whether the stored payload
        still passes its checksum.
        """
        if not self.path.exists():
            return False
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT payload, digest, phase FROM checkpoint WHERE singleton=1"
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc).lower():
                    return False
                raise
            if row is None:
                return False
            payload, digest, phase = row
            if phase == "complete":
                return False
            try:
                decoded = json.loads(payload)
                operation_id = str(decoded.get("operation_id") or "")
            except Exception:
                operation_id = ""
            if not operation_id:
                operation_id = f"c4-reject-drain-{uuid.uuid4()}"
            event = {
                "resolution": "complete_c4_reject_drain",
                "prior_phase": str(phase),
                "checkpoint_digest_valid": hashlib.sha256(payload.encode()).hexdigest() == digest,
                **(detail or {}),
            }
            db.execute("UPDATE checkpoint SET phase='complete' WHERE singleton=1")
            db.execute(
                "INSERT INTO events(operation_id,at,phase,detail) VALUES (?,?,?,?)",
                (operation_id, time.time(), "complete_rejected", _json(event)),
            )
            return True


def match_owners(plan: dict, state, frame_size: tuple[int, int], now: float):
    """Unique stationary spatial binding, never a cross-generation numeric ID."""
    validate_plan(plan)
    if tuple(plan["frame_size"]) != tuple(frame_size):
        raise RecoveryError("Recovery camera dimensions changed")
    if not math.isfinite(state.ts) or not 0 <= now - state.ts <= 1.0 or not state.tracker_generation:
        raise RecoveryError("Recovery requires fresh tracked perception")
    if state.channel_center is None or len(state.channel_center) != 2 or not all(math.isfinite(v) for v in state.channel_center):
        raise RecoveryError("Recovery requires calibrated channel geometry")
    if state.n_pieces != len(state.pieces) or len(state.pieces) != len(plan["owners"]):
        raise RecoveryError("C4 occupancy differs from checkpoint")
    result = []
    seen = set()
    for entry in plan["owners"]:
        x1, y1, x2, y2 = entry["bbox"]
        matches = []
        for observation in state.pieces:
            a, b, c, d = observation.bbox
            if not all(math.isfinite(v) for v in (a, b, c, d)) or a >= c or b >= d:
                raise RecoveryError("Invalid fresh owner geometry")
            intersection = max(0, min(c, x2) - max(a, x1)) * max(0, min(d, y2) - max(b, y1))
            union = (c-a)*(d-b) + (x2-x1)*(y2-y1) - intersection
            displacement = math.hypot((a+c-x1-x2)/2, (b+d-y1-y2)/2)
            if intersection / union >= 0.7 and displacement <= max(5.0, min(x2-x1, y2-y1)*0.1):
                matches.append(observation)
        if len(matches) != 1 or type(matches[0].sv_bt_track_id) is not int or matches[0].sv_bt_track_id in seen:
            raise RecoveryError("Owner cannot be uniquely bound to its stationary position")
        seen.add(matches[0].sv_bt_track_id)
        result.append((entry, matches[0]))
    return result
