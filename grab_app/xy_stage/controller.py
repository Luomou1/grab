"""不依赖 GUI 的 FMC01-02H 双轴安全控制 facade。"""

from __future__ import annotations

import copy
import math
import time
from dataclasses import dataclass
from dataclasses import replace
from typing import Callable

from .models import AxisProfile, AxisStatus, DeviceProfile, DeviceSnapshot
from .safety import (
    HARD_FAULT_MASK,
    LIMIT_MASK,
    SafetyViolation,
    drive_status_has_error,
    validate_absolute_move,
    validate_manual_move,
    validate_parameter_fingerprint,
    validate_relative_move,
)
from .sdk import MotionSdk
from .trajectory import (
    ArcDefinition,
    ArcMove,
    CoordinateMode,
    LineMove,
    LinearPathPlan,
    Point2D,
    PositionTriggerConfig,
    plan_linear_path,
    prepare_position_trigger,
    validate_arc_move,
    validate_line_move,
)


class MotionTimeoutError(TimeoutError):
    """双轴未在期限内停止或未到达目标。"""


HOME_TIMEOUT_SECONDS = 120.0
SOFT_LIMIT_REFRESH_SECONDS = 1.0


@dataclass(slots=True)
class _HomeProgress:
    started_at: float
    activity_seen: bool = False


