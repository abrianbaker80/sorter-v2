"""Tests for the distribution Sending state — chute reopen gating.

Before the two-gate fix, Sending would reopen the distribution gate after a
fixed ``CHUTE_SETTLE_MS`` wall-clock timer without verifying that the
dropped piece had physically left the classification channel. This is the
root cause of the ~63% ``multi_drop_fail`` rate observed in production
runs — the next drop cycle commits while the previous piece is still
inside the exit guide.

The patched Sending state now holds the gate closed until EITHER:

  * the carousel live tracker no longer shows the dropped piece's
    ``global_id`` (vision-confirmed exit), OR
  * the ``post_distribute_cooldown_s`` cooldown has elapsed since drop
    commit (fallback when the tracker signal is unavailable).
"""

from __future__ import annotations

import queue
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from defs.known_object import HARVEST_CONFIRMATION_UNCREDITED, KnownObject, PieceStage
from project_harvest_projects import HarvestProjectError
from piece_transport import ClassificationChannelTransport
from runtime_stats import RuntimeStatsCollector
from subsystems.bus import TickBus
from subsystems.distribution.sending import (
    CHUTE_SETTLE_MS,
    MISSING_DROP_PIECE_GRACE_MS,
    PIECE_EXIT_INCIDENT_MS,
    SAMPLE_COLLECTION_CHUTE_SETTLE_MS,
    Sending,
)
from subsystems.distribution.ready import Ready
from subsystems.distribution.states import DistributionState
from subsystems.shared_variables import SharedVariables


class _NullTimer:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class _Profiler:
    def hit(self, *args, **kwargs) -> None:
        pass

    def mark(self, *args, **kwargs) -> None:
        pass

    def timer(self, *args, **kwargs):
        return _NullTimer()

    def enterState(self, *args, **kwargs) -> None:
        pass

    def exitState(self, *args, **kwargs) -> None:
        pass


class _Logger:
    def info(self, *args, **kwargs) -> None:
        pass

    def warning(self, *args, **kwargs) -> None:
        pass

    def warn(self, *args, **kwargs) -> None:
        pass

    def exception(self, *args, **kwargs) -> None:
        pass

    def error(self, *args, **kwargs) -> None:
        pass


class _RunRecorder:
    def __init__(self) -> None:
        self.pieces: list[KnownObject] = []

    def recordPiece(self, piece: KnownObject) -> None:
        self.pieces.append(piece)


class _FakeVision:
    def __init__(self, live_ids_by_role: dict[str, set[int]] | None = None) -> None:
        self._live = live_ids_by_role or {}
        self.force_killed: list[int] = []

    def getFeederTrackerLiveGlobalIds(self, role: str) -> set[int]:
        return set(self._live.get(role, set()))

    def setLive(self, role: str, ids: set[int]) -> None:
        self._live[role] = set(ids)

    def forceKillCarouselTrack(self, global_id: int) -> bool:
        self.force_killed.append(int(global_id))
        self._live.get("carousel", set()).discard(int(global_id))
        return True


class _GlobalConfig:
    def __init__(self) -> None:
        self.logger = _Logger()
        self.profiler = _Profiler()
        self.runtime_stats = RuntimeStatsCollector()
        self.run_recorder = _RunRecorder()
        self.set_progress_tracker = None
        self.disable_servos = False


def _mkSending(
    *,
    vision: _FakeVision | None,
    cooldown_s: float,
    shared: SharedVariables,
    event_queue: queue.Queue,
    gc: _GlobalConfig,
) -> Sending:
    # ``irl`` is only read for attribute access that Sending/BaseState don't
    # actually touch — a simple stub with a bool chute placeholder is
    # enough for unit-level behavior.
    class _IRL:
        pass

    return Sending(
        _IRL(),  # type: ignore[arg-type]
        gc,  # type: ignore[arg-type]
        shared,
        event_queue,
        vision=vision,
        post_distribute_cooldown_s=cooldown_s,
    )


