from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from defs.known_object import ClassificationStatus, KnownObject
from irl.config import ClassificationChannelConfig
from subsystems.classification_channel.running import Running
from subsystems.classification_channel.zone_manager import TrackAngularExtent


@pytest.fixture(autouse=True)
def _isolated_machine_params(monkeypatch, tmp_path):
    monkeypatch.setenv("MACHINE_SPECIFIC_PARAMS_PATH", str(tmp_path / "machine_params.toml"))


def _classification_channel_zone_payload(
    *,
    drop_start: float = 100.0,
    drop_end: float = 140.0,
) -> dict[str, object]:
    return {
        "resolution": [400, 400],
        "polygons": {
            "classification_channel": [[30, 30], [370, 30], [370, 370], [30, 370]],
        },
        "channel_angles": {"classification_channel": 0.0},
        "arc_params": {
            "classification_channel": {
                "center": [200, 200],
                "inner_radius": 70,
                "outer_radius": 170,
                "resolution": [400, 400],
                "drop_zone": {
                    "start_outer_angle": drop_start,
                    "end_outer_angle": drop_end,
                    "start_inner_angle": drop_start,
                    "end_inner_angle": drop_end,
                },
                "exit_zone": {
                    "start_outer_angle": 320,
                    "end_outer_angle": 350,
                    "start_inner_angle": 320,
                    "end_inner_angle": 350,
                },
            },
        },
    }


class _Logger:
    def debug(self, *args, **kwargs) -> None:
        pass

    def info(self, *args, **kwargs) -> None:
        pass

    def warning(self, *args, **kwargs) -> None:
        pass

    def error(self, *args, **kwargs) -> None:
        pass


class _RuntimeStats:
    def __init__(self) -> None:
        self.leader_wins_events: list[dict[str, object]] = []
        self.recognizer_counts: dict[str, int] = {}
        self.active_incident: dict[str, object] | None = None

    def observeStateTransition(self, *args, **kwargs) -> None:
        pass

    def observeBlockedReason(self, *args, **kwargs) -> None:
        pass

    def observeMultiDropLeaderWins(self, **meta) -> None:
        self.leader_wins_events.append(dict(meta))

    def observeRecognizerCounter(self, name: str) -> None:
        self.recognizer_counts[name] = self.recognizer_counts.get(name, 0) + 1

    def setActiveIncident(self, incident: dict[str, object]) -> None:
        self.active_incident = dict(incident)

    def clearActiveIncident(self, **kwargs) -> None:
        self.active_incident = None


class _Stepper:
    def __init__(self) -> None:
        self.stopped = True
        self.moves: list[float] = []
        self.speed_limits: list[tuple[int, int]] = []
        self.accelerations: list[int] = []

    def degrees_for_microsteps(self, steps: int) -> float:
        return float(steps) / 10.0

    def move_degrees(self, degrees: float) -> bool:
        self.moves.append(float(degrees))
        self.stopped = True
        return True

    def set_speed_limits(self, microsteps: int, speed: int) -> None:
        self.speed_limits.append((int(microsteps), int(speed)))

    def set_acceleration(self, acceleration: int) -> None:
        self.accelerations.append(int(acceleration))

    def estimateMoveDegreesMs(self, degrees: float) -> float:
        return 123.0


class _Transport:
    def __init__(self) -> None:
        self.zone_manager = object()
        self._pieces_by_track: dict[int, SimpleNamespace] = {}
        self.register_calls: list[int | None] = []
        self._pending = False

    def pieceForTrack(self, track_global_id: int):
        return self._pieces_by_track.get(int(track_global_id))

    def activePieces(self):
        return list(self._pieces_by_track.values())

    def registerIncomingPiece(self, *, tracked_global_id: int | None = None):
        piece = KnownObject(
            uuid=f"piece-{tracked_global_id}",
            tracked_global_id=tracked_global_id,
            classification_status=ClassificationStatus.pending,
        )
        if tracked_global_id is not None:
            self._pieces_by_track[int(tracked_global_id)] = piece
        self.register_calls.append(tracked_global_id)
        return piece

    def updateTrackedPieces(self, track_extents):
        return [], []

    def advanceTransport(self, dropped_uuid: str | None = None):
        dropped_piece = None
        if dropped_uuid is not None:
            for track_id, piece in list(self._pieces_by_track.items()):
                if getattr(piece, "uuid", None) == dropped_uuid:
                    dropped_piece = piece
                    del self._pieces_by_track[track_id]
                    break
        return SimpleNamespace(piece_for_distribution_drop=dropped_piece)

    def isPendingClassification(self, uuid: str) -> bool:
        return False

    def hasPendingClassifications(self) -> bool:
        return self._pending

    def resolveFallbackClassification(
        self,
        uuid: str,
        *,
        status: ClassificationStatus,
    ) -> bool:
        for piece in self._pieces_by_track.values():
            if getattr(piece, "uuid", None) != uuid:
                continue
            piece.part_id = None
            piece.destination_bin = None
            piece.confidence = None
            piece.classification_status = status
            return True
        return False


class _Shared:
    def __init__(self) -> None:
        self.classification_gate_calls: list[tuple[bool, str | None]] = []
        self.distribution_ready = True
        self.sample_collection_mode = False
        self._ignored_classification_dropzone_track_ids: set[int] = set()

    def set_classification_gate(self, open: bool, reason: str | None = None) -> None:
        self.classification_gate_calls.append((bool(open), reason))

    def set_distribution_gate(self, open: bool, reason: str | None = None) -> None:
        pass

    def publish_piece_delivered(self, *args, **kwargs) -> None:
        pass

    def publish_piece_request(self, *args, **kwargs) -> None:
        pass

    def set_classification_dropzone_track_ignored(self, global_id: int, ignored: bool) -> None:
        if ignored:
            self._ignored_classification_dropzone_track_ids.add(int(global_id))
        else:
            self._ignored_classification_dropzone_track_ids.discard(int(global_id))

    def ignored_classification_dropzone_track_ids(self) -> set[int]:
        return set(self._ignored_classification_dropzone_track_ids)


class _EventQueue:
    def __init__(self) -> None:
        self.items: list[object] = []

    def put(self, item) -> None:
        self.items.append(item)


class _Vision:
    def __init__(self) -> None:
        self.teacher_capture_calls: list[dict[str, object]] = []
        self.empty_state_calls = 0
        self.latest_crop_by_id: dict[int, dict[str, object]] = {}
        self.c3_exit_occupied = False

    def scheduleClassificationChannelTeacherCaptureAfterMove(self, **kwargs) -> None:
        self.teacher_capture_calls.append(dict(kwargs))

    def saveClassificationChannelEmptyStateCapture(self) -> bool:
        self.empty_state_calls += 1
        return True

    def getLatestFeederTrackPieceCrop(self, global_id: int):
        return self.latest_crop_by_id.get(int(global_id))

    def feederRoleExitOccupied(self, role: str) -> bool:
        return role == "c_channel_3" and self.c3_exit_occupied


