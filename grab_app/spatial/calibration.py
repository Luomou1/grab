"""像素与样品坐标的二维仿射标定。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from .models import CalibrationFit, SpatialCalibration, StagePoint

DEFAULT_PIXEL_SIZE_UM = 0.48


@dataclass(frozen=True)
class CalibrationQualityLimits:
    """自动空间标定的质量门限。

    尺度倍率以默认 ``0.48 um/px`` 对应的 ``px/mm`` 为基准；上下限同时
    约束仿射矩阵的两个奇异值，避免面积尺度正常但单轴已经失控。
    """

    min_displacement_px: float = 20.0
    min_scale_ratio: float = 0.25
    max_scale_ratio: float = 4.0
    max_condition_number: float = 20.0
    max_rms_residual_px: float = 5.0
    max_residual_px: float = 10.0

    def __post_init__(self) -> None:
        values = (
            self.min_displacement_px,
            self.min_scale_ratio,
            self.max_scale_ratio,
            self.max_condition_number,
            self.max_rms_residual_px,
            self.max_residual_px,
        )
        if not np.isfinite(values).all() or any(value <= 0 for value in values):
            raise ValueError("标定质量门限必须是有限正数")
        if self.min_scale_ratio >= self.max_scale_ratio:
            raise ValueError("标定尺度倍率下限必须小于上限")
        if self.max_residual_px < self.max_rms_residual_px:
            raise ValueError("单点残差上限不能小于 RMS 残差上限")


DEFAULT_CALIBRATION_QUALITY_LIMITS = CalibrationQualityLimits()


def default_calibration(
    pixel_size_um: float = DEFAULT_PIXEL_SIZE_UM,
    *,
    origin_stage: StagePoint = StagePoint(0.0, 0.0),
    origin_pixel: tuple[float, float] = (0.0, 0.0),
    fingerprint: dict[str, object] | None = None,
) -> SpatialCalibration:
    """创建无旋转、无镜像的初始标定。"""
    if not np.isfinite(pixel_size_um) or pixel_size_um <= 0:
        raise ValueError("pixel_size_um 必须大于 0")
    pixels_per_mm = 1000.0 / float(pixel_size_um)
    matrix = ((pixels_per_mm, 0.0), (0.0, pixels_per_mm))
    offset = (
        float(origin_pixel[0]) - pixels_per_mm * origin_stage.x_mm,
        float(origin_pixel[1]) - pixels_per_mm * origin_stage.y_mm,
    )
    return SpatialCalibration(matrix, offset, float(pixel_size_um), fingerprint or {})


def _points_array(points: Sequence[StagePoint | Sequence[float]], name: str) -> np.ndarray:
    values = [p.as_tuple() if isinstance(p, StagePoint) else tuple(p) for p in points]
    array = np.asarray(values, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != 2 or not np.isfinite(array).all():
        raise ValueError(f"{name} 必须是有限的 N x 2 坐标")
    return array


def _is_coordinate_pair(value: object) -> bool:
    try:
        return np.asarray(value, dtype=np.float64).shape == (2,)
    except (TypeError, ValueError):
        return False


def _effective_motion_distances_px(stage: np.ndarray, pixel: np.ndarray) -> tuple[float, float, float]:
    """返回覆盖样品点二维跨度的三角形边长（像素）。"""
    deltas = stage[:, None, :] - stage[None, :, :]
    squared_distances = np.sum(np.square(deltas), axis=2)
    first, second = np.unravel_index(int(np.argmax(squared_distances)), squared_distances.shape)
    baseline = stage[second] - stage[first]
    areas = np.abs(
        baseline[0] * (stage[:, 1] - stage[first, 1])
        - baseline[1] * (stage[:, 0] - stage[first, 0])
    )
    third = int(np.argmax(areas))
    indices = ((first, second), (first, third), (second, third))
    return tuple(float(np.linalg.norm(pixel[a] - pixel[b])) for a, b in indices)  # type: ignore[return-value]


def validate_calibration_quality(
    calibration: SpatialCalibration,
    *,
    stage_points: Sequence[StagePoint | Sequence[float]] | None = None,
    pixel_points: Sequence[Sequence[float]] | None = None,
    residuals_px: Sequence[float] | None = None,
    rms_px: float | None = None,
    limits: CalibrationQualityLimits = DEFAULT_CALIBRATION_QUALITY_LIMITS,
) -> None:
    """校验标定的有限值、可逆性、尺度、有效位移与拟合残差。

    只传 ``calibration`` 时可用于加载或规划前检查；拟合流程同时传入原始
    对应点和残差，从而阻止低位移或高残差结果进入应用状态。
    """
    matrix = np.asarray(calibration.matrix, dtype=np.float64)
    offset = np.asarray(calibration.offset, dtype=np.float64)
    if matrix.shape != (2, 2) or offset.shape != (2,) or not np.isfinite(matrix).all() or not np.isfinite(offset).all():
        raise ValueError("空间标定包含非有限值或维度无效")
    try:
        singular_values = np.linalg.svd(matrix, compute_uv=False)
        inverse = np.linalg.inv(matrix)
    except np.linalg.LinAlgError as exc:
        raise ValueError("空间标定矩阵不可逆") from exc
    if not np.isfinite(singular_values).all() or not np.isfinite(inverse).all() or singular_values[-1] <= 0:
        raise ValueError("空间标定矩阵不可逆或数值不稳定")

    expected_scale = 1000.0 / DEFAULT_PIXEL_SIZE_UM
    scale_ratios = singular_values / expected_scale
    if scale_ratios[-1] < limits.min_scale_ratio or scale_ratios[0] > limits.max_scale_ratio:
        raise ValueError(
            "空间标定尺度异常: 相对默认 0.48 um/px 的倍率范围为 "
            f"{scale_ratios[-1]:.4g}~{scale_ratios[0]:.4g}，允许 "
            f"{limits.min_scale_ratio:g}~{limits.max_scale_ratio:g}"
        )
    condition_number = float(singular_values[0] / singular_values[-1])
    if condition_number > limits.max_condition_number:
        raise ValueError(
            f"空间标定矩阵数值不稳定: 条件数 {condition_number:.4g}，"
            f"上限 {limits.max_condition_number:g}"
        )

    if (stage_points is None) != (pixel_points is None):
        raise ValueError("位移质量检查必须同时提供 stage_points 与 pixel_points")
    if stage_points is not None and pixel_points is not None:
        stage = _points_array(stage_points, "stage_points")
        pixel = _points_array(pixel_points, "pixel_points")
        if len(stage) != len(pixel) or len(stage) < 3:
            raise ValueError("位移质量检查至少需要三组一一对应的坐标")
        motion_distances = _effective_motion_distances_px(stage, pixel)
        min_motion = min(motion_distances)
        if min_motion < limits.min_displacement_px:
            raise ValueError(
                f"空间标定位移不足: 有效二维位移最小仅 {min_motion:.3f} px，"
                f"至少需要 {limits.min_displacement_px:g} px"
            )

    if residuals_px is not None:
        residuals = np.asarray(tuple(residuals_px), dtype=np.float64)
        if residuals.ndim != 1 or residuals.size == 0 or not np.isfinite(residuals).all():
            raise ValueError("空间标定残差必须是一组有限数")
        max_residual = float(np.max(np.abs(residuals)))
        if max_residual > limits.max_residual_px:
            raise ValueError(
                f"空间标定单点残差过大: {max_residual:.3f} px，"
                f"上限 {limits.max_residual_px:g} px"
            )
    if rms_px is not None:
        rms = float(rms_px)
        if not np.isfinite(rms) or rms < 0:
            raise ValueError("空间标定 RMS 残差必须是有限非负数")
        if rms > limits.max_rms_residual_px:
            raise ValueError(
                f"空间标定 RMS 残差过大: {rms:.3f} px，"
                f"上限 {limits.max_rms_residual_px:g} px"
            )


def stage_to_pixel(
    calibration: SpatialCalibration,
    points: StagePoint | Sequence[float] | Sequence[StagePoint | Sequence[float]],
) -> tuple[float, float] | np.ndarray:
    """将一个或多个样品坐标转换为像素坐标。"""
    single = isinstance(points, StagePoint) or _is_coordinate_pair(points)
    values = _points_array([points] if single else points, "points")  # type: ignore[list-item, arg-type]
    matrix = np.asarray(calibration.matrix, dtype=np.float64)
    result = values @ matrix.T + np.asarray(calibration.offset)
    return tuple(float(v) for v in result[0]) if single else result


def pixel_to_stage(
    calibration: SpatialCalibration,
    points: Sequence[float] | Sequence[Sequence[float]],
) -> StagePoint | np.ndarray:
    """将一个或多个像素坐标反变换为样品坐标。"""
    single = _is_coordinate_pair(points)
    values = _points_array([points] if single else points, "points")  # type: ignore[list-item, arg-type]
    inverse = np.linalg.inv(np.asarray(calibration.matrix, dtype=np.float64))
    result = (values - np.asarray(calibration.offset)) @ inverse.T
    return StagePoint(*result[0]) if single else result


def fit_affine_calibration(
    stage_points: Sequence[StagePoint | Sequence[float]],
    pixel_points: Sequence[Sequence[float]],
    *,
    fingerprint: dict[str, object] | None = None,
    quality_limits: CalibrationQualityLimits = DEFAULT_CALIBRATION_QUALITY_LIMITS,
) -> CalibrationFit:
    """用至少三个非共线对应点拟合仿射变换，并通过质量门禁。"""
    stage = _points_array(stage_points, "stage_points")
    pixel = _points_array(pixel_points, "pixel_points")
    if len(stage) != len(pixel):
        raise ValueError("stage_points 与 pixel_points 数量必须一致")
    if len(stage) < 3:
        raise ValueError("仿射拟合至少需要三个点")

    design = np.column_stack((stage, np.ones(len(stage))))
    if np.linalg.matrix_rank(design) < 3:
        raise ValueError("样品坐标点共线，无法拟合二维仿射变换")
    try:
        coefficients, _, _, _ = np.linalg.lstsq(design, pixel, rcond=None)
    except np.linalg.LinAlgError as exc:
        raise ValueError("空间标定拟合失败，输入坐标数值不稳定") from exc
    predicted = design @ coefficients
    residuals = np.linalg.norm(predicted - pixel, axis=1)
    matrix_array = coefficients[:2, :].T
    offset = coefficients[2, :]
    if not np.isfinite(matrix_array).all() or not np.isfinite(offset).all() or not np.isfinite(residuals).all():
        raise ValueError("空间标定拟合产生了非有限值")

    # 用局部面积尺度换算等效像素尺寸，可同时兼容旋转和轻微非等比缩放。
    determinant = float(np.linalg.det(matrix_array))
    if not np.isfinite(determinant) or determinant == 0.0:
        raise ValueError("空间标定拟合矩阵不可逆")
    pixels_per_mm = float(np.sqrt(abs(determinant)))
    calibration = SpatialCalibration(
        tuple(tuple(float(v) for v in row) for row in matrix_array),  # type: ignore[arg-type]
        tuple(float(v) for v in offset),  # type: ignore[arg-type]
        1000.0 / pixels_per_mm,
        fingerprint or {},
    )
    rms = float(np.sqrt(np.mean(np.square(residuals))))
    residual_values = tuple(float(v) for v in residuals)
    validate_calibration_quality(
        calibration,
        stage_points=stage,
        pixel_points=pixel,
        residuals_px=residual_values,
        rms_px=rms,
        limits=quality_limits,
    )
    return CalibrationFit(calibration, residual_values, rms)


# 便于调用方采用简短名称，同时保留语义明确的主 API。
fit_affine = fit_affine_calibration
