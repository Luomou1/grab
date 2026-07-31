from __future__ import annotations

import importlib
import os
import sys
import types
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from grab_app.spatial import (
    RegistrationResult,
    SpatialRect,
    StagePoint,
    expected_adjacent_translation,
)
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


def test_main_window_builds_spatial_map_dialog_and_plans_tiles() -> None:
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

        assert not hasattr(window, "viewer_tabs")
        assert window.spatial_map.window() is window.spatial_map_dialog
        assert not window.spatial_map_dialog.isVisible()
        window._show_spatial_map_dialog()
        app.processEvents()
        assert window.spatial_map_dialog.isVisible()
        assert plan.tile_count >= 4
        assert window.spatial_map.model.map_size.width() > 0
        assert len(window.spatial_map.model.tiles) == plan.tile_count
        assert window.spatial_pixel_um.value() == 0.48
        assert window.spatial_pixel_um.text() == "0.48"
        assert window.spatial_overlap.minimum() == 5
        assert window.spatial_overlap.maximum() == 10
        assert window.spatial_overlap.value() == 10
        assert [
            window.spatial_route.itemText(index)
            for index in range(window.spatial_route.count())
        ] == ["蛇形-行", "蛇形-列", "单向"]
        assert window.spatial_route.currentText() == "蛇形-行"
        window.spatial_route.setCurrentText("蛇形-列")
        assert window._plan_spatial_rect(SpatialRect(1.0, 1.0, 2.0, 2.0)).route == (
            "serpentine_column"
        )
        assert window._plan_spatial_center_scan().route == "serpentine_column"
        assert window.btn_measure_spatial.text() == "测距"
        assert window.spatial_measurement_status.text() == "测距: 未测量"
        window._on_spatial_measurement_completed(0.003, -0.004, 0.005)
        assert window.spatial_measurement_status.text() == (
            "测距(约): ΔX=3.00 µm  ΔY=-4.00 µm  直线=5.00 µm"
        )
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
        spatial_labels = {
            label.text()
            for label in window.findChildren(QLabel)
            if label.objectName() == "fieldLabel"
        }
        assert {"X 起点", "X 终点", "Y 起点", "Y 终点"} <= spatial_labels
        assert "DPOS" in window.survey_x_start.toolTip()
        assert "视野中心" in window.xy_realtime_position.toolTip()
        assert window.show_tile_borders.isChecked()
        assert window.spatial_map.tile_borders_visible
        window.show_tile_borders.setChecked(False)
        assert not window.spatial_map.tile_borders_visible
    finally:
        window.camera.h_camera = None
        window.close()
        app.processEvents()