def _make_running() -> tuple[Running, _Transport, _Shared, _EventQueue]:
    transport = _Transport()
    shared = _Shared()
    event_queue = _EventQueue()
    stepper = _Stepper()
    running = Running(
        irl=SimpleNamespace(carousel_stepper=stepper),
        irl_config=SimpleNamespace(
            classification_channel_config=SimpleNamespace(
                intake_angle_deg=0.0,
                intake_body_half_width_deg=10.0,
                intake_guard_deg=28.0,
                drop_angle_deg=30.0,
                drop_tolerance_deg=14.0,
                point_of_no_return_deg=18.0,
                recognition_window_deg=60.0,
                max_zones=4,
                hood_dwell_ms=1200,
                min_carousel_crops_for_recognize=0,
                min_carousel_dwell_ms=0,
                min_carousel_traversal_deg=0.0,
                exit_release_overlap_ratio=0.5,
                exit_release_shimmy_amplitude_deg=1.5,
                exit_release_shimmy_cycles=2,
                exit_release_shimmy_microsteps_per_second=4200,
                exit_release_shimmy_acceleration_microsteps_per_second_sq=9000,
            )
            ,
            feeder_config=SimpleNamespace(
                classification_channel_eject=SimpleNamespace(
                    steps_per_pulse=90,
                    microsteps_per_second=3400,
                    acceleration_microsteps_per_second_sq=2500,
                )
            ),
        ),
        gc=SimpleNamespace(
            logger=_Logger(),
            runtime_stats=_RuntimeStats(),
        ),
        shared=shared,
        transport=transport,
        vision=None,
        event_queue=event_queue,
    )
    return running, transport, shared, event_queue


def _force_manual_exit_incident(running: Running) -> None:
    running._exitReleaseIncidentAutomatic = lambda: False
    running._exitReleaseIncidentOff = lambda: False


def test_unknown_fallback_publishes_classification_unresolved_incident() -> None:
    running, transport, _shared, _events = _make_running()
    piece = KnownObject(
        uuid="piece-deadline",
        tracked_global_id=81,
        classification_status=ClassificationStatus.pending,
    )
    piece.classification_channel_zone_center_deg = 29.5
    piece.classification_channel_exit_offset_deg = -0.5
    transport._pieces_by_track = {81: piece}

    running._applyFallback(
        piece,
        ClassificationStatus.unknown,
        now_wall=100.0,
        reason="drop_deadline_unclassified",
    )

    incident = running.gc.runtime_stats.active_incident
    assert incident is not None
    assert incident["kind"] == "classification_unresolved"
    assert incident["piece_uuid"] == "piece-deadline"
    assert incident["channel"] == "c4"
    assert incident["reason"] == "drop_deadline_unclassified"


def test_multi_drop_fallback_publishes_collision_incident() -> None:
    running, transport, _shared, _events = _make_running()
    piece = KnownObject(
        uuid="piece-collision",
        tracked_global_id=82,
        classification_status=ClassificationStatus.classified,
    )
    piece.classification_channel_zone_center_deg = 30.0
    transport._pieces_by_track = {82: piece}

    running._applyFallback(
        piece,
        ClassificationStatus.multi_drop_fail,
        now_wall=101.0,
        reason="drop_window_collision",
    )

    incident = running.gc.runtime_stats.active_incident
    assert incident is not None
    assert incident["kind"] == "classification_multi_drop_collision"
    assert incident["piece_uuid"] == "piece-collision"
    assert incident["severity"] == "critical"
    assert incident["reason"] == "drop_window_collision"


def test_running_registers_new_intake_piece_only_while_awaiting_handoff() -> None:
    running, transport, shared, _events = _make_running()
    track = TrackAngularExtent(
        global_id=41,
        center_deg=2.0,
        half_width_deg=6.0,
        last_seen_ts=1.0,
        hit_count=3,
    )

    running._registerNewIntakePiece([track], now_wall=10.0, now_mono=20.0)

    assert transport.register_calls == []
    assert shared.classification_gate_calls == []


def test_running_requires_minimum_track_hits_before_registering_new_piece() -> None:
    running, transport, shared, _events = _make_running()
    running._awaiting_intake_piece = True
    running._intake_requested_at_mono = 19.0
    running._intake_requested_at_wall = 9.8
    weak_track = TrackAngularExtent(
        global_id=41,
        center_deg=1.5,
        half_width_deg=6.0,
        last_seen_ts=1.0,
        hit_count=1,
    )

    running._registerNewIntakePiece([weak_track], now_wall=10.0, now_mono=20.0)

    assert transport.register_calls == []
    assert running._awaiting_intake_piece is True
    assert shared.classification_gate_calls == []


def test_running_registers_piece_from_confirmed_track_when_awaiting_handoff() -> None:
    running, transport, shared, events = _make_running()
    running._awaiting_intake_piece = True
    running._intake_requested_at_mono = 19.0
    running._intake_requested_at_wall = 9.8
    strong_track = TrackAngularExtent(
        global_id=41,
        center_deg=1.5,
        half_width_deg=6.0,
        last_seen_ts=10.0,
        hit_count=3,
        first_seen_ts=9.9,
    )

    with patch("blob_manager.getChannelPolygons", return_value={}):
        running._registerNewIntakePiece([strong_track], now_wall=10.0, now_mono=20.0)

    assert transport.register_calls == [41]
    assert running._awaiting_intake_piece is False
    assert running._intake_requested_at_mono is None
    assert running._intake_requested_at_wall is None
    assert shared.classification_gate_calls[-1] == (False, "piece_in_hood")
    assert len(events.items) == 1


def test_running_defers_c4_intake_while_c3_exit_still_occupied() -> None:
    running, transport, shared, _events = _make_running()
    vision = _Vision()
    vision.c3_exit_occupied = True
    running.vision = vision
    running._awaiting_intake_piece = True
    running._intake_requested_at_mono = 19.0
    running._intake_requested_at_wall = 9.8
    track = TrackAngularExtent(
        global_id=41,
        center_deg=1.5,
        half_width_deg=6.0,
        last_seen_ts=10.0,
        hit_count=3,
        first_seen_ts=9.9,
    )

    running._registerNewIntakePiece([track], now_wall=10.0, now_mono=20.0)

    assert transport.register_calls == []
    assert running._awaiting_intake_piece is True
    assert shared.classification_gate_calls[-1] == (False, "awaiting_upstream_exit_clear")

    vision.c3_exit_occupied = False
    with patch("blob_manager.getChannelPolygons", return_value={}):
        running._registerNewIntakePiece([track], now_wall=10.1, now_mono=20.1)

    assert transport.register_calls == [41]
    assert running._awaiting_intake_piece is False


