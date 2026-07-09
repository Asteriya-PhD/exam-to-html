"""
exam_to_html.app — 应用编排 (uvicorn + webview 生命周期)

启动顺序:
  1. ensure_data_dirs() (保证 %APPDATA%/exam-to-html/{inbox,archive,logs} 存在)
  2. warn_legacy_data_dirs() (检测分裂路径, 仅 warn)
  3. UvicornRunner.start() (后台 thread)
  4. UvicornRunner.wait_ready() (探针)
  5. create_window() + webview.start() (阻塞主线程, 直至窗口关闭)
  6. UvicornRunner.stop() (优雅退出)
"""
from __future__ import annotations

import logging
import sys

from . import __version__
from .backend.server import app as _fastapi_app  # noqa: F401  # eager import 让 PyInstaller 静态分析能 trace
from .gui.server import UvicornRunner
from .gui.window import create_window
from .logging_setup import setup_logging
from .paths import ensure_data_dirs, logs_dir, warn_legacy_data_dirs


def _setup_logging() -> logging.Logger:
    """集中日志配置 (设计文档 §5.3): logs/app.log + stderr, 5MB 轮转."""
    return setup_logging(logs_dir())


def main() -> int:
    log = _setup_logging()
    log.info("starting exam-to-html v%s", __version__)

    ensure_data_dirs()
    warn_legacy_data_dirs()

    # 1. uvicorn 后台启动
    runner = UvicornRunner()
    runner.start()
    if not runner.wait_ready(timeout=10.0):
        log.error("uvicorn 启动超时 (10s)")
        runner.stop()
        return 1
    log.info("backend ready: %s", runner.url)

    # 2. webview 主窗口 (阻塞至关闭)
    try:
        window = create_window(runner)
        import webview
        webview.start()
    except KeyboardInterrupt:
        log.info("interrupted by user")
    except Exception as e:
        log.exception("[app] webview 异常退出: %s", e)
        runner.stop()
        return 2

    # 3. 清理 uvicorn
    runner.stop()
    log.info("shutdown clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main"]