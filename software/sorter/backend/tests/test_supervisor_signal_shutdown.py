from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib import request as urllib_request

import pytest

from supervisor import _serve_until_shutdown


class _FakeSupervisor:
    def __init__(self) -> None:
        self.shutdown_requested = False
        self.shutdown_called = False

    def request_shutdown(self) -> None:
        self.shutdown_requested = True

    def shutdown(self) -> None:
        self.shutdown_called = True


class _FakeServer:
    def __init__(self, handlers: dict[int, object]) -> None:
        self._handlers = handlers
        self.shutdown_called = threading.Event()
        self.serve_thread_id: int | None = None
        self.shutdown_thread_id: int | None = None
        self.closed = False

    def serve_forever(self) -> None:
        self.serve_thread_id = threading.get_ident()
        self._handlers[signal.SIGTERM](signal.SIGTERM, None)
        assert self.shutdown_called.wait(timeout=1.0)

    def shutdown(self) -> None:
        self.shutdown_thread_id = threading.get_ident()
        self.shutdown_called.set()

    def server_close(self) -> None:
        self.closed = True


def test_signal_requests_shutdown_from_a_non_serving_thread(monkeypatch) -> None:
    handlers: dict[int, object] = {}
    monkeypatch.setattr(
        signal,
        "signal",
        lambda signum, handler: handlers.__setitem__(signum, handler),
    )
    server = _FakeServer(handlers)
    supervisor = _FakeSupervisor()

    _serve_until_shutdown(server, supervisor)

    assert server.shutdown_called.is_set()
    assert server.shutdown_thread_id != server.serve_thread_id
    assert supervisor.shutdown_requested is True
    assert supervisor.shutdown_called is True
    assert server.closed is True


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal integration test")
def test_sigterm_exits_supervisor_and_backend_process_group() -> None:
    backend_dir = Path(__file__).resolve().parents[1]
    control_port = _unused_loopback_port()
    process = subprocess.Popen(
        [
            sys.executable,
            str(backend_dir / "supervisor.py"),
            "--host",
            "127.0.0.1",
            "--control-port",
            str(control_port),
            "--health-url",
            "http://127.0.0.1:1/health",
            "--health-interval",
            "999",
            "--stop-timeout",
            "1",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
        ],
        cwd=backend_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    backend_pid: int | None = None
    try:
        status_url = f"http://127.0.0.1:{control_port}/api/supervisor/status"
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                with urllib_request.urlopen(status_url, timeout=0.2) as response:
                    status = json.load(response)
                backend_pid = int(status["backend_pid"])
                break
            except Exception:
                if process.poll() is not None:
                    break
                time.sleep(0.05)
        assert backend_pid is not None

        started = time.monotonic()
        os.kill(process.pid, signal.SIGTERM)
        return_code = process.wait(timeout=5.0)

        assert return_code == 0
        assert time.monotonic() - started < 5.0
        deadline = time.monotonic() + 2.0
        while _process_exists(backend_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _process_exists(backend_pid)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2.0)
        if backend_pid is not None and _process_exists(backend_pid):
            os.killpg(backend_pid, signal.SIGKILL)
