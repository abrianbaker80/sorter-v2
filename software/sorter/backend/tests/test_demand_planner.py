import ast
from fractions import Fraction
from pathlib import Path

import pytest

from subsystems.classification_channel.physical_fifo import PhysicalC4FIFO, PocketState
from subsystems.classification_channel.demand_planner import (
    AdvanceKind, C4DemandPlanner, ChuteObservation, NORMAL_FALL_CLEAR_S,
    RouteDeadline, Timing,
)


def make():
    fifo = PhysicalC4FIFO(microsteps_per_revolution=Fraction(52000, 3))
    return C4DemandPlanner(fifo, Timing(1.0, 0.1), discard_destination="reject")


def chute(destination="A", travel=None, moving=None, arrival=None):
    return ChuteObservation(destination, travel or {}, moving, arrival)


def deposit(planner, destination="A"):
    p = planner.fifo.deposit()
    if destination is not None:
        planner.fifo.resolve(p.pocket_id, p.generation, PocketState.ROUTED, destination=destination)
    return p


def finish(planner, decision, now, release=None):
    assert decision.index is not None
    return planner.complete_index(decision.index,
                                  confirmed_microsteps=decision.index.absolute_microsteps,
                                  now=now, physical_release_at=release)


def at_p0(destination="A"):
    planner = make()
    load = deposit(planner, destination)
    for i in range(6):
        d = planner.plan(i * 2, chute(), drain=True)
        assert d.kind is (AdvanceKind.FEED if i == 0 else AdvanceKind.DRAIN)
        assert finish(planner, d, i * 2 + 1) == ()
    assert planner.fifo.station_of(load.pocket_id) == 0
    assert planner.fifo.pockets[load.pocket_id].state is not PocketState.EMPTY
    return planner, load


def test_feed_hold_drain_and_exact_seventh_discharge():
    planner = make()
    assert planner.plan(0, chute()).kind is AdvanceKind.HOLD
    load = deposit(planner)
    d = planner.plan(0, chute())
    assert d.kind is AdvanceKind.FEED
    assert finish(planner, d, 1) == ()
    assert planner.plan(1, chute()).kind is AdvanceKind.HOLD
    for i in range(1, 6):
        d = planner.plan(i * 2, chute(), drain=True)
        assert d.kind is AdvanceKind.DRAIN
        assert finish(planner, d, i * 2 + 1) == ()
    assert planner.fifo.station_of(load.pocket_id) == 0
    d = planner.plan(12, chute(), drain=True)
    events = finish(planner, d, 13)
    assert len(events) == 1
    assert events[0].pocket.pocket_id == load.pocket_id
    assert events[0].boundary == 7
    assert planner.plan(14, chute(), drain=True).kind is AdvanceKind.HOLD


@pytest.mark.parametrize("arrival,expected", [(12.899, AdvanceKind.DRAIN),
                                               (12.9, AdvanceKind.DRAIN),
                                               (12.901, AdvanceKind.HOLD)])
def test_predicted_arrival_includes_margin(arrival, expected):
    planner, _ = at_p0()
    d = planner.plan(12, chute(None, moving="A", arrival=arrival), drain=True)
    assert d.kind is expected
    assert d.chute_move is None
    assert planner.fifo.boundary == 6


def test_aligned_route_needs_no_travel_estimate():
    planner, _ = at_p0()
    assert planner.plan(12, chute("A"), drain=True).kind is AdvanceKind.DRAIN


def test_planned_chute_move_can_beat_release_or_hold_until_ready():
    planner, _ = at_p0()
    d = planner.plan(12, chute("B", {"A": 2}), drain=True)
    assert d.kind is AdvanceKind.HOLD
    assert planner.fifo.pending_index is None
    assert d.chute_move.destination == "A"
    assert d.chute_move.arrive_at == 14
    d = planner.plan(13.2, chute(None, moving="A", arrival=14), drain=True)
    assert d.kind is AdvanceKind.DRAIN
    other, _ = at_p0()
    d = other.plan(12, chute("B", {"A": 0.8}), drain=True)
    assert d.kind is AdvanceKind.DRAIN
    assert d.chute_move.depart_at == 12


def test_unknown_or_expired_arrival_does_not_authorize_release():
    planner, _ = at_p0()
    assert planner.plan(12, chute("B"), drain=True).kind is AdvanceKind.HOLD
    assert planner.plan(12, chute("A", moving="B", arrival=13), drain=True).kind is AdvanceKind.HOLD
    assert planner.plan(12, chute(None, moving="A", arrival=11), drain=True).kind is AdvanceKind.HOLD


def test_future_route_lookahead_without_feed_demand():
    planner = make()
    deposit(planner, "B")
    d = planner.plan(0, chute("A", {"B": 0.5}))
    assert d.kind is AdvanceKind.FEED
    assert d.release_at is None
    assert d.chute_move.destination == "B"
    finish(planner, d, 1)
    d = planner.plan(1, chute("A", {"B": 0.5}))
    assert d.kind is AdvanceKind.HOLD
    assert d.chute_move.destination == "B"


