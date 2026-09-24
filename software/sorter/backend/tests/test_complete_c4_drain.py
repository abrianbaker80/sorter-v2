from types import SimpleNamespace as NS
from unittest.mock import Mock
import threading

import pytest

from runtime_stats import RuntimeStatsCollector
from subsystems.classification_channel.complete_drain import drain_all_pockets
from subsystems.classification_channel.five_sector_platter import C4FiveSectorPlatter
from subsystems.classification_channel.simple_state_machine_rev01.constants import C4_TRAVEL_SIGN


class Clock:
    now = 0.0
    def __call__(self): return self.now
    def sleep(self, duration): self.now += duration


class Motor:
    position = 137
    stopped = True
    software_disabled = False
    def __init__(self, servos): self.moves = []; self.servos = servos
    def set_speed_limits(self, *args): self.limits = args
    def move_steps(self, delta):
        assert all(s.isOpen() for s in self.servos)
        self.moves.append(delta)
        self.position += delta
        return True
    def move_at_speed(self, speed): self.stopped = True


class Flap:
    available = is_calibrated = stopped = True
    opened = False
    def open(self): self.opened = True; return True
    def isOpen(self): return self.opened


def rig():
    flaps = [Flap() for _ in range(3)]
    irl = NS(servos=flaps, carousel_stepper=Motor(flaps),
             **{f"c_channel_{n}_rotor_stepper": NS(stopped=True) for n in (1,2,3)})
    config = NS(classification_channel_config=NS(c4_sector_count=10,
                precise_converge_speed_usteps_per_s=5000))
    return irl, config, Clock()


@pytest.mark.parametrize('owners,reservation,route', [
    (3,False,'normal'), (0,True,'unresolved'), (2,False,'retained_flaps'),
    (6,True,'normal'), (0,False,'empty'), (1,False,'discard_bound')])
def test_all_possible_pockets_reject_regardless_of_ledger(owners,reservation,route):
    irl, config, clock = rig()
    irl.stale_ledger = [NS(destination=route) for _ in range(owners)]
    irl.unresolved_reservation = reservation
    result = drain_all_pockets(irl, config, clock=clock, sleep=clock.sleep)
    platter = C4FiveSectorPlatter.from_irl_config(config)
    assert len(irl.carousel_stepper.moves) == 10
    assert sum(irl.carousel_stepper.moves) == C4_TRAVEL_SIGN * platter.rounded_motor_microsteps_per_output_revolution
    assert result['route'] == 'reject' and result['pockets_swept'] == 10
    assert clock.now >= 16.5
    assert all(s.isOpen() for s in irl.servos)


@pytest.mark.parametrize('fault', ['reject','off_target','timeout','flap','upstream'])
def test_real_fault_never_reports_success(fault):
    irl,config,clock = rig()
    if fault == 'reject': irl.carousel_stepper.move_steps = lambda _: False
    if fault == 'off_target': irl.carousel_stepper.move_steps = lambda _: True
    if fault == 'timeout': irl.carousel_stepper.stopped = False
    if fault == 'flap': irl.servos[1].available = False
    if fault == 'upstream': irl.c_channel_3_rotor_stepper.stopped = False
    stop = Mock(); irl.carousel_stepper.move_at_speed = stop
    with pytest.raises(RuntimeError):
        drain_all_pockets(irl,config,clock=clock,sleep=clock.sleep)
    stop.assert_called_once_with(0)


def test_reconciliation_archives_six_owners_and_reservation_without_delivery_credit():
    stats = RuntimeStatsCollector()
    for i in range(7):
        stats.observeTransferEpisode(dict(episode_id=str(i), started_at_wall=1,
                state='unresolved' if i == 6 else 'admitted'))
    stats.setOwnedPieceUuids(str(i) for i in range(6))
    stats.setActiveIncident(dict(kind='classification_intake_request_timeout',
                               source_kind='bounded_c3_transfer'))
    stats.reconcileC4Drain()
    assert not stats._owned_piece_uuids and stats.activeIncident() is None
    assert len(stats._transfer_episodes) == 7
    assert all(e['completion_category'] == 'unverified' for e in stats._transfer_episodes.values())
    assert all(e['state'] == 'operator_reject_drain' for e in stats._transfer_episodes.values())
    before = dict(stats._transfer_episodes)
    stats.reconcileC4Drain()
    assert stats._transfer_episodes == before


def test_reconciliation_preserves_unrelated_hardware_fault():
    stats = RuntimeStatsCollector()
    stats.setActiveIncident(dict(kind='stepper_stall',channel='c2'))
    stats.reconcileC4Drain()
    assert stats.activeIncident()['kind'] == 'stepper_stall'


