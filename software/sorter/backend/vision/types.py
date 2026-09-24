from dataclasses import dataclass, field
from typing import Optional, Tuple, List
import time
import numpy as np


@dataclass
class VisionResult:
    class_id: Optional[int]
    class_name: Optional[str]
    confidence: float
    bbox: Optional[Tuple[int, int, int, int]]
    timestamp: float
    from_cache: bool = False
    created_at: float = field(default_factory=time.time)


@dataclass
class DetectedMask:
    mask: np.ndarray
    confidence: float
    class_id: int
    instance_id: int
    from_cache: bool = False
    created_at: float = field(default_factory=time.time)


@dataclass(init=False)
class CameraFrame:
    _source_bgr: np.ndarray
    annotated: Optional[np.ndarray]
    results: List[VisionResult]
    timestamp: float
    segmentation_map: Optional[np.ndarray] = field(default=None)
    uncorrected_raw: Optional[np.ndarray] = field(default=None)

    def __init__(self, raw, annotated, results, timestamp, segmentation_map=None,
                 uncorrected_raw=None, *, color_profile=None, color_role=None,
                 profiler=None):
        self._source_bgr = raw
        self.annotated = annotated
        self.results = results
        self.timestamp = timestamp
        self.segmentation_map = segmentation_map
        self.uncorrected_raw = uncorrected_raw
        self.color_profile = color_profile
        self.color_role = color_role
        self.profiler = profiler
        self._corrected_raw = None

    @property
    def source_bgr(self):
        """Original-resolution pixels; profile travels with this exact frame."""
        return self._source_bgr

    def correct_pixels(self, pixels, stage="crop_color_ms"):
        if self.color_profile is None or pixels is None:
            return pixels
        from vision.camera import apply_camera_color_profile
        started = time.perf_counter()
        corrected = apply_camera_color_profile(
            pixels, self.color_profile, role=self.color_role
        )
        if self.profiler is not None:
            self.profiler.observeDuration(
                f"camera.{self.color_role}.{stage}",
                (time.perf_counter() - started) * 1000.0,
            )
        return corrected

    @property
    def raw(self):
        """Compatibility image, corrected only when a full-resolution caller asks.

        Hot-path consumers use source_bgr and correct only their working pixels.
        No timestamp or source ownership changes when an image is materialized.
        """
        if self.color_profile is None:
            return self._source_bgr
        if self._corrected_raw is None:
            self._corrected_raw = self.correct_pixels(self._source_bgr, "full_image_on_demand_ms")
        return self._corrected_raw
