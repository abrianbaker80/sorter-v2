"""Current79bc5297 corridor veto must not create an operator lost-handoff stop."""
from defs.events import PauseCommandEvent
from test_unverified_handoff import lost
from test_c3_owned_jitter import stalled
from test_bounded_transfer import start_recovery


def incident(ep):
    return {'kind':'classification_intake_request_timeout',
        'source_kind':'bounded_c3_transfer','episode_id':ep.episode_id,
        'status':'waiting_for_operator','awaiting_operator':True,
        'rule':'unresolved_c3_transfer','source':'indexed_pocket_pipeline._unresolvedTransfer'}


def test_ambiguous_old_corridor_consumes_reject_slot_without_operator(monkeypatch):
    p,f,c,e,t=lost(monkeypatch)
    e.extra=[e.piece(0,81),e.piece(0,82)]
    t(115.2)
    assert p._episode.state=='discard_bound' and p._tail.route_locked
    assert p.gc.runtime_stats.activeIncident() is None
    assert not any(isinstance(v,PauseCommandEvent) for v in p._deps[-1].queue) and not p.shared.deliveries


def test_same_retained_piece_uses_jitter_without_incident(monkeypatch):
    p,f,c,e,t,s=stalled(monkeypatch);start_recovery(t)
    assert len(s.jitters)==1 and p._episode.state=='recovering'
    assert p.gc.runtime_stats.activeIncident() is None and not any(isinstance(v,PauseCommandEvent) for v in p._deps[-1].queue)


def test_resolution_clears_matching_legacy_incident_only(monkeypatch):
    p,f,c,e,t=lost(monkeypatch);ep=p._episode
    p.gc.runtime_stats.setActiveIncident(incident(ep));t(115.2)
    assert ep.state=='discard_bound' and p.gc.runtime_stats.activeIncident() is None
    assert not p.shared.deliveries and not any(isinstance(v,PauseCommandEvent) for v in p._deps[-1].queue)


def test_resolution_preserves_other_fault(monkeypatch):
    p,f,c,e,t=lost(monkeypatch)
    fault={'kind':'hardware_fault','source_kind':'motor_fault','episode_id':'other'}
    p.gc.runtime_stats.setActiveIncident(fault);t(115.2)
    assert p.gc.runtime_stats.activeIncident()['kind']=='hardware_fault'


def test_visible_original_remains_owned_at_exhaustion(monkeypatch):
    p,f,c,e,t=lost(monkeypatch);e.missing=False;t(115.2)
    assert p._episode.state=='recovering' and not p._ledger.loads
    assert p._episode.recovery_legs[-1]['retained']
    assert p.gc.runtime_stats.activeIncident() is None


def test_current_79bc_incident_corridor_regression(monkeypatch):
    p,f,c,e,t=lost(monkeypatch);ep=p._episode
    ep.episode_id='79bc529723614654bf6dc11ba6545327';ep.leader_id=160
    ep.support={};ep.release_evidence['anchor']={'low':7.3853,'high':35.2577,
        'radius_low':307.2524,'radius_high':393.374}
    ep.support_motion_deg=15.0929
    ep.release_evidence['followers']=[{'id':175}]
    ep.support_missing_frames=58;ep.location_lost=True
    # Same incident: original160 absent, unassociated157/178 in broad old corridor.
    observed={'predicates':{k:True for k in ('active_episode','episode_open','c3_enabled',
        'motors_unsuppressed','c3_owner_consistent','motor_c3_resolved','c3_frame_fresh',
        'reserved_boundary','c4_available','c4_stopped_aligned','c4_frame_fresh')},
        'material':[],'corridor':{'low':-7.7076,'high':35.2577},
        'followers':[{'id':157,'low':-2.9002,'high':19.5983},
                     {'id':178,'low':15.7403,'high':47.0104}],
        'frame_ts':1115.2}
    p.gc.runtime_stats.setActiveIncident(incident(ep))
    assert p._discardUnverifiedHandoff(115.2,{'frame_ts':1115.2},observed)
    assert ep.state=='discard_bound' and p.gc.runtime_stats.activeIncident() is None
    assert not any(isinstance(v,PauseCommandEvent) for v in p._deps[-1].queue)


def test_confirmed_arrival_clears_only_same_transfer_incident(monkeypatch):
    from test_bounded_transfer import setup_episode
    p,f,c,e,t=setup_episode(monkeypatch)
    p.gc.runtime_stats.setActiveIncident(incident(p._episode))
    t(100.1,True);t(100.2,True)
    assert p._episode.state=='admitted' and len(p.shared.deliveries)==1
    assert p.gc.runtime_stats.activeIncident() is None


def test_legacy_terminal_episode_resolves_through_supported_owner_recovery(monkeypatch):
    p,f,c,e,t=lost(monkeypatch);ep=p._episode
    p._unresolvedTransfer('legacy unknown corridor veto')
    assert ep.state=='unresolved' and not ep.recovery_open
    assert p.gc.runtime_stats.activeIncident()['awaiting_operator']
    p.irl.c_channel_2_rotor_stepper=f.irl.c_channel_2_rotor_stepper
    p.irl.c_channel_3_rotor_stepper=f.irl.c_channel_3_rotor_stepper
    p.requestRetainedTransferRecovery(ep.episode_id,ep.boundary_index)
    assert p.applyRetainedTransferRecovery()
    assert p.gc.runtime_stats.activeIncident() is None
    e.extra=[e.piece(0,81),e.piece(0,82)]
    t(c[0]+.1);t(ep.recovery_deadline_mono+.01)
    assert ep.state=='discard_bound' and p.gc.runtime_stats.activeIncident() is None
    assert p._tail.route_locked and not p.shared.deliveries
