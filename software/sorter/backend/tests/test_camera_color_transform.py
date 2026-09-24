"""The optimized C4 CCM must preserve the previous corrected pixels."""

import numpy as np
import pytest

import vision.camera as camera
from irl.config import CameraColorProfile


def _previous_color_transform(frame, profile):
    matrix = np.asarray(profile.matrix, dtype=np.float32)
    bias = np.asarray(profile.bias, dtype=np.float32)
    has_lut = all(
        values is not None and len(values) == 256
        for values in (
            profile.response_lut_r,
            profile.response_lut_g,
            profile.response_lut_b,
        )
    )
    if has_lut:
        lut_r = np.asarray(profile.response_lut_r, dtype=np.float32)
        lut_g = np.asarray(profile.response_lut_g, dtype=np.float32)
        lut_b = np.asarray(profile.response_lut_b, dtype=np.float32)
        rgb = np.stack(
            [lut_r[frame[:, :, 2]], lut_g[frame[:, :, 1]], lut_b[frame[:, :, 0]]],
            axis=-1,
        )
    else:
        rgb = frame[:, :, ::-1].astype(np.float32) / 255.0

    corrected = np.tensordot(rgb, matrix.T, axes=1) + bias
    if (
        profile.gamma_a is not None
        and profile.gamma_exp is not None
        and profile.gamma_b is not None
        and len(profile.gamma_a) == len(profile.gamma_exp) == len(profile.gamma_b) == 3
    ):
        for channel in range(3):
            values = np.clip(corrected[:, :, channel], 0.0, None)
            corrected[:, :, channel] = (
                profile.gamma_a[channel] * values ** profile.gamma_exp[channel]
                + profile.gamma_b[channel]
            )

    corrected = np.clip(corrected, 0.0, 1.0)
    return np.round(corrected[:, :, ::-1] * 255.0).astype(np.uint8)


def _profile(*, lut=False, gamma=False):
    kwargs = {}
    if lut:
        x = np.linspace(0.0, 1.0, 256, dtype=np.float32)
        kwargs.update(
            response_lut_r=(x**1.02).tolist(),
            response_lut_g=(x**0.98).tolist(),
            response_lut_b=(x**1.01).tolist(),
        )
    if gamma:
        kwargs.update(
            gamma_a=[0.95, 1.02, 0.99],
            gamma_exp=[1.03, 0.98, 1.01],
            gamma_b=[0.005, 0.0, 0.01],
        )
    return CameraColorProfile(
        enabled=True,
        matrix=[
            [1.2259544614668858, -0.12140873300717056, 0.013872005995555606],
            [0.11959462818489135, 0.9115867730881115, 0.01797327133839857],
            [0.0958872347836722, 0.10322296426098376, 0.8663687994038818],
        ],
        bias=[0.0, 0.0, 0.0],
        **kwargs,
    )


@pytest.mark.parametrize("profile", [_profile(), _profile(lut=True, gamma=True)])
def test_opencv_matrix_path_preserves_corrected_pixels(profile, monkeypatch):
    monkeypatch.setattr(camera, "COLOR_CORRECTION_ENABLED", True)
    image = np.random.default_rng(20260921).integers(
        0, 256, size=(96, 128, 3), dtype=np.uint8
    )

    expected = _previous_color_transform(image, profile)
    actual = camera.apply_camera_color_profile(
        image, profile, role="classification_channel"
    )

    assert actual.dtype == np.uint8
    assert actual.shape == image.shape
    assert np.array_equal(actual, expected)