def test_starting_survey_keeps_live_preview_and_opens_map_dialog(
    tmp_path,
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    MainWindow = _import_main_window_without_update_file()
    window = MainWindow()

    class FakeWorker:
        def __init__(self) -> None:
            self.config = None

        def start_survey(self, config, _done) -> None:
            self.config = config

        def stop(self) -> None:
            pass

    worker = FakeWorker()
    try:
        window.camera.h_camera = object()
        window.camera.width = 1280
        window.camera.height = 1024
        window.xy_stage = SimpleNamespace(connected=True, close=lambda: None)
        window.save_path.setText(str(tmp_path))
        window._new_spatial_worker = lambda: worker
        window.preview_timer.start()

        window._start_spatial_survey()
        app.processEvents()

        assert worker.config is not None
        assert window.preview_timer.isActive()
        assert window.spatial_map_dialog.isVisible()
        assert window.spatial_map_dialog.scan_running
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


def test_sample_map_draws_lower_absolute_dpos_y_above_higher_y() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    MainWindow = _import_main_window_without_update_file()
    window = MainWindow()
    try:
        window.camera.h_camera = object()
        window.camera.width = 1280
        window.camera.height = 1024
        window.xy_safe_y_min.setValue(-1.0)
        window.survey_x_start.setValue(1.0)
        window.survey_x_end.setValue(2.0)
        window.survey_y_start.setValue(-0.4)
        window.survey_y_end.setValue(0.2)
        plan = window._plan_spatial_center_scan()
        window._prepare_spatial_map(plan)

        start = window._stage_point_to_map(
            plan, plan.start_target.x_mm, plan.start_target.y_mm
        )
        end = window._stage_point_to_map(
            plan, plan.end_target.x_mm, plan.end_target.y_mm
        )

        assert plan.start_target.y_mm == pytest.approx(-0.4)
        assert plan.start_target.y_mm < plan.end_target.y_mm
        assert start.y() < end.y()
    finally:
        window.camera.h_camera = None
        window.close()
        app.processEvents()


def test_horizontal_mirror_checkbox_reverses_default_camera_correction() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    MainWindow = _import_main_window_without_update_file()
    window = MainWindow()
    calls: list[tuple[int, bool]] = []
    try:
        window.camera.h_camera = object()
        window.camera.set_mirror = lambda direction, enabled: calls.append(
            (direction, enabled)
        )

        assert not window.h_mirror.isChecked()
        window._apply_camera_orientation()
        assert calls[-1] == (0, True)

        window.h_mirror.setChecked(True)
        app.processEvents()
        assert calls[-1] == (0, False)

        window.h_mirror.setChecked(False)
        app.processEvents()
        assert calls[-1] == (0, True)
    finally:
        window.camera.h_camera = None
        window.close()
        app.processEvents()


def test_open_camera_applies_horizontal_correction_with_unchecked_ui() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    MainWindow = _import_main_window_without_update_file()
    window = MainWindow()

    class FakeCamera:
        initialized = False
        width = 1280
        height = 1024

        def __init__(self) -> None:
            self.mirror_calls: list[tuple[int, bool]] = []

        def open(self) -> None:
            self.initialized = True

        def set_mirror(self, direction: int, enabled: bool) -> None:
            self.mirror_calls.append((direction, enabled))

        def close(self) -> None:
            self.initialized = False

    camera = FakeCamera()
    try:
        window.camera = camera
        window._refresh_camera_capabilities = lambda: None
        window._apply_current_bit_depth = lambda: None

        window._open_camera()

        assert not window.h_mirror.isChecked()
        assert camera.mirror_calls == [(0, True)]
    finally:
        window.close()
        app.processEvents()


def test_live_mosaic_uses_effective_overlap_and_dpos_anchored_correction(
    monkeypatch,
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    MainWindow = _import_main_window_without_update_file()
    module = sys.modules[MainWindow.__module__]
    window = MainWindow()

    class CaptureComposer:
        def __init__(self, shape: tuple[int, int]) -> None:
            self.shape = shape
            self.origins: list[tuple[float, float]] = []

        def add_tile(self, _tile, origin, *, quality=1.0) -> bool:
            self.origins.append((float(origin[0]), float(origin[1])))
            return True

        def image(self, dtype=np.dtype(np.uint8)) -> np.ndarray:
            return np.zeros(self.shape, dtype=dtype)

    registration_calls: list[tuple[str, float]] = []

    def fake_registration(reference, moving, *, direction, overlap, min_confidence=0.1):
        registration_calls.append((direction, overlap))
        expected_x, expected_y = expected_adjacent_translation(
            reference.shape[:2], direction, overlap
        )
        return RegistrationResult(expected_x + 2.0, expected_y + 1.0, 0.9, True)

    try:
        window.camera.h_camera = object()
        window.camera.width = 1280
        window.camera.height = 1024
        window.xy_safe_y_min.setValue(-1.0)
        window.survey_x_start.setValue(1.902)
        window.survey_x_end.setValue(3.902)
        window.survey_y_start.setValue(0.4018)
        window.survey_y_end.setValue(-0.2782)
        plan = window._plan_spatial_center_scan()
        assert (plan.rows, plan.columns) == (3, 5)
        window._survey_plan = plan
        window._prepare_spatial_map(plan)
        composer = CaptureComposer(window._spatial_map_shape)
        window._spatial_composer = composer
        monkeypatch.setattr(module, "estimate_adjacent_translation", fake_registration)
        assert window.spatial_effective_overlap.text() == (
            "有效重叠: X=10.00%  Y=10.00%"
        )
        frame = np.arange(80 * 100, dtype=np.uint8).reshape(80, 100)
        sample = SimpleNamespace(frame=frame)

        for placement in plan.placements:
            window._on_spatial_tile(
                placement, sample, placement.target.as_tuple()
            )

        effective_x, effective_y = plan.effective_overlap_xy
        assert registration_calls
        for direction, overlap in registration_calls:
            expected = effective_x if direction in {"left", "right"} else effective_y
            assert overlap == pytest.approx(expected)
        last = plan.placements[-1]
        anchored_rect = window._stage_rect_to_map(
            plan, last.bounds_at_center(StagePoint(*last.target.as_tuple()))
        )
        expected_origin = (
            anchored_rect.x() + 2.0 * anchored_rect.width() / frame.shape[1],
            anchored_rect.y() + 1.0 * anchored_rect.height() / frame.shape[0],
        )
        assert composer.origins[-1] == pytest.approx(expected_origin)
    finally:
        window.camera.h_camera = None
        window.close()
        app.processEvents()
