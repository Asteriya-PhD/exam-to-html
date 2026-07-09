"""exam_to_html.__main__ — python -m exam_to_html 入口

⚠️ PyInstaller frozen 模式下, __main__.py 被作为独立脚本运行,
    package 上下文丢失, 相对导入 `from .app import main` 会崩。
    必须用绝对导入: `from exam_to_html.app import main`。
    python -m exam_to_html 时两种写法都 OK, 但绝对导入兼容性更广。
"""
import sys

from exam_to_html.app import main

if __name__ == "__main__":
    sys.exit(main())