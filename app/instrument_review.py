"""Researcher-only, fully escaped questionnaire/task review artifact.

These helpers deliberately have no participant route. The caller must protect
any HTTP exposure with administrator authentication: the inventory contains
answer keys, case secrets, and candidate scoring rules.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from typing import Any

from .age_forms import AGE_BANDS
from .age_tasks import SCENARIOS, task_for
from .instrument_audit import review_inventory

REVIEW_VERSION = "instrument-readability-review-2026-10-19-v2"

RESEARCH_NOTES = [
    "本材料含所有答案、案例私事实及评分标准，仅供研究者审阅，不可发送给受试者或放入参与者接口。",
    "年龄区间只能降低情境理解门槛，不能证明同龄者常识一致。教育经历、阅读理解、相关经验仍可能影响得分。",
    "本次依据受试者后半段看不懂的反馈，修订共同题和各年龄情境题的句法与呈现。尚未取得反馈者的年龄与具体题号，不能据此认定某年龄群体普遍缺乏常识。",
    "问卷为本地原创候选工具。本轮取消易中难各10题的预设，全部标为未标定；不是实测难度，不靠术语、长句或多人物记忆制造难度。30题暂留作内容覆盖，不是质量达标线。",
    "新题拆成短句和分行，未直接指出正确答案；需通过目标受试者复述题意、休息后回试等方式，区分看不懂、疲劳与理解后不会判断，不能把改写后的软件检查当成人群验证。",
    "18道共同题是待检验的锚题，不是已经校准的量尺。应先比较同年龄、同表单版本内随机条件；跨年龄原始百分数不能直接解释为等值能力。",
    "前测材料不使用主任务的活动报名、每周频率、登记甲乙或截止日期，减少直接排练主任务的影响；两者仍测相近的信息操作，须通过预测试检查前测反应性。",
    "任务0–100分表示候选决策质量，不能称为实际成功概率或真实完成率。另报是否规则可行、是否达到案例目标，并区分参加方案与办理方案。",
    "实验不预设哪类素养组受益最大。不同素养组澄清与非澄清条件的分数差可能缩小、扩大或没有变化；应报告真实结果和不确定性。",
    "正式采集前需年龄分层认知访谈、小规模预测试、盲审评分复核和题项分析。75岁及以上应特别检查阅读负担与‘登记甲乙＋两类安排’的理解，不以年龄推断其能力。",
    "建议检查每题正确率、两个实质干扰项选择率、不确定率、校正题总相关及组间差异题项；内部一致性只是证据之一。不得仅为得到预想的干预结果而删题或调分。",
]


def _best(options: list[dict]) -> list[dict]:
    maximum = max(option["score"] for option in options)
    return [option for option in options if option["score"] == maximum]


def _task_checks(cases: list[dict], execution: list[dict]) -> list[dict]:
    lookup = {case["scenario_id"]: case for case in cases}
    best_ids = {key: [option["id"] for option in _best(case["decision_options"])]
                for key, case in lookup.items()}
    expected = {"first_3": "new_onsite", "first_1": "new_flexible",
                "return_3": "return_onsite", "return_1": "return_flexible"}
    checks = [
        {"id": "four_unique_optima", "status": "通过" if best_ids == {key: [value] for key, value in expected.items()} else "需复核",
         "finding": "四个情境各有一个最优参加方案；切换每周可到场次数会改变参加安排，切换既往名额事实会改变登记类别。正确方案不能在不知道两项事实时固定为同一项。"},
        {"id": "category_neutral_labels", "status": "设计约束",
         "finding": "登记甲/乙是中性标签，只能根据B_REPEAT给出的证据映射到既往名额情况。标签本身不能充当新参加/再次参加的常识线索；两类均可报名。"},
        {"id": "independent_execution", "status": "通过" if len(_best(execution)) == 1 and _best(execution)[0]["score"] == 20 else "需复核",
         "finding": "办理方案由受试者独立选择，单独评分期限、渠道、材料，不能从参加方案的重复措辞直接恢复办理规则。办理方案存在唯一20分方案，不同错误允许同分。"},
        {"id": "both_case_facts_consequential", "status": "设计约束",
         "finding": "每周最多1次时，现场安排不满足时间要求；最多3次时，弹性可行但少一次现场合作，未达到案例明确的优先目标。既往获得名额只改变登记类别，不剥夺报名资格。"},
        {"id": "scoring_weights", "status": "待验证",
         "finding": "登记40分、安排40分、独立办理20分是候选权重。40/40/20、弹性可行但非最优时的20分，以及各类错误的损失均需专家论证与预测试敏感性分析，不是客观自然常数。"},
        {"id": "age_context", "status": "待验证",
         "finding": "活动提供材料、从头说明，不要求学术、职业、政策、财务或智能设备知识。年龄情境相近不等于认知难度相等；75岁及以上应先做口头复述和认知访谈。"},
        {"id": "clarification_interpretation", "status": "研究解释边界",
         "finding": "澄清组追问未提供的关键案例事实，非澄清组仍可由受试者主动提供。比较同年龄/问卷版本/案例分层中的随机条件，估计澄清对不同信息素养分数的作用；不能把随机分组的均值差写成同一个人的前后提高。"},
    ]
    # Catch the original upper-bound wording: "before Nov 20" also contains
    # valid earlier submission times, so it should not be scored as an actual
    # late action unless the instruction explicitly asks for a deadline choice.
    ambiguous = any("11月20日24:00前" in option["text"] for option in execution)
    checks.append({"id": "deadline_action_wording", "status": "需文字复核" if ambiguous else "已区分计划时点与规则期限",
                   "finding": ("原候选项写‘11月20日24:00前’包含有效期限前的时间，若按实际行动解释可能歧义。须明确这是准备遵循的截止时点，或把错误项写成计划在11月20日提交；不得把提前提交也记成逾期。"
                               if ambiguous else "办理题应明确评分对象是计划提交时点、渠道和材料；晚于有效期限的方案须明确写为晚交，避免‘更晚日期前’包含有效早交时点的歧义。")})
    return checks


def inventory() -> dict:
    """Return the full private review inventory, including keys and task cases."""
    result = review_inventory()
    tasks = []
    for band, band_info in AGE_BANDS.items():
        task = task_for(band)
        cases = []
        for scenario_id, _, _ in SCENARIOS:
            candidate = task_for(band, scenario_id)
            cases.append({field: candidate[field] for field in
                          ("scenario_id", "participant_facts", "scenario_values", "decision_options")})
        tasks.append({
            "age_band": band, "age_label": band_info["label"],
            "task_id": task["id"], "task_version": task["task_version"],
            "title": task["title"], "instructions": task["instructions"],
            "shared_context": task["shared_context"], "rubric": task["rubric"],
            "scoring_dimensions": task["scoring_dimensions"], "slots": task["slots"],
            "execution_options": task["execution_options"], "evidence_cards": task["evidence_cards"],
            "reference_text": task["reference_text"], "scenarios": cases,
            "content_checks": _task_checks(cases, task["execution_options"]),
            "answer_basis": {
                "path": "B_REPEAT：从未获得过本项目名额→登记甲；此前获得过→登记乙。",
                "schedule": "B_SCHEDULE结合案例目标：每周最多1次→弹性安排；最多3次→现场安排最优，弹性可行但少现场交流。",
                "execution": "B_MATERIALS及C_TIME：两项材料、活动服务点、有效截止要求；以最新更正为准。",
            },
        })
    result.update(review_version=REVIEW_VERSION,
                  generated_at=datetime.now(timezone.utc).isoformat(),
                  access="仅供研究者；含答案及案例私事实；不可公开给受试者",
                  research_notes=list(RESEARCH_NOTES), tasks=tasks,
                  task_count=len(tasks), task_scenario_count=sum(len(task["scenarios"]) for task in tasks))
    return result


def _h(value: Any) -> str:
    return escape(str(value), quote=True)


def _paragraph(value: Any, css_class: str = "") -> str:
    return f'<p class="{_h(css_class)}">{_h(value)}</p>'


def _table(headings: tuple[str, ...], rows: list[list[Any]]) -> str:
    return '<div class="table-wrap"><table><thead><tr>' + ''.join(
        '<th scope="col">' + _h(heading) + '</th>' for heading in headings) + '</tr></thead><tbody>' + ''.join(
        '<tr>' + ''.join('<td>' + _h(cell) + '</td>' for cell in row) + '</tr>' for row in rows) + '</tbody></table></div>'


def _score_parts(option: dict) -> str:
    labels = {"path": "类别", "schedule": "安排", "deadline": "期限", "channel": "渠道", "materials": "材料"}
    return '；'.join(f'{labels.get(key, key)} {value}' for key, value in option.get("score_components", {}).items())


def _question_html(item: dict) -> str:
    options = ''.join('<li' + (' class="answer"' if index == item["correct"] else '') + '>' +
                      _h(option) + (' <strong>〔指定答案〕</strong>' if index == item["correct"] else '') + '</li>'
                      for index, option in enumerate(item["options"]))
    role = "共同锚题（待检验）" if item["anchor"] else "情境适配题"
    difficulty = {"easy": "易", "medium": "中", "hard": "难", "unrated": "未标定"}.get(item["draft_difficulty"], item["draft_difficulty"])
    fields = [("理由", item["rationale"]), ("能力操作", item["skill"]),
              ("作答所需知识", item["expected_knowledge"]), ("审查说明", item["review_note"])]
    return '<article class="question"><h4>' + _h(item["id"]) + ' · ' + _h(item["domain"]) + ' · ' + _h(role) + \
        '</h4>' + _paragraph(item["question"], "stem preline") + '<ol type="A">' + options + '</ol>' + \
        _paragraph('内容预期难度：' + difficulty + '；' + item["difficulty_basis"], "muted") + \
        '<dl>' + ''.join('<dt>' + _h(label) + '</dt><dd>' + _h(value) + '</dd>' for label, value in fields) + '</dl></article>'


def _task_html(task: dict) -> str:
    chunks = ['<section class="task"><h3>信息任务：' + _h(task["title"]) + '</h3>',
              _paragraph(task["task_id"] + ' · ' + task["task_version"], "muted"),
              '<h4>给受试者的角色、目标与背景</h4>', _paragraph(task["instructions"], "preline"),
              _paragraph(task["shared_context"], "preline"), '<h4>独立内容审查</h4>']
    chunks.append(_table(("检查", "结论", "具体说明"), [[check["id"], check["status"], check["finding"]] for check in task["content_checks"]]))
    chunks += ['<h4>评分维度与解释</h4>', _paragraph(task["rubric"], "preline")]
    chunks.append(_table(("维度", "满分", "候选计分规则"), [[dimension["label"], dimension["maximum"], dimension["rule"]]
                  for dimension in task["scoring_dimensions"]]))
    chunks += ['<h4>四个案例：仅两项私事实变化</h4>',
               _paragraph('下表及每个案例的私事实均只供研究者审阅。AI初始只获得共同背景；受试者可以主动提供私事实，澄清组也可追问。')]
    chunks.append(_table(("案例", "每周最多到场", "既往本项目名额", "最优参加方案", "参加分"), [
        [case["scenario_id"], case["scenario_values"]["weekly_availability"],
         "曾获得" if case["scenario_values"]["prior_project_place"] else "从未获得",
         ' / '.join(option["text"] for option in _best(case["decision_options"])),
         max(option["score"] for option in case["decision_options"])] for case in task["scenarios"]]))
    for case in task["scenarios"]:
        chunks += ['<details class="case"><summary>案例 ' + _h(case["scenario_id"]) + '：私事实与全部参加选项</summary>',
                   _paragraph(case["participant_facts"], "preline")]
        chunks.append(_table(("内部ID", "参加方案", "0–80分", "分项", "规则可行", "目标满足"), [
            [option["id"], option["text"], option["score"], _score_parts(option),
             "是" if option.get("feasible") else "否", "是" if option.get("goal_met") else "否"]
            for option in case["decision_options"]]))
        chunks.append('</details>')
    chunks += ['<h4>独立办理选项（各案例相同）</h4>']
    chunks.append(_table(("内部ID", "办理方案", "0–20分", "分项", "规则可行"), [
        [option["id"], option["text"], option["score"], _score_parts(option),
         "是" if option.get("feasible") else "否"] for option in task["execution_options"]]))
    chunks += ['<h4>答案依据</h4>', '<ul>' + ''.join('<li>' + _h(value) + '</li>' for value in task["answer_basis"].values()) + '</ul>',
               '<h4>澄清的两个关键事实</h4>']
    chunks.append(_table(("内部字段", "需要的信息", "可用澄清问题", "关联证据"), [
        [slot["key"], slot["description"], slot["question"], slot["evidence_card_id"]] for slot in task["slots"]]))
    chunks += ['<h4>统一来源与全部证据卡</h4>']
    for card in task["evidence_cards"]:
        chunks += ['<details class="source"><summary>' + _h(card["id"]) + ' · ' + _h(card["title"]) + '</summary>',
                   _paragraph(card["body"], "preline"), '</details>']
    chunks.append('</section>')
    return ''.join(chunks)


def render_review_html(report: dict | None = None) -> str:
    """Render a self-contained escaped researcher report; default loads inventory()."""
    report = inventory() if report is None else report
    forms = {form["age_band"]: form for form in report["forms"]}
    tasks = {task["age_band"]: task for task in report["tasks"]}
    chunks = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
              '<meta name="viewport" content="width=device-width,initial-scale=1">',
              '<meta name="robots" content="noindex,nofollow"><title>全部问卷与任务审查 · 研究者材料</title>',
              '''<style>
:root{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:#203043;background:#f3f6fa;font-size:16px;line-height:1.7}
*{box-sizing:border-box}body{margin:0}main{max-width:1160px;margin:auto;padding:28px 20px 60px}h1,h2,h3,h4{line-height:1.45}h1{font-size:1.9rem}h2{font-size:1.55rem}h3{font-size:1.25rem}h4{font-size:1.05rem}a{color:#155b8e}p,dd{overflow-wrap:anywhere}.notice{border:2px solid #a14b00;background:#fff3df;border-radius:12px;padding:18px}.muted{color:#53647a;font-size:.94rem}.preline{white-space:pre-line}.toc{display:flex;gap:10px;flex-wrap:wrap;list-style:none;padding:0}.toc a{display:block;background:white;border:1px solid #bfd0e4;padding:7px 14px;border-radius:6px}details{background:white;border:1px solid #cdd8e6;border-radius:9px;margin:12px 0;padding:14px}summary{cursor:pointer;font-weight:650;min-height:30px}summary:focus-visible,a:focus-visible{outline:3px solid #3584c6;outline-offset:4px}.age-content{padding-top:4px}.question{border:1px solid #d3deea;border-radius:9px;padding:16px;margin:16px 0;break-inside:avoid;background:#fff}.stem{font-size:1.05rem;font-weight:550}.answer{background:#eaf5ed;border-radius:5px;padding:3px 5px}.answer strong{color:#155b30}li{padding-bottom:5px}dl{display:grid;grid-template-columns:8.5em 1fr;gap:5px 12px}dt{font-weight:650}dd{margin:0}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:.95rem;margin:12px 0}th,td{border:1px solid #cbd7e5;text-align:left;vertical-align:top;padding:10px;min-width:75px}th{background:#eaf0f7}td{white-space:pre-line;overflow-wrap:anywhere}.task{border-top:2px solid #bfd0e4;margin-top:30px;padding-top:18px}.source{background:#f5f8fc}.case{background:#fbfcff}.footer{margin-top:28px;color:#53647a}
@media(max-width:600px){main{padding:14px 12px 40px}h1{font-size:1.5rem}dl{display:block}dd{margin:0 0 10px}details{padding:11px}.question{padding:12px}th,td{padding:8px}}
@media print{body{background:white}main{max-width:none;padding:0}.toc{display:none}details,.question{border-color:#aaa}h2,h3,h4,summary{break-after:avoid}.table-wrap{overflow:visible}table{font-size:9pt}a{color:inherit}details{break-inside:auto}}
</style></head><body><main>''',
              '<h1>全部问卷与信息任务审查</h1>',
              '<div class="notice"><strong>非公开研究材料 · 含答案及案例私事实</strong>' + _paragraph(report["access"]) + '</div>',
              _paragraph('审查版本：' + report["review_version"] + '；生成时间：' + report["generated_at"], "muted"),
              _paragraph(report["status"], "muted"),
              _table(("旧候选题", "新表单呈现题", "独立新材料", "年龄任务", "任务案例"), [[
                  report["legacy_candidate_count"], report["form_item_count"], report["unique_current_material_count"], report["task_count"], report["task_scenario_count"]]]),
              '<h2>审查范围与解释边界</h2>', '<ol>' + ''.join('<li>' + _h(note) + '</li>' for note in report["research_notes"]) + '</ol>',
              '<nav aria-label="年龄与旧题审查目录"><h2>审阅目录</h2><ul class="toc">']
    for band, form in forms.items():
        chunks.append('<li><a href="#age-' + _h(band) + '">' + _h(form["age_label"]) + '</a></li>')
    chunks += ['<li><a href="#legacy">旧题逐项处置</a></li></ul></nav>']
    for band, form in forms.items():
        chunks += ['<details class="age" id="age-' + _h(band) + '"><summary>' + _h(form["age_label"]) +
                   ' · 全部30题＋4个任务案例</summary><div class="age-content">',
                   '<h2>' + _h(form["age_label"]) + '</h2>', _paragraph(form["version"], "muted"),
                   _paragraph('本卷18道共同锚题＋12道情境适配题，五个领域各6题。难度均未标定，不再预设易中难比例；是否读懂及能否区分素养，仍需真实试做核验。'),
                   '<h3>全部问卷题目与审查理由</h3>']
        chunks.extend(_question_html(item) for item in form["items"])
        chunks += [_task_html(tasks[band]), '</div></details>']
    chunks += ['<details id="legacy"><summary>旧题逐项处置：48道候选题</summary>',
               _paragraph('旧代码实际包含30道基础题，加儿童、成人、老年三个家族各6道替换题，共48道候选。九个旧表单重复使用这些材料。全部原文均已重写或移出原知识点，未将旧记录重新评分。')]
    for item in report["legacy_items"]:
        chunks += ['<article class="question"><h3>' + _h(item["id"]) + ' · ' + _h(item["family"]) + ' · ' + _h(item["disposition"]) + '</h3>',
                   _paragraph(item["question"], "stem"), '<ol type="A">']
        for index, option in enumerate(item["options"]):
            chunks.append('<li>' + _h(option) + (' <strong>〔旧题原指定答案〕</strong>' if index == item["correct"] else '') + '</li>')
        chunks += ['</ol><dl><dt>处置理由</dt><dd>' + _h(item["reason"]) + '</dd><dt>原背景门槛</dt><dd>' +
                   _h(item["prior_knowledge_demand"]) + '</dd><dt>替代题ID</dt><dd>' +
                   _h('、'.join(item["replacement_item_ids"])) + '</dd></dl></article>']
    chunks += ['</details>', '<p class="footer">本文件无外部资源，可离线阅读。JSON版本同样包含答案，不得放入受试者可访问的静态目录。上述内容审查不替代真实受试者预测试与伦理审查。</p></main></body></html>']
    return ''.join(chunks)
