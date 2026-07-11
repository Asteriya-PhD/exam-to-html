"""
exam_to_html.backend.exam_renderer — 试卷讲评 HTML 渲染

- 复用 topic_garden.TopicComposer 拿题数据 + md→html (含选项/图/KaTeX 预处理)
- 用我们自己的 exam.html 模板 (单页式 + 侧边导航) 渲染 body
- 包成单文件 HTML (含 KaTeX + CSS + JS)
- 与 topic_garden 内置的 microtopic.html 模板互不影响
"""
from __future__ import annotations

import logging
import re
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
  margin: 8px 0;
  counter-reset: opt-counter;
}
.question-body ol.options > li {
  padding: 4px 0 4px 28px;
  margin: 0;
  position: relative;
  line-height: 1.7;
}
.question-body ol.options > li::before {
  counter-increment: opt-counter;
  content: counter(opt-counter, upper-alpha) ".";
  position: absolute; left: 0; top: 4px;
  font-weight: 700; color: var(--primary);
  font-family: "SF Pro Display", -apple-system, sans-serif;
  font-size: calc(14px * var(--zoom));
  min-width: 22px;
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
    """加载 KaTeX JS + CSS + auto-render JS, inline 到 HTML."""
    try:
        from topic_garden.ingest.katex import _assets
        katex_js, katex_css, auto_render_js = _assets()
    except Exception as e:  # noqa: BLE001
        log.warning("[exam_renderer] KaTeX 资源加载失败: %s", e)
        return ""

    # KaTeX CSS 必须 inline, 否则公式符号渲染不出来 (裸文本形式)
    css_block = f"<style>\n{katex_css}\n</style>"

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
        f"{css_block}\n"
        f"<script>\n{katex_js}\n</script>\n"
        f"<script>\n{auto_render_js}\n</script>\n"
        f"{init_script}\n"
    )


# ============================================================
# LaTeX 预处理 — 主动把裸 LaTeX 命令包成 $..$ 让 KaTeX 渲染
# md_to_html.py 只处理 \frac{}{}, 其它命令 (\theta \alpha \sin \cos \sqrt 等)
# 大量漏网, 在 renderer 层补这一刀, 不动 topic_garden 的代码
# ============================================================
_LATEX_CMDS = (
    r"theta|alpha|beta|gamma|delta|epsilon|zeta|eta|iota|kappa|lambda|mu|nu|xi|pi|"
    r"rho|sigma|tau|upsilon|phi|chi|psi|omega|"
    r"Theta|Alpha|Beta|Gamma|Delta|Epsilon|Zeta|Eta|Iota|Kappa|Lambda|Mu|Nu|Xi|"
    r"Pi|Rho|Sigma|Tau|Upsilon|Phi|Chi|Psi|Omega|"
    r"sin|cos|tan|cot|sec|csc|arcsin|arccos|arctan|sinh|cosh|tanh|"
    r"sqrt|log|ln|exp|lim|sum|prod|int|infty|cdot|times|div|pm|mp|le|ge|neq|"
    r"approx|equiv|sim|cong|to|rightarrow|leftarrow|leftrightarrow|"
    r"Rightarrow|Leftarrow|hbar|ell|nabla|partial|Re|Im|"
    r"forall|exists|in|notin|subset|supset|cup|cap|emptyset|"
    r"mathbb|mathrm|mathit|mathbf|text|operatorname|boxed|"
    r"over|underline|vec|hat|tilde|bar|dot|ddot"
)
# 匹配 \cmd 或 \cmd{...} 或 \cmd{...}{...}
_LATEX_CMD_RE = re.compile(
    r"\\(?:" + _LATEX_CMDS + r")(?:\{[^{}]*\})?(?:\{[^{}]*\})?",
    re.IGNORECASE,
)


