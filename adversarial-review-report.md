# 解析-渲染流水线对抗性审查报告

**审查范围**: `pdf2ppt/` 解析端 + `exam_to_html/backend/` 渲染端
**审查方式**: 两个独立 Oracle (correctness lens + edge cases lens) 并行 + 本地交叉验证
**审查日期**: 2026-07-10
**状态**: 仅报告,未修复

---

## 🔴 High 严重性 (会崩 / 丢数据 / 安全漏洞)

### H-1: `split_inline_options` 用 cleaned 索引切原 line — LaTeX 内容丢失
**文件**: `exam_to_html/backend/_post_process_md.py:99-110`
```python
cleaned = re.sub(r"\$[^$]*\$", "", line)          # $...$ 被移除
matches = list(_INLINE_OPT_SPLIT.finditer(cleaned)) # 索引基于 cleaned
# ...
seg_start = m.end()                                 # cleaned 里的位置
seg_end = matches[i + 1].start() if i + 1 < len(matches) else len(line)
body = line[seg_start:seg_end].strip()              # 但切的是原 line
```
**触发**: 一行内挤 4 个选项且含 LaTeX,如 `A. $\frac{1}{2}$ B. $\frac{1}{3}$ C. $\frac{1}{4}$ D. $\frac{1}{5}$`。`cleaned` 长度 < `line`,索引错位,所有公式内容被切空。选项 A 渲染成 `A. ` 无公式。
**影响**: 任何含公式的选择题只要选项挤在一行,公式全部丢失——这是物理卷的常见形态。
**来源**: correctness lens

### H-2: `_reassign_images_by_y_center` 中 `pages` 变量未定义
**文件**: `pdf2ppt/_v2_parser.py:1698-1702`
```python
if all_images:
    pages = set(img['page_idx'] for img in all_images)

for page in sorted(pages):   # ← all_images 空但 orphan_images 非空时 NameError
```
**触发**: PDF 所有图片都缺 bbox(全是 orphan)。`if not all_images and not orphan_images: return` 早退条件不满足(orphan 非空),进到 `if all_images:` 为 False,`pages` 从未赋值,下一行 `sorted(pages)` 抛 `NameError`。
**影响**: A3 flash 路径下任何"所有图都无 bbox"的 PDF 直接崩溃。
**来源**: correctness lens

### H-3: `api_clear_incomplete` 路径穿越 — 任意文件删除
**文件**: `exam_to_html/backend/server.py:242-249` + `exam_to_html/backend/pipeline.py:214-222`
```python
paths = payload.get("paths", [])     # 用户任意输入
# pipeline.py
for p in paths:
    Path(p).unlink(missing_ok=True)  # 无目录白名单
```
**触发**: `POST /api/incomplete-uploads/clear` body `{"paths": ["/Users/zhewenliu/.ssh/id_rsa"]}` → 删除任意文件。
**影响**: 本地 API 但任何能访问 localhost 的进程都可利用。
**来源**: edge cases lens

### H-4: 上传无大小限制 — OOM
**文件**: `exam_to_html/backend/server.py:114`
```python
content = await file.read()           # 全量读入内存
tmp_pdf.write_bytes(content)
```
**触发**: 上传 2GB 文件。`MAX_PDF_SIZE_BYTES=100MB` 检查在 pipeline._check_file_size,发生在文件已全量驻留内存之后。
**影响**: 单次大上传即 OOM kill。
**来源**: edge cases lens

### H-5: `api_open_html` 路径穿越 — 任意文件打开
**文件**: `exam_to_html/backend/server.py:176-191`
```python
def api_open_html(path: str):
    p = Path(path)
    if not p.is_file(): raise ...
    subprocess.Popen(["open", str(p)])   # 无目录/后缀校验
```
**触发**: `POST /api/open-html?path=/etc/passwd`。
**来源**: edge cases lens + 本地交叉验证

