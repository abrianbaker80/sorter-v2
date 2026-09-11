import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from server import shared_state
from server.routers import detection


class _FakeVisionManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def debugFeederDetection(self, role: str, *, include_capture: bool = False):
        self.calls.append((role, include_capture))
        return {
            "camera": role,
            "algorithm": "gemini_sam",
            "found": False,
            "message": "No piece in frame.",
            "frame_resolution": [1280, 720],
            "candidate_bboxes": [],
            "bbox_count": 0,
            "bbox": None,
            "zone_bbox": None,
        }

    def getFeederOpenRouterModel(self) -> str:
        return "google/gemini-3-flash-preview"


def _synthetic_rotor_frame(phase_deg: float = 22.0) -> np.ndarray:
    image = np.zeros((720, 720, 3), dtype=np.uint8)
    center = (360, 360)
    cv2.circle(image, center, 330, (210, 210, 210), -1)
    cv2.circle(image, center, 125, (0, 0, 0), -1)
    for i in range(5):
        angle = np.deg2rad(phase_deg + i * 72.0)
        inner = (
            int(round(center[0] + np.cos(angle) * 130)),
            int(round(center[1] + np.sin(angle) * 130)),
        )
        outer = (
            int(round(center[0] + np.cos(angle) * 300)),
            int(round(center[1] + np.sin(angle) * 300)),
        )
        cv2.line(image, inner, outer, (105, 105, 105), 10, cv2.LINE_AA)
        cv2.line(image, inner, outer, (245, 245, 245), 3, cv2.LINE_AA)
    cv2.rectangle(image, (330, 360), (395, 720), (0, 0, 0), -1)
    return image


class DetectionRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_vision_manager = shared_state.vision_manager
        self._old_controller_ref = shared_state.controller_ref
        self._old_gc_ref = shared_state.gc_ref

    def tearDown(self) -> None:
        shared_state.vision_manager = self._old_vision_manager
        shared_state.controller_ref = self._old_controller_ref
        shared_state.gc_ref = self._old_gc_ref

    def test_track_lost_clear_queues_state_recovery_before_incident_clear(self) -> None:
        events: list[tuple[str, object]] = []

        class RuntimeStats:
            def __init__(self) -> None:
                self.active = {
                    "kind": "classification_track_lost",
                    "piece_uuid": "piece-1",
                }

            def activeIncident(self):
                return dict(self.active) if self.active is not None else None

            def clearActiveIncident(self, **kwargs) -> None:
                events.append(("clear", kwargs))
                self.active = None

        class Classification:
            def requestTrackLostRecovery(self, piece_uuid: str | None) -> bool:
                events.append(("request", piece_uuid))
                return True

        stats = RuntimeStats()
        shared_state.gc_ref = SimpleNamespace(runtime_stats=stats)
        shared_state.controller_ref = SimpleNamespace(
            coordinator=SimpleNamespace(classification=Classification())
        )

        payload = detection.classification_channel_fallback_incident_clear(
            detection.ClassificationExitIncidentActionPayload(piece_uuid="piece-1")
        )

        self.assertTrue(payload["cleared"])
        self.assertTrue(payload["recovery_requested"])
        self.assertEqual("request", events[0][0])
        self.assertEqual("clear", events[1][0])

    def test_track_lost_clear_preserves_incident_when_owned_recovery_fails(self) -> None:
        events: list[str] = []

        class RuntimeStats:
            def __init__(self) -> None:
                self.active = {
                    "kind": "classification_track_lost",
                    "piece_uuid": "piece-1",
                }

            def activeIncident(self):
                return dict(self.active)

            def clearActiveIncident(self, **kwargs) -> None:
                events.append("clear")
                self.active = None

        class Classification:
            def ownsTrackLostFault(self, piece_uuid: str | None) -> bool:
                return piece_uuid == "piece-1"

            def requestTrackLostRecovery(self, piece_uuid: str | None) -> bool:
                return False

        stats = RuntimeStats()
        shared_state.gc_ref = SimpleNamespace(runtime_stats=stats)
        shared_state.controller_ref = SimpleNamespace(
            coordinator=SimpleNamespace(classification=Classification())
        )

        with self.assertRaises(HTTPException) as excinfo:
            detection.classification_channel_fallback_incident_clear(
                detection.ClassificationExitIncidentActionPayload(
                    piece_uuid="piece-1"
                )
            )

        self.assertEqual(409, excinfo.exception.status_code)
        self.assertEqual([], events)
        self.assertIsNotNone(stats.active)

    def test_debug_feeder_detection_accepts_classification_channel_role(self) -> None:
        fake_vision = _FakeVisionManager()
        shared_state.vision_manager = fake_vision

        payload = detection.debug_feeder_detection("carousel")

        self.assertTrue(payload["ok"])
        self.assertEqual("carousel", payload["camera"])
        self.assertEqual([("carousel", True)], fake_vision.calls)

    def test_debug_feeder_detection_rejects_unknown_role(self) -> None:
        shared_state.vision_manager = _FakeVisionManager()

        with self.assertRaises(HTTPException) as excinfo:
            detection.debug_feeder_detection("nope")

        self.assertEqual(400, excinfo.exception.status_code)
        self.assertEqual("Unsupported feeder role.", excinfo.exception.detail)

    def test_aux_detection_save_uses_actual_crop_offset_and_c4_source_role(self) -> None:
        saved_calls: list[dict] = []

        class FakeTrainingManager:
            def saveAuxiliaryDetectionCapture(self, **kwargs):
                saved_calls.append(kwargs)
                return {"ok": True}

        class FakeVision:
            def supportsCarouselSampleCollection(self) -> bool:
                return True

            def sampleSourceRoleForRole(self, role: str) -> str:
                return "classification_channel" if role == "carousel" else role

        shared_state.vision_manager = FakeVision()
        sample_capture = {
            "input_image": np.zeros((1080, 1308, 3), dtype=np.uint8),
            "frame": np.zeros((1080, 1920, 3), dtype=np.uint8),
            "crop_offset": (308, 0),
        }
        payload = {
            "camera": "carousel",
            "algorithm": "gemini_sam",
            "frame_resolution": [1920, 1080],
            "zone_bbox": [308, -60, 1616, 1248],
            "found": True,
            "bbox": [914, 0, 1087, 158],
            "candidate_bboxes": [
                [914, 0, 1087, 158],
                [996, 3, 1112, 130],
                [738, 85, 815, 129],
            ],
            "bbox_count": 3,
            "score": 0.99,
            "message": "Cloud vision found candidate pieces.",
        }

        with patch.object(detection, "getClassificationTrainingManager", return_value=FakeTrainingManager()):
            result = detection._finalize_aux_detection_debug_payload(
                role="carousel",
                payload=payload,
                sample_capture=sample_capture,
                openrouter_model="google/gemini-3-flash-preview",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(1, len(saved_calls))
        saved = saved_calls[0]
        self.assertEqual("classification_channel", saved["source_role"])
        self.assertEqual("carousel", saved["detection_scope"])
        self.assertEqual([606, 0, 779, 158], saved["detection_bbox"])
        self.assertEqual(
            [
                [606, 0, 779, 158],
                [688, 3, 804, 130],
                [430, 85, 507, 129],
            ],
            saved["detection_candidate_bboxes"],
        )

    def test_classification_channel_wall_phase_uses_live_frame(self) -> None:
        class FakeVision:
            def getCaptureThreadForRole(self, role: str):
                if role == "carousel":
                    return SimpleNamespace(
                        latest_frame=SimpleNamespace(raw=_synthetic_rotor_frame())
                    )
                return None

        shared_state.vision_manager = FakeVision()

        payload = detection.classification_channel_wall_phase()

        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(payload["wall_count"], 4)
        self.assertAlmostEqual(22.0, payload["sector_offset_deg"], delta=3.0)
        self.assertGreater(payload["frame_luma"]["mean"], 100.0)
        self.assertGreater(payload["frame_luma"]["nonblack_gt25_ratio"], 0.5)

    def test_classification_diagnostics_use_loaded_config_before_controller_exists(self) -> None:
        config = SimpleNamespace(
            classification_channel_config=SimpleNamespace(c4_sector_count=10)
        )
        shared_state.controller_ref = None
        shared_state.vision_manager = SimpleNamespace(_irl_config=config)

        self.assertIs(detection._active_irl_config(), config)

    def test_classification_channel_sector_occupancy_rolls_candidates_into_sectors(self) -> None:
        frame = _synthetic_rotor_frame(phase_deg=22.0)
        center = (360.0, 360.0)

        def bbox_at_angle(angle_deg: float) -> list[int]:
            radians = np.deg2rad(angle_deg)
            cx = center[0] + np.cos(radians) * 160.0
            cy = center[1] + np.sin(radians) * 160.0
            return [
                int(round(cx - 18.0)),
                int(round(cy - 18.0)),
                int(round(cx + 18.0)),
                int(round(cy + 18.0)),
            ]

        candidates = [
            bbox_at_angle(58.0),
            bbox_at_angle(202.0),
        ]

        class FakeVision:
            def __init__(self) -> None:
                self.calls: list[tuple[bool, object]] = []

            def getCaptureThreadForRole(self, role: str):
                if role == "carousel":
                    return SimpleNamespace(latest_frame=SimpleNamespace(raw=frame))
                return None

            def getClassificationChannelDetectionCandidates(self, *, force: bool = False, frame=None):
                self.calls.append((force, frame))
                return candidates

        fake_vision = FakeVision()
        shared_state.vision_manager = fake_vision
        shared_state.controller_ref = SimpleNamespace(
            coordinator=SimpleNamespace(
                irl_config=SimpleNamespace(
                    classification_channel_config=SimpleNamespace(
                        intake_angle_deg=305.0,
                        drop_angle_deg=120.0,
                    ),
                    feeder_config=SimpleNamespace(
                        classification_channel_eject=SimpleNamespace(
                            microsteps_per_second=3400,
                            acceleration_microsteps_per_second_sq=2500,
                        )
                    ),
                    c_channel_4_rotor_stepper=SimpleNamespace(microsteps=8),
                )
            )
        )

        payload = detection.classification_channel_sector_occupancy()

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["phase_ok"])
        self.assertGreater(payload["frame_luma"]["mean"], 100.0)
        self.assertEqual(5, payload["sector_count"])
        self.assertEqual(candidates, payload["candidate_bboxes"])
        self.assertEqual([0, 2], [entry["sector_index"] for entry in payload["detections"]])
        self.assertEqual([0, 2], [
            sector["sector_index"]
            for sector in payload["sectors"]
            if sector["state"] == "occupied"
        ])
        self.assertEqual(1, len(fake_vision.calls))
        self.assertFalse(fake_vision.calls[0][0])
        self.assertIs(fake_vision.calls[0][1].raw, frame)

    def test_classification_channel_sector_occupancy_reports_dark_frame_diagnostics(self) -> None:
        frame = np.zeros((720, 720, 3), dtype=np.uint8)

        class FakeVision:
            def getCaptureThreadForRole(self, role: str):
                if role == "carousel":
                    return SimpleNamespace(latest_frame=SimpleNamespace(raw=frame))
                return None

            def getClassificationChannelDetectionCandidates(self, *, force: bool = False, frame=None):
                raise AssertionError("detection should not run before wall phase succeeds")

        shared_state.vision_manager = FakeVision()

        payload = detection.classification_channel_sector_occupancy()

        self.assertFalse(payload["ok"])
        self.assertIn("disc fit failed", payload["message"])
        self.assertEqual(0, payload["frame_luma"]["max"])
        self.assertEqual(0.0, payload["frame_luma"]["nonblack_gt25_ratio"])
        self.assertEqual([], payload["sectors"])
        self.assertEqual([], payload["candidate_bboxes"])
        self.assertEqual([], payload["detections"])

    def test_classification_channel_sector_occupancy_endpoint_reports_dark_frame_diagnostics(self) -> None:
        frame = np.zeros((720, 720, 3), dtype=np.uint8)

        class FakeVision:
            def getCaptureThreadForRole(self, role: str):
                if role == "carousel":
                    return SimpleNamespace(latest_frame=SimpleNamespace(raw=frame))
                return None

            def getClassificationChannelDetectionCandidates(self, *, force: bool = False, frame=None):
                raise AssertionError("detection should not run before wall phase succeeds")

        shared_state.vision_manager = FakeVision()
        app = FastAPI()
        app.include_router(detection.router)

        with TestClient(app) as client:
            response = client.post("/api/classification-channel/sector-occupancy")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertIn("disc fit failed", payload["message"])
        self.assertEqual(0, payload["frame_luma"]["max"])
        self.assertEqual([], payload["sectors"])


    def test_rebrickable_api_key_save_preserves_other_encrypted_keys(self) -> None:
        stored: list[dict[str, str]] = []
        with (
            patch.object(
                detection,
                "getApiKeys",
                return_value={"openrouter": "openrouter-secret"},
            ),
            patch.object(
                detection,
                "setApiKeys",
                side_effect=lambda value: stored.append(value),
            ),
            patch.dict(os.environ, {}, clear=False),
        ):
            result = detection.save_api_key(
                detection.ApiKeySavePayload(
                    provider="rebrickable", key=" rebrickable-secret "
                )
            )
            activated = os.environ["REBRICKABLE_API_KEY"]

        self.assertTrue(result["ok"])
        self.assertEqual(
            {
                "openrouter": "openrouter-secret",
                "rebrickable": "rebrickable-secret",
            },
            stored[0],
        )
        self.assertEqual("rebrickable-secret", activated)

    def test_api_key_status_masks_both_supported_providers(self) -> None:
        with patch.object(
            detection,
            "getApiKeys",
            return_value={
                "openrouter": "openrouter-secret-1234",
                "rebrickable": "rebrickable-secret-5678",
            },
        ):
            result = detection.get_api_keys()

        self.assertNotIn("openrouter-secret-1234", str(result))
        self.assertNotIn("rebrickable-secret-5678", str(result))
        self.assertIsNotNone(result["keys"]["openrouter"])
        self.assertIsNotNone(result["keys"]["rebrickable"])


if __name__ == "__main__":
    unittest.main()
