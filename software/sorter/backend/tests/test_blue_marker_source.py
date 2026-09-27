import json
import math
import struct
from types import SimpleNamespace
from uuid import UUID

import cv2
import numpy as np
import pytest

from subsystems.classification_channel.blue_markers import phase
from subsystems.classification_channel import marker_source
from subsystems.classification_channel.marker_positioner import (
    FrameFence, PositionError, PositionLimits, SourceContinuityMarkers, error_deg,
)
from subsystems.classification_channel.marker_source import (
    BridgeMarkerSource,
    capture_identity,
)

CENTER = (490.5, 269.5)
EPOCH = "d61cceef-69d8-40fc-83b7-33015488ada8"


def frame(angle=0, missing=(), duplicate=False):
    image = np.full((540, 960, 3), 170, dtype=np.uint8)
    for index in range(10):
        if index in missing:
            continue
        theta = math.radians(angle + index * 36)

        def point(radius):
            return tuple(
                round(v)
                for v in (
                    CENTER[0] + radius * math.cos(theta),
                    CENTER[1] + radius * math.sin(theta),
                )
            )

        spans = (
            [(55, 68), (91, 105)]
            if index == 0 or (duplicate and index == 2)
            else [(55, 105)]
        )
        for low, high in spans:
            cv2.line(image, point(low), point(high), (220, 145, 30), 5)
    return image


def identity_segment(sequence=123, mono=9_000_000_000, epoch=EPOCH):
    data = b"SORTEROS-C4\x00\x01" + struct.pack(
        ">16sQQQ", UUID(epoch).bytes, sequence, mono, 12345678
    )
    return b"\xff\xef" + (len(data) + 2).to_bytes(2, "big") + data


def jpeg(image=None, *, sequence=123, mono=9_000_000_000, epoch=EPOCH):
    ok, encoded = cv2.imencode(".jpg", frame() if image is None else image)
    assert ok
    data = encoded.tobytes()
    return data[:2] + identity_segment(sequence, mono, epoch) + data[2:]


@pytest.mark.parametrize(
    "angle", [0, 0.2, 35.9, 36, 72, 144, 180, 212, 288, 324, 359.9]
)
@pytest.mark.parametrize("missing", [(), (2,), (2, 7)])
def test_absolute_phase_uses_dashed_identity_not_command_or_lego(angle, missing):
    measured = phase(frame(angle, missing), CENTER)
    assert measured is not None and abs(error_deg(measured, angle)) < 0.3


@pytest.mark.parametrize(
    "missing,duplicate", [((0,), False), ((1, 2, 3), False), ((), True)]
)
def test_missing_or_ambiguous_reference_has_no_phase(missing, duplicate):
    assert phase(frame(missing=missing, duplicate=duplicate), CENTER) is None


def test_jpeg_identity_is_the_identity_of_exact_encoded_pixels():
    result = capture_identity(jpeg())
    assert (
        result.epoch == EPOCH
        and result.sequence == 123
        and result.captured_ns == 9_000_000_000
    )


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not a jpeg",
        b"\xff\xd8\xff\xd9",
        b"\xff\xd8\xff\xef\xff\xff",
        b"\xff\xd8" + identity_segment(0),
        b"\xff\xd8" + identity_segment() * 2 + b"\xff\xda\x00\x02",
    ],
)
def test_unidentified_malformed_or_ambiguous_jpeg_fails_closed(data):
    with pytest.raises(PositionError):
        capture_identity(data)


def health():
    return dict(
        ok=True,
        source_identity_transport="jpeg-app15-sorteros-c4-v1",
        height=540,
        width=960,
        last_frame_age_s=0.01,
        source_epoch=EPOCH,
        source_sequence=100,
        source_capture_monotonic_ns=8_000_000_000,
    )


def test_source_fence_accounts_for_maximum_age_and_preserves_capture_clock():
    data = health()
    data.update(source_sequence=125, source_capture_monotonic_ns=9_050_000_000)
    source = BridgeMarkerSource(
        "http://camera",
        center=CENTER,
        shape=(540, 960),
        camera_id="c4",
        fetch=lambda path: json.dumps(data).encode() if path == "/health" else jpeg(),
        clock=lambda: 44,
    )
    fence = source.fence()
    assert fence.captured_ns == 9_300_000_000
    sample = source.sample()
    assert sample.captured_ns == 9_000_000_000 and sample.received_mono == 44
    assert sample.sequence == 123 and sample.geometry == source.geometry


