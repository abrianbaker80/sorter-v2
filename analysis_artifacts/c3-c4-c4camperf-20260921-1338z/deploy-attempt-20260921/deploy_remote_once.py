#!/usr/bin/env python3
"""One bounded, hash-guarded C4 camera optimization deployment."""

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


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def request(path, port=8000, post=False, timeout=5):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=b"{}" if post else None,
        headers={"Origin": "http://127.0.0.1", "Content-Type": "application/json"},
        method="POST" if post else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        if response.status == 204 or not response.readable():
            return {"http_status": response.status}
        body = response.read()
        return json.loads(body) if body else {"http_status": response.status}


def process_identity(pid):
    proc = pathlib.Path("/proc") / str(pid)
    raw = (proc / "stat").read_text()
    fields = raw.split(") ", 1)[1].split()
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
        raise RuntimeError(f"not a regular file: {path}")
    if path.parent.resolve() != path.parent or info.st_dev != os.stat(path.parent).st_dev:
        raise RuntimeError(f"unexpected target parent/device: {path}")
    flag_fd = os.open(path, os.O_RDONLY)
    try:
        flag_buf = bytearray(4)
        fcntl.ioctl(flag_fd, 0x80086601, flag_buf, True)  # FS_IOC_GETFLAGS
        flags = struct.unpack("I", flag_buf)[0]
    finally:
        os.close(flag_fd)
    if flags & (0x10 | 0x20):  # immutable or append-only
        raise RuntimeError(f"immutable/append-only source file: {path} flags={flags:#x}")
    return {
        "sha256": digest(path.read_bytes()),
        "uid": info.st_uid,
        "gid": info.st_gid,
        "mode": stat.S_IMODE(info.st_mode),
        "device": info.st_dev,
        "filesystem_flags": flags,
    }


def assert_baseline():
    observed = {}
    for relative, expected in FILES.items():
        state = file_state(relative)
        if (
            state["sha256"] != expected["old"]
            or (state["uid"], state["gid"], state["mode"]) != (1000, 1000, 0o664)
        ):
            raise RuntimeError(f"installed baseline/metadata mismatch: {relative}: {state}")
        observed[relative] = state
    return observed


def assert_payloads():
    payloads = {}
    for relative, expected in FILES.items():
        name = pathlib.Path(relative).name
        data = (STAGE / name).read_bytes()
        if len(data) != expected["bytes"] or digest(data) != expected["new"]:
            raise RuntimeError(f"staged payload hash/size mismatch: {relative}")
        compile(data, relative, "exec")
        payloads[relative] = data
    return payloads


def probe_parent(parent):
    fd, first = tempfile.mkstemp(prefix=".c4-camera-atomic-probe.", dir=parent)
    moved = first + ".renamed"
    try:
        os.fchown(fd, 1000, 1000)
        os.fchmod(fd, 0o664)
        os.write(fd, b"atomic-probe")
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(first, moved)
        sync_dir(parent)
        info = os.stat(moved)
        if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (1000, 1000, 0o664):
            raise RuntimeError(f"atomic probe metadata mismatch in {parent}")
    finally:
        if fd >= 0:
            os.close(fd)
        for name in (first, moved):
            if os.path.exists(name):
                os.unlink(name)
        sync_dir(parent)


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
            raise RuntimeError(f"prepared bytes mismatch: {path}")
        if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (1000, 1000, 0o664):
            raise RuntimeError(f"prepared metadata mismatch: {path}")
        return pathlib.Path(name)
    except Exception:
        if fd >= 0:
            os.close(fd)
        if os.path.exists(name):
            os.unlink(name)
        raise


