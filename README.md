# Exam to HTML

> 给物理教师的试卷讲评课件生成器 —— 拖 PDF 进窗口，60 秒拿到可分享的 HTML 课件。

**目标用户**：懂物理不懂代码的高中物理教师。
**形态**：Windows 单文件 .exe（macOS 顺带）。
**当前进度**：🔨 M1 骨架完工 + CI 自动 build（Win/Mac）
[![Build](https://github.com/yourname/exam-to-html/actions/workflows/build.yml/badge.svg)](https://github.com/yourname/exam-to-html/actions/workflows/build.yml)

---

## 这是什么？

李老师考完试，想把试卷做成讲评课件发给学生。

**老办法**：Word / PPT 排版 30 分钟，公式还要手动截图。
**新办法**：拖 PDF 进 Exam to HTML，60 秒后 HTML 课件直接发给班级群。

HTML 课件包含：
- 试卷原题（含图、含 KaTeX 公式）
- 自包含样式（双击就能在任何浏览器打开，无需联网）
- KaTeX 数学公式本地渲染

---

## 当前状态

| 阶段 | 状态 |
|---|---|
| 设计文档 | ✅ [`docs/distribution-design.md`](docs/distribution-design.md) |
| 骨架 | ⏳ 待开工（M1） |
| UI 打磨 | ⏸ 待开工（M2） |
| v1.0 发布 | 🎯 暑假内 |

---

## 技术依赖

- 库：[`topic_garden_app`](../topic_garden_app/)（提供 PDF→DB→HTML 流水线）
- GUI：[PyWebView](https://pywebview.flowrl.com/) 4.x
- 后端：[FastAPI](https://fastapi.tiangolo.com/) + [uvicorn](https://www.uvicorn.org/)
- 打包：[PyInstaller](https://pyinstaller.org/) 6.x

---

## 开发

```bash
# 方式 A (推荐): 复用 topic_garden_app/.venv
# topic_garden 的 venv 已经装齐 PDF2PPT 全套依赖 (PyMuPDF / python-pptx /
# mineru-open-sdk / zhipuai / rapidocr), 真 PDF 解析可直接跑。
source ../topic_garden_app/.venv/bin/activate     # Mac/Linux
# 或: ..\topic_garden_app\.venv\Scripts\Activate.ps1   # Win

pip install -e .[dev]                            # 只装 exam-to-html 自身

python -m exam_to_html                           # 启动 GUI

# 打包 Windows .exe
pyinstaller pyinstaller.spec

# ---
# 方式 B: 全新独立 venv (需另外装 PDF2PPT 依赖)
python -m venv .venv
source .venv/bin/activate
pip install -e ../topic_garden_app
pip install -e ".[dev]"
pip install -r ../PDF2PPT/requirements.txt       # 仅方式 B 需要
```

详见 [`docs/distribution-design.md`](docs/distribution-design.md) 第 9 节里程碑。

---

## 设计原则

1. **教师看不到技术细节** —— Python / SQLite / MinerU 全部隐藏在"高级设置"折叠区
2. **不主动推销 token** —— flash 模式开箱即用，token 是奖励性升级
3. **100% 本地处理** —— PDF 不上传到任何第三方服务（除了 MinerU API 调用时）
4. **完美主义优先于上线时间** —— 3-5 个种子教师用好，比 100 个教师凑合用更重要

## 自动更新（设计文档 §7）

教师电脑装好后会自动检查更新（24h 节流），高级设置里"📦 检查更新"按钮可手动强制检查。

- **version.json 部署在哪**: GitHub Pages，自动 push。`git tag v0.1.0 && git push origin v0.1.0` 触发 workflow deploy 到 `gh-pages` 分支，URL: `https://<user>.github.io/exam-to-html/version.json`
- **教师不会自动下载/安装** —— 只在高级设置里提示"🆕 新版本可用"，由教师主动点"前往下载"
- **首次配置**: 见 [`docs/build-windows.md`](docs/build-windows.md) §A — push 仓到 GH 后 workflow 自动跑

---

## License

MIT