from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import HTTPException

from irl.config import cameraColorProfileToDict, parseCameraColorProfile
from server.camera_calibration import analyze_color_plate_target
from server import shared_state
from server.routers import cameras


def test_existing_camera_calibration_routes_keep_the_legacy_color_plate_analyzer() -> None:
    assert cameras.analyze_color_plate_target is analyze_color_plate_target


def test_profile_calibration_saves_color_profile_without_changing_camera_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = np.full((24, 24, 3), 100, dtype=np.uint8)
    capture_calls: list[tuple[str, object, dict[str, object]]] = []
    saved_profiles: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(
        cameras,
        "get_camera_device_settings",
        lambda role: {"source": "http://camera/video", "supported": True},
    )
    monkeypatch.setattr(cameras, "_read_machine_params_config", lambda: (None, {}))
    monkeypatch.setattr(cameras, "_picture_settings_for_role", lambda config, role: {"rotation": 0})
    analysis_payload = {
        "target_type": "colorchecker24_after_2014",
        "target_reference": "ColorChecker Classic 24 - After November 2014",
        "tile_samples": {},
    }
    monkeypatch.setattr(
        cameras,
        "analyze_camera_color_target",
        lambda current_frame: SimpleNamespace(to_dict=lambda: analysis_payload),
    )
    monkeypatch.setattr(
        cameras,
        "generate_color_profile_from_analysis",
        lambda analysis: {
            "enabled": True,
            "matrix": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            "bias": [0.0, 0.0, 0.0],
            "calibration_target_type": "colorchecker24_after_2014",
            "calibration_reference": "ColorChecker Classic 24 - After November 2014",
        },
    )

    def capture(role: str, source: object, **kwargs: object) -> np.ndarray:
        capture_calls.append((role, source, kwargs))
        return frame.copy()

    def save_profile(role: str, payload: dict[str, object]) -> dict[str, object]:
        saved_profiles.append((role, payload))
        profile = cameraColorProfileToDict(parseCameraColorProfile(payload))
        return {"ok": True, "role": role, "profile": profile, "applied_live": True}

    def unexpected_device_setting_write(*args: object, **kwargs: object) -> None:
        raise AssertionError("profile-only calibration must not change camera controls")

    monkeypatch.setattr(cameras, "_capture_frame_for_calibration", capture)
    monkeypatch.setattr(cameras, "_save_camera_color_profile", save_profile)
    monkeypatch.setattr(cameras, "preview_camera_device_settings", unexpected_device_setting_write)
    monkeypatch.setattr(cameras, "save_camera_device_settings", unexpected_device_setting_write)
    monkeypatch.setattr(cameras, "COLOR_CORRECTION_ENABLED", False)

    result = cameras.calibrate_camera_color_profile_from_current_frame("classification_channel")

    assert len(capture_calls) == 1
    assert capture_calls[0][0:2] == ("classification_channel", "http://camera/video")
    assert capture_calls[0][2]["color_profile"] == {"enabled": False}
    assert result["camera_settings_changed"] is False
    assert result["active"] is False
    assert result["globally_enabled"] is False
    assert saved_profiles[0][0] == "classification_channel"
    assert result["profile"]["calibration_reference"] == "ColorChecker Classic 24 - After November 2014"


def test_profile_calibration_refuses_non_c4_roles_before_camera_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cameras,
        "get_camera_device_settings",
        lambda role: (_ for _ in ()).throw(AssertionError("camera should not be read")),
    )

    with pytest.raises(HTTPException) as exc_info:
        cameras.calibrate_camera_color_profile_from_current_frame("c_channel_3")

    assert exc_info.value.status_code == 400


def test_carousel_alias_uses_canonical_c4_source_and_picture_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = np.full((24, 24, 3), 100, dtype=np.uint8)
    camera_roles: list[str] = []
    capture_calls: list[tuple[str, dict[str, object]]] = []
    saved_roles: list[str] = []
    manager = SimpleNamespace(
        getCaptureThreadForRole=lambda role: SimpleNamespace(name="classification_channel")
    )
    monkeypatch.setattr(shared_state, "vision_manager", manager)
    monkeypatch.setattr(
        cameras,
        "get_camera_device_settings",
        lambda role: (
            camera_roles.append(role)
            or {"source": "http://camera/video", "supported": True}
        ),
    )
    monkeypatch.setattr(
        cameras,
        "_read_machine_params_config",
        lambda: (
            None,
            {
                "camera_picture_settings": {
                    "classification_channel": {"rotation": 90},
                    "carousel": {"rotation": 0},
                }
            },
        ),
    )
    analysis_payload = {
        "target_type": "colorchecker24_after_2014",
        "target_reference": "ColorChecker Classic 24 - After November 2014",
        "tile_samples": {},
    }
    monkeypatch.setattr(
        cameras,
        "analyze_camera_color_target",
        lambda current_frame: SimpleNamespace(to_dict=lambda: analysis_payload),
    )
    monkeypatch.setattr(
        cameras,
        "generate_color_profile_from_analysis",
        lambda analysis: {"enabled": True, "matrix": np.eye(3).tolist(), "bias": [0.0] * 3},
    )

    def capture(role: str, source: object, **kwargs: object) -> np.ndarray:
        capture_calls.append((role, kwargs))
        return frame.copy()

    monkeypatch.setattr(cameras, "_capture_frame_for_calibration", capture)
    monkeypatch.setattr(
        cameras,
        "_save_camera_color_profile",
        lambda role, payload: (
            saved_roles.append(role)
            or {"ok": True, "role": role, "profile": payload, "applied_live": True}
        ),
    )

    result = cameras.calibrate_camera_color_profile_from_current_frame("carousel")

    assert camera_roles == ["classification_channel"]
    assert capture_calls[0][0] == "classification_channel"
    assert capture_calls[0][1]["picture_settings"]["rotation"] == 90
    assert saved_roles == ["classification_channel"]
    assert result["role"] == "classification_channel"


def test_live_calibration_frame_uses_uncorrected_capture_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corrected = np.full((4, 4, 3), 200, dtype=np.uint8)
    uncorrected = np.full((4, 4, 3), 80, dtype=np.uint8)
    capture = SimpleNamespace(
        latest_frame=SimpleNamespace(raw=corrected, uncorrected_raw=uncorrected, timestamp=1.0)
    )
    manager = SimpleNamespace(getCaptureThreadForRole=lambda role: capture)
    monkeypatch.setattr(shared_state, "vision_manager", manager)

    result = cameras._grab_live_frame(
        "classification_channel",
        after_timestamp=0.0,
        timeout=0.0,
        prefer_uncorrected=True,
    )

    assert result is not None
    assert np.array_equal(result, uncorrected)
    assert result is not uncorrected


def test_live_calibration_frame_never_falls_back_to_corrected_pixels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corrected = np.full((4, 4, 3), 200, dtype=np.uint8)
    capture = SimpleNamespace(
        latest_frame=SimpleNamespace(raw=corrected, uncorrected_raw=None, timestamp=1.0)
    )
    manager = SimpleNamespace(getCaptureThreadForRole=lambda role: capture)
    monkeypatch.setattr(shared_state, "vision_manager", manager)

    result = cameras._grab_live_frame(
        "classification_channel",
        after_timestamp=0.0,
        timeout=0.0,
        prefer_uncorrected=True,
    )

    assert result is None
