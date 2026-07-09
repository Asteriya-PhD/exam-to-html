"""
exam_to_html.backend.exam_renderer — 试卷讲评 HTML 渲染

- 复用 topic_garden.TopicComposer 拿题数据 + md→html (含选项/图/KaTeX 预处理)
- 用我们自己的 exam.html 模板 (单页式 + 侧边导航) 渲染 body
- 包成单文件 HTML (含 KaTeX + CSS + JS)
- 与 topic_garden 内置的 microtopic.html 模板互不影响
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"


# ============================================================
# CSS — 试卷讲评主题 (单页式布局)
# ============================================================
EXAM_CSS = r"""
:root {
  --zoom: 1.0;
  --sidebar-w: 56px;
  --topbar-h: 52px;
  --bottombar-h: 56px;
  --primary: #2563eb;
  --primary-hover: #1d4ed8;
  --accent: #667eea;
  --bg: #ffffff;
  --bg-soft: #f8fafc;
  --fg: #0f172a;
  --muted: #64748b;
  --border: #e2e8f0;
  --sidebar-bg: linear-gradient(180deg, #1e293b 0%, #0f172a 100%);
  --sidebar-fg: rgba(255, 255, 255, 0.7);
  --sidebar-fg-active: #ffffff;
  --shadow: 0 2px 12px rgba(15, 23, 42, 0.08);
}

* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; overflow: hidden; }
body {
  font-family: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "WenQuanYi Micro Hei", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: calc(15px * var(--zoom));
  line-height: 1.75;
  color: var(--fg);
  background: var(--bg-soft);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

.sidebar-rail {
  position: fixed; left: 0; top: 0; bottom: 0;
  width: var(--sidebar-w);
  background: var(--sidebar-bg);
  display: flex; flex-direction: column;
  padding: 12px 0; gap: 4px;
  overflow-y: auto;
  z-index: 100;
  box-shadow: 2px 0 8px rgba(0, 0, 0, 0.15);
  transition: transform 0.25s ease, opacity 0.2s ease;
}
.sidebar-rail.collapsed {
  transform: translateX(calc(-1 * var(--sidebar-w)));
  opacity: 0;
  pointer-events: none;
}
.nav-item {
  display: flex; align-items: center; justify-content: center;
  padding: 6px 0; margin: 0 6px;
  color: var(--sidebar-fg);
  text-decoration: none;
  font-size: calc(11px * var(--zoom));
  font-weight: 600;
  border-radius: 6px;
  transition: all 0.15s;
  letter-spacing: 0.3px;
  flex-shrink: 0;
}
.nav-item:hover {
  background: rgba(255, 255, 255, 0.1);
  color: var(--sidebar-fg-active);
  transform: scale(1.05);
}
.nav-item.active {
  background: linear-gradient(135deg, var(--accent) 0%, #764ba2 100%);
  color: var(--sidebar-fg-active);
  box-shadow: 0 2px 8px rgba(102, 126, 234, 0.4);
}

.main {
  margin-left: var(--sidebar-w);
  height: 100vh;
  display: flex; flex-direction: column;
  transition: margin-left 0.25s ease;
}
body:has(.sidebar-rail.collapsed) .main { margin-left: 0; }

.topbar {
  height: var(--topbar-h);
  display: flex; align-items: center;
  padding: 0 16px; gap: 8px;
  background: var(--bg);
  border-bottom: 1px solid var(--border);
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
  z-index: 50;
  flex-shrink: 0;
}
.topbar-toggle { font-size: 18px; width: 36px; height: 36px; }
.topbar-toggle.active { background: var(--primary); color: #fff; }
.topbar-btn {
  background: var(--bg-soft);
  border: 1px solid var(--border);
  color: var(--fg);
  font-size: 20px;
  font-weight: 600;
  width: 36px; height: 36px;
  border-radius: 8px;
  cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  transition: all 0.15s;
  font-family: inherit;
  line-height: 1;
}
.topbar-btn:hover:not(:disabled) { background: var(--border); border-color: var(--muted); }
.topbar-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.topbar-title {
  font-size: calc(14px * var(--zoom));
  font-weight: 600;
  color: var(--fg);
  min-width: 64px; text-align: center;
  user-select: none;
}
.page-sep { color: var(--muted); margin: 0 4px; }
.topbar-spacer { flex: 1; }
.topbar-tools { display: flex; gap: 4px; }
.topbar-btn-icon {
  background: var(--bg-soft);
  border: 1px solid var(--border);
  color: var(--fg);
  font-size: calc(12px * var(--zoom));
  font-weight: 600;
  height: 36px;
  padding: 0 10px;
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.15s;
  font-family: inherit;
}
.topbar-btn-icon:hover { background: var(--border); }

.stage {
  flex: 1; overflow-y: auto;
  background: var(--bg-soft);
  padding: 24px 16px 16px;
  display: flex; align-items: flex-start;
  justify-content: center;
}
.question-page {
  width: 100%;
  max-width: 920px;
  background: var(--bg);
  border-radius: 12px;
  padding: 36px 48px;
  box-shadow: var(--shadow);
  border: 1px solid var(--border);
}
.question-page[hidden] { display: none; }
.question-card { display: block; }

.question-header {
  display: flex; justify-content: space-between; align-items: flex-start;
  gap: 16px; flex-wrap: wrap;
  padding-bottom: 16px;
  border-bottom: 2px solid var(--primary);
  margin-bottom: 20px;
}
.question-title {
  display: flex; align-items: center; gap: 10px;
  flex-wrap: wrap;
}
.question-num {
  font-size: calc(22px * var(--zoom));
  font-weight: 800;
  color: var(--primary);
  font-family: "SF Pro Display", -apple-system, sans-serif;
  letter-spacing: -0.5px;
}
.question-source {
  font-size: calc(12px * var(--zoom));
  color: var(--muted);
  font-weight: 400;
}
.question-meta { font-size: calc(12px * var(--zoom)); color: var(--muted); }
.page-tag {
  background: var(--bg-soft);
  padding: 4px 10px;
  border-radius: 12px;
  border: 1px solid var(--border);
}

.tag {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 12px;
  font-size: calc(11px * var(--zoom));
  font-weight: 600;
  color: #fff;
  letter-spacing: 0.5px;
  vertical-align: middle;
}
.tag-single { background: #2563eb; }
.tag-multi  { background: #7c3aed; }
.tag-calc   { background: #16a34a; }
.tag-fill   { background: #ea580c; }
.tag-exp    { background: #0891b2; }
.tag-warn   { background: #94a3b8; }

.question-body { font-size: calc(15px * var(--zoom)); line-height: 1.85; color: var(--fg); }
.question-body p { margin: 10px 0; }
.question-body strong { color: var(--primary); font-weight: 600; }
.question-body em { color: #dc2626; font-style: normal; font-weight: 500; }
.question-body img { max-width: 100%; height: auto; border-radius: 8px; margin: 8px 0; }

.question-body ol.options {
  list-style: none;
  padding: 0;
  margin: 12px 0 12px 4px;
  counter-reset: opt-counter;
}
.question-body ol.options > li {
  padding: 8px 12px 8px 36px;
  margin: 4px 0;
  position: relative;
  line-height: 1.7;
  border-radius: 6px;
  background: var(--bg-soft);
  border: 1px solid transparent;
  transition: all 0.15s;
}
.question-body ol.options > li:hover {
  border-color: var(--primary);
  background: #eff6ff;
}
.question-body ol.options > li::before {
  counter-increment: opt-counter;
  content: counter(opt-counter, upper-alpha) ".";
  position: absolute; left: 10px; top: 8px;
  font-weight: 700; color: var(--primary);
  font-family: "SF Pro Display", -apple-system, sans-serif;
  font-size: calc(14px * var(--zoom));
}

.question-body h1, .question-body h2, .question-body h3 {
  margin: 16px 0 8px;
  font-weight: 600;
  color: var(--fg);
}
.question-body h3 { font-size: calc(16px * var(--zoom)); }
.question-body table { width: 100%; border-collapse: collapse; margin: 12px 0; font-size: calc(13.5px * var(--zoom)); }
.question-body th, .question-body td { padding: 8px 12px; border: 1px solid var(--border); text-align: left; }
.question-body th { background: var(--bg-soft); font-weight: 600; }
.question-body code {
  background: var(--bg-soft); padding: 1px 6px; border-radius: 4px;
  font-family: "SF Mono", Consolas, monospace; font-size: calc(13px * var(--zoom));
  color: #be185d;
}

.question-figures {
  margin-top: 16px;
  display: flex; flex-direction: column; align-items: center; gap: 12px;
}
.question-figures img {
  max-width: 100%;
  max-height: 480px;
  height: auto;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: white;
}

.bottombar {
  height: var(--bottombar-h);
  display: flex; align-items: center;
  padding: 0 16px; gap: 12px;
  background: var(--bg);
  border-top: 1px solid var(--border);
  box-shadow: 0 -1px 3px rgba(0, 0, 0, 0.04);
  flex-shrink: 0;
}
.bottombar-btn {
  background: var(--primary);
  border: none;
  color: #fff;
  font-size: calc(13px * var(--zoom));
  font-weight: 500;
  padding: 8px 16px;
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.15s;
  font-family: inherit;
  white-space: nowrap;
}
.bottombar-btn:hover:not(:disabled) { background: var(--primary-hover); }
.bottombar-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.progress {
  flex: 1; height: 6px;
  background: var(--bg-soft);
  border-radius: 3px;
  overflow: hidden;
  border: 1px solid var(--border);
}
.progress-bar {
  height: 100%;
  background: linear-gradient(90deg, var(--primary) 0%, #7c3aed 100%);
  width: 0;
  transition: width 0.3s ease;
  border-radius: 3px;
}

.katex { font-size: 1.05em; }
.katex-display { margin: 12px 0 !important; }

@media print {
  .sidebar-rail, .topbar, .bottombar { display: none !important; }
  .main { margin-left: 0 !important; }
  .stage { padding: 0; background: white; overflow: visible; }
  .question-page {
    display: block !important;
    max-width: 100%;
    page-break-after: always;
    box-shadow: none;
    border: none;
    border-radius: 0;
    padding: 24px;
  }
  .question-page:last-child { page-break-after: auto; }
  .question-page[hidden] { display: none !important; }
  body { overflow: visible; height: auto; }
  html, body { height: auto; }
}

@media (max-width: 768px) {
  :root { --sidebar-w: 44px; }
  .question-page { padding: 20px 18px; }
  .question-num { font-size: calc(20px * var(--zoom)); }
  .topbar-title { font-size: calc(13px * var(--zoom)); min-width: 50px; }
  .bottombar-btn { padding: 6px 10px; font-size: calc(12px * var(--zoom)); }
}
"""


def _jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _load_katex_assets() -> str:
    """加载 KaTeX JS + auto-render JS, inline 到 HTML."""
    try:
        from topic_garden.ingest.katex import _assets
        katex_js, _, auto_render_js = _assets()
    except Exception as e:  # noqa: BLE001
        log.warning("[exam_renderer] KaTeX 资源加载失败: %s", e)
        return ""

    init_script = """
<script>
document.addEventListener("DOMContentLoaded", function () {
  if (typeof renderMathInElement === "function") {
    renderMathInElement(document.body, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "$",  right: "$",  display: false }
      ],
      throwOnError: false
    });
  }
});
</script>"""
    return (
        f"<script>\n{katex_js}\n</script>\n"
        f"<script>\n{auto_render_js}\n</script>\n"
        f"{init_script}\n"
    )


def render_exam_html(
    compose_result: Dict[str, Any],
    title: Optional[str] = None,
) -> str:
    """把 TopicComposer.compose() 的结果包成单文件讲评 HTML."""
    topic = compose_result.get("topic") or {}
    total_questions = (
        compose_result.get("total_questions")
        or topic.get("total_questions")
        or 0
    )
    questions_by_k = (
        compose_result.get("questions_by_k")
        or topic.get("questions_by_k")
        or {}
    )

    final_title = title or topic.get("title") or "试卷讲评"

    env = _jinja_env()
    template = env.get_template("exam.html")
    body = template.render(
        topic=topic,
        questions_by_k=questions_by_k,
        total_questions=total_questions,
        questions=compose_result.get("questions") or {},
        stats=compose_result.get("stats") or {},
    )

    katex_block = _load_katex_assets()

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>{final_title} — 试卷讲评</title>
  <style>
{EXAM_CSS}
  </style>
</head>
<body>
{body}
{katex_block}
</body>
</html>
"""


__all__ = ["render_exam_html", "EXAM_CSS"]
