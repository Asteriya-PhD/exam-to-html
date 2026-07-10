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
import uuid
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


# ============================================================
# NoQuestionsError 诊断消息 (M5-1 fix)
# — 区分 "parser 没识别到题号" vs "全被 dedup 命中",避免给教师错误线索
# ============================================================
class TestNoQuestionsDiagnostic:
    """convert_pdf 在 0 题入库时应给出可定位的错误消息。

    旧实现统一报 "PDF 解析成功但无题目入库" — 在 parser 静默失败时这是误导
    (教师会以为 PDF 没问题,实际是 MinerU 没识别到题号)。

    新实现读 process_inbox 的 summary 区分两种情形:
      - drafts=0          → parser 没识别到题号 (扫描版/题号格式非标准/MinerU 失败)
      - inserted=0, drafts>0 → 真有题但全被 dedup 命中 (库中已有同题)
    """

    @staticmethod
    def _make_pdf(tmp_path: Path, name: str = "diag.pdf") -> Path:
        p = tmp_path / name
        p.write_bytes(b"%PDF-1.4\n%%EOF\n")
        return p

    def test_drafts_zero_message_says_parser_failed(self, tmp_path, monkeypatch):
        """drafts=0 → 错误消息应明确指出 parser 没识别到题号."""
        pytest.importorskip("topic_garden")
        from exam_to_html.backend.pipeline import convert_pdf
        from exam_to_html.errors import NoQuestionsError

        def fake_process_inbox(**kwargs):
            return {
                "summary": {
                    "drafts": 0, "inserted": 0, "skipped": 0, "near_dup": 0,
                    "files_total": 1, "files_completed": 0,
                    "files_partial": 0, "files_failed": 1,
                    "failed_questions": 0,
                },
                "files": [],
            }

        monkeypatch.setattr(
            "topic_garden.ingest_inbox.process_inbox",
            fake_process_inbox,
        )

        pdf = self._make_pdf(tmp_path)
        out = tmp_path / "out"
        out.mkdir()

        with pytest.raises(NoQuestionsError) as exc:
            convert_pdf(pdf_path=pdf, output_dir=out, mode="flash")
        msg = exc.value.message
        # 关键诊断关键词:parser drafts=0 + 暗示扫描版/题号格式
        assert "drafts=0" in msg, f"消息缺 drafts=0 诊断: {msg}"
        assert "扫描版" in msg or "题号格式" in msg, f"消息缺成因提示: {msg}"
        # code 仍为 NO_QUESTIONS (前端按 code 渲染)
        assert exc.value.code == "NO_QUESTIONS"

    def test_dedup_all_skipped_message_says_already_indexed(self, tmp_path, monkeypatch):
        """drafts>0 但 inserted=0 → 错误消息应明确说已入库."""
        pytest.importorskip("topic_garden")
        from exam_to_html.backend.pipeline import convert_pdf
        from exam_to_html.errors import NoQuestionsError

        def fake_process_inbox(**kwargs):
            return {
                "summary": {
                    "drafts": 5, "inserted": 0, "skipped": 5, "near_dup": 0,
                    "files_total": 1, "files_completed": 1,
                    "files_partial": 0, "files_failed": 0,
                    "failed_questions": 0,
                },
                "files": [],
            }

        monkeypatch.setattr(
            "topic_garden.ingest_inbox.process_inbox",
            fake_process_inbox,
        )

        pdf = self._make_pdf(tmp_path, name="dup.pdf")
        out = tmp_path / "out"
        out.mkdir()

        with pytest.raises(NoQuestionsError) as exc:
            convert_pdf(pdf_path=pdf, output_dir=out, mode="flash")
        msg = exc.value.message
        assert "dedup" in msg or "已入库" in msg, f"消息缺 dedup 提示: {msg}"
        assert "drafts=5" in msg, f"消息缺 drafts 计数: {msg}"
        assert exc.value.code == "NO_QUESTIONS"


