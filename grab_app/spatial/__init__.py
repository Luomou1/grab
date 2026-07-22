"""空间扫描、标定、拼图与任务存储公共 API。"""

from .models import (
    CalibrationFit,
    SpatialCalibration,
    SpatialRect,
    StagePoint,
    TilePlacement,
    TilePlan,
)
from .calibration import (
    CalibrationQualityLimits,
    DEFAULT_CALIBRATION_QUALITY_LIMITS,
    DEFAULT_PIXEL_SIZE_UM,
    default_calibration,
    fit_affine,
    fit_affine_calibration,
    pixel_to_stage,
    stage_to_pixel,
    validate_calibration_quality,
)
from .planner import SafetyLimits, frame_footprint_mm, plan_center_scan, plan_grid, plan_tiles
from .registration import RegistrationResult, estimate_adjacent_translation, estimate_translation
from .composer import MosaicComposer, RealtimeMosaicComposer
from .storage import (
    SpatialJobStorage,
    atomic_write_json,
    create_job_directory,
    write_json_atomic,
)

__all__ = [
    "CalibrationFit", "SpatialCalibration", "SpatialRect", "StagePoint",
    "TilePlacement", "TilePlan", "CalibrationQualityLimits",
    "DEFAULT_CALIBRATION_QUALITY_LIMITS", "DEFAULT_PIXEL_SIZE_UM", "default_calibration",
    "fit_affine", "fit_affine_calibration", "pixel_to_stage", "stage_to_pixel",
    "validate_calibration_quality",
    "SafetyLimits", "frame_footprint_mm", "plan_center_scan", "plan_grid", "plan_tiles", "RegistrationResult",
    "estimate_adjacent_translation", "estimate_translation", "MosaicComposer",
    "RealtimeMosaicComposer", "SpatialJobStorage", "atomic_write_json", "write_json_atomic",
    "create_job_directory",
]
