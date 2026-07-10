"""v2 解析引擎 — MinerU + GLM-4V 双引擎"""

import json
import os
import re
import sys
import time
from typing import List, Optional, Dict, Any, Tuple

# 自动加载 .env 文件
from pathlib import Path
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file)
    except ImportError:
        pass

from ._phys_text import (
    clean_text_block,
    count_options_outside_formula,
    extract_options_formula_aware,
    post_process_ocr,
    preprocess_markdown,
)
from ._v2_models import ContentBlock, Question, ParsedExam


# 图片尺寸过滤阈值 (真实像素面积, px²)。
# 依据: 28 份真实物理卷抽样测得, 纯符号/行内公式误诊图 area ≤8000 (最大 143×82=11726
# 属大号公式碎片, 但已是 orphan 无回归); 真实物理插图最小 181×62=11222。取保守 8000,
# 零误删真图, 从根上止血小符号被当图。可通过 parse(min_image_area=...) 覆盖。
MIN_IMAGE_AREA_PX = 8000


# ============================================================
# v0.12.2 — 卷头识别 (单选/多选范围)
# ============================================================
# 湖北卷典型卷头:
#   "一、单项选择题:本题共 7 小题,每小题 5 分,共 35 分。在每小题给出的四个选项中,
#    只有一项是符合题目要求的。"
#   "二、多项选择题:本题共 3 小题,每小题 5 分,共 15 分。在每小题给出的四个选项中,
#    有多项符合题目要求。"
# 但题号范围常散在后面的副段,例如:
#   "1. (5分) ... 7. (5分) ..."   (range=1-7)
#   "8. (5分) ... 10. (5分) ..."  (range=8-10)
# 主流做法是单笔正则吃"单/多项选择题"段标题,再在同段或后续若干页里找题号断点。
# 真实样本里,卷头 + 题号范围常在同一页(PAGE 0 / PAGE 1),less often P2-P4.
#
# 实现策略:扫 markdown 前 5 页文本块,对每页尝试
#   r'(?:单|单项)?选择题?[\s\S]*?(\d+)[.．、\s]\s*[-—–~至]\s*(\d+)'
#   r'(?:多|多项)?选择题?[\s\S]*?(\d+)[.．、\s]\s*[-—–~至]\s*(\d+)'
# 注意 \d+. 模式要规避 4 选项行末尾的"x."这种.我们对 range 内的两个数字都加 .或、空格约束.
#
# 输出: Dict[page_idx, {"single": [range], "multi": [range]}] (range = [start..end])
# 下游 topic_garden 用 (q.source_page, q.index) 查 is_multi_select:
#   page_idx ∈ layout 且 index ∈ layout[page]["multi"] → True
#                                                "single" → False
#                                   不在布局里        → None (沿用旧启发式)

_SECTION_TITLE_RE = re.compile(
    r'^#{1,4}\s*(?:[（(]?[一二三四五六七八九十]+[)）]?[、．.\s]).*?$'
    r'|'
    r'(?:^|\n)\s*(?:[（(]?[一二三四五六七八九十]+[)）]?[、．.\s][^\n]*)',
    re.MULTILINE,
)
# Match "单/多项选择题" section header — capture range (start..end) within same line OR
# later on the same/next page. We pair a SINGLE/MULTI section marker with the nearest range
# after it.
_RANGE_RE = re.compile(r'(\d{1,2})\s*(?:\\~|-{1,2}|—|–|~|至)\s*(\d{1,2})')


def _scan_page_layout_for_first_pages(
    markdown: str, max_pages: int = 5
) -> Dict[int, Dict[str, List[int]]]:
    """扫描 markdown 前 max_pages 页(或按页分隔符切分),提取 single/multi 题号范围。

    Args:
        markdown: 已 preprocess 过的 md 文本
        max_pages: 最多扫前几页 (卷头常在前 1-3 页)

    Returns:
        {page_idx: {"single": [start..end inclusive], "multi": [start..end inclusive]}}
        page_idx 是 0-based,匹配 MinerU 内部 page 编号。

    边界:
        - "选择题"段落内常见的题号段 = 范围不一定在同一页 — 我们采取"用同一页内最近的
          range" 启发。
        - 看不到段标题(纯 OCR 烂掉),返回 {}, 下游走回退启发式。
        - 单/多项都被识别但只有单项有数字 → only "single" key。
    """
    if not markdown:
        return {}

    # 按页分隔符切 markdown (MinerU Flash 在每页页脚有 P<n>L/R 标记;Precision 没);
    # 退路: 用 form feed (\\f) 切 (Markdown 罕见),若无则以整段为 page 0.
    pages = _split_markdown_to_pages(markdown, max_pages=max_pages)
    layout: Dict[int, Dict[str, List[int]]] = {}

    for page_idx, page_md in enumerate(pages):
        if page_idx >= max_pages:
            break
        # 在每页文本里扫描 "单/多项选择题"段 + 紧邻的 range
        page_layout = _extract_layout_from_page_text(page_md)
        if page_layout:
            layout[page_idx] = page_layout
    return layout


def _split_markdown_to_pages(markdown: str, max_pages: int) -> List[str]:
    """把 md 切成 page 列表 (按页面分隔符或 P<n>L/R marker)。

    返回长度 ≤ max_pages + 1 的 list;多出的尾部折进最后一个桶。
    """
    # MinerU marker: "P0L" / "P0R" / "P5L" 行
    marker_split = re.split(r'\n\s*P\d+[LR]\s*\n', markdown)
    if len(marker_split) > 1:
        return marker_split[:max_pages + 1]
    # Form feed (\\f 是 pdf 文本提取常见的硬换页)
    if '\f' in markdown:
        return markdown.split('\f')[:max_pages + 1]
    # 双换行 >= 2 视为页分隔 (heuristic)
    page_split = re.split(r'\n{3,}', markdown)
    if len(page_split) > 1:
        return page_split[:max_pages + 1]
    return [markdown]


def _extract_layout_from_page_text(page_md: str) -> Dict[str, List[int]]:
    """从单页文本里抠出 single/multi 题号范围。

    两轮策略:
        Pass 1 — section markers: 在页内找 (single, multi) section 的出现位置。
                 Hubei 单行: "一、单项选择题" / "二、多项选择题"
                 编号式:   "1。单选" / "2。多选"
        Pass 2 — range pairs:   按 section 顺序,在段后 200 字符内找第一个 range
                 "1-7" / "1~7" / "1至7" / "1\\~7" 等。
    如果只有 section 没 range → fallback heuristic (找 2 个相邻 range 假定 first=single second=multi)。

    Returns:
        {"single": [a..b], "multi": [c..d]} 或 {"single": [a..b]} 或 {} (无)。
    """
    if not page_md:
        return {}

    _SEP = r'(?:\\~|-{1,2}|—|–|~|至)'

    # ----- Pass 1: 找 single / multi section 在页内的位置 -----
    # 按 specificity 排序 (Hubei 详尽 > 编号式 > 退化)
    section_patterns = [
        # 强信号:段标题含 "单项/多项选择题"
        ('multi',  re.compile(r'(?:多项选择题|多项选择题。|多项选择题目|多选题)')),
        ('single', re.compile(r'(?:单项选择题|单项选择题。|单项选择题目|单选题)')),
        # 退化:仅 "单选"/"多选" (无 题 字) 在段标题位置 (行首 + 不在题号上下文)
        ('multi',  re.compile(r'(?:^|\n)\s*(?:\d+[、\.．]\s*)?(?:一|二|三|四|1|2|3|4)?[、\.．\s]*(?:多选|多项)\b')),
        ('single', re.compile(r'(?:^|\n)\s*(?:\d+[、\.．]\s*)?(?:一|二|三|四|1|2|3|4)?[、\.．\s]*(?:单选|单项)\b')),
    ]

    # section 出现位置 + kind,按 offset 升序 (multi 优先于 single 在同 offset — strict)
    section_marks = []
    for kind, pat in section_patterns:
        for m in pat.finditer(page_md):
            section_marks.append((m.start(), m.end(), kind, m.group(0)))
    section_marks.sort(key=lambda x: (x[0], 0 if x[2] == 'multi' else 1))
    # 去重:相邻(20 chars 内) 相同 kind 合并
    deduped = []
    last_end = -100
    last_kind = None
    for s, e, k, txt in section_marks:
        if k == last_kind and s < last_end + 20:
            continue
        deduped.append((s, e, k, txt))
        last_end = e
        last_kind = k
    section_marks = deduped

    # ----- Pass 2: 按 section 顺序,在每 section 之后 250 字符内找第一个 range -----
    range_pat = re.compile(r'(\d{1,2})\s*' + _SEP + r'\s*(\d{1,2})')

    sections_with_range = []  # [(kind, a, b)]
    search_start = 0
    for sec_start, sec_end, kind, txt in section_marks:
        # section 行内或之后 250 字符
        region_start = sec_end - 30 if sec_end > 30 else 0
        region_end = sec_end + 250
        # 找所有 range, 取最早且 > sec_start 的
        for m in range_pat.finditer(page_md[region_start:region_end]):
            try:
                a, b = int(m.group(1)), int(m.group(2))
                if not (1 <= a <= b <= 50):
                    continue
            except (ValueError, IndexError):
                continue
            sections_with_range.append((kind, a, b))
            break  # 每 section 只配一个 range

    out: Dict[str, List[int]] = {}
    for kind, a, b in sections_with_range:
        if kind in out:
            continue
        out[kind] = list(range(a, b + 1))

    # ----- Pass 3: 旧式 hard anchor (向后兼容) — 1 行式范围,如 Hubei 标准 -----
    if "single" not in out or "multi" not in out:
        old_patterns = [
            ("single", re.compile(r'第\s*(\d{1,2})\s*' + _SEP + r'\s*(\d{1,2})\s*题[^。\n]{0,15}?只有一项')),
            ("single", re.compile(r'第\s*(\d{1,2})\s*' + _SEP + r'\s*(\d{1,2})\s*题[^。\n]{0,15}?单项')),
            ("multi",  re.compile(r'第\s*(\d{1,2})\s*' + _SEP + r'\s*(\d{1,2})\s*题(?:(?!只有一项)[^。\n]){0,15}?有多项')),
            ("multi",  re.compile(r'第\s*(\d{1,2})\s*' + _SEP + r'\s*(\d{1,2})\s*题[^。\n]{0,15}?多项')),
            ("single", re.compile(r'(\d{1,2})\s*' + _SEP + r'\s*(\d{1,2})\s*题[^。\n]{0,15}?只有[一1]项')),
            ("single", re.compile(r'(\d{1,2})\s*' + _SEP + r'\s*(\d{1,2})\s*题[^。\n]{0,15}?只有一个[选项]')),
            ("multi",  re.compile(r'(\d{1,2})\s*' + _SEP + r'\s*(\d{1,2})\s*题?[^。\n]{0,15}?有(?:多项|[多2-9]个选项)')),
            ("multi",  re.compile(r'(\d{1,2})\s*' + _SEP + r'\s*(\d{1,2})\s*[^。\n]{0,15}?有多个选项')),
            ("single", re.compile(r'(?:单项选择题|单选题|选择题)\D{0,5}(\d{1,2})\s*' + _SEP + r'\s*(\d{1,2})')),
            ("multi",  re.compile(r'(?:多项选择题|多选题)\D{0,5}(\d{1,2})\s*' + _SEP + r'\s*(\d{1,2})')),
        ]
        for kind, pat in old_patterns:
            if kind in out:
                continue  # 已找到
            m = pat.search(page_md)
            if m:
                try:
                    a, b = int(m.group(1)), int(m.group(2))
                    if 1 <= a <= b <= 50:
                        out[kind] = list(range(a, b + 1))
                except (ValueError, IndexError):
                    pass

    # ----- Pass 4: 终极 fallback — 同页 2 个相邻 range -----
    if not out or ("single" not in out and "multi" not in out):
        ranges = []
        for m in re.finditer(r'(?:第\s*)?(\d{1,2})\s*' + _SEP + r'\s*(\d{1,2})\s*题', page_md):
            try:
                a, b = int(m.group(1)), int(m.group(2))
                if 1 <= a <= b <= 50 and b - a <= 30:
                    ranges.append((a, b))
            except (ValueError, IndexError):
                pass
        if len(ranges) >= 2:
            a, b = ranges[0]
            c, d = ranges[1]
            if c > b and c - b <= 5:
                if "single" not in out:
                    out["single"] = list(range(a, b + 1))
                if "multi" not in out:
                    out["multi"] = list(range(c, d + 1))
    return out


