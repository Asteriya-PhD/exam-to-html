# Exam to HTML — 分发设计文档

> 目标用户：**懂物理不懂代码的高中物理教师**。
> 形态：**Windows 优先单文件 .exe**（macOS 顺带支持）。
> 工期目标：**完美主义打磨**，不抢上线时间。

---

## 1. 用户故事

### 1.1 主流程（v1.0 必做）

> **李老师**想把本周刚考完的高一期中试卷做成讲评课件。

```
1. 李老师在微信群里收到同事转的链接：https://github.com/.../releases
2. 下载 exam-to-html-1.0.0-win.exe（~100MB）
3. 双击 → 弹安装向导 → 一路 Next → 桌面生成 "Exam to HTML" 图标
4. 双击图标 → 弹一个简洁窗口：
   ┌─────────────────────────────────────────────┐
   │  Exam to HTML                                │
   │                                              │
   │  ┌──────────────────────────────────────┐    │
   │  │                                       │    │
   │  │        📄 拖 PDF 试卷到这里           │    │
   │  │            或点击选择文件             │    │
   │  │                                       │    │
   │  └──────────────────────────────────────┘    │
   │                                              │
   │  输出位置:  [📁 桌面                       ▼]  │
   │                                              │
   │  ▸ 高级设置 (0)                              │
   │                                              │
   │         [开始转换]                            │
   │                                              │
   └─────────────────────────────────────────────┘
5. 拖入 `高一期中试卷.pdf` → 进度条开始：
   "正在解析 PDF... 0/12 题"
   → "正在入库... 11 题已添加"
   → "正在生成课件..."
   → "✅ 完成！HTML 已保存到桌面"
6. HTML 自动在浏览器打开 → 李老师直接看讲评课件
7. 如果满意，HTML 文件可以拷给同事 / 上传到班级群
```

### 1.2 边界场景

| 场景 | 用户期望 | 系统行为 |
|---|---|---|
| 拖入非 PDF 文件 | 提示"请拖入 PDF 文件" | 红框 + 拒绝 |
| 拖入加密 PDF | 提示"PDF 已加密，无法解析" | 弹窗 + 不入库 |
| 拖入超大 PDF (>50MB) | 进度条慢但能跑 | 不阻塞 + 提示"大文件，可能需要几分钟" |
| MinerU API 不可用 | 提示"在线解析服务暂时不可用，请稍后再试" | 重试按钮 + 不入库 |
| PDF 含扫描图（无文本层） | 走 OCR fallback | 提示"检测到扫描 PDF，可能需要更长时间" |
| 输出文件夹无写权限 | 提示"无法写入 X，请选择其他文件夹" | 重新选文件夹 |
| 中途关闭 app | 解析未完成的 PDF 不入库 | DB 干净，下次启动重跑 |
| 同时拖入多个 PDF | 排队处理 | 进度条显示 "正在处理 2/3" |

### 1.3 教师 5 分钟上手流程（无技术背景）

| 时间 | 教师动作 | 需要系统做的 |
|---|---|---|
| 0:00 | 下载 .exe | 链接清晰，文件名带版本号 |
| 0:30 | 双击安装 | 安装向导简洁，无 EULA 陷阱 |
| 1:00 | 桌面看到图标 | 图标看一眼知道是"试卷 → HTML" |
| 1:30 | 双击打开 | 窗口立刻出现，无白屏 |
| 2:00 | 拖 PDF | 拖入区域高亮反馈 |
| 2:30 | 点开始 | 进度可见，不焦虑 |
| 5:00 | HTML 在浏览器打开 | 课件质量肉眼可接受 |

**关键 UX 原则**：教师不需要看到任何技术细节（Python、SQL、MinerU、token），所有这些都隐藏在"高级设置"折叠区。

---

## 2. "不主动提示 token" 反向引导设计

### 2.1 核心原则

> **教师不知道自己错过了什么** —— 试卷讲评场景下，单选/多选标签不是硬需求（教师自己清楚题目类型）。flash 模式产出的 HTML 已经完全够用。token / precision 模式是**奖励性升级**，不主动推销。

