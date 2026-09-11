from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np

# Validated against Spencer's hand-labeled crop-quality dataset from Hive
# (2026-07-22, 172 labels across 4 machines; see agent notes
# projects/hive/image-quality-selection-research-2026-07-22.md). Raw whole-crop
# Laplacian variance separates starred from motion-blurred crops at AUC 0.63
# pooled across machines — the crop's BACKGROUND (sharp chute edge, dark seam)
# dominates the score, and machines sit in disjoint absolute ranges. The two
# metrics here fix that: Laplacian variance measured only inside a piece mask
# and normalized by the piece's own contrast (AUC 0.82), and FFT high-frequency
# energy at a fixed resample size, which removes the resolution dependence.
# Rank-combining the two ordered the starred frame above the blurred frame of
# the same burst in 38/39 labeled pairs. Selection is strictly relative WITHIN
# a burst — every frame shares the piece, lighting, and exposure — so nothing
# here is an absolute threshold that would need re-tuning per machine.

FFT_RESAMPLE_SIZE = 128
FFT_HF_RADIUS = 20
# BGR distance from the estimated background colors beyond which a pixel counts
# as piece. Background is estimated from the crop's border pixels (2-means), so
# this only needs to separate piece from backdrop, not be photometrically exact.
BACKGROUND_COLOR_DIST = 40.0
# Containment: the same piece should mask to roughly the same area in every
# frame of its burst; a frame where the piece is partially out of the crop
# shows a large drop vs the burst MEDIAN (not max — motion blur inflates the
# mask, so the max can itself be a blurred frame). Labeled not_contained frames
# sat at 0.0-0.82 of burst median, starred frames at 0.77-1.0+.
CONTAINMENT_AREA_RATIO = 0.70
# Below this fraction of the crop there is effectively no piece in the frame.
MIN_MASK_FRAC = 0.02
# The detector crop itself is color-independent evidence. A crop whose pixel
# area is far outside the rest of an at-rest burst usually contains a second
# pocket/piece or a badly split detection. These deliberately broad bounds do
# not reject ordinary detector-box jitter.
MIN_CROP_AREA_RATIO = 0.45
MAX_CROP_AREA_RATIO = 2.25
# Crops larger than this (longest side) are downsampled before scoring. Bounds
# the per-crop cost on the state-machine thread (~2 ms at typical crop sizes on
# the dev Mac, ~10 ms unbounded at 400 px); within a burst every frame gets the
# same treatment, so the relative comparisons are unaffected.
MAX_ANALYSIS_SIZE = 320


@dataclass
class CropQuality:
    # High-frequency FFT energy ratio at a fixed resample size. Higher = sharper.
    fft_hf_ratio: float
    # Contrast-normalized Laplacian variance inside the piece mask. Higher = sharper.
    lap_var_piece_norm: float
    mask_area_px: int
    mask_frac: float
    # Pixel area of the original detector crop, before analysis downsampling.
    crop_area_px: int
    # Raw whole-crop Laplacian variance, kept for logging continuity with the
    # stored `sharpness` values.
    lap_var: float


def _twoMeansBorderColors(bgr: np.ndarray) -> Optional[np.ndarray]:
    # Deterministic 2-means over the crop's border pixels: centers init from the
    # darkest and brightest border pixel, fixed Lloyd iterations. cv2.kmeans
    # would randomize its init, making scores non-reproducible run to run.
    border = np.concatenate(
        [bgr[0, :], bgr[-1, :], bgr[:, 0], bgr[:, -1]], axis=0
    ).astype(np.float32)
    if len(border) < 2:
        return None
    luma = border.sum(axis=1)
    centers = np.stack([border[int(luma.argmin())], border[int(luma.argmax())]])
    for _ in range(8):
        d = np.linalg.norm(border[:, None, :] - centers[None, :, :], axis=2)
        assign = d.argmin(axis=1)
        moved = 0.0
        for k in range(2):
            sel = border[assign == k]
            if len(sel):
                new_c = sel.mean(axis=0)
                moved = max(moved, float(np.linalg.norm(new_c - centers[k])))
                centers[k] = new_c
        if moved < 0.5:
            break
    return centers


def pieceMask(bgr: np.ndarray) -> Optional[np.ndarray]:
    centers = _twoMeansBorderColors(bgr)
    if centers is None:
        return None
    px = bgr.reshape(-1, 3).astype(np.float32)
    d = np.linalg.norm(px[:, None, :] - centers[None, :, :], axis=2).min(axis=1)
    mask = (d > BACKGROUND_COLOR_DIST).reshape(bgr.shape[:2]).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    return mask


