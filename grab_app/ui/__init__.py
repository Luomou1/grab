from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .spatial_map import SpatialMapModel, SpatialMapWidget, SpatialTile

if TYPE_CHECKING:
    from .main_window import MainWindow


def __getattr__(name: str) -> Any:
    # 保留原有 MainWindow 导出，同时避免独立控件导入相机、位移台等运行时依赖。
    if name == "MainWindow":
        from .main_window import MainWindow

        return MainWindow
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["MainWindow", "SpatialMapModel", "SpatialMapWidget", "SpatialTile"]
