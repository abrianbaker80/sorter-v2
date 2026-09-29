"""PCA command truth, finite receipts, and cross-backend flap paths."""

from types import SimpleNamespace
from unittest.mock import Mock
import struct

import pytest

from hardware.sorter_interface import (
    ServoMotor,
    StepperMotor,
    InterfaceCommandCode as C,
)
from hardware.waveshare_servo import WaveshareServoMotor
from subsystems.distribution.flap_path import qualify
from smart_bins_native_custody import CustodyRefused, install


@pytest.fixture(autouse=True)
def reset_fence():
    install(None)
    yield
    install(None)


class Device:
    def __init__(self):
        self.outcome = b"\x01"
        self.moves = []
        self.moving = False
        self.position = 900
        self.error = None

    def send_command(self, command, channel, payload):
        if command in (
            C.SERVO_MOVE_TO,
            C.SERVO_MOVE_TO_AND_RELEASE,
            C.STEPPER_MOVE_STEPS,
        ):
            self.moves.append((command, channel, payload))
            if self.error:
                raise self.error
            return SimpleNamespace(payload=self.outcome)
        if command == C.SERVO_IS_STOPPED:
            return SimpleNamespace(payload=bytes([not self.moving]))
        if command == C.SERVO_GET_POSITION:
            return SimpleNamespace(payload=struct.pack("<H", self.position))
        return SimpleNamespace(payload=b"\x01")


def pca():
    dev = Device()
    servo = ServoMotor(dev, 0, SimpleNamespace(logger=Mock()))
    servo.set_preset_angles(90, 0)
    return servo, dev


def test_rejection_does_not_become_confirmed_target():
    servo, dev = pca()
    dev.outcome = b"\x00"
    assert servo.open() is False
    assert servo._current_angle is None
    assert not servo.isOpen()
    assert servo.feedback()["command_outcome"] == "REJECTED"
    assert not servo.isOpen()


def test_receipt_takes_ownership_before_finite_lock():
    from smart_bins_native_custody import MOTION_LOCK

    motor = StepperMotor(Device(), 0, SimpleNamespace(logger=Mock()))

    # Nested move_steps also uses the local lock, so provide a reusable context.
    class Lock:
        def __enter__(self):
            assert MOTION_LOCK._is_owned()

        def __exit__(self, *args):
            pass

    motor._finite_lock = Lock()
    assert motor.move_steps_receipt(100).outcome == "ACCEPTED"


def test_pca_accepted_pending_then_controller_confirmation():
    servo, dev = pca()
    assert servo.open()
    assert servo._current_angle is None and not servo.isOpen()
    dev.moving = True
    assert not servo.stopped and not servo.isOpen()
    dev.moving = False
    assert servo.stopped and servo.isOpen()
    assert not servo.isClosed()


def test_pca_ambiguity_invalidates_prior_confirmation():
    servo, dev = pca()
    servo.open()
    assert servo.stopped and servo.isOpen()
    dev.error = OSError("unknown write outcome")
    with pytest.raises(OSError):
        servo.close()
    assert servo._current_angle is None
    assert not servo.isOpen() and not servo.isClosed()
    assert servo._command_outcome == "AMBIGUOUS"


@pytest.mark.parametrize(
    "payload,outcome",
    [
        (b"\x01", "ACCEPTED"),
        (b"\x00", "REJECTED"),
        (b"", "AMBIGUOUS"),
        (b"\x02", "AMBIGUOUS"),
    ],
)
def test_stepper_receipt_generation_and_bool_compatibility(payload, outcome):
    dev = Device()
    stepper = StepperMotor(dev, 0, SimpleNamespace(logger=Mock()))
    dev.outcome = payload
    first = stepper.move_steps_receipt(100)
    second = stepper.move_steps_receipt(200)
    assert second.generation == first.generation + 1
    assert first.outcome == outcome and second.outcome == outcome
    assert first.encoder_present is False and first.proves_piece_displacement is False
    assert len(dev.moves) == 2
    if outcome != "AMBIGUOUS":
        assert stepper.move_steps(10) is (outcome == "ACCEPTED")


def test_stepper_io_exception_is_one_attempt():
    dev = Device()
    dev.error = OSError("ambiguous")
    stepper = StepperMotor(dev, 0, SimpleNamespace(logger=Mock()))
    assert stepper.move_steps_receipt(100).outcome == "AMBIGUOUS"
    assert len(dev.moves) == 1


def test_full_path_both_backends():
    servo, dev = pca()
    servo.close()
    dev.position = 0
    bus = Mock()
    bus.read_angle_limits.return_value = (100, 900)
    bus.read_position.return_value = 100
    bus.is_moving.return_value = False
    bus.move_to.return_value = True
    bus.set_torque.return_value = True
    wave = WaveshareServoMotor(bus, 2)
    wave.initialize()
    wave.open()
    result = qualify(
        [servo, wave], layer_count=2, target_layer=0, commands={0: True, 1: True}
    )
    assert len(result["flaps"]) == 2 and result["receiving_evidence"] is False


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "offline",
        "uncalibrated",
        "rejected",
        "ambiguous",
        "moving",
        "wrong_position",
        "feedback",
    ],
)
def test_full_path_fails_closed(failure):
    servo, dev = pca()
    servo.open()
    commands = {0: True}
    if failure == "missing":
        servos = []
    else:
        servos = [servo]
    if failure == "offline":
        servo = Mock(available=False)
        servos = [servo]
    if failure == "uncalibrated":
        servo.set_preset_angles(None, None)
    if failure == "rejected":
        commands[0] = False
    if failure == "ambiguous":
        servo._command_outcome = "AMBIGUOUS"
    if failure == "moving":
        dev.moving = True
    if failure == "wrong_position":
        dev.position = 500
    if failure == "feedback":
        servo.feedback = lambda: {"position_valid": False}
    with pytest.raises(CustodyRefused):
        qualify(servos, layer_count=1, target_layer=None, commands=commands)
