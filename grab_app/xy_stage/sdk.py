"""FMC01-02H 厂家 DLL 的最小双轴适配层及无硬件 Fake。"""

from __future__ import annotations

import ctypes
import math
import os
import re
import sys
import threading
from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import DeviceProfile
from .trajectory import PreparedPositionTrigger


class SdkError(RuntimeError):
    def __init__(self, function: str, return_code: int, detail: str = "") -> None:
        message = f"{function} 调用失败，SDK 返回码 {return_code}"
        super().__init__(f"{message}：{detail}" if detail else message)
        self.function = function
        self.return_code = return_code


class SdkLoadError(RuntimeError):
    """厂家 DLL 缺失、位数不匹配或依赖无法加载。"""


class SdkThreadError(RuntimeError):
    """厂家 SDK 被连接线程之外的线程调用。"""


@runtime_checkable
class MotionSdk(Protocol):
    """扫描总控依赖的最小硬件协议，可由真实 DLL 或 Fake 实现。"""

    @property
    def is_open(self) -> bool: ...
    def open_com(self, port: str) -> None: ...
    def close(self) -> None: ...
    def get_units(self, axis: int) -> float: ...
    def get_accel(self, axis: int) -> float: ...
    def get_decel(self, axis: int) -> float: ...
    def get_home_speed(self, axis: int) -> float: ...
    def get_home_offset(self, axis: int) -> float: ...
    def get_dpos(self, axis: int) -> float: ...
    def get_mpos(self, axis: int) -> float: ...
    def get_negative_soft_limit(self, axis: int) -> float: ...
    def get_positive_soft_limit(self, axis: int) -> float: ...
    def is_idle(self, axis: int) -> bool: ...
    def get_axis_status(self, axis: int) -> int: ...
    def is_homed(self, axis: int) -> bool: ...
    def get_drive_status(self, axis: int) -> int: ...
    def set_enabled(self, axis: int, enabled: bool) -> None: ...
    def clear_error(self, axis: int) -> None: ...
    def set_speed(self, axis: int, value: float) -> None: ...
    def home(self, axis: int) -> None: ...
    def abort_home(self, axis: int) -> None: ...
    def move_relative(self, axis: int, distance: float) -> None: ...
    def move_absolute(self, axis: int, position: float) -> None: ...
    def move_xy_absolute(self, x: float, y: float) -> None: ...
    def jog(self, axis: int, direction: int) -> None: ...
    def set_mpos(self, axis: int, value: float) -> None: ...
    def set_dpos(self, axis: int, value: float) -> None: ...
    def zero(self, axis: int) -> None: ...
    def interpolate_line(self, axes: tuple[int, int], values: tuple[float, float], absolute: bool) -> None: ...
    def interpolate_arc_center(
        self, axes: tuple[int, int], end: tuple[float, float],
        center: tuple[float, float], direction: int, absolute: bool,
    ) -> None: ...
    def interpolate_arc_three_point(
        self, axes: tuple[int, int], middle: tuple[float, float],
        end: tuple[float, float], absolute: bool,
    ) -> None: ...
    def configure_position_trigger(self, config: PreparedPositionTrigger) -> None: ...
    def stop_position_trigger(self, axis: int) -> None: ...
    def configure_continuous_path(self, axis: int, speed: float, accel: float, decel: float) -> None: ...
    def get_remain_line_buffer(self, axis: int) -> int: ...
    def multi_move_absolute(self, axes: tuple[int, int], points: tuple[tuple[float, float], ...]) -> None: ...
    def stop(self, axis: int) -> None: ...


def parse_com_port(port: str) -> int:
    match = re.fullmatch(r"COM([1-9]\d*)", port.strip(), re.IGNORECASE)
    if match is None:
        raise ValueError(f"无效 COM 口：{port}")
    return int(match.group(1))


