from __future__ import annotations

import threading

import pytest

from grab_app.xy_stage import (
    AxisProfile,
    DeviceProfile,
    FakeMotionSdk,
    FmcSdk,
    MotionTimeoutError,
    SafetyViolation,
    SdkThreadError,
    XYStage,
    XYStageProtocol,
    load_default_profile,
)


@pytest.fixture
def profile() -> DeviceProfile:
    return DeviceProfile(
        name="FMC01-02H + FMSXY100-20-20",
        baudrate=38400,
        poll_interval_ms=200,
        axes=tuple(
            AxisProfile(
                axis=axis,
                name=name,
                units=50000.0,
                accel=100.0,
                decel=100.0,
                home_speed=3.0,
                home_offset=0.0,
                min_position=0.0,
                max_position=20.0,
                max_speed=5.0,
                default_speed=0.5,
            )
            for axis, name in ((0, "X"), (1, "Y"))
        ),
    )


def test_connect_move_wait_and_snapshot(profile: DeviceProfile) -> None:
    sdk = FakeMotionSdk(profile)
    stage = XYStage(sdk, profile)

    connected = stage.connect("COM7")
    arrived = stage.move_absolute(3.25, 4.5, speed=1.0, wait=True)

    assert isinstance(stage, XYStageProtocol)
    assert connected.connected and connected.parameter_valid
    assert sdk.opened_port == "COM7"
    assert sdk.move_history == [(3.25, 4.5)]
    assert arrived.axes[0].dpos == 3.25
    assert arrived.axes[1].dpos == 4.5
    assert stage.snapshot is not stage.snapshot


def test_bundled_profile_matches_confirmed_device_parameters() -> None:
    profile = load_default_profile()

    assert profile.baudrate == 38400
    assert [axis.axis for axis in profile.axes] == [0, 1]
    assert all(axis.units == 50000.0 for axis in profile.axes)
    assert all((axis.min_position, axis.max_position) == (0.0, 20.0) for axis in profile.axes)


def test_controller_soft_limits_are_safety_gate(profile: DeviceProfile) -> None:
    sdk = FakeMotionSdk(profile)
    sdk.soft_limits[0] = (1.0, 10.0)
    stage = XYStage(sdk, profile)
    snapshot = stage.connect("COM1")

    assert snapshot.axes[0].soft_min_position == 1.0
    with pytest.raises(SafetyViolation, match="软限位"):
        stage.move_absolute(0.5, 2.0)
    assert sdk.move_history == []


def test_controller_large_limits_remain_the_coordinate_source(profile: DeviceProfile) -> None:
    sdk = FakeMotionSdk(profile)
    sdk.soft_limits = {
        0: (-200_000_000.0, 200_000_000.0),
        1: (-200_000_000.0, 200_000_000.0),
    }
    stage = XYStage(sdk, profile)

    snapshot = stage.connect("COM8")

    assert snapshot.parameter_valid
    assert snapshot.axes[0].soft_min_position == pytest.approx(-200_000_000.0)
    assert snapshot.axes[0].soft_max_position == pytest.approx(200_000_000.0)
    stage.move_absolute(-11.0, 2.0)
    assert sdk.move_history == [(-11.0, 2.0)]


def test_parameter_mismatch_keeps_connection_but_blocks_motion(profile: DeviceProfile) -> None:
    sdk = FakeMotionSdk(profile)
    sdk.parameter_overrides[(0, "units")] = 10000.0
    stage = XYStage(sdk, profile)

    snapshot = stage.connect("COM2")

    assert snapshot.connected
    assert not snapshot.parameter_valid
    assert "脉冲当量不匹配" in snapshot.fault_message
    with pytest.raises(SafetyViolation, match="参数尚未通过校验"):
        stage.move_absolute(1.0, 1.0)


def test_stop_all_attempts_both_axes_and_disconnect_closes(profile: DeviceProfile) -> None:
    sdk = FakeMotionSdk(profile)
    stage = XYStage(sdk, profile)
    stage.connect("COM3")

    stage.stop_all()
    stage.disconnect()

    assert sdk.stop_history == [0, 1, 0, 1]
    assert not stage.connected
    assert not stage.snapshot.connected


def test_wait_timeout_stops_both_axes(profile: DeviceProfile) -> None:
    sdk = FakeMotionSdk(profile)
    stage = XYStage(sdk, profile)
    stage.connect("COM4")
    sdk.idle = {0: False, 1: False}
    now = [0.0]
    stage._clock = lambda: now[0]
    stage._sleep = lambda delay: now.__setitem__(0, now[0] + delay)

    with pytest.raises(MotionTimeoutError):
        stage.wait_until_idle(timeout_s=0.5)

    assert sdk.stop_history[-2:] == [0, 1]


def test_real_sdk_rejects_cross_thread_calls() -> None:
    sdk = object.__new__(FmcSdk)
    sdk._owner_thread_id = threading.get_ident()
    errors: list[Exception] = []

    def call_from_another_thread() -> None:
        try:
            sdk._claim_sdk_thread()
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=call_from_another_thread)
    worker.start()
    worker.join()

    assert len(errors) == 1
    assert isinstance(errors[0], SdkThreadError)