def test_sample_timing_retains_retrieval_and_records_completed_sample_time():
    data = health()
    data.update(source_sequence=125, source_capture_monotonic_ns=9_050_000_000)
    source = BridgeMarkerSource(
        "http://camera",
        center=CENTER,
        shape=(540, 960),
        camera_id="c4",
        fetch=lambda path: json.dumps(data).encode() if path == "/health" else jpeg(),
    )
    sample = source.sample()
    timing = source.last_sample_timing
    assert sample.received_mono == timing["received_mono"] == timing["sample_completed_monotonic_s"]
    assert timing["phase_fit_end_monotonic_s"] <= sample.received_mono <= timing["sample_return_monotonic_s"]
    assert timing["snapshot_retrieval_complete_monotonic_s"] <= sample.retrieved_mono
    assert sample.retrieved_mono == source.last_observation["retrieval_monotonic_s"]
    assert sample.retrieved_mono <= timing["health_request_start_monotonic_s"]
    assert source.last_observation["sample_completed_monotonic_s"] == sample.received_mono
    assert (timing["source_epoch"], timing["source_sequence"], timing["source_capture_monotonic_ns"]) == (
        sample.epoch, sample.sequence, sample.captured_ns,
    )
    for start, end, duration in (
        ("snapshot_request_start_monotonic_s", "snapshot_retrieval_complete_monotonic_s", "snapshot_fetch_duration_s"),
        ("health_request_start_monotonic_s", "health_response_complete_monotonic_s", "health_fetch_duration_s"),
        ("jpeg_decode_start_monotonic_s", "jpeg_decode_end_monotonic_s", "jpeg_decode_duration_s"),
        ("phase_fit_start_monotonic_s", "phase_fit_end_monotonic_s", "phase_fit_duration_s"),
        ("sample_start_monotonic_s", "sample_return_monotonic_s", "total_sample_duration_s"),
    ):
        assert timing[start] <= timing[end]
        assert timing[duration] == pytest.approx(timing[end] - timing[start])
    assert timing["snapshot_retrieval_complete_monotonic_s"] <= timing["health_request_start_monotonic_s"]
    assert timing["health_response_complete_monotonic_s"] <= timing["jpeg_decode_start_monotonic_s"]
    assert timing["jpeg_decode_end_monotonic_s"] <= timing["phase_fit_start_monotonic_s"]
    assert timing["phase_fit_end_monotonic_s"] <= timing["sample_return_monotonic_s"]


def test_slow_source_processing_is_fresh_on_completion_but_stale_after_real_wait(monkeypatch):
    class Clock:
        value = 100.0

        def __call__(self):
            return self.value

        def advance(self, seconds):
            self.value += seconds

    clock = Clock()
    monkeypatch.setattr(marker_source, "time", SimpleNamespace(
        monotonic=clock, time=lambda: 1_000_000.0 + clock(),
    ))

    def slow_phase(_image, _center):
        clock.advance(0.30)
        return 15.75

    monkeypatch.setattr(marker_source, "phase", slow_phase)
    sequences = (123, 141, 159)
    captures = (9_000_000_000, 9_600_000_000, 10_200_000_000)
    frames = [jpeg(sequence=seq, mono=cap) for seq, cap in zip(sequences, captures)]
    index = 0

    def fetch(path):
        nonlocal index
        if path == "/snapshot.jpg":
            clock.advance(0.01)
            return frames[index]
        assert path == "/health"
        data = health()
        data.update(source_sequence=sequences[index] + 1,
                    source_capture_monotonic_ns=captures[index] + 50_000_000)
        clock.advance(0.25)
        index += 1
        return json.dumps(data).encode()

    source = BridgeMarkerSource(
        "http://camera", center=CENTER, shape=(540, 960), camera_id="c4",
        clock=clock, fetch=fetch,
    )
    samples = []
    for _ in sequences:
        sample = source.sample()
        timing = source.last_sample_timing
        assert sample is not None
        assert sample.received_mono == clock()
        assert sample.received_mono - timing["snapshot_retrieval_complete_monotonic_s"] > 0.5
        assert timing["health_fetch_duration_s"] == pytest.approx(0.25)
        assert timing["phase_fit_duration_s"] == pytest.approx(0.30)
        assert timing["total_sample_duration_s"] > 0.5
        assert sample.retrieved_mono == source.last_observation["retrieval_monotonic_s"]
        assert timing["snapshot_retrieval_complete_monotonic_s"] <= sample.retrieved_mono
        assert timing["source_identity_validation"]["rejection_conditions"] == []
        samples.append(sample)

    def window():
        return SourceContinuityMarkers(
            FrameFence(EPOCH, 122, 8_500_000_000), source.geometry,
            PositionLimits(), capture_period_s=1 / 30,
        )

    fresh = window()
    assert fresh.add(samples[0], samples[0].received_mono) is None
    assert fresh.add(samples[1], samples[1].received_mono) is None
    confirmed = fresh.add(samples[2], samples[2].received_mono)
    assert confirmed is not None and confirmed.phase_deg == pytest.approx(15.75)
    assert fresh.last_reason == "stable_fresh_fit"

    delayed = window()
    assert delayed.add(samples[0], samples[0].received_mono) is None
    assert delayed.add(samples[1], samples[1].received_mono) is None
    assert delayed.add(samples[2], samples[2].received_mono + 0.501) is None
    assert delayed.last_reason == "stale_newest_history_retained"
    assert len(delayed.samples) == 3


