import struct
from threading import Lock
from zlib import crc32

import pytest

from hardware import cobs
from hardware.bus import MCUBus, MCUBusError


class _Serial:
    def __init__(self, reads: list[bytes]) -> None:
        self.reads = list(reads)
        self.read_calls = 0
        self.reset_calls = 0
        self.writes: list[bytes] = []

    def reset_input_buffer(self) -> None:
        self.reset_calls += 1

    def write(self, payload: bytes) -> int:
        self.writes.append(payload)
        return len(payload)

    def read_until(self, expected: bytes, size: int) -> bytes:
        assert expected == b"\x00"
        self.read_calls += 1
        if not self.reads:
            return b""
        return self.reads.pop(0)[:size]


def _bus(serial_port: _Serial) -> MCUBus:
    bus = MCUBus.__new__(MCUBus)
    bus._serial = serial_port
    bus._lock = Lock()
    bus._port = "test"
    bus._unresolved_addresses = set()
    return bus


def _response(address: int, command: int, channel: int, payload: bytes) -> bytes:
    decoded = struct.pack("<BBBB", address, command, channel, len(payload)) + payload
    decoded += struct.pack("<I", crc32(decoded))
    return cobs.encode(decoded) + b"\x00"


def test_send_command_reassembles_split_usb_response_without_resending() -> None:
    encoded = _response(0, 0x14, 0, b"\x01")
    serial_port = _Serial([encoded[:5], encoded[5:]])

    result = _bus(serial_port).send_command(0, 0x14, 3, b"", retries=0)

    assert result.payload == b"\x01"
    assert serial_port.read_calls == 2
    assert serial_port.reset_calls == 1
    assert len(serial_port.writes) == 1


def test_send_command_allows_empty_gap_after_partial_response() -> None:
    encoded = _response(0, 0x1B, 0, b"\x00\x00\x00\x00")
    serial_port = _Serial([encoded[:7], b"", encoded[7:]])

    result = _bus(serial_port).send_command(0, 0x1B, 0, b"", retries=0)

    assert result.payload == b"\x00\x00\x00\x00"
    assert serial_port.read_calls == 3
    assert len(serial_port.writes) == 1


def test_send_command_rejects_a_frame_that_never_terminates() -> None:
    serial_port = _Serial([b"\x02\x03", b"", b"", b""])

    with pytest.raises(MCUBusError, match="missing terminator"):
        _bus(serial_port).send_command(0, 0x14, 3, b"", retries=0)

    assert serial_port.read_calls == 4
    assert len(serial_port.writes) == 1


@pytest.mark.parametrize("command,channel", [(0x15, 0), (0x13, 3), (0x93, 0)])
def test_completion_rejects_unrelated_reply_without_resending(command, channel):
    serial_port = _Serial([_response(0, command, channel, b"\x01")])
    with pytest.raises(MCUBusError, match="Unexpected response"):
        _bus(serial_port).send_command(0, 0x14, 3, b"", retries=2)
    assert len(serial_port.writes) == 1


@pytest.mark.parametrize("channel", [0, 2, 255])
def test_reply_channel_is_unpopulated_not_a_motor_identifier(channel):
    serial_port = _Serial([_response(0, 0x14, channel, b"\x01")])
    result = _bus(serial_port).send_command(0, 0x14, 3, b"")
    assert result.channel == channel  # Preserve actual bytes; do not invent echo.


@pytest.mark.parametrize("bad", [
    _response(1, 0x14, 0, b"\x01"),  # Another device, despite channel zero.
    _response(0, 0x15, 0, b"\x01"),  # Another command.
    b"\x05\x11\x00",  # Malformed COBS.
    bytes(cobs.encode(b"\x00\x14\x00\x00")) + b"\x00",  # No CRC.
    bytes(cobs.encode(b"\x00\x14\x00\x00" + b"\x00" * 4)) + b"\x00",  # Bad CRC.
])
def test_invalid_exchange_blocks_late_feedback_without_resending(bad):
    serial_port = _Serial([bad])
    bus = _bus(serial_port)
    with pytest.raises(MCUBusError):
        bus.send_command(0, 0x14, 3, b"", retries=2)
    serial_port.reads.append(_response(0, 0x14, 0, b"\x01"))
    with pytest.raises(MCUBusError, match="Unresolved prior transaction"):
        bus.send_command(0, 0x14, 2, b"")
    assert len(serial_port.writes) == 1


