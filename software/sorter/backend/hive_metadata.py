from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import threading
import time
from typing import Any, Optional
from urllib.parse import quote, urlsplit

from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import RequestException, Timeout as RequestsTimeout

from blob_manager import getHiveConfig
from global_config import GlobalConfig

# The sorter-client ``HiveClient`` lives alongside the sorter tree and is
# injected onto ``sys.path`` by ``server.hive_models`` at import time.
from server.hive_models import HiveClient, HiveError  # pyright: ignore[reportAttributeAccessIssue]

# Single source of per-piece metadata + BrickLink pricing. Hive owns the parts
# catalog; the machine fetches flattened metadata over the API and keeps a
# write-through persistent cache (local_state.hive_part_metadata_cache) so prices
# keep working across restarts and Hive outages. Nothing here reads a local
# parts.db — that dependency was removed.

# Any single physical dimension (bbox x/y/z) above this is treated as
# "too big" — the piece is sent down the center of the chute to the misc
# bottom bin instead of a real bin, regardless of its sorting category.
OVERSIZE_MAX_DIMENSION_MM = 80.0

# Serve a cached full-metadata entry immediately; if it's older than this, also
# kick a background refresh. Catalog + prices drift slowly, so this is generous.
_REFRESH_AFTER_S = 14 * 86400.0

# Per-process cache keyed by (part_num, color_key). Stores the resolved metadata
# dict, or None for parts Hive doesn't know about (a 404). Transient failures
# (connection errors, no primary target) are NOT cached so they retry.
_cache: dict[tuple[str, Optional[int]], Optional[dict[str, Any]]] = {}
_cache_lock = threading.Lock()

# Keys currently being refreshed in the background, so a burst of stale hits for
# the same part doesn't spawn a thread each.
_refreshing: set[tuple[str, Optional[int]]] = set()
_refreshing_lock = threading.Lock()

_bricklink_colors_cache: Optional[list[dict[str, Any]]] = None
_bricklink_colors_lock = threading.Lock()

_CATALOG_NAMESPACES = frozenset(
    {"rebrickable_part_number", "bricklink_item_number"}
)
_CATALOG_MAX_BATCH = 1
_CATALOG_MAX_RETRY_AFTER_S = 600
_CATALOG_CACHE_VERSION = 1
_CATALOG_CACHE_PREFIX = "catalog:v1"
_CATALOG_CACHE_COLOR_KEY = "catalog_generic_v1"
_CATALOG_POSITIVE_TTL_S = 14 * 86400.0
_CATALOG_NEGATIVE_TTL_S = 6 * 3600.0
_CATALOG_WAITER_TIMEOUT_S = 15.0
_CATALOG_BACKOFF_S = (30, 120, 600)
_CATALOG_POSITIVE_STATUSES = frozenset({"resolved", "no_image"})
_CATALOG_CACHEABLE_STATUSES = _CATALOG_POSITIVE_STATUSES | {"not_found"}
_catalog_cache: dict[str, tuple[dict[str, Any], float]] = {}
_catalog_cache_lock = threading.Lock()
_catalog_provider_lock = threading.Lock()


@dataclass
class _CatalogFlight:
    completed: threading.Event
    result: Optional[dict[str, Any]] = None


_catalog_flights: dict[str, _CatalogFlight] = {}
_catalog_failures: dict[str, tuple[int, float]] = {}


def getPrimaryHiveTarget() -> Optional[dict[str, Any]]:
    config = getHiveConfig()
    if not isinstance(config, dict):
        return None
    targets = [
        target
        for target in config.get("targets", [])
        if isinstance(target, dict)
        and target.get("enabled", True)
        and isinstance(target.get("url"), str)
        and target.get("url")
        and isinstance(target.get("api_token"), str)
        and target.get("api_token")
    ]
    if not targets:
        return None
    primary_id = config.get("primary_target_id")
    for target in targets:
        if target.get("id") == primary_id:
            return target
    return targets[0]


