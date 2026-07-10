# Exam to HTML · Windows 构建 handoff

> 本文档写给**手动在 Windows 机器上** 出 .exe 的开发者。
> **首选：GitHub Actions 云端 build**（见顶部），push 即触发，无需本地 Win。
> macOS 出包见底部补充。

## A. 云端 GitHub Actions（推荐, 零本地 Win 操作）

`.github/workflows/build.yml` 已写好，push 到 GH 后自动跑：

```bash
# 一次性 setup
gh repo create exam-to-html --public --source=. --push    # macOS/Linux
# 或在 GH 网页上创建空 repo, 然后:
git remote add origin git@github.com:yourname/exam-to-html.git
git push -u origin main

# 兄弟仓也要 push (workflow 依赖)
# 在 topic_garden_app 仓目录:
git remote add origin git@github.com:yourname/topic_garden_app.git
git push -u origin main
# PDF2PPT v2 parser 已随本仓 vendored,无需再单独 push
```

**触发方式**:
- `git push origin main` → 跑 test + build, 上传 artifact (Win .zip + Mac .zip)
- `git tag v0.1.0 && git push origin v0.1.0` → 上面 + 自动创建 GitHub Release
- 网页 Actions 页面 → Run workflow → 手动触发

**取产物**:
- 临时测试: [Actions 页] → 对应 run → Artifacts → 下载 `exam-to-html-windows.zip`
- 正式发布: [Releases 页] → 选版本 → 下载 `exam-to-html-windows.zip`

**.zip 内容**:
```
exam-to-html-windows.zip
└── exam-to-html/             ← 整个分发目录
    ├── exam-to-html.exe     ← 教师双击这个
    ├── _internal/           ← Python runtime + deps + 静态资源
    └── ...
```

**version.json 自动更新流**（设计文档 §7.2）:
- `git tag v0.1.0 && git push origin v0.1.0` → release job 上传 `.zip` + `deploy-version` job 把 `version.json` 推到 `gh-pages`
- 一次手动配置: GH 仓 → Settings → Pages → Source: `gh-pages` branch / `(root)` → Save
- 教师 app 自动 GET `https://<user>.github.io/exam-to-html/version.json` 检查更新

---

## B. 本地 Windows 手动 build（备选, 1.5-2 小时）

## 0. 前置条件

| 项 | 版本 / 备注 |
|---|---|
| OS | Windows 10/11 (x64) |
| Python | **3.11+** (推荐 3.12; 3.13 / 3.14 可能缺 wheel, 见风险) |
| Git Bash / PowerShell | 都能用, 示例用 PowerShell |
| WebView2 Runtime | Win11 自带; Win10 需手动装 [Evergreen Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) |

## 1. 拉仓 + 复用 topic_garden venv

**前提**：你已经在 Windows 上有 `topic_garden_app` 的 venv。exam-to-html 直接复用即可。

```powershell
cd C:\path\to\parent
git clone <exam-to-html-repo> exam-to-html
cd exam-to-html

# 复用 topic_garden_app 的 venv (而不是新建)
..\topic_garden_app\.venv\Scripts\Activate.ps1
```

## 2. 装 exam-to-html 自身

```powershell
# 本项目自身 (含 pyinstaller)
pip install -e .[dev]

# precision 模式需要 MinerU SDK (可选, flash 模式不需要)
pip install -e .[precision]
```

> ✅ **PDF2PPT v2 parser 已 vendored 到本仓顶层 pdf2ppt/** — 不再需要 `pip install -r ..\PDF2PPT\requirements.txt`,也不需要 `pdf2ppt.pth` 绕过。
> 验证: `python -c "import pdf2ppt; print(pdf2ppt.__file__)"` 应指向本仓 `pdf2ppt\__init__.py`。

## 3. 出 .exe

```powershell
pyinstaller pyinstaller.spec
```

产物:
```
dist/
└── exam-to-html/
    ├── exam-to-html.exe     ← 主入口 (双击运行)
    ├── _internal/           ← 依赖 + 数据 (整个目录分发)
    └── ...
```

## 5. 准备图标 (出包前)

把 .ico 文件放到 `icons/app.ico`, 然后编辑 `pyinstaller.spec`:
```python
# EXE(...)
icon='icons/app.ico',  # 解除注释

# BUNDLE(...) (macOS)
icon='icons/app.icns',  # 解除注释
```

没有图标也能跑, 只是显示默认 Python 图标。

## 6. 测试 .exe

```powershell
# 1. 双击 dist/exam-to-html/exam-to-html.exe
# 2. 弹窗 → 拖 PDF → 等 HTML
# 3. 检查 %APPDATA%\exam-to-html\ 下:
#    - config.json
#    - db.sqlite3
#    - archive\inbox\
```

## 7. 风险清单

| 风险 | 概率 | 应对 |
|---|---|---|
| Python 3.13/3.14 wheel 缺失 (peewee/jinja2) | 中 | 退到 3.12 或 3.11 |
| vendored pdf2ppt 漂移 (本地包被覆盖) | 低 | 升级时重新从 PDF2PPT 兄弟仓 cp; `import pdf2ppt` 验证 |
| WebView2 缺失 (Win7 / 老 Win10) | 高 | 文档写"仅支持 Win10+", 安装时引导装 Runtime |
| 360 / 杀毒误报 | 高 | code sign (需要 EV 证书 $300-500/年) |
| 中文路径乱码 | 低 | paths.py 全部用 pathlib, 文件读写显式 utf-8 |

## 8. macOS 出包 (补充)

```bash
# 装齐后:
pyinstaller pyinstaller.spec
# 产物: dist/Exam to HTML.app
# 双击运行, 或 open dist/Exam\ to\ HTML.app
```

macOS 出 .app 时如果想脱离 terminal 分发, 还要:
- code sign (开发者账号 $99/年)
- notarize (xcrun notarytool submit ...)

v1.0 M1 不强制, 教师自用可以右键"打开"绕过 gatekeeper。

---

## 9. 出包后验证 checklist

- [ ] .exe 双击启动, 弹窗不闪退
- [ ] 拖 PDF → HTML 成功生成
- [ ] HTML 在浏览器打开, KaTeX 公式渲染正确
- [ ] 关闭 app, %APPDATA%\exam-to-html\ 有 config.json + db.sqlite3
- [ ] 第二次拖同一 PDF → 复用 qid, 新建 Topic (主题列表 +1)
- [ ] 关闭 + 重开 app → 配置保留 (output_dir / token)