"""Bounded index behavior with a motor that can ACK while physically slipping."""

from dataclasses import replace
import json
from threading import RLock
from types import SimpleNamespace

import pytest

from subsystems.classification_channel.marker_positioner import (
    FrameFence,
    MarkerMapping,
    MarkerPositioner,
    MarkerSample,
    PositionError,
    PositionLimits,
    StableMarkers,
    TrackedStepperIndexMotor,
    calibrate_mapping,
)


class Clock:
    value = 100.0

    def __call__(self):
        return self.value

    def advance(self, amount=0.06):
        self.value += amount


class Rig:
    def __init__(self, phase=350, gains=()):
        self.clock = Clock()
        self.phase = phase
        self.gains = iter(gains)
        self.token = 0
        self.sequence = 0
        self.epoch = "camera-a"
        self.geometry = "geometry"
        self.commands = []
        self.failures = []
        self.complete_result = True
        self.marker_missing = False
        self.frozen = False
        self.sign = 1
        self.disabled = False
        self.coordinates = "motor"
        self.p = MarkerPositioner(
            self,
            self,
            MarkerMapping(350, 1, self.geometry, "motor", "pocket 0 at P6"),
            self.failures.append,
            clock=self.clock,
        )

    def stationary_token(self):
        if self.disabled:
            raise RuntimeError("motor stalled")
        return self.token

    def coordinate_identity(self):
        return self.coordinates

    def check_token(self, token):
        if self.stationary_token() != token:
            raise RuntimeError("interrupted command")

    def start(self, degrees, speed, token):
        self.check_token(token)
        self.commands.append((degrees, speed))
        self.token += 1
        self.phase = (self.phase + degrees * self.sign * next(self.gains, 1)) % 360
        return self.token

    def complete(self, receipt):
        self.check_token(receipt)
        return self.complete_result

    def fence(self):
        return FrameFence(self.epoch, self.sequence, int(self.clock() * 1e9))

    def sample(self):
        if self.marker_missing:
            return None
        if not self.frozen:
            self.sequence += 1
        return MarkerSample(
            self.epoch,
            self.sequence,
            int(self.clock() * 1e9),
            self.clock(),
            self.phase,
            self.geometry,
        )

    def finish(self):
        for _ in range(160):
            self.clock.advance()
            result = self.p.poll()
            if result is not None:
                return result
        pytest.fail("did not terminate")

    def bind(self, boundary=0):
        self.p.begin_bind(boundary)
        return self.finish()


def test_diagnostic_sample_timing_is_passive_during_normal_index():
    rig = Rig()
    rig.bind()
    events = []
    rig.p._diagnostic_sink = events.append
    original_sample = rig.sample

    def sampled():
        result = original_sample()
        rig.last_sample_timing = {"source_sequence": result.sequence, "total_sample_duration_s": 0.01}
        return result

    rig.sample = sampled
    rig.p.request_index(1, 1000)
    result = rig.finish()
    observations = [event for event in events if event["event"] == "marker_observation"]
    assert result.boundary == rig.p.boundary == 1
    assert len(rig.commands) == 1 and not rig.failures
    assert observations
    assert all(event["poll_entry_monotonic_s"] is not None for event in observations)
    assert all(event["sample_timing"]["total_sample_duration_s"] == 0.01 for event in observations)
    assert any(event["prior_poll_interval_s"] is not None for event in observations)


