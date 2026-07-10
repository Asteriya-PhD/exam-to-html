"""exam_to_html.backend._post_process_md — K2/K3 题干层归一化。

仅做**已有内容**的结构化/归一化；不补 OCR 丢失内容。
调用方在入库后、`Topic.create` 前对本次 PDF 关联的 Question 行调用一次。
"""
from __future__ import annotations

import logging
import re
from typing import Iterable, List, Optional, Sequence

log = logging.getLogger(__name__)


# ============================================================
# 正则常量
# ============================================================
# 同行内嵌 4 选项 A./B./C./D. (e.g. "A. 5 m/s B. 10 m/s C. 15 m/s D. 20 m/s")
# 要求: 选项标签必须是 ASCII A-D,后跟 `.` `．` 或 `、` 之一
_INLINE_OPT_SPLIT = re.compile(r'(?<![A-Za-z}])(\b[ABCD][\.．、]\s)')

# 题型判定的关键字 (K3/K2 物理卷)
_CALCULATION_HINTS = ("求:", "求：", "计算:", "计算：", "试求", "试求：")
# 实验题特征 — 只匹配**强实验语境**,避免"利用X作为Y"(描述物理装置)误触发
_STRONG_EXPERIMENT = (
    "利用单摆测",        # "利用单摆测重力加速度"
    "实验得到",
    "实验中",
    "用游标卡尺",
    "用秒表",
    "实验步骤",
    "实验数据",
)
# 弱实验信号 — 单独出现不算,需配合"测/量/数据/实验"等强词
_WEAK_EXPERIMENT = ("利用", "如图甲", "如图乙")
# 强实验词 — 必须是实验操作短语,不是一般物理术语
# "量" 单独出现不能用 — "质量/动量" 是一般物理术语,不是实验信号
_EXPERIMENT_STRONG_PHRASES = (
    "测量", "测得", "测出", "量得",  # 测量操作
    "实验", "摆长", "周期", "游标卡尺",  # 实验装置/过程
    "记录", "数据", "秒表", "米尺",  # 实验数据/工具
)
# 分值标记 (X分) — 强计算题信号,不单独用,但可覆盖实验误判
_SCORE_MARKER_RE = re.compile(r"[（(]\s*\d+\s*分\s*[）)]")
_SUBQ_HINTS = (
    r"[（(]\s*[1-9]\d?\s*[)）]",   # (1) (2) (3) / （1）
    r"第\s*[一二三四五六七八九十]+\s*问",  # 第一问 / 第二问
)
_SUBQ_RE = re.compile("|".join(_SUBQ_HINTS))

# 默认 fallback 题型 — 决定不会被解析成 choice 时的归宿
_FALLBACK_TYPE = "fill_blank"

# 选项行严格模式 (K2 选项换行 / 真选项边界)
# - 行首 (允许前置空白) + (可选 (/) + A-D + 可选 )/) + . / ． / 、 + 后接内容
# - 与 pdf2ppt/_v2_parser.py 的 OPTION_PATTERN 一致 (认 (A) / （A） / A.)
_OPTION_LINE_RE = re.compile(r"^\s*[（(]?[ABCD][)）]?[.．、\s]\s*\S")


def _is_inline_options_line(text: str) -> bool:
    """判断一段字符串是否是"挤在一行内的 4 个选项"。

    启发: 行内出现 ≥ 3 个 A-D 标签 + 标签是选项起始 (`.` `．` `、`)
    且至少一段前没有 LaTeX 公式包裹的 `}` (避免把 $\\frac{A}{...}$ 误识)。
    """
    if not text:
        return False
    # 去掉 $...$ 内的字符再统计
    cleaned = re.sub(r"\$[^$]*\$", "", text)
    labels = _INLINE_OPT_SPLIT.findall(cleaned)
    if len(labels) < 3:
        return False
    # 3-4 个选项都要是同一行的开头片段 (A./B./C./D. 起)
    starts = [m.start() for m in _INLINE_OPT_SPLIT.finditer(cleaned)]
    if not starts or starts[0] > 30:
        # 行首 30 字符内应当有第一个选项 (允许题号 + 简短题干尾, e.g. "1. 求末速度 A. ...")
        return False
    return True