def test_recovery_blocks_queued_resume_but_allows_start_when_ready(monkeypatch):
    from sorter_controller import SorterController
    from defs.sorter_controller import SorterLifecycle
    from server import shared_state
    c = SorterController.__new__(SorterController)
    c._operation_lock = threading.RLock()
    c.irl = Mock(); c.coordinator = Mock(); c.gc = Mock(); c.vision = Mock()
    c.state = SorterLifecycle.PAUSED
    monkeypatch.setattr(shared_state,'hardware_state','homing')
    c.resume()
    c.coordinator.resume.assert_not_called()
    monkeypatch.setattr(shared_state,'hardware_state','ready')
    c.resume()
    assert c.state == SorterLifecycle.RUNNING
    c.coordinator.resume.assert_called_once()


def test_ui_api_uses_exclusive_existing_worker(monkeypatch):
    from server.routers import system
    from server import shared_state
    called = Mock()
    monkeypatch.setattr(shared_state,'_hardware_c4_drain_fn',called)
    worker = Mock(return_value={'ok':True})
    monkeypatch.setattr(system,'_start_hardware_worker',worker)
    assert system.complete_c4_drain()['ok']
    assert worker.call_args.kwargs['fn'] is called
    assert worker.call_args.kwargs['state'] == 'homing'
    assert worker.call_args.kwargs['success_state'] == 'ready'


def test_stale_feeder_tick_does_not_strand_absent_piece(monkeypatch):
    from test_unverified_handoff import lost
    p,f,c,e,tick = lost(monkeypatch)
    original = p._discardUnverifiedHandoff
    def stale(now,boundary,observed):
        observed['predicates']['feeder_tick_fresh'] = False
        return original(now,boundary,observed)
    monkeypatch.setattr(p,'_discardUnverifiedHandoff',stale)
    tick(115.2)
    assert p._episode.state == 'discard_bound'
    assert p.gc.runtime_stats.activeIncident() is None


def test_transient_c2_motion_and_no_progress_keep_original_observation_budget(monkeypatch):
    from test_bounded_transfer import setup_episode
    p,f,c,e,tick = setup_episode(monkeypatch)
    original = p.shared.request_c3_recovery
    def observe(ep,boundary,**kwargs):
        result = original(ep,boundary,**kwargs)
        if kwargs.get('action'):
            result['result'] = 'unsafe'
            result['blocking_predicates'] = ['motor_c2_resolved','observed_forward_progress']
        return result
    # No extra movement should be requested: simulate refusal before dispatch.
    def no_dispatch(ep,boundary,**kwargs):
        result = original(ep,boundary)
        if kwargs.get('action'):
            result['result'] = 'unsafe'
            result['blocking_predicates'] = ['motor_c2_resolved','observed_forward_progress']
        return result
    p.shared.request_c3_recovery = no_dispatch
    tick(103.1)
    deadline = p._episode.recovery_deadline_mono
    assert p._episode.state == 'recovering'
    assert p.gc.runtime_stats.activeIncident() is None
    tick(104)
    assert p._episode.recovery_deadline_mono == deadline
    assert not f.irl.c_channel_3_rotor_stepper.moves
    e.missing = True
    tick(105); tick(106); tick(deadline + .1)
    assert p._episode.state == 'discard_bound'


def test_real_controller_teardown_clears_six_loads_reservation_and_flap_route(monkeypatch):
    from test_indexed_pocket_pipeline import _pipeline
    from coordinator import Coordinator
    from sorter_controller import SorterController
    from subsystems.distribution.ready import Ready
    from subsystems.distribution.state_machine import DistributionStateMachine
    from subsystems.distribution.states import DistributionState
    from server import shared_state
    from defs.sorter_controller import SorterLifecycle
    p = _pipeline()
    p.gc.runtime_stats = RuntimeStatsCollector()
    p.gc.profiler = Mock()
    for i in range(6):
        p._admit(10+i)
        p._ledger.advance_to(p._ledger.boundary_index + p._travel_sign)
    p._armArrival(20)
    assert len(p._ledger.loads) == 6 and p.shared.c3_transfer_episode.unresolved
    ready = Ready(p.irl,p.gc,p.shared)
    ready._positioned_uuid = p._ledger.loads[0].payload.ctx.known_object.uuid
    ready._positioned_destination = (1,2,3)
    distribution = DistributionStateMachine.__new__(DistributionStateMachine)
    distribution.gc = p.gc
    distribution.current_state = DistributionState.READY
    distribution.states_map = {DistributionState.READY:ready}
    coordinator = Coordinator.__new__(Coordinator)
    coordinator.feeder = Mock(); coordinator.classification = p
    coordinator.distribution = distribution
    c = SorterController.__new__(SorterController)
    c._operation_lock = threading.RLock(); c.coordinator=coordinator
    c.gc=Mock(); c.vision=Mock(); c.irl=p.irl
    c.stop()
    assert not p._ledger.loads and p.shared.c3_transfer_episode is None
    assert ready._positioned_uuid is None and ready._positioned_destination is None
    irl,config,clock = rig()
    drain_all_pockets(irl,config,clock=clock,sleep=clock.sleep)
    p.gc.runtime_stats.setActiveIncident(dict(kind='distribution_chute_jam',scope='distribution'))
    p.gc.runtime_stats.reconcileC4Drain()
    assert p.gc.runtime_stats.activeIncident() is None
    replacement = _pipeline()
    assert not replacement._ledger.loads and replacement.transport.getPieceForDistributionPositioning() is None
    c.coordinator = Mock(); c.irl=Mock()
    monkeypatch.setattr(shared_state,'hardware_state','ready')
    c.start();c.resume()
    assert c.state == SorterLifecycle.RUNNING


