from __future__ import annotations

import csv
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from grab_app.camera import CameraFrame
from grab_app.services.scanner import ScanConfig, ScanResult
from grab_app.services.spatial_scan import (
    SpatialAcquisitionConfig,
    SpatialScanWorker,
    SurveyConfig,
)
from grab_app.spatial import (
    SafetyLimits,
    SpatialRect,
    default_calibration,
    plan_center_scan,
    plan_tiles,
)


class FakeCamera:
    initialized = True

    def __init__(self) -> None:
        self.count = 0
        self.trigger_modes: list[int] = []
        self.fresh_requests: list[tuple[int, int, int]] = []

    def set_output_format_8bit(self) -> None: pass
    def set_output_format_12bit_packed(self) -> None: pass
    def apply_quantitative_profile(self) -> dict[str, object]: return {}
    def set_trigger_mode(self, mode: int) -> None: self.trigger_modes.append(mode)

    def soft_trigger_and_grab_sample(self, timeout_ms: int) -> CameraFrame:
        self.count += 1
        return CameraFrame(np.full((8, 10), self.count, np.uint8), self.count, float(self.count))

    def capture_barrier(self) -> int:
        return self.count

    def wait_for_fresh_sample(
        self, after_capture_count: int, *, discard_frames: int, timeout_ms: int
    ) -> CameraFrame:
        self.fresh_requests.append((after_capture_count, discard_frames, timeout_ms))
        self.count = after_capture_count + discard_frames + 1
        return CameraFrame(
            np.full((8, 10), self.count, np.uint8),
            self.count,
            float(self.count),
            is_trigger_frame=False,
            sdk_timestamp_01ms=1000 + self.count,
        )


class FakeStage:
    connected = True

    def __init__(self) -> None:
        self.moves: list[tuple[float, float]] = []
        self.stops = 0
        self.position = (0.0, 0.0)

    def move_absolute_blocking(self, x_mm: float, y_mm: float, **_: object) -> tuple[float, float]:
        self.moves.append((x_mm, y_mm))
        self.position = (x_mm, y_mm)
        return x_mm, y_mm

    def refresh_status(self) -> object:
        return SimpleNamespace(
            axes={
                0: SimpleNamespace(dpos=self.position[0]),
                1: SimpleNamespace(dpos=self.position[1]),
            }
        )

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


class ReportingScanner(FakeScanner):
    def __init__(self, *, corrected: bool = False) -> None:
        super().__init__()
        self.corrected = corrected

    def run_sync(self, config: ScanConfig) -> ScanResult:
        result = super().run_sync(config)
        round_dir = result.folder / ("raw/Round_01" if self.corrected else "Round_01")
        round_dir.mkdir(parents=True)
        image_path = round_dir / "img_0001.tiff"
        image_path.write_bytes(b"raw")
        with (round_dir / "scan_log.csv").open(
            "w", encoding="utf-8-sig", newline=""
        ) as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "step", "target_um", "actual_um", "filename",
                    "capture_count", "captured_at",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "step": 1,
                    "target_um": 90.0,
                    "actual_um": 90.02,
                    "filename": image_path.name,
                    "capture_count": 7,
                    "captured_at": 123.5,
                }
            )
        if self.corrected:
            corrected_dir = result.folder / "corrected/Round_01"
            corrected_dir.mkdir(parents=True)
            (corrected_dir / image_path.name).write_bytes(b"corrected")
        saved_images = 2 if self.corrected else 1
        return ScanResult(
            result.folder, completed_images=1, stopped=False,
            saved_images=saved_images,
        )


def _plan(rect: SpatialRect):
    calibration = default_calibration(0.48)
    return calibration, plan_tiles(
        rect,
        (1280, 1024),
        calibration,
        0.1,
        safety_limits=SafetyLimits(0, 20, 0, 20),
    )


def _worker(camera: FakeCamera, scanner: FakeScanner, stage: FakeStage, tiles: list[int]):
    return SpatialScanWorker(
        camera, scanner, stage,
        lambda *_: None,
        lambda placement, *_: tiles.append(placement.sequence),
        lambda *_: None,
    )


def test_survey_uses_continuous_fresh_frames_and_keeps_trigger_mode(tmp_path: Path) -> None:
    calibration, plan = _plan(SpatialRect(1.0, 1.0, 1.5, 1.4))
    camera, scanner, stage, tiles = FakeCamera(), FakeScanner(), FakeStage(), []
    worker = _worker(camera, scanner, stage, tiles)

    result = worker._run_survey(
        SurveyConfig(plan, tmp_path, extension="tiff", bit_depth=8, settle_ms=0, calibration=calibration)
    )

    assert result.completed_tiles == plan.tile_count
    assert len(stage.moves) == plan.tile_count
    assert camera.count == plan.tile_count * 2
    assert tiles == list(range(plan.tile_count))
    assert camera.trigger_modes == [0, 0]
    assert camera.fresh_requests == [
        (index * 2, 1, 2000) for index in range(plan.tile_count)
    ]
    assert (result.folder / "job.json").exists()
    assert (result.folder / "survey" / "tile_index.csv").exists()
    report = (result.folder / "概览扫描报告.txt").read_text(encoding="utf-8")
    assert "任务状态: 已完成" in report
    assert "XY 路径: 蛇形-行" in report
    assert f"网格: {plan.rows} 行 × {plan.columns} 列" in report
    assert "标定矩阵 pixel = matrix @ DPOS + offset" in report
    assert "瓦片明细" in report
    assert "目标X(mm)" in report
    assert "实际X(mm)" in report
    assert "survey/tiles/tile_r0000_c0000.tiff" in report


