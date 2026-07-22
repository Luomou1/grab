from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget


class StatusLamp(QWidget):
    """紧凑状态指示灯，同时保留文字和 tooltip 便于无障碍读取。"""

    COLORS = {
        "off": "#9ca3af",
        "idle": "#9ca3af",
        "ok": "#16a34a",
        "active": "#2563eb",
        "warning": "#f59e0b",
        "alarm": "#dc2626",
    }

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = "off"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 1, 2, 1)
        layout.setSpacing(2)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.title_label = QLabel(title)
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.indicator = QFrame()
        self.indicator.setFixedSize(12, 12)
        self.indicator.setAccessibleName(f"{title}状态灯")
        layout.addWidget(self.title_label)
        layout.addWidget(self.indicator, alignment=Qt.AlignmentFlag.AlignCenter)
        self.set_state("off")

    def set_state(self, state: str, tooltip: str = "") -> None:
        if state not in self.COLORS:
            raise ValueError(f"未知状态灯状态：{state}")
        self.state = state
        color = self.COLORS[state]
        self.indicator.setStyleSheet(
            "QFrame {"
            f"background-color:{color};"
            "border:1px solid rgba(17,24,39,0.35);"
            "border-radius:6px;"
            "}"
        )
        self.setToolTip(tooltip)
        description = tooltip or state
        self.indicator.setAccessibleDescription(description)
        self.indicator.setAccessibleName(f"{self.title_label.text()}：{description}")
