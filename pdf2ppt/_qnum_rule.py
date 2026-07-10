"""
基于 PyMuPDF dict mode 的 font-aware 题号识别规则。

背景: chem/phys/bio engine cascade 假阳的根因之一是 OCR / 文本提取把题干里的数字
(如化学式 "SO4²⁻" 中的 "4"、"0.05" 实验数据) 误识别为新题号。

规则 (基于 chem-sanyuelk + chem-qishi 全卷 probe 数据归纳):
  R1. 必须匹配 QUESTION_START pattern (数字 + .／／、)
  R2. 字号在正文范围 (10-12pt), 排除上标/下标 (6.3pt)
  R3. 题号后有中文 → 真题号 (含温度单位 ℃℉ 时放宽)
  R4. 题号后纯数字 → 假 (实验数据 "0. 05")
  R4b. 题号后数字开头,无中文 → 假 ('0.848 g·cm');
        数字开头有中文 (含℃℉) → 真 ('14. 25℃时...')
  R5. 题号后空 ('19.') → 看同 block 内 / 后续 3 blocks 有中文就视为真
  R6. 卷头说明 (page 0 + 匹配 INSTRUCTION_PAT) → 假

实测效果 (chem 4 PDF):
  - chem-wuhanyy / chem-shiyixiao / chem-qishi / chem-sanyuelk: 19/19 ✅ 0 假阳 0 漏检
  - chem-qishi page 6 上 6 个假 Q0 ('0. 05'/'0. 1'/'0. 2') → 全部正确过滤
  - chem-qishi Q19 (line 只有 '19.') → 通过 R5 正确识别

用法:
  from pdf2ppt._qnum_rule import extract_real_question_numbers
  nums = extract_real_question_numbers("test.pdf")  # 默认 chem 规则
  # 物理/生物引擎可传入自定义规则:
  from pdf2ppt._qnum_rule import QNumRule
  rule = QNumRule(question_start_re=MY_PATTERN, instruction_re=MY_INSTRUCTION)
  nums = rule.extract("test.pdf")
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

import fitz  # PyMuPDF

from ._chem_text import QUESTION_START, _INSTRUCTION_PAT, _PAGE_NUM_PAT  # type: ignore


# Body font size range (chem 试卷典型 10-12pt)
_BODY_FONT_MIN = 10.0
_BODY_FONT_MAX = 12.0

# 题号后纯数字判定 — 实验数据 "0. 05" / "0.1" 之类
_DECIMAL_ONLY_RE = re.compile(r'^\s*\d+(\.\s*\d+)?\s*$')

# 数字开头后第一个字符是数字 (更宽的实验数据 / 小数检测)
# "0.848 g·cm", "1.14 mol/L" 等化学式量
_AFTER_STARTS_WITH_DIGIT_RE = re.compile(r'^\s*\d')

# 4 位年份 (1900-2099) — 题号点后跟年份时仍应识别为真题号
# 例如 "4.2026 年1 月16 日..." 中 "4" 是 Q4,"2026" 是年份
_YEAR_RE = re.compile(r'^(19\d{2}|20\d{2})')

# 增强版 QUESTION_START pattern: 排除小数 (题号点后紧跟数字,且不是年份)
# 例如 "0.2" "0.848" "1.14" 等化学/物理数据值,不应当成题号
QUESTION_START_STRICT = re.compile(r'^\s*(\d{1,2})\s*[\.．、](?!\d)')

# 中文 (严格 — 不含英文/单位符号,避免 "0.848 g·cm" 误判)
_HAS_CHINESE_RE = re.compile(r'[一-鿿]')

# 中文 + 温度单位 (℃℉) — 用于题号后含 "25℃" 这种数字+单位的判断
_HAS_CHINESE_OR_TEMP_RE = re.compile(r'[一-鿿℃℉]')

# 数字开头后前 N 字符内是否有中文/温度单位
# 区别于 "12kg 的玩具" (12kg 后隔 1 字符到" 的") vs "25℃时" (25 紧接 ℃)
# 前 N 字符内有中文 → 真题号 ("14. 25℃时,...")
# 前 N 字符内无中文 → 数据值 ("0.12kg 的玩具...")
# 选 6 是因为 "2026 年1 月16 日" 需 5 字符到 " 年",而 "0.12kg" 6 字符仍无中文
_NEAR_DIGIT_MAX_CHARS = 6

# 中文/字母 (宽 — 用于 block 内查 Chinese 等场合)
_HAS_CONTENT_RE = re.compile(r'[一-鿿a-zA-Z]')


@dataclass
class QNumRule:
    """Font-aware 题号识别规则集 (chem/phys/bio 通用)

    Attributes:
        question_start_re: 匹配题号 pattern (默认 STRICT 版本,排除 "0.2" 等小数)
        instruction_re: 匹配卷头说明 pattern (默认 chem_text 的 _INSTRUCTION_PAT)
        body_font_min/max: 正文字号范围 (默认 10-12pt)
    """
    question_start_re: re.Pattern = field(default_factory=lambda: QUESTION_START_STRICT)
    instruction_re: re.Pattern = field(default_factory=lambda: _INSTRUCTION_PAT)
    body_font_min: float = _BODY_FONT_MIN
    body_font_max: float = _BODY_FONT_MAX

    def has_chinese_in_next_n_blocks(self, blocks: list, current_block_idx: int, n: int = 3) -> bool:
        """看后续 n 个 text blocks (跳过 image blocks) 是否有中文"""
        checked = 0
        for bi in range(current_block_idx + 1, len(blocks)):
            b = blocks[bi]
            if b.get("type") != 0:
                continue
            text = ""
            for line in b.get("lines", []):
                for s in line.get("spans", []):
                    text += s.get("text", "")
            if _HAS_CHINESE_RE.search(text):
                return True
            checked += 1
            if checked >= n:
                break
        return False

    @staticmethod
    def _is_formula_continuation(prev_line: dict, current_text: str) -> bool:
        """检测当前行是否是公式延续 (前一行以 = / + / - / × / ÷ 结尾)

        典型: 前行 "m=" + 当前行 "0.1kg，..." → "0.1kg" 不应被识别为题号

        启发式:
        - 前一行必须是多字符 (≥3) → 排除单字符 "-" / "+" / "—" 这类分隔符
        - 前一行末尾必须是数学符号 (=+×÷∗／) 或 全角减号 (−)
        - 当前行开头通常是数字 (data value)
        """
        if not prev_line:
            return False
        prev_text = "".join(s.get("text", "") for s in prev_line.get("spans", []))
        prev_text = prev_text.rstrip()
        if not prev_text or len(prev_text) < 3:
            return False
        # 前一行末尾是数学符号 (常见 PDF 公式换行位置)
        return bool(re.search(r'[=+×÷∗／−]\s*$', prev_text))

    def _match_question_start(self, text: str):
        """匹配题号 pattern,带年份特例

        Returns:
            re.Match 或 None

        处理顺序:
          1. strict regex (默认) — 拒绝 "0.2" "0.848" 等小数数据
          2. 若 strict 拒绝,尝试 loose + 年份特例 (1900-2099)
             — "4.2026 年1 月16 日" 中 "4" 是 Q4
        """
        m = self.question_start_re.match(text)
        if m:
            return m
        loose_m = QUESTION_START.match(text)
        if loose_m:
            after_loose = text[loose_m.end():]
            if _YEAR_RE.match(after_loose):
                return loose_m
        return None

    def is_real_question_number(
        self,
        line: dict,
        block: dict,
        line_idx_in_block: int,
        block_idx: int,
        all_blocks: list,
        page_num: int = 0,
    ) -> bool:
        """判断 line 是否是真题号

        Args:
            line: PyMuPDF dict mode 的 line dict
            block: 当前 line 所属 block
            line_idx_in_block: line 在 block 中的 index (从 0 开始)
            block_idx: block 在 page dict 中的 index
            all_blocks: 整页所有 blocks (text + image)
            page_num: 当前页码 (0-indexed),用于卷头说明过滤
        """
        spans = line.get("spans", [])
        if not spans:
            return False
        text = "".join(s.get("text", "") for s in spans).strip()
        if not text:
            return False

        # R0: 公式延续检测 (前一行以 = / + / - 结尾 → 当前行是公式部分,不是题号)
        # 必须在 R1 之前,避免 "m=" + "0.1kg" 被误识为 Q0
        # 只看当前 block 的前一行 (跨 block 检查会误伤正常 Q 边界)
        if line_idx_in_block > 0:
            prev_line = block["lines"][line_idx_in_block - 1]
            if self._is_formula_continuation(prev_line, text):
                return False

        # R1: 匹配 question_start pattern
        m = self.question_start_re.match(text)
        if not m:
            # 退化检查: "N.YYYY" (题号+年份) 可能被 strict regex 拒绝
            # 例如 "4.2026 年1 月16 日" — "4" 是 Q4, 但 strict regex 拒绝 "4.2026"
            # 用宽松 pattern 重新匹配 + 年份特例
            loose_m = QUESTION_START.match(text)
            if loose_m:
                after_loose = text[loose_m.end():]
                year_m = _YEAR_RE.match(after_loose)
                if year_m:
                    m = loose_m  # 年份特例: 接受为真题号
                else:
                    return False
            else:
                return False

        # R2: 字号在正文范围
        size = spans[0].get("size", 0)
        if not (self.body_font_min <= size <= self.body_font_max):
            return False

        # R6b: 卷头说明排除 (仅 page 0)
        if page_num == 0 and self.instruction_re.search(text):
            return False

        # R3/R4/R5: 根据题号后内容判断
        after = text[m.end():]

        if _AFTER_STARTS_WITH_DIGIT_RE.match(after):
            # R4b: 题号后数字开头
            #   - 数字+℃ + 中文 → 真 ("14. 25℃时,..." — 25 紧接℃)
            #   - 数字+非数字非中文 (%等) → 假 ("0. 5%淀" — % 是单位标签, 非题目)
            #   - 纯数字+空格+中文 → 假 ("0. 1 淀粉" — 0.1 是数据值)
            near = after.lstrip()
            # 找数字序列结束位置,看紧跟字符
            digit_end = 0
            while digit_end < len(near) and (near[digit_end].isdigit() or near[digit_end] in '. '):
                digit_end += 1
            after_digits = near[digit_end:digit_end + 1]  # 数字后第一个字符
            if after_digits and not after_digits.isascii():
                # 数字后紧跟非 ASCII (℃/中文等) → 可能是真题号
                # 再检查前 6 字符是否有中文/℃
                check = near[:_NEAR_DIGIT_MAX_CHARS]
                if _HAS_CHINESE_OR_TEMP_RE.search(check):
                    return True
                return False
            else:
                # 数字后是 ASCII (空格/%/字母等) → 数据值
                return False

        if _DECIMAL_ONLY_RE.match(after.strip()):
            # R4: 题号后纯数字 (含小数) → 假 (例如 "0. 05")
            return False

        if _HAS_CHINESE_RE.search(after):
            # R3: 题号后有中文 → 真 (例如 "6. 布洛芬...")
            return True

        # R5: 题号后空 ("19.") → 看后续 (block 内 / 后 block)
        if not after.strip():
            # 先看同 block 内其他 line 是否有中文 (chem-shiyixiao: "1." + 后续 Chinese stem)
            same_block_text = ""
            for ln in block.get("lines", []):
                for s in ln.get("spans", []):
                    same_block_text += s.get("text", "")
            if _HAS_CHINESE_RE.search(same_block_text):
                return True
            # 否则看后续 blocks (chem-qishi Q19: 整个 block 0 只有 "19.")
            return self.has_chinese_in_next_n_blocks(all_blocks, block_idx, n=3)

        # 其他情况: 题号后只有标点/空白 → 默认视为真 (保守)
        return True

    def extract(self, pdf_path: str) -> list[dict]:
        """从 PDF 抽所有真题号 (用于 truth 提取或 chem engine sanity check)

        Returns:
            list of {num, page, y, bbox, text}
        """
        doc = fitz.open(pdf_path)
        results: list[dict] = []
        seen_nums: set[tuple[int, int]] = set()

        for pn in range(len(doc)):
            page = doc[pn]
            d = page.get_text("dict")
            blocks = d.get("blocks", [])
            for bi, block in enumerate(blocks):
                if block.get("type") != 0:
                    continue
                for li, line in enumerate(block.get("lines", [])):
                    if not self.is_real_question_number(line, block, li, bi, blocks, page_num=pn):
                        continue
                    spans = line.get("spans", [])
                    text = "".join(s.get("text", "") for s in spans).strip()
                    m = self._match_question_start(text)
                    if not m:
                        continue
                    qnum_str = m.group(1)
                    qnum = int(qnum_str.translate(str.maketrans('０１２３４５６７８９', '0123456789')))
                    key = (pn, qnum)
                    if key in seen_nums:
                        continue
                    seen_nums.add(key)
                    results.append({
                        "num": qnum,
                        "page": pn,
                        "y": line.get("bbox", [0, 0, 0, 0])[1],
                        "bbox": line.get("bbox"),
                        "text": text[:60],
                    })

        doc.close()
        results.sort(key=lambda r: (r["page"], r["y"]))
        return results


# 默认实例 (chem 规则)
_DEFAULT_RULE = QNumRule()


# 兼容旧 API: 直接调用 _DEFAULT_RULE.is_real_question_number
def is_real_question_number(*args, **kwargs) -> bool:
    """[兼容旧 API] 使用默认 chem 规则判断"""
    return _DEFAULT_RULE.is_real_question_number(*args, **kwargs)


def extract_real_question_numbers(pdf_path: str) -> list[dict]:
    """[兼容旧 API] 使用默认 chem 规则抽取真题号"""
    return _DEFAULT_RULE.extract(pdf_path)