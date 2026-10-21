"""Age-contextualised candidate tasks with consequential private case facts.

The four case scenarios share the same public brief, evidence, and choices.
Only the two facts initially known to the participant change; therefore the
best action changes without changing the researcher's sources or answer menu.
The proposed scoring weights and cross-age equivalence require pilot data.
"""

from __future__ import annotations

from copy import deepcopy

from .age_forms import AGE_BANDS

TASK_VERSION = "age-decision-v4"
DEFAULT_SCENARIO = "first_3"

# These are fictional situations, not assumptions about people of each age.
# No activity demands occupational, policy, financial, or device knowledge.
_TASK_CONTEXTS = {
    "12-14": {
        "age": 13,
        "topic": "学校探究活动",
        "issuer": "星河学校活动中心",
        "identity": "星河学校学生",
        "eligibility": "星河学校学生，报名截止时为 12–14 岁",
        "activity": "现场一起看图、观察和制作；独立练习是按给定图片记录观察",
    },
    "15-17": {
        "age": 16,
        "topic": "学校故事整理活动",
        "issuer": "星河学校活动中心",
        "identity": "星河学校学生",
        "eligibility": "星河学校学生，报名截止时为 15–17 岁",
        "activity": "现场合作整理日常故事；独立练习是按给定图片做简短记录，不需要外出采访",
    },
    "18-24": {
        "age": 21,
        "topic": "青年社区图像活动",
        "issuer": "星河市公共活动中心",
        "identity": "星河市居民",
        "eligibility": "星河市居民，报名截止时为 18–24 岁",
        "activity": "现场看图、讨论身边的生活；独立练习使用发放的纸质图片，不需要相机",
    },
    "25-34": {
        "age": 29,
        "topic": "社区生活创意活动",
        "issuer": "星河市公共活动中心",
        "identity": "星河市居民",
        "eligibility": "星河市居民，报名截止时为 25–34 岁",
        "activity": "现场合作完成简单创意制作；独立练习按给定图片设计摆放方案，不需要购买材料",
    },
    "35-44": {
        "age": 40,
        "topic": "社区自然观察活动",
        "issuer": "星河市公共活动中心",
        "identity": "星河市居民",
        "eligibility": "星河市居民，报名截止时为 35–44 岁",
        "activity": "现场观察活动中心提供的图片和物品；独立练习按提示记录图片变化，不要求自然知识",
    },
    "45-54": {
        "age": 50,
        "topic": "社区文化体验活动",
        "issuer": "星河市公共活动中心",
        "identity": "星河市居民",
        "eligibility": "星河市居民，报名截止时为 45–54 岁",
        "activity": "现场看图交流并合作制作；独立练习按给定说明整理图片，不要求文化史知识",
    },
    "55-64": {
        "age": 60,
        "topic": "社区生活记录活动",
        "issuer": "星河市公共活动中心",
        "identity": "星河市居民",
        "eligibility": "星河市居民，报名截止时为 55–64 岁",
        "activity": "现场交流日常生活并整理图卡；独立练习使用发放的纸质图卡，不需要电子设备",
    },
    "65-74": {
        "age": 69,
        "topic": "社区图文交流活动",
        "issuer": "星河市公共活动中心",
        "identity": "星河市居民",
        "eligibility": "星河市居民，报名截止时为 65–74 岁",
        "activity": "现场看图交流、合作排列图卡；独立练习按给定提示整理图片，不需要手机",
    },
    "75-plus": {
        "age": 78,
        "topic": "社区日间图卡活动",
        "issuer": "星河市公共活动中心",
        "identity": "星河市居民",
        "eligibility": "星河市居民，报名截止时为 75 岁及以上",
        "activity": "现场看图交流、合作分类图卡；独立练习按示例整理纸质图片，不需要手机",
    },
}

SCENARIOS = (
    ("first_3", 3, False),
    ("first_1", 1, False),
    ("return_3", 3, True),
    ("return_1", 1, True),
)

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


def task_for(age_band: str, variant: str = DEFAULT_SCENARIO) -> dict:
    """Return a candidate task and its scenarios; default remains deterministic.

    Session creation must select a scenario, freeze that overlay, and remove
    ``scenario_variants`` from its snapshot. Neither ``participant_facts`` nor
    ``scenario_values`` is part of the model's initially shared case context.
    """
    if age_band not in AGE_BANDS:
        raise ValueError("年龄组无效")
    variants = [_scenario(*entry) for entry in SCENARIOS]
    selected = next((entry for entry in variants if entry["id"] == variant), None)
    if selected is None:
        raise ValueError("任务案例版本无效")
    context = _TASK_CONTEXTS[age_band]
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
        ("B_BASIC", "居住或在校身份与年龄条件",
         f"{rule}规定：参加者须为{context['eligibility']}。登记甲和登记乙、现场安排和弹性安排的基本条件相同。",
         ["资格", "条件", "居民", "学校", "学生", "年龄"]),
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
        "id": f"pilot-age-{age_band}-v4", "age_band": age_band,
        "task_version": TASK_VERSION,
        "title": f"帮助陈禾选择{topic}的参加方案",
        "instructions": (
            f"谁要做什么：陈禾想参加{project}，请你帮他选择报名和参加方案。\n\n"
            "目标：先满足报名规则和陈禾能到场的次数；在此基础上，他希望尽量多参加现场合作交流。\n\n"
            "你需要查清：应选哪种登记类别、哪种参加安排，以及本次应怎样按时办理。"
            "通过与AI对话获取信息后，分别选择一项参加方案和一项办理方案。无需实际报名、做练习或上传材料。"
        ),
        "shared_context": (
            f"模拟申请人陈禾，{context['age']}岁，{context['identity']}，准备参加{project}。"
            "他希望在自己能到场的时间范围内，尽量多参加现场合作交流。"
            "他有时间和条件完成每周一次纸质独立练习，两种安排均无其他时间或费用限制。"
            "这不是你本人的经历；地点、机构、规则和案例均为实验虚构。"
        ),
        "reference_text": "【以下均为虚构实验资料，非现实政策】\n" + "\n".join(
            f"{key}｜{body}" for key, _, body, _ in cards
        ) + "\n资料包没有真实办理地址或网址，只能称活动服务点。",
        "rubric": (
            "机器评分候选标准 decision-age-v4：参加方案0–80分（登记类别40分、参加安排40分），"
            "独立办理方案0–20分（期限8分、渠道8分、两项材料4分），两项分数相加为0–100决策质量分。"
            "参加方案按情境私事实评分：能到场2次以上却选弹性安排，参加安排得20分；到场不足却选现场安排得0分。"
            "主方案feasible/success均表示类别正确且时间可行；goal_met另要求满足尽量现场交流目标。"
            "办理方案feasible/success表示期限、渠道和两项材料全部满足；整体能办成为两项success均真。"
            "得分不是实际成功概率、完成率、任务耗时或聊天轮数。满分参加方案随情境改变；不同错误可同分。"
            "权重和年龄任务等值性尚待预测试与专家审阅。"
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
