from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np

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


def _frame_info(capture_count: int) -> SimpleNamespace:
    return SimpleNamespace(
        frame=np.full((2, 3), capture_count, dtype=np.uint8),
        capture_count=capture_count,
        captured_at=time.time(),
        is_trigger_frame=False,
        sdk_timestamp_01ms=1000 + capture_count,
        frame_exposure_us=100,
        frame_gain_x=1.0,
        frame_gamma=100,
        frame_contrast=100,
    )


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


def test_continuous_fresh_sample_requires_discarded_frame_after_barrier() -> None:
    camera = _camera()
    camera.capture_count = 6
    camera._latest_info = _frame_info(6)
    camera._safe_get_exposure = lambda: 100.0
    camera._safe_get_gain_x = lambda: 1.0

    assert camera.wait_for_fresh_sample(5, discard_frames=1, timeout_ms=1) is None

    camera.capture_count = 7
    camera._latest_info = _frame_info(7)
    sample = camera.wait_for_fresh_sample(5, discard_frames=1, timeout_ms=1)

    assert sample is not None
    assert sample.capture_count == 7
    assert sample.sdk_timestamp_01ms == 1007
    assert not sample.is_trigger_frame


def test_soft_trigger_clears_sdk_buffer_before_triggering() -> None:
    camera = _camera()
    camera._control_lock = threading.RLock()
    camera.capture_count = 0
    camera._latest_info = None
    camera.h_camera = object()
    camera._safe_get_exposure = lambda: 100.0
    camera._safe_get_gain_x = lambda: 1.0
    calls: list[str] = []

    def clear_buffer(_handle) -> int:
        calls.append("clear")
        return 0

    def soft_trigger(_handle) -> None:
        calls.append("trigger")
        camera.capture_count = 1
        camera._latest_info = _frame_info(1)
        camera._latest_info.is_trigger_frame = True

    camera._mvsdk.CameraClearBuffer = clear_buffer
    camera._mvsdk.CameraSoftTrigger = soft_trigger

    sample = camera.soft_trigger_and_grab_sample(10)

    assert sample is not None
    assert sample.capture_count == 1
    assert calls == ["clear", "trigger"]
