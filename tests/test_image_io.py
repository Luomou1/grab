from __future__ import annotations

import cv2
import numpy as np

from grab_app.image_io import next_numbered_path, save_image


def test_save_image_supports_unicode_windows_paths(tmp_path) -> None:
    image = np.full((5, 7), 123, dtype=np.uint8)
    path = tmp_path / "中文目录" / "图像.bmp"

    assert save_image(path, image)

    data = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    decoded = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
    assert decoded is not None
    assert decoded.shape == image.shape
    assert int(decoded[0, 0]) == 123


def test_next_numbered_path_continues_from_existing_files(tmp_path) -> None:
    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir()
    (snapshot_dir / "snapshot_0001.bmp").write_bytes(b"old")
    (snapshot_dir / "snapshot_0003.bmp").write_bytes(b"old")
    (snapshot_dir / "other.txt").write_text("ignore", encoding="utf-8")

    assert next_numbered_path(snapshot_dir, "snapshot", "bmp").name == "snapshot_0004.bmp"
    assert next_numbered_path(snapshot_dir, "snapshot", "png").name == "snapshot_0001.png"
