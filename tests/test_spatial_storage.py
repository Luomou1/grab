from __future__ import annotations

import csv
import json
from types import SimpleNamespace

import cv2
import numpy as np

from grab_app.spatial import (
    SpatialJobStorage,
    SpatialRect,
    StagePoint,
    TilePlacement,
    atomic_write_json,
)


def test_job_storage_creates_layout_and_atomically_updates_state(tmp_path) -> None:
    storage = SpatialJobStorage.create(tmp_path / "中文根目录", "SpatialScan-test")

    assert (storage.job_dir / "calibration").is_dir()
    assert (storage.job_dir / "survey" / "tiles").is_dir()
    assert (storage.job_dir / "plan").is_dir()
    assert (storage.job_dir / "acquisition").is_dir()

    storage.write_state({"state": "SURVEY", "point": StagePoint(1.2, 3.4)})
    storage.update_state({"state": "COMPLETED"})
    assert json.loads(storage.job_path.read_text(encoding="utf-8")) == {"state": "COMPLETED"}
    assert not list(storage.job_dir.glob(".job.json.*.tmp"))


def test_storage_appends_csv_and_saves_unicode_preview(tmp_path) -> None:
    storage = SpatialJobStorage.create(tmp_path, "任务")
    storage.append_tile_index({"row": 0, "column": 1, "status": "完成"})
    storage.append_tile_index({"row": 1, "column": 1, "status": "失败"})
    with storage.index_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[1] == {"row": "1", "column": "1", "status": "失败"}

    image = np.full((5, 7), 137, dtype=np.uint8)
    path = storage.save_preview(image)
    decoded = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_GRAYSCALE)
    assert decoded is not None
    assert np.array_equal(decoded, image)


def test_atomic_json_does_not_replace_target_when_serialization_fails(tmp_path) -> None:
    path = tmp_path / "state.json"
    atomic_write_json(path, {"ok": True})

    try:
        atomic_write_json(path, {"bad": object()})
    except TypeError:
        pass

    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}


def test_storage_scan_worker_compatibility_methods(tmp_path) -> None:
    folder = SpatialJobStorage.create_job_directory(tmp_path, "Survey")
    storage = SpatialJobStorage(folder)
    placement = TilePlacement(0, 0, StagePoint(1, 2), SpatialRect(0.5, 1.5, 1.5, 2.5))
    sample = SimpleNamespace(frame=np.full((3, 4), 77, np.uint8), captured_at=12.5)

    storage.write_route({"placements": [placement]})
    storage.append_tile(placement, (1.01, 1.99), sample, folder / "tile.tiff")
    storage.update_job_state("completed", 1, 1)
    preview = storage.save_preview()

    assert (folder / "plan" / "route.json").is_file()
    assert json.loads(storage.job_path.read_text(encoding="utf-8"))["state"] == "completed"
    assert preview.is_file()
