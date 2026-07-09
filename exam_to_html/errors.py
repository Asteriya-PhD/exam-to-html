"""
exam_to_html.errors — 错误类型层 (设计文档 §5)

分两层:
- PipelineError: 业务错误基类 (向 GUI 报告用, 不是 bug)
- UserVisibleError: 教师能看到的错误 (8 类, 每类有 error_code + 中文消息)
- SilentError: 静默错误 (log 即可, 不打扰教师)

PipelineError 也用作内部 catch-all (UNKNOWN 类)。

error_code 用于 FastAPI JSON 序列化, 前端按 code 渲染对应 UI:
    NON_PDF, ENCRYPTED, TOO_LARGE, MINERU_TIMEOUT, MINERU_AUTH,
    OUTPUT_DENIED, NO_DISK, NO_QUESTIONS, DB_LOCKED, UNKNOWN
"""
from __future__ import annotations

from typing import Optional


# ============================================================
# 错误 code 常量 (前端按 code 渲染 UI)
# ============================================================
NON_PDF = "NON_PDF"
ENCRYPTED = "ENCRYPTED"
TOO_LARGE = "TOO_LARGE"
MINERU_TIMEOUT = "MINERU_TIMEOUT"
MINERU_AUTH = "MINERU_AUTH"
OUTPUT_DENIED = "OUTPUT_DENIED"
NO_DISK = "NO_DISK"
NO_QUESTIONS = "NO_QUESTIONS"
DB_LOCKED = "DB_LOCKED"
UNKNOWN = "UNKNOWN"


# ============================================================
# 错误消息 (设计文档 §5.1 文案)
# ============================================================
USER_MESSAGES: dict = {
    NON_PDF: "请拖入 PDF 文件（.pdf 后缀）",
    ENCRYPTED: "PDF 已加密，无法解析。请用 PDF 阅读器解密后重试",
    TOO_LARGE: "文件过大（>100MB），请压缩或拆分",
    MINERU_TIMEOUT: "在线解析服务暂时不可用，请稍后再试",
    MINERU_AUTH: "API token 无效，请在高级设置检查",
    OUTPUT_DENIED: "无法写入输出目录，请选择其他位置",
    NO_DISK: "磁盘空间不足，需要至少 200MB 剩余",
    NO_QUESTIONS: "PDF 解析成功但未识别到题目",
    DB_LOCKED: "数据库正被占用，请稍后再试",
    UNKNOWN: "发生未知错误，请重试或反馈",
}

# 恢复动作 (前端按 code 决定 UI: 重试按钮 / 引导高级设置 / 改输出位置)
RECOVERY_HINTS: dict = {
    NON_PDF: "retry",            # 重新拖
    ENCRYPTED: "retry",          # 解密后重试
    TOO_LARGE: "retry",          # 压缩/拆分后重试
    MINERU_TIMEOUT: "retry_button",  # 显示"重试"按钮
    MINERU_AUTH: "open_settings",    # 引导到高级设置
    OUTPUT_DENIED: "change_output",  # 重新选输出位置
    NO_DISK: "free_space",
    NO_QUESTIONS: "retry",
    DB_LOCKED: "retry_button",
    UNKNOWN: "retry_button",
}


# ============================================================
# 错误类
# ============================================================
class PipelineError(Exception):
    """Pipeline 业务错误基类 (向 GUI 报告用, 不是 bug).

    默认 code='UNKNOWN'. 子类应覆盖.
    """

    code: str = UNKNOWN

    def __init__(self, message: str = "", *, code: Optional[str] = None, cause: Optional[Exception] = None):
        super().__init__(message)
        self.message = message or USER_MESSAGES.get(self.code, "未知错误")
        if code is not None:
            self.code = code
        self.__cause__ = cause

    def to_dict(self) -> dict:
        """序列化为 API JSON 给前端."""
        return {
            "code": self.code,
            "message": self.message,
            "recovery": RECOVERY_HINTS.get(self.code, "retry_button"),
        }


class UserVisibleError(PipelineError):
    """教师能看到的错误 (设计文档 §5.1).

    子类对应 8 项用户可见错误, 每类有明确 error_code + 中性消息.
    """


class SilentError(PipelineError):
    """静默错误 (设计文档 §5.2): log 即可, 不抛给前端.

    用于单题解析失败 / KaTeX 失败 / 图片加载失败.
    目前由 topic_garden 内部处理, exam-to-html 这层暂不直接抛.
    """
    code = "SILENT"


# ============================================================
# 8 项用户可见错误 (具体子类)
# ============================================================
class NonPdfError(UserVisibleError):
    code = NON_PDF


class EncryptedPdfError(UserVisibleError):
    code = ENCRYPTED


class FileTooLargeError(UserVisibleError):
    code = TOO_LARGE


class MineruTimeoutError(UserVisibleError):
    code = MINERU_TIMEOUT


class MineruAuthError(UserVisibleError):
    code = MINERU_AUTH


class OutputPermissionError(UserVisibleError):
    code = OUTPUT_DENIED


class NoDiskSpaceError(UserVisibleError):
    code = NO_DISK


class NoQuestionsError(UserVisibleError):
    """PDF 解析成功但 0 题入库 — 可能是扫描版 / 图全是图."""
    code = NO_QUESTIONS


class DbLockedError(UserVisibleError):
    """数据库被锁 — 重试 3 次后仍失败."""
    code = DB_LOCKED


__all__ = [
    "PipelineError",
    "UserVisibleError",
    "SilentError",
    "NonPdfError",
    "EncryptedPdfError",
    "FileTooLargeError",
    "MineruTimeoutError",
    "MineruAuthError",
    "OutputPermissionError",
    "NoDiskSpaceError",
    "NoQuestionsError",
    "DbLockedError",
    # code 常量
    "NON_PDF", "ENCRYPTED", "TOO_LARGE", "MINERU_TIMEOUT", "MINERU_AUTH",
    "OUTPUT_DENIED", "NO_DISK", "NO_QUESTIONS", "DB_LOCKED", "UNKNOWN",
    "USER_MESSAGES", "RECOVERY_HINTS",
]