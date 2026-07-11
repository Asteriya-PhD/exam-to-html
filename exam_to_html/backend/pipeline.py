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
import os
import shutil
import sys
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
from ..paths import archive_dir, courseware_images_dir, db_path

log = logging.getLogger(__name__)

# 设计文档 §5.1 阈值
MAX_PDF_SIZE_BYTES = 100 * 1024 * 1024   # 100MB
MIN_DISK_SPACE_BYTES = 200 * 1024 * 1024  # 200MB

# DB 锁重试
DB_LOCK_RETRY_TIMES = 3
DB_LOCK_RETRY_INTERVAL_SEC = 0.1


# ============================================================
# images/ 目录链接 — 模板里 <img src="images/X.jpg"> 需同级
# ============================================================
def _ensure_images_link(output_dir: Path) -> Path:
    """确保 output_dir/images 存在并指向 topic_garden 的 courseware/images.

    优先用 symlink (避免复制 3000+ 张图), Windows 走 junction.
    若 symlink 不支持 (Windows 非管理员), 退化为 copy_tree.

    修 M-15: 检测过期 symlink — 目标不存在时 unlink + 重建, 避免 <img src="..."> 404。

    Returns:
        output_dir/images 路径 (新创建或已存在)
    """
    target = output_dir / "images"
    if target.is_dir() and not target.is_symlink():
        return target
    if target.is_symlink():
        # 修 M-15: 显式解析 — resolve(strict=True) 若 target 不存在会 OSError
        try:
            resolved = target.resolve(strict=True)
            if not resolved.exists():
                # symlink 存在但 target 不存在 (courseware/images 被删/移动)
                log.warning("[pipeline] 过期 symlink → %s 已失效, 重建", resolved)
                target.unlink()
            else:
                return target
        except (OSError, RuntimeError):
            target.unlink()

    src = courseware_images_dir()
    if not src.is_dir():
        log.warning("[pipeline] courseware/images 不存在: %s — 试卷图将无法显示", src)
        return target

    try:
        if sys.platform != "win32":
            target.symlink_to(src.resolve(), target_is_directory=True)
        else:
            try:
                import _winapi  # type: ignore
                _winapi.CreateJunction(str(src.resolve()), str(target))
            except (ImportError, OSError):
                shutil.copytree(src, target)
    except OSError as e:
        log.warning("[pipeline] 创建 images 链接失败 (%s), 退化为复制", e)
        if not target.exists():
            shutil.copytree(src, target)
    return target


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
        # PyMuPDF 1.28+ 在损坏 PDF 上偶尔返回 None 而非抛 RuntimeError,
        # 必须显式判 None, 否则 finally doc.close() 报 NoneType.close
        if doc is None:
            log.warning("[pipeline] fitz.open 返回 None (损坏 PDF?), 跳过加密检查")
            return
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
    # 优先尊重 TOPIC_GARDEN_DB_PATH env var (测试隔离 / CI 用),无 env 才走 paths.db_path()
    env_db = os.environ.get("TOPIC_GARDEN_DB_PATH")
    target = env_db if env_db else str(db_path())
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

    # process_inbox summary 是诊断真相: drafts=0 → parser 没识别到题号(扫描版 / 题号格式
    # 不符合 / MinerU 失败); inserted=0 但 drafts>0 → 全被 dedup 命中(已存在同题).
    # 这两种情形都该反映在错误消息里,而不是让 pipeline 后续空查询顶一个"PDF 解析成功".
    summary = (result or {}).get("summary", {}) if isinstance(result, dict) else {}
    drafts_total = summary.get("drafts", 0)
    inserted_total = summary.get("inserted", 0)
    files_failed = summary.get("files_failed", 0)
    log.info(
        "[pipeline] process_inbox summary: %s",
        {k: summary.get(k) for k in ("drafts", "inserted", "skipped", "near_dup", "files_completed", "files_failed", "files_partial")},
    )

    from topic_garden.db import (
        Question,
        Topic,
        add_topic_question,
        add_question_with_dedupe,
    )

    questions = _with_db_retry(
        lambda: list(
            Question.select().where(
                (Question.source_paper == stem)
                & (Question.created_at >= started_wallclock)
            ).order_by(Question.created_at, Question.source_qnum)
        )
    )
    if not questions:
        # 退化: 时间窗没抓到 (clock skew / 旧数据),再按 stem 全量扫
        questions = _with_db_retry(
            lambda: list(
                Question.select().where(
                    Question.source_paper == stem
                ).order_by(Question.created_at, Question.source_qnum)
            )
        )
    if not questions:
        # 区分两类失败,前端 message 才能给教师正确的下一步
        if drafts_total == 0:
            # Parser 没识别到题号 (扫描版 / 题号格式非标准 / MinerU 静默失败)
            # ── 兜底: 用 _qnum_fallback 从 PDF 原文按宽松正则重抽 ──
            # 仅在 drafts=0 时触发, 不影响正常 dedup 路径 (零 API 成本, ~50ms PyMuPDF)
            from ._qnum_fallback import extract_drafts_with_lenient_qnum
            fallback_drafts = extract_drafts_with_lenient_qnum(str(pdf_path))
            if fallback_drafts:
                log.warning(
                    "[pipeline] 本地 pdf2ppt parser 0 题, 兜底正则抽出 %d 题 — %s",
                    len(fallback_drafts), pdf_path.name,
                )
                recovered = 0
                for d in fallback_drafts:
                    try:
                        _with_db_retry(
                            lambda d=d: add_question_with_dedupe(
                                content_md=d.content_md,
                                source_paper=stem,
                                source_qnum=d.source_qnum,
                                source_page=d.source_page,
                                q_type=d.q_type,
                                notes=d.notes,
                                figure_paths=getattr(d, "figure_paths", None) or None,
                                has_figure=getattr(d, "has_figure", None),
                                is_multi_select=getattr(d, "is_multi_select", None),
                            )
                        )
                        recovered += 1
                    except Exception as e:
                        log.warning("[pipeline] fallback insert 跳过: %s", e)
                if recovered:
                    # 重查 — 兜底入库的题现在该命中
                    questions = _with_db_retry(
                        lambda: list(
                            Question.select().where(
                                (Question.source_paper == stem)
                                & (Question.created_at >= started_wallclock)
                            ).order_by(Question.created_at, Question.source_qnum)
                        )
                    )
            if not questions:
                raise NoQuestionsError(
                    f"PDF 解析未识别到题目 (parser drafts=0, files_failed={files_failed}): "
                    f"{pdf_path.name} — 可能是扫描版或题号格式非标准"
                )
        elif inserted_total == 0 and drafts_total > 0:
            # 全被 dedup 命中 → 真有题但库中已存在
            raise NoQuestionsError(
                f"PDF 已入库 (parser drafts={drafts_total}, 全被 dedup 命中): {pdf_path.name}"
            )
        else:
            raise NoQuestionsError(
                f"PDF 解析后入库 0 题 (drafts={drafts_total}, inserted={inserted_total}): "
                f"{pdf_path.name}"
            )

    # K2/K3 题干层归一化 (K2-Q2 ABCD 误切 / K2-Q3 同行 4 选项 / K3-Q3 fill→choice
    # 错位 / K3-Q5 题型 [?] / 求: 截断 / 跨页图归属) — 必须在 Topic.create 前跑完,
    # 后续 composer 才能看到修正后的 content_md / q_type / figure_paths。
    # 不编造 OCR 丢失内容;只规范已有内容。
    if questions:
        try:
            from ._post_process_md import normalize_question_batch
            normalize_question_batch(questions)
            # 重新查询一遍,拿到归一化后的字段 (source_qnum / order 保持不变)
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
                        Question.select().where(Question.source_paper == stem)
                        .order_by(Question.created_at, Question.source_qnum)
                    )
                )
        except Exception as e:
            log.warning("[pipeline] post_process_md 跳过: %s", e)

    topic = _with_db_retry(
        lambda: Topic.create(
            title=stem, day_label="adhoc",
            expected_layout={"作业": len(questions)},
        )
    )
    log.info("[pipeline] created Topic #%d: %s (%d 题)", topic.id, stem, len(questions))

    attached = 0
    for i, q in enumerate(questions, start=1):
        # exam-to-html 用同一 priority (100) — 让 topic_garden composer v0.19
        # 按 (priority desc, source_qnum asc) 排序时, source_qnum 主导 (零填充
        # 过的 sqnum 字典序 = 数值序), 自然按题号顺序展示.
        try:
            add_topic_question(topic.id, q.id, role="作业", priority=100)
            attached += 1
        except Exception as e:
            log.warning("[pipeline] add_topic_question 跳过: qid=%d %s", q.id, e)
    if attached == 0:
        raise PipelineError(f"题目挂载全部失败 (Topic #{topic.id}, {len(questions)} 题)")

    # 7. 组题 + 写 HTML (走 exam.html 单页式模板 + 我们的 CSS wrapper)
    from topic_garden.composer import TopicComposer
    from .exam_renderer import render_exam_html
    output_path = output_dir / f"{stem}.html"
    composer = TopicComposer()
    try:
        try:
            compose_result = composer.compose(topic_id=topic.id)
            from topic_garden.db import log_compose
            full_html = render_exam_html(compose_result, title=stem)
            output_path.write_text(full_html, encoding="utf-8")
            _ensure_images_link(output_dir)
            try:
                log_compose(
                    topic_id=topic.id, html=full_html,
                    class_label=None, source="api",
                )
            except Exception as e:
                log.warning("[pipeline] log_compose 跳过: %s", e)
        except Exception as e:
            # 修 M-14: compose/write 失败时 Topic 变孤儿, 重试会创建新 Topic, 老 Topic 永留 DB。
            # 修法: 失败时 delete topic (含关联的 add_topic_question 行) + 清空输出文件
            log.exception("[pipeline] compose 失败, 回滚 Topic #%d", topic.id)
            try:
                from topic_garden.db import Topic, TopicQuestion
                # 先删关联行 (FK constraint), 再删 Topic
                TopicQuestion.delete().where(TopicQuestion.topic == topic.id).execute()
                Topic.delete().where(Topic.id == topic.id).execute()
            except Exception as cleanup_e:
                log.warning("[pipeline] 回滚 Topic #%d 失败 (留孤儿): %s", topic.id, cleanup_e)
            try:
                if output_path.exists():
                    output_path.unlink()
            except OSError:
                pass
            raise PipelineError(f"HTML 生成失败: {e}", cause=e) from e
    finally:
        # 修 M-9: 清理解析过程产生的 tmp 图片 (HTML 已渲染完, 不再引用)
        # topic_garden process_inbox 期间已把图复制到 courseware/images, 这里的
        # tmp 文件已是无引用孤儿 — 安全删。
        try:
            import glob
            import os as _os
            import tempfile as _tf
            tmpdir = _tf.gettempdir()
            # 清理本次 PDF 上传时间戳之后的 mineru_* 图片 (避免误删别的进程)
            for p in glob.glob(_os.path.join(tmpdir, "mineru_*")):
                try:
                    if _os.path.getmtime(p) >= started:
                        _os.unlink(p)
                except OSError:
                    pass
        except Exception as cleanup_e:
            log.debug("[pipeline] tmp cleanup skipped: %s", cleanup_e)

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
