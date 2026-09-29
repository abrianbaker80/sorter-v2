"""Native Sending cannot turn settle time into received-piece accounting."""

import queue
import time
from piece_transport import ClassificationChannelTransport
from subsystems.distribution.sending import CHUTE_SETTLE_MS, Sending
from subsystems.distribution.states import DistributionState
from subsystems.shared_variables import SharedVariables
from test_distribution_sending import _GlobalConfig
import test_smart_bins_native_completion as completion_fixtures
from test_smart_bins_native_completion import evidence
import smart_bins_native_completion as completion

released = completion_fixtures.released
prepared = completion_fixtures.prepared


class Recorder:
    def __init__(self):
        self.native = []
        self.ordinary = []

    def recordPiece(self, piece):
        self.ordinary.append(piece.uuid)

    def recordCommittedNativePiece(self, piece, reservation_id, delivery_id):
        self.native.append((piece.uuid, reservation_id, delivery_id))


class Tracker:
    def __init__(self):
        self.calls = []

    def record(self, part_id, color_id, category_id):
        self.calls.append((part_id, color_id, category_id))


class Adapter:
    def __init__(self):
        self.calls = []

    def release_completed(self, piece_uuid, reservation_id, delivery_id):
        self.calls.append((piece_uuid, reservation_id, delivery_id))


def setup_sending(piece):
    gc = _GlobalConfig()
    gc.machine_id = "m"
    gc.run_id = "run"
    gc.run_recorder = Recorder()
    gc.set_progress_tracker = Tracker()
    gc.smart_bins_native_adapter = Adapter()
    transport = ClassificationChannelTransport()
    transport._exit_piece = piece
    shared = SharedVariables(gc=gc)
    shared.transport = transport
    shared.set_distribution_gate(False, reason="test")
    events = queue.Queue()
    sending = Sending(object(), gc, shared, events)
    sending.start_time = time.time() - CHUTE_SETTLE_MS / 1000 - 1
    return sending, gc, shared, events


def test_pending_native_claim_has_no_accounting_or_gate(released):
    _, identity, piece = released
    piece.destination_bin = (0, 0, 0)  # intended route before Sending
    sending, gc, shared, events = setup_sending(piece)
    assert sending.step() is None
    assert sending.step() is None
    assert gc.run_recorder.native == gc.run_recorder.ordinary == []
    assert gc.set_progress_tracker.calls == []
    assert not shared.get_distribution_ready()
    assert piece.native_delivery_id is None
    assert piece.distributed_at is None
    assert piece.destination_bin is None
    assert events.qsize() == 1
    assert identity.reservation_id == piece.native_reservation_id


def test_qualified_native_sending_publishes_once(released, monkeypatch):
    _, identity, piece = released
    sending, gc, shared, events = setup_sending(piece)
    assert sending.step() is None
    completion.record_receiving_evidence(evidence(identity))
    sending._native_next_check_at = 0.0
    # A run without a progress tracker still closes the explicit effects.
    monkeypatch.setattr(gc, "set_progress_tracker", None)
    assert sending.step() == DistributionState.IDLE
    assert sending.step() == DistributionState.IDLE
    assert piece.native_delivery_id is not None
    assert gc.run_recorder.ordinary == []
    assert len(gc.run_recorder.native) == 1
    assert gc.set_progress_tracker is None
    assert len(gc.smart_bins_native_adapter.calls) == 1
    assert shared.get_distribution_ready()
    assert events.qsize() == 2  # one truthful pending event, one completion
    for effect in completion.EFFECTS:
        assert completion.effect_phase("m", identity.reservation_id, piece.uuid,
                                       piece.native_delivery_id, effect) == "SUCCEEDED"
