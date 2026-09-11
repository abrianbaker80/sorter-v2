from __future__ import annotations

import queue
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from defs.known_object import KnownObject
from irl.bin_layout import Bin, BinSection, BinSize, DistributionLayout, Layer, extractCategories
from project_harvest_projects import HarvestProjectError
from project_harvest_runtime import rehydrate_active_bin_assignments
from subsystems.distribution.positioning import Positioning


class _Logger:
    def info(self, *_args, **_kwargs) -> None:
        pass

    def exception(self, *_args, **_kwargs) -> None:
        pass

    def error(self, *_args, **_kwargs) -> None:
        pass


def _positioning(category_id: str = "harvest:test:bag-1") -> Positioning:
    state = Positioning.__new__(Positioning)
    state.gc = SimpleNamespace(project_harvest_dir="unused")
    state.logger = _Logger()
    state.event_queue = queue.Queue()
    state._harvest_pause_enqueued = False
    state.layout = SimpleNamespace(
        layers=[
            SimpleNamespace(
                max_pieces_per_bin=100,
                sections=[
                    SimpleNamespace(
                        bins=[SimpleNamespace(category_ids=[category_id])]
                    )
                ],
            )
        ]
    )
    state.chute = SimpleNamespace(isBinReachable=lambda _address: True)
    return state


def test_positioner_uses_exact_active_harvest_destination(monkeypatch) -> None:
    state = _positioning()
    piece = KnownObject(part_id="3001", color_id="2")
    allocation = {
        "project_id": "harvest-project",
        "activation_id": "activation-project",
        "allocation_id": "allocation-project",
        "group_id": "bag-1",
        "exception": False,
        "destination": {
            "group_label": "Bag 1",
            "bin_id": "L1-S1-B1",
            "category_id": "harvest:test:bag-1",
            "layer_index": 0,
            "section_index": 0,
            "bin_index": 0,
        },
    }
    monkeypatch.setattr(
        "local_state.get_current_bin_piece_counts", lambda: {(0, 0, 0): 0}
    )
    with patch("project_harvest_runtime.reserve_piece", return_value=allocation):
        route = state._reserveHarvestRoute(piece)

    assert route is not None and route is not False
    address, category_id = route
    assert (address.layer_index, address.section_index, address.bin_index) == (0, 0, 0)
    assert category_id == "harvest:test:bag-1"
    assert piece.harvest_allocation_id == "allocation-project"
    assert piece.harvest_group_label == "Bag 1"


def test_positioner_pauses_if_activated_bin_assignment_changed(monkeypatch) -> None:
    state = _positioning(category_id="some-other-category")
    control_queue: queue.Queue = queue.Queue()
    piece = KnownObject(part_id="3001", color_id="2")
    allocation = {
        "project_id": "harvest-project",
        "activation_id": "activation-project",
        "allocation_id": "allocation-project",
        "group_id": "bag-1",
        "exception": False,
        "destination": {
            "group_label": "Bag 1",
            "bin_id": "L1-S1-B1",
            "category_id": "harvest:test:bag-1",
            "layer_index": 0,
            "section_index": 0,
            "bin_index": 0,
        },
    }
    with (
        patch("server.shared_state.command_queue", control_queue),
        patch("project_harvest_runtime.reserve_piece", return_value=allocation),
    ):
        route = state._reserveHarvestRoute(piece)

    assert route is False
    assert control_queue.get_nowait().tag == "pause"
    assert state.event_queue.empty()


def _runtime_layout(categories: list[list[str]]) -> DistributionLayout:
    return DistributionLayout(
        layers=[
            Layer(
                sections=[
                    BinSection(
                        bins=[
                            Bin(BinSize.SMALL, category_ids=list(bin_categories))
                            for bin_categories in categories
                        ]
                    )
                ]
            )
        ]
    )


def _active_runtime() -> dict:
    return {
        "activation_id": "activation-test",
        "status": "active",
        "categories_before": [[[ ["normal-a"], ["normal-b"] ]]],
        "assignments": [
            {
                "layer_index": 0,
                "section_index": 0,
                "bin_index": 1,
                "category_id": "harvest:test:bag-1",
            }
        ],
    }


def test_rehydrate_active_bin_assignments_restores_exact_audited_map() -> None:
    layout = _runtime_layout([["normal-a"], ["normal-b"]])
    with (
        patch("project_harvest_runtime.active_runtime", return_value=_active_runtime()),
        patch("blob_manager.setBinCategories") as persist,
    ):
        result = rehydrate_active_bin_assignments(SimpleNamespace(), layout)

    assert result == {
        "status": "rehydrated",
        "changed": True,
        "activation_id": "activation-test",
        "assignment_count": 1,
    }
    assert extractCategories(layout) == [[[ ["normal-a"], ["harvest:test:bag-1"] ]]]
    persist.assert_called_once_with(
        [[[ ["normal-a"], ["harvest:test:bag-1"] ]]]
    )


def test_rehydrate_active_bin_assignments_accepts_already_applied_map() -> None:
    layout = _runtime_layout([["normal-a"], ["harvest:test:bag-1"]])
    with (
        patch("project_harvest_runtime.active_runtime", return_value=_active_runtime()),
        patch("blob_manager.setBinCategories") as persist,
    ):
        result = rehydrate_active_bin_assignments(SimpleNamespace(), layout)

    assert result == {"status": "already_applied", "changed": False}
    persist.assert_called_once()


def test_rehydrate_active_bin_assignments_rejects_unrelated_change() -> None:
    layout = _runtime_layout([["normal-a"], ["operator-change"]])
    with (
        patch("project_harvest_runtime.active_runtime", return_value=_active_runtime()),
        patch("blob_manager.setBinCategories"),
    ):
        with pytest.raises(HarvestProjectError) as exc_info:
            rehydrate_active_bin_assignments(SimpleNamespace(), layout)

    assert exc_info.value.code == "ACTIVE_BIN_ASSIGNMENT_MISMATCH"
    assert extractCategories(layout) == [[[ ["normal-a"], ["operator-change"] ]]]
