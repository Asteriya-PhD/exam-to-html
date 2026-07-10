"""
物理引擎 region 后处理:大题分类标题剥除。

物理 parser 已经把 '一、选择题' / '二、非选择题' 等存到 Question.section_title 字段,
HTMLTypesetter 不渲染这个字段,所以目前无 banner 泄漏。
本模块提供**防御层** + 文本层 banner 剥除(应对未来 parser 改回 text-based banner):

- strip_section_headers_from_text(text) -> str
  单段文本扫描,剥除 SECTION_HEADER 模式行
- strip_section_headers_from_exam(exam) -> None
  对 ParsedExam 的每道题每个 block 跑 strip_section_headers_from_text
  (section_title 字段保留,只有 block.content 会被剥)
"""
import re

# 复用 chem 的 SECTION_HEADER regex(已扩展支持常见 banner 模式)
from ._chem_text import SECTION_HEADER


def strip_section_headers_from_text(text: str) -> str:
    """从单段文本剥除大题分类标题行。

    行为: 按行扫描,匹配 SECTION_HEADER 的行从 content 中删除。
    """
    if not text:
        return text
    lines = text.split("\n")
    kept = []
    for line in lines:
        if SECTION_HEADER.search(line):
            continue
        kept.append(line)
    if len(kept) < len(lines):
        return "\n".join(kept)
    return text


def strip_section_headers_from_exam(exam) -> None:
    """对整个 ParsedExam 剥除大题分类标题(in-place)。

    section_title 字段保留(供 PPTX 分组用),但 block.content 里的 banner
    文字会被剥除(防御层,当前 HTMLTypesetter 不会渲染,但万一以后改)。
    """
    for q in exam.questions:
        from ._v2_models import ContentBlock
        new_blocks = []
        for b in q.blocks:
            if b.block_type == "text" and b.content:
                new_content = strip_section_headers_from_text(b.content)
                # 创建新 block(避免共享引用)
                new_b = ContentBlock(
                    block_type=b.block_type,
                    content=new_content,
                    bbox=b.bbox,
                    page_idx=b.page_idx,
                    img_path=b.img_path,
                    caption=b.caption,
                    text_level=b.text_level,
                    is_inline=b.is_inline,
                )
                new_blocks.append(new_b)
            else:
                new_blocks.append(b)
        q.blocks = new_blocks
