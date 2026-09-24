"""Low-level communication protocol implementation for the MCU bus.

The MCUBus class provides methods for sending commands to devices on the bus and receiving their responses,
using a custom binary protocol with COBS (Consistent Overhead Byte Stuffing) framing and CRC32 for message
integrity checking.

The Message format is as follows:

- Header (4 bytes):
    - Address (1 byte): The device address (0-255)
    - Command (1 byte): The command code (0-255)
    - Channel (1 byte): The channel number (0-255)
    - Payload Length (1 byte): The length of the payload in bytes (0-246
- Payload (0-246 bytes): The command payload
- CRC (4 bytes): A CRC32 checksum of the header and payload for error detection

Followed by one or more 0x00 bytes as a message terminator. The total message size (including header,
payload and CRC) must not exceed 254 bytes. All numbers are encoded in little-endian format.

This interface implementation works both for MCUs directly attached through USB (they will always have
address 0) and for devices connected through a multi-drop bus like RS-485, where each device has a unique
address.

The MCUDevice class is intended to be subclassed for specific device types, providing higher-level methods for commands
specific to that device, while the MCUBus class handles the low-level communication details and access to the
(potentially shared) communication bus.
"""

# Copyright (c) 2020-2026 Jose I. Romero
#
# Licensed under the MIT License. See LICENSE file in the project root for full license information.

import json
import logging
import os
from . import cobs
import serial

import struct
import time
from zlib import crc32
from dataclasses import dataclass
from threading import Lock

# Set SORTER_PROFILE_BUS=1 to log how long each bus round-trip blocks the
# caller. Every send_command takes the bus lock, writes, then waits for one
# complete framed MCU reply — there is no queue or worker thread, so
# the time spent here is time the calling thread (e.g. the coordinator loop) is
# stalled. SORTER_PROFILE_BUS_MIN_MS suppresses noise from fast commands.
_PROFILE_BUS = os.environ.get("SORTER_PROFILE_BUS") == "1"
_PROFILE_BUS_MIN_MS = float(os.environ.get("SORTER_PROFILE_BUS_MIN_MS", "20"))


MAX_PAYLOAD_SIZE = (
    254 - 8
)  # Max total message size is 254, header is 4 bytes, CRC is 4 bytes


@dataclass
class MessageHeader:
    address: int
    command: int
    channel: int
    payload_length: int


@dataclass
class Message:
    dev_address: int
    command: int
    channel: int
    payload: bytes


class BaseCommandCode:
    INIT = 0x00
    PING = 0x01
    REBOOT_BOOTLOADER = 0x02
    GET_OBSERVABILITY = 0x03
    GET_VERSION = 0x04


class MCUBusError(Exception):
    """Base exception class for errors related to the MCUBus communication."""

    pass


class _IncompleteResponseError(MCUBusError):
    """A response started arriving but has not reached its COBS delimiter yet."""

    pass


class _MCUNackError(MCUBusError):
    """An application-level error response from the MCU, which is not retryable."""

    pass