def test_each_confirmed_index_has_an_independent_complete_diagnostic_episode():
    rig = Rig()
    assert rig.bind().boundary == 0
    events = []
    rig.p._diagnostic_sink = events.append

    for boundary in (1, 2):
        rig.p.request_index(boundary, 1000)
        result = rig.finish()
        assert result.boundary == boundary
        assert result.residual_deg == pytest.approx(0)

    requests = [row for row in events if row["event"] == "index_requested"]
    assert len(requests) == 2
    assert requests[0]["attempt_id"] != requests[1]["attempt_id"]
    for boundary, request in enumerate(requests, 1):
        episode = [row for row in events if row.get("attempt_id") == request["attempt_id"]]
        assert request["bound_boundary"] == boundary - 1
        assert request["pending_boundary"] == boundary
        assert request["operation_deadline_monotonic_s"] == pytest.approx(
            request["request_start_monotonic_s"] + 8.0
        )
        assert request["target_phase_deg"] == rig.p.mapping.phase(boundary)
        assert [row["event"] for row in episode].count("confirmed") == 1
        completion = next(row for row in episode if row["event"] == "motor_completion_verified")
        assert completion["stopped"] is True
        assert completion["completion_monotonic_s"] is not None
        fence = next(row for row in episode if row["event"] == "stopped_frame_fence"
                     and row["stage"] == "post_motion")
        assert fence["fence_request_monotonic_s"] is not None
        assert set(fence["fence"]) == {"epoch", "sequence", "source_capture_monotonic_ns"}
        observations = [row for row in episode if row["event"] == "marker_observation"]
        assert observations
        assert all(
            row["target_phase_deg"] == rig.p.mapping.phase(
                boundary - 1 if row["stage"] == "pre_motion" else boundary
            ) for row in observations
        )
        assert all(row["history_count"] <= 32 for row in observations)
        assert all(row["time_remaining_s"] == pytest.approx(
            row["stage_deadline_monotonic_s"] - row["validation_monotonic_s"]
        ) for row in observations)
        assert all(row["stage_deadline_monotonic_s"] == pytest.approx(
            request["operation_deadline_monotonic_s"]
        ) for row in observations if row["stage"] == "pre_motion")
        receipt = next(row for row in episode if row["event"] == "motor_receipt")
        assert receipt["stage_deadline_monotonic_s"] == pytest.approx(
            receipt["stage_started_monotonic_s"] + 8.0
        )
        assert all(row["stage_deadline_monotonic_s"] == pytest.approx(
            receipt["stage_deadline_monotonic_s"]
        ) for row in observations if row["stage"] == "post_motion")
        assert any(row["window_reason"] == "stable_fresh_fit" for row in observations)
        assert any(
            row["stage"] == "post_motion"
            and row["source_sequence"] is not None
            and row["source_capture_monotonic_ns"] is not None
            and row["retrieval_monotonic_s"] is not None
            and row["validation_monotonic_s"] is not None
            and row["age_s"] >= 0
            and row["raw_phase_deg"] is not None
            and row["residual_deg"] is not None
            and row["stable_span_s"] is not None
            and row["stable_spread_deg"] is not None
            for row in observations
        )
    assert rig.p.boundary == 2 and not rig.failures and len(rig.commands) == 2


def test_later_index_timeout_retains_its_observation_and_terminal_evidence():
    rig = Rig()
    rig.bind()
    events = []
    rig.p._diagnostic_sink = events.append
    rig.p.request_index(1, 1000)
    assert rig.finish().boundary == 1
    rig.p.request_index(2, 1000)
    while len(rig.commands) < 2:
        rig.clock.advance()
        assert rig.p.poll() is None
    for _ in range(10):
        rig.clock.advance()
        assert rig.p.poll() is None
        if rig.p._window and rig.p._window.samples:
            break
    assert rig.p._window.samples
    rig.marker_missing = True
    with pytest.raises(PositionError, match="deadline exceeded during observation"):
        rig.finish()

    requests = [row for row in events if row["event"] == "index_requested"]
    assert len(requests) == 2
    second = [row for row in events if row.get("attempt_id") == requests[1]["attempt_id"]]
    assert [row["event"] for row in second].count("failure") == 1
    observations = [row for row in second if row["event"] == "marker_observation"]
    assert any(row["history_count"] > 0 for row in observations)
    assert any(row["past_stopped_frame_fence"] for row in observations)
    assert all("age_s" in row and "window_reason" in row and "time_remaining_s" in row
               for row in observations)
    failure = next(row for row in second if row["event"] == "failure")
    assert failure["reason"] == "marker index deadline exceeded during observation"
    assert all(
        failure["last_observation_state"][key] == value
        for key, value in observations[-1].items()
        if key not in ("attempt_id", "event", "recorded_wall_s", "bound_boundary", "pending_boundary")
    )
    assert failure["last_usable_observation_state"]["stage"] == "pre_motion"
    assert failure["last_usable_post_motion_observation_state"] is None
    assert rig.p.boundary == 1 and rig.p.pending == 2
    assert len(rig.commands) == 2 and rig.failures == [failure["reason"]]


def test_per_index_correction_decision_does_not_change_trim_result():
    rig = Rig(gains=(0.95, 1))
    rig.bind()
    events = []
    rig.p._diagnostic_sink = events.append
    rig.p.request_index(1, 1000)
    result = rig.finish()
    decisions = [row for row in events if row["event"] == "correction_decision"]
    assert len(decisions) == 1 and decisions[0]["action"] == "dispatch_trim"
    assert decisions[0]["correction_number"] == 1
    assert result.boundary == 1 and result.corrections == 1
    assert [command[0] for command in rig.commands] == pytest.approx([36, 1.8])
    assert not rig.failures


