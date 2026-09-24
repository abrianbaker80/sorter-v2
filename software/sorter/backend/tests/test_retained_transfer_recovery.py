"""One explicit recovery of the active unresolved reservation, using real owners."""
import pytest
from types import SimpleNamespace
from defs.known_object import KnownObject
from subsystems.classification_channel.pocket_ledger import PocketLoad
from test_bounded_transfer import setup_episode, finish_move, start_recovery


def retained(monkeypatch):
    p,f,c,e,t=setup_episode(monkeypatch)
    p.irl=f.irl
    ep=p._episode
    ep.state='unresolved'
    ep.forced_reject_reason='c3_arrival_unconfirmed'
    ep.recovery_branch='forward_only'
    ep.recovery_started_mono=85
    ep.recovery_deadline_mono=97
    ep.recovery_legs=[{'stage':n,'key':f'{n}.forward','degrees':3,
                       'accepted':True,'completed_at_mono':90+n,'completed_at_wall':1090+n}
                      for n in (1,2,3)]
    previous=PocketLoad(pocket_id=9,payload=SimpleNamespace(ctx=SimpleNamespace(known_object=KnownObject())),
                        admitted_at_mono=80,indexes_traveled=1)
    p._ledger._loads.append(previous)
    p.gc.runtime_stats.setActiveIncident({'kind':'classification_intake_request_timeout',
        'source_kind':'bounded_c3_transfer','episode_id':ep.episode_id})
    return p,f,c,e,t,previous


def activate(p):
    ep=p._episode
    p.requestRetainedTransferRecovery(ep.episode_id,ep.boundary_index)
    assert p.applyRetainedTransferRecovery()


def test_retained_identity_fresh_frames_one_move_one_arrival(monkeypatch):
    p,f,c,e,t,previous=retained(monkeypatch);ep=p._episode
    identity=(ep.episode_id,ep.boundary_index,ep.pocket_id)
    old_legs=list(ep.recovery_legs)
    activate(p)
    assert ep.state=='unresolved' and ep.terminal_recovery_active and ep.recovery_open
    assert not p.shared.classification_ready and p.gc.runtime_stats.activeIncident() is None
    with pytest.raises(ValueError):p.requestRetainedTransferRecovery(*identity[:2])
    # Pre-recovery frames cannot confirm an arrival, nor arm motion.
    e.stale=True;t(100.1,True);t(100.2,True)
    assert len(p._ledger.loads)==1 and not f.irl.c_channel_3_rotor_stepper.moves
    e.stale=False;t(100.3);t(100.4)
    assert ep.recovery_legs[:3]==old_legs and len(ep.current_recovery_legs)==1
    assert ep.current_recovery_legs[0]['degrees']==28
    assert not p._stepper.moves and not p.shared.classification_ready
    t(100.5,True);t(100.6,True)
    assert len(p._ledger.loads)==1 and p.shared.c3_motion_pending
    finish_move(f,c);t(c[0]+.1,True);t(c[0]+.1,True)
    assert (ep.episode_id,ep.boundary_index,ep.pocket_id)==identity
    assert ep.state=='admitted' and not ep.terminal_recovery_active
    assert p._ledger.loads[0] is previous and previous.indexes_traveled==1
    assert len(p._ledger.loads)==2 and len(p.shared.deliveries)==1
    t(c[0]+.1,True)
    assert len(p._ledger.loads)==2 and len(p.shared.deliveries)==1
    assert len(f.irl.c_channel_3_rotor_stepper.moves)==1


def test_failed_retained_recovery_preserves_ownership_and_cannot_reopen(monkeypatch):
    p,f,c,e,t,previous=retained(monkeypatch);ep=p._episode
    activate(p);t(100.1);t(100.2);finish_move(f,c);t(104);t(104.1)
    assert ep.terminal_recovery_active and len(ep.current_recovery_legs)==2
    t(ep.recovery_deadline_mono+.1)
    assert ep.state=='unresolved' and not ep.terminal_recovery_active
    assert p.shared.c3_transfer_episode is ep and p._ledger.loads==(previous,)
    assert not p.shared.deliveries and len(ep.current_recovery_legs)==2
    assert p.gc.runtime_stats.activeIncident()['episode_id']==ep.episode_id
    with pytest.raises(ValueError):p.requestRetainedTransferRecovery(ep.episode_id,ep.boundary_index)
    t(120,True);t(120.2,True)
    assert not p.shared.deliveries and len(f.irl.c_channel_3_rotor_stepper.moves)==1


