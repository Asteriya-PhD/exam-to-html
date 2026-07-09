"""
exam_to_html.paths — 唯一生产 DB / inbox / archive / config 路径定义

设计原则 (与 topic_garden.paths 同构, 便于复用):
- 全仓唯一真源: 修改这里会同时影响所有模块
- frozen 模式 → OS standard data dir (Windows: %APPDATA%, macOS: ~/Library/Application Support)
- dev 模式 → cwd (开发期间隔离, 不污染 home)

⚠️  v0.10.1 topic-garden PR-E 教训:
    APP_ID 字符串必须全局严格一致, 不允许历史带点 / 不带点的分裂。
    LEGACY_APP_IDS 启动时检测 + warn (不自动迁移, 避免误删)。
"""
import logging
import os
import sys
from pathlib import Path

# 单一真源: 与未来 Windows 安装包 / macOS bundle identifier 严格一致
APP_ID = "com.exam-to-html"

# 历史分裂 ID (本项目尚未发布, 留空数组占位, 未来 v1.0 → v1.1 改名时填)
LEGACY_APP_IDS: list = [
    # e.g. "com.exam-to-html.app",  # v1.0 改名时填这里
]


def _frozen_data_dir() -> str:
    """PyInstaller frozen 模式 → 跨平台 OS standard data dir."""
    if sys.platform == "darwin":
        return os.path.expanduser(f"~/Library/Application Support/{APP_ID}")
    if sys.platform == "win32":
        # APPDATA 默认值: C:\\Users\\<user>\\AppData\\Roaming
        return os.environ.get("APPDATA") or str(Path.home())
    # Linux 等: 走 XDG
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(xdg, APP_ID)


def resolve_data_dir() -> Path:
    """frozen 模式走 OS standard data dir, dev 模式走 cwd.

    Returns:
        Path: 数据根目录 (含 db.sqlite3 / config.json / inbox / archive / logs)
    """
    if getattr(sys, "frozen", False):
        return Path(_frozen_data_dir())
    return Path(".")


def data_dir() -> Path:
    """数据根目录 (与 resolve_data_dir 同义, 短名)."""
    return resolve_data_dir()


def db_path() -> Path:
    """SQLite DB 路径 (与 topic_garden 共用 schema)."""
    return data_dir() / "db.sqlite3"


def inbox_dir() -> Path:
    """待处理 PDF 暂存目录."""
    d = data_dir() / "inbox"
    d.mkdir(parents=True, exist_ok=True)
    return d


def archive_dir() -> Path:
    """处理成功 PDF 归档目录."""
    d = data_dir() / "archive" / "inbox"
    d.mkdir(parents=True, exist_ok=True)
    return d


def logs_dir() -> Path:
    """应用日志目录."""
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_output_dir() -> Path:
    """默认输出目录: 桌面 (教师最熟悉的落点)."""
    if sys.platform == "darwin":
        return Path.home() / "Desktop"
    if sys.platform == "win32":
        # Windows 桌面: USERPROFILE/Desktop
        userprofile = os.environ.get("USERPROFILE") or str(Path.home())
        return Path(userprofile) / "Desktop"
    return Path.home()


def gui_static_dir() -> Path:
    """GUI 静态文件目录 (HTML/CSS/JS).

    dev 模式: <pkg_root>/gui/static
    frozen 模式: PyInstaller 解压到 sys._MEIPASS/exam_to_html/gui/static
                 (由 pyinstaller.spec 的 datas 配置)
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", "")) / "exam_to_html" / "gui" / "static"
    return Path(__file__).parent / "gui" / "static"


def courseware_images_dir() -> Path:
    """topic_garden courseware/images 目录 — 试卷 PDF 解析后图片存这里.

    模板里 <img src="images/X.jpg"> 是相对路径, 必须在输出 HTML 同级
    才能解析。pipeline 会把这个目录链接到 output_dir/images。
    """
    # 1. frozen 模式: 依赖 pyinstaller.spec 把 courseware/images 打进包
    if getattr(sys, "frozen", False):
        meipass = Path(getattr(sys, "_MEIPASS", ""))
        candidate = meipass / "courseware" / "images"
        if candidate.is_dir():
            return candidate
    # 2. dev 模式: topic_garden 可能以 editable install 装在我们的 .venv 里
    import importlib.util
    spec = importlib.util.find_spec("topic_garden")
    if spec and spec.submodule_search_locations:
        src_dir = Path(spec.submodule_search_locations[0])
        for ancestor in [src_dir.parent.parent.parent, src_dir.parent.parent, src_dir.parent]:
            candidate = ancestor / "courseware" / "images"
            if candidate.is_dir():
                return candidate
    return Path("courseware") / "images"


def ensure_data_dirs() -> None:
    """启动时确保所有数据目录存在."""
    for d in (inbox_dir(), archive_dir(), logs_dir()):
        d.mkdir(parents=True, exist_ok=True)


def warn_legacy_data_dirs() -> None:
    """启动时检测 legacy 分裂数据目录, 如果存在就 warn.

    不自动迁移 (避免误删), 只提示用户手动处理。
    """
    log = logging.getLogger(__name__)
    if not getattr(sys, "frozen", False):
        return  # dev 模式 cwd 隔离, 不检查 home

    primary = Path(_frozen_data_dir())
    for legacy_id in LEGACY_APP_IDS:
        if sys.platform == "darwin":
            legacy = Path.home() / "Library" / "Application Support" / legacy_id
        elif sys.platform == "win32":
            legacy = Path(os.environ.get("APPDATA", str(Path.home()))) / legacy_id
        else:
            xdg = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
            legacy = Path(xdg) / legacy_id

        if legacy.exists() and legacy != primary:
            log.warning(
                "[exam-to-html] LEGACY data dir detected: %s\n"
                "  Production data is at: %s\n"
                "  To migrate (one-way, manual):\n"
                "    cp -R %s/* %s/   # if you want to keep old data\n"
                "    rm -rf %s        # after verifying",
                legacy, primary, legacy, primary, legacy,
            )