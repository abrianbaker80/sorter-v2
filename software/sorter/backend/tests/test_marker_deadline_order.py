"""The operation deadline applies to capture, not late fit validation."""

import pytest

from subsystems.classification_channel.marker_positioner import PositionError
from test_marker_index_diagnostic import Rig


def near_deadline_target_history():
    rig = Rig()
    rig.begin_index()
    positioner = rig.positioner
    rig.frozen = True
    rig.clock.advance()
    assert positioner.poll() is None  # Verify stop and establish post-motion fence.
    rig.frozen = False
    for elapsed in (7.61, 7.70):
        rig.clock.value = positioner._started + elapsed
        assert positioner.poll() is None
    assert len(positioner._window.samples) == 2
    return rig


def test_target_captured_before_deadline_confirms_after_validation():
    rig = near_deadline_target_history()
    positioner = rig.positioner
    deadline = positioner._started + positioner.limits.timeout_s
    rig.clock.value = positioner._started + 7.80
    original_sample = rig.sample
    observed = {}

    def slow_validation():
        sample = original_sample()
        observed["sample"] = sample
        rig.clock.advance(0.33547)
        return sample

    rig.sample = slow_validation
    result = positioner.poll()
    assert observed["sample"].captured_ns / 1e9 < deadline
    assert observed["sample"].received_mono < deadline < rig.clock()
    assert result.boundary == 1
    assert abs(result.residual_deg) <= positioner.limits.tolerance_deg
    assert positioner.boundary == 1 and not rig.faults


def test_target_captured_after_deadline_cannot_rescue_move():
    rig = near_deadline_target_history()
    positioner = rig.positioner
    deadline = positioner._started + positioner.limits.timeout_s
    rig.clock.value = deadline - 0.01
    original_sample = rig.sample
    observed = {}

    def late_capture():
        rig.clock.advance(0.04)
        sample = original_sample()
        observed["sample"] = sample
        return sample

    rig.sample = late_capture
    with pytest.raises(PositionError, match="deadline exceeded during observation"):
        positioner.poll()
    assert observed["sample"].captured_ns / 1e9 > deadline
    assert observed["sample"].received_mono > deadline
    assert positioner.boundary == 0 and positioner.pending == 1
    assert "deadline exceeded" in positioner.fault


def test_no_valid_target_frame_by_deadline_times_out():
    rig = near_deadline_target_history()
    positioner = rig.positioner
    rig.phase = (rig.phase + 2.0) % 360  # Outside unchanged stability spread.
    rig.clock.value = positioner._started + 7.80
    original_sample = rig.sample

    def slow_invalid_fit():
        sample = original_sample()
        rig.clock.advance(0.33547)
        return sample

    rig.sample = slow_invalid_fit
    with pytest.raises(PositionError, match="deadline exceeded during observation"):
        positioner.poll()
    assert positioner.boundary == 0 and positioner.pending == 1
    assert "deadline exceeded" in positioner.fault


def test_normal_index_confirmation_and_motor_command_unchanged():
    rig = Rig()
    rig.begin_index()
    result = rig.poll_until(lambda confirmed: confirmed is not None)
    assert result.boundary == 1
    assert rig.commands == [(36.0, 1000)]
    assert not rig.faults