def test_source_identity_diagnostic_sink_remains_connected_after_confirmation():
    rig = Rig()
    events = []
    source = SimpleNamespace(identity_diagnostic_sink=None)
    positioner = MarkerPositioner(
        rig, source, rig.p.mapping, rig.failures.append,
        clock=rig.clock, diagnostic_sink=events.append,
    )
    assert source.identity_diagnostic_sink is positioner._diagnostic_sink
    positioner._diagnostic_active = False
    source.identity_diagnostic_sink({"event": "source_identity_validation", "rejection_conditions": ["epoch_mismatch"]})
    assert events == [{"event": "source_identity_validation", "rejection_conditions": ["epoch_mismatch"]}]


def test_ten_physical_indexes_wrap_and_results_are_consumed_once():
    r = Rig()
    assert r.bind().boundary == 0
    for boundary in range(1, 11):
        r.p.request_index(boundary, 1000)
        assert r.p.boundary == boundary - 1
        result = r.finish()
        assert result.boundary == boundary and abs(result.residual_deg) < 0.01
        assert r.p.poll() is None
    assert len(r.commands) == 10 and not r.failures


def test_ack_with_undertravel_gets_one_bounded_marker_correction():
    r = Rig(gains=(0.95, 1))
    r.bind()
    r.p.request_index(1, 1000)
    result = r.finish()
    assert result.corrections == 1 and result.boundary == 1
    assert [x[0] for x in r.commands] == pytest.approx([36, 1.8])


@pytest.mark.parametrize(
    "gains", [(0,), (0.5,), (-1,), (2,), (1.2,), (0.95, 0), (0.9, 0.5, 0)]
)
def test_slip_reverse_overshoot_or_nonconvergence_never_commits(gains):
    r = Rig(gains=gains)
    r.bind()
    r.p.request_index(1, 1000)
    with pytest.raises(PositionError):
        r.finish()
    assert r.p.boundary == 0 and r.p.pending == 1 and len(r.commands) <= 3
    assert len(r.failures) == 1
    with pytest.raises(PositionError):
        r.p.request_index(1, 1000)
    assert len(r.failures) == 1


@pytest.mark.parametrize(
    "failure", ["interrupted", "stall", "camera", "missing", "frozen", "timeout"]
)
def test_failures_during_move_preserve_boundary_and_existing_fault_path(failure):
    r = Rig()
    r.bind()
    r.p.request_index(1, 1000)
    while not r.commands:
        r.clock.advance()
        assert r.p.poll() is None
    if failure == "interrupted":
        r.token += 1
    if failure == "stall":
        r.disabled = True
    if failure == "camera":
        r.epoch = "new-camera"
    if failure == "missing":
        r.marker_missing = True
    if failure == "frozen":
        r.frozen = True
    if failure == "timeout":
        r.complete_result = False
    with pytest.raises(PositionError):
        r.finish()
    assert r.p.boundary == 0 and len(r.commands) == 1 and r.failures


def test_exact_live_overshoot_reverse_trim_confirms_same_p1():
    r = Rig(phase=195.772024)
    r.p.mapping = replace(r.p.mapping, zero_phase_deg=195.772024)
    r.bind()
    r.phase = 196.116912
    r.gains = iter(((232.481310 - r.phase) / (231.772024 - r.phase), 1))
    r.p.request_index(1, 1000)
    while len(r.commands) < 2:
        r.clock.advance()
        assert r.p.poll() is None
        assert r.p.boundary == 0 and r.p.pending == 1
    assert r.commands[1][0] == pytest.approx(-0.709286)
    result = r.finish()
    assert result.boundary == 1 and result.corrections == 1
    assert result.phase_deg == pytest.approx(231.772024)
    with pytest.raises(PositionError):
        r.p.request_index(0, 1000)


def test_reverse_trim_crosses_target_then_forward_trim_converges():
    r = Rig(gains=(1.05, 1.5, 1))
    r.bind()
    r.p.request_index(1, 1000)
    result = r.finish()
    assert result.boundary == 1 and result.corrections == 2
    assert [c[0] for c in r.commands] == pytest.approx([36, -1.8, 0.9])
    assert abs(result.residual_deg) < 0.01


@pytest.mark.parametrize('gains', [(1.05, -0.5), (1.05, 2), (1.05, 0.03), (1.05, 1.5, 2)])
def test_worse_oscillating_or_insignificant_trim_faults(gains):
    r = Rig(gains=gains)
    r.bind()
    r.p.request_index(1, 1000)
    with pytest.raises(PositionError, match='converge'):
        r.finish()
    assert r.p.boundary == 0 and r.p.pending == 1
    assert len(r.commands) <= 3 and len(r.failures) == 1


