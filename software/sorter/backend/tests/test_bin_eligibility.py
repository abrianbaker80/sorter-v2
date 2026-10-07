"""Focused selection regressions; no controller, provider, or hardware calls."""

import queue
import time
import uuid
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

import local_state
from defs.known_object import KnownObject, PieceStage
from server import shared_state
from sorting_profile import MISC_CATEGORY
from subsystems.distribution.chute import BinAddress
from subsystems.distribution.positioning import Positioning
from subsystems.distribution.states import DistributionState


def _layer(*bins, limit=2, dimension=50, enabled=True, section_enabled=True):
    return NS(
        enabled=enabled,
        max_pieces_per_bin=limit,
        max_dimension_mm=dimension,
        sections=[NS(enabled=section_enabled, bins=list(bins))],
    )


def _bin(*categories, pool=False):
    return NS(category_ids=list(categories), not_in_inventory=pool)


@pytest.fixture
def selection(monkeypatch):
    saved = []
    monkeypatch.setattr("subsystems.distribution.positioning.setBinCategories", lambda categories: saved.append(categories))
    monkeypatch.setattr(local_state, "get_current_bin_occupancy_evidence", lambda: {})
    gc = NS(logger=Mock(), runtime_stats=Mock(), disable_servos=True)
    chute = NS(isBinReachable=Mock(return_value=True))
    state = Positioning(NS(), gc, NS(), chute, NS(layers=[]), Mock(), queue.Queue())
    return state, saved


def _select(state, category="A", dimension=20, pool=False):
    return state._findOrAssignBinForCategory(
        category, not_in_inventory=pool, piece=NS(max_dimension_mm=dimension)
    )


def test_full_matching_unassigned_and_shared_are_skipped(selection, monkeypatch):
    state, saved = selection
    state.layout.layers = [_layer(_bin("A"), _bin(), _bin("B"), _bin(), limit=1)]
    monkeypatch.setattr(local_state, "get_current_bin_occupancy_evidence", lambda: {
        (0, 0, 0): (1, {"A": 1}), (0, 0, 1): (1, {"A": 1}),
        (0, 0, 2): (1, {"B": 1}),
    })
    monkeypatch.setattr("subsystems.distribution.positioning._allowMultiCategoryBins", lambda: True)
    assert _select(state) == (BinAddress(0, 0, 3), True)
    assert state.layout.layers[0].sections[0].bins[1].category_ids == []
    assert len(saved) == 1


@pytest.mark.parametrize("categories,share", [("A", False), (None, False), ("B", True)])
def test_each_full_candidate_type_is_ineligible(selection, monkeypatch, categories, share):
    state, saved = selection
    state.layout.layers = [_layer(_bin(*([categories] if categories else [])), limit=1)]
    monkeypatch.setattr(local_state, "get_current_bin_occupancy_evidence", lambda: {
        (0, 0, 0): (1, {(categories or "A"): 1})
    })
    monkeypatch.setattr("subsystems.distribution.positioning._allowMultiCategoryBins", lambda: share)
    assert _select(state) == (None, False)
    assert saved == []


def test_unlimited_limit_keeps_compatible_occupied_matching_bin(selection, monkeypatch):
    state, saved = selection
    state.layout.layers = [_layer(_bin("A"), limit=None)]
    monkeypatch.setattr(local_state, "get_current_bin_occupancy_evidence", lambda: {(0, 0, 0): (1000, {"A": 1000})})
    assert _select(state) == (BinAddress(0, 0, 0), False)
    assert saved == []


@pytest.mark.parametrize("evidence,allowed", [
    ((2, {"A": 2}), True),
    ((1, {"B": 1}), False),
    ((2, {}), False),
    ((0, {"A": 1}), False),
    ((1, {None: 1}), False),
])
def test_unlabeled_occupied_requires_complete_compatible_evidence(selection, monkeypatch, evidence, allowed):
    state, saved = selection
    state.layout.layers = [_layer(_bin(), limit=None)]
    monkeypatch.setattr(local_state, "get_current_bin_occupancy_evidence", lambda: {(0, 0, 0): evidence})
    assert _select(state) == ((BinAddress(0, 0, 0), True) if allowed else (None, False))
    assert state.layout.layers[0].sections[0].bins[0].category_ids == (["A"] if allowed else [])
    assert len(saved) == int(allowed)


