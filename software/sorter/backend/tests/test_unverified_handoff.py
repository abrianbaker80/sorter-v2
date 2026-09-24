"""Lost confirmation consumes a reject FIFO slot without inventing arrival."""
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from defs.known_object import KnownObject, PieceStage, UNVERIFIED_C4_HANDOFF
from subsystems.classification_channel.indexed_pocket_pipeline import _Phase
from subsystems.classification_channel.pocket_ledger import PocketLoad, PocketRoute
from subsystems.distribution.states import DistributionState
from utils.event import knownObjectToEvent
from test_bounded_transfer import setup_episode, finish_move
from test_distribution_sending import _GlobalConfig, _mkSending
from test_flap_control import rig
from test_flap_routing_integrity import route, due_can_release


def lost(monkeypatch):
    p, f, clock, evidence, tick = setup_episode(monkeypatch)
    evidence.missing = True
    tick(101); tick(102); tick(103.1)
    assert not p._ledger.loads and p._episode.state == 'recovering'
    return p, f, clock, evidence, tick


def test_lost_handoff_converts_original_reservation_once(monkeypatch):
    p, f, c, e, tick = lost(monkeypatch)
    ep = p._episode
    identity = ep.episode_id, ep.boundary_index, ep.pocket_id
    tick(115.2)
    load = p._tail
    obj = load.payload.ctx.known_object
    assert (ep.episode_id, ep.boundary_index, ep.pocket_id) == identity
    assert ep.state == 'discard_bound' and not ep.unresolved
    assert load.pocket_id == ep.pocket_id and load.route_locked
    assert load.route == PocketRoute.REJECT
    assert obj.transfer_episode_id == ep.episode_id
    assert obj.transport_failure_reason == UNVERIFIED_C4_HANDOFF
    assert obj.part_id is None and obj.destination_bin is None
    assert obj.physical_group_size_unknown and not ep.first_pass
    assert not p.shared.deliveries and not f.irl.c_channel_3_rotor_stepper.moves
    assert p.gc.runtime_stats.activeIncident() is None
    assert p._phase == _Phase.INDEX_ONE_POCKET and not p._resolved_failure_drain
    tick(116, True); tick(116.2, True)
    assert p._ledger.loads == (load,) and not p.shared.deliveries
    load.payload.ctx.classify_started_at = 100
    load.payload.ctx.classification_result = {'items': [{'id': '3001', 'score': 1}]}
    p._applyResults()
    assert load.route == PocketRoute.REJECT and obj.part_id is None


@pytest.mark.parametrize('paired_refusal_case',[False,True])
def test_fifo_preserves_normal_loads_and_admits_following_pocket(monkeypatch,paired_refusal_case):
    from test_handoff_observation_stale_tick import paired_refusal
    p, f, c, e, tick = (paired_refusal if paired_refusal_case else lost)(monkeypatch)
    normal = KnownObject(part_id='3001', destination_bin=(2, 1, 0))
    worker = NS(ctx=NS(known_object=normal, classification_applied=True))
    older = PocketLoad(9, worker, 99, indexes_traveled=1,
                       route=PocketRoute.NORMAL, reject_reason=None, route_locked=True)
    p._ledger._loads.append(older)
    tick(115.2)
    suspect = p._tail
    p._indexOnePocket(115.3); p._indexOnePocket(115.4)
    assert older is p._ledger.loads[0] and suspect is p._ledger.loads[1]
    assert normal.destination_bin == (2, 1, 0) and older.route == PocketRoute.NORMAL
    assert p._phase == _Phase.WAIT_INTAKE and p._drain_armed_at == 0
    p._waitIntake(115.5)
    assert p.shared.classification_ready
    p.shared.release_attempt_mono = 116
    c[0] = 116
    p._waitIntake(116)
    new_ep = p._episode
    assert new_ep.episode_id != suspect.payload.ctx.known_object.transfer_episode_id
    tick(116.1, True); tick(116.2, True)
    assert p._ledger.loads[:2] == (older, suspect)
    assert p._tail.route == PocketRoute.NORMAL or not p._tail.route_locked
    assert len(p._ledger.loads) == 3 and len(p.shared.deliveries) == 1
    assert p._tail.pocket_id != suspect.pocket_id


def test_lost_confirmation_after_recovery_motion_waits_for_completion(monkeypatch):
    p, f, c, e, tick = setup_episode(monkeypatch)
    tick(101); tick(102); tick(103.1)
    assert p._episode.recovery_legs and p.shared.c3_motion_pending
    e.missing = True
    tick(103.2)
    assert not p._ledger.loads
    finish_move(f, c)
    tick(c[0] + .1); tick(c[0] + .1)
    assert p._episode.recovery_legs[-1]['completed_at_wall'] is not None
    tick(115.2)
    assert p._episode.state == 'discard_bound' and len(p._ledger.loads) == 1


