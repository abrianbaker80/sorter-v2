from dataclasses import FrozenInstanceError
from fractions import Fraction

import pytest

from subsystems.classification_channel.physical_fifo import (
    IndexTarget, PhysicalC4FIFO, PocketState,
)


def model(**kwargs):
    return PhysicalC4FIFO(microsteps_per_revolution=Fraction(52000, 3), **kwargs)


def advance(fifo):
    target = fifo.prepare_index()
    return fifo.complete_index(target, confirmed_microsteps=target.absolute_microsteps)


@pytest.mark.parametrize("sign", [-1, 1])
def test_exact_station_sequence_and_seventh_sweep(sign):
    fifo = model(clockwise_sign=sign)
    load = fifo.deposit()
    assert len(fifo.pockets) == 10
    assert [p.pocket_id for p in fifo.pockets] == list(range(10))
    assert fifo.station_of(load.pocket_id) == 6
    for completed in range(1, 7):
        assert advance(fifo) == ()
        assert fifo.station_of(load.pocket_id) == 6 - completed
        assert fifo.pockets[load.pocket_id] == load
    assert fifo.station_of(load.pocket_id) == 0
    events = advance(fifo)
    assert len(events) == 1
    assert events[0].pocket == load
    assert events[0].boundary == 7
    assert fifo.station_of(load.pocket_id) == 9
    assert fifo.pockets[load.pocket_id].state is PocketState.EMPTY


def test_many_loaded_cycles_preserve_physical_ids_and_fifo_order():
    fifo = model()
    waiting = []
    seen = []
    for n in range(100):
        load = fifo.deposit({"label": str(n)})
        assert load.pocket_id == n % 10
        assert load.generation == n // 10 + 1
        waiting.append(load)
        for event in advance(fifo):
            assert event.pocket == waiting.pop(0)
            seen.append(event.pocket)
        assert [p.pocket_id for p in fifo.pockets] == list(range(10))
        assert sorted(fifo.station_of(i) for i in range(10)) == list(range(10))
    for _ in range(6):
        for event in advance(fifo):
            assert event.pocket == waiting.pop(0)
            seen.append(event.pocket)
    assert not waiting
    assert len(seen) == 100
    assert all(p.state is PocketState.EMPTY for p in fifo.pockets)


def test_seven_occupied_stations_are_not_ten_loaded_stations():
    fifo = model()
    for _ in range(6):
        fifo.deposit()
        assert advance(fifo) == ()
    fifo.deposit()
    occupied = [p for p in fifo.pockets if p.state is not PocketState.EMPTY]
    assert len(occupied) == 7
    assert sorted(fifo.station_of(p.pocket_id) for p in occupied) == list(range(7))
    assert len(advance(fifo)) == 1


def test_empty_indexes_and_sparse_loads_do_not_fabricate_events():
    fifo = model()
    for _ in range(31):
        assert advance(fifo) == ()
    assert fifo.intake_pocket_id == 1
    load = fifo.deposit()
    for _ in range(6):
        assert advance(fifo) == ()
    assert advance(fifo)[0].pocket == load
    for _ in range(23):
        assert advance(fifo) == ()
    assert sum(p.generation for p in fifo.pockets) == 1


def test_reuse_and_stale_results():
    fifo = model()
    old = fifo.deposit()
    for _ in range(7):
        advance(fifo)
    assert not fifo.resolve(old.pocket_id, old.generation, PocketState.ROUTED, destination="bin-A")
    for _ in range(3):
        advance(fifo)
    new = fifo.deposit()
    assert new.pocket_id == old.pocket_id
    assert new.generation == old.generation + 1
    assert not fifo.resolve(old.pocket_id, old.generation, PocketState.DISCARD)
    assert fifo.pockets[new.pocket_id] == new
    assert fifo.resolve(new.pocket_id, new.generation, PocketState.ROUTED, destination="bin-B")
    assert not fifo.resolve(new.pocket_id, new.generation, PocketState.DISCARD)


@pytest.mark.parametrize("state,destination", [(PocketState.DISCARD, None), (PocketState.ROUTED, "bin-A")])
def test_resolution_survives_all_retained_stations(state, destination):
    fifo = model()
    load = fifo.deposit({"note": "diagnostic only"})
    assert fifo.resolve(load.pocket_id, load.generation, state, destination=destination)
    resolved = fifo.pockets[load.pocket_id]
    for _ in range(6):
        assert advance(fifo) == ()
        assert fifo.pockets[load.pocket_id] == resolved
    assert advance(fifo)[0].pocket == resolved


