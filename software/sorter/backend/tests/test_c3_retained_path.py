"""Original identity, observed guard approach, finite owned motion, no clock-only fault."""
from dataclasses import replace
import pytest
from test_bounded_transfer import setup_episode
from subsystems.feeder.go_to_angle.flow import CHANNEL_OUTPUT_GEAR_RATIO


def retained(monkeypatch, *, gap=3.37, follower=15.34):
    p,f,c,e,t=setup_episode(monkeypatch)
    t(101);t(102)
    ep=p._episode;ep.location_lost=True
    ep.release_evidence['leader_com']=10
    ep.recovery_started_mono=90;ep.recovery_deadline_mono=102;ep.state='recovering'
    e.gap=gap;e.trailing_gap=follower
    return p,f,c,e,t


def complete(f,c,t,s):
    if s._name in f._move_targets:
        s.position=f._move_targets[s._name]
    s.jittering=False
    c[0]=max(c[0]+.1,f._busy_until.get(s._name,c[0])+.01)
    t(c[0]);t(c[0]+.1);t(c[0]+.1)


def test_same_original_reacquired_after_loss_moves_with_follower_bound(monkeypatch):
    p,f,c,e,t=retained(monkeypatch);ep=p._episode;s=f.irl.c_channel_3_rotor_stepper
    t(103.1)
    assert ep.recovery_open and len(ep.recovery_legs)==1
    leg=ep.recovery_legs[0]
    assert leg['retained'] and leg['accepted']
    assert 0<leg['degrees']<ep.recovery_decision['forward_clearance_deg']
    assert ep.recovery_deadline_mono==102 and not p._stepper.moves
    assert ep.recovery_decision['retained_plan']['piece']['id']==ep.leader_id
    assert [x['id'] for x in ep.recovery_decision['retained_plan']['followers']]==[99]
    assert p.gc.runtime_stats.activeIncident() is None


@pytest.mark.parametrize('gap',[-4,-12])
def test_primary_passed_forward_target_comes_from_observed_guard_gap(monkeypatch,gap):
    p,f,c,e,t=retained(monkeypatch,gap=gap,follower=None)
    t(103.1)
    leg=p._episode.recovery_legs[0]
    assert leg['degrees']==pytest.approx(gap+25+3)
    assert leg['observed_com']==pytest.approx(gap)
    assert leg['accepted'] and not p._stepper.moves


def test_jitter_then_fresh_observation_then_forward_even_with_slip(monkeypatch):
    p,f,c,e,t=retained(monkeypatch,gap=0,follower=None)
    p._episode.release_evidence['leader_com']=0
    s=f.irl.c_channel_3_rotor_stepper;t(103.1)
    assert s.jitters==[(6,1,6500,180000)] and not s.moves
    complete(f,c,t,s)
    assert len(s.jitters)==1 and len(s.moves)==1
    assert p._episode.recovery_legs[-1]['degrees']==28
    assert not p._ledger.loads


def test_no_rotor_based_progress_or_infinite_retained_recovery(monkeypatch):
    p,f,c,e,t=retained(monkeypatch,gap=0,follower=None)
    p._episode.release_evidence['leader_com']=0
    cfg=replace(f._cfg(),fast_eject_max_advance_iterations=3)
    f._cfg=lambda:cfg;s=f.irl.c_channel_3_rotor_stepper;t(103.1)
    for _ in range(10):
        if not p._episode.recovery_open:break
        complete(f,c,t,s)
    assert p._episode.state=='unresolved'
    assert len(s.moves)==3 and len(s.jitters)==3
    assert all(l['observed_progress_deg']==0 for l in p._episode.recovery_legs)
    assert not p._ledger.loads and not p._stepper.moves


@pytest.mark.parametrize('arrives',[False,True])
def test_arrival_or_disappearance_after_retained_motion(monkeypatch,arrives):
    p,f,c,e,t=retained(monkeypatch,follower=None)
    ep=p._episode;t(103.1);s=f.irl.c_channel_3_rotor_stepper
    e.missing=True;complete(f,c,lambda at:t(at,arrives),s)
    t(c[0]+.2,arrives);t(c[0]+.3,arrives)
    assert ep.state==('admitted' if arrives else 'discard_bound')
    assert len(p._ledger.loads)==1 and len(p.shared.deliveries)==int(arrives)
    assert len(ep.recovery_legs)==1


@pytest.mark.parametrize('case',['replacement','duplicate_original','stale'])
def test_nearby_material_cannot_authorize_original_recovery(monkeypatch,case):
    p,f,c,e,t=retained(monkeypatch)
    if case=='replacement':e.leader_id=777
    elif case=='duplicate_original':e.extra=[e.piece(30,p._episode.leader_id)]
    else:e.c3_stale=True
    t(103.1)
    assert not p._episode.recovery_legs
    assert not f.irl.c_channel_3_rotor_stepper.moves
    assert not f.irl.c_channel_3_rotor_stepper.jitters
    if case=='replacement':assert p._episode.state=='discard_bound'


