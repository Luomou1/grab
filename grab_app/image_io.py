from __future__ import annotations

from pathlib import Path
import re

import cv2
import numpy as np


def next_numbered_path(directory: str | Path, stem: str, extension: str, digits: int = 4) -> Path:
    target_dir = Path(directory)
    suffix = extension if extension.startswith(".") else f".{extension}"
    pattern = re.compile(rf"^{re.escape(stem)}_(\d+){re.escape(suffix)}$", re.IGNORECASE)
    max_index = 0
    if target_dir.exists():
        for item in target_dir.iterdir():
            if not item.is_file():
                continue
            match = pattern.match(item.name)
            if match:
                max_index = max(max_index, int(match.group(1)))

    index = max_index + 1
    while True:
        candidate = target_dir / f"{stem}_{index:0{digits}d}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def save_image(path: str | Path, image: np.ndarray) -> bool:
    """使用 Python 文件接口写图，避免 Windows 中文路径下 cv2.imwrite 失败。"""
    target = Path(path)
    suffix = target.suffix.lower()
    if not suffix:
        raise ValueError(f"保存路径缺少图像扩展名: {target}")
    ok, encoded = cv2.imencode(suffix, image)
    if not ok:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(encoded.tobytes())
    return True
