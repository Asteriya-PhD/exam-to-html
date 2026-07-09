"""
exam_to_html.logging_setup — 集中日志配置 (设计文档 §5.3)

启动时调 setup_logging() 一次, 输出双路:
  - logs/app.log (RotatingFileHandler, 5MB x 3 备份)
  - stderr (StreamHandler)

设计原则:
- frozen 模式下 logs_dir 在 %APPDATA%/exam-to-html/logs/ (可写)
- dev 模式下 logs_dir 在 cwd/logs/ (gitignored)
- 重复 setup 时清旧 handler (避免双份日志)
- 配置后任何 logging.getLogger(__name__) 自动双路输出
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 5MB x 3 备份 (设计文档没指定, 拍脑袋的合理值)
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(logs_dir: Path, level: int = logging.INFO) -> logging.Logger:
    """配置 root logger, 写入 logs/app.log + stderr.

    Args:
        logs_dir: 日志目录 (会自动 mkdir)
        level: root logger level, 默认 INFO

    Returns:
        root logger (供调用方立即使用)
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "app.log"

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    # 清掉已有 handler (重复 setup 不重复输出)
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    root.info("[logging_setup] initialized, file=%s level=%s", log_file, level)
    return root


__all__ = ["setup_logging", "LOG_MAX_BYTES", "LOG_BACKUP_COUNT"]