@pytest.mark.parametrize('case',['follower_at_exit','suppressed','misaligned','motor_unresolved','ack_unknown'])
def test_real_motion_faults_and_no_safe_sweep_preserve_owner(monkeypatch,case):
    p,f,c,e,t=retained(monkeypatch)
    s=f.irl.c_channel_3_rotor_stepper
    if case=='follower_at_exit':e.trailing_gap=0
    elif case=='suppressed':s.software_disabled=True
    elif case=='misaligned':p._stepper.position+=10
    elif case=='motor_unresolved':
        f._move_targets[s._name]=123456;p.shared.c3_motion_pending=True
    else:s.move_degrees=lambda *a:(_ for _ in ()).throw(IOError('unknown ack'))
    t(103.1)
    assert p._episode.state=='unresolved' and not p._ledger.loads
    assert p.shared.c3_transfer_episode is p._episode and not p._stepper.moves


def test_identity_lost_between_plan_and_dispatch_never_moves(monkeypatch):
    p,f,c,e,t=retained(monkeypatch);original=p.shared.request_c3_recovery
    def recheck(ep,boundary,*,action=None):
        if action:e.leader_id=777
        return original(ep,boundary,action=action)
    p.shared.request_c3_recovery=recheck
    t(103.1);t(103.2)
    assert not p._episode.recovery_legs and p._episode.state=='discard_bound'
    assert not p.gc.runtime_stats.activeIncident()


def test_0831_identity_and_nearest_follower_not_spatial_replacement(monkeypatch):
    from subsystems.feeder.go_to_angle.recovery import retained_path
    p,f,c,e,t=retained(monkeypatch)
    ep=p._episode;ep.leader_id=1
    ep.release_evidence['anchor'].update(radius_low=323.6875,radius_high=368.0099)
    ep.release_evidence['leader_com']=18.4188353361877
    piece={'id':1,'bbox':[965,411,1001,464],'com':3.367876899,
           'low':-2.725037009,'high':9.142061028,'radius_low':318.1995,'radius_high':378.5142}
    follower={'id':2,'bbox':[955,318,1021,413],'com':15.337997992,
              'low':5.505844286,'high':24.912991475,'radius_low':303.3629,'radius_high':386.9330}
    evidence={'material':[piece,follower], 'exit_span_deg':39,'entry':41,'sign':1,
              'drop_sections':list(range(88,202))}
    plan=retained_path(ep,evidence,fresh=True,cfg=f._cfg(),gear_ratio=CHANNEL_OUTPUT_GEAR_RATIO)
    assert plan['piece']['id']==1 and plan['followers'][0]['id']==2
    assert plan['forward_clearance_deg']==pytest.approx(5.505844286)
    assert not plan['reverse_sweep_clear'] and not plan['jitter']
    evidence['material']=[follower]
    assert retained_path(ep,evidence,fresh=True,cfg=f._cfg(),gear_ratio=CHANNEL_OUTPUT_GEAR_RATIO) is None


def test_piece_past_saved_far_guard_does_not_get_another_revolution(monkeypatch):
    from subsystems.feeder.go_to_angle.recovery import retained_path,capture_support
    p,f,c,e,t=retained(monkeypatch,gap=300,follower=None)
    cfg=f._cfg()
    plan=retained_path(p._episode,capture_support(e),fresh=True,cfg=cfg,gear_ratio=CHANNEL_OUTPUT_GEAR_RATIO)
    assert plan['observed_com']==-60 and plan['guard_gap_deg']<0
    assert plan['forward_degrees'] is None and plan['reason'] and not plan['jitter']


def test_new_follower_at_dispatch_recheck_blocks_motion(monkeypatch):
    p,f,c,e,t=retained(monkeypatch,follower=None);original=p.shared.request_c3_recovery
    def recheck(ep,boundary,*,action=None):
        if action:e.trailing_gap=0
        return original(ep,boundary,action=action)
    p.shared.request_c3_recovery=recheck;t(103.1)
    assert not p._episode.recovery_legs and p._episode.state=='recovering'
    t(103.2)  # New normal frame must establish the obstruction.
    assert not p._episode.recovery_legs and p._episode.state=='unresolved'
    assert not p._stepper.moves


@pytest.mark.parametrize('follower_gap',[334.8,200])
def test_wrapped_guard_neighbor_uses_same_clearance_coordinates(monkeypatch,follower_gap):
    from subsystems.feeder.go_to_angle.recovery import retained_path
    p,f,c,e,t=retained(monkeypatch)
    ep=p._episode;ep.release_evidence['leader_com']=-26
    def piece(track,com):
        return {'id':track,'com':com,'low':com-.5,'high':com+.5,
                'radius_low':390,'radius_high':410}
    evidence={'material':[piece(ep.leader_id,334),piece(99,follower_gap)],
              'exit_span_deg':25,'entry':0,'sign':1,'drop_sections':[]}
    plan=retained_path(ep,evidence,fresh=True,cfg=f._cfg(),gear_ratio=CHANNEL_OUTPUT_GEAR_RATIO)
    if follower_gap==334.8:
        assert plan['forward_clearance_deg']<0
        assert not plan['reverse_sweep_clear'] and not plan['jitter']
    else:
        assert plan['forward_clearance_deg']==199.5 and plan['reverse_sweep_clear']
