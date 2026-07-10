"""
exam_to_html.backend._qnum_fallback — PDF2PPT qnum 解析失败时的兜底

背景
----
PDF2PPT v2 parser (`_v2_parser.py:1133`) 和 qnum rule (`_qnum_rule.py:58`)
的题号正则只匹配 `数字 + [.．、` + 空白]`, 漏掉:
  - `（1）` / `(1)`
  - `①`–`⑳` (圈码)
  - `第1题`
  - `T1.` / `Q1.` / `题1.`

PDF2PPT 是独立仓, 用户不希望改它; 本模块在 exam-to-html 这层做兜底。

策略
----
1. PDF2PPT 解析成功 → 本模块不介入 (零行为变更)。
2. PDF2PPT 解析返回 0 题 → 用更宽松的正则从 PDF 原文里抽 qnum,
   把每段题组装成 QuestionDraft, 直接走 db.add_question_with_dedupe 入库。
3. PyMuPDF / MinerU 都不在当前 venv → 静默返回 [] (现状: NoQuestionsError)。

兜底的代价
--------
PyMuPDF 全文抽 + 正则匹配 ~50ms, 无 API 调用。只有 PDF2PPT 失败的 PDF
才会走兜底, 频率极低。
"""
from __future__ import annotations

import logging
import re
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)


# ============================================================
# 宽松题号正则 (支持 5 类格式)
# ============================================================
# 形如 (1) (1) (1) ① 第1题 T1. Q1. 题1. 1. 1. 1、
# 关键防护: 末尾 `(?!\d)` 负向先行禁止紧跟数字 (避免 "0.05" / "1.14" 误识别);
# `\d{1,3}` 限 1-999 防长串实验数据; 中文圈码 ①-⑳ 通过 ord() 映射。
_LENIENT_QNUM_RE = re.compile(
    r"^\s*"
    r"(?:"
    r"[（(](\d{1,3})[）)]"               # （1）/ (1)
    r"|([①-⑳])"                          # 圈码 ①–⑳
    r"|第\s*(\d{1,3})\s*题"               # 第1题
    r"|(?:[TQ]|题)\s*(\d{1,3})[\.．、]"   # T1. / Q1. / 题1.
    r"|(\d{1,3})[\.．、]"                # 1. / 1． / 1、 (原 PDF2PPT 形式)
    r")"
    r"(?!\d)"                             # 防 "1.14" / "0.05" — 题号后不能再是数字
)

# 卷头/说明页常见的提示语 — page 0 上匹配上则当说明处理, 不当题号
_INSTRUCTION_HINTS = (
    "注意事项",
    "考生注意",
    "考试说明",
    "本试卷",
    "满分",
    "考试时间",
    "请将答案",
    "考试形式",
)


def _match_qnum(line: str) -> Optional[int]:
    """单行宽松题号匹配, 返回题号 1-999, 否则 None.

    防止假阳:
      - 数字后紧跟数字 ("0.05", "1.14") — `(?!\\d)` 负向先行守住
      - 行内 mid-num ("题 1.2 步骤") — `^\\s*` + 整行 anchor
      - num > 50 — 多为误识, 试卷很少一卷 50+ 题
    """
    m = _LENIENT_QNUM_RE.match(line)
    if not m:
        return None
    # 6 个捕获组分别对应 5 种格式 + 1 个 (circle 自身)
    paren, circled, di_ti, tq, plain = m.groups()
    if paren is not None:
        num = int(paren)
    elif circled is not None:
        num = ord(circled) - ord("①") + 1
    elif di_ti is not None:
        num = int(di_ti)
    elif tq is not None:
        num = int(tq)
    elif plain is not None:
        num = int(plain)
    else:
        return None
    if num < 1 or num > 50:
        return None
    return num