def _apply_layout_to_questions(
    exam: ParsedExam, layout: Dict[int, Dict[str, List[int]]]
) -> None:
    """根据 page_question_layout 给 exam.questions 标 is_multi_select。

    三轮:
      1) 卷头 layout 命中 → 按 (page, index) 查 single/multi
      2) layout 空 + questions 有 多个不同 section_title → 按 section_title 关键词分类
         (e.g. "一、单选题" → single, "二、多选题" → multi)
      3) 都空 → 留 None (fallback to heuristic 下游)
    """
    # 1) page+index lookup
    if layout:
        for q in exam.questions:
            page_layout = layout.get(q.source_page)
            if not page_layout:
                continue
            if q.index in page_layout.get("multi", []):
                q.is_multi_select = True
            elif q.index in page_layout.get("single", []):
                q.is_multi_select = False

    # 2) section_title fallback — 适用于"选择题区段用 section_title 区分" 的卷
    #    (e.g. "## 一、单选题" → multi=False, "## 二、多选题" → multi=True)
    #    仅在 layout 没覆盖的题上应用 (避免覆盖 1)
    already_tagged = sum(1 for q in exam.questions if q.is_multi_select is not None)
    if already_tagged == len(exam.questions):
        return  # 全部已标

    section_kinds: Dict[str, bool] = {}  # section_title → is_multi_select
    for q in exam.questions:
        if q.is_multi_select is not None:
            continue
        st = (q.section_title or "").strip()
        if not st:
            continue
        if st not in section_kinds:
            if "多选" in st or "多项" in st:
                section_kinds[st] = True
            elif "单选" in st or "单项" in st:
                section_kinds[st] = False
        if st in section_kinds:
            q.is_multi_select = section_kinds[st]
            already_tagged += 1


# 旧启发式:无卷头 / OCR 烂掉时回退 — q.options 数量 + 长题干暗示
def _heuristic_is_multi_select(question_text: str, has_options: bool) -> Optional[bool]:
    """备用:基于题干文本 + 是否有选项 判断多选。

    仅在 page_question_layout 为空时用;已知准确度 ~70% (湖北卷 1-7 vs 8-10)。
    """
    if not has_options:
        return None
    # 关键词: "下列说法" / "正确的是" 等模糊 → 标 None
    # 不实现 (现有 backfill 已有 v0.10.2),保留入口
    return None



class BaseParser:
    """解析引擎基类"""

    def parse(self, pdf_path: str, **kwargs) -> ParsedExam:
        raise NotImplementedError

    def is_available(self) -> bool:
        raise NotImplementedError


# ============================================================
# MinerU 云端 API 解析引擎
# ============================================================

def _has_calc_hint_in_text(text: str) -> bool:
    """是否含求:/计算:/试求 等典型计算题标记。"""
    if not text:
        return False
    return any(h in text for h in ("求:", "求：", "计算:", "计算：", "试求", "试求："))


def _looks_like_real_options(options) -> bool:
    """守卫:options 是否是真选项而非误切出的字母。

    规则:
      - 4 个,字母顺序 A→B→C→D 单调 (K2-Q2 ABCD 全拼/题干里的 [A-Z] 列表都 <4)
      - 每项非空,首字符以 A-D 开头 (防 $\\frac{...}{A}$)
      - 任一项 > 100 字符,视为把题干尾切进选项,丢弃
    """
    import re
    if not options or len(options) < 3:
        return False
    last_letter = None
    for opt in options:
        s = (opt or "").strip()
        if not s or len(s) < 2 or len(s) > 100:
            return False
        m = re.match(r"^([ABCD])[\.．、\s]", s)
        if not m:
            return False
        letter = m.group(1)
        if last_letter is not None and letter <= last_letter:
            return False
        last_letter = letter
    return True


