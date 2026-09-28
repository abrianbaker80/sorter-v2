from subsystems import (
    SharedVariables,
)
from irl.config import IRLInterface, IRLConfig
from global_config import GlobalConfig
from runtime_variables import RuntimeVariables
from vision import VisionManager
from sorting_profile import mkSortingProfile
import queue
import time
from machine_setup import get_machine_setup_definition
from machine_runtime import build_machine_runtime
from subsystems.bus import TickBus


class Coordinator:
    def __init__(
        self,
        irl: IRLInterface,
        irl_config: IRLConfig,
        gc: GlobalConfig,
        vision: VisionManager,
        event_queue: queue.Queue,
        rv: RuntimeVariables,
    ):
        self.irl = irl
        self.irl_config = irl_config
        self.gc = gc
        self.logger = gc.logger
        self.vision = vision
        self.event_queue = event_queue
        self.bus = TickBus()
        self.gc.runtime_stats.setBusProvider(self.bus)
        self.shared = SharedVariables(gc=gc, bus=self.bus)
        self.feeding_mode = getattr(irl_config, "feeding_mode", "auto_channels")
        self.machine_setup = getattr(
            irl_config,
            "machine_setup",
            get_machine_setup_definition(None),
        )
        self.machine_runtime = build_machine_runtime(self.machine_setup.key)
        self.manual_feed_mode = self.machine_setup.manual_feed_mode
        self.gc.use_channel_bus = bool(
            getattr(self.gc, "use_channel_bus", False)
            or getattr(self.machine_setup, "uses_classification_channel", False)
        )
        self.sorting_profile = mkSortingProfile(gc)
        self._sync_set_progress_tracker()

        self.distribution_layout = irl.distribution_layout

        self.transport = self.machine_runtime.create_transport(
            gc=gc,
            event_queue=event_queue,
        )
        self.shared.transport = self.transport
        self.shared.carousel = (
            self.transport if hasattr(self.transport, "rotate") else None
        )

        self.distribution = self.machine_runtime.create_distribution(
            irl=irl,
            irl_config=irl_config,
            gc=gc,
            shared=self.shared,
            sorting_profile=self.sorting_profile,
            distribution_layout=self.distribution_layout,
            event_queue=event_queue,
            vision=vision,
        )
        self.classification = self.machine_runtime.create_classification(
            irl=irl,
            irl_config=irl_config,
            gc=gc,
            shared=self.shared,
            vision=vision,
            event_queue=event_queue,
            transport=self.transport,
        )
        self.feeder = self.machine_runtime.create_feeder(
            irl=irl,
            irl_config=irl_config,
            gc=gc,
            shared=self.shared,
            vision=vision,
        )
        if self.manual_feed_mode:
            self.logger.info(
                "Coordinator: manual carousel feed mode enabled; automatic C-channel feeding is disabled."
            )
        elif not self.machine_setup.runtime_supported:
            self.logger.warning(
                "Coordinator: machine setup %r is persisted, but runtime orchestration "
                "is not implemented yet."
                % self.machine_setup.key
            )

    def _sync_set_progress_tracker(self) -> None:
        existing_tracker = getattr(self.gc, "set_progress_tracker", None)
        if existing_tracker is not None:
            existing_tracker.save()

        self.gc.set_progress_tracker = None
        if self.sorting_profile.is_set_based and self.sorting_profile.set_inventories:
            from set_progress import SetProgressTracker

            self.gc.set_progress_tracker = SetProgressTracker(
                self.sorting_profile.set_inventories,
                self.sorting_profile.artifact_hash,
            )

        try:
            from server.set_progress_sync import getSetProgressSyncWorker

            getSetProgressSyncWorker().notify()
        except Exception:
            pass

    def reload_sorting_profile(self) -> None:
        self.sorting_profile.reload()
        self._sync_set_progress_tracker()

    def _active_incident(self) -> dict | None:
        runtime_stats = getattr(self.gc, "runtime_stats", None)
        if runtime_stats is None or not hasattr(runtime_stats, "activeIncident"):
            return None
        try:
            incident = runtime_stats.activeIncident()
        except Exception:
            return None
        return incident if isinstance(incident, dict) else None

    def _hold_process_for_incident(self, incident: dict) -> None:
        kind = str(incident.get("kind") or "active_incident")
        reason = f"incident:{kind}"
        self.shared.set_classification_gate(False, reason=reason)
        self.shared.set_distribution_gate(False, reason=reason)
        if hasattr(self.gc, "runtime_stats"):
            self.gc.runtime_stats.observeBlockedReason("coordinator", "active_incident")

    def _classification_should_step_during_incident(self, incident: dict) -> bool:
        """The C4 stall watchdog keeps ticking during its own incident: its
        step is what notices the channel going clear and auto-resolves it.
        Other incidents are process-level holds: classification must not
        reopen the C3->C4 gate after the coordinator deliberately closed it."""
        return incident.get("source_kind") == "c4_stall_watchdog"

    def step(self) -> None:
        prof = self.gc.profiler
        prof.hit("coordinator.step.calls")
        prof.mark("coordinator.step.interval_ms")
        # Thread-attribution: name the thread the coordinator is running on so
        # we can prove it never does inference work itself. Cross-reference
        # with inference.by_thread.* counters in vision_manager._runHiveDetection.
        import threading as _th
        _t = _th.current_thread().name
        _safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in _t) or "unknown"
        prof.hit(f"coordinator.step.by_thread.{_safe}")

        # GIL-stall detector: wall-clock vs CPU time. A large gap means the
        # main thread spent its tick blocked on the GIL while another thread
        # held it (typically AnyIO worker threads running YOLO + image work).
        _coord_cpu_t0 = time.process_time()
        with prof.timer("coordinator.step.total_ms"):
            coordinator_started = time.perf_counter()
            self.bus.begin_tick()
            active_incident = self._active_incident()
            if active_incident is not None:
                self._hold_process_for_incident(active_incident)
                prof.hit("coordinator.step.distribution_skipped.active_incident")
                prof.hit("coordinator.step.feeder_skipped.active_incident")
                if self._classification_should_step_during_incident(active_incident):
                    with prof.timer("coordinator.step.classification_ms"):
                        classification_started = time.perf_counter()
                        self.classification.step()
                        self.gc.runtime_stats.observePerfMs(
                            "coordinator.step.classification_ms",
                            (time.perf_counter() - classification_started) * 1000.0,
                        )
                else:
                    prof.hit("coordinator.step.classification_skipped.active_incident")
                self.gc.runtime_stats.observePerfMs(
                    "coordinator.step.total_ms",
                    (time.perf_counter() - coordinator_started) * 1000.0,
                )
                return
            with prof.timer("coordinator.step.distribution_ms"):
                distribution_started = time.perf_counter()
                self.distribution.step()
                self.gc.runtime_stats.observePerfMs(
                    "coordinator.step.distribution_ms",
                    (time.perf_counter() - distribution_started) * 1000.0,
                )
            with prof.timer("coordinator.step.classification_ms"):
                classification_started = time.perf_counter()
                self.classification.step()
                self.gc.runtime_stats.observePerfMs(
                    "coordinator.step.classification_ms",
                    (time.perf_counter() - classification_started) * 1000.0,
                )
            with prof.timer("coordinator.step.feeder_ms"):
                feeder_started = time.perf_counter()
                if self.manual_feed_mode:
                    prof.hit("coordinator.step.feeder_skipped.manual_feed_mode")
                else:
                    self.feeder.step()
                self.gc.runtime_stats.observePerfMs(
                    "coordinator.step.feeder_ms",
                    (time.perf_counter() - feeder_started) * 1000.0,
                )
            _coord_wall_ms = (time.perf_counter() - coordinator_started) * 1000.0
            _coord_cpu_ms = (time.process_time() - _coord_cpu_t0) * 1000.0
            self.gc.runtime_stats.observePerfMs(
                "coordinator.step.total_ms",
                _coord_wall_ms,
            )
            self.gc.runtime_stats.observePerfMs(
                "coordinator.step.cpu_ms",
                _coord_cpu_ms,
            )
            self.gc.runtime_stats.observePerfMs(
                "coordinator.step.gil_stall_ms",
                max(0.0, _coord_wall_ms - _coord_cpu_ms),
            )

    def cleanup(self) -> None:
        self.feeder.cleanup()
        self.classification.cleanup()
        self.distribution.cleanup()
