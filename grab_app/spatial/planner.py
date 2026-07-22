"""矩形空间 ROI 的完整覆盖网格与运动路线规划。"""

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

from .calibration import DEFAULT_PIXEL_SIZE_UM, validate_calibration_quality
from .models import SpatialCalibration, SpatialRect, StagePoint, TilePlacement, TilePlan


@dataclass(frozen=True)
class SafetyLimits:
    """应用安全限位，单位 mm。"""

    x_min_mm: float
    x_max_mm: float
    y_min_mm: float
    y_max_mm: float

    def as_rect(self) -> SpatialRect:
        return SpatialRect(self.x_min_mm, self.y_min_mm, self.x_max_mm, self.y_max_mm)


def _axis_centers(start: float, end: float, footprint: float, overlap: float) -> list[float]:
    length = end - start
    if length <= footprint:
        return [(start + end) / 2.0]
    max_step = footprint * (1.0 - overlap)
    count = max(2, int(math.ceil((length - footprint) / max_step)) + 1)
    first = start + footprint / 2.0
    last = end - footprint / 2.0
    return np.linspace(first, last, count).tolist()


def _scan_centers(start: float, end: float, coverage: float, overlap: float) -> list[float]:
    """按用户指定的相机中心起止坐标生成中心点。

    与 ``plan_tiles`` 的 ROI 外边界语义不同，这个函数用于概览扫描输入框：
    起点和终点就是位移台 DPOS 的相机中心坐标。相同起止坐标保留为一个中心，
    因而天然支持单行或单列扫描。
    """
    if not math.isfinite(start) or not math.isfinite(end):
        raise ValueError("扫描中心坐标必须是有限数")
    if start == end:
        return [float(start)]
    max_step = coverage * (1.0 - overlap)
    if max_step <= 0 or not math.isfinite(max_step):
        raise ValueError("扫描中心间距无效，请检查标定和重叠率")
    count = max(2, int(math.ceil(abs(end - start) / max_step)) + 1)
    return np.linspace(start, end, count).tolist()


def frame_footprint_mm(
    frame_size_px: tuple[int, int], calibration: SpatialCalibration
) -> tuple[float, float]:
    """返回相机帧在样品 X/Y 轴上的保守覆盖尺寸。"""
    width, height = frame_size_px
    if width <= 0 or height <= 0:
        raise ValueError("frame_size_px 必须是正整数")
    expected = (
        width * DEFAULT_PIXEL_SIZE_UM / 1000.0,
        height * DEFAULT_PIXEL_SIZE_UM / 1000.0,
    )
    try:
        inverse = np.linalg.inv(np.asarray(calibration.matrix, dtype=np.float64))
    except np.linalg.LinAlgError as exc:
        raise ValueError(
            f"标定视场明显不合理: {width}x{height} px 无法换算；"
            f"按默认 0.48 um/px 应约为 {expected[0]:.6g}x{expected[1]:.6g} mm"
        ) from exc
    corners_px = np.array([
        [-width / 2, -height / 2], [width / 2, -height / 2],
        [width / 2, height / 2], [-width / 2, height / 2],
    ])
    corners_stage = corners_px @ inverse.T
    spans = np.ptp(corners_stage, axis=0)
    if np.any(spans <= 0) or not np.isfinite(spans).all():
        raise ValueError("标定无法生成有效视场")
    try:
        validate_calibration_quality(calibration)
    except ValueError as exc:
        raise ValueError(
            f"标定视场明显不合理: {width}x{height} px 计算为 "
            f"{spans[0]:.6g}x{spans[1]:.6g} mm；按默认 0.48 um/px "
            f"应约为 {expected[0]:.6g}x{expected[1]:.6g} mm（{exc}）"
        ) from exc
    return float(spans[0]), float(spans[1])


def _guaranteed_coverage_mm(
    frame_size_px: tuple[int, int], calibration: SpatialCalibration
) -> tuple[float, float]:
    """计算完整落在旋转/剪切视场内的保守轴对齐矩形。"""
    width, height = frame_size_px
    matrix = np.abs(np.asarray(calibration.matrix, dtype=np.float64))
    budgets = np.array([width / 2.0, height / 2.0])
    half_capacities: list[float] = []
    for axis in range(2):
        constraints = [budgets[i] / matrix[i, axis] for i in range(2) if matrix[i, axis] > 1e-15]
        if not constraints:
            raise ValueError("标定矩阵缺少有效空间轴")
        half_capacities.append(min(constraints))
    consumption = matrix @ np.asarray(half_capacities)
    scale = min(1.0, *(budgets[i] / consumption[i] for i in range(2) if consumption[i] > 0))
    return 2.0 * half_capacities[0] * scale, 2.0 * half_capacities[1] * scale


