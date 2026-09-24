#!/usr/bin/env python3
"""Continue the exact prepared install from the verified stopped child."""

import hashlib
import fcntl
import json
import os
import pathlib
import stat
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.request


if os.geteuid() != 0 or sys.executable != "/usr/bin/python3":
    raise SystemExit("must run with the verified /usr/bin/python3 privileged interpreter")

ROOT = pathlib.Path(
    "/home/ubuntu/sorter-upstream-fresh-20260919/software/sorter/backend"
)
STAGE = pathlib.Path("/tmp/c4-camera-perf-20260921")
BACKUP = pathlib.Path("/home/ubuntu/deployment-backups/c4-camera-perf-20260921-r2")
FILES = {
    "vision/camera.py": {
        "old": "cfcd8951808d437676b8cf29c0677f1760ec691fba597bd06e4ee7ce98e0ea35",
        "new": "2b29f03acb2d05e078a9cbddd1c0d8064bd6dceeff3317f4d1fb615570735e48",
        "bytes": 50780,
    },
    "vision/camera_service.py": {
        "old": "0ac8d6301e1c010e3e8ec2018e5c8bd8f330a629821ad2b42370775809ff7048",
        "new": "dcbbbce93402ef43003a5be1cca69d144d9c95840031e02b40f1423fa73b7ae8",
        "bytes": 16136,
    },
    "perception/inference.py": {
        "old": "f2c88b8181c43e3f428a5a62bf3234af294c3df7db3449e77b97f8fd487f7ea5",
        "new": "c982de0cdd45f1ed861f0723b4d1a8d390afc6ab7be726d41c7d77dfc9608afc",
        "bytes": 38512,
    },
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save_json(path, value):
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save_json(path, value):
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def request(path, port=8000, post=False, timeout=10):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=b"{}" if post else None,
        headers={"Origin": "http://127.0.0.1", "Content-Type": "application/json"},
        method="POST" if post else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read()
        return json.loads(body) if body else {"http_status": response.status}


def process_identity(pid):
    proc = pathlib.Path("/proc") / str(pid)
    fields = (proc / "stat").read_text().split(") ", 1)[1].split()
    return {
        "pid": pid,
        "ppid": int(fields[1]),
        "pgid": int(fields[2]),
        "start_ticks": int(fields[19]),
        "exe": os.readlink(proc / "exe"),
        "cmdline": (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(),
    }


def file_state(relative):
    path = ROOT / relative
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"target is not a regular file: {path}")
    flag_fd = os.open(path, os.O_RDONLY)
    try:
        flag_buf = bytearray(4)
        fcntl.ioctl(flag_fd, 0x80086601, flag_buf, True)
        flags = struct.unpack("I", flag_buf)[0]
    finally:
        os.close(flag_fd)
    if flags & (0x10 | 0x20):
        raise RuntimeError(f"immutable/append-only source file: {path}")
    return {
        "sha256": digest(path.read_bytes()),
        "uid": info.st_uid,
        "gid": info.st_gid,
        "mode": stat.S_IMODE(info.st_mode),
        "device": info.st_dev,
        "filesystem_flags": flags,
    }


def make_temp(path, data):
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".c4-camera-perf.", dir=path.parent)
    try:
        os.fchown(fd, 1000, 1000)
        os.fchmod(fd, 0o664)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            fd = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        info = os.stat(name)
        if digest(pathlib.Path(name).read_bytes()) != digest(data):
            raise RuntimeError(f"prepared replacement bytes mismatch: {path}")
        if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (1000, 1000, 0o664):
            raise RuntimeError(f"prepared replacement metadata mismatch: {path}")
        return pathlib.Path(name)
    except Exception:
        if fd >= 0:
            os.close(fd)
        if os.path.exists(name):
            os.unlink(name)
        raise


