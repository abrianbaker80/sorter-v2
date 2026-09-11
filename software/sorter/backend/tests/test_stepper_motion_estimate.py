from hardware.sorter_interface import StepperMotor


def _stepper(acceleration: int = 10_000) -> StepperMotor:
    stepper = StepperMotor.__new__(StepperMotor)
    stepper._applied_acceleration = acceleration
    stepper._default_acceleration = acceleration
    return stepper


def test_short_move_estimate_includes_acceleration_and_braking() -> None:
    # Live C3 exit pulse: the old distance/speed estimate returned 48 ms even
    # though the firmware's triangular profile takes about 193 ms.
    assert _stepper().estimateMoveStepsMs(96, max_speed=2_000) == 193


def test_long_move_estimate_includes_ramps_and_cruise() -> None:
    # Live C3 drop pulse: 1,444 steps has two ~200-step ramps plus a cruise.
    assert _stepper().estimateMoveStepsMs(1_444, max_speed=2_000) == 919


def test_zero_move_estimate_is_zero() -> None:
    assert _stepper().estimateMoveStepsMs(0, max_speed=2_000) == 0