def _client() -> Optional[HiveClient]:
    target = getPrimaryHiveTarget()
    if target is None:
        return None
    return HiveClient(target["url"], target["api_token"])


def _parseColorKey(color_id: Optional[Any]) -> Optional[int]:
    if color_id is None:
        return None
    text = str(color_id).strip()
    if not text or text == "any_color":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _fetchFromHive(
    gc: GlobalConfig, part_num: str, color_key: Optional[int]
) -> tuple[Optional[dict[str, Any]], bool]:
    """Fetch flattened metadata for one part. Returns (metadata, definitive):
    definitive=True means Hive answered authoritatively (a dict, or a 404 → None)
    and the result may be cached; definitive=False means a transient failure that
    must not be cached."""
    client = _client()
    if client is None:
        gc.logger.warn("hive metadata: no primary Hive target configured")
        return None, False
    try:
        data = client.get_part_metadata(part_num, color_key)
    except HiveError as exc:
        if exc.status_code == 404:
            return None, True
        gc.logger.warn(f"hive metadata fetch failed for {part_num}: {exc}")
        return None, False
    except Exception as exc:
        gc.logger.warn(f"hive metadata fetch error for {part_num}: {exc}")
        return None, False
    return (data if isinstance(data, dict) else None), True


def _storeResult(
    part_num: str, color_key: Optional[int], metadata: Optional[dict[str, Any]]
) -> None:
    from local_state import put_cached_part_metadata

    with _cache_lock:
        _cache[(part_num, color_key)] = metadata
    moving_avg = metadata.get("moving_avg_price") if isinstance(metadata, dict) else None
    if isinstance(metadata, dict):
        # Only persist authoritative hits; a 404 (metadata=None) stays in-process.
        put_cached_part_metadata(part_num, color_key, metadata, moving_avg)


def _backgroundRefresh(gc: GlobalConfig, part_num: str, color_key: Optional[int]) -> None:
    key = (part_num, color_key)
    with _refreshing_lock:
        if key in _refreshing:
            return
        _refreshing.add(key)

    def _run() -> None:
        try:
            metadata, definitive = _fetchFromHive(gc, part_num, color_key)
            if definitive:
                _storeResult(part_num, color_key, metadata)
        finally:
            with _refreshing_lock:
                _refreshing.discard(key)

    threading.Thread(target=_run, daemon=True, name="hive-metadata-refresh").start()


def getPieceMetadata(
    gc: GlobalConfig,
    part_num: Optional[str],
    color_id: Optional[Any] = None,
    *,
    use_cache: bool = True,
) -> Optional[dict[str, Any]]:
    if not part_num:
        return None
    color_key = _parseColorKey(color_id)
    key = (part_num, color_key)

    if use_cache:
        with _cache_lock:
            if key in _cache:
                return _cache[key]

        from local_state import get_cached_part_metadata

        payload, _moving_avg, cached_at = get_cached_part_metadata(part_num, color_key)
        if payload is not None and cached_at is not None:
            with _cache_lock:
                _cache[key] = payload
            if time.time() - cached_at > _REFRESH_AFTER_S:
                _backgroundRefresh(gc, part_num, color_key)
            return payload

    metadata, definitive = _fetchFromHive(gc, part_num, color_key)
    if definitive:
        _storeResult(part_num, color_key, metadata)
    return metadata


