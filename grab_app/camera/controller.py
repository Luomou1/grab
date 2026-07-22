from __future__ import annotations

import importlib
import os
import platform
import sys
import threading
import time
from ctypes import c_ubyte
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from grab_app.config import CameraSdkPaths


class CameraError(RuntimeError):
    pass


@dataclass(frozen=True)
class CameraFrame:
    frame: np.ndarray
    capture_count: int
    captured_at: float
    trigger_started_at: float | None = None
    exposure_us: float | None = None
    gain_x: float | None = None
    is_trigger_frame: bool | None = None
    sdk_timestamp_01ms: int | None = None
    frame_exposure_us: int | None = None
    frame_gain_x: float | None = None
    frame_gamma: int | None = None
    frame_contrast: int | None = None


@dataclass(frozen=True)
class CameraHealth:
    capture_count: int
    sdk_error_count: int
    timeout_count: int
    trigger_wait_count: int
    last_error: str
    latest_frame_age_ms: float | None
    sdk_total_frames: int | None = None
    sdk_capture_frames: int | None = None
    sdk_lost_frames: int | None = None
    reconnect_count: int | None = None


@dataclass(frozen=True)
class CameraExposureRange:
    exposure_min_us: float
    exposure_max_us: float
    exposure_step_us: float | None
    gain_min_x: float
    gain_max_x: float
    gain_step_x: float | None


@dataclass(frozen=True)
class CameraRoi:
    x: int
    y: int
    width: int
    height: int
    max_width: int
    max_height: int
    min_width: int
    min_height: int
    bin_sum_mask: int
    bin_average_mask: int
    skip_mask: int


@dataclass(frozen=True)
class CameraFrameStatistic:
    total: int
    capture: int
    lost: int


@dataclass(frozen=True)
class _LatestFrameInfo:
    frame: np.ndarray
    capture_count: int
    captured_at: float
    is_trigger_frame: bool | None
    sdk_timestamp_01ms: int | None
    frame_exposure_us: int | None
    frame_gain_x: float | None
    frame_gamma: int | None
    frame_contrast: int | None


def _prepare_sdk_paths(paths: CameraSdkPaths | None = None) -> None:
    paths = paths or CameraSdkPaths()
    for path in paths.existing():
        text = str(path)
        if text not in sys.path:
            sys.path.insert(0, text)
        if platform.system() == "Windows" and hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(text)
            except OSError:
                pass


