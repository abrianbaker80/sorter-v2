"""Only the passive one-index trace fields and unchanged confirmation path."""

import json
import math
import struct
from types import SimpleNamespace
from uuid import UUID

import cv2
import numpy as np
import pytest

from subsystems.classification_channel import marker_source
from subsystems.classification_channel.marker_positioner import (
    FrameFence, MarkerMapping, MarkerPositioner, MarkerSample, PositionError,
)


class Clock:
    value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds=0.06):
        self.value += seconds


class Rig:
    def __init__(self, diagnostic_sink=None, use_logger=False):
        self.clock = Clock()
        self.sequence = 0
        self.phase = 350.0
        self.token = 0
        self.commands = []
        self.events = []
        self.faults = []
        self.frozen = False
        self.capture_period_s = 1 / 30
        if use_logger:
            self.log_lines = []
            logger = SimpleNamespace(_log=lambda level, line: self.log_lines.append((level, line)))
            self.stepper = SimpleNamespace(_gc=SimpleNamespace(logger=logger))
        self.positioner = MarkerPositioner(
            self, self, MarkerMapping(350, 1, "g", "motor", "P0 at P6"),
            self.faults.append, clock=self.clock,
            diagnostic_sink=diagnostic_sink if diagnostic_sink is not None else
                            (None if use_logger else self.events.append),
        )

    def coordinate_identity(self):
        return "motor"

    def stationary_token(self):
        return self.token

    def check_token(self, token):
        assert token == self.token

    def fence(self):
        self.last_fence_health = {
            "epoch": "e", "sequence": self.sequence,
            "source_capture_monotonic_ns": round(self.clock() * 1e9),
            "last_frame_age_s": 0.01,
        }
        return FrameFence("e", self.sequence, round(self.clock() * 1e9))

    def sample(self):
        if not self.frozen:
            self.sequence += 1
        captured = round(self.clock() * 1e9)
        self.last_observation = {
            "epoch": "e", "sequence": self.sequence,
            "source_capture_monotonic_ns": captured,
            "retrieval_monotonic_s": self.clock(),
            "retrieval_wall_s": 1000 + self.clock(),
            "phase_deg": self.phase,
        }
        return MarkerSample("e", self.sequence, captured, self.clock(), self.phase, "g")

    def start(self, degrees, speed, token):
        self.check_token(token)
        self.commands.append((degrees, speed))
        self.token += 1
        self.phase = (self.phase + degrees) % 360
        return SimpleNamespace(motor_id=42, generation=self.token,
                               start_position=100, target_position=110)

    def complete(self, receipt):
        self.check_token(receipt.generation)
        return True

    def poll_until(self, predicate):
        for _ in range(160):
            self.clock.advance()
            result = self.positioner.poll()
            if predicate(result):
                return result
        pytest.fail("positioner did not terminate")

    def begin_index(self):
        self.positioner.begin_bind(0)
        assert self.poll_until(lambda r: r is not None).boundary == 0
        self.positioner.request_index(1, 1000)
        self.poll_until(lambda _: bool(self.commands))


def test_one_index_trace_records_source_fence_motor_receipt_and_every_post_observation():
    rig = Rig()
    rig.begin_index()
    assert rig.poll_until(lambda r: r is not None).boundary == 1
    events = rig.events
    kinds = [event["event"] for event in events]
    assert kinds.count("index_requested") == 1
    assert kinds.count("pre_motion_fit") == 1
    assert kinds.count("motor_request_start") == 1
    assert kinds.count("motor_receipt") == 1
    assert kinds.count("motor_completion_verified") == 1
    assert kinds.count("first_post_fence_frame") == 1
    assert kinds[-1] == "confirmed"
    receipt = next(e for e in events if e["event"] == "motor_receipt")
    assert (receipt["requested_degrees"], receipt["requested_steps"],
            receipt["start_position"], receipt["target_position"]) == (36, 10, 100, 110)
    complete = next(e for e in events if e["event"] == "motor_completion_verified")
    assert complete["stopped"] and complete["final_reported_step_counter"] == 110
    post_fence = next(e for e in events if e["event"] == "stopped_frame_fence" and e["stage"] == "post_motion")
    first = next(e for e in events if e["event"] == "first_post_fence_frame")
    assert kinds.index("motor_completion_verified") < events.index(post_fence) < events.index(first)
    assert first["source_capture_monotonic_ns"] > post_fence["fence"]["source_capture_monotonic_ns"] + 200_000_000
    post = [e for e in events if e["event"] == "marker_observation" and e["stage"] == "post_motion"]
    assert len(post) >= 3
    assert all({"source_sequence", "source_capture_monotonic_ns", "retrieval_monotonic_s",
                "retrieval_wall_s", "raw_phase_deg", "age_s", "window_reason",
                "history", "history_reset_reason"} <= e.keys() for e in post)
    assert post[-1]["window_reason"] == "stable_fresh_fit"
    assert len(post[-1]["history"]) >= 3
    assert rig.commands == [(36, 1000)] and not rig.faults


