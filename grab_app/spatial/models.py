"""空间拼图使用的数据模型。

模型只保存值和轻量校验，算法实现放在同目录的其它模块中，避免 UI 与硬件层耦合。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any


def _finite(value: float, name: str) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} 必须是有限数")
    return value


@dataclass(frozen=True)
class StagePoint:
    """位移台样品坐标，单位为 mm。"""

    x_mm: float
    y_mm: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "x_mm", _finite(self.x_mm, "x_mm"))
        object.__setattr__(self, "y_mm", _finite(self.y_mm, "y_mm"))

    def as_tuple(self) -> tuple[float, float]:
        return self.x_mm, self.y_mm


@dataclass(frozen=True)
class SpatialRect:
    """样品坐标中的轴对齐矩形，单位为 mm。"""

    x_min_mm: float
    y_min_mm: float
    x_max_mm: float
    y_max_mm: float

    def __post_init__(self) -> None:
        vals = (self.x_min_mm, self.y_min_mm, self.x_max_mm, self.y_max_mm)
        vals = tuple(_finite(v, n) for v, n in zip(vals, ("x_min_mm", "y_min_mm", "x_max_mm", "y_max_mm")))
        if vals[2] < vals[0] or vals[3] < vals[1]:
            raise ValueError("矩形上限不能小于下限")
        object.__setattr__(self, "x_min_mm", vals[0])
        object.__setattr__(self, "y_min_mm", vals[1])
        object.__setattr__(self, "x_max_mm", vals[2])
        object.__setattr__(self, "y_max_mm", vals[3])

    @property
    def width_mm(self) -> float:
        return self.x_max_mm - self.x_min_mm

    @property
    def height_mm(self) -> float:
        return self.y_max_mm - self.y_min_mm

    @property
    def center(self) -> StagePoint:
        return StagePoint((self.x_min_mm + self.x_max_mm) / 2, (self.y_min_mm + self.y_max_mm) / 2)

    def contains(self, point: StagePoint, tolerance: float = 0.0) -> bool:
        return (self.x_min_mm - tolerance <= point.x_mm <= self.x_max_mm + tolerance and
                self.y_min_mm - tolerance <= point.y_mm <= self.y_max_mm + tolerance)

    def contains_rect(self, other: "SpatialRect", tolerance: float = 0.0) -> bool:
        return (self.x_min_mm - tolerance <= other.x_min_mm and
                other.x_max_mm <= self.x_max_mm + tolerance and
                self.y_min_mm - tolerance <= other.y_min_mm and
                other.y_max_mm <= self.y_max_mm + tolerance)


@dataclass(frozen=True)
class SpatialCalibration:
    """二维仿射标定，``pixel = matrix @ stage + offset``。

    ``matrix`` 的单位是 px/mm，``offset`` 的单位是 px。使用嵌套 tuple
    保证状态可直接 JSON 序列化；计算时由 calibration 模块转换为 NumPy 数组。
    """

    matrix: tuple[tuple[float, float], tuple[float, float]]
    offset: tuple[float, float] = (0.0, 0.0)
    pixel_size_um: float = 0.48
    fingerprint: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.matrix) != 2 or any(len(row) != 2 for row in self.matrix):
            raise ValueError("matrix 必须是 2x2")
        matrix = tuple(tuple(_finite(v, "matrix") for v in row) for row in self.matrix)
        offset = tuple(_finite(v, "offset") for v in self.offset)
        if len(offset) != 2:
            raise ValueError("offset 必须有两个元素")
        if abs(matrix[0][0] * matrix[1][1] - matrix[0][1] * matrix[1][0]) < 1e-15:
            raise ValueError("标定矩阵不可逆")
        object.__setattr__(self, "matrix", matrix)
        object.__setattr__(self, "offset", offset)
        pixel_size_um = _finite(self.pixel_size_um, "pixel_size_um")
        if pixel_size_um <= 0:
            raise ValueError("pixel_size_um 必须大于 0")
        object.__setattr__(self, "pixel_size_um", pixel_size_um)

    def to_dict(self) -> dict[str, Any]:
        return {"matrix": [list(row) for row in self.matrix], "offset": list(self.offset),
                "pixel_size_um": self.pixel_size_um, "fingerprint": dict(self.fingerprint)}


@dataclass(frozen=True)
class CalibrationFit:
    """多点仿射拟合结果及每个点的残差（单位 px）。"""

    calibration: SpatialCalibration
    residuals_px: tuple[float, ...]
    rms_px: float


@dataclass(frozen=True)
class TilePlacement:
    """一个瓦片的网格索引、目标位置和覆盖边界。"""

    row: int
    column: int
    target: StagePoint
    bounds: SpatialRect
    sequence: int = 0

    @property
    def index(self) -> tuple[int, int]:
        return self.row, self.column


@dataclass(frozen=True)
class TilePlan:
    """完整覆盖网格及路线元数据。"""

    roi: SpatialRect
    placements: tuple[TilePlacement, ...]
    frame_size_px: tuple[int, int]
    overlap: float
    route: str
    tile_size_mm: tuple[float, float]
    spacing_mm: tuple[float, float]
    rows: int
    columns: int
    estimated_distance_mm: float = 0.0

    @property
    def tile_count(self) -> int:
        return len(self.placements)
