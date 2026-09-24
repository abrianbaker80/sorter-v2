"""Tests for CaptureThread's 90-frame ring buffer used by drop-zone burst."""

import time
import unittest

import numpy as np

from irl.config import mkCameraConfig
from vision.camera import CaptureThread
from vision.types import CameraFrame


def _make_frame(marker: int) -> CameraFrame:
    # 4x4 single-pixel marker is cheap to build and keeps the ordering check trivial.
    raw = np.full((4, 4, 3), marker % 256, dtype=np.uint8)
    return CameraFrame(raw=raw, annotated=None, results=[], timestamp=time.time() + marker * 1e-3)


class CameraRingBufferTests(unittest.TestCase):
    def test_ring_buffer_caps_at_90_when_overfilled(self) -> None:
        capture = CaptureThread("test_cam", mkCameraConfig(device_index=-1))
        for i in range(100):
            capture._ring_buffer.append(_make_frame(i))
        self.assertEqual(90, len(capture._ring_buffer))
        # Oldest 10 were evicted — remaining frames start at marker 10.
        self.assertEqual(10, int(capture._ring_buffer[0].raw[0, 0, 0]))
        self.assertEqual(99, int(capture._ring_buffer[-1].raw[0, 0, 0]))

    def test_drain_ring_buffer_returns_chronological_slice(self) -> None:
        capture = CaptureThread("test_cam", mkCameraConfig(device_index=-1))
        for i in range(100):
            capture._ring_buffer.append(_make_frame(i))

        drained = capture.drain_ring_buffer(30)
        self.assertEqual(30, len(drained))
        markers = [int(f.raw[0, 0, 0]) for f in drained]
        # Should be the 30 most recent (markers 70..99) in chronological order.
        self.assertEqual(list(range(70, 100)), markers)
        # Non-destructive — the buffer still holds the same frames.
        self.assertEqual(90, len(capture._ring_buffer))

    def test_drain_returns_all_when_buffer_smaller_than_request(self) -> None:
        capture = CaptureThread("test_cam", mkCameraConfig(device_index=-1))
        for i in range(5):
            capture._ring_buffer.append(_make_frame(i))
        drained = capture.drain_ring_buffer(30)
        self.assertEqual(5, len(drained))
        self.assertEqual([0, 1, 2, 3, 4], [int(f.raw[0, 0, 0]) for f in drained])

    def test_drain_with_zero_or_negative_request_returns_empty(self) -> None:
        capture = CaptureThread("test_cam", mkCameraConfig(device_index=-1))
        capture._ring_buffer.append(_make_frame(0))
        self.assertEqual([], capture.drain_ring_buffer(0))
        self.assertEqual([], capture.drain_ring_buffer(-5))

    def test_frame_at_or_before_returns_exact_match(self) -> None:
        capture = CaptureThread("test_cam", mkCameraConfig(device_index=-1))
        frames = [_make_frame(i) for i in range(5)]
        for frame in frames:
            capture._ring_buffer.append(frame)

        target = frames[2]
        result = capture.frame_at_or_before(target.timestamp)
        self.assertIs(result, target)

    def test_frame_at_or_before_returns_closest_earlier(self) -> None:
        capture = CaptureThread("test_cam", mkCameraConfig(device_index=-1))
        frames = [_make_frame(i) for i in range(5)]
        for frame in frames:
            capture._ring_buffer.append(frame)

        # Query a timestamp between frame[2] and frame[3] — should return frame[2].
        between_ts = (frames[2].timestamp + frames[3].timestamp) / 2.0
        result = capture.frame_at_or_before(between_ts)
        self.assertIs(result, frames[2])

    def test_frame_at_or_before_returns_none_outside_tolerance(self) -> None:
        capture = CaptureThread("test_cam", mkCameraConfig(device_index=-1))
        capture._ring_buffer.append(_make_frame(0))
        # 10 s in the past — way beyond the 0.5 s default tolerance.
        result = capture.frame_at_or_before(time.time() + 10.0, tolerance_s=0.05)
        self.assertIsNone(result)

    def test_frame_at_or_before_returns_none_when_empty(self) -> None:
        capture = CaptureThread("test_cam", mkCameraConfig(device_index=-1))
        self.assertIsNone(capture.frame_at_or_before(time.time()))


if __name__ == "__main__":
    unittest.main()


def test_received_telemetry_does_not_confuse_reported_or_stale_frames(monkeypatch):
    import cv2
    from types import SimpleNamespace
    capture = CaptureThread("test_cam", mkCameraConfig(device_index=-1))
    monkeypatch.setattr("vision.camera.time.time", lambda: 100.0)
    capture._cap = SimpleNamespace(get=lambda key: {
        cv2.CAP_PROP_FRAME_WIDTH: 1920, cv2.CAP_PROP_FRAME_HEIGHT: 1440,
        cv2.CAP_PROP_FPS: 30,
    }.get(key, 0))
    capture._received_resolution = (3840, 2160)
    for stamp in (99.8, 99.9, 100.0):
        frame = _make_frame(0)
        frame.timestamp = stamp
        capture._ring_buffer.append(frame)
    snapshot = capture.getTelemetrySnapshot()
    assert snapshot["resolution"] == (3840, 2160)
    assert abs(snapshot["fps"] - 10) < 0.001
    assert snapshot["reported_fps"] == 30
    monkeypatch.setattr("vision.camera.time.time", lambda: 103.0)
    assert "resolution" not in capture.getTelemetrySnapshot()
    assert "fps" not in capture.getTelemetrySnapshot()


def test_capture_profile_uses_existing_profiler_and_received_raw_dimensions(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    capture = CaptureThread("classification_channel", mkCameraConfig(device_index=-1))
    captured_at = 1_789_990_000.25
    monkeypatch.setattr("vision.camera.time.time", lambda: captured_at)
    # A single in-memory frame exercises the production loop; no camera opens.
    capture._get_config_snapshot = lambda: ("http://camera.invalid/video", True, 1920, 1440, 24, "MJPG")
    raw = np.zeros((8, 12, 3), dtype=np.uint8)
    def grab():
        return True
    def retrieve():
        capture._stop_event.set()
        return True, raw
    cap = SimpleNamespace(
        isOpened=lambda: True,
        grab=grab,
        retrieve=retrieve,
        release=lambda: None,
    )
    monkeypatch.setattr("vision.camera._open_capture_source", lambda *a, **k: cap)
    capture.profiler = SimpleNamespace(enabled=True, observeDuration=Mock())
    capture._captureLoop()
    assert capture._received_resolution == (12, 8)
    assert len(capture._ring_buffer) == 1
    assert capture.latest_frame.timestamp == captured_at
    names = [call.args[0] for call in capture.profiler.observeDuration.call_args_list]
    assert names == [
        "camera.classification_channel.opencv_grab_ms",
        "camera.classification_channel.opencv_retrieve_ms",
        "camera.classification_channel.read_decode",
        "camera.classification_channel.settings",
        "camera.classification_channel.picture_settings_ms",
        "camera.classification_channel.color_profile_ms",
        "camera.classification_channel.transform",
        "camera.classification_channel.shared_publish_ms",
    ]
