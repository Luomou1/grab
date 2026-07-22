"""供扫描总控依赖的位移台 facade 协议。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import DeviceSnapshot


@runtime_checkable
class XYStageProtocol(Protocol):
    @property
    def connected(self) -> bool: ...
    @property
    def snapshot(self) -> DeviceSnapshot: ...
    def connect(self, port: str) -> DeviceSnapshot: ...
    def disconnect(self) -> None: ...
    def refresh_status(self) -> DeviceSnapshot: ...
    def move_absolute(self, x: float, y: float, *, speed: float | None = None,
                      wait: bool = False, timeout_s: float = 30.0,
                      tolerance_mm: float = 0.005) -> DeviceSnapshot: ...
    def wait_until_idle(self, *, timeout_s: float = 30.0,
                        tolerance_mm: float = 0.005) -> DeviceSnapshot: ...
    def stop_all(self) -> DeviceSnapshot: ...


# 简短别名便于在服务层注解依赖。
StageProtocol = XYStageProtocol
