"""Waveshare SC series serial bus servo driver.

Implements the Feetech SC protocol (SC09, SC15, SC40, SC60) over half-duplex
TTL serial at 1 Mbps.  The driver provides both a low-level bus class
(`ScServoBus`) and a high-level `WaveshareServoMotor` that is a drop-in
replacement for the PCA9685-based `ServoMotor` used elsewhere in the sorter.

SC series uses **big-endian** byte order for 16-bit register values.

Auto-calibration
----------------
On first use each servo finds its physical min/max positions by stepping
towards each limit until the servo stalls (load spike + position stops
changing).  The discovered range is stored in the servo's EEPROM so the
procedure only runs once per servo (unless limits are reset to 0-1023).
"""

import logging
import struct
import threading
import time
from typing import Any, Dict

import serial

from smart_bins_native_custody import guard_motion

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SC Protocol constants
# ---------------------------------------------------------------------------

# Instructions
_INST_PING = 0x01
_INST_READ = 0x02
_INST_WRITE = 0x03

# Register addresses — EEPROM
_REG_MODEL_L = 3
_REG_ID = 5
_REG_MIN_ANGLE_L = 9
_REG_MAX_ANGLE_L = 11
_REG_P_COEF = 21
_REG_D_COEF = 22
_REG_I_COEF = 23

# Register addresses — SRAM
_REG_TORQUE_ENABLE = 40
_REG_GOAL_POSITION_L = 42
_REG_LOCK = 48
_REG_PRESENT_POSITION_L = 56
_REG_PRESENT_LOAD_L = 60
_REG_PRESENT_VOLTAGE = 62
_REG_PRESENT_TEMPERATURE = 63
_REG_MOVING = 66
_REG_PRESENT_CURRENT_L = 69


def _checksum(data: bytes) -> int:
    return (~sum(data)) & 0xFF


