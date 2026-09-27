#!/usr/bin/env python3
"""Zero-transcode V4L2 MJPEG bridge for a SorterOS network camera.

The camera's native MJPEG buffers are forwarded unchanged.  No OpenCV decode,
resize, or JPEG encode occurs on the Raspberry Pi.  The HTTP control endpoints
let SorterOS use the same UVC settings, capture-mode, and calibration UI that it
uses for cameras connected directly to the sorter host.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import fcntl
import json
import logging
import os
import re
import selectors
import socket
import struct
import subprocess
import threading
import time
from uuid import UUID, uuid4
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from zeroconf import IPVersion, ServiceInfo, Zeroconf


LOG = logging.getLogger("sorter-camera-bridge")

CAMERA_INDEX = int(os.environ.get("SORTER_CAMERA_INDEX", "0"))
CAMERA_DEVICE = os.environ.get("SORTER_CAMERA_DEVICE", f"/dev/video{CAMERA_INDEX}")
DEFAULT_WIDTH = int(os.environ.get("SORTER_CAMERA_WIDTH", "3840"))
DEFAULT_HEIGHT = int(os.environ.get("SORTER_CAMERA_HEIGHT", "2160"))
DEFAULT_FPS = int(os.environ.get("SORTER_CAMERA_FPS", "30"))
HOST = os.environ.get("SORTER_CAMERA_HOST", "0.0.0.0")
PORT = int(os.environ.get("SORTER_CAMERA_PORT", "18082"))
ADVERTISE_IP = os.environ.get("SORTER_CAMERA_ADVERTISE_IP", "10.55.0.2")
CAMERA_NAME = os.environ.get("SORTER_CAMERA_NAME", "Raspberry Pi Insta360")
CAMERA_ID = os.environ.get("SORTER_CAMERA_ID", "rpi-insta360")
STATE_DIR = Path(os.environ.get("SORTER_CAMERA_STATE_DIR", "/var/lib/sorter-camera-bridge"))
STATE_PATH = STATE_DIR / "state.json"

JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"
MAX_JPEG_BYTES = 16 * 1024 * 1024
SOURCE_MARKER_MAGIC = b"SORTEROS-C4\x00"
SOURCE_MARKER_VERSION = 1
SOURCE_MARKER_APP15 = b"\xff\xef"
SOURCE_MARKER_STRUCT = struct.Struct(">B16sQQQ")
SIOCGIFFLAGS = 0x8913
SIOCGIFADDR = 0x8915
IFF_UP = 0x1
IFF_LOOPBACK = 0x8
IFF_RUNNING = 0x40


def add_source_frame_marker(
    jpeg: bytes,
    *,
    source_epoch: UUID,
    source_sequence: int,
    capture_monotonic_ns: int,
    capture_wall_time_ns: int,
) -> bytes:
    """Insert a small APP15 identity marker without decoding or re-encoding.

    APP15 metadata is ignored by normal JPEG decoders.  Putting the source
    epoch, sequence, and capture clocks inside the JPEG keeps the identity
    attached after multipartdemux, which does not preserve custom HTTP part
    headers in the GStreamer 1.20 stack used by SorterOS.
    """
    if len(jpeg) < 4 or not jpeg.startswith(JPEG_SOI) or not jpeg.endswith(JPEG_EOI):
        raise ValueError("cannot tag an incomplete JPEG")
    if source_sequence < 0 or capture_monotonic_ns < 0 or capture_wall_time_ns < 0:
        raise ValueError("source identity values must be nonnegative")
    payload = SOURCE_MARKER_MAGIC + SOURCE_MARKER_STRUCT.pack(
        SOURCE_MARKER_VERSION,
        source_epoch.bytes,
        int(source_sequence),
        int(capture_monotonic_ns),
        int(capture_wall_time_ns),
    )
    segment_length = len(payload) + 2
    return (
        JPEG_SOI
        + SOURCE_MARKER_APP15
        + segment_length.to_bytes(2, "big")
        + payload
        + jpeg[len(JPEG_SOI) :]
    )


@dataclass(frozen=True, slots=True)
class SourceFramePacket:
    jpeg: bytes
    source_epoch: str
    source_sequence: int
    capture_monotonic_ns: int
    capture_wall_time_ns: int


CONTROL_SPECS: dict[str, dict[str, Any]] = {
    "auto_white_balance": {
        "v4l2": "white_balance_automatic",
        "label": "Auto White Balance",
        "kind": "boolean",
        "help": "Let the camera manage white balance automatically.",
    },
    "autofocus": {
        "v4l2": "focus_automatic_continuous",
        "label": "Autofocus",
        "kind": "boolean",
        "help": "Let the camera focus automatically.",
    },
    "brightness": {
        "v4l2": "brightness",
        "label": "Brightness",
        "kind": "number",
        "help": "Camera brightness control.",
    },
    "contrast": {
        "v4l2": "contrast",
        "label": "Contrast",
        "kind": "number",
        "help": "Camera contrast control.",
    },
    "saturation": {
        "v4l2": "saturation",
        "label": "Saturation",
        "kind": "number",
        "help": "Camera saturation control.",
    },
    "sharpness": {
        "v4l2": "sharpness",
        "label": "Sharpness",
        "kind": "number",
        "help": "Camera sharpening control.",
    },
    "white_balance_temperature": {
        "v4l2": "white_balance_temperature",
        "label": "White Balance Temperature",
        "kind": "number",
        "help": "Manual white balance in Kelvin.",
    },
    "focus": {
        "v4l2": "focus_absolute",
        "label": "Focus",
        "kind": "number",
        "help": "Manual focus position.",
    },
    "power_line_frequency": {
        "v4l2": "power_line_frequency",
        "label": "Power Line Frequency",
        "kind": "number",
        "help": "Anti-flicker frequency: 0 disabled, 1 50 Hz, 2 60 Hz, 3 camera auto.",
    },
}
V4L2_TO_KEY = {str(spec["v4l2"]): key for key, spec in CONTROL_SPECS.items()}


class UvcXuControlQuery(ctypes.Structure):
    _fields_ = [
        ("unit", ctypes.c_uint8),
        ("selector", ctypes.c_uint8),
        ("query", ctypes.c_uint8),
        ("size", ctypes.c_uint16),
        ("data", ctypes.POINTER(ctypes.c_uint8)),
    ]


def _ioc(direction: int, kind: str, number: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord(kind) << 8) | number


UVCIOC_CTRL_QUERY = _ioc(3, "u", 0x21, ctypes.sizeof(UvcXuControlQuery))
UVC_SET_CUR = 0x01
UVC_GET_CUR = 0x81
INSTA360_XU_UNIT = 9
INSTA360_ISO_SELECTOR = 0x19
INSTA360_SHUTTER_SELECTOR = 0x1D
INSTA360_AUTO_EXPOSURE_SELECTOR = 0x1E
INSTA360_AUTO_EXPOSURE_AUTO = 2
INSTA360_AUTO_EXPOSURE_MANUAL = 1
INSTA360_SHUTTER_MIN = 30
INSTA360_SHUTTER_MAX = 8000
INSTA360_SHUTTER_DEFAULT = 120
INSTA360_ISO_MIN = 100
INSTA360_ISO_MAX = 3200
INSTA360_ISO_DEFAULT = 400


def _xu_value(
    selector: int,
    size: int,
    query: int,
    value: int | None = None,
) -> int:
    if size <= 0:
        raise ValueError("XU control size must be positive")
    raw = (ctypes.c_uint8 * size)()
    if value is not None:
        encoded = int(value).to_bytes(size, "little", signed=False)
        for index, byte in enumerate(encoded):
            raw[index] = byte
    request = UvcXuControlQuery(
        INSTA360_XU_UNIT,
        selector,
        query,
        size,
        raw,
    )
    fd = os.open(CAMERA_DEVICE, os.O_RDWR | os.O_NONBLOCK)
    try:
        fcntl.ioctl(fd, UVCIOC_CTRL_QUERY, request)
    finally:
        os.close(fd)
    return int.from_bytes(bytes(raw), "little", signed=False)


def _get_vendor_value(selector: int, size: int) -> int | None:
    try:
        return _xu_value(selector, size, UVC_GET_CUR)
    except OSError:
        return None


def _set_vendor_value(selector: int, size: int, value: int) -> int:
    _xu_value(selector, size, UVC_SET_CUR, value)
    time.sleep(0.05)
    current = _get_vendor_value(selector, size)
    if current != value:
        raise RuntimeError(
            f"Insta360 XU selector 0x{selector:02x} read back {current!r} after setting {value}"
        )
    return current


def _get_vendor_auto_exposure() -> bool | None:
    mode = _get_vendor_value(INSTA360_AUTO_EXPOSURE_SELECTOR, 1)
    if mode == INSTA360_AUTO_EXPOSURE_AUTO:
        return True
    if mode == INSTA360_AUTO_EXPOSURE_MANUAL:
        return False
    return None


def _set_vendor_auto_exposure(enabled: bool) -> bool:
    mode = (
        INSTA360_AUTO_EXPOSURE_AUTO
        if enabled
        else INSTA360_AUTO_EXPOSURE_MANUAL
    )
    _set_vendor_value(INSTA360_AUTO_EXPOSURE_SELECTOR, 1, mode)
    return enabled


def _get_vendor_exposure() -> int | None:
    return _get_vendor_value(INSTA360_SHUTTER_SELECTOR, 2)


def _set_vendor_exposure(value: int | float) -> int:
    clamped = max(
        INSTA360_SHUTTER_MIN,
        min(INSTA360_SHUTTER_MAX, int(round(value))),
    )
    return _set_vendor_value(INSTA360_SHUTTER_SELECTOR, 2, clamped)


def _get_vendor_iso() -> int | None:
    return _get_vendor_value(INSTA360_ISO_SELECTOR, 2)


def _set_vendor_iso(value: int | float) -> int:
    clamped = max(INSTA360_ISO_MIN, min(INSTA360_ISO_MAX, int(round(value))))
    return _set_vendor_value(INSTA360_ISO_SELECTOR, 2, clamped)


def _run_v4l2(*args: str, timeout: float = 5.0, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["v4l2-ctl", "-d", CAMERA_DEVICE, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        message = (result.stderr or result.stdout or "v4l2-ctl failed").strip()
        raise RuntimeError(message)
    return result


def _load_state() -> dict[str, Any]:
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, STATE_PATH)


def _number(details: str, name: str) -> float | None:
    match = re.search(rf"(?:^|\s){re.escape(name)}=(-?\d+(?:\.\d+)?)", details)
    return float(match.group(1)) if match else None


def describe_controls() -> tuple[list[dict[str, Any]], dict[str, int | float | bool]]:
    result = _run_v4l2("-L", check=False)
    controls: list[dict[str, Any]] = []
    settings: dict[str, int | float | bool] = {}
    pattern = re.compile(r"^\s+(\S+)\s+0x[0-9a-fA-F]+\s+\(([^)]+)\)\s*:\s*(.*)$")
    if result.returncode == 0:
        for raw_line in result.stdout.splitlines():
            match = pattern.match(raw_line)
            if not match:
                continue
            name, _, details = match.groups()
            key = V4L2_TO_KEY.get(name)
            if key is None:
                continue
            spec = CONTROL_SPECS[key]
            current = _number(details, "value")
            if current is None:
                continue
            if spec["kind"] == "boolean":
                value: int | float | bool = bool(round(current))
            else:
                value = int(current) if current.is_integer() else current
            settings[key] = value
            control: dict[str, Any] = {
                "key": key,
                "label": spec["label"],
                "kind": spec["kind"],
                "help": spec.get("help"),
                "value": value,
                "inactive": "flags=inactive" in details,
            }
            default = _number(details, "default")
            if spec["kind"] == "boolean":
                if default is not None:
                    control["default"] = bool(round(default))
            else:
                for field in ("min", "max", "step", "default"):
                    numeric = _number(details, field)
                    if numeric is not None:
                        control[field] = int(numeric) if numeric.is_integer() else numeric
                # Some UVC menu controls advertise a stale max while returning
                # a valid firmware default/current value above it (the Insta360
                # power-line auto mode is value 3 while the descriptor says 2).
                for candidate in (value, control.get("default")):
                    if not isinstance(candidate, (int, float)) or isinstance(candidate, bool):
                        continue
                    if isinstance(control.get("min"), (int, float)):
                        control["min"] = min(control["min"], candidate)
                    if isinstance(control.get("max"), (int, float)):
                        control["max"] = max(control["max"], candidate)
            controls.append(control)

    auto_exposure = _get_vendor_auto_exposure()
    if auto_exposure is not None:
        settings["auto_exposure"] = auto_exposure
        controls.append(
            {
                "key": "auto_exposure",
                "label": "Auto Exposure",
                "kind": "boolean",
                "help": "Let the Insta360 camera choose shutter speed and ISO automatically.",
                "value": auto_exposure,
                "default": True,
            }
        )

    exposure = _get_vendor_exposure()
    if exposure is not None:
        settings["exposure"] = exposure
        controls.append(
            {
                "key": "exposure",
                "label": "Shutter Speed (1/N s)",
                "kind": "number",
                "help": "Shutter denominator: 500 means 1/500 second. Larger values shorten exposure and reduce motion blur.",
                "value": exposure,
                "min": INSTA360_SHUTTER_MIN,
                "max": INSTA360_SHUTTER_MAX,
                "step": 1,
                "default": INSTA360_SHUTTER_DEFAULT,
                "inactive": auto_exposure is True,
            }
        )

    iso = _get_vendor_iso()
    if iso is not None:
        settings["iso"] = iso
        controls.append(
            {
                "key": "iso",
                "label": "ISO",
                "kind": "number",
                "help": "Sensor gain used with manual exposure.",
                "value": iso,
                "min": INSTA360_ISO_MIN,
                "max": INSTA360_ISO_MAX,
                "step": 1,
                "default": INSTA360_ISO_DEFAULT,
                "inactive": auto_exposure is True,
            }
        )
    return controls, settings


def _coerce_control_value(control: dict[str, Any], raw: Any) -> int | float | bool:
    if control["kind"] == "boolean":
        if not isinstance(raw, bool):
            raise ValueError(f"{control['key']} must be true or false")
        return raw
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"{control['key']} must be numeric")
    value = float(raw)
    minimum = control.get("min")
    maximum = control.get("max")
    step = control.get("step")
    if isinstance(minimum, (int, float)):
        value = max(float(minimum), value)
    if isinstance(maximum, (int, float)):
        value = min(float(maximum), value)
    if isinstance(step, (int, float)) and step > 0:
        base = float(minimum) if isinstance(minimum, (int, float)) else 0.0
        value = base + round((value - base) / float(step)) * float(step)
    return int(round(value)) if float(value).is_integer() else value


CONTROL_LOCK = threading.Lock()


def apply_camera_settings(payload: dict[str, Any], *, persist: bool) -> dict[str, Any]:
    with CONTROL_LOCK:
        controls, _ = describe_controls()
        by_key = {str(control["key"]): control for control in controls}
        requested: dict[str, int | float | bool] = {}
        for key, raw in payload.items():
            control = by_key.get(key)
            if control is not None:
                requested[key] = _coerce_control_value(control, raw)

        # Automatic modes gate their manual controls.  Disable requested (or
        # implied) auto modes first, apply manual values second, then enable
        # any explicitly requested auto modes last.  This also makes persisted
        # manual focus/exposure survive a fresh stream where firmware defaults
        # may have turned the automatic mode back on.
        dependencies = {
            "focus": "autofocus",
            "white_balance_temperature": "auto_white_balance",
            "exposure": "auto_exposure",
            "iso": "auto_exposure",
        }
        for manual_key, auto_key in dependencies.items():
            if manual_key not in requested:
                continue
            if requested.get(auto_key) is True:
                requested.pop(manual_key, None)
            elif auto_key not in requested and auto_key in by_key:
                requested[auto_key] = False

        auto_keys = {"auto_exposure", "auto_white_balance", "autofocus"}
        ordered = [
            key for key in requested if key in auto_keys and requested[key] is False
        ]
        ordered.extend(key for key in requested if key not in auto_keys)
        ordered.extend(
            key for key in requested if key in auto_keys and requested[key] is True
        )
        for key in ordered:
            value = requested[key]
            if key == "auto_exposure":
                _set_vendor_auto_exposure(bool(value))
                continue
            if key == "exposure":
                _set_vendor_exposure(float(value))
                continue
            if key == "iso":
                _set_vendor_iso(float(value))
                continue
            spec = CONTROL_SPECS[key]
            encoded = "1" if value is True else "0" if value is False else str(int(round(float(value))))
            _run_v4l2("-c", f"{spec['v4l2']}={encoded}")

        controls, settings = describe_controls()
        if persist:
            state = _load_state()
            state["settings"] = settings
            _save_state(state)
        return {
            "ok": True,
            "provider": "usb-opencv",
            "transport": "rpi-v4l2-mjpeg-passthrough",
            "settings": settings,
            "controls": controls,
            "supported": bool(controls),
            "persisted": persist,
            "applied_live": True,
            "message": _camera_settings_message(),
        }


def reset_camera_settings() -> dict[str, Any]:
    controls, _ = describe_controls()
    defaults: dict[str, Any] = {}
    for control in controls:
        key = str(control["key"])
        if key in {"auto_exposure", "auto_white_balance", "autofocus"}:
            defaults[key] = True
        elif isinstance(control.get("default"), (int, float, bool)):
            defaults[key] = control["default"]
    return apply_camera_settings(defaults, persist=True)


def list_capture_modes() -> list[dict[str, Any]]:
    result = _run_v4l2("--list-formats-ext")
    modes: list[dict[str, Any]] = []
    current_fourcc: str | None = None
    current_size: tuple[int, int] | None = None
    seen: set[tuple[int, int, int]] = set()
    for line in result.stdout.splitlines():
        format_match = re.search(r"\[\d+\]:\s+'([^']+)'", line)
        if format_match:
            current_fourcc = format_match.group(1).upper()
            current_size = None
            continue
        size_match = re.search(r"Size:\s+Discrete\s+(\d+)x(\d+)", line)
        if size_match:
            current_size = (int(size_match.group(1)), int(size_match.group(2)))
            continue
        fps_match = re.search(r"\(([0-9.]+)\s+fps\)", line)
        if (
            fps_match
            and current_fourcc == "MJPG"
            and current_size is not None
        ):
            fps = int(round(float(fps_match.group(1))))
            key = (current_size[0], current_size[1], fps)
            if key in seen:
                continue
            seen.add(key)
            modes.append(
                {
                    "width": current_size[0],
                    "height": current_size[1],
                    "fps": fps,
                    "fourcc": "MJPG",
                    "native_fourcc": "MJPG",
                }
            )
    return modes


class CameraWorker:
    def __init__(self) -> None:
        state = _load_state()
        raw_mode = state.get("mode") if isinstance(state.get("mode"), dict) else {}
        self._mode = {
            "width": int(raw_mode.get("width", DEFAULT_WIDTH)),
            "height": int(raw_mode.get("height", DEFAULT_HEIGHT)),
            "fps": int(raw_mode.get("fps", DEFAULT_FPS)),
            "fourcc": "MJPG",
        }
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._restart = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._latest_jpeg: bytes | None = None
        self._latest_at: float | None = None
        self._latest_capture_monotonic_ns: int | None = None
        self._latest_capture_wall_time_ns: int | None = None
        self._source_epoch = uuid4()
        self._sequence = 0
        self._frames = 0
        self._errors = 0
        self._last_error: str | None = None
        self._stderr_tail: deque[str] = deque(maxlen=12)
        self._frame_times: deque[float] = deque(maxlen=120)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="v4l2-mjpeg-capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._restart.set()
        process = self._process
        if process is not None:
            process.terminate()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def mode(self) -> dict[str, Any]:
        with self._condition:
            return dict(self._mode)

    def set_mode(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            width = int(payload.get("width"))
            height = int(payload.get("height"))
            fps = int(payload.get("fps") or DEFAULT_FPS)
        except (TypeError, ValueError) as exc:
            raise ValueError("width, height, and fps must be integers") from exc
        match = next(
            (
                mode
                for mode in list_capture_modes()
                if mode["width"] == width
                and mode["height"] == height
                and mode["fps"] == fps
            ),
            None,
        )
        if match is None:
            raise ValueError(f"MJPG mode {width}x{height}@{fps} is not supported")
        with self._condition:
            self._mode = dict(match)
        state = _load_state()
        state["mode"] = self.mode()
        _save_state(state)
        self._restart.set()
        return self.mode()

    def wait_latest(
        self,
        after_sequence: int,
        timeout: float = 2.0,
    ) -> SourceFramePacket | None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._sequence <= after_sequence and not self._stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            if self._latest_jpeg is None:
                return None
            if (
                self._latest_capture_monotonic_ns is None
                or self._latest_capture_wall_time_ns is None
            ):
                return None
            return SourceFramePacket(
                jpeg=self._latest_jpeg,
                source_epoch=str(self._source_epoch),
                source_sequence=self._sequence,
                capture_monotonic_ns=self._latest_capture_monotonic_ns,
                capture_wall_time_ns=self._latest_capture_wall_time_ns,
            )

    def status(self) -> dict[str, Any]:
        with self._condition:
            age = None if self._latest_at is None else max(0.0, time.time() - self._latest_at)
            times = list(self._frame_times)
            measured_fps = (
                (len(times) - 1) / max(1e-6, times[-1] - times[0])
                if len(times) >= 2
                else 0.0
            )
            return {
                "ok": self._latest_jpeg is not None and age is not None and age < 3.0,
                "camera": CAMERA_DEVICE,
                "source": CAMERA_INDEX,
                **self._mode,
                "transport": "v4l2-mjpeg-passthrough",
                "transcode": False,
                "frames": self._frames,
                "source_epoch": str(self._source_epoch),
                "source_sequence": self._sequence,
                "source_capture_monotonic_ns": self._latest_capture_monotonic_ns,
                "source_capture_wall_time_ns": self._latest_capture_wall_time_ns,
                "source_identity_transport": "jpeg-app15-sorteros-c4-v1",
                "errors": self._errors,
                "last_error": self._last_error,
                "last_frame_age_s": age,
                "measured_fps": round(measured_fps, 2),
                "jpeg_bytes": len(self._latest_jpeg) if self._latest_jpeg is not None else 0,
                "sensor_auto_exposure": _get_vendor_auto_exposure(),
                "sensor_shutter_denominator": _get_vendor_exposure(),
                "sensor_iso": _get_vendor_iso(),
                "stderr_tail": list(self._stderr_tail),
            }

    def _record_frame(self, jpeg: bytes) -> None:
        now_mono = time.monotonic()
        capture_monotonic_ns = time.monotonic_ns()
        capture_wall_time_ns = time.time_ns()
        with self._condition:
            self._sequence += 1
            self._latest_jpeg = add_source_frame_marker(
                jpeg,
                source_epoch=self._source_epoch,
                source_sequence=self._sequence,
                capture_monotonic_ns=capture_monotonic_ns,
                capture_wall_time_ns=capture_wall_time_ns,
            )
            self._latest_at = capture_wall_time_ns / 1_000_000_000.0
            self._latest_capture_monotonic_ns = capture_monotonic_ns
            self._latest_capture_wall_time_ns = capture_wall_time_ns
            self._frames += 1
            self._last_error = None
            self._frame_times.append(now_mono)
            self._condition.notify_all()

    def _record_error(self, message: str) -> None:
        with self._condition:
            self._errors += 1
            self._last_error = message

    def _drain_stderr(self, process: subprocess.Popen[bytes]) -> None:
        assert process.stderr is not None
        for raw_line in iter(process.stderr.readline, b""):
            line = raw_line.decode("utf-8", errors="replace").strip()
            if line:
                self._stderr_tail.append(line)

    def _configure(self) -> None:
        mode = self.mode()
        _run_v4l2(
            f"--set-fmt-video=width={mode['width']},height={mode['height']},pixelformat=MJPG",
            f"--set-parm={mode['fps']}",
        )
        state = _load_state()
        settings = state.get("settings")
        if isinstance(settings, dict) and settings:
            apply_camera_settings(settings, persist=False)

    def _capture_once(self) -> None:
        self._configure()
        process = subprocess.Popen(
            [
                "v4l2-ctl",
                "-d",
                CAMERA_DEVICE,
                "--stream-mmap=4",
                "--stream-to=-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._process = process
        assert process.stdout is not None
        stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(process,),
            name="v4l2-stderr",
            daemon=True,
        )
        stderr_thread.start()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        pending = bytearray()
        last_frame_at = time.monotonic()
        try:
            while not self._stop.is_set() and not self._restart.is_set():
                if process.poll() is not None:
                    raise RuntimeError(f"v4l2-ctl exited with status {process.returncode}")
                events = selector.select(timeout=0.5)
                if not events:
                    if time.monotonic() - last_frame_at > 3.0:
                        raise RuntimeError("native MJPEG capture produced no frame for 3 seconds")
                    continue
                chunk = os.read(process.stdout.fileno(), 1024 * 1024)
                if not chunk:
                    raise RuntimeError("native MJPEG capture reached end of stream")
                pending.extend(chunk)
                while True:
                    start = pending.find(JPEG_SOI)
                    if start < 0:
                        if len(pending) > 1:
                            del pending[:-1]
                        break
                    if start > 0:
                        del pending[:start]
                    end = pending.find(JPEG_EOI, 2)
                    if end < 0:
                        if len(pending) > MAX_JPEG_BYTES:
                            pending.clear()
                            raise RuntimeError("native MJPEG frame exceeded safety limit")
                        break
                    end += len(JPEG_EOI)
                    jpeg = bytes(pending[:end])
                    del pending[:end]
                    self._record_frame(jpeg)
                    last_frame_at = time.monotonic()
        finally:
            selector.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            self._process = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self._restart.clear()
            try:
                self._capture_once()
            except Exception as exc:
                message = str(exc)
                self._record_error(message)
                LOG.exception("capture loop error: %s", message)
                if not self._stop.is_set():
                    time.sleep(1.0)


WORKER = CameraWorker()


def camera_settings_response() -> dict[str, Any]:
    controls, settings = describe_controls()
    return {
        "ok": True,
        "provider": "usb-opencv",
        "transport": "rpi-v4l2-mjpeg-passthrough",
        "settings": settings,
        "controls": controls,
        "supported": bool(controls),
        "message": _camera_settings_message(),
    }


def _camera_settings_message() -> str:
    return (
        "Remote Insta360 UVC controls plus manual shutter and ISO are available "
        "through the Raspberry Pi bridge."
    )


def capture_modes_response() -> dict[str, Any]:
    mode = WORKER.mode()
    return {
        "ok": True,
        "supported": True,
        "backend": "rpi-v4l2-mjpeg-passthrough",
        "modes": list_capture_modes(),
        "current": mode,
        "live": {
            "width": mode["width"],
            "height": mode["height"],
            "fps": WORKER.status()["measured_fps"],
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "SorterCameraBridge/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.debug("%s - %s", self.client_address[0], fmt % args)

    def _send_bytes(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        self._send_bytes(status, "application/json", json.dumps(payload).encode("utf-8"))

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > 65536:
            raise ValueError("request body is too large")
        raw = self.rfile.read(length) if length else b"{}"
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        return parsed

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send_json(HTTPStatus.OK, WORKER.status())
            return
        if path == "/camera-settings":
            self._send_json(HTTPStatus.OK, camera_settings_response())
            return
        if path == "/capture-modes":
            self._send_json(HTTPStatus.OK, capture_modes_response())
            return
        if path == "/snapshot.jpg":
            latest = WORKER.wait_latest(-1, timeout=1.0)
            if latest is None:
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "no frame yet")
                return
            self._send_bytes(HTTPStatus.OK, "image/jpeg", latest.jpeg)
            return
        if path == "/video":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Sorter-Source-Epoch", WORKER.status()["source_epoch"])
            self.end_headers()
            sequence = -1
            try:
                while True:
                    latest = WORKER.wait_latest(sequence, timeout=2.0)
                    if latest is None:
                        continue
                    jpeg = latest.jpeg
                    sequence = latest.source_sequence
                    header = (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        + f"Content-Length: {len(jpeg)}\r\n".encode("ascii")
                        + f"X-Sorter-Source-Epoch: {latest.source_epoch}\r\n".encode("ascii")
                        + f"X-Sorter-Frame-Sequence: {latest.source_sequence}\r\n".encode("ascii")
                        + (
                            "X-Sorter-Capture-Monotonic-Ns: "
                            f"{latest.capture_monotonic_ns}\r\n"
                        ).encode("ascii")
                        + (
                            "X-Sorter-Capture-Wall-Time-Ns: "
                            f"{latest.capture_wall_time_ns}\r\n\r\n"
                        ).encode("ascii")
                    )
                    self.wfile.write(header)
                    self.wfile.write(jpeg)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            payload = self._read_json()
            if path == "/camera-settings/preview":
                self._send_json(HTTPStatus.OK, apply_camera_settings(payload, persist=False))
                return
            if path == "/camera-settings":
                self._send_json(HTTPStatus.OK, apply_camera_settings(payload, persist=True))
                return
            if path == "/camera-settings/reset-defaults":
                self._send_json(HTTPStatus.OK, reset_camera_settings())
                return
            if path == "/capture-modes":
                mode = WORKER.set_mode(payload)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "mode": mode,
                        "persisted": True,
                        "applied_live": True,
                        "message": "Remote camera capture mode saved and the native stream is reopening.",
                    },
                )
                return
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "detail": str(exc)})
            return
        except Exception as exc:
            LOG.exception("request failed")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "detail": str(exc)})
            return
        self.send_error(HTTPStatus.NOT_FOUND)


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        LOG.debug("client disconnected: %s", client_address)


def _advertise_ip_ready() -> bool:
    """Check that the advertised IPv4 address has a live local link.

    Zeroconf joins multicast groups only on interfaces present when it starts.
    The direct Ethernet link can acquire its address after network-online.target,
    so constructing Zeroconf before this check would leave it on loopback only.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            for _, interface in socket.if_nameindex():
                request = struct.pack("256s", interface.encode("utf-8")[:15])
                try:
                    address = socket.inet_ntoa(
                        fcntl.ioctl(probe.fileno(), SIOCGIFADDR, request)[20:24]
                    )
                    flags = struct.unpack(
                        "H", fcntl.ioctl(probe.fileno(), SIOCGIFFLAGS, request)[16:18]
                    )[0]
                except OSError:
                    continue
                # IFF_UP and IFF_RUNNING require an enabled interface with a
                # working carrier; IFF_LOOPBACK must not satisfy the link check.
                if (
                    address == ADVERTISE_IP
                    and flags & IFF_UP
                    and flags & IFF_RUNNING
                    and not flags & IFF_LOOPBACK
                ):
                    return True
    except OSError:
        LOG.exception("cannot inspect camera advertisement interface")
    return False


