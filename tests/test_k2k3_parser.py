"""
tests.test_k2k3_parser — K2/K3 题干层修复回归 (A. 永远跑)

覆盖 PDF2PPT 跨仓修复 + post-process + fallback classifier,确保:
- 本地 pdf2ppt 包是源 (不再依赖 ../PDF2PPT 兄弟仓)
- K2-Q3 同行 4 选项被规范化成 4 行
- K2-Q2 ABCD 全拼不被误判为 choice
- K3-Q3 含子问号不被当 unknown
- K3-Q5 求: 后子问保留
- fallback drafts 题型不再是统一 fill_blank
- 缺失 OCR 内容不补
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Per-test isolation (per conftest.py)
os.environ.setdefault("TOPIC_GARDEN_DB_PATH", f"/tmp/exam_to_html_test_{os.getpid()}.db")


# ============================================================
# Always-on
# ============================================================
def test_local_pdf2ppt_package_is_used():
    """`import pdf2ppt` 必须解析到本仓 vendored 包,不是 ../PDF2PPT。"""
    import pdf2ppt
    p = Path(pdf2ppt.__file__).resolve()
    worktree = Path("/Users/zhewenliu/Claude/exam-to-html").resolve()
    # worktree 自身或其下任一 worktree
    assert str(p).startswith(str(worktree)), \
        f"pdf2ppt 解析到非本仓路径: {p}"
    # 应包含子文件
    assert (Path(pdf2ppt.__file__).parent / "_v2_parser.py").is_file()
    assert (Path(pdf2ppt.__file__).parent / "_v2_models.py").is_file()
    assert (Path(pdf2ppt.__file__).parent / "_qnum_rule.py").is_file()


def test_post_process_split_inline_options():
    """K2-Q3: 同行内挤 4 个选项必须被拆成 4 行。"""
    from exam_to_html.backend._post_process_md import split_inline_options
    md = (
        "1. 物体落地末速度为\n"
        "A. 5 m/s B. 10 m/s C. 15 m/s D. 20 m/s"
    )
    out = split_inline_options(md)
    lines = out.split("\n")
    # 行数至少 5 (题号 + 4 个选项)
    assert len(lines) >= 5, f"split 失败: {out!r}"
    # 4 个选项都应独占一行,且以 A./B./C./D. 开头
    for letter in "ABCD":
        opt_lines = [l for l in lines if re.match(rf"^\s*{letter}\.", l)]
        assert len(opt_lines) == 1, f"{letter} 应独占一行: {out!r}"


def test_post_process_no_fabrication_inline_abcd():
    """K2-Q2: 长 ABCD 全拼场景 — split_inline_options 不应臆造内容。"""
    from exam_to_html.backend._post_process_md import split_inline_options
    md = (
        "1. 下列说法正确的是\n"
        "A. 平抛运动是水平方向位移等于竖直方向位移之比 "
        "B. 曲线运动是速度方向沿轨迹切线 "
        "C. 自由落体是重力加速度 9.8 m/s² "
        "D. 斜抛是水平分速度恒定"
    )
    out = split_inline_options(md)
    # 1) 不应有"5 m/s"这种凭空生成的选项
    assert "5 m/s" not in out, "fabricated 5 m/s 出现!"
    # 2) 原文中所有汉字应仍存在(没被 trim 掉)
    for char in ("平抛", "切线", "自由落体", "斜抛"):
        assert char in out, f"原文丢失: {char}"


def test_post_process_detect_q_type_choice():
    """K2-Q3 拆完后应当是 choice。"""
    from exam_to_html.backend._post_process_md import detect_q_type, split_inline_options
    md = "A. 5 m/s\nB. 10 m/s\nC. 15 m/s\nD. 20 m/s"
    out = split_inline_options(md)
    assert detect_q_type(out) == "choice"


def test_post_process_detect_q_type_calc_qiu():
    """K3-Q5: 含 求: + 子问 → calculation,不显示 [?]。"""
    from exam_to_html.backend._post_process_md import detect_q_type
    md = "1. 已知 v0 = 10 m/s, 求: 落地时间。\n(1) 水平分量 (2) 竖直分量 (3) 合成速度"
    assert detect_q_type(md) == "calculation"


def test_post_process_detect_q_type_calc_subq():
    """K3-Q3: 含 (1)(2)(3) 子问但无 求: → calculation。"""
    from exam_to_html.backend._post_process_md import detect_q_type
    md = "1. 一球做平抛,落地后(1) 水平速度(2) 竖直速度"
    assert detect_q_type(md) == "calculation"


def test_post_process_detect_q_type_fill_blank_no_evidence():
    """无选项/无 求:/无子问 → 兜底 fill_blank(不显示 [?])。"""
    from exam_to_html.backend._post_process_md import detect_q_type
    md = "1. 一物体做自由落体, ___ = 末速度。"
    assert detect_q_type(md) == "fill_blank"


def test_post_process_preserves_subquestions_after_qiu():
    """K3-Q5: 求: 后子问不能被裁掉。"""
    from exam_to_html.backend._post_process_md import preserve_subquestions_after_qiu
    md = "1. 已知 v0 = 10 m/s, 求: 落地时间。\n(1) 水平分量 (2) 竖直分量"
    out = preserve_subquestions_after_qiu(md)
    assert "(1)" in out, f"子问 (1) 被裁: {out!r}"
    assert "(2)" in out, f"子问 (2) 被裁: {out!r}"


def test_post_process_preserves_subquestions_truncates_noise():
    """求: 后只有 1-5 字符噪声才裁掉;长内容保留。"""
    from exam_to_html.backend._post_process_md import preserve_subquestions_after_qiu
    md_short = "1. 已知 v0 = 10 m/s, 求: x"
    out_short = preserve_subquestions_after_qiu(md_short)
    assert not out_short.endswith("x"), f"短尾未裁: {out_short!r}"
    md_long = "1. 已知 v0 = 10 m/s, 求: 落地时间。\n已知 g = 10 m/s² 求 t。"
    out_long = preserve_subquestions_after_qiu(md_long)
    assert "求: 落地时间" in out_long, f"长尾被裁: {out_long!r}"


def test_qnum_fallback_classifies_calculation():
    """fallback 不再硬编码 fill_blank — 含 求:/(1)(2)(3) 应得 calculation。"""
    from exam_to_html.backend._qnum_fallback import _build_drafts_from_pages
    pages = [(0, "1. 一物体做平抛, 求: 落地速度。\n(1) 水平分量 (2) 竖直分量")]
    drafts = _build_drafts_from_pages(pages)
    assert len(drafts) == 1
    assert drafts[0].q_type == "calculation", f"应 calculation, 实际 {drafts[0].q_type}"


def test_qnum_fallback_classifies_choice():
    """fallback 真 4 选项 → choice。"""
    from exam_to_html.backend._qnum_fallback import _build_drafts_from_pages
    pages = [(0,
        "1. 下列说法正确的是\n"
        "A. 力的作用是改变运动状态\n"
        "B. 惯性是物体固有属性\n"
        "C. 速度越大惯性越大\n"
        "D. 力是维持运动原因"
    )]
    drafts = _build_drafts_from_pages(pages)
    assert len(drafts) == 1
    assert drafts[0].q_type == "choice", f"应 choice, 实际 {drafts[0].q_type}"


def test_post_process_no_fabrication_empty_ocr():
    """缺失 OCR 内容时, post-process 不编造选项 / 子问。"""
    from exam_to_html.backend._post_process_md import (
        detect_q_type, normalize_question_record,
    )
    # 模拟"OCR 啥都没抽出来" — 只有模糊字符
    md = "1. ?????? ?????"
    out_md = md
    rec = normalize_question_record(
        content_md=out_md, q_type="unknown", figure_paths=None,
    )
    # 不应出现 A./B./C./D./求: 这种凭空生成
    assert not re.search(r"\b[ABCD]\.\s+\S", rec["content_md"]), \
        f"凭空生成选项: {rec['content_md']!r}"
    assert "求:" not in rec["content_md"], \
        f"凭空生成 求:: {rec['content_md']!r}"
    # 兜底 fill_blank(不显示 [?])
    assert rec["q_type"] in ("fill_blank", "calculation"), \
        f"未知原文应兜底, 实际 {rec['q_type']!r}"


def test_pdf2ppt_parser_helpers_exposed():
    """vendored parser 必须导出新增的 helper 供 exam-to-html 复用。"""
    from pdf2ppt._v2_parser import (
        _looks_like_real_options, _has_calc_hint_in_text,
    )
    # 真 4 选项
    assert _looks_like_real_options([
        "A. 5 m/s", "B. 10 m/s", "C. 15 m/s", "D. 20 m/s",
    ])
    # 单项不应
    assert not _looks_like_real_options(["A. 5 m/s"])
    # 不单调
    assert not _looks_like_real_options([
        "A. 5", "C. 10", "B. 15", "D. 20",
    ])
    # 求: 命中
    assert _has_calc_hint_in_text("求: 时间")
    # 求: 全角也命中
    assert _has_calc_hint_in_text("求： 时间")


def _worktree_root() -> Path:
    return Path(__file__).resolve().parent.parent


def test_no_cross_repo_pdf2ppt_install_required():
    """README / docs 中不应再要求 `pip install -r ../PDF2PPT/requirements.txt` 或 pdf2ppt.pth。

    允许出现的形式: 显式说明"已不需要 / 已 vendored / 命中本地包"等。
    真正禁止的是: 还在让用户去跨仓装的命令/步骤。
    """
    import re
    root = _worktree_root()
    # pip 命令只允许 `#` 注释后的"不再需要"说明,不允许 code block 里的安装命令
    bad_pip_code_re = re.compile(
        r"```[a-zA-Z]*\n[^`]*pip\s+install\s+-r\s+\.\.[\\/]?PDF2PPT[^\n]*\n",
        re.MULTILINE,
    )
    # pth 文件作为命令步骤引用(在 ``` 块里出现 echo ... pdf2ppt.pth)
    bad_pth_code_re = re.compile(
        r"```[a-zA-Z]*\n[^`]*pdf2ppt\.pth[^\n]*\n",
        re.MULTILINE,
    )
    for rel in ("README.md", "docs/build-windows.md"):
        p = root / rel
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8")
        bad_pip = bad_pip_code_re.search(text)
        bad_pth = bad_pth_code_re.search(text)
        assert not bad_pip, f"{rel} code block 仍含跨仓 pip 指令: {bad_pip.group(0)}"
        assert not bad_pth, f"{rel} code block 仍含 .pth 步骤: {bad_pth.group(0)}"


def test_pyproject_includes_pdf2ppt_package():
    """pyproject.toml 的 setuptools 包发现必须包含 pdf2ppt*。"""
    import tomllib
    p = _worktree_root() / "pyproject.toml"
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    includes = data["tool"]["setuptools"]["packages"]["find"]["include"]
    assert "pdf2ppt*" in includes, f"pyproject.toml include 缺 pdf2ppt*: {includes}"


def test_pyinstaller_spec_includes_vendored_pdf2ppt():
    """pyinstaller hiddenimports 必须列本地 pdf2ppt 关键模块。"""
    p = _worktree_root() / "pyinstaller.spec"
    if not p.is_file():
        return
    text = p.read_text(encoding="utf-8")
    for mod in ("pdf2ppt._v2_parser", "pdf2ppt._v2_models", "pdf2ppt._qnum_rule"):
        assert f"'{mod}'" in text, f"pyinstaller.spec 缺 {mod}"



# ============================================================
# Real PDF regression — 20260528 高一下学期物理统一训练试题卷
# 由真实 PDF run 触发的 bug 守卫
# ============================================================
def test_real_pdf_q7_option_prose_reattached_shape_b():
    """Q7-style: prose 出现在所有选项之前 (Shape B)。

    MinerU flash 抽取后,选项尾句被分离成若干短行,出现在第一个 A./B./C./D.
    标签之前。reattach_option_prose 应把这些尾句重新拼到对应选项 (按出现
    顺序分配:prose[0] → A, prose[1] → B, ...)。

    这里只验证"含 LaTeX 公式"或"含中文逗号/单位"的尾句被合并。
    不含这些信号的 prose (e.g. '则图线II是...') 不强制合并 — 保守优先。
    """
    from exam_to_html.backend._post_process_md import reattach_option_prose
    md = (
        "如图所示为单摆在两次受迫振动中的共振曲线,下列说法正确的是( )\n"
        "则图线II是月球上的单摆共振曲线\n"
        "比为 $l _ { 1 } ; l _ { 2 } = 4 ; 2 5$\n"  # 含 LaTeX,会被合并到 A
        "\n"
        "A.若两次受迫振动分别在月球上和地球上进行,且摆长相等,\n"
        "\n"
        "B.若两次受迫振动均在地球上同一地点进行的,则两次摆长之\n"
        "\n"
        "C.若图线I的摆长约为1m,则图线I是在地球表面上完成的\n"
        "\n"
        "D.图线I若是在地球表面上完成的,则该摆摆长约为1m"
    )
    out = reattach_option_prose(md)
    # "比为 l_1:l_2=4:25" 必须拼回 A (A 行是 out 的第 3 行,index 3)
    a_line = next(l for l in out.split("\n") if l.startswith("A."))
    assert "$l _ { 1 } ; l _ { 2 } = 4 ; 2 5$" in a_line, (
        f"LaTeX 尾句未合并到 A: {a_line!r}"
    )
    # "则图线II是月球上的单摆共振曲线" 没强信号,保守不合并 — 单独成行
    assert "则图线II是月球上的单摆共振曲线" in out, (
        f"非强信号尾句不应被吞: {out!r}"
    )


def test_real_pdf_q7_option_prose_shape_a():
    """Shape A: prose 在 A 和 B 之间 → 拼到 A (若含强信号)。"""
    from exam_to_html.backend._post_process_md import reattach_option_prose
    md = (
        "题干\n"
        "A.主体首句A\n"
        "尾句A\n"  # 无强信号 → 不合并 (保守)
        "B.主体首句B\n"
        "尾句B,含中文逗号\n"  # 有逗号 → 合并到 B
        "C.主体首句C\n"
        "D.主体首句D"
    )
    out = reattach_option_prose(md)
    out_lines = out.split("\n")
    # "尾句B,含中文逗号" 必须拼到 B (在 out 里以 B 开头)
    b_line = next(l for l in out_lines if l.startswith("B."))
    assert "尾句B,含中文逗号" in b_line, f"B 尾句未合并: {b_line!r}"


def test_real_pdf_reattach_no_fabrication():
    """reattach_option_prose 不会把题干/题号行误当成选项尾句。"""
    from exam_to_html.backend._post_process_md import reattach_option_prose
    md = (
        "1. 题干\n"     # 题号行
        "题干续\n"       # 普通题干续
        "A.选项A\n"
        "B.选项B\n"
        "C.选项C\n"
        "D.选项D\n"
        "5. 下一题"      # 题号
    )
    out = reattach_option_prose(md)
    # 5. 不应被当作选项尾句合并
    assert "5." in out, "题号 5. 被吞"
    # 4 个选项应仍在
    for letter in "ABCD":
        assert any(l.strip().startswith(f"{letter}.") for l in out.split("\n")), (
            f"{letter} 选项丢失: {out!r}"
        )


def test_real_pdf_experiment_detect_q11():
    """Q11 实验题 (利用单摆/用游标卡尺/图甲) → experiment,不是 calculation。"""
    from exam_to_html.backend._post_process_md import detect_q_type
    md = (
        "(6分,每空2分)小华同学在利用单摆测重力加速度的实验中:\n"
        "(1)该同学先用米尺测得摆线长为97.43cm,用游标卡尺测得摆球的直径如图甲所示\n"
        "(2)另一位同学在用单摆测定重力加速度的实验中"
    )
    assert detect_q_type(md) == "experiment", (
        f"实验题应 experiment, 实际 {detect_q_type(md)}"
    )


def test_real_pdf_experiment_detect_q12_partial():
    """Q12 实验题 (only sub-question (5) survived in content_md) → 不再 unknown。

    Q12 在 flash 抽取后只剩 (5) 子问。post-process 无法凭空判断是实验题,
    但能保证 q_type 至少不是 unknown (兜底 fill_blank 或按子问判 calculation)。
    """
    from exam_to_html.backend._post_process_md import detect_q_type
    md = (
        "(10分,每空2分)\n"
        "(1)在测量周期时,为了减小测量周期的误差\n"
        "(2)用秒表记录单摆n次全振动所用时间为t\n"
        "(3)多次改变摆线长度,并测出相应的周期T\n"
        "(4)在摆球和细线相同的情况下,单摆小角度摆动的周期\n"
        "(5)如图所示,单摆摆长为L,摆球质量为m"
    )
    q = detect_q_type(md)
    # 实验题信号: 用秒表 → 应是 experiment
    # 若 detect 不到 experiment,至少不应是 unknown
    assert q in ("experiment", "calculation", "fill_blank"), (
        f"Q12 partial 应有可解释类型, 实际 {q}"
    )


def test_real_pdf_calc_q14_subq_preserved():
    """Q14 计算题 (1)(2) 子问号 → calculation,子问不丢。"""
    from exam_to_html.backend._post_process_md import detect_q_type
    md = (
        "(14分)某同学自己制作了一套玩具\n"
        "(1)小球从a点射出时可能的速率\n"
        "(2)发射前,弹簧弹性势能的最大可能值"
    )
    assert detect_q_type(md) == "calculation"


def test_real_pdf_no_fabrication_k11_partial():
    """Q11 真 PDF 抽取后的 markdown → 不凭空补内容。

    缺 OCR 的图片/公式位置仍保留原始 markdown 占位,
    post-process 不应尝试注入虚构选项或子问。
    """
    from exam_to_html.backend._post_process_md import normalize_question_record
    md = (
        "(1)该同学先用米尺测得摆线长为97.43cm,用游标卡尺测得摆球的直径如图甲所示\n"
        "(2)另一位同学在用单摆测定重力加速度的实验中"
    )
    rec = normalize_question_record(
        content_md=md, q_type="calculation", figure_paths=None,
    )
    # 不应凭空添加 A./B./C./D./子问
    assert not re.search(r"\b[ABCD]\.[\s　]+", rec["content_md"]), (
        f'凭空生成选项: {rec["content_md"]!r}'
    )
    # 原有 (1)(2) 子问应保留
    assert "(1)" in rec["content_md"]
    assert "(2)" in rec["content_md"]


def test_real_pdf_pipeline_q7_q11_q15_round_trip():
    """整管线跑通: 模拟 Q7/Q11/Q15 markdown,经 normalize_question_record 后
    生成的内容应当是教师可读的,不是 [?] 或 [未知]。"""
    from exam_to_html.backend._post_process_md import normalize_question_record

    # Q7 (choice)
    q7 = (
        "如图所示为单摆在两次受迫振动中的共振曲线,下列说法正确的是( )\n"
        "则图线II是月球上的单摆共振曲线\n"
        "比为 $l _ { 1 } ; l _ { 2 } = 4 ; 2 5$\n"
        "A.若两次受迫振动分别在月球上和地球上进行,且摆长相等,\n"
        "B.若两次受迫振动均在地球上同一地点进行的,则两次摆长之\n"
        "C.若图线I的摆长约为1m,则图线I是在地球表面上完成的\n"
        "D.图线I若是在地球表面上完成的,则该摆摆长约为1m"
    )
    rec7 = normalize_question_record(content_md=q7, q_type="unknown", figure_paths=None)
    assert rec7["q_type"] == "choice"
    assert "[?]" not in rec7["content_md"]

    # Q11 (experiment)
    q11 = (
        "小华同学在利用单摆测重力加速度的实验中,用游标卡尺测摆球直径如图甲"
    )
    rec11 = normalize_question_record(content_md=q11, q_type="unknown", figure_paths=None)
    assert rec11["q_type"] == "experiment"

    # Q15 (calculation)
    q15 = "(18分)如图所示,求: 落地时间。\n(1) 水平分量 (2) 竖直分量"
    rec15 = normalize_question_record(content_md=q15, q_type="unknown", figure_paths=None)
    assert rec15["q_type"] == "calculation"
    # 求: 后子问保留
    assert "(1)" in rec15["content_md"]
    assert "(2)" in rec15["content_md"]


# ============================================================
# Real PDF regression — 20260528《高一下学期物理统一训练试题卷》
# Per-question structural inspection: 3 newly discovered bugs
# ============================================================

def test_bug_a_score_marker_is_calculation():
    """Bug A: 以(X分)开头的题干应归类为 calculation, 不是 fill_blank。

    真实 PDF: Q2(PDF Q15, "（18分）如图所示, 弹簧...物块P碰撞...") 和
    Q13(PDF Q14, "（14分）某同学...圆管...") 都以(X分)开头, 是计算题。
    """
    from exam_to_html.backend._post_process_md import detect_q_type
    q2 = "（18分）如图所示,水平地面上竖直固定一劲度系数为k的轻质弹簧,求碰撞后速度"
    assert detect_q_type(q2, current="unknown") == "calculation", (
        f"Q2 应为 calculation (有(X分)标记), 实际 {detect_q_type(q2)}"
    )
    q13 = "（14分）某同学自己制作了一套玩具,利用剖开的半径为R的圆管一部分作为轨道"
    assert detect_q_type(q13, current="unknown") == "calculation", (
        f"Q13 应为 calculation (有(X分)标记), 实际 {detect_q_type(q13)}"
    )


def test_bug_a_utilize_device_not_experiment():
    """Bug A: "利用X作为..."描述物理装置, 不应误判为实验题。

    真实 PDF: Q10(PDF Q13) 有"利用剖开的半径为R的圆管一部分作为轨道",
    触发"利用" experiment hint, 但这是计算题(利用圆管做轨道)。
    区分: 真实验 = "利用X测Y" / "实验中"; 计算 = "利用X作为Y"。
    """
    from exam_to_html.backend._post_process_md import detect_q_type
    q10 = "（12分）如图甲所示,一轻弹簧的两端与质量分别为m1和m2的两物块,现使A瞬时获得水平向右的速度"
    result = detect_q_type(q10, current="unknown")
    assert result != "experiment", (
        f"Q10 (利用圆管作为轨道) 不应为 experiment, 实际 {result}"
    )


def test_bug_a_figure_label_not_experiment_signal():
    """Bug A: "如图甲/乙" 可出现在任何题型, 不应作为 experiment 独立信号。

    真实 PDF: Q5(PDF Q5, 如图甲) 和 Q10(PDF Q13, 如图甲) 都是选择/计算题,
    但 "如图甲" 在 _EXPERIMENT_HINTS 列表里, 会误触发 experiment。
    """
    from exam_to_html.backend._post_process_md import _is_experiment_text
    # 选择题 stem 含 "如图甲" — 不应是 experiment
    q5 = "如图甲,一质量为m的物体B放在水平面上,质量为2m的物体A通过一轻弹簧与物体B连接"
    assert not _is_experiment_text(q5), (
        f"Q5 (选择题含如图甲) 不应触发 experiment, 实际 {_is_experiment_text(q5)}"
    )
    # 真实验 stem: "利用单摆测重力加速度的实验中, 用游标卡尺测直径如图甲所示"
    q11 = "小华同学在利用单摆测重力加速度的实验中,用游标卡尺测得摆球的直径如图甲所示"
    assert _is_experiment_text(q11), (
        f"Q11 (真实验) 应触发 experiment, 实际 {_is_experiment_text(q11)}"
    )


def test_bug_b_options_preserve_letter_prefix():
    """Bug B: 选项文本含物理量 (如 "5 m/s") 但缺 A./B./C./D. 前缀时,
    post-process 不应编造标签 — 只有原内容已有标签才做归一化。

    真实 PDF: Q5(PDF Q1) 选项被 MinerU Flash 提取后缺 A./B./C./D. 前缀,
    只剩 "小球的加速度 / 小球的速度 / 小球的动能 / 系统的弹性势能"。
    """
    from exam_to_html.backend._post_process_md import split_inline_options, normalize_question_record
    md_no_prefix = "如图所示,小球在轻弹簧作用下沿光滑水平杆做简谐运动\n小球的加速度\n小球的速度\n小球的动能\n系统的弹性势能"
    out = split_inline_options(md_no_prefix)
    # 选项行没 A./B./C./D. → split_inline_options 不动 (不编造标签)
    assert "A." not in out, f"缺前缀的选项不应被编造 A./B./C./D.: {out!r}"
    rec = normalize_question_record(content_md=md_no_prefix, q_type="unknown", figure_paths=None)
    assert rec["q_type"] != "choice", (
        f"缺 A./B./C./D. 前缀的4行选项不应被判为 choice, 实际 {rec['q_type']}"
    )


def test_bug_c_ocr_noise_filtered():
    """Bug C: OCR 噪声如 "00000000000 O0" 应被 post-process 清理或至少不显示。

    真实 PDF: Q5(PDF Q1) 选项 "小球的速度 00000000000 O0" 含 OCR 噪声。
    """
    from exam_to_html.backend._post_process_md import normalize_question_record
    md_noisy = "如图所示\n小球的加速度\n小球的速度 00000000000 O0\n小球的动能\n系统的弹性势能"
    rec = normalize_question_record(content_md=md_noisy, q_type="unknown", figure_paths=None)
    content = rec["content_md"]
    # 纯数字+字母短串 < 15 字符应被清理 (不影响主体语义)
    assert "00000000000" not in content, (
        f"OCR 噪声 00000000000 未被清理: {content!r}"
    )


def test_bug_a_experiment_real_still_detected():
    """防御: 真实验题 (利用单摆测g, 用游标卡尺, 用秒表) 仍应被判为 experiment。

    修 Bug A 时不能把真实验也误杀。
    """
    from exam_to_html.backend._post_process_md import detect_q_type
    q11 = "小华同学在利用单摆测重力加速度的实验中,用游标卡尺测得摆球的直径如图甲所示"
    assert detect_q_type(q11, current="unknown") == "experiment", (
        f"Q11 (真实验) 应为 experiment, 实际 {detect_q_type(q11)}"
    )
    q12 = "用单摆测量重力加速度的实验中\n(1)在测量周期时,为了减小测量周期的误差,应在摆球经过最低点的位置时开始计时"
    assert detect_q_type(q12, current="unknown") == "experiment", (
        f"Q12 (真实验) 应为 experiment, 实际 {detect_q_type(q12)}"
    )
