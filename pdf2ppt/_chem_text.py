"""
化学引擎文本处理:全部纯函数,无外部依赖,unit-test 友好。

包含:
- 题号 / 选项 / 大题标题 regex
- 公式截断 / 公式断裂 regex
- 卷头 / 页脚 / 注意事项 regex
- clean_text / classify_question / is_complex_equation / parse_choice_text
"""
import re
from typing import List, Dict, Optional


# ============================================================
# Regex 常量
# ============================================================

# 行首题号:"1." "12．" "5、"
QUESTION_START = re.compile(r'^\s*(\d{1,2})\s*[\.．、]')

# 行中/末题号 + 高分题分数标记(16~25 题且带分数)
QUESTION_MID = re.compile(
    r'(?:^|\n)\s*(?<![A-Za-z一-鿿])(\d{1,2})\s*[\.．、]'
    r'|(?<!\d)(1[6-9]|2[0-5])[\s\.．、]*[（(]\s*\d+\s*分?\s*[）)]'
)

# KaTeX 圈号:$\textcircled{1}$ → <span class="circled">1</span>
TEXTCIRCLED = re.compile(r'\$\\textcircled\{(\d+)\}\$')

# 大题分类标题: "第Ⅱ卷" "一、选择题" "二、非选择题" "三、实验题" 等
# 用 search 而非 match — 允许 banner 嵌在行内(如 "一、选择题\n1. 下列...")
SECTION_HEADER = re.compile(
    r'(?:'
    r'第\s*[IVXⅠⅡⅢⅣ\d一二三四五六七八九十]+\s*卷|'           # 第Ⅰ卷 第II卷 第1卷 等
    r'[一二三四五六七八九十]\s*[、,]\s*[^。\n]*?(?:小题|选择题|非选择题|实验题|填空题|计算题|综合题|推断题|问答题)|'
    r'本\s*(?:试卷|试题卷)\s*共\s*\d+\s*(?:题|小题|页)|'      # 本试卷共 X 题
    r'可能用到的(?:相对)?原子质量|'                              # 可能用到的原子质量
    r'注意事项:|考生注意:|'
    r'满分\s*\d+\s*分|考试(?:用时|时长)\s*\d+\s*分钟'
    r')'
)

# 选项字母行首匹配:"A." "B．" "C、"
OPTION_PAT = re.compile(r'(?:^|\n)\s*([A-D])[\.．、]')

# 复杂公式:含反应箭头 / 可逆反应
_COMPLEX_EQ_PAT = re.compile(
    r'\\x(?:rightarrow|leftarrow|rightleftharpoons|leftrightarrow)'
    r'|\\rightleftharpoons|→|⇌'
)

# 公式被截断(以 \mathrm 或 \mathrm{xxx 结尾)
_TRUNCATED_FORMULA = re.compile(r'\\mathrm(?:\{[^}]*)?$')

# 公式断裂(空格出现在每个 token 之间,如 \mathrm {C} _ {6})
_SPACED_FORMULA = re.compile(r'\\[a-zA-Z]+\s+\{[A-Z]\}\s+_\s+\{')

# 卷头说明 / 注意事项 / 选择题作答说明
# 旧版用 ^(?:[1-4][.．、]\s*)? 前缀,要求 keyword 紧跟 "N. ",但真实 instruction
# 常是 "2. 请按题号顺序在答题卡上各题目的答题区域内作答" — keyword 在描述之后。
# 改用纯 search 匹配,关键词在 text 中任意位置出现即视为 instruction。
_INSTRUCTION_PAT = re.compile(
    r'(?:'
    r'答题前|选择题的作答|非选择题的作答|考试结束后|'
    r'准考证号|条形码|答题卡|本卷共|本试卷共|本试题卷共|全卷满分|考试用时|'
    r'注意事项|考生在答题前|并将准考证号|'
    r'满分\s*\d+|用时\s*\d+|分钟|'
    r'可能用到的|相对原子质量|学科|'
    r'第\s*[IVXⅠⅡⅢⅣ一二三四五六七八九十]+\s*卷|'
    r'[一二三四五]、\s*(?:选择题|非选择题|实验题|填空题|综合题)|'
    r'考生注意|作答须|'
    # 高三卷头常见表述 — 2026-06-05 chem-qishi/chem-gaosan1 漏抓
    r'请按题号顺序|请按题号|用\s*2B\s*铅笔|黑色签字笔|'
    r'笔迹清楚|字体工整|写在\s*(?:试卷|草稿)|非答题区域|'
    r'答\s*题\s*区\s*域内|所选.{0,4}答案的标号'
    r')'
)