@pytest.mark.parametrize("pool,normal_sharing,allowed", [
    (False, False, False),
    (False, True, True),
    (True, False, True),
])
def test_unlabeled_other_category_uses_existing_sharing_policy(
    selection, monkeypatch, pool, normal_sharing, allowed
):
    state, saved = selection
    b = _bin(pool=pool)
    state.layout.layers = [_layer(b)]
    evidence = {(0, 0, 0): (1, {"B": 1})}
    before = {(0, 0, 0): (1, {"B": 1})}
    monkeypatch.setattr(local_state, "get_current_bin_occupancy_evidence", lambda: evidence)
    monkeypatch.setattr(
        "subsystems.distribution.positioning._allowMultiCategoryBins",
        lambda: normal_sharing,
    )

    expected = (BinAddress(0, 0, 0), True) if allowed else (None, False)
    assert _select(state, pool=pool) == expected
    assert b.category_ids == (["B", "A"] if allowed else [])
    assert len(saved) == int(allowed)
    if allowed:
        assert saved[0][0][0][0] == ["B", "A"]
    assert evidence == before


def test_genuinely_empty_bin_precedes_unlabeled_occupied_sharing(selection, monkeypatch):
    state, saved = selection
    occupied, empty = _bin(), _bin()
    state.layout.layers = [_layer(occupied, empty)]
    evidence = {(0, 0, 0): (1, {"B": 1})}
    monkeypatch.setattr(local_state, "get_current_bin_occupancy_evidence", lambda: evidence)
    monkeypatch.setattr("subsystems.distribution.positioning._allowMultiCategoryBins", lambda: True)

    assert _select(state) == (BinAddress(0, 0, 1), True)
    assert occupied.category_ids == []
    assert empty.category_ids == ["A"]
    assert saved == [[[[[], ["A"]]]]]
    assert evidence == {(0, 0, 0): (1, {"B": 1})}


@pytest.mark.parametrize("pool", [False, True])
def test_recorded_misc_never_restores_physical_assignment(selection, monkeypatch, pool):
    state, saved = selection
    b = _bin(pool=pool)
    state.layout.layers = [_layer(b, limit=3)]
    evidence = {(0, 0, 0): (2, {"A": 1, MISC_CATEGORY: 1})}
    before = {(0, 0, 0): (2, {"A": 1, MISC_CATEGORY: 1})}
    monkeypatch.setattr(local_state, "get_current_bin_occupancy_evidence", lambda: evidence)
    monkeypatch.setattr("subsystems.distribution.positioning._allowMultiCategoryBins", lambda: True)

    assert _select(state, pool=pool) == (None, False)
    assert b.category_ids == []
    assert saved == []
    assert evidence == before


def test_no_fit_then_later_fit_and_no_fit_metadata_without_assignment(selection):
    state, saved = selection
    state.layout.layers = [_layer(_bin(), dimension=40), _layer(_bin(), dimension=80)]
    assert _select(state, dimension=60) == (BinAddress(1, 0, 0), True)
    assert state._no_fit_layer_index is None
    assert state.layout.layers[0].sections[0].bins[0].category_ids == []
    assert len(saved) == 1
    state.layout.layers[1].sections[0].bins[0].category_ids = []
    state.layout.layers[1].max_dimension_mm = 50
    saved.clear()
    assert _select(state, dimension=60) == (None, False)
    assert state._no_fit_layer_index == 0
    assert all(not layer.sections[0].bins[0].category_ids for layer in state.layout.layers)
    assert saved == []


def test_matching_preference_searches_later_fitting_match(selection):
    state, saved = selection
    state.layout.layers = [
        _layer(_bin("A"), dimension=40),
        _layer(_bin("A"), dimension=80),
        _layer(_bin(), dimension=80),
    ]
    assert _select(state, dimension=60) == (BinAddress(1, 0, 0), False)
    assert saved == []


@pytest.mark.parametrize("dimension", [50, None])
def test_exact_limit_and_unknown_dimension_fit(selection, dimension):
    state, _ = selection
    state.layout.layers = [_layer(_bin("A"), dimension=50)]
    assert _select(state, dimension=dimension) == (BinAddress(0, 0, 0), False)


