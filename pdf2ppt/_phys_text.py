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