def getBatchMovingAvgPrices(
    gc: GlobalConfig, pairs: list[tuple[Optional[str], Optional[Any]]]
) -> dict[tuple[Optional[str], Optional[Any]], Optional[float]]:
    """Moving-average price for many (part_id, color_id) pairs. Serves from the
    persistent cache (any age — historical revaluation tolerates stale) and fills
    all misses with a single batch request to Hive. Missing/unreachable → None."""
    from local_state import get_cached_part_prices, put_cached_part_prices

    result: dict[tuple[Optional[str], Optional[Any]], Optional[float]] = {}
    lookup_pairs = [(p, c) for (p, c) in pairs if p]
    cached = get_cached_part_prices([(p, c) for (p, c) in lookup_pairs])

    misses: list[dict[str, Any]] = []
    for part_id, color_id in pairs:
        if not part_id:
            result[(part_id, color_id)] = None
            continue
        color_key = _parseColorKey(color_id)
        norm = "any_color" if color_key is None else str(color_key)
        hit = cached.get((part_id, norm))
        if hit is not None:
            result[(part_id, color_id)] = hit[0]
        else:
            result[(part_id, color_id)] = None
            misses.append({"part_num": part_id, "color_id": color_key, "_orig": (part_id, color_id)})

    if not misses:
        return result

    client = _client()
    if client is None:
        return result
    try:
        payload = client.batch_piece_prices(
            [{"part_num": m["part_num"], "color_id": m["color_id"]} for m in misses]
        )
    except Exception as exc:
        gc.logger.warn(f"hive batch price fetch failed: {exc}")
        return result

    prices = payload.get("prices") if isinstance(payload, dict) else None
    if not isinstance(prices, list):
        return result

    # Align returned rows back to the original request order (the endpoint echoes
    # part_num + color_id per row).
    write_rows: list[tuple[Optional[str], Any, Optional[float]]] = []
    for miss, row in zip(misses, prices):
        moving_avg = row.get("moving_avg_price") if isinstance(row, dict) else None
        moving_avg = float(moving_avg) if isinstance(moving_avg, (int, float)) and moving_avg > 0 else None
        result[miss["_orig"]] = moving_avg
        write_rows.append((miss["part_num"], miss["color_id"], moving_avg))
    put_cached_part_prices(write_rows)
    return result


def listBrickLinkColors(gc: GlobalConfig) -> list[dict[str, Any]]:
    # BrickLink color palette for the correction dropdown. Static for the process
    # lifetime, so fetch once and memoize. Empty on any failure.
    global _bricklink_colors_cache
    with _bricklink_colors_lock:
        if _bricklink_colors_cache is not None:
            return _bricklink_colors_cache
    client = _client()
    if client is None:
        return []
    try:
        payload = client.list_bricklink_colors()
    except Exception as exc:
        gc.logger.warn(f"hive bricklink colors fetch failed: {exc}")
        return []
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return []
    with _bricklink_colors_lock:
        _bricklink_colors_cache = results
    return results


def maxDimensionMm(metadata: Optional[dict[str, Any]]) -> Optional[float]:
    if not isinstance(metadata, dict):
        return None
    dims = metadata.get("dimensions")
    if not isinstance(dims, dict):
        return None
    values = [dims.get("bbox_x_mm"), dims.get("bbox_y_mm"), dims.get("bbox_z_mm")]
    numeric = [float(v) for v in values if isinstance(v, (int, float))]
    return max(numeric) if numeric else None


def isOversize(max_dimension_mm: Optional[float]) -> bool:
    return max_dimension_mm is not None and max_dimension_mm > OVERSIZE_MAX_DIMENSION_MM


def _isCatalogIdentity(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and value == value.strip()
    )


def _normalizeCatalogRequest(
    part_id: Any,
    part_namespace: Any,
    color_id: Any = None,
    color_namespace: Any = None,
) -> dict[str, Optional[str]]:
    if not _isCatalogIdentity(part_id):
        raise ValueError("part_id must be a nonblank 1-64 character string")
    if (
        not isinstance(part_namespace, str)
        or part_namespace not in _CATALOG_NAMESPACES
    ):
        raise ValueError("part_namespace must be an explicit supported namespace")
    if (color_id is None) != (color_namespace is None):
        raise ValueError("color_id and color_namespace must be provided together")

    normalized_color_id: Optional[str] = None
    normalized_color_namespace: Optional[str] = None
    if color_id is not None:
        if (
            not isinstance(color_id, str)
            or not color_id.strip()
            or color_id != color_id.strip()
        ):
            raise ValueError("color_id must be a nonblank string without outer whitespace")
        if (
            not isinstance(color_namespace, str)
            or not color_namespace.strip()
            or color_namespace != color_namespace.strip()
        ):
            raise ValueError(
                "color_namespace must be a nonblank string without outer whitespace"
            )
        normalized_color_id = color_id
        normalized_color_namespace = color_namespace

    return {
        "requested_part_id": part_id,
        "requested_part_namespace": part_namespace,
        "requested_color_id": normalized_color_id,
        "requested_color_namespace": normalized_color_namespace,
    }