def split_inline_options(content_md: str) -> str:
    """将同一行内挤的 4 个选项拆为每个一行;**不动**题干。

    规则: 整段 `content_md` 按 `\n` 切行,若某行匹配 `_is_inline_options_line`
    才执行拆分。其他行原样保留。**不会**编造选项内容。

    实现: 找出所有 A-D 标签的 (start, end) 位置,按段切分 —
    段 1 = 行首到第一个标签 end;段 2 = 第一个标签 end 到第二个标签 start;...
    标签 (含标点) 跟在前段的末尾一起输出,因此不会丢 `A.` 前缀。
    """
    if not content_md:
        return content_md
    out_lines: List[str] = []
    for line in content_md.split("\n"):
        if not _is_inline_options_line(line):
            out_lines.append(line)
            continue
        # 用同一 regex 的 finditer 拿所有 A-D 标签的 (start, end)
        cleaned = re.sub(r"\$[^$]*\$", "", line)
        matches = list(_INLINE_OPT_SPLIT.finditer(cleaned))
        if len(matches) < 2:
            out_lines.append(line)
            continue
        # 第一个 A-D 标签之前可能是题干尾,作为 prefix
        first = matches[0]
        prefix = line[: first.start()].rstrip()
        opts: List[str] = []
        for i, m in enumerate(matches):
            seg_start = m.end()  # 标签 `A. ` 之后
            seg_end = matches[i + 1].start() if i + 1 < len(matches) else len(line)
            body = line[seg_start:seg_end].strip()
            if not body:
                continue
            opts.append(f"{m.group(0).rstrip()} {body}".rstrip())
        rebuilt = (prefix + "\n" if prefix else "") + "\n".join(opts)
        out_lines.append(rebuilt)
    return "\n".join(out_lines)


# Q7-style orphan prose: 选项的尾句游离到下一个 A./B./C./D. 标签之前。
# MinerU flash 把单选/多选 PDF 拆成
#   "A.<选项主体首句>\n<选项主体尾句>\nB.<选项主体首句>\n<选项主体尾句>\n..."
# 我们要识别"上一选项的尾句"(在下一个 A-D 标签前没有 A./B./C./D. 开头,
# 且不像题干),并把它合并回上一选项。
_OPTION_HEAD_RE = re.compile(r"^\s*[（(]?[ABCD][)）]?[.．、\s]\s*\S")


def _looks_like_option_prose(line: str) -> bool:
    """一段不含 A-D 标签、不是题号行、长度合理的文本,很可能是上一选项的尾句。

    启发 (Q7 真实 PDF 抽取后的形态):
      - 不以数字题号 (1. / 1．) 开头
      - 不以 A-D 选项标签开头
      - 长度 ≥ 6 字符 (过短如 '题干'/'甲乙丙' 都是独立标签)
      - 含典型选项尾句模式:
        * 包含 LaTeX 公式 ($...$ 或 \\(...\\))
        * 含中文逗号 (, , 。) 或 是非判断 (是/为/的/了/在/和/与)
        * 以连词结尾 (则 / 比为 / 是 / 为 / 速度 / 摆 / 已知 / 求)
        * 含数字/单位 (cm/m/s/N/W/J/kg)
      - 不包含 "如图" (图引用归题干)
      - 不以单字符图标签开头 (甲/乙/丙/丁/A/B/I/II)
    """
    if not line:
        return False
    s = line.strip()
    if not s or len(s) < 6:
        return False
    if _OPTION_HEAD_RE.match(s):
        return False
    # 题号行 (1. / 1． / 1、) — 不合并
    if re.match(r"^\s*\d{1,2}[\.．、]\s", s):
        return False
    # 中文题号 (一二、) — 不合并
    if re.match(r"^\s*[一二三四五六七八九十]+[、．]\s", s):
        return False
    # 图引用 — 不合并 (题干中的"如图所示")
    if "如图" in s:
        return False
    # 排除纯图标签行 ("甲" / "乙" / "丙" / "丁" / "A" / "B")
    if s in ("甲", "乙", "丙", "丁", "A", "B", "I", "II", "甲.", "乙.", "丙.", "丁."):
        return False
    # 必须含"延续"信号 — LaTeX 公式 或 中文句末标点
    has_latex = bool(re.search(r"\$[^$]*\$", s))
    has_cjk_punct = any(p in s for p in ("，", "。", "；", "：", ",", ";", ":"))
    has_unit = bool(re.search(r"\d+\s*(cm|m|s|N|W|J|kg|°)", s))
    has_continuation_word = any(
        w in s for w in (
            "则 ", "则是", "比为", "之比", "之 ", "约为", "约为 ",
            "已知", "求 ", "求得", "求得 ",
        )
    )
    return has_latex or has_cjk_punct or has_unit or has_continuation_word


