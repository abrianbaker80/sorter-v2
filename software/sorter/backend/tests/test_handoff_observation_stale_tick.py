"""The b252e637 refusal must not spend remaining arrival observation time."""
import pytest

from defs.events import PauseCommandEvent
from test_bounded_transfer import setup_episode, finish_move


def paired_refusal(monkeypatch):
    """Reproduce 4f4f94c5 with real feeder predicates and no dispatched leg."""
    p,f,clock,e,tick=setup_episode(monkeypatch)
    tick(101);tick(102)
    ep=p._episode
    ep.leader_id=192;e.leader_id=193
    ep.release_evidence['anchor']['com']=-2.5580713789693945
    ep.recovery_started_mono=92.297
    ep.recovery_deadline_mono=104.297
    ep.state='recovering'
    f.irl.c_channel_3_rotor_stepper.estimateMoveDegreesMs=lambda *a,**k:1500
    tick(103.1)
    assert set(ep.recovery_decision['blocking_predicates'])=={
        'leg_within_time_budget','observed_forward_progress'}
    assert not ep.location_lost and not ep.recovery_decision['same_piece_retained']
    return p,f,clock,e,tick


@pytest.mark.parametrize('arrives',[False,True])
def test_paired_budget_progress_refusal_preserves_remaining_observation(monkeypatch,arrives):
    p,f,clock,e,tick=paired_refusal(monkeypatch);ep=p._episode
    identity=(ep.episode_id,ep.boundary_index,ep.pocket_id)
    deadline=ep.recovery_deadline_mono
    assert ep.state=='recovering' and ep.recovery_open
    assert ep.recovery_elapsed_s==pytest.approx(10.803)
    assert ep.recovery_decision['result']=='observing_handoff_without_further_motion'
    tick(103.2,arrives);tick(103.3,arrives)
    if not arrives:
        assert not p._ledger.loads and ep.recovery_open
    tick(deadline,arrives);tick(deadline+.1,arrives)
    assert ep.state==('admitted' if arrives else 'discard_bound')
    assert (ep.episode_id,ep.boundary_index,ep.pocket_id)==identity
    assert len(p._ledger.loads)==1 and len(p.shared.deliveries)==int(arrives)
    assert ep.recovery_deadline_mono==deadline
    assert not ep.recovery_legs and not f.irl.c_channel_3_rotor_stepper.moves
    assert not f.irl.c_channel_3_rotor_stepper.jitters
    assert p.gc.runtime_stats.activeIncident() is None
    assert not any(isinstance(x,PauseCommandEvent) for x in p._deps[-1].queue)


def test_policy_refusal_cannot_restart_motion_when_geometry_later_changes(monkeypatch):
    p,f,clock,e,tick=paired_refusal(monkeypatch);ep=p._episode
    e.gap=-6;e.width=50
    f.irl.c_channel_3_rotor_stepper.estimateMoveDegreesMs=lambda *a,**k:100
    tick(103.4)
    assert ep.recovery_branch=='observation_only' and ep.recovery_open
    assert not ep.recovery_legs and not f.irl.c_channel_3_rotor_stepper.moves
    tick(ep.recovery_deadline_mono)
    assert ep.state=='discard_bound' and not p.gc.runtime_stats.activeIncident()


@pytest.mark.parametrize('c4_empty', [True, False])
@pytest.mark.parametrize('then', ['disappears', 'remains', 'arrives', 'stays_stale'])
def test_stale_feeder_tick_does_not_terminalize_no_progress(monkeypatch, c4_empty, then):
    p, f, clock, e, tick = setup_episode(monkeypatch)
    tick(101); tick(102); tick(103.1)
    finish_move(f, clock)
    tick(106.2)  # Acknowledged completion; next sample is post-motion.
    ep = p._episode
    deadline = ep.recovery_deadline_mono
    original_boundary = p._recoveryBoundary
    def boundary(ts):
        result = original_boundary(ts)
        result['predicates']['c4_empty'] = c4_empty
        return result
    p._recoveryBoundary = boundary
    clock[0] = 106.3
    f._last_perception_tick = clock[0] - 1.136  # Actual episode's stale tick age.
    f._motion_tick += 1
    p._waitArrival(clock[0])
    assert 'feeder_tick_fresh' in ep.recovery_decision['blocking_predicates']
    assert ep.recovery_branch=='refresh_refusal'
    assert ep.recovery_decision['result']=='awaiting_fresh_recovery_observation'
    assert not f.irl.c_channel_3_rotor_stepper.jitters
    assert ep.state == 'recovering'
    assert ep.recovery_deadline_mono == deadline == 115.1
    assert len(ep.recovery_legs) == len(f.irl.c_channel_3_rotor_stepper.moves) == 1
    assert not any(isinstance(x, PauseCommandEvent) for x in p._deps[-1].queue)
    p._recoveryBoundary = original_boundary

    if then == 'disappears':
        e.missing = True
        tick(107); tick(107.2); tick(115.2)
        assert ep.state == 'discard_bound'
        assert p._tail.pocket_id == ep.pocket_id
        assert p._tail.payload.ctx.known_object.transport_failure_reason == 'c3_handoff_unverified'
        assert not p.shared.deliveries  # No invented arrival.
    elif then == 'arrives':
        tick(107, True); tick(107.2, True)
        assert ep.state == 'admitted' and len(p.shared.deliveries) == 1
    elif then == 'stays_stale':
        e.missing = True
        for at in (107, 107.2, 115.2):
            clock[0] = at
            f._last_perception_tick = at - 1.136
            f._motion_tick += 1
            p._waitArrival(at)
        assert ep.state == 'discard_bound' and len(p._ledger.loads)==1
        assert not p.shared.deliveries
    else:
        tick(107); tick(115.2)
        assert ep.state == 'unresolved' and not p._ledger.loads
    assert len(f.irl.c_channel_3_rotor_stepper.moves)==1
    assert len(ep.recovery_legs)==1+int(then=='remains')
    assert ep.recovery_deadline_mono == deadline
    assert sum(isinstance(x, PauseCommandEvent) for x in p._deps[-1].queue) == int(then=='remains')
