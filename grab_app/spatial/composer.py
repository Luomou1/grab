"""面向实时显示的低分辨率灰度拼图画布。"""

from __future__ import annotations

import cv2
import numpy as np


class MosaicComposer:
    """维护灰度画布、覆盖次数和融合质量。

    ``origin_px`` 使用全分辨率拼图坐标；内部按 ``downsample`` 缩小。羽化模式
    对重叠区加权平均，稳定覆盖模式只保留质量不低于现有值的像素。
    """

    def __init__(
        self,
        canvas_shape: tuple[int, int],
        *,
        downsample: int = 1,
        mode: str = "feather",
    ) -> None:
        if len(canvas_shape) != 2 or min(canvas_shape) <= 0:
            raise ValueError("canvas_shape 必须是正的 (height, width)")
        if downsample < 1:
            raise ValueError("downsample 必须大于等于 1")
        if mode not in {"feather", "stable"}:
            raise ValueError("mode 仅支持 feather 或 stable")
        self.downsample = int(downsample)
        self.mode = mode
        shape = tuple(int(np.ceil(v / self.downsample)) for v in canvas_shape)
        self.canvas = np.zeros(shape, dtype=np.float32)
        self.coverage = np.zeros(shape, dtype=np.uint32)
        self.quality = np.zeros(shape, dtype=np.float32)
        self._weight = np.zeros(shape, dtype=np.float32)

    @staticmethod
    def _gray(image: np.ndarray) -> np.ndarray:
        if image.ndim == 3 and image.shape[2] == 3:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        elif image.ndim == 3 and image.shape[2] == 4:
            image = cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
        if image.ndim != 2 or image.size == 0:
            raise ValueError("tile 必须是非空灰度、BGR 或 BGRA 图像")
        return np.asarray(image, dtype=np.float32)

    @staticmethod
    def _feather_weights(shape: tuple[int, int]) -> np.ndarray:
        height, width = shape
        y = np.minimum(np.arange(height) + 1, np.arange(height, 0, -1)).astype(np.float32)
        x = np.minimum(np.arange(width) + 1, np.arange(width, 0, -1)).astype(np.float32)
        weights = np.minimum.outer(y, x)
        return weights / float(weights.max())

    def add_tile(
        self,
        tile: np.ndarray,
        origin_px: tuple[float, float],
        *,
        quality: float = 1.0,
    ) -> bool:
        """将瓦片放入画布；完全落在画布外时返回 ``False``。"""
        if not np.isfinite(quality) or quality < 0:
            raise ValueError("quality 必须是非负有限数")
        gray = self._gray(tile)
        if self.downsample > 1:
            target_size = (
                max(1, int(round(gray.shape[1] / self.downsample))),
                max(1, int(round(gray.shape[0] / self.downsample))),
            )
            gray = cv2.resize(gray, target_size, interpolation=cv2.INTER_AREA)
        x0 = int(round(float(origin_px[0]) / self.downsample))
        y0 = int(round(float(origin_px[1]) / self.downsample))
        x1, y1 = x0 + gray.shape[1], y0 + gray.shape[0]
        clip_x0, clip_y0 = max(0, x0), max(0, y0)
        clip_x1, clip_y1 = min(self.canvas.shape[1], x1), min(self.canvas.shape[0], y1)
        if clip_x0 >= clip_x1 or clip_y0 >= clip_y1:
            return False

        tile_view = gray[clip_y0 - y0:clip_y1 - y0, clip_x0 - x0:clip_x1 - x0]
        target = np.s_[clip_y0:clip_y1, clip_x0:clip_x1]
        self.coverage[target] += 1
        if self.mode == "stable":
            canvas_view = self.canvas[target]
            quality_view = self.quality[target]
            weight_view = self._weight[target]
            replace = (weight_view == 0) | (quality >= quality_view)
            canvas_view[replace] = tile_view[replace]
            quality_view[replace] = quality
            weight_view[replace] = 1.0
        else:
            weights = self._feather_weights(gray.shape)[
                clip_y0 - y0:clip_y1 - y0, clip_x0 - x0:clip_x1 - x0
            ]
            weighted = weights * float(quality)
            old_weight = self._weight[target]
            new_weight = old_weight + weighted
            nonzero = new_weight > 0
            canvas = self.canvas[target]
            canvas[nonzero] = (
                canvas[nonzero] * old_weight[nonzero] + tile_view[nonzero] * weighted[nonzero]
            ) / new_weight[nonzero]
            quality_map = self.quality[target]
            quality_map[nonzero] = (
                quality_map[nonzero] * old_weight[nonzero] + quality * weighted[nonzero]
            ) / new_weight[nonzero]
            self._weight[target] = new_weight
        return True

    def image(self, dtype: np.dtype = np.dtype(np.uint8)) -> np.ndarray:
        """返回适合显示或保存的画布副本。"""
        if np.issubdtype(dtype, np.integer):
            limits = np.iinfo(dtype)
            return np.clip(self.canvas, limits.min, limits.max).astype(dtype)
        return self.canvas.astype(dtype, copy=True)


RealtimeMosaicComposer = MosaicComposer
