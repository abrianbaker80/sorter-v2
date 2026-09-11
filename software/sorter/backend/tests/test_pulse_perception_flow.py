from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from perception.cascade import Action
from perception.state import ChannelState, PieceObservation
from subsystems.feeder.pulse_perception.config import PulsePerceptionConfig
from subsystems.feeder.pulse_perception.flow import (
    C3_TO_C4_DELIVERY_TIMEOUT_S,
    PulsePerceptionFeeding,
)


class _Logger:
    def info(self, *_args, **_kwargs) -> None:
        pass

    def warning(self, *_args, **_kwargs) -> None:
        pass


def _channel_state(
    *,
    ts: float,
    n_pieces: int,
    in_exit: bool = False,
    track_id: int | None = None,
) -> ChannelState:
    pieces = ()
    if n_pieces:
        pieces = (
            PieceObservation(
                com_forward_to_exit_deg=0.0,
                com_section=10,
                zone_code=2 if in_exit else 0,
                sv_bt_track_id=track_id,
            ),
        )
    return ChannelState(
        ts=ts,
        in_drop=False,
        in_exit=in_exit,
        n_pieces=n_pieces,
        pieces=pieces,
    )


def _flow() -> PulsePerceptionFeeding:
    deliveries: list[dict] = []
    flow = PulsePerceptionFeeding.__new__(PulsePerceptionFeeding)
    flow._busy_until = {}
    flow._classification_setup = True
    flow._c3_transfer = None
    flow.gc = SimpleNamespace(logger=_Logger())
    flow.shared = SimpleNamespace(
        publish_piece_delivered=lambda **kwargs: deliveries.append(kwargs),
    )
    flow._test_deliveries = deliveries
    return flow


def test_busy_tracks_firmware_motion_after_estimated_cooldown() -> None:
    flow = _flow()
    stepper = SimpleNamespace(_name="c3", stopped=False)

    with patch(
        "subsystems.feeder.pulse_perception.flow.time.monotonic",
        return_value=100.0,
    ):
        assert flow._busy(stepper) is True
        stepper.stopped = True
        assert flow._busy(stepper) is False


def test_successful_c3_exit_pulse_opens_acknowledged_transfer() -> None:
    flow = _flow()
    flow._move = lambda *_args, **_kwargs: True
    stepper = SimpleNamespace(_name="c3", stopped=True)
    c3 = _channel_state(ts=10.0, n_pieces=1, in_exit=True, track_id=7)
    c4 = _channel_state(ts=20.0, n_pieces=0)

    with patch(
        "subsystems.feeder.pulse_perception.flow.time.monotonic",
        return_value=100.0,
    ):
        flow._apply_action(
            "ch3",
            3,
            Action.PRECISE,
            stepper,
            c3,
            PulsePerceptionConfig(),
            downstream_state=c4,
        )

    assert flow._c3_transfer is not None
    assert flow._c3_transfer.started_at == 100.0
    assert flow._c3_transfer.c4_baseline_ts == 20.0
    assert flow._c3_transfer.track_id == 7
    assert flow._test_deliveries == []


def test_pending_transfer_retries_only_same_track_on_a_new_frame() -> None:
    flow = _flow()
    flow._startC3Transfer(
        _channel_state(ts=10.0, n_pieces=1, in_exit=True, track_id=7),
        _channel_state(ts=20.0, n_pieces=0),
        100.0,
    )

    assert not flow._c3TransferRetryAllowed(
        _channel_state(ts=10.0, n_pieces=1, in_exit=True, track_id=7)
    )
    assert not flow._c3TransferRetryAllowed(
        _channel_state(ts=10.1, n_pieces=1, in_exit=True, track_id=8)
    )
    assert flow._c3TransferRetryAllowed(
        _channel_state(ts=10.1, n_pieces=1, in_exit=True, track_id=7)
    )


def test_fresh_c4_occupancy_confirms_delivery_and_releases_transfer() -> None:
    flow = _flow()
    flow._startC3Transfer(
        _channel_state(ts=10.0, n_pieces=1, in_exit=True, track_id=7),
        _channel_state(ts=20.0, n_pieces=0),
        100.0,
    )

    with patch(
        "subsystems.feeder.pulse_perception.autotune.noteDispense"
    ) as note_dispense:
        flow._observeC3Transfer(
            _channel_state(ts=20.1, n_pieces=1, track_id=12),
            100.5,
        )

    assert flow._c3_transfer is None
    assert len(flow._test_deliveries) == 1
    note_dispense.assert_called_once()


def test_unconfirmed_transfer_reports_once_and_never_timer_releases() -> None:
    flow = _flow()
    c3 = _channel_state(ts=10.0, n_pieces=1, in_exit=True, track_id=7)
    c4 = _channel_state(ts=20.0, n_pieces=0)
    flow._startC3Transfer(c3, c4, 100.0)

    with patch(
        "subsystems.feeder.pulse_perception.flow.publish_classification_intake_timeout_incident"
    ) as publish_incident:
        now = 100.0 + C3_TO_C4_DELIVERY_TIMEOUT_S + 0.1
        flow._observeC3Transfer(c4, now)
        flow._observeC3Transfer(c4, now + 1.0)

    publish_incident.assert_called_once()
    assert flow._c3_transfer is not None
    assert flow._c3_transfer.started_at == 100.0
    assert flow._c3_transfer.timeout_reported is True
    assert not flow._c3TransferRetryAllowed(replace(c3, ts=10.1))
