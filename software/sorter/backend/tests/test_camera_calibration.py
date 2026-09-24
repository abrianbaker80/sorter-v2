from __future__ import annotations

from server.calibration_reference import REFERENCE_TILE_RGB
from server.camera_calibration import generate_color_profile_from_analysis


_TILE_EXPECTATIONS = {
    "white_top": "white",
    "black_top": "black",
    "blue": "blue",
    "red": "red",
    "green": "green",
    "yellow": "yellow",
    "black_bottom": "black",
    "white_bottom": "white",
}


def test_six_color_profile_keeps_legacy_gamma_and_response_curve_behavior() -> None:
    analysis = {
        "tile_samples": {
            tile: {"mean_rgb": list(REFERENCE_TILE_RGB[label])}
            for tile, label in _TILE_EXPECTATIONS.items()
        }
    }
    identity_curve = {
        f"lut_{channel}": [index / 255.0 for index in range(256)]
        for channel in "rgb"
    }

    generated = generate_color_profile_from_analysis(analysis, identity_curve)

    assert generated is not None
    assert len(generated["matrix"]) == 3
    assert len(generated["bias"]) == 3
    assert len(generated["gamma_a"]) == 3
    assert len(generated["gamma_exp"]) == 3
    assert len(generated["gamma_b"]) == 3
    assert all(len(generated[f"response_lut_{channel}"]) == 256 for channel in "rgb")


def test_legacy_six_color_analysis_without_target_metadata_remains_supported() -> None:
    analysis = {
        "tile_samples": {
            tile: {"mean_rgb": list(REFERENCE_TILE_RGB[label])}
            for tile, label in _TILE_EXPECTATIONS.items()
        }
    }

    generated = generate_color_profile_from_analysis(analysis)

    assert generated is not None
    assert generated["enabled"] is True
    assert generated["matrix"]