def test_running_filters_ignored_c4_dropzone_track_extents() -> None:
    running, _transport, shared, _events = _make_running()
    shared.set_classification_dropzone_track_ignored(42, True)
    kept = TrackAngularExtent(
        global_id=41,
        center_deg=1.5,
        half_width_deg=6.0,
        last_seen_ts=10.0,
        hit_count=3,
    )
    ignored = TrackAngularExtent(
        global_id=42,
        center_deg=2.5,
        half_width_deg=6.0,
        last_seen_ts=10.0,
        hit_count=3,
    )

    result = running._filterIgnoredDropzoneTrackExtents([kept, ignored])

    assert result == [kept]


def test_running_ignores_stale_track_that_predates_handoff_request() -> None:
    running, transport, shared, _events = _make_running()
    running._awaiting_intake_piece = True
    running._intake_requested_at_mono = 19.0
    running._intake_requested_at_wall = 10.0
    stale_track = TrackAngularExtent(
        global_id=41,
        center_deg=1.5,
        half_width_deg=6.0,
        last_seen_ts=10.5,
        hit_count=8,
        first_seen_ts=7.5,
    )

    running._registerNewIntakePiece([stale_track], now_wall=11.0, now_mono=20.0)

    assert transport.register_calls == []
    assert running._awaiting_intake_piece is True
    assert shared.classification_gate_calls == []


def test_intake_request_timeout_publishes_incident_and_keeps_gate_closed() -> None:
    running, _transport, shared, _events = _make_running()
    running._awaiting_intake_piece = True
    running._intake_requested_at_mono = 10.0
    running._intake_requested_at_wall = 100.0

    running._updateIntakeGate(now_mono=13.0)

    incident = running.gc.runtime_stats.active_incident
    assert incident is not None
    assert incident["kind"] == "classification_intake_request_timeout"
    assert incident["channel"] == "c4"
    assert incident["timeout_ms"] == 3000
    assert running._awaiting_intake_piece is False
    assert shared.classification_gate_calls[-1] == (
        False,
        "intake_request_timeout_incident",
    )


def test_meaningful_stale_track_expiry_publishes_track_lost_incident() -> None:
    running, _transport, _shared, events = _make_running()
    piece = KnownObject(
        uuid="piece-lost",
        tracked_global_id=91,
        classification_status=ClassificationStatus.pending,
    )
    piece.latest_captured_crop = "crop-b64"

    running._emitExpiredPieceEvents([piece])

    incident = running.gc.runtime_stats.active_incident
    assert incident is not None
    assert incident["kind"] == "classification_track_lost"
    assert incident["piece_uuid"] == "piece-lost"
    assert incident["tracked_global_id"] == 91
    assert incident["reason"] == "stale_zone_expired"
    assert len(events.items) == 1


def test_empty_stale_track_expiry_stays_diagnostic_only() -> None:
    running, _transport, _shared, events = _make_running()
    piece = KnownObject(
        uuid="piece-ghost",
        tracked_global_id=92,
        classification_status=ClassificationStatus.pending,
    )

    running._emitExpiredPieceEvents([piece])

    assert running.gc.runtime_stats.active_incident is None
    assert events.items == []


def test_running_recovers_existing_tracks_without_waiting_for_new_handoff() -> None:
    running, transport, shared, events = _make_running()
    old_track = TrackAngularExtent(
        global_id=52,
        center_deg=146.0,
        half_width_deg=8.0,
        last_seen_ts=20.0,
        hit_count=6,
        first_seen_ts=10.0,
    )

    running._recoverExistingTrackedPieces([old_track], now_wall=20.0, now_mono=20.0)

    assert transport.register_calls == [52]
    assert len(transport.activePieces()) == 1
    assert shared.classification_gate_calls[-1] == (False, "recover_existing_piece")
    assert len(events.items) == 1


def test_running_fires_recognition_for_oldest_pending_piece() -> None:
    running, transport, _shared, _events = _make_running()
    younger_piece = KnownObject(
        uuid="piece-younger",
        tracked_global_id=41,
        classification_status=ClassificationStatus.pending,
        created_at=0.0,
        carousel_detected_confirmed_at=5.0,
    )
    younger_piece.classification_channel_zone_center_deg = 180.0
    older_piece = KnownObject(
        uuid="piece-older",
        tracked_global_id=42,
        classification_status=ClassificationStatus.pending,
        created_at=0.0,
        carousel_detected_confirmed_at=1.0,
    )
    older_piece.classification_channel_zone_center_deg = 42.0
    transport._pieces_by_track = {41: younger_piece, 42: older_piece}
    fired: list[str] = []
    running._recognizer = SimpleNamespace(
        fire=lambda piece: fired.append(piece.uuid) or True
    )

    running._fireRecognition(now_wall=10.0)

    assert fired == ["piece-older"]
    assert older_piece.carousel_snapping_started_at == 10.0
    assert older_piece.carousel_snapping_completed_at == 10.0
    assert younger_piece.carousel_snapping_started_at is None


def test_running_refreshes_latest_captured_crop_from_tracker() -> None:
    running, transport, _shared, events = _make_running()
    vision = _Vision()
    vision.latest_crop_by_id[41] = {
        "jpeg_b64": "crop-b64",
        "captured_ts": 12.5,
        "source_role": "carousel",
    }
    running.vision = vision
    piece = KnownObject(
        uuid="piece-crop",
        tracked_global_id=41,
        classification_status=ClassificationStatus.pending,
    )
    transport._pieces_by_track = {41: piece}

    changed = running._refreshLatestCapturedCrop(piece, now_wall=13.0, emit=True)

    assert changed is True
    assert piece.latest_captured_crop == "crop-b64"
    assert piece.latest_captured_crop_ts == 12.5
    assert events.items[-1].data.latest_captured_crop == "crop-b64"


def test_exit_release_candidate_includes_piece_after_center_crosses_exit_line() -> None:
    running, transport, _shared, _events = _make_running()
    running._config.exit_release_overlap_ratio = 0.95
    piece = KnownObject(
        uuid="piece-stuck",
        tracked_global_id=55,
        classification_status=ClassificationStatus.unknown,
    )
    piece.classification_channel_zone_center_deg = 51.5
    piece.classification_channel_zone_half_width_deg = 4.0
    transport._pieces_by_track = {55: piece}

    assert running._pickExitReleaseCandidate() == "piece-stuck"
    assert running._startExitReleaseShimmyIfNeeded("piece-stuck") is True
    assert [round(move, 3) for move in running.irl.carousel_stepper.moves] == [2.708]


