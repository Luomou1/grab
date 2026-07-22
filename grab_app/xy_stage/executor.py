from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .controller import MotionTimeoutError, XYStage
from .models import DeviceProfile, DeviceSnapshot, load_default_profile
from .safety import LIMIT_MASK
from .sdk import FmcSdk
from .trajectory import ArcMove, LineMove, Point2D, PositionTriggerConfig


class MotionCancelledError(RuntimeError):
    """自动任务等待 XY 到位期间收到取消请求。"""


@dataclass
class _Request:
    operation: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    response: queue.Queue[tuple[Any | None, Exception | None]] = field(
        default_factory=lambda: queue.Queue(maxsize=1)
    )


class XYStageExecutor:
    """在唯一后台线程中持有 FMC SDK 和 XYStage。

    同步外观便于扫描总控使用，但所有真正的 DLL 调用都在 executor 线程内。
    ``move_absolute_blocking`` 在同一线程轮询到位，并同时观察取消/急停事件，
    因而不会出现“停止命令排在长时间等待命令之后”的死锁。
    """

    def __init__(
        self,
        dll_dir: str | Path | None = None,
        *,
        profile: DeviceProfile | None = None,
        stage_factory: Callable[[], XYStage] | None = None,
    ) -> None:
        if stage_factory is None and dll_dir is None:
            raise ValueError("必须提供 dll_dir 或 stage_factory")
        self._dll_dir = None if dll_dir is None else Path(dll_dir)
        self._profile = profile or load_default_profile()
        self._stage_factory = stage_factory
        self._requests: queue.Queue[_Request | None] = queue.Queue(maxsize=128)
        self._emergency = threading.Event()
        self._shutdown = threading.Event()
        self._snapshot_lock = threading.Lock()
        self._snapshot = DeviceSnapshot()
        self._ready = threading.Event()
        self._startup_error: Exception | None = None
        self._thread = threading.Thread(target=self._run, name="xy-stage-sdk-worker", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)
        if self._startup_error is not None:
            raise RuntimeError(f"XY 位移台工作线程启动失败: {self._startup_error}") from self._startup_error
        if not self._ready.is_set():
            raise RuntimeError("XY 位移台工作线程启动超时")

    @property
    def connected(self) -> bool:
        with self._snapshot_lock:
            return bool(self._snapshot.connected)

    @property
    def snapshot(self) -> DeviceSnapshot:
        with self._snapshot_lock:
            import copy

            return copy.deepcopy(self._snapshot)

    def connect(self, port: str) -> DeviceSnapshot:
        self._emergency.clear()
        return self._call("connect", port)

    def disconnect(self) -> None:
        self._emergency.set()
        self._call("disconnect")

    def refresh_status(self) -> DeviceSnapshot:
        return self._call("refresh_status")

    status_snapshot = refresh_status

    def set_enabled(self, enabled: bool = True) -> DeviceSnapshot:
        return self._call("set_enabled", enabled)

    def set_axis_enabled(self, axis: int, enabled: bool = True) -> DeviceSnapshot:
        return self._call("set_axis_enabled", axis, enabled)

    enable_axis = set_axis_enabled

    def disable_axis(self, axis: int) -> DeviceSnapshot:
        return self.set_axis_enabled(axis, False)

    def clear_errors(self) -> DeviceSnapshot:
        return self._call("clear_errors")

    def clear_axis_error(self, axis: int) -> DeviceSnapshot:
        return self._call("clear_axis_error", axis)

    clear_error = clear_axis_error

    def home_axis(self, axis: int) -> DeviceSnapshot:
        return self._call("home_axis", axis)

    home = home_axis

    def cancel_home(self, axis: int) -> DeviceSnapshot:
        return self._call("cancel_home", axis)

    abort_home = cancel_home

    def move_axis_relative(self, axis: int, distance: float, *, speed: float | None = None) -> DeviceSnapshot:
        return self._call("move_axis_relative", axis, float(distance), speed=speed)

    move_relative = move_axis_relative

    def move_axis_absolute(self, axis: int, position: float, *, speed: float | None = None) -> DeviceSnapshot:
        return self._call("move_axis_absolute", axis, float(position), speed=speed)

    def start_jog(self, axis: int, direction: int, *, speed: float | None = None) -> DeviceSnapshot:
        return self._call("start_jog", axis, direction, speed=speed)

    jog = start_jog
    jog_start = start_jog

    def stop_axis(self, axis: int) -> DeviceSnapshot:
        return self._call("stop_axis", axis)

    stop_jog = stop_axis
    jog_stop = stop_axis
    axis_stop = stop_axis

    def zero_axis(self, axis: int) -> DeviceSnapshot:
        return self._call("zero_axis", axis)

    zero = zero_axis
    set_dpos_zero = zero_axis

    def set_axis_speed(self, axis: int, speed: float) -> DeviceSnapshot:
        return self._call("set_axis_speed", axis, float(speed))

    set_speed = set_axis_speed

    def interpolate_line(self, move: LineMove) -> DeviceSnapshot:
        return self._call("interpolate_line", move)

    def move_line(self, x: float, y: float, *, absolute: bool = True,
                  speed: float = 0.5) -> DeviceSnapshot:
        return self._call("move_line", float(x), float(y), absolute=absolute, speed=float(speed))

    def interpolate_arc(self, move: ArcMove) -> DeviceSnapshot:
        return self._call("interpolate_arc", move)

    def configure_position_trigger(self, config: PositionTriggerConfig) -> DeviceSnapshot:
        return self._call("configure_position_trigger", config)

    def stop_position_trigger(self, axis: int) -> DeviceSnapshot:
        return self._call("stop_position_trigger", axis)

    def run_linear_path_blocking(
        self,
        points: tuple[Point2D, ...],
        *,
        speed: float,
        window_size: int = 8,
        timeout_seconds: float = 60.0,
        cancel_event: threading.Event | None = None,
    ) -> DeviceSnapshot:
        self._emergency.clear()
        return self._call(
            "run_linear_path_blocking", tuple(points), speed=float(speed),
            window_size=window_size, timeout_seconds=float(timeout_seconds),
            cancel_event=cancel_event, timeout=max(60.0, float(timeout_seconds) + 5.0),
        )

    def move_absolute_blocking(
        self,
        x_mm: float,
        y_mm: float,
        *,
        timeout_seconds: float = 30.0,
        cancel_event: threading.Event | None = None,
        tolerance_mm: float = 0.005,
        speed: float | None = None,
    ) -> tuple[float, float]:
        self._emergency.clear()
        snapshot = self._call(
            "move_absolute_blocking",
            float(x_mm),
            float(y_mm),
            timeout_seconds=float(timeout_seconds),
            cancel_event=cancel_event,
            tolerance_mm=float(tolerance_mm),
            speed=speed,
        )
        return snapshot.axes[0].dpos, snapshot.axes[1].dpos

    def stop_all(self) -> None:
        self._emergency.set()
        if self.connected:
            self._call("stop_all")

    def close(self) -> None:
        if self._shutdown.is_set():
            return
        self._emergency.set()
        try:
            if self.connected:
                self._call("disconnect", timeout=10.0)
        finally:
            self._shutdown.set()
            self._requests.put(None)
            self._thread.join(timeout=5.0)

    def _call(self, operation: str, *args: Any, timeout: float = 60.0, **kwargs: Any):
        if self._shutdown.is_set():
            raise RuntimeError("XY 位移台工作线程已关闭")
        request = _Request(operation, args, kwargs)
        self._requests.put(request, timeout=2.0)
        try:
            value, error = request.response.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError(f"XY 位移台命令等待超时: {operation}") from exc
        if error is not None:
            raise error
        return value

    def _run(self) -> None:
        stage: XYStage | None = None
        try:
            stage = (
                self._stage_factory()
                if self._stage_factory is not None
                else XYStage(FmcSdk(self._dll_dir), self._profile)
            )
            self._ready.set()
            poll_seconds = max(0.1, min(1.0, stage.profile.poll_interval_ms / 1000.0))
            while not self._shutdown.is_set():
                try:
                    request = self._requests.get(timeout=poll_seconds)
                except queue.Empty:
                    if stage.connected:
                        try:
                            self._set_snapshot(stage.refresh_status(preserve_fault=True))
                        except Exception:
                            self._set_snapshot(stage.snapshot)
                    continue
                if request is None:
                    return
                try:
                    result = self._execute(stage, request)
                    if isinstance(result, DeviceSnapshot):
                        self._set_snapshot(result)
                    request.response.put((result, None))
                except Exception as exc:
                    try:
                        self._set_snapshot(stage.snapshot)
                    except Exception:
                        pass
                    request.response.put((None, exc))
        except Exception as exc:
            self._startup_error = exc
            self._ready.set()
        finally:
            if stage is not None and stage.connected:
                try:
                    stage.disconnect()
                except Exception:
                    pass

    def _execute(self, stage: XYStage, request: _Request):
        if request.operation == "move_absolute_blocking":
            return self._move_and_wait(stage, *request.args, **request.kwargs)
        if request.operation == "run_linear_path_blocking":
            return self._run_linear_path(stage, *request.args, **request.kwargs)
        operation = getattr(stage, request.operation)
        result = operation(*request.args, **request.kwargs)
        if request.operation == "disconnect":
            result = stage.snapshot
        return result

    def _move_and_wait(
        self,
        stage: XYStage,
        x_mm: float,
        y_mm: float,
        *,
        timeout_seconds: float,
        cancel_event: threading.Event | None,
        tolerance_mm: float,
        speed: float | None,
    ) -> DeviceSnapshot:
        stage.move_absolute(x_mm, y_mm, speed=speed, wait=False)
        deadline = time.monotonic() + timeout_seconds
        poll_seconds = max(0.02, min(0.2, stage.profile.poll_interval_ms / 1000.0))
        while True:
            if self._emergency.is_set() or (cancel_event is not None and cancel_event.is_set()):
                stage.stop_all()
                raise MotionCancelledError("XY 位移已取消")
            snapshot = stage.refresh_status(preserve_fault=True)
            if snapshot.fault_message and any(axis.hard_fault for axis in snapshot.axes.values()):
                stage.stop_all()
                raise RuntimeError(snapshot.fault_message)
            limited_axes = [
                axis for axis, status in snapshot.axes.items() if status.axis_status & LIMIT_MASK
            ]
            if limited_axes:
                stage.stop_all()
                details = []
                for axis in limited_axes:
                    status = snapshot.axes[axis]
                    directions = []
                    if status.axis_status & 0x10:
                        directions.append("正硬限位")
                    if status.axis_status & 0x20:
                        directions.append("负硬限位")
                    if status.axis_status & 0x200:
                        directions.append("正软限位")
                    if status.axis_status & 0x400:
                        directions.append("负软限位")
                    details.append(
                        f"{stage.profile.axis(axis).name}轴{'/'.join(directions)}，"
                        f"DPOS={status.dpos:.4f} mm，AXISSTATUS=0x{status.axis_status:X}"
                    )
                raise RuntimeError("XY 位移台硬限位触发：" + "；".join(details))
            idle = all(snapshot.axes[axis].idle for axis in (0, 1))
            position_ok = (
                abs(snapshot.axes[0].dpos - x_mm) <= tolerance_mm
                and abs(snapshot.axes[1].dpos - y_mm) <= tolerance_mm
            )
            if idle and position_ok:
                return snapshot
            if time.monotonic() >= deadline:
                stage.stop_all()
                raise MotionTimeoutError(f"等待 XY 位移台到位超时（{timeout_seconds:g} s）")
            time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))

    def _run_linear_path(
        self,
        stage: XYStage,
        points: tuple[Point2D, ...],
        *,
        speed: float,
        window_size: int,
        timeout_seconds: float,
        cancel_event: threading.Event | None,
    ) -> DeviceSnapshot:
        plan = stage.prepare_linear_path(points, speed=speed, window_size=window_size)
        deadline = time.monotonic() + timeout_seconds
        submitted = 0
        final = plan.points[-1]
        poll_seconds = max(0.02, min(0.2, stage.profile.poll_interval_ms / 1000.0))
        while True:
            if self._emergency.is_set() or (cancel_event is not None and cancel_event.is_set()):
                stage.stop_all()
                raise MotionCancelledError("XY 连续轨迹已取消")
            buffered = stage.remaining_line_buffer()
            available = max(plan.window_size - max(buffered, 0), 0)
            if available and submitted < len(plan.points):
                batch = plan.points[submitted:submitted + available]
                stage.submit_linear_path_batch(batch)
                submitted += len(batch)
            snapshot = stage.refresh_status(preserve_fault=True)
            if snapshot.fault_message and any(axis.hard_fault for axis in snapshot.axes.values()):
                stage.stop_all()
                raise RuntimeError(snapshot.fault_message)
            if any(axis.axis_status & LIMIT_MASK for axis in snapshot.axes.values()):
                stage.stop_all()
                raise RuntimeError("XY 位移台限位触发")
            complete = (
                submitted == len(plan.points)
                and stage.remaining_line_buffer() == 0
                and all(snapshot.axes[axis].idle for axis in (0, 1))
                and abs(snapshot.axes[0].dpos - final.x) <= 0.005
                and abs(snapshot.axes[1].dpos - final.y) <= 0.005
            )
            if complete:
                return snapshot
            if time.monotonic() >= deadline:
                stage.stop_all()
                raise MotionTimeoutError(f"等待 XY 连续轨迹完成超时（{timeout_seconds:g} s）")
            time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))

    def _set_snapshot(self, snapshot: DeviceSnapshot) -> None:
        import copy

        with self._snapshot_lock:
            self._snapshot = copy.deepcopy(snapshot)
