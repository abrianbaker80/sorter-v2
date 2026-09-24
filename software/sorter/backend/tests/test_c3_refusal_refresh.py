"""Refusal94642: stale tick plus zero sweep must refresh before physical stop."""
from dataclasses import replace
import pytest
from test_bounded_transfer import setup_episode
from test_c3_retained_path import complete


def stale_refusal(monkeypatch, follower=0):
    p,f,c,e,t=setup_episode(monkeypatch)
    t(101);t(102)
    e.trailing_gap=follower
    c[0]=103.1;f._last_perception_tick=101
    p._waitArrival(c[0])
    assert p._episode.state=='recovering'
    assert not p._episode.recovery_legs
    assert p._episode.recovery_branch=='refresh_refusal'
    return p,f,c,e,t


def test_stale_refusal_refreshes_geometry_then_forward(monkeypatch):
    p,f,c,e,t=stale_refusal(monkeypatch);ep=p._episode
    e.trailing_gap=20
    t(103.2)
    assert len(ep.recovery_legs)==1 and ep.recovery_legs[0]['accepted']
    assert 0<ep.recovery_legs[0]['degrees']<ep.recovery_decision['forward_clearance_deg']
    assert ep.recovery_decision['frame_ts']>1103.1
    assert ep.recovery_deadline_mono==115.1
    assert not p.gc.runtime_stats.activeIncident()


def test_same_old_camera_frame_cannot_establish_physical_stop(monkeypatch):
    p,f,c,e,t=stale_refusal(monkeypatch)
    capture=e.read_pieces_and_frame
    def old(ch):
        pieces,frame=capture(ch);frame.timestamp=1103.1;return pieces,frame
    e.read_pieces_and_frame=old
    t(103.2)
    assert p._episode.recovery_open and not p._episode.recovery_legs
    assert not p.gc.runtime_stats.activeIncident()


def test_stale_tick_alone_never_stops_even_beyond_deadline(monkeypatch):
    p,f,c,e,t=stale_refusal(monkeypatch)
    for at in (104,115.2,120):
        c[0]=at;f._last_perception_tick=at-2;p._waitArrival(at)
        assert p._episode.recovery_open and not p.gc.runtime_stats.activeIncident()
    assert not p._episode.recovery_legs


def test_fresh_original_and_no_bidirectional_clearance_faults_truthfully(monkeypatch):
    p,f,c,e,t=stale_refusal(monkeypatch);t(103.2)
    assert p._episode.state=='unresolved'
    message=p.gc.runtime_stats.activeIncident()['operator_message']
    assert 'Original C3 track 42' in message and 'follower blocks' in message
    assert 'feeder_tick_fresh' not in message
    assert not p._episode.recovery_legs


def test_fresh_disappearance_preserves_budget_then_discards_once(monkeypatch):
    p,f,c,e,t=stale_refusal(monkeypatch);ep=p._episode;e.missing=True
    t(103.2);assert ep.recovery_open and not p._ledger.loads
    t(115.2);t(115.3)
    assert ep.state=='discard_bound' and len(p._ledger.loads)==1
    assert not p.shared.deliveries and not ep.recovery_legs


def test_fresh_replacement_is_not_retained_original(monkeypatch):
    p,f,c,e,t=stale_refusal(monkeypatch);e.leader_id=999;e.trailing_gap=None
    t(103.2);t(115.2)
    assert p._episode.state=='discard_bound' and not p._episode.recovery_legs


def test_arrival_after_refreshed_continuation_completes_once(monkeypatch):
    p,f,c,e,t=stale_refusal(monkeypatch);e.trailing_gap=None;t(103.2)
    e.missing=True;complete(f,c,lambda at:t(at,True),f.irl.c_channel_3_rotor_stepper)
    t(c[0]+.2,True)
    assert p._episode.state=='admitted' and len(p.shared.deliveries)==1
    assert len(p._ledger.loads)==1


def test_no_forward_allowance_but_safe_existing_jitter_is_used(monkeypatch):
    p,f,c,e,t=stale_refusal(monkeypatch, follower=20);e.trailing_gap=None
    cfg=replace(f._cfg(),fast_eject_max_advance_iterations=0);f._cfg=lambda:cfg
    t(103.2)
    assert f.irl.c_channel_3_rotor_stepper.jitters==[(6,1,6500,180000)]
    assert p._episode.recovery_legs[-1]['kind']=='jitter'
    assert p._episode.recovery_decision['predicates']['forward_sweep_clear']
    assert p._episode.recovery_decision['predicates']['reverse_sweep_clear']


def test_positive_but_substep_clearance_does_not_loop(monkeypatch):
    from subsystems.feeder.go_to_angle import recovery as flow
    p,f,c,e,t=stale_refusal(monkeypatch)
    capture=flow.capture_support
    def tiny(service):
        evidence=capture(service)
        for part in evidence['material']:
            if part['id']==99:part['low']=1e-8
        return evidence
    monkeypatch.setattr(flow,'capture_support',tiny)
    t(103.2)
    assert p._episode.state=='unresolved' and not p._episode.recovery_legs


def test_exact_94642_original619_follower626_refusal(monkeypatch):
    from subsystems.feeder.go_to_angle import recovery
    p,f,c,e,t=setup_episode(monkeypatch);t(101);t(102);ep=p._episode
    ep.leader_id=619
    ep.release_evidence['anchor'].update(radius_low=300,radius_high=420)
    original={'id':619,'bbox':[951,435,1000,523],'com':-3.4889223466126964,
        'radius_low':309.1863198589632,'radius_high':399.16565420119446,
        'zone':2,'low':-12.699075086454343,'high':5.378606121121777}
    follower={'id':626,'bbox':[939,317,1042,440],'com':13.198489518732526,
        'radius_low':286.9179992621929,'radius_high':412.8713549599207,
        'zone':3,'low':-0.020894156001691755,'high':25.480845801360612}
    capture=recovery.capture_support
    def recorded(service):
        value=capture(service);value['material']=[original,follower];return value
    monkeypatch.setattr(recovery,'capture_support',recorded)
    c[0]=103.1;f._last_perception_tick=c[0]-1.724052558;p._waitArrival(c[0])
    assert ep.recovery_open and not ep.recovery_legs
    assert not p.gc.runtime_stats.activeIncident()
    t(103.2)
    plan=ep.recovery_decision['retained_plan']
    assert plan['piece']['id']==619
    assert plan['forward_clearance_deg']==pytest.approx(-.020894156001691755)
    assert not plan['jitter_available'] and not plan['reverse_sweep_clear']
    assert ep.state=='unresolved' and not ep.recovery_legs
    assert 'Original C3 track 619' in p.gc.runtime_stats.activeIncident()['operator_message']


def test_fresh_invalid_motion_configuration_does_not_refresh_forever(monkeypatch):
    p,f,c,e,t=stale_refusal(monkeypatch, follower=20)
    e.trailing_gap=None;p._episode.release_evidence['leader_com']=0
    cfg=replace(f._cfg(),jitter_speed_usteps_per_s=0);f._cfg=lambda:cfg
    t(103.2)
    assert p._episode.state=='unresolved' and not p._episode.recovery_legs
    assert 'motion unavailable' in p.gc.runtime_stats.activeIncident()['operator_message']
