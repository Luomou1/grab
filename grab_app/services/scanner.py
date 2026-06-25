from __future__ import annotations

import csv
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

import cv2
import numpy as np

from grab_app.camera import CameraController, CameraFrame
from grab_app.config import PZT_MAX_UM, PZT_MIN_UM
from grab_app.image_io import save_image
from grab_app.pzt import PZTController

ScanMode = Literal["normal", "center"]
TriggerMode = Literal["soft", "continuous"]


@dataclass(frozen=True)
class FlatFieldCalibration:
    dark: np.ndarray
    flat: np.ndarray
    signature: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ScanConfig:
    mode: ScanMode
    channel: int
    start_um: float
    end_um: float
    step_um: float
    stable_ms: int
    repeats: int
    trigger_mode: TriggerMode
    save_dir: Path
    prefix: str
    extension: str
    bit_depth: int
    apply_quantitative_profile: bool = True
    flat_field_calibration: FlatFieldCalibration | None = None
    save_raw_when_correcting: bool = True
    saver_queue_size: int = 256


@dataclass(frozen=True)
class ScanResult:
    folder: Path
    completed_images: int
    stopped: bool
    saved_images: int = 0


@dataclass(frozen=True)
class SaveTask:
    sequence: int
    path: Path
    frame: np.ndarray
    bit_depth: int


@dataclass
class SaveContext:
    tasks: queue.Queue[SaveTask | None]
    thread: threading.Thread
    error_lock: threading.Lock
    first_error: Exception | None = None
    saved_count: int = 0
    full_wait_count: int = 0
    max_depth: int = 0


@dataclass(frozen=True)
class ScanRow:
    step: int
    target_um: float
    actual_um: float | None
    filename: str
    save_sequence: int
    capture_count: int
    trigger_started_at: float | None
    captured_at: float
    exposure_us: float | None
    gain_x: float | None
    is_trigger_frame: bool | None
    sdk_timestamp_01ms: int | None
    frame_exposure_us: int | None
    frame_gain_x: float | None
    frame_gamma: int | None
    frame_contrast: int | None


ProgressCallback = Callable[[str, int, int, float, float | None], None]
MessageCallback = Callable[[str], None]


