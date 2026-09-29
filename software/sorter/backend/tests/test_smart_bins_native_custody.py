"""Explicit native custody persistence; fake identities and isolated SQLite only."""

from dataclasses import replace
from contextlib import contextmanager
import sqlite3

import pytest

import smart_bins_native_custody as native
import smart_bins_native_completion as completion
import smart_bins_service as service
from subsystems.classification_channel.smart_bins_native_adapter import (
    NativeCustodyAdapter,
)
from test_smart_bins_reservations import (
    prepared as prepared,
    claim,
    request,
    qualification,
    connect,
)


@pytest.fixture(autouse=True)
def reset_fence():
    native.install(None)
    yield
    native.install(None)


@pytest.fixture
def bound(prepared):
    native.initialize_native_schema("m", "p", "sorter", 0)
    completion.initialize_schema()
    result = claim(request(custody=False), qualification(), "native-reserve")
    identity = native.NativeIdentity(
        "m", "one", result["reservation_id"], "incarnation", 7, 0, "attempt"
    )
    digest = service.configuration_digest()
    native.bind_reservation(identity, digest)
    return prepared, identity, digest


def test_absent_does_not_create_file(tmp_path, monkeypatch):
    path = tmp_path / "missing.sqlite"
    monkeypatch.setenv("LOCAL_STATE_DB_PATH", str(path))
    assert native.startup_state("m") == {"active": False, "blocked": False}
    assert not path.exists()


def test_ordinary_initialized_without_smart_schemas(prepared):
    # RB02 alone is dormant when there are no claims; no native schema appears.
    assert native.startup_state("m") == {"active": False, "blocked": False}
    with connect(prepared) as conn:
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='smart_bin_native_versions'"
        ).fetchone()


def test_missing_extension_with_held_claim_fails_closed(prepared):
    claim(request(custody=False), qualification(), "held")
    adapter = NativeCustodyAdapter("m")
    with pytest.raises(native.CustodyRefused):
        adapter.require_startup()
    assert not adapter.active


def test_explicit_initialization_is_idempotent(prepared):
    assert native.initialize_native_schema("m", "p", "sorter", 0) == 1
    assert native.initialize_native_schema("m", "p", "sorter", 0) == 1
    assert native.startup_state("m")["active"]
    assert not native.startup_state("m")["blocked"]


def test_intent_and_consume_are_single_use(bound):
    path, identity, digest = bound
    native.arm(identity, digest, {"receiving_evidence": False})
    with connect(path) as conn:
        assert (
            conn.execute("SELECT state FROM smart_bin_reservations").fetchone()[0]
            == "RELEASE_INTENT"
        )
        assert (
            conn.execute("SELECT count(*) FROM smart_bin_release_attempts").fetchone()[
                0
            ]
            == 1
        )
    native.consume(identity, digest)
    with pytest.raises(native.CustodyRefused):
        native.consume(identity, digest)
    with pytest.raises(native.CustodyRefused):
        native.arm(identity, digest, {})


@pytest.mark.parametrize(
    "field,value",
    [
        ("piece_uuid", "other"),
        ("incarnation", "restart"),
        ("head_generation", 8),
        ("route_revision", 1),
        ("attempt_id", "other-attempt"),
    ],
)
def test_stale_permit_refused(bound, field, value):
    _, identity, digest = bound
    native.arm(identity, digest, {})
    with pytest.raises(native.CustodyRefused):
        native.consume(replace(identity, **{field: value}), digest)


def test_restart_does_not_recreate_permit(bound):
    _, identity, digest = bound
    native.arm(identity, digest, {})
    restarted = NativeCustodyAdapter("m")
    assert restarted.permit is None
    with pytest.raises(native.CustodyRefused):
        restarted.require_startup()


