from types import SimpleNamespace
from unittest.mock import Mock

from stepper_stall_monitor import StepperStallMonitor


class _Interface:
    def __init__(self, stepper) -> None:
        self.steppers = [stepper]

    def get_stall_status(self) -> int:
        return 1


def test_expected_chute_home_contact_is_not_reported_as_a_machine_stall() -> None:
    stepper = SimpleNamespace(
        channel=0,
        name="chute",
        stalled=False,
        stallguard_enabled=True,
        stallguard_sgthrs=86,
    )
    chute = SimpleNamespace(stepper=stepper, homing=True, homed=False)
    irl = SimpleNamespace(interfaces={"board": _Interface(stepper)}, chute=chute)
    gc = SimpleNamespace(logger=Mock())
    monitor = StepperStallMonitor(gc)
    monitor._request_pause_if_running = Mock()
    monitor._invalidate_home = Mock()
    monitor._controller_live = Mock(return_value=True)
    monitor._sync_incident = Mock()

    monitor.poll(irl)

    assert stepper.stalled is True
    assert monitor._prev_stalled == set()
    monitor._request_pause_if_running.assert_not_called()
    monitor._invalidate_home.assert_not_called()

    chute.homing = False
    monitor.poll(irl)

    assert monitor._prev_stalled == {"chute"}
    monitor._request_pause_if_running.assert_called_once()
    monitor._invalidate_home.assert_called_once_with(irl, {"chute"})