@pytest.mark.parametrize('mismatch',['identity','boundary','c4_position','new_release','other_incident'])
def test_recovery_never_takes_other_reservation_or_incident(monkeypatch,mismatch):
    p,f,c,e,t,previous=retained(monkeypatch);ep=p._episode
    if mismatch in ('identity','boundary'):
        with pytest.raises(ValueError):
            p.requestRetainedTransferRecovery('historical' if mismatch=='identity' else ep.episode_id,
                                             ep.boundary_index+int(mismatch=='boundary'))
    elif mismatch=='new_release':
        activate(p);p.shared.release_attempt_mono=101;t(101)
        assert not ep.terminal_recovery_active and ep.state=='unresolved'
    else:
        p.requestRetainedTransferRecovery(ep.episode_id,ep.boundary_index)
        if mismatch=='c4_position':p._stepper.position+=100
        else:p.gc.runtime_stats.setActiveIncident({'kind':'hardware_fault','episode_id':'other'})
        assert not p.applyRetainedTransferRecovery()
        assert not ep.terminal_recovery_attempted
    assert p.shared.c3_transfer_episode is ep and p._ledger.loads==(previous,)
    assert not f.irl.c_channel_3_rotor_stepper.moves and not p.shared.deliveries


def test_genuinely_failed_geometry_attempt_does_not_gain_another_retry(monkeypatch):
    p,f,c,e,t=setup_episode(monkeypatch)
    start_recovery(t);finish_move(f,c);t(107);t(107.1)
    assert p._episode.state=='recovering' and len(p._episode.recovery_legs)==2
    t(p._episode.recovery_deadline_mono+.1)
    assert p._episode.state=='unresolved'
    with pytest.raises(ValueError):
        p.requestRetainedTransferRecovery(p._episode.episode_id,p._episode.boundary_index)


def test_incident_replaced_during_validation_is_never_cleared(monkeypatch):
    p,f,c,e,t,previous=retained(monkeypatch);ep=p._episode
    boundary=p._recoveryBoundary
    replacement={'kind':'classification_intake_request_timeout',
                 'source_kind':'other_source','episode_id':'another_episode'}
    def raced_boundary(stamp):
        result=boundary(stamp)
        p.gc.runtime_stats.setActiveIncident(replacement)
        return result
    p._recoveryBoundary=raced_boundary
    p.requestRetainedTransferRecovery(ep.episode_id,ep.boundary_index)
    assert not p.applyRetainedTransferRecovery()
    assert not ep.terminal_recovery_attempted and not ep.terminal_recovery_active
    assert p.gc.runtime_stats.activeIncident()['episode_id']=='another_episode'
    assert p._ledger.loads==(previous,) and not f.irl.c_channel_3_rotor_stepper.moves


def test_terminal_attempt_does_not_create_another_drain_ownership_container(monkeypatch):
    p,f,c,e,t,previous=retained(monkeypatch)
    ep=p._episode;activate(p)
    assert ep.unresolved and p.shared.c3_transfer_episode is ep
    assert p._ledger.loads==(previous,)
    # Complete drain retains its existing ledger/episode inputs, including the
    # failure history. This test does not claim a new complete-drain API exists.
    assert len(ep.recovery_legs)==3 and ep.terminal_recovery_leg_offset==3


def test_supported_api_queues_specific_owner_and_rejects_duplicate(monkeypatch):
    from server.routers import detection
    from fastapi import HTTPException
    p,f,c,e,t,previous=retained(monkeypatch);ep=p._episode
    monkeypatch.setattr(detection.shared_state,'hardware_state','ready')
    monkeypatch.setattr(detection.shared_state,'controller_ref',
        SimpleNamespace(coordinator=SimpleNamespace(classification=SimpleNamespace(_delegate=p))))
    payload=detection.RetainedTransferRecoveryPayload(episode_id=ep.episode_id,boundary_index=ep.boundary_index)
    assert detection.recover_retained_transfer(payload)['queued']
    assert not ep.terminal_recovery_active and not f.irl.c_channel_3_rotor_stepper.moves
    with pytest.raises(HTTPException) as exc:detection.recover_retained_transfer(payload)
    assert exc.value.status_code==409
    assert p.applyRetainedTransferRecovery() and ep.terminal_recovery_active


def test_coordinator_activates_retained_attempt_before_incident_hold(monkeypatch):
    import queue
    from coordinator import Coordinator
    from test_coordinator_order import _FakeRuntime,_Profiler,_Logger
    p,f,c,e,t,previous=retained(monkeypatch);ep=p._episode
    calls=[]
    runtime=_FakeRuntime(calls)
    machine=SimpleNamespace(key='classification_channel',manual_feed_mode=False,runtime_supported=True)
    monkeypatch.setattr('coordinator.build_machine_runtime',lambda key:runtime)
    monkeypatch.setattr('coordinator.mkSortingProfile',lambda gc:SimpleNamespace(is_set_based=False,set_inventories=None))
    gc=SimpleNamespace(logger=_Logger(),profiler=_Profiler(),runtime_stats=p.gc.runtime_stats,set_progress_tracker=None)
    co=Coordinator(SimpleNamespace(distribution_layout=SimpleNamespace()),
                   SimpleNamespace(machine_setup=machine,feeding_mode='auto_channels'),gc,
                   SimpleNamespace(),queue.Queue(),SimpleNamespace())
    co.classification._delegate=p
    p.requestRetainedTransferRecovery(ep.episode_id,ep.boundary_index)
    co.step()
    assert ep.terminal_recovery_active and gc.runtime_stats.activeIncident() is None
    assert calls==['distribution','classification','feeder']
    assert p._ledger.loads==(previous,)
