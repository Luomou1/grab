from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from grab_app.ui.spatial_map import SpatialMapModel, SpatialMapWidget, SpatialTile


@pytest.fixture(scope="module")
def app() -> QApplication:
    instance = QApplication.instance() or QApplication([])
    return instance


def test_pixel_rect_is_normalized_and_clamped_to_map_boundaries() -> None:
    model = SpatialMapModel(100, 80)

    assert model.clamp_pixel_rect(QRect(-10, -5, 30, 25)) == QRect(0, 0, 20, 20)
    assert model.clamp_pixel_rect(QRect(90, 70, 30, 30)) == QRect(90, 70, 10, 10)
    assert model.clamp_pixel_rect(QRect(60, 50, -20, -10)) == QRect(40, 40, 20, 10)


def test_physical_bounds_convert_map_roi_to_sample_coordinates() -> None:
    model = SpatialMapModel(200, 100)
    model.set_map_physical_bounds(-10.0, 20.0, 30.0, 40.0)

    sample_rect = model.map_pixel_rect_to_sample(QRect(50, 25, 100, 50))

    assert sample_rect == pytest.approx((0.0, 25.0, 20.0, 10.0))


def test_affine_transform_converts_rotated_map_roi_to_axis_aligned_sample_rect() -> None:
    model = SpatialMapModel(100, 100)
    model.set_world_to_map_transform((0.0, 2.0, -2.0, 0.0, 100.0, 0.0))

    sample_rect = model.map_pixel_rect_to_sample(QRect(20, 20, 40, 20))

    assert sample_rect == pytest.approx((10.0, 20.0, 10.0, 20.0))


def test_widget_state_updates_are_device_independent(app: QApplication) -> None:
    widget = SpatialMapWidget()
    widget.set_map_size(100, 80)
    widget.set_tile_states(
        [
            SpatialTile("0", QRect(0, 0, 50, 40), "pending"),
            ("1", (50, 0, 50, 40), "scanning"),
        ]
    )
    widget.update_tile_state("0", "complete")
    widget.set_route([(5.0, 6.0), (20.0, 30.0)])
    widget.set_current_scan_point((20.0, 30.0))

    assert widget.model.tiles["0"].status == "complete"
    assert widget.model.tiles["1"].status == "scanning"
    assert [(point.x(), point.y()) for point in widget.model.route] == [(5.0, 6.0), (20.0, 30.0)]
    assert widget.model.current_scan_point is not None
    assert (widget.model.current_scan_point.x(), widget.model.current_scan_point.y()) == (20.0, 30.0)


def test_mouse_drag_emits_map_pixel_roi_at_boundaries(app: QApplication) -> None:
    widget = SpatialMapWidget()
    widget.resize(220, 180)
    widget.set_map_size(100, 80)
    widget.set_selection_enabled(True)
    widget.show()
    app.processEvents()
    emissions: list[tuple[int, int, int, int]] = []
    widget.roi_selected.connect(lambda x, y, width, height: emissions.append((x, y, width, height)))
    view = widget._map_view_rect()

    start = QPoint(round(view.left() + 1), round(view.top() + 1))
    end = QPoint(round(view.right() - 1), round(view.bottom() - 1))
    QTest.mousePress(widget, Qt.LeftButton, pos=start)
    QTest.mouseMove(widget, end)
    QTest.mouseRelease(widget, Qt.LeftButton, pos=end)

    assert emissions == [(0, 0, 100, 80)]


def test_cancel_selection_clears_roi_and_emits_signal(app: QApplication) -> None:
    widget = SpatialMapWidget()
    widget.set_map_size(100, 80)
    widget.set_selected_roi((10, 12, 20, 24))
    emissions: list[bool] = []
    widget.selection_cancelled.connect(lambda: emissions.append(True))

    widget.cancel_selection()

    assert widget.selected_roi is None
    assert emissions == [True]
