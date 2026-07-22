from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from grab_app.ui.xy_stage_dialog import XYStageControlDialog
from grab_app.xy_stage import AxisStatus, DeviceSnapshot


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _snapshot(*, connected: bool = True, enabled: bool = True) -> DeviceSnapshot:
    return DeviceSnapshot(
        connected=connected,
        parameter_valid=connected,
        axes={
            axis: AxisStatus(
                axis=axis,
                connected=connected,
                parameter_valid=connected,
                dpos=1.25 + axis,
                enabled=enabled,
                homed=True,
                soft_min_position=0.0,
                soft_max_position=20.0,
            )
            for axis in (0, 1)
        },
    )


class FakeExecutor:
    def __init__(self, *, connected: bool = True) -> None:
        self._snapshot = _snapshot(connected=connected)
        self.calls: list[tuple[str, tuple, dict]] = []
        self.closed = False

    @property
    def connected(self) -> bool:
        return self._snapshot.connected

    @property
    def snapshot(self) -> DeviceSnapshot:
        return self._snapshot

    def _record(self, name: str, *args, **kwargs) -> DeviceSnapshot:
        self.calls.append((name, args, kwargs))
        return self._snapshot

    def connect(self, port: str) -> DeviceSnapshot:
        self._snapshot = _snapshot()
        return self._record("connect", port)

    def disconnect(self) -> None:
        self.calls.append(("disconnect", (), {}))
        self._snapshot = _snapshot(connected=False)

    def set_axis_enabled(self, *args, **kwargs):
        return self._record("set_axis_enabled", *args, **kwargs)

    def clear_errors(self, *args, **kwargs):
        return self._record("clear_errors", *args, **kwargs)

    def clear_axis_error(self, *args, **kwargs):
        return self._record("clear_axis_error", *args, **kwargs)

    def home_axis(self, *args, **kwargs):
        return self._record("home_axis", *args, **kwargs)

    def cancel_home(self, *args, **kwargs):
        return self._record("cancel_home", *args, **kwargs)

    def set_axis_speed(self, *args, **kwargs):
        return self._record("set_axis_speed", *args, **kwargs)

    def zero_axis(self, *args, **kwargs):
        return self._record("zero_axis", *args, **kwargs)

    def start_jog(self, *args, **kwargs):
        return self._record("start_jog", *args, **kwargs)

    def stop_axis(self, *args, **kwargs):
        return self._record("stop_axis", *args, **kwargs)

    def move_axis_relative(self, *args, **kwargs):
        return self._record("move_axis_relative", *args, **kwargs)

    def move_axis_absolute(self, *args, **kwargs):
        return self._record("move_axis_absolute", *args, **kwargs)

    def stop_all(self, *args, **kwargs):
        return self._record("stop_all", *args, **kwargs)

    def interpolate_line(self, *args, **kwargs):
        return self._record("interpolate_line", *args, **kwargs)

    def interpolate_arc(self, *args, **kwargs):
        return self._record("interpolate_arc", *args, **kwargs)

    def configure_position_trigger(self, *args, **kwargs):
        return self._record("configure_position_trigger", *args, **kwargs)

    def stop_position_trigger(self, *args, **kwargs):
        return self._record("stop_position_trigger", *args, **kwargs)

    def run_linear_path_blocking(self, *args, **kwargs):
        return self._record("run_linear_path_blocking", *args, **kwargs)

    def move_absolute_blocking(self, x_mm, y_mm, **kwargs):
        self.calls.append(("move_absolute_blocking", (x_mm, y_mm), kwargs))
        return x_mm, y_mm

    def close(self) -> None:
        self.closed = True


def test_dialog_updates_axis_readout_and_dispatches_manual_actions(app: QApplication) -> None:
    executor = FakeExecutor()
    dialog = XYStageControlDialog(executor=executor)
    dialog.show()
    app.processEvents()

    assert dialog.axis_panels[0].position_label.text() == "1.2500 mm"
    assert dialog.axis_panels[1].status_lamps["connected"].state == "ok"
    assert dialog.axis_panels[0].axis_status_label.text().startswith("轴状态")

    panel = dialog.axis_panels[0]
    panel.relative_box.setValue(0.5)
    QTest.mouseClick(panel.relative_positive_button, Qt.MouseButton.LeftButton)
    panel.absolute_box.setValue(2.0)
    QTest.mouseClick(panel.absolute_button, Qt.MouseButton.LeftButton)
    QTest.mousePress(panel.jog_positive_button, Qt.MouseButton.LeftButton)
    QTest.mouseRelease(panel.jog_positive_button, Qt.MouseButton.LeftButton)

    names = [name for name, _args, _kwargs in executor.calls]
    assert "move_axis_relative" in names
    assert "move_axis_absolute" in names
    assert names[-2:] == ["start_jog", "stop_axis"]
    dialog.close()
    assert not executor.closed


def test_dialog_uses_application_button_roles_and_consistent_chinese_labels(
    app: QApplication,
) -> None:
    dialog = XYStageControlDialog(executor=FakeExecutor())
    panel = dialog.axis_panels[0]
    buttons = dialog.findChildren(QPushButton)

    assert buttons
    assert all(
        button.objectName() in {"primaryButton", "secondaryButton", "dangerButton"}
        for button in buttons
    )
    assert dialog.refresh_button.text() == "刷新串口"
    assert dialog.clear_fault_button.text() == "清除报警"
    assert dialog.global_lamps["worker"].title_label.text() == "控制线程"
    assert dialog.global_lamps["trigger"].title_label.text() == "位置触发"
    assert panel.title() == "X 轴（通道 0）"
    assert panel.status_lamps["connected"].title_label.text() == "连接"
    assert panel.relative_negative_button.text() == "负向移动"
    assert panel.relative_positive_button.text() == "正向移动"
    assert panel.absolute_button.text() == "移动到位置"
    assert panel.jog_negative_button.text() == "负向点动"
    assert panel.jog_positive_button.text() == "正向点动"
    assert panel.axis_status_label.text().startswith("轴状态")

    dialog.close()