def test_failed_commit_rolls_back_intent(bound, monkeypatch):
    path, identity, digest = bound
    real = native.critical_transaction

    class FailCommit:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, key):
            return getattr(self.conn, key)

        def commit(self):
            raise sqlite3.OperationalError("injected commit failure")

    @contextmanager
    def failure():
        with real() as conn:
            yield FailCommit(conn)

    monkeypatch.setattr(native, "critical_transaction", failure)
    with pytest.raises(sqlite3.OperationalError):
        native.arm(identity, digest, {})
    with connect(path) as conn:
        assert (
            conn.execute("SELECT state FROM smart_bin_reservations").fetchone()[0]
            == "RESERVED"
        )
        assert (
            conn.execute("SELECT count(*) FROM smart_bin_release_attempts").fetchone()[
                0
            ]
            == 0
        )
        assert (
            conn.execute("SELECT count(*) FROM smart_bin_native_evidence").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "outcome,state",
    [
        ("ACCEPTED", "RELEASE_INTENT"),
        ("REJECTED", "RELEASE_INTENT"),
        ("AMBIGUOUS", "UNCERTAIN"),
    ],
)
def test_outcomes_never_create_receiving_credit(bound, outcome, state):
    path, identity, digest = bound
    native.arm(identity, digest, {})
    native.consume(identity, digest)
    native.observe(
        identity,
        "MOTOR_" + outcome,
        {"encoder_present": False},
        uncertain=outcome == "AMBIGUOUS",
    )
    with connect(path) as conn:
        assert (
            conn.execute("SELECT state FROM smart_bin_reservations").fetchone()[0]
            == state
        )
        assert (
            conn.execute("SELECT count(*) FROM smart_bin_deliveries").fetchone()[0] == 0
        )
        assert (
            conn.execute("SELECT pocket_index FROM smart_bin_reservations").fetchone()[
                0
            ]
            is None
        )
    assert native.startup_state("m")["blocked"]


def test_inferred_exit_requires_owned_accepted_command(bound):
    path, identity, digest = bound
    native.arm(identity, digest, {})
    with pytest.raises(native.CustodyRefused):
        native.observe(identity, "INFERRED_EXIT", {})
    native.consume(identity, digest)
    native.observe(identity, "MOTOR_ACCEPTED", {})
    native.observe(identity, "INFERRED_EXIT", {"receiving_evidence": False})
    with connect(path) as conn:
        assert (
            conn.execute("SELECT state FROM smart_bin_reservations").fetchone()[0]
            == "RELEASE_INTENT"
        )
        assert (
            conn.execute("SELECT count(*) FROM smart_bin_deliveries").fetchone()[0] == 0
        )


def test_timeout_retains_capacity_and_blocks_all_motion(bound):
    _, identity, digest = bound
    native.arm(identity, digest, {})
    native.consume(identity, digest)
    native.observe(identity, "MOTOR_ACCEPTED", {})
    native.observe(identity, "EJECT_TIMEOUT_VISIBLE", {}, uncertain=True)
    adapter = NativeCustodyAdapter("m")
    native.install(adapter)
    for kind in ("finite", "route", "manual", "home", "admission"):
        with pytest.raises(native.CustodyRefused):
            with native.motion_entry(kind, "carousel"):
                pytest.fail("hardware must not be called")
    with native.motion_entry("finite", "c_channel_1_rotor"):
        pass
    with native.motion_entry("finite", "c_channel_2_rotor"):
        pass


def test_no_db_writer_during_owned_hardware_scope(bound):
    path, identity, digest = bound
    native.arm(identity, digest, {})
    native.consume(identity, digest)
    with native.owned_scope({"kind": "dispatch"}):
        other = sqlite3.connect(path, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
        finally:
            other.close()


def test_unknown_material_hold_does_not_fabricate_quantity(prepared):
    native.initialize_native_schema("m", "p", "sorter", 0)
    completion.initialize_schema()
    adapter = NativeCustodyAdapter("m")
    with pytest.raises(native.CustodyRefused):
        adapter.refuse_unqualified_motion("UNKNOWN_MATERIAL", {"quantity": None})
    with connect(prepared) as conn:
        assert (
            conn.execute("SELECT count(*) FROM smart_bin_reservations").fetchone()[0]
            == 0
        )
        assert (
            "null"
            in conn.execute(
                "SELECT details_json FROM smart_bin_native_holds"
            ).fetchone()[0]
        )


def test_native_evidence_and_consumed_state_cannot_be_rewritten(bound):
    path, identity, digest = bound
    native.arm(identity, digest, {})
    native.consume(identity, digest)
    with connect(path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE smart_bin_native_evidence SET kind='INFERRED_EXIT'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE smart_bin_native_claims SET status='RELEASE_INTENT'")


def test_partial_native_schema_fails_closed(bound):
    path, _, _ = bound
    with connect(path) as conn:
        conn.execute("DROP TRIGGER smart_bin_native_status_forward")
    with pytest.raises(native.CustodyRefused):
        native.startup_state("m")


def test_hold_is_not_rewritten_every_motion_tick(prepared):
    native.initialize_native_schema("m", "p", "sorter", 0)
    completion.initialize_schema()
    adapter = NativeCustodyAdapter("m")
    for _ in range(3):
        with pytest.raises(native.CustodyRefused):
            adapter.refuse_unqualified_motion("UNKNOWN_MATERIAL", {"quantity": None})
    with connect(prepared) as conn:
        assert (
            conn.execute("SELECT count(*) FROM smart_bin_native_holds").fetchone()[0]
            == 1
        )
