"""Chunk acquisition and candidate transaction safety, using virtual serial time.

Adapted from upstream 94622df0. Transfer scheduling and per-call overhead are
deterministic; no sleeps, real ports, or physical performance claims.
"""

from __future__ import annotations

import struct
from threading import Lock
from types import SimpleNamespace
from zlib import crc32

import pytest
from serial.serialutil import SerialBase

from hardware import bus as bus_module, cobs
from hardware.bus import MCUBus, MCUBusError

GET_STALL_STATUS = 0x1B


def _frame(command=GET_STALL_STATUS, channel=0, payload=b"\x01", address=0):
    data = struct.pack("<BBBB", address, command, channel, len(payload)) + payload
    return bytes(cobs.encode(data + struct.pack("<I", crc32(data)))) + b"\x00"


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class _ScriptedPort(SerialBase):
    """Offsets model USB arrivals; an empty read means the timeout expired."""

    def __init__(self, clock, chunks, *, read_call_s=0.0, timeout=0.1):
        super().__init__(timeout=timeout)
        self.clock = clock
        self.chunks = list(chunks)
        self.read_call_s = read_call_s
        self.written_at = None
        self.consumed = 0
        self.writes = []
        self.requested = []
        self.returned = []
        self.read_until_calls = 0
        self.flush_calls = 0

    def _arrived(self):
        if self.written_at is None:
            return b""
        elapsed = self.clock.now - self.written_at
        return b"".join(data for offset, data in self.chunks if offset <= elapsed)

    @property
    def in_waiting(self):
        return len(self._arrived()) - self.consumed

    def reset_input_buffer(self):
        self.consumed = len(self._arrived())

    def write(self, data):
        self.writes.append(bytes(data))
        self.written_at = self.clock.now
        self.consumed = 0
        return len(data)

    def read(self, size=1):
        self.requested.append(size)
        self.clock.now += self.read_call_s
        deadline = self.clock.now + self.timeout
        if self.in_waiting < size:
            for offset, _ in self.chunks:
                arrival = self.written_at + offset
                if self.clock.now < arrival <= deadline:
                    self.clock.now = arrival
                    if self.in_waiting >= size:
                        break
            if self.in_waiting < size:
                self.clock.now = deadline
        data = self._arrived()[self.consumed:self.consumed + size]
        self.consumed += len(data)
        self.returned.append(data)
        return data

    def read_until(self, *args, **kwargs):
        # Used only by the isolated accepted-C01 reproduction. Final tests
        # assert this inherited byte-at-a-time path was never entered.
        self.read_until_calls += 1
        return super().read_until(*args, **kwargs)

    def flush(self):
        self.flush_calls += 1


def _bus(port, cls=MCUBus):
    result = cls.__new__(cls)
    result._serial = port
    result._lock = Lock()
    result._port = "scripted"
    result._unresolved_addresses = set()
    return result


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(bus_module, "time", SimpleNamespace(monotonic=clock.monotonic))
    return clock


def _send(bus, *, retries=2):
    return bus.send_command(0, GET_STALL_STATUS, 3, b"", retries=retries)


def test_complete_already_arrived_frame_consumed_in_one_chunk(clock):
    reply = _frame()
    assert len(reply) == 11
    port = _ScriptedPort(clock, [(0, reply)])
    assert _send(_bus(port)).payload == b"\x01"
    assert port.requested == [11]
    assert port.read_until_calls == 0
    assert len(port.writes) == 1


def test_whole_reply_is_read_even_when_each_call_is_slow(clock):
    port = _ScriptedPort(clock, [(0.002, _frame())], read_call_s=0.015)
    assert _send(_bus(port)).payload == b"\x01"
    assert port.requested == [1, 10]
    assert port.read_until_calls == 0
    assert len(port.writes) == 1


def test_split_transfers_wait_through_empty_gap_within_serial_timeout(clock):
    reply = _frame()
    port = _ScriptedPort(clock, [(0, reply[:4]), (0.05, reply[4:])])
    assert _send(_bus(port)).payload == b"\x01"
    assert port.requested == [4, 1, 6]
    assert clock.now == 0.05
    assert all(port.returned)  # No data waiting is different from a timed-out read.
    assert port.timeout == 0.1
    assert len(port.writes) == 1


@pytest.mark.parametrize("trailing", [b"\x00", b"\x00\x00", _frame(payload=b"\x02")])
def test_first_terminator_wins_and_trailing_bytes_are_not_carried_forward(clock, trailing):
    port = _ScriptedPort(clock, [(0, _frame() + trailing)])
    bus = _bus(port)
    assert _send(bus).payload == b"\x01"
    assert port.requested == [11 + len(trailing)]
    # Next request has no response: no persistent receive buffer may supply it.
    port.chunks = []
    with pytest.raises(MCUBusError, match="Timeout"):
        _send(bus)
    assert len(port.writes) == 2


