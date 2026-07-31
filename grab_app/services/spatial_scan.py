from __future__ import annotations

import csv
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

    def refresh_status(self) -> object: ...

    def stop_all(self) -> None: ...


ProgressCallback = Callable[[str, int, int, TilePlacement | None], None]
TileCallback = Callable[[TilePlacement, CameraFrame, tuple[float, float]], None]
DoneCallback = Callable[[object | None, Exception | None], None]

_ROUTE_LABELS = {
    "serpentine": "蛇形-行",
    "serpentine_column": "蛇形-列",
    "unidirectional": "单向",
}
_SCAN_MODE_LABELS = {"normal": "普通扫描", "center": "中心扫描"}
_STITCHING_INDEX_FIELDS = (
    "tile_sequence", "row", "column", "route",
    "target_x_mm", "target_y_mm", "actual_x_mm", "actual_y_mm",
    "xy_error_x_um", "xy_error_y_um",
    "tile_width_mm", "tile_height_mm", "spacing_x_mm", "spacing_y_mm",
    "frame_width_px", "frame_height_px", "nominal_overlap",
    "round", "z_step", "target_z_um", "actual_z_um", "pzt_channel",
    "image_kind", "bit_depth", "capture_count", "captured_at",
    "file_exists", "image_relative_path",
)


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


def _stitching_image_variants(
    log_path: Path, filename: str
) -> list[tuple[str, Path]]:
    variants = [("raw", log_path.parent / filename)]
    if log_path.parent.parent.name != "raw":
        return variants
    corrected = (
        log_path.parent.parent.parent / "corrected" / log_path.parent.name / filename
    )
    if corrected.parent.exists():
        variants.append(("corrected", corrected))
    return variants


def _stitching_index_row(
    folder: Path,
    config: SpatialAcquisitionConfig,
    placement: TilePlacement,
    actual: tuple[float, float],
    scan_row: dict[str, str],
    round_value: str | int,
    image_kind: str,
    image_path: Path,
) -> dict[str, object]:
    plan = config.plan
    return {
        "tile_sequence": placement.sequence,
        "row": placement.row,
        "column": placement.column,
        "route": plan.route,
        "target_x_mm": placement.target.x_mm,
        "target_y_mm": placement.target.y_mm,
        "actual_x_mm": actual[0],
        "actual_y_mm": actual[1],
        "xy_error_x_um": (actual[0] - placement.target.x_mm) * 1000.0,
        "xy_error_y_um": (actual[1] - placement.target.y_mm) * 1000.0,
        "tile_width_mm": plan.tile_size_mm[0],
        "tile_height_mm": plan.tile_size_mm[1],
        "spacing_x_mm": plan.spacing_mm[0],
        "spacing_y_mm": plan.spacing_mm[1],
        "frame_width_px": plan.frame_size_px[0],
        "frame_height_px": plan.frame_size_px[1],
        "nominal_overlap": plan.overlap,
        "round": round_value,
        "z_step": scan_row.get("step", ""),
        "target_z_um": scan_row.get("target_um", ""),
        "actual_z_um": scan_row.get("actual_um", ""),
        "pzt_channel": config.pzt_config.channel,
        "image_kind": image_kind,
        "bit_depth": config.pzt_config.bit_depth,
        "capture_count": scan_row.get("capture_count", ""),
        "captured_at": scan_row.get("captured_at", ""),
        "file_exists": image_path.is_file(),
        "image_relative_path": image_path.relative_to(folder).as_posix(),
    }


