"""XY 位移台内核公共 API。"""

from .controller import MotionTimeoutError, XYStage, XYStageController
from .executor import MotionCancelledError, XYStageExecutor
from .models import (
    DEFAULT_PROFILE_PATH,
    AxisProfile,
    AxisStatus,
    DeviceProfile,
    DeviceSnapshot,
    ProfileError,
    load_default_profile,
    load_profile,
)
from .protocol import StageProtocol, XYStageProtocol
from .safety import SafetyViolation
from .sdk import FakeMotionSdk, FakeSdk, FmcSdk, MotionSdk, SdkError, SdkLoadError, SdkThreadError
from .trajectory import (
    ArcDefinition,
    ArcMove,
    CoordinateMode,
    LineMove,
    LinearPathPlan,
    Point2D,
    PositionTriggerConfig,
    PreparedPositionTrigger,
    TrajectoryValidationError,
)

__all__ = [
    "DEFAULT_PROFILE_PATH", "ArcDefinition", "ArcMove", "AxisProfile", "AxisStatus",
    "CoordinateMode", "DeviceProfile", "DeviceSnapshot", "FakeMotionSdk",
    "FakeSdk", "FmcSdk", "MotionCancelledError", "MotionSdk", "MotionTimeoutError", "ProfileError", "SafetyViolation",
    "LineMove", "LinearPathPlan", "Point2D", "PositionTriggerConfig", "PreparedPositionTrigger",
    "SdkError", "SdkLoadError", "SdkThreadError", "StageProtocol", "TrajectoryValidationError", "XYStage",
    "XYStageController", "XYStageExecutor", "XYStageProtocol", "load_default_profile", "load_profile",
]
