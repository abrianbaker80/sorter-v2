"""Wire-only MCU bus safety tests for the native v0.3.0 runtime."""

import struct
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from zlib import crc32

import pytest

from hardware import cobs
from hardware.bus import MAX_FRAME_SIZE, MCUBus, MCUBusError


COMMAND = 0x14


def _response(address: int = 0, command: int = COMMAND, channel: int = 0,
              payload: bytes = b"\x01") -> bytes:
    decoded = struct.pack("<BBBB", address, command, channel, len(payload)) + payload
    decoded += struct.pack("<I", crc32(decoded))
    return bytes(cobs.encode(decoded)) + b"\x00"


class _Serial:
    def __init__(self, reads: list[bytes] | None = None, *, write_length: int | None = None):
        self.reads = list(reads or [])
        self.write_length = write_length
        self.writes: list[bytes] = []
        self.read_calls = 0
        self.reset_calls = 0

    def reset_input_buffer(self) -> None:
        self.reset_calls += 1

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        return len(data) if self.write_length is None else self.write_length

    @property
    def in_waiting(self) -> int:
        return len(self.reads[0]) if self.reads else 0

    def read(self, size: int = 1) -> bytes:
        self.read_calls += 1
        if not self.reads:
            return b""
        chunk = self.reads.pop(0)
        if len(chunk) > size:
            self.reads.insert(0, chunk[size:])
        return chunk[:size]


def _bus(port: _Serial) -> MCUBus:
    bus = MCUBus.__new__(MCUBus)
    bus._serial = port
    bus._lock = Lock()
    bus._port = "scripted"
    bus._unresolved_addresses = set()
    return bus


def _assert_fenced(bus: MCUBus, port: _Serial, address: int = 0) -> None:
    assert bus._unresolved_addresses == {address}
    port.reads.append(_response(address=address))  # Looks like a late success.
    with pytest.raises(MCUBusError, match="Unresolved prior transaction"):
        bus.send_command(address, COMMAND, 2, b"", retries=99)
    assert len(port.writes) == 1


@pytest.mark.parametrize("reads,expected", [
    ([], "Timeout"),
    ([b"\x02\x03"], "Partial response"),
    ([b"\x02" * MAX_FRAME_SIZE], "max frame size"),
])
def test_timeout_partial_and_oversize_are_never_resent(reads, expected):
    port = _Serial(reads)
    bus = _bus(port)
    with pytest.raises(MCUBusError, match=expected):
        bus.send_command(0, COMMAND, 3, b"", retries=99)
    assert len(port.writes) == 1
    _assert_fenced(bus, port)


@pytest.mark.parametrize("bad,expected", [
    (_response(address=1), "Response address mismatch"),
    (_response(command=0x15), "Unexpected response command"),
    (_response(command=0x93), "Unexpected response command"),
    (b"\x05\x11\x00", "MCU transaction failed"),
    (bytes(cobs.encode(b"\x00\x14\x00\x00")) + b"\x00", "Invalid response frame length"),
    (bytes(cobs.encode(b"\x00\x14\x00\x00" + b"\x00" * 4)) + b"\x00", "CRC check failed"),
])
def test_invalid_frame_fences_address_without_resend(bad, expected):
    port = _Serial([bad])
    bus = _bus(port)
    with pytest.raises(MCUBusError, match=expected):
        bus.send_command(0, COMMAND, 3, b"", retries=99)
    assert len(port.writes) == 1
    _assert_fenced(bus, port)


@pytest.mark.parametrize("declared,actual", [(0, b"\x01"), (2, b"\x01")])
def test_declared_payload_length_must_match_exact_wire_payload(declared, actual):
    decoded = b"\x00\x14\x00" + bytes([declared]) + actual
    decoded += struct.pack("<I", crc32(decoded))
    port = _Serial([bytes(cobs.encode(decoded)) + b"\x00"])
    bus = _bus(port)
    with pytest.raises(MCUBusError, match="Payload length mismatch"):
        bus.send_command(0, COMMAND, 3, b"", retries=99)
    _assert_fenced(bus, port)


def test_incomplete_command_write_fences_without_resend():
    port = _Serial([_response()], write_length=1)
    bus = _bus(port)
    with pytest.raises(MCUBusError, match="Incomplete command write"):
        bus.send_command(0, COMMAND, 3, b"", retries=99)
    assert port.read_calls == 0
    _assert_fenced(bus, port)


def test_missing_address_does_not_poison_another_address():
    port = _Serial()
    bus = _bus(port)
    with pytest.raises(MCUBusError, match="Timeout"):
        bus.send_command(1, COMMAND, 0, b"", retries=99)
    assert bus._unresolved_addresses == {1}
    port.reads.append(_response(address=2))
    assert bus.send_command(2, COMMAND, 0, b"").dev_address == 2
    assert len(port.writes) == 2


def test_valid_nack_fails_operation_without_fencing():
    port = _Serial([_response(command=COMMAND | 0x80, payload=b"bad channel")])
    bus = _bus(port)
    with pytest.raises(MCUBusError, match="Error response received"):
        bus.send_command(0, COMMAND, 255, b"", retries=99)
    assert len(port.writes) == 1
    assert bus._unresolved_addresses == set()
    port.reads.append(_response(channel=0))
    assert bus.send_command(0, COMMAND, 3, b"").payload == b"\x01"
    assert len(port.writes) == 2


@pytest.mark.parametrize("channel", [0, 2, 255])
def test_response_channel_is_observed_but_not_an_identifier(channel):
    port = _Serial([_response(channel=channel)])
    result = _bus(port).send_command(0, COMMAND, 3, b"")
    assert result.channel == channel
    assert len(port.writes) == 1


def test_waiting_caller_cannot_write_after_first_exchange_becomes_unresolved():
    entered, release, second_started = Event(), Event(), Event()

    class DelayedSerial(_Serial):
        def read(self, size: int = 1) -> bytes:
            entered.set()
            assert release.wait(2)
            return b""

    port = DelayedSerial()
    bus = _bus(port)

    def second_request():
        second_started.set()
        return bus.send_command(0, COMMAND, 2, b"")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(bus.send_command, 0, COMMAND, 3, b"", retries=99)
        try:
            assert entered.wait(2)
            second = pool.submit(second_request)
            assert second_started.wait(2)
            assert len(port.writes) == 1
        finally:
            release.set()
        with pytest.raises(MCUBusError, match="Timeout"):
            first.result(timeout=2)
        with pytest.raises(MCUBusError, match="Unresolved prior transaction"):
            second.result(timeout=2)
    assert len(port.writes) == 1