### 2.2 三种模式对比

| 模式 | 触发条件 | 输出 HTML 是否有【单选】【多选】徽章 | 教师成本 |
|---|---|---|---|
| **flash**（默认） | 无 MinerU token | ❌ 无徽章（纯净 HTML） | 0（开箱即用） |
| **precision**（隐藏升级） | 有 MinerU token | ✅ 有徽章 | 注册 mineru.net 账号 5 分钟 |
| **纯本地 OCR**（未来可选） | 无网络 + 愿意等 | ❌ 无徽章（本地 OCR 精度低） | 0（首次启动慢） |

### 2.3 UI 设计

主界面**不显示** mode 选择框：

```
┌─────────────────────────────────────────────┐
│  Exam to HTML                                │
│                                              │
│  ┌──────────────────────────────────────┐    │
│  │        📄 拖 PDF 试卷到这里           │    │
│  └──────────────────────────────────────┘    │
│                                              │
│  输出位置:  [📁 桌面                       ▼]  │
│                                              │
│  ▸ 高级设置 (0)                              │  ← 默认折叠，点击展开
│                                              │
│         [开始转换]                            │
└─────────────────────────────────────────────┘
```

点开高级设置：

```
┌─────────────────────────────────────────────┐
│  ▾ 高级设置                                  │
│                                              │
│  ┌──────────────────────────────────────┐    │
│  │  MinerU API Token (可选)              │    │
│  │  [                                  ] │    │
│  │                                       │    │
│  │  💡 不填也能用。                      │    │  ← 极简文案，不强调
│  │  填了之后能识别题目类型               │    │
│  │  (单选/多选/解答)。                   │    │
│  └──────────────────────────────────────┘    │
│                                              │
│  [📦 检查更新]                               │
└─────────────────────────────────────────────┘
```

文案关键：
- **不写** "推荐填写" / "强烈建议" / "获得最佳体验"
- **不写** "flash 模式精度有限"
- **写** "不填也能用"（先打消顾虑）
- **写** "填了之后能识别题目类型"（**解释机制不解释价值**，让教师自己判断要不要）
- **不引导注册流程**：不提供 mineru.net 链接（让有意愿的用户自己搜）

### 2.4 何时教师会发现

教师发现 token 价值的**自然路径**：

1. 用着 flash 模式，HTML 没单选/多选标签——觉得"够用了"
2. 某天点开高级设置，看到"填了之后能识别题目类型"
3. 想试试，注册 mineru.net 账号，填 token
4. 重跑同一份 PDF，发现题上多了【单选】【多选】徽章——惊喜
5. 形成习惯：以后都填 token

**关键**：第 3 步必须**教师主动**发生，不能 app 主动引导。

---

## 3. 技术栈

| 层 | 选型 | 理由 |
|---|---|---|
| GUI 壳 | **PyWebView 4.x** | HTML/CSS UI 跨平台，比 Tkinter 好看 10 倍，体积小 |
| 后端服务 | **FastAPI** (uvicorn) 起 `localhost:8765` | 复用 topic_garden_app 已删的 FastAPI 模式 |
| 前端 | **原生 HTML + CSS + 少量 JS**（无 React/Vue） | 单页应用，3 个文件就够 |
| 打包 | **PyInstaller 6.x** | 成熟，spec 文件可定制 |
| 跨平台 | **Windows 优先** + macOS 顺带 | 80% 教师在 Win |
| Python 版本 | **3.11+** | PyWebView + FastAPI + SQLite 都稳定支持 |

### 3.1 依赖列表（pyproject.toml）

```toml
[project]
name = "exam-to-html"
version = "1.0.0"
requires-python = ">=3.11"
dependencies = [
    "topic-garden-app @ git+ssh://git@github.com/yourname/topic_garden_app.git@v0.18.4",
    # 或本地开发: "topic-garden-app @ file://../topic_garden_app",
    "pywebview>=4.4",
    "fastapi>=0.110",
    "uvicorn[standard]>=0.27",
    "requests>=2.31",  # 检查更新用
]

[project.optional-dependencies]
dev = [
    "pyinstaller>=6.0",
    "pytest>=7.0",
]
```