def plan_tiles(
    roi: SpatialRect,
    frame_size_px: tuple[int, int],
    calibration: SpatialCalibration,
    overlap: float = 0.2,
    *,
    route: str = "serpentine",
    safety_limits: SafetyLimits | SpatialRect | None,
) -> TilePlan:
    """生成完整覆盖 ROI 的最小规则网格，并严格校验整个帧视场。"""
    if not 0.1 <= overlap <= 0.4:
        raise ValueError("overlap 必须位于 [0.1, 0.4] 范围")
    if route not in {"serpentine", "unidirectional"}:
        raise ValueError("route 仅支持 serpentine 或 unidirectional")
    if safety_limits is None:
        raise ValueError("自动规划必须提供应用安全限位")
    limits = safety_limits.as_rect() if isinstance(safety_limits, SafetyLimits) else safety_limits
    if roi.width_mm <= 0 or roi.height_mm <= 0:
        raise ValueError("ROI 必须具有正面积")

    footprint_x, footprint_y = frame_footprint_mm(frame_size_px, calibration)
    coverage_x, coverage_y = _guaranteed_coverage_mm(frame_size_px, calibration)
    x_centers = _axis_centers(roi.x_min_mm, roi.x_max_mm, coverage_x, overlap)
    y_centers = _axis_centers(roi.y_min_mm, roi.y_max_mm, coverage_y, overlap)
    half_x, half_y = footprint_x / 2.0, footprint_y / 2.0

    grid: list[list[TilePlacement]] = []
    for row, y_mm in enumerate(y_centers):
        row_items = []
        for column, x_mm in enumerate(x_centers):
            bounds = SpatialRect(x_mm - half_x, y_mm - half_y, x_mm + half_x, y_mm + half_y)
            if not limits.contains_rect(bounds, tolerance=1e-12):
                raise ValueError(
                    f"瓦片 r{row} c{column} 的完整视场超出安全限位: {bounds}"
                )
            row_items.append(TilePlacement(row, column, StagePoint(x_mm, y_mm), bounds))
        grid.append(row_items)

    ordered: list[TilePlacement] = []
    for row, row_items in enumerate(grid):
        items = list(reversed(row_items)) if route == "serpentine" and row % 2 else row_items
        ordered.extend(items)
    placements = tuple(
        TilePlacement(item.row, item.column, item.target, item.bounds, sequence)
        for sequence, item in enumerate(ordered)
    )
    distance = sum(
        math.hypot(b.target.x_mm - a.target.x_mm, b.target.y_mm - a.target.y_mm)
        for a, b in zip(placements, placements[1:])
    )
    spacing_x = 0.0 if len(x_centers) < 2 else x_centers[1] - x_centers[0]
    spacing_y = 0.0 if len(y_centers) < 2 else y_centers[1] - y_centers[0]
    return TilePlan(
        roi=roi, placements=placements, frame_size_px=frame_size_px, overlap=overlap,
        route=route, tile_size_mm=(footprint_x, footprint_y),
        spacing_mm=(spacing_x, spacing_y), rows=len(y_centers), columns=len(x_centers),
        estimated_distance_mm=distance,
    )


def plan_center_scan(
    x_start_mm: float,
    x_end_mm: float,
    y_start_mm: float,
    y_end_mm: float,
    frame_size_px: tuple[int, int],
    calibration: SpatialCalibration,
    overlap: float = 0.2,
    *,
    route: str = "serpentine",
    safety_limits: SafetyLimits | SpatialRect | None,
) -> TilePlan:
    """按相机中心坐标规划概览扫描，支持单行、单列和负用户坐标。"""
    if not 0.1 <= overlap <= 0.4:
        raise ValueError("overlap 必须位于 [0.1, 0.4] 范围")
    if route not in {"serpentine", "unidirectional"}:
        raise ValueError("route 仅支持 serpentine 或 unidirectional")
    if safety_limits is None:
        raise ValueError("自动规划必须提供应用安全限位")
    limits = safety_limits.as_rect() if isinstance(safety_limits, SafetyLimits) else safety_limits
    footprint_x, footprint_y = frame_footprint_mm(frame_size_px, calibration)
    coverage_x, coverage_y = _guaranteed_coverage_mm(frame_size_px, calibration)
    x_centers = _scan_centers(float(x_start_mm), float(x_end_mm), coverage_x, overlap)
    y_centers = _scan_centers(float(y_start_mm), float(y_end_mm), coverage_y, overlap)
    half_x, half_y = footprint_x / 2.0, footprint_y / 2.0

    grid: list[list[TilePlacement]] = []
    for row, y_mm in enumerate(y_centers):
        row_items: list[TilePlacement] = []
        for column, x_mm in enumerate(x_centers):
            bounds = SpatialRect(x_mm - half_x, y_mm - half_y, x_mm + half_x, y_mm + half_y)
            if not limits.contains(StagePoint(x_mm, y_mm), tolerance=1e-12):
                raise ValueError(
                    f"相机中心 ({x_mm:.6g}, {y_mm:.6g}) mm 超出控制器软限位；"
                    f"当前允许 X[{limits.x_min_mm:.6g}, {limits.x_max_mm:.6g}]、"
                    f"Y[{limits.y_min_mm:.6g}, {limits.y_max_mm:.6g}] mm"
                )
            row_items.append(TilePlacement(row, column, StagePoint(x_mm, y_mm), bounds))
        grid.append(row_items)

    ordered: list[TilePlacement] = []
    for row, row_items in enumerate(grid):
        items = list(reversed(row_items)) if route == "serpentine" and row % 2 else row_items
        ordered.extend(items)
    placements = tuple(
        TilePlacement(item.row, item.column, item.target, item.bounds, sequence)
        for sequence, item in enumerate(ordered)
    )
    roi = SpatialRect(
        min(item.bounds.x_min_mm for item in placements),
        min(item.bounds.y_min_mm for item in placements),
        max(item.bounds.x_max_mm for item in placements),
        max(item.bounds.y_max_mm for item in placements),
    )
    distance = sum(
        math.hypot(b.target.x_mm - a.target.x_mm, b.target.y_mm - a.target.y_mm)
        for a, b in zip(placements, placements[1:])
    )
    spacing_x = 0.0 if len(x_centers) < 2 else x_centers[1] - x_centers[0]
    spacing_y = 0.0 if len(y_centers) < 2 else y_centers[1] - y_centers[0]
    return TilePlan(
        roi=roi,
        placements=placements,
        frame_size_px=frame_size_px,
        overlap=overlap,
        route=route,
        tile_size_mm=(footprint_x, footprint_y),
        spacing_mm=(spacing_x, spacing_y),
        rows=len(y_centers),
        columns=len(x_centers),
        estimated_distance_mm=distance,
    )


plan_grid = plan_tiles