def live_snapshot():
    supervisor = request("/api/supervisor/status", port=8001)
    runtime = request("/runtime-stats")["payload"]
    owners = request("/api/classification-channel/debug")
    system = request("/api/system/status")
    tmc = request("/api/stepper/carousel/tmc")
    position = request("/api/hardware-config/carousel/live")
    incident = runtime.get("active_incident")
    if not supervisor.get("backend_running") or not supervisor.get("backend_healthy"):
        raise RuntimeError(f"backend/supervisor not healthy: {supervisor}")
    if runtime.get("is_running") is not False or runtime.get("lifecycle_state") != "paused":
        raise RuntimeError("sorter is not currently paused/inert")
    if runtime.get("pieces_cached") != 0:
        raise RuntimeError("runtime still owns cached pieces")
    counts = owners.get("counts", {})
    if owners.get("active_pieces") or counts.get("active_pieces") != 0:
        raise RuntimeError("C4 owner ledger is not empty")
    if counts.get("pending_classifications") != 0 or owners.get("zones"):
        raise RuntimeError("C4 classification work/zones remain active")
    if any(owners.get("positions", {}).values()):
        raise RuntimeError("C4 position ledger is occupied")
    if not isinstance(incident, dict) or not (
        incident.get("kind") == "classification_track_lost"
        and incident.get("channel") == "c4"
        and incident.get("status") == "waiting_for_operator"
        and incident.get("reason") == "C4 perception is stale or has an invalid timestamp"
    ):
        raise RuntimeError(f"matching C4 stale-perception hold changed: {incident}")
    if system.get("hardware_error") is not None:
        raise RuntimeError(f"hardware has an error: {system}")
    if not tmc.get("hardware_ready") or tmc.get("drv_status", {}).get("stst") is not True:
        raise RuntimeError("fresh TMC feedback does not confirm motor standstill")
    if tmc.get("stalled"):
        raise RuntimeError("carousel driver reports a stall")
    return {
        "at_epoch_s": time.time(),
        "supervisor": supervisor,
        "backend_identity": process_identity(supervisor["backend_pid"]),
        "runtime": {
            "updated_at": runtime.get("updated_at"),
            "lifecycle_state": runtime.get("lifecycle_state"),
            "is_running": runtime.get("is_running"),
            "pieces_cached": runtime.get("pieces_cached"),
            "counts": runtime.get("counts"),
            "active_incident": incident,
        },
        "c4_owners": {
            "counts": counts,
            "positions": owners.get("positions"),
            "active_piece_uuids": [piece.get("uuid") for piece in owners.get("active_pieces", [])],
        },
        "hardware": {
            "system": system,
            "tmc_standstill": tmc.get("drv_status", {}).get("stst"),
            "tmc_stalled": tmc.get("stalled"),
            "carousel_position_feedback": position,
            "operator_physically_confirmed_stopped": True,
            "operator_confirmed_c4_empty": True,
        },
    }


def wait_stopped(old_pid, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = request("/api/supervisor/status", port=8001)
        if state.get("supervisor_state") == "stopped" and not state.get("backend_running") and state.get("backend_pid") is None and not state.get("restart_requested"):
            if pathlib.Path("/proc", str(old_pid)).exists():
                time.sleep(0.5)
                continue
            return state
        time.sleep(0.5)
    raise RuntimeError("supported supervisor stop did not establish a stopped child")


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
            if not pid:
                stable = 0
            else:
                current = process_identity(pid)
                runtime = request("/runtime-stats").get("payload", {})
                health = request("/health")
                healthy = (
                    supervisor.get("backend_running") is True
                    and supervisor.get("backend_healthy") is True
                    and not supervisor.get("restart_requested")
                    and runtime.get("is_running") is False
                    and "transfer_throughput" in runtime
                    and health == {"status": "ok"}
                )
                if current != identity:
                    identity, stable = current, 0
                stable = stable + 1 if healthy else 0
                if stable >= 3:
                    return identity
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError, FileNotFoundError, ProcessLookupError):
            stable = 0
        time.sleep(2)
    raise RuntimeError("startup not established within the 120-second readiness window")


def install(payloads, temps):
    applied = []
    for relative, temp in temps.items():
        target = ROOT / relative
        os.replace(temp, target)
        sync_dir(target.parent)
        if digest(target.read_bytes()) != FILES[relative]["new"]:
            raise RuntimeError(f"post-rename hash mismatch: {relative}")
        applied.append(relative)
    return applied


def restore_originals(originals):
    for relative, data in originals.items():
        target = ROOT / relative
        temp = make_temp(target, data)
        os.replace(temp, target)
        sync_dir(target.parent)
        if digest(target.read_bytes()) != FILES[relative]["old"]:
            raise RuntimeError(f"rollback hash mismatch: {relative}")


