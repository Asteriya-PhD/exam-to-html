"""examples/demo_via_pdf2ppt.py — PDF2PPT MinerUParser + 真 MinerU 云 API

调用真 MinerU (需 MINERU_TOKEN, 仓内 .env 已配). PDF2PPT MinerUParser
拿到已结构化 markdown 后切题/识别选项/公式 — 物理卷标准解析路径.

用法:
    PYTHONPATH=exam-to-html:exam-to-html/pdf2ppt:topic_garden_app/src \\
    .venv/bin/python examples/demo_via_pdf2ppt.py [pdf]

也可不带参数 — 默认 topic_garden_app/archive/inbox/ 第一个 PDF.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PDF2PPT_LOCAL = _REPO_ROOT / "pdf2ppt"
_TGA_SRC = Path("/Users/zhewenliu/Claude/topic_garden_app/src")

# 清理 sys.path (移除 .pth 自动注入的旧 PDF2PPT/), 强制本地版本优先
sys.path[:] = [
    p for p in sys.path
    if "PDF2PPT/" not in p and "PDF2PPT\\" not in p
]
for p in [str(_PDF2PPT_LOCAL), str(_REPO_ROOT / "exam_to_html"), str(_TGA_SRC)]:
    if Path(p).exists() and p not in sys.path:
        sys.path.insert(0, p)

# 同样清掉 sys.modules 缓存
for k in list(sys.modules.keys()):
    if k == "pdf2ppt" or k.startswith("pdf2ppt."):
        del sys.modules[k]


def _ensure_peewee_topic_garden():
    try:
        import peewee  # noqa: F401
        import jinja2  # noqa: F401
        from topic_garden import db  # noqa: F401
    except ImportError as e:
        print(f"❌ topic_garden 依赖缺失: {e}")
        print("请运行: pip install -e ../topic_garden_app")
        sys.exit(1)


def _load_mineru_token() -> str:
    """从仓内 .env 或环境变量读 MinerU token."""
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / ".env")
    token = os.environ.get("MINERU_TOKEN") or os.environ.get("mineru_token")
    if not token:
        print("❌ MINERU_TOKEN 未设. 在 .env 配 MINERU_TOKEN=... 或环境变量导出.")
        sys.exit(1)
    return token


def _link_images(output_dir: Path, courseware_images: Path) -> None:
    """symlink output_dir/images → courseware/images."""
    target = output_dir / "images"
    if target.is_symlink():
        target.unlink()
    elif target.is_dir():
        import shutil
        shutil.rmtree(target)
    if courseware_images.exists():
        target.symlink_to(courseware_images.resolve(), target_is_directory=True)


def run(pdf_path: str, out_dir: str = "output") -> Path:
    """完整流程: PDF → 真 MinerU 云 API → PDF2PPT MinerUParser → topic_garden → HTML."""
    _ensure_peewee_topic_garden()

    pdf = Path(pdf_path).resolve()
    if not pdf.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf}")

    db_path = Path(tempfile.gettempdir()) / f"exam_demo_{os.getpid()}.db"
    os.environ["TOPIC_GARDEN_DB_PATH"] = str(db_path)

    token = _load_mineru_token()

    from pdf2ppt._v2_parser import MinerUParser
    from topic_garden import db as tg_db
    from topic_garden.db import Question, Topic, add_topic_question
    from topic_garden.composer import TopicComposer
    from exam_to_html.backend.exam_renderer import render_exam_html
    from exam_to_html.paths import courseware_images_dir

    tg_db.init_db()

    print(f"📄 解析 PDF: {pdf.name} (真 MinerU token)")
    parser = MinerUParser(token=token)
    exam = parser.parse(str(pdf), flash=True)
    print(f"   → {len(exam.questions)} 题, {exam.page_count} 页")

    stem = pdf.stem
    inserted = 0
    for q in exam.questions:
        md_parts = [b.content for b in q.blocks if b.content]
        content_md = "\n".join(md_parts).strip()
        if not content_md:
            continue
        q_type = q.question_type if q.question_type != "unknown" else "fill_blank"
        try:
            tg_db.add_question_with_dedupe(
                content_md=content_md,
                source_paper=stem,
                source_qnum=str(q.index).zfill(2) if q.index else "00",
                source_page=q.source_page,
                q_type=q_type,
                notes=None,
                figure_paths=None,
                has_figure=False,
                is_multi_select=q.is_multi_select,
            )
            inserted += 1
        except Exception as e:
            print(f"   skip q{q.index}: {e}")
    print(f"   → 入库 {inserted} 题")

    questions = list(
        Question.select().where(Question.source_paper == stem).order_by(Question.source_qnum)
    )
    if not questions:
        raise RuntimeError(f"入库后 0 题: {stem}")

    topic = Topic.create(
        title=stem,
        day_label="adhoc",
        expected_layout={"作业": len(questions)},
    )
    for q in questions:
        add_topic_question(topic.id, q.id, role="作业", priority=100)

    composer = TopicComposer()
    cr = composer.compose(topic_id=topic.id)

    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    html_path = out / f"{stem}.html"
    html_path.write_text(render_exam_html(cr, title=stem), encoding="utf-8")

    _link_images(out, courseware_images_dir())

    print(f"✅ HTML: {html_path} ({html_path.stat().st_size:,} bytes)")
    print(f"   图目录: {out/'images'}")
    return html_path


def main():
    parser = argparse.ArgumentParser(description="用 PDF2PPT 跑 PDF 试卷演示 (真 MinerU token)")
    parser.add_argument("pdf", nargs="?", help="PDF 路径 (默认 topic_garden_app/archive/inbox/ 第一个)")
    parser.add_argument("-o", "--out-dir", default="output", help="输出目录")
    args = parser.parse_args()

    pdf = args.pdf
    if not pdf:
        candidates = [
            Path("/Users/zhewenliu/Claude/topic_garden_app/archive/inbox"),
            _REPO_ROOT / "archive" / "inbox",
        ]
        candidate = next((c for c in candidates if c.exists()), None)
        pdfs = sorted(candidate.glob("*.pdf")) if candidate else []
        if not pdfs:
            print(f"❌ 没找到 PDF: {candidate}")
            sys.exit(1)
        pdf = str(pdfs[0])
        print(f"使用默认 PDF: {pdf}")

    html = run(pdf, out_dir=args.out_dir)
    print(f"\n🌐 在浏览器打开: file://{html}")


if __name__ == "__main__":
    main()