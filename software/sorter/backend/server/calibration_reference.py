from __future__ import annotations

from typing import Final


# Seeded from the user-provided HDR Pixel reference photo of the calibration plate.
# These are intentionally stored as a stable shared reference palette for later tuning.
REFERENCE_TILE_RGB: Final[dict[str, tuple[int, int, int]]] = {
    "white": (219, 239, 243),
    "black": (27, 30, 37),
    "blue": (38, 156, 221),
    "red": (226, 43, 36),
    "green": (11, 155, 99),
    "yellow": (240, 214, 29),
}


REFERENCE_TILE_HEX: Final[dict[str, str]] = {
    label: f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
    for label, rgb in REFERENCE_TILE_RGB.items()
}


# ColorChecker Classic 24, post-November-2014 chart formulation, row-major
# patch order. These encoded sRGB targets were generated from the
# `ColorChecker24 - After November 2014` CIE xyY values distributed by
# colour-science 0.4.6 (X-Rite data): xyY -> XYZ (chart illuminant) -> sRGB,
# using that conversion's Bradford adaptation to D65. Kept as plain constants
# so the production backend does not need the analysis-only colour-science
# package. X-Rite states that formulations changed after November 2014 and the
# date is printed on the chart back. Calibrite says its 2021 rebrand covered all
# ColorChecker targets, so visible Calibrite branding identifies the newer
# formulation family. See X-Rite's "New color specifications" and Calibrite's
# "Introducing Calibrite Photo Solutions Powered by X-Rite".
COLORCHECKER24_REFERENCE_NAME: Final[str] = "ColorChecker Classic 24 - After November 2014"
COLORCHECKER24_TARGET_TYPE: Final[str] = "colorchecker24_after_2014"
COLORCHECKER24_PATCH_NAMES: Final[tuple[str, ...]] = (
    "dark skin",
    "light skin",
    "blue sky",
    "foliage",
    "blue flower",
    "bluish green",
    "orange",
    "purplish blue",
    "moderate red",
    "purple",
    "yellow green",
    "orange yellow",
    "blue",
    "green",
    "red",
    "yellow",
    "magenta",
    "cyan",
    "white 9.5 (.05 D)",
    "neutral 8 (.23 D)",
    "neutral 6.5 (.44 D)",
    "neutral 5 (.70 D)",
    "neutral 3.5 (1.05 D)",
    "black 2 (1.5 D)",
)
COLORCHECKER24_REFERENCE_RGB: Final[tuple[tuple[float, float, float], ...]] = (
    (0.45356655, 0.31087652, 0.25587675),
    (0.77324114, 0.56326784, 0.49773184),
    (0.35764049, 0.47260144, 0.60713530),
    (0.35759660, 0.42386612, 0.25330799),
    (0.51569792, 0.49941525, 0.68249802),
    (0.37263765, 0.73970920, 0.67432683),
    (0.87821582, 0.48645850, 0.19810499),
    (0.27309810, 0.35352380, 0.65283258),
    (0.77391711, 0.31468424, 0.37237415),
    (0.36529491, 0.22808766, 0.40559957),
    (0.60943939, 0.73202510, 0.24491847),
    (0.89062034, 0.63149716, 0.17313745),
    (0.16213397, 0.24450887, 0.56648226),
    (0.23327505, 0.57548849, 0.28187545),
    (0.69886179, 0.21138462, 0.22285775),
    (0.92511573, 0.78077491, 0.11623990),
    (0.75090031, 0.31185987, 0.57175083),
    (0.00000000, 0.52116605, 0.64612473),
    (0.94489362, 0.94753129, 0.92361443),
    (0.78845251, 0.79305546, 0.78853815),
    (0.63239670, 0.63985328, 0.63845264),
    (0.47322101, 0.47411577, 0.47292113),
    (0.32445235, 0.32956140, 0.33144971),
    (0.19431046, 0.19477451, 0.19743428),
)