def test_grid_scan_layout_stays_compact_when_dialog_is_tall(app: QApplication) -> None:
    dialog = XYStageControlDialog()
    dialog.resize(1200, 1000)
    dialog.tabs.setCurrentIndex(1)
    dialog.show()
    app.processEvents()

    layout = dialog.tabs.widget(1).layout()
    assert layout.rowStretch(4) == 1
    assert dialog.scan_y_start.y() - dialog.scan_x_start.y() < 80
    assert dialog.scan_dwell.y() - dialog.scan_y_start.y() < 80
    assert dialog.scan_progress.y() - dialog.scan_dwell.y() < 80

    dialog.close()


def test_dialog_tab_area_fits_current_page_instead_of_maximum_page(app: QApplication) -> None:
    dialog = XYStageControlDialog()
    dialog.show()
    app.processEvents()

    manual_page = dialog.tabs.widget(0)
    expected_max = manual_page.sizeHint().height() + dialog.tabs.tabBar().sizeHint().height() + 40
    assert dialog.tabs.height() <= expected_max

    dialog.close()


def test_advanced_tab_area_fits_current_subpage(app: QApplication) -> None:
    dialog = XYStageControlDialog()
    dialog.tabs.setCurrentIndex(2)
    dialog.advanced_panel.tabs.setCurrentIndex(0)
    dialog.show()
    app.processEvents()

    subpage = dialog.advanced_panel.tabs.widget(0)
    expected_max = subpage.sizeHint().height() + dialog.advanced_panel.tabs.tabBar().sizeHint().height() + 40
    assert dialog.advanced_panel.tabs.height() <= expected_max
    assert dialog.tabs.height() <= dialog.advanced_panel.sizeHint().height() + 60

    dialog.close()


def test_stage_control_dialog_does_not_contain_acquisition_overview_parameters(
    app: QApplication,
) -> None:
    dialog = XYStageControlDialog()

    assert not hasattr(dialog, "spatial_pixel_um")
    assert not hasattr(dialog, "spatial_overlap")
    assert not hasattr(dialog, "spatial_settle")
    assert not hasattr(dialog, "spatial_route")

    dialog.close()


def test_scan_lock_disables_manual_motion_but_keeps_stop(app: QApplication) -> None:
    executor = FakeExecutor()
    dialog = XYStageControlDialog(executor=executor)
    panel = dialog.axis_panels[0]

    dialog.set_scan_active(True)

    assert not panel.absolute_button.isEnabled()
    assert not panel.jog_positive_button.isEnabled()
    assert panel.stop_button.isEnabled()
    assert dialog.stop_all_button.isEnabled()
    QTest.mouseClick(panel.stop_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(dialog.stop_all_button, Qt.MouseButton.LeftButton)
    assert [item[0] for item in executor.calls][-2:] == ["stop_axis", "stop_all"]
    dialog.close()


def test_connection_signals_support_external_executor_ownership(app: QApplication) -> None:
    dialog = XYStageControlDialog()
    executor = FakeExecutor(connected=False)
    requested: list[str] = []

    def provide_shared_executor(port: str) -> None:
        requested.append(port)
        executor.connect(port)
        dialog.set_executor(executor)

    dialog.connect_requested.connect(provide_shared_executor)
    dialog.port_combo.setEditText("COM7")
    QTest.mouseClick(dialog.connect_button, Qt.MouseButton.LeftButton)

    assert requested == ["COM7"]
    assert dialog.executor is executor
    assert [item[0] for item in executor.calls].count("connect") == 1
    dialog.close()


def test_advanced_buttons_follow_executor_capabilities(app: QApplication) -> None:
    class BasicExecutor(FakeExecutor):
        interpolate_line = None
        interpolate_arc = None
        configure_position_trigger = None
        stop_position_trigger = None
        run_linear_path_blocking = None

    dialog = XYStageControlDialog(executor=BasicExecutor())

    assert not dialog.advanced_panel.line_button.isEnabled()
    assert not dialog.advanced_panel.arc_button.isEnabled()
    assert not dialog.advanced_panel.trigger_start_button.isEnabled()
    assert "不提供" in dialog.advanced_panel.line_button.toolTip()
    dialog.close()


def test_grid_scan_previews_and_runs_serpentine_path(app: QApplication) -> None:
    executor = FakeExecutor()
    dialog = XYStageControlDialog(executor=executor)
    dialog.scan_x_start.setValue(0.0)
    dialog.scan_x_end.setValue(1.0)
    dialog.scan_x_step.setValue(1.0)
    dialog.scan_y_start.setValue(0.0)
    dialog.scan_y_end.setValue(1.0)
    dialog.scan_y_step.setValue(1.0)
    dialog.scan_dwell.setValue(0.0)

    dialog.preview_grid_scan()
    assert dialog.scan_preview_label.text().startswith("4 点")
    dialog.start_grid_scan()
    deadline = time.monotonic() + 2.0
    while dialog._grid_scan_active:
        app.processEvents()
        if time.monotonic() >= deadline:
            pytest.fail("二维扫描未在超时内完成")
        time.sleep(0.01)

    points = [args for name, args, _kwargs in executor.calls if name == "move_absolute_blocking"]
    assert points == [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    assert dialog.scan_progress.value() == 4
    dialog.close()
