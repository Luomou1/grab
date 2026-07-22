from __future__ import annotations

import importlib
import os
import sys
import types
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from grab_app.spatial import SpatialRect
from grab_app.xy_stage import AxisStatus, DeviceSnapshot


def _import_main_window_without_update_file():
    module = types.ModuleType("grab_app.update")

    class UpdateInfo:  # pragma: no cover - 仅满足运行时注解/导入
        pass

    module.UpdateInfo = UpdateInfo
    module.check_latest_release = lambda: None
    module.download_installer = lambda *_args, **_kwargs: None
    module.start_installer = lambda *_args, **_kwargs: None
    sys.modules["grab_app.update"] = module
    sys.modules.pop("grab_app.ui.main_window", None)
    return importlib.import_module("grab_app.ui.main_window").MainWindow


def test_main_window_builds_spatial_map_and_plans_tiles() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    MainWindow = _import_main_window_without_update_file()
    window = MainWindow()
    try:
        window.camera.h_camera = object()
        window.camera.width = 1280
        window.camera.height = 1024
        plan = window._plan_spatial_rect(SpatialRect(1.0, 1.0, 2.0, 2.0))
        window._survey_plan = plan
        window._prepare_spatial_map(plan)

        assert window.viewer_tabs.count() == 2
        assert plan.tile_count >= 4
        assert window.spatial_map.model.map_size.width() > 0
        assert len(window.spatial_map.model.tiles) == plan.tile_count
        assert window.spatial_pixel_um.value() == 0.48
        assert window.spatial_pixel_um.text() == "0.48"
        assert window.btn_xy_section_title.text() == "XY位移"
        assert window.spatial_pixel_um.window() is window.xy_overview_dialog
        assert not hasattr(window.xy_settings_dialog, "spatial_pixel_um")
        assert window.btn_xy_stage_control.toolTip() == "XY位移台控制"
        assert window.xy_status.parentWidget().layout().indexOf(window.xy_status) >= 0
        overview_labels = {
            label.text() for label in window.xy_overview_dialog.findChildren(QLabel)
        }
        assert overview_labels == {
            "像素间距 (µm/px)", "重叠率", "稳定时间", "概览路径"
        }
        assert not window.xy_overview_dialog.findChildren(QPushButton)
    finally:
        window.camera.h_camera = None
        window.close()
        app.processEvents()


def test_spatial_center_controls_allow_single_row_and_follow_shifted_limits() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    MainWindow = _import_main_window_without_update_file()
    window = MainWindow()
    try:
        window.camera.h_camera = object()
        window.camera.width = 1280
        window.camera.height = 1024
        snapshot = DeviceSnapshot(
            connected=True,
            parameter_valid=True,
            axes={
                axis: AxisStatus(
                    axis=axis,
                    connected=True,
                    parameter_valid=True,
                    dpos=0.0,
                    enabled=True,
                    soft_min_position=-5.0,
                    soft_max_position=15.0,
                )
                for axis in (0, 1)
            },
        )

        window._on_xy_dialog_snapshot(snapshot)
        window.xy_stage = SimpleNamespace(snapshot=snapshot, close=lambda: None)
        window.xy_settings_dialog.close()
        window._update_xy_status_readback()
        assert "X=-" not in window.xy_status.text()
        assert "X=0 mm  Y=0 mm" == window.xy_realtime_position.text()
        assert window.xy_axis_lamps[0].state == "ok"
        assert window.xy_axis_lamps[1].state == "ok"
        window.survey_x_start.setValue(-1.0)
        window.survey_x_end.setValue(1.0)
        window.survey_y_start.setValue(-2.0)
        window.survey_y_end.setValue(-2.0)
        plan = window._plan_spatial_center_scan()

        assert window.xy_safe_x_min.value() == -5.0
        assert window.survey_x_start.minimum() < 0
        assert plan.rows == 1
        assert plan.columns > 1
        assert {item.target.y_mm for item in plan.placements} == {-2.0}
    finally:
        window.camera.h_camera = None
        window.close()
        app.processEvents()


def test_large_controller_limits_remain_the_scan_coordinate_source() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    MainWindow = _import_main_window_without_update_file()
    window = MainWindow()
    try:
        snapshot = DeviceSnapshot(
            connected=True,
            parameter_valid=True,
            axes={
                axis: AxisStatus(
                    axis=axis,
                    connected=True,
                    parameter_valid=True,
                    dpos=-5.0 if axis == 0 else 0.0,
                    enabled=True,
                    soft_min_position=-200_000_000.0,
                    soft_max_position=200_000_000.0,
                )
                for axis in (0, 1)
            },
        )

        window._on_xy_dialog_snapshot(snapshot)

        assert window.xy_safe_x_min.value() == -200_000_000.0
        assert window.xy_safe_x_max.value() == 200_000_000.0
        assert window.btn_start_survey.isEnabled()
        assert window.xy_status._raw_text == "XY: 已连接"
        assert "X=-5 mm" in window.xy_realtime_position.text()
    finally:
        window.close()
        app.processEvents()


def test_xy_section_title_opens_dialog_and_light_theme_keeps_readable_text() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    MainWindow = _import_main_window_without_update_file()
    window = MainWindow()
    try:
        window._theme = "light"
        window._apply_style()
        palette = window._theme_palette()

        assert window.xy_status._text_color == palette["text"]
        assert f"color: {palette['text']}" in window.styleSheet()
        assert not window.xy_overview_dialog.isVisible()
        assert not window.xy_settings_dialog.isVisible()
        QTest.mouseClick(window.btn_xy_section_title, Qt.MouseButton.LeftButton)
        app.processEvents()
        assert window.xy_overview_dialog.isVisible()
        assert not window.xy_settings_dialog.isVisible()

        window.xy_overview_dialog.close()
        QTest.mouseClick(window.btn_xy_stage_control, Qt.MouseButton.LeftButton)
        app.processEvents()
        assert window.xy_settings_dialog.isVisible()
        assert not window.xy_overview_dialog.isVisible()
    finally:
        window.close()
        app.processEvents()


def test_xy_spatial_control_tab_stays_compact_when_dialog_is_tall() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    MainWindow = _import_main_window_without_update_file()
    window = MainWindow()
    try:
        spatial_page = window.xy_settings_dialog.tabs.widget(3)
        window.xy_settings_dialog.resize(1200, 1000)
        window.xy_settings_dialog.tabs.setCurrentWidget(spatial_page)
        window.xy_settings_dialog.show()
        app.processEvents()

        layout = spatial_page.layout()
        assert layout.rowStretch(4) == 1
        assert window.xy_safe_y_min.y() - window.xy_safe_x_min.y() < 80
        assert window.btn_xy_calibrate.y() - window.xy_safe_y_min.y() < 80
    finally:
        window.close()
        app.processEvents()
