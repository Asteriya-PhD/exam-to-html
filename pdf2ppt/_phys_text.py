"""
物理引擎文本工具:OCR 后处理 + 文本块清洗 + Markdown 预处理 + 选项计数。

把原 _v2_parser.py 的纯函数抽到这里,便于独立 unit-test。
所有函数零外部依赖(除 re)。
"""
import re


def post_process_ocr(text: str) -> str:
    """后处理 OCR 文本,清理 MinerU 常见乱码字符。

    修复:
    - `??` / `?!` / 独立 `?` / `!` 误识别 → 替换为全角 `？` / `！`(若后跟中文)
    """
    if not text:
        return text
    text = re.sub(r'\?\?(?=[一-鿿])', '？', text)    # ??后跟中文 → ？
    text = re.sub(r'\?!(?=[一-鿿])', '！', text)     # ?!后跟中文 → ！
    text = re.sub(r'\?\?(?!\?)', '？', text)                  # 独立 ?? → ？
    text = re.sub(r'!(?![\?!])', '！', text)                  # 独立 ! → ！
    return text


def count_options_outside_formula(text: str) -> int:
    """统计文本中选项字母数量(A. B. C. D.),排除 LaTeX 公式内的字母。"""
    if not text:
        return 0
    count = 0
    parts = re.split(r'\$[^$]*\$', text)
    for part in parts:
        count += len(re.findall(r'[ABCD]\.', part))
    return count


def extract_options_formula_aware(text: str):
    """公式感知地拆出 A./B./C./D. 选项 (v2026-07-10)。

    Returns:
        (stem_only, options) — stem_only 是 text 中所有 A./B./C./D. label
        之前的部分(含公式段); options 是按出现顺序拆出的 4 选项字符串(已 strip)。

    算法:
      1. 把 text 按 `$...$` 切成交替段
      2. 公式段不切, 跟随其前/后 text 段归入 stem 或 option
      3. 仅在 non-formula 段上做 inline-split `(?=[ABCD][.．、\\s])`

    修 Q4/Q2/Q9: 直接 split 全文会把 "$A 、 B$" 这种公式内 A 误识为选项头,
    也会把 A 选项后的公式内容 ($...$) 留作 stem 而非 option content。
    """
    if not text:
        return text, []
    tokens = re.split(r'(\$[^$]*\$)', text)

    # 状态: 'stem' (没遇到 option label) 或 'opts' (已经开始拆选项)
    state = 'stem'
    stem_parts = []
    opts = []
    cur_opt_parts = None  # 当前正在累积的 option 字符串 list

    def flush_opt():
        nonlocal cur_opt_parts
        if cur_opt_parts is not None:
            joined = ''.join(cur_opt_parts).strip()
            if joined and re.match(r'^[ABCD][．.、]', joined):
                opts.append(joined)
            cur_opt_parts = None

    for tok in tokens:
        if not tok:
            continue
        is_formula = tok.startswith('$') and tok.endswith('$')
        if is_formula:
            # 公式段跟随当前状态
            if state == 'stem':
                stem_parts.append(tok)
            else:
                cur_opt_parts.append(tok)
            continue
        # Text 段: 在 non-formula 上做 inline split
        sub = re.split(r'(?=[ABCD][．.、\s])', tok)
        for seg in sub:
            if not seg:
                continue
            if state == 'stem':
                stripped = seg.strip()
                if stripped and re.match(r'^[ABCD][．.、]', stripped):
                    # 进入 opts 状态
                    state = 'opts'
                    cur_opt_parts = [seg]
                else:
                    stem_parts.append(seg)
            else:
                # 在 opts 状态: 此段以 A./B./C./D. 开头 → 新的 option
                # 先 flush 旧的, 再开新的
                flush_opt()
                cur_opt_parts = [seg]

    flush_opt()
    stem_only = ''.join(stem_parts).rstrip()
    return stem_only, opts


def clean_text_block(text: str) -> str:
    """清洗文本块:跑 post_process_ocr + 合并连续空格 + 剥空白。

    保留策略:含 3+ 中文字符的文本块视为有效,即使有 ? 也保留。
    """
    if not text:
        return text
    text = post_process_ocr(text)
    text = re.sub(r' {3,}', '  ', text)  # 合并连续空格(保留 2 个)
    if re.search(r'[一-鿿]{3,}', text):
        return text.strip()
    return text.strip()


def preprocess_markdown(md: str) -> str:
    """Markdown 预处理:修复 Flash Extract 的常见输出问题(占位 _v2_parser._preprocess_markdown)。"""
    return md
