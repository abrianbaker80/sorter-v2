"""Recognition-only adapter for C4IntakeRoutingBridge workers.

Reuse the existing provider clients and sorting profile lookup, without calling
the legacy recognizer's transport or object-update lifecycle. Construction is
explicit; production provider configuration and runtime selection are untouched.
"""

from __future__ import annotations

from math import isfinite
from typing import Mapping, Protocol

import cv2
import numpy as np

import basically_services
from classification.brickognize import _classifyImages
from classification.providers import (
    COLOR_PROVIDER_BRICKOGNIZE, COLOR_PROVIDER_HIVE_BASICALLY,
)
from global_config import GlobalConfig


class CategoryLookup(Protocol):
    def getCategoryIdForPart(self, part_id: str, color_id: str = "any_color") -> str: ...


def _unambiguous(items: object, min_score: float, margin: float) -> dict | None:
    if not isinstance(items, list) or not items:
        return None
    ranked: list[dict] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
            return None
        score = item.get("score")
        if isinstance(score, bool) or not isinstance(score, (float, int)) or not isfinite(score) or not 0 <= score <= 1:
            return None
        ranked.append(item)
    ranked.sort(key=lambda item: item["score"], reverse=True)
    best = ranked[0]
    if best["score"] < min_score:
        return None
    alternatives = [item for item in ranked[1:] if item["id"] != best["id"]]
    if alternatives and best["score"] - alternatives[0]["score"] <= margin:
        return None
    return best


class PocketRecognitionRouter:
    def __init__(
        self, *, gc: GlobalConfig, profile: CategoryLookup,
        category_destinations: Mapping[str, str], reachable_destinations: frozenset[str],
        min_score: float, ambiguity_margin: float,
        color_provider: str = COLOR_PROVIDER_BRICKOGNIZE,
    ) -> None:
        # Thresholds must be supplied from the integration's approved policy;
        # this slice does not silently pick new production confidence settings.
        for value in (min_score, ambiguity_margin):
            if not isfinite(value) or not 0 <= value <= 1:
                raise ValueError("confidence thresholds must be in [0, 1]")
        if color_provider not in (COLOR_PROVIDER_BRICKOGNIZE, COLOR_PROVIDER_HIVE_BASICALLY):
            raise ValueError("unsupported color provider")
        self._gc = gc
        self._profile = profile
        self._destinations = dict(category_destinations)
        self._reachable = frozenset(reachable_destinations)
        self._min_score = min_score
        self._margin = ambiguity_margin
        self._color_provider = color_provider

    def __call__(self, crop: np.ndarray) -> str | None:
        """Run on a bounded bridge worker. Errors propagate to DISCARD.

        profile is a stable, read-only category lookup for this bridge lifetime;
        reachable destinations are a calibrated snapshot, never a bin allocation.
        """
        result = _classifyImages(self._gc, [crop])
        if not isinstance(result, dict):
            return None
        item = _unambiguous(result.get("items"), self._min_score, self._margin)
        if item is None:
            return None
        if self._color_provider == COLOR_PROVIDER_HIVE_BASICALLY:
            ok, encoded = cv2.imencode(".jpg", crop)
            if not ok:
                return None
            color_result = basically_services.predictColor(self._gc, [encoded.tobytes()], [4])
            if not isinstance(color_result, dict):
                return None
            # Unlike the legacy UI fallback, a selected provider failure in this
            # bridge is a discard. Never substitute another provider's answer.
            color = _unambiguous([{
                "id": str(color_result.get("color_id")) if color_result.get("color_id") is not None else "",
                "score": color_result.get("confidence"),
            }], self._min_score, self._margin)
        else:
            color = _unambiguous(result.get("colors"), self._min_score, self._margin)
        if color is None:
            return None
        category = self._profile.getCategoryIdForPart(item["id"], color["id"])
        destination = self._destinations.get(category)
        return destination if destination and destination.strip() and destination in self._reachable else None
