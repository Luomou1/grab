"""基于预计重叠区域的相邻灰度瓦片平移配准。"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(frozen=True)
class RegistrationResult:
    """相邻瓦片相对首张瓦片的预计平移及可信度。"""

    dx_px: float
    dy_px: float
    confidence: float
    success: bool
    used_fallback: bool = False

    @property
    def translation_px(self) -> tuple[float, float]:
        return self.dx_px, self.dy_px


def _gray_float(image: np.ndarray) -> np.ndarray:
    if image.ndim == 3:
        if image.shape[2] == 3:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        elif image.shape[2] == 4:
            image = cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
        else:
            raise ValueError("仅支持灰度、BGR 或 BGRA 图像")
    if image.ndim != 2 or image.size == 0:
        raise ValueError("图像必须是非空二维数组")
    return np.asarray(image, dtype=np.float32)


def estimate_adjacent_translation(
    reference: np.ndarray,
    moving: np.ndarray,
    *,
    direction: str = "right",
    overlap: float = 0.2,
    min_confidence: float = 0.1,
) -> RegistrationResult:
    """在预期重叠条带内估算 ``moving`` 相对 ``reference`` 的平移。

    返回值包含瓦片原点的完整相对平移。纹理不足、响应非有限或响应低于
    阈值时，安全回退到由帧尺寸和重叠率给出的位移台位置先验。
    """
    if direction not in {"right", "left", "down", "up"}:
        raise ValueError("direction 必须是 right、left、down 或 up")
    if not 0.0 < overlap < 1.0:
        raise ValueError("overlap 必须位于 (0, 1) 范围")
    if not np.isfinite(min_confidence):
        raise ValueError("min_confidence 必须是有限数")

    ref = _gray_float(reference)
    mov = _gray_float(moving)
    height, width = min(ref.shape[0], mov.shape[0]), min(ref.shape[1], mov.shape[1])
    ref, mov = ref[:height, :width], mov[:height, :width]

    if direction in {"right", "left"}:
        band = max(2, int(round(width * overlap)))
        if direction == "right":
            ref_roi, mov_roi = ref[:, width - band:], mov[:, :band]
            fallback = (float(width - band), 0.0)
        else:
            ref_roi, mov_roi = ref[:, :band], mov[:, width - band:]
            fallback = (float(-(width - band)), 0.0)
    else:
        band = max(2, int(round(height * overlap)))
        if direction == "down":
            ref_roi, mov_roi = ref[height - band:, :], mov[:band, :]
            fallback = (0.0, float(height - band))
        else:
            ref_roi, mov_roi = ref[:band, :], mov[height - band:, :]
            fallback = (0.0, float(-(height - band)))

    if ref_roi.shape != mov_roi.shape or min(ref_roi.shape) < 2:
        return RegistrationResult(*fallback, 0.0, False, True)
    if float(np.std(ref_roi)) < 1e-6 or float(np.std(mov_roi)) < 1e-6:
        return RegistrationResult(*fallback, 0.0, False, True)

    window = cv2.createHanningWindow((ref_roi.shape[1], ref_roi.shape[0]), cv2.CV_32F)
    try:
        (residual_x, residual_y), response = cv2.phaseCorrelate(ref_roi, mov_roi, window)
    except cv2.error:
        return RegistrationResult(*fallback, 0.0, False, True)
    confidence = float(response)
    if not all(np.isfinite(v) for v in (residual_x, residual_y, confidence)) or confidence < min_confidence:
        return RegistrationResult(*fallback, max(0.0, confidence) if np.isfinite(confidence) else 0.0, False, True)
    return RegistrationResult(
        fallback[0] + float(residual_x), fallback[1] + float(residual_y),
        confidence, True, False,
    )


estimate_translation = estimate_adjacent_translation
