"""Frictional transport: independently control rotor and observed piece travel."""
from dataclasses import replace
import pytest

from test_bounded_transfer import setup_episode, finish_move, seed_evidence, start_recovery


def slipped_episode(monkeypatch, observed=7.827964577, follower=None):
    p,f,c,e,t=setup_episode(monkeypatch)
    e.channel=replace(e.channel, exit_sections=frozenset(range(340,360))|frozenset(range(40)),
                      precise_sections=frozenset(range(340,360)))
    e.gap=15.329242336
    seed_evidence(p,e,18.339230769)
    e.gap=observed
    e.trailing_gap=follower
    return p,f,c,e,t


def observe_completion(f,c,e,t,gap):
    # The fake rotor completes the entire command independently of piece motion.
    e.gap=gap
    finish_move(f,c)
    t(c[0]+.01)
    t(c[0]+.05)


def test_normal_confirmed_arrival_needs_no_continuation(monkeypatch):
    p,f,c,e,t=slipped_episode(monkeypatch)
    t(100.1,True);t(100.2,True)
    assert p._episode.state=='admitted' and p._episode.first_pass
    assert not f.irl.c_channel_3_rotor_stepper.moves
    assert len(p._ledger.loads)==len(p.shared.deliveries)==1


@pytest.mark.parametrize('remaining', [3.0,-4.0,-10.0])
def test_different_slip_replans_from_observed_position(monkeypatch,remaining):
    p,f,c,e,t=slipped_episode(monkeypatch)
    ep=p._episode;identity=(ep.episode_id,ep.pocket_id,ep.boundary_index)
    start_recovery(t)
    assert ep.recovery_legs[0]['degrees']==pytest.approx(50.827964577)
    assert ep.recovery_legs[0]['observed_progress_deg']==pytest.approx(7.501277759)
    observe_completion(f,c,e,t,remaining)
    assert len(ep.recovery_legs)==2
    assert ep.recovery_legs[1]['degrees']==pytest.approx(remaining+43)
    assert ep.recovery_legs[1]['observed_progress_deg']==pytest.approx(7.827964577-remaining)
    assert (ep.episode_id,ep.pocket_id,ep.boundary_index)==identity
    assert not p._ledger.loads and ep.state=='recovering'
    assert {l['stage'] for l in ep.recovery_legs}=={1}


def test_captured_slip_uses_remaining_sweep_without_claiming_arrival(monkeypatch):
    p,f,c,e,t=slipped_episode(monkeypatch,follower=45)
    ep=p._episode;start_recovery(t)
    leg=ep.recovery_legs[0];limit=ep.recovery_decision['forward_clearance_deg']
    assert 0 < leg['degrees'] < limit < 50.827964577
    assert ep.recovery_decision['predicates']['forward_sweep_clear']
    assert ep.state=='recovering' and not p._ledger.loads
    # This reproduces the 18.34 / 7.50 initial slip; motion was not completion.
    assert ep.recovery_legs[0]['observed_progress_deg']==pytest.approx(7.501277759)


def test_arrival_during_second_move_cancels_continuation_and_admits_once(monkeypatch):
    p,f,c,e,t=slipped_episode(monkeypatch)
    ep=p._episode;start_recovery(t);observe_completion(f,c,e,t,3)
    t(c[0]+.1,True);t(c[0]+.1,True)
    assert ep.recovery_arrival and len(ep.recovery_legs)==2
    assert not p._ledger.loads  # C4 cannot index while the accepted C3 move settles.
    finish_move(f,c);t(c[0]+.1,True);t(c[0]+.1,True)
    assert len(p._ledger.loads)==len(p.shared.deliveries)==1
    assert ep.state=='admitted' and len(ep.recovery_legs)==2
    assert p.shared.c3_transfer_episode is ep


def test_stale_c3_frame_cannot_advance_again(monkeypatch):
    p,f,c,e,t=slipped_episode(monkeypatch)
    start_recovery(t);e.c3_stale=True
    observe_completion(f,c,e,t,-4)
    assert len(p._episode.recovery_legs)==1 and not p._ledger.loads
    e.c3_stale=False;t(c[0]+.1)
    assert len(p._episode.recovery_legs)==2


def test_stale_c4_frame_cannot_confirm_arrival_or_authorize_motion(monkeypatch):
    p,f,c,e,t=slipped_episode(monkeypatch)
    start_recovery(t);e.stale=True
    observe_completion(f,c,e,t,-4)
    t(c[0]+.1,True);t(c[0]+.1,True)
    assert len(p._episode.recovery_legs)==1 and not p._ledger.loads