def test_pools_sharing_and_unreachable_or_disabled_bins(selection, monkeypatch):
    state, saved = selection
    state.layout.layers = [
        _layer(_bin(), enabled=False),
        _layer(_bin(), section_enabled=False),
        _layer(_bin(), _bin("B", pool=True), _bin("C", pool=True)),
    ]
    state.chute.isBinReachable.side_effect = lambda address: address.bin_index != 0
    monkeypatch.setattr("subsystems.distribution.positioning._allowMultiCategoryBins", lambda: False)
    assert _select(state, pool=False) == (None, False)
    assert _select(state, pool=True) == (BinAddress(2, 0, 1), True)
    assert state.layout.layers[2].sections[0].bins[1].category_ids == ["B", "A"]
    assert len(saved) == 1


def test_normal_sharing_requires_policy_and_preserves_recorded_contents(selection, monkeypatch):
    state, saved = selection
    state.layout.layers = [_layer(_bin("B"), limit=None)]
    monkeypatch.setattr(local_state, "get_current_bin_occupancy_evidence", lambda: {(0, 0, 0): (1, {"B": 1})})
    monkeypatch.setattr("subsystems.distribution.positioning._allowMultiCategoryBins", lambda: False)
    assert _select(state) == (None, False)
    monkeypatch.setattr("subsystems.distribution.positioning._allowMultiCategoryBins", lambda: True)
    assert _select(state) == (BinAddress(0, 0, 0), True)
    assert state.layout.layers[0].sections[0].bins[0].category_ids == ["B", "A"]
    assert len(saved) == 1


def test_misc_and_read_or_write_failure_never_assign(selection, monkeypatch):
    state, saved = selection
    state.layout.layers = [_layer(_bin())]
    assert _select(state, category=MISC_CATEGORY) == (None, False)
    assert saved == []
    monkeypatch.setattr(local_state, "get_current_bin_occupancy_evidence", Mock(side_effect=OSError("read failed")))
    with pytest.raises(OSError, match="read failed"):
        _select(state)
    assert state.layout.layers[0].sections[0].bins[0].category_ids == []
    monkeypatch.setattr(local_state, "get_current_bin_occupancy_evidence", lambda: {})
    monkeypatch.setattr("subsystems.distribution.positioning.setBinCategories", Mock(side_effect=OSError("write failed")))
    with pytest.raises(OSError, match="write failed"):
        _select(state)
    assert state.layout.layers[0].sections[0].bins[0].category_ids == []


@pytest.mark.parametrize("harvest", [False, True])
def test_no_fit_passthrough_keeps_metadata_and_harvest_destination(selection, monkeypatch, harvest):
    state, saved = selection
    state.layout.layers = [_layer(_bin("A") if harvest else _bin(), dimension=40)]
    piece = KnownObject(part_id="3001", color_id="1")
    piece.max_dimension_mm = 60
    state.shared = NS(
        set_distribution_gate=Mock(),
        transport=NS(getPieceForDistributionPositioning=Mock(return_value=piece)),
    )
    state.sorting_profile.getCategoryIdForPart.return_value = "A"
    state.sorting_profile.highValueCategoryId.return_value = None
    state._reserveHarvestRoute = Mock(return_value=(BinAddress(0, 0, 0), "A") if harvest else None)
    state._clearBinsFullAlertIfOwned = Mock()
    state._clearChuteJamAlertIfOwned = Mock()
    state._openAllDoorsForPassthrough = Mock(return_value=True)
    state._finishPassthrough = Mock(return_value=DistributionState.READY)
    assert state.step() == DistributionState.READY
    assert piece.too_big_for_layer is True
    assert piece.intended_layer_index == 0
    assert piece.category_id == MISC_CATEGORY
    assert piece.destination_bin is None
    assert bool(piece.harvest_exception) is harvest
    assert saved == []


def _prepare_selection_step(state, monkeypatch):
    piece = KnownObject(part_id="3001", color_id="1")
    piece.max_dimension_mm = 20
    state.shared = NS(
        set_distribution_gate=Mock(),
        transport=NS(getPieceForDistributionPositioning=Mock(return_value=piece)),
        c4_runtime_owner=NS(physical_c4_authority=True),
    )
    state.sorting_profile.getCategoryIdForPart.return_value = "A"
    state.sorting_profile.highValueCategoryId.return_value = None
    state._reserveHarvestRoute = Mock(return_value=None)
    state._consumeNoBinPassthroughApproval = Mock(return_value=False)
    state._raiseNoBinAvailableIncident = Mock(return_value=True)
    state._raiseBinsFullAlert = Mock()
    state._clearBinsFullAlertIfOwned = Mock()
    state._clearChuteJamAlertIfOwned = Mock()
    monkeypatch.setattr("subsystems.distribution.positioning._allowMultiCategoryBins", lambda: False)
    monkeypatch.setattr("subsystems.distribution.positioning._incidentHandlingOff", lambda kind: False)
    monkeypatch.setattr(shared_state, "hardware_error", None)
    monkeypatch.setattr(shared_state, "command_queue", queue.Queue())
    state.gc.disable_servos = False
    state.gc.profiler = Mock()
    servos = []
    for _ in state.layout.layers:
        servo = NS(available=True, is_calibrated=True, stopped=True, opened=False)
        servo.isOpen = lambda servo=servo: servo.opened
        servo.isClosed = lambda servo=servo: not servo.opened
        servo.open = Mock(side_effect=lambda servo=servo: setattr(servo, "opened", True))
        servos.append(servo)
    state.irl.servos = servos
    return piece