@pytest.mark.parametrize("declared,actual", [(0, b"\x01"), (2, b"\x01")])
def test_exact_frame_payload_length_is_required(declared, actual):
    frame = b"\x00\x14\x00" + bytes([declared]) + actual
    frame += struct.pack("<I", crc32(frame))
    bus = _bus(_Serial([bytes(cobs.encode(frame)) + b"\x00"]))
    with pytest.raises(MCUBusError, match="Payload length mismatch"):
        bus.send_command(0, 0x14, 3, b"")


def test_valid_nack_completes_exchange_without_resending():
    serial_port = _Serial([_response(0, 0x94, 0, b"Invalid channel")])
    bus = _bus(serial_port)
    with pytest.raises(MCUBusError, match="Error response received"):
        bus.send_command(0, 0x14, 255, b"")
    assert len(serial_port.writes) == 1
    serial_port.reads.append(_response(0, 0x14, 0, b"\x01"))
    assert bus.send_command(0, 0x14, 3, b"").payload == b"\x01"


def test_timeout_keeps_waiting_call_from_consuming_delayed_other_motor_reply():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, release, second_started = Event(), Event(), Event()

    class DelayedSerial(_Serial):
        def read_until(self, expected, size):
            entered.set()
            assert release.wait(2)
            return b""  # First status times out; its reply can arrive later.

    serial_port = DelayedSerial([])
    bus = _bus(serial_port)

    def second_read():
        second_started.set()
        return bus.send_command(0, 0x14, 2, b"")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(bus.send_command, 0, 0x14, 3, b"")
        try:
            assert entered.wait(2)
            second = pool.submit(second_read)
            assert second_started.wait(2)
            assert len(serial_port.writes) == 1
        finally:
            release.set()
        with pytest.raises(MCUBusError, match="Timeout"):
            first.result(timeout=2)
        with pytest.raises(MCUBusError, match="Unresolved prior transaction"):
            second.result(timeout=2)
    assert len(serial_port.writes) == 1


def test_missing_discovery_address_does_not_poison_another_device():
    serial_port = _Serial([])
    bus = _bus(serial_port)
    with pytest.raises(MCUBusError, match="Timeout"):
        bus.send_command(1, 1, 0, b"", retries=0)
    serial_port.reads.append(_response(2, 1, 0, b""))
    assert bus.send_command(2, 1, 0, b"").dev_address == 2


def test_startup_digital_write_accepts_observed_non_echoing_reply():
    # Recorded startup fields: request DIGITAL_WRITE/channel 1, reply 0x31/0.
    # Empty ACK payload follows the firmware handler; this is reconstructed,
    # not a claim that the journal captured the complete wire frame.
    serial_port = _Serial([_response(0, 0x31, 0, b"")])
    result = _bus(serial_port).send_command(0, 0x31, 1, b"\x01", retries=0)
    assert result.command == 0x31 and result.channel == 0
    assert result.payload == b""
    assert len(serial_port.writes) == 1


def _wire_owned_move(monkeypatch, *, inverted=False, lost_ack=False):
    from hardware.bus import MCUDevice
    from hardware.sorter_interface import StepperMotor
    from test_eject_controller import _owned_feeder, _cfg

    feeder, now = _owned_feeder(monkeypatch)
    serial_port = _Serial([])
    bus = _bus(serial_port)
    motor = StepperMotor(MCUDevice(bus, 0), 3, feeder.gc)
    motor._direction_inverted = inverted
    motor.estimateMoveDegreesMs = lambda *a, **kw: 100
    feeder.irl.c_channel_3_rotor_stepper = motor
    serial_port.reads.extend([
        _response(0, 0x15, 0, struct.pack("<i", -100 if inverted else 100)),
        _response(0, 0x12, 0, b""),
    ])
    if lost_ack:
        with pytest.raises(MCUBusError, match="Timeout"):
            feeder._move("ch3", motor, 2, 0, _cfg())
    else:
        serial_port.reads.append(_response(0, 0x10, 0, b"\x01"))
        assert feeder._move("ch3", motor, 2, 0, _cfg())
    now[0] += 0.2
    feeder._motion_tick += 1
    return feeder, motor, serial_port, now


@pytest.mark.parametrize("inverted", [False, True])
def test_short_move_finishes_via_real_driver_and_bus_without_seen_moving(monkeypatch, inverted):
    feeder, motor, wire, _ = _wire_owned_move(monkeypatch, inverted=inverted)
    target = feeder._move_targets[motor._name]
    physical_target = -target if inverted else target
    wire.reads.extend([_response(0, 0x14, 0, b"\x01"),
                       _response(0, 0x15, 0, struct.pack("<i", physical_target))])
    assert not feeder._busy(motor)
    assert not feeder.shared.c3_motion_pending
    # Motor identity comes from these serialized requests, not the reply byte.
    headers = [tuple(cobs.decode(frame[:-1])[:3]) for frame in wire.writes]
    assert headers == [(0, cmd, 3) for cmd in (0x15, 0x12, 0x10, 0x14, 0x15)]


