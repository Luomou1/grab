from __future__ import annotations

import numpy as np
import pytest

from grab_app.spatial import (
    SpatialCalibration,
    StagePoint,
    default_calibration,
    fit_affine_calibration,
    pixel_to_stage,
    stage_to_pixel,
    validate_calibration_quality,
)


def test_default_calibration_uses_048_um_per_pixel_and_round_trips() -> None:
    calibration = default_calibration()

    pixel = stage_to_pixel(calibration, StagePoint(0.48, 0.24))
    assert pixel == pytest.approx((1000.0, 500.0))
    stage = pixel_to_stage(calibration, pixel)
    assert isinstance(stage, StagePoint)
    assert stage.as_tuple() == pytest.approx((0.48, 0.24))
    assert stage_to_pixel(calibration, np.array([0.48, 0.24])) == pytest.approx((1000, 500))


def test_affine_fit_recovers_rotation_offset_and_reports_residuals() -> None:
    stage = np.array([
        [0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [1.0, 1.0], [0.3, 0.7],
    ])
    matrix = np.array([[2010.0, -45.0], [31.0, 2075.0]])
    offset = np.array([123.0, -17.0])
    pixels = stage @ matrix.T + offset

    fit = fit_affine_calibration(stage.tolist(), pixels.tolist())

    assert np.asarray(fit.calibration.matrix) == pytest.approx(matrix)
    assert fit.calibration.offset == pytest.approx(offset)
    assert fit.rms_px < 1e-9
    assert fit.residuals_px == pytest.approx([0.0] * len(stage), abs=1e-9)
    transformed = stage_to_pixel(fit.calibration, stage.tolist())
    assert transformed == pytest.approx(pixels)


def test_affine_fit_rejects_collinear_points() -> None:
    with pytest.raises(ValueError, match="共线"):
        fit_affine_calibration([(0, 0), (1, 1), (2, 2)], [(0, 0), (2, 2), (4, 4)])


def test_affine_fit_rejects_noninvertible_pixel_mapping() -> None:
    with pytest.raises(ValueError, match="不可逆"):
        fit_affine_calibration(
            [(0, 0), (0.2, 0), (0, 0.2)],
            [(0, 0), (400, 0), (800, 0)],
        )


def test_affine_fit_rejects_too_small_calibration_motion_without_replacing_default() -> None:
    current = default_calibration()
    stage = np.array([[0.0, 0.0], [0.001, 0.0], [0.0, 0.001]])
    pixels = np.asarray(stage_to_pixel(current, stage.tolist()))

    with pytest.raises(ValueError, match="位移不足"):
        fit_affine_calibration(stage.tolist(), pixels.tolist())

    assert current.pixel_size_um == pytest.approx(0.48)
    assert np.asarray(current.matrix) == pytest.approx(np.eye(2) * 1000 / 0.48)


def test_affine_fit_rejects_scale_far_from_default_even_when_motion_exceeds_threshold() -> None:
    stage = [(0.0, 0.0), (0.2, 0.0), (0.0, 0.2)]
    pixels = [(0.0, 0.0), (25.0, 0.0), (0.0, 25.0)]

    with pytest.raises(ValueError, match="尺度异常"):
        fit_affine_calibration(stage, pixels)


def test_affine_fit_rejects_large_residuals() -> None:
    stage = np.array([
        [0.0, 0.0], [0.2, 0.0], [0.0, 0.2], [0.2, 0.2], [0.1, 0.1],
    ])
    pixels = stage * (1000 / 0.48)
    pixels[-1] += (30.0, -30.0)

    with pytest.raises(ValueError, match="残差过大"):
        fit_affine_calibration(stage.tolist(), pixels.tolist())


def test_quality_gate_rejects_nearly_singular_matrix() -> None:
    calibration = SpatialCalibration(((1000 / 0.48, 0.0), (0.0, 1e-9)))

    with pytest.raises(ValueError, match="尺度异常|数值不稳定"):
        validate_calibration_quality(calibration)
