"""
exam_to_html.gui.server — uvicorn 后台启动 + 健康探针

设计要点:
- uvicorn 跑后台 daemon thread (主线程给 webview.start() 阻塞)
- 端口自动分配 (避免教师电脑端口冲突)
- wait_ready() 探针确保 backend ready 后再开窗口 (避免前端 fetch 404)
"""
from __future__ import annotations

import logging
import socket
import threading
import time

log = logging.getLogger(__name__)


def find_free_port(host: str = "127.0.0.1") -> int:
    """让 OS 分配一个空闲端口."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        port = s.getsockname()[1]
    return port


class UvicornRunner:
    """后台跑 uvicorn 实例, 主线程可继续 webview.start()."""

    def __init__(self, host: str = "127.0.0.1", port: int | None = None) -> None:
        import uvicorn

        self.host = host
        self.port = port or find_free_port(host)
        self.config = uvicorn.Config(
            "exam_to_html.backend.server:app",
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
            # 关键: lifespan="off" 避免 uvicorn 默认的 startup/shutdown 事件
            # 干扰我们的 thread-based 编排
            lifespan="off",
        )
        self.server = uvicorn.Server(self.config)
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        """非阻塞启动 uvicorn (后台 daemon thread)."""
        if self._thread is not None:
            raise RuntimeError("UvicornRunner already started")
        self._thread = threading.Thread(
            target=self.server.run, name="uvicorn-thread", daemon=True
        )
        self._thread.start()
        log.info("[uvicorn] thread started, target=%s", self.url)

    def wait_ready(self, timeout: float = 10.0) -> bool:
        """轮询 server.started 直到 True 或超时."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.server.started:
                return True
            time.sleep(0.05)
        return False

    def stop(self, timeout: float = 3.0) -> None:
        """优雅停 uvicorn (should_exit → join)."""
        self.server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        log.info("[uvicorn] stopped")


__all__ = ["UvicornRunner", "find_free_port"]