def scoreCrop(bgr: np.ndarray) -> CropQuality:
    crop_area_px = int(bgr.shape[0] * bgr.shape[1])
    longest = max(bgr.shape[0], bgr.shape[1])
    if longest > MAX_ANALYSIS_SIZE:
        scale = MAX_ANALYSIS_SIZE / float(longest)
        bgr = cv2.resize(
            bgr,
            (max(1, round(bgr.shape[1] * scale)), max(1, round(bgr.shape[0] * scale))),
            interpolation=cv2.INTER_AREA,
        )
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    lap = cv2.Laplacian(gray, cv2.CV_32F)
    lap_var = float(lap.var())

    rs = cv2.resize(
        gray, (FFT_RESAMPLE_SIZE, FFT_RESAMPLE_SIZE), interpolation=cv2.INTER_AREA
    )
    rs = rs - rs.mean()
    f = np.abs(np.fft.fftshift(np.fft.fft2(rs)))
    half = FFT_RESAMPLE_SIZE // 2
    yy, xx = np.mgrid[-half:half, -half:half]
    r = np.sqrt(xx**2 + yy**2)
    fft_hf_ratio = float(f[r > FFT_HF_RADIUS].sum() / (f.sum() + 1e-6))

    mask = pieceMask(bgr)
    mask_area = int(mask.sum()) if mask is not None else 0
    mask_frac = float(mask_area) / float(max(1, gray.size))
    if mask is not None and mask_area > 50:
        m = mask.astype(bool)
        piece_contrast = float(gray[m].std()) + 1e-6
        lap_var_piece_norm = float(lap[m].var()) / (piece_contrast**2)
    else:
        # No usable mask (empty frame, or piece indistinguishable from the
        # backdrop): fall back to whole-crop contrast normalization so the
        # value still ranks sensibly instead of going to zero.
        contrast = float(gray.std()) + 1e-6
        lap_var_piece_norm = lap_var / (contrast**2)

    return CropQuality(
        fft_hf_ratio=fft_hf_ratio,
        lap_var_piece_norm=lap_var_piece_norm,
        mask_area_px=mask_area,
        mask_frac=mask_frac,
        crop_area_px=crop_area_px,
        lap_var=lap_var,
    )


def _combinedRankOrder(qualities: list[CropQuality], eligible: list[int]) -> list[int]:
    # Indices ordered best-first by the sum of per-metric ranks. Ties break
    # toward the LATER capture (more settled).
    fft_rank = {
        i: p for p, i in enumerate(sorted(eligible, key=lambda i: qualities[i].fft_hf_ratio))
    }
    lpn_rank = {
        i: p
        for p, i in enumerate(sorted(eligible, key=lambda i: qualities[i].lap_var_piece_norm))
    }
    return sorted(eligible, key=lambda i: (fft_rank[i] + lpn_rank[i], i), reverse=True)


def selectBurstIndices(qualities: list[CropQuality], max_count: int) -> list[int]:
    """Which frames of one at-rest burst ship to classification.

    Returns capture-order indices, always at least one when max_count permits.
    Detector-crop geometry removes gross multi-pocket/outlier crops. The color
    mask is used for containment only when the burst proves that mask reliable;
    pale parts on a pale background otherwise lose their most informative
    views. Every geometrically valid view is retained when it fits the caller's
    existing image ceiling. Sharpness only ranks frames when a smaller ceiling
    forces a choice (including the single-image anchor).
    """
    n = len(qualities)
    if n == 0 or max_count <= 0:
        return []
    eligible = list(range(n))

    if n >= 3:
        median_crop_area = float(np.median([q.crop_area_px for q in qualities]))
        kept = [
            i
            for i in eligible
            if MIN_CROP_AREA_RATIO * median_crop_area
            <= qualities[i].crop_area_px
            <= MAX_CROP_AREA_RATIO * median_crop_area
        ]
        if kept:
            eligible = kept

    if len(eligible) > 1:
        median_mask_frac = float(np.median([qualities[i].mask_frac for i in eligible]))
        if median_mask_frac >= MIN_MASK_FRAC:
            median_mask_area = float(
                np.median([qualities[i].mask_area_px for i in eligible])
            )
            kept = [
                i
                for i in eligible
                if qualities[i].mask_frac >= MIN_MASK_FRAC
                and qualities[i].mask_area_px
                >= CONTAINMENT_AREA_RATIO * median_mask_area
            ]
            if kept:
                eligible = kept

    if len(eligible) <= max_count:
        return sorted(eligible)

    ordered = _combinedRankOrder(qualities, eligible)
    return sorted(ordered[:max_count])


def bestIndex(qualities: list[CropQuality]) -> Optional[int]:
    selected = selectBurstIndices(qualities, 1)
    return selected[0] if selected else None
