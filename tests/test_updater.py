"""
exam-to-html 自动更新测试 (M5-4)

测:
- is_newer semver 边界
- 节流 (24h) — 节流 vs 过期 vs force
- HTTP fetch — 200 / 4xx / 网络失败 / JSON 损坏
- check_update 端到端 (mock fetch_version_json)
- config last_check_ts 节流持久化

不需要 PDF2PPT / topic_garden 全套依赖, 只要 exam_to_html + 标准库。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


# ============================================================
# 1. semver 比对
# ============================================================
class TestSemver:
    def test_parse_basic(self):
        from exam_to_html.updater import _parse_semver
        assert _parse_semver("1.2.3") == (1, 2, 3)
        assert _parse_semver("0.1.0") == (0, 1, 0)

    def test_parse_2digit_vs_10_digit(self):
        """1.10.0 > 1.9.0 (numeric, not lexicographic)."""
        from exam_to_html.updater import _parse_semver
        assert _parse_semver("1.10.0") == (1, 10, 0)
        assert _parse_semver("1.9.0") == (1, 9, 0)

    def test_parse_pre_release(self):
        """'1.0.0-beta' → (1, 0, 0), pre-release 标签被忽略."""
        from exam_to_html.updater import _parse_semver
        assert _parse_semver("1.0.0-beta") == (1, 0, 0)
        assert _parse_semver("2.0.0-rc.1") == (2, 0, 0)

    def test_parse_invalid(self):
        from exam_to_html.updater import _parse_semver
        for bad in ["", None, "invalid", "1.2", "1", "1.2.3.4", "abc.def.ghi"]:
            assert _parse_semver(bad) is None, f"expected None for {bad!r}"

    def test_is_newer_true(self):
        from exam_to_html.updater import is_newer
        assert is_newer("1.2.0", "1.1.0") is True
        assert is_newer("1.10.0", "1.9.0") is True  # 关键: numeric
        assert is_newer("0.2.0", "0.1.0") is True
        assert is_newer("2.0.0", "1.99.99") is True

    def test_is_newer_false(self):
        from exam_to_html.updater import is_newer
        assert is_newer("1.0.0", "1.0.0") is False
        assert is_newer("0.9.0", "1.0.0") is False
        assert is_newer("1.0.0-beta", "1.0.0") is False  # pre-release 忽略

    def test_is_newer_none_on_invalid(self):
        from exam_to_html.updater import is_newer
        assert is_newer("invalid", "1.0.0") is None
        assert is_newer("1.0.0", "invalid") is None


# ============================================================
# 2. HTTP fetch 错误处理
# ============================================================
class TestFetch:
    def test_empty_url(self):
        from exam_to_html.updater import UpdateCheckError, fetch_version_json

        with pytest.raises(UpdateCheckError, match="不合法"):
            fetch_version_json("")

    def test_invalid_scheme(self):
        from exam_to_html.updater import UpdateCheckError, fetch_version_json

        for bad in ["not-a-url", "file:///etc/passwd", "ftp://example.com/x"]:
            with pytest.raises(UpdateCheckError, match="不合法"):
                fetch_version_json(bad)

    def test_network_failure(self):
        from exam_to_html.updater import UpdateCheckError, fetch_version_json

        with pytest.raises(UpdateCheckError, match="拉取失败"):
            fetch_version_json("http://invalid-host-xxx-12345.example.com/version.json")


# ============================================================
# 3. 节流
# ============================================================
class TestThrottle:
    def test_none_means_not_throttled(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from exam_to_html import config
        from exam_to_html.updater import _should_throttle

        cfg = config.load()
        assert cfg["last_check_ts"] is None
        assert _should_throttle(cfg["last_check_ts"], force=False) is False

    def test_recent_check_throttled(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from exam_to_html import config
        from exam_to_html.updater import _should_throttle

        cfg = config.load()
        # 1 分钟前
        from datetime import datetime, timezone, timedelta
        cfg["last_check_ts"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat()
        config.save(cfg)
        assert _should_throttle(cfg["last_check_ts"], force=False) is True

    def test_force_bypasses(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from exam_to_html import config
        from exam_to_html.updater import _should_throttle

        cfg = config.load()
        from datetime import datetime, timezone, timedelta
        cfg["last_check_ts"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat()
        config.save(cfg)
        assert _should_throttle(cfg["last_check_ts"], force=True) is False

    def test_old_check_not_throttled(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from exam_to_html import config
        from exam_to_html.updater import _should_throttle

        cfg = config.load()
        from datetime import datetime, timezone, timedelta
        cfg["last_check_ts"] = (
            datetime.now(timezone.utc) - timedelta(hours=25)
        ).isoformat()
        config.save(cfg)
        assert _should_throttle(cfg["last_check_ts"], force=False) is False

    def test_corrupt_ts_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from exam_to_html import config
        from exam_to_html.updater import _should_throttle

        cfg = config.load()
        cfg["last_check_ts"] = "not a timestamp"
        config.save(cfg)
        assert _should_throttle(cfg["last_check_ts"], force=False) is False


# ============================================================
# 4. check_update 端到端 (mock fetch_version_json)
# ============================================================
class TestCheckUpdate:
    def _setup(self, tmp_path, monkeypatch):
        """isolate config dir + cwd."""
        monkeypatch.chdir(tmp_path)

    def test_update_available(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        from exam_to_html import config
        from exam_to_html import updater

        cfg = config.load()
        cfg["last_check_ts"] = None
        config.save(cfg)

        fake_response = {
            "latest_version": "99.0.0",  # 远大于当前
            "download_url": "https://example.com/dl",
            "release_notes": "test notes",
        }
        with patch.object(updater, "fetch_version_json", return_value=fake_response):
            result = updater.check_update(current_version="0.1.0", force=True)

        assert result["status"] == "update_available"
        assert result["latest_version"] == "99.0.0"
        assert result["download_url"] == "https://example.com/dl"
        assert result["release_notes"] == "test notes"
        assert result["error"] is None
        # 持久化了 last_check_ts
        cfg2 = config.load()
        assert cfg2["last_check_ts"] is not None

    def test_up_to_date(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        from exam_to_html import config
        from exam_to_html import updater

        fake_response = {"latest_version": "0.1.0", "download_url": "x"}
        with patch.object(updater, "fetch_version_json", return_value=fake_response):
            result = updater.check_update(current_version="0.1.0", force=True)

        assert result["status"] == "up_to_date"
        assert result["latest_version"] == "0.1.0"

    def test_check_failed(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        from exam_to_html import config
        from exam_to_html import updater

        from exam_to_html.updater import UpdateCheckError

        with patch.object(
            updater,
            "fetch_version_json",
            side_effect=UpdateCheckError("network down"),
        ):
            result = updater.check_update(current_version="0.1.0", force=True)

        assert result["status"] == "check_failed"
        assert "network down" in result["error"]
        # 失败也更新 last_check_ts (避免重试风暴)
        cfg = config.load()
        assert cfg["last_check_ts"] is not None

    def test_throttled_returns_early(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        from datetime import datetime, timezone, timedelta
        from exam_to_html import config
        from exam_to_html import updater

        cfg = config.load()
        cfg["last_check_ts"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat()
        config.save(cfg)

        # fetch 不应被调用
        with patch.object(updater, "fetch_version_json") as mock_fetch:
            result = updater.check_update(current_version="0.1.0", force=False)

        mock_fetch.assert_not_called()
        assert result["status"] == "throttled"
        assert result["throttled"] is True
        assert result["latest_version"] is None

    def test_check_failed_invalid_json_version(self, tmp_path, monkeypatch):
        """version.json 缺 latest_version → check_failed."""
        self._setup(tmp_path, monkeypatch)
        from exam_to_html import updater

        with patch.object(
            updater, "fetch_version_json", return_value={"foo": "bar"}
        ):
            result = updater.check_update(current_version="0.1.0", force=True)

        assert result["status"] == "check_failed"


# ============================================================
# 5. FastAPI endpoints 集成 (需要 topic_garden)
# ============================================================
@pytest.mark.skipif(
    os.environ.get("SKIP_FASTAPI_TEST") == "1",
    reason="fastapi deps not available",
)
class TestApiVersion:
    def test_get_version_returns_expected_shape(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from fastapi.testclient import TestClient
        from exam_to_html import config
        from exam_to_html import updater

        cfg = config.load()
        cfg["last_check_ts"] = None
        config.save(cfg)

        fake = {"latest_version": "99.0.0", "download_url": "x", "release_notes": "y"}
        with patch.object(updater, "fetch_version_json", return_value=fake):
            from exam_to_html.backend.server import app
            client = TestClient(app)
            r = client.get("/api/version")

        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "update_available"
        assert data["latest_version"] == "99.0.0"
        assert data["current_version"]  # 不为空
        assert "last_check_ts" in data

    def test_post_check_force(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from fastapi.testclient import TestClient
        from exam_to_html import config
        from exam_to_html import updater

        cfg = config.load()
        # 刚检查过 → 强制模式应跳过节流
        from datetime import datetime, timezone, timedelta
        cfg["last_check_ts"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat()
        config.save(cfg)

        fake = {"latest_version": "0.1.0"}  # 与 current 相同
        with patch.object(updater, "fetch_version_json", return_value=fake) as mock_fetch:
            from exam_to_html.backend.server import app
            client = TestClient(app)
            r = client.post("/api/version/check")

        assert r.status_code == 200
        # 强制模式必须调 fetch (即使刚检查过)
        mock_fetch.assert_called_once()
        data = r.json()
        assert data["status"] == "up_to_date"