"""Waveshare physical-state truth with fake bus and controlled time."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hardware.waveshare_servo import ScServoBus, WaveshareServoMotor
from hardware.waveshare_bus_service import WaveshareBusService


@pytest.fixture
def servo(monkeypatch):
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(
        "hardware.waveshare_servo.time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    bus = Mock()
    bus.read_angle_limits.return_value = (100, 900)
    bus.read_position.return_value = 500
    bus.set_torque.return_value = True
    bus.move_to.return_value = True
    bus.is_moving.return_value = False
    result = WaveshareServoMotor(bus, 3)
    result.initialize()
    return result, bus, clock


@pytest.mark.parametrize(
    "raw,expected",
    [(b"\x01", True), (b"\x00", False), (None, None), (b"", None), (b"\x02", None)],
)
def test_moving_register_preserves_unknown(raw, expected):
    bus = object.__new__(ScServoBus)
    bus.read_bytes = Mock(return_value=raw)
    assert bus.is_moving(3) is expected
    bus.read_bytes.assert_called_once_with(3, 66, 1)


@pytest.mark.parametrize("value", [True, False, None])
def test_service_moving_uses_serialized_execute(value):
    service = WaveshareBusService("fake")
    bus = Mock()
    bus.is_moving.return_value = value
    service._execute = Mock(side_effect=lambda fn: fn(bus))
    assert service.is_moving(3) is value
    service._execute.assert_called_once()
    bus.is_moving.assert_called_once_with(3)


@pytest.mark.parametrize(
    "method,args",
    [("move_to", (3, 400)), ("set_torque", (3, True)), ("calibrate_servo", (3,))],
)
def test_service_refuses_motion_before_serial_lock_or_recovery(method, args):
    from smart_bins_native_custody import install, CustodyRefused

    adapter = Mock()
    adapter.check_motion.side_effect = CustodyRefused("held claim")
    service = WaveshareBusService("fake")
    service._execute = Mock()
    install(adapter)
    try:
        with pytest.raises(CustodyRefused):
            getattr(service, method)(*args)
        service._execute.assert_not_called()
        assert service.consecutive_failures == 0
        assert service.recovery_attempts == 0
    finally:
        install(None)


def test_accepted_target_is_pending_not_confirmed(servo):
    motor, bus, _ = servo
    assert motor.move_to(180)
    assert motor._pending_target == 900
    assert motor._confirmed_position == 500
    assert motor._last_command_outcome == "ACCEPTED"
    assert not motor.isClosed()
    bus.move_to.assert_called_once_with(3, 900, 300)


@pytest.mark.parametrize("method", ["open", "close", "move_to"])
def test_unconfirmed_command_invalidates_truth(servo, method):
    motor, bus, _ = servo
    bus.move_to.return_value = False
    args = (180,) if method == "move_to" else ()
    assert getattr(motor, method)(*args) is False
    assert motor._last_command_outcome == "AMBIGUOUS"
    assert motor._confirmed_position is None
    assert not motor.isOpen() and not motor.isClosed()
    if method in ("open", "close"):
        assert not motor._release_pending
        assert bus.set_torque.call_args.args == (3, False)


def test_command_exception_keeps_original_error(servo):
    motor, bus, _ = servo
    error = OSError("write outcome unknown")
    bus.move_to.side_effect = error
    with pytest.raises(OSError) as raised:
        motor.open()
    assert raised.value is error
    assert motor._last_command_outcome == "AMBIGUOUS"
    assert motor._confirmed_position is None


def test_live_position_and_missing_position(servo):
    motor, bus, _ = servo
    bus.read_position.return_value = 750
    assert motor.position == 750
    assert motor._confirmed_position == 750
    bus.read_position.return_value = None
    assert motor.position is None
    assert motor._confirmed_position is None
    assert not motor.isClosed() and not motor.isOpen()


@pytest.mark.parametrize(
    "moving,expected", [(True, False), (False, True), (None, False)]
)
def test_stopped_requires_controller_feedback(servo, moving, expected):
    motor, bus, clock = servo
    motor.close()
    bus.is_moving.return_value = moving
    bus.read_position.return_value = 900
    clock.now += 100
    assert motor.stopped is expected
    assert motor.isClosed() is (moving is False)


def test_wrong_stopped_position_keeps_pending(servo):
    motor, bus, _ = servo
    motor.close()
    bus.read_position.return_value = 700
    assert motor.stopped
    assert motor._confirmed_position == 700
    assert motor._pending_target == 900
    assert not motor.isOpen() and not motor.isClosed()


@pytest.mark.parametrize(
    "method,target,opened", [("open", 100, True), ("close", 900, False)]
)
def test_confirmed_route_feedback(servo, method, target, opened):
    motor, bus, _ = servo
    assert getattr(motor, method)()
    bus.read_position.return_value = target
    feedback = motor.feedback()
    assert feedback["position"] == target
    assert feedback["is_open"] is opened
    assert feedback["is_closed"] is (not opened)
    assert feedback["position_valid"] and feedback["motion_state_valid"]
    assert feedback["stopped"] and feedback["command_outcome"] == "ACCEPTED"
    assert feedback["pending_target"] is None


def test_feedback_failure_is_unknown(servo):
    motor, bus, _ = servo
    motor.close()
    bus.read_position.return_value = None
    feedback = motor.feedback()
    assert feedback["position"] is None and feedback["angle"] is None
    assert not feedback["position_valid"]
    assert not feedback["is_open"] and not feedback["is_closed"]
    assert feedback["available"]


def test_release_on_observed_stop(servo):
    motor, bus, clock = servo
    assert motor.move_to_and_release(180)
    bus.set_torque.reset_mock()
    bus.is_moving.return_value = None
    clock.now += 1
    assert not motor.stopped
    bus.set_torque.assert_not_called()
    bus.is_moving.return_value = False
    bus.read_position.return_value = 900
    assert motor.stopped
    bus.set_torque.assert_called_once_with(3, False)


def test_protective_timeout_release_does_not_claim_stopped(servo):
    motor, bus, clock = servo
    motor.open()
    bus.set_torque.reset_mock()
    bus.is_moving.return_value = None
    clock.now += 10
    assert not motor.stopped
    bus.set_torque.assert_called_once_with(3, False)
    assert not motor.isOpen() and not motor.isClosed()


def test_calibration_and_inversion_preserve_raw_observation(servo):
    motor, bus, _ = servo
    assert motor.is_calibrated
    motor.open()
    bus.read_position.return_value = 100
    assert motor.stopped and motor.isOpen()
    motor.set_invert(True)
    assert motor._confirmed_position == 100
    assert motor.isClosed() and not motor.isOpen()
    assert motor.open()
    bus.move_to.assert_called_with(3, 900, 300)


def test_failed_initialization_never_calibrated(servo):
    motor, bus, _ = servo
    bus.read_angle_limits.return_value = None
    with pytest.raises(RuntimeError):
        motor.initialize()
    assert not motor.is_calibrated
    assert not motor.open()


def test_recalibration_does_not_invent_center(monkeypatch, servo):
    motor, bus, _ = servo
    monkeypatch.setattr(
        "hardware.waveshare_servo.calibrate_servo", lambda *a: (200, 800)
    )
    bus.read_position.return_value = None
    assert motor.recalibrate() == (200, 800)
    assert motor.is_calibrated
    assert motor._confirmed_position is None
    assert not motor.isOpen() and not motor.isClosed()


def test_initial_position_missing_stays_unknown(servo):
    motor, bus, _ = servo
    bus.read_position.return_value = None
    motor.initialize()
    assert motor._confirmed_position is None
    assert motor._pending_target is None


def test_ack_then_unknown_feedback_cannot_reuse_previous_success(servo):
    motor, bus, _ = servo
    motor.open()
    bus.read_position.return_value = 100
    assert motor.stopped and motor.isOpen()
    bus.is_moving.return_value = None
    feedback = motor.feedback()
    assert not feedback["motion_state_valid"]
    assert not feedback["position_valid"]
    assert feedback["stopped"] is None
    assert not feedback["is_open"]