# ============================================================
# 宽松题号兜底 (M5-2 — _qnum_fallback.py)
# — PDF2PPT 的 QUESTION_PATTERN 漏掉 (1) ① 第1题 T1. 等格式;
#   本模块在 PDF2PPT 返 0 题时从 PDF 原文按宽松正则重抽。
# ============================================================
class TestQnumFallback:
    """_qnum_fallback 单元测试 — 覆盖 5 类题号格式 + 假阳防护。"""

    def test_matches_parenthesized_full_width(self):
        from exam_to_html.backend._qnum_fallback import _match_qnum
        assert _match_qnum("（1） 下列说法正确的是") == 1
        assert _match_qnum("（12） 第十二题") == 12

    def test_matches_parenthesized_half_width(self):
        from exam_to_html.backend._qnum_fallback import _match_qnum
        assert _match_qnum("(2) 关于X的描述") == 2
        assert _match_qnum("  (3)  第三个") == 3

    def test_matches_circled_digits(self):
        from exam_to_html.backend._qnum_fallback import _match_qnum
        assert _match_qnum("① 甲烷分子式") == 1
        assert _match_qnum("② 乙烷") == 2
        assert _match_qnum("⑩ 第十题") == 10
        # 圈码范围 1-20; 超过当无效
        assert _match_qnum("⓿ 假阳") is None  # '⓿' 是 0, 不在 ①-⑳

    def test_matches_chinese_prefix(self):
        from exam_to_html.backend._qnum_fallback import _match_qnum
        assert _match_qnum("第1题 单选题") == 1
        assert _match_qnum("第 12 题 解答") == 12

    def test_matches_t_prefix(self):
        from exam_to_html.backend._qnum_fallback import _match_qnum
        assert _match_qnum("T1. 真题") == 1
        assert _match_qnum("Q2. 第二题") == 2
        assert _match_qnum("题3. 第三题") == 3

    def test_matches_plain_digit_period(self):
        """原 PDF2PPT 兼容格式."""
        from exam_to_html.backend._qnum_fallback import _match_qnum
        assert _match_qnum("1. 普通题号") == 1
        assert _match_qnum("12． 全宽句点") == 12
        assert _match_qnum("5、 顿号") == 5

    def test_rejects_decimal_data_values(self):
        """实验数据假阳 — `(?!\\d)` 守住."""
        from exam_to_html.backend._qnum_fallback import _match_qnum
        assert _match_qnum("0.05 g 试剂") is None
        assert _match_qnum("1.14 mol/L 溶液") is None
        assert _match_qnum("0.848 g·cm") is None
        assert _match_qnum("m=0.1kg 的物体") is None

    def test_rejects_qnum_above_50(self):
        """num > 50 多为误识 (实验数据 / 年份), 不当题号."""
        from exam_to_html.backend._qnum_fallback import _match_qnum
        assert _match_qnum("2026 年 1 月") is None  # 2026 > 50
        assert _match_qnum("99. 不可能试卷") is None

    def test_accepts_qnum_followed_by_year(self):
        """M5-5: 真实题号 "6.2024 年12 月" 不能被 `(?!\d)` 误杀 — 4+ 位数字是年份."""
        from exam_to_html.backend._qnum_fallback import _match_top_qnum
        assert _match_top_qnum("6.2024 年12 月") == 6
        assert _match_top_qnum("6.2025") == 6
        assert _match_top_qnum("11.2024") == 11

    def test_rejects_decimal_with_unit(self):
        """M5-5: 小数+单位 "1.5m" / "9.8m/s" 不应被误识为题号 (lookahead `(?!\d{1,3}\s?[a-zA-Z])` 守)."""
        from exam_to_html.backend._qnum_fallback import _match_top_qnum
        assert _match_top_qnum("1.5m") is None
        assert _match_top_qnum("9.8m/s") is None
        assert _match_top_qnum("0.05 g") is None
        assert _match_top_qnum("3.14 rad") is None

    def test_extract_from_mixed_text_skips_instruction_lines(self):
        from exam_to_html.backend._qnum_fallback import extract_qnums_from_text
        text = """注意事项: 请先读题
满分 150 分
考试时间 120 分钟
（1） 第一题题干
（2） 第二题题干
① 圈码第一
第1题 中文前缀
T1. T前缀
1. 普通
0.05 g 假阳"""
        qnums = extract_qnums_from_text(text)
        nums = [n for (_, n) in qnums]
        # 新契约 (M5-3): extract_qnums_from_text 只返回顶级题号,
        # 子问号 （1）/（2） 不参与顶级题号流, 故只挑出 4 个顶级:
        #   ① → 1, 第1题 → 1, T1. → 1, 1. → 1 (单调递增全部合法)
        # 注意事项 / 满分 / 考试时间 跳过, 0.05 g 假阳跳过
        assert nums == [1, 1, 1, 1], f"unexpected: {qnums}"

    def test_extract_returns_empty_for_empty_text(self):
        from exam_to_html.backend._qnum_fallback import extract_qnums_from_text
        assert extract_qnums_from_text("") == []
        assert extract_qnums_from_text("没有任何题号的纯文本内容") == []

    def test_extract_drafts_returns_empty_when_no_qnums(self):
        """PDF 原文里没题号 → 兜底返 [], pipeline 走原 NoQuestionsError 路径."""
        from exam_to_html.backend._qnum_fallback import extract_drafts_with_lenient_qnum
        import unittest.mock as mock
        # PyMuPDF 不在 exam-to-html venv, 直接 mock _iter_pages_text
        with mock.patch(
            "exam_to_html.backend._qnum_fallback._iter_pages_text",
            return_value=[(0, "注意事项\n没有任何题号的纯文本内容")],
        ):
            drafts = extract_drafts_with_lenient_qnum("ignored.pdf")
            assert drafts == []

    def test_extract_drafts_chunks_questions_by_qnum(self):
        """PDF 原文按顶级 qnum 切分, 每段 → 一条 QuestionDraft.

        新契约 (M5-3): 顶级题号开新题段, 子问号 (1)/(2)/(3) 附在当前顶级题题干,
        不独立成题. 这修复了 page 1+ 上 11 题下面的 (1)/(2)/(3) 被切成 3 道
        独立题的 bug.
        """
        from exam_to_html.backend._qnum_fallback import extract_drafts_with_lenient_qnum
        import unittest.mock as mock
        # 模拟 PyMuPDF 抽出: 2 道顶级题, 每道有 3 个子问号
        with mock.patch(
            "exam_to_html.backend._qnum_fallback._iter_pages_text",
            return_value=[(0,
                "注意事项\n"
                "1. 第一题题干\n"
                "（1） 子问一\n"
                "（2） 子问二\n"
                "（3） 子问三\n"
                "2. 第二题题干\n"
                "（1） 子问一\n"
                "（2） 子问二\n"
            )],
        ):
            drafts = extract_drafts_with_lenient_qnum("mock.pdf")
        # 应该是 2 道顶级题, 子问号附在题干里 (不开新题)
        assert len(drafts) == 2, drafts
        assert drafts[0].source_qnum == "01"  # 零填充, 让字典序 = 数值序
        assert "第一题题干" in drafts[0].content_md
        assert "（1） 子问一" in drafts[0].content_md
        assert "（2） 子问二" in drafts[0].content_md
        assert "（3） 子问三" in drafts[0].content_md
        assert drafts[1].source_qnum == "02"  # 零填充, 让字典序 = 数值序
        assert "第二题题干" in drafts[1].content_md
        assert "（1） 子问一" in drafts[1].content_md
        assert "（2） 子问二" in drafts[1].content_md
        # 卷头注意事项不应被任何题"吞"进来 (它是题号前的内容, 全部丢弃)
        for d in drafts:
            assert "注意事项" not in d.content_md

    def test_extract_drafts_demotes_circled_qnum_after_digit_top(self):
        """M5-4: 真卷 11 题下面 ①/②/③/④ 实验步骤圈码被识别为顶级题号 — 修复后
        圈码在见过数字顶级题号后降级为子编号, 落到当前顶级题的题干中."""
        from exam_to_html.backend._qnum_fallback import extract_drafts_with_lenient_qnum
        import unittest.mock as mock
        # 模拟 PyMuPDF 抽出: 11 题 + 4 个圈码子编号 + 12 题
        with mock.patch(
            "exam_to_html.backend._qnum_fallback._iter_pages_text",
            return_value=[(0,
                "注意事项\n"
                "11．（8 分）某实验\n"
                "实验步骤:\n"
                "①测量两个滑块的质量\n"
                "②接通气源\n"
                "③拨动两滑块\n"
                "④导出传感器数据\n"
                "则本实验要探究的问题是____。\n"
                "12．（8 分）下一道题\n"
                "继续 12 题内容\n"
            )],
        ):
            drafts = extract_drafts_with_lenient_qnum("mock.pdf")
        # 应该 2 道顶级题 (11 和 12), ①/②/③/④ 应附在 11 题题干中
        assert len(drafts) == 2, drafts
        assert drafts[0].source_qnum == "11"
        assert "①测量两个滑块的质量" in drafts[0].content_md
        assert "②接通气源" in drafts[0].content_md
        assert "③拨动两滑块" in drafts[0].content_md
        assert "④导出传感器数据" in drafts[0].content_md
        assert drafts[1].source_qnum == "12"
        assert "下一道题" in drafts[1].content_md

    def test_extract_drafts_accepts_circled_qnum_when_no_digit_top(self):
        """M5-4 边界: 整张卷子都用圈码顶级 (e.g. ①/②/③/④), 没有数字顶级 —
        圈码应仍被认作顶级题号, 否则 0 题识别 = 兜底失败."""
        from exam_to_html.backend._qnum_fallback import extract_drafts_with_lenient_qnum
        import unittest.mock as mock
        with mock.patch(
            "exam_to_html.backend._qnum_fallback._iter_pages_text",
            return_value=[(0,
                "① 第一题题干\n"
                "继续第一题\n"
                "② 第二题题干\n"
                "继续第二题\n"
                "③ 第三题题干\n"
            )],
        ):
            drafts = extract_drafts_with_lenient_qnum("mock.pdf")
        assert len(drafts) == 3, drafts
        assert drafts[0].source_qnum == "01"
        assert "第一题题干" in drafts[0].content_md
        assert drafts[1].source_qnum == "02"
        assert drafts[2].source_qnum == "03"


