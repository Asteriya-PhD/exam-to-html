"""
exam_to_html.backend._qnum_fallback — 本地 pdf2ppt qnum 解析失败时的兜底

背景
----
本仓 vendored 的 pdf2ppt v2 parser (`pdf2ppt/_v2_parser.py:1133`) 和
qnum rule (`pdf2ppt/_qnum_rule.py:58`) 的题号正则只匹配
`数字 + [.．、` + 空白]`, 漏掉:
  - `（1）` / `(1)`
  - `①`–`⑳` (圈码)
  - `第1题`
  - `T1.` / `Q1.` / `题1.`

vendored parser 现在是本仓受控代码 (M5-2),但修改它会回归现有 PDF,
本模块仍在 exam-to-html 这层做兜底。

策略
----
1. 本地 pdf2ppt 解析成功 → 本模块不介入 (零行为变更)。
2. 本地 pdf2ppt 解析返回 0 题 → 用更宽松的正则从 PDF 原文里抽 qnum,
   把每段题组装成 QuestionDraft, q_type 复用 _post_process_md.detect_q_type
   (不再硬编码 fill_blank), 直接走 db.add_question_with_dedupe 入库。
3. PyMuPDF / MinerU 都不在当前 venv → 静默返回 [] (现状: NoQuestionsError)。

兜底的代价
--------
PyMuPDF 全文抽 + 正则匹配 ~50ms, 无 API 调用。只有本地 pdf2ppt 失败的 PDF
才会走兜底, 频率极低。
"""
from __future__ import annotations

import logging
import os
import re
from typing import List, Optional, Tuple

# 复用 _post_process_md 做 K2/K3 题型判定 (避免硬编码 fill_blank)
from ._post_process_md import detect_q_type

log = logging.getLogger(__name__)


# ============================================================
# 题号正则 — 顶级 vs 子问号 拆分
# ============================================================
# 顶级题号: 1. / 1． / 1、 / ① / 第N题 / T1. / Q1. / 题1.
#   判定: 单调递增 (n > last_top_qnum), 否则视作噪声丢弃
# 子问号: （1）/ (1) / （2）/ (2) ...
#   判定: 出现在某个顶级题号 "范围" 内, 不开新题, 仅附到当前顶级题题干末尾
#
# 关键防护 (沿用原版):
#   - 末尾 `(?!\d)` 负向先行禁止紧跟数字 (避免 "0.05" / "1.14" 误识别)
#   - `\d{1,3}` 限 1-999 防长串实验数据
#   - 中文圈码 ①-⑳ 通过 ord() 映射
_TOP_QNUM_RE = re.compile(
    r"^\s*"
    r"(?:"
    r"([①-⑳])"                          # 圈码 ①–⑳
    r"|第\s*(\d{1,3})\s*题"               # 第1题
    r"|(?:[TQ]|题)\s*(\d{1,3})[\.．、]"   # T1. / Q1. / 题1.
    r"|(\d{1,3})[\.．、]"                # 1. / 1． / 1、 (原 PDF2PPT 形式)
    r")"
    r"(?!\d{1,3}[\.．、])"                # 防小数点假阳 (1.14/0.05)
    r"(?!\d{1,3}\s?[a-zA-Z])"             # 防小数+单位假阳 (1.5m / 9.8m/s)
)

# 子问号正则 — 形如 （1）/(1) — 永远是某个顶级题内的子问
# 用全角/半角括号都接受 (实际 PDF 两种都常见)
_SUB_QNUM_RE = re.compile(
    r"^\s*[（(](\d{1,3})[）)]"
    r"(?!\d)"
)

# 兼容旧 API: 旧 _LENIENT_QNUM_RE 仍导出 (供测试 / 外部使用) — 但标记 DEPRECATED
_LENIENT_QNUM_RE = re.compile(
    r"^\s*"
    r"(?:"
    r"[（(](\d{1,3})[）)]"               # （1）/ (1)
    r"|([①-⑳])"                          # 圈码 ①–⑳
    r"|第\s*(\d{1,3})\s*题"               # 第1题
    r"|(?:[TQ]|题)\s*(\d{1,3})[\.．、]"   # T1. / Q1. / 题1.
    r"|(\d{1,3})[\.．、]"                # 1. / 1． / 1、 (原 PDF2PPT 形式)
    r")"
    r"(?!\d)"
)

