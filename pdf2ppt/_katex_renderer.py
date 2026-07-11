"""KaTeX 渲染工具 — 支持纯公式和含 $...$ 的混合文本

两种模式：
1. render_formulas(): 纯 LaTeX 公式 → PNG（无文本上下文）
2. render_text_blocks(): 混合文本（含 $...$ 公式）→ PNG，用 KaTeX auto-render
"""

import os
import re
from pathlib import Path
from typing import List, Optional

KATEX_DIR = Path(__file__).parent / "katex"


def _read_assets() -> tuple:
    """读 vendored KaTeX 资源.

    修 L-8: 用 utf-8-sig 自动剥 BOM, 避免 KaTeX JS 含 BOM 字符时浏览器拒绝解析
    (极低概率, 但零成本修复)。
    """
    js_path = KATEX_DIR / "katex.min.js"
    css_path = KATEX_DIR / "katex.min.css"
    auto_render_path = KATEX_DIR / "auto-render.min.js"
    js = js_path.read_text(encoding="utf-8-sig")
    css = css_path.read_text(encoding="utf-8-sig")
    auto_render_js = auto_render_path.read_text(encoding="utf-8-sig")
    # 内联字体
    import base64
    fonts_dir = KATEX_DIR / "fonts"
    # 修 #d: 扩展内联字体列表 — 老版本只 inline 2 个, KaTeX_Main-Italic 等仍走
    # 相对路径 fonts/, 双击 HTML 时找不到. 现 inline 所有 KaTeX_*.woff2 字体.
    for fname in [
        "KaTeX_AMS-Regular.woff2",
        "KaTeX_Main-Regular.woff2",
        "KaTeX_Main-Bold.woff2",
        "KaTeX_Main-Italic.woff2",
        "KaTeX_Main-BoldItalic.woff2",
        "KaTeX_Math-Italic.woff2",
        "KaTeX_Size1-Regular.woff2",
        "KaTeX_Size2-Regular.woff2",
        "KaTeX_Size3-Regular.woff2",
        "KaTeX_Size4-Regular.woff2",
    ]:
        fpath = fonts_dir / fname
        if fpath.exists():
            b64 = base64.b64encode(fpath.read_bytes()).decode()
            css = css.replace(f'url(fonts/{fname})', f'url(data:font/woff2;base64,{b64})')
            # 同时替换 .woff 和 .ttf 引用 (CSS 链有 fallback)
            css = css.replace(f'url(fonts/{fname.replace(".woff2", ".woff")})', f'url(data:font/woff2;base64,{b64})')
            css = css.replace(f'url(fonts/{fname.replace(".woff2", ".ttf")})', f'url(data:font/ttf;base64,{b64})')
    return js, css, auto_render_js


def render_text_blocks(blocks: List[dict], scale: float = 2.0,
                       font_size: str = "18px") -> List[Optional[bytes]]:
    """渲染含 $...$ 公式的混合文本为 PNG

    Args:
        blocks: [{text: str, id: str}, ...]
            text 中可以用 $...$ 包裹 LaTeX 公式
        scale: 截图倍率
        font_size: 正文字号

    Returns:
        [PNG bytes or None, ...]
    """
    if not blocks:
        return []

    katex_js, katex_css, auto_render_js = _read_assets()

    items = []
    for i, b in enumerate(blocks):
        text = b.get("text", "").strip()
        if not text:
            continue
        # KaTeX 中 $...$ 需要转义，但 auto-render 会自动处理
        # 把文本中的 $ 保持原样（auto-render 识别 $...$ 为数学模式）
        # 但需要确保 < > & 等 HTML 特殊字符被转义
        text = (text
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))
        items.append(
            f'<div class="text-block" data-id="{b.get("id", i)}" '
            f'style="font-size:{font_size};line-height:1.6;">{text}</div>'
        )

    if not items:
        return [None] * len(blocks)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    background: white;
    padding: 8px 12px;
    font-family: 'Microsoft YaHei', 'PingFang SC', 'KaTeX_Main', sans-serif;
  }}
  {katex_css}
  .text-block {{ max-width: 680px; }}
