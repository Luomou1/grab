from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from grab_app.camera.controller import CameraController, CameraError


def test_normalize_sensor_roi_aligns_and_clamps_to_sensor_bounds() -> None:
    camera = CameraController.__new__(CameraController)

    roi = camera._normalize_sensor_roi(
        x=1279,
        y=1023,
        width=99,
        height=99,
        min_width=16,
        min_height=16,
        max_width=1280,
        max_height=1024,
    )

    assert roi == (1264, 1008, 16, 16)


def test_normalize_sensor_roi_preserves_valid_even_region() -> None:
    camera = CameraController.__new__(CameraController)

    roi = camera._normalize_sensor_roi(
        x=101,
        y=51,
        width=333,
        height=221,
        min_width=16,
        min_height=16,
        max_width=1280,
        max_height=1024,
    )

    assert roi == (96, 48, 328, 216)


def test_normalize_sensor_roi_handles_zero_minimum_limits() -> None:
    camera = CameraController.__new__(CameraController)

    roi = camera._normalize_sensor_roi(
        x=1,
        y=1,
        width=1,
        height=1,
        min_width=0,
        min_height=0,
        max_width=1280,
        max_height=1024,
    )

    assert roi == (0, 0, 8, 8)


def test_normalize_sensor_roi_rejects_invalid_sensor_bounds() -> None:
    camera = CameraController.__new__(CameraController)

    with pytest.raises(CameraError, match="ROI 最大范围无效"):
        camera._normalize_sensor_roi(
            x=0,
            y=0,
            width=16,
            height=16,
            min_width=0,
            min_height=0,
            max_width=0,
            max_height=1024,
        )


def test_set_sensor_roi_converts_display_y_to_sensor_y() -> None:
    class FakeMvsdk:
        def __init__(self) -> None:
            self.resolution_args: tuple[object, ...] | None = None

        def CameraSetImageResolutionEx(self, *args: object) -> int:
            self.resolution_args = args
            return 0

        def CameraStop(self, _h_camera: object) -> None:
            return None

        def CameraPlay(self, _h_camera: object) -> None:
            return None

        def CameraClearBuffer(self, _h_camera: object) -> None:
            return None

    camera = CameraController.__new__(CameraController)
    fake_mvsdk = FakeMvsdk()
    camera._mvsdk = fake_mvsdk
    camera.h_camera = object()
    camera.cap = SimpleNamespace(
        sResolutionRange=SimpleNamespace(iWidthMin=16, iHeightMin=16, iWidthMax=1280, iHeightMax=1024)
    )
    camera._control_lock = threading.RLock()
    camera._frame_lock = threading.Lock()
    camera.latest_frame = object()
    camera._latest_info = object()
    camera.latest_frame_time = 1.0

    roi = camera.set_sensor_roi(496, 216, 688, 688)

    assert roi == (496, 216, 688, 688)
    assert fake_mvsdk.resolution_args == (camera.h_camera, 0xFF, 0, 0, 496, 120, 688, 688, 0, 0)


def test_sensor_roi_y_mapping_round_trips_display_coordinates() -> None:
    sensor_roi = CameraController._display_roi_to_sensor_roi(
        x=296,
        y=240,
        width=696,
        height=320,
        _max_width=1280,
        max_height=1024,
    )

    assert sensor_roi == (296, 464, 696, 320)

    display_x, display_y = CameraController._sensor_roi_to_display_roi(
        sensor_x=sensor_roi[0],
        sensor_y=sensor_roi[1],
        width=sensor_roi[2],
        height=sensor_roi[3],
        _max_width=1280,
        max_height=1024,
    )

    assert (display_x, display_y) == (296, 240)
