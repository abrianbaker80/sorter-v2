"""ColorChecker Classic 24 detection and encoded-RGB profile fitting."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Any

import cv2
import numpy as np

try:
    from calibration_reference import (
        COLORCHECKER24_PATCH_NAMES,
        COLORCHECKER24_REFERENCE_NAME,
        COLORCHECKER24_REFERENCE_RGB,
        COLORCHECKER24_TARGET_TYPE,
    )
except ModuleNotFoundError:
    from .calibration_reference import (
        COLORCHECKER24_PATCH_NAMES,
        COLORCHECKER24_REFERENCE_NAME,
        COLORCHECKER24_REFERENCE_RGB,
        COLORCHECKER24_TARGET_TYPE,
    )


_MAX_DETECTION_DIMENSION = 1280
_SATURATION_THRESHOLDS = (45, 55, 65, 75, 85)
_TARGET_COLS = 6
_COLOR_ROWS = 3
_PATCH_COUNT = 24
_MATRIX_REGULARIZATION = 0.05
_SAMPLE_HALF_WIDTH = 0.28
# Limits use CIE L*a*b* units (OpenCV float conversion, 0..1 encoded sRGB),
# with Euclidean CIE76 distance. They bound both the leave-one-column-out
# validation and the final fit. The values are based on C4 captures at two
# independent chart placements: 5.19/15.47 validation mean/max and 4.90/14.28
# holdout corrected mean/max. Keep a small margin for pixel quantization.
_MAX_CIE76_MEAN = 5.5
_MAX_CIE76_PATCH = 16.5


@dataclass(frozen=True)
class _GridCandidate:
    homography: np.ndarray
    pitch: float
    residual: float
    component_count: int


def analyze_colorchecker24(frame: np.ndarray):
    """Return an analysis only for a complete, unambiguous ColorChecker24.

    The 18 chromatic patches provide a 6 by 3 center grid. Its homography
    predicts the neutral row; reference-color fitting determines the chart's
    reading direction. Sampling is restricted to central patch interiors.
    """
    if frame is None or frame.size == 0 or frame.ndim != 3 or frame.shape[2] != 3:
        return None

    candidate = _detect_chromatic_grid(frame)
    if candidate is None:
        return None

    orientation_candidates: list[tuple[float, list[dict[str, Any]], np.ndarray, int, int]] = []
    for reverse_columns in (False, True):
        for reverse_rows in (False, True):
            samples = _sample_orientation(
                frame,
                candidate.homography,
                reverse_columns=reverse_columns,
                reverse_rows=reverse_rows,
            )
            if samples is None:
                continue
            observed = np.asarray(
                [sample["mean_rgb"] for sample in samples], dtype=np.float64
            ) / 255.0
            target = np.asarray(COLORCHECKER24_REFERENCE_RGB, dtype=np.float64)
            matrix = _fit_matrix(observed, target)
            if matrix is None:
                continue
            corrected = np.clip(observed @ matrix.T, 0.0, 1.0)
            error = float(np.mean(np.linalg.norm(corrected - target, axis=1)))
            orientation_candidates.append(
                (
                    error,
                    samples,
                    matrix,
                    -1 if reverse_rows else 1,
                    -1 if reverse_columns else 1,
                )
            )

    if len(orientation_candidates) < 2:
        return None
    orientation_candidates.sort(key=lambda item: item[0])
    best_error, samples, matrix, row_direction, column_direction = orientation_candidates[0]
    runner_up_error = orientation_candidates[1][0]
    if (
        best_error > 0.20
        or runner_up_error - best_error < max(0.012, best_error * 0.15)
        or np.any(np.abs(matrix) > 2.0)
    ):
        return None

    sample_clipping = [float(sample["clip_fraction"]) for sample in samples]
    if max(sample_clipping, default=1.0) > 0.12:
        return None
    neutral_rgb_check = np.asarray(
        [sample["mean_rgb"] for sample in samples[18:]], dtype=np.float64
    ) / 255.0
    neutral_luma = neutral_rgb_check @ np.array([0.2126, 0.7152, 0.0722])
    if np.any(np.diff(neutral_luma) >= -0.008):
        return None

    from server.camera_calibration import CalibrationAnalysis

    target = np.asarray(COLORCHECKER24_REFERENCE_RGB, dtype=np.float64)
    observed = np.asarray(
        [sample["mean_rgb"] for sample in samples], dtype=np.float64
    ) / 255.0
    predicted = np.clip(observed @ matrix.T, 0.0, 1.0)
    corrected_rgb_error = np.linalg.norm(predicted - target, axis=1)
    sample_luma = observed @ np.array([0.2126, 0.7152, 0.0722]) * 255.0
    bgr_rows = np.asarray([sample["mean_rgb"][::-1] for sample in samples], dtype=np.uint8)
    lab = cv2.cvtColor(bgr_rows.reshape(1, _PATCH_COUNT, 3), cv2.COLOR_BGR2LAB).reshape(
        _PATCH_COUNT, 3
    )
    chromatic_rgb = bgr_rows[:18]
    chromatic_hsv = cv2.cvtColor(
        chromatic_rgb.reshape(1, 18, 3), cv2.COLOR_BGR2HSV
    ).reshape(18, 3)
    chromatic_labs = lab[:18].astype(np.float32)
    color_distances = [
        float(np.linalg.norm(chromatic_labs[left] - chromatic_labs[right]))
        for left, right in combinations(range(18), 2)
    ]
    neutral_rgb = np.asarray([sample["mean_rgb"] for sample in samples[18:]], dtype=np.float64)
    neutral_cast = float(
        np.mean(
            np.std(
                neutral_rgb / np.maximum(neutral_rgb.mean(axis=1, keepdims=True), 1.0),
                axis=1,
            )
        )
    )

    chart_row_min = -1.5 if row_direction < 0 else -0.5
    chart_row_max = 2.5 if row_direction < 0 else 3.5
    cell_corners = _project_grid(
        candidate.homography,
        [
            (-0.5, chart_row_min),
            (5.5, chart_row_min),
            (5.5, chart_row_max),
            (-0.5, chart_row_max),
        ],
    )
    if cell_corners is None:
        return None
    bbox = (
        int(np.floor(np.min(cell_corners[:, 0]))),
        int(np.floor(np.min(cell_corners[:, 1]))),
        int(np.ceil(np.max(cell_corners[:, 0]))),
        int(np.ceil(np.max(cell_corners[:, 1]))),
    )
    bbox = (
        max(0, bbox[0]),
        max(0, bbox[1]),
        min(frame.shape[1], bbox[2]),
        min(frame.shape[0], bbox[3]),
    )
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        return None

    tile_samples: dict[str, dict[str, Any]] = {}
    for index, (name, sample, match_error) in enumerate(
        zip(
            COLORCHECKER24_PATCH_NAMES,
            samples,
            corrected_rgb_error,
            strict=True,
        )
    ):
        tile_samples[f"cc24_{index + 1:02d}_{name.replace(' ', '_').replace('(', '').replace(')', '')}"] = {
            "mean_rgb": sample["mean_rgb"],
            "clip_fraction": sample["clip_fraction"],
            "sample_pixels": sample["sample_pixels"],
            "center_xy": sample["center_xy"],
            "sample_polygon_xy": sample["sample_polygon_xy"],
            "reference_error": float(match_error),
            "reference_match_percent": float(max(0.0, 100.0 * (1.0 - match_error))),
        }

    return CalibrationAnalysis(
        pattern_size=(_TARGET_COLS, 4),
        score=float(100.0 * (1.0 - best_error)),
        total_cells=_PATCH_COUNT,
        bright_cell_count=int(np.count_nonzero(sample_luma[18:] >= 150.0)),
        dark_cell_count=int(np.count_nonzero(sample_luma[18:] <= 85.0)),
        color_cell_count=18,
        white_luma_mean=float(sample_luma[18]),
        black_luma_mean=float(sample_luma[23]),
        neutral_contrast=float(sample_luma[18] - sample_luma[23]),
        clipped_white_fraction=max(sample_clipping[18:19], default=0.0),
        shadow_black_fraction=float(np.mean(np.asarray(samples[23]["mean_rgb"]) <= 8.0)),
        white_balance_cast=neutral_cast,
        color_separation=float(np.mean(color_distances)) if color_distances else 0.0,
        colorfulness=float(np.mean(chromatic_hsv[:, 1])),
        reference_color_error_mean=float(np.mean(corrected_rgb_error)),
        board_bbox=bbox,
        normalized_board_bbox=(
            bbox[0] / frame.shape[1],
            bbox[1] / frame.shape[0],
            bbox[2] / frame.shape[1],
            bbox[3] / frame.shape[0],
        ),
        neutral_mean_bgr=tuple(float(value) for value in np.mean(neutral_rgb, axis=0)[::-1]),
        tile_samples=tile_samples,
        target_type=COLORCHECKER24_TARGET_TYPE,
        target_reference=COLORCHECKER24_REFERENCE_NAME,
    )


def generate_color_profile(
    tile_samples: dict[str, Any],
    *,
    target_reference: str | None = None,
) -> dict[str, Any] | None:
    """Fit the matrix-only encoded-sRGB operation used by the live runtime."""
    if target_reference != COLORCHECKER24_REFERENCE_NAME:
        return None

    observed_rows: list[list[float]] = []
    labels = _sample_labels()
    for label in labels:
        sample = tile_samples.get(label)
        rgb = sample.get("mean_rgb") if isinstance(sample, dict) else None
        if (
            not isinstance(rgb, list)
            or len(rgb) != 3
            or not all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and np.isfinite(value)
                and 0.0 <= value <= 255.0
                for value in rgb
            )
        ):
            return None
        if float(sample.get("clip_fraction", 0.0)) > 0.12:
            return None
        observed_rows.append([float(value) / 255.0 for value in rgb])

    observed = np.asarray(observed_rows, dtype=np.float64)
    target = np.asarray(COLORCHECKER24_REFERENCE_RGB, dtype=np.float64)
    if observed.shape != (24, 3) or np.linalg.matrix_rank(observed) < 3:
        return None

    matrix = _fit_matrix(observed, target)
    if matrix is None or np.any(np.abs(matrix) > 2.0):
        return None
    if not np.isfinite(np.linalg.cond(matrix)) or np.linalg.cond(matrix) > 25.0:
        return None

    corrected_unclipped = observed @ matrix.T
    corrected = np.clip(corrected_unclipped, 0.0, 1.0)
    clipped_fraction = float(np.mean((corrected_unclipped < 0.0) | (corrected_unclipped > 1.0)))
    if clipped_fraction > 0.08:
        return None

    cross_validated = np.empty_like(target)
    for held_out_column in range(_TARGET_COLS):
        held_out = np.arange(_PATCH_COUNT) % _TARGET_COLS == held_out_column
        validation_matrix = _fit_matrix(observed[~held_out], target[~held_out])
        if validation_matrix is None or np.any(np.abs(validation_matrix) > 2.0):
            return None
        cross_validated[held_out] = np.clip(
            observed[held_out] @ validation_matrix.T, 0.0, 1.0
        )
    validation_rgb = cross_validated
    validation_lab = _encoded_srgb_to_lab(validation_rgb)
    target_lab = _encoded_srgb_to_lab(target)
    validation_errors = np.linalg.norm(validation_lab - target_lab, axis=1)
    if (
        float(np.mean(validation_errors)) > _MAX_CIE76_MEAN
        or float(np.max(validation_errors)) > _MAX_CIE76_PATCH
    ):
        return None

    fit_lab = _encoded_srgb_to_lab(corrected)
    fit_errors = np.linalg.norm(fit_lab - target_lab, axis=1)
    if (
        float(np.mean(fit_errors)) > _MAX_CIE76_MEAN
        or float(np.max(fit_errors)) > _MAX_CIE76_PATCH
    ):
        return None
    return {
        "enabled": True,
        "matrix": [[float(value) for value in row] for row in matrix.tolist()],
        "bias": [0.0, 0.0, 0.0],
        "calibration_target_type": COLORCHECKER24_TARGET_TYPE,
        "calibration_reference": COLORCHECKER24_REFERENCE_NAME,
        "reference_error_mean": float(np.mean(fit_errors)),
        "reference_error_max": float(np.max(fit_errors)),
        "validation_error_mean": float(np.mean(validation_errors)),
        "validation_error_max": float(np.max(validation_errors)),
    }


def _sample_labels() -> tuple[str, ...]:
    return tuple(
        f"cc24_{index + 1:02d}_{name.replace(' ', '_').replace('(', '').replace(')', '')}"
        for index, name in enumerate(COLORCHECKER24_PATCH_NAMES)
    )


def _fit_matrix(observed: np.ndarray, reference: np.ndarray) -> np.ndarray | None:
    if (
        observed.ndim != 2
        or reference.shape != observed.shape
        or observed.shape[1] != 3
        or observed.shape[0] < 3
        or np.linalg.matrix_rank(observed) < 3
    ):
        return None
    prior = np.eye(3, dtype=np.float64) * np.sqrt(_MATRIX_REGULARIZATION)
    solution, _, _, _ = np.linalg.lstsq(
        np.vstack([observed, prior]),
        np.vstack([reference, prior]),
        rcond=None,
    )
    matrix = solution.T
    if not np.isfinite(matrix).all():
        return None
    return matrix


def _encoded_srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    bounded = np.clip(rgb, 0.0, 1.0).astype(np.float32)
    bgr = bounded[:, ::-1].reshape(1, len(bounded), 3)
    # Float input keeps OpenCV's Lab output in CIE L*a*b* units. The 8-bit
    # conversion scales L* to 0..255, which must not be compared to a Lab
    # distance threshold as if all three channels shared CIE units.
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB).reshape(len(bounded), 3).astype(np.float64)


def _detect_chromatic_grid(frame: np.ndarray) -> _GridCandidate | None:
    scale = min(1.0, _MAX_DETECTION_DIMENSION / float(max(frame.shape[:2])))
    work = (
        cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        if scale < 1.0
        else frame
    )
    hsv = cv2.cvtColor(work, cv2.COLOR_BGR2HSV)
    frame_area = float(work.shape[0] * work.shape[1])
    best: _GridCandidate | None = None
    best_score = float("inf")

    for threshold in _SATURATION_THRESHOLDS:
        mask = cv2.inRange(
            hsv,
            np.array([0, threshold, 25], dtype=np.uint8),
            np.array([179, 255, 255], dtype=np.uint8),
        )
        count, _, stats, centers = cv2.connectedComponentsWithStats(mask)
        components: list[tuple[np.ndarray, float, float]] = []
        for index in range(1, count):
            x, y, width, height, area = (int(value) for value in stats[index])
            if (
                area < frame_area * 0.00025
                or area > frame_area * 0.025
                or min(width, height) < 7
                or max(width, height) / float(max(1, min(width, height))) > 1.9
                or area / float(max(1, width * height)) < 0.30
            ):
                continue
            components.append(
                (centers[index].astype(np.float64), float(area), float(np.sqrt(width * height)))
            )
        if len(components) < 14:
            continue

        sizes = np.asarray([component[2] for component in components], dtype=np.float64)
        size_window = float(np.log(1.4))
        size_mode = max(
            sizes.tolist(),
            key=lambda value: int(np.count_nonzero(np.abs(np.log(sizes / value)) <= size_window)),
        )
        group = [
            component
            for component in components
            if abs(float(np.log(component[2] / size_mode))) <= size_window
        ]
        if len(group) < 14:
            continue
        points = np.asarray([component[0] for component in group], dtype=np.float64)
        patch_sizes = np.asarray([component[2] for component in group], dtype=np.float64)
        pitch = float(np.median(patch_sizes) * 1.05)
        candidate = _best_lattice(points, patch_sizes, pitch, frame_area, scale)
        if candidate is None:
            continue
        score = candidate.residual + abs(candidate.pitch - pitch) / max(pitch, 1.0)
        if score < best_score:
            best = candidate
            best_score = score
    return best


def _best_lattice(
    points: np.ndarray,
    patch_sizes: np.ndarray,
    pitch: float,
    frame_area: float,
    scale: float,
) -> _GridCandidate | None:
    neighbor_distance = np.linalg.norm(points[:, None, :] - points[None, :, :], axis=2)
    best: _GridCandidate | None = None
    best_score = float("inf")
    normalized_limit = 0.34

    for anchor_index, anchor in enumerate(points):
        neighbors = np.flatnonzero(
            (neighbor_distance[anchor_index] >= pitch * 0.78)
            & (neighbor_distance[anchor_index] <= pitch * 1.45)
        )
        if len(neighbors) < 2:
            continue
        for first_index, second_index in combinations(neighbors.tolist(), 2):
            first = points[first_index] - anchor
            second = points[second_index] - anchor
            first_length = float(np.linalg.norm(first))
            second_length = float(np.linalg.norm(second))
            if min(first_length, second_length) / max(first_length, second_length) < 0.68:
                continue
            cosine = abs(float(np.dot(first, second) / (first_length * second_length)))
            if cosine > 0.55:
                continue
            basis = np.column_stack([first, second])
            if abs(float(np.linalg.det(basis))) < pitch * pitch * 0.65:
                continue
            try:
                coordinates = np.linalg.solve(basis, (points - anchor).T).T
            except np.linalg.LinAlgError:
                continue

            rounded = np.rint(coordinates)
            residuals = np.linalg.norm(coordinates - rounded, axis=1)
            valid = (
                (residuals <= normalized_limit)
                & (rounded[:, 0] >= 0)
                & (rounded[:, 0] < _TARGET_COLS)
                & (rounded[:, 1] >= 0)
                & (rounded[:, 1] < _COLOR_ROWS)
            )
            matches: dict[tuple[int, int], tuple[float, int]] = {}
            for point_index in np.flatnonzero(valid):
                cell = (int(rounded[point_index, 0]), int(rounded[point_index, 1]))
                residual = float(residuals[point_index])
                previous = matches.get(cell)
                if previous is None or residual < previous[0]:
                    matches[cell] = (residual, int(point_index))
            if len(matches) != _TARGET_COLS * _COLOR_ROWS:
                continue
            if any(sum(cell[1] == row for cell in matches) != _TARGET_COLS for row in range(_COLOR_ROWS)):
                continue

            source = np.asarray(list(matches.keys()), dtype=np.float32)
            destination = np.asarray(
                [points[matches[tuple(int(v) for v in cell)][1]] / scale for cell in matches],
                dtype=np.float32,
            )
            homography, _ = cv2.findHomography(source, destination, method=0)
            if homography is None or not np.isfinite(homography).all():
                continue
            fitted = _project_grid(homography, [tuple(point) for point in source.tolist()])
            if fitted is None:
                continue
            error = np.linalg.norm(fitted - destination, axis=1)
            median_size_native = float(np.median(patch_sizes) / scale)
            normalized_error = float(np.mean(error) / max(median_size_native, 1.0))
            if normalized_error > 0.16:
                continue
            if normalized_error < best_score:
                best = _GridCandidate(
                    homography=homography,
                    pitch=float(np.mean([first_length, second_length]) / scale),
                    residual=normalized_error,
                    component_count=len(matches),
                )
                best_score = normalized_error
    return best


def _sample_orientation(
    frame: np.ndarray,
    homography: np.ndarray,
    *,
    reverse_columns: bool,
    reverse_rows: bool,
) -> list[dict[str, Any]] | None:
    locations: list[tuple[int, int]] = []
    for row in range(4):
        grid_row = (2 - row) if reverse_rows else row
        if row == 3:
            grid_row = -1 if reverse_rows else 3
        for column in range(_TARGET_COLS):
            grid_column = (_TARGET_COLS - 1 - column) if reverse_columns else column
            locations.append((grid_column, grid_row))

    samples: list[dict[str, Any]] = []
    for column, row in locations:
        sample = _sample_patch(frame, homography, column, row)
        if sample is None:
            return None
        samples.append(sample)
    return samples


def _sample_patch(
    frame: np.ndarray,
    homography: np.ndarray,
    column: int,
    row: int,
) -> dict[str, Any] | None:
    half = _SAMPLE_HALF_WIDTH
    source = np.asarray(
        [
            [column - half, row - half],
            [column + half, row - half],
            [column + half, row + half],
            [column - half, row + half],
        ],
        dtype=np.float32,
    ).reshape(1, 4, 2)
    polygon = cv2.perspectiveTransform(source, homography)[0]
    if not np.isfinite(polygon).all():
        return None
    center = _project_grid(homography, [(float(column), float(row))])
    if center is None:
        return None
    x0 = max(0, int(np.floor(np.min(polygon[:, 0]))))
    y0 = max(0, int(np.floor(np.min(polygon[:, 1]))))
    x1 = min(frame.shape[1], int(np.ceil(np.max(polygon[:, 0]))) + 1)
    y1 = min(frame.shape[0], int(np.ceil(np.max(polygon[:, 1]))) + 1)
    if x1 - x0 < 5 or y1 - y0 < 5:
        return None
    local_polygon = np.round(polygon - np.asarray([x0, y0], dtype=np.float32)).astype(np.int32)
    mask = np.zeros((y1 - y0, x1 - x0), dtype=np.uint8)
    cv2.fillConvexPoly(mask, local_polygon, 255)
    pixels = frame[y0:y1, x0:x1][mask > 0]
    if len(pixels) < 25:
        return None
    median_bgr = np.median(pixels, axis=0)
    clipping = float(np.mean(np.any(pixels >= 252, axis=1)))
    return {
        "mean_rgb": [float(value) for value in median_bgr[::-1]],
        "clip_fraction": clipping,
        "sample_pixels": int(len(pixels)),
        "center_xy": [float(value) for value in center[0]],
        "sample_polygon_xy": [
            [float(value) for value in point]
            for point in polygon.tolist()
        ],
    }


def _project_grid(
    homography: np.ndarray,
    points: list[tuple[float, float]],
) -> np.ndarray | None:
    source = np.asarray(points, dtype=np.float32).reshape(1, len(points), 2)
    projected = cv2.perspectiveTransform(source, homography)[0]
    return projected if projected.shape == (len(points), 2) and np.isfinite(projected).all() else None
