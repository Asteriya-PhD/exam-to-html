"""exam_to_html.backend._ocr_fix — 干净的 OCR Unicode → LaTeX 转换

为避免 exam_renderer.py 里的正则 raw-string 末尾反斜杠陷阱
(Python 3.14 re parser 把 \\X 当 bad escape), 这个模块单独实现.
"""
from __future__ import annotations

import re
import uuid
from typing import List

# Mathematical Italic / Greek / Digit 字符 → \mathit{<ascii>}
_MATH_ITALIC_MAP = {
    **{chr(c): chr(ord('a') + (c - 0x1D44E)) for c in range(0x1D44E, 0x1D44E + 26)},
    **{chr(c): chr(ord('A') + (c - 0x1D434)) for c in range(0x1D434, 0x1D434 + 26)},
    **{chr(c): chr(0x03B1 + (c - 0x1D6FC)) for c in range(0x1D6FC, 0x1D6FC + 18)},
    **{chr(c): chr(0x0391 + (c - 0x1D6A8)) for c in range(0x1D6A8, 0x1D6A8 + 18)},
    **{chr(c): str(c - 0x1D7D8) for c in range(0x1D7D8, 0x1D7D8 + 10)},
}

# OCR 上下标映射 — 仅数学专用字符
_OCR_SUB_MAP = {'!': '0', '#': '1', '$': '2', '%': '3', '&': '4', "'": '5', '*': '8', '+': '9'}


def _ocr_unicode_to_latex(text: str) -> str:
    r"""OCR 出的 Unicode 数学符号 → LaTeX (math mode ready).

    转换:
    - Mathematical Italic / Greek / Digit → \mathit{<ascii>}
    - OCR 上/下标符号 → _{N} 或 ^{N} (字母后下标, 数字后上标)
    - 删孤立 $\mathit{X}$ 后跟中文标点的 $ (避免 KaTeX 报 Unicode text in math mode)

    严守边界: 跳过已在 $...$ 内的内容 (避免破坏用户已写的 LaTeX).
    """
    # 保护已有 $...$ 块
    placeholder_prefix = f"KOCR_{uuid.uuid4().hex[:8]}_"
    placeholders: List[str] = []

    def _protect(m: "re.Match[str]") -> str:
        placeholders.append(m.group(0))
        idx = len(placeholders) - 1
        return f"{placeholder_prefix}{idx}__END"

    out = re.sub(r"\$[^$\n]+?\$", _protect, text)

    BS = chr(92)  # \ 单字符 (避免 raw string 末尾 \ 陷阱)
    DL = chr(36)  # $

    # 1) Mathematical Italic / Greek / Digit → \mathit{<ascii>}
    def _replace_math_italic(m: "re.Match[str]") -> str:
        c = m.group(0)
        return BS + "mathit{" + _MATH_ITALIC_MAP[c] + "}"
    pattern = "[" + "".join(_MATH_ITALIC_MAP.keys()) + "]"
    out = re.sub(pattern, _replace_math_italic, out)

    # 1b) 删孤立 \mathit{X}$ 后跟中文标点/汉字 的 $ (避免 KaTeX Unicode text in math mode)
    # 模式 1: \mathit{X}$[CJK 标点]
    # 注: re 里 \m 是 bad escape, 必须用 \\m (双反斜杠) 让 re 解析成字面 \m.
    # BS = chr(92) 是单反斜杠. BS*2 = chr(92)*2 是双反斜杠 (字面 \\).
    BS2 = chr(92) * 2  # 字面 "\\"
    out = re.sub(
        BS2 + "m" + "a" + "t" + "h" + "i" + "l" + r"\{([a-zA-Z])\}" + DL + r"([、，。；：！？])",
        BS2 + "m" + "a" + "t" + "h" + "i" + "l" + r"{\1}\2",
        out,
    )
    # 模式 2: \mathit{X}$ 后直接跟汉字
    out = re.sub(
        BS2 + "m" + "a" + "t" + "h" + "i" + "l" + r"\{([a-zA-Z])\}" + DL + r"(?=[一-鿿])",
        BS2 + "m" + "a" + "t" + "h" + "i" + "l" + r"{\1}",
        out,
    )

    # 2) OCR 上下标转换
    # 模式 a: \mathit{X} 后跟 1-2 个标点 → 下标 (m_0)
    out = re.sub(
        BS2 + "m" + "a" + "t" + "h" + "i" + "l" + r"\{([a-zA-Z])\}([!\#$%&'()*+,./\-]{1,3})",
        lambda m: BS2 + "m" + "a" + "t" + "h" + "i" + "l" + "{" + m.group(1) + "}_" + _OCR_SUB_MAP.get(m.group(2)[0], m.group(2)[0]),
        out,
    )
    # 模式 b: 数字 + 1 个标点 → 上标 (10³ → 10^3). 仅当第一个标点在 _OCR_SUB_MAP
    out = re.sub(
        r"(\d)([!\#$%&'()*+]{1,2})",
        lambda m: (
            m.group(1) + "^" + _OCR_SUB_MAP[m.group(2)[0]]
            if m.group(2)[0] in _OCR_SUB_MAP
            else m.group(0)
        ),
        out,
    )

    # 3) 跨行分子分母
    def _fraction_across_lines(m: "re.Match[str]") -> str:
        inner1 = m.group(1)[len(BS2 + "m" + "a" + "t" + "h" + "i" + "l" + "{"):-1]
        inner2 = m.group(2)[len(BS2 + "m" + "a" + "t" + "h" + "i" + "l" + "{"):-1]
        return BS2 + f"frac{{{inner1}}}{{{inner2}}}"
    out = re.sub(
        BS2 + "m" + "a" + "t" + "h" + "i" + "l" + r"\{([a-zA-Z])\}([\n ]+)"
        + BS2 + "m" + "a" + "t" + "h" + "i" + "l" + r"\{([a-zA-Z])\})",
        _fraction_across_lines,
        out,
    )

    # 还原 $...$ 占位符
    placeholder_re = re.escape(placeholder_prefix) + r"(\d+)__END"

    def _restore(m: "re.Match[str]") -> str:
        idx = int(m.group(1))
        return placeholders[idx]

    out = re.sub(placeholder_re, _restore, out)
    return out