def test_absolute_signed_travel_cannot_cancel_the_six_degree_limit():
    r = Rig(gains=(1 + 3.5 / 36, 1 + 3 / 3.5))
    r.bind()
    r.p.request_index(1, 1000)
    with pytest.raises(PositionError, match='not converged'):
        r.finish()
    assert [c[0] for c in r.commands] == pytest.approx([36, -3.5])
    assert r.p.boundary == 0  # next +3 trim would total 6.5 absolute degrees


def test_signed_trims_allow_exact_six_degree_total():
    r = Rig(gains=(1 + 3.5 / 36, 1 + 2.5 / 3.5, 1))
    r.bind()
    r.p.request_index(1, 1000)
    assert r.finish().boundary == 1
    assert [c[0] for c in r.commands] == pytest.approx([36, -3.5, 2.5])


def test_exact_target_needs_no_correction():
    r = Rig()
    r.bind()
    r.p.request_index(1, 1000)
    assert r.finish().corrections == 0
    assert [c[0] for c in r.commands] == [36]


@pytest.mark.parametrize('offset', [-0.7, 0, 0.7, -4, 4])
def test_maintenance_target_trim_observes_then_binds_without_index(offset):
    r = Rig(phase=(26 + offset) % 360)
    r.p.begin_target_trim(1, 1000)
    assert r.p.boundary is None and r.p.pending == 1 and not r.commands
    result = r.finish()
    assert result.boundary == 1 and abs(result.residual_deg) <= 0.5
    assert [c[0] for c in r.commands] == ([] if offset == 0 else pytest.approx([-offset]))
    with pytest.raises(PositionError, match='already bound'):
        r.p.begin_target_trim(0, 1000)


@pytest.mark.parametrize('offset', [-4.01, 4.01, 36])
def test_maintenance_target_trim_refuses_unattributable_position(offset):
    r = Rig(phase=(26 + offset) % 360)
    r.p.begin_target_trim(1, 1000)
    with pytest.raises(PositionError):
        r.finish()
    assert not r.commands and r.p.boundary is None


def test_trim_stage_does_not_renew_without_fresh_markers():
    r = Rig(gains=(1.05, 1))
    r.bind()
    r.p.request_index(1, 1000)
    while len(r.commands) < 2:
        r.clock.advance()
        r.p.poll()
    correction_started = r.p._started
    r.frozen = True
    with pytest.raises(PositionError, match='deadline'):
        r.finish()
    assert r.p._started == correction_started
    assert r.p.boundary == 0 and len(r.commands) == 2


def _near_deadline_target_history(phase_offset=0):
    r = Rig()
    r.bind()
    r.p.request_index(1, 1000)
    while not r.commands:
        r.clock.advance()
        assert r.p.poll() is None
    r.clock.value = r.p._started + 0.5
    assert r.p.poll() is None  # establish the post-motion source fence
    r.phase = (r.p.mapping.phase(1) + phase_offset) % 360
    for elapsed in (7.68, 7.76):
        r.clock.value = r.p._started + elapsed
        assert r.p.poll() is None
    assert len(r.p._window.samples) == 2
    return r


def _dispatch_near_deadline_correction():
    r = _near_deadline_target_history(phase_offset=-0.611686)
    prior_stage_started = r.p._started
    r.clock.value = prior_stage_started + 7.84
    assert r.p.poll() is None
    assert [command[0] for command in r.commands] == pytest.approx([36, 0.611686])
    assert r.p._started == pytest.approx(prior_stage_started + 7.84)
    assert r.p._started < prior_stage_started + 8.0
    return r, prior_stage_started


def test_predeadline_correction_gets_its_own_completion_and_confirmation_stage():
    r, prior_stage_started = _dispatch_near_deadline_correction()
    correction_started = r.p._started
    r.clock.value = prior_stage_started + 8.185
    assert r.p.poll() is None  # correction completes after the initial move's deadline
    assert r.p._started == correction_started
    assert r.p.boundary == 0 and r.p.pending == 1

    result = r.finish()
    assert result.boundary == r.p.boundary == 1
    assert result.corrections == 1 and result.residual_deg == pytest.approx(0)
    assert r.clock() < correction_started + 8.0
    assert not r.failures and len(r.commands) == 2