# 页脚/页码多种格式
_PAGE_NUM_PAT = re.compile(
    r'^[\s\-—]*\d+\s*[/／of]\s*\d+[\s\-—]*$|'  # - 1/6 - / 1 of 6
    r'^[\s\-—]*第\s*\d+\s*页[\s\-—]*$|'           # 第 1 页
    r'^[\s\-—]*Page\s*\d+[\s\-—]*$|'              # Page 1
    r'^[\s\-—]*\d+[\s\-—]*$'                       # - 1 -
)


# ============================================================
# 纯函数
# ============================================================

def clean_text(content: str) -> str:
    """剥离 HTML 标签,返回纯文本。"""
    return re.sub(r'<[^>]+>', '', content)


def classify_question(q: dict) -> str:
    """根据题目内容判断是 choice 还是 free_response。

    调用时机:所有 region 跨页归位完成之后(expand_free_response 之后)。
    判定顺序很关键——必须先看 free_response 信号("回答下列" / (1)(2)(3)),
    否则"下列说法正确的是 + 4 个 ABCD 选项"的主观判断题会被错分为 choice。
    """
    all_text = " ".join(
        clean_text(r.get("content", ""))
        for r in q["regions"]
        if r.get("label") == "text"
    )
    # 1) 显式 free_response 信号
    if re.search(r'回答\s*下列\s*(?:问题|各题)', all_text):
        return "free_response"
    # 2) 多个不同的 (1)(2)(3) 子问标记
    subq = re.findall(r'[\(（]\s*([1-9])\s*[\)）]', all_text)
    if len(set(subq)) >= 2:
        return "free_response"
    # 3) 4 个不同的 ABCD 选项 → choice
    opt_letters = set(re.findall(r'(?:^|\n)\s*([A-D])\s*[\.．、]', all_text))
    if len(opt_letters) >= 3:
        return "choice"
    # 4) 默认
    return "choice"


def is_complex_equation(raw_clean: str) -> bool:
    """判断文本是否含复杂公式需要截图。

    启发式:
    - $...$ 内容占总字符 < 60% → 题目描述,不是公式区
    - 含 \\xrightleftharpoons / → / ⇌ → 反应方程式
    - 以 \\mathrm 结尾 → 公式被截断
    - 多个紧贴的 $...$ 块 → 严重碎片化
    """
    # 计算 $...$ 内容占总字符比例,低于 60% → 是题目描述而非公式区
    dollar_chars = sum(
        len(m.group()) for m in re.finditer(r'\$[^$]*\$', raw_clean)
    )
    if dollar_chars / max(len(raw_clean), 1) < 0.6:
        return False
    frag_pos = [m.end() for m in re.finditer(r'\$\s*\\mathrm\{[^}]*\}\s*\$', raw_clean)]
    is_fragmented = any(
        frag_pos[i+1] - frag_pos[i] < 30
        for i in range(len(frag_pos) - 1)
    )
    dollar_blocks = [(m.start(), m.end()) for m in re.finditer(r'\$[^$]*\$', raw_clean)]
    is_tight = len(dollar_blocks) >= 3 and all(
        dollar_blocks[i+1][0] - dollar_blocks[i][1] < 10
        for i in range(len(dollar_blocks) - 1)
    )
    return bool(
        _COMPLEX_EQ_PAT.search(raw_clean)
        or _TRUNCATED_FORMULA.search(raw_clean)
        or is_fragmented or is_tight
        or _SPACED_FORMULA.search(raw_clean)
    )