@pytest.mark.parametrize(
    "field,value",
    [
        ("last_frame_age_s", 0.3),
        ("last_frame_age_s", float("nan")),
        ("width", 3840),
        ("ok", False),
        ("source_sequence", True),
        ("source_identity_transport", "retrieval"),
    ],
)
def test_source_rejects_stale_or_incompatible_bridge(field, value):
    data = health()
    data[field] = value
    source = BridgeMarkerSource(
        "http://camera",
        center=CENTER,
        shape=(540, 960),
        camera_id="c4",
        fetch=lambda path: json.dumps(data).encode(),
    )
    with pytest.raises(PositionError):
        source.fence()


def test_sample_rejects_old_jpeg_even_with_new_http_retrieval_time():
    data = health()
    data.update(source_sequence=500, source_capture_monotonic_ns=11_000_000_000)
    source = BridgeMarkerSource(
        "http://camera",
        center=CENTER,
        shape=(540, 960),
        camera_id="c4",
        fetch=lambda path: json.dumps(data).encode() if path == "/health" else jpeg(),
    )
    with pytest.raises(PositionError, match="fresh capture"):
        source.sample()


@pytest.mark.parametrize(
    "health_epoch,health_sequence,health_capture_ns,expected_conditions,accepted",
    [
        ("a61cceef-69d8-40fc-83b7-33015488ada8", 125, 9_050_000_000, ["epoch_mismatch"], False),
        (EPOCH, 122, 9_050_000_000, ["health_sequence_behind_snapshot"], False),
        (EPOCH, 125, 8_950_000_000, ["health_capture_timestamp_behind_snapshot"], False),
        (EPOCH, 125, 9_300_000_000, ["source_age_exceeds_250ms"], False),
        (EPOCH, 125, 9_050_000_000, [], True),
    ],
)
def test_sample_identity_diagnostics_distinguish_each_predicate_without_changing_decision(
    health_epoch, health_sequence, health_capture_ns, expected_conditions, accepted
):
    data = health()
    data.update(
        source_epoch=health_epoch,
        source_sequence=health_sequence,
        source_capture_monotonic_ns=health_capture_ns,
    )
    calls, events = [], []

    def fetch(path):
        calls.append(path)
        return json.dumps(data).encode() if path == "/health" else jpeg()

    source = BridgeMarkerSource(
        "http://camera", center=CENTER, shape=(540, 960), camera_id="c4",
        fetch=fetch, clock=lambda: 44,
    )
    source.identity_diagnostic_sink = events.append
    if accepted:
        sample = source.sample()
        assert sample is not None and sample.received_mono == 44
    else:
        with pytest.raises(PositionError, match="snapshot is not a fresh capture from the current source"):
            source.sample()
        assert "received_mono" not in source.last_sample_timing
    assert calls == ["/snapshot.jpg", "/health"]
    assert len(events) == 1
    evidence = events[0]
    assert source.last_identity_validation is evidence
    assert source.last_sample_timing["source_identity_validation"] is evidence
    assert evidence["event"] == "source_identity_validation"
    assert evidence["snapshot_request_start_monotonic_s"] <= evidence["snapshot_retrieval_complete_monotonic_s"]
    assert evidence["snapshot_retrieval_complete_monotonic_s"] <= evidence["health_request_start_monotonic_s"]
    assert evidence["health_request_start_monotonic_s"] <= evidence["health_response_complete_monotonic_s"]
    assert (evidence["snapshot_epoch"], evidence["snapshot_sequence"],
            evidence["snapshot_source_capture_monotonic_ns"]) == (EPOCH, 123, 9_000_000_000)
    assert (evidence["health_epoch"], evidence["health_sequence"],
            evidence["health_source_capture_monotonic_ns"], evidence["health_last_frame_age_s"]) == (
                health_epoch, health_sequence, health_capture_ns, 0.01,
            )
    assert evidence["epoch_match"] is (health_epoch == EPOCH)
    assert evidence["health_sequence_minus_snapshot_sequence"] == health_sequence - 123
    assert evidence["health_capture_ns_minus_snapshot_capture_ns"] == health_capture_ns - 9_000_000_000
    assert evidence["accepted_source_age_range_ns"] == [0, 250_000_000]
    assert evidence["rejection_conditions"] == expected_conditions


def test_sample_identity_diagnostic_sink_failure_does_not_change_validation():
    data = health()
    data.update(source_sequence=125, source_capture_monotonic_ns=9_050_000_000)
    source = BridgeMarkerSource(
        "http://camera", center=CENTER, shape=(540, 960), camera_id="c4",
        fetch=lambda path: json.dumps(data).encode() if path == "/health" else jpeg(),
    )

    def broken_sink(_row):
        raise RuntimeError("diagnostic sink unavailable")

    source.identity_diagnostic_sink = broken_sink
    assert source.sample() is not None
    data["source_sequence"] = 122
    with pytest.raises(PositionError, match="fresh capture"):
        source.sample()
