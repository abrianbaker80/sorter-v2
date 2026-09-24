"""Tests for the global gate and C4-only color correction role boundary.

The global switch remains hardcoded. When enabled, only the canonical C4 camera
role may apply a profile; aliases and role-less calls fail closed.
"""

import unittest
from unittest import mock

import numpy as np

from irl.config import COLOR_CORRECTION_ALLOWED_ROLES, mkCameraColorProfile
from vision import camera as camera_module

# Deliberately far from identity so a single applied pass is unmistakable.
_WRECKING_PROFILE = mkCameraColorProfile(
    enabled=True,
    matrix=[[0.2, 0.9, 0.1], [0.4, 0.3, 0.7], [0.8, 0.1, 0.5]],
    bias=[0.3, -0.2, 0.4],
)


class ColorCorrectionKillSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.frame = np.full((8, 8, 3), 120, dtype=np.uint8)

    def test_disabled_switch_passes_frame_through_untouched(self) -> None:
        with mock.patch.object(camera_module, "COLOR_CORRECTION_ENABLED", False):
            result = camera_module.apply_camera_color_profile(
                self.frame,
                _WRECKING_PROFILE,
                role="classification_channel",
            )

        self.assertTrue(np.array_equal(result, self.frame))
        # Same object, not a copy — the disabled path must cost nothing per frame.
        self.assertIs(result, self.frame)

    def test_enabled_switch_applies_only_to_the_canonical_c4_role(self) -> None:
        with mock.patch.object(camera_module, "COLOR_CORRECTION_ENABLED", True):
            result = camera_module.apply_camera_color_profile(
                self.frame,
                _WRECKING_PROFILE,
                role="classification_channel",
            )

        self.assertFalse(np.array_equal(result, self.frame))
        self.assertEqual(COLOR_CORRECTION_ALLOWED_ROLES, frozenset({"classification_channel"}))

        for role in (None, "carousel", "feeder", "c_channel_2", "c_channel_3", "classification_top"):
            with self.subTest(role=role), mock.patch.object(camera_module, "COLOR_CORRECTION_ENABLED", True):
                result = camera_module.apply_camera_color_profile(
                    self.frame,
                    _WRECKING_PROFILE,
                    role=role,
                )
            self.assertIs(result, self.frame)

    def test_enabled_switch_leaves_a_disabled_profile_alone(self) -> None:
        disabled_profile = mkCameraColorProfile(enabled=False, matrix=_WRECKING_PROFILE.matrix)
        with mock.patch.object(camera_module, "COLOR_CORRECTION_ENABLED", True):
            result = camera_module.apply_camera_color_profile(
                self.frame,
                disabled_profile,
                role="classification_channel",
            )

        self.assertTrue(np.array_equal(result, self.frame))


if __name__ == "__main__":
    unittest.main()