def _catalogResult(
    request: dict[str, Optional[str]],
    *,
    canonical_part_id: Optional[str] = None,
    name: Optional[str] = None,
    image_url: Optional[str] = None,
    image_match: str = "none",
    found: bool,
    status: str,
) -> dict[str, Any]:
    return {
        **request,
        "canonical_part_id": canonical_part_id,
        "canonical_part_namespace": (
            "rebrickable_part_number" if canonical_part_id is not None else None
        ),
        "provider_part_id": canonical_part_id,
        "name": name,
        "image_url": image_url,
        "image_source": "hive_rebrickable_catalog" if image_url is not None else None,
        "image_match": image_match,
        "color_specific": False,
        "found": found,
        "status": status,
        "stale": False,
        "retry_after_seconds": None,
    }


def _clampCatalogRetryAfter(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return min(value, _CATALOG_MAX_RETRY_AFTER_S)


def _temporaryCatalogResult(
    request: dict[str, Optional[str]],
    kind: str,
    retry_after_seconds: Any = None,
) -> dict[str, Any]:
    result = _catalogResult(request, found=False, status="temporarily_unavailable")
    result["retry_after_seconds"] = _clampCatalogRetryAfter(retry_after_seconds)
    result["_failure_kind"] = kind
    return result


def _unsupportedCatalogResult(request: dict[str, Optional[str]]) -> dict[str, Any]:
    return _temporaryCatalogResult(request, "unsupported_namespace", 600)


def _normalizeCatalogRow(
    request: dict[str, Optional[str]], hive_row: Any
) -> dict[str, Any]:
    def malformed() -> dict[str, Any]:
        return _temporaryCatalogResult(request, "malformed_response")

    if not isinstance(hive_row, dict) or hive_row.get("source") != "hive":
        return malformed()
    if (
        hive_row.get("requested_part_id") != request["requested_part_id"]
        or hive_row.get("requested_part_namespace")
        != request["requested_part_namespace"]
    ):
        return malformed()

    found = hive_row.get("found")
    resolution_status = hive_row.get("resolution_status")
    canonical_part_id = hive_row.get("canonical_part_id")
    canonical_namespace = hive_row.get("canonical_part_namespace")
    name = hive_row.get("name")
    image_url = hive_row.get("img_url")
    image_match = hive_row.get("image_match")

    if not isinstance(found, bool) or not isinstance(resolution_status, str):
        return malformed()
    if (canonical_part_id is None) != (canonical_namespace is None):
        return malformed()
    if canonical_part_id is not None and (
        not _isCatalogIdentity(canonical_part_id)
        or canonical_namespace != "rebrickable_part_number"
    ):
        return malformed()
    if name is not None and not isinstance(name, str):
        return malformed()
    if image_url is not None and (
        not isinstance(image_url, str)
        or not image_url.strip()
        or image_url != image_url.strip()
    ):
        return malformed()
    if image_match not in ("exact", "mapped", "none"):
        return malformed()

    if found is False:
        if (
            resolution_status == "not_found"
            and canonical_part_id is None
            and name is None
            and image_url is None
            and image_match == "none"
        ):
            return _catalogResult(request, found=False, status="not_found")
        return malformed()

    if resolution_status not in ("resolved", "ambiguous_mapping"):
        return malformed()
    if image_url is None:
        if image_match != "none":
            return malformed()
    elif image_match == "none":
        return malformed()

    if request["requested_part_namespace"] == "rebrickable_part_number":
        expected_match = "exact" if image_url is not None else "none"
        if (
            resolution_status != "resolved"
            or canonical_part_id != request["requested_part_id"]
            or image_match != expected_match
        ):
            return malformed()
    else:
        if resolution_status == "ambiguous_mapping" and (
            canonical_part_id is not None or image_url is not None
        ):
            return malformed()
        expected_match = (
            "mapped"
            if canonical_part_id is not None and image_url is not None
            else "none"
        )
        if image_match != expected_match:
            return malformed()

    return _catalogResult(
        request,
        canonical_part_id=canonical_part_id,
        name=name,
        image_url=image_url,
        image_match=image_match,
        found=True,
        status="resolved" if image_url is not None else "no_image",
    )


@dataclass(frozen=True)
class _CatalogTargetSnapshot:
    target_id: str
    base_url: str

    @property
    def identity(self) -> tuple[str, str]:
        return (self.target_id, self.base_url)


def _catalogTargetOperation() -> Optional[tuple[_CatalogTargetSnapshot, HiveClient]]:
    target = getPrimaryHiveTarget()
    if target is None:
        return None

    target_id = target.get("id")
    base_url = target.get("url")
    api_token = target.get("api_token")
    if (
        not isinstance(target_id, str)
        or not target_id.strip()
        or not isinstance(base_url, str)
        or not isinstance(api_token, str)
        or not api_token
    ):
        return None

    normalized_url = base_url.rstrip("/")
    if not normalized_url:
        return None

    try:
        parsed_url = urlsplit(normalized_url)
        hostname = parsed_url.hostname
        has_userinfo = parsed_url.username is not None or parsed_url.password is not None
    except ValueError:
        return None
    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.netloc
        or not hostname
        or has_userinfo
    ):
        return None

    snapshot = _CatalogTargetSnapshot(target_id=target_id, base_url=normalized_url)
    return snapshot, HiveClient(normalized_url, api_token)


