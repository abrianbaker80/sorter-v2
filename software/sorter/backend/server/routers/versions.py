"""Software version listing and one-click git-based updates."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

# Release channels are tag namespaces. A machine "on" a channel is sitting on
# one of the channel's tags; updating moves it to that channel's newest tag.
STABLE_TAG_PREFIX = "sorter/stable/v"
CANARY_TAG_PREFIX = "sorter/canary/v"
RELEASE_CHANNELS = (("stable", STABLE_TAG_PREFIX), ("canary", CANARY_TAG_PREFIX))
MAX_TAGS_LISTED = 20
GIT_TIMEOUT_S = 30.0
GIT_FETCH_TIMEOUT_S = 90.0
REF_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
DEPENDENCY_FILES = (
    "software/sorter/backend/pyproject.toml",
    "software/sorter/backend/requirements.txt",
    "software/sorter/frontend/package.json",
)

# The C4 camera is a network MJPEG source on the Raspberry Pi.  It lives in
# the ignored machine-specific TOML, so a release checkout must never replace
# it.  The release guard below also verifies that the target source still
# understands the persisted split-feeder URL before allowing a switch.
CAMERA_CONFIG_RELATIVE_PATH = "software/machine.toml"
CAMERA_PARSER_RELATIVE_PATH = "software/sorter/backend/irl/config.py"
CAMERA_SERVICE_RELATIVE_PATH = "software/sorter/backend/vision/camera_service.py"

_repo_root_cache: Optional[Path] = None
_update_lock = threading.Lock()
_update_target: Optional[str] = None


class UpdateRequest(BaseModel):
    kind: str
    name: str
    restart: bool = True


def _machine_params_path() -> Path:
    configured = os.getenv("MACHINE_SPECIFIC_PARAMS_PATH")
    if configured:
        return Path(configured).expanduser()
    return _repoRoot() / CAMERA_CONFIG_RELATIVE_PATH


def _camera_contract_from_config(raw: object) -> Dict[str, Any]:
    """Return the persisted C4 source contract without opening the camera."""
    if not isinstance(raw, dict):
        return {
            "configured": False,
            "layout": None,
            "source_kind": "missing",
            "source": None,
        }

    cameras = raw.get("cameras")
    if not isinstance(cameras, dict):
        return {
            "configured": False,
            "layout": None,
            "source_kind": "missing",
            "source": None,
        }

    layout = cameras.get("layout", "default")
    source = cameras.get("classification_channel")
    # Older configs called the same physical C4 camera ``carousel``.  Keep
    # that compatibility alias when classification_channel is absent.
    source_key = "classification_channel"
    if source is None:
        source = cameras.get("carousel")
        source_key = "carousel"

    if isinstance(source, str) and source.strip():
        source_kind = "url"
    elif isinstance(source, int) and not isinstance(source, bool) and source >= 0:
        source_kind = "device_index"
    else:
        source_kind = "missing"

    return {
        "configured": source_kind != "missing",
        "layout": layout if isinstance(layout, str) else None,
        "source_key": source_key,
        "source_kind": source_kind,
        # Do not expose the private Pi address in the UI payload.
        "source": None,
    }


def _load_camera_contract() -> Dict[str, Any]:
    path = _machine_params_path()
    try:
        with path.open("rb") as handle:
            return _camera_contract_from_config(tomllib.load(handle))
    except FileNotFoundError:
        return _camera_contract_from_config(None)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {
            "configured": False,
            "layout": None,
            "source_kind": "invalid",
            "source": None,
            "error": str(exc),
        }


def _camera_source_is_supported_by_target(
    target_ref: str,
    contract: Dict[str, Any],
) -> tuple[bool | None, str]:
    """Check the target release's persisted-source compatibility contract.

    This is intentionally a source-level check performed before checkout.  It
    prevents the update endpoint from restarting into a release that silently
    ignores the Raspberry Pi C4 URL, while keeping the machine TOML untouched.
    """
    source_kind = contract.get("source_kind")
    if source_kind == "missing":
        return None, "No persisted C4 camera source is configured."
    if source_kind == "invalid":
        return False, "The persisted machine camera configuration is invalid."

    parser = _gitText("show", f"{target_ref}:{CAMERA_PARSER_RELATIVE_PATH}")
    service = _gitText("show", f"{target_ref}:{CAMERA_SERVICE_RELATIVE_PATH}")
    if parser is None or service is None:
        return False, "The release does not contain the camera compatibility modules."

    if source_kind == "url":
        parser_support = bool(
            re.search(
                r"cameras_section\.get\(\s*['\"]classification_channel['\"]",
                parser,
            )
            and re.search(r"url\s*=\s*carousel_source", parser)
        )
        service_support = bool(
            re.search(r"config\.url\s+is\s+not\s+None", service)
            and "_config_source_key" in service
        )
        if parser_support and service_support:
            return True, "Release preserves the configured network C4 camera URL."
        return False, "Release cannot prove support for the configured network C4 camera URL."

    parser_support = bool(re.search(r"device_index\s*=\s*carousel_source", parser))
    service_support = "config.device_index" in service
    if parser_support and service_support:
        return True, "Release preserves the configured C4 device index."
    return False, "Release cannot prove support for the configured C4 device index."


def _live_c4_camera_status() -> str | None:
    """Read the current process-local C4 health without probing hardware."""
    try:
        from server import shared_state

        service = getattr(shared_state, "camera_service", None)
        if service is None:
            return None
        health = service.get_health_map()
        return health.get("classification_channel") or health.get("carousel")
    except Exception:
        return None


def _repoRoot() -> Path:
    global _repo_root_cache
    if _repo_root_cache is None:
        backend_dir = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            ["git", "-C", str(backend_dir), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Could not locate git repo root: {result.stderr.strip()}")
        _repo_root_cache = Path(result.stdout.strip())
    return _repo_root_cache


def _git(*args: str, timeout: float = GIT_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(_repoRoot()), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _gitLine(*args: str) -> Optional[str]:
    result = _git(*args)
    if result.returncode != 0:
        return None
    line = result.stdout.strip()
    return line if line else None


def _gitText(*args: str) -> Optional[str]:
    result = _git(*args)
    if result.returncode != 0:
        return None
    return result.stdout


def _commitInfo(ref: str) -> Optional[Dict[str, Any]]:
    line = _gitLine("log", "-1", "--format=%h%x09%H%x09%ct%x09%s", ref)
    if line is None:
        return None
    parts = line.split("\t", 3)
    if len(parts) != 4:
        return None
    return {
        "sha": parts[0],
        "full_sha": parts[1],
        "commit_unix": int(parts[2]),
        "subject": parts[3],
    }


def _currentInfo() -> Dict[str, Any]:
    branch = _gitLine("rev-parse", "--abbrev-ref", "HEAD") or "HEAD"
    detached = branch == "HEAD"
    head = _commitInfo("HEAD") or {}
    describe = _gitLine("describe", "--tags", "--always") or head.get("sha", "unknown")
    dirty_result = _git("status", "--porcelain", "--untracked-files=no")
    dirty = bool(dirty_result.stdout.strip())
    return {
        "ref": describe if detached else branch,
        "branch": None if detached else branch,
        "detached": detached,
        "describe": describe,
        "dirty": dirty,
        **head,
    }


def _branchEntries(current: Dict[str, Any]) -> List[Dict[str, Any]]:
    # Only the branch the machine is actually on — no `main` unless that's it.
    current_branch = current.get("branch")
    if not isinstance(current_branch, str) or not current_branch:
        return []
    info = _commitInfo(f"origin/{current_branch}")
    if info is None:
        return []
    return [
        {
            "kind": "branch",
            "name": current_branch,
            "sha": info["sha"],
            "commit_unix": info["commit_unix"],
            "subject": info["subject"],
            "is_current": True,
            "up_to_date": info["full_sha"] == current.get("full_sha"),
        }
    ]


def _tagsForPrefix(prefix: str) -> List[Dict[str, Any]]:
    # Version-descending so the channel's newest *release number* is first,
    # independent of commit/tag dates (v1.10.0 > v1.9.0, not lexical).
    result = _git(
        "tag",
        "-l",
        f"{prefix}*",
        "--sort=-v:refname",
        "--format=%(refname:strip=2)",
    )
    if result.returncode != 0:
        return []
    tags: List[Dict[str, Any]] = []
    for name in result.stdout.strip().splitlines()[:MAX_TAGS_LISTED]:
        info = _commitInfo(f"refs/tags/{name}")
        if info is None:
            continue
        tags.append({"name": name, **info})
    return tags


def _channelEntries(current: Dict[str, Any]) -> List[Dict[str, Any]]:
    head_full = current.get("full_sha")
    entries: List[Dict[str, Any]] = []
    for channel, prefix in RELEASE_CHANNELS:
        tags = _tagsForPrefix(prefix)
        if not tags:
            continue
        latest = tags[0]
        on_channel = any(tag["full_sha"] == head_full for tag in tags)
        entries.append(
            {
                "kind": "tag",
                "channel": channel,
                "name": latest["name"],
                "sha": latest["sha"],
                "commit_unix": latest["commit_unix"],
                "subject": latest["subject"],
                "is_current": on_channel,
                "up_to_date": on_channel and latest["full_sha"] == head_full,
            }
        )
    return entries


def _changedDependencyFiles(old_sha: str, new_sha: str) -> List[str]:
    if old_sha == new_sha:
        return []
    result = _git("diff", "--name-only", old_sha, new_sha, "--", *DEPENDENCY_FILES)
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.strip().splitlines() if line]


def _deferredRestart() -> None:
    def _exit() -> None:
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_exit, daemon=True).start()


@router.get("/api/system/versions")
def get_versions(refresh: bool = False) -> Dict[str, Any]:
    fetch_error: Optional[str] = None
    if refresh:
        result = _git(
            "fetch", "--tags", "--prune", "--prune-tags", "--force", "origin",
            timeout=GIT_FETCH_TIMEOUT_S,
        )
        if result.returncode != 0:
            fetch_error = result.stderr.strip() or "git fetch failed"

    current = _currentInfo()
    available = _branchEntries(current) + _channelEntries(current)
    camera_contract = _load_camera_contract()
    for entry in available:
        target_ref = (
            f"origin/{entry['name']}"
            if entry["kind"] == "branch"
            else f"refs/tags/{entry['name']}"
        )
        supported, reason = _camera_source_is_supported_by_target(
            target_ref, camera_contract
        )
        entry["c4_camera_compatible"] = supported
        entry["c4_camera_reason"] = reason

    return {
        "ok": True,
        "current": current,
        "available": available,
        "fetch_error": fetch_error,
        "update_in_progress": _update_target,
        "c4_camera": {
            "configured": bool(camera_contract.get("configured")),
            "layout": camera_contract.get("layout"),
            "source_kind": camera_contract.get("source_kind"),
            "live_status": _live_c4_camera_status(),
            "config_preserved_by_updates": True,
        },
    }


@router.post("/api/system/update")
def update_version(req: UpdateRequest) -> Dict[str, Any]:
    global _update_target

    if req.kind not in ("branch", "tag"):
        return {"ok": False, "message": f"Unknown ref kind: {req.kind}"}
    if not REF_NAME_PATTERN.match(req.name) or ".." in req.name:
        return {"ok": False, "message": f"Invalid ref name: {req.name}"}
    if req.kind == "tag" and not any(
        req.name.startswith(prefix) for _, prefix in RELEASE_CHANNELS
    ):
        allowed = " or ".join(prefix for _, prefix in RELEASE_CHANNELS)
        return {"ok": False, "message": f"Release tags must start with {allowed}"}

    if not _update_lock.acquire(blocking=False):
        return {"ok": False, "message": f"Update already in progress: {_update_target}"}
    try:
        _update_target = f"{req.kind}:{req.name}"

        fetch = _git(
            "fetch", "--tags", "--prune", "--prune-tags", "--force", "origin",
            timeout=GIT_FETCH_TIMEOUT_S,
        )
        if fetch.returncode != 0:
            return {"ok": False, "message": f"git fetch failed: {fetch.stderr.strip()}"}

        target_ref = f"origin/{req.name}" if req.kind == "branch" else f"refs/tags/{req.name}"
        target = _commitInfo(target_ref)
        if target is None:
            return {"ok": False, "message": f"Ref not found on origin: {target_ref}"}

        camera_contract = _load_camera_contract()
        camera_supported, camera_reason = _camera_source_is_supported_by_target(
            target_ref, camera_contract
        )
        if camera_contract.get("configured") and camera_supported is not True:
            return {
                "ok": False,
                "message": f"Cannot switch to {req.name}: {camera_reason}",
                "c4_camera_compatible": camera_supported,
            }

        old = _commitInfo("HEAD") or {}
        old_sha = old.get("full_sha", "")

        # Never `git clean` here — gitignored machine config (machine.toml,
        # .env, mine/, sqlite) must survive every update. Local tracked edits
        # are stashed, not discarded, so nothing is ever silently lost.
        dirty = bool(_git("status", "--porcelain", "--untracked-files=no").stdout.strip())
        stashed = False
        if dirty:
            stash = _git("stash", "push", "-m", f"pre-update {time.strftime('%Y-%m-%d %H:%M:%S')}")
            if stash.returncode != 0:
                return {"ok": False, "message": f"git stash failed: {stash.stderr.strip()}"}
            stashed = True

        if req.kind == "branch":
            checkout = _git("checkout", "-f", "-B", req.name, f"origin/{req.name}")
        else:
            checkout = _git("checkout", "-f", "--detach", f"refs/tags/{req.name}")
        if checkout.returncode != 0:
            return {"ok": False, "message": f"git checkout failed: {checkout.stderr.strip()}"}

        deps_changed = _changedDependencyFiles(old_sha, target["full_sha"])

        if req.restart:
            _deferredRestart()

        return {
            "ok": True,
            "old_sha": old.get("sha"),
            "new_sha": target["sha"],
            "changed": old_sha != target["full_sha"],
            "stashed_local_changes": stashed,
            "deps_changed": deps_changed,
            "restarting": req.restart,
        }
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "message": f"git command timed out: {exc}"}
    finally:
        _update_target = None
        _update_lock.release()
