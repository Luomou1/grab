from __future__ import annotations

import threading
from types import SimpleNamespace

from grab_app.camera.controller import CameraController


class _CameraTimeout(Exception):
    def __init__(self) -> None:
        super().__init__("CameraGetImageBuffer timeout")
        self.error_code = -12


def _camera() -> CameraController:
    camera = CameraController.__new__(CameraController)
    camera._mvsdk = SimpleNamespace(CAMERA_STATUS_TIME_OUT=-12)
    camera._frame_lock = threading.Lock()
    camera.current_trigger_mode = 0
    camera.timeout_count = 0
    camera.trigger_wait_count = 0
    camera.sdk_error_count = 0
    camera.last_error = ""
    return camera


def test_trigger_wait_timeout_is_not_reported_as_camera_fault() -> None:
    camera = _camera()
    camera.current_trigger_mode = 1

    camera._record_grab_exception(_CameraTimeout())

    assert camera.trigger_wait_count == 1
    assert camera.timeout_count == 0
    assert camera.sdk_error_count == 0
    assert camera.last_error == ""


def test_continuous_mode_timeout_remains_visible_and_resettable() -> None:
    camera = _camera()

    camera._record_grab_exception(_CameraTimeout())

    assert camera.timeout_count == 1
    assert camera.sdk_error_count == 1
    assert "timeout" in camera.last_error
    camera.reset_timeout_counters()
    assert camera.timeout_count == 0
    assert camera.trigger_wait_count == 0
    assert camera.last_error == ""