### H-6: `output_dir` 参数路径穿越 — 任意目录写入
**文件**: `exam_to_html/backend/server.py:108`
```python
out_dir = Path(output_dir) if output_dir else app_config.resolve_output_dir(cfg)
```
**触发**: `POST /api/convert` with `output_dir=/etc`。`_check_output_dir` 只检查可写,不检查路径范围。
**来源**: edge cases lens

### H-7: 0 页 A3 PDF 崩溃
**文件**: `pdf2ppt/_v3_a3_splitter.py:84-86` + `pdf2ppt/_v2_parser.py:483`
```python
# splitter
if total_pages == 0: doc.close(); return None
# parser
long_pdf = splitter.merge_to_long_pdf(pdf_path)
print(f"  📄 生成 A3 长PDF，共 {len(fitz.open(long_pdf))} 页")  # fitz.open(None) crash
```
**触发**: 0 页 A3 宽幅 PDF(宽度 ≥900pt 但无页面)。`is_a3_pdf` 用 `doc[0]` 会 IndexError 被 except 吞掉返回 False,所以 0 页走不到 A3 路径——但若有 1 页空壳 A3 PDF,`is_a3_pdf` 返回 True,`merge_to_long_pdf` 在 split 阶段产生 0 页 long PDF,返回 None,下一行崩。
**来源**: edge cases lens

### H-8: `render_formulas` LaTeX 未 HTML 转义 — XSS
**文件**: `pdf2ppt/_katex_renderer.py:165`
```python
items.append(f'<div class="{cls}" data-id="{i}">{latex}</div>')  # latex 原样拼入
```
对比 `render_text_blocks` line 60-63 做了 `&<` 转义,`render_formulas` 没有。
**触发**: PDF 嵌入恶意 LaTeX 如 `\text{<script>alert(1)</script>}`,被 parser 提取后作为 latex 传入。最终 HTML 教师双击打开即触发。
**来源**: edge cases lens

### H-9: `final_title` 未 HTML 转义 — XSS
**文件**: `exam_to_html/backend/exam_renderer.py:537`
```python
return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <title>{final_title} — 试卷讲评</title>   <!-- final_title = pdf_path.stem -->
```
**触发**: PDF 文件名含 `<script>` 等标签,如 `物理<script>alert(1)</script>.pdf`。f-string 不走 Jinja2 autoescape。
**来源**: edge cases lens + 本地交叉验证

---

## 🟡 Medium 严重性 (功能受损可绕过 / 资源泄漏)

### M-1: `reattach_option_prose` Shape B 多行 prose 全落最后一个选项
**文件**: `exam_to_html/backend/_post_process_md.py:234-244`
```python
for i, prose_line in enumerate(pending_prose):
    target_idx = new_idx + i
    if target_idx < len(out):           # B/C/D 还没 append,out 长度不够
        out[target_idx] = ...
    else:
        out[new_idx] = (out[new_idx].rstrip() + prose_line).strip()  # 全堆到 A
```
**触发**: 题干后 2+ 行尾句游离,后跟 `A./B./C./D.`。prose[0] 给 A,prose[1] 本应给 B 但 B 还没 append,走 else 全堆到 A。
**影响**: 选项内容错位。
**来源**: correctness lens

### M-2: A3 path `column_map` 是死代码,用粗暴"题号≤10 左栏"启发式
**文件**: `pdf2ppt/_v2_parser.py:521-576`
```python
column_map = {}  # line 521-544 扫描 P0L/P0R 标记,建完只用 print
# ...
if max_q <= 10: mid_idx = max_q  # line 563 — 所有题左栏
```
**触发**: A3 PDF 题数 ≤10 但实际分双栏。所有题被赋 `column=0`,右栏图片全部错配到左栏题。
**影响**: A3 模式下图归属错乱。
**来源**: correctness lens

