"""Private review of the current university materials, with no legacy defaults.

This inventory contains answer keys and scenario facts. Every HTTP caller must
require researcher authentication; it must never enter participant/model input.
Historical material review remains available in instrument_review/audit.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from html import escape

from .student_materials import (
    AGE_BANDS, COHORT_LABEL, DESIGN_VERSION, EDUCATION_STAGES,
    FORM_VERSION, SCENARIOS, TASK_VERSION, form_for, task_for,
)

REVIEW_VERSION = "student-material-review-v1"
RESEARCH_NOTES = [
    "仅适用于18–25岁、当前在读的专科生和本科生。年龄及在读阶段是准入条件，不代表已核验真实学籍。",
    "三个年龄区间均使用同一份30题问卷及同一校园任务。年龄仅作人口学记录，不存在三份年龄适配卷。",
    "30题覆盖检索、来源与时效、证据判断、规则应用、核验与使用五个领域，每个领域6题。题目所需信息均在材料内提供。",
    "本地原创候选工具尚未标定难度、信度及效度。大学生背景不证明每位参与者都能读懂，需在目标群体内做认知访谈、预测试与题项分析。",
    "anchor_score保留18个指定题ID的兼容分项。所有题文均为当前学生版；该分项不代表与旧跨年龄问卷等值，也不构成已校准量尺。",
    "前测不排练主任务的报名类别、每周频率或截止时间；前测反应性仍需实测。旧记录的答案、分数和任务快照不得按新材料重写。",
    "两组共用模型、公共资料、参数与完整对话历史；澄清组额外收到澄清提示。两项案例私事实由参与者掌握，模型初始只获得公共背景和资料。",
    "四个情景只改变每周可到场次数和既往本项目名额，均影响最优参加方案。非澄清组的参与者也可以主动提供这些事实。",
    "0–100分是候选决策质量，不是实际成功概率或真实完成率；分别记录规则可行和目标满足，40/40/20权重需专家审阅和敏感性分析。",
    "比较同问卷/任务版本、同案例分层下的随机条件，不预设澄清对哪类素养组效果最大，不将组间差写成同一个人的前后提高。模拟数据不能充当真实受试者证据。",
]


def _best(options: list[dict]) -> list[dict]:
    maximum = max(option["score"] for option in options)
    return [option for option in options if option["score"] == maximum]


def _checks(cases: list[dict], execution: list[dict]) -> list[dict]:
    expected = {"first_3": "new_onsite", "first_1": "new_flexible",
                "return_3": "return_onsite", "return_1": "return_flexible"}
    observed = {case["scenario_id"]: [option["id"] for option in _best(case["decision_options"])]
                for case in cases}
    return [
        {"id": "four_unique_optima", "status": "通过" if observed == {key: [value] for key, value in expected.items()} else "需复核",
         "finding": "四个案例各有一个80分参加方案；两项私事实分别改变登记类别和参加安排。"},
        {"id": "independent_execution", "status": "通过" if [option["id"] for option in _best(execution)] == ["service_current_complete"] else "需复核",
         "finding": "办理选择独立评分期限、渠道和两项材料，唯一满分20分；不能从参加选项恢复办理规则。"},
        {"id": "student_scope", "status": "设计约束",
         "finding": "所有年龄段相同任务；申请人21岁在读本科生，规则明确仅18–25岁在读专科生和本科生，无额外专业或设备要求。"},
        {"id": "neutral_category_labels", "status": "设计约束",
         "finding": "登记甲乙为中性标签，两类都可报名，必须根据公共规则和私事实决定类别。"},
        {"id": "scoring_weights", "status": "待验证",
         "finding": "40/40/20权重与可行但非最优安排的部分分是候选定义，不能当作客观成功概率。"},
        {"id": "deadline_action_wording", "status": "已区分计划时点与规则期限",
         "finding": "选项明确要求选择准备遵循的有效截止时点，避免把更晚日期前包含的有效早交时间误判成逾期。"},
    ]


def inventory() -> dict:
    """Return one reviewed form and three storage IDs for one shared task."""
    _, items = form_for(next(iter(AGE_BANDS)))
    tasks = []
    for band, info in AGE_BANDS.items():
        task = task_for(band)
        cases = [{key: candidate[key] for key in
                  ("scenario_id", "participant_facts", "scenario_values", "decision_options")}
                 for candidate in (task_for(band, scenario_id) for scenario_id, _, _ in SCENARIOS)]
        tasks.append({
            "age_band": band, "age_label": info["label"], "task_id": task["id"],
            "task_version": TASK_VERSION,
            **{key: task[key] for key in ("title", "instructions", "shared_context", "rubric",
                                        "scoring_dimensions", "slots", "execution_options",
                                        "evidence_cards", "reference_text")},
            "scenarios": cases, "content_checks": _checks(cases, task["execution_options"]),
            "answer_basis": {
                "eligibility": "B_BASIC：18–25岁、当前在读的专科生或本科生；陈禾21岁在读本科，满足共同条件。",
                "path": "B_REPEAT：从未获得本项目名额→登记甲；曾获得→登记乙。",
                "schedule": "B_SCHEDULE和案例目标：最多1次→弹性安排；最多3次→现场安排最优，弹性可行但少现场交流。",
                "execution": "B_MATERIALS和C_TIME：以11月18日17:00为有效截止，到活动服务点交报名表及参加计划。",
            },
        })
    return {
        "review_version": REVIEW_VERSION, "generated_at": datetime.now(timezone.utc).isoformat(),
        "design_version": DESIGN_VERSION, "instrument_version": FORM_VERSION,
        "status": "大学生材料候选版；可读性、题项质量和评分权重尚待真实预测试",
        "access": "仅供研究者；含答案及案例私事实；不可公开给受试者或输入模型",
        "cohort_label": COHORT_LABEL, "education_stages": dict(EDUCATION_STAGES),
        "age_bands": deepcopy(AGE_BANDS), "applicable_age_band_count": len(AGE_BANDS),
        "research_notes": list(RESEARCH_NOTES),
        "forms": [{"age_band": "student-shared", "age_label": "大学生统一问卷（所有准入年龄区间）",
                   "applicable_age_bands": list(AGE_BANDS), "version": FORM_VERSION, "items": items}],
        "form_count": 1, "form_item_count": len(items), "unique_current_material_count": len(items),
        "legacy_candidate_count": 0, "legacy_items": [],
        "legacy_access": "旧跨年龄材料保留在历史审查模块，当前版本不呈现，不重评分旧记录。",
        "tasks": tasks, "task_count": len(tasks), "unique_task_count": 1,
        "task_scenario_count": len(SCENARIOS), "stored_task_scenario_count": len(tasks) * len(SCENARIOS),
    }


def _h(value) -> str:
    return escape(str(value), quote=True)


def _table(headings, rows) -> str:
    return '<div class="table-wrap"><table><thead><tr>' + ''.join(
        '<th scope="col">' + _h(value) + '</th>' for value in headings
    ) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join(
        '<td>' + _h(value) + '</td>' for value in row) + '</tr>' for row in rows) + '</tbody></table></div>'


def _p(value) -> str:
    return '<p class="preline">' + _h(value) + '</p>'


def render_review_html(report: dict | None = None) -> str:
    """Render escaped standalone researcher materials, with all 30 items once."""
    report = inventory() if report is None else report
    chunks = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
              '<meta name="viewport" content="width=device-width,initial-scale=1">',
              '<meta name="robots" content="noindex,nofollow"><title>大学生问卷与任务审查</title>',
              '<style>body{font:16px/1.7 system-ui,sans-serif;color:#203043;background:#f3f6fa;margin:0}main{max-width:1100px;margin:auto;padding:24px}h1,h2,h3{line-height:1.4}.notice{background:#fff3df;border:2px solid #a14b00;padding:16px}.preline{white-space:pre-line}.question,details{background:white;border:1px solid #cdd8e6;border-radius:8px;padding:16px;margin:16px 0}.answer{background:#eaf5ed}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse}th,td{border:1px solid #cbd7e5;padding:9px;vertical-align:top;text-align:left;white-space:pre-line}summary{cursor:pointer;font-weight:600}a{color:#155b8e}@media(max-width:600px){main{padding:12px}h1{font-size:1.5rem}}@media print{body{background:white}main{padding:0}.question{break-inside:avoid}h2,h3,summary{break-after:avoid}}</style></head><body><main>',
              '<h1>大学生问卷与信息任务审查</h1><div class="notice"><strong>非公开研究材料 · 含答案及案例私事实</strong>',
              _p(report["access"]), '</div>', _p(report["cohort_label"]),
              _p(report["review_version"] + ' · ' + report["generated_at"]), _p(report["status"]),
              _table(("统一问卷", "独立题目", "统一任务", "私事实情景", "人口学年龄区间"),
                     [[1, report["form_item_count"], report["unique_task_count"], report["task_scenario_count"], report["applicable_age_band_count"]]]),
              '<h2>研究范围与解释边界</h2><ol>',
              *('<li>' + _h(note) + '</li>' for note in report["research_notes"]),
              '</ol><h2>统一30题问卷</h2>', _p(report["instrument_version"])]
    for item in report["forms"][0]["items"]:
        chunks += ['<article class="question"><h3>' + _h(item["id"]) + ' · ' + _h(item["domain"]) + '</h3>',
                   _p(item["question"]), '<ol type="A">']
        chunks.extend('<li' + (' class="answer"' if index == item["correct"] else '') + '>' + _h(option) +
                      (' <strong>〔指定答案〕</strong>' if index == item["correct"] else '') + '</li>'
                      for index, option in enumerate(item["options"]))
        chunks += ['</ol>', _p('答案依据：' + item["rationale"]), _p('能力操作：' + item["skill"]),
                   _p('所需知识：' + item["expected_knowledge"]), _p(item["difficulty_basis"]),
                   _p(item["review_note"]), '</article>']
    task = report["tasks"][0]
    chunks += ['<h2>统一校园共创任务</h2>', _p('以下三个任务ID仅用于匹配人口学年龄区间，材料及评分完全相同。'),
               _table(("年龄区间", "默认任务ID"), [[entry["age_label"], entry["task_id"]] for entry in report["tasks"]]),
               '<h3>' + _h(task["title"]) + '</h3>', _p(task["instructions"]), _p(task["shared_context"]),
               '<h3>内容检查</h3>', _table(("检查", "结论", "依据"),
                   [[check["id"], check["status"], check["finding"]] for check in task["content_checks"]]),
               '<h3>评分规则</h3>', _p(task["rubric"]),
               _table(("维度", "满分", "候选计分规则"),
                      [[entry["label"], entry["maximum"], entry["rule"]] for entry in task["scoring_dimensions"]]),
               '<h3>四个案例（私事实不进入模型初始上下文）</h3>']
    for case in task["scenarios"]:
        chunks += ['<details open><summary>' + _h(case["scenario_id"]) + '</summary>', _p(case["participant_facts"]),
                   _table(("选项ID", "参加方案", "0–80分", "分项", "规则可行", "目标满足"),
                          [[option["id"], option["text"], option["score"], option["score_components"],
                            option["feasible"], option["goal_met"]] for option in case["decision_options"]]), '</details>']
    chunks += ['<h3>独立办理方案</h3>', _table(("选项ID", "办理方案", "0–20分", "分项", "规则可行"),
               [[option["id"], option["text"], option["score"], option["score_components"], option["feasible"]]
                for option in task["execution_options"]]), '<h3>答案依据</h3>']
    chunks.extend(_p(value) for value in task["answer_basis"].values())
    chunks += ['<h3>澄清事实</h3>', _table(("字段", "说明", "可用问题", "关联证据"),
               [[slot["key"], slot["description"], slot["question"], slot["evidence_card_id"]] for slot in task["slots"]]),
               '<h3>全部公共资料</h3>']
    for card in task["evidence_cards"]:
        chunks += ['<details open><summary>' + _h(card["id"]) + ' · ' + _h(card["title"]) + '</summary>',
                   _p(card["body"]), '</details>']
    chunks += [_p(report["legacy_access"]), _p('本文件可离线阅读；不得放入参与者可访问的静态目录。内容检查不替代真实目标人群预测试。'),
               '</main></body></html>']
    return ''.join(chunks)
