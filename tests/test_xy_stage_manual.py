from __future__ import annotations

import threading
import time

import pytest

from grab_app.xy_stage import (
    ArcDefinition,
    ArcMove,
    CoordinateMode,
    FakeMotionSdk,
    FmcSdk,
    LineMove,
    Point2D,
    PositionTriggerConfig,
    SafetyViolation,
    XYStage,
    XYStageExecutor,
    load_default_profile,
)


def _stage() -> tuple[XYStage, FakeMotionSdk]:
    profile = load_default_profile()
    sdk = FakeMotionSdk(profile)
    stage = XYStage(sdk, profile)
    stage.connect("COM7")
    return stage, sdk


def test_manual_single_axis_controls_and_dpos_zero() -> None:
    stage, sdk = _stage()
    sdk.soft_limits[0] = (-5.0, 15.0)
    stage._last_limit_refresh_at = 0.0
    stage.refresh_status()

    assert not stage.set_axis_enabled(0, False).axes[0].enabled
    assert stage.set_axis_enabled(0, True).axes[0].enabled
    stage.set_axis_speed(0, 1.25)
    stage.move_axis_absolute(0, 2.0)
    stage.move_axis_relative(0, 1.5, speed=0.75)
    zeroed = stage.zero_axis(0)

    assert sdk.speeds[0] == pytest.approx(0.75)
    assert sdk.axis_move_history == [("absolute", 0, 2.0), ("relative", 0, 1.5)]
    assert zeroed.axes[0].dpos == 0.0
    assert zeroed.axes[0].mpos == 0.0
    assert zeroed.axes[0].soft_min_position == pytest.approx(-5.0)
    assert zeroed.axes[0].soft_max_position == pytest.approx(15.0)

    moved_negative = stage.move_axis_absolute(0, -1.0)
    assert moved_negative.axes[0].dpos == pytest.approx(-1.0)

    sdk.axis_status[0] = 0x100
    cleared = stage.clear_axis_error(0)
    assert cleared.axes[0].axis_status == 0


def test_real_sdk_manual_methods_use_confirmed_vendor_names_and_cancel_modes() -> None:
    sdk = object.__new__(FmcSdk)
    calls: list[tuple[object, ...]] = []
    bit_calls: list[tuple[int, bool]] = []
    sdk._call = lambda name, *args: calls.append((name, *args))
    sdk._modbus_set_bit = lambda address, enabled: bit_calls.append((address, enabled))

    sdk.home(1)
    sdk.abort_home(1)
    sdk.stop(1)
    sdk.move_relative(0, 1.5)
    sdk.move_absolute(0, 2.5)
    sdk.jog(0, -1)
    sdk.zero(0)

    assert bit_calls == [(100, True)]
    assert calls[0] == ("ZAux_Direct_Single_Cancel", 1, 2)
    assert calls[1] == ("ZAux_Direct_Single_Cancel", 1, 3)
    assert [call[0] for call in calls[2:]] == [
        "ZAux_Direct_Single_Move",
        "ZAux_Direct_Single_MoveAbs",
        "ZAux_Direct_Single_Vmove",
        "ZAux_Direct_SetMpos",
        "ZAux_Direct_SetDpos",
    ]


def test_home_cancel_and_jog_stop_have_distinct_cancel_modes() -> None:
    stage, sdk = _stage()
    sdk.auto_complete_home = False

    homing = stage.home_axis(1)
    assert homing.axes[1].home_in_progress
    assert sdk.home_history == [1]

    cancelled = stage.cancel_home(1)
    assert not cancelled.axes[1].home_in_progress
    assert sdk.abort_home_history == [1]

    jogging = stage.start_jog(1, -1, speed=0.4)
    assert jogging.axes[1].running
    stopped = stage.stop_axis(1)
    assert stopped.axes[1].idle
    assert sdk.jog_history == [(1, -1)]
    assert sdk.stop_history[-1] == 1


def test_manual_move_rejects_soft_limit_and_active_limit_direction() -> None:
    stage, sdk = _stage()

    with pytest.raises(SafetyViolation, match="软限位"):
        stage.move_axis_relative(0, 21.0)
    sdk.axis_status[0] = 0x10
    with pytest.raises(SafetyViolation, match="正限位"):
        stage.start_jog(0, 1)

    assert sdk.axis_move_history == []
    assert sdk.jog_history == []


