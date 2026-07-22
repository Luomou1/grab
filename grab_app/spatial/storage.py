"""空间任务目录与可恢复状态的轻量存储。"""

from __future__ import annotations

import csv
from dataclasses import asdict, is_dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

import cv2
import numpy as np


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"无法序列化类型: {type(value)!r}")


def atomic_write_json(path: str | Path, data: Mapping[str, Any] | Any) -> Path:
    """先写同目录临时文件，再原子替换目标 JSON。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2, default=_json_default)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return target


def create_job_directory(base_dir: str | Path, job_id: str | None = None) -> Path:
    """创建任务目录及设计文档约定的子目录。"""
    root = Path(base_dir)
    name = job_id or f"SpatialScan-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    target = root / name
    suffix = 1
    while target.exists():
        target = root / f"{name}-{suffix:02d}"
        suffix += 1
    for relative in (
        "calibration", "survey/tiles", "plan", "acquisition",
    ):
        (target / relative).mkdir(parents=True, exist_ok=False)
    return target


class SpatialJobStorage:
    """一个空间任务的状态、索引和预览文件写入器。"""

    def __init__(self, job_dir: str | Path) -> None:
        self.job_dir = Path(job_dir)
        self.job_dir.mkdir(parents=True, exist_ok=True)
        for relative in ("calibration", "survey/tiles", "plan", "acquisition"):
            (self.job_dir / relative).mkdir(parents=True, exist_ok=True)
        self.job_path = self.job_dir / "job.json"
        self.index_path = self.job_dir / "survey" / "tile_index.csv"
        self._latest_preview: np.ndarray | None = None

    @classmethod
    def create(cls, base_dir: str | Path, job_id: str | None = None) -> "SpatialJobStorage":
        return cls(create_job_directory(base_dir, job_id))

    @staticmethod
    def create_job_directory(base_dir: str | Path, prefix: str = "SpatialScan") -> Path:
        """兼容扫描总控的目录工厂：按前缀附加时间戳并避免重名。"""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return create_job_directory(base_dir, f"{prefix}-{stamp}")

    def write_state(self, state: Mapping[str, Any] | Any, *, filename: str = "job.json") -> Path:
        """原子更新任务状态（默认写入 job.json）。"""
        return atomic_write_json(self.job_dir / filename, state)

    update_state = write_state

    def write_route(self, plan: Any) -> Path:
        """保存可恢复的网格路线。"""
        return self.write_state(plan, filename="plan/route.json")

    def update_job_state(self, status: str, completed: int, total: int) -> Path:
        return self.write_state({
            "state": status, "completed_tiles": int(completed), "total_tiles": int(total),
        })

    def append_tile_index(self, row: Mapping[str, Any] | Any) -> Path:
        """追加一条瓦片索引；首次调用写入表头。"""
        values = asdict(row) if is_dataclass(row) else dict(row)
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        exists = self.index_path.exists() and self.index_path.stat().st_size > 0
        if not exists:
            fieldnames = list(values)
        else:
            with self.index_path.open("r", encoding="utf-8", newline="") as handle:
                fieldnames = next(csv.reader(handle), list(values))
        with self.index_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            if not exists:
                writer.writeheader()
            writer.writerow({name: values.get(name, "") for name in fieldnames})
            handle.flush()
            os.fsync(handle.fileno())
        return self.index_path

    def append_tile(self, placement: Any, actual_position: Any, sample: Any, tile_path: str | Path) -> Path:
        """记录概览瓦片，并缓存最后一帧作为无 UI 依赖的预览。"""
        frame = getattr(sample, "frame", None)
        if isinstance(frame, np.ndarray):
            self._latest_preview = frame.copy()
        captured_at = getattr(sample, "captured_at", None)
        row = {
            "row": getattr(placement, "row", ""), "column": getattr(placement, "column", ""),
            "target_x_mm": getattr(getattr(placement, "target", None), "x_mm", ""),
            "target_y_mm": getattr(getattr(placement, "target", None), "y_mm", ""),
            "actual_x_mm": actual_position[0] if actual_position is not None else "",
            "actual_y_mm": actual_position[1] if actual_position is not None else "",
            "capture_time": captured_at if captured_at is not None else "",
            "path": str(tile_path), "status": "completed",
        }
        return self.append_tile_index(row)

    def append_acquisition_tile(self, placement: Any, actual_position: Any, result: Any) -> Path:
        row = {
            "row": getattr(placement, "row", ""), "column": getattr(placement, "column", ""),
            "actual_x_mm": actual_position[0] if actual_position is not None else "",
            "actual_y_mm": actual_position[1] if actual_position is not None else "",
            "folder": str(getattr(result, "folder", "")),
            "completed_images": getattr(result, "completed_images", ""),
            "status": "stopped" if getattr(result, "stopped", False) else "completed",
        }
        return self.append_tile_index(row)

    def save_preview_if_available(self) -> Path | None:
        if self._latest_preview is None:
            return None
        return self.save_preview(self._latest_preview)

    def save_preview(self, image: np.ndarray | None = None, *, filename: str = "preview.png") -> Path:
        """保存预览；未传图像时使用最近一次概览帧或生成黑色占位图。"""
        if image is None:
            image = self._latest_preview
        if image is None:
            image = np.zeros((1, 1), dtype=np.uint8)
        target = self.job_dir / "survey" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        ok, encoded = cv2.imencode(target.suffix or ".png", image)
        if not ok:
            raise ValueError("预览图编码失败")
        target.write_bytes(encoded.tobytes())
        return target


write_json_atomic = atomic_write_json
