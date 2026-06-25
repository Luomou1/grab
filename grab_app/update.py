from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from urllib.error import HTTPError, URLError


LATEST_RELEASE_API = "https://api.github.com/repos/Luomou1/grab/releases/latest"
LATEST_RELEASE_PAGE = "https://github.com/Luomou1/grab/releases/latest"


@dataclass(frozen=True)
class UpdateInfo:
    current_version: str
    latest_version: str
    release_url: str
    download_url: str
    is_newer: bool


def check_latest_release(current_version: str, timeout: float = 8.0) -> UpdateInfo:
    request = urllib.request.Request(
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "HTGE-Acquisition-Updater",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"检查更新失败: {exc}") from exc

    latest_version = str(payload.get("tag_name") or "").strip()
    if not latest_version:
        raise RuntimeError("检查更新失败: GitHub Release 未返回版本号")

    release_url = str(payload.get("html_url") or LATEST_RELEASE_PAGE)
    download_url = _first_installer_url(payload) or release_url
    return UpdateInfo(
        current_version=current_version,
        latest_version=latest_version,
        release_url=release_url,
        download_url=download_url,
        is_newer=_version_key(latest_version) > _version_key(current_version),
    )


def _first_installer_url(payload: dict[str, object]) -> str:
    assets = payload.get("assets")
    if not isinstance(assets, list):
        return ""
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "").lower()
        url = str(asset.get("browser_download_url") or "")
        if name.endswith(".exe") and url:
            return url
    return ""


def _version_key(version: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", version)
    return tuple(int(value) for value in numbers[:4]) or (0,)