def test_survey_saves_actual_dpos_as_captured_view_center(tmp_path: Path) -> None:
    calibration, plan = _plan(SpatialRect(1.0, 1.0, 1.2, 1.2))

    class FeedbackStage(FakeStage):
        def move_absolute_blocking(
            self, x_mm: float, y_mm: float, **_: object
        ) -> tuple[float, float]:
            self.moves.append((x_mm, y_mm))
            self.position = (x_mm + 0.001, y_mm - 0.002)
            return self.position

        def refresh_status(self) -> object:
            # 验证保存和拼图使用稳定等待后的再次读回值，而非 move 返回值。
            return SimpleNamespace(
                axes={
                    0: SimpleNamespace(dpos=self.position[0] + 0.0002),
                    1: SimpleNamespace(dpos=self.position[1] - 0.0003),
                }
            )

    camera, scanner, stage = FakeCamera(), FakeScanner(), FeedbackStage()
    actual_centers: list[tuple[float, float]] = []
    worker = SpatialScanWorker(
        camera,
        scanner,
        stage,
        lambda *_: None,
        lambda _placement, _sample, actual: actual_centers.append(actual),
        lambda *_: None,
    )

    result = worker._run_survey(
        SurveyConfig(
            plan, tmp_path, extension="tiff", bit_depth=8,
            settle_ms=0, calibration=calibration,
        )
    )

    with (result.folder / "survey" / "tile_index.csv").open(
        "r", encoding="utf-8", newline=""
    ) as handle:
        row = next(csv.DictReader(handle))
    expected = (
        plan.placements[0].target.x_mm + 0.0012,
        plan.placements[0].target.y_mm - 0.0023,
    )
    assert actual_centers[0] == pytest.approx(expected)
    assert float(row["actual_x_mm"]) == pytest.approx(expected[0])
    assert float(row["actual_y_mm"]) == pytest.approx(expected[1])
    assert int(row["capture_barrier_count"]) == 0
    assert int(row["capture_count"]) == 2
    assert int(row["sdk_timestamp_01ms"]) == 1002
    assert row["is_trigger_frame"] == "False"


def test_spatial_acquisition_runs_one_pzt_scan_per_xy_tile(tmp_path: Path) -> None:
    _, plan = _plan(SpatialRect(1.0, 1.0, 2.0, 1.4))
    camera, scanner, stage, tiles = FakeCamera(), FakeScanner(), FakeStage(), []
    worker = _worker(camera, scanner, stage, tiles)
    pzt = ScanConfig(
        mode="normal", channel=0, start_um=0, end_um=1, step_um=0.5,
        stable_ms=0, repeats=1, trigger_mode="continuous", save_dir=tmp_path,
        prefix="img", extension="tiff", bit_depth=8,
    )

    result = worker._run_acquisition(SpatialAcquisitionConfig(plan, pzt, tmp_path, settle_ms=0))

    assert result.completed_tiles == plan.tile_count
    assert len(scanner.configs) == plan.tile_count
    assert len(stage.moves) == plan.tile_count
    assert all(config.save_dir.parent.name == "acquisition" for config in scanner.configs)
    assert all(config.trigger_mode == "soft" for config in scanner.configs)
    assert pzt.trigger_mode == "continuous"


def test_spatial_acquisition_writes_human_readable_3d_stitching_reports(
    tmp_path: Path,
) -> None:
    _, plan = _plan(SpatialRect(1.0, 1.0, 2.0, 1.4))
    scanner = ReportingScanner()
    worker = _worker(FakeCamera(), scanner, FakeStage(), [])
    pzt = ScanConfig(
        mode="center", channel=0, start_um=90, end_um=110, step_um=0.1,
        stable_ms=200, repeats=1, trigger_mode="continuous", save_dir=tmp_path,
        prefix="img", extension="tiff", bit_depth=8,
    )

    result = worker._run_acquisition(
        SpatialAcquisitionConfig(plan, pzt, tmp_path, settle_ms=0)
    )

    guide_path = result.folder / "三维拼接说明.txt"
    index_path = result.folder / "acquisition" / "三维拼接索引.csv"
    guide = guide_path.read_text(encoding="utf-8")
    with index_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert "XY 路径: 蛇形-行" in guide
    assert "PZT 扫描模式: 中心扫描" in guide
    assert f"索引图像数: {plan.tile_count}" in guide
    assert len(rows) == plan.tile_count
    assert [int(row["tile_sequence"]) for row in rows] == list(range(plan.tile_count))
    assert rows[0]["tile_sequence"] == "0"
    assert rows[0]["row"] == "0"
    assert rows[0]["column"] == "0"
    assert float(rows[0]["actual_x_mm"]) == pytest.approx(plan.placements[0].target.x_mm)
    assert float(rows[0]["actual_y_mm"]) == pytest.approx(plan.placements[0].target.y_mm)
    assert float(rows[0]["actual_z_um"]) == pytest.approx(90.02)
    assert rows[0]["image_kind"] == "raw"
    assert rows[0]["image_relative_path"].endswith(
        "Scan-test/Round_01/img_0001.tiff"
    )
    assert (result.folder / rows[0]["image_relative_path"]).is_file()