class FmcSdk:
    """ctypes 封装；首次调用线程会独占 DLL 实例的整个生命周期。"""

    REQUIRED_EXPORTS = (
        "ZAux_OpenCom", "ZAux_Close", "ZAux_Execute", "ZAux_SetTimeOut",
        "ZAux_Direct_GetUnits", "ZAux_Direct_GetAccel", "ZAux_Direct_GetDecel",
        "ZAux_Direct_GetDpos", "ZAux_Direct_GetMpos", "ZAux_Direct_GetRsLimit",
        "ZAux_Direct_GetFsLimit",
        "ZAux_Direct_GetIfIdle", "ZAux_Direct_GetAxisStatus",
        "ZAux_Direct_SetSpeed", "ZAux_Direct_SetAccel", "ZAux_Direct_SetDecel",
        "ZAux_Direct_SetMpos", "ZAux_Direct_SetDpos",
        "ZAux_Direct_Single_Move", "ZAux_Direct_Single_MoveAbs",
        "ZAux_Direct_Single_Vmove",
        "ZAux_Direct_Single_Cancel", "ZAux_Direct_MoveAbs",
        "ZAux_Direct_Move", "ZAux_Direct_MoveCirc", "ZAux_Direct_MoveCircAbs",
        "ZAux_Direct_MoveCirc2", "ZAux_Direct_MoveCirc2Abs",
        "ZAux_Direct_SetMerge", "ZAux_Direct_SetLspeed", "ZAux_Direct_SetCornerMode",
        "ZAux_Direct_SetDecelAngle", "ZAux_Direct_SetStopAngle",
        "ZAux_Direct_SetFullSpRadius", "ZAux_Direct_SetZsmooth",
        "ZAux_Direct_SetTable", "ZAux_Direct_HwPswitch2", "ZAux_Direct_HwTimer",
        "ZAux_Trigger", "ZAux_Direct_GetRemain_LineBuffer", "ZAux_Direct_MultiMoveAbs",
        "ZAux_Modbus_Set0x", "ZAux_Modbus_Get0x", "ZAux_Modbus_Get4x_Float",
    )

    def __init__(self, dll_dir: str | Path) -> None:
        if sys.platform != "win32":
            raise SdkLoadError("FMC01-02H 厂家 DLL 仅支持 Windows")
        directory = Path(dll_dir).resolve()
        dll_path = directory / "zauxdll.dll"
        dependency_path = directory / "zmotion.dll"
        if not dll_path.is_file() or not dependency_path.is_file():
            raise SdkLoadError(f"厂家 DLL 不完整：{directory}")
        self._dll_directory = None
        try:
            if hasattr(os, "add_dll_directory"):
                self._dll_directory = os.add_dll_directory(str(directory))
            self._dll = ctypes.WinDLL(str(dll_path))
        except OSError as error:
            raise SdkLoadError(f"无法加载 {dll_path}，请确认 Python 与 DLL 均为 x64：{error}") from error
        self._handle = ctypes.c_void_p()
        self._owner_thread_id: int | None = None
        missing = self.required_exports_missing
        if missing:
            raise SdkLoadError(f"厂家 DLL 缺少导出函数：{', '.join(missing)}")
        self._bind_prototypes()

    @property
    def is_open(self) -> bool:
        return self._handle.value is not None

    @property
    def required_exports_missing(self) -> tuple[str, ...]:
        return tuple(name for name in self.REQUIRED_EXPORTS if not hasattr(self._dll, name))

    def _prototype(self, name: str, argtypes: list[object]) -> None:
        function = getattr(self._dll, name)
        function.argtypes = argtypes
        function.restype = ctypes.c_int32

    def _bind_prototypes(self) -> None:
        handle, integer = ctypes.c_void_p, ctypes.c_int
        float_pointer, int_pointer = ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_int)
        byte_pointer = ctypes.POINTER(ctypes.c_uint8)
        self._prototype("ZAux_OpenCom", [ctypes.c_uint32, ctypes.POINTER(handle)])
        self._prototype("ZAux_Close", [handle])
        self._prototype("ZAux_Execute", [handle, ctypes.c_char_p, ctypes.POINTER(ctypes.c_char), ctypes.c_uint32])
        self._prototype("ZAux_SetTimeOut", [handle, ctypes.c_uint32])
        for name in ("ZAux_Direct_GetUnits", "ZAux_Direct_GetAccel", "ZAux_Direct_GetDecel",
                     "ZAux_Direct_GetDpos", "ZAux_Direct_GetMpos", "ZAux_Direct_GetRsLimit",
                     "ZAux_Direct_GetFsLimit"):
            self._prototype(name, [handle, integer, float_pointer])
        self._prototype("ZAux_Direct_GetIfIdle", [handle, integer, int_pointer])
        self._prototype("ZAux_Direct_GetAxisStatus", [handle, integer, int_pointer])
        self._prototype("ZAux_Direct_SetSpeed", [handle, integer, ctypes.c_float])
        self._prototype("ZAux_Direct_SetAccel", [handle, integer, ctypes.c_float])
        self._prototype("ZAux_Direct_SetDecel", [handle, integer, ctypes.c_float])
        self._prototype("ZAux_Direct_SetMpos", [handle, integer, ctypes.c_float])
        self._prototype("ZAux_Direct_SetDpos", [handle, integer, ctypes.c_float])
        self._prototype("ZAux_Direct_Single_Move", [handle, integer, ctypes.c_float])
        self._prototype("ZAux_Direct_Single_MoveAbs", [handle, integer, ctypes.c_float])
        self._prototype("ZAux_Direct_Single_Vmove", [handle, integer, integer])
        self._prototype("ZAux_Direct_Single_Cancel", [handle, integer, integer])
        axis_pointer = ctypes.POINTER(integer)
        self._prototype("ZAux_Direct_Move", [handle, integer, axis_pointer, float_pointer])
        self._prototype("ZAux_Direct_MoveAbs", [handle, integer, axis_pointer, float_pointer])
        circle_args = [handle, integer, axis_pointer] + [ctypes.c_float] * 4
        self._prototype("ZAux_Direct_MoveCirc", circle_args + [integer])
        self._prototype("ZAux_Direct_MoveCircAbs", circle_args + [integer])
        self._prototype("ZAux_Direct_MoveCirc2", circle_args)
        self._prototype("ZAux_Direct_MoveCirc2Abs", circle_args)
        self._prototype("ZAux_Direct_SetMerge", [handle, integer, integer])
        self._prototype("ZAux_Direct_SetLspeed", [handle, integer, ctypes.c_float])
        self._prototype("ZAux_Direct_SetCornerMode", [handle, integer, integer])
        self._prototype("ZAux_Direct_SetDecelAngle", [handle, integer, ctypes.c_float])
        self._prototype("ZAux_Direct_SetStopAngle", [handle, integer, ctypes.c_float])
        self._prototype("ZAux_Direct_SetFullSpRadius", [handle, integer, ctypes.c_float])
        self._prototype("ZAux_Direct_SetZsmooth", [handle, integer, ctypes.c_float])
        self._prototype("ZAux_Direct_SetTable", [handle, integer, integer, float_pointer])
        self._prototype("ZAux_Direct_HwPswitch2", [handle, integer, integer, integer, integer] + [ctypes.c_float] * 4)
        self._prototype("ZAux_Direct_HwTimer", [handle, integer, integer, integer, integer, integer, integer])
        self._prototype("ZAux_Trigger", [handle])
        self._prototype("ZAux_Direct_GetRemain_LineBuffer", [handle, integer, int_pointer])
        self._prototype("ZAux_Direct_MultiMoveAbs", [handle, integer, integer, axis_pointer, float_pointer])
        self._prototype("ZAux_Modbus_Set0x", [handle, ctypes.c_uint16, ctypes.c_uint16, byte_pointer])
        self._prototype("ZAux_Modbus_Get0x", [handle, ctypes.c_uint16, ctypes.c_uint16, byte_pointer])
        self._prototype("ZAux_Modbus_Get4x_Float", [handle, ctypes.c_uint16, ctypes.c_uint16, float_pointer])

    def _claim_sdk_thread(self) -> None:
        thread_id = threading.get_ident()
        if self._owner_thread_id is None:
            self._owner_thread_id = thread_id
        elif self._owner_thread_id != thread_id:
            raise SdkThreadError("厂家 SDK 只能由创建连接的单一工作线程调用")

    def _ensure_open(self) -> None:
        if not self.is_open:
            raise SdkError("设备连接", -1, "控制器尚未连接")

    def _call(self, name: str, *args: object) -> None:
        self._claim_sdk_thread()
        self._ensure_open()
        code = int(getattr(self._dll, name)(self._handle, *args))
        if code != 0:
            raise SdkError(name, code)

    def open_com(self, port: str) -> None:
        self._claim_sdk_thread()
        if self.is_open:
            self.close()
        code = int(self._dll.ZAux_OpenCom(parse_com_port(port), ctypes.byref(self._handle)))
        if code != 0 or not self.is_open:
            self._handle = ctypes.c_void_p()
            raise SdkError("ZAux_OpenCom", code, port)
        self._call("ZAux_SetTimeOut", ctypes.c_uint32(1000))

    def close(self) -> None:
        if not self.is_open:
            return
        self._claim_sdk_thread()
        code = int(self._dll.ZAux_Close(self._handle))
        self._handle = ctypes.c_void_p()
        if code != 0:
            raise SdkError("ZAux_Close", code)

    def _get_float(self, name: str, axis: int) -> float:
        value = ctypes.c_float()
        self._call(name, axis, ctypes.byref(value))
        return float(value.value)

    def _get_int(self, name: str, axis: int) -> int:
        value = ctypes.c_int()
        self._call(name, axis, ctypes.byref(value))
        return int(value.value)

    def get_units(self, axis: int) -> float: return self._get_float("ZAux_Direct_GetUnits", axis)
    def get_accel(self, axis: int) -> float: return self._get_float("ZAux_Direct_GetAccel", axis)
    def get_decel(self, axis: int) -> float: return self._get_float("ZAux_Direct_GetDecel", axis)
    def get_dpos(self, axis: int) -> float: return self._get_float("ZAux_Direct_GetDpos", axis)
    def get_mpos(self, axis: int) -> float: return self._get_float("ZAux_Direct_GetMpos", axis)
    def get_negative_soft_limit(self, axis: int) -> float: return self._get_float("ZAux_Direct_GetRsLimit", axis)
    def get_positive_soft_limit(self, axis: int) -> float: return self._get_float("ZAux_Direct_GetFsLimit", axis)
    def is_idle(self, axis: int) -> bool: return self._get_int("ZAux_Direct_GetIfIdle", axis) == -1
    def get_axis_status(self, axis: int) -> int: return self._get_int("ZAux_Direct_GetAxisStatus", axis)

    @staticmethod
    def _base(axis: int) -> int:
        if axis not in (0, 1):
            raise ValueError(f"FMC01-02H 仅支持轴 0 和轴 1，收到轴 {axis}")
        return axis * 100

    def _modbus_get_bit(self, address: int) -> bool:
        value = (ctypes.c_uint8 * 1)()
        self._call("ZAux_Modbus_Get0x", address, 1, value)
        return bool(value[0])

    def _modbus_set_bit(self, address: int, enabled: bool) -> None:
        value = (ctypes.c_uint8 * 1)(1 if enabled else 0)
        self._call("ZAux_Modbus_Set0x", address, 1, value)

    def _modbus_get_float(self, address: int) -> float:
        value = (ctypes.c_float * 1)()
        self._call("ZAux_Modbus_Get4x_Float", address, 1, value)
        return float(value[0])

    def get_home_speed(self, axis: int) -> float: return self._modbus_get_float(self._base(axis) + 8)
    def get_home_offset(self, axis: int) -> float: return self._modbus_get_float(self._base(axis) + 12)
    def is_homed(self, axis: int) -> bool: return self._modbus_get_bit(self._base(axis) + 10)
    def set_enabled(self, axis: int, enabled: bool) -> None: self._modbus_set_bit(self._base(axis) + 15, enabled)
    def clear_error(self, axis: int) -> None: self._modbus_set_bit(self._base(axis) + 18, True)
    def home(self, axis: int) -> None: self._modbus_set_bit(self._base(axis), True)

    def execute(self, command: str, response_size: int = 256) -> str:
        response = ctypes.create_string_buffer(response_size)
        self._call("ZAux_Execute", command.encode("ascii"), response, response_size)
        return response.value.decode("ascii", errors="replace").strip()

    def get_drive_status(self, axis: int) -> int:
        response = self.execute(f"?DRIVE_STATUS({axis})")
        match = re.search(r"-?\d+", response)
        if match is None:
            raise SdkError("ZAux_Execute", -2, f"无法解析 DRIVE_STATUS：{response!r}")
        return int(match.group())

    @staticmethod
    def drive_is_enabled(drive_status: int) -> bool: return drive_status & 0x6F == 0x27
    @staticmethod
    def drive_is_error(drive_status: int) -> bool: return (drive_status & 0x4F) == 0x08
    def set_speed(self, axis: int, value: float) -> None:
        self._call("ZAux_Direct_SetSpeed", axis, ctypes.c_float(value))
    def configure_continuous_path(self, axis: int, speed: float, accel: float, decel: float) -> None:
        """采用厂家连续小线段例程的合并与拐角参数。"""
        self._call("ZAux_Direct_SetSpeed", axis, ctypes.c_float(speed))
        self._call("ZAux_Direct_SetAccel", axis, ctypes.c_float(accel))
        self._call("ZAux_Direct_SetDecel", axis, ctypes.c_float(decel))
        self._call("ZAux_Direct_SetMerge", axis, 1)
        self._call("ZAux_Direct_SetLspeed", axis, ctypes.c_float(0.0))
        self._call("ZAux_Direct_SetCornerMode", axis, 0)
        self._call("ZAux_Direct_SetDecelAngle", axis, ctypes.c_float(math.radians(60.0)))
        self._call("ZAux_Direct_SetStopAngle", axis, ctypes.c_float(math.radians(120.0)))
        self._call("ZAux_Direct_SetFullSpRadius", axis, ctypes.c_float(5.0))
        self._call("ZAux_Direct_SetZsmooth", axis, ctypes.c_float(5.0))
    def abort_home(self, axis: int) -> None: self._call("ZAux_Direct_Single_Cancel", axis, 2)
    def move_relative(self, axis: int, distance: float) -> None:
        self._call("ZAux_Direct_Single_Move", axis, ctypes.c_float(distance))

    def move_absolute(self, axis: int, position: float) -> None:
        self._call("ZAux_Direct_Single_MoveAbs", axis, ctypes.c_float(position))

    def jog(self, axis: int, direction: int) -> None:
        if direction not in (-1, 1):
            raise ValueError("点动方向必须是 -1 或 1")
        self._call("ZAux_Direct_Single_Vmove", axis, direction)

    def set_mpos(self, axis: int, value: float) -> None:
        self._call("ZAux_Direct_SetMpos", axis, ctypes.c_float(value))

    def set_dpos(self, axis: int, value: float) -> None:
        self._call("ZAux_Direct_SetDpos", axis, ctypes.c_float(value))

    def zero(self, axis: int) -> None:
        self.set_mpos(axis, 0.0)
        self.set_dpos(axis, 0.0)

    @staticmethod
    def _axis_array(axes: tuple[int, int]):
        return (ctypes.c_int * len(axes))(*axes)

    @staticmethod
    def _float_array(values: tuple[float, ...]):
        return (ctypes.c_float * len(values))(*values)

    def move_xy_absolute(self, x: float, y: float) -> None:
        axes = (ctypes.c_int * 2)(0, 1)
        positions = (ctypes.c_float * 2)(x, y)
        self._call("ZAux_Direct_MoveAbs", 2, axes, positions)

    def interpolate_line(self, axes: tuple[int, int], values: tuple[float, float], absolute: bool) -> None:
        self._call("ZAux_Direct_MoveAbs" if absolute else "ZAux_Direct_Move",
                   len(axes), self._axis_array(axes), self._float_array(values))

    def interpolate_arc_center(self, axes: tuple[int, int], end: tuple[float, float],
                               center: tuple[float, float], direction: int, absolute: bool) -> None:
        self._call("ZAux_Direct_MoveCircAbs" if absolute else "ZAux_Direct_MoveCirc",
                   len(axes), self._axis_array(axes), *(ctypes.c_float(value) for value in (*end, *center)), direction)

    def interpolate_arc_three_point(self, axes: tuple[int, int], middle: tuple[float, float],
                                    end: tuple[float, float], absolute: bool) -> None:
        self._call("ZAux_Direct_MoveCirc2Abs" if absolute else "ZAux_Direct_MoveCirc2",
                   len(axes), self._axis_array(axes), *(ctypes.c_float(value) for value in (*middle, *end)))

    def configure_position_trigger(self, config: PreparedPositionTrigger) -> None:
        values = self._float_array(config.positions)
        table_end = config.table_start + len(config.positions) - 1
        self.stop_position_trigger(config.axis)
        self._call("ZAux_Direct_SetTable", config.table_start, len(config.positions), values)
        self._call("ZAux_Direct_HwPswitch2", config.axis, 1, config.output, config.active_state,
                   ctypes.c_float(config.table_start), ctypes.c_float(table_end),
                   ctypes.c_float(config.direction), ctypes.c_float(0))
        self._call("ZAux_Direct_HwTimer", 2, config.cycle_us, config.pulse_width_us,
                   len(config.positions), 1 - config.active_state, config.output)
        self._call("ZAux_Trigger")

    def stop_position_trigger(self, axis: int) -> None:
        self._call("ZAux_Direct_HwPswitch2", axis, 2, 0, 0,
                   ctypes.c_float(0), ctypes.c_float(0), ctypes.c_float(0), ctypes.c_float(0))
        self._call("ZAux_Direct_HwTimer", 0, 0, 0, 0, 0, 0)

    def get_remain_line_buffer(self, axis: int) -> int:
        return self._get_int("ZAux_Direct_GetRemain_LineBuffer", axis)

    def multi_move_absolute(self, axes: tuple[int, int], points: tuple[tuple[float, float], ...]) -> None:
        flattened = tuple(value for point in points for value in point)
        self._call("ZAux_Direct_MultiMoveAbs", len(points), len(axes),
                   self._axis_array(axes), self._float_array(flattened))

    def stop(self, axis: int) -> None:
        self._call("ZAux_Direct_Single_Cancel", axis, 3)


