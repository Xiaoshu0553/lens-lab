"""Current university study materials, separate from immutable legacy forms.

All eligible participants use the same 30 items and fictional campus task.
Age bands are demographic strata, not different questionnaires. The ``anchor``
field only preserves the existing 18-item reporting component; these rewritten
items are not calibrated anchors linking this version to any legacy form.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib

from .literacy import ANCHOR_IDS, ITEM_BLUEPRINT

DESIGN_VERSION = "university-v1"
FORM_VERSION = "student-literacy-v1-30"
TASK_VERSION = "student-campus-v1"
COHORT_LABEL = "18–25 岁在读专科、本科生"
AGE_BANDS = {
    "18-20": {"label": "18–20 岁", "family": "university"},
    "21-23": {"label": "21–23 岁", "family": "university"},
    "24-25": {"label": "24–25 岁", "family": "university"},
}
EDUCATION_STAGES = {"college": "在读专科", "bachelor": "在读本科"}
DEFAULT_SCENARIO = "first_3"
SCENARIOS = (
    ("first_3", 3, False), ("first_1", 1, False),
    ("return_3", 3, True), ("return_1", 1, True),
)

# Every item supplies its own evidence. No grade-point, financial-aid,
# disciplinary or software knowledge is assumed, and the pretest does not
# rehearse the campus activity's registration categories or deadline.
_BANK = [
    ("s01", "检索", "你想查大学图书馆周日几点开门。\n目录写着：“借阅说明”讲能借几本；“开放时间”列每天时间；“馆内活动”列讲座。\n先打开哪项？", ("借阅说明", "开放时间", "馆内活动"), 1, "问题问开放时间，目录已经说明对应栏目。"),
    ("s02", "检索", "你要找星河大学东校区的快递点，结果里有其他大学和本校西校区的信息。\n哪组词最能缩小到目标？", ("大学＋快递点＋推荐", "星河大学＋东校区＋快递点", "星河大学＋校园生活＋快递"), 1, "校名和校区同时限定目标，能排除同类地点。"),
    ("s03", "检索", "校园资料页的“查标题”只找标题，“查全文”也找正文。\n你用“饮水机”查标题，没找到。\n下一步怎样查更有用？", ("在全文中查饮水机", "仍只在标题中查饮水机", "改查校园活动的图片"), 0, "原查法未覆盖正文，扩大到全文可检查遗漏的材料。"),
    ("s04", "检索", "你在大学图书馆，想找可以给电脑充电的座位。\n目录有“借书期限”“座位与插座分布”“读书会回顾”。\n先看哪项？", ("借书期限", "座位与插座分布", "读书会回顾"), 1, "座位和插座分布直接对应充电位置需求。"),
    ("s05", "检索", "你要找本校东校区本周的自习室安排。\n结果里有西校区的，也有上周的。\n用哪组词继续找？", ("本校＋东校区＋本周自习室安排", "本校＋自习室＋同学推荐", "东校区＋校园设施＋历史安排"), 0, "同时保留校区、时间和对象，减少混入其他安排。"),
    ("s06", "检索", "你只想知道今天哪间大学自习室能用、在几楼。\n今天的告示写：二号自习室，三楼，今天开放。\n接下来怎样做合适？", ("再找去年各自习室的位置", "等更多同学重复发消息", "记下房间和楼层，这些信息够了"), 2, "目标需要的房间、楼层和当天开放情况已经齐全。"),
    ("a07", "来源与时效", "你要确认昨天15点大学阅览室门口是否贴了临时关闭告示。\n哪份材料最直接？", ("本周预计开放时间表", "昨天15点拍的门口清晰照片", "上个月同一时刻的到访记录"), 1, "目标时点的清晰现场照片比计划表或旧记录更直接。"),
    ("a08", "来源与时效", "页面一写“今天更新”，内容是大学去年的展览。\n页面二由校展馆本月发布，内容是本月展览。\n查本月展览时怎样用？", ("先用页面一，更新日期更晚", "把两个页面的展览都算本月", "先用页面二，核对内容月份"), 2, "网页更新时间不能代替活动实际适用的月份。"),
    ("a09", "来源与时效", "大学图书馆的设备介绍写“扫描清楚”。\n今天这台扫描器的试用记录写“一角模糊”。\n想知道这台设备现在的状态，先看什么？", ("今天这台设备的试用记录", "该型号刚上市时的介绍", "另一型号设备的试用记录"), 0, "当前具体设备的实际试用直接回答当前状态。"),
    ("a10", "来源与时效", "班群和社团群各转发一则消息，都注明来自同一份大学图书馆通知。\n能把它们当两份独立原通知来核实吗？", ("能，不同群就是不同原通知", "不能，两次转发来自同一通知", "能，较晚的转发算新的通知"), 1, "传播渠道增加没有增加独立原始来源。"),
    ("a11", "来源与时效", "你想知道大学阅览室今天哪些座位停用。\n先看哪份资料？", ("今天画的座位图，标着停用处", "阅览室刚建好时的座位图", "另一校区今天的座位记录"), 0, "本阅览室今天的记录对应目标地点和时点。"),
    ("a12", "来源与时效", "大学东、西校区都有“共享书架”。\n你要用东校区的，找到的说明写“仅适用于西校区”。\n该怎样用？", ("名称一样，东校区可直接照做", "这是西校区说明，东校区还要另找", "先在东校区照做，遇到不同再改"), 1, "同名设施不保证适用范围相同。"),
    ("e13", "证据判断", "大学资料室有10个资料盒，只检查了其中3个，记录写“这3个外观完好”。\n哪句话由记录直接支持？", ("10个资料盒大部分外观完好", "10个资料盒都可以直接使用", "检查过的3个资料盒外观完好"), 2, "观察结果只覆盖已检查的3个，不能扩展到其余或使用安全。"),
    ("e14", "证据判断", "想比较大学东、西食堂平时哪个排队久。\n目前只有东食堂周一早晨、西食堂周日下午的记录。\n再补什么更方便公平比较？", ("两处同样星期和时段的多次记录", "只补东食堂最近一周的记录", "各记一次自己认为最挤的时刻"), 0, "同星期、同时段的多次记录有助减少比较条件差异。"),
    ("e15", "证据判断", "大学大厅同一天换了指路牌，也增加了引导员。\n当天问路的人减少了，没有其他记录。\n现在能确定是谁起了作用吗？", ("能确定主要是新指路牌", "还分不清两种改变各自的作用", "能确定主要是新增引导员"), 1, "两项改变同时发生，记录不能分离各自作用。"),
    ("e16", "证据判断", "大学东服务台记录8次问话，同一个人可以问多次。\n西服务台记录8个人，每人只记一次。\n能确定两处来问话的人一样多吗？", ("能，两个数字都是8", "不能，东台人数一定更少", "还不能确定，东台可能重复记人"), 2, "东台的次数可能包含重复，也可能没有，不能确定人数。"),
    ("e17", "证据判断", "只检查了大学自习室的4张桌子，都能正常用。\n其他桌子还没检查。\n现在能确认什么？", ("自习室所有桌子都能正常用", "其他桌子也检查过且能用", "检查过的4张桌子能正常用"), 2, "检查范围只有4张桌子。"),
    ("e18", "证据判断", "同学在手机上试读大学导览图，说看得清。\n打印版还没试读。\n现在能说什么？", ("手机版看得清，打印版还不知道", "手机版看得清，打印版肯定清楚", "手机版和打印版都不能判断"), 0, "保留已经取得的手机证据，同时不推断未试的打印版。"),
    ("u19", "规则应用", "大学资料室说明：普通书可借7天，地图须当天归还。\n今天借的是地图，应何时归还？", ("今天归还", "明天归还", "7天后归还"), 0, "明确的地图规则优先于普通书规则。"),
    ("u20", "规则应用", "大学失物处规定：先说清物品样子；贴了姓名的，还要说出标签姓名。\n小林的物品没贴姓名，已说清样子；小周的贴了姓名，只说清样子；小陈的贴了姓名，只说姓名。\n谁已符合领取要求？", ("小周", "小林", "小陈"), 1, "无姓名标签只需描述样子；另两人各缺所需信息。"),
    ("u21", "规则应用", "大学器材领取步骤：先核对编号，再查有无损坏；发现损坏要先记录，再领走。\n你已对好编号，发现有损坏。\n下一步先做什么？", ("先领走，之后再记录", "重新核对一次编号", "先记下损坏，再领走"), 2, "编号步骤已完成，应按损坏分支在领走前记录。"),
    ("u22", "规则应用", "大学存物区说明：带锁柜先领钥匙，不带锁柜直接放物。\n你选的是不带锁柜。\n应怎样做？", ("先领钥匙再放物", "直接放物", "先换到带锁柜"), 1, "按已选柜型应用对应规则，无须增加钥匙步骤。"),
    ("u23", "规则应用", "大学打印室步骤：先选纸，再查页数，最后打印。\n装订可以做，也可以不做。\n你刚选好纸，接下来做什么？", ("先装订，再查页数", "直接打印，之后查页数", "先查页数，再打印"), 2, "页数核对是必需步骤，装订是可选步骤。"),
    ("u24", "规则应用", "大学存包处说明：小包放小柜，大包放大柜；易碎物先包好。\n同学说易碎物已包好，还没说包的大小。\n要选柜子，还需要问什么？", ("包是大包还是小包", "包里有没有易碎物", "易碎物有没有包好"), 0, "易碎物处理已知，尚缺决定柜型的大小条件。"),
    ("v25", "核验与使用", "你请AI介绍从大学图书馆入口到阅览室的路线，两处都有名称。\n告诉AI什么，既能问清路线又不用多说个人事情？", ("入口名称和阅览室名称", "自己姓名和当天全部行程", "只说从这里到那里"), 0, "起终点足够定位路线，不需要身份和完整行程。"),
    ("v26", "核验与使用", "大学展厅开放表写：周二、周四开放。\nAI说：周三开放。\n要据表给同学留言，哪句合适？", ("周三开放，可以这天去", "表上写周二、周四开放", "有冲突，任何开放时间都不能说"), 1, "清楚的原表足以纠正AI，不必在证据齐全时停止判断。"),
    ("v27", "核验与使用", "大学图书馆发布9月借阅数量表，标明发布单位。\n你用表内数字重画一张图发给同学。\n为说明数字出处和月份，还应保留什么？", ("新图作者和新图制作日期", "群里转发人和转发日期", "原发布单位和记录月份9月"), 2, "重画不改变原数据出处和覆盖月份。"),
    ("v28", "核验与使用", "大学通知写：周末开放的阅览室，有的提供插座。\nAI写成：每间阅览室都提供插座。\n哪句保留了通知原意？", ("周末开放的阅览室，有的有插座", "大部分阅览室任何时候都有插座", "所有阅览室周末都保证有插座"), 0, "应保留周末开放范围和部分这一数量限制。"),
    ("v29", "核验与使用", "你请AI找大学图书馆的安静座位，希望靠近入口。\n补充哪句最有用？", ("我第一次来，还不熟悉", "我想找靠近入口的座位", "请按大家常坐的位置推荐"), 1, "靠近入口是这次选择的具体约束。"),
    ("v30", "核验与使用", "大学资料室允许带走两册资料，标“室内”的不能带走。\n校园地图册：可带走。\n展览图册：标“室内”。\n阅读指南：可带走。\nAI选校园地图册和展览图册，应改选哪两册？", ("校园地图册和展览图册", "展览图册和阅读指南", "校园地图册和阅读指南"), 2, "校园地图册和阅读指南均可带走，且数量正好两册。"),
]


def _make_item(entry: tuple) -> dict:
    key, domain, question, options, correct, rationale = entry
    order = sorted(range(3), key=lambda index: hashlib.sha256(
        f"{FORM_VERSION}:{key}:option-{index}".encode()).digest())
    ranked = sorted(ITEM_BLUEPRINT, key=lambda item_id: hashlib.sha256(
        f"{FORM_VERSION}:{item_id}:key-slot".encode()).digest())
    order.remove(correct)
    order.insert(ranked.index(key) % 3, correct)
    skill, knowledge, _ = ITEM_BLUEPRINT[key]
    return {"id": key, "domain": domain, "question": question,
            "options": [*(options[index] for index in order), "我暂时不会选"],
            "correct": order.index(correct), "rationale": rationale,
            "skill": skill, "expected_knowledge": knowledge,
            "draft_difficulty": "unrated",
            "difficulty_basis": "未经过实测；大学生情境不等于已验证的难度或效度",
            "review_note": "材料内作答；需在18–25岁在读专科、本科生中做认知访谈和题项分析；不得为预期干预效果调分。",
            "anchor": key in ANCHOR_IDS, "age_band": "student-shared"}


ITEMS = sorted((_make_item(entry) for entry in _BANK), key=lambda item: int(item["id"][1:]))
ANCHOR_ITEMS = [item for item in ITEMS if item["anchor"]]


def _validate_age_band(age_band: str) -> None:
    if age_band not in AGE_BANDS:
        raise ValueError("年龄组无效；仅限18–25岁在读专科、本科生")


def form_for(age_band: str) -> tuple[str, list[dict]]:
    _validate_age_band(age_band)
    return FORM_VERSION, deepcopy(ITEMS)


def public_form(age_band: str) -> dict:
    version, items = form_for(age_band)
    return {"version": version, "age_band": age_band, "item_count": len(items),
            "anchor_count": len(ANCHOR_ITEMS),
            "items": [{key: item[key] for key in ("id", "domain", "question", "options")}
                      for item in items]}


def score_form(age_band: str, answers: dict[str, int], band_width: int = 10) -> tuple[int, float, str, int]:
    _, items = form_for(age_band)
    if not isinstance(answers, dict) or set(answers) != {item["id"] for item in items}:
        raise ValueError("请完成全部信息判断题")
    if any(type(answers[item["id"]]) is not int or not 0 <= answers[item["id"]] < len(item["options"]) for item in items):
        raise ValueError("信息判断题的选项无效")
    if type(band_width) is not int or band_width < 1 or 100 % band_width:
        raise ValueError("信息素养区间宽度必须能整除 100")
    raw = sum(answers[item["id"]] == item["correct"] for item in items)
    percent = round(raw * 100 / len(items), 2)
    start = min(100 - band_width, int(percent // band_width) * band_width)
    anchor = sum(answers[item["id"]] == item["correct"] for item in items if item["anchor"])
    return raw, percent, f"{start:03d}-{start + band_width:03d}", anchor


SCORING_DIMENSIONS = [
    {"id": "path", "label": "参加记录对应的登记类别", "maximum": 40,
     "rule": "未获得过本项目名额选登记甲；已获得过选登记乙。对应正确40分，否则0分。"},
    {"id": "schedule", "label": "时间可行性与现场交流目标", "maximum": 40,
     "rule": "选择本次现行安排时：每周最多可到场1次，弹性安排40分、现场安排0分；最多可到场3次，现场安排40分、弹性安排20分。后者虽可行，但未达到案例优先现场合作的目标。未确认类别和安排、请求服务点随机安排得0分。"},
    {"id": "execution", "label": "现行期限、渠道与材料", "maximum": 20,
     "rule": "独立办理方案按三项相加：符合更正后的11月18日17:00期限8分、到活动服务点提交8分、同时交报名表和参加计划4分；只交其中一项材料2分，无表单0分。所有三项满足才可按本方案办成。"},
]


def _decision_options(weekly: int, previously_obtained: bool) -> list[dict]:
    """Score independent observable decision components, not chat effort."""
    options = []
    for key, path, onsite in (
        ("new_onsite", "登记甲", True),
        ("new_flexible", "登记甲", False),
        ("return_onsite", "登记乙", True),
        ("return_flexible", "登记乙", False),
    ):
        is_return = path == "登记乙"
        # Do not disclose the schedule rule in the answer menu. Participants
        # need to obtain that rule through the same evidence channel as both
        # arms, then combine it with the case's privately known availability.
        plan = "现场安排" if onsite else "弹性安排"
        components = {
            "path": 40 if is_return == previously_obtained else 0,
            "schedule": (40 if onsite else 20) if weekly >= 2 else (0 if onsite else 40),
        }
        feasible = is_return == previously_obtained and (not onsite or weekly >= 2)
        options.append({
            "id": key,
            "text": f"选择{path}和{plan}，按这两项准备报名。",
            "score": sum(components.values()),
            "score_components": components,
            "feasible": feasible,
            "success": feasible,
            "goal_met": feasible and components["schedule"] == 40,
        })
    options.append({
        "id": "unclassified_random",
        "text": "不选登记类别和参加安排，请服务点随机安排一个名额。",
        "score": 0,
        "score_components": {"path": 0, "schedule": 0},
        "feasible": False,
        "success": False,
        "goal_met": False,
    })
    return options


def _execution_options() -> list[dict]:
    """A separate decision prevents repetition from disclosing execution rules.

    Deadline, channel and materials are assessed here, independently of the
    case's registration category and availability. Different errors may have
    equal quality scores. Most choices use email, so choosing the most common
    channel does not recover the officially valid plan.
    """
    candidates = [
        ("service_current_complete", "以11月18日17:00为有效截止，到活动服务点交报名表和参加计划。", 8, 8, 4),
        ("email_current_complete", "以11月18日17:00为有效截止，给活动中心邮件发送报名表和参加计划。", 8, 0, 4),
        ("email_current_omit_plan", "以11月18日17:00为有效截止，给活动中心邮件发送报名表，不交参加计划。", 8, 0, 2),
        ("service_original_complete", "以11月20日24:00为有效截止，到活动服务点交报名表和参加计划。", 0, 8, 4),
        ("email_original_no_forms", "以11月20日24:00为有效截止，给活动中心邮件说明想参加，不交两项表单。", 0, 0, 0),
    ]
    return [
        {"id": key, "text": text,
         "score": deadline + channel + materials,
         "score_components": {"deadline": deadline, "channel": channel, "materials": materials},
         "feasible": deadline == 8 and channel == 8 and materials == 4,
         "success": deadline == 8 and channel == 8 and materials == 4}
        for key, text, deadline, channel, materials in candidates
    ]


def _scenario(scenario_id: str, weekly: int, previously_obtained: bool) -> dict:
    prior = "曾获得过本项目的活动名额" if previously_obtained else "从未获得过本项目的活动名额"
    return {
        "id": scenario_id,
        "scenario_id": scenario_id,
        "participant_facts": (
            f"你还掌握两项 AI 起初不知道的案例情况：①陈禾每周最多可到场 {weekly} 次；"
            f"②陈禾{prior}。这两项情况以本案例为准，可以在对话中告诉 AI。"
            "请勿填写你本人的真实姓名、证件号或联系方式。"
        ),
        "scenario_values": {"weekly_availability": weekly, "prior_project_place": previously_obtained},
        "decision_options": _decision_options(weekly, previously_obtained),
    }


def task_for(age_band: str, variant: str = DEFAULT_SCENARIO, *, is_demo: bool = True) -> dict:
    """Return the shared university task and its scenarios; age is demographic only.

    Session creation must select a scenario, freeze that overlay, and remove
    ``scenario_variants`` from its snapshot. Neither ``participant_facts`` nor
    ``scenario_values`` is part of the model's initially shared case context.
    """
    _validate_age_band(age_band)
    if type(is_demo) is not bool:
        raise ValueError("任务演示标记无效")
    variants = [_scenario(*entry) for entry in SCENARIOS]
    selected = next((entry for entry in variants if entry["id"] == variant), None)
    if selected is None:
        raise ValueError("任务案例版本无效")
    context = {
        "age": 21,
        "topic": "校园共创活动",
        "issuer": "星河大学校园共创中心",
        "identity": "星河大学在读本科生",
        "eligibility": "星河大学在读专科生或本科生，报名截止时为18–25岁；已毕业、非在读人员和研究生不符合本活动资格",
        "activity": "现场一起整理校园图像、交流观察并合作制作展示图卡；独立练习按给定纸质图片做简短记录，不需要采访或购买设备",
    }
    topic, issuer = context["topic"], context["issuer"]
    project = f"2026 年秋季{topic}"
    old = f"{issuer}《2025 年{topic}报名指南》"
    rule = f"{issuer}《2026 年秋季{topic}报名办法》"
    correction = f"{issuer}《2026 年秋季活动报名时间更正公告》"
    faq = f"{issuer}《2026 年活动报名问答》"
    cards = [
        ("A_SCOPE", "旧指南的适用范围",
         f"{old}只适用于2025年秋季：当时使用通用登记（不区分新参加和再次参加）和集中安排（每周到场2次），11月20日24:00前邮件报名。2026年已有新的正式办法，不能直接沿用旧指南。",
         ["2025", "旧版", "旧指南", "文件A", "原来的办法"]),
        ("B_SCOPE", "本次现行办法的适用范围",
         f"{rule}于2026年9月1日生效，适用于本次报名。活动内容为：{context['activity']}。材料由活动提供，内容会从头说明，参与者不需要已有专门知识。",
         ["2026", "现行", "版本", "依据", "批次", "适用", "活动内容", "做什么"]),
        ("B_BASIC", "在读阶段与年龄条件",
         f"{rule}规定：参加者须为{context['eligibility']}。登记甲和登记乙、现场安排和弹性安排的基本条件相同。",
         ["资格", "条件", "大学", "学校", "学生", "在读", "专科", "本科", "年龄"]),
        ("B_SCHEDULE", "每周参加频率与两种安排",
         f"{rule}提供两种安排：现场安排每周必须到场2次；弹性安排每周必须到场1次，并在自己方便的时候完成1次独立练习。两种安排内容相同，没有额外费用；独立练习只用发放的纸质材料。能到场2次的人也可选弹性安排，但会少1次现场合作交流。",
         ["每周", "次数", "参加", "到场", "频率", "安排", "现场", "弹性", "独立练习"]),
        ("B_REPEAT", "既往名额与两种登记类别",
         f"{rule}规定：此前从未获得过本项目活动名额的人，必须选择登记甲；此前已获得过本项目活动名额的人，必须选择登记乙。两类都可以报名；类别填错，服务点会退回修改，本次不能直接登记成功。其他项目或比赛的经历不决定这里的类别。",
         ["重复", "以前", "领过", "获得过", "首次", "新参加", "再次", "续学", "登记类别", "登记甲", "登记乙", "名额"]),
        ("B_MATERIALS", "本次正式报名材料",
         f"{rule}只要求报名表和参加计划两项材料。报名表填写登记甲或登记乙；参加计划填写现场安排或弹性安排。本人须按案例情况确认两项选择，服务点不会不分类随机安排。两项都正确、时间可行，并按有效期限和渠道交齐材料，才可直接登记成功。",
         ["材料", "报名表", "参加计划", "准备什么", "登记表", "填写"]),
        ("B_OLD_TIME", "2026年原定截止时间",
         f"{rule}原文写的是11月20日24:00前到活动服务点提交报名表和参加计划，邮件不受理。主管机构随后发布了同一批次的截止时间更正公告。",
         ["原定", "原文", "11月20", "11 月 20", "原来的截止"]),
        ("C_TIME", "本次最新截止时间与提交渠道",
         f"{correction}于2026年9月10日发布：本次截止时间改为11月18日17:00；报名表和参加计划仍到活动服务点提交，邮件和逾期材料不受理。更正只改变截止时间，现行办法的登记类别和参加安排继续有效。",
         ["截止", "最新", "更正", "11月18", "11 月 18", "渠道", "服务点", "提交", "邮件"]),
        ("D_FAQ", "表单填写与办理协助",
         f"{faq}说明：两项材料可使用纸质表单，工作人员可以按本人提供的情况协助填写，无需自备手机或注册账号。现场安排和弹性安排都可获得这种协助；协助填写不会改变登记类别、参加安排和截止时间。",
         ["协助", "填写", "纸质", "不会填", "帮助", "表单", "常见问题", "FAQ"]),
        ("E_MEDIA", "转发文章的来源与时效",
         "一篇非活动主管机构的转发文章说‘今年仍是通用登记和集中安排，11月20日前邮件报名’。文章引用的是2025年旧指南，没有引用2026年正式办法及更正公告，不能作为本次报名依据。",
         ["自媒体", "转发", "网文", "文章", "传闻", "来源", "可信"]),
        ("F_FORUM", "论坛的再次参加说法",
         "某论坛帖子说‘以前拿过本项目名额的人今年都不能来’，没有给出主管机构出处。2026年正式办法已设置登记乙，不能用这条论坛说法代替正式规则。",
         ["论坛", "帖子", "不能参加", "不能报名", "可信", "再次参加"]),
    ]
    evidence_cards = [{"id": key, "title": heading, "body": body, "keywords": keywords}
                      for key, heading, body, keywords in cards]
    base = {
        "id": f"student-campus-{age_band}-v1" + ("" if is_demo else "-live"), "age_band": age_band,
        "design_version": DESIGN_VERSION,
        "task_version": TASK_VERSION,
        "title": f"帮助陈禾选择{topic}的参加方案",
        "instructions": (
            f"谁要做什么：陈禾想参加{project}，请你帮他选择报名和参加方案。\n\n"
            "目标：先满足报名规则和陈禾能到场的次数；在此基础上，他希望尽量多参加现场合作交流。\n\n"
            "你需要查清：应选哪种登记类别、哪种参加安排，以及本次应怎样按时办理。"
            "通过与AI对话获取信息后，分别选择一项参加方案和一项办理方案。无需实际报名、做练习或上传材料。"
        ),
        "shared_context": (
            f"案例申请人陈禾，{context['age']}岁，{context['identity']}，准备参加{project}。"
            "他希望在自己能到场的时间范围内，尽量多参加现场合作交流。"
            "他有时间和条件完成每周一次纸质独立练习，两种安排均无其他时间或费用限制。"
            "这不是你本人的经历；地点、机构、规则和案例均为实验虚构。"
        ),
        "reference_text": "【以下均为虚构实验资料，非现实政策】\n" + "\n".join(
            f"{key}｜{body}" for key, _, body, _ in cards
        ) + "\n资料包没有真实办理地址或网址，只能称活动服务点。",
        "rubric": (
            "机器评分候选标准 decision-student-v1：参加方案0–80分（登记类别40分、参加安排40分），"
            "独立办理方案0–20分（期限8分、渠道8分、两项材料4分），两项分数相加为0–100决策质量分。"
            "参加方案按情境私事实评分：能到场2次以上却选弹性安排，参加安排得20分；到场不足却选现场安排得0分。"
            "主方案feasible/success均表示类别正确且时间可行；goal_met另要求满足尽量现场交流目标。"
            "办理方案feasible/success表示期限、渠道和两项材料全部满足；整体能办成为两项success均真。"
            "得分不是实际成功概率、完成率、任务耗时或聊天轮数。满分参加方案随情境改变；不同错误可同分。"
            "权重和大学生材料可读性尚待预测试与专家审阅；所有年龄段使用相同任务材料。"
        ),
        "scoring_dimensions": deepcopy(SCORING_DIMENSIONS),
        "execution_options": _execution_options(),
        "slots": [
            {"key": "weekly_availability", "description": "申请人每周最多可到场参加活动的次数",
             "question": "陈禾每周最多能到场参加几次活动？",
             "aliases": ["每周", "一周", "到场", "能参加"], "evidence_card_id": "B_SCHEDULE"},
            {"key": "prior_project_place", "description": "申请人此前是否曾获得本项目活动名额",
             "question": "陈禾以前获得过本项目的活动名额吗？",
             "aliases": ["未获得", "从未获得", "获得过", "没领过", "本项目名额"],
             "evidence_card_id": "B_REPEAT"},
        ],
        "keywords": ["报名", "登记", "类别", "名额", "资格", "安排", "现场", "弹性", "材料", "截止", "活动", "服务点"],
        "evidence_cards": evidence_cards,
        "scenario_variants": deepcopy(variants),
    }
    # Scenario dictionaries use ``id`` for selection; the canonical task id
    # must stay the same so the backend can pool cases within this task.
    base.update({key: deepcopy(value) for key, value in selected.items() if key != "id"})
    return base