def test_silent_mcu_times_out_once_and_fences_address(clock):
    port = _ScriptedPort(clock, [])
    bus = _bus(port)
    with pytest.raises(MCUBusError, match="Timeout waiting for response terminator"):
        _send(bus, retries=99)
    assert port.requested == [1]
    assert clock.now == port.timeout
    with pytest.raises(MCUBusError, match="Unresolved prior transaction"):
        _send(bus)
    assert len(port.writes) == 1


@pytest.mark.parametrize("late", [False, True])
def test_unfinished_frame_stops_at_first_empty_read_and_fences_late_reply(clock, late):
    reply = _frame()
    chunks = [(0, reply[:6])]
    if late:
        chunks.append((0.15, reply[6:]))
    port = _ScriptedPort(clock, chunks)
    bus = _bus(port)
    with pytest.raises(MCUBusError, match="Partial response.*6 bytes"):
        _send(bus, retries=99)
    assert port.requested == [6, 1]
    assert port.returned[-1] == b""
    clock.now = 0.2
    with pytest.raises(MCUBusError, match="Unresolved prior transaction"):
        _send(bus)
    assert len(port.writes) == 1


@pytest.mark.parametrize("prefix_size", [0, 200])
def test_oversize_unterminated_frame_never_requests_beyond_capacity(clock, prefix_size):
    chunks = [(0, b"\x01" * prefix_size), (0.01, b"\x01" * 300)] if prefix_size else [(0, b"\x01" * 300)]
    port = _ScriptedPort(clock, chunks)
    bus = _bus(port)
    with pytest.raises(MCUBusError, match="exceeded max frame size"):
        _send(bus, retries=99)
    consumed = 0
    for size, returned in zip(port.requested, port.returned):
        assert 1 <= size <= 254 - consumed
        consumed += len(returned)
    assert consumed == 254
    assert bus._unresolved_addresses == {0}
    assert len(port.writes) == 1


def test_terminator_at_exact_capacity_is_accepted(clock):
    reply = _frame(payload=b"\x01" * 244)
    assert len(reply) == 254
    port = _ScriptedPort(clock, [(0, reply)])
    assert _send(_bus(port)).payload == b"\x01" * 244
    assert port.requested == [254]


def test_total_frame_deadline_bounds_continuously_arriving_data(clock):
    port = _ScriptedPort(clock, [(i * 0.04, b"\x01") for i in range(100)])
    bus = _bus(port)
    with pytest.raises(MCUBusError, match="Partial response"):
        _send(bus)
    assert 1.0 <= clock.now <= 1.0 + port.timeout
    assert len(port.returned) < 30
    assert bus._unresolved_addresses == {0}
    assert len(port.writes) == 1


def test_deadline_prevents_another_read_after_slow_chunk(clock):
    port = _ScriptedPort(clock, [(0, b"\x01")], read_call_s=1.0)
    with pytest.raises(MCUBusError, match="Partial response"):
        _send(_bus(port))
    assert port.requested == [1]
    assert len(port.writes) == 1


@pytest.mark.parametrize("bad", [
    _frame(address=1), _frame(command=0x14), b"\x05\x11\x00",
    bytes(cobs.encode(b"\x00\x1b\x00\x00")) + b"\x00",
    bytes(cobs.encode(b"\x00\x1b\x00\x00" + b"\x00" * 4)) + b"\x00",
    b"\x00",
])
def test_malformed_or_unrelated_reply_writes_once_and_blocks_late_response(clock, bad):
    port = _ScriptedPort(clock, [(0, bad)])
    bus = _bus(port)
    with pytest.raises(MCUBusError):
        _send(bus, retries=99)
    port.chunks = [(0, _frame())]
    with pytest.raises(MCUBusError, match="Unresolved prior transaction"):
        _send(bus)
    assert bus._unresolved_addresses == {0}
    assert len(port.writes) == 1


@pytest.mark.parametrize("written", [0, 5])
def test_incomplete_write_is_ambiguous_and_never_retried(clock, written):
    class ShortWrite(_ScriptedPort):
        def write(self, data):
            super().write(data)
            return written

    port = ShortWrite(clock, [(0, _frame())])
    bus = _bus(port)
    with pytest.raises(MCUBusError, match="Incomplete command write"):
        _send(bus, retries=99)
    with pytest.raises(MCUBusError, match="Unresolved prior transaction"):
        _send(bus)
    assert port.requested == []
    assert len(port.writes) == 1


def test_valid_nack_does_not_fence_or_resend(clock):
    port = _ScriptedPort(clock, [(0, _frame(command=GET_STALL_STATUS | 0x80))])
    bus = _bus(port)
    with pytest.raises(MCUBusError, match="Error response received"):
        _send(bus, retries=99)
    assert bus._unresolved_addresses == set()
    assert len(port.writes) == 1
    port.chunks = [(0, _frame(channel=255))]
    assert _send(bus).channel == 255
    assert len(port.writes) == 2


def test_no_response_command_still_writes_and_flushes_without_read(clock):
    port = _ScriptedPort(clock, [])
    _bus(port).send_command_no_response(0, 2, 0)
    assert len(port.writes) == 1
    assert port.flush_calls == 1
    assert port.requested == []
