from __future__ import annotations

from collections.abc import Mapping

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from grab_app.xy_stage.trajectory import (
    ArcDefinition,
    ArcMove,
    CoordinateMode,
    LineMove,
    Point2D,
    PositionTriggerConfig,
)


class _NoWheelMixin:
    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt API
        event.ignore()


class _DoubleSpinBox(_NoWheelMixin, QDoubleSpinBox):
    pass


class _SpinBox(_NoWheelMixin, QSpinBox):
    pass


class _ComboBox(_NoWheelMixin, QComboBox):
    pass


class XYAdvancedPanel(QWidget):
    """高级运动的参数界面与信号入口。

    是否可执行由 ``set_capabilities`` 和 ``set_motion_ready`` 共同决定；
    这样在 executor 缺少能力时只显示界面，不会使用软件模拟硬件执行。
    """

    line_requested = Signal(object)
    arc_requested = Signal(object)
    trigger_requested = Signal(object)
    trigger_stop_requested = Signal(int)
    path_requested = Signal(object, float, int)
    path_cancel_requested = Signal()
    input_error = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("xyStageAdvancedPanel")
        self._capabilities = {
            "line": False,
            "arc": False,
            "trigger": False,
            "trigger_stop": False,
            "path": False,
            "path_cancel": False,
        }
        self._ready = False
        self._connected = False
        self._task_active = False

        self.tabs = QTabWidget()
        self.tabs.setObjectName("xyStageAdvancedTabs")
        self.tabs.addTab(self._build_line_tab(), "直线插补")
        self.tabs.addTab(self._build_arc_tab(), "圆弧插补")
        self.tabs.addTab(self._build_trigger_tab(), "位置触发")
        self.tabs.addTab(self._build_path_tab(), "连续轨迹")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs)
        self._refresh_buttons()
        self.tabs.currentChanged.connect(
            lambda _index: QTimer.singleShot(0, self._fit_current_tab)
        )
        QTimer.singleShot(0, self._fit_current_tab)

    def _fit_current_tab(self) -> None:
        """按当前高级功能子页收缩内层页签，避免短页面留下大块空白。"""
        page = self.tabs.currentWidget()
        if page is None:
            return
        page_height = max(page.sizeHint().height(), page.minimumSizeHint().height())
        tab_bar_height = self.tabs.tabBar().sizeHint().height()
        self.tabs.setFixedHeight(max(70, page_height + tab_bar_height + 6))
        self.updateGeometry()
        ancestor = self.parentWidget()
        while ancestor is not None:
            fit = getattr(ancestor, "_fit_tab_to_current_page", None)
            if callable(fit):
                QTimer.singleShot(0, fit)
                break
            ancestor = ancestor.parentWidget()

    @staticmethod
    def _position_box(value: float = 0.0) -> _DoubleSpinBox:
        box = _DoubleSpinBox()
        box.setDecimals(4)
        box.setRange(-20.0, 20.0)
        box.setValue(value)
        box.setKeyboardTracking(False)
        return box

    @staticmethod
    def _speed_box() -> _DoubleSpinBox:
        box = _DoubleSpinBox()
        box.setDecimals(4)
        box.setRange(0.001, 5.0)
        box.setValue(0.5)
        box.setSuffix(" mm/s")
        box.setKeyboardTracking(False)
        return box

    @staticmethod
    def _mode_combo() -> _ComboBox:
        combo = _ComboBox()
        combo.addItem("绝对运动", CoordinateMode.ABSOLUTE)
        combo.addItem("相对运动", CoordinateMode.RELATIVE)
        return combo

    def _build_line_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.line_mode = self._mode_combo()
        self.line_x = self._position_box(10.0)
        self.line_y = self._position_box(10.0)
        self.line_speed = self._speed_box()
        self.line_button = QPushButton("执行硬件直线插补")
        self.line_button.setObjectName("primaryButton")
        self.line_button.clicked.connect(self._emit_line)
        form.addRow("坐标方式", self.line_mode)
        form.addRow("X 终点/位移", self.line_x)
        form.addRow("Y 终点/位移", self.line_y)
        form.addRow("合成速度", self.line_speed)
        form.addRow(self.line_button)
        return tab

    def _build_arc_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.arc_mode = self._mode_combo()
        self.arc_definition = _ComboBox()
        self.arc_definition.addItem("圆心 + 终点", ArcDefinition.CENTER)
        self.arc_definition.addItem("中间点 + 终点", ArcDefinition.THREE_POINT)
        self.arc_aux_x = self._position_box(10.0)
        self.arc_aux_y = self._position_box(10.0)
        self.arc_end_x = self._position_box(12.0)
        self.arc_end_y = self._position_box(10.0)
        self.arc_direction = _ComboBox()
        self.arc_direction.addItem("逆时针", 0)
        self.arc_direction.addItem("顺时针", 1)
        self.arc_speed = self._speed_box()
        self.arc_button = QPushButton("执行硬件圆弧插补")
        self.arc_button.setObjectName("primaryButton")
        self.arc_button.clicked.connect(self._emit_arc)
        form.addRow("坐标方式", self.arc_mode)
        form.addRow("定义方式", self.arc_definition)
        form.addRow("圆心/中间点 X", self.arc_aux_x)
        form.addRow("圆心/中间点 Y", self.arc_aux_y)
        form.addRow("终点 X", self.arc_end_x)
        form.addRow("终点 Y", self.arc_end_y)
        form.addRow("方向", self.arc_direction)
        form.addRow("合成速度", self.arc_speed)
        form.addRow(self.arc_button)
        return tab

    def _build_trigger_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.trigger_axis = _ComboBox()
        self.trigger_axis.addItem("X 轴 (0)", 0)
        self.trigger_axis.addItem("Y 轴 (1)", 1)
        self.trigger_positions = QPlainTextEdit("1, 2, 3")
        self.trigger_positions.setMaximumHeight(58)
        self.trigger_active_state = _ComboBox()
        self.trigger_active_state.addItem("高电平有效", 1)
        self.trigger_active_state.addItem("低电平有效", 0)
        self.trigger_pulse = _SpinBox()
        self.trigger_pulse.setRange(1, 1_000_000)
        self.trigger_pulse.setValue(100)
        self.trigger_pulse.setSuffix(" μs")
        self.trigger_cycle = _SpinBox()
        self.trigger_cycle.setRange(2, 1_000_000)
        self.trigger_cycle.setValue(500)
        self.trigger_cycle.setSuffix(" μs")
        button_row = QWidget()
        buttons = QHBoxLayout(button_row)
        buttons.setContentsMargins(0, 0, 0, 0)
        self.trigger_start_button = QPushButton("启用位置触发")
        self.trigger_stop_button = QPushButton("停止触发")
        self.trigger_start_button.setObjectName("primaryButton")
        self.trigger_stop_button.setObjectName("dangerButton")
        self.trigger_start_button.clicked.connect(self._emit_trigger)
        self.trigger_stop_button.clicked.connect(
            lambda: self.trigger_stop_requested.emit(int(self.trigger_axis.currentData()))
        )
        buttons.addWidget(self.trigger_start_button)
        buttons.addWidget(self.trigger_stop_button)
        form.addRow("比较轴", self.trigger_axis)
        form.addRow("触发位置列表", self.trigger_positions)
        form.addRow("输出通道", QLabel("控制器通道 0（OUT0）"))
        form.addRow("有效电平", self.trigger_active_state)
        form.addRow("脉宽", self.trigger_pulse)
        form.addRow("周期", self.trigger_cycle)
        form.addRow(button_row)
        return tab

    def _build_path_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.path_points = QPlainTextEdit("6,6\n8,6\n8,8\n6,8")
        self.path_points.setPlaceholderText("每行一个绝对坐标：X,Y")
        self.path_points.setMaximumHeight(92)
        self.path_speed = self._speed_box()
        self.path_window = _SpinBox()
        self.path_window.setRange(1, 16)
        self.path_window.setValue(4)
        self.path_state = QLabel("未运行")
        button_row = QWidget()
        buttons = QHBoxLayout(button_row)
        buttons.setContentsMargins(0, 0, 0, 0)
        self.path_start_button = QPushButton("开始连续轨迹")
        self.path_cancel_button = QPushButton("取消轨迹")
        self.path_start_button.setObjectName("primaryButton")
        self.path_cancel_button.setObjectName("dangerButton")
        self.path_start_button.clicked.connect(self._emit_path)
        self.path_cancel_button.clicked.connect(self.path_cancel_requested.emit)
        buttons.addWidget(self.path_start_button)
        buttons.addWidget(self.path_cancel_button)
        form.addRow("绝对坐标点", self.path_points)
        form.addRow("合成速度", self.path_speed)
        form.addRow("硬件缓冲窗口", self.path_window)
        form.addRow("状态", self.path_state)
        form.addRow(button_row)
        return tab

    def _emit_line(self) -> None:
        self.line_requested.emit(
            LineMove(
                Point2D(self.line_x.value(), self.line_y.value()),
                self.line_mode.currentData(),
                self.line_speed.value(),
            )
        )

    def _emit_arc(self) -> None:
        self.arc_requested.emit(
            ArcMove(
                end=Point2D(self.arc_end_x.value(), self.arc_end_y.value()),
                auxiliary=Point2D(self.arc_aux_x.value(), self.arc_aux_y.value()),
                coordinate_mode=self.arc_mode.currentData(),
                definition=self.arc_definition.currentData(),
                direction=int(self.arc_direction.currentData()),
                speed=self.arc_speed.value(),
            )
        )

    def _emit_trigger(self) -> None:
        try:
            raw_values = self.trigger_positions.toPlainText().replace("\n", ",")
            positions = tuple(
                float(value.strip()) for value in raw_values.split(",") if value.strip()
            )
            if not positions:
                raise ValueError("请至少输入一个触发位置")
            config = PositionTriggerConfig(
                axis=int(self.trigger_axis.currentData()),
                positions=positions,
                output=0,
                active_state=int(self.trigger_active_state.currentData()),
                pulse_width_us=self.trigger_pulse.value(),
                cycle_us=self.trigger_cycle.value(),
            )
        except ValueError as error:
            self.input_error.emit(f"位置触发参数错误：{error}")
            return
        self.trigger_requested.emit(config)

    def _emit_path(self) -> None:
        try:
            points: list[Point2D] = []
            for line_number, line in enumerate(self.path_points.toPlainText().splitlines(), 1):
                if not line.strip():
                    continue
                values = [value.strip() for value in line.split(",")]
                if len(values) != 2:
                    raise ValueError(f"第 {line_number} 行应为 X,Y")
                points.append(Point2D(float(values[0]), float(values[1])))
            if not points:
                raise ValueError("请至少输入一个轨迹点")
        except ValueError as error:
            self.input_error.emit(f"连续轨迹参数错误：{error}")
            return
        self.path_requested.emit(tuple(points), self.path_speed.value(), self.path_window.value())

    def set_capabilities(self, capabilities: Mapping[str, bool]) -> None:
        self._capabilities.update({name: bool(value) for name, value in capabilities.items()})
        self._refresh_buttons()

    def set_motion_ready(self, ready: bool, *, connected: bool, task_active: bool) -> None:
        self._ready = bool(ready)
        self._connected = bool(connected)
        self._task_active = bool(task_active)
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        regular_ready = self._ready and not self._task_active
        capability_buttons = (
            (self.line_button, "line"),
            (self.arc_button, "arc"),
            (self.trigger_start_button, "trigger"),
            (self.path_start_button, "path"),
        )
        for button, capability in capability_buttons:
            available = self._capabilities[capability]
            button.setEnabled(regular_ready and available)
            button.setToolTip("" if available else "当前位移台控制接口不提供此硬件功能")
        self.trigger_stop_button.setEnabled(
            self._connected and self._capabilities["trigger_stop"]
        )
        self.path_cancel_button.setEnabled(
            self._task_active and self._capabilities["path_cancel"]
        )

    def update_limits(
        self, x_limits: tuple[float, float], y_limits: tuple[float, float]
    ) -> None:
        x_min, x_max = x_limits
        y_min, y_max = y_limits
        x_span = max(x_max - x_min, 1.0)
        y_span = max(y_max - y_min, 1.0)
        for box in (self.line_x, self.arc_aux_x, self.arc_end_x):
            box.setRange(min(x_min, -x_span), max(x_max, x_span))
        for box in (self.line_y, self.arc_aux_y, self.arc_end_y):
            box.setRange(min(y_min, -y_span), max(y_max, y_span))

    def update_path_progress(self, completed: int, total: int, state: str) -> None:
        self.path_state.setText(f"{completed}/{total} | {state}")


AdvancedPanel = XYAdvancedPanel


__all__ = ["AdvancedPanel", "XYAdvancedPanel"]