def test_correction_stage_uses_retrieval_deadline_after_slow_sample_completion():
    r, _ = _dispatch_near_deadline_correction()
    correction_started = r.p._started
    r.clock.value = correction_started + 0.5
    assert r.p.poll() is None  # establish the post-correction fence
    for elapsed in (7.68, 7.76):
        r.clock.value = correction_started + elapsed
        assert r.p.poll() is None
    assert len(r.p._window.samples) == 2
    r.clock.value = correction_started + 7.84
    original_sample = r.sample
    completed = []

    def slow_sample():
        observation = original_sample()
        retrieved = observation.received_mono
        r.clock.advance(0.29547)
        completed.append(replace(
            observation, retrieved_mono=retrieved, received_mono=r.clock(),
        ))
        return completed[-1]

    r.sample = slow_sample
    result = r.p.poll()
    deadline = correction_started + r.p.limits.timeout_s
    assert completed[0].retrieved_mono < deadline < completed[0].received_mono
    assert result.boundary == r.p.boundary == 1
    assert result.corrections == 1 and result.residual_deg == pytest.approx(0)
    assert not r.failures and len(r.commands) == 2


def test_expired_move_stage_cannot_dispatch_needed_correction():
    r = _near_deadline_target_history(phase_offset=-0.611686)
    move_stage_started = r.p._started
    r.clock.value = move_stage_started + 8.01

    with pytest.raises(PositionError, match="deadline exceeded during observation"):
        r.p.poll()
    assert r.p._started == move_stage_started
    assert r.p.boundary == 0 and r.p.pending == 1
    assert len(r.commands) == 1 and len(r.failures) == 1


def test_rejected_motor_dispatch_does_not_start_another_stage():
    r = Rig()
    r.bind()
    r.p.request_index(1, 1000)
    pre_motion_started = r.p._started

    def reject_start(_degrees, _speed, _token):
        raise RuntimeError("motor refused command")

    r.start = reject_start
    with pytest.raises(RuntimeError, match="motor refused command"):
        r.finish()
    assert r.p._started == pre_motion_started
    assert r.p.boundary == 0 and r.p.pending == 1
    assert not r.commands and r.failures == ["motor refused command"]


def test_correction_stage_faults_after_its_own_eight_seconds():
    r, _ = _dispatch_near_deadline_correction()
    correction_started = r.p._started
    r.clock.value = correction_started + 0.5
    assert r.p.poll() is None
    r.marker_missing = True
    r.clock.value = correction_started + 8.01

    with pytest.raises(PositionError, match="deadline exceeded during observation"):
        r.p.poll()
    assert r.p._started == correction_started
    assert r.p.boundary == 0 and r.p.pending == 1
    assert len(r.commands) == 2 and len(r.failures) == 1


def test_second_legal_correction_gets_one_stage_but_cannot_exceed_correction_limit():
    r = Rig(gains=(0.95, 0.5, 1))
    r.bind()
    events = []
    r.p._diagnostic_sink = events.append
    r.p.request_index(1, 1000)
    result = r.finish()
    receipts = [row for row in events if row["event"] == "motor_receipt"]
    assert result.boundary == 1 and result.corrections == 2
    assert [command[0] for command in r.commands] == pytest.approx([36, 1.8, 0.9])
    assert len(receipts) == 3
    assert all(row["stage_deadline_monotonic_s"] == pytest.approx(
        row["stage_started_monotonic_s"] + 8.0
    ) for row in receipts)
    assert [row["stage_started_monotonic_s"] for row in receipts] == sorted(
        row["stage_started_monotonic_s"] for row in receipts
    )

    limited = Rig(gains=(0.95, 0.5, 0.2))
    limited.bind()
    limited.p.request_index(1, 1000)
    with pytest.raises(PositionError, match="not converged"):
        limited.finish()
    assert len(limited.commands) == 3  # no third correction or fifth stage
    assert limited.p.boundary == 0 and limited.p._corrections == 2


def test_polling_and_samples_do_not_renew_move_stage():
    r = Rig()
    r.bind()
    r.p.request_index(1, 1000)
    while not r.commands:
        r.clock.advance()
        assert r.p.poll() is None
    move_stage_started = r.p._started
    r.marker_missing = True
    for elapsed in (1.0, 4.0, 7.9):
        r.clock.value = move_stage_started + elapsed
        assert r.p.poll() is None
        assert r.p._started == move_stage_started
    r.clock.value = move_stage_started + 8.01
    with pytest.raises(PositionError, match="deadline exceeded during observation"):
        r.p.poll()
    assert len(r.commands) == 1 and r.p.boundary == 0


def test_predeadline_target_capture_confirms_after_validation_crosses_deadline():
    r = _near_deadline_target_history(phase_offset=-0.020165)
    r.clock.value = r.p._started + 7.84
    original_sample = r.sample
    captured = []

    def slow_validation():
        observation = original_sample()
        captured.append(observation)
        r.clock.advance(0.29547)
        return observation

    r.sample = slow_validation
    result = r.p.poll()
    assert captured[0].received_mono < r.p._started + r.p.limits.timeout_s
    assert r.clock() - r.p._started == pytest.approx(8.13547)
    assert result.boundary == r.p.boundary == 1
    assert result.source_capture_ns == captured[0].captured_ns
    assert result.residual_deg == pytest.approx(0.020165)
    assert not r.failures and len(r.commands) == 1