def wait_started(timeout=120):
    deadline = time.monotonic() + timeout
    identity = None
    stable = 0
    while time.monotonic() < deadline:
        try:
            supervisor = request("/api/supervisor/status", port=8001)
            if supervisor.get("crash_looping") or supervisor.get("consecutive_fast_crashes", 0) >= 2:
                raise RuntimeError("supervisor reports repeated backend crashes")
            pid = supervisor.get("backend_pid")
            if pid:
                current = process_identity(pid)
                runtime = request("/runtime-stats").get("payload", {})
                healthy = (
                    supervisor.get("backend_running") is True
                    and supervisor.get("backend_healthy") is True
                    and not supervisor.get("restart_requested")
                    and runtime.get("is_running") is False
                    and "transfer_throughput" in runtime
                    and request("/health") == {"status": "ok"}
                )
                if current != identity:
                    identity, stable = current, 0
                stable = stable + 1 if healthy else 0
                if stable >= 3:
                    return identity
            else:
                stable = 0
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError, FileNotFoundError, ProcessLookupError):
            stable = 0
        time.sleep(2)
    raise RuntimeError("bounded 120-second inert startup was not established")


def restore_originals(originals):
    for relative, data in originals.items():
        target = ROOT / relative
        temp = make_temp(target, data)
        os.replace(temp, target)
        sync_dir(target.parent)
        if digest(target.read_bytes()) != FILES[relative]["old"]:
            raise RuntimeError(f"rollback hash mismatch: {relative}")


