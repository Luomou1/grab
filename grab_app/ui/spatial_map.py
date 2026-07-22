from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Iterable, Mapping, Sequence

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QTransform,
)
from PySide6.QtWidgets import QWidget


@dataclass(frozen=True)
class SpatialTile:
    """一块地图瓦片及其当前扫描状态，矩形使用地图像素坐标。"""

    tile_id: str
    rect: QRect
    status: str = "pending"


class SpatialMapModel:
    """保存与设备无关的空间地图状态及二维坐标变换。"""

    def __init__(self, width: int = 0, height: int = 0) -> None:
        self.width = 0
        self.height = 0
        self.tiles: dict[str, SpatialTile] = {}
        self.route: tuple[QPointF, ...] = ()
        self.current_scan_point: QPointF | None = None
        self.selected_roi: QRect | None = None
        self._world_to_map: QTransform | None = None
        self._physical_bounds: tuple[float, float, float, float] | None = None
        self.set_map_size(width, height)

    @property
    def map_size(self) -> QSize:
        return QSize(self.width, self.height)

    @property
    def world_to_map_transform(self) -> QTransform | None:
        return QTransform(self._world_to_map) if self._world_to_map is not None else None

    @property
    def physical_bounds(self) -> tuple[float, float, float, float] | None:
        return self._physical_bounds

    def set_map_size(self, width: int, height: int) -> None:
        width = int(width)
        height = int(height)
        if width < 0 or height < 0:
            raise ValueError("地图尺寸不能为负数")
        self.width = width
        self.height = height
        if self._physical_bounds is not None and width > 0 and height > 0:
            self._apply_physical_bounds_transform()
        if self.selected_roi is not None:
            self.selected_roi = self.clamp_pixel_rect(self.selected_roi)

    def set_world_to_map_transform(
        self, transform: QTransform | Sequence[float]
    ) -> None:
        """设置样品坐标到地图像素的仿射变换。

        六元素序列采用 ``(a, b, c, d, tx, ty)``，即
        ``map_x = a*x + c*y + tx``、``map_y = b*x + d*y + ty``。
        """

        if isinstance(transform, QTransform):
            candidate = QTransform(transform)
        else:
            values = tuple(float(value) for value in transform)
            if len(values) != 6:
                raise ValueError("仿射变换必须包含 6 个元素")
            if not all(isfinite(value) for value in values):
                raise ValueError("仿射变换必须是有限数值")
            a, b, c, d, tx, ty = values
            candidate = QTransform(a, b, 0.0, c, d, 0.0, tx, ty, 1.0)
        _inverse, invertible = candidate.inverted()
        if not invertible:
            raise ValueError("仿射变换不可逆")
        self._world_to_map = candidate
        self._physical_bounds = None

    def set_map_physical_bounds(
        self, min_x: float, min_y: float, max_x: float, max_y: float
    ) -> None:
        """将整幅地图线性映射到给定二维样品坐标边界。"""

        bounds = tuple(float(value) for value in (min_x, min_y, max_x, max_y))
        if not all(isfinite(value) for value in bounds):
            raise ValueError("物理边界必须是有限数值")
        if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            raise ValueError("物理边界的最大值必须大于最小值")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("设置物理边界前必须先设置地图尺寸")
        self._physical_bounds = bounds
        self._apply_physical_bounds_transform()

    def _apply_physical_bounds_transform(self) -> None:
        assert self._physical_bounds is not None
        min_x, min_y, max_x, max_y = self._physical_bounds
        scale_x = self.width / (max_x - min_x)
        scale_y = self.height / (max_y - min_y)
        self._world_to_map = QTransform(
            scale_x,
            0.0,
            0.0,
            0.0,
            scale_y,
            0.0,
            -min_x * scale_x,
            -min_y * scale_y,
            1.0,
        )

    def clamp_pixel_rect(self, rect: QRect | Sequence[int]) -> QRect:
        if not isinstance(rect, QRect):
            values = tuple(int(value) for value in rect)
            if len(values) != 4:
                raise ValueError("像素矩形必须包含 x、y、width、height")
            rect = QRect(*values)
        if self.width <= 0 or self.height <= 0:
            return QRect()
        return rect.normalized().intersected(QRect(0, 0, self.width, self.height))

    def map_pixel_rect_to_sample(
        self, rect: QRect | QRectF | Sequence[float]
    ) -> tuple[float, float, float, float]:
        """把地图像素矩形转换为轴对齐的样品坐标矩形。"""

        if self._world_to_map is None:
            raise RuntimeError("尚未设置地图坐标变换")
        if isinstance(rect, QRect):
            pixel_rect = QRectF(rect.x(), rect.y(), rect.width(), rect.height())
        elif isinstance(rect, QRectF):
            pixel_rect = QRectF(rect)
        else:
            values = tuple(float(value) for value in rect)
            if len(values) != 4:
                raise ValueError("像素矩形必须包含 x、y、width、height")
            pixel_rect = QRectF(*values)
        pixel_rect = pixel_rect.normalized().intersected(
            QRectF(0.0, 0.0, float(self.width), float(self.height))
        )
        if pixel_rect.isEmpty():
            raise ValueError("像素矩形不在地图范围内")

        map_to_world, invertible = self._world_to_map.inverted()
        if not invertible:  # set_world_to_map_transform 已拦截，此处用于防御状态损坏。
            raise RuntimeError("地图坐标变换不可逆")
        corners = (
            pixel_rect.topLeft(),
            pixel_rect.topRight(),
            pixel_rect.bottomLeft(),
            pixel_rect.bottomRight(),
        )
        sample_points = tuple(map_to_world.map(point) for point in corners)
        xs = tuple(point.x() for point in sample_points)
        ys = tuple(point.y() for point in sample_points)
        left, right = min(xs), max(xs)
        top, bottom = min(ys), max(ys)
        return left, top, right - left, bottom - top