def test_predeadline_retrieval_confirms_when_sample_completes_after_deadline():
    r = _near_deadline_target_history(phase_offset=-0.020165)
    r.clock.value = r.p._started + 7.84
    original_sample = r.sample
    completed = []

    def slow_sample():
        observation = original_sample()
        retrieved = observation.received_mono
        r.clock.advance(0.29547)
        completed.append(replace(
            observation, retrieved_mono=retrieved, received_mono=r.clock(),
        ))
        return completed[-1]

    r.sample = slow_sample
    result = r.p.poll()
    deadline = r.p._started + r.p.limits.timeout_s
    assert completed[0].retrieved_mono < deadline < completed[0].received_mono
    assert result.boundary == r.p.boundary == 1
    assert result.residual_deg == pytest.approx(0.020165)
    assert result.source_capture_ns == completed[0].captured_ns
    assert not r.failures and len(r.commands) == 1


def test_predeadline_received_target_can_confirm_when_poll_begins_after_deadline():
    r = _near_deadline_target_history()
    r.clock.value = r.p._started + 7.84
    observation = r.sample()  # already received from the source before expiry
    r.clock.value = r.p._started + 8.13547
    r.sample = lambda: observation

    result = r.p.poll()
    assert observation.received_mono < r.p._started + r.p.limits.timeout_s
    assert result.boundary == r.p.boundary == 1
    assert result.source_capture_ns == observation.captured_ns
    assert not r.failures and len(r.commands) == 1


def test_postdeadline_target_capture_cannot_confirm():
    r = _near_deadline_target_history()
    r.clock.value = r.p._started + 7.99
    original_sample = r.sample
    captured = []

    def late_capture():
        r.clock.advance(0.03)
        observation = original_sample()
        captured.append(observation)
        return observation

    r.sample = late_capture
    with pytest.raises(PositionError, match="deadline exceeded during observation"):
        r.p.poll()
    assert captured[0].received_mono > r.p._started + r.p.limits.timeout_s
    assert r.p.boundary == 0 and r.p.pending == 1
    assert len(r.commands) == 1 and len(r.failures) == 1


def test_postdeadline_retrieval_cannot_confirm_even_with_fresh_completed_sample():
    r = _near_deadline_target_history()
    r.clock.value = r.p._started + 7.99
    original_sample = r.sample
    completed = []

    def late_sample():
        r.clock.advance(0.03)
        observation = original_sample()
        retrieved = observation.received_mono
        r.clock.advance(0.10)
        completed.append(replace(
            observation, retrieved_mono=retrieved, received_mono=r.clock(),
        ))
        return completed[-1]

    r.sample = late_sample
    with pytest.raises(PositionError, match="deadline exceeded during observation"):
        r.p.poll()
    deadline = r.p._started + r.p.limits.timeout_s
    assert deadline < completed[0].retrieved_mono < completed[0].received_mono
    assert r.p.boundary == 0 and r.p.pending == 1
    assert len(r.commands) == 1 and len(r.failures) == 1


def test_no_predeadline_target_confirmation_times_out_without_trim():
    r = _near_deadline_target_history(phase_offset=2)
    r.clock.value = r.p._started + 7.84
    original_sample = r.sample

    def slow_validation():
        observation = original_sample()
        r.clock.advance(0.29547)
        return observation

    r.sample = slow_validation
    with pytest.raises(PositionError, match="deadline exceeded during observation"):
        r.p.poll()
    assert r.p.boundary == 0 and r.p.pending == 1
    assert len(r.commands) == 1 and len(r.failures) == 1


def test_in_deadline_target_confirmation_still_succeeds():
    r = _near_deadline_target_history()
    r.clock.value = r.p._started + 7.84
    result = r.p.poll()
    assert result.boundary == r.p.boundary == 1
    assert result.corrections == 0 and result.residual_deg == pytest.approx(0)
    assert r.clock() - r.p._started < r.p.limits.timeout_s
    assert not r.failures and len(r.commands) == 1


def test_expired_motor_receipt_faults_without_another_move():
    r = Rig()
    r.bind()
    r.p.request_index(1, 1000)
    while not r.commands:
        r.clock.advance()
        assert r.p.poll() is None
    r.complete_result = False
    r.clock.value = r.p._started + 8.01

    with pytest.raises(PositionError, match="marker index deadline exceeded"):
        r.p.poll()
    assert r.p.boundary == 0 and r.p.pending == 1
    assert len(r.commands) == 1 and len(r.failures) == 1


