from __future__ import annotations

import cv2
import numpy as np

from grab_app.camera import CameraFrame
import grab_app.services.scanner as scanner_module
from grab_app.services.scanner import FlatFieldCalibration, ScanConfig, ScanWorker


class FakeCamera:
    def __init__(self) -> None:
        self.capture_count = 0
        self.buffer = np.zeros((6, 6), dtype=np.uint8)
        self.trigger_modes: list[int] = []
        self.output_formats: list[int] = []

    def set_output_format_8bit(self) -> None:
        self.output_formats.append(8)

    def set_output_format_12bit_packed(self) -> None:
        self.output_formats.append(12)

    def set_trigger_mode(self, mode: int) -> None:
        self.trigger_modes.append(mode)

    def apply_quantitative_profile(self) -> dict[str, object]:
        return self.capture_signature()

    def capture_signature(self) -> dict[str, object]:
        return {
            "width": 6,
            "height": 6,
            "output_bit_depth": self.output_formats[-1] if self.output_formats else 8,
            "exposure_us": 100.0,
            "gain_x": 1.0,
        }

    def soft_trigger_and_grab_sample(self, timeout_ms: int = 2000) -> CameraFrame:
        self.capture_count += 1
        self.buffer[:, :] = self.capture_count
        return CameraFrame(
            frame=self.buffer,
            capture_count=self.capture_count,
            captured_at=float(self.capture_count),
            trigger_started_at=float(self.capture_count) - 0.1,
            exposure_us=100.0,
            gain_x=1.0,
        )

    def grab_sample(self) -> CameraFrame:
        return self.soft_trigger_and_grab_sample()


class FakePzt:
    def __init__(self) -> None:
        self.position = 0.0

    def send_move(self, channel: int, value: float) -> None:
        self.position = value

    def read_move(self, channel: int) -> float:
        return self.position


def _scan_config(tmp_path, mode: str = "normal", step_um: float = 1.0) -> ScanConfig:
    return ScanConfig(
        mode=mode,
        channel=0,
        start_um=0.0,
        end_um=2.0,
        step_um=step_um,
        stable_ms=0,
        repeats=1,
        trigger_mode="soft",
        save_dir=tmp_path,
        prefix="img",
        extension="png",
        bit_depth=8,
    )


def test_scan_folder_name_uses_mode_step_and_minute(tmp_path, monkeypatch) -> None:
    real_datetime = scanner_module.datetime

    class FixedDatetime:
        @classmethod
        def now(cls):
            return real_datetime(2026, 6, 25, 16, 30, 45)

    monkeypatch.setattr(scanner_module, "datetime", FixedDatetime)
    worker = ScanWorker(FakeCamera(), FakePzt(), lambda *_: None, lambda *_: None)
    config = _scan_config(tmp_path, mode="center", step_um=0.5)

    first = worker._next_scan_folder(config)
    first.mkdir()
    second = worker._next_scan_folder(config)

    assert first.name == "CS-0.5um-06251630"
    assert second.name == "CS-0.5um-06251630-02"


def test_async_save_binds_frame_copy_to_scan_sequence(tmp_path) -> None:
    camera = FakeCamera()
    pzt = FakePzt()
    worker = ScanWorker(camera, pzt, lambda *_: None, lambda *_: None)
    config = _scan_config(tmp_path)

    result = worker._scan(config)

    assert result.completed_images == 3
    assert result.saved_images == 3
    round_folder = result.folder / "Round_01"
    values = [
        int(cv2.imread(str(round_folder / f"img_{idx:04d}.png"), cv2.IMREAD_GRAYSCALE)[0, 0])
        for idx in range(1, 4)
    ]
    assert values == [1, 2, 3]
    assert camera.output_formats == [8]
    assert camera.trigger_modes == [1, 0]


def test_save_frame_preserves_12bit_uint16_png(tmp_path) -> None:
    worker = ScanWorker(FakeCamera(), FakePzt(), lambda *_: None, lambda *_: None)
    frame = np.array([[0, 1024], [2048, 4095]], dtype=np.uint16)
    path = tmp_path / "sample.png"

    worker._save_frame(path, frame, 12)

    saved = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    assert saved.dtype == np.uint16
    assert saved.tolist() == np.left_shift(frame, 4).tolist()
    assert np.right_shift(saved, 4).tolist() == frame.tolist()


def test_flat_field_correction_preserves_raw_and_corrected_outputs(tmp_path) -> None:
    camera = FakeCamera()
    pzt = FakePzt()
    worker = ScanWorker(camera, pzt, lambda *_: None, lambda *_: None)
    signature = {
        "width": 6,
        "height": 6,
        "output_bit_depth": 8,
        "exposure_us": 100.0,
        "gain_x": 1.0,
    }
    config = ScanConfig(
        mode="normal",
        channel=0,
        start_um=0.0,
        end_um=1.0,
        step_um=1.0,
        stable_ms=0,
        repeats=1,
        trigger_mode="soft",
        save_dir=tmp_path,
        prefix="img",
        extension="png",
        bit_depth=8,
        flat_field_calibration=FlatFieldCalibration(
            dark=np.zeros((6, 6), dtype=np.float32),
            flat=np.full((6, 6), 2, dtype=np.float32),
            signature=signature,
        ),
    )

    result = worker._scan(config)

    assert result.completed_images == 2
    assert result.saved_images == 4
    raw_folder = result.folder / "raw" / "Round_01"
    corrected_folder = result.folder / "corrected" / "Round_01"
    raw_values = [
        int(cv2.imread(str(raw_folder / f"img_{idx:04d}.png"), cv2.IMREAD_GRAYSCALE)[0, 0])
        for idx in range(1, 3)
    ]
    corrected_values = [
        int(cv2.imread(str(corrected_folder / f"img_{idx:04d}.png"), cv2.IMREAD_GRAYSCALE)[0, 0])
        for idx in range(1, 3)
    ]
    assert raw_values == [1, 2]
    assert corrected_values == [1, 2]
    assert (result.folder / "calibration" / "dark_average.tiff").exists()
    assert (result.folder / "calibration" / "flat_average.tiff").exists()


def test_flat_field_correction_rejects_mismatched_signature(tmp_path) -> None:
    worker = ScanWorker(FakeCamera(), FakePzt(), lambda *_: None, lambda *_: None)
    config = ScanConfig(
        mode="normal",
        channel=0,
        start_um=0.0,
        end_um=1.0,
        step_um=1.0,
        stable_ms=0,
        repeats=1,
        trigger_mode="soft",
        save_dir=tmp_path,
        prefix="img",
        extension="png",
        bit_depth=8,
        flat_field_calibration=FlatFieldCalibration(
            dark=np.zeros((6, 6), dtype=np.float32),
            flat=np.ones((6, 6), dtype=np.float32),
            signature={"width": 6, "height": 6, "exposure_us": 200.0},
        ),
    )

    try:
        worker._scan(config)
    except ValueError as exc:
        assert "校准与当前采集参数不一致" in str(exc)
    else:
        raise AssertionError("expected mismatched calibration to fail")