### 3.2 目录结构

```
exam-to-html/
├── pyproject.toml
├── README.md                       # 5 分钟上手图解（教师面向）
├── LICENSE                         # MIT
├── docs/
│   ├── distribution-design.md     # 本文档
│   ├── user-flow.md                # 教师 UX 流程图（PDF 截图版）
│   ├── error-handling.md           # 错误处理矩阵
│   └── architecture.md             # 技术架构图
├── exam_to_html/
│   ├── __init__.py
│   ├── __main__.py                 # python -m exam_to_html
│   ├── app.py                      # 主入口（GUI + 后端编排）
│   ├── config.py                   # %APPDATA% 路径 + config.json 读写
│   ├── updater.py                  # 检查更新
│   ├── paths.py                    # 跨平台路径
│   ├── gui/
│   │   ├── __init__.py
│   │   ├── window.py               # PyWebView 主窗口
│   │   ├── server.py               # 启动 uvicorn + 健康检查
│   │   └── static/
│   │       ├── index.html          # 单页 UI
│   │       ├── style.css
│   │       └── app.js              # 拖拽 + 进度轮询
│   └── backend/
│       ├── __init__.py
│       ├── server.py               # FastAPI app
│       ├── routes.py               # /api/convert /api/status /api/config
│       └── pipeline.py             # 复用 topic_garden_app 的 process_inbox + compose
├── pyinstaller.spec                # Windows 打包
├── pyinstaller.macos.spec          # macOS 打包
├── icons/
│   ├── app.ico                     # Windows 图标 (256x256 + 16x16 + 32x32)
│   ├── app.png                     # macOS 图标 (1024x1024)
│   └── app.icns                    # macOS .icns
├── installer/
│   ├── windows.iss                 # Inno Setup 脚本（生成 .exe 安装向导）
│   └── build_windows.ps1
├── scripts/
│   ├── build_windows.ps1
│   ├── build_macos.sh
│   └── dev_run.sh                  # 本地开发模式启动
├── tests/
│   ├── test_smoke.py               # E2E: PDF → HTML
│   ├── test_config.py
│   └── test_updater.py
└── .github/
    └── workflows/
        └── release.yml             # tag → build exe → upload to release
```

---

## 4. 数据流

### 4.1 PDF → HTML 完整链路

```
[教师拖 PDF]
    │
    ▼
[GUI: index.html] 
    │ POST /api/convert (file: PDF bytes, output_dir: string)
    ▼
[FastAPI: /api/convert handler]
    │
    │ 1. 保存 PDF 到 %APPDATA%/exam-to-html/inbox/<uuid>.pdf
    │
    │ 2. 读取 config.json → 取 mineru_token (可能为空)
    │    └─ if token: mode = 'precision'
    │    └─ else:    mode = 'flash'
    │
    │ 3. 调用 topic_garden_app.process_inbox(
    │        inbox_dir=temp_inbox,
    │        archive_dir=temp_archive,
    │        mode=mode,
    │        mineru_token=token,
    │     )
    │    → 返回 QuestionDraft 列表 + IngestionRun
    │
    │ 4. 调 topic_garden_app.db.add_question_with_dedupe() × N
    │    → Question 行入库（去重）
    │
    │ 5. 创建 Topic (title=PDF filename stem, day_label='adhoc')
    │
    │ 6. 把所有 Question 挂到 Topic (role='作业' 或按 qnum priority)
    │
    │ 7. 调 topic_garden_app.composer.TopicComposer.compose_to_file()
    │    → 输出 HTML 到 output_dir/
    │
    │ 8. 返回 HTML 路径给 GUI
    │
    ▼
[GUI: 进度更新 + 浏览器打开 HTML]
```

### 4.2 状态机