def test_suspect_consumes_capacity_until_its_fifo_turn(monkeypatch):
    p, f, c, e, tick = lost(monkeypatch)
    normals = []
    for age in range(6, 0, -1):
        obj = KnownObject(part_id=str(age), destination_bin=(1, age, 0))
        worker = NS(ctx=NS(known_object=obj, classification_applied=True))
        normals.append(PocketLoad((-age) % 10, worker, 99-age, indexes_traveled=age,
                                  route=PocketRoute.NORMAL, reject_reason=None, route_locked=True))
    p._ledger._loads.extend(normals)
    tick(115.2)
    suspect = p._tail
    assert not p._ledger.can_admit and len(p._ledger.loads) == 7
    assert p._ledger.due_to_exit_on_next_index() is normals[0]
    p._indexOnePocket(116); p._indexOnePocket(116.1)
    assert p._ledger.loads == (*normals[1:], suspect)
    assert p._ledger.can_admit
    assert all(load.route == PocketRoute.NORMAL for load in normals)


@pytest.mark.parametrize('fault', ['visible', 'reappeared', 'stale_c3', 'stale_c4',
                                    'moving_c3', 'misaligned', 'id_elsewhere'])
def test_real_blockers_never_convert_to_discard(monkeypatch, fault):
    p, f, c, e, tick = lost(monkeypatch)
    if fault in ('visible', 'reappeared'):
        e.missing = False
    elif fault == 'stale_c3': e.c3_stale = True
    elif fault == 'stale_c4': e.stale = True
    elif fault == 'moving_c3':
        f._move_targets[f.irl.c_channel_3_rotor_stepper._name] = 123456
        p.shared.c3_motion_pending = True
    elif fault == 'misaligned': p._stepper.position += 5
    elif fault == 'ambiguous': e.extra = [e.piece(0, 81), e.piece(0, 82)]
    elif fault == 'id_elsewhere': e.missing = False; e.gap = 40
    tick(115.2)
    expected = 'recovering' if fault in ('stale_c3','stale_c4','visible','reappeared','id_elsewhere') else 'unresolved'
    assert not p._ledger.loads and p._episode.state == expected
    if expected=='recovering':assert p.gc.runtime_stats.activeIncident() is None
    assert not p.shared.deliveries


def test_unverified_route_overrides_former_destination_but_waits_for_flaps(route):
    route.piece.transport_failure_reason = UNVERIFIED_C4_HANDOFF
    route.piece.forced_reject_reason = UNVERIFIED_C4_HANDOFF
    route.piece.destination_bin = (2, 0, 0)
    route.firmware.hold = True
    route.machine.step(); route.machine.step()
    assert route.machine.current_state == DistributionState.POSITIONING
    assert not due_can_release(route)
    route.positioning._reserveHarvestRoute.assert_not_called()
    route.positioning._findOrAssignBinForCategory.assert_not_called()
    route.firmware.hold = False
    route.machine.step(); route.machine.step()
    assert route.machine.current_state == DistributionState.READY
    assert all(s.isOpen() for s in route.servos)
    assert route.piece.destination_bin is None and due_can_release(route)


@pytest.mark.parametrize('physically_occupied', [False, True])
@pytest.mark.parametrize('paired_refusal_case',[False,True])
def test_reject_cycle_retires_uncertain_slot_without_physical_piece_credit(monkeypatch, physically_occupied, paired_refusal_case):
    from test_handoff_observation_stale_tick import paired_refusal
    p, f, c, e, tick = (paired_refusal if paired_refusal_case else lost)(monkeypatch)
    tick(115.2)
    load = p._tail
    obj = load.payload.ctx.known_object
    # After admission, the same pocket identity is transported by acknowledged
    # indexes. Intake detector changes cannot move/re-admit this reservation.
    e.arrived = physically_occupied
    for i in range(7):
        p._indexOnePocket(116+i); p._indexOnePocket(116.1+i)
    assert not p._ledger.loads
    assert p.transport.getPieceForDistributionDrop() is obj
    p._observeRuntime()
    assert obj.uuid in p.gc.runtime_stats._owned_piece_uuids
    gc = _GlobalConfig()
    gc.runtime_stats = p.gc.runtime_stats
    gc.set_progress_tracker = Mock()
    p.shared.transport = p.transport
    p.shared.set_distribution_gate = Mock()
    sending = _mkSending(vision=None, cooldown_s=0, shared=p.shared,
                         event_queue=p._deps[-1], gc=gc)
    sending.start_time = 1100
    confirm = Mock(side_effect=AssertionError('unverified pocket cannot confirm Harvest'))
    monkeypatch.setattr('project_harvest_runtime.confirm_piece_drop', confirm)
    assert sending.step() == DistributionState.IDLE
    assert obj.stage == PieceStage.distributed and not gc.run_recorder.pieces
    gc.set_progress_tracker.record.assert_not_called()
    event = knownObjectToEvent(obj).data.model_dump()
    gc.runtime_stats.setLifecycleState('running')
    gc.runtime_stats.observeKnownObject(event)
    gc.runtime_stats.observeKnownObject(event)
    p._observeRuntime()
    assert obj.uuid not in gc.runtime_stats._owned_piece_uuids
    snapshot = gc.runtime_stats.snapshot()
    result = snapshot['transfer_throughput']
    assert result['unverified_pockets_cleared'] == 1
    assert result['pending_episodes'] == result['completed_loads'] == 0
    assert result['total_completed_pieces'] is None and not result['cohort_reconciled']
    assert result['completed_by_outcome']['transport'] == 0
    assert snapshot['counts']['distributed'] == 0
    assert gc.runtime_stats.lookupKnownObject(obj.uuid)['transport_failure_reason'] == UNVERIFIED_C4_HANDOFF