def test_expiry_during_pre_motion_diagnostic_cannot_dispatch_index():
    r = Rig()
    r.bind()

    def slow_diagnostic(row):
        if row["event"] == "pre_motion_fit":
            r.clock.advance(8)

    r.p._diagnostic_sink = slow_diagnostic
    r.p.request_index(1, 1000)
    with pytest.raises(PositionError, match="deadline exceeded"):
        r.finish()
    assert r.p.boundary == 0 and r.p.pending == 1
    assert not r.commands and len(r.failures) == 1


def test_idle_rotor_displacement_refuses_new_motion():
    r = Rig()
    r.bind()
    r.phase = (r.phase + 36) % 360
    r.p.request_index(1, 1000)
    with pytest.raises(PositionError, match="idle rotor"):
        r.finish()
    assert not r.commands


@pytest.mark.parametrize('gain, succeeds', [(1, True), (0, False), (-1, False)])
def test_maintenance_adapter_uses_positioner_for_recovery_then_next_indexes(
    tmp_path, monkeypatch, gain, succeeds
):
    import importlib.util
    from pathlib import Path
    import sys
    from types import ModuleType

    artifacts = Path(__file__).resolve().parents[5] / 'analysis_artifacts'

    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    shared = ModuleType('server.shared_state')
    monkeypatch.setitem(sys.modules, 'server.shared_state', shared)
    adapter = load('signed_qualification_test', artifacts / 'c4-positioner-signed-20260922/payload/c4_marker_qualification.py')
    helpers = load('qualification_helpers', artifacts / 'c4-positioner-live-20260922/test_adapter.py')
    r = helpers.Rig(adapter, tmp_path, gains=(gain,))
    r.phase = 232.481310
    MarkerMapping(195.772024, 1, r.geometry, r.motor.coordinate_identity(), 'P0 at P6').save_new(r.mapping)
    saved_mapping = r.mapping.read_bytes()
    monkeypatch.setattr(adapter, 'prior_result', lambda: {'polarity': {'status': 'proven'}})
    if succeeds:
        r.run()
        assert r.report['passed'] == 10
        assert [row['confirmed_index']['boundary'] for row in r.report['moves']] == list(range(1, 11))
        assert r.report['moves'][0]['commands'][0]['steps'] < 0
        assert all(row['commands'][0]['steps'] > 0 for row in r.report['moves'][1:])
        assert abs(r.report['return_error']) <= 0.5
    else:
        with pytest.raises(PositionError):
            r.run()
        assert len(r.report['moves']) == 1 and r.report['passed'] == 0
        assert r.report['stop_verified'] and r.stops
    assert r.mapping.read_bytes() == saved_mapping


def test_rebind_and_skipped_or_duplicate_boundaries_are_refused():
    r = Rig()
    r.bind()
    for boundary in (0, 2, True):
        with pytest.raises(PositionError):
            r.p.request_index(boundary, 1000)
    with pytest.raises(PositionError):
        r.p.begin_bind(10)
    assert not r.commands


def test_restart_binding_verifies_modulo_only_and_never_invents_turn_count():
    r = Rig()
    assert r.bind(20).boundary == 20  # caller supplies durable revolution count
    wrong = Rig()
    wrong.p.begin_bind(21)
    with pytest.raises(PositionError):
        wrong.finish()
    assert wrong.p.boundary is None and not wrong.commands


def test_negative_motor_polarity_still_targets_clockwise_marker_phase():
    r = Rig()
    r.sign = -1
    r.p.mapping = replace(r.p.mapping, motor_sign=-1)
    r.bind()
    r.p.request_index(1, 1000)
    assert r.finish().boundary == 1
    assert r.commands[0][0] == -36


@pytest.mark.parametrize("when", ["before_bind", "during_move"])
def test_motor_config_drift_is_not_adopted_by_new_stopped_token(when):
    r = Rig()
    if when == "before_bind":
        r.coordinates = "new-gearing"
        with pytest.raises(PositionError, match="coordinates"):
            r.p.begin_bind(0)
        assert not r.commands
    else:
        r.bind()
        r.p.request_index(1, 1000)
        while not r.commands:
            r.clock.advance()
            r.p.poll()
        r.coordinates = "new-gearing"
        with pytest.raises(PositionError, match="coordinates"):
            r.finish()
        assert r.p.boundary == 0 and len(r.commands) == 1