def reattach_option_prose(content_md: str) -> str:
    """修复 Q7-style 选项尾句游离 (从真实 PDF run 发现的 bug)。

    MinerU flash 把选项跨两行的 PDF 抽出后,会出现两种"尾句游离":

    Shape A — 尾句在选项之间:
        A.<主体首句>
        <尾句 A>
        B.<主体首句>
        ...
    → 尾句 A 应拼回 A。

    Shape B — 尾句在所有选项之前 (Q7 实际抽取后):
        [题干]
        <尾句 A>
        <尾句 B>
        A.<主体首句 A>
        B.<主体首句 B>
        ...
    → 尾句 A → A, 尾句 B → B (按出现顺序分配到对应选项)

    算法:
    - 走单遍。遇到 A-D 选项行: 如果上一 out 行**不是**选项(题干/图
      引用/题号),pending_prose 是 Shape B,按序分配到 A/B/...。
    - 如果上一 out 行**是**选项(Shape A),pending_prose 拼到上一选项。
    """
    if not content_md:
        return content_md
    lines = content_md.split("\n")
    out: List[str] = []
    pending_prose: List[str] = []
    last_option_idx: Optional[int] = None
    option_seq = ["A", "B", "C", "D", "E", "F"]
    for line in lines:
        if _OPTION_HEAD_RE.match(line):
            out.append(line)
            new_idx = len(out) - 1
            if pending_prose:
                if (
                    last_option_idx is not None
                    and last_option_idx == len(out) - 2
                ):
                    # Shape A: 上一行是选项 → prose 拼到上一选项
                    out[last_option_idx] = (
                        out[last_option_idx].rstrip()
                        + "".join(pending_prose)
                    ).strip()
                else:
                    # Shape B: 上一行不是选项 → prose 按出现顺序分配到对应选项
                    # 取当前选项字母 (e.g. "B.") 决定起始 slot
                    cur_letter = _OPTION_HEAD_RE.match(line).group(0).strip()[0].upper()
                    start_idx = option_seq.index(cur_letter) if cur_letter in option_seq else 0
                    # 把 pending_prose 顺序分配到 A→B→C→...
                    # 但要确保分配不会超出当前选项的范围:
                    # 当前是 cur_letter, prose 有 N 行 → 把 prose[0..N-1] 拼到 out[new_idx..new_idx+N-1]
                    # 注意: out[new_idx..] 已经包含本选项头,我们只 append 后续 prose
                    # 实际上,只分配 prose[0..N-1] 中从 start_idx 开始的 slot
                    # 简化:把 prose[i] 拼到 out[new_idx + i] (i 从 0 开始,但第 0 个是当前选项)
                    # 实际逻辑:prose[0] 拼到 out[new_idx] (A 尾),prose[1] 拼到 out[new_idx+1] (B 尾) ...
                    for i, prose_line in enumerate(pending_prose):
                        target_idx = new_idx + i
                        if target_idx < len(out):
                            out[target_idx] = (
                                out[target_idx].rstrip() + prose_line
                            ).strip()
                        else:
                            # prose 多于选项数 — 落到最后一个选项 (兜底)
                            out[new_idx] = (
                                out[new_idx].rstrip() + prose_line
                            ).strip()
                pending_prose = []
            last_option_idx = new_idx
        elif _looks_like_option_prose(line):
            pending_prose.append(line.strip())
        else:
            out.append(line)
            last_option_idx = None
    return "\n".join(out)


def normalize_options_to_lines(content_md: str) -> str:
    """将 A./B./C./D. 选项在 content_md 末尾规范成每行一个。

    - 如果末尾已有 ≥2 个独立行以 A-D 开头,保持原样。
    - 如果末尾同一行挤了 4 个选项,拆成 4 行。
    - 如果有"X. A. 选项 / Y. B. 选项"嵌套编号 (e.g. PDF 子题 "X. 1) A. ..."),
      保留嵌套结构,只对外层 A-D 标号做换行。
    """
    return split_inline_options(content_md)


