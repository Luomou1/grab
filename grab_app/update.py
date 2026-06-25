from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse


LATEST_RELEASE_API = "https://api.github.com/repos/Luomou1/grab/releases/latest"
LATEST_RELEASE_PAGE = "https://github.com/Luomou1/grab/releases/latest"
UPDATE_CACHE_DIR = Path(tempfile.gettempdir()) / "grab-updates"


@dataclass(frozen=True)
class UpdateInfo:
    current_version: str
    latest_version: str
    release_url: str
    download_url: str
    asset_name: str
    asset_size: int
    asset_digest: str
    release_notes: str
    is_newer: bool


ProgressCallback = Callable[[int, int], None]


def check_latest_release(current_version: str, timeout: float = 8.0) -> UpdateInfo:
    request = urllib.request.Request(
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "grab-updater",
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
    installer_asset = _first_installer_asset(payload)
    download_url = str(installer_asset.get("browser_download_url") or "")
    if not download_url:
        raise RuntimeError("检查更新失败: 最新 Release 没有可下载的安装包")
    return UpdateInfo(
        current_version=current_version,
        latest_version=latest_version,
        release_url=release_url,
        download_url=download_url,
        asset_name=str(installer_asset.get("name") or _filename_from_url(download_url)),
        asset_size=int(installer_asset.get("size") or 0),
        asset_digest=str(installer_asset.get("digest") or ""),
        release_notes=str(payload.get("body") or ""),
        is_newer=_version_key(latest_version) > _version_key(current_version),
    )


def _first_installer_url(payload: dict[str, object]) -> str:
    return str(_first_installer_asset(payload).get("browser_download_url") or "")


def _first_installer_asset(payload: dict[str, object]) -> dict[str, object]:
    assets = payload.get("assets")
    if not isinstance(assets, list):
        return {}
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "").lower()
        if name.endswith(".exe") and asset.get("browser_download_url"):
            return asset
    return {}


def download_installer(
    update: UpdateInfo,
    progress: ProgressCallback | None = None,
    timeout: float = 20.0,
) -> Path:
    target_dir = UPDATE_CACHE_DIR / update.latest_version.lstrip("v")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / _safe_filename(update.asset_name or _filename_from_url(update.download_url))
    partial = target.with_suffix(target.suffix + ".part")
    request = urllib.request.Request(
        update.download_url,
        headers={"User-Agent": "grab-updater"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            total = int(response.headers.get("Content-Length") or update.asset_size or 0)
            received = 0
            with partial.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    handle.write(chunk)
                    received += len(chunk)
                    if progress is not None:
                        progress(received, total)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        if partial.exists():
            partial.unlink(missing_ok=True)
        raise RuntimeError(f"下载安装包失败: {exc}") from exc

    if target.exists():
        target.unlink()
    shutil.move(str(partial), target)
    _verify_digest(target, update.asset_digest)
    return target


def start_installer(installer_path: Path) -> None:
    subprocess.Popen(
        [
            str(installer_path),
            "/SP-",
            "/SILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/CLOSEAPPLICATIONS",
            "/RESTARTAPPLICATIONS",
        ],
        close_fds=True,
    )


def cleanup_update_cache(current_version: str, cache_dir: Path = UPDATE_CACHE_DIR) -> None:
    if not cache_dir.exists():
        return
    keep_dir_name = current_version.lstrip("v")
    for path in cache_dir.iterdir():
        try:
            if path.is_file() and path.suffix == ".part":
                path.unlink(missing_ok=True)
            elif path.is_dir() and path.name != keep_dir_name:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def _verify_digest(path: Path, digest: str) -> None:
    if not digest:
        return
    algorithm, _, expected = digest.partition(":")
    if algorithm.lower() != "sha256" or not expected:
        return
    import hashlib

    sha256 = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha256.update(chunk)
    actual = sha256.hexdigest()
    if actual.lower() != expected.lower():
        path.unlink(missing_ok=True)
        raise RuntimeError("安装包校验失败，请稍后重试")


def _filename_from_url(url: str) -> str:
    name = Path(urlparse(url).path).name
    return name or "grab_update.exe"


def _safe_filename(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return safe or "grab_update.exe"


def _version_key(version: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", version)
    return tuple(int(value) for value in numbers[:4]) or (0,)