def test_fall_clear_holds_departure_until_exact_boundary():
    planner, first = at_p0()
    deposit(planner, "B")
    d = planner.plan(12, chute("A"))
    assert d.kind is AdvanceKind.FEED
    assert finish(planner, d, 13.2, release=13)[0].pocket.pocket_id == first.pocket_id
    assert planner.fall_clear_at == 14.5
    for now in (13.2, 14.499):
        d = planner.plan(now, chute("A", {"B": 0.4}))
        assert d.chute_move is None
    d = planner.plan(14.5, chute("A", {"B": 0.4}))
    assert d.kind is AdvanceKind.HOLD  # no feed demand, but preposition now
    assert d.chute_move.destination == "B"
    assert d.chute_move.depart_at == 14.5


def test_completion_time_is_conservative_when_actual_release_unknown():
    planner, _ = at_p0()
    d = planner.plan(12, chute(), drain=True)
    finish(planner, d, 15)
    assert planner.fall_clear_at == 16.5  # not predicted release 13 + 1.5


def test_pending_deadline_and_discard_destination():
    planner = make()
    p = deposit(planner, None)
    deadline = RouteDeadline(p.pocket_id, p.generation, 2)
    d = planner.plan(0, chute(), deadlines=(deadline,))
    finish(planner, d, 1)
    planner.plan(1, chute(), deadlines=(deadline,))
    assert planner.fifo.pockets[p.pocket_id].state is PocketState.PENDING
    d = planner.plan(2, chute("A", {"reject": 0.3}), deadlines=(deadline,))
    assert planner.fifo.pockets[p.pocket_id].state is PocketState.DISCARD
    assert d.chute_move.destination == "reject"


def test_p0_is_final_pending_deadline_not_transport_lock():
    planner, load = at_p0(None)
    d = planner.plan(12, chute("reject"), drain=True)
    assert d.kind is AdvanceKind.DRAIN
    assert planner.fifo.pockets[load.pocket_id].state is PocketState.DISCARD
    assert finish(planner, d, 13)[0].pocket.state is PocketState.DISCARD


def test_explicit_discard_load_uses_discard_bin_through_seven_advances():
    planner = make()
    p = deposit(planner, None)
    planner.fifo.resolve(p.pocket_id, p.generation, PocketState.DISCARD)
    for i in range(7):
        d = planner.plan(i * 2, chute("reject"), drain=True)
        assert d.kind is not AdvanceKind.HOLD
        events = finish(planner, d, i * 2 + 1)
        if i < 6:
            assert events == ()
        else:
            assert events[0].pocket.state is PocketState.DISCARD


def test_old_completion_does_not_clear_new_planner_index():
    planner = make()
    deposit(planner)
    first = planner.plan(0, chute())
    finish(planner, first, 1)
    second = planner.plan(2, chute(), drain=True)
    assert finish(planner, first, 2.1) == ()
    d = planner.plan(2.2, chute("B"), drain=True)
    assert d.kind is AdvanceKind.HOLD
    assert d.index == second.index
    assert d.chute_move is None
    finish(planner, second, 3)
    assert planner.fifo.boundary == 2


def test_wraparound_stale_deadline_cannot_discard_new_generation():
    planner = make()
    exits = []
    for i in range(31):
        p = deposit(planner)
        assert p.pocket_id == i % 10
        stale = RouteDeadline(p.pocket_id, p.generation - 1, 0)
        d = planner.plan(i * 3, chute(), deadlines=(stale,))
        assert d.kind is AdvanceKind.FEED
        assert planner.fifo.pockets[p.pocket_id].state is PocketState.ROUTED
        exits.extend(finish(planner, d, i * 3 + 1))
    assert [e.pocket.pocket_id for e in exits] == [i % 10 for i in range(25)]
    assert [e.boundary for e in exits] == list(range(7, 32))


def test_repeated_planning_and_completion_cannot_reissue_or_extend_fall():
    planner, _ = at_p0()
    d = planner.plan(12, chute(), drain=True)
    assert planner.plan(12.1, chute(), drain=True).kind is AdvanceKind.HOLD
    assert planner.fifo.pending_index == d.index
    finish(planner, d, 13)
    clear = planner.fall_clear_at
    assert finish(planner, d, 14) == ()
    assert planner.fall_clear_at == clear
    assert planner.fifo.boundary == 7


def test_failed_completion_preserves_pending_target():
    planner = make()
    deposit(planner)
    d = planner.plan(0, chute())
    with pytest.raises(ValueError):
        planner.complete_index(d.index, confirmed_microsteps=0, now=1)
    assert planner.fifo.boundary == 0
    assert planner.fifo.pending_index == d.index
    finish(planner, d, 2)
    assert planner.fifo.boundary == 1


def test_normal_timing_source_matches_real_sending_constant_without_runtime_import():
    source = Path(__file__).parents[1] / "subsystems/distribution/sending.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    value = next(ast.literal_eval(node.value) for node in tree.body
                 if isinstance(node, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "CHUTE_SETTLE_MS" for t in node.targets))
    assert NORMAL_FALL_CLEAR_S == value / 1000


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf")])
def test_invalid_timing_rejected(bad):
    with pytest.raises(ValueError):
        Timing(bad, 0.1)
    with pytest.raises(ValueError):
        chute(None, {"A": bad})


def test_clock_reversal_and_invalid_release_rejected():
    planner = make()
    deposit(planner)
    d = planner.plan(2, chute())
    with pytest.raises(ValueError):
        planner.plan(1, chute())
    with pytest.raises(ValueError):
        finish(planner, d, 3, release=4)
    assert planner.fifo.boundary == 0