def test_rotor_completion_without_observed_progress_stops_with_owner(monkeypatch):
    p,f,c,e,t=slipped_episode(monkeypatch)
    ep=p._episode;start_recovery(t)
    observe_completion(f,c,e,t,e.gap)
    assert ep.state=='recovering' and len(ep.recovery_legs)==2
    assert ep.recovery_legs[-1]['kind']=='jitter'
    t(ep.recovery_deadline_mono+.1)
    assert ep.state=='unresolved' and p.shared.c3_transfer_episode is ep
    assert len(ep.recovery_legs)==2 and not p._ledger.loads
    assert 'owned_c3_motion_unresolved' in ep.recovery_decision['blocking_predicates']


def test_normal_release_without_measurable_progress_cannot_start_loop(monkeypatch):
    p,f,c,e,t=slipped_episode(monkeypatch,observed=15.329242336)
    start_recovery(t)
    assert p._episode.state=='recovering' and p._episode.recovery_legs[-1]['kind']=='jitter'
    t(p._episode.recovery_deadline_mono+.1)
    assert p._episode.state=='unresolved' and len(p._episode.recovery_legs)==1
    assert p.shared.c3_transfer_episode is p._episode


def test_exhausted_sweep_stops_even_when_leader_progresses(monkeypatch):
    p,f,c,e,t=slipped_episode(monkeypatch,follower=45)
    ep=p._episode;start_recovery(t)
    # Distinct following material at the entrance while retained leader is
    # farther into the opening. Same rotor, no fake empty-boundary recovery.
    e.trailing_gap=0
    observe_completion(f,c,e,t,-15)
    assert ep.recovery_open  # Refusal must first obtain a fresh normal frame.
    t(c[0]+.1)
    assert ep.state=='unresolved' and len(ep.recovery_legs)==1
    assert p.shared.c3_transfer_episode is ep and not p._ledger.loads
    assert not p._stepper.moves


def test_rounded_motor_steps_stay_strictly_inside_follower_clearance(monkeypatch):
    from hardware.sorter_interface import StepperMotor
    from test_eject_controller import _OwnedStepper
    class RealConversionStepper(_OwnedStepper):
        _steps_per_revolution=200
        _microsteps=8
        microsteps_for_degrees=StepperMotor.microsteps_for_degrees
    p,f,c,e,t=slipped_episode(monkeypatch,follower=42.2103)
    s=RealConversionStepper('c3');f.irl.c_channel_3_rotor_stepper=s
    start_recovery(t)
    leg=p._episode.recovery_legs[0]
    actual=abs(leg['microsteps'])*360/1600/(130/12)
    assert actual < p._episode.recovery_decision['forward_clearance_deg']
    assert leg['microsteps']==s.microsteps_for_degrees(s.moves[0])


def test_follower_inside_old_corridor_stays_outside_owned_material(monkeypatch):
    p,f,c,e,t=slipped_episode(monkeypatch,follower=45)
    start_recovery(t)
    e.trailing_gap=15
    observe_completion(f,c,e,t,-10)
    d=p._episode.recovery_decision
    assert d['current_ids']==[42]
    assert [x['id'] for x in d['followers']]==[99]
    assert len(p._episode.recovery_legs)==2
    assert 0 < p._episode.recovery_legs[-1]['degrees'] < d['forward_clearance_deg'] < 15


def test_missing_ids_do_not_merge_distinct_boxes_in_old_corridor(monkeypatch):
    from subsystems.feeder.go_to_angle.recovery import capture_support, assess_support
    p,f,c,e,t=slipped_episode(monkeypatch)
    ep=p._episode
    ep.leader_id=None
    ep.support={'low':-20,'high':20,'ids':[None], 'follower_ids':[None]}
    e.override=[e.piece(-10,None),e.piece(10,None)]
    evidence=capture_support(e)
    support=assess_support(ep,evidence,fresh=True,moving=False,proposed=20)
    assert not support['material'] and len(support['followers'])==2
    assert not support['predicates']['spatial_association']
    assert not support['predicates']['forward_sweep_clear']


def test_known_follower_cannot_replace_disappeared_leader(monkeypatch):
    from subsystems.feeder.go_to_angle.recovery import capture_support, assess_support
    p,f,c,e,t=slipped_episode(monkeypatch)
    ep=p._episode
    ep.support={'low':-20,'high':20,'ids':[42], 'follower_ids':[99]}
    e.override=[e.piece(0,99)]
    support=assess_support(ep,capture_support(e),fresh=True,moving=False,proposed=20)
    assert not support['material'] and [x['id'] for x in support['followers']]==[99]
    assert not support['predicates']['spatial_association']


def test_geometry_horizon_can_be_capped_without_claiming_completion():
    from subsystems.feeder.go_to_angle.recovery import followthrough_to_exit_end
    assert followthrough_to_exit_end({'exit_span_deg':40},{'material':[{'com':0}]},
                                     margin=3,maximum=30)==30
