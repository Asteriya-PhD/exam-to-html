"""examples/demo_via_pdf2ppt.py — 用 PDF2PPT MinerUParser (高保真) 跑物理卷演示

PDF2PPT._v2_parser.MinerUParser 是 PDF2PPT 项目里针对物理卷成熟的解析器,
精度高于 exam-to-html.backend._qnum_fallback (后者只在 MinerU SDK 不可用
时才走, 是兜底).

本脚本:
1. 用 fake MinerU SDK (PyMuPDF 抽 markdown) 替代真实云 API — 离线可跑
2. 调 vendored pdf2ppt._v2_parser.MinerUParser 解析
3. 把 ParsedExam 转 topic_garden QuestionDraft 入库
4. 走 TopicComposer + render_exam_html 渲染
5. 输出到 output/<stem>.html + 把题图 symlink 到同级 images/

用法:
    pip install -e ../topic_garden_app
    python examples/demo_via_pdf2ppt.py <path/to/pdf>
    python examples/demo_via_pdf2ppt.py  # 默认 archive/inbox/ 第一个 PDF
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import types
from pathlib import Path

# 把 vendored pdf2ppt 与 topic_garden_app 加入 path
# 必须在所有 import 之前; 同时移除可能的旧 PDF2PPT/ 冲突目录 (sys.path 里有
# /Users/zhewenliu/Claude/PDF2PPT/pdf2ppt 会抢先加载过期版本).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_PDF2PPT_LOCAL = _REPO_ROOT / "pdf2ppt"
_TGA_SRC = Path("/Users/zhewenliu/Claude/topic_garden_app/src")

# 清理 path: 移除所有指向 PDF2PPT/ 兄弟仓旧版的路径 (包括 venv .pth 添加的)
# 然后插入本地版本优先
sys.path[:] = [
    p for p in sys.path
    if "PDF2PPT/" not in p and "PDF2PPT\\" not in p
]
# 然后插入本地路径 (本地优先)
for p in [str(_PDF2PPT_LOCAL), str(_REPO_ROOT / "exam_to_html"), str(_TGA_SRC)]:
    if Path(p).exists() and p not in sys.path:
        sys.path.insert(0, p)


def _install_fake_mineru():
    """装 fake MinerU SDK — 不调网络, 用 PyMuPDF 直接抽 markdown.

    PDF2PPT MinerUParser 调 MinerU.flash_extract() 拿 markdown. 真 MinerU SDK
    是云 API (需 token). fake 实现完全兼容 MinerU 接口形状, 离线可跑.

    MinerU.__init__ 实际需要 token 参数, 但默认 token=None 时 _api=None (flash-only).
    """
    import fitz  # noqa: F401  # ensure installed

    class _FakeApiClient:
        source = "pdf2ppt-v2"

    class _FakeFlashApiClient:
        source = "pdf2ppt-v2"

    class _FakeMinerU:
        def __init__(self, *a, **kw):
            self._api = _FakeApiClient()
            self._flash_api = _FakeFlashApiClient()

        def set_source(self, source):
            self._api.source = source
            self._flash_api.source = source

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

        def flash_extract(self, pdf_path, **kw):
            import fitz as _fitz
            doc = _fitz.open(pdf_path)
            pages = [doc[pn].get_text("text") for pn in range(len(doc))]
            doc.close()
            return types.SimpleNamespace(
                state="done",
                markdown="\n\n".join(pages),
                images=[],
                content_list=[],
            )

        def extract(self, pdf_path, **kw):
            return self.flash_extract(pdf_path, **kw)

    if "mineru" not in sys.modules:
        m = types.ModuleType("mineru")
        m.MinerU = _FakeMinerU
        sys.modules["mineru"] = m
    if "mineru_open_sdk" not in sys.modules:
        sys.modules["mineru_open_sdk"] = types.ModuleType("mineru_open_sdk")


def _ensure_peewee_topic_garden():
    """确保 topic_garden 依赖 (peewee, jinja2) 在 path 里."""
    try:
        import peewee  # noqa: F401
        import jinja2  # noqa: F401
        from topic_garden import db  # noqa: F401
    except ImportError as e:
        print(f"❌ topic_garden 依赖缺失: {e}")
        print("请运行: pip install -e ../topic_garden_app")
        sys.exit(1)


def _link_images(output_dir: Path, courseware_images: Path) -> None:
    """symlink output_dir/images → topic_garden courseware/images (PDF2PPT 抽出的图)."""
    target = output_dir / "images"
    if target.is_symlink():
        target.unlink()
    elif target.is_dir():
        import shutil
        shutil.rmtree(target)
    if courseware_images.exists():
        target.symlink_to(courseware_images.resolve(), target_is_directory=True)
    else:
        # fake 模式: PDF2PPT 抽的图在 tmp, 复制过去
        import shutil
        target.mkdir()
        for f in courseware_images.glob("*.png"):
            shutil.copy2(f, target / f.name)


def run(pdf_path: str, out_dir: str = "output") -> Path:
    """完整流程: PDF → PDF2PPT MinerU → topic_garden DB → render_exam_html → 文件.

    返回 HTML 文件路径.
    """
    _install_fake_mineru()
    _ensure_peewee_topic_garden()

    pdf = Path(pdf_path).resolve()
    if not pdf.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf}")

    # 用独立 DB 避免污染
    db_path = Path(tempfile.gettempdir()) / f"exam_demo_{os.getpid()}.db"
    os.environ["TOPIC_GARDEN_DB_PATH"] = str(db_path)

    # 强制清掉已加载的 pdf2ppt (避免其他位置先 import 了 PDF2PPT/ 旧版)
    for k in list(sys.modules.keys()):
        if k == "pdf2ppt" or k.startswith("pdf2ppt."):
            del sys.modules[k]

    from pdf2ppt._v2_parser import MinerUParser
    from pdf2ppt._v2_models import ContentBlock
    from topic_garden import db as tg_db
    from topic_garden.db import Question, Topic, add_topic_question
    from topic_garden.composer import TopicComposer
    from exam_to_html.backend.exam_renderer import render_exam_html
    from exam_to_html.paths import courseware_images_dir

    tg_db.init_db()

    # Step 1: PDF2PPT MinerUParser 解析
    print(f"📄 解析 PDF: {pdf.name}")
    parser = MinerUParser(token=None)
    exam = parser.parse(str(pdf), flash=True)
    print(f"   → {len(exam.questions)} 题, {exam.page_count} 页")

    # Step 2: 转 topic_garden QuestionDraft 入库
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

    # Step 3: Topic + compose + render
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

    # Step 4: 写 HTML
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    html_path = out / f"{stem}.html"
    html_path.write_text(render_exam_html(cr, title=stem), encoding="utf-8")

    # Step 5: symlink 图片
    _link_images(out, courseware_images_dir())

    print(f"✅ HTML: {html_path} ({html_path.stat().st_size:,} bytes)")
    print(f"   图目录: {out/'images'}")
    return html_path


def main():
    parser = argparse.ArgumentParser(description="用 PDF2PPT 跑 PDF 试卷演示")
    parser.add_argument("pdf", nargs="?", help="PDF 路径 (默认 archive/inbox/ 第一个)")
    parser.add_argument("-o", "--out-dir", default="output", help="输出目录")
    args = parser.parse_args()

    pdf = args.pdf
    if not pdf:
        # 默认 topic_garden_app/archive/inbox/ 第一个 (该目录不入 exam-to-html 仓,
        # 教师真实卷子源; 避免 demo 默认 PDF 不存在)
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