"""Ledger verified native provenance suppresses the legacy count writer."""

import time

import pytest
import local_state
from runtime_stats import RuntimeStatsCollector
import smart_bins_native_completion as completion
import test_smart_bins_native_completion as completion_fixtures
from test_smart_bins_native_completion import evidence

released = completion_fixtures.released
prepared = completion_fixtures.prepared


def test_pending_verified_and_ordinary_accounting(released, monkeypatch):
    _, identity, piece = released
    calls = []
    monkeypatch.setattr(local_state, "record_piece_distribution",
                        lambda payload: calls.append(payload["uuid"]))
    stats = RuntimeStatsCollector()
    stats.setLifecycleState("running")
    common = {"uuid": piece.uuid, "native_machine_id": "m",
              "native_reservation_id": identity.reservation_id,
              "destination_bin": (0, 0, 0), "distributed_at": time.time()}
    stats.observeKnownObject(common)
    assert calls == []
    completion.record_receiving_evidence(evidence(identity))
    delivery_id = completion.complete_native(piece, "m", identity.reservation_id, "run")
    stats.observeKnownObject({"uuid": piece.uuid, "native_delivery_id": delivery_id})
    assert calls == []
    stats.observeKnownObject({"uuid": "ordinary", "destination_bin": (0, 0, 1),
                              "distributed_at": time.time()})
    assert calls == ["ordinary"]


def test_partial_update_survives_lookup_eviction_and_mutation_rejected(
    released, monkeypatch,
):
    _, identity, piece = released
    calls = []
    monkeypatch.setattr(local_state, "record_piece_distribution",
                        lambda payload: calls.append(payload["uuid"]))
    monkeypatch.setattr("runtime_stats.MAX_KNOWN_OBJECT_LOOKUP_ENTRIES", 1)
    stats = RuntimeStatsCollector()
    stats.setLifecycleState("running")
    stats.observeKnownObject({"uuid": piece.uuid, "native_machine_id": "m",
                              "native_reservation_id": identity.reservation_id})
    stats.observeKnownObject({"uuid": "evict"})
    assert stats.lookupKnownObject(piece.uuid) is None
    stats.observeKnownObject({"uuid": piece.uuid, "destination_bin": (0, 0, 0),
                              "distributed_at": time.time()})
    assert calls == []
    with pytest.raises(ValueError, match="immutable"):
        stats.observeKnownObject({"uuid": piece.uuid,
                                  "native_reservation_id": "foreign"})


def test_forged_native_fields_do_not_suppress_ordinary_writer(released, monkeypatch):
    calls = []
    monkeypatch.setattr(local_state, "record_piece_distribution",
                        lambda payload: calls.append(payload["uuid"]))
    stats = RuntimeStatsCollector()
    stats.setLifecycleState("running")
    stats.observeKnownObject({"uuid": "ordinary", "native_machine_id": "m",
                              "native_reservation_id": "made-up",
                              "destination_bin": (0, 0, 0),
                              "distributed_at": time.time()})
    assert calls == ["ordinary"]
