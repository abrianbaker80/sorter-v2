"""Owned firmware jitter through real paired perception and pocket ledger."""
import pytest
from dataclasses import replace
from test_bounded_transfer import setup_episode, start_recovery


def stalled(monkeypatch):
    p,f,c,e,t=setup_episode(monkeypatch)
    p._episode.release_evidence['leader_com']=0
    s=f.irl.c_channel_3_rotor_stepper
    return p,f,c,e,t,s


def finish(f,c,t,s):
    s.jittering=False
    c[0]=max(c[0]+.1,f._busy_until[s._name]+.01)
    t(c[0]);t(c[0]+.1);t(c[0]+.1)


def test_same_retained_piece_selects_original_single_cycle(monkeypatch):
    p,f,c,e,t,s=stalled(monkeypatch);start_recovery(t)
    assert s.jitters==[(6,1,6500,180000)]
    assert not s.moves and not p._stepper.moves
    assert p.shared.c3_motion_pending
    assert p._episode.recovery_deadline_mono==pytest.approx(115.1)
    assert p._episode.recovery_legs[-1]['target']==s.position


def test_arrival_after_jitter_admitted_once(monkeypatch):
    p,f,c,e,t,s=stalled(monkeypatch);start_recovery(t)
    e.missing=True;finish(f,c,t,s)
    t(c[0]+.1,True);t(c[0]+.1,True);t(c[0]+.1,True)
    assert len(p._ledger.loads)==1 and len(p.shared.deliveries)==1
    assert len(s.jitters)==1


def test_disappeared_after_jitter_uses_unchanged_discard(monkeypatch):
    p,f,c,e,t,s=stalled(monkeypatch);start_recovery(t)
    e.missing=True;finish(f,c,t,s)
    t(c[0]+.2);t(p._episode.recovery_deadline_mono+.01)
    assert len(p._ledger.loads)==1
    assert p._episode.forced_reject_reason=='c3_handoff_unverified'
    assert p._tail.route_locked and p._episode.state!='unresolved'


def test_jitter_continues_to_observed_forward_path(monkeypatch):
    p,f,c,e,t,s=stalled(monkeypatch);start_recovery(t)
    finish(f,c,t,s)
    assert len(s.jitters)==1 and len(s.moves)==1
    assert p._episode.state=='recovering'
    assert p.shared.c3_transfer_episode is p._episode
    assert not p._ledger.loads and not p._stepper.moves
    assert p._episode.recovery_legs[-1]['retained']


@pytest.mark.parametrize('gap',[-1,1])
def test_observed_change_allows_next_bounded_cycle(monkeypatch,gap):
    p,f,c,e,t,s=stalled(monkeypatch);start_recovery(t)
    e.gap=gap;finish(f,c,t,s)
    assert len(s.jitters)==1 and p._episode.state=='recovering'
    assert len(s.moves)==1


@pytest.mark.parametrize('condition',['wrong_id','stale','misaligned','sweep','reverse','suppressed','amplitude'])
def test_no_jitter_without_identity_alignment_clearance_limits(monkeypatch,condition):
    p,f,c,e,t,s=stalled(monkeypatch)
    if condition=='wrong_id':e.leader_id=99
    if condition=='stale':e.c3_stale=True
    if condition=='misaligned':p._stepper.position+=100
    if condition=='sweep':e.extra=[e.piece(-10,99)]
    if condition=='reverse':p._episode.group_size_unknown=True
    if condition=='suppressed':s.software_disabled=True
    if condition=='amplitude':
        cfg=replace(f._cfg(),jitter_amplitude_motor_deg=30);f._cfg=lambda:cfg
    start_recovery(t)
    assert not s.jitters and not p._ledger.loads


def test_midstroke_origin_is_not_completion_and_query_fault_retains_owner(monkeypatch):
    p,f,c,e,t,s=stalled(monkeypatch);start_recovery(t)
    c[0]=f._busy_until[s._name]+.1;f._motion_tick+=1
    assert s.position==f._move_targets[s._name] and s.stopped
    assert f._busy(s) and p.shared.c3_motion_pending
    s.is_jittering=lambda:(_ for _ in ()).throw(IOError('status unavailable'))
    f._motion_tick+=1
    assert f._busy(s) and p.shared.c3_motion_pending


def test_normal_arrival_never_jitters(monkeypatch):
    p,f,c,e,t,s=stalled(monkeypatch)
    t(100.1,True);t(100.2,True)
    assert len(p._ledger.loads)==1 and not s.jitters


def test_same_piece_outside_exit_stays_retained(monkeypatch):
    p,f,c,e,t,s=stalled(monkeypatch)
    e.gap=-35
    p._episode.support_motion_deg=40
    start_recovery(t)
    assert p._episode.recovery_decision['same_piece_retained']
    assert not p._episode.location_lost and len(s.jitters)==1


def test_unknown_ack_retains_motor_owner(monkeypatch):
    p,f,c,e,t,s=stalled(monkeypatch)
    s.jitter_degrees=lambda *a:(_ for _ in ()).throw(IOError('lost ack'))
    start_recovery(t)
    assert p.shared.c3_motion_pending and s._name in f._move_targets
    assert p._episode.recovery_legs[-1]['accepted'] is None
    assert p._episode.state=='unresolved' and not p._ledger.loads


def test_stale_action_recheck_observes_without_terminalizing(monkeypatch):
    p,f,c,e,t,s=stalled(monkeypatch)
    original=f._recover_transfer
    def request(ep,boundary,*,action=None):
        if action:e.c3_stale=True
        return original(ep,boundary,action=action)
    p.shared.request_c3_recovery=request
    start_recovery(t)
    assert not s.jitters and p._episode.state=='recovering'
    assert p._episode.recovery_decision['result']=='wait'


def test_reverse_jitter_envelope_keeps_continuous_original_identity(monkeypatch):
    from subsystems.feeder.go_to_angle.recovery import capture_support,assess_support
    p,f,c,e,t,s=stalled(monkeypatch);start_recovery(t)
    anchor=dict(p._episode.support)
    # Existing uncertainty-padded bounds narrowly overlap only the reverse allowance.
    sample=capture_support(e);piece=sample['material'][0]
    piece['low']=anchor['high']+.1;piece['high']=piece['low']+.1
    sample['ts']+=.1
    result=assess_support(p._episode,sample,fresh=True,moving=True,proposed=None)
    assert result['same_piece_retained']