class MinerUParser(BaseParser):
    """
    MinerU 解析引擎（基于 mineru-open-sdk）
    
    两种模式：
    - Flash Extract：免费，无需 Token，输出 Markdown（无图片/无 content_list）
    - Precision Extract（VLM 模型）：需 Token，输出完整结构化数据（含图片、content_list）
    
    SDK 文档: https://pypi.org/project/mineru-open-sdk/
    
    注意：仅使用 VLM 模型。Pipeline 模型已废弃（题数不完整、选项覆盖率仅 8-9%）。
    """

    def __init__(self, token: Optional[str] = None):
        self.token = token or os.environ.get("MINERU_TOKEN", "")
        self._client = None

    def is_available(self) -> bool:
        # Flash Extract 始终可用
        return True

    def _get_client(self):
        if self._client is None:
            from mineru import MinerU
            self._client = MinerU(token=self.token if self.token else None)
            self._client.set_source("pdf2ppt-v2")
        return self._client

    def parse(self, pdf_path: str, **kwargs) -> ParsedExam:
        """解析 PDF 文件"""
        # 提取 debug 选项
        self._debug_mode = kwargs.get("debug", False)

        # 检测 A3 双栏并裁切
        from ._v3_a3_splitter import A3Splitter
        if A3Splitter.is_a3_pdf(pdf_path):
            print(f"  📄 检测到 A3 双栏格式，正在裁切...")
            exam = self._parse_a3_pdf(pdf_path, **kwargs)
        else:
            client = self._get_client()
            use_flash = kwargs.get("flash", not bool(self.token))
            if use_flash:
                exam = self._parse_flash(client, pdf_path, **kwargs)
            else:
                exam = self._parse_precision(client, pdf_path, **kwargs)

        # 尺寸过滤: MinerU 常把小的行内公式/物理符号(如 "1/5√15gL"、"60°"、"F₁:F₂=…")
        # 误识别成 image block。这些"图"面积极小 (实测 ≤8000px²), 而真实物理插图
        # (受力图/图像/轨迹图) 最小也 >11000px² (181×62)。低于阈值的一律丢弃,
        # 从根上阻止小符号被当图渲染 (否则会污染 HTML / 丢失选项公式内容)。
        min_area = kwargs.get("min_image_area", MIN_IMAGE_AREA_PX)
        MinerUParser._filter_tiny_images(exam, min_area)
        return exam

    @staticmethod
    def _filter_tiny_images(exam: ParsedExam, min_area: int) -> int:
        """丢弃真实像素面积 < min_area 的 image block (误诊的小公式/符号).

        用 PIL 读 img_path 的真实尺寸 (MinerU 的 bbox 对 orphan 不可靠, 常 None)。
        对每个 question.blocks 和 exam.raw_blocks 都过滤。读图失败 → 保守保留
        (不因 IO 错误误删可能的真图)。

        Returns: 丢弃的图片数量。
        """
        if min_area <= 0:
            return 0
        import os
        from PIL import Image

        # 缓存 img_path → 是否过小, 避免同图重复读盘
        too_small_cache: dict = {}

        def _is_tiny(block) -> bool:
            if getattr(block, "block_type", None) != "image":
                return False
            path = getattr(block, "img_path", None)
            if not path:
                return False
            if path in too_small_cache:
                return too_small_cache[path]
            tiny = False
            try:
                if os.path.exists(path):
                    with Image.open(path) as im:
                        w, h = im.size
                    tiny = (w * h) < min_area
            except Exception:
                tiny = False  # 读图失败 → 保守保留
            too_small_cache[path] = tiny
            return tiny

        dropped = 0
        for q in getattr(exam, "questions", []) or []:
            kept = []
            for b in q.blocks:
                if _is_tiny(b):
                    dropped += 1
                else:
                    kept.append(b)
            q.blocks = kept
        raw_dropped = 0
        if hasattr(exam, "raw_blocks") and exam.raw_blocks:
            before = len(exam.raw_blocks)
            exam.raw_blocks = [b for b in exam.raw_blocks if not _is_tiny(b)]
            raw_dropped = before - len(exam.raw_blocks)

        total = dropped + raw_dropped
        if total:
            print(
                f"  🧹 尺寸过滤: 丢弃 {total} 张过小误诊图片 "
                f"(挂题 {dropped} + 孤儿 {raw_dropped}, area < {min_area}px²)"
            )
        return total

    def _parse_a3_pdf(self, pdf_path: str, **kwargs) -> ParsedExam:
        """解析 A3 双栏 PDF：裁切为左右两半，分别解析后合并"""
        import fitz
        import tempfile
        import os
        from ._v3_a3_splitter import A3Splitter

        splitter = A3Splitter()

        # 方案1：裁切成长 PDF（左栏在上，右栏在下）- 避免 y 值冲突
        long_pdf = splitter.merge_to_long_pdf(pdf_path)
        print(f"  📄 生成 A3 长PDF，共 {len(fitz.open(long_pdf))} 页")

        client = self._get_client()
        use_flash = kwargs.get("flash", not bool(self.token))

        # 解析长 PDF
        if use_flash:
            result = client.flash_extract(
                long_pdf,
                enable_formula=kwargs.get("enable_formula", True),
                enable_table=kwargs.get("enable_table", True),
                is_ocr=kwargs.get("is_ocr", True),
                language=kwargs.get("language", "ch"),
            )
            exam = self._parse_markdown(result.markdown) if result.state == "done" else ParsedExam()
            self._extract_images_from_pdf(exam, long_pdf)
            # 将 raw_blocks 中的图片分配到题目
            if exam.raw_blocks:
                exam.questions = MinerUParser._rebuild_questions_with_images(exam)
        else:
            result = client.extract(
                long_pdf,
                model="vlm",
                ocr=True,
                language=kwargs.get("language", "ch"),
                timeout=kwargs.get("max_wait", 300),
            )
            if result.content_list:
                cl = result.content_list if isinstance(result.content_list, list) else json.loads(result.content_list)
                exam = self._parse_content_list(cl)  # 无需 page_offset，因为页码已连续
            else:
                exam = self._parse_markdown(result.markdown) if result.state == "done" else ParsedExam()

            # 处理图片
            if result.images:
                exam = self._associate_images(exam, result.images)

        # 提取栏位信息：长 PDF Page 0,2,4...=左栏, Page 1,3,5...=右栏
        column_map = {}  # {long_pdf_page: column}
        try:
            doc = fitz.open(long_pdf)
            for pn in range(len(doc)):
                page = doc[pn]
                page_width = page.rect.width
                dict_text = page.get_text('dict')
                for block in dict_text.get('blocks', []):
                    if block.get('type') != 0:
                        continue
                    for line in block.get('lines', []):
                        for span in line.get('spans', []):
                            txt = span.get('text', '')
                            bbox = span.get('bbox', [])
                            if not bbox:
                                continue
                            if txt.startswith('P') and len(txt) >= 3:
                                x = bbox[0]
                                if x > page_width * 0.8:
                                    col = 0 if txt[-1] == 'L' else 1
                                    column_map[pn] = col
            doc.close()
        except Exception:
            pass

        # 批量更新题目的栏位
        # 长 PDF 结构：Page 0,2,4...=左栏, Page 1,3,5...=右栏
        # Flash 模式所有题目的 source_page=0，用 column_map 无法区分
        # 使用题号推断：前 N 道题在左栏，后面的在右栏
        # 规则：题号 ≤ 10 的选择题在左栏，题号 > 10 的非选择题在右栏
        print(f"  📌 column_map: {column_map}")

        if exam.questions:
            # 选择题（题号 1-10）在左栏，非选择题（题号 > 10）在右栏
            # 对于题号不连续的情况，按题号排序后前 N 道分配给左栏
            sorted_qs = sorted(exam.questions, key=lambda x: x.index)
            mid_idx = 10  # 前 10 道在左栏

            # 如果题号范围较小，使用题号作为阈值
            q_nums = [q.index for q in sorted_qs if q.index]
            if q_nums:
                max_q = max(q_nums)
                if max_q <= 10:
                    mid_idx = max_q  # 所有题都在左栏
                elif max_q <= 20:
                    # 假设 1-10 选择题在左栏，11+ 非选择题在右栏
                    mid_idx = 10

            updated = 0
            for q in exam.questions:
                if q.index and q.index <= mid_idx:
                    q.column = 0  # 左栏
                else:
                    q.column = 1  # 右栏
                    updated += 1

            print(f"  📌 题目题号范围: {min(q_nums)}-{max_q}，前 {mid_idx} 道在左栏")
            if updated > 0:
                print(f"  📌 发现 {updated} 道题在右栏")

        # 清理临时文件
        try:
            os.unlink(long_pdf)
        except Exception:
            pass

        print(f"  ✅ A3 裁切解析完成：共 {len(exam.questions)} 道题")
        exam.parser_used = "mineru-a3-long"
        # v0.12.2 — 卷头识别 (A3 路径也走 markdown 分支,所以可以共用 layout)
        try:
            exam.page_question_layout = _scan_page_layout_for_first_pages(
                result.markdown if 'result' in locals() else "", max_pages=5
            )
        except Exception:
            pass
        _apply_layout_to_questions(exam, exam.page_question_layout)
        return exam

    def _parse_flash(self, client, pdf_path: str, **kwargs) -> ParsedExam:
        """Flash Extract：免费快速，输出 Markdown"""
        print(f"  ⚡ MinerU Flash Extract（免费，无需Token）...")
        result = client.flash_extract(
            pdf_path,
            enable_formula=kwargs.get("enable_formula", True),
            enable_table=kwargs.get("enable_table", True),
            is_ocr=kwargs.get("is_ocr", True),
            language=kwargs.get("language", "ch"),
        )

        if result.state != "done":
            raise RuntimeError(f"MinerU Flash Extract 失败: state={result.state}")

        markdown = result.markdown
        print(f"  ✅ 解析完成，Markdown {len(markdown)} 字符")

        # 从 PDF 直接提取页码标记信息（用于 A3 双栏模式）
        # 长 PDF 结构：Page 0,2,4...=左栏, Page 1,3,5...=右栏
        # column_map: {long_pdf_page: column}  0=左栏, 1=右栏
        column_map = {}
        try:
            import fitz
            doc = fitz.open(pdf_path)
            for pn in range(len(doc)):
                page = doc[pn]
                page_width = page.rect.width
                dict_text = page.get_text('dict')
                for block in dict_text.get('blocks', []):
                    if block.get('type') != 0:
                        continue
                    for line in block.get('lines', []):
                        for span in line.get('spans', []):
                            txt = span.get('text', '')
                            bbox = span.get('bbox', [])
                            if not bbox:
                                continue
                            if txt.startswith('P') and len(txt) >= 3:
                                x = bbox[0]
                                if x > page_width * 0.8:
                                    col = 0 if txt[-1] == 'L' else 1
                                    column_map[pn] = col
            doc.close()
        except Exception:
            pass

        # 从 Markdown 解析为 ParsedExam
        exam = self._parse_markdown(markdown)

        # 如果有页码标记，批量更新题目的栏位
        if column_map:
            for q in exam.questions:
                if q.source_page in column_map:
                    q.column = column_map[q.source_page]
                    print(f"  📌 Q{q.index} 栏位: {'右' if q.column else '左'}")

        # 对于 Flash 模式，图片不可用，需要从原始 PDF 提取
        self._extract_images_from_pdf(exam, pdf_path)

        exam.parser_used = "mineru-flash"
        # v0.12.2 — 卷头识别
        exam.page_question_layout = _scan_page_layout_for_first_pages(markdown, max_pages=5)
        _apply_layout_to_questions(exam, exam.page_question_layout)
        return exam

    def _parse_precision(self, client, pdf_path: str, **kwargs) -> ParsedExam:
        """Precision Extract：需 Token，输出完整结构化数据"""
        print(f"  🎯 MinerU Precision Extract（Token 模式）...")
        result = client.extract(
            pdf_path,
            model="vlm",
            ocr=kwargs.get("is_ocr", True),  # OCR 默认开启：PDF 自定义字体需要 OCR 才能正确识别物理符号
            language=kwargs.get("language", "ch"),
            pages=kwargs.get("pages"),
            timeout=kwargs.get("max_wait", 300),
        )

        if result.state != "done":
            raise RuntimeError(f"MinerU Precision Extract 失败: state={result.state}")

        print(f"  ✅ 解析完成")

        # 优先使用 content_list
        exam = ParsedExam()
        if result.content_list:
            import json
            cl = result.content_list
            if isinstance(cl, str):
                cl = json.loads(cl)
            exam = self._parse_content_list(cl)
            exam.parser_used = "mineru-precision-contentlist"
        else:
            # 回退到 Markdown
            exam = self._parse_markdown(result.markdown)
            exam.parser_used = "mineru-precision-markdown"

        # v0.12.2 — 卷头识别 (content_list 路径无 markdown, 标空 layout)
        try:
            md_for_layout = result.markdown if result.content_list else result.markdown
            exam.page_question_layout = _scan_page_layout_for_first_pages(
                md_for_layout or "", max_pages=5
            )
        except Exception:
            pass
        _apply_layout_to_questions(exam, exam.page_question_layout)

        # 保存图片到本地并关联到 content_list
        if result.images:
            import tempfile
            print(f"  📷 提取到 {len(result.images)} 张图片，保存到本地...")
            img_path_map = {}  # content_list 中的相对路径 -> 本地绝对路径
            for img in result.images:
                # 保存图片到临时文件
                ext = os.path.splitext(img.name)[1] or ".jpg"
                tmp = tempfile.NamedTemporaryFile(
                    suffix=ext, prefix="mineru_", delete=False
                )
                tmp.write(img.data)
                tmp.close()
                # content_list 中的 img_path 是相对路径如 "images/xxx.jpg"
                img_path_map[img.path] = tmp.name

            # 统计 content_list 中已有的 image block 数量
            existing_img_paths = set()
            for block in exam.raw_blocks:
                if block.block_type == "image" and block.img_path:
                    existing_img_paths.add(block.img_path)

            # 跟踪已关联的图片路径
            associated_paths = set()

            # 更新 content_list 中的图片路径
            for block in exam.raw_blocks:
                if block.block_type == "image" and block.img_path:
                    relative = block.img_path
                    if relative in img_path_map:
                        block.img_path = img_path_map[relative]
                        associated_paths.add(relative)
                    else:
                        # 尝试匹配文件名
                        img_name = os.path.basename(relative)
                        for rel_path, abs_path in img_path_map.items():
                            if os.path.basename(rel_path) == img_name:
                                block.img_path = abs_path
                                associated_paths.add(rel_path)
                                break

            # 添加孤儿图片（在 result.images 中但不在 content_list 的 image block 中）
            # 这些图片可能是 table 类型或没有 bbox 的图片
            orphan_count = 0
            for img_path, local_path in img_path_map.items():
                if img_path not in associated_paths:
                    # 尝试从 img_path 推断 page_idx（路径格式通常是 images/page_X_xxx.jpg）
                    page_idx = 0
                    import re
                    page_match = re.search(r'page[_\s-]*(\d+)', img_path.lower())
                    if page_match:
                        page_idx = int(page_match.group(1)) - 1  # 转为 0-based

                    # 添加为孤儿图片块
                    exam.raw_blocks.append(ContentBlock(
                        block_type="image",
                        content="PDF提取图片",
                        img_path=local_path,
                        page_idx=page_idx,
                        bbox=None,  # 没有 bbox 信息
                    ))
                    orphan_count += 1

            if orphan_count > 0:
                print(f"  ⚠️ 添加了 {orphan_count} 张孤儿图片（不在 content_list 中）")

        return exam

    @staticmethod
    def _associate_images(exam: ParsedExam, images: list) -> ParsedExam:
        """将 MinerU 返回的图片数据保存到本地并关联到题目"""
        if not images:
            return exam

        import tempfile
        img_path_map = {}  # content_list 中的相对路径 -> 本地绝对路径
        for img in images:
            ext = os.path.splitext(img.name)[1] or ".jpg"
            tmp = tempfile.NamedTemporaryFile(suffix=ext, prefix="mineru_", delete=False)
            tmp.write(img.data)
            tmp.close()
            img_path_map[img.path] = tmp.name

        # 更新 raw_blocks 中的图片路径
        for block in exam.raw_blocks:
            if block.block_type == "image" and block.img_path:
                relative = block.img_path
                if relative in img_path_map:
                    block.img_path = img_path_map[relative]
                else:
                    img_name = os.path.basename(relative)
                    for rel_path, abs_path in img_path_map.items():
                        if os.path.basename(rel_path) == img_name:
                            block.img_path = abs_path
                            break

        # 处理孤儿图片
        for img_path, local_path in img_path_map.items():
            found = any(b.img_path == local_path for b in exam.raw_blocks if b.block_type == "image")
            if not found:
                page_idx = 0
                page_match = re.search(r'page[_\s-]*(\d+)', img_path.lower())
                if page_match:
                    page_idx = int(page_match.group(1)) - 1
                exam.raw_blocks.append(ContentBlock(
                    block_type="image",
                    content="PDF提取图片",
                    img_path=local_path,
                    page_idx=page_idx,
                    bbox=None,
                ))

        # 重建 questions 中的图片关联
        exam.questions = MinerUParser._rebuild_questions_with_images(exam)

        return exam

    @staticmethod
    def _rebuild_questions_with_images(exam: ParsedExam) -> List[Question]:
        """根据 raw_blocks 重建 questions，基于 y_center 智能分配图片"""
        from ._v2_models import ContentBlock, Question, ParsedExam

        # 收集页面上所有图片及其 y_center 和栏位
        # Flash 模式图片没有 bbox，使用 page_idx 直接判断栏位
        images_by_page = {}
        for block in exam.raw_blocks:
            if block.block_type == "image" and block.img_path:
                page_idx = block.page_idx
                # 长 PDF 结构：Page 0,2,4...=左栏，Page 1,3,5...=右栏
                # 原始页码 = page_idx // 2
                # 栏位 = page_idx % 2 (0=左栏, 1=右栏)
                orig_page = page_idx // 2
                col_idx = page_idx % 2  # 0=左栏, 1=右栏

                # 如果图片没有 bbox，使用 text_level 获取栏位（从 _extract_images_from_pdf 设置）
                if not block.bbox or len(block.bbox) < 4:
                    if hasattr(block, 'text_level'):
                        col_idx = block.text_level

                if orig_page not in images_by_page:
                    images_by_page[orig_page] = []

                # Flash 模式没有 bbox，使用 page_idx 估算 y_center
                # 左栏图片均匀分布在 y=0-842，右栏图片分布在 y=421-1263
                if block.bbox and len(block.bbox) >= 4:
                    y_center = (block.bbox[1] + block.bbox[3]) / 2
                else:
                    # 估算：每页约 19 张图片，平均分布在各 y 位置
                    # 使用 page_idx 和索引估算分布
                    page_img_count = len([b for b in exam.raw_blocks
                                           if b.block_type == "image"
                                           and b.page_idx == page_idx])
                    img_idx = sum(1 for b in exam.raw_blocks
                                  if b.block_type == "image"
                                  and b.page_idx == page_idx
                                  and id(b) < id(block))
                    y_center = (img_idx / max(page_img_count, 1)) * 842

                images_by_page[orig_page].append((block, y_center, col_idx))

        for orig_page in images_by_page:
            images_by_page[orig_page].sort(key=lambda x: x[1])

        for q in exam.questions:
            q.blocks = [b for b in q.blocks if b.block_type != "image"]

        # 分配图片：基于栏位匹配
        # 所有题目 source_page=0，需要按 column 分开处理
        for orig_page, img_list in images_by_page.items():
            # 分离左右栏图片
            left_imgs = [(b, y) for b, y, col in img_list if col == 0]
            right_imgs = [(b, y) for b, y, col in img_list if col == 1]

            # 按 column 分离题目
            left_questions = [q for q in exam.questions if q.column == 0]
            right_questions = [q for q in exam.questions if q.column == 1]

            # 分配左栏图片给左栏题目
            if left_imgs and left_questions:
                MinerUParser._assign_images_to_questions(left_imgs, left_questions)

            # 分配右栏图片给右栏题目
            if right_imgs and right_questions:
                MinerUParser._assign_images_to_questions(right_imgs, right_questions)

            # 处理孤儿图片
            orphan_imgs = [(b, y) for b, y, col in img_list if col not in (0, 1)]
            if orphan_imgs and exam.questions:
                MinerUParser._assign_images_to_questions(orphan_imgs, exam.questions)

        return exam.questions

    @staticmethod
    def _assign_images_to_questions(img_list: list, questions: list):
        """将图片列表分配到题目列表，使用 y_center 匹配"""
        if not questions or not img_list:
            return

        # 计算每道题的 y 位置
        # 优先使用 bbox，否则使用题号顺序估算位置
        for i, q in enumerate(questions):
            q._q_y = float('inf')
            for b in q.blocks:
                if b.block_type == "text" and b.bbox and len(b.bbox) >= 4:
                    q._q_y = b.bbox[1]
                    break
            if q._q_y == float('inf'):
                # 没有 bbox 时，按题号顺序分配位置
                # 每个题占 842/len(questions) 的高度
                q._q_y = i * (842 / max(len(questions), 1))

        questions_sorted = sorted(questions, key=lambda q: q._q_y)

        # 为每道题计算 y 范围
        for i, q in enumerate(questions_sorted):
            if i + 1 < len(questions_sorted):
                q._y_range = (q._q_y, questions_sorted[i + 1]._q_y)
            else:
                q._y_range = (q._q_y, float('inf'))

        # 分配图片：图片的 y_center 落在哪个题目的 y 范围内
        for img_block, img_y in img_list:
            assigned = False
            for q in questions_sorted:
                if q._y_range[0] <= img_y < q._y_range[1]:
                    q.blocks.append(img_block)
                    assigned = True
                    break
            if not assigned:
                # 如果没找到，分配给最近的题目
                if questions_sorted:
                    nearest = min(questions_sorted, key=lambda q: abs(q._q_y - img_y))
                    nearest.blocks.append(img_block)

    def _parse_markdown(self, markdown: str) -> ParsedExam:
        """将 Markdown 文本解析为 ParsedExam"""
        import re

        # Bug 3 修复: 当外部直接调 _parse_markdown (跳过 __init__/parse)
        # 时, self._debug_mode 还没被赋值。_split_into_questions 末尾会
        # 读 self._debug_mode 传给 _reassign_images_by_y_center, 这里兜底。
        if not hasattr(self, "_debug_mode"):
            self._debug_mode = False

        # 预处理 Markdown
        markdown = preprocess_markdown(markdown)

        # 规范化换行符
        markdown = markdown.replace('\r\n', '\n').replace('\r', '\n')

        # 分析页码标记，识别左右栏内容
        # 格式：P0L = A3第0页左栏, P0R = A3第0页右栏, P1L = A3第1页左栏...
        # 注：MinerU Flash 模式下这些标记可能被编码，需要用页码推断
        # 长 PDF 结构：Page 0,2,4...=左栏, Page 1,3,5...=右栏
        page_markers = {}
        marker_pattern = re.compile(r'P(\d+)([LR])')
        for m in marker_pattern.finditer(markdown):
            orig_page = int(m.group(1))
            col = 0 if m.group(2) == 'L' else 1
            page_markers[orig_page] = col

        exam = ParsedExam()

        SECTION_PATTERN = re.compile(r'^##\s*([一二三四五六七八九十]+[、．.\s].*?)(?:\n|$)')
        # 题号模式：支持 "1." "1、" "1．" 等
        QUESTION_PATTERN = re.compile(r'^(\d{1,3})[．.、\s]')
        # 选项模式：匹配行内或行首的 A/B/C/D 选项
        INLINE_OPTION_PATTERN = re.compile(r'([ABCD])[．.、\s]')
        LINE_OPTION_PATTERN = re.compile(r'^([ABCD])[．.、\s]')
        # 页码标记模式：P0L, P0R, P1L, P1R...
        PAGE_MARKER_PATTERN = re.compile(r'^P(\d+)([LR])$')

        lines = markdown.split('\n')
        current_section = None
        current_question = None
        question_text_buffer = ""
        current_page_idx = 0  # 当前解析到的长PDF页码
        current_column = 0   # 0=左栏, 1=右栏

        def flush_question():
            nonlocal question_text_buffer
            if current_question:
                # 从文本中提取行内选项
                text = question_text_buffer.strip()
                # 匹配 "（ ）A．...B．...C．...D．..." 模式
                opt_match = re.search(r'[（(]\s*[)）]\s*([ABCD][．.、\s].*)', text)
                if opt_match and not current_question.options:
                    opt_text = opt_match.group(1)
                    # 分割选项
                    parts = re.split(r'(?=[ABCD][．.、\s])', opt_text)
                    for part in parts:
                        part = part.strip()
                        if part and re.match(r'^[ABCD][．.、\s]', part):
                            current_question.options.append(part)
                    # 从文本中移除选项部分
                    text = text[:opt_match.start()].strip()

                current_question.blocks.append(ContentBlock(
                    block_type="text",
                    content=text,
                ))
                exam.questions.append(current_question)
                question_text_buffer = ""

        for line in lines:
            stripped = line.strip()

            # 跳过空行和 HTML 注释
            if not stripped or stripped.startswith('<!--'):
                continue

            # 检测页码标记（P0L, P0R, P1L, P1R...），更新当前栏位
            page_marker_match = PAGE_MARKER_PATTERN.match(stripped)
            if page_marker_match:
                current_page_idx = int(page_marker_match.group(1))
                current_column = 0 if page_marker_match.group(2) == 'L' else 1
                continue

            # 后处理 OCR 乱码
            stripped = clean_text_block(stripped)
            if not stripped:
                continue

            # 检测大题标题
            section_match = SECTION_PATTERN.match(stripped)
            if section_match:
                current_section = section_match.group(1).strip()
                continue

            # 检测题号：优先匹配题号，而不是选项
            q_match = QUESTION_PATTERN.match(stripped)
            if q_match:
                num = int(q_match.group(1))
                if 1 <= num <= 50:
                    flush_question()
                    current_question = Question(
                        index=num,
                        label=f"{num}．",
                        section_title=current_section,
                        source_page=current_page_idx,
                        column=current_column,  # 使用当前解析到的栏位
                    )
                    question_text_buffer = stripped[q_match.end():]
                    continue

            # 检测独立行的选项（但排除行尾包含题号的行，如 "D. xxx2. xxx"）
            line_opt_match = LINE_OPTION_PATTERN.match(stripped)
            if line_opt_match and current_question:
                # 检查行尾是否有题号模式（如 "2.一个" 没有空格分隔）
                # 匹配：数字 + . + 非ASCII字符（题号后跟中文）
                trailing_q_match = re.search(r'(\d+)\.([^\x00-\x7F].*)$', stripped)
                if trailing_q_match:
                    q_num = int(trailing_q_match.group(1))
                    if 1 <= q_num <= 50:
                        # 分割：选项部分保留，题号部分作为新题
                        opt_part = stripped[:trailing_q_match.start()].strip()
                        if opt_part and re.match(r'^[ABCD][．.、\s]', opt_part):
                            current_question.options.append(opt_part)
                        flush_question()
                        current_question = Question(
                            index=q_num,
                            label=f"{q_num}．",
                            section_title=current_section,
                            source_page=current_page_idx,
                            column=current_column,
                        )
                else:
                    # 检查是否以选项开头
                    if re.match(r'^[ABCD][．.、\s]', stripped):
                        # 检查是否多选项挤一行（行中有多个 "选项字母."）
                        # 注意：不在 LaTeX 公式内部计数
                        opt_count = count_options_outside_formula(stripped)
                        if opt_count >= 2:
                            # 多选项挤一行：分割成独立选项
                            parts = re.split(r'(?=[ABCD][．.、\s])', stripped)
                            for part in parts:
                                part = part.strip()
                                if part and re.match(r'^[ABCD][．.、\s]', part):
                                    current_question.options.append(part)
                        else:
                            current_question.options.append(stripped)
                    else:
                        # 尝试查找多选项挤一行的情况
                        all_opts = list(re.finditer(r'(?<![A-Za-z}]) ([ABCD])[．.、\s]', stripped))
                        if len(all_opts) >= 2:
                            for i in range(len(all_opts)):
                                start = all_opts[i].start()
                                if i + 1 < len(all_opts):
                                    end = all_opts[i + 1].start()
                                else:
                                    end = len(stripped)
                                opt_text = stripped[start:end].strip()
                                if opt_text:
                                    current_question.options.append(opt_text)
                        else:
                            current_question.options.append(stripped)
                continue

            # 普通文本
            if current_question:
                question_text_buffer += "\n" + stripped

        # 最后一题
        flush_question()

        # 识别题型
        for q in exam.questions:
            if q.options:
                q.question_type = "choice"
            elif q.section_title:
                if "实验" in q.section_title:
                    q.question_type = "experiment"
                elif "计算" in q.section_title or "解答" in q.section_title:
                    q.question_type = "calculation"
                elif "填空" in q.section_title:
                    q.question_type = "fill_blank"

        return exam

    def _extract_images_from_pdf(self, exam: ParsedExam, pdf_path: str):
        """从原始 PDF 提取图片，关联到题目中"""
        import fitz
        try:
            doc = fitz.open(pdf_path)
            exam.page_count = len(doc)
            for pn in range(len(doc)):
                page = doc[pn]
                exam.page_sizes.append((page.rect.width, page.rect.height))

                # 检测栏位标记（L=左栏, R=右栏）
                # 左栏偶数页(0,2,4...)的x坐标在左半部分
                # 右栏奇数页(1,3,5...)的x坐标在右半部分
                is_right_column = (pn % 2 == 1)

                for img_info in page.get_images(full=True):
                    try:
                        xref = img_info[0]
                        base_image = doc.extract_image(xref)
                        if base_image:
                            # 保存图片到临时目录
                            import tempfile
                            img_ext = base_image.get("ext", "png")  # 修复：使用 get 方法
                            img_data = base_image.get("image", b"")  # 修复：使用 get 方法
                            if not img_data:
                                continue
                            tmp = tempfile.NamedTemporaryFile(
                                suffix=f".{img_ext}", delete=False
                            )
                            tmp.write(img_data)
                            tmp.close()
                            # 添加为全局图片块，标记栏位
                            exam.raw_blocks.append(ContentBlock(
                                block_type="image",
                                content="PDF提取图片",
                                img_path=tmp.name,
                                page_idx=pn,
                                text_level=1 if is_right_column else 0,  # 用 text_level 暂存栏位: 1=右栏
                            ))
                    except Exception as ex:
                        print(f"  ⚠ 图片提取失败: {ex}")
            doc.close()
        except Exception as e:
            print(f"  ⚠ PDF 图片提取失败: {e}")

    def _parse_content_list(self, content_list: list, page_offset: int = 0) -> ParsedExam:
        """将 MinerU content_list.json 转换为 ParsedExam"""
        # Bug 3 修复: 兜底 _debug_mode (同 _parse_markdown 注释)
        if not hasattr(self, "_debug_mode"):
            self._debug_mode = False
        exam = ParsedExam()

        for item in content_list:
            block_type = item.get("type", "text")
            page_idx = item.get("page_idx", 0) + page_offset  # 应用页码偏移
            bbox = item.get("bbox")

            if block_type == "text":
                exam.raw_blocks.append(ContentBlock(
                    block_type="text",
                    content=item.get("text", ""),
                    bbox=bbox,
                    page_idx=page_idx,
                    text_level=item.get("text_level", 0),
                ))
            elif block_type == "equation":
                exam.raw_blocks.append(ContentBlock(
                    block_type="equation",
                    content=item.get("text", ""),
                    bbox=bbox,
                    page_idx=page_idx,
                ))
            elif block_type == "image":
                exam.raw_blocks.append(ContentBlock(
                    block_type="image",
                    content=item.get("text_detected", [""])[0] if item.get("text_detected") else "",
                    bbox=bbox,
                    page_idx=page_idx,
                    img_path=item.get("img_path"),
                ))
            elif block_type == "table":
                exam.raw_blocks.append(ContentBlock(
                    block_type="table",
                    content=item.get("html", ""),
                    bbox=bbox,
                    page_idx=page_idx,
                ))
            elif block_type == "list":
                # list 块包含 list_items，通常是选项列表
                # 将每个 list_item 展开为独立的 text 块
                list_items = item.get("list_items", [])
                for li in list_items:
                    if isinstance(li, dict):
                        li_text = li.get("text", "")
                    elif isinstance(li, str):
                        li_text = li
                    else:
                        continue
                    if li_text.strip():
                        exam.raw_blocks.append(ContentBlock(
                            block_type="text",
                            content=li_text.strip(),
                            bbox=bbox,
                            page_idx=page_idx,
                        ))

        # 保持 content_list 原始顺序（MinerU 已按阅读顺序排列）
        # 注意：MinerU 的 content_list 已经按 PDF 中的阅读顺序排列，
        # 不需要按坐标重新排序，否则会破坏选择题选项的顺序（A→B→C→D）
        # 如果需要排序（如双栏布局），应在题目分割后再处理
        exam.questions = self._split_into_questions(exam.raw_blocks)
        return exam

    def _split_into_questions(self, blocks: List[ContentBlock]) -> List[Question]:
        """
        将内容块按题号分割为独立的题目。

        图片分配策略（基于 v1 图片截取逻辑）：
        - 图片归入其视觉位置最近的题目（通过 y 坐标判断）
        - 如果图片的 y 坐标在题号之前，归入当前题目（题目会引用该图片）
        - 如果图片的 y 坐标在前一题的范围内，归入前一题
        - 避免图片被错误地归入没有引用它的题目
        """
        import re

        # Bug 3 修复: 直接调 _split_into_questions (绕过 __init__/parse)
        # 时, 末尾 _reassign_images_by_y_center 会读 self._debug_mode, 这里兜底。
        if not hasattr(self, "_debug_mode"):
            self._debug_mode = False

        SECTION_PATTERN = re.compile(r'^[（(]?[一二三四五六七八九十]+[)）]?[、．.\s]')
        QUESTION_PATTERN = re.compile(r'^(\d{1,3})[\.．、\s]')
        OPTION_PATTERN = re.compile(r'^[（(]?[ABCD][)）]?[\.．、\s]')

        # 不排序：MinerU content_list 已按阅读顺序排列
        # 如果排序会破坏选择题选项顺序（A→B→C→D）

        questions = []
        current_question = None
        current_section = None

        # 收集图片信息用于智能分配
        pending_images = []  # 待分配的图片 [(block, y_pos, page_idx)]

        def assign_pending_images(target_question):
            """将待分配的图片分配给目标题目"""
            for img_block, y_pos, page_idx in pending_images:
                img_block.page_idx = page_idx  # 确保 page_idx 正确
                target_question.blocks.append(img_block)
            pending_images.clear()

        def should_assign_to_prev_question(y_pos, current_q, questions):
            """
            判断图片是否应该归入前一题（而不是当前题）。
            规则：如果图片的 y 坐标落在前一题的文本范围内，说明它属于前一题。
            """
            if not questions:
                return False
            prev_q = questions[-1]
            # 检查前一题是否有文本引用图片（通过关键字判断）
            for block in prev_q.blocks:
                if block.block_type == 'text' and block.content:
                    # 常见的图片引用关键字
                    if any(kw in block.content for kw in ['如图所示', '如图', '如图1', '如图2', '装置如图', '图像', '示意图']):
                        return True
            # 备选规则：图片紧跟在前一题的选项后面
            if prev_q.options and len(prev_q.blocks) > 0:
                last_block = prev_q.blocks[-1]
                if last_block.block_type == 'text':
                    # 如果图片 y 坐标接近前一题最后一个文本块的 y 坐标
                    if last_block.bbox:
                        last_y = last_block.bbox[3]  # bbox = [x0, y0, x1, y1]
                        if y_pos < last_y + 50:  # 图片在文本块下方 50pt 内
                            return True
            return False

        for block in blocks:
            # 处理图片：暂时收集，稍后分配
            if block.block_type == "image":
                y_pos = block.bbox[3] if block.bbox and len(block.bbox) >= 4 else 0
                pending_images.append((block, y_pos, block.page_idx))
                continue

            # 遇到文本块时，先把之前收集的图片分配给当前题目
            # 然后根据图片位置决定是分配给当前题还是前一题
            if pending_images and current_question:
                for img_block, y_pos, page_idx in pending_images:
                    # 判断图片应该归入当前题还是前一题
                    if should_assign_to_prev_question(y_pos, current_question, questions):
                        if questions:
                            questions[-1].blocks.append(img_block)
                    else:
                        img_block.page_idx = page_idx
                        current_question.blocks.append(img_block)
                pending_images.clear()

            if block.block_type != "text":
                if current_question:
                    current_question.blocks.append(block)
                continue

            text = block.content.strip()

            # 后处理 OCR 乱码
            text = post_process_ocr(text)
            if not text:
                continue

            # 过滤 OCR 噪声：包含乱码字符的短文本块
            # 保留包含单字符物理变量（可能被 VLM 误识别为 ?）的文本
            # 只过滤明确是噪声的模式（ZZZZ, 连续问号, 花括号单独出现等）
            if len(text) < 30 and re.search(r'(ZZZZ|\?\?(?!\S)|[{}]{2,}|<{2,}|>{2,})', text):
                if not re.match(r'^[ABCD][．.、\s]', text):
                    continue

            if SECTION_PATTERN.match(text):
                current_section = text
                continue

            match = QUESTION_PATTERN.match(text)
            if match and not OPTION_PATTERN.match(text):
                num = int(match.group(1))
                if 1 <= num <= 50:
                    # 排除物理量表达式（如 "4 kg·m/s" 被误判为题号 4）
                    rest = text[match.end():].strip()
                    if rest:
                        PHYS_UNITS = ['kg', 'm/', 's/', 'J ', 'N ', 'W ', 'kg·', 'm/s', 'J/s']
                        if any(rest.startswith(u) for u in PHYS_UNITS) or re.match(r'^\d+\.?\d*\s*[a-zA-Z]\s*/\s*[a-zA-Z]', rest):
                            # 物理量而非题号，归入当前题目
                            if current_question:
                                current_question.blocks.append(block)
                            continue
                    if current_question:
                        questions.append(current_question)
                    # 长 PDF 结构：page_idx 0,2,4... 是左栏，1,3,5... 是右栏
                    # 原始页码 = page_idx // 2，栏位 = page_idx % 2
                    col_idx = block.page_idx % 2
                    current_question = Question(
                        index=num,
                        label=text,
                        section_title=current_section,
                        source_page=block.page_idx // 2,  # 转换回原始页码
                        column=col_idx,  # 栏位：0=左栏, 1=右栏
                    )
                    remaining = text[match.end():].strip()
                    if remaining:
                        # Q4/Q9-style: MinerU 把 stem + 4 选项合并成单一 text 块
                        # (例: "4. ... 释放位置是 ( )\n\nA. A点\n\nB. B点\n\nC. O点\n\nD. A、B两点")
                        # 旧逻辑会把剩余整段作 stem → 4 选项被吞。修复: 检测到剩余含
                        # A./B./C./D. labels 时,拆出选项,保留纯 stem 部分。
                        if count_options_outside_formula(remaining) >= 2:
                            # 公式感知 split: 先按 $...$ 切, 仅在非公式部分拆 A./B./C./D.
                            stem_only, opts = extract_options_formula_aware(remaining)
                            for op in opts:
                                current_question.options.append(post_process_ocr(op))
                            if stem_only:
                                current_question.blocks.append(ContentBlock(
                                    block_type="text",
                                    content=stem_only,
                                    bbox=block.bbox,
                                    page_idx=block.page_idx,
                                ))
                        else:
                            current_question.blocks.append(ContentBlock(
                                block_type="text",
                                content=remaining,
                                bbox=block.bbox,
                                page_idx=block.page_idx,
                            ))
                    continue

            option_match = OPTION_PATTERN.match(text)
            if option_match and current_question:
                # 实验题不剥离选项，保留在文本块中行内显示（小问中的选择题选项要跟着小问走）
                if current_question.section_title and "实验" in current_question.section_title:
                    current_question.blocks.append(block)
                    continue
                # 选项统一拆解 (修复 Q5/Q7/Q10 inline-pollution bug, v2026-07-10)
                #
                # 原逻辑先跑 multi-line 分支: 把 \n 分隔的行做 line-prefix 检查,
                # ≥2 行匹配就直接 append,跳过 inline-split。但 MinerU 把 4 选项
                # 拆成 "A. ...\nB. ... C. ...\nD. ..." 时,C 嵌在 B 行内,
                # multi-line 只看到 3 行 → 漏 C 并把 C/D 合并到 B。
                #
                # 新逻辑: 用 count_options_outside_formula 数总 label 数,
                #   - ≥2 → extract_options_formula_aware 一刀切 (公式感知,
                #          同时覆盖 line-prefix + inline 两种格式)
                #   - <2 → 整段当单选项
                opt_count = count_options_outside_formula(text)
                if opt_count >= 2:
                    _, opts = extract_options_formula_aware(text)
                    for op in opts:
                        current_question.options.append(post_process_ocr(op))
                else:
                    current_question.options.append(post_process_ocr(text))
                continue

            if current_question:
                # Q9-style: text 块以 "下列说法正确的是( ) \n A. ... B. ... C. ... D. ..."
                # 起头, 不匹配 QUESTION/OPTION/SECTION 任一 pattern,
                # 但含 ≥2 个 A./B./C./D. label → 拆出选项, prose 留作 stem。
                if count_options_outside_formula(text) >= 2:
                    stem_only, opts = extract_options_formula_aware(text)
                    for op in opts:
                        current_question.options.append(post_process_ocr(op))
                    if stem_only:
                        current_question.blocks.append(ContentBlock(
                            block_type="text",
                            content=stem_only,
                            bbox=block.bbox,
                            page_idx=block.page_idx,
                        ))
                else:
                    current_question.blocks.append(block)

        if current_question:
            questions.append(current_question)

        # 处理最后剩余的图片（如果最后一道题之后还有图片）
        if pending_images and questions:
            last_q = questions[-1]
            for img_block, y_pos, page_idx in pending_images:
                img_block.page_idx = page_idx
                last_q.blocks.append(img_block)
            pending_images.clear()

        # 后处理：根据题目文本中引用的图数量，限制每题的图片数
        import re
        for q in questions:
            # 统计题目文本中引用了几张图
            all_text = ''
            for b in q.blocks:
                if b.block_type == "text":
                    all_text += b.content
            # 匹配 "图(a)", "图(b)", "图1", "图2", "图甲", "图乙" 等
            fig_refs = re.findall(r'图\s*[(（]?[a-zA-Z0-9一二三四五甲乙丙丁][)）]?', all_text)
            max_figs = len(fig_refs) if fig_refs else 99  # 没有引用则不限制
            # 保留前 max_figs 张图，移除多余的
            img_blocks = [b for b in q.blocks if b.block_type == "image"]
            if len(img_blocks) > max_figs:
                # 移除多余的图片（保留前 max_figs 张）
                removed = img_blocks[max_figs:]
                q.blocks = [b for b in q.blocks if b not in removed or b.block_type != "image"]
                # 将多余的图片移到下一题（如果有的话）
                q_idx = questions.index(q)
                if q_idx + 1 < len(questions):
                    for img in removed:
                        questions[q_idx + 1].blocks.append(img)

        # 后处理：基于 y 坐标平均值重新分配图片
        # 策略：用题目文本框的 y 中心与图片的 y 中心进行匹配
        # 规则：图片分配给视觉位置最近的题目
        self._reassign_images_by_y_center(questions, debug=self._debug_mode)

        # 后处理：从文本块中提取行内选项
        # 注意：实验题/计算题中可能有"选项格式的描述"（如"A、B两球"），不是真正的选择题选项
        # 实验题保留行内选项，不剥离（小问中的选择题选项要跟着小问走）
        INLINE_OPT_SPLIT = re.compile(r'[)）]\s*(?=[ABCD][\.．、\s])')
        # 无括号的行内选项模式：A. xxx B. xxx C. xxx D. xxx
        INLINE_OPT_NO_PAREN = re.compile(r'(?:(?<=\s)|(?<=^))(?=[ABCD][\.．、]\s)')
        for q in questions:
            if q.options:
                continue
            if q.section_title and "实验" in q.section_title:
                continue
            new_blocks = []
            for block in q.blocks:
                if block.block_type != "text":
                    new_blocks.append(block)
                    continue
                text = block.content
                # 检查是否包含行内选项（优先带括号，其次无括号）
                parts = None
                if re.search(r'[)）]\s*[ABCD][\.．、\s]', text):
                    parts = INLINE_OPT_SPLIT.split(text)
                else:
                    opt_count = len(re.findall(r'(?<!\w)[ABCD][\.．、]\s', text))
                    if opt_count >= 3:
                        parts = re.split(r'(?=\b[ABCD][\.．、]\s)', text)
                if parts:
                    stem_parts = []
                    for part in parts:
                        part = part.strip()
                        if not part:
                            continue
                        if re.match(r'^[ABCD][\.．、\s]', part):
                            is_real_option = True
                            if len(part) > 50:
                                if any(kw in part for kw in ['沿一直线运动', '碰撞前后', '位移-时间图像', '已知', '试求', '求']):
                                    is_real_option = False
                            if is_real_option:
                                # INLINE_OPT_SPLIT 只在 ） 处切一次,后面 "A. .. B. .. C. .. D. .." 仍可能 4 合 1
                                # 数学试卷 Q1 命中: text="...平均数为（）\nA. 8 B. 9 C. 12 D. 18" 切成 stem + 1 个 4 合 1 option
                                # 用 count_options_outside_formula 排除 LaTeX 公式内字母,≥2 时再切一次
                                opt_count = count_options_outside_formula(part)
                                if opt_count >= 2:
                                    sub_parts = re.split(r'(?=[ABCD][．.、\s])', part)
                                    for sub in sub_parts:
                                        sub = sub.strip()
                                        if sub and re.match(r'^[ABCD][．.、\s]', sub):
                                            q.options.append(sub)
                                else:
                                    q.options.append(part)
                            else:
                                stem_parts.append(part)
                        else:
                            stem_parts.append(part)
                    if stem_parts:
                        new_blocks.append(ContentBlock(
                            block_type="text",
                            content=" ".join(stem_parts),
                            bbox=block.bbox,
                            page_idx=block.page_idx,
                        ))
                else:
                    new_blocks.append(block)
            q.blocks = new_blocks

        for q in questions:
            # K3 求:/子问 不再被标 unknown — content_md 里有 "求:" 或
            # "(1) ... (2) ..." 一律当 calculation,避免模板渲染 [?]。
            stem_text = " ".join(
                b.content for b in q.blocks if b.block_type == "text"
            )
            if q.section_title and "实验" in q.section_title:
                q.question_type = "experiment"
            elif q.options:
                # K2-题 2 ABCD 误切: options 来自题干里的 [A-Z] 字母变量而非真选项
                # 守卫: 选项必须 1) 字母顺序 A→D 单调, 2) 每项 > 1 字符非空
                if _looks_like_real_options(q.options):
                    q.question_type = "choice"
                else:
                    q.options = []
                    if _has_calc_hint_in_text(stem_text):
                        q.question_type = "calculation"
                    elif "__" in stem_text or "____" in stem_text:
                        q.question_type = "fill_blank"
                    else:
                        q.question_type = "unknown"  # 让 post_process_md 二次修复
            elif q.section_title and ("计算" in q.section_title or "解答" in q.section_title):
                q.question_type = "calculation"
            elif _has_calc_hint_in_text(stem_text):
                q.question_type = "calculation"
            elif "__" in stem_text or "____" in stem_text:
                q.question_type = "fill_blank"

        return questions

    @staticmethod
    def _get_block_y_center(block: ContentBlock) -> float:
        """获取 block 的 y 中心坐标"""
        if block.bbox and len(block.bbox) >= 4:
            return (block.bbox[1] + block.bbox[3]) / 2
        return block.page_idx * 10000  # 默认为页码 * 10000 表示不同页

    @staticmethod
    def _get_block_x_center(block: ContentBlock) -> float:
        """获取 block 的 x 中心坐标（用于双栏布局）"""
        if block.bbox and len(block.bbox) >= 4:
            return (block.bbox[0] + block.bbox[2]) / 2
        return 500  # 默认居中

    @staticmethod
    def _debug_image_assignment(questions: List[Question]):
        """打印图片分配详情，用于调试"""
        print("  图片分配详情:")
        for i, q in enumerate(questions):
            imgs = [b for b in q.blocks if b.block_type == "image"]
            if imgs:
                y_centers = [MinerUParser._get_block_y_center(b) for b in imgs]
                x_centers = [MinerUParser._get_block_x_center(b) for b in imgs]
                avg_y = sum(y_centers) / len(y_centers)
                avg_x = sum(x_centers) / len(x_centers)
                print(f"    题{i+1}: {len(imgs)}张图, y_center={avg_y:.1f}, x_center={avg_x:.1f}")
            else:
                print(f"    题{i+1}: 无图")

    @staticmethod
    def _validate_image_assignment(questions: List[Question]) -> List[str]:
        """
        验证图片分配是否合理，返回警告列表。

        检查：
        - 图片是否在对应题目的视觉区域内
        - 双栏布局的图片是否错配
        - 图片引用关键字是否匹配
        """
        warnings = []

        for i, q in enumerate(questions):
            imgs = [b for b in q.blocks if b.block_type == "image"]
            if not imgs:
                continue

            # 检查图片引用关键字是否匹配
            all_text = ' '.join(b.content for b in q.blocks if b.block_type == "text")
            has_fig_ref = any(kw in all_text for kw in ['图', '如图', '下图', '附图', '装置', '示意图'])
            has_img = len(imgs) > 0

            if has_img and not has_fig_ref:
                # 有图片但文本中没有图片引用关键字
                warnings.append(f"题{i+1}: 有{len(imgs)}张图但文本中无引用关键字")

            # 检查双栏错配（如果有多个题在同一页，且图片 x 坐标偏离）
            page_imgs = [(img, img.page_idx) for img in imgs]
            if page_imgs:
                page = page_imgs[0][1]
                page_qs = [j for j, q2 in enumerate(questions) if any(
                    b.page_idx == page and b.block_type == "text" for b in q2.blocks
                )]
                if len(page_qs) > 1:
                    # 多题在同一页，检查图片 x 坐标
                    q_text_blocks = [b for b in q.blocks if b.block_type == "text" and b.bbox]
                    if q_text_blocks:
                        q_x_centers = [MinerUParser._get_block_x_center(b) for b in q_text_blocks]
                        q_avg_x = sum(q_x_centers) / len(q_x_centers)
                        for img in imgs:
                            img_x = MinerUParser._get_block_x_center(img)
                            # 如果图片 x 坐标与题目文本 x 坐标差异 > 200，认为可能错配
                            if abs(img_x - q_avg_x) > 200:
                                warnings.append(
                                    f"题{i+1}: 图片 x={img_x:.1f} 与题目 x={q_avg_x:.1f} 差异较大"
                                )

        return warnings

    def _reassign_images_by_y_center(self, questions: List[Question], debug: bool = False):
        """
        基于 y 坐标重新分配图片（改进版）。

        策略：
        - 按页处理图片
        - 对于每张图片，找到"视觉位置最近"的题目
        - 使用图片落在哪个题目的文本 y 范围内来判断
        - 不再使用 avg_y（会被跨页题目误导）
        - 特殊处理孤儿图片（没有 bbox 的图片）
        """
        import re

        if debug:
            print("  图片分配前:")
            self._debug_image_assignment(questions)

        # 收集所有图片（包括孤儿图片）
        all_images = []
        orphan_images = []  # 没有 bbox 的孤儿图片

        for q_idx, q in enumerate(questions):
            for block in q.blocks:
                if block.block_type == "image":
                    has_valid_bbox = block.bbox is not None and len(block.bbox) >= 4
                    if has_valid_bbox:
                        all_images.append({
                            'block': block,
                            'y_center': self._get_block_y_center(block),
                            'y_bottom': block.bbox[3] if block.bbox else 0,
                            'page_idx': block.page_idx,
                            'q_idx': q_idx,
                            'is_orphan': False
                        })
                    else:
                        # 孤儿图片：没有 bbox，需要特殊处理
                        orphan_images.append({
                            'block': block,
                            'page_idx': block.page_idx,
                            'q_idx': q_idx
                        })

        if not all_images and not orphan_images:
            return

        reassign_count = 0

        # 处理有 bbox 的图片（原有逻辑）
        if all_images:
            # 按页处理
            pages = set(img['page_idx'] for img in all_images)

        for page in sorted(pages):
            page_images = [img for img in all_images if img['page_idx'] == page]

            # 收集该页上的所有文本块，按题目分组
            page_text_blocks = []  # [(q_idx, y0, y1, y_center, x_center)]
            for qi, q in enumerate(questions):
                for b in q.blocks:
                    if b.block_type == "text" and b.bbox and b.page_idx == page:
                        y0, y1 = b.bbox[1], b.bbox[3]
                        page_text_blocks.append({
                            'q_idx': qi,
                            'y0': y0,
                            'y1': y1,
                            'y_center': (y0 + y1) / 2,
                            'x_center': (b.bbox[0] + b.bbox[2]) / 2
                        })

            if not page_text_blocks:
                continue

            # 按 y0 排序文本块
            page_text_blocks.sort(key=lambda x: x['y0'])

            for img in page_images:
                img_y = img['y_center']
                img_y_bottom = img['y_bottom']

                # 策略：
                # 1. 如果图片 y_center 落在某个文本块的范围内，归入该题
                # 2. 如果图片在两个文本块之间的间隙，归入"间隙上方"的题目
                #    因为图片通常出现在题干之后，应该归入上方题目

                best_q_idx = None
                best_dist = float('inf')

                # 找图片落在哪两个题之间
                prev_q_idx = None
                prev_y1 = 0

                for tb in page_text_blocks:
                    q_idx = tb['q_idx']
                    y0, y1 = tb['y0'], tb['y1']

                    # 检查图片是否在这个文本块的范围内
                    if y0 <= img_y <= y1:
                        best_q_idx = q_idx
                        best_dist = 0
                        break

                    # 图片在文本块下方
                    if img_y > y1:
                        # 检查是否在当前题和下一题之间
                        if prev_q_idx is not None and prev_y1 < img_y < y0:
                            # 图片在 prev_q 和当前题之间的间隙
                            # 归入 prev_q（因为图片通常在上一题文本之后）
                            best_q_idx = prev_q_idx
                            best_dist = img_y - prev_y1
                            break
                        prev_q_idx = q_idx
                        prev_y1 = y1
                        best_dist = img_y - y1  # 距离当前题底部的距离
                        best_q_idx = q_idx

                    # 图片在文本块上方（图片 y_bottom <= 当前文本 y0）
                    if img_y_bottom <= y0:
                        # 检查是否在上一题和当前题之间
                        if prev_q_idx is not None and prev_y1 < img_y_bottom:
                            # 图片在 prev_q 和当前题之间的间隙
                            # 归入 prev_q
                            best_q_idx = prev_q_idx
                            best_dist = y0 - img_y_bottom
                            break

                # 执行分配
                if best_q_idx is not None and best_q_idx != img['q_idx']:
                    orig_q = questions[img['q_idx']]
                    orig_q.blocks = [b for b in orig_q.blocks if b is not img['block']]
                    questions[best_q_idx].blocks.append(img['block'])
                    reassign_count += 1
                    if debug:
                        print(f"    图片 y={img_y:.0f}: 题{img['q_idx']+1} → 题{best_q_idx+1}")

        # 处理孤儿图片（没有 bbox 的图片）
        # 策略：按页分配给该页第一个引用"如图"的题目
        if orphan_images:
            print(f"  处理 {len(orphan_images)} 张孤儿图片（无 bbox）")
            for orphan in orphan_images:
                page = orphan['page_idx']
                target_q = None

                # 找该页第一个引用"如图"的题目
                for qi, q in enumerate(questions):
                    # 检查该题是否有在该页的文本块
                    has_page_text = any(b.page_idx == page and b.block_type == 'text' for b in q.blocks)
                    if has_page_text:
                        # 检查是否引用"如图"
                        all_text = ' '.join(b.content for b in q.blocks if b.block_type == 'text')
                        if '如图' in all_text:
                            target_q = qi
                            break

                # 如果找到了目标题，分配图片
                if target_q is not None:
                    orig_q_idx = orphan['q_idx']
                    if target_q != orig_q_idx:
                        orig_q = questions[orig_q_idx]
                        orig_q.blocks = [b for b in orig_q.blocks if b is not orphan['block']]
                        questions[target_q].blocks.append(orphan['block'])
                        if debug:
                            print(f"    孤儿图片: 题{orig_q_idx+1} → 题{target_q+1} (页{page+1}, \"如图\"引用)")
                elif debug:
                    print(f"    孤儿图片: 题{orphan['q_idx']+1} (页{page+1}, 未找到目标题)")

        if debug:
            print("  图片分配后:")
            self._debug_image_assignment(questions)
            warnings = self._validate_image_assignment(questions)
            if warnings:
                print("  图片分配警告:")
                for w in warnings:
                    print(f"    - {w}")
            else:
                print("  图片分配验证: OK")

    def _match_images_to_blocks(self, exam: ParsedExam, img_map: dict):
        """将提取的图片匹配到对应的题目"""
        # 简单实现：按页码分配图片到题目
        for block in exam.raw_blocks:
            if block.block_type == "image" and not block.img_path:
                page = block.page_idx
                for q in exam.questions:
                    for qb in q.blocks:
                        if qb.page_idx == page and qb.block_type == "image" and qb.img_path:
                            block.img_path = qb.img_path


