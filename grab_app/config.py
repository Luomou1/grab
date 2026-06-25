from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "grab"
PZT_ADDRESS = 0x01
PZT_CHANNELS = (0, 1, 2)
PZT_MIN_UM = 0.0
PZT_MAX_UM = 270.0
PZT_DEFAULT_BAUD = 115200
PZT_BAUD_RATES = (9600, 19200, 38400, 57600, 115200)
PZT_UDP_PORT = 7010
PZT_DEFAULT_IP = "192.168.0.100"


def app_icon_path() -> Path:
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    candidates = (
        bundle_root / "assets" / "grab.ico",
        Path(__file__).resolve().parents[1] / "packaging" / "assets" / "grab.ico",
    )
    for path in candidates:
        if path.exists():
            return path
    return candidates[-1]


@dataclass(frozen=True)
class CameraSdkPaths:
    vendor_x64: Path = Path(r"D:\HuaTengVision\SDK\X64")
    vendor_root: Path = Path(r"D:\HuaTengVision\SDK")
    local_camera: Path = Path(__file__).resolve().parent / "camera"

    def existing(self) -> list[Path]:
        return [path for path in (self.vendor_x64, self.vendor_root, self.local_camera) if path.exists()]