def _stitching_guide_text(
    config: SpatialAcquisitionConfig,
    state: str,
    completed_tiles: int,
    indexed_images: int,
) -> str:
    plan = config.plan
    pzt = config.pzt_config
    route_label = _ROUTE_LABELS.get(plan.route, plan.route)
    mode_label = _SCAN_MODE_LABELS.get(pzt.mode, pzt.mode)
    return (
        "三维拼接数据说明\n================\n\n"
        f"任务状态: {state}\n"
        f"已完成 XY Tile: {completed_tiles}/{plan.tile_count}\n"
        f"索引图像数: {indexed_images}\n\n"
        "XY 扫描\n"
        f"XY 路径: {route_label}\n"
        f"网格: {plan.rows} 行 × {plan.columns} 列\n"
        f"相机帧: {plan.frame_size_px[0]} × {plan.frame_size_px[1]} px\n"
        f"单 Tile 视场: {plan.tile_size_mm[0]:.9g} × {plan.tile_size_mm[1]:.9g} mm\n"
        f"Tile 中心间距: X={plan.spacing_mm[0]:.9g} mm, "
        f"Y={plan.spacing_mm[1]:.9g} mm\n"
        f"名义重叠率: {plan.overlap * 100:.4g}%\n\n"
        "PZT 纵向扫描\n"
        f"PZT 扫描模式: {mode_label}\n"
        f"通道: {pzt.channel}\n"
        f"范围: {pzt.start_um:.9g} → {pzt.end_um:.9g} µm\n"
        f"步长: {pzt.step_um:.9g} µm\n"
        f"稳定时间: {pzt.stable_ms} ms\n"
        f"重复次数: {pzt.repeats}\n"
        "空间纵向扫描固定使用软触发。\n\n"
        "三维拼接索引\n"
        "文件: acquisition/三维拼接索引.csv\n"
        "每行对应一个图像文件；启用平场校正时，raw 与 corrected 分行记录。\n"
        "建议使用 actual_x_mm、actual_y_mm、actual_z_um 作为图像的 XYZ 视野中心。\n"
        "X/Y 单位为 mm，Z 单位为 µm；image_relative_path 相对于任务根目录。\n"
        "tile_sequence 是执行次序，row/column 是几何网格位置。\n"
        "target_* 是规划目标，actual_* 是设备反馈；拼接优先使用 actual_*。\n"
        "索引记录视野中心和轴对齐视场尺寸，不包含逐像素旋转/剪切仿射矩阵；\n"
        "精确像素级拼接仍应结合相机-位移台标定和图像配准。\n"
    )


def _survey_tile_lines(index_path: Path) -> list[str]:
    if not index_path.is_file():
        return []
    lines: list[str] = []
    with index_path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            target_x = float(row["target_x_mm"])
            target_y = float(row["target_y_mm"])
            actual_x = float(row["actual_x_mm"])
            actual_y = float(row["actual_y_mm"])
            image_path = Path(row["path"]).as_posix()
            lines.append(
                f"{row['sequence']}\t{row['row']}\t{row['column']}\t"
                f"{target_x:.6f}\t{target_y:.6f}\t"
                f"{actual_x:.6f}\t{actual_y:.6f}\t"
                f"{(actual_x - target_x) * 1000.0:.3f}\t"
                f"{(actual_y - target_y) * 1000.0:.3f}\t{image_path}"
            )
    return lines


def _survey_calibration_lines(config: SurveyConfig) -> list[str]:
    if config.calibration is None:
        return ["标定: 未提供"]
    calibration = config.calibration
    return [
        f"标定像素间距: {calibration.pixel_size_um:.9g} µm/px",
        "标定矩阵 pixel = matrix @ DPOS + offset:",
        f"  [{calibration.matrix[0][0]:.9g}, {calibration.matrix[0][1]:.9g}]",
        f"  [{calibration.matrix[1][0]:.9g}, {calibration.matrix[1][1]:.9g}]",
        f"标定偏移 offset: [{calibration.offset[0]:.9g}, "
        f"{calibration.offset[1]:.9g}] px",
        "标定文件: calibration/stage_camera_affine.json",
    ]