# ============================================================
# GLM-4V API 解析引擎（备用路线）
# ============================================================

class GLM4VParser(BaseParser):
    """
    GLM-4V 视觉模型解析引擎
    使用智谱 AI 的 GLM-4V-Flash（免费）进行试卷结构化解析。
    
    流程：
    1. 将 PDF 每页渲染为图片
    2. 调用 GLM-4V API 识别内容
    3. 解析 JSON 输出为 ParsedExam
    """

    API_BASE = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("GLM_API_KEY", "")
        self.model = os.environ.get("GLM_MODEL", "glm-4v-flash")

    def is_available(self) -> bool:
        return bool(self.api_key)

    def parse(self, pdf_path: str, **kwargs) -> ParsedExam:
        """解析 PDF 文件"""
        import fitz
        import base64
        import requests
        from PIL import Image
        import io

        if not self.api_key:
            raise RuntimeError("GLM-4V API key 未设置。请设置环境变量 GLM_API_KEY 或传入 --glm-key")

        doc = fitz.open(pdf_path)
        exam = ParsedExam(page_count=len(doc))

        for pn in range(len(doc)):
            page = doc[pn]
            exam.page_sizes.append((page.rect.width, page.rect.height))

            # 渲染为图片
            mat = fitz.Matrix(2.0, 2.0)
            pix = page.get_pixmap(matrix=mat)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

            # 转为 base64
            buf = io.BytesIO()
            img.save(buf, format="PNG", quality=85)
            img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

            print(f"  📖 GLM-4V 解析第 {pn+1}/{len(doc)} 页...", end=" ", flush=True)

            # 调用 API
            prompt = """请解析这张试卷页面，返回 JSON 格式的结构化内容。
要求：
1. 识别所有题目，包括题号、题干、选项（如有）
2. 数学公式用 LaTeX 格式表示
3. 图片/示意图标记为 [IMAGE] 并描述内容
4. 表格用 HTML 格式表示

返回格式：
{
  "blocks": [
    {"type": "question", "number": 1, "text": "题干内容（含LaTeX公式）", "options": ["A. ...", "B. ..."]},
    {"type": "text", "text": "非题目文本"},
    {"type": "image", "description": "图片描述"},
    {"type": "equation", "latex": "LaTeX公式"}
  ]
}

只返回 JSON，不要其他内容。"""

            resp = requests.post(
                self.API_BASE,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                                {"type": "text", "text": prompt},
                            ],
                        }
                    ],
                    "temperature": 0.1,
                    "max_tokens": 4096,
                },
                timeout=60,
            )

            if resp.status_code != 200:
                print(f"失败 (HTTP {resp.status_code})")
                continue

            result = resp.json()
            content = result["choices"][0]["message"]["content"]

            # 提取 JSON
            try:
                # 尝试直接解析
                parsed = json.loads(content)
            except json.JSONDecodeError:
                # 尝试提取 JSON 块
                import re
                json_match = re.search(r'\{[\s\S]*\}', content)
                if json_match:
                    parsed = json.loads(json_match.group())
                else:
                    print(f"JSON 解析失败")
                    continue

            # 转换为 ContentBlock
            for block_data in parsed.get("blocks", []):
                block_type = block_data.get("type", "text")
                if block_type == "question":
                    q = Question(
                        index=block_data.get("number", 0),
                        label=f"{block_data.get('number', '')}．",
                        options=block_data.get("options", []),
                        question_type="choice" if block_data.get("options") else "unknown",
                    )
                    q.blocks.append(ContentBlock(
                        block_type="text",
                        content=block_data.get("text", ""),
                        page_idx=pn,
                    ))
                    exam.questions.append(q)
                elif block_type == "text":
                    exam.raw_blocks.append(ContentBlock(
                        block_type="text",
                        content=block_data.get("text", ""),
                        page_idx=pn,
                    ))
                elif block_type == "equation":
                    exam.raw_blocks.append(ContentBlock(
                        block_type="equation",
                        content=block_data.get("latex", ""),
                        page_idx=pn,
                    ))
                elif block_type == "image":
                    exam.raw_blocks.append(ContentBlock(
                        block_type="image",
                        content=block_data.get("description", ""),
                        page_idx=pn,
                    ))

            print("完成")

        doc.close()
        exam.parser_used = f"glm-4v-{self.model}"
        return exam

