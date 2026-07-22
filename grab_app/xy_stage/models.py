"""FMC01-02H 双轴位移台的数据模型与配置加载。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ProfileError(ValueError):
    """设备配置不完整或违反已确认的 FMC01-02H 参数约束。"""


@dataclass(frozen=True, slots=True)
class AxisProfile:
    axis: int
    name: str
    units: float
    accel: float
    decel: float
    home_speed: float
    home_offset: float
    min_position: float
    max_position: float
    max_speed: float
    default_speed: float


@dataclass(frozen=True, slots=True)
class DeviceProfile:
    name: str
    baudrate: int
    poll_interval_ms: int
    axes: tuple[AxisProfile, AxisProfile]

    def axis(self, axis: int) -> AxisProfile:
        for item in self.axes:
            if item.axis == axis:
                return item
        raise KeyError(f"配置中不存在轴 {axis}")


@dataclass(frozen=True, slots=True)
class AxisStatus:
    axis: int
    connected: bool = False
    parameter_valid: bool = False
    dpos: float = 0.0
    mpos: float = 0.0
    idle: bool = True
    axis_status: int = 0
    homed: bool = False
    home_in_progress: bool = False
    drive_status: int = 0
    enabled: bool = False
    hard_fault: bool = False
    limit_interlock: bool = False
    fault_message: str = ""
    soft_min_position: float = 0.0
    soft_max_position: float = 20.0

    @property
    def running(self) -> bool:
        return not self.idle

    @property
    def positive_limit(self) -> bool:
        return bool(self.axis_status & 0x10)

    @property
    def negative_limit(self) -> bool:
        return bool(self.axis_status & 0x20)

    @property
    def positive_soft_limit(self) -> bool:
        return bool(self.axis_status & 0x200)

    @property
    def negative_soft_limit(self) -> bool:
        return bool(self.axis_status & 0x400)

    @property
    def positive_any_limit(self) -> bool:
        return self.positive_limit or self.positive_soft_limit

    @property
    def negative_any_limit(self) -> bool:
        return self.negative_limit or self.negative_soft_limit

    @property
    def homing(self) -> bool:
        return self.home_in_progress or bool(self.axis_status & 0x40)

@dataclass(slots=True)
class DeviceSnapshot:
    connected: bool = False
    parameter_valid: bool = False
    axes: dict[int, AxisStatus] = field(default_factory=dict)
    fault_message: str = ""
    communication_failures: int = 0

DEFAULT_PROFILE_PATH = Path(__file__).with_name("device_profile.json")


def _number(data: dict[str, Any], key: str) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileError(f"参数 {key} 必须是数值")
    return float(value)


def load_profile(path: str | Path) -> DeviceProfile:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProfileError(f"无法读取设备配置：{error}") from error
    if not isinstance(raw, dict) or not isinstance(raw.get("axes"), list):
        raise ProfileError("设备配置必须包含 axes 数组")
    axes: list[AxisProfile] = []
    for item in raw["axes"]:
        if not isinstance(item, dict):
            raise ProfileError("轴配置必须是对象")
        try:
            profile = AxisProfile(
                axis=int(item["axis"]), name=str(item["name"]),
                units=_number(item, "units"), accel=_number(item, "accel"),
                decel=_number(item, "decel"), home_speed=_number(item, "home_speed"),
                home_offset=_number(item, "home_offset"),
                min_position=_number(item, "min_position"),
                max_position=_number(item, "max_position"),
                max_speed=_number(item, "max_speed"),
                default_speed=_number(item, "default_speed"),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ProfileError("轴配置缺少有效参数") from error
        if abs(profile.units - 50000.0) > 1e-6:
            raise ProfileError("ZMC 实际脉冲当量必须为 50000 pulse/mm")
        if profile.min_position >= profile.max_position:
            raise ProfileError(f"轴 {profile.axis} 的软限位范围无效")
        if not 0 < profile.default_speed <= profile.max_speed <= 5.0:
            raise ProfileError(f"轴 {profile.axis} 的速度配置无效")
        axes.append(profile)
    axes.sort(key=lambda item: item.axis)
    if [item.axis for item in axes] != [0, 1]:
        raise ProfileError("设备配置必须且只能包含轴 0 和轴 1")
    baudrate = raw.get("baudrate")
    poll_interval_ms = raw.get("poll_interval_ms")
    if baudrate != 38400:
        raise ProfileError("FMC01-02H 串口波特率必须为 38400")
    if not isinstance(poll_interval_ms, int) or not 100 <= poll_interval_ms <= 1000:
        raise ProfileError("状态轮询周期必须在 100–1000 ms")
    return DeviceProfile(str(raw.get("name", "FMC01-02H")), baudrate, poll_interval_ms, tuple(axes))


def load_default_profile() -> DeviceProfile:
    """加载随应用发布的 FMC01-02H + 20 mm 双轴台参数。"""
    return load_profile(DEFAULT_PROFILE_PATH)