```
[IDLE] --拖 PDF--> [UPLOADING]
[UPLOADING] --API 200--> [PARSING]
[UPLOADING] --API 4xx/5xx--> [ERROR] --关闭--> [IDLE]
[PARSING] --0%--> [PARSING] --50%--> [INDEXING]
[PARSING] --parse fail--> [ERROR]
[INDEXING] --100%--> [COMPOSING]
[INDEXING] --db fail--> [ERROR]
[COMPOSING] --100%--> [DONE]
[COMPOSING] --render fail--> [ERROR]
[DONE] --新 PDF--> [IDLE]
[DONE] --关闭--> [EXIT]
[ERROR] --重试--> [IDLE]
```

---

## 5. 错误处理矩阵

### 5.1 用户可见错误

| 错误 | 用户看到的提示 | 恢复动作 |
|---|---|---|
| 非 PDF 文件 | "请拖入 PDF 文件（.pdf 后缀）" | 重新拖 |
| 加密 PDF | "PDF 已加密，无法解析。请用 PDF 阅读器解密后重试" | 重新拖 |
| PDF > 100MB | "文件过大（>100MB），请压缩或拆分" | 重新拖 |
| MinerU API 超时 | "在线解析服务暂时不可用，请稍后再试" | 重试按钮 |
| MinerU API 401 | "API token 无效，请在高级设置检查" | 引导到高级设置 |
| 输出目录无权限 | "无法写入 [目录]，请选择其他位置" | 重新选 |
| 磁盘空间不足 | "磁盘空间不足，需要至少 200MB" | 释放空间 |
| 中途关闭 | 下次启动时显示"上次有未完成的 PDF，是否重新处理？" | 重新拖 |

### 5.2 静默错误（不打扰用户）

| 错误 | 行为 |
|---|---|
| 单题解析失败 | 该题跳过 + 写入日志，其他题继续 |
| KaTeX 公式渲染失败 | 显示原始 LaTeX 文本，不中断 |
| 图片加载失败 | 显示"图片加载失败"占位 |
| 数据库锁 | 重试 3 次（间隔 100ms） |

### 5.3 日志位置

- `%APPDATA%/exam-to-html/logs/app.log` (Windows)
- `~/Library/Logs/exam-to-html/app.log` (macOS)
- 用户看不到日志入口，但"反馈问题"按钮可一键打 zip 包

---

## 6. 跨平台兼容性

### 6.1 Windows

| 项 | 状态 |
|---|---|
| Windows 11 (x64) | ✅ 第一目标 |
| Windows 10 (x64) | ✅ 测试 |
| Windows 7 | ❌ 不支持（PyWebView 4.x 需 WebView2，Win7 默认无） |
| Windows ARM64 | ⚠️ 后续版本 |
| 中文路径 | ✅ 测试 |
| 中文文件名 | ✅ 测试 |
| 中文 PDF 内容 | ✅ 测试 |
| 网络代理 | ⚠️ MinerU API 走系统代理 |

### 6.2 macOS

| 项 | 状态 |
|---|---|
| macOS 14 Sonoma | ✅ 测试 |
| macOS 13 Ventura | ✅ 测试 |
| macOS 12 Monterey | ⚠️ PyWebView 4.x 兼容 |
| Apple Silicon (M1/M2/M3) | ✅ 原生 |
| Intel Mac | ⚠️ 用 universal2 binary |
| code signing | ⚠️ 第一次需要开发者账号（$99/年） |
| notarization | ⚠️ 第一次需要 notarytool |

### 6.3 字体兼容性

PDF 解析结果用 system-ui 字体，跨平台 fallback：
- Win: 微软雅黑 → 苹方 fallback
- Mac: 苹方 → 微软雅黑 fallback
- 不打包 woff2（HTML 体积 < 500KB 优先）

---

## 7. 自动更新

### 7.1 更新流程

