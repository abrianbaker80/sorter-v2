"""Canonical native identity is carried without a second history write."""

from types import SimpleNamespace

import pytest
from defs.events import KnownObjectData
import piece_records
from run_recorder import RunRecorder
import smart_bins_native_completion as completion
import test_smart_bins_native_completion as completion_fixtures
from test_smart_bins_native_completion import evidence
from utils.event import knownObjectToEvent

released = completion_fixtures.released
prepared = completion_fixtures.prepared


def test_native_recorder_adopts_one_verified_history_without_upsert(
    released, monkeypatch,
):
    _, identity, piece = released
    completion.record_receiving_evidence(evidence(identity))
    delivery_id = completion.complete_native(piece, "m", identity.reservation_id, "run")
    piece.native_delivery_id = delivery_id
    gc = SimpleNamespace(run_id="run", machine_id="m",
                         sorting_profile_path=None, logger=SimpleNamespace())
    recorder = RunRecorder(gc)
    def forbidden(*args, **kwargs):
        raise AssertionError("second piece-record write")
    monkeypatch.setattr(piece_records, "recordPiece", forbidden)
    recorder.recordCommittedNativePiece(piece, identity.reservation_id, delivery_id)
    recorder.recordCommittedNativePiece(piece, identity.reservation_id, delivery_id)
    assert recorder.pieces == [piece]
    with pytest.raises(completion.NativeCompletionRefused, match="Foreign"):
        recorder.recordCommittedNativePiece(piece, identity.reservation_id, "other")
    foreign_run = RunRecorder(SimpleNamespace(
        run_id="another-run", machine_id="m", sorting_profile_path=None,
        logger=SimpleNamespace(),
    ))
    with pytest.raises(completion.NativeCompletionRefused, match="Foreign native run"):
        foreign_run.recordCommittedNativePiece(piece, identity.reservation_id, delivery_id)


def test_native_ids_serialize_and_old_event_shape_remains_valid(released):
    _, identity, piece = released
    event = knownObjectToEvent(piece)
    data = event.data.model_dump()
    assert data["native_machine_id"] == "m"
    assert data["native_reservation_id"] == identity.reservation_id
    assert data["native_delivery_id"] is None
    old = {key: value for key, value in data.items() if not key.startswith("native_")}
    restored = KnownObjectData.model_validate(old)
    assert restored.native_machine_id is None
    assert restored.native_reservation_id is None
    assert restored.native_delivery_id is None