def detect_q_type(content_md: str, current: Optional[str] = None) -> str:
    """根据 content_md 推断更可解释的题型,避免 unknown/[?]。

    优先级 (保守, 不会把已知 choice 改掉):
    1. 已有合法 choice → 'choice'
    2. 实验题特征 (图甲/图乙/利用 ... 实验/用游标卡尺/用秒表) → 'experiment'
    3. 分值标记 (X分) → 'calculation' (覆盖实验误判)
    4. 含 `求:` / `计算:` / `试求` + 子问号 (1)(2)(3) → 'calculation'
    5. 含 (1)/(2)/(3) 子问号 (无 choice 标签) → 'calculation'
    6. 已是 fill_blank / calculation / experiment → 保留
    7. 兜底 → 'fill_blank' (避免 unknown 触发 [?])
    """
    if not content_md:
        return current or _FALLBACK_TYPE
    text = content_md.strip()
    # 1) 选择题判定: 行首 A/B/C/D 选项 ≥ 2 个,且每项长度合理 (避免 ABCD 全拼误判)
    opt_lines = [l for l in text.splitlines() if _OPTION_LINE_RE.match(l)]
    if len(opt_lines) >= 2 and _looks_like_real_options(opt_lines):
        return "choice"
    # 2) 实验题判定
    if _is_experiment_text(text):
        return "experiment"
    # 3) 分值标记 (X分) — 计算题强信号 (覆盖"如图甲"误判)
    if _SCORE_MARKER_RE.search(text):
        return "calculation"
    # 4) 计算题判定
    if _has_calc_hint(text):
        return "calculation"
    # 5) 子问号
    if _SUBQ_RE.search(text):
        return "calculation"
    # 6) 已有题型保留
    if current in ("choice", "fill_blank", "calculation", "experiment"):
        return current
    # 7) 兜底
    return _FALLBACK_TYPE


def _is_experiment_text(text: str) -> bool:
    """实验题特征判定 (Q11/Q12 模式)。

    策略 (v0.20 修复 Bug A):
    1. 强实验信号命中 → experiment (如"用游标卡尺"/"用秒表"/"利用单摆测")
    2. 弱信号 ("利用"/"如图甲/乙") 单独不触发 — 需配合"测/量/数据"等强词
    3. 分值标记 "(X分)" 通常出现在计算题, 可覆盖弱实验信号
    """
    if not text:
        return False
    # 1) 强实验信号
    if any(h in text for h in _STRONG_EXPERIMENT):
        return True
    # 2) 弱信号 + 强实验词
    has_weak = any(h in text for h in _WEAK_EXPERIMENT)
    has_strong_phrase = any(w in text for w in _EXPERIMENT_STRONG_PHRASES)
    if has_weak and has_strong_phrase:
        return True
    # 3) 分值标记 (X分) → 计算题, 反驳 experiment
    if _SCORE_MARKER_RE.search(text):
        return False
    return False


def _looks_like_real_options(options) -> bool:
    """复用 pdf2ppt._v2_parser._looks_like_real_options 的判定,避免 ABCD 全拼被当选项。

    规则 (与 vendored parser 保持一致):
      - ≥ 3 个,字母顺序 A→B→C→D 单调
      - 每项 2-100 字符,以 A-D 开头
    """
    if not options or len(options) < 3:
        return False
    last_letter = None
    for opt in options:
        s = (opt or "").strip()
        if not s or len(s) < 2 or len(s) > 100:
            return False
        m = re.match(r"^([ABCD])[\.．、\s]", s)
        if not m:
            return False
        letter = m.group(1)
        if last_letter is not None and letter <= last_letter:
            return False
        last_letter = letter
    return True


def _has_calc_hint(text: str) -> bool:
    return any(h in text for h in _CALCULATION_HINTS)


