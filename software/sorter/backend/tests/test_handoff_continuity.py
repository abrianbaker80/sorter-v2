"""Regression for follower absorption and early terminal arrival observation."""
from defs.events import PauseCommandEvent
from subsystems.feeder.go_to_angle.recovery import capture_support, assess_support
from test_bounded_transfer import setup_episode, finish_move


def test_known_overlapping_follower_never_becomes_owned_leader(monkeypatch):
    p, f, c, e, tick = setup_episode(monkeypatch)
    e.trailing_gap = 2
    sample = capture_support(e)
    p._episode.release_evidence['followers'] = [sample['material'][1]]
    p._episode.support['ids'] = [42, 99]  # Previously polluted evidence cannot perpetuate it.
    p._episode.support['follower_ids'] = [99]
    result = assess_support(p._episode, sample, fresh=True, moving=False, proposed=10)
    assert result['current_ids'] == [42]
    assert [x['id'] for x in result['followers']] == [99]
    assert not result['predicates']['forward_sweep_clear']


def test_lost_leader_with_follower_in_old_corridor_uses_one_reject_slot(monkeypatch):
    p, f, c, e, tick = setup_episode(monkeypatch)
    e.trailing_gap = 2
    sample = capture_support(e)
    ep = p._episode
    ep.release_evidence['followers'] = [sample['material'][1]]
    ep.support['ids'] = [42, 99]
    ep.support['follower_ids'] = [99]
    e.missing = True
    tick(101); tick(102); tick(103.1); tick(115.2)
    assert ep.state == 'discard_bound' and len(p._ledger.loads) == 1
    assert p._tail.pocket_id == ep.pocket_id
    assert not p.shared.deliveries and not f.irl.c_channel_3_rotor_stepper.moves
    assert not any(isinstance(x, PauseCommandEvent) for x in p._deps[-1].queue)
    tick(116, True); tick(116.1, True)
    assert len(p._ledger.loads) == 1
    p._indexOnePocket(116.2); p._indexOnePocket(116.3); p._waitIntake(116.4)
    assert p.shared.classification_ready and not ep.unresolved


def test_no_progress_uses_jitter_with_existing_arrival_budget(monkeypatch):
    p, f, c, e, tick = setup_episode(monkeypatch)
    tick(101); tick(102); tick(103.1)
    finish_move(f, c)
    tick(106.2); tick(106.3)
    assert p._episode.state == 'recovering'
    assert p._episode.recovery_deadline_mono == 115.1
    assert len(p._episode.recovery_legs) == 2
    assert p._episode.recovery_legs[-1]['kind']=='jitter'
    assert not any(isinstance(x, PauseCommandEvent) for x in p._deps[-1].queue)
    f.irl.c_channel_3_rotor_stepper.jittering=False
    tick(108, True); tick(108.1, True)
    assert p._episode.state == 'admitted' and len(p.shared.deliveries) == 1
    assert len(f.irl.c_channel_3_rotor_stepper.moves) == 1