def test_missing_owned_record_becomes_reject_pocket():
    from test_indexed_pocket_pipeline import _pipeline
    from defs.known_object import UNVERIFIED_C4_HANDOFF
    p = _pipeline();p._admit(10)
    load=p._tail;load.payload.ctx.known_object=None
    p._prepareDueLoad(load)
    assert load.route.value == 'reject' and load.route_locked
    assert load.payload.ctx.known_object.transport_failure_reason == UNVERIFIED_C4_HANDOFF


def test_conflicting_position_route_cancels_then_rejects_without_phantom_drop():
    from test_indexed_pocket_pipeline import _pipeline
    from defs.known_object import KnownObject
    p=_pipeline();p._admit(10);load=p._tail
    other=KnownObject();p.transport.placePieceForDistribution(other)
    assert not p._prepareDueLoad(load)
    assert p.transport.getPieceForDistributionDrop() is None
    assert not p._prepareDueLoad(load)
    assert p.transport.consumeCanceledPieceForDistribution(other.uuid)
    p._prepareDueLoad(load)
    assert p.transport.getPieceForDistributionPositioning() is load.payload.ctx.known_object
    assert load.route.value == 'reject'


def test_actual_ready_idle_cancel_handshake_rearms_previously_distributing_reject():
    from test_indexed_pocket_pipeline import _pipeline
    from piece_transport import ClassificationChannelTransport
    from defs.known_object import KnownObject,PieceStage
    from subsystems.distribution.ready import Ready
    from subsystems.distribution.idle import Idle
    from subsystems.distribution.states import DistributionState
    p=_pipeline();p.gc.runtime_stats=RuntimeStatsCollector()
    p.transport=ClassificationChannelTransport();p.shared.transport=p.transport
    p.shared.set_distribution_gate=lambda ready,reason=None:setattr(p.shared,'distribution_ready',ready)
    p._admit(10);load=p._tail;load.distribution_placed=True
    load.payload.ctx.known_object.stage=PieceStage.distributing
    other=KnownObject(stage=PieceStage.distributing)
    p.transport.placePieceForDistribution(other)
    ready=Ready(p.irl,p.gc,p.shared);ready.signaled=True;ready._positioned_uuid=other.uuid
    assert not p._prepareDueLoad(load)
    assert p.transport.getPieceForDistributionDrop() is None
    assert ready.step() == DistributionState.IDLE
    ready.cleanup()
    p._prepareDueLoad(load)
    assert load.payload.ctx.known_object.stage == PieceStage.created
    assert Idle(p.irl,p.gc,p.shared).step() == DistributionState.POSITIONING
    assert load.route.value == 'reject'


def test_missing_positioning_slot_uses_actual_cancel_ack_then_reject():
    from test_indexed_pocket_pipeline import _pipeline
    from piece_transport import ClassificationChannelTransport
    from defs.known_object import PieceStage
    from subsystems.distribution.ready import Ready
    from subsystems.distribution.idle import Idle
    from subsystems.distribution.states import DistributionState
    p=_pipeline();p.gc.runtime_stats=RuntimeStatsCollector()
    p.transport=ClassificationChannelTransport();p.shared.transport=p.transport
    p.shared.set_distribution_gate=lambda ready,reason=None:setattr(p.shared,'distribution_ready',ready)
    p._admit(10);load=p._tail;load.distribution_placed=True
    obj=load.payload.ctx.known_object;obj.stage=PieceStage.distributing
    ready=Ready(p.irl,p.gc,p.shared);ready.signaled=True;ready._positioned_uuid=obj.uuid
    assert not p._prepareDueLoad(load)
    assert ready.step() == DistributionState.IDLE
    assert p.transport.getPieceForDistributionDrop() is None
    p._prepareDueLoad(load)
    assert Idle(p.irl,p.gc,p.shared).step() == DistributionState.POSITIONING
    assert load.route.value == 'reject' and obj.stage == PieceStage.created
