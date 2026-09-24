from __future__ import annotations

from unittest import mock

import cv2
import numpy as np
import pytest

from irl.config import cameraColorProfileToDict, parseCameraColorProfile
from server.calibration_reference import (
    COLORCHECKER24_PATCH_NAMES,
    COLORCHECKER24_REFERENCE_NAME,
    COLORCHECKER24_REFERENCE_RGB,
    COLORCHECKER24_TARGET_TYPE,
)
from server.camera_calibration import (
    analyze_camera_color_target,
    generate_color_profile_from_analysis,
)
from server.colorchecker24 import analyze_colorchecker24
from vision import camera as camera_module


_CAMERA_MATRIX = np.array(
    [
        [1.08, -0.04, -0.02],
        [-0.02, 1.06, -0.03],
        [-0.02, -0.03, 1.05],
    ],
    dtype=np.float64,
)


def _synthetic_colorchecker_frame(
    *,
    corners: np.ndarray | None = None,
    missing_patch: int | None = None,
    glare_patch: int | None = None,
    observed_rgb: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    cell = 70
    chart = np.full((4 * cell, 6 * cell, 3), 24, dtype=np.uint8)
    references = np.asarray(COLORCHECKER24_REFERENCE_RGB, dtype=np.float64)
    observed = (
        np.clip(references @ np.linalg.inv(_CAMERA_MATRIX).T, 0.0, 1.0)
        if observed_rgb is None
        else np.asarray(observed_rgb, dtype=np.float64)
    )
    for index, rgb in enumerate(observed):
        if index == missing_patch:
            continue
        row, column = divmod(index, 6)
        x0, y0 = column * cell + 5, row * cell + 5
        x1, y1 = (column + 1) * cell - 5, (row + 1) * cell - 5
        bgr = np.round(rgb[::-1] * 255.0).astype(np.uint8).tolist()
        cv2.rectangle(chart, (x0, y0), (x1 - 1, y1 - 1), bgr, thickness=-1)
        if index == glare_patch:
            cv2.rectangle(
                chart,
                (x0 + 8, y0 + 8),
                (x1 - 9, y1 - 9),
                (255, 255, 255),
                thickness=-1,
            )

    if corners is None:
        corners = np.float32([[155, 105], [620, 145], [585, 430], [125, 390]])
    transform = cv2.getPerspectiveTransform(
        np.float32([[0, 0], [chart.shape[1], 0], [chart.shape[1], chart.shape[0]], [0, chart.shape[0]]]),
        np.asarray(corners, dtype=np.float32),
    )
    frame = cv2.warpPerspective(
        chart,
        transform,
        (800, 560),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(48, 48, 48),
    )
    return frame, observed


@pytest.mark.parametrize(
    "corners",
    [
        np.float32([[155, 105], [620, 145], [585, 430], [125, 390]]),
        np.float32([[610, 100], [610, 470], [350, 465], [350, 105]]),
        np.float32([[635, 110], [170, 155], [205, 440], [670, 395]]),
    ],
    ids=("perspective", "rotated", "mirrored"),
)
def test_colorchecker24_detection_and_profile_are_rotation_safe(
    corners: np.ndarray,
) -> None:
    frame, observed = _synthetic_colorchecker_frame(corners=corners)

    analysis = analyze_camera_color_target(frame)

    assert analysis is not None
    assert analysis.target_type == COLORCHECKER24_TARGET_TYPE
    assert analysis.target_reference == COLORCHECKER24_REFERENCE_NAME
    assert analysis.pattern_size == (6, 4)
    assert len(analysis.tile_samples) == 24
    assert [sample["mean_rgb"] for sample in analysis.tile_samples.values()]
    assert list(COLORCHECKER24_PATCH_NAMES)[0] == "dark skin"

    generated = generate_color_profile_from_analysis(analysis)

    assert generated is not None
    assert generated["bias"] == [0.0, 0.0, 0.0]
    assert generated["calibration_target_type"] == COLORCHECKER24_TARGET_TYPE
    assert generated["calibration_reference"] == COLORCHECKER24_REFERENCE_NAME
    assert "validation_error_mean" in generated
    assert "validation_error_max" in generated
    assert generated["validation_error_mean"] <= 5.5
    assert generated["validation_error_max"] <= 16.5
    assert generated["reference_error_mean"] <= 5.5
    assert generated["reference_error_max"] <= 16.5
    assert not any(key.startswith("response_lut_") for key in generated)
    assert not any(key.startswith("gamma_") for key in generated)
    stored_profile = cameraColorProfileToDict(parseCameraColorProfile(generated))
    assert stored_profile["calibration_target_type"] == COLORCHECKER24_TARGET_TYPE
    assert stored_profile["calibration_reference"] == COLORCHECKER24_REFERENCE_NAME

    # Exercise the same encoded-RGB matrix, channel order, and quantization as
    # the runtime rather than only multiplying in the calibration helper.
    bgr_frame = np.round(observed[:, ::-1] * 255.0).astype(np.uint8).reshape(1, 24, 3)
    profile = parseCameraColorProfile(generated)
    with mock.patch.object(camera_module, "COLOR_CORRECTION_ENABLED", True):
        corrected = camera_module.apply_camera_color_profile(
            bgr_frame,
            profile,
            role="classification_channel",
        )
    corrected_rgb = corrected[0, :, ::-1].astype(np.float64) / 255.0
    target_rgb = np.asarray(COLORCHECKER24_REFERENCE_RGB, dtype=np.float64)
    assert float(np.mean(np.linalg.norm(corrected_rgb - target_rgb, axis=1))) < 0.025


@pytest.mark.parametrize(
    ("missing_patch", "glare_patch"),
    [(0, None), (None, 3)],
    ids=("missing-color-patch", "glare-in-sample-area"),
)
def test_colorchecker24_rejects_incomplete_or_glared_targets(
    missing_patch: int | None,
    glare_patch: int | None,
) -> None:
    frame, _ = _synthetic_colorchecker_frame(
        missing_patch=missing_patch,
        glare_patch=glare_patch,
    )

    assert analyze_camera_color_target(frame) is None


def test_colorchecker24_fitter_rejects_missing_neutral_patch_sample() -> None:
    frame, _ = _synthetic_colorchecker_frame()
    analysis = analyze_camera_color_target(frame)
    assert analysis is not None
    samples = analysis.to_dict()
    labels = list(samples["tile_samples"])
    del samples["tile_samples"][labels[-1]]

    assert generate_color_profile_from_analysis(samples) is None


@pytest.mark.parametrize("reference", [None, "unsupported-chart-revision"])
def test_colorchecker24_fitter_requires_the_known_chart_reference(
    reference: str | None,
) -> None:
    frame, _ = _synthetic_colorchecker_frame()
    analysis = analyze_camera_color_target(frame)
    assert analysis is not None
    samples = analysis.to_dict()
    samples["target_reference"] = reference

    assert generate_color_profile_from_analysis(samples) is None


def test_colorchecker24_fitter_rejects_non_matrix_camera_response() -> None:
    reference = np.asarray(COLORCHECKER24_REFERENCE_RGB, dtype=np.float64)
    camera_response = np.power(reference, np.asarray([0.7, 1.3, 0.8]))
    tile_samples = {}
    for index, (name, observed) in enumerate(
        zip(COLORCHECKER24_PATCH_NAMES, camera_response, strict=True)
    ):
        label = f"cc24_{index + 1:02d}_{name.replace(' ', '_').replace('(', '').replace(')', '')}"
        tile_samples[label] = {
            "mean_rgb": (observed * 255.0).tolist(),
            "clip_fraction": 0.0,
        }
    analysis = {
        "target_type": COLORCHECKER24_TARGET_TYPE,
        "target_reference": COLORCHECKER24_REFERENCE_NAME,
        "tile_samples": tile_samples,
    }

    assert generate_color_profile_from_analysis(analysis) is None


def test_colorchecker24_detector_rejects_a_distractor_color_grid() -> None:
    rng = np.random.default_rng(8101)
    hsv = np.column_stack(
        [
            rng.integers(0, 180, 24),
            rng.integers(180, 256, 24),
            rng.integers(110, 236, 24),
        ]
    ).astype(np.uint8)
    bgr = cv2.cvtColor(hsv.reshape(1, 24, 3), cv2.COLOR_HSV2BGR).reshape(24, 3)
    frame, _ = _synthetic_colorchecker_frame(observed_rgb=bgr[:, ::-1] / 255.0)

    assert analyze_colorchecker24(frame) is None
