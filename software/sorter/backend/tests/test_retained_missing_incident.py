"""The retained reservation, not a cleared UI incident, owns recovery identity."""
import pytest

from test_retained_transfer_recovery import retained, activate


def missing_incident(monkeypatch):
    p, f, clock, evidence, tick, previous = retained(monkeypatch)
    ep = p._episode
    ep.recovery_branch = 'exit_clearance'
    ep.recovery_legs = []  # Actual 597225bb state: no accepted recovery leg.
    p.gc.runtime_stats.clearActiveIncident(resolved_by='test')
    return p, f, clock, evidence, tick, previous


def test_absent_piece_reaches_existing_discard_path_without_incident(monkeypatch):
    p, f, clock, evidence, tick, previous = missing_incident(monkeypatch)
    ep = p._episode
    identity = ep.episode_id, ep.boundary_index, ep.pocket_id
    evidence.missing = True
    activate(p)
    assert ep.recovery_deadline_mono - ep.recovery_started_mono == 12
    tick(100.1); tick(100.2)
    assert ep.location_lost and ep.support_missing_frames >= 2
    tick(ep.recovery_deadline_mono + .1)
    assert ep.state == 'discard_bound'
    assert (ep.episode_id, ep.boundary_index, ep.pocket_id) == identity
    assert p._ledger.loads[0] is previous and len(p._ledger.loads) == 2
    assert p._tail.reject_reason == 'c3_handoff_unverified'
    assert not f.irl.c_channel_3_rotor_stepper.moves
    assert not p.shared.deliveries


def test_matching_late_arrival_uses_existing_admission(monkeypatch):
    p, f, clock, evidence, tick, previous = missing_incident(monkeypatch)
    ep = p._episode
    evidence.missing = True
    activate(p)
    tick(100.1, True); tick(100.2, True)
    assert ep.state == 'admitted' and len(p.shared.deliveries) == 1
    assert p._ledger.loads[0] is previous and len(p._ledger.loads) == 2
    assert not f.irl.c_channel_3_rotor_stepper.moves


def test_new_incident_racing_absence_validation_is_not_cleared(monkeypatch):
    p, f, clock, evidence, tick, previous = missing_incident(monkeypatch)
    ep = p._episode
    boundary = p._recoveryBoundary
    def raced(stamp):
        result = boundary(stamp)
        p.gc.runtime_stats.setActiveIncident({'kind': 'hardware_fault', 'episode_id': 'other'})
        return result
    p._recoveryBoundary = raced
    p.requestRetainedTransferRecovery(ep.episode_id, ep.boundary_index)
    assert not p.applyRetainedTransferRecovery()
    assert not ep.terminal_recovery_attempted
    assert p.gc.runtime_stats.activeIncident()['episode_id'] == 'other'
    assert p._ledger.loads == (previous,)
    assert not f.irl.c_channel_3_rotor_stepper.moves


@pytest.mark.parametrize('spent', ['attempt', 'accepted_leg'])
def test_missing_incident_does_not_grant_extra_attempt(monkeypatch, spent):
    p, f, clock, evidence, tick, previous = missing_incident(monkeypatch)
    ep = p._episode
    if spent == 'attempt': ep.terminal_recovery_attempted = True
    else: ep.recovery_legs = [{'accepted': True}]
    with pytest.raises(ValueError, match='already spent'):
        p.requestRetainedTransferRecovery(ep.episode_id, ep.boundary_index)
    assert p._ledger.loads == (previous,)
    assert not f.irl.c_channel_3_rotor_stepper.moves
