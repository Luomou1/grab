from __future__ import annotations

from grab_app.update import _first_installer_url, _version_key, cleanup_update_cache


def test_version_key_compares_common_release_tags() -> None:
    assert _version_key("v1.1.0") > _version_key("v1.0.9")
    assert _version_key("1.0.10") > _version_key("v1.0.2")


def test_first_installer_url_picks_exe_asset() -> None:
    payload = {
        "assets": [
            {"name": "notes.txt", "browser_download_url": "https://example.invalid/notes.txt"},
            {
                "name": "grab_Setup_v1.2.0.exe",
                "browser_download_url": "https://example.invalid/setup.exe",
            },
        ]
    }

    assert _first_installer_url(payload) == "https://example.invalid/setup.exe"


def test_cleanup_update_cache_removes_old_versions_and_partials(tmp_path) -> None:
    current = tmp_path / "1.2.4"
    old = tmp_path / "1.2.3"
    current.mkdir()
    old.mkdir()
    (current / "grab_Setup_v1.2.4.exe").write_text("current", encoding="utf-8")
    (old / "grab_Setup_v1.2.3.exe").write_text("old", encoding="utf-8")
    (tmp_path / "download.exe.part").write_text("partial", encoding="utf-8")

    cleanup_update_cache("1.2.4", tmp_path)

    assert current.exists()
    assert (current / "grab_Setup_v1.2.4.exe").exists()
    assert not old.exists()
    assert not (tmp_path / "download.exe.part").exists()