class MCUBus:
    """Class for communicating with the MCU over a serial bus using a custom protocol."""

    def __init__(self, port: str, baudrate: int = 576000, timeout: float = 0.1):
        """Initialize the MCUBus with the given serial port parameters.

        Args:
            port: The serial port to use (e.g. "/dev/ttyUSB0")
            baudrate: The baud rate for the serial communication (default 576000)
            timeout: The read timeout in seconds (default 0.01s = 10ms)
        """

        self._serial = serial.Serial(port, baudrate=baudrate, timeout=timeout)
        self._lock = Lock()
        self._port = port
        self._timeout = timeout
        self._rx_buffer = bytearray()

    @property
    def port(self) -> str:
        return self._port

    @property
    def is_open(self) -> bool:
        return bool(self._serial.is_open)

    def close(self) -> None:
        # Releases the tty fd so an external actor (e.g. the firmware flasher
        # waiting for the Pico to re-enumerate) sees a clean device. Safe to
        # call twice. Any in-flight send_command finishes first via the lock.
        with self._lock:
            try:
                self._serial.close()
            except Exception:
                pass

    def send_command_no_response(
        self, address: int, command: int, channel: int, payload: bytes = b""
    ) -> None:
        # For commands after which the MCU cannot reply (e.g. REBOOT_BOOTLOADER
        # 0x02 — the chip resets into the UF2 bootloader immediately). A normal
        # send_command would burn its timeout waiting for a response that will
        # never come, then raise.
        message = (
            struct.pack("<BBBB", address, command, channel, len(payload)) + payload
        )
        message += struct.pack("<I", crc32(message))
        encoded_message = cobs.encode(message) + b"\x00"
        with self._lock:
            self._serial.reset_input_buffer()
            self._serial.write(encoded_message)
            self._serial.flush()

    def _read_response_frame(self) -> bytearray:
        """Read one complete COBS frame without discarding an in-progress reply.

        PySerial's ``read_until`` requests one byte per read.  On a busy process,
        that makes a short USB CDC response vulnerable to the transaction timeout
        between bytes.  Read the bytes that are already queued as a chunk instead,
        and retain an incomplete frame so the same command can continue waiting for
        its reply instead of retransmitting and flushing its tail.
        """
        deadline = time.monotonic() + self._timeout
        while True:
            delimiter = self._rx_buffer.find(b"\x00")
            if delimiter >= 0:
                frame = self._rx_buffer[: delimiter + 1]
                del self._rx_buffer[: delimiter + 1]
                # Consecutive delimiters are framing noise, not a response.
                if len(frame) > 1:
                    return frame
                continue

            if len(self._rx_buffer) >= 254:
                self._rx_buffer.clear()
                raise MCUBusError("Response exceeded max frame size before terminator")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if self._rx_buffer:
                    raise _IncompleteResponseError(
                        f"Partial response (missing terminator), got {len(self._rx_buffer)} bytes"
                    )
                raise MCUBusError("Timeout waiting for response terminator (0x00)")

            # The first byte may require a blocking wait.  Once the CDC driver has
            # queued data, consume all currently available bytes in one read.
            read_size = min(
                254 - len(self._rx_buffer),
                max(1, int(self._serial.in_waiting)),
            )
            previous_timeout = self._serial.timeout
            try:
                self._serial.timeout = remaining
                chunk = self._serial.read(read_size)
            finally:
                self._serial.timeout = previous_timeout

            if not chunk:
                if self._rx_buffer:
                    raise _IncompleteResponseError(
                        f"Partial response (missing terminator), got {len(self._rx_buffer)} bytes"
                    )
                raise MCUBusError("Timeout waiting for response terminator (0x00)")
            self._rx_buffer.extend(chunk)

    @staticmethod
    def _decode_response(
        resp_buf: bytearray,
        expected_address: int,
        expected_command: int,
    ) -> Message:
        logging.debug(f"Received: {resp_buf.hex(b' ', 1)}")
        decoded_resp = cobs.decode(resp_buf[:-1])  # Exclude terminator

        if crc32(decoded_resp[:-4]) != struct.unpack("<I", decoded_resp[-4:])[0]:
            raise MCUBusError("CRC check failed")

        response_header = MessageHeader(*struct.unpack("<BBBB", decoded_resp[:4]))
        message = Message(
            dev_address=response_header.address,
            command=response_header.command,
            channel=response_header.channel,
            payload=bytes(decoded_resp[4:-4][: response_header.payload_length]),
        )

        if response_header.payload_length != len(message.payload):
            raise MCUBusError(
                f"Payload length mismatch: expected {response_header.payload_length}, got {len(message.payload)}"
            )

        if message.dev_address != expected_address:
            raise MCUBusError(
                f"Response address mismatch: expected {expected_address}, got {message.dev_address}"
            )

        if message.command & 0x80:
            raise _MCUNackError(
                f"Error response received, command: {message.command:#04x}, payload: {message.payload}"
            )

        if message.command != expected_command:
            raise MCUBusError(
                f"Response command mismatch: expected {expected_command:#04x}, got {message.command:#04x}"
            )

        # The firmware response header carries the device address and command,
        # but handlers do not populate its channel field. It is therefore not a
        # transaction-correlating value and is commonly zero for motor commands.

        return message

    def send_command(
        self,
        address: int,
        command: int,
        channel: int,
        payload: bytes,
        *,
        retries: int = 2,
    ) -> Message:
        """Send a command to the MCU and return the response from it.

        Args:
            address: The device address (0-255)
            command: The command code (0-255)
            channel: The channel number (0-255)
            payload: The command payload (0-246 bytes)
            retries: Extra attempts on transient framing/CRC errors before
                giving up (default 2 → 3 attempts total). Pass 0 for paths
                where a missing device is expected (e.g. bus discovery).

        Returns:
            A Message object containing the response from the MCU.

        Raises:
            ValueError: If any of the input parameters are out of range or if the payload is too large.
            MCUBusError: If there is a communication error, CRC check failure, or if the response indicates an error.
        """
        payload_length = len(payload)
        # Validate inputs
        if payload_length > MAX_PAYLOAD_SIZE:
            raise ValueError(
                f"Payload too large: {payload_length} bytes (max {MAX_PAYLOAD_SIZE})"
            )
        if address < 0 or address > 255:
            raise ValueError(f"Address must be 0-255, got {address}")
        if command < 0 or command > 255:
            raise ValueError(f"Command must be 0-255, got {command}")
        if channel < 0 or channel > 255:
            raise ValueError(f"Channel must be 0-255, got {channel}")
        # Construct message
        message = (
            struct.pack("<BBBB", address, command, channel, payload_length) + payload
        )
        # Append CRC
        crc = crc32(message)
        message += struct.pack("<I", crc)
        logging.debug(
            f"Raw message: {message[:-4].hex(b' ', 1)}, CRC: {crc32(message[:-4]):08X}, Length: {len(message)-4}"
        )
        encoded_message = cobs.encode(message) + b"\x00"
        logging.debug(f"Sending: {encoded_message.hex(b' ', 1)}")

        # Keep the complete transaction under one lock.  In particular, a caller
        # must never insert another command between a timed-out partial response
        # and the rest of that same response.
        attempts = max(1, retries + 1)
        last_exc: MCUBusError | None = None
        # A prior caller may have timed out after receiving only part of its
        # response.  Finish that frame before putting another command on the
        # wire; otherwise its tail can be mistaken for this transaction.
        resend = not bool(self._rx_buffer)
        with self._lock:
            for attempt in range(attempts):
                try:
                    if resend:
                        self._serial.write(encoded_message)
                    resp_buf = self._read_response_frame()
                    return self._decode_response(resp_buf, address, command)
                except _MCUNackError:
                    raise
                except MCUBusError as exc:
                    last_exc = exc
                    # Once any response byte arrives, it belongs to the command
                    # already on the wire.  Preserve it and continue reading
                    # rather than retransmitting a potentially non-idempotent
                    # command or flushing its terminator.
                    resend = not bool(self._rx_buffer)
                    if attempt + 1 < attempts:
                        logging.warning(
                            "MCU bus transient error (attempt %d/%d) addr=%d cmd=%#04x ch=%d: %s",
                            attempt + 1,
                            attempts,
                            address,
                            command,
                            channel,
                            exc,
                        )
                        time.sleep(0.005 * (attempt + 1))
                        continue
                    raise
        # Loop must exit via return or raise — defensive fallthrough
        raise last_exc if last_exc is not None else MCUBusError("send_command exhausted retries with no error")

    @classmethod
    def enumerate_buses(cls, vid=0x2E8A, pid=0x000A) -> list[str]:
        """Enumerate available serial ports that could be used for the MCU bus. Filtered by VID and PID

        Args:
            vid: The USB Vendor ID to filter by (default 0x2e8a, Raspberry Pi Foundation)
            pid: The USB Product ID to filter by (default 0x000a, Pico SDK CDC UART)

        Returns:
            A list of serial port names (e.g. ["/dev/ttyUSB0", "/dev/ttyUSB1"])
        """
        import serial.tools.list_ports

        ports = serial.tools.list_ports.comports()
        return [port.device for port in ports if port.vid == vid and port.pid == pid]

    def scan_devices(self, min_address=0, max_address=15) -> list[int]:
        """Scan the bus for devices by sending a ping command to each address in the specified range.

        Note: this process can take a long time since it waits for a timeout for each address that doesn't respond.
        The default range is 0-15 since we expect only a few devices on the bus, but this can be adjusted as needed.

        Args:
            min_address: The minimum device address to scan (default 0)
            max_address: The maximum device address to scan (default 15)

        Returns:
            A list of device addresses that responded to the ping command.
        """
        found_devices = []
        for addr in range(min_address, max_address + 1):
            try:
                self.send_command(addr, BaseCommandCode.PING, 0, b"", retries=0)
                found_devices.append(addr)
                logging.debug(f"Device found at address {addr}")
            except Exception as e:
                logging.debug(f"No response from address {addr}: {e}")
                pass
        return found_devices