def test_mapping_calibration_persistence_and_incompatible_geometry(tmp_path):
    r = Rig()
    mapping = calibrate_mapping(
        r,
        r,
        geometry=r.geometry,
        known_boundary=10,
        motor_sign=1,
        physical_reference="marked pocket 0 at P6",
        clock=r.clock,
        wait=r.clock.advance,
    )
    assert mapping.zero_phase_deg == 350 and not r.commands
    path = tmp_path / "marker-map.json"
    mapping.save_new(path)
    assert MarkerMapping.load(path, r.geometry, "motor") == mapping
    with pytest.raises(FileExistsError):
        mapping.save_new(path)
    with pytest.raises(PositionError):
        MarkerMapping.load(path, "changed-center", "motor")
    with pytest.raises(PositionError):
        MarkerMapping.load(path, r.geometry, "changed-inversion")
    document = json.loads(path.read_text())
    document["mapping"]["zero_phase_deg"] = 20
    path.write_text(json.dumps(document))
    with pytest.raises(PositionError, match="checksum"):
        MarkerMapping.load(path, r.geometry, "motor")


def test_stable_window_requires_distinct_post_fence_source_frames():
    w = StableMarkers(FrameFence("e", 10, 1_000_000_000), "g", PositionLimits())

    def sample(seq, ns, phase=359.95):
        return MarkerSample("e", seq, ns, 10, phase, "g")

    assert w.add(sample(9, 2_000_000_000), 10) is None
    assert w.add(sample(11, 1_100_000_000), 10) is None
    assert w.add(sample(12, 1_300_000_000), 10) is None
    assert w.add(sample(12, 1_400_000_000), 10) is None
    assert w.add(sample(13, 1_360_000_000, 0.01), 10) is None
    assert w.add(sample(14, 1_420_000_000), 10) is not None


def test_stable_window_rejects_nonfinite_stale_and_moving_frames():
    for phases in ((0, 1, 2), (0, float("nan"), 0)):
        w = StableMarkers(FrameFence("e", 1, 0), "g", PositionLimits())
        for seq, phase in enumerate(phases, 2):
            assert (
                w.add(MarkerSample("e", seq, seq * 1_000_000_000, 10, phase, "g"), 10)
                is None
            )
    w = StableMarkers(FrameFence("e", 1, 0), "g", PositionLimits())
    assert w.add(MarkerSample("e", 2, 1_000_000_000, 1, 0, "g"), 10) is None


@pytest.mark.parametrize("fps", [30, 60, 120])
def test_stability_does_not_depend_on_slow_polling(fps):
    w = StableMarkers(FrameFence("e", 1, 0), "g", PositionLimits())
    result = None
    for seq in range(2, fps + 2):
        now = seq / fps
        result = w.add(MarkerSample("e", seq, int(now * 1e9), now, 25, "g"), now)
        if result:
            break
    assert result is not None


def test_instability_restarts_entire_stable_interval():
    w = StableMarkers(FrameFence("e", 1, 0), "g", PositionLimits())
    for seq, phase in enumerate([20, 20, 21, 20, 20, 20], 10):
        now = seq / 30
        assert (
            w.add(MarkerSample("e", seq, int(now * 1e9), now, phase, "g"), now) is None
        )


def test_deployed_adapter_delegates_receipts_and_rejects_replaced_motor():
    class Stepper:
        _motion_lock = RLock()
        _motion_generation = 4
        software_disabled = False
        enabled = True
        stalled = False
        position = 10
        direction_inverted = False
        _microsteps = 8
        steps_per_revolution = 200

        def stationary_verified(self):
            return True

        def start_tracked_move(self, steps, speed):
            self._motion_generation += 1
            self.position += steps
            return (self._motion_generation, self.position)

        def tracked_move_complete(self, receipt):
            if receipt != (self._motion_generation, self.position):
                raise RuntimeError("interrupted")
            return True

    stepper = Stepper()
    adapter = TrackedStepperIndexMotor(
        stepper,
        SimpleNamespace(
            output_degrees_to_motor_microsteps=lambda d: round(d * 10),
            motor_steps_per_revolution=200,
            microsteps=8,
            gear_ratio=10,
        ),
    )
    token = adapter.stationary_token()
    receipt = adapter.start(36, 100, token)
    assert receipt == (5, 370) and adapter.complete(receipt)
    with pytest.raises(PositionError):
        adapter.start(36, 100, token)
    stepper._motion_generation += 1
    with pytest.raises(RuntimeError):
        adapter.complete(receipt)
    token = adapter.stationary_token()
    stepper.direction_inverted = True
    with pytest.raises(PositionError):
        adapter.check_token(token)