class ScServoBus:
    """Low-level half-duplex serial bus for SC servos."""

    def __init__(self, port: str, baudrate: int = 1_000_000, timeout: float = 0.05):
        self._serial = serial.Serial(port, baudrate=baudrate, timeout=timeout)
        self._lock = threading.Lock()

    def close(self):
        self._serial.close()

    # -- packet I/O ---------------------------------------------------------

    def _read_packet(self) -> bytes | None:
        first = self._serial.read(1)
        if len(first) < 1:
            return None

        while True:
            if first == b"\xFF":
                second = self._serial.read(1)
                if len(second) < 1:
                    return None
                if second == b"\xFF":
                    break
                first = second
                continue

            first = self._serial.read(1)
            if len(first) < 1:
                return None

        meta = self._serial.read(3)
        if len(meta) < 3:
            return None

        resp_length = meta[1]
        if resp_length < 2:
            return None

        tail = self._serial.read(resp_length - 1)
        if len(tail) < resp_length - 1:
            return None

        return b"\xFF\xFF" + meta + tail

    def _send(self, servo_id: int, instruction: int, params: bytes = b"") -> bytes | None:
        with self._lock:
            length = len(params) + 2
            pkt = bytes([0xFF, 0xFF, servo_id, length, instruction]) + params
            pkt += bytes([_checksum(pkt[2:])])

            self._serial.reset_input_buffer()
            self._serial.write(pkt)
            self._serial.flush()

            packet = self._read_packet()
            if packet == pkt:
                packet = self._read_packet()
            if packet is None or len(packet) < 6:
                return None

            response_id = packet[2]
            resp_length = packet[3]
            if resp_length < 2:
                return None
            error = packet[4]
            if response_id != servo_id:
                return None

            payload = packet[5:-1]
            checksum = packet[-1]
            expected_checksum = _checksum(packet[2:-1])
            if checksum != expected_checksum:
                return None
            if error != 0:
                return None

            return payload

    # -- helpers ------------------------------------------------------------

    def ping(self, servo_id: int) -> bool:
        return self._send(servo_id, _INST_PING) is not None

    def read_bytes(self, servo_id: int, address: int, count: int) -> bytes | None:
        resp = self._send(servo_id, _INST_READ, bytes([address, count]))
        if resp is None or len(resp) < count:
            return None
        return resp[:count]

    def write_bytes(self, servo_id: int, address: int, data: bytes) -> bool:
        return self._send(servo_id, _INST_WRITE, bytes([address]) + data) is not None

    def read_word(self, servo_id: int, address: int) -> int | None:
        data = self.read_bytes(servo_id, address, 2)
        if data is None:
            return None
        return struct.unpack(">H", data)[0]  # big-endian

    def write_word(self, servo_id: int, address: int, value: int) -> bool:
        return self.write_bytes(servo_id, address, struct.pack(">H", value))

    def write_byte(self, servo_id: int, address: int, value: int) -> bool:
        return self.write_bytes(servo_id, address, bytes([value]))

    # -- high-level servo commands ------------------------------------------

    def scan(self, start: int = 1, end: int = 20) -> list[int]:
        found = []
        for sid in range(start, end + 1):
            if self.ping(sid):
                found.append(sid)
            time.sleep(0.002)
        return found

    def set_torque(self, servo_id: int, enable: bool) -> bool:
        if enable:
            from smart_bins_native_custody import motion_entry
            with motion_entry("enable", "route"):
                return self.write_byte(servo_id, _REG_TORQUE_ENABLE, 1)
        return self.write_byte(servo_id, _REG_TORQUE_ENABLE, 1 if enable else 0)

    @guard_motion("route")
    def move_to(self, servo_id: int, position: int, time_ms: int = 500) -> bool:
        position = max(0, min(1023, position))
        pos_bytes = struct.pack(">H", position)
        time_bytes = struct.pack(">H", time_ms)
        speed_bytes = struct.pack(">H", 0)
        return self.write_bytes(
            servo_id, _REG_GOAL_POSITION_L,
            pos_bytes + time_bytes + speed_bytes,
        )

    def read_position(self, servo_id: int) -> int | None:
        val = self.read_word(servo_id, _REG_PRESENT_POSITION_L)
        if val is None:
            return None
        return val & 0x03FF

    def read_load(self, servo_id: int) -> int | None:
        val = self.read_word(servo_id, _REG_PRESENT_LOAD_L)
        if val is None:
            return None
        raw = val & 0x3FF
        if val & 0x400:
            return -raw
        return raw

    def is_moving(self, servo_id: int) -> bool | None:
        data = self.read_bytes(servo_id, _REG_MOVING, 1)
        if data is None or len(data) != 1 or data[0] not in (0, 1):
            return None
        return data[0] == 1

    def read_angle_limits(self, servo_id: int) -> tuple[int, int] | None:
        data = self.read_bytes(servo_id, _REG_MIN_ANGLE_L, 4)
        if data is None or len(data) < 4:
            return None
        min_angle = struct.unpack(">H", data[0:2])[0]
        max_angle = struct.unpack(">H", data[2:4])[0]
        return min_angle, max_angle

    def set_angle_limits(self, servo_id: int, min_val: int, max_val: int) -> bool:
        self.write_byte(servo_id, _REG_LOCK, 0)  # unlock EEPROM
        time.sleep(0.01)
        data = struct.pack(">H", min_val) + struct.pack(">H", max_val)
        result = self.write_bytes(servo_id, _REG_MIN_ANGLE_L, data)
        time.sleep(0.01)
        self.write_byte(servo_id, _REG_LOCK, 1)  # lock EEPROM
        return result

    def set_pid(self, servo_id: int, p: int, d: int, i: int) -> bool:
        self.write_byte(servo_id, _REG_LOCK, 0)
        time.sleep(0.01)
        result = self.write_bytes(servo_id, _REG_P_COEF, bytes([p, d, i]))
        time.sleep(0.01)
        self.write_byte(servo_id, _REG_LOCK, 1)
        return result

    def set_id(self, old_id: int, new_id: int) -> bool:
        """Change a servo's ID.  Requires EEPROM unlock."""
        if new_id < 1 or new_id > 253:
            return False
        self.write_byte(old_id, _REG_LOCK, 0)
        time.sleep(0.01)
        result = self.write_byte(old_id, _REG_ID, new_id)
        time.sleep(0.01)
        self.write_byte(new_id, _REG_LOCK, 1)
        return result

    def read_servo_info(self, servo_id: int) -> dict | None:
        """Read identification and live telemetry from a servo."""
        if not self.ping(servo_id):
            return None

        model = self.read_word(servo_id, _REG_MODEL_L)
        position = self.read_position(servo_id)
        load = self.read_load(servo_id)
        limits = self.read_angle_limits(servo_id)

        temp_data = self.read_bytes(servo_id, _REG_PRESENT_VOLTAGE, 2)
        voltage: int | None = None
        temperature: int | None = None
        if temp_data is not None and len(temp_data) >= 2:
            voltage = temp_data[0]
            temperature = temp_data[1]

        current = self.read_word(servo_id, _REG_PRESENT_CURRENT_L)

        pid_data = self.read_bytes(servo_id, _REG_P_COEF, 3)
        pid = None
        if pid_data is not None and len(pid_data) >= 3:
            pid = {"p": pid_data[0], "d": pid_data[1], "i": pid_data[2]}

        model_name = None
        if model is not None:
            # Model detection based on observed firmware values (see sc_servo.rs)
            if model in (4, 9, 0x0400, 0x0504, 0x0405, 0x0900):
                model_name = "SC09"
            elif model in (15, 0x0F00, 0x050F, 0x0F05):
                model_name = "SC15"
            elif model in (40, 0x2800):
                model_name = "SC40"
            elif model in (60, 0x3C00):
                model_name = "SC60"

        return {
            "id": servo_id,
            "model": model,
            "model_name": model_name,
            "position": position,
            "load": load,
            "min_limit": limits[0] if limits else None,
            "max_limit": limits[1] if limits else None,
            "voltage": round(voltage / 10.0, 1) if voltage is not None else None,
            "temperature": temperature,
            "current": current,
            "pid": pid,
        }