def main():
    if BACKUP.exists():
        raise RuntimeError(f"deployment evidence path already exists: {BACKUP}")
    payloads = assert_payloads()
    original_state = assert_baseline()
    BACKUP.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(BACKUP, 0o700)
    originals = {}
    for relative in FILES:
        path = ROOT / relative
        data = path.read_bytes()
        originals[relative] = data
        backup_file = BACKUP / (relative.replace("/", "__"))
        fd = os.open(backup_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if digest(backup_file.read_bytes()) != FILES[relative]["old"]:
            raise RuntimeError(f"rollback bytes failed hash verification: {relative}")
    sync_dir(BACKUP)

    probes = sorted({(ROOT / name).parent for name in FILES})
    for parent in probes:
        probe_parent(parent)
    temps = {relative: make_temp(ROOT / relative, payloads[relative]) for relative in FILES}
    before = None
    stopped = None
    stop_response = None
    applied = []
    candidate_start_requested = False
    try:
        before = live_snapshot()
        if assert_baseline() != original_state:
            raise RuntimeError("target baseline changed after the first preflight")
        save_json(BACKUP / "preflight.json", {
            "at_epoch_s": time.time(),
            "target_root": str(ROOT),
            "files": FILES,
            "original_metadata_and_hashes": original_state,
            "payload_hashes": {key: digest(value) for key, value in payloads.items()},
            "live_before": before,
            "operator_clearance": "C4 stopped and empty, confirmed in conversation; no drain required",
            "atomic_same_directory_probe": "passed",
        })

        # Refresh all supported stop/ownership signals immediately before stopping.
        immediately_before = live_snapshot()
        if immediately_before["backend_identity"] != before["backend_identity"]:
            raise RuntimeError("backend identity changed before the deployment stop")
        if assert_baseline() != original_state:
            raise RuntimeError("target bytes or metadata changed before deployment stop")

        stop_response = request("/api/supervisor/stop", port=8001, post=True)
        stopped = wait_stopped(before["backend_identity"]["pid"])
        if assert_baseline() != original_state:
            raise RuntimeError("source changed while backend was stopping")

        applied = install(payloads, temps)
        installed = {relative: file_state(relative) for relative in FILES}
        if any(installed[key]["sha256"] != FILES[key]["new"] for key in FILES):
            raise RuntimeError("installed file set failed exact hash verification")
        save_json(BACKUP / "installed.json", {
            "at_epoch_s": time.time(),
            "supervisor_stop_response": stop_response,
            "supervisor_stopped": stopped,
            "installed": installed,
        })
        candidate_start_requested = True
        start_response = request("/api/supervisor/start", port=8001, post=True)
        new_identity = wait_started()
        if new_identity == before["backend_identity"]:
            raise RuntimeError("backend PID did not change across the restart boundary")
        final_files = {relative: file_state(relative) for relative in FILES}
        if any(final_files[key]["sha256"] != FILES[key]["new"] for key in FILES):
            raise RuntimeError("deployed hashes changed after startup")
        system = request("/api/system/status")
        runtime = request("/runtime-stats").get("payload", {})
        if runtime.get("is_running") is not False:
            raise RuntimeError("sorter unexpectedly entered RUNNING during inert startup")
        save_json(BACKUP / "result.json", {
            "success": True,
            "at_epoch_s": time.time(),
            "old_identity": before["backend_identity"],
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
            "prior_matching_c4_hold": before["runtime"]["active_incident"],
            "matching_hold_was_not_manually_cleared": True,
        })
        print(json.dumps({
            "success": True,
            "old_pid": before["backend_identity"]["pid"],
            "new_pid": new_identity["pid"],
            "files": final_files,
            "inert": runtime.get("is_running") is False,
            "backup": str(BACKUP),
        }, sort_keys=True))
    except Exception as exc:
        save_json(BACKUP / "failure.json", {
            "at_epoch_s": time.time(),
            "error": repr(exc),
            "applied": applied,
            "candidate_start_requested": candidate_start_requested,
        })
        # Never restore code after an uncertain candidate start. Before that
        # point, reconcile the supported supervisor state; if stopped, restore
        # the exact originals and bring the inert backend back once.
        if not candidate_start_requested:
            try:
                supervisor = request("/api/supervisor/status", port=8001)
            except Exception:
                supervisor = None
            child_stopped = bool(
                supervisor
                and supervisor.get("supervisor_state") == "stopped"
                and not supervisor.get("backend_running")
                and supervisor.get("backend_pid") is None
                and not supervisor.get("restart_requested")
            )
            if child_stopped:
                restore_originals(originals)
                original_start_response = request("/api/supervisor/start", port=8001, post=True)
                original_identity = wait_started()
                save_json(BACKUP / "rollback.json", {
                    "at_epoch_s": time.time(),
                    "reason": repr(exc),
                    "restored": {relative: file_state(relative) for relative in FILES},
                    "supervisor_start_response": original_start_response,
                    "restored_backend_identity": original_identity,
                })
            raise
        raise
    finally:
        for temp in temps.values():
            if temp.exists():
                temp.unlink()
                sync_dir(temp.parent)


if __name__ == "__main__":
    main()
