from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Protocol

from grab_app.camera import CameraController, CameraFrame
from grab_app.services.scanner import ScanConfig, ScanResult, ScanWorker
from grab_app.spatial.models import SpatialCalibration, SpatialRect, TilePlan, TilePlacement
from grab_app.spatial.storage import SpatialJobStorage


class StageMoveError(RuntimeError):
    """XY 位移台未能在规定时间内到达目标位置。"""


class SpatialStage(Protocol):
    @property
    def connected(self) -> bool: ...

    def move_absolute_blocking(
        self,
        x_mm: float,
        y_mm: float,
        *,
        timeout_seconds: float,
        cancel_event: threading.Event | None = None,
    ) -> tuple[float, float]: ...

    def stop_all(self) -> None: ...


ProgressCallback = Callable[[str, int, int, TilePlacement | None], None]
TileCallback = Callable[[TilePlacement, CameraFrame, tuple[float, float]], None]
DoneCallback = Callable[[object | None, Exception | None], None]


@dataclass(frozen=True)
class SurveyConfig:
    plan: TilePlan
    save_dir: Path
    prefix: str = "survey"
    extension: str = "tiff"
    bit_depth: int = 12
    settle_ms: int = 200
    timeout_seconds: float = 30.0
    calibration: SpatialCalibration | None = None


@dataclass(frozen=True)
class SpatialAcquisitionConfig:
    plan: TilePlan
    pzt_config: ScanConfig
    save_dir: Path
    settle_ms: int = 200
    timeout_seconds: float = 30.0


@dataclass(frozen=True)
class SpatialScanResult:
    folder: Path
    completed_tiles: int
    total_tiles: int
    stopped: bool
    survey: bool
    tile_results: tuple[ScanResult, ...] = ()


