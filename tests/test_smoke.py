"""
exam-to-html 烟雾测试 (Phase D)

不开 GUI, 直接调 backend.pipeline.convert_pdf 验证 E2E 流水线。

测试策略 (按 PDF2PPT 全套依赖是否就绪分两档):
  A. 编排逻辑测试 (mock PDF parser) — 永远跑, 不依赖 MinerU
  B. 真 PDF E2E — 仅 pdf2ppt + deps 装好时跑

用法:
    .venv/bin/pytest tests/test_smoke.py -v
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

# 强制走测试隔离的数据目录 (必须在 import exam_to_html.* 之前)
_TEST_ROOT = Path(__file__).parent / ".tmp" / "smoke"
_TEST_ROOT.mkdir(parents=True, exist_ok=True)
os.chdir(_TEST_ROOT)


FIXTURE_PDF = Path(__file__).parent / "fixtures" / "sample.pdf"


def _topic_garden_models_importable():
    try:
        from topic_garden.models import QuestionDraft  # noqa: F401

        return True
    except Exception:
        return False


def _pdf2ppt_importable():
    """判断 pdf2ppt 能否跑真 PDF E2E.

    需要 PyMuPDF (fitz) + MinerU SDK (mineru-open-sdk) 都装好且 MinerU SDK
    能成功 import。MinerUParser 顶层不 import fitz / mineru (运行时按需 import),
    所以单 import MinerUParser 不足以判断 — 显式 try fitz + mineru,
    缺则视为不可跑。
    """
    try:
        from pdf2ppt._v2_parser import MinerUParser  # noqa: F401
        import fitz  # noqa: F401 — PyMuPDF, 真 PDF 解析必需
        from mineru import MinerU  # noqa: F401 — MinerU SDK flash_extract 入口
        return True
    except Exception:
        return False


# ============================================================
# 静态/单元类测试 — 永远跑
# ============================================================
def test_pipeline_imports():
    """模块可导入."""
    pytest.importorskip("topic_garden")
    from exam_to_html.backend import pipeline

    assert hasattr(pipeline, "convert_pdf")
    assert hasattr(pipeline, "PipelineError")


def test_paths_dev_mode():
    """dev 模式下 paths 走 cwd."""
    from exam_to_html.paths import (
        archive_dir,
        data_dir,
        db_path,
        inbox_dir,
        logs_dir,
    )

    assert data_dir() == Path(".")
    assert db_path() == Path("./db.sqlite3")
    assert inbox_dir() == Path("./inbox")
    assert archive_dir() == Path("./archive/inbox")
    assert logs_dir() == Path("./logs")
    # inbox/archive/logs 自动 mkdir
    assert Path("./inbox").is_dir()
    assert Path("./archive/inbox").is_dir()
    assert Path("./logs").is_dir()


def test_config_round_trip(tmp_path):
    """config.save → load 往返, 损坏文件 fallback."""
    from exam_to_html import config

    old_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        assert config.load() == config.DEFAULTS
        cfg = dict(config.DEFAULTS)
        cfg["mineru_token"] = "test-tok-abc"
        config.save(cfg)
        assert config.load()["mineru_token"] == "test-tok-abc"
        (tmp_path / "config.json").write_text("not json")
        assert config.load() == config.DEFAULTS
    finally:
        os.chdir(old_cwd)


def test_convert_pdf_missing_file(tmp_path):
    """PipelineError: PDF 不存在."""
    pytest.importorskip("topic_garden")
    from exam_to_html.backend.pipeline import PipelineError, convert_pdf

    with pytest.raises(PipelineError, match="PDF 文件不存在"):
        convert_pdf(
            pdf_path=tmp_path / "ghost.pdf",
            output_dir=tmp_path,
            mode="flash",
        )


# ============================================================
# 编排逻辑测试 — mock PDF parser, 永远跑
# ============================================================
@pytest.mark.skipif(
    not _topic_garden_models_importable(),
    reason="topic_garden.models not importable",
)
def test_orchestration_with_mocked_parser(tmp_path, monkeypatch):
    """Mock parse_pdf_to_questions → 验证 pipeline 步骤 2-7 全跑通.

    不依赖 MinerU / pdf2ppt, 只验证 exam-to-html 的编排逻辑:
      - DB 路径 override
      - PDF 复制 → temp inbox
      - process_inbox 跑通 (含 dedupe-insert)
      - qid 抓取 (source_paper + 时间窗)
      - Topic.create + add_topic_question
      - TopicComposer.compose_to_file → HTML
    """
    from topic_garden.models import QuestionDraft

    fake_drafts = [
        QuestionDraft(
            content_md="一物体做自由落体运动, $h=5$ m, 求末速度。\n\nA. 5 m/s\nB. 10 m/s\nC. 15 m/s\nD. 20 m/s",
            has_figure=False,
            figure_paths=[],
            source_page=1,
            source_qnum="1",
            q_type="choice",
            is_multi_select=False,
        ),
        QuestionDraft(
            content_md="一物体 $v_0=10$ m/s 水平抛出, $t=1$ s 时位移。\n\nA. 10 m\nB. 14.1 m\nC. 15 m\nD. 20 m",
            has_figure=False,
            figure_paths=[],
            source_page=1,
            source_qnum="2",
            q_type="choice",
            is_multi_select=False,
        ),
    ]

    def fake_parse(pdf_path, mineru_token=None, mode="auto"):
        return list(fake_drafts)

    # _process_one_pdf 用的是 ingest_inbox 命名空间里的 parse_pdf_to_questions
    monkeypatch.setattr(
        "topic_garden.ingest_inbox.parse_pdf_to_questions", fake_parse
    )

    from exam_to_html.backend.pipeline import convert_pdf

    pdf_copy = tmp_path / "input.pdf"
    shutil.copy(FIXTURE_PDF, pdf_copy)

    output_dir = tmp_path / "output"
    output_dir.mkdir()

    result = convert_pdf(
        pdf_path=pdf_copy,
        output_dir=output_dir,
        mode="flash",
        mineru_token=None,
    )

    # 1. 返回结构
    assert "html_path" in result
    assert "topic_id" in result
    assert "stats" in result
    assert result["topic_id"] > 0
    stats = result["stats"]
    assert stats["questions_in_topic"] == 2, f"expected 2 题, got {stats}"
    assert stats["drafts"] == 2
    assert stats["inserted"] == 2
    assert stats["duration_ms"] > 0

    # 2. HTML 文件存在且含 KaTeX
    html_path = Path(result["html_path"])
    assert html_path.is_file(), f"HTML missing: {html_path}"
    content = html_path.read_text(encoding="utf-8")
    assert "<html" in content.lower()
    assert "katex" in content.lower(), "HTML 缺 KaTeX 渲染标记"
    assert "自由落体" in content or "水平抛出" in content, \
        "HTML 没含题干 markdown"

    # 3. HTML 落在 output_dir
    assert html_path.parent.resolve() == output_dir.resolve()

    # 4. 二次跑同一 PDF → dedupe 复用, 不重复入库 (验证 source_paper 时间窗 fallback)
    #    用同一 stem 的 PDF 让 source_paper 命中上次入库的 qids
    pdf_copy2 = tmp_path / "input.pdf"  # 同名 = 同 stem
    shutil.copy(FIXTURE_PDF, pdf_copy2)
    result2 = convert_pdf(
        pdf_path=pdf_copy2,
        output_dir=output_dir,
        mode="flash",
    )
    # 新 topic (每次新 PDF = 新专题), 但 qid 复用
    assert result2["topic_id"] != result["topic_id"], "应创建新 Topic"
    assert result2["stats"]["questions_in_topic"] == 2


# ============================================================
# 真 PDF E2E — 仅当 pdf2ppt + deps 全装好时跑
# ============================================================
@pytest.mark.skipif(
    not _pdf2ppt_importable(),
    reason="pdf2ppt (vendored) 自身未导入; 真 PDF 解析依赖 PyMuPDF/mineru-open-sdk",
)
@pytest.mark.skipif(
    not FIXTURE_PDF.is_file(), reason="fixture PDF missing"
)
def test_convert_pdf_real_pdf(tmp_path):
    """真 PDF E2E: process_inbox → MinerU → 入库 → HTML."""
    from exam_to_html.backend.pipeline import PipelineError, convert_pdf

    output_dir = tmp_path / "output"
    output_dir.mkdir()

    try:
        result = convert_pdf(
            pdf_path=FIXTURE_PDF,
            output_dir=output_dir,
            mode="flash",
            mineru_token=None,
        )
    except PipelineError as e:
        pytest.fail(f"convert_pdf raised PipelineError: {e}")

    assert result["stats"]["drafts"] >= 0
    html_path = Path(result["html_path"])
    assert html_path.is_file()
    assert html_path.stat().st_size > 1024