### M-3: `min(q_nums)` 空序列崩溃
**文件**: `pdf2ppt/_v2_parser.py:577`
```python
q_nums = [q.index for q in sorted_qs if q.index]   # q.index=0 被过滤
if q_nums: max_q = max(q_nums); ...
print(f"  📌 题目题号范围: {min(q_nums)}-{max_q}...")  # 在 if q_nums 外
```
**触发**: A3 PDF 所有题 `index=0`(parser 没抽出题号)。`q_nums=[]`,`min([])` 抛 `ValueError`。
**来源**: correctness lens

### M-4: `_rebuild_questions_with_images` 用 `id(b) < id(block)` 排序图片
**文件**: `pdf2ppt/_v2_parser.py:857-860`
```python
img_idx = sum(1 for b in exam.raw_blocks
              if b.block_type == "image"
              and b.page_idx == page_idx
              and id(b) < id(block))   # ← CPython 内存地址,不保证 = 分配顺序
```
**触发**: GC 后地址复用,`id()` 顺序不保证反映插入顺序。y_center 估算错位,图归属错题。
**来源**: correctness lens

### M-5: A3 precision path 用启发式覆盖正确的 column
**文件**: `pdf2ppt/_v2_parser.py:546-576`
`_split_into_questions` 已基于 `page_idx % 2` 正确赋 `column`(line 1360-1366),但 line 546-576 的"题号≤10"启发式又把 column 重写掉。
**来源**: correctness lens

### M-6: `_assign_images_to_questions` 多次调用相互覆盖 `_q_y`/`_y_range`
**文件**: `pdf2ppt/_v2_parser.py:906-923`
左栏调用算一次 `_q_y`,右栏调用覆盖,孤儿调用再用全量题集覆盖。孤儿图分配用的是全量题的 y 范围,不是对应栏的。
**来源**: correctness lens

### M-7: `b not in removed` 用 dataclass `==` 而非 identity
**文件**: `pdf2ppt/_v2_parser.py:1466`
```python
q.blocks = [b for b in q.blocks if b not in removed or b.block_type != "image"]
```
**触发**: 一题含 2 个完全相同的 image block(同 img_path/bbox/page_idx),`not in` 用 `==` 会把两个都删,即使只有一个在 `removed`。
**对比**: line 1778 用 `b is not img['block']`(identity)是正确的——不一致。
**来源**: correctness lens

### M-8: `_parse_a3_pdf` 临时 `long_pdf` 异常路径泄漏
**文件**: `pdf2ppt/_v2_parser.py:482, 582-585`
`os.unlink(long_pdf)` 不在 `finally` 块,中间任何异常都泄漏一个 PDF 到 temp 目录。
**来源**: edge cases lens

### M-9: 临时图片文件从不清理(3 处)
**文件**: `pdf2ppt/_v2_parser.py:713-717, 783-786, 1150-1154`
```python
tmp = tempfile.NamedTemporaryFile(suffix=ext, prefix="mineru_", delete=False)
tmp.write(img.data); tmp.close()
# tmp.name 永不删除
```
**影响**: 长期运行逐渐填满磁盘。
**来源**: edge cases lens

### M-10: GLM4VParser 多重资源泄漏
**文件**: `pdf2ppt/_v2_parser.py:1873-1993`
`doc` 无 try/finally,`img`(PIL)/`buf`(BytesIO)/`resp`(requests)在循环里从不 close。第 10 页 API 超时 → 前 10 个资源全泄漏。
**来源**: edge cases lens

### M-11: GLM4VParser API 响应无错误处理
**文件**: `pdf2ppt/_v2_parser.py:1939-1940`
```python
result = resp.json()                                    # JSONDecodeError
content = result["choices"][0]["message"]["content"]    # KeyError/IndexError
```
**触发**: API 返回错误结构如 `{"error": {...}}`。
**来源**: edge cases lens

### M-12: fitz doc 异常路径不 close(多处)
**文件**: `pdf2ppt/_v2_parser.py:523-542, 621-641, 1128-1165` + `pdf2ppt/_v3_a3_splitter.py:29, 47, 81` + `pdf2ppt/_qnum_rule.py:264-297`
模式都是 `doc = fitz.open(...)` + `doc.close()` 在 try 块末尾,except 吞掉异常但不 close。
**来源**: edge cases lens

