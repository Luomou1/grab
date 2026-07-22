"""位移台运动前置条件和软限位校验。"""

from __future__ import annotations

import math

from .models import AxisProfile, AxisStatus


class SafetyViolation(RuntimeError):
    """操作不满足设备安全前置条件。"""


# AXISSTATUS bit 9/10 是正/负软限位，触发后必须允许反向脱离，不能归类为
# 不可恢复硬故障。四种限位统一用于自动任务互锁。
HARD_FAULT_MASK = sum(1 << bit for bit in (1, 2, 3, 8, 12, 18, 22))
LIMIT_MASK = 0x10 | 0x20 | 0x200 | 0x400


def drive_status_has_error(drive_status: int) -> bool:
    return (drive_status & 0x4F) == 0x08


def validate_parameter_fingerprint(profile: AxisProfile, *, units: float, accel: float,
                                   decel: float, home_speed: float, home_offset: float,
                                   tolerance: float = 1e-3) -> list[str]:
    actual = {"脉冲当量": units, "加速度": accel, "减速度": decel,
              "回零速度": home_speed, "回零偏移": home_offset}
    expected = {"脉冲当量": profile.units, "加速度": profile.accel, "减速度": profile.decel,
                "回零速度": profile.home_speed, "回零偏移": profile.home_offset}
    return [f"{name}不匹配：期望 {expected[name]:g}，读取到 {value:g}"
            for name, value in actual.items() if abs(value - expected[name]) > tolerance]


def validate_target(profile: AxisProfile, status: AxisStatus, target: float) -> None:
    if not math.isfinite(target):
        raise SafetyViolation(f"{profile.name} 轴目标不是有限数值")
    minimum = max(profile.min_position, status.soft_min_position)
    maximum = min(profile.max_position, status.soft_max_position)
    if not minimum <= target <= maximum:
        raise SafetyViolation(f"目标 {target:g} mm 超出软限位 [{minimum:g}, {maximum:g}] mm")


def validate_manual_move(profile: AxisProfile, status: AxisStatus) -> None:
    if not status.connected:
        raise SafetyViolation(f"{profile.name} 轴未连接")
    if not status.parameter_valid:
        raise SafetyViolation(f"{profile.name} 轴参数尚未通过校验")
    if not status.enabled:
        raise SafetyViolation(f"{profile.name} 轴未使能")
    if status.hard_fault or status.axis_status & HARD_FAULT_MASK:
        raise SafetyViolation(f"{profile.name} 轴状态异常：0x{status.axis_status:X}")
    if status.homing:
        raise SafetyViolation(f"{profile.name} 轴正在搜零")
    if not status.idle:
        raise SafetyViolation(f"{profile.name} 轴正在运动")


def validate_absolute_move(profile: AxisProfile, status: AxisStatus, target: float) -> None:
    validate_manual_move(profile, status)
    validate_target(profile, status, target)
    delta = target - status.dpos
    if delta < 0 and status.negative_any_limit:
        raise SafetyViolation(f"{profile.name} 轴负限位已触发，只允许正向离开限位")
    if delta > 0 and status.positive_any_limit:
        raise SafetyViolation(f"{profile.name} 轴正限位已触发，只允许负向离开限位")


def validate_relative_move(profile: AxisProfile, status: AxisStatus, distance: float) -> float:
    """校验单轴相对运动，并返回对应的绝对目标。"""
    if not math.isfinite(distance):
        raise SafetyViolation(f"{profile.name} 轴相对位移不是有限数值")
    target = status.dpos + distance
    validate_manual_move(profile, status)
    validate_target(profile, status, target)
    if distance < 0 and status.negative_any_limit:
        raise SafetyViolation(f"{profile.name} 轴负限位已触发，只允许正向离开限位")
    if distance > 0 and status.positive_any_limit:
        raise SafetyViolation(f"{profile.name} 轴正限位已触发，只允许负向离开限位")
    return target
