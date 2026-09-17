from types import SimpleNamespace
from unittest.mock import Mock

from coordinator import Coordinator


def test_indexed_pause_preserves_distribution_transaction() -> None:
    coordinator = object.__new__(Coordinator)
    coordinator.feeder = SimpleNamespace(hold_motion=Mock(), cleanup=Mock())
    coordinator.classification = SimpleNamespace(
        supportsStatefulPause=lambda: True,
        pause=Mock(),
        resume=Mock(),
        cleanup=Mock(),
    )
    coordinator.distribution = SimpleNamespace(cleanup=Mock(), resume=Mock(return_value=False))

    coordinator.pause()

    coordinator.feeder.hold_motion.assert_called_once_with()
    coordinator.classification.pause.assert_called_once_with()
    coordinator.classification.cleanup.assert_not_called()
    coordinator.distribution.cleanup.assert_not_called()

    coordinator.resume()
    coordinator.distribution.resume.assert_called_once_with()
    coordinator.classification.resume.assert_called_once_with()


def test_non_indexed_pause_keeps_existing_full_cleanup() -> None:
    coordinator = object.__new__(Coordinator)
    coordinator.feeder = SimpleNamespace(hold_motion=Mock(), cleanup=Mock())
    coordinator.classification = SimpleNamespace(
        supportsStatefulPause=lambda: False,
        cleanup=Mock(),
    )
    coordinator.distribution = SimpleNamespace(cleanup=Mock())

    coordinator.pause()

    coordinator.feeder.cleanup.assert_called_once_with()
    coordinator.classification.cleanup.assert_called_once_with()
    coordinator.distribution.cleanup.assert_called_once_with()