def _make_running_with_carousel_gate(
    *,
    min_carousel_crops_for_recognize: int,
    min_carousel_dwell_ms: int,
    min_carousel_traversal_deg: float = 0.0,
    vision=None,
) -> tuple[Running, _Transport, _Shared, _EventQueue]:
    transport = _Transport()
    shared = _Shared()
    event_queue = _EventQueue()
    stepper = _Stepper()
    running = Running(
        irl=SimpleNamespace(carousel_stepper=stepper),
        irl_config=SimpleNamespace(
            classification_channel_config=SimpleNamespace(
                intake_angle_deg=0.0,
                intake_body_half_width_deg=10.0,
                intake_guard_deg=28.0,
                drop_angle_deg=30.0,
                drop_tolerance_deg=14.0,
                point_of_no_return_deg=18.0,
                recognition_window_deg=60.0,
                max_zones=4,
                hood_dwell_ms=1200,
                min_carousel_crops_for_recognize=min_carousel_crops_for_recognize,
                min_carousel_dwell_ms=min_carousel_dwell_ms,
                min_carousel_traversal_deg=min_carousel_traversal_deg,
                exit_release_overlap_ratio=0.5,
                exit_release_shimmy_amplitude_deg=1.5,
                exit_release_shimmy_cycles=2,
                exit_release_shimmy_microsteps_per_second=4200,
                exit_release_shimmy_acceleration_microsteps_per_second_sq=9000,
            ),
            feeder_config=SimpleNamespace(
                classification_channel_eject=SimpleNamespace(
                    steps_per_pulse=90,
                    microsteps_per_second=3400,
                    acceleration_microsteps_per_second_sq=2500,
                )
            ),
        ),
        gc=SimpleNamespace(logger=_Logger(), runtime_stats=_RuntimeStats()),
        shared=shared,
        transport=transport,
        vision=vision,
        event_queue=event_queue,
    )
    return running, transport, shared, event_queue


def _make_pending_piece(
    *,
    uuid: str = "piece-carousel",
    tracked_global_id: int = 77,
    carousel_confirmed_at: float = 1.0,
    first_carousel_seen_ts: float | None = None,
    first_carousel_seen_angle_deg: float | None = None,
    current_zone_center_deg: float | None = None,
) -> KnownObject:
    piece = KnownObject(
        uuid=uuid,
        tracked_global_id=tracked_global_id,
        classification_status=ClassificationStatus.pending,
        created_at=0.0,
        carousel_detected_confirmed_at=carousel_confirmed_at,
    )
    piece.first_carousel_seen_ts = first_carousel_seen_ts
    piece.first_carousel_seen_angle_deg = first_carousel_seen_angle_deg
    piece.classification_channel_zone_center_deg = current_zone_center_deg
    return piece


class _StubRecognizer:
    def __init__(self, carousel_crop_count: int, fire_result: bool = True) -> None:
        self._carousel_crop_count = int(carousel_crop_count)
        self._fire_result = bool(fire_result)
        self.fire_calls: list[str] = []

    def countCarouselCrops(self, piece) -> int:
        return self._carousel_crop_count

    def fire(self, piece) -> bool:
        self.fire_calls.append(piece.uuid)
        return self._fire_result


def test_running_skips_recognition_when_carousel_crop_quota_unmet() -> None:
    running, transport, _shared, _events = _make_running_with_carousel_gate(
        min_carousel_crops_for_recognize=2,
        min_carousel_dwell_ms=0,
    )
    piece = _make_pending_piece(first_carousel_seen_ts=1.0)
    transport._pieces_by_track = {piece.tracked_global_id: piece}
    recognizer = _StubRecognizer(carousel_crop_count=1)
    running._recognizer = recognizer

    running._fireRecognition(now_wall=10.0)

    assert recognizer.fire_calls == []
    counts = running.gc.runtime_stats.recognizer_counts
    assert counts.get("recognize_skipped_carousel_quota") == 1


def test_running_skips_recognition_when_carousel_dwell_not_elapsed() -> None:
    running, transport, _shared, _events = _make_running_with_carousel_gate(
        min_carousel_crops_for_recognize=0,
        min_carousel_dwell_ms=500,
    )
    piece = _make_pending_piece(first_carousel_seen_ts=9.9)  # 100ms ago
    transport._pieces_by_track = {piece.tracked_global_id: piece}
    recognizer = _StubRecognizer(carousel_crop_count=4)
    running._recognizer = recognizer

    running._fireRecognition(now_wall=10.0)

    assert recognizer.fire_calls == []
    counts = running.gc.runtime_stats.recognizer_counts
    assert counts.get("recognize_skipped_carousel_dwell") == 1


def test_running_skips_recognition_when_piece_still_alive_on_c3() -> None:
    class _Vision:
        def __init__(self) -> None:
            self._live = {
                "c_channel_3": {77},
                "carousel": set(),
            }

        def getFeederTrackerLiveGlobalIds(self, role: str) -> set[int]:
            return set(self._live.get(role, set()))

    vision = _Vision()
    running, transport, _shared, _events = _make_running_with_carousel_gate(
        min_carousel_crops_for_recognize=0,
        min_carousel_dwell_ms=0,
        vision=vision,
    )
    piece = _make_pending_piece(first_carousel_seen_ts=1.0)
    transport._pieces_by_track = {piece.tracked_global_id: piece}
    recognizer = _StubRecognizer(carousel_crop_count=3)
    running._recognizer = recognizer

    running._fireRecognition(now_wall=10.0)

    assert recognizer.fire_calls == []
    counts = running.gc.runtime_stats.recognizer_counts
    assert counts.get("recognize_skipped_not_on_carousel") == 1


def test_running_skips_recognition_when_carousel_traversal_unmet() -> None:
    running, transport, _shared, _events = _make_running_with_carousel_gate(
        min_carousel_crops_for_recognize=0,
        min_carousel_dwell_ms=0,
        min_carousel_traversal_deg=60.0,
    )
    piece = _make_pending_piece(
        first_carousel_seen_ts=1.0,
        first_carousel_seen_angle_deg=100.0,
        current_zone_center_deg=120.0,  # only 20 deg of traversal
    )
    transport._pieces_by_track = {piece.tracked_global_id: piece}
    recognizer = _StubRecognizer(carousel_crop_count=4)
    running._recognizer = recognizer

    running._fireRecognition(now_wall=10.0)

    assert recognizer.fire_calls == []
    counts = running.gc.runtime_stats.recognizer_counts
    assert counts.get("recognize_skipped_carousel_traversal") == 1


