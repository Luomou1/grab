from __future__ import annotations

import ctypes
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QPoint, QRect, QSize, QObject, QTimer, Qt, Signal
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
    QProgressDialog,
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
)
from grab_app import __version__
from grab_app.image_io import next_numbered_path
from grab_app.pzt import PZTController
from grab_app.services import FlatFieldCalibration, ScanConfig, ScanResult, ScanWorker
from grab_app.update import UpdateInfo, check_latest_release, download_installer, start_installer


class NoWheelComboBox(QComboBox):
    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()


class NoWheelSpinBox(QSpinBox):
    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()


class NoWheelDoubleSpinBox(QDoubleSpinBox):
    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()


class DeviceStatusLabel(QLabel):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.setObjectName("deviceStatus")
        self.setText(text)

    def setText(self, text: str) -> None:
        online = "未连接" not in text and "失败" not in text
        display_text = text.replace("相机:", "相机").replace("PZT:", "PZT")
        dot_color = "#55c982" if online else "#e66767"
        super().setText(
            f'<span style="color:{dot_color};">●</span>'
            f'&nbsp;&nbsp;<span style="color:#d8d8dc;">{display_text}</span>'
        )


def enable_dark_title_bar(widget: QWidget) -> None:
    if sys.platform != "win32":
        return
    try:
        hwnd = int(widget.winId())
        enabled = ctypes.c_int(1)
        for attribute in (20, 19):
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd,
                attribute,
                ctypes.byref(enabled),
                ctypes.sizeof(enabled),
            )
            if result == 0:
                break
    except Exception:
        pass


def tool_icon(kind: str) -> QIcon:
    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(QColor("#e6e6e8"), 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))

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

    painter.end()
    return QIcon(pixmap)