def test_axisstatus_soft_limit_is_recoverable_only_in_opposite_direction() -> None:
    stage, sdk = _stage()
    sdk.axis_status[0] = 0x400

    with pytest.raises(SafetyViolation, match="负限位"):
        stage.start_jog(0, -1)

    snapshot = stage.start_jog(0, 1)

    assert sdk.jog_history == [(0, 1)]
    assert snapshot.axes[0].negative_soft_limit
    assert not snapshot.axes[0].hard_fault


def test_spatial_double_axis_move_uses_controller_defined_negative_limits() -> None:
    stage, sdk = _stage()
    sdk.soft_limits = {0: (-10.0, 10.0), 1: (-10.0, 10.0)}
    stage._last_limit_refresh_at = 0.0
    stage.refresh_status()
    stage.move_absolute(5.0, 5.0)
    stage.zero_axis(0)
    stage.zero_axis(1)

    moved = stage.move_absolute(-2.0, -1.0)

    assert moved.axes[0].dpos == pytest.approx(-2.0)
    assert moved.axes[1].dpos == pytest.approx(-1.0)
    assert moved.axes[0].soft_min_position == pytest.approx(-10.0)
    assert moved.axes[1].soft_min_position == pytest.approx(-10.0)


def test_external_zero_does_not_invent_soft_limit_side_effects() -> None:
    stage, sdk = _stage()
    sdk.positions[0] = 5.0
    sdk.motor_positions[0] = 5.0
    sdk.zero(0)
    stage._last_limit_refresh_at = 0.0

    snapshot = stage.refresh_status()

    assert snapshot.axes[0].dpos == pytest.approx(0.0)
    assert snapshot.axes[0].soft_min_position == pytest.approx(0.0)
    assert snapshot.axes[0].soft_max_position == pytest.approx(20.0)


def test_confirmed_interpolation_trigger_and_continuous_path_apis() -> None:
    profile = load_default_profile()
    created: list[FakeMotionSdk] = []

    def factory() -> XYStage:
        sdk = FakeMotionSdk(profile)
        created.append(sdk)
        return XYStage(sdk, profile)

    executor = XYStageExecutor(stage_factory=factory)
    try:
        executor.connect("COM9")
        executor.interpolate_line(
            LineMove(Point2D(1.0, 1.0), CoordinateMode.ABSOLUTE, 0.8)
        )
        executor.interpolate_arc(
            ArcMove(
                end=Point2D(3.0, 1.0),
                auxiliary=Point2D(2.0, 1.0),
                coordinate_mode=CoordinateMode.ABSOLUTE,
                definition=ArcDefinition.CENTER,
                direction=1,
                speed=0.5,
            )
        )
        executor.configure_position_trigger(
            PositionTriggerConfig(axis=0, positions=(3.0, 4.0, 5.0))
        )
        executor.stop_position_trigger(0)
        arrived = executor.run_linear_path_blocking(
            (Point2D(4.0, 2.0), Point2D(5.0, 3.0)), speed=0.6, window_size=2
        )

        sdk = created[0]
        assert sdk.line_history == [((1.0, 1.0), True)]
        assert sdk.arc_history[0][0] == "center"
        assert sdk.trigger_stop_history == [0]
        assert sdk.path_history == [((4.0, 2.0), (5.0, 3.0))]
        assert (arrived.axes[0].dpos, arrived.axes[1].dpos) == pytest.approx((5.0, 3.0))
        assert set(sdk.call_thread_ids) == {executor._thread.ident}
        assert threading.get_ident() not in sdk.call_thread_ids
    finally:
        executor.close()


def test_executor_periodically_refreshes_snapshot_on_its_only_sdk_thread() -> None:
    profile = load_default_profile()
    created: list[FakeMotionSdk] = []

    def factory() -> XYStage:
        sdk = FakeMotionSdk(profile)
        created.append(sdk)
        return XYStage(sdk, profile)

    executor = XYStageExecutor(stage_factory=factory)
    try:
        executor.connect("COM10")
        sdk = created[0]
        # 直接改变 Fake 的硬件状态，验证无需显式 refresh 也会形成新快照。
        sdk.positions[0] = 6.25
        deadline = time.monotonic() + 1.5
        while executor.snapshot.axes[0].dpos != pytest.approx(6.25):
            if time.monotonic() >= deadline:
                pytest.fail("executor 未在轮询周期内更新状态快照")
            time.sleep(0.02)
        assert set(sdk.call_thread_ids) == {executor._thread.ident}
    finally:
        executor.close()