def _wrap_more_latex(html: str) -> str:
    """把裸 LaTeX 命令 (没在 $..$ 内的) 包成 $..$ 让 KaTeX 渲染.

    ⚠️ 必须在 KaTeX auto-render 跑之前调用, 但这里返回的是 HTML 字符串,
    KaTeX 在前端 DOMContentLoaded 时跑 — 所以这是后处理, 客户端拿到时
    KaTeX 会扫到新包的 $..$ 并渲染.

    修 L-7: 改 placeholder 为 UUID4 风格 (e.g. KXXX_7f3a9b2c), 碰撞概率
    极低 (\x00 + 单字母前缀 + NUL 模式只适合早期 demo)。
    """
    import uuid as _uuid
    placeholder_prefix = "KMATH_" + _uuid.uuid4().hex[:8] + "_"  # e.g. KMATH_7f3a9b2c_
    segments: list = []
    counter = {"i": 0}

    def _protect(m: "re.Match[str]") -> str:
        segments.append(m.group(0))
        idx = counter["i"]
        counter["i"] += 1
        return f"{placeholder_prefix}{idx}__END"  # 唯一 sentinel, 不会撞字面

    # 保护 $$...$$ (display)
    html2 = re.sub(r"\$\$[\s\S]+?\$\$", _protect, html)
    # 保护 $...$ (inline)
    html2 = re.sub(r"\$[^$\n]+?\$", _protect, html2)
    # 保护已经渲染的 <span class="katex"> 块 (防止反向破坏)
    html2 = re.sub(
        r'<span class="katex[^"]*">[\s\S]*?</span>',
        _protect,
        html2,
    )

    # 在剩余文本里包 $..$
    def _wrap(m: "re.Match[str]") -> str:
        return "$" + m.group(0) + "$"

    # 但要避免重复包 — 用占位符, 处理后再还原
    html2 = _LATEX_CMD_RE.sub(_wrap, html2)

    # 还原 $...$ 占位符 (用动态构造的 regex, 匹配本次 prefix)
    placeholder_re = re.escape(placeholder_prefix) + r"(\d+)__END"
    def _restore(m: "re.Match[str]") -> str:
        idx = int(m.group(1))
        return segments[idx]

    html2 = re.sub(placeholder_re, _restore, html2)
    return html2


# ============================================================
# 多空格归一化 — md 里 ` ` 多个连续空格在 HTML 渲染时折叠成 1 个
# 同时清掉题首题尾的多余空白
# ============================================================
_MULTI_WS_RE = re.compile(r"[ \t]{2,}")


def _normalize_whitespace(html: str) -> str:
    # 在 <pre>/<code>/<table> 块内不处理 (保留格式)
    parts = re.split(r"(<(?:pre|code|table)[\s\S]*?</(?:pre|code|table)>)", html)
    out = []
    for p in parts:
        if p.startswith("<pre") or p.startswith("<code") or p.startswith("<table"):
            out.append(p)
        else:
            # 多空格 / 多 tab 折成 1 个
            p = _MULTI_WS_RE.sub(" ", p)
            # 行尾空格去掉
            p = re.sub(r"[ \t]+\n", "\n", p)
            out.append(p)
    return "".join(out)


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

    # 优先用 questions_by_k (老 v0.18 API), 没有则用 topic_questions_list (新 v0.19+ API)
    questions_by_k = (
        compose_result.get("questions_by_k")
        or topic.get("questions_by_k")
        or {}
    )
    if not questions_by_k:
        # v0.19+: topic_questions_list 是一维列表 (不再分 K 桶)
        flat = topic.get("topic_questions_list") or []
        if flat:
            questions_by_k = {"_all": flat}

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

    # 后处理: 把更多 LaTeX 命令包成 $..$ 让 KaTeX 渲染 (md_to_html.py 只处理 \frac)
    body = _wrap_more_latex(body)
    # 归一化多空格
    body = _normalize_whitespace(body)

    katex_block = _load_katex_assets()

    # 安全 (H-9): final_title = pdf_path.stem 直接 f-string 拼入 <title>。
    # PDF 文件名含 `<script>` 等会被原样输出, 双击 HTML 触发 XSS。
    # f-string 不走 Jinja2 autoescape, 必须显式 HTML 转义。
    from html import escape as _html_escape
    safe_title = _html_escape(final_title)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>{safe_title} — 试卷讲评</title>
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