### M-13: Playwright browser 异常路径不 close(2 处)
**文件**: `pdf2ppt/_katex_renderer.py:121-149, 209-233`
`browser.close()` 不在 finally,`page.set_content()` 超时即泄漏 chromium 进程。
**来源**: edge cases lens + 本地交叉验证

### M-14: `convert_pdf` compose 失败后 Topic 残留无回滚
**文件**: `exam_to_html/backend/pipeline.py:379-420`
Topic.create + add_topic_question 成功后 compose/write 失败,Topic 变孤儿。重试创建新 Topic,旧 Topic 永留 DB。
**来源**: edge cases lens

### M-15: `_ensure_images_link` 过期 symlink 未检测
**文件**: `exam_to_html/backend/pipeline.py:70-74`
```python
target.resolve(strict=False)   # strict=False → 目标不存在也返回
return target                   # 返回过期 symlink
```
**触发**: 第一次创建 symlink 后 `courseware/images` 被删/移动。第二次运行返回过期 symlink,HTML 中 `<img src="images/...">` 全 404。
**来源**: edge cases lens

### M-16: `_jobs` 内存 dict 无限增长
**文件**: `exam_to_html/backend/server.py:50-51`
无 TTL、无 max size、无清理。
**来源**: edge cases lens

### M-17: `self._debug_mode` / `self._client` 实例字段并发竞态
**文件**: `pdf2ppt/_v2_parser.py:380-389`
**触发**: server.py 用 `threading.Thread` 并发跑 `convert_pdf`。若 topic_garden 复用同一个 `MinerUParser` 实例,`_debug_mode` 布尔竞态 + `_client` MinerU SDK 客户端线程安全性未知。
**不确定**: 是否真触发取决于 topic_garden 是否复用 parser 实例——需查 `process_inbox` 内部。
**来源**: edge cases lens

### M-18: `_parse_a3_pdf` line 483 fitz 文件句柄泄漏
**文件**: `pdf2ppt/_v2_parser.py:483`
```python
print(f"  📄 生成 A3 长PDF，共 {len(fitz.open(long_pdf))} 页")
```
`fitz.open()` 返回的 Document 被 `len()` 消费后丢弃,不 close。
**来源**: edge cases lens

### M-19: precision / GLM4V 路径 `page_count` / `page_sizes` 未设
**文件**: `pdf2ppt/_v2_parser.py:664(_parse_precision), 1862(GLM4VParser.parse)`
precision 不调 `_extract_images_from_pdf`,`page_count=0`,`page_sizes=[]`。GLM4V 设了 `page_count` 但图片从不关联到题目(全留 raw_blocks)。
**来源**: correctness lens

---

## 🟢 Low 严重性 (理论问题 / 难触发 / 代码异味)

### L-1: `_qnum_fallback.py:248` 行尾注释粘贴错位
**文件**: `exam_to_html/backend/_qnum_fallback.py:248`
```python
# =========================================================
_MAX_QNUM = 50   # 注释挤在代码行尾,且 _MAX_QNUM 从未被引用
```
注释块标题被粘到代码行,且 `_MAX_QNUM=50` 是死代码(实际阈值在各 `_match_*_qnum` 内硬编码 50)。
**来源**: 本地交叉验证

### L-2: `text_level` 字段被复用存栏位信息
**文件**: `pdf2ppt/_v2_parser.py:1161`
```python
text_level=1 if is_right_column else 0,  # 用 text_level 暂存栏位: 1=右栏
```
`text_level` 在 `pdf2ppt/_v2_models.py:18` 文档为"0=正文,1=一级标题"。语义重载,下游若按文档语义读 image block 的 text_level 会得到错误值。
**来源**: correctness lens

### L-3: `_clean_ocr_noise` 误删合法 4 位数字
**文件**: `exam_to_html/backend/_post_process_md.py:405`
```python
re.sub(r'\s+\d{4,}\s*[A-Z]?\d*\s*', ' ', text)
```
**触发**: 题干含 ` 1024 Pa` 或 ` 7600 V` 等合法 4 位物理量。
**来源**: correctness lens

