from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QPoint, QPointF, QRect, QSize, QObject, QSettings, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QMouseEvent, QPainter, QPen, QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QDialog,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRubberBand,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QDoubleSpinBox,
)

from grab_app.camera import CameraController
from grab_app.config import (
    PZT_BAUD_RATES,
    APP_NAME,
    PZT_DEFAULT_BAUD,
    PZT_DEFAULT_IP,
    PZT_MAX_UM,
    PZT_UDP_PORT,
    app_icon_path,
)
from grab_app import __version__
from grab_app.image_io import next_numbered_path
from grab_app.pzt import PZTController
from grab_app.services import (
    FlatFieldCalibration,
    ScanConfig,
    ScanResult,
    ScanWorker,
    SpatialAcquisitionConfig,
    SpatialScanResult,
    SpatialScanWorker,
    SurveyConfig,
)
from grab_app.spatial import (
    MosaicComposer,
    SafetyLimits,
    SpatialJobStorage,
    SpatialRect,
    TilePlan,
    default_calibration,
    estimate_adjacent_translation,
    fit_affine_calibration,
    plan_center_scan,
    plan_tiles,
)
from grab_app.ui.spatial_map import SpatialMapWidget, SpatialTile
from grab_app.ui.xy_stage_dialog import XYStageControlDialog
from grab_app.xy_stage import XYStageExecutor
from grab_app.update import UpdateInfo, check_latest_release, download_installer, start_installer

from .status_lamp import StatusLamp


class NoWheelComboBox(QComboBox):
    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()


class NoWheelSpinBox(QSpinBox):
    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()


class NoWheelDoubleSpinBox(QDoubleSpinBox):
    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()

    def textFromValue(self, value: float) -> str:  # noqa: N802 - Qt API
        """保留输入精度，但显示时去掉无意义的末尾 0。"""
        text = f"{float(value):.{self.decimals()}f}"
        return text.rstrip("0").rstrip(".") if "." in text else text


THEME_LABELS = {
    "dark": "深色",
    "light": "白色",
}


class DeviceStatusLabel(QLabel):
    def __init__(self, text: str) -> None:
        super().__init__()
        self._raw_text = ""
        self._text_color = "#d8d8dc"
        self._online_color = "#55c982"
        self._offline_color = "#e66767"
        self.setObjectName("deviceStatus")
        self.setText(text)

    def set_theme_colors(self, text_color: str, online_color: str, offline_color: str) -> None:
        self._text_color = text_color
        self._online_color = online_color
        self._offline_color = offline_color
        self.setText(self._raw_text)

    def setText(self, text: str) -> None:
        self._raw_text = text
        online = "未连接" not in text and "失败" not in text
        display_text = text.replace("相机:", "相机").replace("PZT:", "PZT").replace("XY:", "XY")
        dot_color = self._online_color if online else self._offline_color
        super().setText(
            f'<span style="color:{dot_color};">●</span>'
            f'&nbsp;&nbsp;<span style="color:{self._text_color};">{display_text}</span>'
        )


def enable_dark_title_bar(widget: QWidget, enabled: bool = True) -> None:
    if sys.platform != "win32":
        return
    try:
        hwnd = int(widget.winId())
        value = ctypes.c_int(1 if enabled else 0)
        for attribute in (20, 19):
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd,
                attribute,
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
            if result == 0:
                break
    except Exception:
        pass


def tool_icon(kind: str, color: str = "#e6e6e8") -> QIcon:
    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(QColor(color), 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))

    if kind == "camera":
        painter.drawRoundedRect(3, 7, 18, 12, 2, 2)
        painter.drawEllipse(9, 10, 6, 6)
        painter.drawLine(7, 7, 9, 4)
        painter.drawLine(9, 4, 14, 4)
        painter.drawLine(14, 4, 16, 7)
    elif kind == "image":
        painter.drawRect(3, 4, 18, 16)
        painter.drawEllipse(15, 7, 3, 3)
        painter.drawLine(5, 17, 10, 11)
        painter.drawLine(10, 11, 13, 14)
        painter.drawLine(13, 14, 16, 11)
        painter.drawLine(16, 11, 20, 17)
    elif kind == "roi":
        painter.drawLine(4, 9, 4, 4)
        painter.drawLine(4, 4, 9, 4)
        painter.drawLine(15, 4, 20, 4)
        painter.drawLine(20, 4, 20, 9)
        painter.drawLine(4, 15, 4, 20)
        painter.drawLine(4, 20, 9, 20)
        painter.drawLine(15, 20, 20, 20)
        painter.drawLine(20, 15, 20, 20)
    elif kind == "pzt":
        painter.drawLine(12, 4, 12, 20)
        painter.drawLine(8, 8, 12, 4)
        painter.drawLine(16, 8, 12, 4)
        painter.drawLine(8, 16, 12, 20)
        painter.drawLine(16, 16, 12, 20)
        painter.drawLine(5, 12, 19, 12)
    elif kind == "xy":
        painter.drawRect(4, 5, 16, 14)
        painter.drawLine(7, 16, 17, 16)
        painter.drawLine(14, 13, 17, 16)
        painter.drawLine(14, 19, 17, 16)
        painter.drawLine(7, 16, 7, 8)
        painter.drawLine(4, 11, 7, 8)
        painter.drawLine(10, 11, 7, 8)
    elif kind == "log":
        painter.drawRoundedRect(5, 3, 14, 18, 1, 1)
        painter.drawLine(8, 8, 16, 8)
        painter.drawLine(8, 12, 16, 12)
        painter.drawLine(8, 16, 14, 16)
    elif kind == "update":
        painter.drawArc(5, 5, 14, 14, 35 * 16, 285 * 16)
        painter.drawLine(15, 4, 19, 5)
        painter.drawLine(19, 5, 18, 9)
        painter.drawLine(9, 20, 5, 19)
        painter.drawLine(5, 19, 6, 15)
    elif kind == "settings":
        painter.drawEllipse(8, 8, 8, 8)
        painter.drawEllipse(10, 10, 4, 4)
        for start, end in (
            ((12, 3), (12, 6)),
            ((12, 18), (12, 21)),
            ((3, 12), (6, 12)),
            ((18, 12), (21, 12)),
            ((5, 5), (7, 7)),
            ((17, 17), (19, 19)),
            ((19, 5), (17, 7)),
            ((7, 17), (5, 19)),
        ):
            painter.drawLine(*start, *end)

    painter.end()
    return QIcon(pixmap)


class UiBridge(QObject):
    progress = Signal(str, int, int, float, object)
    done = Signal(object, object)
    log = Signal(str)
    update_checked = Signal(object, object)
    update_download_progress = Signal(int, int)
    update_downloaded = Signal(object, object)
    spatial_progress = Signal(str, int, int, object)
    spatial_tile = Signal(object, object, object)
    spatial_done = Signal(object, object)
    spatial_calibrated = Signal(object, object)


class CollapsibleSection(QFrame):
    def __init__(self, title: str, content: QWidget, expanded: bool = False) -> None:
        super().__init__()
        self.setObjectName("collapsible")
        self.toggle = QToolButton()
        self.toggle.setObjectName("disclosure")
        self.toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setChecked(expanded)
        self.toggle.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.content = content
        self.content.setVisible(expanded)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.toggle)
        layout.addWidget(self.content)
        self.toggle.toggled.connect(self._set_expanded)

    def _set_expanded(self, expanded: bool) -> None:
        self.toggle.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.content.setVisible(expanded)


class UpdateProgressDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("updateProgressDialog")
        self.setWindowTitle("在线更新")
        self.setModal(True)
        self.setFixedSize(420, 188)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 20)
        layout.setSpacing(12)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(10)

        icon = QLabel()
        icon.setObjectName("updateDialogIcon")
        icon.setFixedSize(28, 28)
        icon.setAlignment(Qt.AlignCenter)
        icon.setPixmap(tool_icon("update").pixmap(22, 22))

        title = QLabel("正在更新")
        title.setObjectName("updateDialogTitle")
        title_row.addWidget(icon)
        title_row.addWidget(title)
        title_row.addStretch(1)
        layout.addLayout(title_row)

        self.status_label = QLabel("正在下载安装包...")
        self.status_label.setObjectName("updateDialogStatus")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.progress = QProgressBar()
        self.progress.setObjectName("updateProgress")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(12)
        layout.addWidget(self.progress)

        self.detail_label = QLabel("0%")
        self.detail_label.setObjectName("updateDialogDetail")
        self.detail_label.setAlignment(Qt.AlignRight)
        layout.addWidget(self.detail_label)

    def set_progress(self, received: int, total: int) -> None:
        if total > 0:
            percent = min(100, max(0, round(received * 100 / total)))
            self.progress.setRange(0, 100)
            self.progress.setValue(percent)
            self.status_label.setText(
                f"正在下载安装包... {received / 1024 / 1024:.1f} / {total / 1024 / 1024:.1f} MB"
            )
            self.detail_label.setText(f"{percent}%")
        else:
            self.progress.setRange(0, 0)
            self.status_label.setText(f"正在下载安装包... {received / 1024 / 1024:.1f} MB")
            self.detail_label.setText("下载中")