def test_old_idle_position_cannot_complete_current_move_on_wire(monkeypatch):
    from test_eject_controller import _cfg

    feeder, motor, wire, _ = _wire_owned_move(monkeypatch)
    wire.reads.extend([_response(0, 0x14, 0, b"\x01"),
                       _response(0, 0x15, 0, struct.pack("<i", 100))])
    assert feeder._busy(motor)
    assert feeder._recovery_move(motor, 3, _cfg()) is None
    assert len(wire.writes) == 5
    feeder._motion_tick += 1
    target = feeder._move_targets[motor._name]
    wire.reads.extend([_response(0, 0x14, 0, b"\x01"),
                       _response(0, 0x15, 0, struct.pack("<i", target))])
    assert not feeder._busy(motor)


@pytest.mark.parametrize("first_replies", [
    [],  # Missing status.
    [_response(0, 0x14, 0, b"\x01")],  # Missing position.
    [_response(0, 0x15, 0, struct.pack("<i", 100))],  # Unrelated response.
])
def test_failed_read_and_late_target_reply_never_release_owner(monkeypatch, first_replies):
    from test_eject_controller import _cfg

    feeder, motor, wire, now = _wire_owned_move(monkeypatch)
    wire.reads.extend(first_replies)
    assert feeder._busy(motor)
    writes = len(wire.writes)
    target = feeder._move_targets[motor._name]
    wire.reads.extend([_response(0, 0x14, 0, b"\x01"),
                       _response(0, 0x15, 0, struct.pack("<i", target))])
    now[0] += 100  # Neither elapsed time nor a late perfect target unlocks it.
    feeder._motion_tick += 1
    assert feeder._busy(motor)
    assert feeder._recovery_move(motor, 3, _cfg()) is None
    assert not feeder._move("normal", motor, 2, 0, _cfg())
    assert len(wire.writes) == writes
    assert feeder.shared.c3_motion_pending


@pytest.mark.parametrize("payload", [b"", b"\x02", b"\x01\x00"])
def test_malformed_stopped_value_is_not_completion(monkeypatch, payload):
    feeder, motor, wire, _ = _wire_owned_move(monkeypatch)
    wire.reads.append(_response(0, 0x14, 0, payload))
    assert feeder._busy(motor)
    assert feeder.shared.c3_motion_pending


def test_disabled_owned_motor_cannot_use_synthetic_stopped(monkeypatch):
    feeder, motor, wire, _ = _wire_owned_move(monkeypatch)
    motor.software_disabled = True
    assert feeder._busy(motor)
    assert len(wire.writes) == 3
    assert feeder.shared.c3_motion_pending


def test_lost_move_ack_is_not_resent_and_late_ack_cannot_unlock(monkeypatch):
    feeder, motor, wire, _ = _wire_owned_move(monkeypatch, lost_ack=True)
    wire.reads.append(_response(0, 0x10, 0, b"\x01"))
    assert feeder._busy(motor)
    assert len(wire.writes) == 3  # Exactly one MOVE, even with default retries.
    assert feeder.shared.c3_motion_pending


def test_two_motor_owners_complete_independently_on_one_serial_bus(monkeypatch):
    from hardware.sorter_interface import StepperMotor
    from test_eject_controller import _cfg

    feeder, c3, wire, now = _wire_owned_move(monkeypatch)
    c2 = StepperMotor(c3._dev, 2, feeder.gc)
    c2.estimateMoveDegreesMs = lambda *a, **kw: 100
    feeder.irl.c_channel_2_rotor_stepper = c2
    wire.reads.extend([_response(0, 0x15, 0, struct.pack("<i", 200)),
                       _response(0, 0x12, 0, b""), _response(0, 0x10, 0, b"\x01")])
    assert feeder._move("ch2", c2, 2, 0, _cfg())
    now[0] += 0.2
    feeder._motion_tick += 1
    target2 = feeder._move_targets[c2._name]
    wire.reads.extend([_response(0, 0x14, 0, b"\x01"),
                       _response(0, 0x15, 0, struct.pack("<i", target2))])
    assert not feeder._busy(c2)
    assert c3._name in feeder._move_targets and feeder.shared.c3_motion_pending
    headers = [tuple(cobs.decode(frame[:-1])[:3]) for frame in wire.writes]
    assert headers[-5:] == [(0, cmd, 2) for cmd in (0x15, 0x12, 0x10, 0x14, 0x15)]
