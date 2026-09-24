import json
import math
import struct
from uuid import UUID

import cv2
import numpy as np
import pytest

from subsystems.classification_channel.blue_markers import phase
from subsystems.classification_channel.marker_positioner import PositionError, error_deg
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


def identity_segment(sequence=123, mono=9_000_000_000):
    data = b"SORTEROS-C4\x00\x01" + struct.pack(
        ">16sQQQ", UUID(EPOCH).bytes, sequence, mono, 12345678
    )
    return b"\xff\xef" + (len(data) + 2).to_bytes(2, "big") + data


def jpeg(image=None):
    ok, encoded = cv2.imencode(".jpg", frame() if image is None else image)
    assert ok
    data = encoded.tobytes()
    return data[:2] + identity_segment() + data[2:]


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
