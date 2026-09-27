"""Isolated tests for the Pi bridge's network advertisement lifecycle."""

from __future__ import annotations

import importlib.util
import socket
import struct
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch


SCRIPT = Path(__file__).resolve().parents[1] / "raspberry_pi_camera_bridge.py"
SPEC = importlib.util.spec_from_file_location(
    "raspberry_pi_camera_bridge_under_test", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
bridge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bridge
if sys.platform == "win32":
    # The bridge runs on Linux. Stub its existing fcntl import so these
    # discovery tests can exercise the logic without a camera or Linux host.
    fcntl_stub = types.ModuleType("fcntl")
    fcntl_stub.ioctl = lambda *_args: (_ for _ in ()).throw(OSError("stub"))
    with patch.dict(sys.modules, {"fcntl": fcntl_stub}):
        SPEC.loader.exec_module(bridge)
else:
    SPEC.loader.exec_module(bridge)


def _ifreq(address: str, flags: int) -> bytes:
    result = bytearray(256)
    result[16:18] = struct.pack("H", flags)
    result[20:24] = socket.inet_aton(address)
    return bytes(result)


class AdvertisementInterfaceTests(unittest.TestCase):
    def _ready_with(self, address: str, flags: int) -> bool:
        probe = MagicMock()
        probe.__enter__.return_value.fileno.return_value = 9

        def ioctl(_fd: int, operation: int, _request: bytes) -> bytes:
            if operation == bridge.SIOCGIFADDR:
                return _ifreq(address, flags)
            if operation == bridge.SIOCGIFFLAGS:
                return _ifreq(address, flags)
            raise AssertionError(f"unexpected ioctl: {operation}")

        with (
            patch.object(bridge.socket, "if_nameindex", return_value=[(2, "eth0")]),
            patch.object(bridge.socket, "socket", return_value=probe),
            patch.object(bridge.fcntl, "ioctl", side_effect=ioctl),
        ):
            return bridge._advertise_ip_ready()

    def test_requires_configured_address_and_live_non_loopback_link(self) -> None:
        live = bridge.IFF_UP | bridge.IFF_RUNNING
        self.assertTrue(self._ready_with(bridge.ADVERTISE_IP, live))
        self.assertFalse(self._ready_with("192.0.2.4", live))
        self.assertFalse(self._ready_with(bridge.ADVERTISE_IP, bridge.IFF_UP))
        self.assertFalse(self._ready_with(bridge.ADVERTISE_IP, bridge.IFF_RUNNING))
        self.assertFalse(
            self._ready_with(bridge.ADVERTISE_IP, live | bridge.IFF_LOOPBACK)
        )

    def test_missing_ipv4_interface_is_not_ready(self) -> None:
        probe = MagicMock()
        probe.__enter__.return_value.fileno.return_value = 9
        with (
            patch.object(bridge.socket, "if_nameindex", return_value=[(2, "eth0")]),
            patch.object(bridge.socket, "socket", return_value=probe),
            patch.object(bridge.fcntl, "ioctl", side_effect=OSError("no address")),
        ):
            self.assertFalse(bridge._advertise_ip_ready())


class AdvertisementLifecycleTests(unittest.TestCase):
    def test_registers_existing_service_on_configured_interface(self) -> None:
        zeroconf = MagicMock()
        with patch.object(bridge, "Zeroconf", return_value=zeroconf) as constructor:
            result = bridge._advertise()

        self.assertIs(result, zeroconf)
        constructor.assert_called_once_with(
            interfaces=[bridge.ADVERTISE_IP], ip_version=bridge.IPVersion.V4Only
        )
        info = zeroconf.register_service.call_args.args[0]
        self.assertEqual(info.type, "_legosorter-camera._tcp.local.")
        self.assertEqual(info.addresses, [socket.inet_aton(bridge.ADVERTISE_IP)])
        self.assertEqual(info.port, bridge.PORT)
        self.assertEqual(info.properties[b"path"], b"/video")
        self.assertEqual(info.properties[b"snapshot"], b"/snapshot.jpg")
        self.assertEqual(info.properties[b"health"], b"/health")
        self.assertEqual(zeroconf.register_service.call_args.kwargs, {"strict": False})

    def test_failed_registration_closes_socket_for_retry(self) -> None:
        zeroconf = MagicMock()
        zeroconf.register_service.side_effect = OSError("network changed")
        with patch.object(bridge, "Zeroconf", return_value=zeroconf):
            with self.assertRaises(OSError):
                bridge._advertise()
        zeroconf.close.assert_called_once_with()

    def test_late_link_and_rejoin_recreate_advertisement_once(self) -> None:
        first = MagicMock()
        second = MagicMock()
        with (
            patch.object(
                bridge,
                "_advertise_ip_ready",
                side_effect=[False, True, True, False, True],
            ),
            patch.object(
                bridge, "_advertise", side_effect=[first, second]
            ) as advertise,
            patch.object(bridge, "_close_advertisement") as close,
        ):
            current = None
            states = []
            for _ in range(5):
                current = bridge._reconcile_advertisement(current)
                states.append(current)

        self.assertEqual(states, [None, first, first, None, second])
        self.assertEqual(advertise.call_count, 2)
        close.assert_called_once_with(first)

    def test_loop_retries_failure_and_closes_registration_on_stop(self) -> None:
        zeroconf = MagicMock()
        stop = MagicMock()
        stop.is_set.side_effect = [False, False, True]
        with (
            patch.object(
                bridge,
                "_reconcile_advertisement",
                side_effect=[OSError("temporary"), zeroconf],
            ) as reconcile,
            patch.object(bridge, "_close_advertisement") as close,
            patch.object(bridge.LOG, "exception"),
        ):
            bridge._advertisement_loop(stop)

        self.assertEqual(reconcile.call_args_list, [call(None), call(None)])
        self.assertEqual(stop.wait.call_args_list, [call(1.0), call(1.0)])
        close.assert_called_once_with(zeroconf)


if __name__ == "__main__":
    unittest.main()