def test_submission_ack_retry_interruption_resume_only_complete_once():
    fifo = model()
    load = fifo.deposit()
    target = fifo.prepare_index()
    # Simulated adapter: submission/ACK and interrupted motion do not call
    # complete_index. Resume retrieves the outstanding absolute target.
    for _adapter_event in ("submit", "ack", "retry", "interrupt", "resume"):
        assert fifo.prepare_index() is target
        assert fifo.boundary == 0
        assert fifo.pockets[0] == load
        assert fifo.station_of(0) == 6
    with pytest.raises(ValueError):
        fifo.complete_index(target, confirmed_microsteps=target.absolute_microsteps - 1)
    assert fifo.pending_index is target
    assert fifo.complete_index(target, confirmed_microsteps=target.absolute_microsteps) == ()
    assert fifo.boundary == 1
    second = fifo.prepare_index()
    assert fifo.complete_index(target, confirmed_microsteps=target.absolute_microsteps) == ()
    assert fifo.boundary == 1
    assert fifo.pending_index is second
    assert fifo.complete_index(second, confirmed_microsteps=second.absolute_microsteps) == ()
    assert fifo.boundary == 2


def test_repeated_exit_completion_never_emits_twice():
    fifo = model()
    fifo.deposit()
    for _ in range(6):
        advance(fifo)
    target = fifo.prepare_index()
    assert len(fifo.complete_index(target, confirmed_microsteps=target.absolute_microsteps)) == 1
    for _ in range(3):
        assert fifo.complete_index(target, confirmed_microsteps=target.absolute_microsteps) == ()
    assert fifo.boundary == 7


def test_invalid_completion_cannot_mutate_state():
    fifo = model()
    fifo.deposit()
    target = fifo.prepare_index()
    for bad in (fifo.target_for(0), fifo.target_for(2), IndexTarget(1, 999)):
        with pytest.raises(ValueError):
            fifo.complete_index(bad, confirmed_microsteps=bad.absolute_microsteps)
        assert fifo.boundary == 0
        assert fifo.pending_index == target
    fresh = model()
    with pytest.raises(ValueError):
        fresh.complete_index(target, confirmed_microsteps=target.absolute_microsteps)


@pytest.mark.parametrize("sign", [-1, 1])
def test_absolute_targets_do_not_accumulate_rounding(sign):
    fifo = model(clockwise_sign=sign, origin_microsteps=-17)
    # 1733 1/3 steps per pocket: rounding relative increments would drift.
    assert fifo.target_for(3).absolute_microsteps == -17 + sign * 5200
    assert fifo.target_for(30).absolute_microsteps == -17 + sign * 52000
    for boundary in range(1, 301):
        exact = Fraction(-17) + sign * boundary * Fraction(5200, 3)
        expected = (exact + Fraction(1, 2)).numerator // (exact + Fraction(1, 2)).denominator
        target = fifo.prepare_index()
        assert target == IndexTarget(boundary, expected)
        assert fifo.target_for(boundary) == target
        assert advance(fifo) == ()
    assert fifo.boundary == 300


def test_no_deposit_overwrite_or_in_motion_and_metadata_is_immutable():
    fifo = model()
    source = {"note": "original"}
    load = fifo.deposit(source)
    source["note"] = "modified"
    assert load.metadata == (("note", "original"),)
    with pytest.raises(FrozenInstanceError):
        load.generation = 99
    with pytest.raises(RuntimeError):
        fifo.deposit()
    advance(fifo)
    fifo.prepare_index()
    with pytest.raises(RuntimeError):
        fifo.deposit()


@pytest.mark.parametrize("steps", [True, 9, 0, -10, 100.5])
def test_invalid_geometry(steps):
    with pytest.raises(ValueError):
        PhysicalC4FIFO(microsteps_per_revolution=steps)


@pytest.mark.parametrize("pocket_id", [-1, 10, True, 1.5])
def test_invalid_pocket_id(pocket_id):
    with pytest.raises(ValueError):
        model().station_of(pocket_id)


@pytest.mark.parametrize("state,destination", [(PocketState.EMPTY, None), (PocketState.PENDING, None),
                                              (PocketState.ROUTED, ""), (PocketState.DISCARD, "bin")])
def test_invalid_resolution(state, destination):
    fifo = model()
    load = fifo.deposit()
    with pytest.raises(ValueError):
        fifo.resolve(load.pocket_id, load.generation, state, destination=destination)
    assert fifo.pockets[load.pocket_id] == load
