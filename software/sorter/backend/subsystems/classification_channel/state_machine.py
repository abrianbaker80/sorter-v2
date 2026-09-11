import threading
import time

from global_config import GlobalConfig
from irl.config import ClassificationChannelMode, IRLConfig, IRLInterface
from piece_transport import ClassificationChannelTransport
from subsystems.base_subsystem import BaseSubsystem
from subsystems.classification_channel.detecting import Detecting
from subsystems.classification_channel.ejecting import Ejecting
from subsystems.classification_channel.idle import Idle
from subsystems.classification_channel.incidents import (
    C4_EXIT_STUCK_INCIDENT_KIND,
    c4_stall_incident_active,
    clear_c4_exit_stuck_incident,
    publish_c4_exit_stuck_incident,
    record_c4_exit_stuck_auto_resolved,
)

# General no-progress watchdog: if a piece is physically on the classification
# channel (perception n_pieces > 0) but the flow makes NO progress for this
# long, the process is wedged — no matter WHICH state it's stuck in or which
# zone perception thinks the piece is in. "Progress" is a state transition in
# simple mode; in two-piece mode it also covers track ids appearing/leaving,
# zone changes, substantial piece movement, and capture/classify milestones.
# With automatic handling the watchdog first tries to clear the channel itself
# (rotate forward up to _STALL_AUTO_CLEAR_MAX_TURNS full output turns, checking
# occupancy as it goes); only if that fails does it raise the operator
# exit-stuck incident. While the incident is active the flow is frozen and only
# the watchdog keeps running, so the incident auto-clears the moment perception
# sees the channel empty. The threshold is well above any normal single-state
# dwell (rotate/classify/discharge all transition within a few seconds).
_STALL_INCIDENT_MS = 30000.0
_STALL_AUTO_CLEAR_MAX_TURNS = 2
from subsystems.classification_channel.running import Running
from subsystems.classification_channel.simple_state_machine_rev01 import (
    buildRev01StatesMap,
)
from subsystems.classification_channel.snapping import Snapping
from subsystems.classification_channel.states import ClassificationChannelState
from subsystems.shared_variables import SharedVariables

# =============================================================================
# CLASSIFICATION CHANNEL PATHS
# =============================================================================
# SIMPLE_STATE_MACHINE_REV01  (the one that pairs with GO_TO_ANGLE_REV01 feeder)
#   - The rev01 package (simple_state_machine_rev01/) is the relevant one for
#     current Rev04 + jitter work on the classification side.
#   - Has its own perception vs legacy vision branches inside the rev01 states.
#
# Everything else (DYNAMIC + the old classification/ package states) is the
# legacy path and is not the focus when working on go-to-angle feeder jitter.
# =============================================================================