def test_timeout_trace_records_last_history_and_reason_without_changing_fault():
    rig = Rig()
    rig.begin_index()
    rig.frozen = True
    with pytest.raises(PositionError, match="deadline"):
        rig.poll_until(lambda r: r is not None)
    failure = rig.events[-1]
    assert failure["event"] == "failure"
    assert failure["reason"] == "marker index deadline exceeded"
    assert failure["history"] == []
    assert rig.positioner.boundary == 0 and rig.commands == [(36, 1000)]


def test_diagnostic_sink_failure_cannot_change_index_result():
    def broken_sink(_row):
        raise OSError("diagnostic storage unavailable")

    rig = Rig(diagnostic_sink=broken_sink)
    rig.begin_index()
    assert rig.poll_until(lambda result: result is not None).boundary == 1
    assert rig.commands == [(36, 1000)] and not rig.faults


def test_production_logger_path_emits_structured_rows():
    rig = Rig(use_logger=True)
    rig.begin_index()
    assert rig.poll_until(lambda result: result is not None).boundary == 1
    rows = [json.loads(line.removeprefix("[C4-POSITIONER] ")) for level, line in rig.log_lines]
    assert rows and all(level == "INFO" for level, _ in rig.log_lines)
    assert {row["event"] for row in rows} >= {
        "pre_motion_fit", "motor_receipt", "motor_completion_verified",
        "first_post_fence_frame", "marker_observation", "confirmed",
    }


@pytest.mark.parametrize("measured", [15.0, None])
def test_bridge_retains_exact_jpeg_source_identity_even_without_marker_fit(monkeypatch, measured):
    epoch = "d61cceef-69d8-40fc-83b7-33015488ada8"
    payload = b"SORTEROS-C4\x00\x01" + struct.pack(">16sQQQ", UUID(epoch).bytes, 123, 9_000_000_000, 12345678)
    segment = b"\xff\xef" + (len(payload) + 2).to_bytes(2, "big") + payload
    ok, encoded = cv2.imencode(".jpg", np.zeros((540, 960, 3), dtype=np.uint8))
    assert ok
    jpeg = encoded.tobytes()[:2] + segment + encoded.tobytes()[2:]
    health = dict(ok=True, source_identity_transport="jpeg-app15-sorteros-c4-v1",
                  height=540, width=960, last_frame_age_s=.01,
                  source_epoch=epoch, source_sequence=124,
                  source_capture_monotonic_ns=9_010_000_000, fps=30)
    monkeypatch.setattr(marker_source, "phase", lambda image, center: measured)
    source = marker_source.BridgeMarkerSource(
        "http://unused", center=(490.5, 269.5), shape=(540, 960), camera_id="c4",
        clock=lambda: 44.0,
        fetch=lambda path: json.dumps(health).encode() if path == "/health" else jpeg,
    )
    result = source.sample()
    assert (result.phase_deg if result is not None else None) == measured
    assert source.last_observation["sequence"] == 123
    assert source.last_observation["source_capture_monotonic_ns"] == 9_000_000_000
    assert source.last_observation["retrieval_monotonic_s"] == 44.0
    assert source.last_observation["phase_deg"] == measured
    assert math.isfinite(source.last_observation["retrieval_wall_s"])