class ReadyDropSignalTests(unittest.TestCase):
    def _ready(
        self,
        transport: ClassificationChannelTransport,
    ) -> tuple[Ready, SharedVariables]:
        gc = _GlobalConfig()
        shared = SharedVariables(gc=gc, bus=TickBus())
        shared.transport = transport
        return Ready(type("IRL", (), {})(), gc, shared), shared

    def test_incident_gate_close_is_not_mistaken_for_piece_drop(self) -> None:
        transport = ClassificationChannelTransport()
        piece = KnownObject()
        transport.placePieceForDistribution(piece)
        ready, shared = self._ready(transport)

        self.assertIsNone(ready.step())
        shared.set_distribution_gate(False, reason="incident:test")

        self.assertIsNone(ready.step())
        previous_exit_piece = KnownObject()
        transport._exit_piece = previous_exit_piece  # noqa: SLF001 — stale prior drop
        self.assertIs(piece, transport.cancelPieceForDistribution(piece.uuid))
        self.assertEqual(DistributionState.IDLE, ready.step())
        self.assertIs(previous_exit_piece, transport.getPieceForDistributionDrop())

    def test_canceled_piece_before_ready_returns_to_idle(self) -> None:
        transport = ClassificationChannelTransport()
        piece = KnownObject()
        transport.placePieceForDistribution(piece)
        self.assertIs(piece, transport.cancelPieceForDistribution(piece.uuid))
        self.assertTrue(transport.isCanceledPieceForDistribution(piece.uuid))
        ready, _shared = self._ready(transport)

        self.assertEqual(DistributionState.IDLE, ready.step())
        self.assertFalse(transport.isCanceledPieceForDistribution(piece.uuid))


