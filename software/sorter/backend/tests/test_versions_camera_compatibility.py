from __future__ import annotations

import subprocess
from pathlib import Path

from server.routers import versions


def test_camera_contract_prefers_classification_channel_url() -> None:
    contract = versions._camera_contract_from_config(
        {
            "cameras": {
                "layout": "split_feeder",
                "classification_channel": "http://10.55.0.2:18082/video",
                "carousel": 1,
            }
        }
    )

    assert contract == {
        "configured": True,
        "layout": "split_feeder",
        "source_key": "classification_channel",
        "source_kind": "url",
        "source": None,
    }


def test_camera_contract_supports_legacy_carousel_alias() -> None:
    contract = versions._camera_contract_from_config(
        {"cameras": {"layout": "split_feeder", "carousel": "http://pi/video"}}
    )

    assert contract["configured"] is True
    assert contract["source_key"] == "carousel"
    assert contract["source_kind"] == "url"


def test_url_c4_source_requires_parser_and_service_support(monkeypatch) -> None:
    contract = versions._camera_contract_from_config(
        {
            "cameras": {
                "layout": "split_feeder",
                "classification_channel": "http://pi/video",
            }
        }
    )
    parser = (
        'carousel_source = cameras_section.get("classification_channel")\n'
        "config = _mkCameraConfigForRole(url=carousel_source)\n"
    )
    service = (
        'if config.url is not None:\n'
        '    return ("url", config.url)\n'
        '_config_source_key = config.url\n'
    )

    def fake_git_line(*args: str) -> str | None:
        path = args[-1]
        if path.endswith(versions.CAMERA_PARSER_RELATIVE_PATH):
            return parser
        if path.endswith(versions.CAMERA_SERVICE_RELATIVE_PATH):
            return service
        return None

    monkeypatch.setattr(versions, "_gitText", fake_git_line)

    assert versions._camera_source_is_supported_by_target("refs/tags/test", contract) == (
        True,
        "Release preserves the configured network C4 camera URL.",
    )

    monkeypatch.setattr(
        versions,
        "_gitText",
        lambda *args: parser if args[-1].endswith(versions.CAMERA_PARSER_RELATIVE_PATH) else "",
    )
    supported, reason = versions._camera_source_is_supported_by_target("refs/tags/test", contract)
    assert supported is False
    assert "cannot prove support" in reason


def test_missing_c4_source_is_unknown_and_does_not_claim_support() -> None:
    supported, reason = versions._camera_source_is_supported_by_target(
        "refs/tags/test",
        versions._camera_contract_from_config({"cameras": {"layout": "default"}}),
    )

    assert supported is None
    assert "No persisted C4 camera source" in reason


def test_load_camera_contract_reads_machine_specific_path(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "machine.toml"
    config.write_text(
        '[cameras]\nlayout = "split_feeder"\nclassification_channel = "http://pi/video"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MACHINE_SPECIFIC_PARAMS_PATH", str(config))

    contract = versions._load_camera_contract()

    assert contract["configured"] is True
    assert contract["source_kind"] == "url"


def test_update_rejects_incompatible_release_before_stashing(monkeypatch) -> None:
    git_calls: list[tuple[str, ...]] = []

    def fake_git(*args: str, **kwargs) -> subprocess.CompletedProcess[str]:
        git_calls.append(args)
        return subprocess.CompletedProcess(["git"], 0, "", "")

    monkeypatch.setattr(versions, "_git", fake_git)
    monkeypatch.setattr(
        versions,
        "_commitInfo",
        lambda ref: {
            "sha": "target",
            "full_sha": "target-full",
            "commit_unix": 1,
            "subject": "target",
        },
    )
    monkeypatch.setattr(
        versions,
        "_load_camera_contract",
        lambda: {"configured": True, "source_kind": "url"},
    )
    monkeypatch.setattr(
        versions,
        "_camera_source_is_supported_by_target",
        lambda target_ref, contract: (False, "target has no C4 URL support"),
    )

    result = versions.update_version(
        versions.UpdateRequest(kind="tag", name="sorter/stable/v0.0.0", restart=False)
    )

    assert result == {
        "ok": False,
        "message": "Cannot switch to sorter/stable/v0.0.0: target has no C4 URL support",
        "c4_camera_compatible": False,
    }
    assert not any(call[:2] == ("stash", "push") for call in git_calls)