class ScanWorker:
    def __init__(
        self,
        camera: CameraController,
        pzt: PZTController,
        progress: ProgressCallback,
        message: MessageCallback,
    ) -> None:
        self.camera = camera
        self.pzt = pzt
        self.progress = progress
        self.message = message
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: Exception | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, config: ScanConfig, done: Callable[[ScanResult | None, Exception | None], None]) -> None:
        if self.running:
            raise RuntimeError("扫描已在运行")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(config, done), name="scan-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self, config: ScanConfig, done: Callable[[ScanResult | None, Exception | None], None]) -> None:
        try:
            result = self._scan(config)
            done(result, None)
        except Exception as exc:
            done(None, exc)

    def _scan(self, config: ScanConfig) -> ScanResult:
        self._validate(config)
        scan_folder = self._next_scan_folder(config)
        scan_folder.mkdir(parents=True, exist_ok=False)

        if config.bit_depth == 8:
            self.camera.set_output_format_8bit()
        else:
            self.camera.set_output_format_12bit_packed()
        camera_profile: dict[str, object] = {}
        if config.apply_quantitative_profile and hasattr(self.camera, "apply_quantitative_profile"):
            camera_profile = self.camera.apply_quantitative_profile()
            self.message("已应用定量采集 profile: 关闭自动曝光并锁定当前曝光/增益")
        elif hasattr(self.camera, "capture_signature"):
            camera_profile = self.camera.capture_signature()

        self.camera.set_trigger_mode(1 if config.trigger_mode == "soft" else 0)
        self._validate_calibration(config, camera_profile)
        save_context = self._start_saver(config.saver_queue_size)

        try:
            positions = self._positions(config.start_um, config.end_um, config.step_um)
            total = len(positions) * config.repeats
            completed = 0
            sequence = 0
            stopped = False

            for round_index in range(1, config.repeats + 1):
                if self._stop.is_set():
                    stopped = True
                    break
                round_name = f"Round_{round_index:02d}"
                if config.flat_field_calibration is None:
                    round_folder = scan_folder / round_name
                    raw_folder = round_folder
                    corrected_folder = None
                else:
                    raw_folder = scan_folder / "raw" / round_name
                    corrected_folder = scan_folder / "corrected" / round_name
                    round_folder = raw_folder
                raw_folder.mkdir(parents=True)
                if corrected_folder is not None:
                    corrected_folder.mkdir(parents=True)
                rows: list[ScanRow] = []

                self.pzt.send_move(config.channel, positions[0])
                time.sleep(config.stable_ms / 1000.0 + 0.1)

                for step_index, target_um in enumerate(positions, start=1):
                    if self._stop.is_set():
                        stopped = True
                        break

                    self._raise_save_error(save_context)
                    actual_um = self.pzt.read_move(config.channel)
                    sample = self._capture(config)
                    sequence += 1
                    filename = f"{config.prefix}_{step_index:04d}.{config.extension}"
                    if config.flat_field_calibration is None:
                        self._enqueue_save(
                            save_context,
                            SaveTask(
                                sequence=sequence,
                                path=round_folder / filename,
                                frame=sample.frame.copy(),
                                bit_depth=config.bit_depth,
                            ),
                        )
                    else:
                        if config.save_raw_when_correcting:
                            self._enqueue_save(
                                save_context,
                                SaveTask(
                                    sequence=sequence,
                                    path=raw_folder / filename,
                                    frame=sample.frame.copy(),
                                    bit_depth=config.bit_depth,
                                ),
                            )
                        corrected = self._correct_flat_field(sample.frame, config.flat_field_calibration, config.bit_depth)
                        self._enqueue_save(
                            save_context,
                            SaveTask(
                                sequence=sequence,
                                path=corrected_folder / filename,
                                frame=corrected,
                                bit_depth=config.bit_depth,
                            ),
                        )

                    completed += 1
                    rows.append(
                        ScanRow(
                            step=step_index,
                            target_um=target_um,
                            actual_um=actual_um,
                            filename=filename,
                            save_sequence=sequence,
                            capture_count=sample.capture_count,
                            trigger_started_at=sample.trigger_started_at,
                            captured_at=sample.captured_at,
                            exposure_us=sample.exposure_us,
                            gain_x=sample.gain_x,
                            is_trigger_frame=sample.is_trigger_frame,
                            sdk_timestamp_01ms=sample.sdk_timestamp_01ms,
                            frame_exposure_us=sample.frame_exposure_us,
                            frame_gain_x=sample.frame_gain_x,
                            frame_gamma=sample.frame_gamma,
                            frame_contrast=sample.frame_contrast,
                        )
                    )
                    self.progress(f"第 {round_index}/{config.repeats} 轮", completed, total, target_um, actual_um)

                    if step_index < len(positions):
                        next_um = positions[step_index]
                        self.pzt.send_move(config.channel, next_um)
                        time.sleep(config.stable_ms / 1000.0)

                self._write_round_log(round_folder / "scan_log.csv", rows)
                if stopped:
                    break

            self._finish_saver(save_context, raise_error=True)
            self.camera.set_trigger_mode(0)
            self._write_calibration(scan_folder / "calibration", config)
            if hasattr(self.camera, "capture_signature"):
                camera_profile = self.camera.capture_signature()
            self._write_summary(
                scan_folder / "summary.txt",
                config,
                completed,
                total,
                stopped,
                save_context,
                camera_profile,
            )
            return ScanResult(scan_folder, completed, stopped, save_context.saved_count)
        except Exception:
            self._finish_saver(save_context, raise_error=False)
            self.camera.set_trigger_mode(0)
            raise

    def _validate(self, config: ScanConfig) -> None:
        if not config.save_dir.exists():
            raise ValueError("保存路径不存在")
        if config.step_um <= 0:
            raise ValueError("扫描步长必须大于 0")
        if config.repeats < 1:
            raise ValueError("重复次数必须大于 0")
        if config.start_um == config.end_um:
            raise ValueError("起始位置和终止位置不能相同")
        if config.start_um < PZT_MIN_UM or config.end_um > PZT_MAX_UM:
            raise ValueError("扫描范围超出 PZT 限制(0-270um)")

    def _next_scan_folder(self, config: ScanConfig) -> Path:
        base_name = self._scan_folder_name(config)
        candidate = config.save_dir / base_name
        index = 2
        while candidate.exists():
            candidate = config.save_dir / f"{base_name}-{index:02d}"
            index += 1
        return candidate

    def _scan_folder_name(self, config: ScanConfig) -> str:
        mode_name = "CS" if config.mode == "center" else "Scan"
        step_text = f"{config.step_um:g}um"
        stamp = datetime.now().strftime("%m%d%H%M")
        return f"{mode_name}-{step_text}-{stamp}"

    def _positions(self, start: float, end: float, step: float) -> list[float]:
        sign = 1 if end >= start else -1
        step = abs(step) * sign
        positions: list[float] = []
        current = start
        while (sign > 0 and current <= end + 1e-9) or (sign < 0 and current >= end - 1e-9):
            positions.append(round(current, 6))
            current += step
        if positions[-1] != end:
            positions[-1] = end
        return positions

    def _capture(self, config: ScanConfig) -> CameraFrame:
        sample = (
            self.camera.soft_trigger_and_grab_sample(2000)
            if config.trigger_mode == "soft"
            else self.camera.grab_sample()
        )
        if sample is None:
            time.sleep(0.05)
            sample = (
                self.camera.soft_trigger_and_grab_sample(2000)
                if config.trigger_mode == "soft"
                else self.camera.grab_sample()
            )
        if sample is None:
            raise RuntimeError("获取图像失败")
        return sample

    def _validate_calibration(self, config: ScanConfig, camera_profile: dict[str, object]) -> None:
        calibration = config.flat_field_calibration
        if calibration is None:
            return
        if calibration.dark.shape != calibration.flat.shape:
            raise ValueError("暗场和平场尺寸不一致")
        if calibration.dark.ndim != 2 or calibration.flat.ndim != 2:
            raise ValueError("暗场/平场校正只支持单通道灰度图")
        if tuple(calibration.dark.shape) != (int(camera_profile.get("height", 0)), int(camera_profile.get("width", 0))):
            raise ValueError("暗场/平场尺寸与当前相机 ROI 不一致")
        stable_keys = (
            "width",
            "height",
            "output_bit_depth",
            "exposure_us",
            "gain_x",
            "roi_x",
            "roi_y",
            "roi_width",
            "roi_height",
            "lut_mode",
            "gamma",
            "contrast",
            "sharpness",
            "black_level",
            "white_level",
            "defect_correction",
            "noise_filter",
            "sdk_flat_fielding",
            "denoise3d",
        )
        mismatches: list[str] = []
        for key in stable_keys:
            if key not in calibration.signature:
                continue
            expected = calibration.signature.get(key)
            actual = camera_profile.get(key)
            if isinstance(expected, float) or isinstance(actual, float):
                if expected is None or actual is None or abs(float(expected) - float(actual)) > 1e-3:
                    mismatches.append(f"{key}: {expected} != {actual}")
            elif expected != actual:
                mismatches.append(f"{key}: {expected} != {actual}")
        expected_bit_depth = calibration.signature.get("save_bit_depth")
        if expected_bit_depth is not None and int(expected_bit_depth) != config.bit_depth:
            mismatches.append(f"save_bit_depth: {expected_bit_depth} != {config.bit_depth}")
        if mismatches:
            raise ValueError("暗场/平场校准与当前采集参数不一致: " + "; ".join(mismatches[:4]))

    def _start_saver(self, queue_size: int = 256) -> SaveContext:
        context = SaveContext(
            tasks=queue.Queue(maxsize=max(1, int(queue_size))),
            thread=threading.Thread(),
            error_lock=threading.Lock(),
        )
        context.thread = threading.Thread(target=self._save_loop, args=(context,), name="image-save-worker", daemon=True)
        context.thread.start()
        return context

    def _save_loop(self, context: SaveContext) -> None:
        while True:
            task = context.tasks.get()
            try:
                if task is None:
                    return
                self._save_frame(task.path, task.frame, task.bit_depth)
                with context.error_lock:
                    context.saved_count += 1
            except Exception as exc:
                with context.error_lock:
                    if context.first_error is None:
                        context.first_error = exc if isinstance(exc, Exception) else RuntimeError(str(exc))
            finally:
                context.tasks.task_done()

    def _enqueue_save(self, context: SaveContext, task: SaveTask) -> None:
        self._raise_save_error(context)
        while True:
            try:
                context.tasks.put(task, timeout=0.1)
                with context.error_lock:
                    context.max_depth = max(context.max_depth, context.tasks.qsize())
                return
            except queue.Full:
                with context.error_lock:
                    context.full_wait_count += 1
                self._raise_save_error(context)

    def _finish_saver(self, context: SaveContext, raise_error: bool) -> None:
        while True:
            try:
                context.tasks.put(None, timeout=0.1)
                break
            except queue.Full:
                if raise_error:
                    self._raise_save_error(context)
        context.tasks.join()
        context.thread.join(timeout=2.0)
        if raise_error:
            self._raise_save_error(context)

    def _raise_save_error(self, context: SaveContext) -> None:
        with context.error_lock:
            if context.first_error is not None:
                raise RuntimeError(f"异步保存失败: {context.first_error}") from context.first_error

    def _save_frame(self, path: Path, frame: np.ndarray, bit_depth: int) -> None:
        image = frame
        if bit_depth == 8:
            if image.ndim != 2:
                raise RuntimeError("8bit 保存只支持单通道灰度图")
            if image.dtype != np.uint8:
                image = np.clip(image, 0, 255).astype(np.uint8)
        elif bit_depth == 12:
            if path.suffix.lower() not in {".png", ".tif", ".tiff"}:
                raise RuntimeError("12bit 图像请保存为 png 或 tiff")
            if image.ndim != 2:
                raise RuntimeError("12bit 保存只支持单通道灰度图")
            if image.dtype != np.uint16:
                image = image.astype(np.uint16)
            image = np.left_shift(np.clip(image, 0, 4095), 4).astype(np.uint16)
        else:
            raise RuntimeError(f"不支持的位深: {bit_depth}")
        ok = save_image(path, image)
        if not ok:
            raise RuntimeError(f"图像保存失败: {path}")

    def _correct_flat_field(
        self,
        frame: np.ndarray,
        calibration: FlatFieldCalibration,
        bit_depth: int,
    ) -> np.ndarray:
        if frame.shape != calibration.dark.shape:
            raise RuntimeError(
                f"校正图尺寸不匹配: frame={frame.shape}, calibration={calibration.dark.shape}"
            )
        frame_f = frame.astype(np.float32)
        dark = calibration.dark.astype(np.float32)
        flat = calibration.flat.astype(np.float32)
        flat_signal = flat - dark
        valid = flat_signal > 1.0
        if not np.any(valid):
            raise RuntimeError("平场信号无有效区域，请重新采集平场")
        scale = float(np.mean(flat_signal[valid]))
        denominator = np.where(valid, flat_signal, scale)
        corrected = (frame_f - dark) / denominator * scale
        max_value = 255 if bit_depth == 8 else 4095
        dtype = np.uint8 if bit_depth == 8 else np.uint16
        return np.clip(np.rint(corrected), 0, max_value).astype(dtype)

    def _write_calibration(self, directory: Path, config: ScanConfig) -> None:
        calibration = config.flat_field_calibration
        if calibration is None:
            return
        directory.mkdir(parents=True, exist_ok=True)
        dark = np.clip(np.rint(calibration.dark), 0, 4095 if config.bit_depth == 12 else 255).astype(np.uint16)
        flat = np.clip(np.rint(calibration.flat), 0, 4095 if config.bit_depth == 12 else 255).astype(np.uint16)
        self._save_frame(directory / "dark_average.tiff", dark, config.bit_depth)
        self._save_frame(directory / "flat_average.tiff", flat, config.bit_depth)
        with (directory / "calibration_signature.txt").open("w", encoding="utf-8") as handle:
            for key, value in sorted(calibration.signature.items()):
                handle.write(f"{key}: {value}\n")

    def _write_round_log(self, path: Path, rows: list[ScanRow]) -> None:
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "step",
                    "target_um",
                    "actual_um",
                    "filename",
                    "save_sequence",
                    "capture_count",
                    "trigger_started_at",
                    "captured_at",
                    "exposure_us",
                    "gain_x",
                    "is_trigger_frame",
                    "sdk_timestamp_01ms",
                    "frame_exposure_us",
                    "frame_gain_x",
                    "frame_gamma",
                    "frame_contrast",
                ]
            )
            for row in rows:
                writer.writerow(
                    [
                        row.step,
                        row.target_um,
                        row.actual_um,
                        row.filename,
                        row.save_sequence,
                        row.capture_count,
                        row.trigger_started_at,
                        row.captured_at,
                        row.exposure_us,
                        row.gain_x,
                        row.is_trigger_frame,
                        row.sdk_timestamp_01ms,
                        row.frame_exposure_us,
                        row.frame_gain_x,
                        row.frame_gamma,
                        row.frame_contrast,
                    ]
                )

    def _write_summary(
        self,
        path: Path,
        config: ScanConfig,
        completed: int,
        total: int,
        stopped: bool,
        save_context: SaveContext,
        camera_profile: dict[str, object],
    ) -> None:
        with path.open("w", encoding="utf-8") as handle:
            handle.write("扫描汇总\n")
            handle.write(f"完成时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n")
            handle.write(f"扫描模式: {config.mode}\n")
            handle.write(f"触发模式: {config.trigger_mode}\n")
            handle.write(f"PZT通道: {config.channel}\n")
            handle.write(f"起始位置: {config.start_um:.6f} um\n")
            handle.write(f"终止位置: {config.end_um:.6f} um\n")
            handle.write(f"扫描步长: {config.step_um:.6f} um\n")
            handle.write(f"稳定时间: {config.stable_ms} ms\n")
            handle.write(f"重复次数: {config.repeats}\n")
            handle.write(f"完成图像: {completed} / {total}\n")
            handle.write(f"保存图像: {save_context.saved_count} / {completed}\n")
            handle.write(f"是否停止: {stopped}\n")
            handle.write(f"定量采集profile: {config.apply_quantitative_profile}\n")
            handle.write(f"暗场/平场校正: {config.flat_field_calibration is not None}\n")
            handle.write(f"保存队列上限: {config.saver_queue_size}\n")
            handle.write(f"保存队列最大积压: {save_context.max_depth}\n")
            handle.write(f"保存队列等待次数: {save_context.full_wait_count}\n")
            if camera_profile:
                handle.write("\n相机参数\n")
                for key, value in sorted(camera_profile.items()):
                    handle.write(f"{key}: {value}\n")
            calibration = config.flat_field_calibration
            if calibration is not None:
                handle.write("\n校正参数\n")
                for key, value in sorted(calibration.signature.items()):
                    handle.write(f"{key}: {value}\n")