def _catalogCacheKey(
    target: _CatalogTargetSnapshot, request: dict[str, Optional[str]]
) -> str:
    identity = json.dumps(target.identity, ensure_ascii=False, separators=(",", ":"))
    target_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    part_id = quote(str(request["requested_part_id"]), safe="")
    return (
        f"{_CATALOG_CACHE_PREFIX}:{target_hash}:"
        f"{request['requested_part_namespace']}:{part_id}:generic"
    )


def _catalogEnvelopeResult(
    envelope: Any,
    cache_key: str,
    request: dict[str, Optional[str]],
) -> Optional[dict[str, Any]]:
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"version", "cache_key", "result"}
        or type(envelope.get("version")) is not int
        or envelope.get("version") != _CATALOG_CACHE_VERSION
        or envelope.get("cache_key") != cache_key
    ):
        return None
    result = envelope.get("result")
    if (
        not isinstance(result, dict)
        or result.get("status") not in _CATALOG_CACHEABLE_STATUSES
    ):
        return None
    status = result["status"]
    row = {
        "source": "hive",
        "requested_part_id": request["requested_part_id"],
        "requested_part_namespace": request["requested_part_namespace"],
        "found": result.get("found"),
        "resolution_status": "not_found" if status == "not_found" else "resolved",
        "canonical_part_id": result.get("canonical_part_id"),
        "canonical_part_namespace": result.get("canonical_part_namespace"),
        "name": result.get("name"),
        "img_url": result.get("image_url"),
        "image_match": result.get("image_match"),
    }
    normalized = _normalizeCatalogRow(request, row)
    if set(result) != set(normalized) or any(
        type(result[key]) is not type(value) or result[key] != value
        for key, value in normalized.items()
    ):
        return None
    return normalized


