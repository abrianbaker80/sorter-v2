"""Read-only bridge source using JPEG capture identity, never retrieval time."""

from __future__ import annotations

import hashlib
import json
import math
import struct
import time
from urllib.request import urlopen
from uuid import UUID

from .blue_markers import phase
from .marker_positioner import FrameFence, MarkerSample, PositionError


def capture_identity(jpeg: bytes) -> FrameFence:
    """Parse the deployed SORTEROS-C4 v1 identity before decoding pixels."""
    if jpeg[:2] != b"\xff\xd8":
        raise PositionError("not a JPEG")
    offset, found = 2, None
    while offset + 4 <= len(jpeg):
        if jpeg[offset] != 255:
            raise PositionError("malformed JPEG metadata")
        marker = jpeg[offset + 1]
        if marker in (0xDA, 0xD9):
            break
        size = int.from_bytes(jpeg[offset + 2 : offset + 4], "big")
        if size < 2 or offset + 2 + size > len(jpeg):
            raise PositionError("truncated JPEG metadata")
        payload = jpeg[offset + 4 : offset + 2 + size]
        if marker == 0xEF and payload.startswith(b"SORTEROS-C4\x00"):
            if found is not None or len(payload) != 53 or payload[12] != 1:
                raise PositionError("ambiguous or unsupported source identity")
            epoch, sequence, mono_ns, _wall_ns = struct.unpack(">16sQQQ", payload[13:])
            if not sequence or not mono_ns:
                raise PositionError("invalid source identity")
            found = FrameFence(str(UUID(bytes=epoch)), sequence, mono_ns)
        offset += size + 2
    if found is None:
        raise PositionError("camera source has no capture identity")
    return found


class BridgeMarkerSource:
    def __init__(
        self,
        base_url: str,
        *,
        center: tuple[float, float],
        shape: tuple[int, int],
        camera_id: str,
        clock=time.monotonic,
        fetch=None,
    ):
        if (
            not camera_id
            or len(shape) != 2
            or any(type(v) is not int or v <= 0 for v in shape)
            or len(center) != 2
            or any(not math.isfinite(v) for v in center)
            or not 0 < center[0] < shape[1]
            or not 0 < center[1] < shape[0]
        ):
            raise ValueError(
                "explicit camera identity, dimensions, and rotor center required"
            )
        self.base_url, self.center, self.shape = base_url.rstrip("/"), center, shape
        self.clock = clock
        self.fetch = fetch or self._fetch
        self.geometry = hashlib.sha256(
            json.dumps(
                {
                    "camera": camera_id,
                    "center": center,
                    "shape": shape,
                    "detector": "blue-annulus-dashed-solid-fit-v1",
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def _fetch(self, path: str) -> bytes:
        with urlopen(self.base_url + path, timeout=0.75) as response:
            payload = response.read(8_000_001)
        if len(payload) > 8_000_000:
            raise PositionError("oversized camera response")
        return payload

    def fence(self) -> FrameFence:
        # Requested after stopped receipt verification. Later JPEGs must carry
        # a newer capture identity plus settle time on this SAME source clock.
        health = json.loads(self.fetch("/health"))
        if (
            health.get("ok") is not True
            or health.get("source_identity_transport") != "jpeg-app15-sorteros-c4-v1"
            or (health.get("height"), health.get("width")) != self.shape
            or not 0 <= float(health.get("last_frame_age_s", math.inf)) <= 0.25
        ):
            raise PositionError("camera bridge unavailable, stale, or incompatible")
        # Source cadence is metadata for startup continuity, not validation age.
        fps = float(health.get("fps", 0))
        self.capture_period_s = 1.0 / fps if math.isfinite(fps) and fps > 0 else None
        epoch = str(UUID(health["source_epoch"]))
        sequence, captured = (
            health["source_sequence"],
            health["source_capture_monotonic_ns"],
        )
        if (
            type(sequence) is not int
            or type(captured) is not int
            or sequence <= 0
            or captured <= 0
        ):
            raise PositionError("invalid camera fence")
        # Passive evidence only: retain the source capture behind this fence.
        self.last_fence_health = {
            "epoch": epoch,
            "sequence": sequence,
            "source_capture_monotonic_ns": captured,
            "last_frame_age_s": float(health["last_frame_age_s"]),
        }
        # The most recent capture may precede the health request by up to the
        # admitted age. Fence that entire budget before the settle interval.
        return FrameFence(epoch, sequence, captured + 250_000_000)

    def sample(self) -> MarkerSample | None:
        import cv2
        import numpy as np

        self.last_observation = None
        jpeg = self.fetch("/snapshot.jpg")
        identity = capture_identity(jpeg)
        received = self.clock()
        received_wall = time.time()
        latest = self.fence()
        source_age_ns = latest.captured_ns - 250_000_000 - identity.captured_ns
        if (
            latest.epoch != identity.epoch
            or latest.sequence < identity.sequence
            or not 0 <= source_age_ns <= 250_000_000
        ):
            raise PositionError(
                "snapshot is not a fresh capture from the current source"
            )
        image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.shape[:2] != self.shape:
            raise PositionError("camera image geometry changed")
        measured = phase(image, self.center)
        # Preserve identity even when no marker fit is returned. This is never
        # used for source validation or marker control decisions.
        self.last_observation = {
            "epoch": identity.epoch,
            "sequence": identity.sequence,
            "source_capture_monotonic_ns": identity.captured_ns,
            "retrieval_monotonic_s": received,
            "retrieval_wall_s": received_wall,
            "phase_deg": measured,
        }
        if measured is None:
            return None
        return MarkerSample(
            identity.epoch,
            identity.sequence,
            identity.captured_ns,
            received,
            measured,
            self.geometry,
        )