class RoiPreviewLabel(QLabel):
    roi_selected = Signal(int, int, int, int)

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.selection_enabled = False
        self._source_width = 0
        self._source_height = 0
        self._origin_x = 0
        self._origin_y = 0
        self._drag_start: QPoint | None = None
        self._rubber_band = QRubberBand(QRubberBand.Rectangle, self)

    def set_selection_enabled(self, enabled: bool) -> None:
        self.selection_enabled = enabled
        self.setCursor(Qt.CrossCursor if enabled else Qt.ArrowCursor)

    def set_source_geometry(self, width: int, height: int, origin_x: int = 0, origin_y: int = 0) -> None:
        self._source_width = max(0, int(width))
        self._source_height = max(0, int(height))
        self._origin_x = max(0, int(origin_x))
        self._origin_y = max(0, int(origin_y))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not self._can_select(event):
            super().mousePressEvent(event)
            return
        position = event.position().toPoint()
        if not self._pixmap_rect().contains(position):
            return
        self._drag_start = position
        self._rubber_band.setGeometry(QRect(position, position))
        self._rubber_band.show()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_start is None:
            super().mouseMoveEvent(event)
            return
        current = self._clamp_to_pixmap(event.position().toPoint())
        self._rubber_band.setGeometry(QRect(self._drag_start, current).normalized())

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drag_start is None:
            super().mouseReleaseEvent(event)
            return
        current = self._clamp_to_pixmap(event.position().toPoint())
        selection = QRect(self._drag_start, current).normalized()
        self._drag_start = None
        self._rubber_band.hide()
        if selection.width() < 4 or selection.height() < 4:
            return
        roi = self._selection_to_source_roi(selection)
        if roi is not None:
            self.roi_selected.emit(*roi)

    def _can_select(self, event: QMouseEvent) -> bool:
        return (
            self.selection_enabled
            and event.button() == Qt.LeftButton
            and self.pixmap() is not None
            and self._source_width > 0
            and self._source_height > 0
        )

    def _pixmap_rect(self) -> QRect:
        pixmap = self.pixmap()
        if pixmap is None:
            return QRect()
        size = pixmap.size()
        left = max(0, (self.width() - size.width()) // 2)
        top = max(0, (self.height() - size.height()) // 2)
        return QRect(left, top, size.width(), size.height())

    def _clamp_to_pixmap(self, point: QPoint) -> QPoint:
        rect = self._pixmap_rect()
        x = min(max(point.x(), rect.left()), rect.right())
        y = min(max(point.y(), rect.top()), rect.bottom())
        return QPoint(x, y)

    def _selection_to_source_roi(self, selection: QRect) -> tuple[int, int, int, int] | None:
        pixmap_rect = self._pixmap_rect()
        clipped = selection.intersected(pixmap_rect)
        if clipped.width() < 1 or clipped.height() < 1:
            return None
        x_scale = self._source_width / max(pixmap_rect.width(), 1)
        y_scale = self._source_height / max(pixmap_rect.height(), 1)
        x = self._origin_x + round((clipped.left() - pixmap_rect.left()) * x_scale)
        y = self._origin_y + round((clipped.top() - pixmap_rect.top()) * y_scale)
        right = self._origin_x + round((clipped.right() - pixmap_rect.left() + 1) * x_scale)
        bottom = self._origin_y + round((clipped.bottom() - pixmap_rect.top() + 1) * y_scale)
        width = max(1, right - x)
        height = max(1, bottom - y)
        return x, y, width, height


class RoiSnapshotLabel(QLabel):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self._source_width = 0
        self._source_height = 0
        self._origin_x = 0
        self._origin_y = 0
        self._roi: tuple[int, int, int, int] | None = None
        self.setAlignment(Qt.AlignCenter)

    def set_snapshot(
        self,
        pixmap: QPixmap,
        source_width: int,
        source_height: int,
        origin_x: int = 0,
        origin_y: int = 0,
    ) -> None:
        self._source_width = max(0, int(source_width))
        self._source_height = max(0, int(source_height))
        self._origin_x = max(0, int(origin_x))
        self._origin_y = max(0, int(origin_y))
        self.setPixmap(pixmap)
        self.update()

    def set_roi(self, x: int, y: int, width: int, height: int) -> None:
        self._roi = (int(x), int(y), int(width), int(height))
        self.update()

    def clear_snapshot(self) -> None:
        self._source_width = 0
        self._source_height = 0
        self._origin_x = 0
        self._origin_y = 0
        self._roi = None
        self.clear()
        self.setText("无预览")

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.pixmap() is None or self._roi is None or self._source_width <= 0 or self._source_height <= 0:
            return
        visible_roi = self._visible_roi()
        if visible_roi is None:
            return

        pixmap_rect = self._pixmap_rect()
        x, y, width, height = visible_roi
        x_scale = pixmap_rect.width() / max(self._source_width, 1)
        y_scale = pixmap_rect.height() / max(self._source_height, 1)
        draw_rect = QRect(
            pixmap_rect.left() + round(x * x_scale),
            pixmap_rect.top() + round(y * y_scale),
            max(1, round(width * x_scale)),
            max(1, round(height * y_scale)),
        )

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#18d2d0"), 2))
        painter.setBrush(QColor(24, 210, 208, 36))
        painter.drawRect(draw_rect.adjusted(0, 0, -1, -1))

    def _pixmap_rect(self) -> QRect:
        pixmap = self.pixmap()
        if pixmap is None:
            return QRect()
        size = pixmap.size()
        left = max(0, (self.width() - size.width()) // 2)
        top = max(0, (self.height() - size.height()) // 2)
        return QRect(left, top, size.width(), size.height())

    def _visible_roi(self) -> tuple[int, int, int, int] | None:
        if self._roi is None:
            return None
        roi_x, roi_y, roi_w, roi_h = self._roi
        local_x = roi_x - self._origin_x
        local_y = roi_y - self._origin_y
        left = max(0, local_x)
        top = max(0, local_y)
        right = min(self._source_width, local_x + roi_w)
        bottom = min(self._source_height, local_y + roi_h)
        if right <= left or bottom <= top:
            return None
        return left, top, right - left, bottom - top


def frame_to_pixmap(frame: np.ndarray, width: int, height: int) -> QPixmap:
    if frame.ndim == 2:
        if frame.dtype == np.uint16:
            image = np.clip(frame, 0, 4095).astype(np.uint32)
            image = ((image * 255) // 4095).astype(np.uint8)
        else:
            image = frame.astype(np.uint8, copy=False)
        image = np.ascontiguousarray(image)
        qimage = QImage(image.data, image.shape[1], image.shape[0], image.strides[0], QImage.Format_Grayscale8)
    else:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = np.ascontiguousarray(rgb)
        qimage = QImage(image.data, image.shape[1], image.shape[0], image.strides[0], QImage.Format_RGB888)
    pixmap = QPixmap.fromImage(qimage.copy())
    return pixmap.scaled(width, height, Qt.KeepAspectRatio, Qt.SmoothTransformation)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(app_icon_path())))
        self.resize(1500, 920)
        self.setMinimumSize(1080, 680)

        self.camera = CameraController()
        self.pzt = PZTController()
        self.bridge = UiBridge()
        self.settings = QSettings("HTGE", APP_NAME)
        self._theme = self._load_theme()
        self._tool_buttons: list[tuple[QToolButton, str]] = []
        self.scanner = ScanWorker(self.camera, self.pzt, self._emit_progress, self._log)
        self.preview_timer = QTimer(self)
        self.preview_timer.setInterval(17)
        self.preview_timer.timeout.connect(self._update_preview)
        self.status_timer = QTimer(self)
        self.status_timer.setInterval(500)
        self.status_timer.timeout.connect(self._update_camera_status_readbacks)
        self.xy_status_timer = QTimer(self)
        self.xy_status_timer.setInterval(200)
        self.xy_status_timer.timeout.connect(self._update_xy_status_readback)
        self.monitor_timer = QTimer(self)
        self.monitor_timer.setInterval(500)
        self.monitor_timer.timeout.connect(self._monitor_pzt)
        self._monitor_restart_after_scan = False
        self._monitor_pause_checked_before_scan = False
        self._dark_average: np.ndarray | None = None
        self._flat_average: np.ndarray | None = None
        self._calibration_signature: dict[str, object] | None = None
        self._roi_selection_active = False
        self._preview_origin = (0, 0)
        self._sensor_max_width = 1280
        self._sensor_max_height = 1024
        self._sensor_min_width = 1
        self._sensor_min_height = 1
        self._update_progress_dialog: UpdateProgressDialog | None = None
        self.xy_stage: XYStageExecutor | None = None
        self._xy_controller_limits: tuple[float, float, float, float] | None = None
        self.spatial_worker: SpatialScanWorker | None = None
        self.spatial_calibration = default_calibration(0.48)
        self._survey_plan: TilePlan | None = None
        self._acquisition_plan: TilePlan | None = None
        self._spatial_roi: SpatialRect | None = None
        self._spatial_composer: MosaicComposer | None = None
        self._spatial_map_shape = (0, 0)
        self._spatial_tile_states: dict[str, SpatialTile] = {}
        self._spatial_tile_origins: dict[int, tuple[float, float]] = {}
        self._last_spatial_tile: tuple[object, np.ndarray] | None = None

        self.bridge.progress.connect(self._on_scan_progress)
        self.bridge.done.connect(self._on_scan_done)
        self.bridge.log.connect(self._append_log)
        self.bridge.update_checked.connect(self._on_update_checked)
        self.bridge.update_download_progress.connect(self._on_update_download_progress)
        self.bridge.update_downloaded.connect(self._on_update_downloaded)
        self.bridge.spatial_progress.connect(self._on_spatial_progress)
        self.bridge.spatial_tile.connect(self._on_spatial_tile)
        self.bridge.spatial_done.connect(self._on_spatial_done)
        self.bridge.spatial_calibrated.connect(self._on_spatial_calibrated)

        self._build_ui()
        self._apply_style()
        self._refresh_ports()
        self._refresh_xy_ports()
        self._sync_exposure_controls()
        self.status_timer.start()
        self.xy_status_timer.start()
        enable_dark_title_bar(self, self._theme == "dark")

    def _load_theme(self) -> str:
        value = self.settings.value("ui/theme", "dark")
        return str(value) if str(value) in THEME_LABELS else "dark"

    def _theme_changed(self, label: str) -> None:
        theme = next((key for key, value in THEME_LABELS.items() if value == label), "dark")
        if theme == self._theme:
            return
        self._theme = theme
        self.settings.setValue("ui/theme", theme)
        self._apply_style()
        self._log(f"界面主题已切换为{THEME_LABELS[theme]}")

    def _theme_palette(self) -> dict[str, str]:
        if self._theme == "light":
            return {
                "accent": "#0b8f98",
                "accent_hover": "#0aa7b2",
                "accent_pressed": "#087981",
                "accent_soft": "#e6f7f8",
                "main_bg": "#f5f7fa",
                "surface": "#ffffff",
                "panel": "#f8fafc",
                "panel_alt": "#eef2f6",
                "panel_strong": "#e9eef4",
                "border": "#d5dce6",
                "border_strong": "#b8c2cf",
                "text": "#1f2933",
                "text_soft": "#536170",
                "text_muted": "#6c7886",
                "title": "#111827",
                "preview_bg": "#eef2f6",
                "preview_text": "#667080",
                "button_bg": "#ffffff",
                "button_hover": "#edf3f8",
                "button_pressed": "#dde7f0",
                "danger_bg": "#fff1f1",
                "danger_hover": "#ffe2e2",
                "danger_pressed": "#ffd1d1",
                "danger_text": "#9f1d25",
                "danger_border": "#e4a1a7",
                "disabled_bg": "#eef1f5",
                "disabled_text": "#9aa5b1",
                "input_bg": "#ffffff",
                "input_focus": "#f5fbfc",
                "selection_text": "#ffffff",
                "tool_icon": "#24303d",
                "device_ok": "#16834a",
                "device_bad": "#c9363f",
                "message_bg": "#f6f7f9",
                "message_button": "#ffffff",
            }
        return {
            "accent": "#16d6d0",
            "accent_hover": "#18c3c2",
            "accent_pressed": "#0b8b90",
            "accent_soft": "#174d50",
            "main_bg": "#1c1d22",
            "surface": "#232329",
            "panel": "#35323a",
            "panel_alt": "#34313a",
            "panel_strong": "#302d35",
            "border": "#4d4953",
            "border_strong": "#69636f",
            "text": "#e5e7ea",
            "text_soft": "#c4c3c8",
            "text_muted": "#8a919b",
            "title": "#f3f4f5",
            "preview_bg": "#0f1115",
            "preview_text": "#8a919b",
            "button_bg": "#3b3841",
            "button_hover": "#46424c",
            "button_pressed": "#2b2931",
            "danger_bg": "#3d3338",
            "danger_hover": "#5b343a",
            "danger_pressed": "#742f36",
            "danger_text": "#ffd9d9",
            "danger_border": "#8d4c51",
            "disabled_bg": "#2d2b31",
            "disabled_text": "#777982",
            "input_bg": "#24232a",
            "input_focus": "#202026",
            "selection_text": "#061214",
            "tool_icon": "#e6e6e8",
            "device_ok": "#55c982",
            "device_bad": "#e66767",
            "message_bg": "#f6f7f9",
            "message_button": "#ffffff",
        }

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("mainRoot")
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.preview = RoiPreviewLabel("相机未连接")
        self.preview.setObjectName("preview")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(420, 300)
        self.preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview.roi_selected.connect(self._roi_selected_from_preview)

        self.camera_settings_dialog = self._settings_dialog("相机与预览", self._camera_box())
        self.image_settings_dialog = self._settings_dialog("图像设置", self._image_section())
        self.roi_settings_panel = self._roi_section()
        self.roi_settings_panel.hide()
        self.xy_settings_dialog = XYStageControlDialog(parent=self)
        self.xy_settings_dialog.tabs.addTab(self._xy_stage_box(), "空间扫描")
        self.xy_settings_dialog.connect_requested.connect(self._connect_xy_stage_from_dialog)
        self.xy_settings_dialog.disconnect_requested.connect(self._disconnect_xy_stage_from_dialog)
        self.xy_settings_dialog.snapshot_updated.connect(self._on_xy_dialog_snapshot)
        self.xy_settings_dialog.error_occurred.connect(
            lambda message: self._log(f"XY 位移台: {message}")
        )
        # 侧栏“XY位移”只配置空间概览采集参数，与导航栏位移台控制弹窗分离。
        self.xy_overview_dialog = self._settings_dialog(
            "XY位移", self._spatial_overview_settings_box()
        )
        self.pzt_settings_dialog = self._settings_dialog("PZT 位移", self._pzt_box())
        self.log_dialog = self._settings_dialog("运行日志", self._log_panel())
        self.log_dialog.setMinimumSize(720, 420)
        self.app_settings_dialog = self._settings_dialog("设置", self._app_settings_panel())

        self.camera_status = DeviceStatusLabel("相机: 未连接")
        self.pzt_status = DeviceStatusLabel("PZT: 未连接")
        self.xy_status = DeviceStatusLabel("XY: 未连接")

        left_area = QWidget()
        left_area.setObjectName("leftArea")
        left_layout = QVBoxLayout(left_area)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        top_bar = QFrame()
        top_bar.setObjectName("topBar")
        top_bar_layout = QHBoxLayout(top_bar)
        top_bar_layout.setContentsMargins(6, 2, 8, 2)
        top_bar_layout.setSpacing(2)
        top_bar_layout.addWidget(
            self._settings_button(
                "相机设置",
                self.camera_settings_dialog,
                "camera",
            )
        )
        top_bar_layout.addWidget(
            self._settings_button(
                "图像设置",
                self.image_settings_dialog,
                "image",
            )
        )
        self.btn_roi_select = self._action_button("传感器 ROI", "roi", self._activate_roi_selection)
        self.btn_roi_select.setCheckable(True)
        top_bar_layout.addWidget(self.btn_roi_select)
        top_bar_layout.addWidget(
            self._settings_button(
                "PZT 设置",
                self.pzt_settings_dialog,
                "pzt",
            )
        )
        self.btn_xy_stage_control = self._settings_button(
            "XY位移台控制", self.xy_settings_dialog, "xy"
        )
        top_bar_layout.addWidget(self.btn_xy_stage_control)
        top_bar_layout.addWidget(self._settings_button("运行日志", self.log_dialog, "log"))
        top_bar_layout.addWidget(self._settings_button("设置", self.app_settings_dialog, "settings"))
        top_bar_layout.addSpacing(10)
        top_bar_layout.addWidget(self.camera_status)
        top_bar_layout.addWidget(self.pzt_status)
        top_bar_layout.addWidget(self.xy_status)
        top_bar_layout.addStretch()
        left_layout.addWidget(top_bar)

        work_surface = QFrame()
        work_surface.setObjectName("workSurface")
        work_layout = QVBoxLayout(work_surface)
        work_layout.setContentsMargins(0, 0, 0, 0)
        work_layout.setSpacing(0)

        viewer_shell = QFrame()
        viewer_shell.setObjectName("viewerShell")
        viewer_layout = QVBoxLayout(viewer_shell)
        viewer_layout.setContentsMargins(12, 12, 12, 10)
        viewer_layout.setSpacing(8)

        self.viewer_tabs = QTabWidget()
        self.viewer_tabs.setObjectName("viewerTabs")
        camera_page = QWidget()
        camera_layout = QVBoxLayout(camera_page)
        camera_layout.setContentsMargins(0, 0, 0, 0)
        camera_layout.addWidget(self.preview)
        self.spatial_map = SpatialMapWidget()
        self.spatial_map.roi_sample_selected.connect(self._spatial_roi_selected)
        self.viewer_tabs.addTab(camera_page, "相机预览")
        self.viewer_tabs.addTab(self.spatial_map, "样品地图")
        viewer_layout.addWidget(self.viewer_tabs, 1)

        self.progress = QProgressBar()
        self.progress.setObjectName("scanProgress")
        self.progress.setFixedHeight(8)
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        viewer_layout.addWidget(self.progress)

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        self.fps_status = QLabel("显示: -- FPS | 采集帧: 0")
        self.exposure_status = QLabel("曝光: -- us | 增益: --")
        for item in (self.fps_status, self.exposure_status):
            item.setObjectName("statusPill")
            item.setFixedHeight(28)
            item.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            status_row.addWidget(item, 1)
        viewer_layout.addLayout(status_row)
        work_layout.addWidget(viewer_shell, 1)
        left_layout.addWidget(work_surface, 1)

        side_widget = QWidget()
        side_widget.setObjectName("sidePanel")
        side_layout = QVBoxLayout(side_widget)
        side_layout.setContentsMargins(12, 8, 12, 14)
        side_layout.setSpacing(4)
        side_layout.addWidget(self._scan_tabs())
        side_layout.addWidget(self._spatial_scan_box())
        side_layout.addWidget(self._calibration_box())
        side_layout.addWidget(self._save_box())
        side_layout.addWidget(self.roi_settings_panel)
        side_layout.addWidget(self._scan_actions())
        side_layout.addStretch()

        scroll = QScrollArea()
        scroll.setObjectName("sideScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(side_widget)
        # 四列参数区包含“中心起点/终点”等较长标签，留足宽度避免右列被裁切。
        scroll.setFixedWidth(430)

        root_layout.addWidget(left_area, 1)
        root_layout.addWidget(scroll)
        self.setCentralWidget(root)

    def _settings_dialog(self, title: str, content: QWidget) -> QDialog:
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setObjectName("settingsDialog")
        dialog.setModal(False)
        dialog.setMinimumWidth(500)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        layout.addWidget(content)
        dialog.adjustSize()
        return dialog

    def _settings_button(
        self,
        tooltip: str,
        dialog: QDialog,
        icon: str,
    ) -> QToolButton:
        button = QToolButton()
        button.setObjectName("ribbonToolButton")
        button.setToolTip(tooltip)
        button.setIcon(tool_icon(icon, self._theme_palette()["tool_icon"]))
        button.setIconSize(QSize(20, 20))
        button.setToolButtonStyle(Qt.ToolButtonIconOnly)
        button.clicked.connect(lambda: self._show_settings_dialog(dialog))
        self._tool_buttons.append((button, icon))
        return button

    def _action_button(self, tooltip: str, icon: str, callback) -> QToolButton:
        button = QToolButton()
        button.setObjectName("ribbonToolButton")
        button.setToolTip(tooltip)
        button.setIcon(tool_icon(icon, self._theme_palette()["tool_icon"]))
        button.setIconSize(QSize(20, 20))
        button.setToolButtonStyle(Qt.ToolButtonIconOnly)
        button.clicked.connect(lambda _checked=False: callback())
        self._tool_buttons.append((button, icon))
        return button

    def _show_settings_dialog(self, dialog: QDialog) -> None:
        dialog.setStyleSheet(self.styleSheet())
        dialog.show()
        enable_dark_title_bar(dialog, self._theme == "dark")
        dialog.raise_()
        dialog.activateWindow()

    def _camera_box(self) -> QFrame:
        content = QFrame()
        content.setObjectName("collapsibleContent")
        layout = QGridLayout(content)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(8)

        self.btn_camera_open = QPushButton("连接相机")
        self.btn_preview = QPushButton("预览")
        self.btn_stop_preview = QPushButton("停止")
        self.btn_snapshot = QPushButton("抓拍")
        self.btn_camera_open.setObjectName("primaryButton")
        self.btn_preview.setObjectName("secondaryButton")
        self.btn_stop_preview.setObjectName("dangerButton")
        self.btn_snapshot.setObjectName("secondaryButton")
        self.btn_stop_preview.setEnabled(False)

        self.display_rate = self._combo(["5 FPS", "10 FPS", "20 FPS", "30 FPS", "60 FPS"], "60 FPS")
        self.frame_speed = self._combo(["低速", "中速", "高速"], "高速")
        self.auto_exposure = QCheckBox("自动曝光")
        self.auto_exposure.setChecked(True)
        self.exposure_us = self._dspin(100, 100000, 10000, decimals=2, step=100)
        self.gain_x = self._dspin(1, 16, 1, decimals=2, step=0.1)

        layout.addWidget(self.btn_camera_open, 0, 0)
        layout.addWidget(self.btn_preview, 0, 1)
        layout.addWidget(self.btn_stop_preview, 0, 2)
        layout.addWidget(self.btn_snapshot, 0, 3)
        layout.addWidget(self._label("显示"), 1, 0)
        layout.addWidget(self.display_rate, 1, 1)
        layout.addWidget(self._label("采集"), 1, 2)
        layout.addWidget(self.frame_speed, 1, 3)
        layout.addWidget(self.auto_exposure, 2, 0)
        layout.addWidget(self._label("曝光 us"), 2, 1)
        layout.addWidget(self.exposure_us, 2, 2, 1, 2)
        layout.addWidget(self._label("增益"), 3, 0)
        layout.addWidget(self.gain_x, 3, 1, 1, 3)

        self.btn_camera_open.clicked.connect(self._open_camera)
        self.btn_preview.clicked.connect(self._start_preview)
        self.btn_stop_preview.clicked.connect(self._stop_preview)
        self.btn_snapshot.clicked.connect(self._snapshot)
        self.display_rate.currentTextChanged.connect(self._display_rate_changed)
        self.frame_speed.currentIndexChanged.connect(lambda idx: self._safe_camera_call(lambda: self.camera.set_frame_speed(idx)))
        self.auto_exposure.toggled.connect(self._auto_exposure_changed)
        self.exposure_us.editingFinished.connect(self._manual_exposure_committed)
        self.gain_x.editingFinished.connect(self._manual_gain_committed)
        return content

    def _image_section(self) -> QFrame:
        content = QFrame()
        content.setObjectName("collapsibleContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(10)

        flicker_row = QHBoxLayout()
        self.anti_flick = QCheckBox("抗频闪")
        self.light_frequency = self._combo(["50Hz", "60Hz"], "50Hz")
        self.light_frequency.setEnabled(False)
        self.btn_once_wb = QPushButton("一次白平衡")
        self.btn_once_wb.setObjectName("secondaryButton")
        flicker_row.addWidget(self.anti_flick)
        flicker_row.addWidget(self.light_frequency)
        flicker_row.addWidget(self.btn_once_wb)
        layout.addLayout(flicker_row)

        transform_row = QHBoxLayout()
        self.h_mirror = QCheckBox("水平镜像")
        self.v_mirror = QCheckBox("垂直镜像")
        self.rotate = self._combo(["0°", "90°", "180°", "270°"], "0°")
        transform_row.addWidget(self.h_mirror)
        transform_row.addWidget(self.v_mirror)
        transform_row.addWidget(self._label("旋转"))
        transform_row.addWidget(self.rotate)
        layout.addLayout(transform_row)

        self.contrast_slider = self._slider(0, 200, 100)
        self.gamma_slider = self._slider(0, 200, 100)
        self.saturation_slider = self._slider(0, 200, 100)
        self.sharpness_slider = self._slider(0, 100, 0)
        for name, slider in [
            ("对比度", self.contrast_slider),
            ("Gamma", self.gamma_slider),
            ("饱和度", self.saturation_slider),
            ("锐度", self.sharpness_slider),
        ]:
            row = QHBoxLayout()
            row.addWidget(self._label(name), 0)
            row.addWidget(slider, 1)
            value_label = QLabel(str(slider.value()))
            value_label.setObjectName("meterValue")
            row.addWidget(value_label)
            slider.valueChanged.connect(value_label.setNum)
            layout.addLayout(row)

        self.anti_flick.toggled.connect(self._anti_flick_changed)
        self.light_frequency.currentTextChanged.connect(self._light_frequency_changed)
        self.btn_once_wb.clicked.connect(lambda: self._safe_camera_call(self.camera.set_once_white_balance))
        self.h_mirror.toggled.connect(
            lambda v: self._camera_geometry_changed(lambda: self.camera.set_mirror(0, v))
        )
        self.v_mirror.toggled.connect(
            lambda v: self._camera_geometry_changed(lambda: self.camera.set_mirror(1, v))
        )
        self.rotate.currentIndexChanged.connect(
            lambda idx: self._camera_geometry_changed(lambda: self.camera.set_rotate(idx))
        )
        self.contrast_slider.valueChanged.connect(lambda v: self._safe_camera_call(lambda: self.camera.set_contrast(v)))
        self.gamma_slider.valueChanged.connect(lambda v: self._safe_camera_call(lambda: self.camera.set_gamma(v)))
        self.saturation_slider.valueChanged.connect(lambda v: self._safe_camera_call(lambda: self.camera.set_saturation(v)))
        self.sharpness_slider.valueChanged.connect(lambda v: self._safe_camera_call(lambda: self.camera.set_sharpness(v)))
        return content

    def _roi_section(self) -> QGroupBox:
        content = QGroupBox("ROI 预览")
        content.setObjectName("roiPanel")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 8, 0, 4)
        layout.setSpacing(7)

        self.roi_enabled = QCheckBox("启用传感器 ROI")
        self.roi_enabled.hide()
        self.roi_snapshot = RoiSnapshotLabel("无预览")
        self.roi_snapshot.setObjectName("roiSnapshot")
        self.roi_snapshot.setMinimumHeight(230)
        self.roi_snapshot.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.roi_x = self._ispin(0, 100000, 0)
        self.roi_y = self._ispin(0, 100000, 0)
        self.roi_w = self._ispin(1, 100000, 1280)
        self.roi_h = self._ispin(1, 100000, 1024)
        self.btn_apply_roi = QPushButton("应用")
        self.btn_restore_roi = QPushButton("恢复")
        self.btn_reset_roi = QPushButton("全幅")
        self.btn_apply_roi.setObjectName("primaryButton")
        self.btn_restore_roi.setObjectName("secondaryButton")
        self.btn_reset_roi.setObjectName("secondaryButton")

        coord_grid = QGridLayout()
        coord_grid.setContentsMargins(0, 0, 0, 0)
        coord_grid.setHorizontalSpacing(6)
        coord_grid.setVerticalSpacing(6)
        coord_grid.addWidget(self._label("X"), 0, 0)
        coord_grid.addWidget(self.roi_x, 0, 1)
        coord_grid.addWidget(self._label("Y"), 0, 2)
        coord_grid.addWidget(self.roi_y, 0, 3)
        coord_grid.addWidget(self._label("宽"), 1, 0)
        coord_grid.addWidget(self.roi_w, 1, 1)
        coord_grid.addWidget(self._label("高"), 1, 2)
        coord_grid.addWidget(self.roi_h, 1, 3)

        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 0, 0, 0)
        button_row.setSpacing(8)
        button_row.addWidget(self.btn_apply_roi)
        button_row.addWidget(self.btn_restore_roi)
        button_row.addWidget(self.btn_reset_roi)

        layout.addWidget(self.roi_snapshot)
        layout.addLayout(coord_grid)
        layout.addLayout(button_row)

        self.btn_apply_roi.clicked.connect(self._apply_roi)
        self.btn_restore_roi.clicked.connect(self._restore_roi_full_frame)
        self.btn_reset_roi.clicked.connect(self._reset_roi)
        return content

    def _xy_stage_box(self) -> QFrame:
        """主项目专属的空间标定设置页。

        位移台连接、状态读数和手动/高级运动由 ``XYStageControlDialog``
        统一提供，这里只保留采集程序额外需要的安全范围与空间标定入口。
        """
        content = QFrame()
        content.setObjectName("xyStageTabContent")
        layout = QGridLayout(content)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(8)

        self.btn_xy_calibrate = QPushButton("自动空间标定")
        self.xy_safe_x_min = self._dspin(-1000, 1000, 0, 4, 0.1)
        self.xy_safe_x_max = self._dspin(-1000, 1000, 20, 4, 0.1)
        self.xy_safe_y_min = self._dspin(-1000, 1000, 0, 4, 0.1)
        self.xy_safe_y_max = self._dspin(-1000, 1000, 20, 4, 0.1)
        for widget in (
            self.xy_safe_x_min, self.xy_safe_x_max, self.xy_safe_y_min, self.xy_safe_y_max,
        ):
            widget.setReadOnly(True)
            widget.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.btn_xy_calibrate.setObjectName("primaryButton")

        hint = QLabel(
            "空间扫描和自动标定与上方位移台控制共用同一个 SDK 工作线程。"
            "扫描运行时手动运动会锁定，但单轴停止和全轴停止始终可用。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint, 0, 0, 1, 4)
        layout.addWidget(self._label("X 安全范围"), 1, 0)
        layout.addWidget(self.xy_safe_x_min, 1, 1)
        layout.addWidget(self.xy_safe_x_max, 1, 2)
        layout.addWidget(self._label("mm"), 1, 3)
        layout.addWidget(self._label("Y 安全范围"), 2, 0)
        layout.addWidget(self.xy_safe_y_min, 2, 1)
        layout.addWidget(self.xy_safe_y_max, 2, 2)
        layout.addWidget(self._label("mm"), 2, 3)
        layout.addWidget(self.btn_xy_calibrate, 3, 0, 1, 4)
        # 空余高度集中在底部，避免安全范围输入框被纵向拉开。
        layout.setRowStretch(4, 1)

        self.btn_xy_calibrate.clicked.connect(self._start_spatial_calibration)
        return content

    def _spatial_overview_settings_box(self) -> QFrame:
        """侧栏 XY 标题对应的小弹窗，仅放置空间概览采集参数。"""
        content = QFrame()
        content.setObjectName("collapsibleContent")
        layout = QGridLayout(content)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(10)

        self.spatial_pixel_um = self._dspin(0.01, 10.0, 0.48, 6, 0.01)
        self.spatial_overlap = self._ispin(10, 40, 20)
        self.spatial_overlap.setSuffix(" %")
        self.spatial_settle = self._ispin(0, 5000, 200)
        self.spatial_settle.setSuffix(" ms")
        self.spatial_route = self._combo(["蛇形", "单向"], "蛇形")

        layout.addWidget(self._label("像素间距 (µm/px)"), 0, 0)
        layout.addWidget(self.spatial_pixel_um, 0, 1)
        layout.addWidget(self._label("重叠率"), 1, 0)
        layout.addWidget(self.spatial_overlap, 1, 1)
        layout.addWidget(self._label("稳定时间"), 2, 0)
        layout.addWidget(self.spatial_settle, 2, 1)
        layout.addWidget(self._label("概览路径"), 3, 0)
        layout.addWidget(self.spatial_route, 3, 1)
        layout.setColumnStretch(1, 1)
        return content

    def _spatial_scan_box(self) -> QGroupBox:
        box = QGroupBox()
        box.setObjectName("xySpatialGroup")
        layout = QGridLayout(box)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setHorizontalSpacing(6)
        layout.setVerticalSpacing(7)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(3, 1)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(7)
        self.btn_xy_section_title = QToolButton()
        self.btn_xy_section_title.setObjectName("xySectionTitle")
        self.btn_xy_section_title.setText("XY位移")
        self.btn_xy_section_title.setToolTip("设置空间概览参数")
        self.btn_xy_section_title.setAccessibleName("打开 XY 空间概览参数")
        self.btn_xy_section_title.setCursor(Qt.PointingHandCursor)
        self.btn_xy_section_title.clicked.connect(
            lambda: self._show_settings_dialog(self.xy_overview_dialog)
        )
        self.xy_realtime_position = QLabel("X=-- mm  Y=-- mm")
        self.xy_realtime_position.setObjectName("xyRealtimePosition")
        self.xy_axis_lamps = {0: StatusLamp("X"), 1: StatusLamp("Y")}
        header.addWidget(self.btn_xy_section_title)
        header.addWidget(self.xy_realtime_position)
        header.addStretch(1)
        header.addWidget(self.xy_axis_lamps[0])
        header.addWidget(self.xy_axis_lamps[1])
        layout.addLayout(header, 0, 0, 1, 4)

        self.survey_x_start = self._dspin(-1000, 1000, 1.0, 4, 0.1)
        self.survey_x_end = self._dspin(-1000, 1000, 2.0, 4, 0.1)
        self.survey_y_start = self._dspin(-1000, 1000, 1.0, 4, 0.1)
        self.survey_y_end = self._dspin(-1000, 1000, 2.0, 4, 0.1)
        self.spatial_roi_status = QLabel("空间 ROI: 未选择")
        self.spatial_roi_status.setWordWrap(True)
        self.btn_start_survey = QPushButton("概览扫描")
        self.btn_select_spatial_roi = QPushButton("框选区域")
        self.btn_plan_spatial = QPushButton("规划")
        self.btn_start_spatial = QPushButton("执行纵向扫描")
        self.btn_stop_spatial = QPushButton("停止")
        self.btn_start_survey.setObjectName("primaryButton")
        self.btn_start_spatial.setObjectName("primaryButton")
        self.btn_stop_spatial.setObjectName("dangerButton")
        for button in (self.btn_select_spatial_roi, self.btn_plan_spatial):
            button.setObjectName("secondaryButton")
        self.btn_stop_spatial.setEnabled(False)

        fields = (
            ("X 中心起点", self.survey_x_start, "X 中心终点", self.survey_x_end),
            ("Y 中心起点", self.survey_y_start, "Y 中心终点", self.survey_y_end),
        )
        for row, (left_text, left, right_text, right) in enumerate(fields, start=1):
            layout.addWidget(self._label(left_text), row, 0)
            layout.addWidget(left, row, 1)
            layout.addWidget(self._label(right_text), row, 2)
            layout.addWidget(right, row, 3)
        layout.addWidget(self.spatial_roi_status, 3, 0, 1, 4)
        layout.addWidget(self.btn_start_survey, 4, 0, 1, 2)
        layout.addWidget(self.btn_select_spatial_roi, 4, 2, 1, 2)
        layout.addWidget(self.btn_plan_spatial, 5, 0)
        layout.addWidget(self.btn_start_spatial, 5, 1, 1, 2)
        layout.addWidget(self.btn_stop_spatial, 5, 3)

        self.btn_start_survey.clicked.connect(self._start_spatial_survey)
        self.btn_select_spatial_roi.clicked.connect(self._activate_spatial_selection)
        self.btn_plan_spatial.clicked.connect(self._plan_selected_spatial_roi)
        self.btn_start_spatial.clicked.connect(self._start_spatial_acquisition)
        self.btn_stop_spatial.clicked.connect(self._stop_spatial_scan)
        self.spatial_pixel_um.editingFinished.connect(self._spatial_pixel_size_changed)
        return box

    def _pzt_box(self) -> QFrame:
        content = QFrame()
        content.setObjectName("collapsibleContent")
        layout = QGridLayout(content)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(8)

        self.conn_type = self._combo(["串口", "网口"], "串口")
        self.port_combo = NoWheelComboBox()
        self.baud_combo = self._combo([str(item) for item in PZT_BAUD_RATES], str(PZT_DEFAULT_BAUD))
        self.btn_refresh_ports = QPushButton("刷新")
        self.btn_pzt_connect = QPushButton("连接")
        self.btn_refresh_ports.setObjectName("secondaryButton")
        self.btn_pzt_connect.setObjectName("primaryButton")
        self.channel_combo = self._combo(["通道0", "通道1", "通道2"], "通道0")
        self.target_um = self._dspin(0, PZT_MAX_UM, 0, decimals=4, step=0.1)
        self.actual_um = QLineEdit("--")
        self.actual_um.setReadOnly(True)
        self.btn_set_pos = QPushButton("设置位移")
        self.btn_zero = QPushButton("归零")
        self.btn_pause_monitor = QPushButton("暂停监控")
        self.btn_set_pos.setObjectName("secondaryButton")
        self.btn_zero.setObjectName("secondaryButton")
        self.btn_pause_monitor.setObjectName("secondaryButton")
        self.btn_pause_monitor.setCheckable(True)
        self.btn_pause_monitor.setEnabled(False)

        layout.addWidget(self._label("类型"), 0, 0)
        layout.addWidget(self.conn_type, 0, 1)
        layout.addWidget(self._label("端口/IP"), 0, 2)
        layout.addWidget(self.port_combo, 0, 3)
        layout.addWidget(self._label("波特率"), 1, 0)
        layout.addWidget(self.baud_combo, 1, 1)
        layout.addWidget(self.btn_refresh_ports, 1, 2)
        layout.addWidget(self.btn_pzt_connect, 1, 3)
        layout.addWidget(self._label("通道"), 2, 0)
        layout.addWidget(self.channel_combo, 2, 1)
        layout.addWidget(self._label("目标 um"), 2, 2)
        layout.addWidget(self.target_um, 2, 3)
        layout.addWidget(self._label("当前 um"), 3, 0)
        layout.addWidget(self.actual_um, 3, 1)
        layout.addWidget(self.btn_set_pos, 3, 2)
        layout.addWidget(self.btn_zero, 3, 3)
        layout.addWidget(self.btn_pause_monitor, 4, 3)

        self.conn_type.currentTextChanged.connect(self._connection_type_changed)
        self.btn_refresh_ports.clicked.connect(self._refresh_ports)
        self.btn_pzt_connect.clicked.connect(self._toggle_pzt)
        self.btn_set_pos.clicked.connect(lambda: self._send_manual_move(self.target_um.value()))
        self.btn_zero.clicked.connect(lambda: self._send_manual_move(0.0))
        return content

    def _scan_tabs(self) -> QFrame:
        box = QFrame()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 4)
        self.scan_tabs = QTabWidget()
        normal = QWidget()
        center = QWidget()

        self.normal_start = self._dspin(0, PZT_MAX_UM, 0, decimals=4, step=0.1)
        self.normal_end = self._dspin(0, PZT_MAX_UM, 10, decimals=4, step=0.1)
        self.normal_step = self._dspin(0.001, PZT_MAX_UM, 0.1, decimals=4, step=0.01)
        self.normal_stable = self._ispin(50, 2000, 200)
        self.normal_repeats = self._ispin(1, 100, 1)

        self.center_pos = self._dspin(0, PZT_MAX_UM, 100, decimals=4, step=0.1)
        self.center_range = self._dspin(0.001, 200, 20, decimals=4, step=0.1)
        self.center_step = self._dspin(0.001, PZT_MAX_UM, 0.1, decimals=4, step=0.01)
        self.center_stable = self._ispin(50, 2000, 200)
        self.center_repeats = self._ispin(1, 100, 1)

        def build_scan_grid(container: QWidget, fields: list[tuple[str, QWidget]]) -> None:
            grid = QGridLayout(container)
            grid.setContentsMargins(4, 8, 4, 4)
            grid.setHorizontalSpacing(6)
            grid.setVerticalSpacing(7)
            for index, (text, widget) in enumerate(fields):
                row = index // 2
                column = (index % 2) * 2
                label = self._label(text)
                label.setFixedWidth(50)
                widget.setFixedWidth(102)
                grid.addWidget(label, row, column)
                grid.addWidget(widget, row, column + 1)
            grid.setColumnStretch(4, 1)

        build_scan_grid(
            normal,
            [
                ("起始 um", self.normal_start),
                ("终止 um", self.normal_end),
                ("步长 um", self.normal_step),
                ("稳定 ms", self.normal_stable),
                ("重复次数", self.normal_repeats),
            ],
        )
        build_scan_grid(
            center,
            [
                ("中心 um", self.center_pos),
                ("范围 um", self.center_range),
                ("步长 um", self.center_step),
                ("稳定 ms", self.center_stable),
                ("重复次数", self.center_repeats),
            ],
        )

        self.scan_tabs.addTab(normal, "普通扫描")
        self.scan_tabs.addTab(center, "中心扫描")
        layout.addWidget(self.scan_tabs)
        return box

    def _calibration_box(self) -> QGroupBox:
        box = QGroupBox("暗场 / 平场校正")
        layout = QGridLayout(box)
        layout.setContentsMargins(0, 8, 0, 4)
        layout.setHorizontalSpacing(6)
        layout.setVerticalSpacing(7)
        self.enable_flat_field = QCheckBox("启用暗场/平场校正")
        self.calibration_frames = self._ispin(1, 128, 16)
        self.calibration_frames.setMaximumWidth(90)
        self.btn_capture_dark = QPushButton("采集暗场")
        self.btn_capture_flat = QPushButton("采集平场")
        self.btn_capture_dark.setObjectName("secondaryButton")
        self.btn_capture_flat.setObjectName("secondaryButton")
        self.calibration_status = QLabel("校正: 未采集")
        self.calibration_status.setWordWrap(True)

        layout.addWidget(self.enable_flat_field, 0, 0, 1, 2)
        layout.addWidget(self._label("校准帧"), 0, 2)
        layout.addWidget(self.calibration_frames, 0, 3)
        layout.addWidget(self.btn_capture_dark, 1, 0, 1, 2)
        layout.addWidget(self.btn_capture_flat, 1, 2, 1, 2)
        layout.addWidget(self.calibration_status, 2, 0, 1, 4)
        self.btn_capture_dark.clicked.connect(lambda: self._capture_calibration("dark"))
        self.btn_capture_flat.clicked.connect(lambda: self._capture_calibration("flat"))
        return box

    def _save_box(self) -> QGroupBox:
        box = QGroupBox("保存")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 8, 0, 4)
        layout.setSpacing(7)
        self.bit_depth = self._combo(["8bit", "12bit"], "12bit")
        self.image_format = self._combo(["tiff", "png"], "tiff")
        self.prefix = QLineEdit("img")
        self.trigger_mode = self._combo(["软触发", "连续采集"], "软触发")
        self.bit_depth.setFixedWidth(132)
        self.prefix.setFixedWidth(132)
        self.image_format.setFixedWidth(84)
        self.trigger_mode.setFixedWidth(84)
        self.save_path = QLineEdit(r"E:\项目文件\lg_profiles\Vscode_projects\samples_pic")
        self.btn_browse = QPushButton("浏览")
        self.btn_browse.setObjectName("secondaryButton")

        def compact_row(
            first_text: str,
            first_widget: QWidget,
            second_text: str,
            second_widget: QWidget,
        ) -> QHBoxLayout:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            first_label = self._label(first_text)
            second_label = self._label(second_text)
            first_label.setFixedWidth(38)
            second_label.setFixedWidth(34)
            row.addWidget(first_label)
            row.addWidget(first_widget)
            row.addSpacing(4)
            row.addWidget(second_label)
            row.addWidget(second_widget)
            row.addStretch(1)
            return row

        layout.addLayout(compact_row("位深", self.bit_depth, "格式", self.image_format))
        layout.addLayout(compact_row("前缀", self.prefix, "触发", self.trigger_mode))

        path_row = QHBoxLayout()
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.setSpacing(6)
        path_label = self._label("路径")
        path_label.setFixedWidth(38)
        self.btn_browse.setFixedWidth(72)
        path_row.addWidget(path_label)
        path_row.addWidget(self.save_path, 1)
        path_row.addWidget(self.btn_browse)
        layout.addLayout(path_row)
        self.btn_browse.clicked.connect(self._browse_save_dir)
        self.bit_depth.currentIndexChanged.connect(self._bit_depth_changed)
        return box

    def _scan_actions(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("scanActions")
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)
        self.btn_start_scan = QPushButton("开始扫描")
        self.btn_stop_scan = QPushButton("停止")
        self.btn_start_scan.setObjectName("primaryButton")
        self.btn_stop_scan.setObjectName("dangerButton")
        self.btn_stop_scan.setEnabled(False)
        layout.addWidget(self.btn_start_scan)
        layout.addWidget(self.btn_stop_scan)
        self.btn_start_scan.clicked.connect(self._start_scan)
        self.btn_stop_scan.clicked.connect(self._stop_scan)
        return panel

    def _log_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("collapsibleContent")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setMinimumSize(680, 360)
        layout.addWidget(self.log)
        return panel

    def _app_settings_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("collapsibleContent")
        layout = QGridLayout(panel)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(10)

        self.theme_combo = self._combo(list(THEME_LABELS.values()), THEME_LABELS[self._theme])
        self.btn_check_update = QPushButton("检查更新")
        self.btn_check_update.setObjectName("secondaryButton")

        layout.addWidget(self._label("界面主题"), 0, 0)
        layout.addWidget(self.theme_combo, 0, 1)
        layout.addWidget(self._label("在线更新"), 1, 0)
        layout.addWidget(self.btn_check_update, 1, 1)
        layout.setColumnStretch(1, 1)

        self.theme_combo.currentTextChanged.connect(self._theme_changed)
        self.btn_check_update.clicked.connect(self._check_updates)
        return panel

    def _combo(self, items: list[str], current: str) -> NoWheelComboBox:
        combo = NoWheelComboBox()
        combo.addItems(items)
        combo.setCurrentText(current)
        return combo

    def _dspin(self, low: float, high: float, value: float, decimals: int, step: float) -> NoWheelDoubleSpinBox:
        box = NoWheelDoubleSpinBox()
        box.setRange(low, high)
        box.setDecimals(decimals)
        box.setSingleStep(step)
        box.setValue(value)
        box.setButtonSymbols(QAbstractSpinBox.NoButtons)
        return box

    def _ispin(self, low: int, high: int, value: int) -> NoWheelSpinBox:
        box = NoWheelSpinBox()
        box.setRange(low, high)
        box.setValue(value)
        box.setButtonSymbols(QAbstractSpinBox.NoButtons)
        return box

    def _slider(self, low: int, high: int, value: int) -> QSlider:
        slider = QSlider(Qt.Horizontal)
        slider.setRange(low, high)
        slider.setValue(value)
        return slider

    def _label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("fieldLabel")
        return label

    def _activate_roi_selection(self) -> None:
        if self._roi_selection_active:
            self._deactivate_roi_selection(restore_full=True)
            return
        if not self.camera.initialized:
            self._open_camera()
            if not self.camera.initialized:
                self.btn_roi_select.setChecked(False)
                return
        if not self.preview_timer.isActive():
            self._start_preview()

        self._roi_selection_active = True
        self.roi_enabled.setChecked(True)
        self.btn_roi_select.setChecked(True)
        self.preview.set_selection_enabled(True)
        self._show_roi_panel()
        self._capture_roi_snapshot()
        self._log("ROI 框选已启用：在预览图上拖拽矩形，松开后会回填坐标，点击“应用”后生效")

    def _deactivate_roi_selection(self, restore_full: bool = False) -> None:
        if restore_full:
            self._reset_sensor_roi_to_full()
        self._roi_selection_active = False
        self.roi_enabled.setChecked(False)
        self.preview.set_selection_enabled(False)
        self.btn_roi_select.setChecked(False)
        if hasattr(self, "roi_settings_panel"):
            self.roi_settings_panel.hide()
        if hasattr(self, "roi_snapshot"):
            self.roi_snapshot.clear_snapshot()

    def _show_roi_panel(self) -> None:
        self.roi_settings_panel.show()

    def _capture_roi_snapshot(self) -> None:
        if not self.camera.initialized:
            self.roi_snapshot.clear_snapshot()
            return
        self._update_preview_roi_geometry_from_camera()
        frame = self.camera.grab()
        if frame is None:
            self.roi_snapshot.clear_snapshot()
            return
        origin_x, origin_y = self._preview_origin
        self.roi_snapshot.set_snapshot(
            frame_to_pixmap(frame, max(self.roi_snapshot.width(), 320), max(self.roi_snapshot.height(), 220)),
            frame.shape[1],
            frame.shape[0],
            origin_x,
            origin_y,
        )
        self._refresh_roi_snapshot_overlay()

    def _refresh_roi_snapshot_overlay(self) -> None:
        self.roi_snapshot.set_roi(
            self.roi_x.value(),
            self.roi_y.value(),
            self.roi_w.value(),
            self.roi_h.value(),
        )

    def _reset_sensor_roi_to_full(self) -> tuple[int, int, int, int] | None:
        if not self.camera.initialized:
            return None
        try:
            x, y, width, height = self.camera.reset_sensor_roi()
            self._set_roi_values(x, y, width, height)
            self._preview_origin = (0, 0)
            self.camera_status.setText(f"相机: 已连接 {self.camera.width}x{self.camera.height}")
            self._update_preview_roi_geometry_from_camera()
            self._invalidate_calibration("ROI 已变化")
            self._log(f"ROI 已恢复全幅: X={x}, Y={y}, 宽={width}, 高={height}")
            return x, y, width, height
        except Exception as exc:
            self._show_error("ROI 恢复全幅失败", exc)
            return None

    def _apply_style(self) -> None:
        p = self._theme_palette()
        self.setStyleSheet(
            f"""
            QMainWindow {{ background: {p["main_bg"]}; }}
            QWidget {{
                color: {p["text"]};
                font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
                font-size: 12px;
                letter-spacing: 0;
            }}
            QFrame#workSurface {{ background: {p["main_bg"]}; }}
            QFrame#topBar {{
                background: {p["panel_strong"]};
                border-bottom: 1px solid {p["border"]};
            }}
            QToolButton#ribbonToolButton {{
                min-width: 44px;
                max-width: 44px;
                min-height: 38px;
                max-height: 38px;
                padding: 2px 5px;
                color: {p["tool_icon"]};
                background: transparent;
                border: 0;
                border-right: 1px solid {p["border"]};
            }}
            QToolButton#ribbonToolButton:hover {{
                background: {p["button_hover"]};
            }}
            QToolButton#ribbonToolButton:pressed {{
                background: {p["button_pressed"]};
            }}
            QToolButton#ribbonToolButton:checked {{
                background: {p["accent_soft"]};
                border-bottom: 2px solid {p["accent"]};
            }}
            QLabel#deviceStatus {{
                min-width: 88px;
                padding: 3px 5px;
                color: {p["text"]};
                background: transparent;
                border: 0;
                font-size: 12px;
                font-weight: 400;
            }}
            QFrame#viewerShell {{
                background: {p["surface"]};
                border: 0;
            }}
            QLabel#preview, QLabel#roiSnapshot {{
                background: {p["preview_bg"]};
                color: {p["preview_text"]};
                border: 1px solid {p["border_strong"]};
                border-radius: 2px;
                font-weight: 400;
            }}
            QLabel#preview {{ font-size: 16px; }}
            QLabel#roiSnapshot {{ font-size: 13px; }}
            QLabel#statusPill {{
                background: {p["panel_alt"]};
                color: {p["text"]};
                border: 1px solid {p["border"]};
                border-left: 3px solid {p["accent"]};
                border-radius: 2px;
                padding: 5px 8px;
                font-size: 12px;
            }}
            QWidget#sidePanel, QScrollArea#sideScroll {{
                background: {p["panel"]};
                border-left: 1px solid {p["border"]};
            }}
            QGroupBox {{
                background: transparent;
                border: 0;
                border-top: 1px solid {p["border"]};
                margin-top: 20px;
                padding: 12px 0 0 0;
                font-weight: 500;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 0;
                padding: 0 8px 0 0;
                color: {p["title"]};
                background: {p["panel"]};
            }}
            QGroupBox#xySpatialGroup {{
                margin-top: 6px;
                padding-top: 6px;
            }}
            QToolButton#xySectionTitle {{
                color: {p["title"]};
                background: transparent;
                border: 0;
                border-radius: 2px;
                padding: 3px 5px;
                font-size: 13px;
                font-weight: 700;
            }}
            QToolButton#xySectionTitle:hover {{
                color: {p["accent"]};
                background: {p["accent_soft"]};
            }}
            QToolButton#xySectionTitle:pressed {{
                background: {p["button_pressed"]};
            }}
            QLabel#xyRealtimePosition {{
                color: {p["text"]};
                background: transparent;
                border: 0;
                padding: 3px 1px;
                font-family: "Consolas", "Microsoft YaHei UI", sans-serif;
                font-size: 12px;
                font-weight: 600;
            }}
            QLabel#technicalReadout {{
                color: {p["text_soft"]};
                background: transparent;
                border: 0;
                font-size: 11px;
            }}
            QFrame#collapsible {{
                background: {p["panel_alt"]};
                border: 1px solid {p["border"]};
                border-radius: 2px;
            }}
            QFrame#collapsibleContent {{
                background: {p["panel_alt"]};
                border-top: 1px solid {p["border"]};
            }}
            QDialog#xyStageDialog,
            QWidget#xyStageManualPage,
            QWidget#xyStageGridPage,
            QWidget#xyStageAdvancedPanel,
            QFrame#xyStageTabContent,
            QTabWidget#xyStageTabs,
            QTabWidget#xyStageTabs::pane,
            QTabWidget#xyStageAdvancedTabs,
            QTabWidget#xyStageAdvancedTabs::pane {{
                background: {p["panel"]};
            }}
            QToolButton#disclosure {{
                border: 0;
                border-radius: 2px;
                padding: 10px 11px;
                background: {p["panel_alt"]};
                color: {p["title"]};
                font-weight: 700;
                text-align: left;
            }}
            QToolButton#disclosure:hover {{ background: {p["button_hover"]}; }}
            QLabel#fieldLabel {{
                color: {p["text_soft"]};
                font-size: 12px;
                font-weight: 400;
            }}
            QPushButton {{
                border-radius: 2px;
                padding: 5px 9px;
                min-height: 20px;
                font-weight: 400;
            }}
            QPushButton#primaryButton {{
                background: {p["accent"]};
                color: {p["selection_text"]};
                border: 1px solid {p["accent_hover"]};
            }}
            QPushButton#primaryButton:hover {{ background: {p["accent_hover"]}; }}
            QPushButton#primaryButton:pressed {{ background: {p["accent_pressed"]}; }}
            QPushButton#secondaryButton {{
                background: {p["button_bg"]};
                color: {p["text"]};
                border: 1px solid {p["border_strong"]};
            }}
            QPushButton#secondaryButton:hover {{
                background: {p["button_hover"]};
                border-color: {p["accent"]};
            }}
            QPushButton#secondaryButton:pressed {{ background: {p["button_pressed"]}; }}
            QPushButton#dangerButton {{
                background: {p["danger_bg"]};
                color: {p["danger_text"]};
                border: 1px solid {p["danger_border"]};
            }}
            QPushButton#dangerButton:hover {{
                background: {p["danger_hover"]};
                border-color: {p["danger_text"]};
            }}
            QPushButton#dangerButton:pressed {{ background: {p["danger_pressed"]}; }}
            QPushButton#primaryButton:disabled,
            QPushButton#secondaryButton:disabled,
            QPushButton#dangerButton:disabled,
            QPushButton:disabled {{
                background: {p["disabled_bg"]};
                border: 1px solid {p["border"]};
                color: {p["disabled_text"]};
            }}
            QCheckBox {{
                spacing: 7px;
                color: {p["text"]};
                font-weight: 400;
            }}
            QCheckBox::indicator {{
                width: 14px;
                height: 14px;
                border: 1px solid {p["border_strong"]};
                background: {p["input_bg"]};
                border-radius: 2px;
            }}
            QCheckBox::indicator:checked {{
                background: {p["accent"]};
                border-color: {p["accent_hover"]};
            }}
            QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox, QPlainTextEdit {{
                background: {p["input_bg"]};
                color: {p["title"]};
                border: 1px solid {p["border_strong"]};
                border-radius: 2px;
                padding: 6px;
                selection-background-color: {p["accent"]};
                selection-color: {p["selection_text"]};
            }}
            QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus, QSpinBox:focus {{
                border-color: {p["accent"]};
                background: {p["input_focus"]};
            }}
            QComboBox::drop-down {{
                border: 0;
                width: 22px;
                background: {p["panel_alt"]};
            }}
            QComboBox QAbstractItemView {{
                background: {p["surface"]};
                color: {p["title"]};
                border: 1px solid {p["border_strong"]};
                selection-background-color: {p["accent"]};
                selection-color: {p["selection_text"]};
            }}
            QLineEdit:read-only {{
                color: {p["accent"]};
                background: {p["input_focus"]};
            }}
            QTabWidget::pane {{
                border: 0;
                border-top: 1px solid {p["border"]};
                background: transparent;
                top: 0;
            }}
            QTabBar::tab {{
                background: transparent;
                color: {p["text_muted"]};
                padding: 7px 14px;
                border: 0;
                border-bottom: 2px solid transparent;
                font-weight: 400;
            }}
            QTabBar::tab:selected {{
                color: {p["title"]};
                border-bottom: 2px solid {p["accent"]};
            }}
            QSlider::groove:horizontal {{
                height: 4px;
                background: {p["panel_strong"]};
                border: 1px solid {p["border"]};
                border-radius: 2px;
            }}
            QSlider::sub-page:horizontal {{
                background: {p["accent"]};
                border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                width: 14px;
                height: 14px;
                margin: -6px 0;
                border-radius: 7px;
                background: {p["surface"]};
                border: 1px solid {p["accent"]};
            }}
            QLabel#meterValue {{
                color: {p["accent"]};
                min-width: 34px;
            }}
            QProgressBar {{
                background: {p["input_bg"]};
                color: {p["text"]};
                border: 1px solid {p["border_strong"]};
                border-radius: 2px;
                text-align: center;
                min-height: 20px;
                font-weight: 400;
            }}
            QProgressBar::chunk {{
                background: {p["accent"]};
                border-radius: 1px;
            }}
            QProgressBar#scanProgress {{
                min-height: 6px;
                max-height: 6px;
                border: 0;
                border-radius: 0;
                background: {p["panel_alt"]};
            }}
            QProgressBar#scanProgress::chunk {{
                background: {p["accent"]};
                border-radius: 0;
            }}
            QPlainTextEdit {{
                font-size: 12px;
                line-height: 1.35;
            }}
            QScrollBar:vertical {{
                background: {p["panel_alt"]};
                width: 10px;
                margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: {p["border_strong"]};
                border-radius: 2px;
                min-height: 28px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QDialog#settingsDialog {{
                background: {p["panel_strong"]};
            }}
            QDialog#settingsDialog QFrame#collapsibleContent {{
                border: 1px solid {p["border"]};
                border-radius: 2px;
                background: {p["panel_alt"]};
            }}
            QDialog#updateProgressDialog {{
                background: {p["panel_strong"]};
                color: {p["text"]};
                font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
            }}
            QDialog#updateProgressDialog QLabel#updateDialogIcon {{
                background: {p["accent"]};
                border-radius: 6px;
                padding: 3px;
            }}
            QDialog#updateProgressDialog QLabel#updateDialogTitle {{
                color: {p["title"]};
                font-size: 18px;
                font-weight: 700;
            }}
            QDialog#updateProgressDialog QLabel#updateDialogStatus {{
                color: {p["text_soft"]};
                font-size: 13px;
                font-weight: 500;
            }}
            QDialog#updateProgressDialog QLabel#updateDialogDetail {{
                color: {p["accent"]};
                font-size: 12px;
                font-weight: 700;
                font-variant-numeric: tabular-nums;
            }}
            QProgressBar#updateProgress {{
                min-height: 12px;
                max-height: 12px;
                border: 1px solid {p["border_strong"]};
                border-radius: 3px;
                background: {p["input_bg"]};
            }}
            QProgressBar#updateProgress::chunk {{
                background: {p["accent"]};
                border-radius: 2px;
            }}
            QMessageBox {{
                background: {p["message_bg"]};
            }}
            QMessageBox QLabel {{
                color: #1f2933;
                font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
                font-size: 13px;
                font-weight: 500;
            }}
            QMessageBox QPushButton {{
                min-width: 78px;
                min-height: 28px;
                padding: 5px 14px;
                color: #1f2933;
                background: {p["message_button"]};
                border: 1px solid #c8ced8;
                border-radius: 3px;
                font-weight: 600;
            }}
            QMessageBox QPushButton:hover {{
                background: #edf2f6;
                border-color: #9aa6b2;
            }}
            """
        )
        self._refresh_theme_widgets(p)

    def _refresh_theme_widgets(self, palette: dict[str, str]) -> None:
        for button, icon in self._tool_buttons:
            button.setIcon(tool_icon(icon, palette["tool_icon"]))
        if hasattr(self, "camera_status"):
            self.camera_status.set_theme_colors(palette["text"], palette["device_ok"], palette["device_bad"])
        if hasattr(self, "pzt_status"):
            self.pzt_status.set_theme_colors(palette["text"], palette["device_ok"], palette["device_bad"])
        if hasattr(self, "xy_status"):
            self.xy_status.set_theme_colors(palette["text"], palette["device_ok"], palette["device_bad"])
        if hasattr(self, "theme_combo"):
            self.theme_combo.blockSignals(True)
            self.theme_combo.setCurrentText(THEME_LABELS[self._theme])
            self.theme_combo.blockSignals(False)
        enable_dark_title_bar(self, self._theme == "dark")

    def _check_updates(self) -> None:
        self.btn_check_update.setEnabled(False)
        self._log(f"正在检查更新，当前版本 v{__version__} ...")

        def worker() -> None:
            try:
                result = check_latest_release(__version__)
                self.bridge.update_checked.emit(result, None)
            except Exception as exc:
                self.bridge.update_checked.emit(None, exc)

        threading.Thread(target=worker, name="update-checker", daemon=True).start()

    def _on_update_checked(self, result: object, exc: object) -> None:
        self.btn_check_update.setEnabled(True)
        if exc is not None:
            message = str(exc if isinstance(exc, Exception) else RuntimeError(str(exc)))
            self._append_log(message)
            QMessageBox.warning(self, "检查更新失败", message)
            return
        update = result if isinstance(result, UpdateInfo) else None
        if update is None:
            return
        self._append_log(f"检查更新完成: 当前 v{update.current_version}，最新 {update.latest_version}")
        if not update.is_newer:
            QMessageBox.information(self, "检查更新", f"当前已是最新版本: v{update.current_version}")
            return

        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Information)
        dialog.setWindowTitle("发现新版本")
        dialog.setText(f"发现新版本 {update.latest_version}，当前版本 v{update.current_version}。")
        dialog.setInformativeText("点击“立即更新”后会在后台下载安装包，下载完成后自动启动安装器并退出当前程序。")
        if update.release_notes:
            dialog.setDetailedText(update.release_notes[:4000])
        dialog.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        dialog.setDefaultButton(QMessageBox.Yes)
        yes_button = dialog.button(QMessageBox.Yes)
        no_button = dialog.button(QMessageBox.No)
        if yes_button is not None:
            yes_button.setText("立即更新")
        if no_button is not None:
            no_button.setText("稍后")
        if dialog.exec() == QMessageBox.Yes:
            self._download_update(update)

    def _download_update(self, update: UpdateInfo) -> None:
        self.btn_check_update.setEnabled(False)
        self._update_progress_dialog = UpdateProgressDialog(self)
        self._update_progress_dialog.setStyleSheet(self.styleSheet())
        self._update_progress_dialog.show()
        enable_dark_title_bar(self._update_progress_dialog, self._theme == "dark")

        def worker() -> None:
            try:
                path = download_installer(
                    update,
                    progress=lambda received, total: self.bridge.update_download_progress.emit(received, total),
                )
                self.bridge.update_downloaded.emit(path, None)
            except Exception as exc:
                self.bridge.update_downloaded.emit(None, exc)

        threading.Thread(target=worker, name="update-downloader", daemon=True).start()

    def _on_update_download_progress(self, received: int, total: int) -> None:
        dialog = self._update_progress_dialog
        if dialog is None:
            return
        dialog.set_progress(received, total)

    def _on_update_downloaded(self, path: object, exc: object) -> None:
        self.btn_check_update.setEnabled(True)
        if self._update_progress_dialog is not None:
            self._update_progress_dialog.close()
            self._update_progress_dialog = None
        if exc is not None:
            message = str(exc if isinstance(exc, Exception) else RuntimeError(str(exc)))
            self._append_log(message)
            QMessageBox.warning(self, "在线更新失败", message)
            return
        installer_path = Path(str(path))
        self._append_log(f"安装包下载完成: {installer_path}")
        QMessageBox.information(self, "在线更新", "安装包已下载完成。程序将退出并启动安装器。")
        try:
            start_installer(installer_path)
        except Exception as start_exc:
            self._show_error("启动安装器失败", start_exc)
            return
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _open_camera(self) -> None:
        try:
            self.camera.open()
            self._refresh_camera_capabilities()
            self._apply_current_bit_depth()
            self.camera_status.setText(f"相机: 已连接 {self.camera.width}x{self.camera.height}")
            self._update_spatial_center_ranges()
            self._log("相机初始化完成")
        except Exception as exc:
            self._show_error("相机连接失败", exc)

    def _start_preview(self) -> None:
        if not self.camera.initialized:
            self._open_camera()
            if not self.camera.initialized:
                return
        self.camera.reset_capture_count()
        self.camera.reset_timeout_counters()
        self.camera.set_trigger_mode(0)
        self.preview_timer.start()
        self.btn_preview.setEnabled(False)
        self.btn_stop_preview.setEnabled(True)
        self.camera_status.setText("相机: 预览中")

    def _stop_preview(self) -> None:
        self.preview_timer.stop()
        self.btn_preview.setEnabled(True)
        self.btn_stop_preview.setEnabled(False)
        self.camera_status.setText("相机: 已停止")

    def _display_rate_changed(self, text: str) -> None:
        fps = int(text.split()[0])
        self.preview_timer.setInterval(max(1, round(1000 / fps)))

    def _update_preview(self) -> None:
        frame = self.camera.grab()
        if frame is not None:
            self._update_preview_roi_geometry(frame)
            self.preview.setPixmap(frame_to_pixmap(frame, self.preview.width(), self.preview.height()))

    def _update_camera_status_readbacks(self) -> None:
        self._update_camera_health()
        self._refresh_exposure_readback()

    def _update_xy_status_readback(self) -> None:
        """无论位移台弹窗是否打开，都从 executor 的线程安全快照刷新主界面。"""
        if self.xy_stage is None:
            self.xy_status.setText("XY: 未连接")
            self.xy_realtime_position.setText("X=-- mm  Y=-- mm")
            for lamp in self.xy_axis_lamps.values():
                lamp.set_state("off", "位移台未连接")
            return
        try:
            self._on_xy_dialog_snapshot(self.xy_stage.snapshot)
        except Exception as exc:
            self._log(f"XY 状态刷新失败: {exc}")

    def _update_camera_health(self) -> None:
        if not self.camera.initialized:
            self.fps_status.setText("显示: -- FPS | 采集帧: 0")
            self.fps_status.setToolTip("")
            return
        health = self.camera.health()
        display_fps = 1000 / max(self.preview_timer.interval(), 1) if self.preview_timer.isActive() else 0.0
        age_text = "--" if health.latest_frame_age_ms is None else f"{health.latest_frame_age_ms:.0f} ms"
        lost_text = "--" if health.sdk_lost_frames is None else str(health.sdk_lost_frames)
        self.fps_status.setText(
            f"显示: {display_fps:.1f} FPS | 帧: {health.capture_count} | 丢帧: {lost_text} | "
            f"异常超时: {health.timeout_count} | 触发等待: {health.trigger_wait_count} | 帧龄: {age_text}"
        )
        reconnect = "--" if health.reconnect_count is None else str(health.reconnect_count)
        self.fps_status.setToolTip((health.last_error or "相机最近无错误") + f"\n自动重连次数: {reconnect}")

    def _refresh_exposure_readback(self) -> None:
        if not self.camera.initialized:
            return
        try:
            exposure = self.camera.get_exposure()
            gain = self.camera.get_gain_x()
        except Exception:
            return
        self.exposure_status.setText(f"曝光: {exposure:.2f} us | 增益: {gain:.2f}")
        if self.auto_exposure.isChecked():
            self.exposure_us.blockSignals(True)
            self.gain_x.blockSignals(True)
            self.exposure_us.setValue(exposure)
            self.gain_x.setValue(gain)
            self.exposure_us.blockSignals(False)
            self.gain_x.blockSignals(False)

    def _auto_exposure_changed(self, checked: bool) -> None:
        self._sync_exposure_controls()
        self._safe_camera_call(lambda: self.camera.set_auto_exposure(checked))
        if not checked:
            self._manual_exposure_committed()
            self._manual_gain_committed()

    def _sync_exposure_controls(self) -> None:
        auto = self.auto_exposure.isChecked()
        self.exposure_us.setReadOnly(auto)
        self.gain_x.setReadOnly(auto)
        self.exposure_us.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.gain_x.setButtonSymbols(QAbstractSpinBox.NoButtons)

    def _manual_exposure_committed(self) -> None:
        if self.auto_exposure.isChecked():
            return
        self._safe_camera_call(lambda: self.camera.set_exposure(self.exposure_us.value()))

    def _manual_gain_committed(self) -> None:
        if self.auto_exposure.isChecked():
            return
        self._safe_camera_call(lambda: self.camera.set_gain_x(self.gain_x.value()))

    def _refresh_camera_capabilities(self) -> None:
        try:
            exposure_range = self.camera.exposure_range()
            roi = self.camera.roi()
        except Exception as exc:
            self._log(f"读取相机能力失败: {exc}")
            return
        self.exposure_us.blockSignals(True)
        self.gain_x.blockSignals(True)
        self.exposure_us.setRange(exposure_range.exposure_min_us, exposure_range.exposure_max_us)
        self.gain_x.setRange(exposure_range.gain_min_x, exposure_range.gain_max_x)
        if exposure_range.exposure_step_us:
            self.exposure_us.setSingleStep(max(exposure_range.exposure_step_us, 1.0))
        if exposure_range.gain_step_x:
            self.gain_x.setSingleStep(max(exposure_range.gain_step_x, 0.01))
        self.exposure_us.blockSignals(False)
        self.gain_x.blockSignals(False)

        sensor_min_width, sensor_min_height, sensor_max_width, sensor_max_height = self._safe_roi_spin_limits(
            roi.min_width,
            roi.min_height,
            roi.max_width,
            roi.max_height,
        )
        for spin, low, high, value in [
            (self.roi_x, 0, sensor_max_width - sensor_min_width, roi.x),
            (self.roi_y, 0, sensor_max_height - sensor_min_height, roi.y),
            (self.roi_w, sensor_min_width, sensor_max_width, roi.width),
            (self.roi_h, sensor_min_height, sensor_max_height, roi.height),
        ]:
            spin.blockSignals(True)
            spin.setRange(max(0, low), max(0, high))
            spin.setValue(max(spin.minimum(), min(spin.maximum(), value)))
            spin.blockSignals(False)
        self._sensor_max_width = sensor_max_width
        self._sensor_max_height = sensor_max_height
        self._sensor_min_width = sensor_min_width
        self._sensor_min_height = sensor_min_height
        self._preview_origin = (roi.x, roi.y)
        self._log(
            "相机能力: "
            f"曝光 {exposure_range.exposure_min_us:.2f}-{exposure_range.exposure_max_us:.2f} us, "
            f"增益 {exposure_range.gain_min_x:.2f}-{exposure_range.gain_max_x:.2f}, "
            f"ROI {roi.max_width}x{roi.max_height}, "
            f"BinSumMask={roi.bin_sum_mask}, BinAvgMask={roi.bin_average_mask}, SkipMask={roi.skip_mask}"
        )

    def _apply_roi(self) -> None:
        if not self.camera.initialized:
            return
        try:
            if self.roi_enabled.isChecked():
                x = self.roi_x.value()
                y = self.roi_y.value()
                width = self.roi_w.value()
                height = self.roi_h.value()
                x, y, width, height = self.camera.set_sensor_roi(x, y, width, height)
                self._set_roi_values(x, y, width, height)
                self._preview_origin = (x, y)
            else:
                x, y, width, height = self.camera.reset_sensor_roi()
                self._set_roi_values(x, y, width, height)
                self._preview_origin = (0, 0)
            self.camera_status.setText(f"相机: 已连接 {self.camera.width}x{self.camera.height}")
            self._update_preview_roi_geometry_from_camera()
            self._refresh_roi_snapshot_overlay()
            self._invalidate_calibration("ROI 已变化")
            self._log(f"ROI 已应用: X={x}, Y={y}, 宽={width}, 高={height}")
        except Exception as exc:
            self._show_error("ROI 设置失败", exc)

    def _safe_roi_spin_limits(
        self,
        min_width: int,
        min_height: int,
        max_width: int,
        max_height: int,
    ) -> tuple[int, int, int, int]:
        max_width = max(1, int(max_width or 0))
        max_height = max(1, int(max_height or 0))
        min_width = max(1, min(int(min_width or 0), max_width))
        min_height = max(1, min(int(min_height or 0), max_height))
        return min_width, min_height, max_width, max_height

    def _reset_roi(self) -> None:
        self._deactivate_roi_selection(restore_full=True)

    def _restore_roi_full_frame(self) -> None:
        if self._reset_sensor_roi_to_full() is None:
            return
        self._roi_selection_active = True
        self.roi_enabled.setChecked(True)
        self.btn_roi_select.setChecked(True)
        self.preview.set_selection_enabled(True)
        self._show_roi_panel()
        self._capture_roi_snapshot()

    def _update_preview_roi_geometry(self, frame: np.ndarray) -> None:
        self._update_preview_roi_geometry_from_camera()
        origin_x, origin_y = self._preview_origin
        self.preview.set_source_geometry(frame.shape[1], frame.shape[0], origin_x, origin_y)

    def _update_preview_roi_geometry_from_camera(self) -> None:
        if not self.camera.initialized:
            self._preview_origin = (0, 0)
            return
        try:
            roi = self.camera.roi()
            self._preview_origin = (roi.x, roi.y)
        except Exception:
            pass

    def _roi_selected_from_preview(self, x: int, y: int, width: int, height: int) -> None:
        if not self._roi_selection_active:
            return
        self.roi_enabled.setChecked(True)
        sensor_min_width, sensor_min_height, sensor_max_width, sensor_max_height = self._safe_roi_spin_limits(
            self._sensor_min_width,
            self._sensor_min_height,
            self._sensor_max_width,
            self._sensor_max_height,
        )
        x = max(0, min(x, sensor_max_width - sensor_min_width))
        y = max(0, min(y, sensor_max_height - sensor_min_height))
        width = max(sensor_min_width, min(width, sensor_max_width - x))
        height = max(sensor_min_height, min(height, sensor_max_height - y))
        self.roi_x.setValue(x)
        self.roi_y.setValue(y)
        self.roi_w.setValue(width)
        self.roi_h.setValue(height)
        self._show_roi_panel()
        self._capture_roi_snapshot()
        self._log(
            "ROI 坐标已回填: "
            f"X={x}, Y={y}, 宽={width}, 高={height}；点击“应用”后切换传感器 ROI"
        )

    def _set_roi_values(self, x: int, y: int, width: int, height: int) -> None:
        for spin, value in (
            (self.roi_x, x),
            (self.roi_y, y),
            (self.roi_w, width),
            (self.roi_h, height),
        ):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        self._refresh_roi_snapshot_overlay()

    def _anti_flick_changed(self, checked: bool) -> None:
        self.light_frequency.setEnabled(checked)
        self._safe_camera_call(lambda: self.camera.set_anti_flick(checked))

    def _light_frequency_changed(self, text: str) -> None:
        self._safe_camera_call(lambda: self.camera.set_light_frequency(0 if text == "50Hz" else 1))

    def _snapshot(self) -> None:
        frame = self.camera.grab()
        if frame is None:
            self._show_error("抓拍失败", RuntimeError("没有可用图像"))
            return
        snapshot_dir = Path(self.save_path.text()) / "snapshot"
        path = next_numbered_path(snapshot_dir, "snapshot", self.image_format.currentText())
        try:
            self.scanner._save_frame(path, frame, 8 if self.bit_depth.currentIndex() == 0 else 12)
            self._log(f"抓拍已保存: {path}")
        except Exception as exc:
            self._show_error("抓拍失败", exc)

    def _capture_calibration(self, kind: str) -> None:
        if not self.camera.initialized:
            self._show_error("校准失败", RuntimeError("请先连接相机"))
            return
        if kind == "dark":
            confirmed = self._ask_confirmation("采集暗场", "请遮光或关闭光源后继续采集暗场。")
        else:
            confirmed = self._ask_confirmation("采集平场", "请切换到均匀照明、无样品或无干涉条纹条件后继续采集平场。")
        if not confirmed:
            return
        try:
            self._apply_current_bit_depth()
            if hasattr(self.camera, "apply_quantitative_profile"):
                self.camera.apply_quantitative_profile()
                self.auto_exposure.setChecked(False)
                self._sync_exposure_controls()
                self._refresh_exposure_readback()
            frame_count = self.calibration_frames.value()
            average = self._capture_average_frame(frame_count)
            signature = self._stable_camera_signature()
            if kind == "dark":
                self._dark_average = average
                self._calibration_signature = signature
            else:
                self._flat_average = average
                if self._calibration_signature is not None:
                    self._assert_signature_matches(self._calibration_signature, signature)
                else:
                    self._calibration_signature = signature
            self._update_calibration_status()
            self._log(f"{'暗场' if kind == 'dark' else '平场'}已采集: {frame_count} 帧平均")
        except Exception as exc:
            self._show_error("校准失败", exc)

    def _ask_confirmation(self, title: str, text: str) -> bool:
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Question)
        dialog.setWindowTitle(title)
        dialog.setText(text)
        dialog.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        dialog.setDefaultButton(QMessageBox.No)
        yes_button = dialog.button(QMessageBox.Yes)
        no_button = dialog.button(QMessageBox.No)
        if yes_button is not None:
            yes_button.setText("继续")
        if no_button is not None:
            no_button.setText("取消")
        return dialog.exec() == QMessageBox.Yes

    def _capture_average_frame(self, frame_count: int) -> np.ndarray:
        frames: list[np.ndarray] = []
        deadline = frame_count * 2.0 + 5.0
        start = self.camera.capture_count
        limit_time = time.perf_counter() + deadline
        while len(frames) < frame_count and time.perf_counter() < limit_time:
            sample = self.camera.grab_sample()
            if sample is not None and sample.capture_count > start:
                frames.append(sample.frame.astype(np.float32))
                start = sample.capture_count
            time.sleep(0.03)
        if len(frames) < frame_count:
            raise RuntimeError(f"校准帧不足: {len(frames)} / {frame_count}")
        return np.mean(np.stack(frames, axis=0), axis=0)

    def _stable_camera_signature(self) -> dict[str, object]:
        signature = self.camera.capture_signature() if hasattr(self.camera, "capture_signature") else {}
        keys = (
            "width",
            "height",
            "output_bit_depth",
            "exposure_us",
            "gain_x",
            "roi_x",
            "roi_y",
            "roi_width",
            "roi_height",
            "lut_mode",
            "gamma",
            "contrast",
            "sharpness",
            "black_level",
            "white_level",
            "defect_correction",
            "noise_filter",
            "sdk_flat_fielding",
            "denoise3d",
        )
        stable = {key: signature.get(key) for key in keys}
        stable["save_bit_depth"] = 8 if self.bit_depth.currentIndex() == 0 else 12
        return stable

    def _assert_signature_matches(self, expected: dict[str, object], actual: dict[str, object]) -> None:
        mismatches = []
        for key, expected_value in expected.items():
            actual_value = actual.get(key)
            if isinstance(expected_value, float) or isinstance(actual_value, float):
                if expected_value is None or actual_value is None or abs(float(expected_value) - float(actual_value)) > 1e-3:
                    mismatches.append(f"{key}: {expected_value} != {actual_value}")
            elif expected_value != actual_value:
                mismatches.append(f"{key}: {expected_value} != {actual_value}")
        if mismatches:
            raise RuntimeError("校准参数不匹配: " + "; ".join(mismatches[:4]))

    def _update_calibration_status(self) -> None:
        dark = "暗场OK" if self._dark_average is not None else "暗场缺失"
        flat = "平场OK" if self._flat_average is not None else "平场缺失"
        self.calibration_status.setText(f"校正: {dark} | {flat}")

    def _invalidate_calibration(self, reason: str) -> None:
        had_camera_calibration = self._dark_average is not None or self._flat_average is not None
        if had_camera_calibration:
            self._dark_average = None
            self._flat_average = None
            self._calibration_signature = None
            self._update_calibration_status()
        had_spatial_calibration = bool(getattr(self, "spatial_calibration", None) and self.spatial_calibration.fingerprint)
        had_spatial_state = self._survey_plan is not None or self._acquisition_plan is not None
        if hasattr(self, "spatial_pixel_um"):
            self.spatial_calibration = default_calibration(self.spatial_pixel_um.value())
            self._survey_plan = None
            self._acquisition_plan = None
            self._spatial_roi = None
            self.spatial_roi_status.setText("空间 ROI: 标定已失效")
        if had_camera_calibration or had_spatial_calibration or had_spatial_state:
            self._log(f"校准已失效: {reason}")

    def _connection_type_changed(self, text: str) -> None:
        self.baud_combo.setEnabled(text == "串口")
        self._refresh_ports()

    def _refresh_ports(self) -> None:
        self.port_combo.clear()
        if self.conn_type.currentText() == "串口":
            ports = self.pzt.list_serial_ports()
            self.port_combo.addItems(ports if ports else ["无可用串口"])
        else:
            self.port_combo.addItem(PZT_DEFAULT_IP)
            self.baud_combo.setCurrentText(str(PZT_UDP_PORT))

    def _refresh_xy_ports(self) -> None:
        if hasattr(self, "xy_settings_dialog"):
            self.xy_settings_dialog.refresh_ports()

    def _xy_dll_dir(self) -> Path:
        candidates: list[Path] = []
        configured = os.environ.get("HTGE_XY_STAGE_DLL_DIR")
        if configured:
            candidates.append(Path(configured))
        candidates.extend(
            [
                Path(__file__).resolve().parents[1] / "xy_stage" / "vendor" / "x64",
                Path(sys.executable).resolve().parent / "vendor" / "x64",
            ]
        )
        for candidate in candidates:
            if (candidate / "zauxdll.dll").exists() and (candidate / "zmotion.dll").exists():
                return candidate
        raise RuntimeError(
            "未找到 XY 位移台 DLL。请将 zauxdll.dll 和 zmotion.dll 放入 "
            "grab_app/xy_stage/vendor/x64，或设置 HTGE_XY_STAGE_DLL_DIR。"
        )

    def _connect_xy_stage_from_dialog(self, port: str) -> None:
        try:
            if self.xy_stage is not None and self.xy_stage.connected:
                self.xy_settings_dialog.set_executor(self.xy_stage)
                return
            port = port.strip().upper()
            if not port:
                raise RuntimeError("请选择 XY 位移台串口")
            executor = XYStageExecutor(self._xy_dll_dir())
            try:
                snapshot = executor.connect(port)
            except Exception:
                executor.close()
                raise
            self.xy_stage = executor
            if not snapshot.parameter_valid:
                message = snapshot.fault_message or "XY 位移台参数校验未通过"
                executor.close()
                self.xy_stage = None
                raise RuntimeError(message)
            self.xy_settings_dialog.set_executor(executor)
            self.xy_status.setText("XY: 已连接")
            self._update_xy_snapshot(snapshot)
            self._log(f"XY 位移台已连接: {port}")
        except Exception as exc:
            if self.xy_stage is not None:
                try:
                    self.xy_stage.close()
                finally:
                    self.xy_stage = None
            self.xy_settings_dialog.set_executor(None)
            self._show_error("XY 位移台连接失败", exc)

    def _disconnect_xy_stage_from_dialog(self) -> None:
        try:
            if self.xy_settings_dialog.task_locked:
                raise RuntimeError("自动扫描或轨迹运行中，不能断开 XY 位移台")
            if self.xy_stage is not None:
                self.xy_stage.close()
                self.xy_stage = None
            self.xy_settings_dialog.set_executor(None)
            self.xy_status.setText("XY: 未连接")
            self._log("XY 位移台已断开")
        except Exception as exc:
            self._show_error("XY 位移台断开失败", exc)

    def _toggle_xy_stage(self) -> None:
        """兼容旧调用入口，实际连接控件由完整控制弹窗持有。"""
        if self.xy_stage is not None and self.xy_stage.connected:
            self._disconnect_xy_stage_from_dialog()
        else:
            self._connect_xy_stage_from_dialog(self.xy_settings_dialog.port_combo.currentText())

    def _enable_xy_stage(self) -> None:
        try:
            if self.xy_stage is None or not self.xy_stage.connected:
                raise RuntimeError("请先连接 XY 位移台")
            snapshot = self.xy_stage.set_enabled(True)
            self._update_xy_snapshot(snapshot)
            self._log("XY 双轴已使能")
        except Exception as exc:
            self._show_error("XY 位移台使能失败", exc)

    def _clear_xy_stage(self) -> None:
        try:
            if self.xy_stage is None or not self.xy_stage.connected:
                raise RuntimeError("请先连接 XY 位移台")
            snapshot = self.xy_stage.clear_errors()
            self._update_xy_snapshot(snapshot)
            self._log("XY 位移台报警已清除")
        except Exception as exc:
            self._show_error("XY 位移台清错失败", exc)

    def _update_xy_snapshot(self, snapshot: object) -> None:
        axes = getattr(snapshot, "axes", {})
        if 0 not in axes or 1 not in axes:
            return
        self.xy_settings_dialog.update_snapshot(snapshot)
        self._on_xy_dialog_snapshot(snapshot)

    def _on_xy_dialog_snapshot(self, snapshot: object) -> None:
        axes = getattr(snapshot, "axes", {})
        connected = bool(getattr(snapshot, "connected", False))
        if connected and 0 in axes and 1 in axes:
            self.xy_status.setText("XY: 已连接")
            x_text = f"{axes[0].dpos:.4f}".rstrip("0").rstrip(".")
            y_text = f"{axes[1].dpos:.4f}".rstrip("0").rstrip(".")
            self.xy_realtime_position.setText(f"X={x_text} mm  Y={y_text} mm")
            for axis, name in ((0, "X"), (1, "Y")):
                status = axes[axis]
                if bool(getattr(status, "hard_fault", False)) or bool(
                    getattr(status, "positive_any_limit", False)
                    or getattr(status, "negative_any_limit", False)
                ):
                    state, detail = "alarm", f"{name} 轴报警或限位"
                elif bool(getattr(status, "running", False)) or bool(
                    getattr(status, "homing", False)
                ):
                    state, detail = "active", f"{name} 轴运动中"
                elif bool(getattr(status, "enabled", False)):
                    state, detail = "ok", f"{name} 轴已连接并使能"
                else:
                    state, detail = "warning", f"{name} 轴已连接但未使能"
                self.xy_axis_lamps[axis].set_state(state, detail)
            controller_limits = (
                float(axes[0].soft_min_position), float(axes[0].soft_max_position),
                float(axes[1].soft_min_position), float(axes[1].soft_max_position),
            )
            if controller_limits != self._xy_controller_limits:
                self._xy_controller_limits = controller_limits
                for widget in (
                    self.xy_safe_x_min, self.xy_safe_x_max,
                    self.xy_safe_y_min, self.xy_safe_y_max,
                ):
                    widget.setSpecialValueText("")
                for widget in (self.xy_safe_x_min, self.xy_safe_x_max):
                    widget.setRange(controller_limits[0], controller_limits[1])
                for widget in (self.xy_safe_y_min, self.xy_safe_y_max):
                    widget.setRange(controller_limits[2], controller_limits[3])
                self.xy_safe_x_min.setValue(controller_limits[0])
                self.xy_safe_x_max.setValue(controller_limits[1])
                self.xy_safe_y_min.setValue(controller_limits[2])
                self.xy_safe_y_max.setValue(controller_limits[3])
                self._update_spatial_center_ranges()
        else:
            self.xy_status.setText("XY: 未连接")
            self.xy_realtime_position.setText("X=-- mm  Y=-- mm")
            for lamp in self.xy_axis_lamps.values():
                lamp.set_state("off", "位移台未连接")
            self._xy_controller_limits = None
            self._update_spatial_center_ranges()

    def _spatial_safety_changed(self) -> None:
        try:
            self._spatial_safety_limits()
            self._update_spatial_center_ranges()
        except ValueError:
            # 让用户完成另一端输入后再统一提示，不在编辑半途中弹窗。
            return

    def _update_spatial_center_ranges(self) -> None:
        """按控制器当前 RS_LIMIT/FS_LIMIT 更新概览 DPOS 输入范围。"""
        try:
            limits = self._spatial_safety_limits()
        except ValueError:
            return
        x_min, x_max = limits.x_min_mm, limits.x_max_mm
        y_min, y_max = limits.y_min_mm, limits.y_max_mm
        if x_min < x_max:
            for widget in (self.survey_x_start, self.survey_x_end):
                widget.setRange(x_min, x_max)
        if y_min < y_max:
            for widget in (self.survey_y_start, self.survey_y_end):
                widget.setRange(y_min, y_max)

    def _start_spatial_calibration(self) -> None:
        if self.xy_stage is None or not self.xy_stage.connected:
            self._show_error("空间标定失败", RuntimeError("请先连接并使能 XY 位移台"))
            return
        if not self.camera.initialized:
            self._show_error("空间标定失败", RuntimeError("请先连接相机"))
            return
        if self.spatial_worker is not None and self.spatial_worker.running:
            self._show_error("空间标定失败", RuntimeError("空间扫描正在运行"))
            return
        self.preview_timer.stop()
        self.btn_xy_calibrate.setEnabled(False)
        limits = self._spatial_safety_limits()
        settle_seconds = self.spatial_settle.value() / 1000.0
        fingerprint = self._spatial_calibration_fingerprint()
        threading.Thread(
            target=self._run_spatial_calibration,
            args=(limits, settle_seconds, fingerprint),
            name="spatial-calibration",
            daemon=True,
        ).start()

    def _run_spatial_calibration(
        self,
        limits: SafetyLimits,
        settle_seconds: float,
        fingerprint: dict[str, object],
    ) -> None:
        try:
            assert self.xy_stage is not None
            snapshot = self.xy_stage.refresh_status()
            x0, y0 = snapshot.axes[0].dpos, snapshot.axes[1].dpos
            step = 0.2
            dx = step if x0 + step <= limits.x_max_mm else -step
            dy = step if y0 + step <= limits.y_max_mm else -step
            if not limits.x_min_mm <= x0 + dx <= limits.x_max_mm:
                raise RuntimeError("当前位置附近没有足够的 X 标定空间")
            if not limits.y_min_mm <= y0 + dy <= limits.y_max_mm:
                raise RuntimeError("当前位置附近没有足够的 Y 标定空间")
            self.camera.apply_quantitative_profile()
            self.camera.set_trigger_mode(1)
            base = self.camera.soft_trigger_and_grab_sample(2000)
            if base is None:
                raise RuntimeError("获取标定基准图失败")
            self.xy_stage.move_absolute_blocking(x0 + dx, y0, timeout_seconds=30)
            time.sleep(settle_seconds)
            x_sample = self.camera.soft_trigger_and_grab_sample(2000)
            self.xy_stage.move_absolute_blocking(x0, y0, timeout_seconds=30)
            self.xy_stage.move_absolute_blocking(x0, y0 + dy, timeout_seconds=30)
            time.sleep(settle_seconds)
            y_sample = self.camera.soft_trigger_and_grab_sample(2000)
            self.xy_stage.move_absolute_blocking(x0, y0, timeout_seconds=30)
            if x_sample is None or y_sample is None:
                raise RuntimeError("标定图像不足")
            base_f = base.frame.astype(np.float32)
            x_f = x_sample.frame.astype(np.float32)
            y_f = y_sample.frame.astype(np.float32)
            (x_dx, x_dy), x_response = cv2.phaseCorrelate(base_f, x_f)
            (y_dx, y_dy), y_response = cv2.phaseCorrelate(base_f, y_f)
            if min(x_response, y_response) < 0.1:
                raise RuntimeError(
                    f"空间标定相关度过低: X={x_response:.3f}, Y={y_response:.3f}"
                )
            fit = fit_affine_calibration(
                [(x0, y0), (x0 + dx, y0), (x0, y0 + dy)],
                [(0.0, 0.0), (x_dx, x_dy), (y_dx, y_dy)],
                fingerprint=fingerprint,
            )
            self.bridge.spatial_calibrated.emit(fit, None)
        except Exception as exc:
            try:
                if self.xy_stage is not None and self.xy_stage.connected:
                    self.xy_stage.stop_all()
            except Exception:
                pass
            self.bridge.spatial_calibrated.emit(None, exc)
        finally:
            try:
                if self.camera.initialized:
                    self.camera.set_trigger_mode(0)
            except Exception:
                pass

    def _on_spatial_calibrated(self, fit: object, exc: object) -> None:
        self.btn_xy_calibrate.setEnabled(True)
        if exc is not None:
            self._show_error("空间标定失败", exc if isinstance(exc, Exception) else RuntimeError(str(exc)))
            return
        if fit is None:
            return
        self.spatial_calibration = fit.calibration
        self.spatial_pixel_um.setValue(fit.calibration.pixel_size_um)
        self._update_spatial_center_ranges()
        self._log(f"空间标定完成: 等效 {fit.calibration.pixel_size_um:.6f} µm/px, RMS={fit.rms_px:.3f} px")

    def _spatial_safety_limits(self) -> SafetyLimits:
        values = (
            self.xy_safe_x_min.value(), self.xy_safe_x_max.value(),
            self.xy_safe_y_min.value(), self.xy_safe_y_max.value(),
        )
        if values[0] >= values[1] or values[2] >= values[3]:
            raise ValueError("XY 应用安全范围无效")
        return SafetyLimits(*values)

    def _spatial_pixel_size_changed(self) -> None:
        if abs(self.spatial_calibration.pixel_size_um - self.spatial_pixel_um.value()) <= 1e-9:
            return
        self.spatial_calibration = default_calibration(self.spatial_pixel_um.value())
        self._survey_plan = None
        self._acquisition_plan = None
        self._spatial_roi = None
        self.spatial_roi_status.setText("空间 ROI: 像素间距已变化，请重新概览")
        self._update_spatial_center_ranges()
        self._log(f"空间像素间距已设为 {self.spatial_pixel_um.value():.6f} µm/px")

    def _spatial_calibration_fingerprint(self) -> dict[str, object]:
        return {
            "width": int(getattr(self.camera, "width", 0)),
            "height": int(getattr(self.camera, "height", 0)),
            "pixel_size_um_input": self.spatial_pixel_um.value(),
            "roi": self._stable_camera_signature().get("roi_width"),
        }

    def _spatial_rect_from_controls(self) -> SpatialRect:
        x0, x1 = self.survey_x_start.value(), self.survey_x_end.value()
        y0, y1 = self.survey_y_start.value(), self.survey_y_end.value()
        return SpatialRect(
            min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1),
        )

    def _plan_spatial_center_scan(self) -> TilePlan:
        if not self.camera.initialized:
            raise RuntimeError("请先连接相机")
        calibration = self.spatial_calibration
        if abs(calibration.pixel_size_um - self.spatial_pixel_um.value()) > 1e-9:
            calibration = default_calibration(
                self.spatial_pixel_um.value(), fingerprint=self._spatial_calibration_fingerprint()
            )
        route = "serpentine" if self.spatial_route.currentText() == "蛇形" else "unidirectional"
        return plan_center_scan(
            self.survey_x_start.value(), self.survey_x_end.value(),
            self.survey_y_start.value(), self.survey_y_end.value(),
            (int(self.camera.width), int(self.camera.height)), calibration,
            self.spatial_overlap.value() / 100.0,
            route=route,
            safety_limits=self._spatial_safety_limits(),
        )

    def _plan_spatial_rect(self, rect: SpatialRect) -> TilePlan:
        if not self.camera.initialized:
            raise RuntimeError("请先连接相机")
        calibration = self.spatial_calibration
        if abs(calibration.pixel_size_um - self.spatial_pixel_um.value()) > 1e-9:
            calibration = default_calibration(
                self.spatial_pixel_um.value(), fingerprint=self._spatial_calibration_fingerprint()
            )
        route = "serpentine" if self.spatial_route.currentText() == "蛇形" else "unidirectional"
        return plan_tiles(
            rect,
            (int(self.camera.width), int(self.camera.height)),
            calibration,
            self.spatial_overlap.value() / 100.0,
            route=route,
            safety_limits=self._spatial_safety_limits(),
        )

    def _prepare_spatial_map(self, plan: TilePlan) -> None:
        width_mm, height_mm = plan.roi.width_mm, plan.roi.height_mm
        scale = min(1600.0 / max(width_mm, 1e-9), 1000.0 / max(height_mm, 1e-9))
        width = max(320, min(1600, int(round(width_mm * scale))))
        height = max(240, min(1000, int(round(height_mm * scale))))
        self._spatial_map_shape = (height, width)
        self.spatial_map.set_map_size(width, height)
        self.spatial_map.set_map_physical_bounds(
            plan.roi.x_min_mm, plan.roi.y_min_mm, plan.roi.x_max_mm, plan.roi.y_max_mm
        )
        self._spatial_composer = MosaicComposer((height, width), mode="feather")
        route = [self._stage_point_to_map(plan, item.target.x_mm, item.target.y_mm) for item in plan.placements]
        self.spatial_map.set_route(route)
        tile_states: list[SpatialTile] = []
        for item in plan.placements:
            rect = self._stage_rect_to_map(plan, item.bounds)
            tile_states.append(SpatialTile(f"{item.row}:{item.column}", rect, "pending"))
        self.spatial_map.set_tile_states(tile_states)
        self.spatial_map.clear_map_image()
        self._spatial_tile_states = {tile.tile_id: tile for tile in tile_states}
        self._spatial_tile_origins.clear()
        self._last_spatial_tile = None
        self.viewer_tabs.setCurrentIndex(1)

    def _stage_point_to_map(self, plan: TilePlan, x_mm: float, y_mm: float) -> QPointF:
        height, width = self._spatial_map_shape
        return QPointF(
            (x_mm - plan.roi.x_min_mm) / max(plan.roi.width_mm, 1e-9) * width,
            (y_mm - plan.roi.y_min_mm) / max(plan.roi.height_mm, 1e-9) * height,
        )

    def _stage_rect_to_map(self, plan: TilePlan, rect: SpatialRect) -> QRect:
        height, width = self._spatial_map_shape
        x = round((rect.x_min_mm - plan.roi.x_min_mm) / max(plan.roi.width_mm, 1e-9) * width)
        y = round((rect.y_min_mm - plan.roi.y_min_mm) / max(plan.roi.height_mm, 1e-9) * height)
        w = max(1, round(rect.width_mm / max(plan.roi.width_mm, 1e-9) * width))
        h = max(1, round(rect.height_mm / max(plan.roi.height_mm, 1e-9) * height))
        return QRect(x, y, w, h)

    def _map_image_from_composer(self) -> None:
        if self._spatial_composer is None:
            return
        image = self._spatial_composer.image(np.dtype(np.uint8))
        self.spatial_map.set_map_image(frame_to_pixmap(image, image.shape[1], image.shape[0]))

    def _activate_spatial_selection(self) -> None:
        if self._survey_plan is None:
            self._show_error("框选空间 ROI", RuntimeError("请先完成一次概览扫描"))
            return
        self.viewer_tabs.setCurrentIndex(1)
        self.spatial_map.set_selection_enabled(True)
        self._log("请在样品地图上拖拽框选空间 ROI，按 Esc 或右键取消")

    def _spatial_roi_selected(self, x: float, y: float, width: float, height: float) -> None:
        try:
            rect = SpatialRect(x, y, x + width, y + height)
            self._spatial_roi = rect
            self.spatial_roi_status.setText(
                f"空间 ROI: X {rect.x_min_mm:.4f}–{rect.x_max_mm:.4f} mm, "
                f"Y {rect.y_min_mm:.4f}–{rect.y_max_mm:.4f} mm"
            )
            self._log("空间 ROI 已选择")
        except Exception as exc:
            self._show_error("空间 ROI 无效", exc)

    def _plan_selected_spatial_roi(self) -> None:
        try:
            if self._spatial_roi is None:
                raise RuntimeError("请先在样品地图上框选空间 ROI")
            self._acquisition_plan = self._plan_spatial_rect(self._spatial_roi)
            basis = self._survey_plan or self._acquisition_plan
            self.spatial_map.set_route(
                [self._stage_point_to_map(basis, item.target.x_mm, item.target.y_mm)
                 for item in self._acquisition_plan.placements]
            )
            states = [
                SpatialTile(
                    f"{item.row}:{item.column}",
                    self._stage_rect_to_map(basis, item.bounds),
                    "pending",
                )
                for item in self._acquisition_plan.placements
            ]
            self.spatial_map.set_tile_states(states)
            self._spatial_tile_states = {tile.tile_id: tile for tile in states}
            self._log(
                f"空间路径已规划: {self._acquisition_plan.rows}×{self._acquisition_plan.columns}，"
                f"共 {self._acquisition_plan.tile_count} 个 XY 瓦片"
            )
        except Exception as exc:
            self._show_error("空间路径规划失败", exc)

    def _new_spatial_worker(self) -> SpatialScanWorker:
        if self.xy_stage is None or not self.xy_stage.connected:
            raise RuntimeError("请先连接 XY 位移台")
        if self.xy_settings_dialog.task_locked:
            raise RuntimeError("XY 位移台已有二维扫描或轨迹任务在运行")
        return SpatialScanWorker(
            self.camera,
            self.scanner,
            self.xy_stage,
            lambda text, current, total, placement: self.bridge.spatial_progress.emit(
                text, current, total, placement
            ),
            lambda placement, sample, actual: self.bridge.spatial_tile.emit(
                placement, sample, actual
            ),
            self._log,
        )

    def _start_spatial_survey(self) -> None:
        try:
            if not self.camera.initialized:
                raise RuntimeError("请先连接相机")
            if self.xy_stage is None or not self.xy_stage.connected:
                raise RuntimeError("请先连接 XY 位移台")
            if abs(self.spatial_calibration.pixel_size_um - self.spatial_pixel_um.value()) > 1e-9:
                self.spatial_calibration = default_calibration(
                    self.spatial_pixel_um.value(), fingerprint=self._spatial_calibration_fingerprint()
                )
            plan = self._plan_spatial_center_scan()
            self._survey_plan = plan
            self._acquisition_plan = None
            self._spatial_roi = None
            self.spatial_roi_status.setText("空间 ROI: 未选择")
            self._prepare_spatial_map(plan)
            save_dir = Path(self.save_path.text())
            save_dir.mkdir(parents=True, exist_ok=True)
            if self.preview_timer.isActive():
                self._stop_preview()
            self.spatial_worker = self._new_spatial_worker()
            self._set_spatial_locked(True)
            self.spatial_worker.start_survey(
                SurveyConfig(
                    plan=plan,
                    save_dir=save_dir,
                    prefix=self.prefix.text() or "survey",
                    extension=self.image_format.currentText(),
                    bit_depth=8 if self.bit_depth.currentIndex() == 0 else 12,
                    settle_ms=self.spatial_settle.value(),
                    calibration=self.spatial_calibration,
                ),
                lambda result, exc: self.bridge.spatial_done.emit(result, exc),
            )
            self._log(f"XY 概览扫描开始: {plan.rows}×{plan.columns}，共 {plan.tile_count} 瓦片")
        except Exception as exc:
            self._set_spatial_locked(False)
            self._show_error("概览扫描启动失败", exc)

    def _start_spatial_acquisition(self) -> None:
        try:
            if self._acquisition_plan is None:
                self._plan_selected_spatial_roi()
            if self._acquisition_plan is None:
                return
            if not self.pzt.connected:
                raise RuntimeError("请先连接 PZT")
            save_dir = Path(self.save_path.text())
            save_dir.mkdir(parents=True, exist_ok=True)
            pzt_config = self._scan_config(save_dir)
            if self.preview_timer.isActive():
                self._stop_preview()
            self._pause_pzt_monitor_for_scan()
            self.spatial_worker = self._new_spatial_worker()
            self._set_spatial_locked(True)
            self.spatial_worker.start_acquisition(
                SpatialAcquisitionConfig(
                    plan=self._acquisition_plan,
                    pzt_config=pzt_config,
                    save_dir=save_dir,
                    settle_ms=self.spatial_settle.value(),
                ),
                lambda result, exc: self.bridge.spatial_done.emit(result, exc),
            )
            self._log(
                f"空间纵向扫描开始: {self._acquisition_plan.tile_count} 个 XY 瓦片，"
                "每个瓦片执行一次完整 PZT 扫描"
            )
        except Exception as exc:
            self._restore_pzt_monitor_after_scan()
            self._set_spatial_locked(False)
            self._show_error("空间扫描启动失败", exc)

    def _stop_spatial_scan(self) -> None:
        if self.spatial_worker is not None:
            self.spatial_worker.stop()
            self.spatial_worker.wait(timeout=5.0)
        self._log("正在停止 XY 与 PZT 空间扫描...")

    def _set_spatial_locked(self, locked: bool) -> None:
        self._set_scan_locked(locked)
        self.btn_start_survey.setEnabled(not locked)
        self.btn_select_spatial_roi.setEnabled(not locked)
        self.btn_plan_spatial.setEnabled(not locked)
        self.btn_start_spatial.setEnabled(not locked)
        self.btn_stop_spatial.setEnabled(locked)
        self.btn_xy_calibrate.setEnabled(not locked)

    def _on_spatial_progress(self, text: str, current: int, total: int, placement: object) -> None:
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(current)
        if placement is not None:
            plan = self._survey_plan if text == "概览扫描" else self._acquisition_plan
            basis = self._survey_plan or plan
            if basis is not None:
                self.spatial_map.set_current_scan_point(
                    self._stage_point_to_map(basis, placement.target.x_mm, placement.target.y_mm)
                )

    def _on_spatial_tile(self, placement: object, sample: object, actual: object) -> None:
        if self._survey_plan is None or self._spatial_composer is None:
            return
        frame = sample.frame
        rect = self._stage_rect_to_map(self._survey_plan, placement.bounds)
        clipped = rect.intersected(QRect(0, 0, self._spatial_map_shape[1], self._spatial_map_shape[0]))
        if clipped.isEmpty():
            return
        display = frame
        if display.dtype == np.uint16:
            display = ((np.clip(display, 0, 4095).astype(np.uint32) * 255) // 4095).astype(np.uint8)
        else:
            display = display.astype(np.uint8, copy=False)
        resized = cv2.resize(display, (max(1, rect.width()), max(1, rect.height())), interpolation=cv2.INTER_AREA)
        origin = (float(rect.x()), float(rect.y()))
        quality = 1.0
        previous = self._last_spatial_tile
        if previous is not None:
            previous_placement, previous_frame = previous
            row_delta = placement.row - previous_placement.row
            column_delta = placement.column - previous_placement.column
            direction = None
            if row_delta == 0 and column_delta == 1:
                direction = "right"
            elif row_delta == 0 and column_delta == -1:
                direction = "left"
            elif abs(row_delta) == 1 and column_delta == 0:
                direction = "down" if row_delta > 0 else "up"
            if direction is not None:
                registration = estimate_adjacent_translation(
                    previous_frame,
                    frame,
                    direction=direction,
                    overlap=self.spatial_overlap.value() / 100.0,
                )
                quality = max(0.05, registration.confidence) if registration.success else 0.25
                previous_origin = self._spatial_tile_origins.get(previous_placement.sequence)
                if registration.success and previous_origin is not None:
                    sx = rect.width() / max(frame.shape[1], 1)
                    sy = rect.height() / max(frame.shape[0], 1)
                    origin = (
                        previous_origin[0] + registration.dx_px * sx,
                        previous_origin[1] + registration.dy_px * sy,
                    )
        self._spatial_composer.add_tile(resized, origin, quality=quality)
        self._spatial_tile_origins[placement.sequence] = origin
        self._last_spatial_tile = (placement, frame.copy())
        tile_id = f"{placement.row}:{placement.column}"
        try:
            self.spatial_map.update_tile_state(tile_id, "complete")
        except KeyError:
            pass
        self._map_image_from_composer()

    def _on_spatial_done(self, result: object, exc: object) -> None:
        self._restore_pzt_monitor_after_scan()
        self._set_spatial_locked(False)
        self.spatial_map.set_current_scan_point(None)
        if exc is not None:
            self._show_error("空间扫描错误", exc if isinstance(exc, Exception) else RuntimeError(str(exc)))
            return
        scan_result = result if isinstance(result, SpatialScanResult) else None
        if scan_result is None:
            return
        if scan_result.survey and self._spatial_composer is not None:
            try:
                SpatialJobStorage(scan_result.folder).save_preview(
                    self._spatial_composer.image(np.dtype(np.uint8))
                )
            except Exception as preview_error:
                self._log(f"空间概览图保存失败: {preview_error}")
        state = "已停止" if scan_result.stopped else "完成"
        self._log(
            f"{'概览' if scan_result.survey else '空间纵向'}扫描{state}: "
            f"{scan_result.completed_tiles}/{scan_result.total_tiles} 瓦片，目录 {scan_result.folder}"
        )


    def _toggle_pzt(self) -> None:
        try:
            if self.pzt.connected:
                self.monitor_timer.stop()
                self.pzt.close()
                self.btn_pzt_connect.setText("连接")
                self.pzt_status.setText("PZT: 未连接")
                self.btn_pause_monitor.setEnabled(False)
                return
            if self.conn_type.currentText() == "串口":
                port = self.port_combo.currentText()
                if port == "无可用串口":
                    raise RuntimeError("无可用串口")
                self.pzt.connect_serial(port, int(self.baud_combo.currentText()))
            else:
                self.pzt.connect_udp(self.port_combo.currentText())
            self.btn_pzt_connect.setText("断开")
            self.pzt_status.setText("PZT: 已连接")
            self.btn_pause_monitor.setEnabled(True)
            self.monitor_timer.start()
            self._log("PZT 已连接并下发闭环模式")
        except Exception as exc:
            self._show_error("PZT 连接失败", exc)

    def _monitor_pzt(self) -> None:
        if self.btn_pause_monitor.isChecked() or not self.pzt.connected:
            return
        value = self.pzt.read_move(self._channel())
        if value is not None:
            self.actual_um.setText(f"{value:.4f}")

    def _send_manual_move(self, value: float) -> None:
        try:
            self.pzt.send_move(self._channel(), value)
            self.target_um.setValue(value)
            self._log(f"PZT 设置位移: 通道{self._channel()} -> {value:.4f} um")
        except Exception as exc:
            self._show_error("PZT 设置失败", exc)

    def _start_scan(self) -> None:
        try:
            if not self.camera.initialized:
                raise RuntimeError("请先连接相机")
            if not self.pzt.connected:
                raise RuntimeError("请先连接 PZT")
            if self.xy_settings_dialog.task_locked:
                raise RuntimeError("XY 位移台已有自动任务在运行")
            save_dir = Path(self.save_path.text())
            save_dir.mkdir(parents=True, exist_ok=True)
            if self.trigger_mode.currentText() == "软触发" and self.preview_timer.isActive():
                self._stop_preview()
            config = self._scan_config(save_dir)
            self._pause_pzt_monitor_for_scan()
            self._set_scan_locked(True)
            self.btn_start_scan.setEnabled(False)
            self.btn_stop_scan.setEnabled(True)
            self.progress.setValue(0)
            try:
                self.scanner.start(config, lambda result, exc: self.bridge.done.emit(result, exc))
            except Exception:
                self._restore_pzt_monitor_after_scan()
                self._set_scan_locked(False)
                raise
        except Exception as exc:
            self._show_error("扫描启动失败", exc)

    def _scan_config(self, save_dir: Path) -> ScanConfig:
        if self.scan_tabs.currentIndex() == 0:
            mode = "normal"
            start = self.normal_start.value()
            end = self.normal_end.value()
            step = self.normal_step.value()
            stable = self.normal_stable.value()
            repeats = self.normal_repeats.value()
        else:
            mode = "center"
            center = self.center_pos.value()
            radius = self.center_range.value() / 2
            start = center - radius
            end = center + radius
            step = self.center_step.value()
            stable = self.center_stable.value()
            repeats = self.center_repeats.value()
        return ScanConfig(
            mode=mode,
            channel=self._channel(),
            start_um=start,
            end_um=end,
            step_um=step,
            stable_ms=stable,
            repeats=repeats,
            trigger_mode="soft" if self.trigger_mode.currentText() == "软触发" else "continuous",
            save_dir=save_dir,
            prefix=self.prefix.text() or "img",
            extension=self.image_format.currentText(),
            bit_depth=8 if self.bit_depth.currentIndex() == 0 else 12,
            apply_quantitative_profile=True,
            flat_field_calibration=self._scan_calibration(),
            save_raw_when_correcting=True,
            saver_queue_size=256,
        )

    def _scan_calibration(self) -> FlatFieldCalibration | None:
        if not self.enable_flat_field.isChecked():
            return None
        if self._dark_average is None or self._flat_average is None or self._calibration_signature is None:
            raise RuntimeError("已启用暗场/平场校正，但暗场或平场尚未采集")
        self._assert_signature_matches(self._calibration_signature, self._stable_camera_signature())
        return FlatFieldCalibration(
            dark=self._dark_average.copy(),
            flat=self._flat_average.copy(),
            signature=dict(self._calibration_signature),
        )

    def _stop_scan(self) -> None:
        self.scanner.stop()
        self._log("正在停止扫描...")

    def _emit_progress(self, text: str, current: int, total: int, target: float, actual: float | None) -> None:
        self.bridge.progress.emit(text, current, total, target, actual)

    def _on_scan_progress(self, text: str, current: int, total: int, target: float, actual: object) -> None:
        self.progress.setMaximum(total)
        self.progress.setValue(current)

    def _on_scan_done(self, result: object, exc: object) -> None:
        self._restore_pzt_monitor_after_scan()
        self._set_scan_locked(False)
        self.btn_start_scan.setEnabled(True)
        self.btn_stop_scan.setEnabled(False)
        if exc is not None:
            self._show_error("扫描错误", exc if isinstance(exc, Exception) else RuntimeError(str(exc)))
            return
        scan_result = result if isinstance(result, ScanResult) else None
        if scan_result is None:
            return
        state = "已停止" if scan_result.stopped else "全部完成"
        self._log(
            f"扫描{state}: 采集 {scan_result.completed_images}，"
            f"保存 {scan_result.saved_images}，目录 {scan_result.folder}"
        )

    def _pause_pzt_monitor_for_scan(self) -> None:
        self._monitor_restart_after_scan = self.monitor_timer.isActive()
        self._monitor_pause_checked_before_scan = self.btn_pause_monitor.isChecked()
        self.monitor_timer.stop()
        self.btn_pause_monitor.setEnabled(False)
        if self.pzt.connected:
            self.pzt_status.setText("PZT: 扫描占用")

    def _restore_pzt_monitor_after_scan(self) -> None:
        self.btn_pause_monitor.setChecked(self._monitor_pause_checked_before_scan)
        self.btn_pause_monitor.setEnabled(self.pzt.connected)
        if self.pzt.connected:
            if self._monitor_restart_after_scan and not self._monitor_pause_checked_before_scan:
                self.monitor_timer.start()
            self.pzt_status.setText("PZT: 已连接")
        else:
            self.pzt_status.setText("PZT: 未连接")
        self._monitor_restart_after_scan = False

    def _set_scan_locked(self, locked: bool) -> None:
        enabled = not locked
        self.xy_settings_dialog.set_scan_active(locked)
        for widget in (
            self.btn_camera_open,
            self.btn_preview,
            self.btn_snapshot,
            self.btn_roi_select,
            self.frame_speed,
            self.auto_exposure,
            self.exposure_us,
            self.gain_x,
            self.roi_enabled,
            self.roi_x,
            self.roi_y,
            self.roi_w,
            self.roi_h,
            self.btn_apply_roi,
            self.btn_restore_roi,
            self.btn_reset_roi,
            self.bit_depth,
            self.trigger_mode,
            self.enable_flat_field,
            self.calibration_frames,
            self.btn_capture_dark,
            self.btn_capture_flat,
            self.btn_pzt_connect,
            self.btn_refresh_ports,
            self.channel_combo,
            self.target_um,
            self.btn_set_pos,
            self.btn_zero,
            self.anti_flick,
            self.light_frequency,
            self.btn_once_wb,
            self.h_mirror,
            self.v_mirror,
            self.rotate,
            self.contrast_slider,
            self.gamma_slider,
            self.saturation_slider,
            self.sharpness_slider,
            self.btn_start_survey,
            self.btn_select_spatial_roi,
            self.btn_plan_spatial,
            self.btn_start_spatial,
            self.survey_x_start,
            self.survey_x_end,
            self.survey_y_start,
            self.survey_y_end,
            self.spatial_pixel_um,
            self.spatial_overlap,
            self.spatial_settle,
            self.spatial_route,
            self.btn_xy_calibrate,
            self.xy_safe_x_min,
            self.xy_safe_x_max,
            self.xy_safe_y_min,
            self.xy_safe_y_max,
        ):
            widget.setEnabled(enabled)
        if not locked:
            self.btn_stop_preview.setEnabled(self.preview_timer.isActive())
            self.btn_preview.setEnabled(not self.preview_timer.isActive())
            self.btn_pause_monitor.setEnabled(self.pzt.connected)
            self.light_frequency.setEnabled(self.anti_flick.isChecked())
            self._sync_exposure_controls()

    def _browse_save_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择保存路径", self.save_path.text())
        if path:
            self.save_path.setText(path)

    def _bit_depth_changed(self, index: int) -> None:
        current = self.image_format.currentText()
        formats = ["bmp", "png", "jpg", "tiff"] if index == 0 else ["tiff", "png"]
        default = current if current in formats else formats[0]
        self.image_format.blockSignals(True)
        self.image_format.clear()
        self.image_format.addItems(formats)
        self.image_format.setCurrentText(default)
        self.image_format.blockSignals(False)
        if self.camera.initialized:
            self._apply_current_bit_depth()
            self._invalidate_calibration("位深已变化")

    def _apply_current_bit_depth(self) -> None:
        if not self.camera.initialized:
            return
        if self.bit_depth.currentIndex() == 0:
            self.camera.set_output_format_8bit()
        else:
            self.camera.set_output_format_12bit_packed()

    def _channel(self) -> int:
        return int(self.channel_combo.currentText()[-1])

    def _safe_camera_call(self, func) -> None:
        if not self.camera.initialized:
            return
        try:
            func()
        except Exception as exc:
            self._show_error("相机设置失败", exc)

    def _camera_geometry_changed(self, func) -> None:
        self._safe_camera_call(func)
        self._invalidate_calibration("图像方向已变化")

    def _log(self, text: str) -> None:
        self.bridge.log.emit(text)

    def _append_log(self, text: str) -> None:
        self.log.appendPlainText(text)

    def _show_error(self, title: str, exc: Exception) -> None:
        self._append_log(f"{title}: {exc}")
        QMessageBox.critical(self, title, str(exc))

    def closeEvent(self, event) -> None:
        if self.spatial_worker is not None:
            self.spatial_worker.stop()
        self.scanner.stop()
        self.preview_timer.stop()
        self.monitor_timer.stop()
        self.status_timer.stop()
        self.xy_status_timer.stop()
        self.pzt.close()
        self.xy_settings_dialog.close()
        self.xy_overview_dialog.close()
        if self.xy_stage is not None:
            self.xy_stage.close()
            self.xy_stage = None
        self.xy_settings_dialog.set_executor(None)
        self.camera.close()
        event.accept()