def _advertise() -> Zeroconf:
    service_type = "_legosorter-camera._tcp.local."
    info = ServiceInfo(
        service_type,
        f"{CAMERA_NAME}._legosorter-camera._tcp.local.",
        addresses=[socket.inet_aton(ADVERTISE_IP)],
        port=PORT,
        properties={
            b"id": CAMERA_ID.encode("utf-8"),
            b"name": CAMERA_NAME.encode("utf-8"),
            b"model": b"Insta360 Link 2C Pro",
            b"transport": b"rpi-v4l2-mjpeg-passthrough",
            b"transcode": b"false",
            b"path": b"/video",
            b"snapshot": b"/snapshot.jpg",
            b"health": b"/health",
            b"settings": b"/camera-settings",
            b"capture_modes": b"/capture-modes",
        },
        server=f"{socket.gethostname()}.local.",
    )
    zeroconf = Zeroconf(interfaces=[ADVERTISE_IP], ip_version=IPVersion.V4Only)
    try:
        zeroconf.register_service(info, strict=False)
    except BaseException:
        zeroconf.close()
        raise
    LOG.info("advertised %s at http://%s:%s/video", CAMERA_NAME, ADVERTISE_IP, PORT)
    return zeroconf


def _close_advertisement(zeroconf: Zeroconf) -> None:
    try:
        zeroconf.unregister_all_services()
    except Exception:
        LOG.exception("failed to unregister camera advertisement")
    finally:
        try:
            zeroconf.close()
        except Exception:
            LOG.exception("failed to close camera advertisement")