class FakeMotionSdk:
    """可注入参数、限位和状态的同步 Fake，默认移动立即到位。"""

    def __init__(self, profile: DeviceProfile, *, enabled: bool = True) -> None:
        self.profile = profile
        self._is_open = False
        self.positions = {0: 0.0, 1: 0.0}
        self.motor_positions = {0: 0.0, 1: 0.0}
        self.idle = {0: True, 1: True}
        self.axis_status = {0: 0, 1: 0}
        self.homed = {0: False, 1: False}
        self.enabled = {0: enabled, 1: enabled}
        self.speeds = {item.axis: item.default_speed for item in profile.axes}
        self.soft_limits = {item.axis: (item.min_position, item.max_position) for item in profile.axes}
        self.parameter_overrides: dict[tuple[int, str], float] = {}
        self.move_history: list[tuple[float, float]] = []
        self.axis_move_history: list[tuple[str, int, float]] = []
        self.jog_history: list[tuple[int, int]] = []
        self.home_history: list[int] = []
        self.abort_home_history: list[int] = []
        self.stop_history: list[int] = []
        self.line_history: list[tuple[tuple[float, float], bool]] = []
        self.arc_history: list[tuple[str, tuple[float, float], tuple[float, float], int | None, bool]] = []
        self.trigger_config: PreparedPositionTrigger | None = None
        self.trigger_stop_history: list[int] = []
        self.path_history: list[tuple[tuple[float, float], ...]] = []
        self.call_thread_ids: list[int] = []
        self._owner_thread_id: int | None = None
        self.auto_complete_home = True
        self._home_pending_polls = {0: 0, 1: 0}
        self.opened_port: str | None = None

    @property
    def is_open(self) -> bool: return self._is_open

    def _require_open(self) -> None:
        self._record_call()
        if not self._is_open:
            raise SdkError("设备连接", -1, "Fake 控制器尚未连接")

    def _record_call(self) -> None:
        thread_id = threading.get_ident()
        self.call_thread_ids.append(thread_id)
        if self._owner_thread_id is None:
            self._owner_thread_id = thread_id
        elif self._owner_thread_id != thread_id:
            raise SdkThreadError("Fake SDK 也只能由唯一工作线程调用")

    def open_com(self, port: str) -> None:
        self._record_call()
        parse_com_port(port)
        self._is_open, self.opened_port = True, port.upper()

    def close(self) -> None:
        self._record_call()
        self._is_open, self.opened_port = False, None

    def _parameter(self, axis: int, name: str) -> float:
        self._require_open()
        return self.parameter_overrides.get((axis, name), float(getattr(self.profile.axis(axis), name)))

    def get_units(self, axis: int) -> float: return self._parameter(axis, "units")
    def get_accel(self, axis: int) -> float: return self._parameter(axis, "accel")
    def get_decel(self, axis: int) -> float: return self._parameter(axis, "decel")
    def get_home_speed(self, axis: int) -> float: return self._parameter(axis, "home_speed")
    def get_home_offset(self, axis: int) -> float: return self._parameter(axis, "home_offset")
    def get_dpos(self, axis: int) -> float: self._require_open(); return self.positions[axis]
    def get_mpos(self, axis: int) -> float: self._require_open(); return self.motor_positions[axis]
    def get_negative_soft_limit(self, axis: int) -> float: self._require_open(); return self.soft_limits[axis][0]
    def get_positive_soft_limit(self, axis: int) -> float: self._require_open(); return self.soft_limits[axis][1]
    def is_idle(self, axis: int) -> bool:
        self._require_open()
        if self._home_pending_polls[axis] > 0:
            self._home_pending_polls[axis] -= 1
            if self._home_pending_polls[axis] == 0:
                self.idle[axis] = True
                self.homed[axis] = True
            return False
        return self.idle[axis]
    def get_axis_status(self, axis: int) -> int: self._require_open(); return self.axis_status[axis]
    def is_homed(self, axis: int) -> bool: self._require_open(); return self.homed[axis]

    def get_drive_status(self, axis: int) -> int:
        self._require_open()
        return 0x27 if self.enabled[axis] else 0

    @staticmethod
    def drive_is_enabled(drive_status: int) -> bool: return drive_status & 0x6F == 0x27
    @staticmethod
    def drive_is_error(drive_status: int) -> bool: return (drive_status & 0x4F) == 0x08
    def set_enabled(self, axis: int, enabled: bool) -> None: self._require_open(); self.enabled[axis] = enabled
    def clear_error(self, axis: int) -> None: self._require_open(); self.axis_status[axis] = 0
    def set_speed(self, axis: int, value: float) -> None: self._require_open(); self.speeds[axis] = value

    def home(self, axis: int) -> None:
        self._require_open()
        self.home_history.append(axis)
        self.idle[axis] = False
        self.homed[axis] = False
        if self.auto_complete_home:
            self.positions[axis] = self.motor_positions[axis] = 0.0
            self._home_pending_polls[axis] = 1

    def abort_home(self, axis: int) -> None:
        self._require_open()
        self.abort_home_history.append(axis)
        self._home_pending_polls[axis] = 0
        self.idle[axis] = True

    def move_relative(self, axis: int, distance: float) -> None:
        self._require_open()
        self.positions[axis] += float(distance)
        self.motor_positions[axis] += float(distance)
        self.axis_move_history.append(("relative", axis, float(distance)))

    def move_absolute(self, axis: int, position: float) -> None:
        self._require_open()
        self.positions[axis] = float(position)
        self.motor_positions[axis] = float(position)
        self.axis_move_history.append(("absolute", axis, float(position)))

    def move_xy_absolute(self, x: float, y: float) -> None:
        self._require_open()
        self.positions.update({0: float(x), 1: float(y)})
        self.motor_positions.update({0: float(x), 1: float(y)})
        self.move_history.append((float(x), float(y)))

    def jog(self, axis: int, direction: int) -> None:
        self._require_open()
        if direction not in (-1, 1):
            raise ValueError("点动方向必须是 -1 或 1")
        self.idle[axis] = False
        self.jog_history.append((axis, direction))

    def set_dpos(self, axis: int, value: float) -> None:
        self._require_open()
        # 原厂文档只保证写 DPOS 转换为 OFFPOS 偏移且不移动电机；没有
        # 规定该调用会改写 RS_LIMIT/FS_LIMIT，因此 Fake 不模拟未证实副作用。
        self.positions[axis] = float(value)

    def set_mpos(self, axis: int, value: float) -> None:
        self._require_open()
        self.motor_positions[axis] = float(value)

    def zero(self, axis: int) -> None:
        self.set_mpos(axis, 0.0)
        self.set_dpos(axis, 0.0)

    def interpolate_line(self, axes: tuple[int, int], values: tuple[float, float], absolute: bool) -> None:
        self._require_open()
        targets = values if absolute else tuple(self.positions[axis] + value for axis, value in zip(axes, values))
        for axis, target in zip(axes, targets):
            self.positions[axis] = self.motor_positions[axis] = float(target)
        self.line_history.append((tuple(float(value) for value in values), absolute))

    def interpolate_arc_center(self, axes: tuple[int, int], end: tuple[float, float],
                               center: tuple[float, float], direction: int, absolute: bool) -> None:
        self._require_open()
        targets = end if absolute else tuple(self.positions[axis] + value for axis, value in zip(axes, end))
        for axis, target in zip(axes, targets):
            self.positions[axis] = self.motor_positions[axis] = float(target)
        self.arc_history.append(("center", end, center, direction, absolute))

    def interpolate_arc_three_point(self, axes: tuple[int, int], middle: tuple[float, float],
                                    end: tuple[float, float], absolute: bool) -> None:
        self._require_open()
        targets = end if absolute else tuple(self.positions[axis] + value for axis, value in zip(axes, end))
        for axis, target in zip(axes, targets):
            self.positions[axis] = self.motor_positions[axis] = float(target)
        self.arc_history.append(("three_point", end, middle, None, absolute))

    def configure_position_trigger(self, config: PreparedPositionTrigger) -> None:
        self._require_open(); self.trigger_config = config

    def stop_position_trigger(self, axis: int) -> None:
        self._require_open(); self.trigger_config = None; self.trigger_stop_history.append(axis)

    def configure_continuous_path(self, axis: int, speed: float, accel: float, decel: float) -> None:
        self._require_open(); self.speeds[axis] = speed

    def get_remain_line_buffer(self, axis: int) -> int:
        self._require_open(); return 0

    def multi_move_absolute(self, axes: tuple[int, int], points: tuple[tuple[float, float], ...]) -> None:
        self._require_open()
        if points:
            for axis, target in zip(axes, points[-1]):
                self.positions[axis] = self.motor_positions[axis] = float(target)
        self.path_history.append(points)

    def stop(self, axis: int) -> None:
        self._require_open()
        self.idle[axis] = True
        self.stop_history.append(axis)


# 兼容更直观的测试/接线命名。
FakeSdk = FakeMotionSdk