def _survey_report_text(
    folder: Path,
    config: SurveyConfig,
    state: str,
    completed_tiles: int,
) -> str:
    plan = config.plan
    route_label = _ROUTE_LABELS.get(plan.route, plan.route)
    overlap_x, overlap_y = plan.effective_overlap_xy
    tile_lines = _survey_tile_lines(folder / "survey" / "tile_index.csv")
    lines = [
        "概览扫描报告",
        "============",
        "",
        f"任务状态: {state}",
        f"已完成 XY Tile: {completed_tiles}/{plan.tile_count}",
        "",
        "XY 扫描参数",
        f"XY 路径: {route_label}",
        f"网格: {plan.rows} 行 × {plan.columns} 列",
        f"相机帧: {plan.frame_size_px[0]} × {plan.frame_size_px[1]} px",
        f"单 Tile 视场: {plan.tile_size_mm[0]:.9g} × {plan.tile_size_mm[1]:.9g} mm",
        f"Tile 中心间距: X={plan.spacing_mm[0]:.9g} mm, Y={plan.spacing_mm[1]:.9g} mm",
        f"名义重叠率: {plan.overlap * 100:.4g}%",
        f"有效重叠率: X={overlap_x * 100:.4g}%, Y={overlap_y * 100:.4g}%",
        f"预计 XY 路程: {plan.estimated_distance_mm:.9g} mm",
        f"XY 到位后稳定时间: {config.settle_ms} ms",
        "",
        "相机与标定",
        f"保存位深: {config.bit_depth} bit",
        f"图像格式: {config.extension}",
        *_survey_calibration_lines(config),
        "",
        "文件说明",
        "拼接预览图: survey/preview.png",
        "概览原始瓦片: survey/tiles/",
        "逐瓦片机器索引: survey/tile_index.csv",
        "完整规划数据: plan/route.json",
        "后续拼接优先使用实际 X/Y，而不是目标 X/Y。",
        "",
        "瓦片明细",
        "序号\t行\t列\t目标X(mm)\t目标Y(mm)\t实际X(mm)\t实际Y(mm)\tX误差(µm)\tY误差(µm)\t图像路径",
        *tile_lines,
        "",
    ]
    return "\n".join(lines)


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
        self._write_survey_report(folder, config, "进行中", 0)
        self._prepare_camera(config.bit_depth)
        completed = 0
        try:
            for index, placement in enumerate(config.plan.placements, start=1):
                self._raise_if_stopped()
                self.stage.move_absolute_blocking(
                    placement.target.x_mm,
                    placement.target.y_mm,
                    timeout_seconds=config.timeout_seconds,
                    cancel_event=self._stop,
                )
                self._wait_settle(config.settle_ms)
                self._raise_if_stopped()
                capture_barrier = self.camera.capture_barrier()
                sample = self.camera.wait_for_fresh_sample(
                    capture_barrier,
                    discard_frames=1,
                    timeout_ms=2000,
                )
                if sample is None:
                    raise RuntimeError(
                        f"概览瓦片 {index} 未能取得到位后的第二张连续新帧"
                    )
                actual = self._refresh_stage_position()
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
                        "capture_barrier_count": capture_barrier,
                        "discarded_frames": 1,
                        "capture_count": sample.capture_count,
                        "captured_at": sample.captured_at,
                        "frame_age_ms": max(0.0, (time.time() - sample.captured_at) * 1000.0),
                        "is_trigger_frame": sample.is_trigger_frame,
                        "sdk_timestamp_01ms": sample.sdk_timestamp_01ms,
                        "path": str(tile_path.relative_to(folder)),
                        "status": "completed",
                    }
                )
                self.tile_ready(placement, sample, actual)
                completed = index
                self._write_state(storage, "running", completed, len(config.plan.placements))
                self._write_survey_report(folder, config, "进行中", completed)
                self.progress("概览扫描", completed, len(config.plan.placements), placement)
            self._write_state(storage, "completed", completed, len(config.plan.placements))
            self._save_preview(storage)
            self._write_survey_report(folder, config, "已完成", completed)
            return SpatialScanResult(folder, completed, len(config.plan.placements), False, True)
        except Exception:
            if self._stop.is_set():
                self._write_state(storage, "stopped", completed, len(config.plan.placements))
                self._save_preview(storage)
                self._write_survey_report(folder, config, "已停止", completed)
                return SpatialScanResult(folder, completed, len(config.plan.placements), True, True)
            self._write_state(storage, "failed", completed, len(config.plan.placements))
            self._write_survey_report(folder, config, "失败", completed)
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
        self._ensure_stitching_index(folder)
        self._write_stitching_guide(folder, config, "进行中", 0, 0)
        completed = 0
        indexed_images = 0
        results: list[ScanResult] = []
        tile_scan_stopped = False
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
                # XY 区域内的逐瓦片纵向扫描固定使用软触发；普通保存扫描的
                # 用户选择由其自身入口处理，不能串入这里。
                tile_config = replace(
                    config.pzt_config,
                    save_dir=tile_dir,
                    trigger_mode="soft",
                )
                result = self.scanner.run_sync(tile_config)
                results.append(result)
                completed = index
                indexed_images += self._append_stitching_index(
                    folder, config, placement, actual, result
                )
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
                self._write_stitching_guide(
                    folder, config, "进行中", completed, indexed_images
                )
                self.progress("空间纵向扫描", completed, len(config.plan.placements), placement)
                if result.stopped:
                    tile_scan_stopped = True
                    break
        except Exception:
            if not self._stop.is_set():
                self._write_state(storage, "failed", completed, len(config.plan.placements))
                self._write_stitching_guide(
                    folder, config, "失败", completed, indexed_images
                )
                raise
        stopped = (
            self._stop.is_set()
            or tile_scan_stopped
            or completed < len(config.plan.placements)
        )
        self._write_state(storage, "stopped" if stopped else "completed", completed, len(config.plan.placements))
        self._write_stitching_guide(
            folder,
            config,
            "已停止" if stopped else "已完成",
            completed,
            indexed_images,
        )
        return SpatialScanResult(folder, completed, len(config.plan.placements), stopped, False, tuple(results))

    @staticmethod
    def _ensure_stitching_index(folder: Path) -> Path:
        index_path = folder / "acquisition" / "三维拼接索引.csv"
        with index_path.open("w", encoding="utf-8-sig", newline="") as handle:
            csv.DictWriter(handle, fieldnames=_STITCHING_INDEX_FIELDS).writeheader()
        return index_path

    @staticmethod
    def _write_survey_report(
        folder: Path,
        config: SurveyConfig,
        state: str,
        completed_tiles: int,
    ) -> Path:
        path = folder / "概览扫描报告.txt"
        path.write_text(
            _survey_report_text(folder, config, state, completed_tiles),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _append_stitching_index(
        folder: Path,
        config: SpatialAcquisitionConfig,
        placement: TilePlacement,
        actual: tuple[float, float],
        result: ScanResult,
    ) -> int:
        """把单 Tile 的分轮 PZT 日志展开为可直接用于三维拼接的逐图索引。"""
        index_path = folder / "acquisition" / "三维拼接索引.csv"
        written = 0
        # BOM 只在首次建表时写入；追加使用普通 UTF-8，避免每个 Tile 中间插入 BOM。
        with index_path.open("a", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=_STITCHING_INDEX_FIELDS)
            for log_path in sorted(result.folder.rglob("scan_log.csv")):
                round_name = log_path.parent.name
                round_text = round_name.removeprefix("Round_")
                round_value: str | int = int(round_text) if round_text.isdigit() else round_name
                with log_path.open("r", encoding="utf-8-sig", newline="") as source:
                    for scan_row in csv.DictReader(source):
                        filename = scan_row.get("filename", "")
                        for image_kind, image_path in _stitching_image_variants(
                            log_path, filename
                        ):
                            writer.writerow(_stitching_index_row(
                                folder, config, placement, actual, scan_row,
                                round_value, image_kind, image_path,
                            ))
                            written += 1
        return written

    @staticmethod
    def _write_stitching_guide(
        folder: Path,
        config: SpatialAcquisitionConfig,
        state: str,
        completed_tiles: int,
        indexed_images: int,
    ) -> Path:
        path = folder / "三维拼接说明.txt"
        path.write_text(
            _stitching_guide_text(
                config, state, completed_tiles, indexed_images
            ),
            encoding="utf-8",
        )
        return path

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
        # 样品地图概览独占连续采集策略，主界面预览可在扫描期间继续刷新。
        self.camera.set_trigger_mode(0)

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

    def _refresh_stage_position(self) -> tuple[float, float]:
        """在抓拍完成后重新读取 DPOS，作为该帧唯一的位置记录。"""
        snapshot = self.stage.refresh_status()
        axes = getattr(snapshot, "axes", None)
        if axes is None or 0 not in axes or 1 not in axes:
            raise RuntimeError("抓拍后无法读取 XY 位移台 DPOS")
        return float(axes[0].dpos), float(axes[1].dpos)

    def _raise_if_stopped(self) -> None:
        if self._stop.is_set():
            raise RuntimeError("空间扫描已停止")
