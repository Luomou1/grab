from __future__ import annotations

from grab_app.update import _first_installer_url, _version_key


def test_version_key_compares_common_release_tags() -> None:
    assert _version_key("v1.1.0") > _version_key("v1.0.9")
    assert _version_key("1.0.10") > _version_key("v1.0.2")


def test_first_installer_url_picks_exe_asset() -> None:
    payload = {
        "assets": [
            {"name": "notes.txt", "browser_download_url": "https://example.invalid/notes.txt"},
            {
                "name": "HTGE采集程序_Setup_v1.1.0.exe",
                "browser_download_url": "https://example.invalid/setup.exe",
            },
        ]
    }

    assert _first_installer_url(payload) == "https://example.invalid/setup.exe"