def test_running_fires_recognition_when_carousel_traversal_sufficient() -> None:
    running, transport, _shared, _events = _make_running_with_carousel_gate(
        min_carousel_crops_for_recognize=0,
        min_carousel_dwell_ms=0,
        min_carousel_traversal_deg=60.0,
    )
    piece = _make_pending_piece(
        first_carousel_seen_ts=1.0,
        first_carousel_seen_angle_deg=100.0,
        current_zone_center_deg=175.0,  # 75 deg of traversal
    )
    transport._pieces_by_track = {piece.tracked_global_id: piece}
    recognizer = _StubRecognizer(carousel_crop_count=4)
    running._recognizer = recognizer

    running._fireRecognition(now_wall=10.0)

    assert recognizer.fire_calls == [piece.uuid]
    counts = running.gc.runtime_stats.recognizer_counts
    assert counts.get("recognize_skipped_carousel_traversal") is None


def test_running_skips_traversal_gate_when_angle_unavailable() -> None:
    # Graceful-degradation case: no first-seen angle -> gate does not
    # block, bumping no traversal counter. Other gates still apply.
    running, transport, _shared, _events = _make_running_with_carousel_gate(
        min_carousel_crops_for_recognize=0,
        min_carousel_dwell_ms=0,
        min_carousel_traversal_deg=60.0,
    )
    piece = _make_pending_piece(
        first_carousel_seen_ts=1.0,
        first_carousel_seen_angle_deg=None,
        current_zone_center_deg=175.0,
    )
    transport._pieces_by_track = {piece.tracked_global_id: piece}
    recognizer = _StubRecognizer(carousel_crop_count=4)
    running._recognizer = recognizer

    running._fireRecognition(now_wall=10.0)

    assert recognizer.fire_calls == [piece.uuid]
    counts = running.gc.runtime_stats.recognizer_counts
    assert counts.get("recognize_skipped_carousel_traversal") is None


def test_running_fires_recognition_when_carousel_gate_clears() -> None:
    class _Vision:
        def getFeederTrackerLiveGlobalIds(self, role: str) -> set[int]:
            # c_channel_3 track has died; carousel is live -> handoff done.
            return {77} if role == "carousel" else set()

    running, transport, _shared, _events = _make_running_with_carousel_gate(
        min_carousel_crops_for_recognize=2,
        min_carousel_dwell_ms=300,
        vision=_Vision(),
    )
    piece = _make_pending_piece(first_carousel_seen_ts=9.0)  # 1s of dwell
    transport._pieces_by_track = {piece.tracked_global_id: piece}
    recognizer = _StubRecognizer(carousel_crop_count=2)
    running._recognizer = recognizer

    running._fireRecognition(now_wall=10.0)

    assert recognizer.fire_calls == [piece.uuid]
    assert piece.carousel_snapping_completed_at == 10.0


def test_drop_body_overlap_ratio_is_high_when_piece_is_mostly_in_drop_window() -> None:
    running, _transport, _shared, _events = _make_running()
    piece = KnownObject()
    piece.classification_channel_zone_center_deg = 34.0
    piece.classification_channel_zone_half_width_deg = 12.0

    ratio = running._dropBodyOverlapRatio(piece)

    assert ratio > 0.5


def test_start_exit_release_shimmy_builds_escalating_release_stage() -> None:
    running, transport, _shared, _events = _make_running()
    piece = KnownObject(
        uuid="piece-drop",
        tracked_global_id=99,
        classification_status=ClassificationStatus.classified,
    )
    piece.classification_channel_zone_center_deg = 30.0
    piece.classification_channel_zone_half_width_deg = 12.0
    transport._pieces_by_track = {99: piece}

    started = running._startExitReleaseShimmyIfNeeded(piece.uuid)

    assert started is True
    assert [round(move, 3) for move in running.irl.carousel_stepper.moves[:1]] == [2.708]
    assert running.irl.carousel_stepper.speed_limits[:1] == [(16, 700)]
    assert running.irl.carousel_stepper.accelerations[:1] == [1800]
    assert running._exit_release_drop_uuid == piece.uuid
    assert [round(stroke.move_deg, 3) for stroke in running._exit_release_plan] == [
        -5.417,
        2.708,
        2.708,
        -5.417,
        2.708,
    ]
    assert {stroke.speed for stroke in running._exit_release_plan} == {700}
    assert {stroke.acceleration for stroke in running._exit_release_plan} == {1800}


def test_exit_release_repeated_attempts_escalate_stages() -> None:
    running, transport, _shared, _events = _make_running()
    piece = KnownObject(
        uuid="piece-drop",
        tracked_global_id=99,
        classification_status=ClassificationStatus.classified,
    )
    piece.classification_channel_zone_center_deg = 30.0
    piece.classification_channel_zone_half_width_deg = 12.0
    transport._pieces_by_track = {99: piece}

    assert running._startExitReleaseShimmyIfNeeded(piece.uuid) is True
    running._exit_release_drop_uuid = None
    running._exit_release_plan = []

    assert running._startExitReleaseShimmyIfNeeded(piece.uuid) is True

    assert [round(move, 3) for move in running.irl.carousel_stepper.moves] == [2.708, 5.417]
    assert running.irl.carousel_stepper.speed_limits[-1] == (16, 950)
    assert running.irl.carousel_stepper.accelerations[-1] == 2600


def test_exit_release_review_pause_waits_for_operator_before_release() -> None:
    running, transport, shared, _events = _make_running()
    _force_manual_exit_incident(running)
    running._config.exit_release_review_pause_enabled = True
    piece = KnownObject(
        uuid="piece-stuck",
        tracked_global_id=99,
        classification_status=ClassificationStatus.unknown,
    )
    piece.classification_channel_zone_center_deg = 51.5
    piece.classification_channel_zone_half_width_deg = 4.0
    transport._pieces_by_track = {99: piece}

    started = running._startExitReleaseShimmyIfNeeded(piece.uuid)

    assert started is True
    assert running.irl.carousel_stepper.moves == []
    assert running._exit_release_drop_uuid is None
    assert running.gc.runtime_stats.active_incident is not None
    assert running.gc.runtime_stats.active_incident["kind"] == "exit_stuck"
    assert running.gc.runtime_stats.active_incident["source_kind"] == "classification_exit_release"
    assert running.gc.runtime_stats.active_incident["status"] == "waiting_for_operator"
    assert running.gc.runtime_stats.active_incident["piece_uuid"] == piece.uuid
    assert shared.classification_gate_calls[-1] == (False, "exit_incident_review")

    approved = running.approveExitReleaseIncident(piece.uuid)
    assert approved["status"] == "approved"

    started_after_approval = running._startExitReleaseShimmyIfNeeded(piece.uuid)

    assert started_after_approval is True
    assert [round(move, 3) for move in running.irl.carousel_stepper.moves] == [2.708]
    assert running._exit_release_drop_uuid == piece.uuid
    assert running.gc.runtime_stats.active_incident is not None
    assert running.gc.runtime_stats.active_incident["status"] == "running"