def test_3d_stitching_index_distinguishes_raw_and_corrected_images(
    tmp_path: Path,
) -> None:
    _, plan = _plan(SpatialRect(1.0, 1.0, 1.2, 1.2))
    scanner = ReportingScanner(corrected=True)
    worker = _worker(FakeCamera(), scanner, FakeStage(), [])
    pzt = ScanConfig(
        mode="normal", channel=0, start_um=90, end_um=110, step_um=0.1,
        stable_ms=200, repeats=1, trigger_mode="continuous", save_dir=tmp_path,
        prefix="img", extension="tiff", bit_depth=8,
    )

    result = worker._run_acquisition(
        SpatialAcquisitionConfig(plan, pzt, tmp_path, settle_ms=0)
    )

    with (result.folder / "acquisition" / "三维拼接索引.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        rows = list(csv.DictReader(handle))
    assert [row["image_kind"] for row in rows] == ["raw", "corrected"]
    assert all((result.folder / row["image_relative_path"]).is_file() for row in rows)


def test_stop_sets_all_device_cancellation_paths() -> None:
    camera, scanner, stage, tiles = FakeCamera(), FakeScanner(), FakeStage(), []
    worker = _worker(camera, scanner, stage, tiles)
    worker.stop()
    assert scanner.stopped
    assert stage.stops == 1


def _even_row_center_plan():
    calibration = default_calibration(0.48)
    plan = plan_center_scan(
        -2.0, 2.0, -3.0, -2.7,
        (1280, 1024), calibration,
        route="serpentine",
        safety_limits=SafetyLimits(-10.0, 10.0, -10.0, 10.0),
    )
    assert plan.rows == 2
    return calibration, plan


def test_survey_stays_at_last_captured_tile(tmp_path: Path) -> None:
    calibration, plan = _even_row_center_plan()
    camera, scanner, stage, tiles = FakeCamera(), FakeScanner(), FakeStage(), []
    worker = _worker(camera, scanner, stage, tiles)

    worker._run_survey(
        SurveyConfig(
            plan, tmp_path, extension="tiff", bit_depth=8,
            settle_ms=0, calibration=calibration,
        )
    )

    assert stage.moves[0] == pytest.approx((-2.0, -3.0))
    assert stage.moves[-1] == pytest.approx(plan.placements[-1].target.as_tuple())
    assert len(stage.moves) == plan.tile_count


def test_spatial_acquisition_stays_at_last_captured_tile(
    tmp_path: Path,
) -> None:
    _, plan = _even_row_center_plan()
    camera, scanner, stage, tiles = FakeCamera(), FakeScanner(), FakeStage(), []
    worker = _worker(camera, scanner, stage, tiles)
    pzt = ScanConfig(
        mode="normal", channel=0, start_um=0, end_um=1, step_um=0.5,
        stable_ms=0, repeats=1, trigger_mode="soft", save_dir=tmp_path,
        prefix="img", extension="tiff", bit_depth=8,
    )

    worker._run_acquisition(
        SpatialAcquisitionConfig(plan, pzt, tmp_path, settle_ms=0)
    )

    assert stage.moves[0] == pytest.approx((-2.0, -3.0))
    assert stage.moves[-1] == pytest.approx(plan.placements[-1].target.as_tuple())
    assert len(stage.moves) == plan.tile_count


def test_stopped_final_pzt_tile_does_not_reposition_to_scan_end(tmp_path: Path) -> None:
    _, plan = _even_row_center_plan()

    class StopsOnLastScanner(FakeScanner):
        def run_sync(self, config: ScanConfig) -> ScanResult:
            result = super().run_sync(config)
            return ScanResult(
                result.folder,
                completed_images=result.completed_images,
                stopped=len(self.configs) == plan.tile_count,
                saved_images=result.saved_images,
            )

    camera, scanner, stage, tiles = FakeCamera(), StopsOnLastScanner(), FakeStage(), []
    worker = _worker(camera, scanner, stage, tiles)
    pzt = ScanConfig(
        mode="normal", channel=0, start_um=0, end_um=1, step_um=0.5,
        stable_ms=0, repeats=1, trigger_mode="soft", save_dir=tmp_path,
        prefix="img", extension="tiff", bit_depth=8,
    )

    result = worker._run_acquisition(
        SpatialAcquisitionConfig(plan, pzt, tmp_path, settle_ms=0)
    )

    assert result.stopped
    assert len(stage.moves) == plan.tile_count
    assert stage.moves[-1] == pytest.approx(plan.placements[-1].target.as_tuple())
