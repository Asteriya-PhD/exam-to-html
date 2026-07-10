# -*- mode: python ; coding: utf-8 -*-
"""
exam-to-html PyInstaller spec

跨平台 (Mac 出 .app, Win 出 .exe)。
打包前必须:
  pip install -e ../topic_garden_app
  pip install -e .[precision]    # 可选, precision 模式

PDF2PPT v2 parser 子集已 vendored 到本仓 pdf2ppt/，不再需要 ../PDF2PPT。
"""
import sys
from pathlib import Path

# 找到 topic_garden 包路径 (兄弟仓 editable 安装)
import topic_garden
TG_PKG = Path(topic_garden.__path__[0])
TG_REPO = TG_PKG.parent.parent  # src/topic_garden/ → src/ → repo root

block_cipher = None

a = Analysis(
    ['exam_to_html/__main__.py'],
    pathex=[
        str(TG_REPO / 'src'),
        str(TG_REPO / 'src' / 'topic_garden'),
    ],
    binaries=[],
    datas=[
        # exam-to-html 的 GUI 静态文件
        ('exam_to_html/gui/static', 'exam_to_html/gui/static'),
        # topic_garden 的 Jinja 模板 (composer 渲染用)
        (str(TG_REPO / 'templates'), 'topic_garden/templates'),
        # topic_garden 的 courseware/images (figure_paths 引用此目录)
        # 注: 此目录可能为空, 缺失时 PyInstaller 会 warn 但不报错
        (str(TG_REPO / 'courseware' / 'images'), 'courseware/images'),
        # inbox/archive 空目录占位 (避免 frozen 模式下空目录问题)
        # PyInstaller 不打包空目录, 但运行时 %APPDATA% 自动 mkdir
    ],
    hiddenimports=[
        # exam_to_html 自身子模块 (PyInstaller 静态分析可能漏掉 string import)
        'exam_to_html',
        'exam_to_html.app',
        'exam_to_html.backend',
        'exam_to_html.backend.server',
        'exam_to_html.backend.pipeline',
        'exam_to_html.gui',
        'exam_to_html.gui.server',
        'exam_to_html.gui.window',
        'exam_to_html.paths',
        'exam_to_html.config',
        # topic_garden 子模块 (动态 import)
        'peewee', 'jinja2',
        'topic_garden.composer',
        'topic_garden.ingest_inbox',
        'topic_garden.ingest.pdf_parser',
        'topic_garden.ingest.katex',
        'topic_garden.parse_quality',
        'topic_garden.metadata_suggester',
        'topic_garden.question_recommender',
        # pdf2ppt 已 vendored 到本仓 (顶层 pdf2ppt/ 包), frozen 模式下 static
        # analysis 可能漏掉 lazy 引用,这里显式 hidden-import
        'pdf2ppt._v2_parser',
        'pdf2ppt._v2_models',
        'pdf2ppt._qnum_rule',
        'pdf2ppt._v3_a3_splitter',
        'pdf2ppt._katex_renderer',
        'pdf2ppt._phys_text',
        'pdf2ppt._phys_postprocess',
        'pdf2ppt._chem_text',
        # pywebview 平台绑定
        'webview',
        'webview.platforms.cocoa',      # macOS
        'webview.platforms.winforms',   # Windows
        'webview.platforms.gtk',        # Linux
        # uvicorn 协议子模块
        'uvicorn',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.off',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'unittest', 'pydoc', 'doctest',
        'matplotlib', 'numpy.tests', 'PIL.tests',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='exam-to-html',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,           # 教师面向: 不弹黑窗口
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon='icons/app.ico'  # Win 出包前解除注释
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='exam-to-html',
)

# macOS: 额外生成 .app bundle
if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='Exam to HTML.app',
        icon=None,           # macOS 出包前: 'icons/app.icns'
        bundle_identifier='com.exam-to-html',
        info_plist={
            'CFBundleName': 'Exam to HTML',
            'CFBundleDisplayName': 'Exam to HTML',
            'CFBundleShortVersionString': '0.1.0',
            'NSHighResolutionCapable': True,
            'LSMinimumSystemVersion': '11.0',
        },
    )