class ClassificationChannelStateMachine(BaseSubsystem):
    def __init__(
        self,
        *,
        irl: IRLInterface,
        irl_config: IRLConfig,
        gc: GlobalConfig,
        shared: SharedVariables,
        vision,
        event_queue,
        transport: ClassificationChannelTransport,
    ):
        super().__init__()
        self.irl = irl
        self.gc = gc
        self.logger = gc.logger
        self.shared = shared
        self.vision = vision
        self.event_queue = event_queue
        self.transport = transport
        self.irl_config = irl_config
        self._mode: ClassificationChannelMode = getattr(
            irl_config.classification_channel_config,
            "mode",
            ClassificationChannelMode.DYNAMIC,
        )
        self._dynamic_mode = self._mode == ClassificationChannelMode.DYNAMIC
        if self._dynamic_mode:
            self.transport.configureDynamicMode(irl_config.classification_channel_config)
        self.current_state = ClassificationChannelState.IDLE
        # Pipeline modes are self-contained controllers (not a states_map);
        # step()/cleanup() delegate to the selected controller.
        self._delegate = None
        self._two_piece = None  # compatibility alias for watchdog diagnostics/tests
        if self._mode == ClassificationChannelMode.SIMPLE_STATE_MACHINE_REV01:
            self.states_map = buildRev01StatesMap(
                irl=irl,
                irl_config=irl_config,
                gc=gc,
                shared=shared,
                transport=transport,
                vision=vision,
                event_queue=event_queue,
            )
        elif self._mode == ClassificationChannelMode.TWO_PIECE_STATE_MACHINE_REV01:
            from subsystems.classification_channel.two_piece import (
                TwoPieceClassificationChannel,
            )
            from subsystems.classification_channel.simple_state_machine_rev01.context import (
                SimpleStateMachineRev01Context,
            )
            self.states_map = {}
            self._delegate = TwoPieceClassificationChannel(
                irl,
                irl_config,
                gc,
                shared,
                transport,
                vision,
                event_queue,
                SimpleStateMachineRev01Context(),
            )
            self._two_piece = self._delegate
        elif self._mode == ClassificationChannelMode.INDEXED_POCKET_PIPELINE_REV01:
            from subsystems.classification_channel.indexed_pocket_pipeline import (
                IndexedPocketPipeline,
            )

            self.states_map = {}
            self._delegate = IndexedPocketPipeline(
                irl, irl_config, gc, shared, transport, vision, event_queue
            )
        else:
            self.states_map = {
                ClassificationChannelState.IDLE: Idle(
                    irl, irl_config, gc, shared, transport, vision
                ),
            }
            if self._dynamic_mode:
                self.states_map[ClassificationChannelState.RUNNING] = Running(
                    irl,
                    irl_config,
                    gc,
                    shared,
                    transport,
                    vision,
                    event_queue,
                )
            else:
                self.states_map.update(
                    {
                        ClassificationChannelState.DETECTING: Detecting(
                            irl, gc, shared, transport, vision, event_queue
                        ),
                        ClassificationChannelState.SNAPPING: Snapping(
                            irl, gc, shared, transport, vision, event_queue
                        ),
                        ClassificationChannelState.EJECTING: Ejecting(
                            irl, irl_config, gc, shared, transport, vision, event_queue
                        ),
                    }
                )
        self.gc.profiler.enterState("classification", self.current_state.value)
        if hasattr(self.gc, "runtime_stats"):
            self.gc.runtime_stats.observeStateTransition(
                "classification", None, self.current_state.value
            )
        # No-progress watchdog state: last time the SM made a transition (its
        # "progress" signal) and whether we've raised the stall incident.
        self._last_progress_at = time.monotonic()
        self._stall_incident_raised = False
        # Operator pressed "Auto Resolve" on an active stall incident: the next
        # step() runs the same rotate-to-clear routine the automatic policy
        # uses, on the coordinator thread (never from the HTTP handler).
        self._stall_resolve_requested = False
        # Track-loss acknowledgement arrives on the HTTP thread.  The request
        # is consumed by step() so state cleanup, ownership reset, and gate
        # changes all happen on the coordinator thread.
        self._track_lost_recovery_lock = threading.Lock()
        self._track_lost_recovery_piece_uuid: str | None = None

    def step(self) -> None:
        if self._applyRequestedTrackLostRecovery():
            self._checkStall(time.monotonic())
            return
        # While OUR stall incident is active the flow is frozen: only the
        # watchdog runs, so the incident auto-clears the moment the operator
        # removes the piece (or it finally falls off) — and nothing moves while
        # the operator's hands are in the machine.
        stall_hold = self._stall_incident_raised and c4_stall_incident_active(self.gc)
        if stall_hold and self._stall_resolve_requested:
            self._runRequestedStallResolve()
            stall_hold = self._stall_incident_raised and c4_stall_incident_active(self.gc)
        delegate = self._activeDelegate()
        if delegate is not None:
            if not stall_hold:
                delegate.step()
            self._checkStall(time.monotonic())
            return
        if stall_hold:
            self._checkStall(time.monotonic())
            return
        import time as _time
        _t0 = _time.perf_counter()
        self.gc.profiler.hit("classification.state_machine.step.calls")
        _t1 = _time.perf_counter()
        next_state = self.states_map[self.current_state].step()
        _t2 = _time.perf_counter()
        _after_t0 = _time.perf_counter()
        if next_state and next_state != self.current_state:
            _cleanup_t0 = _time.perf_counter()
            prev_state = self.current_state
            # A state transition is the SM's "forward progress" signal.
            self._last_progress_at = _time.monotonic()
            self.logger.info(
                f"ClassificationChannel: {prev_state.value} -> {next_state.value}"
            )
            self.gc.profiler.hit(
                f"classification.state_machine.transition.{prev_state.value}->{next_state.value}"
            )
            self.states_map[prev_state].cleanup()
            self.current_state = next_state
            if hasattr(self.gc, "runtime_stats"):
                self.gc.runtime_stats.observeStateTransition(
                    "classification", prev_state.value, next_state.value
                )
            self.gc.profiler.enterState("classification", self.current_state.value)
            self.gc.runtime_stats.observePerfMs(
                "classification.sm.transition_cleanup_ms",
                (_time.perf_counter() - _cleanup_t0) * 1000.0,
            )
        _t3 = _time.perf_counter()
        self.gc.runtime_stats.observePerfMs(
            f"classification.sm.state_step_ms.{self.current_state.value}",
            (_t2 - _t1) * 1000.0,
        )
        self.gc.runtime_stats.observePerfMs(
            "classification.sm.overhead_before_state_ms",
            (_t1 - _t0) * 1000.0,
        )
        self.gc.runtime_stats.observePerfMs(
            "classification.sm.overhead_after_state_ms",
            (_t3 - _after_t0) * 1000.0,
        )
        self.gc.runtime_stats.observePerfMs(
            "classification.sm.total_ms",
            (_t3 - _t0) * 1000.0,
        )
        self._checkStall(_time.monotonic())

    def _watchdogStateLabel(self) -> str:
        delegate = self._activeDelegate()
        if delegate is not None:
            return delegate.phaseName()
        return self.current_state.value

    def _progressAt(self) -> float:
        delegate = self._activeDelegate()
        if delegate is not None:
            return float(delegate.last_progress_at)
        return self._last_progress_at

    def _rearmProgress(self, now: float) -> None:
        self._last_progress_at = now
        delegate = self._activeDelegate()
        if delegate is not None:
            delegate.noteProgress()

    def _activeDelegate(self):
        return getattr(self, "_delegate", None) or getattr(self, "_two_piece", None)

    def supportsStatefulPause(self) -> bool:
        return self._mode == ClassificationChannelMode.INDEXED_POCKET_PIPELINE_REV01

    def pause(self) -> None:
        delegate = self._activeDelegate()
        pause = getattr(delegate, "pause", None)
        if callable(pause):
            pause()

    def resume(self) -> None:
        delegate = self._activeDelegate()
        resume = getattr(delegate, "resume", None)
        if callable(resume):
            resume()

    def _checkStall(self, now: float) -> None:
        # Only the supported rev01 paths (simple + two-piece). Legacy/dynamic
        # paths have their own flow.
        if self._mode not in (
            ClassificationChannelMode.SIMPLE_STATE_MACHINE_REV01,
            ClassificationChannelMode.TWO_PIECE_STATE_MACHINE_REV01,
        ):
            return

        # If we raised the incident and it's since been resolved (operator
        # cleared it), re-arm from now so we don't instantly re-fire on the next
        # step — give the resumed flow a fresh window to make progress.
        if self._stall_incident_raised and not c4_stall_incident_active(self.gc):
            self._stall_incident_raised = False
            self._rearmProgress(now)
            return

        perception_service = getattr(self.gc, "perception_service", None)
        occupied = False
        if perception_service is not None:
            try:
                occupied = int(perception_service.read_state(4).n_pieces) > 0
            except Exception:
                occupied = False

        if not occupied:
            # Channel clear -> not stuck. Re-arm and drop any raised incident
            # (the piece left / was removed).
            self._rearmProgress(now)
            if self._stall_incident_raised:
                clear_c4_exit_stuck_incident(self.gc)
                self._stall_incident_raised = False
            return

        stalled_ms = (now - self._progressAt()) * 1000.0
        if self._stall_incident_raised or stalled_ms < _STALL_INCIDENT_MS:
            return

        try:
            from toml_config import incidentHandlingOff

            if incidentHandlingOff(C4_EXIT_STUCK_INCIDENT_KIND):
                return
        except Exception:
            pass

        # Auto-resolve: when this incident is set to automatic handling, try to
        # clear the channel ourselves (advance forward until the piece is gone,
        # the same routine spoke-home uses) instead of stopping for an operator.
        # Only fall through to the manual incident if that didn't clear it.
        auto_result = self._tryAutoResolveStall(stalled_ms)
        if auto_result is not None and auto_result.cleared:
            record_c4_exit_stuck_auto_resolved(
                self.gc,
                stalled_ms=stalled_ms,
                stalled_state=self._watchdogStateLabel(),
                moved_deg=auto_result.output_deg_moved,
            )
            return

        published = publish_c4_exit_stuck_incident(
            self.gc,
            stalled_ms=stalled_ms,
            stalled_state=self._watchdogStateLabel(),
            auto_clear_failed=auto_result is not None,
            auto_clear_moved_deg=(
                auto_result.output_deg_moved if auto_result is not None else 0.0
            ),
        )
        self._stall_incident_raised = bool(published)
        if not published:
            # Another incident owns the slot (or stats are unavailable). Re-arm
            # so we retry after a full window instead of every tick.
            self._rearmProgress(now)
        self.logger.info(
            f"ClassificationChannel: STALLED in {self._watchdogStateLabel()} for "
            f"{stalled_ms:.0f}ms with a piece on the channel — raised exit-stuck "
            f"incident (published={self._stall_incident_raised})"
        )

    def _tryAutoResolveStall(self, stalled_ms: float):
        """Returns None when auto handling is off, otherwise the
        ChannelClearResult of the attempt (check .cleared)."""
        try:
            from toml_config import incidentHandlingAutomatic

            if not incidentHandlingAutomatic(C4_EXIT_STUCK_INCIDENT_KIND):
                return None
        except Exception:
            return None

        self.logger.info(
            f"ClassificationChannel: STALLED in {self._watchdogStateLabel()} for "
            f"{stalled_ms:.0f}ms — auto-resolve enabled, advancing channel to clear the piece"
        )
        return self._runStallClear()

    def _runStallClear(self):
        """The one stall-recovery action, shared by the automatic policy and the
        operator's Auto Resolve button: rotate the channel forward
        (occupancy-checked) until it clears or the budget runs out. Blocking;
        must only run on the coordinator thread. Returns a ChannelClearResult."""
        max_output_deg = _STALL_AUTO_CLEAR_MAX_TURNS * 360.0
        delegate = self._activeDelegate()
        if delegate is not None:
            result = delegate.attemptStallAutoClear(max_output_deg=max_output_deg)
        else:
            from subsystems.classification_channel.simple_state_machine_rev01.channel_clear import (
                clearChannelByAdvancing,
            )

            result = clearChannelByAdvancing(
                self.gc,
                self.irl,
                self.irl_config,
                vision=self.vision,
                max_output_deg=max_output_deg,
            )
        if result.cleared:
            # Re-arm fresh: the blocking clear consumed real time, so the window
            # restarts from now, not from the pre-clear timestamp.
            self._rearmProgress(time.monotonic())
            self.logger.info(
                f"ClassificationChannel: stall clear advanced "
                f"{result.output_deg_moved:.0f}° and the channel is empty — resuming"
            )
        else:
            self.logger.warning(
                f"ClassificationChannel: stall clear advanced {result.output_deg_moved:.0f}° but the "
                f"channel is still occupied ({result.reason})"
            )
        return result

    def requestStallAutoResolve(self) -> bool:
        """Called from the HTTP router when the operator presses Auto Resolve on
        an active stall incident. Only sets a flag — the coordinator thread
        performs the actual motion on its next step()."""
        if not c4_stall_incident_active(self.gc):
            return False
        self._stall_resolve_requested = True
        return True

    def requestTrackLostRecovery(self, piece_uuid: str | None) -> bool:
        """Queue recovery for a faulted, pre-distribution rev01 piece.

        The active incident keeps the coordinator from stepping while this is
        called by the HTTP handler, so the state and piece ownership are stable
        while we validate them.  Returning False deliberately preserves the
        existing clear-only behavior for track-loss incidents owned by other
        stages (for example distribution's exit-track timeout).
        """
        if self._mode != ClassificationChannelMode.SIMPLE_STATE_MACHINE_REV01:
            return False
        if self.current_state not in {
            ClassificationChannelState.REV01_MOVING_TO_PRECISE,
            ClassificationChannelState.REV01_AWAITING_DISTRIBUTION,
            ClassificationChannelState.REV01_DISCHARGING,
        }:
            return False
        current = self.states_map.get(self.current_state)
        can_recover = getattr(current, "canRecoverLostTrack", None)
        if not callable(can_recover) or not bool(can_recover(piece_uuid)):
            return False
        with self._track_lost_recovery_lock:
            self._track_lost_recovery_piece_uuid = piece_uuid
        return True

    def ownsTrackLostFault(self, piece_uuid: str | None) -> bool:
        """Whether the active track-loss incident owns this rev01 state latch."""
        if self._mode != ClassificationChannelMode.SIMPLE_STATE_MACHINE_REV01:
            return False
        current = self.states_map.get(self.current_state)
        owns_fault = getattr(current, "ownsLostTrackFault", None)
        return bool(callable(owns_fault) and owns_fault(piece_uuid))

    def _applyRequestedTrackLostRecovery(self) -> bool:
        lock = getattr(self, "_track_lost_recovery_lock", None)
        if lock is None:
            return False
        with lock:
            piece_uuid = self._track_lost_recovery_piece_uuid
            self._track_lost_recovery_piece_uuid = None
        if piece_uuid is None:
            return False

        current = self.states_map.get(self.current_state)
        can_recover = getattr(current, "canRecoverLostTrack", None)
        if not callable(can_recover) or not bool(can_recover(piece_uuid)):
            self.logger.warning(
                "ClassificationChannel: ignored stale track-loss recovery request "
                f"for {piece_uuid[:8]} in {self.current_state.value}"
            )
            return False

        prev_state = self.current_state
        self.logger.info(
            "ClassificationChannel: operator cleared lost C4 track for "
            f"{piece_uuid[:8]} — abandoning the faulted cycle without credit"
        )
        set_ready = getattr(current, "setClassificationReady", None)
        abandon_lost = getattr(current, "abandonLostTrackObject", None)
        abandon = getattr(current, "abandonInFlightObject", None)
        cleanup = getattr(current, "cleanup", None)
        context = getattr(current, "ctx", None)
        reset_context = getattr(context, "reset", None)
        if (
            not callable(set_ready)
            or (not callable(abandon_lost) and not callable(abandon))
            or not callable(cleanup)
            or not callable(reset_context)
        ):
            self.logger.error(
                "ClassificationChannel: track-loss recovery state is incomplete; holding"
            )
            return False
        # Keep C3 closed until IDLE sees its normal consecutive clear frames.
        set_ready(False, "recovering cleared track loss")
        if callable(abandon_lost):
            if not bool(abandon_lost("operator cleared C4 track-loss incident")):
                self.logger.error(
                    "ClassificationChannel: could not cancel the lost-track piece; holding"
                )
                return False
        else:
            abandon("operator cleared C4 track-loss incident")
        cleanup()
        reset_context()
        self.current_state = ClassificationChannelState.IDLE
        self._last_progress_at = time.monotonic()
        if hasattr(self.gc, "runtime_stats"):
            self.gc.runtime_stats.observeStateTransition(
                "classification", prev_state.value, self.current_state.value
            )
        self.gc.profiler.enterState("classification", self.current_state.value)
        return True

    def _runRequestedStallResolve(self) -> None:
        self._stall_resolve_requested = False
        runtime_stats = getattr(self.gc, "runtime_stats", None)
        if runtime_stats is None or not hasattr(runtime_stats, "activeIncident"):
            return
        active = runtime_stats.activeIncident()
        if not isinstance(active, dict) or active.get("kind") != C4_EXIT_STUCK_INCIDENT_KIND:
            return
        # Show the run in the popup (and lock its buttons) before the blocking
        # clear starts.
        running = dict(active)
        running["status"] = "auto_release_running"
        running["awaiting_operator"] = False
        runtime_stats.setActiveIncident(running)
        self.logger.info(
            "ClassificationChannel: operator requested stall auto-resolve — "
            "advancing channel to clear the piece"
        )
        result = self._runStallClear()
        if result.cleared:
            clear_c4_exit_stuck_incident(self.gc)
            self._stall_incident_raised = False
            return
        failed = dict(running)
        failed["status"] = "waiting_for_operator"
        failed["awaiting_operator"] = True
        failed["auto_clear_failed"] = True
        failed["auto_clear_moved_deg"] = float(result.output_deg_moved)
        failed["operator_message"] = (
            "Auto resolve rotated the channel "
            f"{result.output_deg_moved:.0f}° and it is still occupied. Remove the "
            "piece (or clear the jam) to continue."
        )
        runtime_stats.setActiveIncident(failed)

    def cleanup(self) -> None:
        self.gc.profiler.exitState("classification")
        # Fresh watchdog window on the next start — a pause/standby stretch must
        # not count toward "stalled".
        self._last_progress_at = time.monotonic()
        delegate = self._activeDelegate()
        if delegate is not None:
            delegate.cleanup()
            return
        # Tearing down mid-cycle (machine stop / standby): if a piece was
        # photographed but never classified or distributed, mark it aborted so
        # the UI drops it instead of leaving it stuck in "capturing" forever.
        if self._mode == ClassificationChannelMode.SIMPLE_STATE_MACHINE_REV01:
            current = self.states_map[self.current_state]
            abandon = getattr(current, "abandonInFlightObject", None)
            if callable(abandon):
                abandon("classification channel teardown")
        self.states_map[self.current_state].cleanup()
        if self._dynamic_mode and hasattr(self.transport, "resetDynamicState"):
            self.transport.resetDynamicState()
        # Reset to IDLE so the next resume / start re-runs the chamber
        # purge check instead of resuming mid-cycle.
        self.current_state = ClassificationChannelState.IDLE