class SendingChuteReopenGateTests(unittest.TestCase):
    def _mkTransportWithDrop(self, *, tracked_global_id: int) -> ClassificationChannelTransport:
        transport = ClassificationChannelTransport()
        # Manually stage a piece into the exit buffer so
        # ``getPieceForDistributionDrop`` returns it without exercising
        # the full zone manager pipeline.
        piece = KnownObject(tracked_global_id=tracked_global_id)
        transport._exit_piece = piece  # noqa: SLF001 — test-only shortcut
        return transport

    def _mkSharedWithTransport(self, transport: ClassificationChannelTransport) -> SharedVariables:
        bus = TickBus()
        gc_stub = _GlobalConfig()
        shared = SharedVariables(gc=gc_stub, bus=bus)
        shared.transport = transport
        # Close the distribution gate — Sending's job is to reopen it.
        shared.set_distribution_gate(False, reason="test_setup")
        return shared

    def test_reopens_when_tracker_no_longer_sees_piece(self) -> None:
        transport = self._mkTransportWithDrop(tracked_global_id=42)
        shared = self._mkSharedWithTransport(transport)
        gc = _GlobalConfig()
        event_queue: queue.Queue = queue.Queue()
        vision = _FakeVision(live_ids_by_role={"carousel": set()})  # piece has exited

        sending = _mkSending(
            vision=vision,
            cooldown_s=0.8,
            shared=shared,
            event_queue=event_queue,
            gc=gc,
        )

        # First step: pick up the piece, start the settle timer.
        self.assertIsNone(sending.step())
        self.assertIsNotNone(sending.piece)
        self.assertEqual(42, sending.piece.tracked_global_id)

        # Backdate start_time past the chute-settle threshold so we don't
        # have to actually sleep 1.5s; we also push it past the cooldown
        # fallback so only the tracker signal is what lets us reopen.
        sending.start_time = time.time() - (CHUTE_SETTLE_MS / 1000.0) - 5.0

        next_state = sending.step()
        self.assertEqual(DistributionState.IDLE, next_state)
        self.assertTrue(shared.get_distribution_ready())

    def test_live_harvest_drop_is_confirmed_before_piece_commit(self) -> None:
        transport = self._mkTransportWithDrop(tracked_global_id=43)
        piece = transport._exit_piece  # noqa: SLF001
        piece.harvest_project_id = "harvest-project"
        piece.harvest_activation_id = "activation-project"
        piece.harvest_allocation_id = "allocation-project"
        piece.destination_bin = (0, 0, 1)
        shared = self._mkSharedWithTransport(transport)
        gc = _GlobalConfig()
        allocation = {"mode": "live", "runtime_id": "activation-project",
                      "allocation_id": "allocation-project", "group_id": "bag-1",
                      "status": "confirmed", "confirmed_at": datetime.fromtimestamp(time.time() - 1, timezone.utc).isoformat(),
                      "project_completed": True}
        gc.runtime_stats.observeHarvestReservation(allocation, now_wall=time.time() - 60)
        event_queue: queue.Queue = queue.Queue()
        sending = _mkSending(
            vision=_FakeVision(live_ids_by_role={"carousel": set()}),
            cooldown_s=0,
            shared=shared,
            event_queue=event_queue,
            gc=gc,
        )
        self.assertIsNone(sending.step())
        sending.start_time = time.time() - (CHUTE_SETTLE_MS / 1000.0) - 1

        control_queue: queue.Queue = queue.Queue()
        with (
            patch("server.shared_state.command_queue", control_queue),
            patch(
                "project_harvest_runtime.confirm_piece_drop",
                return_value=allocation,
            ) as confirm,
        ):
            self.assertEqual(DistributionState.IDLE, sending.step())

        confirm.assert_called_once_with(gc, piece)
        counts = gc.runtime_stats.snapshot()["harvest_throughput"]
        self.assertEqual(1, counts["bag_count"], (counts, gc.runtime_stats._harvest_scope, allocation))
        self.assertEqual(1, counts["distributed_count"])
        self.assertEqual("distributed", piece.stage.value)
        self.assertEqual([piece], gc.run_recorder.pieces)
        tags = [getattr(event, "tag", None) for event in list(event_queue.queue)]
        self.assertIn("known_object", tags)
        self.assertNotIn("pause", tags)
        self.assertEqual("pause", control_queue.get_nowait().tag)

    def test_controlled_harvest_limit_requests_pause_after_confirmed_drop(self) -> None:
        transport = self._mkTransportWithDrop(tracked_global_id=45)
        piece = transport._exit_piece  # noqa: SLF001
        piece.harvest_project_id = "harvest-project"
        piece.harvest_activation_id = "activation-project"
        piece.harvest_allocation_id = "allocation-project"
        piece.destination_bin = (0, 0, 1)
        shared = self._mkSharedWithTransport(transport)
        gc = _GlobalConfig()
        event_queue: queue.Queue = queue.Queue()
        sending = _mkSending(
            vision=_FakeVision(live_ids_by_role={"carousel": set()}),
            cooldown_s=0,
            shared=shared,
            event_queue=event_queue,
            gc=gc,
        )
        self.assertIsNone(sending.step())
        sending.start_time = time.time() - (CHUTE_SETTLE_MS / 1000.0) - 1

        control_queue: queue.Queue = queue.Queue()
        with (
            patch("server.shared_state.command_queue", control_queue),
            patch(
                "project_harvest_runtime.confirm_piece_drop",
                return_value={"project_completed": False, "pause_required": True},
            ),
        ):
            self.assertEqual(DistributionState.IDLE, sending.step())

        self.assertEqual("distributed", piece.stage.value)
        self.assertEqual("pause", control_queue.get_nowait().tag)

    def test_live_harvest_confirmation_failure_holds_gate_and_pauses(self) -> None:
        transport = self._mkTransportWithDrop(tracked_global_id=44)
        piece = transport._exit_piece  # noqa: SLF001
        piece.harvest_project_id = "harvest-project"
        piece.harvest_activation_id = "activation-project"
        piece.harvest_allocation_id = "allocation-project"
        piece.destination_bin = (0, 0, 1)
        piece.c4_marker_exit_boundary = 21
        shared = self._mkSharedWithTransport(transport)
        gc = _GlobalConfig()
        event_queue: queue.Queue = queue.Queue()
        sending = _mkSending(
            vision=_FakeVision(live_ids_by_role={"carousel": set()}),
            cooldown_s=0,
            shared=shared,
            event_queue=event_queue,
            gc=gc,
        )
        self.assertIsNone(sending.step())
        sending.start_time = time.time() - (CHUTE_SETTLE_MS / 1000.0) - 1

        control_queue: queue.Queue = queue.Queue()
        with (
            patch("server.shared_state.command_queue", control_queue),
            patch(
                "project_harvest_runtime.confirm_piece_drop",
                side_effect=RuntimeError("database unavailable"),
            ),
        ):
            self.assertIsNone(sending.step())

        self.assertNotEqual("distributed", piece.stage.value)
        self.assertFalse(shared.get_distribution_ready())
        self.assertEqual([], gc.run_recorder.pieces)
        self.assertEqual("pause", control_queue.get_nowait().tag)

    def _markerHarvestSending(self):
        transport = self._mkTransportWithDrop(tracked_global_id=46)
        piece = transport._exit_piece
        piece.stage = PieceStage.distributing
        piece.harvest_project_id = "harvest-project"
        piece.harvest_activation_id = "activation-project"
        piece.harvest_allocation_id = "allocation-project"
        piece.destination_bin = (0, 0, 1)
        piece.c4_marker_exit_boundary = 21
        shared = self._mkSharedWithTransport(transport)
        gc = _GlobalConfig()
        gc.set_progress_tracker = Mock()
        gc.runtime_stats.setLifecycleState("running")
        gc.runtime_stats.observeHarvestReservation({
            "mode": "live", "runtime_id": piece.harvest_activation_id,
            "allocation_id": piece.harvest_allocation_id, "status": "planned",
        }, now_wall=time.time() - 60)
        events = queue.Queue()
        sending = _mkSending(
            vision=_FakeVision(live_ids_by_role={"carousel": {46}}), cooldown_s=0,
            shared=shared, event_queue=events, gc=gc,
        )
        return sending, piece, shared, gc, events

    def test_marker_harvest_evidence_failures_continue_without_credit(self) -> None:
        for code in (
            "HARVEST_DESTINATION_MISSING", "ALLOCATION_NOT_FOUND", "ALLOCATION_UNDONE",
            "PROJECT_NOT_FOUND", "PROJECT_NOT_ACTIVE", "RUNTIME_STATE_MISMATCH",
            "PHYSICAL_EVIDENCE_REQUIRED",
            "HARVEST_CONFIRMATION_IDENTITY_MISMATCH",
        ):
            with self.subTest(code=code):
                sending, piece, shared, gc, events = self._markerHarvestSending()
                commands = queue.Queue()
                with (
                    patch("server.shared_state.command_queue", commands),
                    patch("server.shared_state.setHardwareStatus") as hardware_status,
                    patch("project_harvest_runtime.confirm_piece_drop",
                          side_effect=HarvestProjectError(code, "evidence unavailable")) as confirm,
                    patch("project_harvest_runtime.retire_unconfirmed_piece_drop", return_value=1) as retire,
                    patch("local_state.record_piece_distribution") as bin_credit,
                ):
                    self.assertIsNone(sending.step())
                    confirm.assert_not_called()
                    sending.start_time = time.time() - CHUTE_SETTLE_MS / 1000 - 1
                    self.assertEqual(DistributionState.IDLE, sending.step())
                    confirm.assert_called_once_with(gc, piece)
                    retire.assert_called_once()
                    event = events.get_nowait()
                    gc.runtime_stats.observeKnownObject(event.data.model_dump(mode="json"))
                    bin_credit.assert_not_called()
                    hardware_status.assert_not_called()
                self.assertTrue(shared.get_distribution_ready())
                self.assertTrue(piece.aborted)
                self.assertEqual(HARVEST_CONFIRMATION_UNCREDITED, piece.transport_failure_reason)
                self.assertEqual(PieceStage.distributing, piece.stage)
                self.assertIsNone(piece.distributed_at)
                self.assertEqual((0, 0, 1), piece.destination_bin)
                self.assertEqual("allocation-project", piece.harvest_allocation_id)
                self.assertEqual([], gc.run_recorder.pieces)
                gc.set_progress_tracker.record.assert_not_called()
                self.assertTrue(commands.empty())
                snapshot = gc.runtime_stats.snapshot()
                self.assertEqual(0, snapshot["counts"]["stage_distributed"])
                self.assertEqual(0, snapshot["counts"]["distributed"])
                counts = snapshot["harvest_throughput"]
                self.assertEqual(0, counts["bag_count"])
                self.assertEqual(0, counts["distributed_count"])
                self.assertTrue(events.empty())

    def test_marker_harvest_malformed_ids_continue_without_credit(self) -> None:
        from test_project_harvest_projects import _store_with_retirement_allocations

        for field in ("harvest_allocation_id", "harvest_project_id"):
            with self.subTest(field=field), TemporaryDirectory() as directory:
                store, project_id, _original, allocation = _store_with_retirement_allocations(Path(directory))
                sending, piece, shared, gc, events = self._markerHarvestSending()
                piece.uuid = allocation["piece_id"]
                piece.harvest_project_id = project_id
                piece.harvest_allocation_id = allocation["allocation_id"]
                piece.harvest_activation_id = "current-activation"
                setattr(piece, field, "malformed-id")
                with closing(sqlite3.connect(store.db_path)) as conn:
                    allocations_before = list(conn.execute("SELECT * FROM harvest_allocations ORDER BY allocation_id"))
                    audit_before = list(conn.execute("SELECT * FROM harvest_events ORDER BY sequence"))
                commands = queue.Queue()
                with (
                    patch("server.shared_state.command_queue", commands),
                    patch("server.shared_state.setHardwareStatus") as hardware_status,
                    patch("project_harvest_runtime._store", return_value=store),
                    patch.object(store, "confirm_allocation", wraps=store.confirm_allocation) as confirm,
                    patch("local_state.record_piece_distribution") as bin_credit,
                ):
                    self.assertIsNone(sending.step())
                    confirm.assert_not_called()
                    sending.start_time = time.time() - CHUTE_SETTLE_MS / 1000 - 1
                    self.assertEqual(DistributionState.IDLE, sending.step())
                    confirm.assert_called_once()
                    self.assertEqual((piece.harvest_project_id, piece.harvest_allocation_id), confirm.call_args.args)
                    failure = events.get_nowait().data
                    self.assertTrue(failure.aborted)
                    self.assertEqual(PieceStage.distributing, failure.stage)
                    self.assertIsNone(failure.distributed_at)
                    gc.runtime_stats.observeKnownObject(failure.model_dump(mode="json"))
                    bin_credit.assert_not_called()
                    hardware_status.assert_not_called()
                self.assertTrue(shared.get_distribution_ready())
                self.assertTrue(piece.aborted)
                self.assertEqual(HARVEST_CONFIRMATION_UNCREDITED, piece.transport_failure_reason)
                self.assertEqual(PieceStage.distributing, piece.stage)
                self.assertIsNone(piece.distributed_at)
                self.assertIsNone(piece.native_delivery_id)
                self.assertEqual([], gc.run_recorder.pieces)
                gc.set_progress_tracker.record.assert_not_called()
                self.assertTrue(commands.empty())
                self.assertTrue(events.empty())
                snapshot = gc.runtime_stats.snapshot()
                self.assertEqual(0, snapshot["counts"]["stage_distributed"])
                self.assertEqual(0, snapshot["counts"]["distributed"])
                self.assertEqual(0, snapshot["harvest_throughput"]["bag_count"])
                self.assertEqual(0, snapshot["harvest_throughput"]["distributed_count"])
                with closing(sqlite3.connect(store.db_path)) as conn:
                    self.assertEqual(allocations_before, list(conn.execute("SELECT * FROM harvest_allocations ORDER BY allocation_id")))
                    self.assertEqual(audit_before, list(conn.execute("SELECT * FROM harvest_events ORDER BY sequence")))

    def test_marker_harvest_terminal_replay_does_not_reconfirm_or_credit(self) -> None:
        sending, piece, shared, gc, events = self._markerHarvestSending()
        commands = queue.Queue()
        with (
            patch("server.shared_state.command_queue", commands),
            patch("project_harvest_runtime.confirm_piece_drop",
                  side_effect=HarvestProjectError("ALLOCATION_UNDONE", "already retired")) as confirm,
            patch("project_harvest_runtime.retire_unconfirmed_piece_drop", return_value=0) as retire,
        ):
            self.assertIsNone(sending.step())
            sending.start_time = time.time() - CHUTE_SETTLE_MS / 1000 - 1
            self.assertEqual(DistributionState.IDLE, sending.step())
            self.assertEqual(DistributionState.IDLE, sending.step())
            sending.cleanup()
            shared.set_distribution_gate(False, reason="replay")
            self.assertIsNone(sending.step())
            sending.start_time = time.time() - CHUTE_SETTLE_MS / 1000 - 1
            self.assertEqual(DistributionState.IDLE, sending.step())
            confirm.assert_called_once()
            retire.assert_called_once()
        self.assertTrue(shared.get_distribution_ready())
        self.assertEqual(1, events.qsize())
        self.assertIsNone(piece.distributed_at)
        self.assertEqual([], gc.run_recorder.pieces)
        gc.set_progress_tracker.record.assert_not_called()
        self.assertTrue(commands.empty())

    def test_marker_harvest_retirement_storage_failure_retains_pause(self) -> None:
        sending, piece, shared, gc, events = self._markerHarvestSending()
        self.assertIsNone(sending.step())
        sending.start_time = time.time() - CHUTE_SETTLE_MS / 1000 - 1
        commands = queue.Queue()
        with (
            patch("server.shared_state.command_queue", commands),
            patch("project_harvest_runtime.confirm_piece_drop",
                  side_effect=HarvestProjectError("PHYSICAL_EVIDENCE_REQUIRED", "unconfirmed")),
            patch("project_harvest_runtime.retire_unconfirmed_piece_drop",
                  side_effect=RuntimeError("retirement storage unavailable")),
        ):
            self.assertIsNone(sending.step())
        self.assertFalse(shared.get_distribution_ready())
        self.assertFalse(piece.aborted)
        self.assertIsNone(piece.transport_failure_reason)
        self.assertIsNone(piece.distributed_at)
        self.assertEqual([], gc.run_recorder.pieces)
        self.assertTrue(events.empty())
        self.assertEqual("pause", commands.get_nowait().tag)

    def test_harvest_evidence_recovery_excludes_non_marker_and_dynamic_modes(self) -> None:
        for mode in ("non_marker", "dynamic"):
            with self.subTest(mode=mode):
                sending, piece, shared, gc, events = self._markerHarvestSending()
                if mode == "non_marker":
                    piece.c4_marker_exit_boundary = None
                else:
                    shared.transport._dynamic_mode = True
                # Retain the selected drop so this test isolates the recovery gate.
                sending.piece = piece
                sending.start_time = time.time() - CHUTE_SETTLE_MS / 1000 - 1
                commands = queue.Queue()
                with (
                    patch("server.shared_state.command_queue", commands),
                    patch("project_harvest_runtime.confirm_piece_drop",
                          side_effect=HarvestProjectError("PHYSICAL_EVIDENCE_REQUIRED", "unconfirmed")),
                    patch("project_harvest_runtime.retire_unconfirmed_piece_drop") as retire,
                ):
                    self.assertIsNone(sending.step())
                    retire.assert_not_called()
                self.assertFalse(piece.aborted)
                self.assertFalse(shared.get_distribution_ready())
                self.assertEqual("pause", commands.get_nowait().tag)
                self.assertTrue(events.empty())

    def test_uncredited_harvest_drop_waits_for_existing_chute_motion(self) -> None:
        sending, piece, shared, _gc, events = self._markerHarvestSending()
        self.assertIsNone(sending.step())
        sending.start_time = time.time() - CHUTE_SETTLE_MS / 1000 - 1
        shared.set_chute_motion(True, target_bin=None)
        commands = queue.Queue()
        with (
            patch("server.shared_state.command_queue", commands),
            patch("project_harvest_runtime.confirm_piece_drop",
                  side_effect=HarvestProjectError("PHYSICAL_EVIDENCE_REQUIRED", "unconfirmed")) as confirm,
            patch("project_harvest_runtime.retire_unconfirmed_piece_drop", return_value=1),
        ):
            self.assertIsNone(sending.step())
            self.assertFalse(shared.get_distribution_ready())
            self.assertTrue(piece.aborted)
            shared.set_chute_motion(False, target_bin=None)
            self.assertEqual(DistributionState.IDLE, sending.step())
            confirm.assert_called_once()
        self.assertTrue(shared.get_distribution_ready())
        self.assertTrue(commands.empty())
        self.assertEqual(1, events.qsize())

    def test_sending_does_not_confirm_a_still_routable_positioning_piece(self) -> None:
        sending, piece, shared, _gc, _events = self._markerHarvestSending()
        shared.transport._exit_piece = None
        piece.c4_marker_exit_boundary = None
        shared.transport.placePieceForDistribution(piece)
        with patch("project_harvest_runtime.confirm_piece_drop") as confirm:
            self.assertIsNone(sending.step())
            sending.start_time = time.time() - MISSING_DROP_PIECE_GRACE_MS / 1000 - 1
            self.assertEqual(DistributionState.IDLE, sending.step())
            confirm.assert_not_called()
        self.assertIs(piece, shared.transport.getPieceForDistributionPositioning())
        self.assertFalse(piece.aborted)

    def test_holds_gate_while_tracker_still_sees_piece(self) -> None:
        transport = self._mkTransportWithDrop(tracked_global_id=17)
        shared = self._mkSharedWithTransport(transport)
        gc = _GlobalConfig()
        event_queue: queue.Queue = queue.Queue()
        # Tracker still reports the piece — Sending must NOT reopen even
        # after the settle+cooldown clock has elapsed.
        vision = _FakeVision(live_ids_by_role={"carousel": {17}})

        sending = _mkSending(
            vision=vision,
            cooldown_s=0.2,
            shared=shared,
            event_queue=event_queue,
            gc=gc,
        )
        self.assertIsNone(sending.step())

        # Backdate past both settle and cooldown — tracker still dominates.
        sending.start_time = time.time() - (CHUTE_SETTLE_MS / 1000.0) - 5.0

        next_state = sending.step()
        self.assertIsNone(next_state)
        self.assertFalse(shared.get_distribution_ready())

        # Now the tracker loses sight of the piece — gate opens on the
        # next step() call without a fresh cooldown.
        vision.setLive("carousel", set())
        next_state = sending.step()
        self.assertEqual(DistributionState.IDLE, next_state)
        self.assertTrue(shared.get_distribution_ready())

    def test_cooldown_fallback_when_tracker_unavailable(self) -> None:
        transport = self._mkTransportWithDrop(tracked_global_id=99)
        shared = self._mkSharedWithTransport(transport)
        gc = _GlobalConfig()
        event_queue: queue.Queue = queue.Queue()

        sending = _mkSending(
            vision=None,  # no tracker signal at all
            cooldown_s=0.25,
            shared=shared,
            event_queue=event_queue,
            gc=gc,
        )

        # Settle timer elapsed, but cooldown has NOT — gate must stay closed.
        self.assertIsNone(sending.step())
        sending.start_time = time.time() - (CHUTE_SETTLE_MS / 1000.0) - 0.05
        self.assertIsNone(sending.step())
        self.assertFalse(shared.get_distribution_ready())

        # Advance past the cooldown — gate opens.
        sending.start_time = time.time() - (CHUTE_SETTLE_MS / 1000.0) - 0.3
        next_state = sending.step()
        self.assertEqual(DistributionState.IDLE, next_state)
        self.assertTrue(shared.get_distribution_ready())

    def test_settle_timer_still_required_before_commit(self) -> None:
        transport = self._mkTransportWithDrop(tracked_global_id=3)
        shared = self._mkSharedWithTransport(transport)
        gc = _GlobalConfig()
        event_queue: queue.Queue = queue.Queue()
        # Tracker clear, cooldown zero — the ONLY gate left is the settle
        # timer itself. Sending must not reopen before it elapses even in
        # this "perfect world" scenario.
        vision = _FakeVision(live_ids_by_role={"carousel": set()})

        sending = _mkSending(
            vision=vision,
            cooldown_s=0.0,
            shared=shared,
            event_queue=event_queue,
            gc=gc,
        )
        self.assertIsNone(sending.step())

        # Still within settle window — piece must not commit yet.
        self.assertFalse(shared.get_distribution_ready())
        self.assertEqual([], gc.run_recorder.pieces)  # commit guard holds

        # Jump past the settle timer — now it commits AND reopens.
        sending.start_time = time.time() - (CHUTE_SETTLE_MS / 1000.0) - 0.01
        next_state = sending.step()
        self.assertEqual(DistributionState.IDLE, next_state)
        self.assertTrue(shared.get_distribution_ready())

    def test_sample_collection_reopens_after_short_passthrough_settle(self) -> None:
        transport = self._mkTransportWithDrop(tracked_global_id=24)
        shared = self._mkSharedWithTransport(transport)
        shared.sample_collection_mode = True
        gc = _GlobalConfig()
        event_queue: queue.Queue = queue.Queue()
        vision = _FakeVision(live_ids_by_role={"carousel": {24}})

        sending = _mkSending(
            vision=vision,
            cooldown_s=5.0,
            shared=shared,
            event_queue=event_queue,
            gc=gc,
        )
        self.assertIsNone(sending.step())

        sending.start_time = (
            time.time() - (SAMPLE_COLLECTION_CHUTE_SETTLE_MS / 1000.0) - 0.01
        )

        next_state = sending.step()
        self.assertEqual(DistributionState.IDLE, next_state)
        self.assertTrue(shared.get_distribution_ready())

    def test_missing_drop_piece_reopens_after_grace(self) -> None:
        transport = ClassificationChannelTransport()
        shared = self._mkSharedWithTransport(transport)
        gc = _GlobalConfig()
        event_queue: queue.Queue = queue.Queue()
        sending = _mkSending(
            vision=_FakeVision(),
            cooldown_s=0.0,
            shared=shared,
            event_queue=event_queue,
            gc=gc,
        )

        self.assertIsNone(sending.step())
        sending.start_time = (
            time.time() - (MISSING_DROP_PIECE_GRACE_MS / 1000.0) - 0.01
        )

        next_state = sending.step()
        self.assertEqual(DistributionState.IDLE, next_state)
        self.assertTrue(shared.get_distribution_ready())

    def test_live_track_timeout_publishes_incident_until_cleared(self) -> None:
        transport = self._mkTransportWithDrop(tracked_global_id=77)
        shared = self._mkSharedWithTransport(transport)
        gc = _GlobalConfig()
        event_queue: queue.Queue = queue.Queue()
        vision = _FakeVision(live_ids_by_role={"carousel": {77}})
        sending = _mkSending(
            vision=vision,
            cooldown_s=0.0,
            shared=shared,
            event_queue=event_queue,
            gc=gc,
        )

        def publish_incident(gc_arg, *, piece, reason: str) -> bool:
            gc_arg.runtime_stats.setActiveIncident(
                {
                    "kind": "classification_track_lost",
                    "piece_uuid": piece.uuid,
                    "reason": reason,
                }
            )
            return True

        self.assertIsNone(sending.step())
        sending.start_time = (
            time.time()
            - (max(CHUTE_SETTLE_MS, PIECE_EXIT_INCIDENT_MS) / 1000.0)
            - 0.01
        )
        with patch(
            "subsystems.distribution.sending.publish_classification_track_lost_incident",
            side_effect=publish_incident,
        ):
            self.assertIsNone(sending.step())

        self.assertFalse(shared.get_distribution_ready())
        active = gc.runtime_stats.activeIncident()
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual("classification_track_lost", active["kind"])

        gc.runtime_stats.clearActiveIncident(
            kind="classification_track_lost",
            piece_uuid=transport._exit_piece.uuid,  # noqa: SLF001
        )
        next_state = sending.step()
        self.assertEqual(DistributionState.IDLE, next_state)
        self.assertTrue(shared.get_distribution_ready())
        self.assertEqual([77], vision.force_killed)

    def test_live_track_timeout_reopens_when_incident_disabled(self) -> None:
        transport = self._mkTransportWithDrop(tracked_global_id=88)
        shared = self._mkSharedWithTransport(transport)
        gc = _GlobalConfig()
        event_queue: queue.Queue = queue.Queue()
        vision = _FakeVision(live_ids_by_role={"carousel": {88}})
        sending = _mkSending(
            vision=vision,
            cooldown_s=0.0,
            shared=shared,
            event_queue=event_queue,
            gc=gc,
        )

        self.assertIsNone(sending.step())
        sending.start_time = (
            time.time()
            - (max(CHUTE_SETTLE_MS, PIECE_EXIT_INCIDENT_MS) / 1000.0)
            - 0.01
        )
        with patch(
            "subsystems.distribution.sending.publish_classification_track_lost_incident",
            return_value=False,
        ):
            next_state = sending.step()

        self.assertEqual(DistributionState.IDLE, next_state)
        self.assertTrue(shared.get_distribution_ready())
        self.assertEqual([88], vision.force_killed)


if __name__ == "__main__":
    unittest.main()