def _catalogCachedEntry(
    cache_key: str,
    request: dict[str, Optional[str]],
    now: float,
) -> Optional[tuple[dict[str, Any], bool]]:
    with _catalog_cache_lock:
        cached = _catalog_cache.get(cache_key)
    if cached is None:
        try:
            from local_state import get_cached_part_metadata

            envelope, _moving_avg, cached_at = get_cached_part_metadata(
                cache_key, _CATALOG_CACHE_COLOR_KEY
            )
        except Exception:
            return None
        if isinstance(cached_at, bool) or not isinstance(cached_at, (int, float)):
            return None
        timestamp = float(cached_at)
        if not math.isfinite(timestamp):
            return None
        result = _catalogEnvelopeResult(envelope, cache_key, request)
        if result is None:
            return None
        cached = (result, timestamp)
        with _catalog_cache_lock:
            cached = _catalog_cache.setdefault(cache_key, cached)
    result, cached_at = cached
    ttl = (
        _CATALOG_NEGATIVE_TTL_S
        if result["status"] == "not_found"
        else _CATALOG_POSITIVE_TTL_S
    )
    return dict(result), now - cached_at <= ttl


def _publishCatalogMemoryResult(
    cache_key: str, result: dict[str, Any]
) -> dict[str, Any]:
    clean = dict(result)
    clean.pop("_failure_kind", None)
    clean["stale"] = False
    clean["retry_after_seconds"] = None
    with _catalog_cache_lock:
        _catalog_cache[cache_key] = (clean, time.time())
    return clean


def _persistCatalogResult(cache_key: str, clean: dict[str, Any]) -> None:
    envelope = {
        "version": _CATALOG_CACHE_VERSION,
        "cache_key": cache_key,
        "result": clean,
    }
    try:
        from local_state import put_cached_part_metadata

        put_cached_part_metadata(
            cache_key, _CATALOG_CACHE_COLOR_KEY, envelope, None
        )
    except Exception:
        pass


_catalogMonotonic = time.monotonic


def _recordCatalogFailure(cache_key: str, retry_after_seconds: Any) -> int:
    provider_delay = _clampCatalogRetryAfter(retry_after_seconds) or 0
    with _catalog_cache_lock:
        failure_count = _catalog_failures.get(cache_key, (0, 0.0))[0] + 1
        local_delay = _CATALOG_BACKOFF_S[min(failure_count - 1, 2)]
        delay = max(local_delay, provider_delay)
        _catalog_failures[cache_key] = (failure_count, _catalogMonotonic() + delay)
    return delay


def _completeCatalogFlight(flight: _CatalogFlight, result: dict[str, Any]) -> None:
    with _catalog_cache_lock:
        flight.result = dict(result)
        flight.completed.set()


def _waitCatalogFlight(flight: _CatalogFlight, timeout: float) -> bool:
    return flight.completed.wait(timeout)


def _catalogFailureKind(exc: Exception) -> str:
    if isinstance(exc, HiveError):
        if getattr(exc, "local_validation", False) is True:
            return "malformed_response"
        if getattr(exc, "status_code", None) == 429:
            return "rate_limited"
        return "provider_error"
    if isinstance(exc, RequestsTimeout):
        return "timeout"
    if isinstance(exc, (RequestsConnectionError, RequestException)):
        return "network_error"
    return "provider_error"


def _temporaryCatalogResults(
    requests: list[dict[str, Optional[str]]],
    kind: str,
    retry_after_seconds: Any = None,
) -> list[dict[str, Any]]:
    return [
        _temporaryCatalogResult(request, kind, retry_after_seconds)
        for request in requests
    ]


def resolvePartCatalog(
    part_id: Any,
    part_namespace: Any,
    color_id: Any = None,
    color_namespace: Any = None,
) -> dict[str, Any]:
    return resolvePartCatalogBatch(
        [
            {
                "part_id": part_id,
                "part_namespace": part_namespace,
                "color_id": color_id,
                "color_namespace": color_namespace,
            }
        ]
    )[0]


