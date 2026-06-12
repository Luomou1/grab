from __future__ import annotations

from grab_app.camera.controller import CameraController


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

    assert roi == (100, 50, 332, 220)
