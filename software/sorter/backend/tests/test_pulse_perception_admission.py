from types import SimpleNamespace

from subsystems.feeder.pulse_perception.config import PulsePerceptionConfig
from subsystems.feeder.pulse_perception.flow import (
    CLASSIFICATION_PENDING_ADMISSION_MS,
    PulsePerceptionFeeding,
)


class _Logger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, message: str) -> None:
        self.messages.append(message)


class _Perception:
    def __init__(self) -> None:
        self.intake_occupied = False

    def secondary_zone_occupied(self, channel_id: int, *, source_channel: int) -> bool:
        assert channel_id == 4
        assert source_channel == 3
        return self.intake_occupied


def _flow() -> PulsePerceptionFeeding:
    flow = PulsePerceptionFeeding.__new__(PulsePerceptionFeeding)
    flow.gc = SimpleNamespace(logger=_Logger())
    flow.shared = SimpleNamespace(classification_ready=True)
    flow._classification_setup = True
    flow._classification_pending_until = 0.0
    flow._classification_was_ready = None
    return flow


def test_c4_intake_acknowledgement_blocks_followup_c3_release() -> None:
    flow = _flow()
    perception = _Perception()
    cfg = PulsePerceptionConfig()

    assert flow._classification_ready(cfg, perception, 10.0) is True

    perception.intake_occupied = True
    assert flow._classification_ready(cfg, perception, 10.1) is False
    expected_until = 10.1 + CLASSIFICATION_PENDING_ADMISSION_MS / 1000.0
    assert flow._classification_pending_until == expected_until

    # Even if the throat clears and the primary C4 gate still flickers open,
    # admission stays closed long enough for C4 to register the first piece.
    perception.intake_occupied = False
    assert flow._classification_ready(cfg, perception, 10.2) is False
    assert flow._classification_ready(cfg, perception, expected_until) is True


def test_c4_primary_gate_edge_is_fallback_acknowledgement() -> None:
    flow = _flow()
    perception = _Perception()
    cfg = PulsePerceptionConfig()

    assert flow._classification_ready(cfg, perception, 20.0) is True
    flow.shared.classification_ready = False
    assert flow._classification_ready(cfg, perception, 20.1) is False
    expected_until = 20.1 + CLASSIFICATION_PENDING_ADMISSION_MS / 1000.0
    assert flow._classification_pending_until == expected_until

    # A stable closed gate must not keep extending the timer; once a real C4
    # cycle completes after the fixed window, the next piece can enter at once.
    assert flow._classification_ready(cfg, perception, 22.0) is False
    assert flow._classification_pending_until == expected_until
    flow.shared.classification_ready = True
    assert flow._classification_ready(cfg, perception, 22.1) is True


def test_disabled_classification_gate_preserves_ungated_mode() -> None:
    flow = _flow()
    perception = _Perception()
    perception.intake_occupied = True
    flow.shared.classification_ready = False
    cfg = PulsePerceptionConfig(gate_ch3_on_classification_ready=False)

    assert flow._classification_ready(cfg, perception, 30.0) is True
    assert flow._classification_pending_until == 0.0