class SpatialScanWorker:
    """统一串行调度相机、XY 位移台和 PZT 扫描。

    XY 控制器只通过回调注入，避免采集层直接依赖厂家 DLL；回调内部应由
    唯一的位移台工作线程实现。这样既能在无硬件测试中使用模拟 stage，
    也不会让两个扫描线程同时拥有相机或位移台。
    """

    def __init__(
        self,
        camera: CameraController,
        scanner: ScanWorker,
        stage: SpatialStage,
        progress: ProgressCallback,
        tile_ready: TileCallback,
        message: Callable[[str], None],
        preview_provider: Callable[[], object | None] | None = None,
    ) -> None:
        self.camera = camera
        self.scanner = scanner
        self.stage = stage
        self.progress = progress
        self.tile_ready = tile_ready
        self.message = message
        self.preview_provider = preview_provider
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start_survey(self, config: SurveyConfig, done: DoneCallback) -> None:
        self._start(lambda: self._run_survey(config), done, "spatial-survey-worker")

    def start_acquisition(self, config: SpatialAcquisitionConfig, done: DoneCallback) -> None:
        self._start(lambda: self._run_acquisition(config), done, "spatial-acquisition-worker")

    def stop(self) -> None:
        self._stop.set()
        self.scanner.stop()
        try:
            self.stage.stop_all()
        except Exception as exc:
            self.message(f"停止 XY 位移台失败: {exc}")

    def wait(self, timeout: float | None = None) -> bool:
        """等待后台任务退出；返回是否已完全结束。"""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def _start(self, operation: Callable[[], SpatialScanResult], done: DoneCallback, name: str) -> None:
        with self._lock:
            if self.running:
                raise RuntimeError("空间扫描已在运行")
            if self.scanner.running:
                raise RuntimeError("PZT 扫描已在运行")
            if not self.stage.connected:
                raise RuntimeError("XY 位移台未连接")
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, args=(operation, done), name=name, daemon=True)
            self._thread.start()

    def _run(self, operation: Callable[[], SpatialScanResult], done: DoneCallback) -> None:
        try:
            done(operation(), None)
        except Exception as exc:
            done(None, exc)

    def _run_survey(self, config: SurveyConfig) -> SpatialScanResult:
        if not config.plan.placements:
            raise ValueError("概览扫描计划为空")
        folder = SpatialJobStorage.create_job_directory(config.save_dir, "Survey")
        storage = SpatialJobStorage(folder)
        storage.write_state(config.plan, filename="plan/route.json")
        if config.calibration is not None:
            storage.write_state(config.calibration, filename="calibration/stage_camera_affine.json")
        self._write_state(storage, "running", 0, len(config.plan.placements))
        self._prepare_camera(config.bit_depth)
        completed = 0
        try:
            for index, placement in enumerate(config.plan.placements, start=1):
                self._raise_if_stopped()
                actual = self.stage.move_absolute_blocking(
                    placement.target.x_mm,
                    placement.target.y_mm,
                    timeout_seconds=config.timeout_seconds,
                    cancel_event=self._stop,
                )
                self._wait_settle(config.settle_ms)
                self._raise_if_stopped()
                sample = self.camera.soft_trigger_and_grab_sample(2000)
                if sample is None:
                    raise RuntimeError(f"概览瓦片 {index} 获取图像失败")
                tile_path = folder / "survey" / "tiles" / f"tile_r{placement.row:04d}_c{placement.column:04d}.{config.extension}"
                tile_path.parent.mkdir(parents=True, exist_ok=True)
                self.scanner.save_frame(tile_path, sample.frame, config.bit_depth)
                storage.append_tile_index(
                    {
                        "sequence": placement.sequence,
                        "row": placement.row,
                        "column": placement.column,
                        "target_x_mm": placement.target.x_mm,
                        "target_y_mm": placement.target.y_mm,
                        "actual_x_mm": actual[0],
                        "actual_y_mm": actual[1],
                        "capture_count": sample.capture_count,
                        "captured_at": sample.captured_at,
                        "path": str(tile_path.relative_to(folder)),
                        "status": "completed",
                    }
                )
                self.tile_ready(placement, sample, actual)
                completed = index
                self._write_state(storage, "running", completed, len(config.plan.placements))
                self.progress("概览扫描", completed, len(config.plan.placements), placement)
            self._write_state(storage, "completed", completed, len(config.plan.placements))
            self._save_preview(storage)
            return SpatialScanResult(folder, completed, len(config.plan.placements), False, True)
        except Exception:
            if self._stop.is_set():
                self._write_state(storage, "stopped", completed, len(config.plan.placements))
                self._save_preview(storage)
                return SpatialScanResult(folder, completed, len(config.plan.placements), True, True)
            self._write_state(storage, "failed", completed, len(config.plan.placements))
            raise
        finally:
            self._restore_camera()

    def _run_acquisition(self, config: SpatialAcquisitionConfig) -> SpatialScanResult:
        if not config.plan.placements:
            raise ValueError("空间采集计划为空")
        folder = SpatialJobStorage.create_job_directory(config.save_dir, "SpatialScan")
        storage = SpatialJobStorage(folder)
        storage.write_state(config.plan, filename="plan/route.json")
        self._write_state(storage, "running", 0, len(config.plan.placements))
        completed = 0
        results: list[ScanResult] = []
        try:
            for index, placement in enumerate(config.plan.placements, start=1):
                self._raise_if_stopped()
                actual = self.stage.move_absolute_blocking(
                    placement.target.x_mm,
                    placement.target.y_mm,
                    timeout_seconds=config.timeout_seconds,
                    cancel_event=self._stop,
                )
                self._wait_settle(config.settle_ms)
                self._raise_if_stopped()
                tile_dir = folder / "acquisition" / f"tile_r{placement.row:04d}_c{placement.column:04d}"
                tile_dir.mkdir(parents=True, exist_ok=True)
                tile_config = replace(config.pzt_config, save_dir=tile_dir)
                result = self.scanner.run_sync(tile_config)
                results.append(result)
                completed = index
                storage.write_state(
                    {
                        "placement": placement,
                        "actual_x_mm": actual[0],
                        "actual_y_mm": actual[1],
                        "scan_folder": str(result.folder.relative_to(folder)),
                        "completed_images": result.completed_images,
                        "saved_images": result.saved_images,
                        "stopped": result.stopped,
                    },
                    filename=f"acquisition/tile_r{placement.row:04d}_c{placement.column:04d}/tile.json",
                )
                self._write_state(storage, "running", completed, len(config.plan.placements))
                self.progress("空间纵向扫描", completed, len(config.plan.placements), placement)
                if result.stopped:
                    break
        except Exception:
            if not self._stop.is_set():
                self._write_state(storage, "failed", completed, len(config.plan.placements))
                raise
        stopped = self._stop.is_set() or completed < len(config.plan.placements)
        self._write_state(storage, "stopped" if stopped else "completed", completed, len(config.plan.placements))
        return SpatialScanResult(folder, completed, len(config.plan.placements), stopped, False, tuple(results))

    @staticmethod
    def _write_state(storage: SpatialJobStorage, state: str, completed: int, total: int) -> None:
        storage.write_state(
            {
                "state": state,
                "completed_tiles": completed,
                "total_tiles": total,
                "updated_at": time.time(),
            }
        )

    def _save_preview(self, storage: SpatialJobStorage) -> None:
        if self.preview_provider is None:
            return
        image = self.preview_provider()
        if image is not None:
            storage.save_preview(image)

    def _prepare_camera(self, bit_depth: int) -> None:
        if not self.camera.initialized:
            raise RuntimeError("请先连接相机")
        if bit_depth == 8:
            self.camera.set_output_format_8bit()
        elif bit_depth == 12:
            self.camera.set_output_format_12bit_packed()
        else:
            raise ValueError(f"不支持的位深: {bit_depth}")
        self.camera.apply_quantitative_profile()
        self.camera.set_trigger_mode(1)

    def _restore_camera(self) -> None:
        try:
            if self.camera.initialized:
                self.camera.set_trigger_mode(0)
        except Exception as exc:
            self.message(f"恢复相机连续采集失败: {exc}")

    def _wait_settle(self, settle_ms: int) -> None:
        deadline = time.perf_counter() + max(0, settle_ms) / 1000.0
        while time.perf_counter() < deadline:
            self._raise_if_stopped()
            time.sleep(min(0.02, max(0.0, deadline - time.perf_counter())))

    def _raise_if_stopped(self) -> None:
        if self._stop.is_set():
            raise RuntimeError("空间扫描已停止")
