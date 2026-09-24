"""Terminal outcome is physical evidence, never the elapsed budget alone."""
import pytest
from defs.events import PauseCommandEvent
from test_bounded_transfer import setup_episode,start_recovery
from test_unverified_handoff import lost
from test_c3_terminal_fallthrough import incident


def test_ad8481_lost_original_reseeded_envelope_does_not_stop(monkeypatch):
    p,f,c,e,t=lost(monkeypatch);ep=p._episode
    ep.episode_id='ad8481a3719b4736bac2838e9b7e7b06';ep.leader_id=350
    ep.location_lost=True;ep.support_missing_frames=0
    ep.support={'low':-5.4323,'high':52.2373,'ids':[381,395,360]}
    p.gc.runtime_stats.setActiveIncident(incident(ep))
    observed={'predicates':{k:True for k in ('active_episode','episode_open','c3_enabled',
        'motors_unsuppressed','c3_owner_consistent','motor_c3_resolved','c3_frame_fresh',
        'reserved_boundary','c4_available','c4_stopped_aligned','c4_frame_fresh')},
        'material':[{'id':381},{'id':395},{'id':360}], 'followers':[],
        'corridor':ep.support,'frame_ts':1115.2}
    observed['predicates']['spatial_association']=False
    ep.recovery_decision=observed
    p._advanceRecovery(115.2,{'frame_ts':1115.2},observed)
    assert ep.state=='discard_bound' and p._tail.route_locked
    assert p.gc.runtime_stats.activeIncident() is None
    assert not any(isinstance(x,PauseCommandEvent) for x in p._deps[-1].queue)
    assert not p.shared.deliveries


def test_confirmed_arrival_at_expired_ceiling_admits_once(monkeypatch):
    p,f,c,e,t=lost(monkeypatch);ep=p._episode
    deadline=ep.recovery_deadline_mono
    t(deadline-.01,True);t(deadline,True);t(deadline+.1,True)
    assert ep.state=='admitted' and ep.recovery_arrival
    assert len(p._ledger.loads)==1 and len(p.shared.deliveries)==1
    assert p.gc.runtime_stats.activeIncident() is None


@pytest.mark.parametrize('camera',['c3','c4'])
def test_expiry_with_stale_evidence_waits_without_new_budget_or_incident(monkeypatch,camera):
    p,f,c,e,t=lost(monkeypatch);ep=p._episode;deadline=ep.recovery_deadline_mono
    if camera=='c3':e.c3_stale=True
    else:e.stale=True
    t(deadline+.1);t(deadline+10)
    assert ep.state=='recovering' and not p._ledger.loads
    assert ep.recovery_deadline_mono==deadline and not ep.recovery_legs
    assert p.gc.runtime_stats.activeIncident() is None
    assert ep.recovery_decision['result']=='awaiting_terminal_physical_evidence'
    e.c3_stale=e.stale=False;t(deadline+10.2)
    assert ep.state=='discard_bound' and not p.shared.deliveries


def test_expiry_original_visible_is_real_retention_not_clock_fault(monkeypatch):
    p,f,c,e,t=lost(monkeypatch);e.missing=False;t(115.2)
    assert p._episode.state=='recovering' and not p._ledger.loads
    assert p._episode.recovery_legs[-1]['retained']
    assert p.gc.runtime_stats.activeIncident() is None


def test_reseeded_envelope_never_hides_original_visible_elsewhere(monkeypatch):
    p,f,c,e,t=lost(monkeypatch);ep=p._episode
    ep.location_lost=True;ep.support={'low':-5,'high':50,'ids':[381]}
    observed={'predicates':{'spatial_association':False},'material':[{'id':381}],
        'followers':[{'id':ep.leader_id}]}
    assert p._retainedPieceVisible(observed)


@pytest.mark.parametrize('positive_identity',[False,True])
def test_unbroken_replacement_track_is_not_original_identity(monkeypatch,positive_identity):
    p,f,c,e,t=lost(monkeypatch);ep=p._episode
    ep.leader_id=192;ep.location_lost=False
    observed={'predicates':{'spatial_association':True,'c3_supported_region':True},
              'material':[{'id':193}],'followers':[],
              'same_piece_retained':positive_identity}
    assert p._retainedPieceVisible(observed) is positive_identity
