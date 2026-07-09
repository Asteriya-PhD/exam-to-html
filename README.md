# Exam to HTML

> 给物理教师的试卷讲评课件生成器 —— 拖 PDF 进窗口，60 秒拿到可分享的 HTML 课件。

**目标用户**：懂物理不懂代码的高中物理教师。
**形态**：Windows 单文件 .exe（macOS 顺带）。
**当前进度**：🔨 设计阶段（M0）

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
# 在仓根目录
python -m venv .venv
source .venv/bin/activate  # 或 .venv\Scripts\activate (Win)

pip install -e ../topic_garden_app  # 引用老仓（本地开发）
pip install -e ".[dev]"

# 本地启动（开发模式）
python -m exam_to_html

# 打包 Windows .exe
pyinstaller pyinstaller.spec
```

详见 [`docs/distribution-design.md`](docs/distribution-design.md) 第 9 节里程碑。

---

## 设计原则

1. **教师看不到技术细节** —— Python / SQLite / MinerU 全部隐藏在"高级设置"折叠区
2. **不主动推销 token** —— flash 模式开箱即用，token 是奖励性升级
3. **100% 本地处理** —— PDF 不上传到任何第三方服务（除了 MinerU API 调用时）
4. **完美主义优先于上线时间** —— 3-5 个种子教师用好，比 100 个教师凑合用更重要

---

## License

MIT