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
# topic_garden layout: <repo>/src/topic_garden/ — TG_PKG.parent = src, .parent.parent = repo root
import topic_garden
TG_PKG = Path(topic_garden.__path__[0])
TG_REPO = TG_PKG.parent.parent  # src/topic_garden/ → src/ → repo root
# 兜底: 兼容 flat layout (旧版本 topic_garden 把模板/图放在 repo 根)
if not (TG_REPO / 'templates').exists():
    TG_REPO = TG_PKG.parent  # flat layout: topic_garden/ → repo root

# 修 CI PyInstaller fail: courseware/images 是 topic_garden 运行时产物,
# 被 gitignore, CI clone 后目录不存在 — 用 .exists() 过滤掉, 缺失仅 warn。
def _opt_data(src_rel: str, dst: str):
    """仅当 src 存在时返回 datas 元组, 否则返回 None (供 filter 排除)。"""
    src = TG_REPO / src_rel
    if src.exists():
        return (str(src), dst)
    print(f"[spec] skip data: {src} 不存在 (CI clone 时常因 gitignore 缺失)")
    return None

_datas = [
    # exam-to-html 的 GUI 静态文件
    ('exam_to_html/gui/static', 'exam_to_html/gui/static'),
    # topic_garden 的 Jinja 模板 (composer 渲染用) — 缺失时仅 warn
    _opt_data('templates', 'topic_garden/templates'),
    # topic_garden 的 courseware/images (figure_paths 引用此目录) — 同上
    _opt_data('courseware/images', 'courseware/images'),
]
datas = [d for d in _datas if d is not None]

block_cipher = None

a = Analysis(
    ['exam_to_html/__main__.py'],
    pathex=[
        str(TG_REPO / 'src'),
        str(TG_REPO / 'src' / 'topic_garden'),
    ],
    binaries=[],
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