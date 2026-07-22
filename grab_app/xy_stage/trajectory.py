"""双轴插补、OUT0 位置触发与连续轨迹的数据模型和纯校验。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class TrajectoryValidationError(ValueError):
    """轨迹或硬件触发参数不满足安全约束。"""


class CoordinateMode(Enum):
    ABSOLUTE = "绝对"
    RELATIVE = "相对"


class ArcDefinition(Enum):
    CENTER = "圆心"
    THREE_POINT = "三点"


@dataclass(frozen=True, slots=True)
class Point2D:
    x: float
    y: float


@dataclass(frozen=True, slots=True)
class LineMove:
    target: Point2D
    coordinate_mode: CoordinateMode = CoordinateMode.ABSOLUTE
    speed: float = 0.5


@dataclass(frozen=True, slots=True)
class ArcMove:
    end: Point2D
    auxiliary: Point2D
    coordinate_mode: CoordinateMode
    definition: ArcDefinition
    direction: int = 0
    speed: float = 0.5


@dataclass(frozen=True, slots=True)
class PositionTriggerConfig:
    axis: int
    positions: tuple[float, ...]
    output: int = 0
    active_state: int = 1
    pulse_width_us: int = 100
    cycle_us: int = 500
    table_start: int = 0


@dataclass(frozen=True, slots=True)
class PreparedPositionTrigger:
    axis: int
    positions: tuple[float, ...]
    direction: int
    output: int
    active_state: int
    pulse_width_us: int
    cycle_us: int
    table_start: int


@dataclass(frozen=True, slots=True)
class LinearPathPlan:
    points: tuple[Point2D, ...]
    speed: float
    window_size: int


@dataclass(frozen=True, slots=True)
class ValidatedArc:
    end: Point2D
    auxiliary: Point2D
    direction: int


def _absolute(point: Point2D, mode: CoordinateMode, start: Point2D) -> Point2D:
    if mode is CoordinateMode.ABSOLUTE:
        return point
    return Point2D(start.x + point.x, start.y + point.y)


def _validate_point(
    point: Point2D,
    *,
    x_limits: tuple[float, float],
    y_limits: tuple[float, float],
    label: str,
) -> None:
    if not all(math.isfinite(value) for value in (point.x, point.y)):
        raise TrajectoryValidationError(f"{label}必须是有限坐标")
    if not x_limits[0] <= point.x <= x_limits[1]:
        raise TrajectoryValidationError(f"{label} X={point.x:g} 超出软限位")
    if not y_limits[0] <= point.y <= y_limits[1]:
        raise TrajectoryValidationError(f"{label} Y={point.y:g} 超出软限位")


def validate_line_move(
    move: LineMove,
    *,
    start: Point2D,
    x_limits: tuple[float, float],
    y_limits: tuple[float, float],
) -> Point2D:
    if not math.isfinite(move.speed) or move.speed <= 0:
        raise TrajectoryValidationError("插补速度必须是正有限数值")
    target = _absolute(move.target, move.coordinate_mode, start)
    _validate_point(target, x_limits=x_limits, y_limits=y_limits, label="直线终点")
    return target


def _angle_on_sweep(angle: float, start: float, end: float, direction: int) -> bool:
    turn = 2 * math.pi
    total = ((end - start) if direction == 0 else (start - end)) % turn
    offset = ((angle - start) if direction == 0 else (start - angle)) % turn
    return offset <= total + 1e-9


def _circle_center(first: Point2D, second: Point2D, third: Point2D) -> Point2D:
    determinant = 2 * (
        first.x * (second.y - third.y)
        + second.x * (third.y - first.y)
        + third.x * (first.y - second.y)
    )
    if abs(determinant) <= 1e-9:
        raise TrajectoryValidationError("圆弧起点、中间点和终点共线")
    first_norm = first.x**2 + first.y**2
    second_norm = second.x**2 + second.y**2
    third_norm = third.x**2 + third.y**2
    return Point2D(
        (first_norm * (second.y - third.y) + second_norm * (third.y - first.y)
         + third_norm * (first.y - second.y)) / determinant,
        (first_norm * (third.x - second.x) + second_norm * (first.x - third.x)
         + third_norm * (second.x - first.x)) / determinant,
    )


def _validate_arc_extent(
    start: Point2D,
    end: Point2D,
    center: Point2D,
    direction: int,
    *,
    x_limits: tuple[float, float],
    y_limits: tuple[float, float],
) -> None:
    radius = math.hypot(start.x - center.x, start.y - center.y)
    end_radius = math.hypot(end.x - center.x, end.y - center.y)
    tolerance = max(1e-4, radius * 1e-3)
    if radius <= 1e-9:
        raise TrajectoryValidationError("圆弧起点不能与圆心重合")
    if abs(radius - end_radius) > tolerance:
        raise TrajectoryValidationError("圆弧起点和终点半径不一致")
    if math.hypot(start.x - end.x, start.y - end.y) <= tolerance:
        raise TrajectoryValidationError("圆弧起点与终点不能重合")
    start_angle = math.atan2(start.y - center.y, start.x - center.x)
    end_angle = math.atan2(end.y - center.y, end.x - center.x)
    points = [start, end]
    for angle in (0.0, math.pi / 2, math.pi, 3 * math.pi / 2):
        if _angle_on_sweep(angle, start_angle, end_angle, direction):
            points.append(Point2D(center.x + radius * math.cos(angle), center.y + radius * math.sin(angle)))
    for point in points:
        try:
            _validate_point(point, x_limits=x_limits, y_limits=y_limits, label="圆弧轨迹")
        except TrajectoryValidationError as error:
            raise TrajectoryValidationError(f"整段圆弧越界：{error}") from error


def validate_arc_move(
    move: ArcMove,
    *,
    start: Point2D,
    x_limits: tuple[float, float],
    y_limits: tuple[float, float],
) -> ValidatedArc:
    if not math.isfinite(move.speed) or move.speed <= 0:
        raise TrajectoryValidationError("圆弧速度必须是正有限数值")
    end = _absolute(move.end, move.coordinate_mode, start)
    auxiliary = _absolute(move.auxiliary, move.coordinate_mode, start)
    _validate_point(end, x_limits=x_limits, y_limits=y_limits, label="圆弧终点")
    if move.definition is ArcDefinition.CENTER:
        if move.direction not in (0, 1):
            raise TrajectoryValidationError("圆弧方向必须为 0 或 1")
        center, direction = auxiliary, move.direction
    else:
        _validate_point(auxiliary, x_limits=x_limits, y_limits=y_limits, label="圆弧中间点")
        center = _circle_center(start, auxiliary, end)
        start_angle = math.atan2(start.y - center.y, start.x - center.x)
        middle_angle = math.atan2(auxiliary.y - center.y, auxiliary.x - center.x)
        end_angle = math.atan2(end.y - center.y, end.x - center.x)
        direction = 0 if _angle_on_sweep(middle_angle, start_angle, end_angle, 0) else 1
    _validate_arc_extent(start, end, center, direction, x_limits=x_limits, y_limits=y_limits)
    return ValidatedArc(end, auxiliary, direction)


def prepare_position_trigger(
    config: PositionTriggerConfig, *, limits: tuple[float, float]
) -> PreparedPositionTrigger:
    if config.axis not in (0, 1):
        raise TrajectoryValidationError("位置触发轴只能是 0 或 1")
    if config.output != 0:
        raise TrajectoryValidationError("FMC01-02H 位置触发仅允许使用 OUT0")
    if config.active_state not in (0, 1):
        raise TrajectoryValidationError("OUT0 有效电平必须是 0 或 1")
    if not 1 <= len(config.positions) <= 512:
        raise TrajectoryValidationError("位置触发列表必须包含 1–512 个位置")
    if config.table_start < 0:
        raise TrajectoryValidationError("TABLE 起始地址不能为负数")
    if not 0 < config.pulse_width_us < config.cycle_us:
        raise TrajectoryValidationError("脉宽必须大于 0 且小于周期")
    for position in config.positions:
        if not math.isfinite(position) or not limits[0] <= position <= limits[1]:
            raise TrajectoryValidationError(f"触发位置 {position:g} 超出软限位")
    deltas = [b - a for a, b in zip(config.positions, config.positions[1:])]
    if deltas and all(delta > 0 for delta in deltas):
        direction = 1
    elif deltas and all(delta < 0 for delta in deltas):
        direction = 0
    elif deltas:
        raise TrajectoryValidationError("位置触发列表必须严格单调")
    else:
        direction = -1
    return PreparedPositionTrigger(
        config.axis, config.positions, direction, config.output, config.active_state,
        config.pulse_width_us, config.cycle_us, config.table_start,
    )


def plan_linear_path(
    points: tuple[Point2D, ...],
    *,
    start: Point2D,
    x_limits: tuple[float, float],
    y_limits: tuple[float, float],
    speed: float,
    window_size: int,
) -> LinearPathPlan:
    if not points:
        raise TrajectoryValidationError("连续轨迹至少需要一个点")
    if not 1 <= window_size <= 16:
        raise TrajectoryValidationError("预加载窗口必须在 1–16 段之间")
    current = start
    for point in points:
        validate_line_move(LineMove(point, CoordinateMode.ABSOLUTE, speed), start=current,
                           x_limits=x_limits, y_limits=y_limits)
        current = point
    return LinearPathPlan(points, speed, window_size)