</style>
</head>
<body>
{''.join(items)}
<script>
{katex_js}
{auto_render_js}
(function() {{
  document.querySelectorAll('.text-block').forEach(function(el) {{
    try {{
      renderMathInElement(el, {{
        delimiters: [
          {{left: '$$', right: '$$', display: true}},
          {{left: '$', right: '$', display: false}}
        ],
        throwOnError: false
      }});
    }} catch(e) {{
      console.error(e);
    }}
  }});
}})();
</script>
</body>
</html>"""

    from playwright.sync_api import sync_playwright
    results = [None] * len(blocks)

    # 若标准 chromium-1223 没装 (cdn.playwright.dev 不通),允许用其他来源的 chromium
    # 通过 PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH 指定 (e.g. puppeteer 装的)
    _chromium_exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    _launch_kwargs = {"headless": True}
    if _chromium_exe and os.path.exists(_chromium_exe):
        _launch_kwargs["executable_path"] = _chromium_exe

    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs)
        try:
            page = browser.new_page(
                viewport={"width": 1920, "height": 1080},
                device_scale_factor=scale,
            )
            page.set_content(html, wait_until="networkidle")
            try:
                page.wait_for_function(
                    "typeof renderMathInElement !== 'undefined'",
                    timeout=10000,
                )
                page.wait_for_timeout(500)
            except Exception:
                pass

            for i, b in enumerate(blocks):
                bid = b.get("id", i)
                try:
                    selector = f'[data-id="{bid}"]'
                    el = page.query_selector(selector)
                    if el:
                        buf = el.screenshot(type="png")
                        if buf and len(buf) > 200:
                            results[i] = buf
                except Exception as e:
                    print(f"    ⚠ 文本截图失败 [{i}]: {e}")
        finally:
            # 修 M-13: page.set_content 超时 / 异常路径也保证 browser 关闭
            browser.close()

    return results


def render_formulas(formulas: List[dict], scale: float = 2.0) -> List[Optional[bytes]]:
    """渲染纯 LaTeX 公式为 PNG

    安全 (H-8): 修 XSS — 老代码直接 {latex} 拼进 HTML div, PDF 嵌入恶意
    LaTeX (如 `\\text{<script>alert(1)</script>}`) 会被解析 → 双击 HTML 触发。
    现在按 render_text_blocks 的标准做 & < > 转义 (LaTeX 里这些符号本就用
    \\& / \\lt / \\gt 表达, 真实公式不会含裸字符)。
    """
    katex_js, katex_css, _ = _read_assets()

    items = []
    for i, f in enumerate(formulas):
        latex = f.get("latex", "").strip()
        if not latex:
            continue
        is_display = f.get("display", False)
        cls = "formula-display" if is_display else "formula-inline"
        # 与 render_text_blocks 一致: & < > 转义, 阻断 <script> XSS
        latex_esc = (
            latex
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        items.append(f'<div class="{cls}" data-id="{i}">{latex_esc}</div>')

    if not items:
        return [None] * len(formulas)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background: transparent; padding: 0; }}
  {katex_css}
  .formula-inline {{ display:inline-block; padding:2px 4px; }}
  .formula-display {{ text-align:center; padding:8px 16px; max-width:680px; }}
</style>
</head>
<body>
{''.join(items)}
<script>
{katex_js}
(function() {{
  document.querySelectorAll('.formula-inline, .formula-display').forEach(function(el) {{
    try {{
      var display = el.classList.contains('formula-display');
      katex.render(el.textContent, el, {{ throwOnError: false, displayMode: display }});
    }} catch(e) {{
      el.textContent = '[' + el.textContent.substring(0, 40) + '...]';
    }}
  }});
}})();
</script>
</body>
</html>"""

    from playwright.sync_api import sync_playwright
    results = [None] * len(formulas)

    # 同上:允许用 PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH 走 puppeteer/其他 chromium
    _chromium_exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    _launch_kwargs = {"headless": True}
    if _chromium_exe and os.path.exists(_chromium_exe):
        _launch_kwargs["executable_path"] = _chromium_exe

    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs)
        try:
            page = browser.new_page(
                viewport={"width": 1920, "height": 1080},
                device_scale_factor=scale,
            )
            page.set_content(html, wait_until="networkidle")
            try:
                page.wait_for_function("typeof katex !== 'undefined'", timeout=10000)
                page.wait_for_timeout(300)
            except Exception:
                pass

            for i in range(len(formulas)):
                try:
                    selector = f'[data-id="{i}"]'
                    rendered = page.query_selector(f'{selector} .katex') or page.query_selector(selector)
                    if rendered:
                        buf = rendered.screenshot(type="png")
                        if buf and len(buf) > 200:
                            results[i] = buf
                except Exception as e:
                    print(f"    ⚠ 公式截图失败 [{i}]: {e}")
        finally:
            # 修 M-13: 异常路径也保证 browser 关闭
            browser.close()
    return results
