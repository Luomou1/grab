from __future__ import annotations

import re
import threading
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QEvent, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QProgressBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from grab_app.xy_stage import DeviceProfile, DeviceSnapshot, XYStageExecutor, load_default_profile
from grab_app.xy_stage.models import AxisStatus

from .status_lamp import StatusLamp
from .xy_advanced_panel import XYAdvancedPanel
from .xy_axis_panel import XYAxisPanel


class CompactDoubleSpinBox(QDoubleSpinBox):
    """按配置精度保存数值，显示时去除无意义的末尾 0。"""

    def textFromValue(self, value: float) -> str:  # noqa: N802 - Qt API
        text = f"{float(value):.{self.decimals()}f}"
        return text.rstrip("0").rstrip(".") if "." in text else text


class XYStageControlDialog(QDialog):
    """XY 位移台手动控制弹窗。

    ``XYStageExecutor`` 由主程序持有并通过 :meth:`set_executor` 共享；
    本对话框不创建、不 ``close`` executor，因而不会引入第二个
    SDK 线程或与空间扫描争用串口。
    """

    connect_requested = Signal(str)
    disconnect_requested = Signal()
    executor_changed = Signal(object)
    snapshot_updated = Signal(object)
    error_occurred = Signal(str)
    _trajectory_finished = Signal(object)
    _grid_scan_progress = Signal(int, int, str)
    _grid_scan_finished = Signal(object)

    def __init__(
        self,
        executor: XYStageExecutor | None = None,
        profile: DeviceProfile | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.profile = profile or load_default_profile()
        self._executor: XYStageExecutor | None = None
        self.snapshot = self._empty_snapshot()
        self._scan_locked = False
        self._grid_scan_active = False
        self._grid_scan_cancel: threading.Event | None = None
        self._trajectory_active = False
        self._trajectory_cancel: threading.Event | None = None
        self._trigger_active = False

        self.setWindowTitle("XY 位移台控制")
        self.setObjectName("xyStageDialog")
        self.resize(920, 620)
        self.setModal(False)
        self._build_ui()
        self._connect_signals()
        self._apply_theme()
        self.refresh_ports()
        self.set_executor(executor)

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(self.profile.poll_interval_ms)
        self.poll_timer.timeout.connect(self.refresh_snapshot)
        self.poll_timer.start()
        self.tabs.currentChanged.connect(
            lambda _index: QTimer.singleShot(0, self._fit_tab_to_current_page)
        )
        QTimer.singleShot(0, self._fit_tab_to_current_page)

    def _fit_tab_to_current_page(self) -> None:
        """按当前页内容收缩页签区域，避免被高级页高度撑出空白。"""
        page = self.tabs.currentWidget()
        if page is None:
            return
        page_height = max(page.sizeHint().height(), page.minimumSizeHint().height())
        tab_bar_height = self.tabs.tabBar().sizeHint().height()
        self.tabs.setFixedHeight(max(70, page_height + tab_bar_height + 6))
        self.adjustSize()

    @property
    def executor(self) -> XYStageExecutor | None:
        return self._executor

    @property
    def task_locked(self) -> bool:
        return self._scan_locked or self._grid_scan_active or self._trajectory_active

    def _empty_snapshot(self) -> DeviceSnapshot:
        return DeviceSnapshot(
            axes={
                item.axis: AxisStatus(
                    axis=item.axis,
                    soft_min_position=item.min_position,
                    soft_max_position=item.max_position,
                )
                for item in self.profile.axes
            }
        )

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        connection_group = QGroupBox("FMC01-02H 控制器")
        connection_layout = QHBoxLayout(connection_group)
        connection_layout.addWidget(QLabel("串口"))
        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self.port_combo.setMinimumWidth(170)
        connection_layout.addWidget(self.port_combo)
        connection_layout.addWidget(QLabel("波特率 38400"))
        self.refresh_button = QPushButton("刷新串口")
        self.connect_button = QPushButton("连接")
        self.disconnect_button = QPushButton("断开")
        self.clear_fault_button = QPushButton("清除报警")
        self.stop_all_button = QPushButton("全轴停止")
        self.refresh_button.setObjectName("secondaryButton")
        self.connect_button.setObjectName("primaryButton")
        self.disconnect_button.setObjectName("secondaryButton")
        self.clear_fault_button.setObjectName("secondaryButton")
        self.stop_all_button.setObjectName("dangerButton")
        connection_layout.addWidget(self.refresh_button)
        connection_layout.addWidget(self.connect_button)
        connection_layout.addWidget(self.disconnect_button)
        connection_layout.addWidget(self.clear_fault_button)
        self.global_lamps = {
            "worker": StatusLamp("控制线程"),
            "controller": StatusLamp("控制器"),
            "communication": StatusLamp("通信"),
            "parameters": StatusLamp("参数"),
            "task": StatusLamp("自动任务"),
            "buffer": StatusLamp("轨迹缓冲"),
            "trigger": StatusLamp("位置触发"),
            "fault": StatusLamp("故障"),
        }
        for lamp in self.global_lamps.values():
            connection_layout.addWidget(lamp)
        connection_layout.addStretch()
        connection_layout.addWidget(self.stop_all_button)
        root.addWidget(connection_group)

        axes_layout = QHBoxLayout()
        self.axis_panels: dict[int, XYAxisPanel] = {}
        for axis_profile in self.profile.axes:
            panel = XYAxisPanel(axis_profile)
            self.axis_panels[axis_profile.axis] = panel
            axes_layout.addWidget(panel)
        root.addLayout(axes_layout)

        self.advanced_panel = XYAdvancedPanel()
        self.tabs = QTabWidget()
        self.tabs.setObjectName("xyStageTabs")
        manual_page = QWidget()
        manual_page.setObjectName("xyStageManualPage")
        manual_hint_layout = QVBoxLayout(manual_page)
        manual_hint_layout.setContentsMargins(8, 8, 8, 8)
        self.message_label = QLabel("就绪")
        self.message_label.setWordWrap(True)
        manual_hint_layout.addWidget(QLabel(
            "手动操作位于上方两个轴面板；按住“负向点动”或“正向点动”，松开即停止。"
        ))
        manual_hint_layout.addWidget(self.message_label)
        manual_hint_layout.addStretch()
        self.tabs.addTab(manual_page, "手动控制")
        self.tabs.addTab(self._build_grid_scan_page(), "二维扫描")
        self.tabs.addTab(self.advanced_panel, "高级功能")
        root.addWidget(self.tabs)

    @staticmethod
    def _number_box(minimum: float, maximum: float, value: float) -> QDoubleSpinBox:
        box = CompactDoubleSpinBox()
        box.setDecimals(4)
        box.setRange(minimum, maximum)
        box.setValue(value)
        box.setKeyboardTracking(False)
        box.setSingleStep(0.1)
        return box

    def _build_grid_scan_page(self) -> QWidget:
        """构建与独立位移台程序一致的二维蛇形扫描页。"""
        page = QWidget()
        page.setObjectName("xyStageGridPage")
        layout = QGridLayout(page)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(8)
        x_profile, y_profile = self.profile.axis(0), self.profile.axis(1)
        self.scan_x_start = self._number_box(x_profile.min_position, x_profile.max_position, 0.0)
        self.scan_x_end = self._number_box(x_profile.min_position, x_profile.max_position, 5.0)
        self.scan_x_step = self._number_box(0.001, 20.0, 0.5)
        self.scan_y_start = self._number_box(y_profile.min_position, y_profile.max_position, 0.0)
        self.scan_y_end = self._number_box(y_profile.min_position, y_profile.max_position, 5.0)
        self.scan_y_step = self._number_box(0.001, 20.0, 0.5)
        self.scan_dwell = self._number_box(0.0, 3600.0, 0.2)
        self.scan_preview_label = QLabel("尚未预览")
        self.scan_preview_button = QPushButton("预览任务")
        self.scan_start_button = QPushButton("开始扫描")
        self.scan_cancel_button = QPushButton("取消扫描")
        self.scan_preview_button.setObjectName("secondaryButton")
        self.scan_start_button.setObjectName("primaryButton")
        self.scan_cancel_button.setObjectName("dangerButton")
        self.scan_progress = QProgressBar()
        self.scan_progress.setFormat("未运行")

        fields = (
            ("X 起点", self.scan_x_start, "X 终点", self.scan_x_end, "X 步距", self.scan_x_step),
            ("Y 起点", self.scan_y_start, "Y 终点", self.scan_y_end, "Y 步距", self.scan_y_step),
        )
        for row, values in enumerate(fields):
            for column in range(0, 6, 2):
                layout.addWidget(QLabel(values[column]), row, column)
                layout.addWidget(values[column + 1], row, column + 1)
        layout.addWidget(QLabel("每点驻留时间(s)"), 2, 0)
        layout.addWidget(self.scan_dwell, 2, 1)
        layout.addWidget(self.scan_preview_label, 2, 2)
        layout.addWidget(self.scan_preview_button, 2, 3)
        layout.addWidget(self.scan_start_button, 2, 4)
        layout.addWidget(self.scan_cancel_button, 2, 5)
        layout.addWidget(self.scan_progress, 3, 0, 1, 6)
        # 页面被高级功能页撑高时，空余高度集中在底部，不拉散扫描控件。
        layout.setRowStretch(4, 1)

        return page

    def _connect_signals(self) -> None:
        self.refresh_button.clicked.connect(self.refresh_ports)
        self.connect_button.clicked.connect(self.request_connect)
        self.disconnect_button.clicked.connect(self.request_disconnect)
        self.clear_fault_button.clicked.connect(
            lambda: self._execute("全轴清错", "clear_errors")
        )
        self.stop_all_button.clicked.connect(self.stop_all)
        self._trajectory_finished.connect(self._on_trajectory_finished)
        self._grid_scan_progress.connect(self._on_grid_scan_progress)
        self._grid_scan_finished.connect(self._on_grid_scan_finished)
        self.scan_preview_button.clicked.connect(self.preview_grid_scan)
        self.scan_start_button.clicked.connect(self.start_grid_scan)
        self.scan_cancel_button.clicked.connect(self.cancel_grid_scan)

        for panel in self.axis_panels.values():
            panel.enable_requested.connect(
                lambda axis, enabled: self._execute(
                    "使能" if enabled else "失能", "set_axis_enabled", axis, enabled
                )
            )
            panel.clear_error_requested.connect(
                lambda axis: self._execute("单轴清错", "clear_axis_error", axis)
            )
            panel.home_requested.connect(
                lambda axis: self._execute("搜零", "home_axis", axis)
            )
            panel.abort_home_requested.connect(
                lambda axis: self._execute("中止搜零", "cancel_home", axis)
            )
            panel.speed_requested.connect(
                lambda axis, speed: self._execute("设置速度", "set_axis_speed", axis, speed)
            )
            panel.zero_requested.connect(
                lambda axis: self._execute("坐标置零", "zero_axis", axis)
            )
            panel.jog_requested.connect(
                lambda axis, direction, speed: self._execute(
                    "点动", "start_jog", axis, direction, speed=speed
                )
            )
            panel.stop_requested.connect(
                lambda axis: self._execute("单轴停止", "stop_axis", axis, allow_locked=True)
            )
            panel.relative_move_requested.connect(
                lambda axis, distance, speed: self._execute(
                    "相对运动", "move_axis_relative", axis, distance, speed=speed
                )
            )
            panel.absolute_move_requested.connect(
                lambda axis, position, speed: self._execute(
                    "绝对运动", "move_axis_absolute", axis, position, speed=speed
                )
            )

        self.advanced_panel.line_requested.connect(
            lambda move: self._execute("直线插补", "interpolate_line", move)
        )
        self.advanced_panel.arc_requested.connect(
            lambda move: self._execute("圆弧插补", "interpolate_arc", move)
        )
        self.advanced_panel.trigger_requested.connect(self._configure_trigger)
        self.advanced_panel.trigger_stop_requested.connect(self._stop_trigger)
        self.advanced_panel.path_requested.connect(self._start_path)
        self.advanced_panel.path_cancel_requested.connect(self._cancel_path)
        self.advanced_panel.input_error.connect(self._show_error)

    def _apply_theme(self) -> None:
        self.setStyleSheet(
            """
            QDialog { background:#f4f5f7; }
            QDialog#xyStageDialog,
            QWidget#xyStageManualPage,
            QWidget#xyStageGridPage,
            QWidget#xyStageAdvancedPanel { background:#f8fafc; }
            QGroupBox { border:1px solid #b8bcc4; margin-top:8px; padding-top:7px; }
            QGroupBox::title { subcontrol-origin:margin; left:12px; padding:0 5px; }
            QPushButton { min-height:25px; padding:1px 7px; }
            QDoubleSpinBox, QComboBox { min-height:25px; }
            QPushButton#primaryButton { background:#0f969c; color:white; border:1px solid #087f85; }
            QPushButton#secondaryButton { background:white; color:#1f2937; border:1px solid #9aa6b2; }
            QPushButton#dangerButton { background:#fff1f1; color:#b91c1c; border:1px solid #d9a0a0; }
            QPushButton:disabled { background:#e9edf2; color:#9aa6b2; border:1px solid #c8ced8; }
            """
        )

    def set_executor(self, executor: XYStageExecutor | None) -> None:
        """注入主程序共享的 executor；不转移生命周期所有权。"""
        self._executor = executor
        if executor is None or not bool(getattr(executor, "connected", False)):
            self._trigger_active = False
        capabilities = {
            "line": callable(getattr(executor, "interpolate_line", None)),
            "arc": callable(getattr(executor, "interpolate_arc", None)),
            "trigger": callable(getattr(executor, "configure_position_trigger", None)),
            "trigger_stop": callable(getattr(executor, "stop_position_trigger", None)),
            "path": callable(getattr(executor, "run_linear_path_blocking", None)),
            "path_cancel": callable(getattr(executor, "stop_all", None)),
        }
        self.advanced_panel.set_capabilities(capabilities)
        self.executor_changed.emit(executor)
        self.refresh_snapshot()

    @Slot()
    def refresh_ports(self) -> None:
        selected = self.port_combo.currentData()
        typed = self._selected_port()
        self.port_combo.clear()
        try:
            from serial.tools import list_ports

            ports = sorted(list_ports.comports(), key=lambda item: item.device)
        except Exception:
            ports = []
        for port in ports:
            self.port_combo.addItem(f"{port.device} — {port.description}", port.device)
        if self.port_combo.count() == 0:
            self.port_combo.addItem("未发现串口", "")
        if selected:
            index = self.port_combo.findData(selected)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)
        if re.fullmatch(r"COM[1-9]\d*", typed, re.IGNORECASE):
            self.port_combo.setEditText(typed.upper())

    def _selected_port(self) -> str:
        return self.port_combo.currentText().split("—", 1)[0].strip().upper()

    @Slot()
    def request_connect(self) -> None:
        port = self._selected_port()
        if re.fullmatch(r"COM[1-9]\d*", port) is None:
            self._show_error("请输入有效 COM 口，例如 COM7。")
            return
        before = self._executor
        self.connect_requested.emit(port)
        # 主窗口若在信号处理中创建/连接 executor，下面不会重复连接。
        current = self._executor
        if current is before and current is not None and not bool(current.connected):
            self._execute("连接", "connect", port, allow_locked=True)
        else:
            self.refresh_snapshot()

    @Slot()
    def request_disconnect(self) -> None:
        before = self._executor
        self.disconnect_requested.emit()
        current = self._executor
        if current is before and current is not None and bool(current.connected):
            self._execute("断开", "disconnect", allow_locked=True)
        else:
            self.refresh_snapshot()

    @Slot()
    def refresh_snapshot(self) -> None:
        executor = self._executor
        if executor is None:
            self.update_snapshot(self._empty_snapshot())
            return
        try:
            snapshot = executor.snapshot
        except Exception as error:
            self._show_error(f"读取位移台状态失败：{error}")
            return
        self.update_snapshot(snapshot)

    def update_snapshot(self, snapshot: DeviceSnapshot) -> None:
        self.snapshot = snapshot
        for axis, panel in self.axis_panels.items():
            panel.update_status(snapshot.axes.get(axis, AxisStatus(axis=axis)))
        x_status = snapshot.axes.get(0)
        y_status = snapshot.axes.get(1)
        if x_status is not None and y_status is not None:
            self.advanced_panel.update_limits(
                (x_status.soft_min_position, x_status.soft_max_position),
                (y_status.soft_min_position, y_status.soft_max_position),
            )
            self.scan_x_start.setRange(x_status.soft_min_position, x_status.soft_max_position)
            self.scan_x_end.setRange(x_status.soft_min_position, x_status.soft_max_position)
            self.scan_y_start.setRange(y_status.soft_min_position, y_status.soft_max_position)
            self.scan_y_end.setRange(y_status.soft_min_position, y_status.soft_max_position)

        connected = snapshot.connected
        fault_active = bool(snapshot.fault_message) or any(
            axis.hard_fault or bool(axis.fault_message) for axis in snapshot.axes.values()
        )
        worker_ready = self._executor is not None
        communication_ok = connected and snapshot.communication_failures == 0
        self.global_lamps["worker"].set_state("ok" if worker_ready else "off")
        self.global_lamps["controller"].set_state("ok" if connected else "off")
        self.global_lamps["communication"].set_state(
            "ok" if communication_ok else ("alarm" if connected else "off"),
            "状态轮询正常" if communication_ok else "通信未建立或读取失败",
        )
        self.global_lamps["parameters"].set_state(
            "ok" if snapshot.parameter_valid else ("alarm" if connected else "off")
        )
        self.global_lamps["task"].set_state(
            "active" if self.task_locked else ("idle" if connected else "off"),
            "扫描或轨迹任务运行中" if self.task_locked else "无自动任务",
        )
        self.global_lamps["buffer"].set_state(
            "active" if self._trajectory_active else ("idle" if connected else "off")
        )
        self.global_lamps["trigger"].set_state(
            "active" if self._trigger_active else ("idle" if connected else "off")
        )
        self.global_lamps["fault"].set_state(
            "alarm" if fault_active else ("idle" if connected else "off"),
            snapshot.fault_message,
        )
        self.connect_button.setEnabled(not connected)
        self.disconnect_button.setEnabled(connected)
        self.port_combo.setEnabled(not connected)
        self.refresh_button.setEnabled(not connected)
        self.clear_fault_button.setEnabled(connected and fault_active and not self.task_locked)
        self.stop_all_button.setEnabled(connected)

        ready = (
            connected
            and snapshot.parameter_valid
            and not self.task_locked
            and all(
                status.connected
                and status.parameter_valid
                and status.enabled
                and status.idle
                and not status.homing
                and not status.limit_interlock
                and not status.hard_fault
                and not status.fault_message
                for status in snapshot.axes.values()
            )
            and len(snapshot.axes) >= 2
            and not snapshot.fault_message
        )
        self.advanced_panel.set_motion_ready(
            ready, connected=connected, task_active=self._trajectory_active
        )
        self.scan_preview_button.setEnabled(not self.task_locked)
        self.scan_start_button.setEnabled(ready)
        self.scan_cancel_button.setEnabled(self._grid_scan_active)
        for panel in self.axis_panels.values():
            panel.set_task_locked(self.task_locked)
        self.snapshot_updated.emit(snapshot)

    def set_scan_active(self, active: bool) -> None:
        """由空间扫描总控同步任务锁；停止按钮仍然可用。"""
        self._scan_locked = bool(active)
        self.update_snapshot(self.snapshot)

    set_task_locked = set_scan_active

    def stop_all(self) -> None:
        self._cancel_path(set_message=False)
        if self._grid_scan_cancel is not None:
            self._grid_scan_cancel.set()
        self._execute("全轴停止", "stop_all", allow_locked=True)

    def _execute(
        self,
        label: str,
        method_name: str,
        *args: Any,
        allow_locked: bool = False,
        **kwargs: Any,
    ) -> Any | None:
        if self.task_locked and not allow_locked:
            self._show_error("扫描/轨迹任务运行中，手动操作已锁定。")
            return None
        executor = self._executor
        if executor is None:
            self._show_error("位移台控制接口尚未初始化。")
            return None
        method: Callable[..., Any] | None = getattr(executor, method_name, None)
        if not callable(method):
            self._show_error(f"当前位移台控制接口不支持“{label}”。")
            return None
        try:
            result = method(*args, **kwargs)
        except Exception as error:
            self._show_error(f"{label}失败：{error}")
            return None
        self.message_label.setStyleSheet("")
        self.message_label.setText(f"{label}指令已发送")
        if isinstance(result, DeviceSnapshot):
            self.update_snapshot(result)
        else:
            self.refresh_snapshot()
        return result

    def _configure_trigger(self, config: object) -> None:
        if self._execute("启用位置触发", "configure_position_trigger", config) is not None:
            self._trigger_active = True
            self.update_snapshot(self.snapshot)

    def _stop_trigger(self, axis: int) -> None:
        if self._execute(
            "停止位置触发", "stop_position_trigger", axis, allow_locked=True
        ) is not None:
            self._trigger_active = False
            self.update_snapshot(self.snapshot)

    @staticmethod
    def _inclusive_axis_values(start: float, end: float, step: float) -> list[float]:
        if step <= 0:
            raise ValueError("扫描步距必须大于 0")
        direction = 1.0 if end >= start else -1.0
        distance = abs(end - start)
        count = int(distance // step) + 1
        values = [start + direction * step * index for index in range(count)]
        if not values or abs(values[-1] - end) > 1e-9:
            values.append(end)
        return values

    def _grid_scan_points(self) -> list[tuple[float, float]]:
        x_values = self._inclusive_axis_values(
            self.scan_x_start.value(), self.scan_x_end.value(), self.scan_x_step.value()
        )
        y_values = self._inclusive_axis_values(
            self.scan_y_start.value(), self.scan_y_end.value(), self.scan_y_step.value()
        )
        count = len(x_values) * len(y_values)
        if count > 100_000:
            raise ValueError(f"扫描点数 {count} 过多，请增大步距")
        points: list[tuple[float, float]] = []
        for row, y_value in enumerate(y_values):
            row_values = x_values if row % 2 == 0 else reversed(x_values)
            points.extend((x_value, y_value) for x_value in row_values)
        return points

    @Slot()
    def preview_grid_scan(self) -> None:
        try:
            points = self._grid_scan_points()
        except ValueError as error:
            self._show_error(str(error))
            return
        dwell = self.scan_dwell.value()
        self.scan_preview_label.setText(
            f"{len(points)} 点，驻留约 {len(points) * dwell:.1f} s（不含运动）"
        )
        self.scan_progress.setMaximum(max(1, len(points)))
        self.scan_progress.setValue(0)
        self.scan_progress.setFormat(f"0/{len(points)} | 已预览")

    @Slot()
    def start_grid_scan(self) -> None:
        if self.task_locked:
            self._show_error("已有自动任务在运行。")
            return
        executor = self._executor
        move = getattr(executor, "move_absolute_blocking", None)
        if executor is None or not bool(executor.connected) or not callable(move):
            self._show_error("请先连接并使能 XY 位移台。")
            return
        try:
            points = self._grid_scan_points()
        except ValueError as error:
            self._show_error(str(error))
            return
        self._grid_scan_active = True
        cancel_event = threading.Event()
        self._grid_scan_cancel = cancel_event
        dwell_seconds = self.scan_dwell.value()
        self.scan_progress.setMaximum(max(1, len(points)))
        self.scan_progress.setValue(0)
        self.update_snapshot(self.snapshot)

        def run() -> None:
            try:
                for index, (x_mm, y_mm) in enumerate(points, 1):
                    if cancel_event.is_set():
                        raise RuntimeError("二维扫描已取消")
                    move(
                        x_mm,
                        y_mm,
                        cancel_event=cancel_event,
                    )
                    if dwell_seconds > 0 and cancel_event.wait(dwell_seconds):
                        raise RuntimeError("二维扫描已取消")
                    self._grid_scan_progress.emit(index, len(points), "运行中")
            except Exception as error:
                self._grid_scan_finished.emit(error)
            else:
                self._grid_scan_finished.emit(None)

        threading.Thread(target=run, name="xy-dialog-grid-scan", daemon=True).start()

    @Slot()
    def cancel_grid_scan(self) -> None:
        if self._grid_scan_cancel is not None:
            self._grid_scan_cancel.set()
        executor = self._executor
        if self._grid_scan_active and executor is not None:
            try:
                executor.stop_all()
            except Exception as error:
                self._show_error(f"取消二维扫描失败：{error}")

    @Slot(int, int, str)
    def _on_grid_scan_progress(self, completed: int, total: int, state: str) -> None:
        self.scan_progress.setMaximum(max(1, total))
        self.scan_progress.setValue(completed)
        self.scan_progress.setFormat(f"{completed}/{total} | {state}")

    @Slot(object)
    def _on_grid_scan_finished(self, error: object) -> None:
        self._grid_scan_active = False
        self._grid_scan_cancel = None
        if isinstance(error, Exception):
            state = "已取消" if "取消" in str(error) else "失败"
            self.scan_progress.setFormat(f"%v/%m | {state}")
            if state == "失败":
                self._show_error(f"二维扫描失败：{error}")
        else:
            self.scan_progress.setValue(self.scan_progress.maximum())
            self.scan_progress.setFormat("%v/%m | 完成")
            self.message_label.setText("二维扫描已完成")
        self.update_snapshot(self.snapshot)

    def _start_path(self, points: tuple, speed: float, window_size: int) -> None:
        if self.task_locked:
            self._show_error("已有自动任务在运行。")
            return
        executor = self._executor
        method = getattr(executor, "run_linear_path_blocking", None)
        if executor is None or not callable(method):
            self._show_error("当前 executor 不支持连续轨迹。")
            return
        self._trajectory_active = True
        self._trajectory_cancel = threading.Event()
        self.advanced_panel.update_path_progress(0, len(points), "运行中")
        self.update_snapshot(self.snapshot)

        def run() -> None:
            try:
                method(
                    points,
                    speed=speed,
                    window_size=window_size,
                    cancel_event=self._trajectory_cancel,
                )
            except Exception as error:
                self._trajectory_finished.emit(error)
            else:
                self._trajectory_finished.emit(None)

        threading.Thread(target=run, name="xy-dialog-linear-path", daemon=True).start()

    def _cancel_path(self, *, set_message: bool = True) -> None:
        if self._trajectory_cancel is not None:
            self._trajectory_cancel.set()
        if set_message and self._trajectory_active:
            self.message_label.setText("正在取消连续轨迹…")

    @Slot(object)
    def _on_trajectory_finished(self, error: object) -> None:
        self._trajectory_active = False
        self._trajectory_cancel = None
        if isinstance(error, Exception):
            self._show_error(f"连续轨迹结束：{error}")
            state = "已取消/失败"
        else:
            self.message_label.setStyleSheet("")
            self.message_label.setText("连续轨迹已完成")
            state = "已完成"
        self.advanced_panel.update_path_progress(0, 0, state)
        self.refresh_snapshot()

    def _show_error(self, message: str) -> None:
        self.message_label.setText(message)
        self.message_label.setStyleSheet("color:#b91c1c")
        self.error_occurred.emit(message)

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt API
        if event.type() == QEvent.Type.ActivationChange and not self.isActiveWindow():
            for panel in self.axis_panels.values():
                panel.force_stop_jog()
        super().changeEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        # 主窗口通常复用同一个设置弹窗，重新显示时恢复轮询。
        if hasattr(self, "poll_timer"):
            self.poll_timer.start()
            self.refresh_snapshot()
        super().showEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.poll_timer.stop()
        self._cancel_path(set_message=False)
        if self._grid_scan_cancel is not None:
            self._grid_scan_cancel.set()
        for panel in self.axis_panels.values():
            panel.force_stop_jog()
        # executor 生命周期归主程序，弹窗关闭时不断开、不 close。
        event.accept()


XYStageDialog = XYStageControlDialog


__all__ = ["XYStageControlDialog", "XYStageDialog"]
