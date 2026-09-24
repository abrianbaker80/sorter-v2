"""C3 admission adapter around the existing observed-progress recovery policy."""

import time
from dataclasses import asdict

from defs.known_object import UNVERIFIED_C4_HANDOFF
from .c3_transfer_recovery import C3TransferRecovery


class PhysicalHandoff(C3TransferRecovery):
    def __init__(self, controller, binding):
        self.controller = controller
        self.binding = binding
        self._episode = binding.episode
        self.shared, self.gc = controller.shared, controller.gc
        self.irl, self.logger = controller.irl, controller.gc.logger
        self._config = controller.config
        self._arrival_armed_at_mono = self._episode.started_at_mono
        self._arrival_armed_at_wall = self._episode.started_at_wall
        self._arrival_last_frame_ts = 0.0
        self._arrival_presence_streak = self._arrival_empty_streak = 0
        self._arrival_samples = []
        self._arrival_timed_out = False
        self._confirmed_recovery_samples = None

    def noteProgress(self):
        self.controller.noteProgress()

    def _observeEpisode(self):
        self.gc.runtime_stats.observeTransferEpisode(asdict(self._episode))

    def _recordRecoveryDecision(self):
        self._episode.recovery_decision["command_history"] = list(
            self._episode.recovery_legs
        )
        self._observeEpisode()

    def _unresolvedTransfer(self, reason):
        # The reusable C3 policy reaches this only for ownership/motor faults
        # or positive original-piece retention with exhausted safe recovery.
        # Software arrival uncertainty is finalized below, without incidents.
        self._episode.state = "unresolved"
        self._observeEpisode()
        raise RuntimeError(reason)

    def _clearArrivalArm(self):
        self._arrival_presence_streak = self._arrival_empty_streak = 0
        self._arrival_samples = []
        self._confirmed_recovery_samples = None

    def _admit(self, now, *, unverified=False):
        self.controller.finish_handoff(self.binding, now, arrived=not unverified)

    def _discardUnverifiedHandoff(self, now, boundary, observed):
        ep = self._episode
        predicates = observed["predicates"]
        # Camera freshness is an image/recovery-motion condition, not custody.
        # An expired unlocated handoff retains its potentially occupied pocket.
        if (
            ep.recovery_arrival
            or self._confirmed_recovery_samples
            or (
                predicates.get("c3_frame_fresh")
                and self._retainedPieceVisible(observed)
            )
            or not predicates.get("motor_c3_resolved")
            or not predicates.get("motor_c2_resolved")
            or getattr(self.shared, "c3_motion_pending", False)
        ):
            return False
        if not all(
            boundary["predicates"][k]
            for k in ("reserved_boundary", "c4_available", "c4_stopped_aligned")
        ):
            raise RuntimeError("corrupt physical intake reservation")
        ep.forced_reject_reason = UNVERIFIED_C4_HANDOFF
        ep.recovery_decision["final_outcome"] = "unverified_discard_bound"
        self._admit(now, unverified=True)
        self._recordRecoveryDecision()
        return True

    def tick(self, now):
        read = getattr(self.controller.perception, "read_pieces_and_frame", None)
        sample = read(4) if callable(read) else None
        pieces, frame = sample if sample else ((), None)
        ts = getattr(frame, "timestamp", 0.0)
        fresh = (
            ts > self._arrival_last_frame_ts
            and ts > self._arrival_armed_at_wall
            and 0 <= time.time() - ts <= 1.5
        )
        drops = [p for p in pieces if p.zone_code == 1]
        if fresh:
            self._arrival_last_frame_ts = ts
            self._arrival_presence_streak = (
                self._arrival_presence_streak + 1 if drops else 0
            )
            self._arrival_empty_streak = (
                self._arrival_empty_streak + 1 if not drops else 0
            )
            if len(drops) > 1:
                self._episode.group_size_unknown = True
        boundary = self.controller.recovery_boundary(self.binding, ts)
        request = self.shared.request_c3_recovery
        if not callable(request):
            raise RuntimeError("C3 recovery owner is unavailable")
        observed = request(self._episode, boundary)
        if self._arrival_presence_streak >= max(
            1, self._config.presence_streak_to_start
        ):
            self._episode.recovery_arrival = True
        if (
            self._episode.recovery_arrival
            and observed["predicates"]["motor_c3_resolved"]
        ):
            # A positive original C3 piece cannot be replaced by an unrelated
            # intake detection. Retain the original reservation and recovery.
            if not self._retainedPieceVisible(observed):
                self._admit(now)
                return
            self._episode.recovery_arrival = False
        if (
            now - self._arrival_armed_at_mono >= 3
            or self._episode.recovery_started_mono is not None
        ):
            self._advanceRecovery(now, boundary, observed)