def main():
    preflight = json.loads((BACKUP / "preflight.json").read_text())
    failure = json.loads((BACKUP / "failure.json").read_text())
    if failure.get("candidate_start_requested") or failure.get("applied"):
        raise RuntimeError("prior attempt may have changed code or started candidate; manual reconciliation required")
    if failure.get("error") != "TimeoutError('timed out')":
        raise RuntimeError("prior stop receipt is not the expected reconciled timeout")

    supervisor = request("/api/supervisor/status", port=8001)
    if not (
        supervisor.get("supervisor_state") == "stopped"
        and supervisor.get("backend_running") is False
        and supervisor.get("backend_pid") is None
        and supervisor.get("restart_requested") is False
        and supervisor.get("last_exit_reason") == "manual stop requested"
        and not supervisor.get("crash_looping")
    ):
        raise RuntimeError(f"fresh supervisor state does not prove the requested stop: {supervisor}")
    if not preflight["live_before"]["hardware"]["tmc_standstill"] or not preflight["live_before"]["hardware"]["operator_physically_confirmed_stopped"]:
        raise RuntimeError("saved fresh stop evidence is missing")
    if preflight["live_before"]["runtime"]["pieces_cached"] != 0 or preflight["live_before"]["c4_owners"]["active_piece_uuids"]:
        raise RuntimeError("saved current owner ledger was not empty")
    incident = preflight["live_before"]["runtime"]["active_incident"]
    if not (incident.get("channel") == "c4" and incident.get("reason") == "C4 perception is stale or has an invalid timestamp"):
        raise RuntimeError("saved incident is not the intended matching C4 freshness hold")

    originals = {}
    payloads = {}
    for relative, expected in FILES.items():
        path = ROOT / relative
        state = file_state(relative)
        recorded = preflight["original_metadata_and_hashes"][relative]
        if state != recorded or state["sha256"] != expected["old"]:
            raise RuntimeError(f"installed bytes or metadata changed before continuation: {relative}: {state}")
        backup = BACKUP / relative.replace("/", "__")
        originals[relative] = backup.read_bytes()
        if digest(originals[relative]) != expected["old"]:
            raise RuntimeError(f"rollback backup hash mismatch: {relative}")
        payload = (STAGE / pathlib.Path(relative).name).read_bytes()
        if len(payload) != expected["bytes"] or digest(payload) != expected["new"]:
            raise RuntimeError(f"staged payload hash/size mismatch: {relative}")
        compile(payload, relative, "exec")
        payloads[relative] = payload

    if supervisor.get("backend_pid") is not None:
        raise RuntimeError("backend became active during deployment reconciliation")

    temps = {relative: make_temp(ROOT / relative, payloads[relative]) for relative in FILES}
    applied = []
    candidate_start_requested = False
    try:
        # The prior supported Stop is now freshly proven complete. Recheck the
        # exact baseline and replace only the three prepared source files.
        for relative, temp in temps.items():
            target = ROOT / relative
            if digest(target.read_bytes()) != FILES[relative]["old"]:
                raise RuntimeError(f"baseline changed immediately before install: {relative}")
            os.replace(temp, target)
            sync_dir(target.parent)
            if digest(target.read_bytes()) != FILES[relative]["new"]:
                raise RuntimeError(f"installed hash mismatch: {relative}")
            applied.append(relative)
        installed = {relative: file_state(relative) for relative in FILES}
        save_json(BACKUP / "installed.json", {
            "at_epoch_s": time.time(),
            "supervisor_state_before_start": supervisor,
            "installed": installed,
        })

        candidate_start_requested = True
        try:
            start_response = request("/api/supervisor/start", port=8001, post=True, timeout=15)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            # A start response may be lost after acceptance. Observe status;
            # never repeat an uncertain start POST.
            start_response = {"response_unavailable": repr(exc)}
        new_identity = wait_started()
        old_identity = preflight["live_before"]["backend_identity"]
        if new_identity == old_identity:
            raise RuntimeError("backend identity did not change across the verified restart")
        final_files = {relative: file_state(relative) for relative in FILES}
        if any(final_files[key]["sha256"] != FILES[key]["new"] for key in FILES):
            raise RuntimeError("deployed hashes changed after backend startup")
        system = request("/api/system/status")
        runtime = request("/runtime-stats").get("payload", {})
        if runtime.get("is_running") is not False:
            raise RuntimeError("backend entered RUNNING during inert startup")
        if system.get("hardware_state") != "standby" or system.get("hardware_error") is not None:
            raise RuntimeError(f"fresh process did not remain in healthy hardware standby: {system}")
        if system.get("c4_drain") is not None or system.get("occupied_recovery") is not None:
            raise RuntimeError(f"unexpected C4 recovery operation is active: {system}")
        save_json(BACKUP / "result.json", {
            "success": True,
            "at_epoch_s": time.time(),
            "old_identity": old_identity,
            "new_identity": new_identity,
            "supervisor_start_response": start_response,
            "system_after_start": system,
            "runtime_after_start": {
                "lifecycle_state": runtime.get("lifecycle_state"),
                "is_running": runtime.get("is_running"),
                "pieces_cached": runtime.get("pieces_cached"),
                "active_incident": runtime.get("active_incident"),
            },
            "deployed_files": final_files,
            "prior_matching_c4_hold": incident,
            "matching_hold_was_not_manually_cleared": True,
        })
        print(json.dumps({
            "success": True,
            "old_pid": old_identity["pid"],
            "new_pid": new_identity["pid"],
            "files": final_files,
            "inert": runtime.get("is_running") is False,
            "backup": str(BACKUP),
        }, sort_keys=True))
    except Exception as exc:
        save_json(BACKUP / "continuation-failure.json", {
            "at_epoch_s": time.time(),
            "error": repr(exc),
            "applied": applied,
            "candidate_start_requested": candidate_start_requested,
        })
        if not candidate_start_requested:
            current = request("/api/supervisor/status", port=8001)
            if current.get("supervisor_state") == "stopped" and current.get("backend_pid") is None:
                restore_originals(originals)
                original_start = request("/api/supervisor/start", port=8001, post=True, timeout=15)
                original_identity = wait_started()
                save_json(BACKUP / "rollback.json", {
                    "at_epoch_s": time.time(),
                    "reason": repr(exc),
                    "restored": {relative: file_state(relative) for relative in FILES},
                    "supervisor_start_response": original_start,
                    "restored_backend_identity": original_identity,
                })
        raise
    finally:
        for temp in temps.values():
            if temp.exists():
                temp.unlink()
                sync_dir(temp.parent)


if __name__ == "__main__":
    main()
