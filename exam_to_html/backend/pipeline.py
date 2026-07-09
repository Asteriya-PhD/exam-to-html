"""
exam_to_html.backend.pipeline — PDF → HTML 讲评课件编排

复用 topic_garden 的 process_inbox + TopicComposer, 不重写 PDF 解析/组题。

5 步流水线 (单 PDF 粒度, 试卷讲评用例):
  0. 预检 (设计文档 §5.1): 文件存在 / .pdf 后缀 / 大小 / 加密 / 输出权限 / 磁盘空间
  1. 复制 PDF 到 per-call temp inbox (隔离, 不污染永久 archive)
  2. topic_garden.process_inbox() → 解析 + dedupe-insert + 归档
  3. 按 source_paper + 时间窗抓 qid (dedupe 复用旧 qid 也覆盖)
  4. Topic.create + add_topic_question (role=作业)
  5. TopicComposer.compose_to_file → HTML

设计权衡:
- 用 temp inbox 而非永久 inbox/: 避免教师上一次失败的 PDF 误被下次 process
- qid 抓取 source_paper + 时间窗: 时间窗保证本会话, source_paper 兼容 dedupe 复用
- TopicComposer k_modules={}: 试卷讲评无 K 模块分类, 留 K1-K5 空白即可
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from ..errors import (
    DbLockedError,
    EncryptedPdfError,
    FileTooLargeError,
    NoDiskSpaceError,
    NoQuestionsError,
    NonPdfError,
    OutputPermissionError,
    PipelineError,
)
from ..paths import archive_dir, db_path

log = logging.getLogger(__name__)

# 设计文档 §5.1 阈值
MAX_PDF_SIZE_BYTES = 100 * 1024 * 1024   # 100MB
MIN_DISK_SPACE_BYTES = 200 * 1024 * 1024  # 200MB

# DB 锁重试
DB_LOCK_RETRY_TIMES = 3
DB_LOCK_RETRY_INTERVAL_SEC = 0.1


def _check_pdf_extension(pdf_path: Path) -> None:
    if pdf_path.suffix.lower() != ".pdf":
        raise NonPdfError(f"非 PDF 文件: {pdf_path.name}")


def _check_file_size(pdf_path: Path) -> None:
    try:
        size = pdf_path.stat().st_size
    except OSError as e:
        raise PipelineError(f"无法读取文件大小: {e}", cause=e)
    if size > MAX_PDF_SIZE_BYTES:
        raise FileTooLargeError(f"文件 {size / 1024 / 1024:.1f}MB 超过 100MB")


def _check_pdf_encrypted(pdf_path: Path) -> None:
    try:
        import fitz
    except ImportError:
        log.warning("[pipeline] fitz 未装, 跳过加密检查")
        return
    try:
        doc = fitz.open(str(pdf_path))
        try:
            if doc.is_encrypted:
                raise EncryptedPdfError(f"PDF 已加密: {pdf_path.name}")
        finally:
            doc.close()
    except EncryptedPdfError:
        raise
    except Exception as e:
        log.warning("[pipeline] 加密检查失败 (后续 process_inbox 会捕获): %s", e)


def _check_output_dir(output_dir: Path) -> None:
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError) as e:
        raise OutputPermissionError(f"无法创建输出目录: {output_dir} ({e})", cause=e)
    testfile = output_dir / f".exam_to_html_write_test_{uuid.uuid4().hex[:8]}"
    try:
        testfile.write_text("test", encoding="utf-8")
    except (OSError, PermissionError) as e:
        raise OutputPermissionError(f"无法写入 {output_dir} ({e})", cause=e)
    finally:
        try:
            testfile.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        free = shutil.disk_usage(output_dir).free
    except OSError as e:
        log.warning("[pipeline] disk_usage 失败: %s", e)
        return
    if free < MIN_DISK_SPACE_BYTES:
        raise NoDiskSpaceError(f"磁盘剩余 {free / 1024 / 1024:.0f}MB, 需要 ≥200MB")


def _preflight(pdf_path: Path, output_dir: Path) -> None:
    if not pdf_path.is_file():
        raise PipelineError(f"PDF 文件不存在: {pdf_path}")
    _check_pdf_extension(pdf_path)
    _check_file_size(pdf_path)
    _check_pdf_encrypted(pdf_path)
    _check_output_dir(output_dir)


def _with_db_retry(fn, *args, **kwargs):
    import peewee
    last_err: Optional[Exception] = None
    for attempt in range(DB_LOCK_RETRY_TIMES):
        try:
            return fn(*args, **kwargs)
        except peewee.OperationalError as e:
            if "locked" not in str(e).lower():
                raise
            last_err = e
            log.warning("[pipeline] DB locked, retry %d/%d", attempt + 1, DB_LOCK_RETRY_TIMES)
            time.sleep(DB_LOCK_RETRY_INTERVAL_SEC)
    raise DbLockedError(f"数据库持续被锁 ({DB_LOCK_RETRY_TIMES} 次重试失败)", cause=last_err)


def _ensure_topic_garden_db() -> None:
    from topic_garden import db as tg_db
    target = str(db_path())
    if str(tg_db.DB_PATH) != target:
        tg_db.reset_db_path(target)
    _with_db_retry(tg_db.init_db)


def scan_incomplete_uploads(within_hours: int = 24) -> list:
    from datetime import datetime, timezone, timedelta
    from ..paths import archive_dir, inbox_dir
    threshold = datetime.now(timezone.utc) - timedelta(hours=within_hours)
    found = []
    for location, base_dir in (("inbox", inbox_dir()), ("archive", archive_dir())):
        if not base_dir.is_dir():
            continue
        for pdf in sorted(base_dir.glob("*.pdf")):
            if not pdf.is_file():
                continue
            try:
                stat = pdf.stat()
                mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if mtime < threshold:
                continue
            found.append({
                "filename": pdf.name, "path": str(pdf), "size": stat.st_size,
                "mtime": mtime.isoformat(), "location": location,
            })
    return found


def clear_incomplete_uploads(paths: list) -> int:
    deleted = 0
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
            deleted += 1
        except OSError as e:
            log.warning("[pipeline] 清除残留失败: %s — %s", p, e)
    return deleted


def convert_pdf(
    pdf_path: str | Path,
    output_dir: str | Path,
    mode: str = "auto",
    mineru_token: Optional[str] = None,
) -> Dict[str, Any]:
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    started = time.time()
    started_wallclock = datetime.utcnow()
    stem = pdf_path.stem

    _preflight(pdf_path, output_dir)
    _ensure_topic_garden_db()

    temp_inbox = Path(tempfile.mkdtemp(prefix="exam_inbox_"))
    archive = archive_dir()
    try:
        dest = temp_inbox / pdf_path.name
        shutil.copy2(pdf_path, dest)
        log.info("[pipeline] copied PDF → %s", dest)
        from topic_garden.ingest_inbox import process_inbox
        try:
            result = process_inbox(
                inbox_dir=temp_inbox, archive_dir=archive,
                mode=mode, mineru_token=mineru_token,
            )
        except Exception as e:
            log.exception("[pipeline] process_inbox 抛异常")
            raise PipelineError(f"PDF 解析失败: {e}", cause=e) from e
    finally:
        shutil.rmtree(temp_inbox, ignore_errors=True)

    from topic_garden.db import Question, Topic, add_topic_question

    questions = _with_db_retry(
        lambda: list(
            Question.select().where(
                (Question.source_paper == stem)
                & (Question.created_at >= started_wallclock)
            ).order_by(Question.created_at, Question.source_qnum)
        )
    )
    if not questions:
        questions = _with_db_retry(
            lambda: list(
                Question.select().where(
                    Question.source_paper == stem
                ).order_by(Question.created_at, Question.source_qnum)
            )
        )
    if not questions:
        raise NoQuestionsError(f"PDF 解析成功但无题目入库: {pdf_path.name}")

    topic = _with_db_retry(
        lambda: Topic.create(
            title=stem, day_label="adhoc",
            expected_layout={"作业": len(questions)},
        )
    )
    log.info("[pipeline] created Topic #%d: %s (%d 题)", topic.id, stem, len(questions))

    attached = 0
    for i, q in enumerate(questions, start=1):
        try:
            add_topic_question(topic.id, q.id, role="作业", priority=i)
            attached += 1
        except Exception as e:
            log.warning("[pipeline] add_topic_question 跳过: qid=%d %s", q.id, e)
    if attached == 0:
        raise PipelineError(f"题目挂载全部失败 (Topic #{topic.id}, {len(questions)} 题)")

    from topic_garden.composer import TopicComposer
    output_path = output_dir / f"{stem}.html"
    composer = TopicComposer()
    try:
        composer.compose_to_file(
            topic_id=topic.id, output_path=str(output_path),
            class_label=None, source="api",
        )
    except Exception as e:
        log.exception("[pipeline] compose 失败")
        raise PipelineError(f"HTML 生成失败: {e}", cause=e) from e

    summary = result.get("summary", {})
    duration_ms = int((time.time() - started) * 1000)
    return {
        "html_path": str(output_path),
        "topic_id": topic.id,
        "stats": {
            "drafts": summary.get("drafts", 0), "inserted": summary.get("inserted", 0),
            "skipped": summary.get("skipped", 0), "near_dup": summary.get("near_dup", 0),
            "questions_in_topic": len(questions), "duration_ms": duration_ms,
        },
    }


__all__ = ["convert_pdf", "PipelineError"]
