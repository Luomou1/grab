from __future__ import annotations

import threading
from pathlib import Path

import numpy as np

from grab_app.camera import CameraFrame
from grab_app.services.scanner import ScanConfig, ScanResult
from grab_app.services.spatial_scan import (
    SpatialAcquisitionConfig,
    SpatialScanWorker,
    SurveyConfig,
)
from grab_app.spatial import SafetyLimits, SpatialRect, default_calibration, plan_tiles


class FakeCamera:
    initialized = True

    def __init__(self) -> None:
        self.count = 0
        self.trigger_modes: list[int] = []

    def set_output_format_8bit(self) -> None: pass
    def set_output_format_12bit_packed(self) -> None: pass
    def apply_quantitative_profile(self) -> dict[str, object]: return {}
    def set_trigger_mode(self, mode: int) -> None: self.trigger_modes.append(mode)

    def soft_trigger_and_grab_sample(self, timeout_ms: int) -> CameraFrame:
        self.count += 1
        return CameraFrame(np.full((8, 10), self.count, np.uint8), self.count, float(self.count))


class FakeStage:
    connected = True

    def __init__(self) -> None:
        self.moves: list[tuple[float, float]] = []
        self.stops = 0

    def move_absolute_blocking(self, x_mm: float, y_mm: float, **_: object) -> tuple[float, float]:
        self.moves.append((x_mm, y_mm))
        return x_mm, y_mm

    def stop_all(self) -> None:
        self.stops += 1


class FakeScanner:
    def __init__(self) -> None:
        self.configs: list[ScanConfig] = []
        self.stopped = False

    @property
    def running(self) -> bool:
        return False

    def stop(self) -> None:
        self.stopped = True

    def save_frame(self, path: Path, frame: np.ndarray, bit_depth: int) -> None:
        path.write_bytes(frame.tobytes())

    def run_sync(self, config: ScanConfig) -> ScanResult:
        self.configs.append(config)
        folder = config.save_dir / "Scan-test"
        folder.mkdir(parents=True)
        return ScanResult(folder, completed_images=3, stopped=False, saved_images=3)


def _plan(rect: SpatialRect):
    calibration = default_calibration(0.48)
    return calibration, plan_tiles(
        rect,
        (1280, 1024),
        calibration,
        0.2,
        safety_limits=SafetyLimits(0, 20, 0, 20),
    )


def _worker(camera: FakeCamera, scanner: FakeScanner, stage: FakeStage, tiles: list[int]):
    return SpatialScanWorker(
        camera, scanner, stage,
        lambda *_: None,
        lambda placement, *_: tiles.append(placement.sequence),
        lambda *_: None,
    )


def test_survey_moves_captures_saves_and_restores_trigger(tmp_path: Path) -> None:
    calibration, plan = _plan(SpatialRect(1.0, 1.0, 1.5, 1.4))
    camera, scanner, stage, tiles = FakeCamera(), FakeScanner(), FakeStage(), []
    worker = _worker(camera, scanner, stage, tiles)

    result = worker._run_survey(
        SurveyConfig(plan, tmp_path, extension="tiff", bit_depth=8, settle_ms=0, calibration=calibration)
    )

    assert result.completed_tiles == plan.tile_count
    assert len(stage.moves) == plan.tile_count
    assert camera.count == plan.tile_count
    assert tiles == list(range(plan.tile_count))
    assert camera.trigger_modes == [1, 0]
    assert (result.folder / "job.json").exists()
    assert (result.folder / "survey" / "tile_index.csv").exists()


def test_spatial_acquisition_runs_one_pzt_scan_per_xy_tile(tmp_path: Path) -> None:
    _, plan = _plan(SpatialRect(1.0, 1.0, 2.0, 1.4))
    camera, scanner, stage, tiles = FakeCamera(), FakeScanner(), FakeStage(), []
    worker = _worker(camera, scanner, stage, tiles)
    pzt = ScanConfig(
        mode="normal", channel=0, start_um=0, end_um=1, step_um=0.5,
        stable_ms=0, repeats=1, trigger_mode="soft", save_dir=tmp_path,
        prefix="img", extension="tiff", bit_depth=8,
    )

    result = worker._run_acquisition(SpatialAcquisitionConfig(plan, pzt, tmp_path, settle_ms=0))

    assert result.completed_tiles == plan.tile_count
    assert len(scanner.configs) == plan.tile_count
    assert len(stage.moves) == plan.tile_count
    assert all(config.save_dir.parent.name == "acquisition" for config in scanner.configs)


def test_stop_sets_all_device_cancellation_paths() -> None:
    camera, scanner, stage, tiles = FakeCamera(), FakeScanner(), FakeStage(), []
    worker = _worker(camera, scanner, stage, tiles)
    worker.stop()
    assert scanner.stopped
    assert stage.stops == 1
