import unittest

from subsystems.feeder.pulse_perception.config import PulsePerceptionConfig
from subsystems.feeder.pulse_perception.stuck_watchdog import FeederStuckWatchdog
from subsystems.feeder.go_to_angle.config import GoToAngleConfig
from subsystems.feeder.incidents import FEEDER_JAM_INCIDENT_KIND


class _FakeRuntimeStats:
    def __init__(self) -> None:
        self._active = None
        self.auto_resolved: list[dict] = []

    def setActiveIncident(self, incident: dict) -> None:
        self._active = dict(incident)

    def activeIncident(self):
        return dict(self._active) if self._active else None

    def clearActiveIncident(self, *, kind=None, piece_uuid=None, resolved_by="system") -> None:
        if self._active is None:
            return
        if kind is not None and self._active.get("kind") != kind:
            return
        self._active = None

    def recordAutoResolvedIncident(self, incident: dict, *, resolved_by="auto") -> None:
        self.auto_resolved.append({**incident, "resolved_by": resolved_by})


class _FakeLogger:
    def info(self, *_a, **_k) -> None:
        pass

    def warning(self, *_a, **_k) -> None:
        pass


class _FakeGC:
    def __init__(self) -> None:
        self.runtime_stats = _FakeRuntimeStats()
        self.logger = _FakeLogger()


class _FakeStepper:
    def __init__(self) -> None:
        self.moves: list[float] = []
        self.speed_limits: list[tuple[int, int]] = []
        self.enabled = False

    def set_speed_limits(self, lo, hi) -> None:
        self.speed_limits.append((int(lo), int(hi)))

    def move_degrees(self, deg: float) -> bool:
        self.moves.append(float(deg))
        return True


def _cfg() -> PulsePerceptionConfig:
    cfg = PulsePerceptionConfig()
    cfg.stuck_watchdog_enabled = True
    cfg.stuck_no_progress_ms = 1000
    cfg.stuck_progress_epsilon_deg = 3.0
    cfg.stuck_nudge_output_deg = 4.0
    cfg.stuck_max_nudge_attempts = 3
    return cfg