# ---------------------------------------------------------------------------
# Auto-calibration
# ---------------------------------------------------------------------------

_CAL_STEP_SIZE = 30
_CAL_STEP_TIME_MS = 500
_CAL_SETTLE_MS = 600
_CAL_LOAD_THRESHOLD = 300
_CAL_STALL_CHECKS = 3
_CAL_MARGIN = 5


def calibrate_servo(bus: ScServoBus, servo_id: int) -> tuple[int, int]:
    """Find the physical min/max of a servo by stepping until stall.

    Returns (safe_min, safe_max) with a small safety margin applied.
    Raises RuntimeError if calibration fails.
    """
    logger.info(f"Calibrating servo {servo_id}...")

    # Temporarily open full range
    bus.set_angle_limits(servo_id, 0, 1023)
    time.sleep(0.02)
    bus.set_torque(servo_id, True)
    time.sleep(0.02)

    current = bus.read_position(servo_id)
    if current is None:
        raise RuntimeError(f"Cannot read position of servo {servo_id}")

    def find_limit(start_pos: int, direction: int) -> int:
        """Step in `direction` (-1 for min, +1 for max) until stall."""
        best = start_pos
        target = start_pos
        last_pos = None
        stall_count = 0

        while True:
            next_target = target + direction * _CAL_STEP_SIZE
            next_target = max(0, min(1023, next_target))
            if next_target == target:
                # Hit 0 or 1023 boundary
                return best
            target = next_target

            bus.move_to(servo_id, target, _CAL_STEP_TIME_MS)
            time.sleep(_CAL_SETTLE_MS / 1000.0)

            # Poll for stall
            for _ in range(5):
                time.sleep(0.1)
                pos = bus.read_position(servo_id)
                load = bus.read_load(servo_id)
                if pos is None or load is None:
                    continue

                if (direction < 0 and pos < best) or (direction > 0 and pos > best):
                    best = pos

                near_target = abs(pos - target) < 10
                if near_target:
                    break  # reached target, issue next step

                if last_pos is not None and abs(pos - last_pos) < 2:
                    stall_count += 1
                else:
                    stall_count = 0
                last_pos = pos

                if stall_count >= _CAL_STALL_CHECKS or (abs(load) > _CAL_LOAD_THRESHOLD and stall_count >= 2):
                    logger.info(f"  Servo {servo_id}: limit found at {best} (stall, load={load})")
                    return best
            else:
                last_pos = pos

        return best  # unreachable but keeps linter happy

    cal_min = find_limit(current, -1)
    logger.info(f"  Servo {servo_id}: min = {cal_min}")

    cal_max = find_limit(cal_min, +1)
    logger.info(f"  Servo {servo_id}: max = {cal_max}")

    span = cal_max - cal_min
    if span < 20:
        raise RuntimeError(
            f"Servo {servo_id} calibration failed: range too small ({cal_min}-{cal_max}, span={span})"
        )

    margin = min(_CAL_MARGIN, span // 4)
    safe_min = cal_min + margin
    safe_max = cal_max - margin

    # Save to EEPROM so we don't need to recalibrate next time
    bus.set_angle_limits(servo_id, safe_min, safe_max)
    logger.info(f"  Servo {servo_id}: calibrated range {safe_min}-{safe_max} (saved to EEPROM)")

    # Move to center
    center = (safe_min + safe_max) // 2
    bus.move_to(servo_id, center, 500)
    time.sleep(0.5)
    bus.set_torque(servo_id, False)

    return safe_min, safe_max


# ---------------------------------------------------------------------------
# WaveshareServoMotor — drop-in replacement for ServoMotor
# ---------------------------------------------------------------------------

class WaveshareServoMotor:
    """A servo motor controlled via the Waveshare SC serial bus.

    Presents the same interface as `hardware.sorter_interface.ServoMotor`
    so it can be used as a drop-in replacement in the distribution system.

    open/close positions are mapped to the EEPROM angle limits:
    - open  = min limit  (gate opens, piece falls through)
    - close = max limit  (gate blocks)
    If `invert` is True these are swapped.
    """

    def __init__(self, bus: ScServoBus, servo_id: int, invert: bool = False):
        self._bus = bus
        self._servo_id = servo_id
        self._invert = invert
        self._name = f"waveshare_servo_{servo_id}"
        self._enabled = False
        self._confirmed_position: int | None = None  # raw SC units, never a target
        self._pending_target: int | None = None
        self._last_command_outcome = "NONE"
        self._last_position_observed_at: float | None = None
        self._last_motion_observed_at: float | None = None
        self._moving: bool | None = None
        self._calibrated = False
        self._release_pending = False
        self._min_limit: int = 0
        self._max_limit: int = 1023
        self._open_position: int = 0
        self._closed_position: int = 1023
        self._move_started_at: float = 0.0
        self._move_duration: float = 0.0

    def initialize(self) -> None:
        """Read or auto-calibrate limits and apply good PID settings."""
        self._calibrated = False
        self._invalidate_observations()
        # Set PID to avoid undershooting (factory default I=0 causes issues)
        self._bus.set_pid(self._servo_id, 32, 32, 20)

        limits = self._bus.read_angle_limits(self._servo_id)
        if limits is None:
            raise RuntimeError(f"Cannot communicate with servo {self._servo_id}")

        min_lim, max_lim = limits
        needs_calibration = (min_lim == 0 and max_lim == 1023) or (max_lim - min_lim < 20)

        if needs_calibration:
            logger.info(f"Servo {self._servo_id}: limits are {min_lim}-{max_lim}, running auto-calibration")
            min_lim, max_lim = calibrate_servo(self._bus, self._servo_id)
        else:
            logger.info(f"Servo {self._servo_id}: using stored limits {min_lim}-{max_lim}")

        self._min_limit = min_lim
        self._max_limit = max_lim

        if self._invert:
            self._open_position = max_lim
            self._closed_position = min_lim
        else:
            self._open_position = min_lim
            self._closed_position = max_lim

        self._calibrated = self._valid_limits(min_lim, max_lim)
        self._pending_target = None
        self._last_command_outcome = "NONE"
        self._invalidate_observations()
        self.position

    def set_invert(self, invert: bool) -> None:
        self._invert = invert
        if self._invert:
            self._open_position = self._max_limit
            self._closed_position = self._min_limit
        else:
            self._open_position = self._min_limit
            self._closed_position = self._max_limit

    def recalibrate(self) -> tuple[int, int]:
        self._calibrated = False
        self._pending_target = None
        self._last_command_outcome = "NONE"
        self._invalidate_observations()
        min_lim, max_lim = calibrate_servo(self._bus, self._servo_id)
        self._min_limit, self._max_limit = min_lim, max_lim
        self.set_invert(self._invert)
        self._calibrated = self._valid_limits(min_lim, max_lim)
        self.position
        return min_lim, max_lim

    @staticmethod
    def _valid_limits(minimum: int, maximum: int) -> bool:
        return 0 <= minimum < maximum <= 1023 and maximum - minimum >= 20 and (minimum, maximum) != (0, 1023)

    @property
    def is_calibrated(self) -> bool:
        return self._calibrated and self._valid_limits(self._min_limit, self._max_limit)

    def _invalidate_observations(self) -> None:
        self._confirmed_position = None
        self._last_position_observed_at = None
        self._last_motion_observed_at = None
        self._moving = None

    # -- ServoMotor-compatible interface ------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool):
        self._enabled = bool(value)
        self._bus.set_torque(self._servo_id, self._enabled)

    @guard_motion("route")
    def _move_raw(self, position: int, *, release: bool) -> bool:
        self._pending_target = position
        self._moving = None
        self._last_motion_observed_at = None
        self._last_position_observed_at = None
        self._release_pending = release
        self._move_duration = 0.3  # advisory only; never stopped evidence
        self._move_started_at = time.monotonic()
        try:
            if not self._enabled:
                self.enabled = True
            acknowledged = self._bus.move_to(self._servo_id, position, 300)
        except Exception:
            self._last_command_outcome = "AMBIGUOUS"
            self._invalidate_observations()
            if release:
                try:
                    self._release_torque()
                except Exception:
                    logger.warning("Waveshare ambiguous command torque release was not acknowledged")
            raise
        self._last_command_outcome = "ACCEPTED" if acknowledged else "AMBIGUOUS"
        if not acknowledged:
            self._invalidate_observations()
            if release:
                try:
                    self._release_torque()
                except Exception:
                    logger.warning("Waveshare ambiguous command torque release was not acknowledged")
        return bool(acknowledged)

    def move_to(self, angle: int) -> bool:
        """Request an angle; an ACK does not establish physical position."""
        return self._move_raw(self._angle_to_position(angle), release=False)

    def move_to_and_release(self, angle: int) -> bool:
        return self._move_raw(self._angle_to_position(angle), release=True)

    @property
    def position(self) -> int | None:
        try:
            pos = self._bus.read_position(self._servo_id)
        except Exception:
            self._confirmed_position = None
            self._last_position_observed_at = None
            raise
        if not isinstance(pos, int) or isinstance(pos, bool) or not 0 <= pos <= 1023:
            self._confirmed_position = None
            self._last_position_observed_at = None
            return None
        self._confirmed_position = pos
        self._last_position_observed_at = time.monotonic()
        return pos

    def stop(self):
        self._invalidate_observations()
        self._pending_target = None
        self._last_command_outcome = "NONE"
        self._release_torque()

    def _release_torque(self) -> None:
        # A failed release remains pending so the next normal poll can release.
        if not self._bus.set_torque(self._servo_id, False):
            raise RuntimeError("Waveshare torque release was not acknowledged")
        self._enabled = False
        self._release_pending = False

    @property
    def stopped(self) -> bool:
        try:
            moving = self._bus.is_moving(self._servo_id)
            self._moving = moving if isinstance(moving, bool) else None
            self._last_motion_observed_at = time.monotonic() if self._moving is not None else None
            if self._moving is False:
                pos = self.position
                if (pos is not None and self._pending_target is not None
                        and self._last_command_outcome == "ACCEPTED"
                        and abs(pos - self._pending_target) <= self.POSITION_TOLERANCE):
                    self._pending_target = None
                if self._release_pending:
                    self._release_torque()
                return True
            # Last observed position cannot qualify a flap while moving/unknown.
            self._confirmed_position = None
            self._last_position_observed_at = None
            if self._release_pending and time.monotonic() - self._move_started_at >= 3.5:
                # Protective release on timeout is NOT stopped/target evidence.
                self._release_torque()
            return False
        except Exception:
            self._invalidate_observations()
            if self._release_pending and time.monotonic() - self._move_started_at >= 3.5:
                try:
                    self._release_torque()
                except Exception:
                    logger.warning("Waveshare protective torque release was not acknowledged")
            raise

    @property
    def available(self) -> bool:
        return True

    def open(self, open_angle: int | None = None) -> bool:
        if not self.is_calibrated:
            return False
        return self._move_raw(self._open_position, release=True)

    def close(self, closed_angle: int | None = None) -> bool:
        if not self.is_calibrated:
            return False
        return self._move_raw(self._closed_position, release=True)

    def toggle(self) -> None:
        if self.isOpen():
            self.close()
        else:
            self.open()

    # Native calibration used <10 raw counts for a reached target. Keep the
    # tolerance below that bound (SC has 1024 raw positions), without rounding.
    POSITION_TOLERANCE = 9

    def _at_position(self, target: int) -> bool:
        return (self.is_calibrated and self._confirmed_position is not None
                and self._last_position_observed_at is not None
                and self._moving is False and self._last_motion_observed_at is not None
                and self._pending_target is None
                and self._last_command_outcome != "AMBIGUOUS"
                and abs(self._confirmed_position - target) <= self.POSITION_TOLERANCE)

    def isOpen(self) -> bool:
        return self._at_position(self._open_position)

    def isClosed(self) -> bool:
        return self._at_position(self._closed_position)

    def set_speed_limits(self, min_speed: int, max_speed: int) -> None:
        pass  # SC servos use time-based moves, speed is implicit

    def set_acceleration(self, acceleration: int) -> None:
        pass  # not applicable for SC servos

    def set_duty_limits(self, min_duty_us: int, max_duty_us: int) -> None:
        pass  # not applicable for SC servos

    def set_name(self, name: str) -> None:
        self._name = name

    def set_preset_angles(self, open_angle: int, closed_angle: int) -> None:
        # For waveshare, open/close are determined by calibration + invert,
        # so we ignore the angle values but accept the call for compatibility.
        pass

    @property
    def angle(self) -> int | None:
        pos = self._confirmed_position
        return self._position_to_angle(pos) if pos is not None else None

    @property
    def channel(self) -> int:
        return self._servo_id

    def feedback(self) -> Dict[str, Any]:
        error = None
        try:
            self.stopped  # one moving read; one position read when stopped
        except Exception as exc:
            error = str(exc)
        position = self._confirmed_position
        return {
            "available": self.available,
            "channel": self._servo_id,
            "position": position,
            "angle": self._position_to_angle(position) if position is not None else None,
            "pending_target": self._pending_target,
            "open_position": self._open_position,
            "closed_position": self._closed_position,
            "min_limit": self._min_limit,
            "max_limit": self._max_limit,
            "is_open": self.isOpen(),
            "is_closed": self.isClosed(),
            "calibrated": self.is_calibrated,
            "moving": self._moving,
            "stopped": not self._moving if self._moving is not None else None,
            "motion_state_valid": self._last_motion_observed_at is not None,
            "position_valid": position is not None and self._last_position_observed_at is not None,
            "command_outcome": self._last_command_outcome,
            "position_observed_at": self._last_position_observed_at,
            "motion_observed_at": self._last_motion_observed_at,
            "invert": self._invert,
            "error": error,
        }

    # -- internal -----------------------------------------------------------

    def _angle_to_position(self, angle: int) -> int:
        """Map 0-180 degrees to calibrated min-max range."""
        angle = max(0, min(180, angle))
        span = self._max_limit - self._min_limit
        return self._min_limit + int(round(angle / 180.0 * span))

    def _position_to_angle(self, position: int) -> int:
        """Map calibrated position back to 0-180 degrees."""
        span = self._max_limit - self._min_limit
        if span == 0:
            return 0
        return int(round((position - self._min_limit) / span * 180.0))