def test_exit_release_review_blocks_normal_c4_tracking_until_operator_action() -> None:
    running, transport, shared, _events = _make_running()
    _force_manual_exit_incident(running)
    running._config.exit_release_review_pause_enabled = True
    piece = KnownObject(
        uuid="piece-stuck",
        tracked_global_id=99,
        classification_status=ClassificationStatus.unknown,
    )
    piece.classification_channel_zone_center_deg = 51.5
    piece.classification_channel_zone_half_width_deg = 4.0
    transport._pieces_by_track = {99: piece}
    assert running._startExitReleaseShimmyIfNeeded(piece.uuid) is True

    class _VisionThatMustNotPoll:
        def getFeederTrackAngularExtents(self, *args, **kwargs):
            raise AssertionError("normal C4 tracking should be paused during exit incident")

    running.vision = _VisionThatMustNotPoll()

    running.step()

    assert running.irl.carousel_stepper.moves == []
    assert running._exit_release_review is not None
    assert running.gc.runtime_stats.active_incident is not None
    assert running.gc.runtime_stats.active_incident["status"] == "waiting_for_operator"
    assert shared.classification_gate_calls[-1] == (False, "exit_incident_review")


def test_manual_exit_release_test_uses_slider_values_without_clearing_incident() -> None:
    running, transport, _shared, _events = _make_running()
    _force_manual_exit_incident(running)
    running._config.exit_release_review_pause_enabled = True
    piece = KnownObject(
        uuid="piece-stuck",
        tracked_global_id=99,
        classification_status=ClassificationStatus.unknown,
    )
    piece.classification_channel_zone_center_deg = 51.5
    piece.classification_channel_zone_half_width_deg = 4.0
    transport._pieces_by_track = {99: piece}
    assert running._startExitReleaseShimmyIfNeeded(piece.uuid) is True

    result = running.testExitReleaseIncident(
        piece_uuid=piece.uuid,
        amplitude_output_deg=3.0,
        microsteps_per_second=16000,
        cycles=3,
        acceleration_microsteps_per_second_sq=32000,
    )

    assert result["amplitude_output_deg"] == 3.0
    assert result["cycles"] == 3
    assert round(result["first_stroke_stepper_deg"], 3) == 32.5
    assert result["microsteps_per_second"] == 16000
    assert result["stroke_count"] == 9
    assert running.gc.runtime_stats.active_incident is not None
    assert running.gc.runtime_stats.active_incident["status"] == "manual_test_running"
    assert [round(stroke.move_deg, 3) for stroke in running._exit_release_plan[:3]] == [
        32.5,
        -65.0,
        32.5,
    ]

    assert running._advanceExitReleaseShimmy() is True

    assert [round(move, 3) for move in running.irl.carousel_stepper.moves] == [32.5]
    assert running.irl.carousel_stepper.speed_limits[-1] == (16, 16000)
    assert result["acceleration_microsteps_per_second_sq"] == 32000
    assert running.irl.carousel_stepper.accelerations[-1] == 32000
    assert running._exit_release_review is not None


def test_normal_drop_path_can_start_release_without_operator_review() -> None:
    running, transport, _shared, _events = _make_running()
    running._config.exit_release_review_pause_enabled = True
    piece = KnownObject(
        uuid="piece-drop",
        tracked_global_id=99,
        classification_status=ClassificationStatus.classified,
    )
    piece.classification_channel_zone_center_deg = 30.0
    piece.classification_channel_zone_half_width_deg = 12.0
    transport._pieces_by_track = {99: piece}

    started = running._startExitReleaseShimmyIfNeeded(
        piece.uuid,
        review_allowed=False,
    )

    assert started is True
    assert [round(move, 3) for move in running.irl.carousel_stepper.moves] == [2.708]
    assert running.gc.runtime_stats.active_incident is None


def test_recovered_track_after_failed_drop_requires_exit_incident_review() -> None:
    running, transport, shared, _events = _make_running()
    _force_manual_exit_incident(running)
    running._config.exit_release_review_pause_enabled = True
    running._last_drop_pulse_completed_mono = 100.0
    running._last_drop_pulse_completed_wall = 1000.0
    running._last_drop_pulse_piece_uuid = "previous-drop"
    running._last_drop_pulse_exit_attempt_count = 1
    recovered_track = TrackAngularExtent(
        global_id=6254,
        center_deg=31.0,
        half_width_deg=7.0,
        last_seen_ts=1001.0,
        hit_count=6,
        first_seen_ts=1000.2,
    )

    running._recoverExistingTrackedPieces(
        [recovered_track],
        now_wall=1001.0,
        now_mono=101.0,
    )
    piece = transport.pieceForTrack(6254)
    assert piece is not None
    piece.classification_status = ClassificationStatus.unknown

    started = running._startExitReleaseShimmyIfNeeded(
        piece.uuid,
        review_allowed=False,
    )

    assert started is True
    assert running.irl.carousel_stepper.moves == []
    assert piece.uuid in running._exit_release_review_required_uuids
    assert running.gc.runtime_stats.active_incident is not None
    assert running.gc.runtime_stats.active_incident["kind"] == "exit_stuck"
    assert running.gc.runtime_stats.active_incident["piece_uuid"] == piece.uuid
    assert running.gc.runtime_stats.active_incident["stage_number"] == 2
    assert shared.classification_gate_calls[-1] == (False, "exit_incident_review")


def test_old_recovered_track_does_not_require_exit_incident_review() -> None:
    running, transport, _shared, _events = _make_running()
    _force_manual_exit_incident(running)
    running._config.exit_release_review_pause_enabled = True
    running._last_drop_pulse_completed_mono = 100.0
    running._last_drop_pulse_completed_wall = 1000.0
    old_track = TrackAngularExtent(
        global_id=6255,
        center_deg=31.0,
        half_width_deg=7.0,
        last_seen_ts=1001.0,
        hit_count=6,
        first_seen_ts=998.0,
    )

    running._recoverExistingTrackedPieces(
        [old_track],
        now_wall=1001.0,
        now_mono=101.0,
    )
    piece = transport.pieceForTrack(6255)
    assert piece is not None
    piece.classification_status = ClassificationStatus.unknown

    started = running._startExitReleaseShimmyIfNeeded(
        piece.uuid,
        review_allowed=False,
    )

    assert started is True
    assert [round(move, 3) for move in running.irl.carousel_stepper.moves] == [2.708]
    assert running.gc.runtime_stats.active_incident is None


def test_exit_release_incident_clears_when_released_piece_drops() -> None:
    running, transport, _shared, _events = _make_running()
    piece = KnownObject(
        uuid="piece-drop",
        tracked_global_id=99,
        classification_status=ClassificationStatus.classified,
    )
    transport._pieces_by_track = {99: piece}
    running.gc.runtime_stats.setActiveIncident(
        {
            "kind": "exit_stuck",
            "source_kind": "classification_exit_release",
            "piece_uuid": piece.uuid,
            "status": "running",
        }
    )
    running._pending_drop_uuid = piece.uuid
    running._pulse_in_flight = True

    running._finalizePulse(now_mono=123.0)

    assert running.gc.runtime_stats.active_incident is None
    assert running._pulse_in_flight is False
    assert running._pending_drop_uuid is None


