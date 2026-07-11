"""v2 数据结构 — ContentBlock, Question, ParsedExam"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Tuple


@dataclass
class ContentBlock:
    """一个内容块（文本/公式/图片/表格）"""
    block_type: str       # "text", "equation", "image", "table"
    content: str          # 文本内容 / LaTeX公式 / 图片路径 / HTML表格
    bbox: Optional[List[float]] = None  # [x0, y0, x1, y1] 归一化坐标 (0-1000)
    page_idx: int = 0
    # 图片相关
    img_path: Optional[str] = None
    caption: Optional[str] = None
    # 文本相关
    text_level: int = 0   # 0=正文, 1=一级标题, 2=二级标题...
    # 公式相关
    is_inline: bool = False  # 是否为行内公式
    # 修 L-2: 显式 column 字段替代 text_level 重载 (0=左栏, 1=右栏, -1=未设)
    column: int = -1


@dataclass
class Question:
    """一道完整的题目"""
    index: int                    # 题号（从1开始）
    label: str                    # 题目标签（如 "1．" 或 "二、实验题"）
    section_title: Optional[str] = None  # 所属大题标题
    blocks: List[ContentBlock] = field(default_factory=list)
    question_type: str = "unknown"  # "choice", "fill_blank", "calculation", "experiment", "unknown"
    options: List[str] = field(default_factory=list)  # 选择题选项 A/B/C/D
    source_page: int = 0          # 题目来源页（用于排序）
    column: int = 0               # 栏位：0=左栏, 1=右栏（用于 A3 双栏）
    is_multi_select: Optional[bool] = None  # v0.12.2 — True=多选, False=单选, None=未知


@dataclass
class ParsedExam:
    """解析后的完整试卷"""
    title: str = ""
    questions: List[Question] = field(default_factory=list)
    raw_blocks: List[ContentBlock] = field(default_factory=list)
    page_count: int = 0
    page_sizes: List[Tuple[float, float]] = field(default_factory=list)
    parser_used: str = ""
    # v0.12.2 — 卷头识别结果,只在用卷头定位"单选/多选范围"的试卷里非空。
    # 形如 {page_idx (0-based): {"single": [1,2,3,4,5,6,7], "multi": [8,9,10]}}
    # 一张卷子多个 page_idx 可同时有区间 (湖北卷头常 1 次性把多选题范围写在 page=1)。
    page_question_layout: Dict[int, Dict[str, List[int]]] = field(default_factory=dict)
    # 修 M-9: 跟踪解析过程中生成的临时图片文件路径, 上层 pipeline 在 HTML
    # 渲染完成后调 cleanup_temp_files() 删除孤儿。
    _temp_files: List[str] = field(default_factory=list)

    def cleanup_temp_files(self) -> int:
        """删除解析过程中注册的临时文件, 返回成功删除数.

        修 M-9: 上层 pipeline 在 HTML 渲染 + courseware/images 接管后调用, 避免
        长期运行填满磁盘。被 topic_garden 复制的图(td_path 仍存在)正常 unlink。
        """
        import os
        deleted = 0
        for p in list(self._temp_files):
            try:
                if os.path.exists(p):
                    os.unlink(p)
                    deleted += 1
            except OSError:
                pass
            self._temp_files.clear()
        return deleted


