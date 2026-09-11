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

    def write(self, payload: bytes) -> None:
        self.writes.append(payload)

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
    return bus


def _response(address: int, command: int, channel: int, payload: bytes) -> bytes:
    decoded = struct.pack("<BBBB", address, command, channel, len(payload)) + payload
    decoded += struct.pack("<I", crc32(decoded))
    return cobs.encode(decoded) + b"\x00"


def test_send_command_reassembles_split_usb_response_without_resending() -> None:
    encoded = _response(0, 0x14, 3, b"\x01")
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