def resolvePartCatalogBatch(catalog_requests: Any) -> list[dict[str, Any]]:
    if not isinstance(catalog_requests, list) or not catalog_requests:
        raise ValueError("catalog requests must be a nonempty list")

    normalized_requests: list[dict[str, Optional[str]]] = []
    for request in catalog_requests:
        if not isinstance(request, dict):
            raise ValueError("each catalog request must be a dictionary")
        normalized_requests.append(
            _normalizeCatalogRequest(
                request.get("part_id"),
                request.get("part_namespace"),
                request.get("color_id"),
                request.get("color_namespace"),
            )
        )

    if all(
        request["requested_part_namespace"] != "rebrickable_part_number"
        for request in normalized_requests
    ):
        return [_unsupportedCatalogResult(request) for request in normalized_requests]

    try:
        operation = _catalogTargetOperation()
    except Exception:
        operation = None
    if operation is None:
        return [
            (
                _temporaryCatalogResult(request, "missing_target")
                if request["requested_part_namespace"] == "rebrickable_part_number"
                else _unsupportedCatalogResult(request)
            )
            for request in normalized_requests
        ]

    target_snapshot, client = operation
    unique: dict[str, dict[str, Optional[str]]] = {}
    position_keys: list[Optional[str]] = []
    for request in normalized_requests:
        if request["requested_part_namespace"] != "rebrickable_part_number":
            position_keys.append(None)
            continue
        generic_request = dict(request)
        generic_request["requested_color_id"] = None
        generic_request["requested_color_namespace"] = None
        cache_key = _catalogCacheKey(target_snapshot, generic_request)
        position_keys.append(cache_key)
        unique.setdefault(cache_key, generic_request)

    now = time.time()
    base_results: dict[str, dict[str, Any]] = {}
    stale_results: dict[str, dict[str, Any]] = {}
    owner_flights: dict[str, _CatalogFlight] = {}
    waiter_flights: dict[str, _CatalogFlight] = {}
    with _catalog_cache_lock:
        for cache_key in unique:
            flight = _catalog_flights.get(cache_key)
            if flight is None:
                flight = _CatalogFlight(threading.Event())
                _catalog_flights[cache_key] = flight
                owner_flights[cache_key] = flight
            else:
                waiter_flights[cache_key] = flight

    try:
        refresh_keys: list[str] = []
        for cache_key, flight in owner_flights.items():
            request = unique[cache_key]
            cached = _catalogCachedEntry(cache_key, request, now)
            if cached is not None:
                result, fresh = cached
                if fresh:
                    base_results[cache_key] = result
                    _completeCatalogFlight(flight, result)
                    continue
                if result["status"] in _CATALOG_POSITIVE_STATUSES:
                    stale_results[cache_key] = result
            refresh_keys.append(cache_key)

        monotonic_now = _catalogMonotonic()
        with _catalog_cache_lock:
            failures = {
                cache_key: _catalog_failures.get(cache_key)
                for cache_key in refresh_keys
            }
        owner_keys: list[str] = []
        for cache_key in refresh_keys:
            failure = failures[cache_key]
            if failure is None or failure[1] <= monotonic_now:
                owner_keys.append(cache_key)
                continue
            if cache_key in stale_results:
                outcome = dict(stale_results[cache_key])
                outcome["stale"] = True
            else:
                outcome = _temporaryCatalogResult(
                    unique[cache_key],
                    "provider_backoff",
                    math.ceil(failure[1] - monotonic_now),
                )
            base_results[cache_key] = outcome
            _completeCatalogFlight(owner_flights[cache_key], outcome)

        persistent_results: list[tuple[str, dict[str, Any]]] = []
        provider_outage: Optional[tuple[str, Any]] = None
        for offset in range(0, len(owner_keys), _CATALOG_MAX_BATCH):
            chunk_keys = owner_keys[offset : offset + _CATALOG_MAX_BATCH]
            chunk_requests = [unique[cache_key] for cache_key in chunk_keys]
            if provider_outage is not None:
                chunk_results = _temporaryCatalogResults(
                    chunk_requests, *provider_outage
                )
            else:
                request = chunk_requests[0]
                try:
                    with _catalog_provider_lock:
                        row = client.get_part_metadata(
                            str(request["requested_part_id"]), catalog=True
                        )
                except (HiveError, RequestsTimeout, RequestsConnectionError, RequestException) as exc:
                    status_code = getattr(exc, "status_code", None)
                    error_code = getattr(exc, "code", None)
                    retry_after_seconds = getattr(exc, "retry_after_seconds", None)
                    failure_kind = _catalogFailureKind(exc)
                    if (
                        isinstance(exc, HiveError)
                        and status_code == 404
                        and error_code == "part_not_found"
                    ):
                        chunk_results = [
                            _catalogResult(request, found=False, status="not_found")
                        ]
                        continue_outage = False
                    else:
                        chunk_results = _temporaryCatalogResults(
                            chunk_requests, failure_kind, retry_after_seconds
                        )
                        continue_outage = not (
                            isinstance(exc, HiveError)
                            and (
                                status_code == 404
                                or getattr(exc, "local_validation", False) is True
                            )
                        )
                    if continue_outage:
                        provider_outage = (failure_kind, retry_after_seconds)
                else:
                    image_url = row.get("img_url")
                    chunk_results = [
                        _catalogResult(
                            request,
                            canonical_part_id=request["requested_part_id"],
                            name=row.get("name"),
                            image_url=image_url,
                            image_match="exact" if image_url is not None else "none",
                            found=True,
                            status="resolved" if image_url is not None else "no_image",
                        )
                    ]

            for cache_key, result in zip(chunk_keys, chunk_results):
                if result["status"] in _CATALOG_CACHEABLE_STATUSES:
                    with _catalog_cache_lock:
                        _catalog_failures.pop(cache_key, None)
                else:
                    result["retry_after_seconds"] = _recordCatalogFailure(
                        cache_key, result.get("retry_after_seconds")
                    )

                if (
                    cache_key in stale_results
                    and result["status"] not in _CATALOG_POSITIVE_STATUSES
                ):
                    outcome = dict(stale_results[cache_key])
                    outcome["stale"] = True
                elif result["status"] in _CATALOG_CACHEABLE_STATUSES:
                    clean = _publishCatalogMemoryResult(cache_key, result)
                    outcome = clean
                    persistent_results.append((cache_key, clean))
                else:
                    outcome = result
                base_results[cache_key] = outcome
                _completeCatalogFlight(owner_flights[cache_key], outcome)

        for cache_key, clean in persistent_results:
            _persistCatalogResult(cache_key, clean)
    except Exception:
        for cache_key, flight in owner_flights.items():
            if not flight.completed.is_set():
                _completeCatalogFlight(
                    flight,
                    _temporaryCatalogResult(unique[cache_key], "owner_exception"),
                )
        raise
    finally:
        with _catalog_cache_lock:
            for cache_key, flight in owner_flights.items():
                if _catalog_flights.get(cache_key) is flight:
                    _catalog_flights.pop(cache_key)

    wait_deadline = _catalogMonotonic() + _CATALOG_WAITER_TIMEOUT_S
    for cache_key, flight in waiter_flights.items():
        remaining = max(0.0, wait_deadline - _catalogMonotonic())
        if not _waitCatalogFlight(flight, remaining):
            base_results[cache_key] = _temporaryCatalogResult(
                unique[cache_key], "flight_timeout"
            )
        else:
            assert flight.result is not None
            base_results[cache_key] = dict(flight.result)

    return [
        (
            _unsupportedCatalogResult(request)
            if cache_key is None
            else {**base_results[cache_key], **request}
        )
        for cache_key, request in zip(position_keys, normalized_requests)
    ]