@pytest.mark.parametrize("case", ["matching", "unassigned", "shared", "mixed_capacity"])
def test_uncertain_destination_uses_existing_reject_without_ack(selection, monkeypatch, case):
    state, saved = selection
    uncertain = _bin("B") if case == "shared" else _bin() if case == "unassigned" else _bin("A")
    state.layout.layers = [_layer(*([_bin("A"), uncertain] if case == "mixed_capacity" else [uncertain]), limit=3)]
    evidence = {(0, 0, 0): (1, {})}
    if case == "shared":
        evidence = {(0, 0, 0): (2, {"B": 1})}
    elif case == "mixed_capacity":
        evidence = {(0, 0, 0): (3, {"A": 3}), (0, 0, 1): (1, {})}
    before = {key: (count, dict(recorded)) for key, (count, recorded) in evidence.items()}
    assignments = [list(b.category_ids) for b in state.layout.layers[0].sections[0].bins]
    monkeypatch.setattr(local_state, "get_current_bin_occupancy_evidence", lambda: evidence)
    piece = _prepare_selection_step(state, monkeypatch)
    if case == "shared":
        monkeypatch.setattr("subsystems.distribution.positioning._allowMultiCategoryBins", lambda: True)

    assert state.step() == DistributionState.READY
    assert state._occupancy_state == "positioning.passthrough_uncertain_bins"
    assert piece.stage == PieceStage.distributing
    assert piece.destination_bin is None
    assert piece.part_id == "3001"
    assert piece.distribution_positioned_at is not None
    state._raiseNoBinAvailableIncident.assert_not_called()
    state._consumeNoBinPassthroughApproval.assert_not_called()
    state._raiseBinsFullAlert.assert_not_called()
    state._clearBinsFullAlertIfOwned.assert_called_once()
    state.gc.runtime_stats.setActiveIncident.assert_not_called()
    state.irl.servos[0].open.assert_called_once()
    assert [b.category_ids for b in state.layout.layers[0].sections[0].bins] == assignments
    assert evidence == before
    assert saved == []


@pytest.mark.parametrize("case", [
    "full", "wrong_pool", "disabled_layer", "disabled_section", "unreachable",
    "undersized", "no_share", "recorded_misc", "incompatible", "known_incompatible_uncertainty",
])
def test_uncertainty_does_not_override_other_exclusions(selection, monkeypatch, case):
    state, saved = selection
    b = _bin("B") if case == "no_share" else _bin("A", pool=case == "wrong_pool")
    state.layout.layers = [_layer(
        b, limit=1 if case == "full" else 3, dimension=10 if case == "undersized" else 50,
        enabled=case != "disabled_layer", section_enabled=case != "disabled_section",
    )]
    evidence = {(0, 0, 0): (1, {})}
    if case == "full":
        evidence = {(0, 0, 0): (1, {"A": 1})}
    elif case == "recorded_misc":
        evidence = {(0, 0, 0): (1, {MISC_CATEGORY: 1})}
    elif case == "incompatible":
        evidence = {(0, 0, 0): (1, {"B": 1})}
    elif case == "known_incompatible_uncertainty":
        evidence = {(0, 0, 0): (2, {"B": 1})}
    monkeypatch.setattr(local_state, "get_current_bin_occupancy_evidence", lambda: evidence)
    piece = _prepare_selection_step(state, monkeypatch)
    if case == "unreachable":
        state.chute.isBinReachable.return_value = False

    assert state.step() == DistributionState.IDLE
    assert state._uncertain_destination_blocked is False
    assert state._occupancy_state == "positioning.no_bin_incident"
    state._raiseNoBinAvailableIncident.assert_called_once_with(piece, "A")
    state.irl.servos[0].open.assert_not_called()
    assert piece.stage == PieceStage.created
    assert saved == []


