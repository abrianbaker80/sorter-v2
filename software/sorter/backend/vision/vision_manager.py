from typing import Any, Optional

from blob_manager import VideoRecorder
from global_config import GlobalConfig
from irl.config import CameraPictureSettings
from .camera import CaptureThread
from .camera_service import CameraService
from .overlays.telemetry import TelemetryOverlay
from .types import CameraFrame


class VisionManager:
    def __init__(self, gc: GlobalConfig, camera_service: CameraService):
        self.gc = gc
        self._camera_service = camera_service
        self._video_recorder = VideoRecorder() if gc.should_write_camera_feeds else None

    def start(self) -> None:
        for role, feed in self._camera_service.feeds.items():
            def statsForCamera(camera_role=role):
                capture = self.getCaptureThreadForRole(camera_role)
                return capture.getTelemetrySnapshot() if capture is not None else None

            feed.clear_overlays()
            feed.add_overlay(TelemetryOverlay(statsForCamera))

    def stop(self) -> None:
        if self._video_recorder:
            self._video_recorder.close()

    def reloadPolygons(self) -> None:
        perception = self.gc.perception_service
        if perception is not None:
            perception.request_reconcile()

    def recordFrames(self) -> None:
        if self._video_recorder is None:
            return
        for camera in self._camera_service.active_cameras:
            frame = self.getFrame(camera.value)
            if frame is not None:
                self._video_recorder.writeFrame(camera.value, frame.raw, frame.annotated)

    def getCaptureThreadForRole(self, camera_name: str) -> Optional[CaptureThread]:
        return self._camera_service.get_capture_thread_for_role(camera_name)

    def setCameraSourceForRole(
        self,
        camera_name: str,
        source: int | str | None,
    ) -> bool:
        return self._camera_service.set_camera_source_for_role(camera_name, source)

    def setPictureSettingsForRole(
        self,
        camera_name: str,
        settings: CameraPictureSettings,
    ) -> bool:
        return self._camera_service.set_picture_settings_for_role(camera_name, settings)

    def setDeviceSettingsForRole(
        self,
        camera_name: str,
        settings: dict[str, int | float | bool] | None,
        *,
        persist: bool = False,
    ) -> dict[str, int | float | bool] | None:
        return self._camera_service.set_device_settings_for_role(camera_name, settings, persist=persist)

    def getDeviceSettingsForRole(
        self,
        camera_name: str,
    ) -> dict[str, int | float | bool] | None:
        return self._camera_service.get_device_settings_for_role(camera_name)

    def describeDeviceControlsForRole(
        self,
        camera_name: str,
    ) -> tuple[list[dict[str, Any]], dict[str, int | float | bool]] | None:
        return self._camera_service.describe_device_controls_for_role(camera_name)

    def getFrame(self, camera_name: str) -> Optional[CameraFrame]:
        feed = self._camera_service.get_feed(camera_name)
        return feed.get_frame(annotated=True) if feed else None