def extract_qnums_from_text(text: str) -> List[Tuple[int, int]]:
    """从纯文本抽 (行号, 题号) 列表, 去重按行号顺序.

    Args:
        text: PDF 全文 (按页 \f 或 \n 分隔均可)

    Returns:
        [(line_idx, qnum), ...], line_idx 是 text.splitlines() 索引
    """
    out: List[Tuple[int, int]] = []
    seen_lines: set[int] = set()
    for i, line in enumerate(text.splitlines()):
        # 卷头/说明页 (page 0 顶部) — 跳过明显的"非题号"行
        stripped = line.strip()
        if not stripped:
            continue
        if any(hint in stripped for hint in _INSTRUCTION_HINTS):
            # 仅在该行长度 < 80 时跳过 (避免误伤题中提到"注意事项"的内容)
            if len(stripped) < 80:
                continue
        num = _match_qnum(line)
        if num is None:
            continue
        if i in seen_lines:
            continue
        seen_lines.add(i)
        out.append((i, num))
    return out


# ============================================================
# PyMuPDF 全文抽取 → 题段切分
# =========================================================_MAX_QNUM = 50
_PAGE_FORM_FEED = "\f"


def _iter_pages_text(pdf_path: str) -> List[Tuple[int, str]]:
    """PyMuPDF 逐页抽纯文本, 返回 [(page_idx, text), ...].

    PyMuPDF 不在时返回 [] (兜底静默失败)。
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        log.warning("[qnum_fallback] PyMuPDF (fitz) 未安装, 跳过兜底")
        return []
    out: List[Tuple[int, str]] = []
    try:
        doc = fitz.open(pdf_path)
        try:
            for pn in range(len(doc)):
                page_text = doc[pn].get_text("text") or ""
                out.append((pn, page_text))
        finally:
            doc.close()
    except Exception as e:
        log.warning("[qnum_fallback] PyMuPDF 抽页失败: %s", e)
        return []
    return out


def _build_drafts_from_pages(pages: List[Tuple[int, str]]):
    """从 (page_idx, text) 列表切出题段, 生成 QuestionDraft.

    Lazy import QuestionDraft — 避免 exam-to-html 不需要 topic_garden.models 时
    也强制依赖 (虽然 exam-to-html 总是依赖 topic-garden, 但 lazy import 让本模块
    在测试里能独立跑)。
    """
    from topic_garden.models import QuestionDraft

    # 1) 收集所有 (global_line_idx, page_idx, qnum)
    matches: List[Tuple[int, int, int]] = []
    global_idx = 0
    for pn, text in pages:
        for line in text.splitlines():
            num = _match_qnum(line)
            if num is not None:
                matches.append((global_idx, pn, num))
            global_idx += 1
    if not matches:
        return []

    # 2) 切片: 每题的内容 = 上一个 qnum 行 + 1 到当前 qnum 行 (不含)
    all_lines: List[Tuple[int, str]] = []
    for pn, text in pages:
        for line in text.splitlines():
            all_lines.append((pn, line))

    drafts = []
    for i, (gline, pn, num) in enumerate(matches):
        # 题段 = [qnum 行, 下一题 qnum 行) — 包含 qnum 行本身 (题干起点)
        start = gline
        end = matches[i + 1][0] if i + 1 < len(matches) else len(all_lines)
        body_lines = [ln for (_pn, ln) in all_lines[start:end] if ln.strip()]
        content_md = "\n".join(body_lines).strip()
        if not content_md:
            continue
        drafts.append(QuestionDraft(
            content_md=content_md,
            has_figure=False,
            figure_paths=[],
            source_page=pn,
            source_qnum=str(num),
            q_type="fill_blank",  # 兜底没法判定 choice / 计算题
            is_multi_select=None,
            tag_slugs=[],
            notes=None,
            parsed_blocks=None,
        ))
    return drafts


def extract_drafts_with_lenient_qnum(pdf_path: str):
    """从 PDF 抽题段 (PyMuPDF + 宽松正则). 失败或无 qnum 时返回 [].

    Returns:
        List[QuestionDraft] 或 []
    """
    pages = _iter_pages_text(pdf_path)
    if not pages:
        return []
    drafts = _build_drafts_from_pages(pages)
    if drafts:
        log.info(
            "[qnum_fallback] PDF2PPT 0 题, 兜底从 PDF 原文抽出 %d 题段",
            len(drafts),
        )
    return drafts


__all__ = [
    "extract_drafts_with_lenient_qnum",
    "extract_qnums_from_text",
    "_match_qnum",
]