class CameraController:
    _SENSOR_ROI_ALIGN = 8

    def __init__(self, sdk_paths: CameraSdkPaths | None = None) -> None:
        _prepare_sdk_paths(sdk_paths)
        self._mvsdk = importlib.import_module("grab_app.camera.mvsdk")
        self.h_camera: Any = None
        self.cap: Any = None
        self.frame_buffer: Any = None
        self.is_mono = False
        self.width = 0
        self.height = 0
        self.current_output_format = 8
        self.latest_frame: np.ndarray | None = None
        self._latest_info: _LatestFrameInfo | None = None
        self._frame_lock = threading.Lock()
        self._control_lock = threading.RLock()
        self._running = False
        self._grab_thread: threading.Thread | None = None
        self.capture_count = 0
        self.last_error: str = ""
        self.sdk_error_count = 0
        self.timeout_count = 0
        self.trigger_wait_count = 0
        self.current_trigger_mode = 0
        self.latest_frame_time: float | None = None

    @property
    def initialized(self) -> bool:
        return self.h_camera is not None

    def enumerate_devices(self) -> list[str]:
        dev_list = self._mvsdk.CameraEnumerateDevice()
        return [item.GetFriendlyName() for item in dev_list]

    def open(self, index: int = 0) -> None:
        if self.initialized:
            return
        dev_list = self._mvsdk.CameraEnumerateDevice()
        if len(dev_list) < 1:
            raise CameraError("未找到相机，请检查连接、驱动和华腾 SDK")
        if index >= len(dev_list):
            raise CameraError(f"相机索引超出范围: {index}")

        self.h_camera = self._mvsdk.CameraInit(dev_list[index], -1, -1)
        self.cap = self._mvsdk.CameraGetCapability(self.h_camera)
        self.is_mono = self.cap.sIspCapacity.bMonoSensor != 0
        self.width = int(self.cap.sResolutionRange.iWidthMax)
        self.height = int(self.cap.sResolutionRange.iHeightMax)

        if not self.is_mono:
            raise CameraError("当前程序仅支持黑白相机")
        self.set_output_format_8bit()

        frame_buffer_size = self.width * self.height * 2
        self.frame_buffer = self._mvsdk.CameraAlignMalloc(frame_buffer_size, 16)

        frame_speed = max(0, int(getattr(self.cap, "iFrameSpeedDesc", 3)) - 1)
        self.set_frame_speed(frame_speed)
        self.set_trigger_mode(0)
        self.set_auto_exposure(True)
        self.set_ae_target(120)
        self.set_anti_flick(False)
        try:
            self._mvsdk.CameraSetFrameResendCount(self.h_camera, 2)
        except Exception:
            pass
        try:
            self._mvsdk.CameraSetAutoConnect(self.h_camera, 1)
        except Exception:
            pass

        self._mvsdk.CameraPlay(self.h_camera)
        self._running = True
        self._grab_thread = threading.Thread(target=self._grab_loop, name="camera-grab", daemon=True)
        self._grab_thread.start()

    def _grab_loop(self) -> None:
        while self._running:
            try:
                with self._control_lock:
                    raw_data, frame_head = self._mvsdk.CameraGetImageBuffer(self.h_camera, 200)
                    try:
                        self._mvsdk.CameraImageProcess(self.h_camera, raw_data, self.frame_buffer, frame_head)
                        output_format = self.current_output_format
                    finally:
                        self._mvsdk.CameraReleaseImageBuffer(self.h_camera, raw_data)

                if output_format == 12:
                    frame_data = (c_ubyte * frame_head.uBytes).from_address(self.frame_buffer)
                    frame = np.frombuffer(frame_data, dtype=np.uint16).reshape((frame_head.iHeight, frame_head.iWidth))
                    # SDK 将 12bit 数据左对齐到 MONO16，右移后保存真实 0..4095 灰度值。
                    frame = np.right_shift(frame, 4).astype(np.uint16, copy=False)
                else:
                    frame_data = (c_ubyte * frame_head.uBytes).from_address(self.frame_buffer)
                    frame = np.frombuffer(frame_data, dtype=np.uint8)
                    frame = frame.reshape((frame_head.iHeight, frame_head.iWidth))

                with self._frame_lock:
                    self.capture_count += 1
                    frame_copy = frame.copy()
                    self.latest_frame_time = time.time()
                    self.latest_frame = frame_copy
                    self._latest_info = _LatestFrameInfo(
                        frame=frame_copy,
                        capture_count=self.capture_count,
                        captured_at=self.latest_frame_time,
                        is_trigger_frame=bool(getattr(frame_head, "bIsTrigger", 0)),
                        sdk_timestamp_01ms=int(getattr(frame_head, "uiTimeStamp", 0)),
                        frame_exposure_us=int(getattr(frame_head, "uiExpTime", 0)),
                        frame_gain_x=float(getattr(frame_head, "fAnalogGain", 0.0)),
                        frame_gamma=int(getattr(frame_head, "iGamma", -1)),
                        frame_contrast=int(getattr(frame_head, "iContrast", -1)),
                    )
            except self._mvsdk.CameraException as exc:
                self._record_grab_exception(exc)
                time.sleep(0.001)
            except Exception as exc:
                self.last_error = str(exc)
                self.sdk_error_count += 1
                time.sleep(0.01)

    def _record_grab_exception(self, exc: Exception) -> None:
        is_timeout = (
            getattr(exc, "error_code", None)
            == getattr(self._mvsdk, "CAMERA_STATUS_TIME_OUT", None)
        )
        if is_timeout and self.current_trigger_mode == 1:
            # 触发模式下两次触发之间没有图像是正常等待，不应作为故障超时。
            self.trigger_wait_count += 1
            return
        self.last_error = str(exc)
        self.sdk_error_count += 1
        if is_timeout:
            self.timeout_count += 1

    def grab(self) -> np.ndarray | None:
        with self._frame_lock:
            if self._latest_info is None:
                return None
            return self._latest_info.frame.copy()

    def grab_sample(self) -> CameraFrame | None:
        with self._frame_lock:
            if self._latest_info is None:
                return None
            info = self._latest_info
            return CameraFrame(
                frame=info.frame.copy(),
                capture_count=info.capture_count,
                captured_at=info.captured_at,
                exposure_us=self._safe_get_exposure(),
                gain_x=self._safe_get_gain_x(),
                is_trigger_frame=info.is_trigger_frame,
                sdk_timestamp_01ms=info.sdk_timestamp_01ms,
                frame_exposure_us=info.frame_exposure_us,
                frame_gain_x=info.frame_gain_x,
                frame_gamma=info.frame_gamma,
                frame_contrast=info.frame_contrast,
            )

    def soft_trigger_and_grab(self, timeout_ms: int = 2000) -> np.ndarray | None:
        sample = self.soft_trigger_and_grab_sample(timeout_ms)
        return None if sample is None else sample.frame

    def soft_trigger_and_grab_sample(self, timeout_ms: int = 2000) -> CameraFrame | None:
        with self._frame_lock:
            start_count = self.capture_count
        triggered_at = time.time()
        with self._control_lock:
            self._mvsdk.CameraSoftTrigger(self.h_camera)
        deadline = time.perf_counter() + max(timeout_ms, 1) / 1000.0
        while time.perf_counter() < deadline:
            with self._frame_lock:
                if self._latest_info is not None and self._latest_info.capture_count > start_count:
                    info = self._latest_info
                    return CameraFrame(
                        frame=info.frame.copy(),
                        capture_count=info.capture_count,
                        captured_at=info.captured_at,
                        trigger_started_at=triggered_at,
                        exposure_us=self._safe_get_exposure(),
                        gain_x=self._safe_get_gain_x(),
                        is_trigger_frame=info.is_trigger_frame,
                        sdk_timestamp_01ms=info.sdk_timestamp_01ms,
                        frame_exposure_us=info.frame_exposure_us,
                        frame_gain_x=info.frame_gain_x,
                        frame_gamma=info.frame_gamma,
                        frame_contrast=info.frame_contrast,
                    )
            time.sleep(0.001)
        return None

    def reset_capture_count(self) -> None:
        with self._frame_lock:
            self.capture_count = 0

    def reset_timeout_counters(self) -> None:
        """开始新的预览/触发会话时清零分模式超时统计。"""
        with self._frame_lock:
            self.timeout_count = 0
            self.trigger_wait_count = 0
            if "timeout" in self.last_error.lower() or "超时" in self.last_error:
                self.last_error = ""

    def set_output_format_8bit(self) -> None:
        with self._control_lock:
            self._set_media_type(self._mvsdk.CAMERA_MEDIA_TYPE_MONO8)
            self._mvsdk.CameraSetIspOutFormat(self.h_camera, self._mvsdk.CAMERA_MEDIA_TYPE_MONO8)
            self.current_output_format = 8

    def set_output_format_12bit_packed(self) -> None:
        with self._control_lock:
            self._set_media_type(self._mvsdk.CAMERA_MEDIA_TYPE_MONO12_PACKED)
            self._mvsdk.CameraSetIspOutFormat(self.h_camera, self._mvsdk.CAMERA_MEDIA_TYPE_MONO16)
            self.current_output_format = 12

    def _set_media_type(self, media_type: int) -> None:
        if self.cap is None:
            return
        for index in range(int(self.cap.iMediaTypeDesc)):
            desc = self.cap.pMediaTypeDesc[index]
            if int(desc.iMediaType) == int(media_type):
                self._mvsdk.CameraSetMediaType(self.h_camera, int(desc.iIndex))
                return
        raise CameraError(f"相机不支持请求的图像格式: {media_type:#x}")

    def set_frame_speed(self, speed: int) -> None:
        with self._control_lock:
            self._mvsdk.CameraSetFrameSpeed(self.h_camera, int(speed))

    def set_trigger_mode(self, mode: int) -> None:
        mode = int(mode)
        changed = mode != self.current_trigger_mode
        with self._control_lock:
            self._mvsdk.CameraSetTriggerMode(self.h_camera, mode)
            if mode == 1:
                try:
                    self._mvsdk.CameraSetTriggerCount(self.h_camera, 1)
                except Exception:
                    pass
            self.current_trigger_mode = mode
        if changed:
            self.reset_timeout_counters()

    def set_auto_exposure(self, enabled: bool) -> None:
        with self._control_lock:
            self._mvsdk.CameraSetAeState(self.h_camera, int(enabled))

    def set_ae_target(self, value: int) -> None:
        with self._control_lock:
            self._mvsdk.CameraSetAeTarget(self.h_camera, int(value))

    def set_exposure(self, value_us: float) -> None:
        with self._control_lock:
            self._mvsdk.CameraSetExposureTime(self.h_camera, float(value_us))

    def get_exposure(self) -> float:
        return float(self._mvsdk.CameraGetExposureTime(self.h_camera))

    def set_gain_x(self, value: float) -> None:
        with self._control_lock:
            self._mvsdk.CameraSetAnalogGainX(self.h_camera, float(value))

    def get_gain_x(self) -> float:
        return float(self._mvsdk.CameraGetAnalogGainX(self.h_camera))

    def exposure_range(self) -> CameraExposureRange:
        exposure_min, exposure_max, exposure_step = self._exposure_time_range()
        gain_min, gain_max, gain_step = self._gain_x_range()
        return CameraExposureRange(
            exposure_min_us=exposure_min,
            exposure_max_us=exposure_max,
            exposure_step_us=exposure_step,
            gain_min_x=gain_min,
            gain_max_x=gain_max,
            gain_step_x=gain_step,
        )

    def roi(self) -> CameraRoi:
        if self.cap is None:
            raise CameraError("相机未初始化")
        limit = self.cap.sResolutionRange
        try:
            _, _, _, sensor_x, sensor_y, width, height, _, _ = self._mvsdk.CameraGetImageResolutionEx(self.h_camera)
            x, y = self._sensor_roi_to_display_roi(
                int(sensor_x),
                int(sensor_y),
                int(width),
                int(height),
                int(limit.iWidthMax),
                int(limit.iHeightMax),
            )
            width = int(width)
            height = int(height)
        except Exception:
            current = self._mvsdk.CameraGetImageResolution(self.h_camera)
            x = int(current.iHOffsetFOV)
            y = int(current.iVOffsetFOV)
            width = int(current.iWidthFOV or current.iWidth)
            height = int(current.iHeightFOV or current.iHeight)
        return CameraRoi(
            x=x,
            y=y,
            width=width,
            height=height,
            max_width=int(limit.iWidthMax),
            max_height=int(limit.iHeightMax),
            min_width=int(limit.iWidthMin),
            min_height=int(limit.iHeightMin),
            bin_sum_mask=int(limit.uBinSumModeMask),
            bin_average_mask=int(limit.uBinAverageModeMask),
            skip_mask=int(limit.uSkipModeMask),
        )

    def set_sensor_roi(self, x: int, y: int, width: int, height: int) -> tuple[int, int, int, int]:
        if self.cap is None:
            raise CameraError("相机未初始化")
        limit = self.cap.sResolutionRange
        min_width = int(limit.iWidthMin or 0)
        min_height = int(limit.iHeightMin or 0)
        max_width = int(limit.iWidthMax or 0)
        max_height = int(limit.iHeightMax or 0)
        x, y, width, height = self._normalize_sensor_roi(
            int(x),
            int(y),
            int(width),
            int(height),
            min_width,
            min_height,
            max_width,
            max_height,
        )
        safe_min_width, safe_min_height, safe_max_width, safe_max_height = self._safe_sensor_roi_limits(
            min_width,
            min_height,
            max_width,
            max_height,
        )
        if width < safe_min_width or height < safe_min_height:
            raise CameraError(f"ROI 尺寸小于相机限制: 最小 {safe_min_width}x{safe_min_height}")
        if x < 0 or y < 0 or x + width > safe_max_width or y + height > safe_max_height:
            raise CameraError(f"ROI 超出传感器范围: 最大 {safe_max_width}x{safe_max_height}")

        with self._control_lock:
            self._try_call("CameraStop")
            try:
                sensor_x, sensor_y, sensor_width, sensor_height = self._display_roi_to_sensor_roi(
                    x,
                    y,
                    width,
                    height,
                    safe_max_width,
                    safe_max_height,
                )
                err_code = self._set_sensor_roi_resolution(sensor_x, sensor_y, sensor_width, sensor_height)
                if err_code:
                    raise CameraError(f"设置 ROI 失败，SDK 错误码: {err_code}")
                self.width = width
                self.height = height
                with self._frame_lock:
                    self.latest_frame = None
                    self._latest_info = None
                    self.latest_frame_time = None
                self._try_call("CameraClearBuffer")
            finally:
                self._mvsdk.CameraPlay(self.h_camera)
        return x, y, width, height

    def reset_sensor_roi(self) -> tuple[int, int, int, int]:
        if self.cap is None:
            raise CameraError("相机未初始化")
        limit = self.cap.sResolutionRange
        return self.set_sensor_roi(0, 0, int(limit.iWidthMax), int(limit.iHeightMax))

    def _safe_sensor_roi_limits(
        self,
        min_width: int,
        min_height: int,
        max_width: int,
        max_height: int,
    ) -> tuple[int, int, int, int]:
        align = self._SENSOR_ROI_ALIGN
        max_width = int(max_width or 0)
        max_height = int(max_height or 0)
        if max_width <= 0 or max_height <= 0:
            raise CameraError(f"相机返回的 ROI 最大范围无效: {max_width}x{max_height}")

        min_width = max(1, int(min_width or 0))
        min_height = max(1, int(min_height or 0))
        min_width = min(max_width, max(align, ((min_width + align - 1) // align) * align))
        min_height = min(max_height, max(align, ((min_height + align - 1) // align) * align))
        return min_width, min_height, max_width, max_height

    def _set_sensor_roi_resolution(self, x: int, y: int, width: int, height: int) -> int:
        try:
            return int(self._mvsdk.CameraSetImageResolutionEx(self.h_camera, 0xFF, 0, 0, x, y, width, height, 0, 0))
        except AttributeError:
            resolution = self._mvsdk.CameraGetImageResolution(self.h_camera)
            resolution.iIndex = 0xFF
            resolution.uBinSumMode = 0
            resolution.uBinAverageMode = 0
            resolution.uSkipMode = 0
            resolution.iHOffsetFOV = x
            resolution.iVOffsetFOV = y
            resolution.iWidthFOV = width
            resolution.iHeightFOV = height
            resolution.iWidth = width
            resolution.iHeight = height
            resolution.iWidthZoomHd = 0
            resolution.iHeightZoomHd = 0
            resolution.iWidthZoomSw = 0
            resolution.iHeightZoomSw = 0
            return int(self._mvsdk.CameraSetImageResolution(self.h_camera, resolution))

    @staticmethod
    def _display_roi_to_sensor_roi(
        x: int,
        y: int,
        width: int,
        height: int,
        _max_width: int,
        max_height: int,
    ) -> tuple[int, int, int, int]:
        # SDK 的 sensor ROI 使用传感器坐标；CameraImageProcess 输出图像与 sensor 的 Y 方向相反。
        sensor_y = max(0, int(max_height) - int(y) - int(height))
        return int(x), sensor_y, int(width), int(height)

    @staticmethod
    def _sensor_roi_to_display_roi(
        sensor_x: int,
        sensor_y: int,
        width: int,
        height: int,
        _max_width: int,
        max_height: int,
    ) -> tuple[int, int]:
        display_y = max(0, int(max_height) - int(sensor_y) - int(height))
        return int(sensor_x), display_y

    def _normalize_sensor_roi(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        min_width: int,
        min_height: int,
        max_width: int,
        max_height: int,
    ) -> tuple[int, int, int, int]:
        # 自定义 sensor ROI 对硬件步进更敏感；统一 8 像素对齐，避免 SDK 接受参数后停止出帧。
        align = self._SENSOR_ROI_ALIGN
        min_width, min_height, max_width, max_height = self._safe_sensor_roi_limits(
            min_width,
            min_height,
            max_width,
            max_height,
        )

        x = max(0, min(x, max_width - min_width))
        y = max(0, min(y, max_height - min_height))
        x = (x // align) * align
        y = (y // align) * align
        width = max(min_width, min(width, max_width - x))
        height = max(min_height, min(height, max_height - y))
        width = min(max_width - x, max(min_width, (width // align) * align))
        height = min(max_height - y, max(min_height, (height // align) * align))
        if x + width > max_width:
            x = max(0, max_width - width)
            x = (x // align) * align
        if y + height > max_height:
            y = max(0, max_height - height)
            y = (y // align) * align
        return x, y, width, height

    def apply_quantitative_profile(self) -> dict[str, object]:
        exposure = self._safe_get_exposure()
        gain = self._safe_get_gain_x()
        with self._control_lock:
            self._mvsdk.CameraSetAeState(self.h_camera, 0)
            if exposure is not None:
                self._mvsdk.CameraSetExposureTime(self.h_camera, float(exposure))
            if gain is not None:
                self._mvsdk.CameraSetAnalogGainX(self.h_camera, float(gain))
            self._try_call("CameraSetLutMode", 0)
            self._try_call("CameraSetGamma", 100)
            self._try_call("CameraSetContrast", 100)
            self._try_call("CameraSetSaturation", 100)
            self._try_call("CameraSetSharpness", 0)
            self._try_call("CameraSetAntiFlick", 0)
            self._try_call("CameraSetBlackLevel", 0)
            self._try_call("CameraSetWhiteLevel", 256)
            self._try_call("CameraSetCorrectDeadPixel", 1)
            self._try_call("CameraSetNoiseFilter", 0)
            self._try_call("CameraFlatFieldingCorrectSetEnable", 0)
            try:
                self._mvsdk.CameraSetDenoise3DParams(self.h_camera, 0, 2, None)
            except Exception:
                pass
        return self.capture_signature()

    def frame_statistic(self) -> CameraFrameStatistic | None:
        if not self.initialized:
            return None
        try:
            stat = self._mvsdk.CameraGetFrameStatistic(self.h_camera)
            return CameraFrameStatistic(total=int(stat.iTotal), capture=int(stat.iCapture), lost=int(stat.iLost))
        except Exception:
            return None

    def capture_signature(self) -> dict[str, object]:
        roi = self.roi() if self.initialized else None
        stat = self.frame_statistic()
        return {
            "width": self.width,
            "height": self.height,
            "output_bit_depth": self.current_output_format,
            "auto_exposure": self._safe_get_auto_exposure(),
            "exposure_us": self._safe_get_exposure(),
            "gain_x": self._safe_get_gain_x(),
            "roi_x": None if roi is None else roi.x,
            "roi_y": None if roi is None else roi.y,
            "roi_width": None if roi is None else roi.width,
            "roi_height": None if roi is None else roi.height,
            "lut_mode": self._safe_get("CameraGetLutMode"),
            "gamma": self._safe_get("CameraGetGamma"),
            "contrast": self._safe_get("CameraGetContrast"),
            "sharpness": self._safe_get("CameraGetSharpness"),
            "black_level": self._safe_get("CameraGetBlackLevel"),
            "white_level": self._safe_get("CameraGetWhiteLevel"),
            "defect_correction": self._safe_get("CameraGetCorrectDeadPixel"),
            "noise_filter": self._safe_get("CameraGetNoiseFilterState"),
            "sdk_flat_fielding": self._safe_get("CameraFlatFieldingCorrectGetEnable"),
            "denoise3d": self._safe_get_denoise_enabled(),
            "sdk_total_frames": None if stat is None else stat.total,
            "sdk_capture_frames": None if stat is None else stat.capture,
            "sdk_lost_frames": None if stat is None else stat.lost,
        }

    def set_anti_flick(self, enabled: bool) -> None:
        with self._control_lock:
            self._mvsdk.CameraSetAntiFlick(self.h_camera, int(enabled))

    def set_light_frequency(self, frequency: int) -> None:
        with self._control_lock:
            self._mvsdk.CameraSetLightFrequency(self.h_camera, int(frequency))

    def set_contrast(self, value: int) -> None:
        with self._control_lock:
            self._mvsdk.CameraSetContrast(self.h_camera, int(value))

    def set_gamma(self, value: int) -> None:
        with self._control_lock:
            self._mvsdk.CameraSetGamma(self.h_camera, int(value))

    def set_saturation(self, value: int) -> None:
        with self._control_lock:
            self._mvsdk.CameraSetSaturation(self.h_camera, int(value))

    def set_sharpness(self, value: int) -> None:
        with self._control_lock:
            self._mvsdk.CameraSetSharpness(self.h_camera, int(value))

    def set_mirror(self, direction: int, enabled: bool) -> None:
        with self._control_lock:
            self._mvsdk.CameraSetMirror(self.h_camera, int(direction), int(enabled))

    def set_rotate(self, rotate: int) -> None:
        with self._control_lock:
            self._mvsdk.CameraSetRotate(self.h_camera, int(rotate))

    def set_once_white_balance(self) -> None:
        with self._control_lock:
            self._mvsdk.CameraSetOnceWB(self.h_camera)

    def health(self) -> CameraHealth:
        with self._frame_lock:
            frame_time = self.latest_frame_time
            age_ms = None if frame_time is None else max(0.0, (time.time() - frame_time) * 1000.0)
            stat = self.frame_statistic()
            return CameraHealth(
                capture_count=self.capture_count,
                sdk_error_count=self.sdk_error_count,
                timeout_count=self.timeout_count,
                trigger_wait_count=self.trigger_wait_count,
                last_error=self.last_error,
                latest_frame_age_ms=age_ms,
                sdk_total_frames=None if stat is None else stat.total,
                sdk_capture_frames=None if stat is None else stat.capture,
                sdk_lost_frames=None if stat is None else stat.lost,
                reconnect_count=self._safe_get("CameraGetReConnectCounts"),
            )

    def _exposure_time_range(self) -> tuple[float, float, float | None]:
        try:
            min_us, max_us, step_us = self._mvsdk.CameraGetExposureTimeRange(self.h_camera)
            if max_us > min_us:
                return float(min_us), float(max_us), float(step_us) if step_us > 0 else None
        except Exception:
            pass
        if self.cap is not None:
            desc = self.cap.sExposeDesc
            try:
                line_us = float(self._mvsdk.CameraGetExposureLineTime(self.h_camera))
            except Exception:
                line_us = 1.0
            return (
                float(desc.uiExposeTimeMin) * line_us,
                float(desc.uiExposeTimeMax) * line_us,
                line_us if line_us > 0 else None,
            )
        return 1.0, 1_000_000.0, None

    def _gain_x_range(self) -> tuple[float, float, float | None]:
        try:
            min_x, max_x, step_x = self._mvsdk.CameraGetAnalogGainXRange(self.h_camera)
            if max_x > min_x:
                return float(min_x), float(max_x), float(step_x) if step_x > 0 else None
        except Exception:
            pass
        if self.cap is not None:
            desc = self.cap.sExposeDesc
            step = float(desc.fAnalogGainStep) if desc.fAnalogGainStep else 1.0
            return float(desc.uiAnalogGainMin) * step, float(desc.uiAnalogGainMax) * step, step
        return 1.0, 64.0, None

    def _try_call(self, name: str, *args: object) -> None:
        func = getattr(self._mvsdk, name, None)
        if func is None:
            return
        try:
            func(self.h_camera, *args)
        except Exception:
            pass

    def _safe_get(self, name: str) -> object | None:
        if not self.initialized:
            return None
        func = getattr(self._mvsdk, name, None)
        if func is None:
            return None
        try:
            return func(self.h_camera)
        except Exception:
            return None

    def _safe_get_auto_exposure(self) -> bool | None:
        value = self._safe_get("CameraGetAeState")
        return None if value is None else bool(value)

    def _safe_get_denoise_enabled(self) -> bool | None:
        if not self.initialized:
            return None
        try:
            enabled, *_ = self._mvsdk.CameraGetDenoise3DParams(self.h_camera)
            return bool(enabled)
        except Exception:
            return None

    def _safe_get_exposure(self) -> float | None:
        if not self.initialized:
            return None
        try:
            return self.get_exposure()
        except Exception:
            return None

    def _safe_get_gain_x(self) -> float | None:
        if not self.initialized:
            return None
        try:
            return self.get_gain_x()
        except Exception:
            return None

    def close(self) -> None:
        self._running = False
        if self._grab_thread is not None:
            self._grab_thread.join(timeout=1.0)
            self._grab_thread = None
        if self.h_camera is not None:
            try:
                self._mvsdk.CameraUnInit(self.h_camera)
            finally:
                self.h_camera = None
        if self.frame_buffer is not None:
            try:
                self._mvsdk.CameraAlignFree(self.frame_buffer)
            finally:
                self.frame_buffer = None
