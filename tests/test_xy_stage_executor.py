from __future__ import annotations

import threading

import pytest

from grab_app.xy_stage import (
    FakeMotionSdk,
    MotionCancelledError,
    XYStage,
    XYStageExecutor,
    load_default_profile,
)


def _executor() -> tuple[XYStageExecutor, list[FakeMotionSdk]]:
    profile = load_default_profile()
    created: list[FakeMotionSdk] = []

    def factory() -> XYStage:
        sdk = FakeMotionSdk(profile)
        created.append(sdk)
        return XYStage(sdk, profile)

    return XYStageExecutor(stage_factory=factory), created


def test_executor_keeps_connection_move_and_stop_on_worker_thread() -> None:
    executor, created = _executor()
    try:
        snapshot = executor.connect("COM7")
        assert snapshot.connected
        assert executor.move_absolute_blocking(1.25, 2.5) == pytest.approx((1.25, 2.5))
        executor.stop_all()
        assert created[0].move_history == [(1.25, 2.5)]
        assert created[0].stop_history[-2:] == [0, 1]
    finally:
        executor.close()


def test_executor_rejects_pre_cancelled_move_and_stops_both_axes() -> None:
    executor, created = _executor()
    try:
        executor.connect("COM8")
        cancelled = threading.Event()
        cancelled.set()
        with pytest.raises(MotionCancelledError, match="已取消"):
            executor.move_absolute_blocking(1.0, 1.0, cancel_event=cancelled)
        assert created[0].stop_history[-2:] == [0, 1]
    finally:
        executor.close()