class FeederStuckWatchdogTests(unittest.TestCase):
    def test_pause_preserves_elapsed_time_and_exhausted_recovery_budget(self):
        gc, up, cfg = _FakeGC(), _FakeStepper(), _cfg()
        cfg.stuck_max_nudge_attempts = 1
        wd = FeederStuckWatchdog(gc)
        self._observe(wd, gc, up, cfg, pos=40, wants=True, now=0)
        self._observe(wd, gc, up, cfg, pos=40, wants=True, now=2)
        wd.pause(2.5)
        wd.pause(100)  # Repeated hold is idempotent.
        self._observe(wd, gc, up, cfg, pos=40, wants=True, now=200)
        self.assertIsNone(gc.runtime_stats.activeIncident())
        wd.resume(202.5)
        wd.resume(202.6)  # Repeated resume cannot extend the recovery window.
        self._observe(wd, gc, up, cfg, pos=40, wants=True, now=202.75)
        self.assertIsNone(gc.runtime_stats.activeIncident())
        self._observe(wd, gc, up, cfg, pos=40, wants=True, now=203.1)
        self.assertEqual(len(up.moves), 1)
        self.assertEqual(gc.runtime_stats.activeIncident()["nudge_attempts"], 1)

    def test_auto_resolved_duration_excludes_paused_time(self):
        gc, up, cfg = _FakeGC(), _FakeStepper(), _cfg()
        wd = FeederStuckWatchdog(gc)
        self._observe(wd, gc, up, cfg, pos=40, wants=True, now=0)
        self._observe(wd, gc, up, cfg, pos=40, wants=True, now=2)
        wd.pause(2.5)
        wd.resume(202.5)
        self._observe(wd, gc, up, cfg, pos=30, wants=True, now=203)
        row = gc.runtime_stats.auto_resolved[0]
        self.assertAlmostEqual(row["no_progress_ms"], 3000)
        self.assertEqual(row["nudge_attempts"], 1)

    def test_pause_does_not_clear_an_existing_operator_incident(self):
        gc, up, cfg = _FakeGC(), _FakeStepper(), _cfg()
        cfg.stuck_max_nudge_attempts = 0
        wd = FeederStuckWatchdog(gc)
        self._observe(wd, gc, up, cfg, pos=40, wants=True, now=0)
        self._observe(wd, gc, up, cfg, pos=40, wants=True, now=2)
        incident = gc.runtime_stats.activeIncident()
        wd.pause(2.5)
        wd.resume(202.5)
        self._observe(wd, gc, up, cfg, pos=40, wants=True, now=203)
        self.assertEqual(gc.runtime_stats.activeIncident(), incident)
        self.assertEqual(up.moves, [])

    def test_busy_owner_defers_without_spending_recovery_attempt(self):
        gc, up, cfg = _FakeGC(), _FakeStepper(), _cfg()
        results = iter([None, True])
        wd = FeederStuckWatchdog(gc, request_nudge=lambda *_: next(results))
        self._observe(wd, gc, up, cfg, pos=40.0, wants=True, now=0.0)
        self._observe(wd, gc, up, cfg, pos=40.0, wants=True, now=2.0)
        self.assertEqual(wd._trackers[2].nudge_attempts, 0)
        self._observe(wd, gc, up, cfg, pos=40.0, wants=True, now=2.1)
        self.assertEqual(wd._trackers[2].nudge_attempts, 1)
        self.assertEqual(up.moves, [])

    def _observe(self, wd, gc, up, cfg, *, pos, wants, now, upstream_enabled=True, track_id=None):
        wd.observe(
            channel_id=2,
            channel_label="C2",
            upstream_label="C1",
            upstream_channel_id=1,
            upstream_stepper=up,
            upstream_enabled=upstream_enabled,
            leading_pos_deg=pos,
            leading_track_id=track_id,
            wants_advance=wants,
            cfg=cfg,
            now=now,
        )

    def test_identity_changes_and_missing_ids_cannot_renew_a_real_stall(self):
        gc, up, cfg = _FakeGC(), _FakeStepper(), _cfg()
        wd = FeederStuckWatchdog(gc)
        self._observe(wd, gc, up, cfg, pos=40, wants=True, now=0, track_id=11)
        for at, track in [(2, 12), (4, None), (6, 13), (8, 14)]:
            self._observe(wd, gc, up, cfg, pos=40, wants=True, now=at, track_id=track)
        self.assertEqual(len(up.moves), 3)
        self.assertEqual(gc.runtime_stats.activeIncident()["nudge_attempts"], 3)
        incident = gc.runtime_stats.activeIncident()
        self._observe(wd, gc, up, cfg, pos=40, wants=True, now=10, track_id=15)
        self.assertEqual(gc.runtime_stats.activeIncident(), incident)

    def test_successor_rebases_position_but_keeps_budget_until_actual_progress(self):
        gc, up, cfg = _FakeGC(), _FakeStepper(), _cfg()
        wd = FeederStuckWatchdog(gc)
        self._observe(wd, gc, up, cfg, pos=1, wants=True, now=0, track_id=11)
        self._observe(wd, gc, up, cfg, pos=1, wants=True, now=2, track_id=11)
        tracker = wd._trackers[2]
        self._observe(wd, gc, up, cfg, pos=100, wants=True, now=2.5, track_id=13)
        self.assertEqual((tracker.last_progress_at, tracker.nudge_attempts), (2, 1))
        self.assertEqual(tracker.stall_started_at, 0)
        self.assertEqual(gc.runtime_stats.auto_resolved, [])
        self._observe(wd, gc, up, cfg, pos=95, wants=True, now=2.75, track_id=13)
        self.assertEqual(tracker.nudge_attempts, 0)
        self.assertEqual(tracker.last_progress_at, 2.75)
        self.assertEqual(len(up.moves), 1)
        self.assertEqual(gc.runtime_stats.auto_resolved[0]["nudge_attempts"], 1)

    def test_missing_identity_does_not_replace_known_reference(self):
        gc, up, cfg = _FakeGC(), _FakeStepper(), _cfg()
        wd = FeederStuckWatchdog(gc)
        self._observe(wd, gc, up, cfg, pos=40, wants=True, now=0, track_id=11)
        self._observe(wd, gc, up, cfg, pos=38, wants=True, now=0.5, track_id=None)
        self.assertEqual(wd._trackers[2].track_positions[11], 40)
        self._observe(wd, gc, up, cfg, pos=36, wants=True, now=0.75, track_id=11)
        self.assertEqual(wd._trackers[2].last_progress_at, 0.75)
        self.assertEqual(up.moves, [])

    def test_alternating_moving_leaders_accumulate_small_progress(self):
        gc, up, cfg = _FakeGC(), _FakeStepper(), _cfg()
        wd = FeederStuckWatchdog(gc)
        for tick in range(31):
            self._observe(wd, gc, up, cfg, pos=60-tick, wants=True,
                          now=tick*0.2, track_id=1+tick%2)
        self.assertEqual(up.moves, [])
        self.assertIsNone(gc.runtime_stats.activeIncident())

    def test_alternating_stationary_leaders_do_not_manufacture_progress(self):
        gc, up, cfg = _FakeGC(), _FakeStepper(), _cfg()
        wd = FeederStuckWatchdog(gc)
        for tick in range(51):
            self._observe(wd, gc, up, cfg, pos=60 if tick%2 else 30,
                          wants=True, now=tick*0.2, track_id=1+tick%2)
        self.assertEqual(len(up.moves), 3)
        self.assertIsNotNone(gc.runtime_stats.activeIncident())
        self.assertEqual(gc.runtime_stats.auto_resolved, [])

    def test_new_identity_cannot_clear_incident_by_appearing_closer(self):
        gc, up, cfg = _FakeGC(), _FakeStepper(), _cfg()
        cfg.stuck_max_nudge_attempts = 0
        wd = FeederStuckWatchdog(gc)
        self._observe(wd, gc, up, cfg, pos=60, wants=True, now=0, track_id=1)
        self._observe(wd, gc, up, cfg, pos=60, wants=True, now=2, track_id=1)
        incident = gc.runtime_stats.activeIncident()
        self._observe(wd, gc, up, cfg, pos=30, wants=True, now=3, track_id=2)
        self.assertEqual(gc.runtime_stats.activeIncident(), incident)
        self._observe(wd, gc, up, cfg, pos=25, wants=True, now=4, track_id=2)
        self.assertIsNone(gc.runtime_stats.activeIncident())

    def test_missing_identity_does_not_compare_different_known_leaders(self):
        gc, up, cfg = _FakeGC(), _FakeStepper(), _cfg()
        wd = FeederStuckWatchdog(gc)
        for tick in range(51):
            self._observe(wd, gc, up, cfg, pos=60 if tick%2 else 30,
                          wants=True, now=tick*0.2, track_id=1 if tick%2 else None)
        self.assertEqual(len(up.moves), 3)
        self.assertIsNotNone(gc.runtime_stats.activeIncident())

    def test_identity_churn_is_bounded_without_postponing_escalation(self):
        gc, up, cfg = _FakeGC(), _FakeStepper(), _cfg()
        wd = FeederStuckWatchdog(gc)
        for tick in range(100):
            self._observe(wd, gc, up, cfg, pos=40, wants=True,
                          now=tick*0.2, track_id=tick)
            self.assertLessEqual(len(wd._trackers[2].track_positions), 32)
        self.assertEqual(len(up.moves), 3)
        self.assertIsNotNone(gc.runtime_stats.activeIncident())

    def test_previously_credited_motion_cannot_be_reused_after_leader_switch(self):
        gc, up, cfg = _FakeGC(), _FakeStepper(), _cfg()
        wd = FeederStuckWatchdog(gc)
        for at, track, pos in [(0, 1, 60), (0.1, 2, 50), (0.2, 1, 55)]:
            self._observe(wd, gc, up, cfg, pos=pos, wants=True, now=at, track_id=track)
        # The second piece's old 50-degree reference predates credited motion.
        # Its first observation in the new window cannot extend that window.
        self._observe(wd, gc, up, cfg, pos=45, wants=True, now=0.9, track_id=2)
        self._observe(wd, gc, up, cfg, pos=45, wants=True, now=1.3, track_id=2)
        self.assertEqual(len(up.moves), 1)

    def test_nudges_upstream_then_escalates_to_jam(self) -> None:
        gc = _FakeGC()
        up = _FakeStepper()
        wd = FeederStuckWatchdog(gc)
        cfg = _cfg()

        # Piece parked at the same position while the channel keeps wanting to
        # advance it: three nudges, then a jam incident.
        self._observe(wd, gc, up, cfg, pos=40.0, wants=True, now=0.0)
        for i in range(3):
            # Cross the no-progress window each round; each should nudge once.
            self._observe(wd, gc, up, cfg, pos=40.0, wants=True, now=(i + 1) * 2.0)
            self.assertEqual(len(up.moves), i + 1, f"expected {i + 1} nudges")
            self.assertIsNone(gc.runtime_stats.activeIncident())

        # Fourth stall window: nudges exhausted -> operator jam incident.
        self._observe(wd, gc, up, cfg, pos=40.0, wants=True, now=8.0)
        self.assertEqual(len(up.moves), 3, "no fourth nudge")
        active = gc.runtime_stats.activeIncident()
        self.assertIsNotNone(active)
        self.assertEqual(active["kind"], FEEDER_JAM_INCIDENT_KIND)
        self.assertEqual(active["channel_label"], "C2")

    def test_forward_progress_resets_and_never_nudges(self) -> None:
        gc = _FakeGC()
        up = _FakeStepper()
        wd = FeederStuckWatchdog(gc)
        cfg = _cfg()

        pos = 60.0
        for i in range(10):
            pos -= 5.0  # advancing toward the exit each tick (> epsilon)
            self._observe(wd, gc, up, cfg, pos=pos, wants=True, now=float(i) * 2.0)
        self.assertEqual(up.moves, [], "moving piece must never be nudged")
        self.assertIsNone(gc.runtime_stats.activeIncident())

    def test_go_to_angle_config_uses_shared_move_speed_for_nudge(self) -> None:
        gc = _FakeGC()
        up = _FakeStepper()
        wd = FeederStuckWatchdog(gc)
        cfg = GoToAngleConfig(
            move_speed_usteps_per_s=1997,
            stuck_no_progress_ms=1000,
        )

        self._observe(wd, gc, up, cfg, pos=40.0, wants=True, now=0.0)
        self._observe(wd, gc, up, cfg, pos=40.0, wants=True, now=2.0)

        self.assertEqual(up.speed_limits, [(16, 1997)])
        self.assertEqual(len(up.moves), 1)

    def test_holding_for_downstream_does_not_count_as_stuck(self) -> None:
        gc = _FakeGC()
        up = _FakeStepper()
        wd = FeederStuckWatchdog(gc)
        cfg = _cfg()

        # Piece present but intentionally held (wants_advance False) for a long
        # time: the clock is paused, so no nudge and no incident.
        self._observe(wd, gc, up, cfg, pos=40.0, wants=False, now=0.0)
        self._observe(wd, gc, up, cfg, pos=40.0, wants=False, now=100.0)
        self.assertEqual(up.moves, [])
        self.assertIsNone(gc.runtime_stats.activeIncident())

    def test_piece_leaving_resets_attempts(self) -> None:
        gc = _FakeGC()
        up = _FakeStepper()
        wd = FeederStuckWatchdog(gc)
        cfg = _cfg()

        self._observe(wd, gc, up, cfg, pos=40.0, wants=True, now=0.0)
        self._observe(wd, gc, up, cfg, pos=40.0, wants=True, now=2.0)
        self.assertEqual(len(up.moves), 1)
        # Channel clears (no piece) -> tracker reset.
        self._observe(wd, gc, up, cfg, pos=None, wants=False, now=3.0)
        # A brand-new stall starts its own attempt budget.
        self._observe(wd, gc, up, cfg, pos=50.0, wants=True, now=4.0)
        self._observe(wd, gc, up, cfg, pos=50.0, wants=True, now=6.0)
        self.assertEqual(len(up.moves), 2)
        self.assertIsNone(gc.runtime_stats.activeIncident())

    def test_nudge_that_frees_piece_is_recorded_as_auto_resolved(self) -> None:
        gc = _FakeGC()
        up = _FakeStepper()
        wd = FeederStuckWatchdog(gc)
        cfg = _cfg()

        # Stall, nudge once, then the piece advances (the nudge freed it).
        self._observe(wd, gc, up, cfg, pos=40.0, wants=True, now=0.0)
        self._observe(wd, gc, up, cfg, pos=40.0, wants=True, now=2.0)
        self.assertEqual(len(up.moves), 1)
        self._observe(wd, gc, up, cfg, pos=30.0, wants=True, now=3.0)

        # Never escalated to an operator hold, but the freed jam is logged.
        self.assertIsNone(gc.runtime_stats.activeIncident())
        recorded = gc.runtime_stats.auto_resolved
        self.assertEqual(len(recorded), 1)
        row = recorded[0]
        self.assertEqual(row["kind"], FEEDER_JAM_INCIDENT_KIND)
        self.assertEqual(row["status"], "auto_resolved")
        self.assertEqual(row["channel_label"], "C2")
        self.assertEqual(row["nudge_attempts"], 1)
        self.assertEqual(row["resolved_by"], "auto")
        self.assertGreater(row["resolved_at"], row["triggered_at"])

    def test_escalated_jam_is_not_double_logged_as_auto_resolved(self) -> None:
        gc = _FakeGC()
        up = _FakeStepper()
        wd = FeederStuckWatchdog(gc)
        cfg = _cfg()

        # Drive to an operator jam, then the operator frees it (piece advances).
        self._observe(wd, gc, up, cfg, pos=40.0, wants=True, now=0.0)
        for t in (2.0, 4.0, 6.0, 8.0):
            self._observe(wd, gc, up, cfg, pos=40.0, wants=True, now=t)
        self.assertEqual(gc.runtime_stats.activeIncident()["kind"], FEEDER_JAM_INCIDENT_KIND)
        self._observe(wd, gc, up, cfg, pos=30.0, wants=True, now=10.0)

        # The active-slot clear path owns that resolution; no auto-resolved row.
        self.assertIsNone(gc.runtime_stats.activeIncident())
        self.assertEqual(gc.runtime_stats.auto_resolved, [])

    def test_disabled_watchdog_does_nothing(self) -> None:
        gc = _FakeGC()
        up = _FakeStepper()
        wd = FeederStuckWatchdog(gc)
        cfg = _cfg()
        cfg.stuck_watchdog_enabled = False

        self._observe(wd, gc, up, cfg, pos=40.0, wants=True, now=0.0)
        self._observe(wd, gc, up, cfg, pos=40.0, wants=True, now=100.0)
        self.assertEqual(up.moves, [])
        self.assertIsNone(gc.runtime_stats.activeIncident())

    def test_disabled_upstream_does_not_nudge_or_raise(self) -> None:
        gc = _FakeGC()
        up = _FakeStepper()
        wd = FeederStuckWatchdog(gc)
        cfg = _cfg()

        self._observe(
            wd, gc, up, cfg, pos=40.0, wants=True, now=0.0, upstream_enabled=False
        )
        self._observe(
            wd, gc, up, cfg, pos=40.0, wants=True, now=100.0, upstream_enabled=False
        )
        self.assertEqual(up.moves, [])
        self.assertIsNone(gc.runtime_stats.activeIncident())

    def test_active_jam_clears_when_piece_advances(self) -> None:
        gc = _FakeGC()
        up = _FakeStepper()
        wd = FeederStuckWatchdog(gc)
        cfg = _cfg()

        # Drive to a jam.
        self._observe(wd, gc, up, cfg, pos=40.0, wants=True, now=0.0)
        for t in (2.0, 4.0, 6.0, 8.0):
            self._observe(wd, gc, up, cfg, pos=40.0, wants=True, now=t)
        self.assertEqual(gc.runtime_stats.activeIncident()["kind"], FEEDER_JAM_INCIDENT_KIND)

        # Operator frees it; the piece now advances -> incident auto-clears.
        self._observe(wd, gc, up, cfg, pos=30.0, wants=True, now=10.0)
        self.assertIsNone(gc.runtime_stats.activeIncident())


if __name__ == "__main__":
    unittest.main()
