from __future__ import annotations

import numpy as np
import pytest

from grab_app.spatial import (
    SpatialCalibration,
    SpatialRect,
    StagePoint,
    default_calibration,
    plan_center_scan,
    plan_tiles,
)
from grab_app.spatial.planner import SafetyLimits, frame_footprint_mm


def test_default_1280x1024_frame_has_expected_physical_fov() -> None:
    assert frame_footprint_mm((1280, 1024), default_calibration()) == pytest.approx(
        (0.6144, 0.49152)
    )


def test_planner_rejects_runaway_fov_with_readable_expected_size() -> None:
    runaway = SpatialCalibration(((1.0, 0.0), (0.0, 1.0)), pixel_size_um=1000.0)

    with pytest.raises(ValueError, match=r"视场明显不合理.*0\.6144.*0\.49152"):
        plan_tiles(
            SpatialRect(0, 0, 1, 1),
            (1280, 1024),
            runaway,
            safety_limits=SpatialRect(-1000, -1000, 2000, 2000),
        )


def test_planner_generates_minimal_complete_serpentine_grid() -> None:
    plan = plan_tiles(
        SpatialRect(0, 0, 1, 1),
        (1000, 1000),
        default_calibration(),
        0.1,
        route="serpentine",
        safety_limits=SafetyLimits(-0.5, 1.5, -0.5, 1.5),
    )

    assert (plan.rows, plan.columns, plan.tile_count) == (3, 3, 9)
    assert plan.tile_size_mm == pytest.approx((0.48, 0.48))
    assert [p.column for p in plan.placements[:6]] == [0, 1, 2, 2, 1, 0]
    assert plan.placements[0].bounds.x_min_mm == pytest.approx(0)
    assert 1.0 <= plan.placements[-1].bounds.y_max_mm < 1.48
    assert [p.sequence for p in plan.placements] == list(range(9))


def test_planner_generates_column_first_serpentine_grid() -> None:
    plan = plan_tiles(
        SpatialRect(0, 0, 1, 1),
        (1000, 1000),
        default_calibration(),
        0.1,
        route="serpentine_column",
        safety_limits=SafetyLimits(-0.5, 1.5, -0.5, 1.5),
    )

    assert (plan.rows, plan.columns, plan.tile_count) == (3, 3, 9)
    assert [p.row for p in plan.placements[:6]] == [0, 1, 2, 2, 1, 0]
    assert [p.column for p in plan.placements[:6]] == [0, 0, 0, 1, 1, 1]


def test_unidirectional_keeps_same_column_order_on_each_row() -> None:
    plan = plan_tiles(
        SpatialRect(0, 0, 0.8, 0.8), (1000, 1000), default_calibration(),
        route="unidirectional", safety_limits=SpatialRect(-0.5, -0.5, 1.5, 1.5),
    )
    for row in range(plan.rows):
        assert [p.column for p in plan.placements if p.row == row] == list(range(plan.columns))


def test_planner_rejects_any_tile_whose_field_exceeds_safety_limits() -> None:
    with pytest.raises(ValueError, match="完整视场超出安全限位"):
        plan_tiles(
            SpatialRect(0, 0, 1, 1), (1000, 1000), default_calibration(),
            safety_limits=SpatialRect(0.1, 0.1, 0.9, 0.9),
        )


def test_center_scan_supports_single_column_and_negative_user_coordinates() -> None:
    plan = plan_center_scan(
        -1.0, -1.0, -2.0, 0.0,
        (1280, 1024), default_calibration(),
        safety_limits=SafetyLimits(-5.0, 15.0, -5.0, 15.0),
    )

    assert plan.columns == 1
    assert plan.rows > 1
    assert {item.target.x_mm for item in plan.placements} == {-1.0}
    assert plan.placements[0].target.y_mm == -2.0
    assert 0.0 <= plan.placements[-1].target.y_mm < plan.tile_size_mm[1]


def test_center_scan_supports_single_row() -> None:
    plan = plan_center_scan(
        1.0, 2.0, 3.0, 3.0,
        (1280, 1024), default_calibration(),
        safety_limits=SafetyLimits(0.0, 20.0, 0.0, 20.0),
    )

    assert plan.rows == 1
    assert plan.columns > 1
    assert {item.target.y_mm for item in plan.placements} == {3.0}


