"""Parallel fictional pilot tasks for narrow age strata.

All versions use the same evidence graph, five decision qualities, and source
conflict. Their equivalence is a hypothesis to test, not an established fact.
"""

from __future__ import annotations

from app.age_forms import AGE_BANDS, TOPICS

_PERSONAS = {
    "12-14": (13, "初中生", "校级绘画比赛奖品"),
    "15-17": (16, "高中生", "校级写作比赛奖品"),
    "18-24": (21, "青年", "一次性摄影比赛奖品"),
    "25-34": (29, "成年人", "一次性摄影比赛奖品"),
    "35-44": (40, "成年人", "一次性摄影比赛奖品"),
    "45-54": (50, "成年人", "一次性摄影比赛奖品"),
    "55-64": (60, "成年人", "一次性摄影比赛奖品"),
    "65-74": (69, "老年人", "一次性书法比赛奖品"),
    "75-plus": (78, "老年人", "一次性书法比赛奖品"),
}


def task_for(age_band: str) -> dict:
    if age_band not in AGE_BANDS:
        raise ValueError("年龄组无效")
    title = TOPICS[age_band]
    age, person, prize = _PERSONAS[age_band]
    age_label = AGE_BANDS[age_band]["label"]
    issuer = "星河市公共学习中心"
    project = f"2026 年秋季{title}"
    old = f"{issuer}《2025 年{title}报名指南》"
    rule = f"{issuer}《2026 年秋季{title}报名办法》"
    correction = f"{issuer}《2026 年秋季报名时间更正公告》"
    faq = f"{issuer}《2026 年报名常见问题》"
    common = (
        f"模拟申请人陈禾，{age} 岁，星河市居民，{person}，准备报名{project}。"
        f"曾获得{prize}。所有地点、机构和规则均为实验虚构。"
    )
    private = (
        "你掌握两项 AI 起初不知道的情况：陈禾每周可参加 3 次活动，从未获得过本项目的学习名额。"
        "之前的比赛奖品并非本项目名额。AI 若需要，可在对话中提供这些情况。"
        "请勿输入真实姓名、证件号或联系方式。"
    )
    cards = [
        ("A_SCOPE", "旧指南的适用范围", f"{old}只适用于 2025 年秋季批次；其中的报名办法不能直接用于 2026 年秋季。", ["2025", "旧版", "旧指南", "文件A", "适用"]),
        ("B_SCOPE", "现行办法的效力", f"{rule}于 2026 年 9 月 1 日生效，替代 2025 年旧指南，适用于本次报名。", ["2026", "现行", "版本", "依据", "批次", "适用", "办法", "文件B"]),
        ("B_BASIC", "居住地与年龄条件", f"{rule}规定：报名者须是星河市居民，报名截止时属于{age_label}。", ["资格", "条件", "居民", "年龄"]),
        ("B_SCHEDULE", "参加频率条件", f"{rule}规定：报名者须能每周至少参加 2 次活动。这是获得本次名额的必要条件。", ["每周", "次数", "参加", "时间", "频率"]),
        ("B_REPEAT", "既往名额与比赛奖品", f"{rule}规定：此前不得获得过本项目的学习名额；其他比赛的一次性奖品不算本项目重复名额。", ["重复", "以前", "奖品", "比赛", "领过"]),
        ("B_MATERIALS", "正式报名材料", f"{rule}列明四项材料：报名表、星河市居住情况说明、每周参加计划和本人确认书。", ["材料", "报名表", "居住", "参加计划", "确认书"]),
        ("B_OLD_TIME", "原定办理时点", f"{rule}原文写的是 2026 年 11 月 20 日 24:00 前通过官方报名平台提交，邮件不受理。随后主管机构发布了时间更正公告。", ["原定", "11月20", "11 月 20", "原文", "截止"]),
        ("C_TIME", "截止时间更正", f"{correction}将本次官方报名平台的截止时点更正为 2026 年 11 月 18 日 17:00；逾期和邮件提交均不受理。", ["截止", "最新", "时间", "更正", "11月18", "11 月 18", "平台", "提交"]),
        ("D_FAQ", "参加计划的解释", f"{faq}说明：每周参加次数以报名表内的参加计划为准；上传材料须为清晰 PDF。问答不更改正式办法的资格条件和更正公告的截止时间。", ["参加计划", "PDF", "格式", "常见问题", "FAQ"]),
        ("E_MEDIA", "自媒体说法的来源", f"一篇非主管机构的自媒体文章沿用了 2025 年旧指南的报名办法，没有引用 2026 年现行办法及更正公告，不能单独作为本次报名依据。", ["自媒体", "传闻", "来源", "可信"]),
        ("F_FORUM", "论坛说法的来源", f"某论坛帖子称拿过任何比赛奖品的人都不能报名{project}，但没有给出主管机构文件或发布渠道，不能代替正式办法。", ["论坛", "比赛", "奖品", "不能报名", "可信"]),
    ]
    evidence_cards = [{"id": key, "title": heading, "body": body, "keywords": keywords}
                      for key, heading, body, keywords in cards]
    reference_text = "\n".join(f"{key}｜{body}" for key, _, body, _ in cards)
    return {
        "id": f"pilot-age-{age_band}-v3", "age_band": age_band,
        "title": f"帮助陈禾选择{title}的报名方案",
        "instructions": (
            f"任务背景：陈禾想报名{project}，请你帮他做决定。\n\n"
            "你的目标：根据案例情况和本次规则，判断他能否报名，并确定应怎样办理。\n\n"
            "最终提交：通过与 AI 对话获取所需信息后，在五个行动方案中独立选择最合适的一项。"
            "无需实际报名或上传材料。"
        ),
        "shared_context": common,
        "participant_facts": private,
        "reference_text": "【全部为虚构实验资料】\n" + reference_text + "\n资料包没有真实报名网址，只能称官方报名平台。",
        "rubric": "机器评分 decision-age-v1：按现行规则及时提交四项材料 100；漏掉每周参加计划 75；对无关比赛奖品过度等待 50；沿用旧截止时间 20；因比赛奖品误判不得报名 0。该评分仅为预测试候选标准。",
        "slots": [
            {"key": "weekly_availability", "description": "申请人每周可参加活动的次数", "question": "申请人每周能参加几次活动？", "aliases": ["每周", "3 次", "三次", "能参加"]},
            {"key": "prior_project_place", "description": "申请人是否曾获得本项目名额", "question": "申请人以前获得过本项目的学习名额吗？", "aliases": ["没参加过", "未获得", "从未获得", "没领过", "获得过"]},
        ],
        "keywords": ["报名", "名额", "资格", "材料", "截止", "活动", "文件", "平台"],
        "evidence_cards": evidence_cards,
        "decision_options": [
            {"id": "complete", "text": "在 11 月 18 日 17:00 前通过官方报名平台提交报名表、星河市居住情况说明、每周参加计划和本人确认书。", "score": 100},
            {"id": "omit_plan", "text": "在 11 月 18 日 17:00 前通过官方报名平台提交报名表、星河市居住情况说明和本人确认书；参加计划等平台要求时再补。", "score": 75},
            {"id": "wait_prize", "text": "先询问主管机构比赛奖品是否算重复名额，得到答复后再决定是否准备材料和报名。", "score": 50},
            {"id": "old_deadline", "text": "在 11 月 20 日 24:00 前通过官方报名平台提交报名表、星河市居住情况说明、每周参加计划和本人确认书。", "score": 20},
            {"id": "reject_prize", "text": "因为陈禾曾获得比赛奖品，本次不准备材料，也不报名学习活动。", "score": 0},
        ],
    }