def _reconcile_advertisement(zeroconf: Zeroconf | None) -> Zeroconf | None:
    if _advertise_ip_ready():
        return zeroconf if zeroconf is not None else _advertise()
    if zeroconf is not None:
        LOG.warning("camera advertisement link lost; waiting for %s", ADVERTISE_IP)
        _close_advertisement(zeroconf)
    return None


def _advertisement_loop(stop: threading.Event) -> None:
    zeroconf: Zeroconf | None = None
    try:
        while not stop.is_set():
            try:
                zeroconf = _reconcile_advertisement(zeroconf)
            except Exception:
                LOG.exception("camera advertisement failed; retrying")
            stop.wait(1.0)
    finally:
        if zeroconf is not None:
            _close_advertisement(zeroconf)


def main() -> None:
    logging.basicConfig(level=os.environ.get("SORTER_CAMERA_LOG_LEVEL", "INFO"))
    if not Path(CAMERA_DEVICE).exists():
        raise SystemExit(f"camera device does not exist: {CAMERA_DEVICE}")
    if not shutil_which("v4l2-ctl"):
        raise SystemExit("v4l2-ctl is required; install the v4l-utils package")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    WORKER.start()
    advertisement_stop = threading.Event()
    advertisement_thread = threading.Thread(
        target=_advertisement_loop,
        args=(advertisement_stop,),
        name="camera-mdns-advertisement",
        daemon=True,
    )
    advertisement_thread.start()
    server: BridgeServer | None = None
    try:
        server = BridgeServer((HOST, PORT), Handler)
        LOG.info("serving zero-transcode camera bridge on %s:%s", HOST, PORT)
        server.serve_forever()
    finally:
        if server is not None:
            server.shutdown()
        advertisement_stop.set()
        advertisement_thread.join(timeout=5.0)
        WORKER.stop()
        if server is not None:
            server.server_close()


def shutil_which(name: str) -> str | None:
    """Small local equivalent of shutil.which without importing the full module."""
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


if __name__ == "__main__":
    main()