def test_uncertainty_resets_and_available_destination_still_wins(selection, monkeypatch):
    state, saved = selection
    uncertain = _bin("A")
    state.layout.layers = [_layer(uncertain)]
    evidence = {(0, 0, 0): (1, {})}
    monkeypatch.setattr(local_state, "get_current_bin_occupancy_evidence", lambda: evidence)
    assert _select(state) == (None, False)
    assert state._uncertain_destination_blocked is True

    state.layout.layers[0].sections[0].bins.append(_bin("A"))
    assert _select(state) == (BinAddress(0, 0, 1), False)
    assert state._uncertain_destination_blocked is False
    state.layout.layers[0].sections[0].bins.pop()
    evidence[(0, 0, 0)] = (2, {"A": 2})
    assert _select(state) == (None, False)
    assert state._uncertain_destination_blocked is False
    assert uncertain.category_ids == ["A"]
    assert saved == []


def test_uncertainty_reject_preserves_failed_flap_hold(selection, monkeypatch):
    state, saved = selection
    state.layout.layers = [_layer(_bin("A"))]
    evidence = {(0, 0, 0): (1, {})}
    monkeypatch.setattr(local_state, "get_current_bin_occupancy_evidence", lambda: evidence)
    piece = _prepare_selection_step(state, monkeypatch)
    servo = state.irl.servos[0]
    servo.open.side_effect = None
    servo.open.return_value = False

    assert state.step() is None
    state._raiseNoBinAvailableIncident.assert_not_called()
    state._raiseBinsFullAlert.assert_not_called()
    incident = state.gc.runtime_stats.setActiveIncident.call_args.args[0]
    assert incident["kind"] == "distribution_chute_jam"
    assert state._jam_pause_enqueued is True
    assert shared_state.command_queue.get_nowait().tag == "pause"
    assert piece.stage == PieceStage.created
    assert piece.distribution_positioned_at is None
    assert state._piece is None
    assert state.event_queue.empty()
    assert evidence == {(0, 0, 0): (1, {})}
    assert saved == []


@pytest.mark.parametrize("owner", [None, NS(), NS(physical_c4_authority=False)], ids=["absent", "unmarked", "false"])
def test_nonphysical_owner_keeps_uncertainty_no_bin_incident(selection, monkeypatch, owner):
    state, saved = selection
    state.layout.layers = [_layer(_bin("A"))]
    evidence = {(0, 0, 0): (1, {})}
    monkeypatch.setattr(local_state, "get_current_bin_occupancy_evidence", lambda: evidence)
    piece = _prepare_selection_step(state, monkeypatch)
    state.shared.c4_runtime_owner = owner

    assert state.step() == DistributionState.IDLE
    assert state._uncertain_destination_blocked is True
    assert state._occupancy_state == "positioning.no_bin_incident"
    state._raiseNoBinAvailableIncident.assert_called_once_with(piece, "A")
    state.irl.servos[0].open.assert_not_called()
    assert piece.stage == PieceStage.created
    assert saved == []


def test_occupancy_reader_uses_current_session_and_detects_aggregate_only_row(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_STATE_DB_PATH", str(tmp_path / "local.sqlite"))
    monkeypatch.setenv("MACHINE_SPECIFIC_PARAMS_PATH", str(tmp_path / "machine.toml"))
    local_state.close_local_state_keeper()
    local_state.record_piece_distribution({
        "uuid": str(uuid.uuid4()), "destination_bin": (0, 0, 0),
        "distributed_at": time.time(), "category_id": "A", "part_id": "3001",
    })
    assert local_state.get_current_bin_occupancy_evidence()[(0, 0, 0)] == (1, {"A": 1})
    with local_state._connection() as conn:
        session = local_state._get_meta(conn, local_state._META_KEY_ACTIVE_SORTING_SESSION_ID)
        conn.execute(
            "UPDATE bin_state_current SET piece_count = 0 WHERE session_id = ? AND layer_index = 0 AND section_index = 0 AND bin_index = 0",
            (session,),
        )
        conn.commit()
    assert local_state.get_current_bin_occupancy_evidence()[(0, 0, 0)] == (0, {"A": 1})
    local_state.close_local_state_keeper()