def preserve_subquestions_after_qiu(content_md: str) -> str:
    """确保 `求:` / `求：` 之后的内容(子问 / 公式)被保留在 content_md 中。

    现状: parser 可能因 `求:` 截断;本函数只对题干做"修整" —
    若 `求:` 后出现 `(1)` `（1）` `1)` 等子问起始,确保子问不丢。
    """
    if not content_md or "求" not in content_md:
        return content_md
    # 找到最近的 `求:` / `求：` / `计算:` / `计算：`
    m = re.search(r"(求|计算)\s*[:：]", content_md)
    if not m:
        return content_md
    tail = content_md[m.end():]
    # 若 tail 包含子问起始,本段保留原样;否则用空字符串替换尾部 (裁掉截断噪声)
    if _SUBQ_RE.search(tail):
        return content_md
    # 短尾 (< 6 字符) 视为噪声,裁掉
    if len(tail.strip()) < 6:
        return content_md[: m.end()].rstrip()
    return content_md


def normalize_figure_paths(figure_paths) -> List[str]:
    """对 figure_paths 做稳定排序/去重/剥空白;**不**补图、不删图。"""
    if not figure_paths:
        return []
    if isinstance(figure_paths, str):
        figure_paths = [figure_paths]
    seen: List[str] = []
    for fp in figure_paths:
        if not isinstance(fp, str):
            continue
        s = fp.strip()
        if s and s not in seen:
            seen.append(s)
    return seen


def _clean_ocr_noise(text: str) -> str:
    """清理 OCR 噪声 — 纯数字/字母短串插入在选项/题干文本中。

    典型: "小球的速度 00000000000 O0" → "小球的速度"
    策略: 匹配行内独立的纯数字 (≥4位) + 可选单字母后缀,替换为空。
    不影响正常物理量如 "97.43cm" (有单位) 或 "1.5m" (有小数点+单位)。
    """
    if not text:
        return text
    # 清理纯数字垃圾 (≥4位连续数字, 或连续0, 不跟单位)
    # 例: "00000000000 O0", "0000", "12345"
    text = re.sub(r'\s+\d{4,}\s*[A-Z]?\d*\s*', ' ', text)
    # 合并多余空格
    text = re.sub(r' {2,}', ' ', text)
    return text.strip()


def normalize_question_record(
    *,
    content_md: str,
    q_type: Optional[str],
    figure_paths,
) -> dict:
    """对单条 Question 行做归一化,返回 (new_content_md, new_q_type, new_figure_paths)。

    调用方写回 DB (Question.update) — 不能编造内容,只能规范已有内容。
    """
    new_content = preserve_subquestions_after_qiu(content_md)
    new_content = reattach_option_prose(new_content)
    new_content = split_inline_options(new_content)
    new_content = _clean_ocr_noise(new_content)
    new_type = detect_q_type(new_content, current=q_type)
    new_figs = normalize_figure_paths(figure_paths)
    return {
        "content_md": new_content,
        "q_type": new_type,
        "figure_paths": new_figs,
    }


def normalize_question_batch(questions) -> int:
    """对可迭代的 Question 记录做归一化,直接写回 DB。

    期望每条记录至少含: id, content_md, q_type, figure_paths。
    返回成功更新的条数。
    """
    from topic_garden import db as tg_db

    updated = 0
    for q in questions:
        try:
            new = normalize_question_record(
                content_md=q.content_md or "",
                q_type=q.q_type,
                figure_paths=q.figure_paths,
            )
            # 图 JSON 化与 topic_garden.add_question_with_dedupe 保持一致
            import json
            fig_json = (
                json.dumps(new["figure_paths"], ensure_ascii=False)
                if new["figure_paths"]
                else None
            )
            tg_db.Question.update(
                content_md=new["content_md"],
                q_type=new["q_type"],
                figure_paths=fig_json,
            ).where(tg_db.Question.id == q.id).execute()
            updated += 1
        except Exception as e:  # pragma: no cover - 写回失败不影响主流程
            log.warning("[post_process] 题 %s 归一化失败: %s", getattr(q, "id", "?"), e)
    if updated:
        log.info("[post_process] 归一化 %d 题", updated)
    return updated


__all__ = [
    "normalize_options_to_lines",
    "split_inline_options",
    "reattach_option_prose",
    "detect_q_type",
    "preserve_subquestions_after_qiu",
    "normalize_figure_paths",
    "normalize_question_record",
    "normalize_question_batch",
]