# ============================================================
# 兜底与 pipeline 集成 — PDF2PPT 0 题时, _qnum_fallback 接管
# ============================================================
class TestQnumFallbackPipelineHook:
    """convert_pdf 在 process_inbox 返 drafts=0 时, 应调用 _qnum_fallback 兜底."""

    def test_fallback_drafts_insert_into_db_successfully(self, tmp_path, monkeypatch):
        """fallback 抽出 drafts → 走 add_question_with_dedupe 入库 → DB 可见.

        DB 隔离由 tests/conftest.py 通过 TOPIC_GARDEN_DB_PATH env var 保证
        (per-pid 路径, 不污染 repo 根的 db.sqlite3)。
        """
        pytest.importorskip("topic_garden")
        from exam_to_html.backend.pipeline import _ensure_topic_garden_db
        from exam_to_html.backend._qnum_fallback import extract_drafts_with_lenient_qnum
        from topic_garden import db as tg_db
        from topic_garden.models import QuestionDraft

        _ensure_topic_garden_db()

        # 用 UUID 命名 source_paper 保证全局唯一, 跨 run 不撞
        unique_stem = f"fallback_test_{uuid.uuid4().hex[:12]}"

        # content_md 也用 UUID 防 Jaccard near_dup
        suffix = uuid.uuid4().hex[:8]
        fake_drafts = [
            QuestionDraft(
                content_md=f"fallback-unique-{suffix}-A 一段独特题干防撞 一",
                has_figure=False, figure_paths=[],
                source_page=0, source_qnum="1",
                q_type="fill_blank", is_multi_select=None,
                tag_slugs=[], notes=None,
            ),
            QuestionDraft(
                content_md=f"fallback-unique-{suffix}-B 一段独特题干防撞 二",
                has_figure=False, figure_paths=[],
                source_page=0, source_qnum="2",
                q_type="fill_blank", is_multi_select=None,
                tag_slugs=[], notes=None,
            ),
        ]
        monkeypatch.setattr(
            "exam_to_html.backend._qnum_fallback._iter_pages_text",
            lambda pdf_path: [(0, "mock page text")],
        )
        monkeypatch.setattr(
            "exam_to_html.backend._qnum_fallback._build_drafts_from_pages",
            lambda pages: fake_drafts,
        )

        pdf = tmp_path / f"{unique_stem}.pdf"
        pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")

        # 1. 兜底函数返 2 个 draft (mock 注入)
        drafts = extract_drafts_with_lenient_qnum(str(pdf))
        assert len(drafts) == 2

        # 2. 入库 (unique_stem 保证 0 collision, 不依赖 DB 干净状态)
        for d in drafts:
            qid, is_new, sim, reason = tg_db.add_question_with_dedupe(
                content_md=d.content_md,
                source_paper=unique_stem,
                source_qnum=d.source_qnum,
                source_page=d.source_page,
                q_type=d.q_type,
                notes=d.notes,
            )
            assert qid is not None and qid > 0

        # 3. 按 unique_stem 查, 至少 2 条
        rows = list(tg_db.Question.select().where(tg_db.Question.source_paper == unique_stem))
        assert len(rows) >= 2
        assert {r.source_qnum for r in rows} >= {"1", "2"}

    def test_convert_pdf_invokes_fallback_when_drafts_zero(self, tmp_path, monkeypatch):
        """convert_pdf 在 process_inbox 返 drafts=0 时, 必须调用 _qnum_fallback.

        这验证 hook 点是否真的接到了 — 不需要跑完整 compose, 只要兜底函数被调用。
        """
        pytest.importorskip("topic_garden")
        from exam_to_html.backend.pipeline import convert_pdf, _ensure_topic_garden_db

        monkeypatch.chdir(tmp_path)
        _ensure_topic_garden_db()

        # 1. Mock process_inbox 返 drafts=0
        def fake_process_inbox(**kwargs):
            return {
                "summary": {
                    "drafts": 0, "inserted": 0, "skipped": 0, "near_dup": 0,
                    "files_total": 1, "files_completed": 0,
                    "files_partial": 0, "files_failed": 1,
                    "failed_questions": 0,
                },
                "files": [],
            }
        monkeypatch.setattr(
            "topic_garden.ingest_inbox.process_inbox", fake_process_inbox
        )

        # 2. Mock _qnum_fallback 返 [] (无 qnum) → 应走 NoQuestionsError, 不 compose
        # pipeline 里是 local import: from ._qnum_fallback import ...
        # patch 源模块即可
        monkeypatch.setattr(
            "exam_to_html.backend._qnum_fallback.extract_drafts_with_lenient_qnum",
            lambda pdf_path: [],
        )

        # 3. PDF + output_dir
        pdf = tmp_path / "test.pdf"
        pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
        out = tmp_path / "out"
        out.mkdir()

        # 4. 期望 NoQuestionsError (因为兜底也返 0)
        from exam_to_html.errors import NoQuestionsError
        with pytest.raises(NoQuestionsError) as exc:
            convert_pdf(pdf_path=pdf, output_dir=out, mode="flash")
        # drafts=0 + 兜底 0 → 报"扫描版/题号格式非标准"
        msg = exc.value.message
        assert ("扫描版" in msg) or ("题号格式" in msg), \
            f"兜底 0 题时报错消息缺成因提示: {msg}"