def test_sample_collection_mode_skips_exit_release_shimmy() -> None:
    running, transport, shared, _events = _make_running()
    shared.sample_collection_mode = True
    piece = KnownObject(
        uuid="piece-drop",
        tracked_global_id=99,
        classification_status=ClassificationStatus.classified,
    )
    piece.classification_channel_zone_center_deg = 30.0
    piece.classification_channel_zone_half_width_deg = 12.0
    transport._pieces_by_track = {99: piece}

    started = running._startExitReleaseShimmyIfNeeded(piece.uuid)

    assert started is False
    assert running.irl.carousel_stepper.moves == []
    assert running._exit_release_drop_uuid is None
    assert running._exit_release_plan == []


def test_sample_collection_mode_aborts_existing_exit_release_plan() -> None:
    running, _transport, shared, _events = _make_running()
    shared.sample_collection_mode = True
    running._exit_release_drop_uuid = "piece-drop"
    running._exit_release_plan = [object()]

    advanced = running._advanceExitReleaseShimmy()

    assert advanced is False
    assert running.irl.carousel_stepper.moves == []
    assert running._exit_release_drop_uuid == "piece-drop"
    assert running._exit_release_plan == []


def test_c4_teacher_capture_is_queued_after_pulse_when_sample_mode_is_enabled() -> None:
    running, _transport, shared, _events = _make_running()
    vision = _Vision()
    running.vision = vision
    shared.sample_collection_mode = True

    sent = running._sendPulse(None)

    assert sent is True
    assert len(vision.teacher_capture_calls) == 1
    assert vision.teacher_capture_calls[0]["move_label"] == "sample_c4_pulse"
    assert vision.teacher_capture_calls[0]["pulse_degrees"] == 9.0
    assert vision.teacher_capture_calls[0]["delay_s"] == 0.123


def test_c4_teacher_capture_is_queued_after_pulse_in_normal_mode() -> None:
    running, _transport, _shared, _events = _make_running()
    vision = _Vision()
    running.vision = vision

    sent = running._sendPulse(None)

    assert sent is True
    assert len(vision.teacher_capture_calls) == 1
    assert vision.teacher_capture_calls[0]["move_label"] == "sample_c4_pulse"


def test_sample_collection_mode_archives_empty_state_when_c4_is_empty() -> None:
    running, _transport, shared, _events = _make_running()
    vision = _Vision()
    running.vision = vision
    shared.sample_collection_mode = True

    running._maybeCaptureSampleModeEmptyState([], [], now_mono=10.0)
    running._maybeCaptureSampleModeEmptyState([], [], now_mono=11.0)
    running._maybeCaptureSampleModeEmptyState([], [], now_mono=16.0)

    assert vision.empty_state_calls == 2


def test_sample_collection_mode_does_not_archive_empty_state_with_tracks() -> None:
    running, _transport, shared, _events = _make_running()
    vision = _Vision()
    running.vision = vision
    shared.sample_collection_mode = True
    track = TrackAngularExtent(
        global_id=41,
        center_deg=2.0,
        half_width_deg=6.0,
        last_seen_ts=1.0,
        hit_count=3,
    )

    running._maybeCaptureSampleModeEmptyState([track], [], now_mono=10.0)

    assert vision.empty_state_calls == 0


# ---------------------------------------------------------------------------
# _updateIntakeGate / _isDropCommitted — production-geometry intake-flow tests
# ---------------------------------------------------------------------------


class _RecordingZoneManager:
    """Minimal stub: records the ignore_piece_uuids the call site passes
    in. Reports clear/blocked per a caller-set flag. Mirrors
    ``ExclusionZoneManager.is_arc_clear`` shape without doing real
    polygon math.
    """

    def __init__(self, *, clear: bool = True) -> None:
        self._clear = clear
        self.calls: list[dict] = []

    def is_arc_clear(
        self,
        *,
        center_deg: float,
        body_half_width_deg: float,
        hard_guard_deg: float,
        ignore_piece_uuid: str | None = None,
        ignore_piece_uuids: set | None = None,
    ) -> bool:
        self.calls.append(
            {
                "center_deg": center_deg,
                "body_half_width_deg": body_half_width_deg,
                "hard_guard_deg": hard_guard_deg,
                "ignore_piece_uuids": set(ignore_piece_uuids or set()),
            }
        )
        return self._clear


def _make_production_geometry_running() -> tuple[Running, _Transport, _Shared]:
    """Mirror the real C4 geometry so intake-gate tests exercise the
    production slot count, intake guard, and drop approach window."""
    transport = _Transport()
    shared = _Shared()
    running = Running(
        irl=SimpleNamespace(carousel_stepper=_Stepper()),
        irl_config=SimpleNamespace(
            classification_channel_config=SimpleNamespace(
                intake_angle_deg=305.0,
                intake_body_half_width_deg=10.0,
                intake_guard_deg=0.0,
                intake_registration_window_deg=46.0,
                drop_angle_deg=30.0,
                drop_tolerance_deg=14.0,
                point_of_no_return_deg=18.0,
                recognition_window_deg=60.0,
                positioning_window_deg=48.0,
                max_zones=4,
                hood_dwell_ms=1200,
                min_carousel_crops_for_recognize=0,
                min_carousel_dwell_ms=0,
                min_carousel_traversal_deg=0.0,
                exit_release_overlap_ratio=0.5,
                exit_release_shimmy_amplitude_deg=1.5,
                exit_release_shimmy_cycles=2,
                exit_release_shimmy_microsteps_per_second=4200,
                exit_release_shimmy_acceleration_microsteps_per_second_sq=9000,
            ),
            feeder_config=SimpleNamespace(
                classification_channel_eject=SimpleNamespace(
                    steps_per_pulse=90,
                    microsteps_per_second=3400,
                    acceleration_microsteps_per_second_sq=2500,
                )
            ),
        ),
        gc=SimpleNamespace(logger=_Logger(), runtime_stats=_RuntimeStats()),
        shared=shared,
        transport=transport,
        vision=None,
        event_queue=_EventQueue(),
    )
    return running, transport, shared


def test_production_c4_defaults_keep_four_piece_pipeline_open() -> None:
    config = ClassificationChannelConfig()

    assert config.max_zones == 4
    assert config.intake_guard_deg == 0.0
    assert config.intake_registration_window_deg == 46.0


