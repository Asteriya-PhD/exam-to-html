"""
exam_to_html.gui.window — PyWebView 主窗口

PyWebView 是 native 浏览器壳 (macOS WebKit / Win WebView2 / Linux GTK-WebKit)。
单窗口 560x680 (拖 PDF + 状态足够)。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .server import UvicornRunner

log = logging.getLogger(__name__)


def create_window(runner: "UvicornRunner", title: str = "Exam to HTML"):
    """创建 PyWebView 窗口, 指向 uvicorn URL."""
    import webview

    log.info("[window] creating webview, url=%s", runner.url)
    return webview.create_window(
        title=title,
        url=runner.url,
        width=1080,
        height=720,
        resizable=True,
        min_size=(960, 640),
        text_select=True,
        # confirm_close=False 让 X 直接关 (避免教师每次点确认)
        confirm_close=False,
    )


__all__ = ["create_window"]