def test_center_scan_supports_column_first_serpentine_route() -> None:
    plan = plan_center_scan(
        0.0, 1.0, 0.0, 1.0,
        (1000, 1000), default_calibration(),
        route="serpentine_column",
        safety_limits=SafetyLimits(-1.0, 2.0, -1.0, 2.0),
    )

    first_column = plan.placements[:plan.rows]
    second_column = plan.placements[plan.rows:2 * plan.rows]
    assert [item.column for item in first_column] == [0] * plan.rows
    assert [item.row for item in first_column] == list(range(plan.rows))
    assert [item.column for item in second_column] == [1] * plan.rows
    assert [item.row for item in second_column] == list(reversed(range(plan.rows)))


def test_center_scan_keeps_start_and_allows_last_capture_to_cover_past_end() -> None:
    plan = plan_center_scan(
        -2.0, 2.0, -3.0, -2.7,
        (1280, 1024), default_calibration(),
        route="serpentine",
        safety_limits=SafetyLimits(-10.0, 10.0, -10.0, 10.0),
    )

    assert plan.rows == 2
    assert plan.start_target.as_tuple() == pytest.approx((-2.0, -3.0))
    assert plan.end_target.x_mm >= 2.0
    assert plan.end_target.y_mm >= -2.7
    assert plan.placements[-1].target.as_tuple() != pytest.approx(plan.end_target.as_tuple())


def test_center_scan_reports_actual_per_axis_overlap_for_ten_and_five_percent() -> None:
    limits = SafetyLimits(-10.0, 10.0, -10.0, 10.0)
    plan_10 = plan_center_scan(
        1.902, 4.202, 0.4018, -0.2782,
        (1280, 1024), default_calibration(0.48), 0.10,
        safety_limits=limits,
    )
    plan_5 = plan_center_scan(
        1.902, 4.202, 0.4018, -0.2782,
        (1280, 1024), default_calibration(0.48), 0.05,
        safety_limits=limits,
    )

    assert (plan_10.columns, plan_10.rows) == (6, 3)
    assert (plan_5.columns, plan_5.rows) == (5, 3)
    assert plan_10.effective_overlap_xy == pytest.approx((0.10, 0.10))
    assert plan_5.effective_overlap_xy == pytest.approx((0.05, 0.05))


def test_tile_bounds_can_use_actual_dpos_as_captured_view_center() -> None:
    plan = plan_center_scan(
        0.0, 0.0, 0.0, 0.0,
        (1280, 1024), default_calibration(),
        safety_limits=SafetyLimits(-10.0, 10.0, -10.0, 10.0),
    )
    placement = plan.placements[0]

    actual_bounds = placement.bounds_at_center(StagePoint(0.125, -0.25))

    assert actual_bounds.center.as_tuple() == pytest.approx((0.125, -0.25))
    assert actual_bounds.width_mm == pytest.approx(placement.bounds.width_mm)
    assert actual_bounds.height_mm == pytest.approx(placement.bounds.height_mm)


def test_center_scan_uses_controller_limits_without_camera_half_frame_inset() -> None:
    plan = plan_center_scan(
        0.0, 0.0, 20.0, 20.0,
        (1280, 1024), default_calibration(),
        safety_limits=SafetyLimits(0.0, 20.0, 0.0, 20.0),
    )

    assert plan.placements[0].target.as_tuple() == pytest.approx((0.0, 20.0))
    assert plan.placements[0].bounds.x_min_mm < 0.0


def test_rotated_calibration_uses_conservative_coverage_but_full_bounds_for_safety() -> None:
    angle = np.deg2rad(20)
    scale = 1000 / 0.48
    calibration = SpatialCalibration((
        (scale * np.cos(angle), -scale * np.sin(angle)),
        (scale * np.sin(angle), scale * np.cos(angle)),
    ))
    with pytest.raises(ValueError, match="完整视场超出安全限位"):
        plan_tiles(
            SpatialRect(0, 0, 0.3, 0.3), (1000, 1000), calibration,
            safety_limits=SpatialRect(0, 0, 0.3, 0.3),
        )


@pytest.mark.parametrize("overlap", [0.049, 0.101])
def test_planner_validates_overlap(overlap: float) -> None:
    with pytest.raises(ValueError, match="overlap"):
        plan_tiles(
            SpatialRect(0, 0, 1, 1), (1000, 1000), default_calibration(), overlap,
            safety_limits=SpatialRect(-1, -1, 2, 2),
        )
