"""
exam_to_html.updater — 自动更新检查 (设计文档 §7)

工作流:
  1. 启动时 / 手动按钮 → check_update()
  2. 节流: 距上次检查 < 24h 跳过 (除非 force=True)
  3. HTTP GET version.json → 解析 latest_version + download_url
  4. semver 比较: latest > current → 返回 update_available
  5. 写 config.last_check_ts (无论成功失败, 都更新时间戳防失败重试风暴)

version.json schema (部署到 GH Pages):
    {
        "latest_version": "1.2.0",
        "current_version": "1.0.0",          # 可选 (冗余, 仅展示)
        "download_url": "https://github.com/.../exam-to-html-1.2.0-win.exe",
        "release_notes": "修复了...",
        "released_at": "2026-07-15"          # 可选
    }
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import __version__
from . import config as app_config

log = logging.getLogger(__name__)

# 默认 version.json URL (GH Pages)
# 用户应 push 到自己的 GH user / org 后, 通过环境变量或 config 覆盖
DEFAULT_VERSION_URL = (
    "https://yourname.github.io/exam-to-html/version.json"
)

# 节流间隔 (24h)
THROTTLE_HOURS = 24

# HTTP timeout (秒)
HTTP_TIMEOUT = 5.0

# User-Agent (GH Pages 会按 UA 区分流量)
USER_AGENT = f"exam-to-html/{__version__} (+https://github.com/yourname/exam-to-html)"


# ============================================================
# Semver 比对
# ============================================================
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


def _parse_semver(v: str) -> Optional[tuple]:
    """解析 '1.2.3-beta' → (1, 2, 3). 失败返回 None.

    简化版: 不处理 pre-release 排序 (1.0.0-beta < 1.0.0).
    """
    if not v or not isinstance(v, str):
        return None
    m = _VERSION_RE.match(v.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def is_newer(latest: str, current: str) -> Optional[bool]:
    """latest > current ? True : False. 解析失败返回 None.

    比较规则: 仅 major.minor.patch, pre-release 标签忽略.
    """
    p_latest = _parse_semver(latest)
    p_current = _parse_semver(current)
    if p_latest is None or p_current is None:
        return None
    return p_latest > p_current


# ============================================================
# version.json 拉取
# ============================================================
class UpdateCheckError(Exception):
    """更新检查业务错误 (网络 / 解析失败)."""


def fetch_version_json(url: str) -> Dict[str, Any]:
    """HTTP GET version.json → dict. 失败抛 UpdateCheckError.

    Raises:
        UpdateCheckError: 网络错误 / HTTP 非 2xx / JSON 解析失败
    """
    if not url or not urlparse(url).scheme.startswith("http"):
        raise UpdateCheckError(f"version URL 不合法: {url!r}")

    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                raise UpdateCheckError(f"HTTP {resp.status} from {url}")
            raw = resp.read()
    except Exception as e:
        raise UpdateCheckError(f"拉取失败: {e}") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise UpdateCheckError(f"JSON 解析失败: {e}") from e

    if not isinstance(data, dict):
        raise UpdateCheckError(f"version.json 不是 dict: {type(data).__name__}")

    if "latest_version" not in data:
        raise UpdateCheckError("version.json 缺 latest_version 字段")

    return data


# ============================================================
# 节流 + 主入口
# ============================================================
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        # 'Z' 后缀兼容
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _should_throttle(last_check_ts: Optional[str], force: bool) -> bool:
    """是否需要跳过 (节流中). force=True 时永远不节流."""
    if force:
        return False
    last = _parse_iso(last_check_ts)
    if last is None:
        return False
    threshold = datetime.now(timezone.utc) - timedelta(hours=THROTTLE_HOURS)
    return last > threshold


def check_update(
    url: Optional[str] = None,
    current_version: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """检查更新 — 启动时 / 手动按钮入口.

    Args:
        url: version.json URL (None = 用 DEFAULT_VERSION_URL)
        current_version: 当前版本 (None = 用 __version__)
        force: 跳过 24h 节流 (手动按钮场景)

    Returns:
        {
            'status': 'up_to_date' | 'update_available' | 'check_failed' | 'throttled',
            'latest_version': str | None,
            'current_version': str,
            'download_url': str | None,
            'release_notes': str | None,
            'error': str | None,          # status == 'check_failed' 时有内容
            'last_check_ts': str,         # 本次检查时间
            'throttled': bool,            # 是否因节流跳过 fetch
        }
    """
    url = url or DEFAULT_VERSION_URL
    current_version = current_version or __version__

    cfg = app_config.load()
    last_check_ts = cfg.get("last_check_ts")

    base = {
        "current_version": current_version,
        "last_check_ts": _now_iso(),
        "throttled": False,
    }

    # 1. 节流
    if _should_throttle(last_check_ts, force):
        log.info("[updater] throttled (last check < %dh ago)", THROTTLE_HOURS)
        # 返回上次的状态 (简化: up_to_date, 因为我们没缓存上次的结果)
        return {
            **base,
            "status": "throttled",
            "latest_version": None,
            "download_url": None,
            "release_notes": None,
            "error": None,
            "throttled": True,
        }

    # 2. fetch
    try:
        data = fetch_version_json(url)
    except UpdateCheckError as e:
        log.warning("[updater] fetch failed: %s", e)
        # 失败也更新时间戳 (避免失败重试风暴)
        cfg["last_check_ts"] = base["last_check_ts"]
        app_config.save(cfg)
        return {
            **base,
            "status": "check_failed",
            "latest_version": None,
            "download_url": None,
            "release_notes": None,
            "error": str(e),
        }

    latest = data.get("latest_version", "")
    newer = is_newer(latest, current_version)
    status = (
        "update_available" if newer is True
        else "up_to_date" if newer is False
        else "check_failed"  # 解析失败
    )

    # 3. 更新 last_check_ts
    cfg["last_check_ts"] = base["last_check_ts"]
    app_config.save(cfg)

    return {
        **base,
        "status": status,
        "latest_version": latest,
        "download_url": data.get("download_url"),
        "release_notes": data.get("release_notes"),
        "error": None if status != "check_failed" else f"version 解析失败: latest={latest!r}",
    }


__all__ = [
    "check_update",
    "fetch_version_json",
    "is_newer",
    "DEFAULT_VERSION_URL",
    "THROTTLE_HOURS",
    "UpdateCheckError",
]