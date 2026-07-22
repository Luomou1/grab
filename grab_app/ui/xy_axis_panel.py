from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from grab_app.xy_stage.models import AxisProfile, AxisStatus
from grab_app.xy_stage.safety import HARD_FAULT_MASK, drive_status_has_error

from .status_lamp import StatusLamp


class _NoWheelDoubleSpinBox(QDoubleSpinBox):
    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt API
        event.ignore()


class XYAxisPanel(QGroupBox):
    """单轴手动控制面板；只发送意图，不直接访问硬件。"""

    enable_requested = Signal(int, bool)
    clear_error_requested = Signal(int)
    home_requested = Signal(int)
    abort_home_requested = Signal(int)
    speed_requested = Signal(int, float)
    zero_requested = Signal(int)
    jog_requested = Signal(int, int, float)
    stop_requested = Signal(int)
    relative_move_requested = Signal(int, float, float)
    absolute_move_requested = Signal(int, float, float)

    def __init__(self, profile: AxisProfile, parent: QWidget | None = None) -> None:
        super().__init__(f"{profile.name} 轴（通道 {profile.axis}）", parent)
        self.profile = profile
        self.status = AxisStatus(axis=profile.axis)
        self.task_locked = False
        self._jog_active = False
        self._build_ui()
        self.update_status(self.status)

    @staticmethod
    def _spinbox(
        minimum: float,
        maximum: float,
        value: float,
        *,
        suffix: str,
    ) -> _NoWheelDoubleSpinBox:
        box = _NoWheelDoubleSpinBox()
        box.setDecimals(4)
        box.setRange(minimum, maximum)
        box.setValue(value)
        box.setSingleStep(0.1)
        box.setSuffix(suffix)
        box.setKeyboardTracking(False)
        return box

    def _build_ui(self) -> None:
        root = QGridLayout(self)
        root.setContentsMargins(8, 10, 8, 8)
        root.setHorizontalSpacing(6)
        root.setVerticalSpacing(5)

        status_row = QHBoxLayout()
        self.position_label = QLabel("0.0000 mm")
        position_font = QFont()
        position_font.setPointSize(16)
        position_font.setBold(True)
        self.position_label.setFont(position_font)
        self.position_label.setAccessibleName(f"{self.profile.name}轴位置")
        self.state_label = QLabel("未连接")
        status_row.addWidget(self.position_label)
        status_row.addStretch()
        status_row.addWidget(self.state_label)
        root.addLayout(status_row, 0, 0, 1, 4)

        indicators = QGridLayout()
        indicators.setHorizontalSpacing(1)
        self.status_lamps = {
            "connected": StatusLamp("连接"),
            "enabled": StatusLamp("使能"),
            "running": StatusLamp("运行"),
            "homed": StatusLamp("已搜零"),
            "positive_limit": StatusLamp("正限位"),
            "negative_limit": StatusLamp("负限位"),
            "alarm": StatusLamp("报警"),
        }
        for column, lamp in enumerate(self.status_lamps.values()):
            indicators.addWidget(lamp, 0, column)
        self.axis_status_label = QLabel("轴状态 0x0  |  驱动状态 0x0")
        self.axis_status_label.setObjectName("technicalReadout")
        self.axis_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        indicators.addWidget(self.axis_status_label, 1, 0, 1, 7)
        root.addLayout(indicators, 1, 0, 1, 4)

        action_row = QHBoxLayout()
        self.enable_button = QPushButton("使能")
        self.clear_button = QPushButton("清除报警")
        self.home_button = QPushButton("搜零")
        self.abort_home_button = QPushButton("中止搜零")
        for button in (
            self.enable_button,
            self.clear_button,
            self.home_button,
            self.abort_home_button,
        ):
            action_row.addWidget(button)
        root.addLayout(action_row, 2, 0, 1, 4)

        self.speed_box = self._spinbox(
            0.001, self.profile.max_speed, self.profile.default_speed, suffix=" mm/s"
        )
        self.relative_box = self._spinbox(-20.0, 20.0, 0.1, suffix=" mm")
        self.absolute_box = self._spinbox(
            self.profile.min_position,
            self.profile.max_position,
            self.profile.min_position,
            suffix=" mm",
        )
        self.relative_negative_button = QPushButton("负向移动")
        self.relative_positive_button = QPushButton("正向移动")
        self.absolute_button = QPushButton("移动到位置")
        self.return_zero_button = QPushButton("返回零位")
        self.jog_negative_button = QPushButton("负向点动")
        self.stop_button = QPushButton("停止")
        self.jog_positive_button = QPushButton("正向点动")
        self.zero_button = QPushButton("置零")
        for button in (
            self.enable_button,
            self.clear_button,
            self.home_button,
            self.abort_home_button,
            self.relative_negative_button,
            self.relative_positive_button,
            self.absolute_button,
            self.return_zero_button,
            self.jog_negative_button,
            self.jog_positive_button,
            self.zero_button,
        ):
            button.setObjectName("secondaryButton")
        self.abort_home_button.setObjectName("dangerButton")
        self.stop_button.setObjectName("dangerButton")

        root.addWidget(QLabel("运行速度"), 3, 0)
        root.addWidget(self.speed_box, 3, 1, 1, 3)
        root.addWidget(QLabel("相对位移"), 4, 0)
        root.addWidget(self.relative_box, 4, 1)
        root.addWidget(self.relative_negative_button, 4, 2)
        root.addWidget(self.relative_positive_button, 4, 3)
        root.addWidget(QLabel("绝对位置"), 5, 0)
        root.addWidget(self.absolute_box, 5, 1)
        root.addWidget(self.absolute_button, 5, 2)
        root.addWidget(self.return_zero_button, 5, 3)
        root.addWidget(self.jog_negative_button, 6, 0)
        root.addWidget(self.stop_button, 6, 1)
        root.addWidget(self.jog_positive_button, 6, 2)
        root.addWidget(self.zero_button, 6, 3)

        self.enable_button.clicked.connect(self._request_enable)
        self.clear_button.clicked.connect(
            lambda: self.clear_error_requested.emit(self.profile.axis)
        )
        self.home_button.clicked.connect(lambda: self.home_requested.emit(self.profile.axis))
        self.abort_home_button.clicked.connect(
            lambda: self.abort_home_requested.emit(self.profile.axis)
        )
        self.speed_box.editingFinished.connect(
            lambda: self.speed_requested.emit(self.profile.axis, self.speed_box.value())
        )
        self.zero_button.clicked.connect(lambda: self.zero_requested.emit(self.profile.axis))
        self.stop_button.clicked.connect(self._stop_jog)
        self.relative_negative_button.clicked.connect(lambda: self._request_relative(-1))
        self.relative_positive_button.clicked.connect(lambda: self._request_relative(1))
        self.absolute_button.clicked.connect(self._request_absolute)
        self.return_zero_button.clicked.connect(
            lambda: self.absolute_move_requested.emit(
                self.profile.axis, 0.0, self.speed_box.value()
            )
        )
        self.jog_negative_button.pressed.connect(lambda: self._start_jog(-1))
        self.jog_positive_button.pressed.connect(lambda: self._start_jog(1))
        self.jog_negative_button.released.connect(self._stop_jog)
        self.jog_positive_button.released.connect(self._stop_jog)

    def _request_enable(self) -> None:
        self.enable_requested.emit(self.profile.axis, not self.status.enabled)

    def _request_relative(self, sign: int) -> None:
        self.relative_move_requested.emit(
            self.profile.axis,
            abs(self.relative_box.value()) * sign,
            self.speed_box.value(),
        )

    def _request_absolute(self) -> None:
        self.absolute_move_requested.emit(
            self.profile.axis, self.absolute_box.value(), self.speed_box.value()
        )

    def _start_jog(self, direction: int) -> None:
        if not self.jog_negative_button.isEnabled() and direction < 0:
            return
        if not self.jog_positive_button.isEnabled() and direction > 0:
            return
        self._jog_active = True
        self.jog_requested.emit(self.profile.axis, direction, self.speed_box.value())

    def _stop_jog(self) -> None:
        # 停止不受 task lock 限制；按键释放和显式停止都会发送指令。
        self._jog_active = False
        self.stop_requested.emit(self.profile.axis)

    def force_stop_jog(self) -> None:
        if self._jog_active:
            self._stop_jog()

    def set_task_locked(self, locked: bool) -> None:
        self.task_locked = bool(locked)
        if locked:
            self.force_stop_jog()
        self.update_status(self.status)

    def update_status(self, status: AxisStatus) -> None:
        self.status = status
        if status.soft_min_position < status.soft_max_position:
            travel = status.soft_max_position - status.soft_min_position
            self.relative_box.setRange(-travel, travel)
            self.absolute_box.setRange(status.soft_min_position, status.soft_max_position)
            self.absolute_box.setToolTip(
                "当前用户坐标软限位："
                f"[{status.soft_min_position:g}, {status.soft_max_position:g}] mm"
            )
        self.position_label.setText(f"{status.dpos:.4f} mm")
        if not status.connected:
            state_text = "未连接"
        elif status.homing:
            state_text = "搜零中"
        elif status.negative_any_limit and status.positive_any_limit:
            state_text = "双向限位"
        elif status.negative_soft_limit:
            state_text = "负软限位"
        elif status.positive_soft_limit:
            state_text = "正软限位"
        elif status.negative_limit:
            state_text = "负限位"
        elif status.positive_limit:
            state_text = "正限位"
        elif status.running:
            state_text = "运动中"
        elif status.fault_message or status.hard_fault:
            state_text = "故障"
        else:
            state_text = "空闲"
        colors = {
            "未连接": "#6b7280",
            "空闲": "#16a34a",
            "运动中": "#2563eb",
            "搜零中": "#2563eb",
            "负限位": "#dc2626",
            "正限位": "#dc2626",
            "负软限位": "#dc2626",
            "正软限位": "#dc2626",
            "双向限位": "#dc2626",
            "故障": "#dc2626",
        }
        self.state_label.setText(state_text)
        self.state_label.setStyleSheet(f"color:{colors[state_text]};font-weight:700")

        fault_present = bool(
            status.hard_fault
            or status.fault_message
            or status.axis_status & HARD_FAULT_MASK
            or drive_status_has_error(status.drive_status)
        )
        lamps = self.status_lamps
        lamps["connected"].set_state(
            "ok" if status.connected else "off",
            "轴状态读取正常" if status.connected else "轴未连接",
        )
        lamps["enabled"].set_state(
            "ok" if status.enabled else "off",
            "驱动已使能" if status.enabled else "驱动未使能",
        )
        lamps["running"].set_state(
            "active" if status.running else ("idle" if status.connected else "off"),
            "运动中" if status.running else "未运动",
        )
        lamps["homed"].set_state(
            "ok" if status.homed else ("warning" if status.connected else "off"),
            "已完成搜零" if status.homed else "尚未完成搜零",
        )
        lamps["positive_limit"].set_state(
            "alarm" if status.positive_any_limit else ("idle" if status.connected else "off"),
            (
                "正硬限位触发" if status.positive_limit
                else "正软限位触发" if status.positive_soft_limit
                else "正限位正常"
            ),
        )
        lamps["negative_limit"].set_state(
            "alarm" if status.negative_any_limit else ("idle" if status.connected else "off"),
            (
                "负硬限位触发" if status.negative_limit
                else "负软限位触发" if status.negative_soft_limit
                else "负限位正常"
            ),
        )
        lamps["alarm"].set_state(
            "alarm" if fault_present else ("idle" if status.connected else "off"),
            status.fault_message or ("存在报警" if fault_present else "无报警"),
        )
        self.axis_status_label.setText(
            f"轴状态 0x{status.axis_status:X}  |  驱动状态 0x{status.drive_status:X}"
        )
        self.enable_button.setText("失能" if status.enabled else "使能")

        connected_ready = status.connected and status.parameter_valid
        idle_ready = (
            connected_ready
            and status.idle
            and not fault_present
            and not status.homing
            and not self.task_locked
        )
        motion_ready = idle_ready and status.enabled
        self.enable_button.setEnabled(idle_ready)
        self.clear_button.setEnabled(status.connected and fault_present and not self.task_locked)
        self.home_button.setEnabled(motion_ready)
        self.abort_home_button.setEnabled(
            status.connected and status.homing and not self.task_locked
        )
        self.relative_negative_button.setEnabled(motion_ready and not status.negative_any_limit)
        self.relative_positive_button.setEnabled(motion_ready and not status.positive_any_limit)
        self.absolute_button.setEnabled(motion_ready)
        return_delta = -status.dpos
        self.return_zero_button.setEnabled(
            motion_ready
            and not (return_delta < 0 and status.negative_any_limit)
            and not (return_delta > 0 and status.positive_any_limit)
        )
        self.jog_negative_button.setEnabled(motion_ready and not status.negative_any_limit)
        self.jog_positive_button.setEnabled(motion_ready and not status.positive_any_limit)
        self.stop_button.setEnabled(status.connected)
        self.zero_button.setEnabled(motion_ready)
        for editor in (self.speed_box, self.relative_box, self.absolute_box):
            editor.setEnabled(connected_ready and not self.task_locked)


# 兼容参考项目的类名，便于后续代码迁移。
AxisPanel = XYAxisPanel


__all__ = ["AxisPanel", "XYAxisPanel"]