class UiBridge(QObject):
    progress = Signal(str, int, int, float, object)
    done = Signal(object, object)
    log = Signal(str)
    update_checked = Signal(object, object)
    update_download_progress = Signal(int, int)
    update_downloaded = Signal(object, object)


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
        self.resize(1500, 920)
        self.setMinimumSize(1080, 680)

        self.camera = CameraController()
        self.pzt = PZTController()
        self.bridge = UiBridge()
        self.scanner = ScanWorker(self.camera, self.pzt, self._emit_progress, self._log)
        self.preview_timer = QTimer(self)
        self.preview_timer.setInterval(17)
        self.preview_timer.timeout.connect(self._update_preview)
        self.status_timer = QTimer(self)
        self.status_timer.setInterval(500)
        self.status_timer.timeout.connect(self._update_camera_status_readbacks)
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
        self._update_progress_dialog: QProgressDialog | None = None

        self.bridge.progress.connect(self._on_scan_progress)
        self.bridge.done.connect(self._on_scan_done)
        self.bridge.log.connect(self._append_log)
        self.bridge.update_checked.connect(self._on_update_checked)
        self.bridge.update_download_progress.connect(self._on_update_download_progress)
        self.bridge.update_downloaded.connect(self._on_update_downloaded)

        self._build_ui()
        self._apply_style()
        self._refresh_ports()
        self._sync_exposure_controls()
        self.status_timer.start()
        enable_dark_title_bar(self)

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
        self.pzt_settings_dialog = self._settings_dialog("PZT 位移", self._pzt_box())
        self.log_dialog = self._settings_dialog("运行日志", self._log_panel())
        self.log_dialog.setMinimumSize(720, 420)

        self.camera_status = DeviceStatusLabel("相机: 未连接")
        self.pzt_status = DeviceStatusLabel("PZT: 未连接")

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
        top_bar_layout.addWidget(self._settings_button("运行日志", self.log_dialog, "log"))
        self.btn_check_update = self._action_button("检查更新", "update", self._check_updates)
        top_bar_layout.addWidget(self.btn_check_update)
        top_bar_layout.addSpacing(10)
        top_bar_layout.addWidget(self.camera_status)
        top_bar_layout.addWidget(self.pzt_status)
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

        viewer_layout.addWidget(self.preview, 1)

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
        scroll.setFixedWidth(370)

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
        button.setIcon(tool_icon(icon))
        button.setIconSize(QSize(20, 20))
        button.setToolButtonStyle(Qt.ToolButtonIconOnly)
        button.clicked.connect(lambda: self._show_settings_dialog(dialog))
        return button

    def _action_button(self, tooltip: str, icon: str, callback) -> QToolButton:
        button = QToolButton()
        button.setObjectName("ribbonToolButton")
        button.setToolTip(tooltip)
        button.setIcon(tool_icon(icon))
        button.setIconSize(QSize(20, 20))
        button.setToolButtonStyle(Qt.ToolButtonIconOnly)
        button.clicked.connect(lambda _checked=False: callback())
        return button

    def _show_settings_dialog(self, dialog: QDialog) -> None:
        dialog.show()
        enable_dark_title_bar(dialog)
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
        self.h_mirror.toggled.connect(lambda v: self._safe_camera_call(lambda: self.camera.set_mirror(0, v)))
        self.v_mirror.toggled.connect(lambda v: self._safe_camera_call(lambda: self.camera.set_mirror(1, v)))
        self.rotate.currentIndexChanged.connect(lambda idx: self._safe_camera_call(lambda: self.camera.set_rotate(idx)))
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
        self.setStyleSheet(
            """
            QMainWindow { background: #1c1d22; }
            QWidget {
                color: #e5e7ea;
                font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
                font-size: 12px;
                letter-spacing: 0;
            }
            QFrame#workSurface { background: #1c1d22; }
            QFrame#topBar {
                background: #2b2930;
                border-bottom: 1px solid #4d4953;
            }
            QToolButton#ribbonToolButton {
                min-width: 44px;
                max-width: 44px;
                min-height: 38px;
                max-height: 38px;
                padding: 2px 5px;
                color: #e6e6e8;
                background: transparent;
                border: 0;
                border-right: 1px solid #45424a;
            }
            QToolButton#ribbonToolButton:hover {
                color: #ffffff;
                background: #39363f;
            }
            QToolButton#ribbonToolButton:pressed {
                background: #222127;
            }
            QToolButton#ribbonToolButton:checked {
                background: #174d50;
                border-bottom: 2px solid #16d6d0;
            }
            QLabel#deviceStatus {
                min-width: 88px;
                padding: 3px 5px;
                color: #d8d8dc;
                background: transparent;
                border: 0;
                font-size: 12px;
                font-weight: 400;
            }
            QFrame#viewerShell {
                background: #232329;
                border: 0;
            }
            QLabel#preview {
                background: #0f1115;
                color: #8a919b;
                border: 1px solid #55535b;
                border-radius: 2px;
                font-size: 16px;
                font-weight: 400;
            }
            QLabel#roiSnapshot {
                background: #0f1115;
                color: #8a919b;
                border: 1px solid #55535b;
                border-radius: 2px;
                font-size: 13px;
                font-weight: 400;
            }
            QLabel#statusPill {
                background: #2d2d34;
                color: #d6dbe1;
                border: 1px solid #494850;
                border-left: 3px solid #16d6d0;
                border-radius: 2px;
                padding: 5px 8px;
                font-size: 12px;
            }
            QWidget#sidePanel, QScrollArea#sideScroll {
                background: #35323a;
                border-left: 1px solid #4b4751;
            }
            QGroupBox {
                background: transparent;
                border: 0;
                border-top: 1px solid #4d4953;
                margin-top: 20px;
                padding: 12px 0 0 0;
                font-weight: 500;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 0;
                padding: 0 8px 0 0;
                color: #efeff1;
                background: #35323a;
            }
            QFrame#collapsible {
                background: #34313a;
                border: 1px solid #57515e;
                border-radius: 2px;
            }
            QFrame#collapsibleContent {
                background: #34313a;
                border-top: 1px solid #514c58;
            }
            QToolButton#disclosure {
                border: 0;
                border-radius: 2px;
                padding: 10px 11px;
                background: #34313a;
                color: #f3f4f5;
                font-weight: 700;
                text-align: left;
            }
            QToolButton#disclosure:hover { background: #3b3741; }
            QLabel#fieldLabel {
                color: #c4c3c8;
                font-size: 12px;
                font-weight: 400;
            }
            QPushButton {
                border-radius: 2px;
                padding: 5px 9px;
                min-height: 20px;
                font-weight: 400;
            }
            QPushButton#primaryButton {
                background: #0ea5a8;
                color: #061214;
                border: 1px solid #20d4d1;
            }
            QPushButton#primaryButton:hover { background: #18c3c2; }
            QPushButton#primaryButton:pressed { background: #0b8b90; }
            QPushButton#secondaryButton {
                background: #3b3841;
                color: #f2f4f5;
                border: 1px solid #69626f;
            }
            QPushButton#secondaryButton:hover {
                background: #46424c;
                border-color: #8a8390;
            }
            QPushButton#secondaryButton:pressed { background: #2b2931; }
            QPushButton#dangerButton {
                background: #3d3338;
                color: #ffd9d9;
                border: 1px solid #8d4c51;
            }
            QPushButton#dangerButton:hover {
                background: #5b343a;
                border-color: #d86466;
            }
            QPushButton#dangerButton:pressed { background: #742f36; }
            QPushButton#primaryButton:disabled,
            QPushButton#secondaryButton:disabled,
            QPushButton#dangerButton:disabled,
            QPushButton:disabled {
                background: #2d2b31;
                border: 1px solid #44404a;
                color: #777982;
            }
            QCheckBox {
                spacing: 7px;
                color: #e0e2e6;
                font-weight: 400;
            }
            QCheckBox::indicator {
                width: 14px;
                height: 14px;
                border: 1px solid #77717d;
                background: #26242a;
                border-radius: 2px;
            }
            QCheckBox::indicator:checked {
                background: #16d6d0;
                border-color: #63fff7;
            }
            QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox, QPlainTextEdit {
                background: #24232a;
                color: #f2f4f5;
                border: 1px solid #69636f;
                border-radius: 2px;
                padding: 6px;
                selection-background-color: #0ea5a8;
                selection-color: #061214;
            }
            QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus, QSpinBox:focus {
                border-color: #18d4ce;
                background: #202026;
            }
            QComboBox::drop-down {
                border: 0;
                width: 22px;
                background: #34313a;
            }
            QComboBox QAbstractItemView {
                background: #2f2d35;
                color: #eef1f3;
                border: 1px solid #625b68;
                selection-background-color: #0ea5a8;
                selection-color: #081316;
            }
            QLineEdit:read-only {
                color: #16d6d0;
                background: #202026;
            }
            QTabWidget::pane {
                border: 0;
                border-top: 1px solid #4f4b55;
                background: transparent;
                top: 0;
            }
            QTabBar::tab {
                background: transparent;
                color: #b8bdc4;
                padding: 7px 14px;
                border: 0;
                border-bottom: 2px solid transparent;
                font-weight: 400;
            }
            QTabBar::tab:selected {
                color: #ffffff;
                border-bottom: 2px solid #16d6d0;
            }
            QSlider::groove:horizontal {
                height: 4px;
                background: #232229;
                border: 1px solid #55505b;
                border-radius: 2px;
            }
            QSlider::sub-page:horizontal {
                background: #16d6d0;
                border-radius: 2px;
            }
            QSlider::handle:horizontal {
                width: 14px;
                height: 14px;
                margin: -6px 0;
                border-radius: 7px;
                background: #e8fbfb;
                border: 1px solid #16d6d0;
            }
            QLabel#meterValue {
                color: #16d6d0;
                min-width: 34px;
            }
            QProgressBar {
                background: #24232a;
                color: #e9eeee;
                border: 1px solid #69636f;
                border-radius: 2px;
                text-align: center;
                min-height: 20px;
                font-weight: 400;
            }
            QProgressBar::chunk {
                background: #16d6d0;
                border-radius: 1px;
            }
            QProgressBar#scanProgress {
                min-height: 6px;
                max-height: 6px;
                border: 0;
                border-radius: 0;
                background: #34313a;
            }
            QProgressBar#scanProgress::chunk {
                background: #16d6d0;
                border-radius: 0;
            }
            QPlainTextEdit {
                font-size: 12px;
                line-height: 1.35;
            }
            QScrollBar:vertical {
                background: #292730;
                width: 10px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #5b5561;
                border-radius: 2px;
                min-height: 28px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QDialog#settingsDialog {
                background: #302d35;
            }
            QDialog#settingsDialog QFrame#collapsibleContent {
                border: 1px solid #57515e;
                border-radius: 2px;
                background: #34313a;
            }
            QMessageBox {
                background: #f6f7f9;
            }
            QMessageBox QLabel {
                color: #1f2933;
                font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
                font-size: 13px;
                font-weight: 500;
            }
            QMessageBox QPushButton {
                min-width: 78px;
                min-height: 28px;
                padding: 5px 14px;
                color: #1f2933;
                background: #ffffff;
                border: 1px solid #c8ced8;
                border-radius: 3px;
                font-weight: 600;
            }
            QMessageBox QPushButton:hover {
                background: #edf2f6;
                border-color: #9aa6b2;
            }
            """
        )

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
        self._update_progress_dialog = QProgressDialog("正在下载安装包...", "取消", 0, 100, self)
        self._update_progress_dialog.setWindowTitle("在线更新")
        self._update_progress_dialog.setWindowModality(Qt.WindowModal)
        self._update_progress_dialog.setCancelButton(None)
        self._update_progress_dialog.setMinimumDuration(0)
        self._update_progress_dialog.setValue(0)
        self._update_progress_dialog.show()

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
        if total > 0:
            dialog.setMaximum(total)
            dialog.setValue(min(received, total))
            dialog.setLabelText(f"正在下载安装包... {received / 1024 / 1024:.1f} / {total / 1024 / 1024:.1f} MB")
        else:
            dialog.setMaximum(0)
            dialog.setLabelText(f"正在下载安装包... {received / 1024 / 1024:.1f} MB")

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
            self._log("相机初始化完成")
        except Exception as exc:
            self._show_error("相机连接失败", exc)

    def _start_preview(self) -> None:
        if not self.camera.initialized:
            self._open_camera()
            if not self.camera.initialized:
                return
        self.camera.reset_capture_count()
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
            f"显示: {display_fps:.1f} FPS | 帧: {health.capture_count} | 丢帧: {lost_text} | 超时: {health.timeout_count} | 帧龄: {age_text}"
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
        if self._dark_average is None and self._flat_average is None:
            return
        self._dark_average = None
        self._flat_average = None
        self._calibration_signature = None
        self._update_calibration_status()
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

    def _log(self, text: str) -> None:
        self.bridge.log.emit(text)

    def _append_log(self, text: str) -> None:
        self.log.appendPlainText(text)

    def _show_error(self, title: str, exc: Exception) -> None:
        self._append_log(f"{title}: {exc}")
        QMessageBox.critical(self, title, str(exc))

    def closeEvent(self, event) -> None:
        self.scanner.stop()
        self.preview_timer.stop()
        self.monitor_timer.stop()
        self.status_timer.stop()
        self.pzt.close()
        self.camera.close()
        event.accept()