# 纯数字顶级题号 (无圈码) — 用于圈码降级判定:
# "见过的数字顶级" 之后再出现 ①/②/③ 应降级为子编号
_DIGIT_TOP_QNUM_RE = re.compile(
    r"^\s*"
    r"(?:"
    r"第\s*(\d{1,3})\s*题"
    r"|(?:[TQ]|题)\s*(\d{1,3})[\.．、]"
    r"|(\d{1,3})[\.．、]"
    r")"
    r"(?!\d{1,3}[\.．、])"                # 同 _TOP_QNUM_RE — 防小数点
    r"(?!\d{1,3}\s?[a-zA-Z])"             # 防小数+单位
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
    """DEPRECATED: 兼容旧 API. 内部用 _match_top_qnum + _match_sub_qnum 替代.

    单行宽松题号匹配, 返回题号 1-999, 否则 None. **不区分顶级/子问** — 新代码不应再用.
    """
    m = _LENIENT_QNUM_RE.match(line)
    if not m:
        return None
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


def _match_top_qnum(line: str) -> Optional[int]:
    """单行顶级题号匹配. 排除 (N) / [N] 形式的子问号.

    返回题号 1-50 (num > 50 视为噪声). 匹配格式:
      - 圈码 ①-⑳
      - 第N题
      - T1./Q1./题1.
      - 1./1．/1、

    不匹配 (1) / （1） 这类子问 — 子问需用 _match_sub_qnum.
    """
    m = _TOP_QNUM_RE.match(line)
    if not m:
        return None
    circled, di_ti, tq, plain = m.groups()
    if circled is not None:
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


def _match_sub_qnum(line: str) -> Optional[int]:
    """单行子问号匹配. 形如 （1）/(1).  返回 1-50 的子问号."""
    m = _SUB_QNUM_RE.match(line)
    if not m:
        return None
    num = int(m.group(1))
    if num < 1 or num > 50:
        return None
    return num


def _match_digit_top_qnum(line: str) -> Optional[int]:
    """单行**纯数字**顶级题号匹配. 排除圈码 ①/②/③.

    用于状态机: 在 _build_drafts_from_pages 中, 一旦见过数字顶级, 后续
    圈码应降级为子编号 (因为 ①/②/③/④ 在中文物理题里常作为实验步骤 /
    子问编号, 而不是顶级题号).
    """
    m = _DIGIT_TOP_QNUM_RE.match(line)
    if not m:
        return None
    di_ti, tq, plain = m.groups()
    if di_ti is not None:
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
    """从纯文本抽 (行号, 顶级题号) 列表, 去重按行号顺序.

    **只返回顶级题号** — 子问号 (1)/(2)/(3) 不算顶级, 不出现在返回中.

    顶级题号的唯一判定依据是 `_TOP_QNUM_RE` 匹配 + `1 <= num <= 50`,
    不再做单调性校验 — 同一卷里 `① / 第1题 / T1. / 1.` 都是题号 1 是合法的
    (题号格式重置但指向"题 1"); 而 `0.05 / 1.14` 这类假阳已被 `_TOP_QNUM_RE`
    的 `(?!\\d)` 负向先行 + `num >= 1` 守住。

    圈码 (M5-4) 注意: 本函数不区分圈码与数字顶级 — 调用方
    `_build_drafts_from_pages` 用状态机做圈码降级.

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
        # 子问号 (1)/(2)/(3) 不参与顶级题号流 — 后面切片时会附到当前顶级题题干
        if _match_sub_qnum(line) is not None:
            continue
        num = _match_top_qnum(line)
        if num is None:
            continue
        if i in seen_lines:
            continue
        seen_lines.add(i)
        out.append((i, num))
    return out


# ============================================================
# PyMuPDF 全文抽取 → 题段切分
# ============================================================
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
        # PyMuPDF 1.28+ 损坏 PDF 返回 None 而非抛异常
        if doc is None:
            log.warning("[qnum_fallback] fitz.open 返回 None, 跳过")
            return []
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


# 修 #c: PDF 图片提取。`_iter_pages_text` 只抽文字, 不抽图。
# 加 `_iter_pages_images` 返回 [(page_idx, [(img_path, y0, y1, x0, x1), ...]), ...],
# 由 `_build_drafts_from_pages` 按 y 坐标把图插入对应题段的题干 (占位符 [IMG_N]).
def _iter_pages_images(pdf_path: str, images_dir: Optional[str] = None):
    """PyMuPDF 抽页内图片, 保存到 images_dir, 返回 [(page_idx, [(path, y0, y1, x0, x1), ...])].

    Args:
        pdf_path: PDF 路径
        images_dir: 图片保存目录; 缺省用 topic_garden 的 courseware/images
                     (复用 process_inbox 已有的图库, HTML 引用 images/<basename>).
    """
    try:
        import fitz
    except ImportError:
        return []
    if images_dir is None:
        # 默认 courseware/images — 复用 pipeline 已 symlink 的目录
        from ..paths import courseware_images_dir
        images_dir = str(courseware_images_dir())
    try:
        from pathlib import Path as _P
        _P(images_dir).mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    out: List[Tuple[int, list]] = []
    try:
        doc = fitz.open(pdf_path)
        if doc is None:
            return []
        try:
            for pn in range(len(doc)):
                page = doc[pn]
                imgs: list = []
                try:
                    for img_info in page.get_images(full=True):
                        xref = img_info[0]
                        try:
                            base = doc.extract_image(xref)
                        except Exception:
                            continue
                        if not base or not base.get("image"):
                            continue
                        ext = base.get("ext", "png")
                        import hashlib, tempfile
                        digest = hashlib.md5(base["image"]).hexdigest()[:12]
                        fname = f"mineru_{digest}.{ext}"
                        fpath = os.path.join(images_dir, fname)
                        if not os.path.exists(fpath):
                            try:
                                with open(fpath, "wb") as f:
                                    f.write(base["image"])
                            except Exception:
                                continue
                        # bbox 估算: 该 img 在 page 上的所有 bbox 矩形 union
                        # 简化: 跳过精确 bbox, 用 y=x 中位数占位 (后续 _build_drafts
                        # 按 y 范围分桶时不依赖精确位置)
                        rects = page.get_image_rects(xref)
                        y0, y1, x0, x1 = 0.0, page.rect.height, 0.0, page.rect.width
                        if rects:
                            r = rects[0]
                            y0, y1, x0, x1 = r.y0, r.y1, r.x0, r.x1
                        imgs.append((fpath, y0, y1, x0, x1))
                except Exception:
                    pass
                out.append((pn, imgs))
        finally:
            doc.close()
    except Exception as e:
        log.warning("[qnum_fallback] PyMuPDF 抽图失败: %s", e)
        return []
    return out


def _build_drafts_from_pages(pages: List[Tuple[int, str]], pdf_path: Optional[str] = None):
    """从 (page_idx, text) 列表切出题段, 生成 QuestionDraft.

    **按顶级题号切片** — 子问号 (1)/(2)/(3) 自然附在当前顶级题的题干中,
    不会独立成题. 这修复了 sample.pdf 上 page 1 把 `（1）/（2）/（3）` 误识
    为顶级题号 1/2/3, 把 11 题的子问号切成 3 道独立题段的问题.

    圈码 ①/②/③ 处理 (M5-4): 用状态机 — 圈码仅当**尚未见过任何数字顶级题号**
    时认作顶级 (因为纯圈码顶级卷确实存在, e.g. 整卷用 ①/②/③/④ 编号);
    一旦见过 `1./11．（8 分）/T1.` 这类数字顶级, 后续圈码降级为子编号
    (中文物理卷常把 ①/②/③/④ 当实验步骤 / 子问编号).

    修 #c: 可选传 pdf_path, 抽 PDF 图片按 y 坐标挂到对应题段.
    题段里加 `![图](images/<hash>.png)` 行, 同时 figure_paths 记录路径.

    Lazy import QuestionDraft — 避免 exam-to-html 不需要 topic_garden.models 时
    也强制依赖 (虽然 exam-to-html 总是依赖 topic-garden, 但 lazy import 让本模块
    在测试里能独立跑)。
    """
    from topic_garden.models import QuestionDraft

    # 1) 收集所有 (global_line_idx, page_idx, **顶级** qnum) — 状态机
    #    子问号 (_match_sub_qnum) 不参与匹配, 自然落到对应顶级题的题干里
    #    圈码 (_match_top_qnum 中带 circled 组) 仅在 seen_digit_top=False 时认顶级
    matches: List[Tuple[int, int, int]] = []
    global_idx = 0
    seen_digit_top = False  # 是否已见过数字顶级题号 — 用于圈码降级
    for pn, text in pages:
        for line in text.splitlines():
            # 优先看是不是数字顶级 (任何时候都认)
            digit_top = _match_digit_top_qnum(line)
            if digit_top is not None:
                matches.append((global_idx, pn, digit_top))
                seen_digit_top = True
                global_idx += 1
                continue
            # 圈码顶级: 仅在尚未见过数字顶级时认 (避免 11 题下 ①/②/③/④ 误识)
            if not seen_digit_top:
                circled_top = _match_top_qnum(line)
                if circled_top is not None:
                    matches.append((global_idx, pn, circled_top))
            # seen_digit_top=True 后, 圈码降级为子编号 — 不开新顶级, 自然落到题干
            global_idx += 1
    if not matches:
        return []

    # 2) 切片: 每题的内容 = 上一个顶级 qnum 行 + 1 到当前顶级 qnum 行 (不含)
    #    子问号 (1)/(2)/(3) 行自然落在题段内, 不会开新题
    all_lines: List[Tuple[int, str]] = []
    for pn, text in pages:
        for line in text.splitlines():
            all_lines.append((pn, line))

    drafts = []
    # 修 #c: 抽 PDF 图片 (按 y 坐标分配到题段)
    page_imgs: dict = {}  # pn -> list of (path, y0, y1, x0, x1)
    if pdf_path:
        try:
            for pn, imgs in _iter_pages_images(pdf_path):
                page_imgs[pn] = imgs
        except Exception as e:
            log.warning("[qnum_fallback] 抽图失败: %s", e)

    # 计算每张图的 basename (用于 markdown + figure_paths)
    img_meta: dict = {}  # abs_path -> {basename, y0, y1, page_idx}
    for pn, imgs in page_imgs.items():
        for (path, y0, y1, x0, x1) in imgs:
            img_meta[path] = {
                "basename": os.path.basename(path),
                "y0": y0, "y1": y1, "x0": x0, "x1": x1,
                "page_idx": pn,
            }

    # 估算每题段在 page 上的 y 范围 (用 page 文本块的 y 估算)
    page_text_y: dict = {}  # pn -> list of (gline, y0, y1) — 实际我们不知道 gline 的 y, 用题段行数估算
    # 简化: 不算精确 y 范围, 直接按 page 平均分配 (每页的图按顺序挂到该页题段, 题段内仅展示前几张)
    page_q_indices: dict = {}  # pn -> list of indices into drafts (该页所有题)
    for i, (gline, pn, num) in enumerate(matches):
        d = {
            "start": gline,
            "end": matches[i + 1][0] if i + 1 < len(matches) else len(all_lines),
            "pn": pn,
        }
        page_q_indices.setdefault(pn, []).append((i, d))

    for i, (gline, pn, num) in enumerate(matches):
        # 题段 = [顶级 qnum 行, 下一顶级 qnum 行) — 包含顶级 qnum 行本身 (题干起点)
        start = gline
        end = matches[i + 1][0] if i + 1 < len(matches) else len(all_lines)
        body_lines = [ln for (_pn, ln) in all_lines[start:end] if ln.strip()]
        content_md = "\n".join(body_lines).strip()
        if not content_md:
            continue
        # 修 #c: 给该题段挂页上的图 (按题段在该页的索引顺序, 每题段挂前 1 张图)
        # — 简化: 同页多题段共享页图, 用题段号取模分配避免一张图挂多题
        figures = []
        if pn in page_imgs and page_imgs[pn]:
            imgs_on_page = page_imgs[pn]
            # 取该题段是该页的第几个题
            q_idx_on_page = next(
                (qi for qi, (gi, _) in enumerate(page_q_indices[pn]) if gi == i),
                0,
            )
            # 平均分配: 每题段最多 1 张图, 按出现顺序 round-robin
            if q_idx_on_page < len(imgs_on_page):
                path = imgs_on_page[q_idx_on_page][0]
                img_md = f"\n\n![图](images/{os.path.basename(path)})\n\n"
                content_md = content_md + img_md
                figures = [path]
        drafts.append(QuestionDraft(
            content_md=content_md,
            has_figure=bool(figures),
            figure_paths=figures,
            source_page=pn,
            source_qnum=f"{num:02d}",  # 零填充 → 字典序 = 数值序 (修 composer 反序 bug)
            # 题型判定:复用 _post_process_md 规则,避免硬编码全部 fill_blank
            # (历史 bug 让 K3 计算题/含子问题显示 [?])
            q_type=detect_q_type(content_md, current="fill_blank"),
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

    修 #c: 同时抽 PDF 图片, 按题段在该页的索引分配 (避免一张图挂多题).
    """
    pages = _iter_pages_text(pdf_path)
    if not pages:
        return []
    drafts = _build_drafts_from_pages(pages, pdf_path=pdf_path)
    if drafts:
        log.info(
            "[qnum_fallback] 本地 pdf2ppt 0 题, 兜底从 PDF 原文抽出 %d 题段",
            len(drafts),
        )
    return drafts


__all__ = [
    "extract_drafts_with_lenient_qnum",
    "extract_qnums_from_text",
    "_match_qnum",
]