def test_c4_intake_window_comes_from_saved_dropzone() -> None:
    running, _transport, _shared = _make_production_geometry_running()

    with patch(
        "blob_manager.getChannelPolygons",
        return_value=_classification_channel_zone_payload(drop_start=100.0, drop_end=140.0),
    ):
        center, half_width = running._classificationDropzoneWindow()

    assert center == 120.0
    assert half_width == 20.0


def test_is_drop_committed_true_when_piece_past_point_of_no_return() -> None:
    running, _t, _s = _make_production_geometry_running()
    piece = KnownObject(uuid="committed", tracked_global_id=1)
    # drop_angle = 30°, PoNR = 18°. center=20° → diff=-10°, within PoNR.
    piece.classification_channel_zone_center_deg = 20.0

    assert running._isDropCommitted(piece) is True


def test_is_drop_committed_false_when_piece_still_approaching() -> None:
    running, _t, _s = _make_production_geometry_running()
    piece = KnownObject(uuid="approaching", tracked_global_id=2)
    # 30° drop − 30° = 0° (in absolute). Diff = -30°, well outside PoNR=18°.
    piece.classification_channel_zone_center_deg = 0.0

    assert running._isDropCommitted(piece) is False


def test_intake_gate_opens_when_only_piece_is_drop_committed() -> None:
    """A piece sitting at drop (about to fall) does not consume an intake
    admission slot.
    """
    running, transport, shared = _make_production_geometry_running()
    zone_manager = _RecordingZoneManager(clear=True)
    transport.zone_manager = zone_manager

    piece = KnownObject(uuid="committed", tracked_global_id=1)
    piece.classification_channel_zone_center_deg = 30.0  # exactly at drop
    transport._pieces_by_track[1] = piece

    running._updateIntakeGate(now_mono=100.0)

    # Gate opened (True) — the committed piece did NOT block intake.
    last_call = shared.classification_gate_calls[-1]
    assert last_call == (True, None), f"expected gate open, got {last_call}"
    assert zone_manager.calls
    assert "committed" in zone_manager.calls[-1]["ignore_piece_uuids"]
    assert zone_manager.calls[-1]["hard_guard_deg"] == 0.0


def test_intake_gate_checks_saved_dropzone_not_fixed_intake_angle() -> None:
    running, transport, shared = _make_production_geometry_running()
    zone_manager = _RecordingZoneManager(clear=True)
    transport.zone_manager = zone_manager

    with patch(
        "blob_manager.getChannelPolygons",
        return_value=_classification_channel_zone_payload(drop_start=100.0, drop_end=140.0),
    ):
        running._updateIntakeGate(now_mono=100.0)

    last_call = shared.classification_gate_calls[-1]
    assert last_call == (True, None)
    assert zone_manager.calls[-1]["center_deg"] == 120.0
    assert zone_manager.calls[-1]["body_half_width_deg"] == 20.0
    assert zone_manager.calls[-1]["hard_guard_deg"] == 0.0


def test_registers_new_intake_piece_from_saved_dropzone_window() -> None:
    running, transport, shared = _make_production_geometry_running()
    running._awaiting_intake_piece = True
    running._intake_requested_at_mono = 99.0
    running._intake_requested_at_wall = 9.8

    track = TrackAngularExtent(
        global_id=41,
        center_deg=120.0,
        half_width_deg=6.0,
        last_seen_ts=10.0,
        hit_count=3,
        first_seen_ts=9.9,
    )

    with patch(
        "blob_manager.getChannelPolygons",
        return_value=_classification_channel_zone_payload(drop_start=100.0, drop_end=140.0),
    ):
        running._registerNewIntakePiece([track], now_wall=10.0, now_mono=100.0)

    assert transport.register_calls == [41]
    assert running._awaiting_intake_piece is False
    assert shared.classification_gate_calls[-1] == (False, "piece_in_hood")


def test_intake_gate_stays_closed_when_approaching_piece_is_not_yet_committed() -> None:
    """A piece in the approach window but BEFORE PoNR still holds intake
    — the platter is mid-maneuver toward drop and we don't want a new
    piece arriving in that window."""
    running, transport, shared = _make_production_geometry_running()
    transport.zone_manager = _RecordingZoneManager(clear=True)

    piece = KnownObject(uuid="approaching", tracked_global_id=1)
    # 30° drop − 22° approaching → center=8°. Inside the short approach
    # window (22°) but outside PoNR (18°).
    piece.classification_channel_zone_center_deg = 8.0
    transport._pieces_by_track[1] = piece

    running._updateIntakeGate(now_mono=100.0)

    last_call = shared.classification_gate_calls[-1]
    assert last_call == (False, "drop_approach_busy")


def test_intake_gate_opens_with_three_resident_pieces() -> None:
    """C4 should request another piece while three non-overlapping pieces
    are already resident, targeting the four-piece pipeline.
    """
    running, transport, shared = _make_production_geometry_running()
    transport.zone_manager = _RecordingZoneManager(clear=True)

    for idx, center in enumerate((60.0, 150.0, 235.0), start=1):
        piece = KnownObject(uuid=f"resident-{idx}", tracked_global_id=idx)
        piece.classification_channel_zone_center_deg = center
        transport._pieces_by_track[idx] = piece

    running._updateIntakeGate(now_mono=100.0)

    last_call = shared.classification_gate_calls[-1]
    assert last_call == (True, None)


def test_intake_gate_closes_at_four_resident_pieces() -> None:
    running, transport, shared = _make_production_geometry_running()
    transport.zone_manager = _RecordingZoneManager(clear=True)

    for idx, center in enumerate((60.0, 150.0, 235.0, 330.0), start=1):
        piece = KnownObject(uuid=f"resident-{idx}", tracked_global_id=idx)
        piece.classification_channel_zone_center_deg = center
        transport._pieces_by_track[idx] = piece

    running._updateIntakeGate(now_mono=100.0)

    last_call = shared.classification_gate_calls[-1]
    assert last_call == (False, "intake_blocked")


def test_intake_gate_max_zones_excludes_drop_committed_pieces() -> None:
    """Drop-committed pieces count as leaving, so they do not consume one of
    the four resident C4 pipeline slots.
    """
    running, transport, shared = _make_production_geometry_running()
    transport.zone_manager = _RecordingZoneManager(clear=True)

    committed = KnownObject(uuid="committed", tracked_global_id=1)
    committed.classification_channel_zone_center_deg = 30.0  # drop-committed
    transport._pieces_by_track[1] = committed

    resident = KnownObject(uuid="resident", tracked_global_id=2)
    # Far from drop and from intake — solidly mid-platter.
    resident.classification_channel_zone_center_deg = 150.0
    transport._pieces_by_track[2] = resident

    running._updateIntakeGate(now_mono=100.0)

    last_call = shared.classification_gate_calls[-1]
    assert last_call == (True, None)