class MCUDevice:
    """Higher-level abstraction for a device on the MCU bus, providing methods for common commands."""

    def __init__(self, bus: MCUBus, address: int):
        self._bus = bus
        self._address = address

    def send_command(self, command: int, channel: int, payload: bytes) -> Message:
        """Send a command to this device and return the response.

        This is a synchronous request/response round-trip — there is no queue
        or worker thread. The calling thread blocks here until the MCU replies
        (or the serial read times out). Set SORTER_PROFILE_BUS=1 to log how long
        each call blocks the caller.
        """
        if not _PROFILE_BUS:
            return self._bus.send_command(self._address, command, channel, payload)
        started = time.perf_counter()
        try:
            return self._bus.send_command(self._address, command, channel, payload)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            if elapsed_ms >= _PROFILE_BUS_MIN_MS:
                logging.warning(
                    "MCU bus blocked caller %.1fms (addr=%d cmd=%#04x ch=%d)",
                    elapsed_ms, self._address, command, channel,
                )

    def ping(self, payload: bytes = b"") -> bytes:
        """Send a ping command and return the device's echoed payload bytes.

        A successful response implies the device is responsive; the returned value
        is the raw bytes payload echoed by the device.
        """
        return self.send_command(BaseCommandCode.PING, 0, payload).payload
    
    def detect(self) -> dict:
        """Send an init command to the device to check if it's responsive and properly initialized."""
        res = self.send_command(BaseCommandCode.INIT, 0, b"") # Returns a JSON string with device info if successful
        info_str = res.payload.decode("utf-8")
        return json.loads(info_str)

    def get_version(self) -> dict:
        res = self.send_command(BaseCommandCode.GET_VERSION, 0, b"")
        info_str = res.payload.decode("utf-8")
        return json.loads(info_str)



if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    print("Enumerating buses...")
    buses = MCUBus.enumerate_buses()
    print(f"Available buses: {buses}")
    if not buses:
        print("No buses found, exiting.")
    else:
        print(f"Testing bus on port {buses[0]}...")
        bus = MCUBus(port=buses[0])
        devices = bus.scan_devices()
        print(f"Devices found: {devices}")
        if devices:
            device = MCUDevice(bus, devices[0])
            response = device.ping(b"Hello")
            print(f"Ping response: {response}")
            info = device.detect()
            print(f"Device info: {info}")