def parse_choice_text(content: str) -> list:
    """将选择题文本解析为 [{type, letter, text, html}, ...] 列表。

    关键点:仅在行首或空白前分割选项字母,避免反应流程中 "C." 被误判为选项。
    """
    clean_raw = clean_text(content)
    clean = re.sub(r'</?div[^>]*>', '', clean_raw)
    clean = TEXTCIRCLED.sub(r'<span class="circled">\1</span>', clean)

    lines = clean.split('\n')
    expanded = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # 仅在行首或空白前分割选项字母(避免反应流程中 "C." 被误判为选项)
        # 标准格式 "A.xxx  B.yyy  C.zzz" → 正确分割
        # 反应流程 "CO₂ + H₂O → B.xxx" → 不分割(B 前是 : 而非空白)
        inline_parts = re.split(r'(?:(?<=^)|(?<=\s))(?=[A-D][.．、])', stripped)
        # 后处理:合并被误拆的反应流程标签(如 "→ B." 中 B 不是选项)
        merged = [inline_parts[0]]
        for part in inline_parts[1:]:
            m = OPTION_PAT.match(part.strip())
            if m and merged:
                prev = merged[-1].strip()
                prev_end = prev[-3:] if len(prev) >= 3 else prev
                # 若前一段以反应箭头/化学式结尾,则这不是选项,合并回去
                if any(prev_end.endswith(s) for s in ('→', '⇌', '=', '>', '<', ':')):
                    merged[-1] += part
                    continue
            merged.append(part)
        inline_parts = merged
        found_opts = [OPTION_PAT.match(s.strip()) for s in inline_parts if OPTION_PAT.match(s.strip())]
        line_starts_with_opt = bool(OPTION_PAT.match(stripped))
        should_split = False
        if len(found_opts) > 1:
            if not line_starts_with_opt and found_opts[0].group(1) != 'A':
                should_split = False
            else:
                LUT = {'A':0,'B':1,'C':2,'D':3,'E':4,'F':5}
                indices = [LUT[s.group(1)] for s in found_opts if s.group(1) in LUT]
                should_split = all(indices[i+1] - indices[i] == 1 for i in range(len(indices) - 1))
        elif found_opts and (line_starts_with_opt or found_opts[0].group(1) == 'A'):
            should_split = True
        if should_split:
            for s in inline_parts:
                s = s.strip()
                if s:
                    expanded.append(s)
        else:
            expanded.append(stripped)

    # 去重:相同字母的多个候选保留最后一个;如果前一项不是选项就合并到前一项
    deduped = []
    seen_letters = {}
    for item in expanded:
        m = OPTION_PAT.match(item)
        if m and m.group(1) in seen_letters:
            first_idx = seen_letters[m.group(1)]
            if first_idx > 0:
                deduped[first_idx - 1] += deduped[first_idx]
            deduped[first_idx] = item
        else:
            if m:
                seen_letters[m.group(1)] = len(deduped)
            deduped.append(item)
    expanded = deduped

    result = []
    for line in expanded:
        m = OPTION_PAT.match(line)
        if m:
            rest = line[len(m.group(0)):]
            result.append({'type': 'opt', 'letter': m.group(1), 'text': rest})
        else:
            result.append({'type': 'text', 'text': line})
    # 非选项行合并到前一个选项
    merged = []
    for item in result:
        if item['type'] == 'text' and merged and merged[-1]['type'] == 'opt':
            merged[-1]['text'] += item['text']
        else:
            merged.append(item)

    out = []
    for item in merged:
        if item['type'] == 'opt':
            bare = not item['text'].strip()  # 纯字母(选项内容是图片)
            html = f'<p class="opt"><b>{item["letter"]}.</b> {item["text"]}</p>'
            out.append({'html': html, 'opt_letter': item['letter'], 'bare': bare})
        else:
            html = f'<p>{item["text"]}</p>'
            out.append({'html': html, 'opt_letter': None, 'bare': False})
    return out