class SpatialMapWidget(QWidget):
    """实时空间地图控件；所有地图与扫描状态都由公开方法注入。"""

    roi_selected = Signal(int, int, int, int)
    roi_sample_selected = Signal(float, float, float, float)
    selection_cancelled = Signal()
    state_changed = Signal()

    _TILE_COLORS = {
        "pending": QColor(130, 138, 152, 36),
        "scanning": QColor(255, 184, 77, 104),
        "complete": QColor(67, 196, 126, 78),
        "error": QColor(231, 92, 92, 112),
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = SpatialMapModel()
        self._map_pixmap: QPixmap | None = None
        self._selection_enabled = False
        self._drag_start: QPointF | None = None
        self._drag_current: QPointF | None = None
        self.setMinimumSize(180, 140)
        self.setFocusPolicy(Qt.StrongFocus)

    @property
    def selected_roi(self) -> QRect | None:
        return QRect(self.model.selected_roi) if self.model.selected_roi is not None else None

    def set_map_size(self, width: int, height: int) -> None:
        self.model.set_map_size(width, height)
        self.state_changed.emit()
        self.update()

    def set_map_image(self, image: QImage | QPixmap) -> None:
        pixmap = QPixmap.fromImage(image) if isinstance(image, QImage) else QPixmap(image)
        if pixmap.isNull():
            raise ValueError("地图图像不能为空")
        self._map_pixmap = pixmap
        self.model.set_map_size(pixmap.width(), pixmap.height())
        self.state_changed.emit()
        self.update()

    def clear_map_image(self) -> None:
        self._map_pixmap = None
        self.state_changed.emit()
        self.update()

    def set_tile_states(
        self,
        tiles: Iterable[SpatialTile | tuple[str, QRect | Sequence[int], str]],
    ) -> None:
        updated: dict[str, SpatialTile] = {}
        for item in tiles:
            if isinstance(item, SpatialTile):
                tile_id, rect, status = item.tile_id, item.rect, item.status
            else:
                tile_id, rect, status = item
            rect = QRect(rect) if isinstance(rect, QRect) else QRect(*(int(value) for value in rect))
            updated[str(tile_id)] = SpatialTile(str(tile_id), self.model.clamp_pixel_rect(rect), str(status))
        self.model.tiles = updated
        self.state_changed.emit()
        self.update()

    def update_tile_state(
        self,
        tile_id: str,
        status: str,
        rect: QRect | Sequence[int] | None = None,
    ) -> None:
        key = str(tile_id)
        existing = self.model.tiles.get(key)
        if rect is None:
            if existing is None:
                raise KeyError(f"未知瓦片: {key}")
            tile_rect = existing.rect
        else:
            candidate = QRect(rect) if isinstance(rect, QRect) else QRect(*(int(value) for value in rect))
            tile_rect = self.model.clamp_pixel_rect(candidate)
        self.model.tiles[key] = SpatialTile(key, tile_rect, str(status))
        self.state_changed.emit()
        self.update()

    def set_current_scan_point(self, point: QPointF | Sequence[float] | None) -> None:
        self.model.current_scan_point = None if point is None else self._coerce_point(point)
        self.state_changed.emit()
        self.update()

    def set_route(self, points: Iterable[QPointF | Sequence[float]]) -> None:
        self.model.route = tuple(self._coerce_point(point) for point in points)
        self.state_changed.emit()
        self.update()

    def set_selection_enabled(self, enabled: bool) -> None:
        self._selection_enabled = bool(enabled)
        self.setCursor(Qt.CrossCursor if enabled else Qt.ArrowCursor)
        if not enabled and self._drag_start is not None:
            self.cancel_selection(clear_roi=False)

    def set_world_to_map_transform(self, transform: QTransform | Sequence[float]) -> None:
        self.model.set_world_to_map_transform(transform)

    def set_map_physical_bounds(
        self, min_x: float, min_y: float, max_x: float, max_y: float
    ) -> None:
        self.model.set_map_physical_bounds(min_x, min_y, max_x, max_y)

    def map_pixel_rect_to_sample(
        self, rect: QRect | QRectF | Sequence[float]
    ) -> tuple[float, float, float, float]:
        return self.model.map_pixel_rect_to_sample(rect)

    def set_selected_roi(self, rect: QRect | Sequence[int] | None) -> None:
        self.model.selected_roi = None if rect is None else self.model.clamp_pixel_rect(rect)
        self.state_changed.emit()
        self.update()

    def cancel_selection(self, clear_roi: bool = True) -> None:
        had_selection = self._drag_start is not None or (clear_roi and self.model.selected_roi is not None)
        self._drag_start = None
        self._drag_current = None
        if clear_roi:
            self.model.selected_roi = None
        if had_selection:
            self.selection_cancelled.emit()
            self.state_changed.emit()
        self.update()

    @staticmethod
    def _coerce_point(point: QPointF | Sequence[float]) -> QPointF:
        if isinstance(point, QPointF):
            result = QPointF(point)
        else:
            x, y = point
            result = QPointF(float(x), float(y))
        if not isfinite(result.x()) or not isfinite(result.y()):
            raise ValueError("地图点必须是有限数值")
        return result

    def _map_view_rect(self) -> QRectF:
        if self.model.width <= 0 or self.model.height <= 0:
            return QRectF()
        available = QRectF(self.rect()).adjusted(8.0, 8.0, -8.0, -8.0)
        if available.isEmpty():
            return QRectF()
        scale = min(available.width() / self.model.width, available.height() / self.model.height)
        width = self.model.width * scale
        height = self.model.height * scale
        return QRectF(
            available.center().x() - width / 2,
            available.center().y() - height / 2,
            width,
            height,
        )

    def _widget_to_map(self, point: QPointF) -> QPointF:
        view = self._map_view_rect()
        if view.isEmpty():
            return QPointF()
        x = min(max(point.x(), view.left()), view.right())
        y = min(max(point.y(), view.top()), view.bottom())
        return QPointF(
            (x - view.left()) * self.model.width / view.width(),
            (y - view.top()) * self.model.height / view.height(),
        )

    def _map_to_widget(self, point: QPointF) -> QPointF:
        view = self._map_view_rect()
        return QPointF(
            view.left() + point.x() * view.width() / max(self.model.width, 1),
            view.top() + point.y() * view.height() / max(self.model.height, 1),
        )

    def _map_rect_to_widget(self, rect: QRect | QRectF) -> QRectF:
        top_left = self._map_to_widget(QPointF(rect.x(), rect.y()))
        bottom_right = self._map_to_widget(
            QPointF(rect.x() + rect.width(), rect.y() + rect.height())
        )
        return QRectF(top_left, bottom_right).normalized()

    def _drag_pixel_rect(self) -> QRect:
        if self._drag_start is None or self._drag_current is None:
            return QRect()
        start = self._widget_to_map(self._drag_start)
        end = self._widget_to_map(self._drag_current)
        left = int(min(start.x(), end.x()))
        top = int(min(start.y(), end.y()))
        right = int(max(start.x(), end.x()) + 0.999999)
        bottom = int(max(start.y(), end.y()) + 0.999999)
        return self.model.clamp_pixel_rect(QRect(left, top, right - left, bottom - top))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.RightButton:
            self.cancel_selection()
            event.accept()
            return
        if (
            not self._selection_enabled
            or event.button() != Qt.LeftButton
            or not self._map_view_rect().contains(event.position())
        ):
            super().mousePressEvent(event)
            return
        self.setFocus(Qt.MouseFocusReason)
        self._drag_start = event.position()
        self._drag_current = event.position()
        self.update()
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_start is None:
            super().mouseMoveEvent(event)
            return
        self._drag_current = event.position()
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drag_start is None or event.button() != Qt.LeftButton:
            super().mouseReleaseEvent(event)
            return
        self._drag_current = event.position()
        roi = self._drag_pixel_rect()
        self._drag_start = None
        self._drag_current = None
        if roi.width() > 0 and roi.height() > 0:
            self.model.selected_roi = roi
            self.roi_selected.emit(roi.x(), roi.y(), roi.width(), roi.height())
            try:
                sample = self.model.map_pixel_rect_to_sample(roi)
            except RuntimeError:
                pass
            else:
                self.roi_sample_selected.emit(*sample)
            self.state_changed.emit()
        self.update()
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key_Escape:
            self.cancel_selection()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#171a20"))
        view = self._map_view_rect()
        if view.isEmpty():
            painter.setPen(QColor("#8c939f"))
            painter.drawText(self.rect(), Qt.AlignCenter, "暂无空间地图")
            return

        painter.fillRect(view, QColor("#252a33"))
        if self._map_pixmap is not None:
            painter.drawPixmap(view, self._map_pixmap, QRectF(self._map_pixmap.rect()))

        for tile in self.model.tiles.values():
            draw_rect = self._map_rect_to_widget(tile.rect)
            color = self._TILE_COLORS.get(tile.status, QColor(84, 154, 223, 70))
            painter.fillRect(draw_rect, color)
            painter.setPen(QPen(color.lighter(150), 1.0))
            painter.drawRect(draw_rect)

        if len(self.model.route) >= 2:
            path = QPainterPath(self._map_to_widget(self.model.route[0]))
            for point in self.model.route[1:]:
                path.lineTo(self._map_to_widget(point))
            painter.setPen(QPen(QColor("#4bb4ff"), 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)

        if self.model.current_scan_point is not None:
            center = self._map_to_widget(self.model.current_scan_point)
            painter.setPen(QPen(QColor("#ffffff"), 1.5))
            painter.setBrush(QColor("#ffb84d"))
            painter.drawEllipse(center, 5.0, 5.0)

        roi = self._drag_pixel_rect() if self._drag_start is not None else self.model.selected_roi
        if roi is not None and not roi.isEmpty():
            draw_rect = self._map_rect_to_widget(roi)
            painter.setPen(QPen(QColor("#18d2d0"), 2.0))
            painter.setBrush(QColor(24, 210, 208, 42))
            painter.drawRect(draw_rect)

        painter.setPen(QPen(QColor("#555d69"), 1.0))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(view)


__all__ = ["SpatialMapModel", "SpatialMapWidget", "SpatialTile"]