class XYStage:
    """同步、单调用线程的双轴位移台入口，适合由扫描工作线程持有。"""

    AXES = (0, 1)

    def __init__(self, sdk: MotionSdk, profile: DeviceProfile,
                 *, clock: Callable[[], float] = time.monotonic,
                 sleeper: Callable[[float], None] = time.sleep) -> None:
        self.sdk = sdk
        self.profile = profile
        self._clock = clock
        self._sleep = sleeper
        self._soft_limits = {
            item.axis: (item.min_position, item.max_position) for item in profile.axes
        }
        self._parameter_valid = False
        self._last_target: tuple[float, float] | None = None
        self._last_limit_refresh_at = 0.0
        self._homing_axes: dict[int, _HomeProgress] = {}
        self._snapshot = DeviceSnapshot(axes=self._disconnected_axes())

    @property
    def connected(self) -> bool:
        return self.sdk.is_open

    @property
    def snapshot(self) -> DeviceSnapshot:
        """返回副本，避免 UI/扫描线程误改内核状态。"""
        return copy.deepcopy(self._snapshot)

    def _disconnected_axes(self) -> dict[int, AxisStatus]:
        return {
            item.axis: AxisStatus(
                axis=item.axis,
                soft_min_position=self._soft_limits[item.axis][0],
                soft_max_position=self._soft_limits[item.axis][1],
            )
            for item in self.profile.axes
        }

    def _refresh_axis_soft_limits(self, axis: int) -> None:
        minimum = self.sdk.get_negative_soft_limit(axis)
        maximum = self.sdk.get_positive_soft_limit(axis)
        if not math.isfinite(minimum) or not math.isfinite(maximum) or minimum >= maximum:
            raise SafetyViolation(
                f"{self.profile.axis(axis).name} 轴控制器软限位无效："
                f"[{minimum:g}, {maximum:g}] mm"
            )
        self._soft_limits[axis] = (minimum, maximum)

    @staticmethod
    def _drive_enabled(sdk: MotionSdk, value: int) -> bool:
        parser = getattr(sdk, "drive_is_enabled", None)
        return bool(parser(value)) if callable(parser) else value & 0x6F == 0x27

    @staticmethod
    def _drive_error(sdk: MotionSdk, value: int) -> bool:
        parser = getattr(sdk, "drive_is_error", None)
        return bool(parser(value)) if callable(parser) else drive_status_has_error(value)

    def connect(self, port: str) -> DeviceSnapshot:
        if self.sdk.is_open:
            self.disconnect()
        self._parameter_valid = False
        self._last_target = None
        self._last_limit_refresh_at = 0.0
        self._homing_axes.clear()
        self.sdk.open_com(port)
        mismatches: list[str] = []
        try:
            for item in self.profile.axes:
                mismatches.extend(
                    f"{item.name} 轴：{message}"
                    for message in validate_parameter_fingerprint(
                        item,
                        units=self.sdk.get_units(item.axis),
                        accel=self.sdk.get_accel(item.axis),
                        decel=self.sdk.get_decel(item.axis),
                        home_speed=self.sdk.get_home_speed(item.axis),
                        home_offset=self.sdk.get_home_offset(item.axis),
                    )
                )
                try:
                    self._refresh_axis_soft_limits(item.axis)
                except SafetyViolation as error:
                    mismatches.append(str(error))
        except Exception:
            # 连接后的参数读取异常不能留下半初始化设备。
            try:
                self.sdk.close()
            finally:
                self._snapshot = DeviceSnapshot(axes=self._disconnected_axes())
            raise
        self._parameter_valid = not mismatches
        self._snapshot = DeviceSnapshot(
            connected=True,
            parameter_valid=self._parameter_valid,
            axes=self._disconnected_axes(),
            fault_message="；".join(mismatches),
        )
        return self.refresh_status(preserve_fault=True)

    def disconnect(self) -> None:
        try:
            if self.sdk.is_open:
                try:
                    self.stop_all()
                finally:
                    self.sdk.close()
        finally:
            self._parameter_valid = False
            self._last_target = None
            self._last_limit_refresh_at = 0.0
            self._homing_axes.clear()
            self._soft_limits = {
                item.axis: (item.min_position, item.max_position) for item in self.profile.axes
            }
            self._snapshot = DeviceSnapshot(axes=self._disconnected_axes())

    close = disconnect

    def refresh_status(self, *, preserve_fault: bool = False) -> DeviceSnapshot:
        if not self.sdk.is_open:
            return self.snapshot
        previous_fault = self._snapshot.fault_message
        try:
            now = self._clock()
            refresh_limits = now - self._last_limit_refresh_at >= SOFT_LIMIT_REFRESH_SECONDS
            if refresh_limits:
                for axis in self.AXES:
                    self._refresh_axis_soft_limits(axis)
                self._last_limit_refresh_at = now
            axes: dict[int, AxisStatus] = {}
            messages: list[str] = []
            for item in self.profile.axes:
                axis = item.axis
                drive_status = self.sdk.get_drive_status(axis)
                axis_status = self.sdk.get_axis_status(axis)
                dpos = self.sdk.get_dpos(axis)
                mpos = self.sdk.get_mpos(axis)
                idle = self.sdk.is_idle(axis)
                homed = self.sdk.is_homed(axis)
                home_timeout = False
                progress = self._homing_axes.get(axis)
                if progress is not None:
                    if not idle or not homed:
                        progress.activity_seen = True
                    if idle and homed and progress.activity_seen:
                        self._homing_axes.pop(axis, None)
                        self._refresh_axis_soft_limits(axis)
                    elif self._clock() - progress.started_at >= HOME_TIMEOUT_SECONDS:
                        self._homing_axes.pop(axis, None)
                        home_timeout = True
                hard_fault = (
                    self._drive_error(self.sdk, drive_status)
                    or bool(axis_status & HARD_FAULT_MASK)
                    or home_timeout
                )
                if hard_fault:
                    detail = (
                        f"搜零超过 {HOME_TIMEOUT_SECONDS:g} 秒"
                        if home_timeout else f"故障：0x{axis_status:X}"
                    )
                    messages.append(f"{item.name} 轴{detail}")
                axes[axis] = AxisStatus(
                    axis=axis,
                    connected=True,
                    parameter_valid=self._parameter_valid,
                    dpos=dpos,
                    mpos=mpos,
                    idle=idle,
                    axis_status=axis_status,
                    homed=homed,
                    home_in_progress=axis in self._homing_axes,
                    drive_status=drive_status,
                    enabled=self._drive_enabled(self.sdk, drive_status),
                    hard_fault=hard_fault,
                    limit_interlock=bool(axis_status & LIMIT_MASK) and axis not in self._homing_axes,
                    fault_message=(f"{item.name} 轴故障" if hard_fault else ""),
                    soft_min_position=self._soft_limits[axis][0],
                    soft_max_position=self._soft_limits[axis][1],
                )
            fault = "；".join(messages)
            if preserve_fault and previous_fault:
                fault = "；".join(part for part in (previous_fault, fault) if part)
            self._snapshot = DeviceSnapshot(
                connected=True,
                parameter_valid=self._parameter_valid,
                axes=axes,
                fault_message=fault,
            )
        except Exception as error:
            self._snapshot.communication_failures += 1
            self._snapshot.fault_message = f"状态读取失败：{error}"
            raise
        return self.snapshot

    # status_snapshot 是扫描服务更易读的同义入口。
    status_snapshot = refresh_status

    def set_enabled(self, enabled: bool = True) -> DeviceSnapshot:
        self._require_connected()
        for axis in self.AXES:
            self.sdk.set_enabled(axis, enabled)
        return self.refresh_status(preserve_fault=True)

    enable_all = set_enabled

    def _axis_profile(self, axis: int) -> AxisProfile:
        if axis not in self.AXES:
            raise SafetyViolation(f"无效轴号：{axis}")
        minimum, maximum = self._soft_limits[axis]
        return replace(self.profile.axis(axis), min_position=minimum, max_position=maximum)

    def _axis_status(self, axis: int, *, refresh: bool = True) -> AxisStatus:
        if axis not in self.AXES:
            raise SafetyViolation(f"无效轴号：{axis}")
        snapshot = self.refresh_status(preserve_fault=True) if refresh else self._snapshot
        return snapshot.axes[axis]

    def _validate_recovery(self, axis: int) -> AxisStatus:
        status = self._axis_status(axis)
        profile = self._axis_profile(axis)
        if not status.connected:
            raise SafetyViolation(f"{profile.name} 轴未连接")
        if not status.parameter_valid:
            raise SafetyViolation(f"{profile.name} 轴参数尚未通过校验")
        if not status.idle:
            raise SafetyViolation(f"{profile.name} 轴正在运动")
        return status

    def _validated_speed(self, axis: int, speed: float | None, *, operation: str = "速度") -> float:
        profile = self._axis_profile(axis)
        value = profile.default_speed if speed is None else float(speed)
        if not math.isfinite(value) or not 0 < value <= profile.max_speed:
            raise SafetyViolation(f"{operation}必须在 0–{profile.max_speed:g} mm/s")
        return value

    def set_axis_enabled(self, axis: int, enabled: bool = True) -> DeviceSnapshot:
        self._validate_recovery(axis)
        self.sdk.set_enabled(axis, enabled)
        return self.refresh_status(preserve_fault=True)

    enable_axis = set_axis_enabled

    def disable_axis(self, axis: int) -> DeviceSnapshot:
        return self.set_axis_enabled(axis, False)

    def clear_axis_error(self, axis: int) -> DeviceSnapshot:
        self._require_connected()
        self._axis_profile(axis)
        self.sdk.clear_error(axis)
        return self.refresh_status()

    clear_error = clear_axis_error

    def clear_errors(self) -> DeviceSnapshot:
        self._require_connected()
        for axis in self.AXES:
            self.sdk.clear_error(axis)
        return self.refresh_status()

    def set_axis_speed(self, axis: int, speed: float) -> DeviceSnapshot:
        self._validate_recovery(axis)
        value = self._validated_speed(axis, speed)
        self.sdk.set_speed(axis, value)
        return self.refresh_status(preserve_fault=True)

    set_speed = set_axis_speed

    def home_axis(self, axis: int) -> DeviceSnapshot:
        status = self._axis_status(axis)
        validate_manual_move(self._axis_profile(axis), status)
        self.sdk.home(axis)
        self._homing_axes[axis] = _HomeProgress(self._clock())
        return self.refresh_status(preserve_fault=True)

    home = home_axis

    def cancel_home(self, axis: int) -> DeviceSnapshot:
        self._require_connected()
        self._axis_profile(axis)
        self.sdk.abort_home(axis)
        self._homing_axes.pop(axis, None)
        return self.refresh_status(preserve_fault=True)

    abort_home = cancel_home

    def move_axis_relative(self, axis: int, distance: float, *, speed: float | None = None) -> DeviceSnapshot:
        status = self._axis_status(axis)
        validate_relative_move(self._axis_profile(axis), status, float(distance))
        value = self._validated_speed(axis, speed)
        self.sdk.set_speed(axis, value)
        self.sdk.move_relative(axis, float(distance))
        return self.refresh_status(preserve_fault=True)

    move_relative = move_axis_relative

    def move_axis_absolute(self, axis: int, position: float, *, speed: float | None = None) -> DeviceSnapshot:
        status = self._axis_status(axis)
        validate_absolute_move(self._axis_profile(axis), status, float(position))
        value = self._validated_speed(axis, speed)
        self.sdk.set_speed(axis, value)
        self.sdk.move_absolute(axis, float(position))
        return self.refresh_status(preserve_fault=True)

    def start_jog(self, axis: int, direction: int, *, speed: float | None = None) -> DeviceSnapshot:
        status = self._axis_status(axis)
        validate_manual_move(self._axis_profile(axis), status)
        if direction not in (-1, 1):
            raise SafetyViolation("点动方向必须是 -1 或 1")
        if direction < 0 and status.negative_any_limit:
            raise SafetyViolation("负限位已触发，只允许正向离开限位")
        if direction > 0 and status.positive_any_limit:
            raise SafetyViolation("正限位已触发，只允许负向离开限位")
        value = self._validated_speed(axis, speed, operation="点动速度")
        self.sdk.set_speed(axis, value)
        self.sdk.jog(axis, direction)
        return self.refresh_status(preserve_fault=True)

    jog = start_jog
    jog_start = start_jog

    def stop_axis(self, axis: int) -> DeviceSnapshot:
        self._require_connected()
        self._axis_profile(axis)
        self.sdk.stop(axis)
        self._homing_axes.pop(axis, None)
        return self.refresh_status(preserve_fault=True)

    stop_jog = stop_axis
    jog_stop = stop_axis
    axis_stop = stop_axis

    def zero_axis(self, axis: int) -> DeviceSnapshot:
        status = self._axis_status(axis)
        validate_manual_move(self._axis_profile(axis), status)
        # 官方 ft_single_zero 依次清 MPOS、DPOS；SDK.zero 保持同一顺序。
        self.sdk.zero(axis)
        self._refresh_axis_soft_limits(axis)
        return self.refresh_status(preserve_fault=True)

    zero = zero_axis
    set_dpos_zero = zero_axis

    def _require_connected(self) -> None:
        if not self.sdk.is_open:
            raise SafetyViolation("XY 位移台未连接")

    def move_absolute(self, x: float, y: float, *, speed: float | None = None,
                      wait: bool = False, timeout_s: float = 30.0,
                      tolerance_mm: float = 0.005) -> DeviceSnapshot:
        self._require_connected()
        current = self.refresh_status(preserve_fault=True)
        targets = {0: float(x), 1: float(y)}
        for axis, target in targets.items():
            # 目标始终按控制器当前用户坐标 RS/FS 校验；不以配置文件的
            # 0–20 mm 名义范围替代控制器坐标，也不假设 SetDpos 会平移限位。
            validate_absolute_move(self._axis_profile(axis), current.axes[axis], target)
        if speed is not None:
            if not math.isfinite(speed) or speed <= 0:
                raise SafetyViolation("移动速度必须是正有限数值")
            for item in self.profile.axes:
                if speed > item.max_speed:
                    raise SafetyViolation(
                        f"移动速度 {speed:g} mm/s 超过 {item.name} 轴上限 {item.max_speed:g} mm/s"
                    )
                self.sdk.set_speed(item.axis, speed)
        self.sdk.move_xy_absolute(targets[0], targets[1])
        self._last_target = (targets[0], targets[1])
        if wait:
            return self.wait_until_idle(timeout_s=timeout_s, tolerance_mm=tolerance_mm)
        return self.refresh_status(preserve_fault=True)

    move_xy = move_absolute

    def interpolate_line(self, move: LineMove) -> DeviceSnapshot:
        current = self.refresh_status(preserve_fault=True)
        for axis in self.AXES:
            validate_manual_move(self._axis_profile(axis), current.axes[axis])
        start = Point2D(current.axes[0].dpos, current.axes[1].dpos)
        target = validate_line_move(
            move, start=start, x_limits=self._soft_limits[0], y_limits=self._soft_limits[1]
        )
        max_speed = min(self.profile.axis(axis).max_speed for axis in self.AXES)
        if move.speed > max_speed:
            raise SafetyViolation(f"插补速度超过轴上限 {max_speed:g} mm/s")
        self.sdk.set_speed(0, move.speed)
        self.sdk.interpolate_line(
            self.AXES, (move.target.x, move.target.y),
            move.coordinate_mode is CoordinateMode.ABSOLUTE,
        )
        self._last_target = (target.x, target.y)
        return self.refresh_status(preserve_fault=True)

    def move_line(self, x: float, y: float, *, absolute: bool = True,
                  speed: float = 0.5) -> DeviceSnapshot:
        return self.interpolate_line(LineMove(
            Point2D(float(x), float(y)),
            CoordinateMode.ABSOLUTE if absolute else CoordinateMode.RELATIVE,
            float(speed),
        ))

    def interpolate_arc(self, move: ArcMove) -> DeviceSnapshot:
        current = self.refresh_status(preserve_fault=True)
        for axis in self.AXES:
            validate_manual_move(self._axis_profile(axis), current.axes[axis])
        start = Point2D(current.axes[0].dpos, current.axes[1].dpos)
        arc = validate_arc_move(
            move, start=start, x_limits=self._soft_limits[0], y_limits=self._soft_limits[1]
        )
        max_speed = min(self.profile.axis(axis).max_speed for axis in self.AXES)
        if move.speed > max_speed:
            raise SafetyViolation(f"圆弧速度超过轴上限 {max_speed:g} mm/s")
        self.sdk.set_speed(0, move.speed)
        absolute = move.coordinate_mode is CoordinateMode.ABSOLUTE
        if move.definition is ArcDefinition.CENTER:
            self.sdk.interpolate_arc_center(
                self.AXES, (move.end.x, move.end.y),
                (move.auxiliary.x, move.auxiliary.y), move.direction, absolute,
            )
        else:
            self.sdk.interpolate_arc_three_point(
                self.AXES, (move.auxiliary.x, move.auxiliary.y),
                (move.end.x, move.end.y), absolute,
            )
        self._last_target = (arc.end.x, arc.end.y)
        return self.refresh_status(preserve_fault=True)

    def configure_position_trigger(self, config: PositionTriggerConfig) -> DeviceSnapshot:
        status = self._axis_status(config.axis)
        validate_manual_move(self._axis_profile(config.axis), status)
        prepared = prepare_position_trigger(config, limits=self._soft_limits[config.axis])
        self.sdk.configure_position_trigger(prepared)
        return self.snapshot

    def stop_position_trigger(self, axis: int) -> DeviceSnapshot:
        self._require_connected()
        self._axis_profile(axis)
        self.sdk.stop_position_trigger(axis)
        return self.snapshot

    def prepare_linear_path(
        self, points: tuple[Point2D, ...], *, speed: float, window_size: int = 8
    ) -> LinearPathPlan:
        current = self.refresh_status(preserve_fault=True)
        for axis in self.AXES:
            validate_manual_move(self._axis_profile(axis), current.axes[axis])
        plan = plan_linear_path(
            tuple(points), start=Point2D(current.axes[0].dpos, current.axes[1].dpos),
            x_limits=self._soft_limits[0], y_limits=self._soft_limits[1],
            speed=float(speed), window_size=window_size,
        )
        max_speed = min(self.profile.axis(axis).max_speed for axis in self.AXES)
        if plan.speed > max_speed:
            raise SafetyViolation(f"连续轨迹速度超过轴上限 {max_speed:g} mm/s")
        base = self.profile.axis(0)
        self.sdk.configure_continuous_path(0, plan.speed, base.accel, base.decel)
        return plan

    def remaining_line_buffer(self) -> int:
        self._require_connected()
        return self.sdk.get_remain_line_buffer(0)

    def submit_linear_path_batch(self, points: tuple[Point2D, ...]) -> None:
        self._require_connected()
        if points:
            self.sdk.multi_move_absolute(
                self.AXES, tuple((point.x, point.y) for point in points)
            )

    def wait_until_idle(self, *, timeout_s: float = 30.0,
                        tolerance_mm: float = 0.005) -> DeviceSnapshot:
        self._require_connected()
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("等待超时必须是正有限数值")
        if not math.isfinite(tolerance_mm) or tolerance_mm < 0:
            raise ValueError("到位容差必须是非负有限数值")
        deadline = self._clock() + timeout_s
        poll_interval = self.profile.poll_interval_ms / 1000.0
        while True:
            snapshot = self.refresh_status(preserve_fault=True)
            if snapshot.fault_message and any(item.hard_fault for item in snapshot.axes.values()):
                self.stop_all()
                raise SafetyViolation(snapshot.fault_message)
            all_idle = all(snapshot.axes[axis].idle for axis in self.AXES)
            position_ok = self._last_target is None or all(
                abs(snapshot.axes[axis].dpos - self._last_target[axis]) <= tolerance_mm
                for axis in self.AXES
            )
            if all_idle and position_ok:
                return snapshot
            if self._clock() >= deadline:
                self.stop_all()
                raise MotionTimeoutError(f"等待 XY 位移台到位超时（{timeout_s:g} s）")
            self._sleep(min(poll_interval, max(0.0, deadline - self._clock())))

    wait_for_idle = wait_until_idle

    def stop_all(self) -> DeviceSnapshot:
        self._require_connected()
        errors: list[Exception] = []
        # 两轴均尝试停止，避免首轴异常导致另一轴继续运动。
        for axis in self.AXES:
            try:
                self.sdk.stop(axis)
            except Exception as error:
                errors.append(error)
        self._last_target = None
        if errors:
            raise errors[0]
        return self.refresh_status(preserve_fault=True)


# 兼容扫描服务中常见的 Controller 命名。
XYStageController = XYStage