### L-4: `_parse_a3_pdf` 静默吞异常
**文件**: `pdf2ppt/_v2_parser.py:590-595`
```python
except Exception: pass   # _scan_page_layout_for_first_pages 任何 bug 被吞
```
**来源**: edge cases lens

### L-5: `api_post_config` 无值校验
**文件**: `exam_to_html/backend/server.py:199-207`
`{"output_dir": "/dev/null"}` 任意值直接写入 config.json。
**来源**: edge cases lens

### L-6: `normalize_question_batch` 无事务
**文件**: `exam_to_html/backend/_post_process_md.py:457-461`
每题独立 update,中断后部分归一化部分未归一化,状态不一致。
**来源**: edge cases lens

### L-7: `_wrap_more_latex` placeholder 理论碰撞
**文件**: `exam_to_html/backend/exam_renderer.py:428`
`placeholder = "\x00K{}X\x00"` 用 `str.format`,若 HTML 含字面 `\x00K0X\x00` 会被误替换。null byte 极罕见,理论风险。
**来源**: edge cases lens + 本地交叉验证

### L-8: `_read_assets` 无 BOM 处理
**文件**: `pdf2ppt/_katex_renderer.py:20`
`read_text(encoding="utf-8")` 不剥 BOM。KaTeX 资源是 vendored,极低风险。
**来源**: edge cases lens

### L-9: `_split_into_questions` 题前 equation block 静默丢失
**文件**: `pdf2ppt/_v2_parser.py:1320-1323`
无 current_question 时 equation block 走 `continue` 被丢。卷首全局公式会丢。
**来源**: correctness lens

### L-10: `_parse_markdown` PAGE_MARKER 误匹配
**文件**: `pdf2ppt/_v2_parser.py:975`
`^P(\d+)([LR])$` 匹配纯文本 `P0L`。物理题涉及页引用时极罕见误杀。
**来源**: correctness lens

---

## 修复优先级建议(不修复,仅排序)

| 优先级 | Bug | 理由 |
|---|---|---|
| P0 立即 | H-1, H-2 | 静默丢内容/崩溃,用户无感知 |
| P0 立即 | H-3 ~ H-7 | 安全漏洞,localhost 也有风险 |
| P0 立即 | H-8, H-9 | XSS,最终 HTML 双击即触发 |
| P1 近期 | M-1 ~ M-7 | 内容错位/图归属错,影响输出质量 |
| P1 近期 | M-8 ~ M-13 | 资源泄漏,长期运行退化 |
| P2 排期 | M-14 ~ M-19 | 孤儿数据/竞态,需特定条件触发 |
| P3 顺手 | L-1 ~ L-10 | 代码异味/理论风险 |

---

## 关键不确定点(需验证)

1. **M-17**: topic_garden `process_inbox` 是否复用 `MinerUParser` 实例?复用则有并发竞态。
2. **H-1**: 实际 PDF 解析后 `content_md` 是否会出现"一行 4 选项+LaTeX"形态?若 parser 已拆行则不触发。
3. **H-7**: 1 页空壳 A3 PDF 能否实际产生?需构造测试。

---

## 审查方法说明

- **correctness lens Oracle**: 状态突变、类型混淆、off-by-one、控制流不对称、A3 vs 单栏路径差异
- **edge cases lens Oracle**: 边界输入、部分失败、资源泄漏、并发竞态、路径穿越、XSS
- **本地交叉验证**: 直接读 `pipeline.py` / `exam_renderer.py` / `_post_process_md.py` / `_qnum_fallback.py` / `server.py` / `_v2_models.py` / `exam.html` / `_katex_renderer.py`,交叉验证 topic_garden `db.py` 的 Question schema
- 去重合并: 两个 Oracle 重复报的 bug 合并为一条,保留更精确的行号
