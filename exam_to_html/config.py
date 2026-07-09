"""
exam_to_html.config — 用户配置 (config.json) 读写

存放位置:
- frozen 模式: <data_dir>/config.json (%APPDATA%/exam-to-html/config.json on Win)
- dev 模式:    ./config.json (cwd, gitignored)

Schema:
    {
        "mineru_token": str | null,   # precision 模式 token, None = flash 模式
        "output_dir":   str | null,   # HTML 输出目录, None = 桌面
        "last_check_ts": str | null,  # ISO 8601, 更新检查节流
        "mode":         "auto" | "flash" | "precision",  # 默认 auto (有 token 走 precision)
    }

写入用 .tmp + os.replace, 原子写 (避免写一半 app 崩溃 config.json 损坏)。
"""
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from .paths import data_dir, default_output_dir, ensure_data_dirs

log = logging.getLogger(__name__)

# 默认配置: 全部用 None (auto-detect at runtime)
DEFAULTS: Dict[str, Any] = {
    "mineru_token": None,
    "output_dir": None,
    "last_check_ts": None,
    "mode": "auto",
}


def config_path() -> Path:
    """config.json 绝对路径."""
    return data_dir() / "config.json"


def load() -> Dict[str, Any]:
    """读 config.json. 文件不存在或损坏 → 返回 DEFAULTS (不抛异常)."""
    p = config_path()
    if not p.is_file():
        return dict(DEFAULTS)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("[config] config.json 损坏, 使用默认: %s", e)
        return dict(DEFAULTS)
    # 合并默认值 (新增字段时旧 config 也能跑)
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in data.items() if k in DEFAULTS})
    return merged


def save(cfg: Dict[str, Any]) -> None:
    """原子写 config.json. 先写 .tmp, 再 os.replace."""
    ensure_data_dirs()
    p = config_path()
    # 只持久化已知字段
    payload = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}

    # 原子写: 同目录下 .tmp → os.replace
    fd, tmp_path = tempfile.mkstemp(
        prefix="config.", suffix=".tmp", dir=str(p.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, p)
    except Exception:
        # 失败清理 .tmp
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def resolve_mode(cfg: Dict[str, Any]) -> str:
    """根据 config 解析实际生效的 mode.

    - cfg['mode'] == 'flash' / 'precision' → 直接返回
    - cfg['mode'] == 'auto' (默认) → 有 token 走 precision, 否则 flash
    """
    explicit = cfg.get("mode", "auto")
    if explicit in ("flash", "precision"):
        return explicit
    # auto
    return "precision" if cfg.get("mineru_token") else "flash"


def resolve_output_dir(cfg: Dict[str, Any]) -> Path:
    """根据 config 解析实际输出目录. 未配置 → 桌面."""
    raw = cfg.get("output_dir")
    if raw and str(raw).strip():
        p = Path(raw).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p
    # 默认桌面
    d = default_output_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d