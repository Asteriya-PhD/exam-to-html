"""
exam-to-html 错误处理测试 (M3-4)

覆盖:
- 8 项用户可见错误 (NonPdf / Encrypted / TooLarge / Mineru_xxx / OutputDenied / NoDisk / NoQuestions)
- DB 锁重试
- 日志配置 (setup_logging)
- 中途关闭恢复 (scan_incomplete_uploads)
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest


# ============================================================
# 错误类 + USER_MESSAGES 一致性
# ============================================================
class TestErrorCodes:
    def test_all_codes_have_messages(self):
        from exam_to_html.errors import USER_MESSAGES, RECOVERY_HINTS

        for code in ["NON_PDF", "ENCRYPTED", "TOO_LARGE", "MINERU_TIMEOUT",
                     "MINERU_AUTH", "OUTPUT_DENIED", "NO_DISK", "NO_QUESTIONS",
                     "DB_LOCKED", "UNKNOWN"]:
            assert code in USER_MESSAGES, f"USER_MESSAGES 缺 {code}"
            assert code in RECOVERY_HINTS, f"RECOVERY_HINTS 缺 {code}"

    def test_to_dict_shape(self):
        from exam_to_html.errors import (
            NonPdfError, EncryptedPdfError, FileTooLargeError,
            MineruAuthError, NoDiskSpaceError, DbLockedError,
        )

        for cls in (NonPdfError, EncryptedPdfError, FileTooLargeError,
                    MineruAuthError, NoDiskSpaceError, DbLockedError):
            d = cls("custom message").to_dict()
            assert set(d.keys()) == {"code", "message", "recovery"}
            assert d["message"] == "custom message"

    def test_default_message_fallback(self):
        """无 message 构造时走 USER_MESSAGES 默认文案."""
        from exam_to_html.errors import NonPdfError, USER_MESSAGES

        err = NonPdfError()
        assert err.message == USER_MESSAGES["NON_PDF"]

    def test_pipeline_error_base_unknown_code(self):
        from exam_to_html.errors import PipelineError, UNKNOWN

        err = PipelineError("oops")
        assert err.code == UNKNOWN
        assert err.to_dict()["recovery"] == "retry_button"


# ============================================================
# 预检 (M3-1)
# ============================================================
class TestPreflight:
    def test_non_pdf_raises(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from exam_to_html.backend.pipeline import _preflight
        from exam_to_html.errors import NonPdfError

        txt = tmp_path / "doc.txt"
        txt.write_text("hi")

        with pytest.raises(NonPdfError) as exc:
            _preflight(txt, tmp_path)
        assert exc.value.code == "NON_PDF"

    def test_missing_file_raises(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from exam_to_html.backend.pipeline import _preflight
        from exam_to_html.errors import PipelineError

        with pytest.raises(PipelineError, match="不存在"):
            _preflight(tmp_path / "ghost.pdf", tmp_path)

    def test_too_large_raises(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from exam_to_html.backend.pipeline import _preflight, MAX_PDF_SIZE_BYTES
        from exam_to_html.errors import FileTooLargeError

        big = tmp_path / "huge.pdf"
        big.write_bytes(b"%PDF-1.4\n" + b"x" * (MAX_PDF_SIZE_BYTES + 100) + b"\n%%EOF\n")

        with pytest.raises(FileTooLargeError) as exc:
            _preflight(big, tmp_path)
        assert exc.value.code == "TOO_LARGE"

    def test_output_denied_raises(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from exam_to_html.backend.pipeline import _preflight
        from exam_to_html.errors import OutputPermissionError

        small_pdf = tmp_path / "ok.pdf"
        small_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")

        # 不可写目录
        bad_dir = tmp_path / "noperm"
        bad_dir.mkdir()
        os.chmod(bad_dir, 0o000)

        try:
            with pytest.raises(OutputPermissionError) as exc:
                _preflight(small_pdf, bad_dir)
            assert exc.value.code == "OUTPUT_DENIED"
        finally:
            os.chmod(bad_dir, 0o755)


# ============================================================
# DB 锁重试
# ============================================================
class TestDbRetry:
    def test_retries_on_lock_then_succeeds(self):
        """第一次 lock, 第二次成功."""
        from exam_to_html.backend.pipeline import _with_db_retry
        import peewee

        call_count = [0]

        def flaky():
            call_count[0] += 1
            if call_count[0] < 2:
                raise peewee.OperationalError("database is locked")
            return "ok"

        result = _with_db_retry(flaky)
        assert result == "ok"
        assert call_count[0] == 2

    def test_raises_DbLocked_after_max_retries(self):
        from exam_to_html.backend.pipeline import _with_db_retry, DB_LOCK_RETRY_TIMES
        from exam_to_html.errors import DbLockedError
        import peewee

        def always_locked():
            raise peewee.OperationalError("database is locked")

        with pytest.raises(DbLockedError) as exc:
            _with_db_retry(always_locked)
        assert exc.value.code == "DB_LOCKED"
        # 重试 N 次都失败 → cause 是最后一次的 OperationalError
        assert "locked" in str(exc.value.__cause__)
        # 错误消息体现重试次数
        assert str(DB_LOCK_RETRY_TIMES) in exc.value.message

    def test_other_operational_error_not_retried(self):
        """非 'locked' 的 OperationalError 立即抛 (不重试)."""
        from exam_to_html.backend.pipeline import _with_db_retry
        import peewee

        def other_error():
            raise peewee.OperationalError("no such table: foo")

        call_count = [0]

        def counting_error():
            call_count[0] += 1
            other_error()

        with pytest.raises(peewee.OperationalError, match="no such table"):
            _with_db_retry(counting_error)
        assert call_count[0] == 1  # 没重试


# ============================================================
# 日志配置
# ============================================================
class TestLogging:
    def test_setup_creates_log_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from exam_to_html.logging_setup import setup_logging
        import logging

        log = setup_logging(tmp_path / "logs")
        assert (tmp_path / "logs" / "app.log").is_file()

        # 任意子 logger 写日志都会双路 (root logger 配了 handler)
        logging.getLogger("exam_to_html.test").info("test message 12345")
        for h in log.handlers:
            h.flush()

        content = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
        assert "test message 12345" in content
        assert "[INFO]" in content
        assert "exam_to_html.test" in content  # 子 logger 名字

    def test_setup_idempotent_clears_old_handlers(self, tmp_path):
        from exam_to_html.logging_setup import setup_logging
        import logging

        setup_logging(tmp_path / "logs1")
        first_count = len(logging.getLogger().handlers)

        setup_logging(tmp_path / "logs2")
        second_count = len(logging.getLogger().handlers)

        # 不应该累积 handler
        assert first_count == second_count


# ============================================================
# M3-2: scan_incomplete_uploads
# ============================================================
class TestIncompleteUploads:
    def test_empty(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from exam_to_html.backend.pipeline import scan_incomplete_uploads

        assert scan_incomplete_uploads(within_hours=24) == []

    def test_finds_recent_pdfs(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from exam_to_html.backend.pipeline import scan_incomplete_uploads
        from exam_to_html.paths import archive_dir, inbox_dir

        inbox_pdf = inbox_dir() / "in1.pdf"
        inbox_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
        archive_pdf = archive_dir() / "ar1.pdf"
        archive_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")

        results = scan_incomplete_uploads(within_hours=24)
        assert len(results) == 2
        locations = {r["location"] for r in results}
        assert locations == {"inbox", "archive"}

    def test_filters_old_pdfs(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from exam_to_html.backend.pipeline import scan_incomplete_uploads
        from exam_to_html.paths import inbox_dir

        old_pdf = inbox_dir() / "old.pdf"
        old_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
        old_time = time.time() - (25 * 3600)
        os.utime(old_pdf, (old_time, old_time))

        assert scan_incomplete_uploads(within_hours=24) == []

    def test_clear(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from exam_to_html.backend.pipeline import clear_incomplete_uploads
        from exam_to_html.paths import inbox_dir

        p1 = inbox_dir() / "p1.pdf"
        p1.write_bytes(b"%PDF-1.4\n%%EOF\n")
        p2 = inbox_dir() / "p2.pdf"
        p2.write_bytes(b"%PDF-1.4\n%%EOF\n")

        deleted = clear_incomplete_uploads([str(p1), str(p2)])
        assert deleted == 2
        assert not p1.exists()
        assert not p2.exists()