```
[app 启动时]
    │
    ▼
[读 %APPDATA%/exam-to-html/config.json: last_check_ts]
    │
    │ if (now - last_check_ts) > 24h:
    │   GET https://exam-to-html.yourname.com/version.json
    │   {
    │     "latest_version": "1.2.0",
    │     "current_version": "1.0.0",
    │     "download_url": "https://github.com/.../exam-to-html-1.2.0-win.exe",
    │     "release_notes": "修复了..."
    │   }
    │
    ▼
[如果 latest > current]
    │
    ▼
[高级设置: "📦 检查更新"按钮变成 "🆕 新版本可用: 1.2.0"]
    │
    │ 点按钮 → 打开浏览器到 download_url
    ▼
[教师手动下载 + 安装新版本]
```

**关键**：更新**不自动**下载安装（避免教师电脑被自动改）。只在高级设置显示提示，由教师主动下载。

### 7.2 version.json 部署

- 简单：GitHub Pages + `version.json`
- 正式：CDN + `version.json`
- v1.0 用 GitHub Pages（0 成本）

---

## 8. 安全 + 隐私

### 8.1 隐私承诺

- **100% 本地处理**：PDF 不上传到任何第三方服务（除了 MinerU API 调用时）
- **DB 本地存储**：所有题库数据存教师自己电脑
- **HTML 本地生成**：不经过任何服务器
- **可选 MinerU token**：教师自己注册、自己填、自己付费

### 8.2 数据流隔离

```
教师电脑
├── %APPDATA%/exam-to-html/
│   ├── config.json           ← token / 输出位置
│   ├── db.sqlite3            ← 题库（全部本地）
│   ├── inbox/                ← 待处理 PDF（处理后删除）
│   └── logs/                 ← 错误日志
│
└── 桌面/
    └── 高一期中试卷.html     ← 输出课件（教师控制）
```

### 8.3 不做的事

- ❌ 不收集使用统计
- ❌ 不上传 PDF 到我们自己的服务器
- ❌ 不内置广告 / 推广链接
- ❌ 不写注册表黑魔法（macOS: 不写 LaunchAgent）

---

## 9. 里程碑

| 阶段 | 工期 | 交付 |
|---|---|---|
| **M0** 设计 | 0.5 天 | 本文档 |
| **M1** 骨架 | 1 天 | 最小可跑 .exe（黑窗口 + 拖 PDF + 出 HTML） |
| **M2** UI 打磨 | 2 天 | PyWebView 单页 UI + 拖拽高亮 + 进度条 |
| **M3** 错误处理 | 1 天 | 错误矩阵全部覆盖 |
| **M4** 跨平台 | 1 天 | Mac 出包 + Win 出包 + README |
| **M5** 自动更新 | 0.5 天 | version.json + 检查更新按钮 |
| **M6** 种子用户测试 | 3-5 天 | 3 个种子教师实跑 + 反馈收集 |
| **v1.0** 发布 | - | GitHub Release + .exe + README |

---

## 10. 未来扩展（v1.0 不做）

- 批量 PDF（一次拖多份）
- 历史记录（之前的解析结果可重新打开）
- 题目库搜索（教师复用之前的题）
- 班级标签（同一份 HTML 输出多份给不同班级）
- 多教师协作（云端 DB）—— **明确不做**，违反隐私承诺

---

## 11. 风险

| 风险 | 概率 | 影响 | 应对 |
|---|---|---|---|
| PyWebView 在 Win7 不支持 | 高 | 部分教师装不上 | 明确只支持 Win10/11，README 写清楚 |
| PyInstaller 打包后 sqlite 锁 | 中 | 多窗口打开时冲突 | 单实例锁（PyWebView 启动时检查） |
| MinerU API 价格变化 | 中 | 教师成本变化 | 保持 flash mode 永远可用 |
| topic_garden_app 库 API 变动 | 低 | 引用失败 | 锁定 v0.18.4 版本，定期 bump |
| 教师电脑装了 360 / 杀毒软件 | 高 | PyInstaller exe 误报 | code sign + 白名单提交 |
| PDF 解析质量参差 | 中 | 教师不满意 | flash mode 输出已经够用；token 是升级路径 |