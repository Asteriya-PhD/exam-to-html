"""A3 双栏试卷裁切工具"""

import os
import tempfile
import fitz  # PyMuPDF


class A3Splitter:
    """
    将 A3 双栏 PDF 裁切为两个 A4 PDF（左右栏各一个）

    A3 试卷（1191×842 pt）从中间裁切后：
    - 左栏：0-595 pt (≈ A4)
    - 右栏：596-1191 pt (≈ A4)

    使用方式：
    >>> splitter = A3Splitter()
    >>> left_pdf, right_pdf = splitter.split('test-pdfs/期末复习试题二.pdf')
    >>> # left_pdf 和 right_pdf 是两个 A4 尺寸的临时 PDF 文件路径
    """

    # A3 双栏阈值：页面宽度超过这个值认为是 A3 双栏
    A3_WIDTH_THRESHOLD = 900  # pt

    @classmethod
    def is_a3_pdf(cls, pdf_path: str) -> bool:
        """检测 PDF 是否为 A3 双栏格式"""
        try:
            doc = fitz.open(pdf_path)
            first_page = doc[0]
            is_a3 = first_page.rect.width >= cls.A3_WIDTH_THRESHOLD
            doc.close()
            return is_a3
        except Exception:
            return False

    def split(self, pdf_path: str) -> tuple:
        """
        将 A3 PDF 裁切为左右两个 A4 PDF

        Args:
            pdf_path: A3 PDF 文件路径

        Returns:
            (left_pdf_path, right_pdf_path): 两个临时 A4 PDF 的路径
        """
        doc = fitz.open(pdf_path)
        total_pages = len(doc)

        # 左栏页码偏移: 0, 1, 2, ...
        # 右栏页码偏移: 100, 101, 102, ... (避免与左栏重复)
        left_pdf_path = self._create_half_pdf(doc, is_left=True, page_offset=0)
        right_pdf_path = self._create_half_pdf(doc, is_left=False, page_offset=100)

        doc.close()

        return left_pdf_path, right_pdf_path

    def merge_to_long_pdf(self, pdf_path: str) -> str:
        """
        将 A3 PDF 裁切并左右拼接成一个长 PDF

        左栏内容在上半页，右栏内容在下半页。
        每半页宽=W/2，高=H（保持 A4 尺寸）。

        长 PDF 结构（以 2 页 A3 为例）：
        - Page 0: 左栏 A3Page0 左半 (x:0-W/2, y:0-H) → 宽=W/2, 高=H
        - Page 1: 右栏 A3Page0 右半 (x:W/2-W, y:0-H) → 宽=W/2, 高=H
        - Page 2: 左栏 A3Page1 左半 → 宽=W/2, 高=H
        - Page 3: 右栏 A3Page1 右半 → 宽=W/2, 高=H
        ...

        解析后：奇数页(0,2,4...)=左栏，偶数页(1,3,5...)=右栏

        Args:
            pdf_path: A3 PDF 文件路径

        Returns:
            长 PDF 的临时文件路径
        """
        doc = fitz.open(pdf_path)
        total_pages = len(doc)

        if total_pages == 0:
            doc.close()
            return None

        first_page = doc[0]
        page_width = first_page.rect.width
        page_height = first_page.rect.height
        half_width = page_width / 2

        # 每半页尺寸：A4 宽(half_width) × A4 高(page_height)
        new_doc = fitz.open()

        for page_num in range(total_pages):
            page = doc[page_num]

            # 左栏半页
            left_clip = fitz.Rect(0, 0, half_width, page_height)
            left_page = new_doc.new_page(width=half_width, height=page_height)
            left_page.show_pdf_page(
                fitz.Rect(0, 0, half_width, page_height),
                doc,
                page_num,
                clip=left_clip
            )
            # 左上角标记页码 "P0L" 格式（在内容之后绘制，避免被覆盖）
            # 使用更粗体字和更明显的位置
            left_page.insert_text(
                (half_width - 40, 20),  # 右下角
                f"P{page_num}L",
                fontsize=10,
                color=(0.8, 0.0, 0.0),  # 红色更容易被识别
                fontname="helv"
            )

            # 右栏半页
            right_clip = fitz.Rect(half_width, 0, page_width, page_height)
            right_page = new_doc.new_page(width=half_width, height=page_height)
            right_page.show_pdf_page(
                fitz.Rect(0, 0, half_width, page_height),
                doc,
                page_num,
                clip=right_clip
            )
            # 右下角标记页码 "P0R" 格式
            right_page.insert_text(
                (half_width - 40, 20),
                f"P{page_num}R",
                fontsize=10,
                color=(0.0, 0.0, 0.8),  # 蓝色区分左右栏
                fontname="helv"
            )

        doc.close()

        # 保存到临时文件
        tmp = tempfile.NamedTemporaryFile(
            suffix='.pdf', prefix='a3long_', delete=False
        )
        tmp.close()
        new_doc.save(tmp.name, garbage=4, deflate=True)
        new_doc.close()

        return tmp.name

    def _create_half_pdf(self, doc, is_left: bool, page_offset: int = 0) -> str:
        """创建左栏或右栏的 PDF（带页码偏移标记）"""
        page = doc[0]  # 取第一页分析尺寸
        page_width = page.rect.width
        page_height = page.rect.height

        # 中间分割线
        mid_x = page_width / 2

        # 创建新 PDF
        new_doc = fitz.open()

        for page_num in range(len(doc)):
            page = doc[page_num]

            if is_left:
                # 左栏：保留 0 到 mid_x 部分
                clip_rect = fitz.Rect(0, 0, mid_x, page_height)
            else:
                # 右栏：保留 mid_x 到 page_width 部分，并平移到 x=0
                clip_rect = fitz.Rect(mid_x, 0, page_width, page_height)

            # 创建新页面（A4 尺寸）
            new_page = new_doc.new_page(width=mid_x, height=page_height)

            # 裁切并绘制内容
            new_page.show_pdf_page(
                fitz.Rect(0, 0, mid_x, page_height),
                doc,
                page_num,
                clip=clip_rect
            )

            # 添加页码标记（用于识别来源栏位）
            # 左栏: 第N页显示 "L-N"，右栏: "R-N"
            prefix = "L" if is_left else "R"
            new_page.insert_text(
                (10, page_height - 20),
                f"{prefix}-{page_num}",
                fontsize=8,
                color=(0.8, 0.8, 0.8)  # 浅灰色，不影响内容
            )

        # 保存到临时文件
        tmp = tempfile.NamedTemporaryFile(
            suffix='.pdf', prefix='a3split_', delete=False
        )
        tmp.close()
        new_doc.save(tmp.name, garbage=4, deflate=True)
        new_doc.close()

        return tmp.name

    def split_and_parse(self, pdf_path: str, parser_func):
        """
        裁切 A3 PDF 并分别解析，然后合并结果

        Args:
            pdf_path: A3 PDF 文件路径
            parser_func: 解析函数，接收 pdf_path 返回 ParsedExam

        Returns:
            ParsedExam: 合并后的解析结果
        """
        from ._v2_models import ParsedExam

        # 检测是否为 A3
        if not self.is_a3_pdf(pdf_path):
            # 不是 A3，直接解析
            return parser_func(pdf_path)

        print(f"  📄 检测到 A3 双栏试卷，正在裁切...")
        left_pdf, right_pdf = self.split(pdf_path)

        # 分别解析
        print(f"  📄 解析左栏...")
        left_exam = parser_func(left_pdf)

        print(f"  📄 解析右栏...")
        right_exam = parser_func(right_pdf)

        # 合并结果：右栏题目的 index 加上左栏的题目数
        left_count = len(left_exam.questions)
        for q in right_exam.questions:
            q.index += left_count

        # 合并题目列表
        merged_exam = left_exam
        merged_exam.questions.extend(right_exam.questions)

        # 清理临时文件
        try:
            os.unlink(left_pdf)
            os.unlink(right_pdf)
        except Exception:
            pass

        print(f"  ✅ 合并完成：共 {len(merged_exam.questions)} 道题")

        return merged_exam


# 便捷函数
def split_a3_pdf(pdf_path: str) -> tuple:
    """裁切 A3 PDF 为左右两个 A4 PDF（便捷函数）"""
    splitter = A3Splitter()
    return splitter.split(pdf_path)


def is_a3_pdf(pdf_path: str) -> bool:
    """检测 PDF 是否为 A3 格式"""
    return A3Splitter.is_a3_pdf(pdf_path)