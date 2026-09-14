"""Opt-in C4 intake bridge. Construction never activates a production runtime.

The FIFO owner calls confirmed_deposit with the paired perception snapshot used
to confirm a stationary C3->P6 arrival, then calls poll before planner.plan,
passing its returned deadlines. Workers own only copied crops and mailboxes;
only the caller thread can mutate the FIFO. No provider wait occurs on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from threading import BoundedSemaphore, Lock, Thread, get_ident
from typing import Callable, Sequence

import numpy as np

from perception.capture import PerceptionFrame
from perception.channel_crop_capture import _cropBbox
from perception.state import PieceObservation
from .demand_planner import RouteDeadline
from .physical_fifo import PhysicalC4FIFO, Pocket, PocketState


@dataclass(frozen=True)
class RoutingKey:
    # Object identity segregates callbacks even if a replacement FIFO restarts
    # pocket generations at one. This key is process-local, never persisted.
    epoch: object
    pocket_id: int
    generation: int


@dataclass
class _Request:
    deadline: RouteDeadline
    completed: bool = False
    destination: str | None = None


class C4IntakeRoutingBridge:
    def __init__(
        self, fifo: PhysicalC4FIFO, *, recognize: Callable[[np.ndarray], str | None],
        routing_timeout_s: float, max_workers: int = 4, crop_padding_px: int = 0,
    ) -> None:
        if not isfinite(routing_timeout_s) or routing_timeout_s <= 0:
            raise ValueError("routing timeout must be finite and positive")
        if type(max_workers) is not int or not 1 <= max_workers <= 10:
            raise ValueError("worker bound must be in [1, 10]")
        if type(crop_padding_px) is not int or crop_padding_px < 0:
            raise ValueError("crop padding must be nonnegative")
        self.fifo = fifo
        self._recognize = recognize
        self._timeout = routing_timeout_s
        self._padding = crop_padding_px
        self._capacity = BoundedSemaphore(max_workers)
        self._owner = get_ident()
        self._epoch = object()
        self._lock = Lock()
        self._requests: dict[RoutingKey, _Request] = {}
        self._closed = False
        self._last_boundary = -1
        self._last_frame_ts = float("-inf")
        self._last_now = 0.0

    def _writer(self, now: float) -> None:
        if get_ident() != self._owner:
            raise RuntimeError("FIFO bridge requires its single owner thread")
        if not isfinite(now) or now < self._last_now:
            raise ValueError("now must be finite, nonnegative and monotonic")
        self._last_now = now

    def key_for(self, pocket: Pocket) -> RoutingKey:
        return RoutingKey(self._epoch, pocket.pocket_id, pocket.generation)

    def confirmed_deposit(
        self, *, boundary: int, now: float,
        sample: tuple[Sequence[PieceObservation], PerceptionFrame] | None,
    ) -> Pocket | None:
        """Record one confirmed physical deposit, regardless of usable imagery.

        Caller establishes physical arrival, stationary boundary, and freshness
        of this exact paired C4 sample. This method does not infer a transfer
        from a detection. DROP (zone_code=1) must be calibrated to physical P6.
        Other pockets in the frame are excluded. Missing/ambiguous imagery or
        multiple P6 detections records the load as DISCARD. Replayed boundary
        confirmations are no-ops; unknown/wrong boundaries are rejected.

        Frame timestamps are camera timestamps; now/deadlines use the planner's
        monotonic clock. Never compare the two clock domains.
        """
        self._writer(now)
        if self._closed:
            raise RuntimeError("bridge is closed")
        if type(boundary) is not int or boundary < 0:
            raise ValueError("boundary must be a nonnegative integer")
        if boundary <= self._last_boundary:
            return None
        if boundary != self.fifo.boundary:
            raise ValueError("confirmation does not match current rotor boundary")
        deadline_at = now + self._timeout
        if not isfinite(deadline_at):
            raise ValueError("routing deadline overflow")
        pocket = self.fifo.deposit()
        self._last_boundary = boundary
        key = self.key_for(pocket)
        # Prune completed/discharged generations even if the caller has been
        # advancing the pure FIFO directly in an integration harness.
        self.poll(now)
        crop = None
        try:
            if sample is not None:
                pieces, frame = sample
                if (frame.source_id == "carousel" and isfinite(frame.timestamp)
                        and frame.timestamp > self._last_frame_ts):
                    self._last_frame_ts = frame.timestamp
                    intake = [p for p in pieces if p.zone_code == 1]
                    if len(intake) == 1:
                        h, w = frame.bgr.shape[:2]
                        crop = _cropBbox(frame.bgr, intake[0].bbox, self._padding, w, h)
        except Exception:
            # Physical arrival remains recorded when capture data is unusable.
            crop = None
        if crop is None or crop.size == 0 or not self._capacity.acquire(blocking=False):
            self.fifo.resolve(pocket.pocket_id, pocket.generation, PocketState.DISCARD)
            return self.fifo.pockets[pocket.pocket_id]
        with self._lock:
            self._requests[key] = _Request(RouteDeadline(pocket.pocket_id, pocket.generation, deadline_at))

        def run() -> None:
            try:
                destination = self._recognize(crop)
                self.post_result(key, destination)
            except Exception:
                self.post_result(key, None)
            finally:
                self._capacity.release()

        try:
            # No work queue: stuck providers consume a bounded slot until they
            # actually return. Expiry must not free it and spawn unlimited work.
            Thread(target=run, daemon=True, name="c4-pocket-routing").start()
        except Exception:
            self._capacity.release()
            self.post_result(key, None)
        return pocket

    def post_result(self, key: RoutingKey, destination: str | None) -> bool:
        """Thread-safe, once-only mailbox delivery; never touches the FIFO."""
        with self._lock:
            request = self._requests.get(key)
            if self._closed or request is None or request.completed:
                return False
            request.destination = destination if isinstance(destination, str) and destination.strip() else None
            request.completed = True
            return True

    def poll(self, now: float) -> tuple[RouteDeadline, ...]:
        """Apply timely results and return deadlines for Slice 2 planner.plan.

        At/after expiry a queued success cannot win. The planner alone turns
        still-pending deadlines into DISCARD, including its P0 final deadline.
        Calling this never waits for recognition, cancellation or thread exit.
        """
        self._writer(now)
        deadlines = []
        with self._lock:
            for key, request in list(self._requests.items()):
                pocket = self.fifo.pockets[key.pocket_id]
                if pocket.generation != key.generation or pocket.state is not PocketState.PENDING:
                    del self._requests[key]
                    continue
                if now < request.deadline.at and request.completed:
                    state = PocketState.ROUTED if request.destination is not None else PocketState.DISCARD
                    self.fifo.resolve(key.pocket_id, key.generation, state, destination=request.destination)
                    del self._requests[key]
                else:
                    deadlines.append(request.deadline)
        return tuple(deadlines)

    def close(self, now: float) -> None:
        """Retire this bridge on its owner thread without joining providers."""
        self._writer(now)
        with self._lock:
            self._closed = True
            for key in self._requests:
                self.fifo.resolve(key.pocket_id, key.generation, PocketState.DISCARD)
            self._requests.clear()
