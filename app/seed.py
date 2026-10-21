from __future__ import annotations

import json

from .db import connect, now_utc
from .age_forms import AGE_BANDS
from .age_tasks import task_for


DEMO_TASK = {
    "id": "demo-xinghe-grant",
    "title": "模拟任务：星河学习支持金申请",
    "instructions": "请用 AI 帮助你判断模拟申请人能否申请，并弄清申请材料和截止日期。完成对话后，用自己的话提交判断和依据。",
    "shared_context": "模拟申请人是星河大学全日制本科二年级学生，平均绩点 3.4。",
    "participant_facts": "你还知道：这名模拟申请人从未获得过同类学习支持金。这个信息不会预先告诉 AI，你可以在对话中说明。",
    "reference_text": "【虚构规则，仅用于演示】星河学习支持金面向星河大学全日制本科二年级学生；平均绩点至少 3.0；未获得过同类学习支持金。三个条件均满足才符合申请资格。申请材料为在读证明、成绩单、申请表。截止日期为 2026 年 11 月 20 日。",
    "rubric": "资格判断 40 分：符合申请资格，理由需包括三项条件；材料 35 分：在读证明、成绩单、申请表；截止日期 25 分：2026 年 11 月 20 日。仅为演示任务，不进入正式分析。",
    "slots": [{"key": "prior_award", "description": "申请人是否获得过同类学习支持金；只有判断申请资格时才需要", "question": "申请人以前获得过同类学习支持金吗？", "aliases": ["没领过", "未获得", "没有同类", "从未获得", "领过", "获得过", "拿过"]}],
    "keywords": ["支持金", "申请", "资格", "材料", "截止", "成绩", "奖学金"],
}


PILOT_TASK = {
    "id": "pilot-xinghe-learning-2026-v3",
    "title": "预测试任务：核验星河市数字研修资助申请",
    "instructions": (
        "请利用 AI 提供的资料，判断案例申请人能否申请 2026 年秋季数字研修资助。"
        "完成对话后，独立提交一份简短建议：给出资格判断与逐项依据、应准备的材料、有效截止时间与提交方式；"
        "指出资料之间的冲突及你采用的现行来源。无法确定之处请明确说明，不要编造。"
    ),
    "shared_context": (
        "案例申请人林澈，21 岁，星河市户籍，在星河大学全日制本科二年级，平均绩点 3.35。"
        "曾在 2025 年获得一次市级学科竞赛奖金。现在准备申请 2026 年秋季数字研修资助。"
    ),
    "participant_facts": (
        "你还掌握两项 AI 起初不知道的情况：按最近三个月的家庭收入证明计算，林澈家庭月人均可支配收入为 5,800 元；"
        "林澈从未获得过‘数字研修资助’，2025 年的竞赛奖金是一次性学科竞赛奖励。"
        "AI 若需要，你可以在对话中提供这些情况。请勿输入真实姓名、证件号或联系方式。"
    ),
    "reference_text": (
        "【全部为虚构实验资料；文件编号用于引用。资料包中故意包含过期或非权威信息。】\n"
        "A｜星河市教育事务中心《2025 年数字研修资助申请指南》，2025-08-20 发布。"
        "2025 年秋季申请人须为本市户籍、18—25 岁的全日制本科生，绩点至少 3.0；"
        "同类资助不得重复领取；材料为申请表、在读证明和成绩单；2025-11-20 24:00 前可邮件提交。"
        "文件首页标明‘仅适用于 2025 年秋季批次’。\n"
        "B｜星河市教育事务中心《2026 年秋季数字研修资助办法》，2026-08-28 发布，2026-09-01 生效。"
        "本办法替代 A，适用于 2026 年秋季批次。申请人须同时满足：本市户籍、申请截止日年龄 18—25 岁、"
        "全日制本科二或三年级、绩点不低于 3.2、家庭月人均可支配收入不高于 6,000 元、"
        "此前未获得过本项目‘数字研修资助’。竞赛奖金不属于本项目重复资助。"
        "第 4 条原载：2026-11-20 24:00 前通过官方申报平台提交，邮件不受理。"
        "第 5 条列明：申请表、当学期在读证明、盖章成绩单、家庭收入证明。\n"
        "C｜星河市教育事务中心《2026 年秋季批次时间更正公告》，2026-09-10 发布。"
        "本公告仅更正 B 第 4 条：官方申报平台的截止时点改为 2026-11-18 17:00；"
        "其他资格和材料条款继续适用。逾期或邮件提交不受理。\n"
        "D｜星河市教育事务中心《2026 年申请常见问题》，2026-09-12 发布。"
        "‘家庭月人均可支配收入’依据最近三个月的家庭收入证明计算；"
        "竞赛奖金不算此前获得本项目资助；上传材料为清晰 PDF。"
        "以 B 和 C 的正式文件为准，FAQ 不修改资格门槛或截止时点。\n"
        "E｜‘校园互助站’自媒体《今年还是 3.0 就能申》，2026-09-15 发布。"
        "文章沿用 A 的 3.0 门槛与 11 月 20 日邮件提交说法，未引用 B 或 C，非主管部门渠道。\n"
        "F｜某论坛帖子《问 AI 就行》，2026-09-18 发布。"
        "帖文声称‘凡拿过市级奖金者均不能申请’，无文件链接与发布机构。\n"
        "资料包没有真实申请网址。只可称‘官方申报平台’，不得编造链接。"
    ),
    "rubric": (
        "总分 100。资格结论与逐项应用 35 分：符合申请条件 5；本市户籍、21 岁、全日制本科二年级、"
        "绩点 3.35≥3.2 各 4；收入 5,800≤6,000 且属于最近三个月证明 7；未领过本项目且竞赛奖金不算重复 7。"
        "现行资料辨识 25 分：认定 A 仅适用 2025、E 和 F 非权威或失实 10；以 B 为 2026 资格和材料依据 8；"
        "以 C 更正 B 第 4 条、D 仅作解释 7。材料 15 分：申请表、当学期在读证明、盖章成绩单、"
        "家庭收入证明各 3；清晰 PDF 3。时间与渠道 15 分：2026-11-18 17:00 及官方申报平台各 6；"
        "指出邮件无效 3。证据边界 10 分：给出文件编号及对应依据 5；不编造网址、额外条件或材料 5。"
        "只按最终提交评分；对话表现另作过程指标。缺少关键个人事实时，只能给条件性结论，不得满分。"
    ),
    "slots": [
        {"key": "household_income", "description": "家庭月人均可支配收入是否不高于 6000 元；资格判断必需", "question": "申请人的家庭月人均可支配收入是多少？", "aliases": ["收入", "人均", "5800", "5,800"]},
        {"key": "prior_program_award", "description": "是否曾获得本项目数字研修资助；竞赛奖金不算同类", "question": "申请人以前获得过‘数字研修资助’吗？", "aliases": ["研修资助", "此前", "从未获得", "竞赛奖金"]},
    ],
    "keywords": ["资助", "申请", "资格", "材料", "截止", "收入", "文件", "平台"],
}


PILOT_TASK_V4 = {
    **PILOT_TASK,
    "id": "pilot-xinghe-learning-2026-v4",
    "instructions": (
        "请与 AI 逐项核查 2026 年秋季数字研修资助的现行规则、申请人情况和办理要求。"
        "AI 每轮只提供一项相关资料或提出一个澄清问题，不会替你作最终决定。"
        "完成核查后，从五个行动方案中选出你认为最合适的一项。"
    ),
    "rubric": (
        "机器评分版本 decision-v1：根据最终行动方案固定计分。完整且按现行规则及时提交 100；"
        "及时提交但漏必需收入证明 75；对已可判定的条件过度等待 50；沿用原截止时间 20；"
        "因竞赛奖金误判为不得申请 0。选项在受试者端随机排列，分值和排序不显示。"
        "对话轮数、涉及的资料主题和异常标记用于过程分析与有效性复核，不改动机器分。"
    ),
    "slots": [
        {"key": "household_income", "description": "申请人的家庭月人均可支配收入", "question": "申请人的家庭月人均可支配收入是多少？", "aliases": ["5800", "5,800", "五千八"]},
        {"key": "prior_program_award", "description": "申请人此前是否获得过本项目数字研修资助", "question": "申请人以前获得过本项目‘数字研修资助’吗？", "aliases": ["没领过", "未领过", "从未获得", "没有获得", "已领过", "曾获得过数字研修"]},
    ],
    "evidence_cards": [
        {"id": "A_SCOPE", "title": "2025 年旧指南的适用范围", "body": "文件 A 由星河市教育事务中心发布，只适用于 2025 年秋季批次。其中的 3.0 绩点门槛和邮件提交方式不能直接用于 2026 年秋季申请。", "keywords": ["2025", "旧版", "旧指南", "文件A", "邮件", "3.0"]},
        {"id": "B_SCOPE", "title": "2026 年办法的效力", "body": "文件 B 是星河市教育事务中心发布的《2026 年秋季数字研修资助办法》，2026 年 9 月 1 日生效，替代文件 A，适用于本次申请。", "keywords": ["2026", "现行", "适用", "版本", "权威", "依据", "文件B"]},
        {"id": "B_BASIC", "title": "户籍、年龄、学籍与绩点", "body": "文件 B 规定：申请人须为本市户籍，申请截止日年龄 18—25 岁，全日制本科二或三年级，绩点不低于 3.2。", "keywords": ["资格", "条件", "户籍", "年龄", "年级", "绩点", "学籍"]},
        {"id": "B_INCOME", "title": "家庭收入门槛", "body": "文件 B 规定：家庭月人均可支配收入不得高于 6,000 元。这是本次申请的一项必要条件。", "keywords": ["收入", "家庭", "人均", "6000", "6,000", "经济"]},
        {"id": "B_REPEAT", "title": "重复资助与竞赛奖金", "body": "文件 B 规定：此前不得获得过本项目‘数字研修资助’；一次性学科竞赛奖金不属于本项目的重复资助。", "keywords": ["重复", "资助", "竞赛", "奖金", "领过", "获得过"]},
        {"id": "B_MATERIALS", "title": "正式申请材料", "body": "文件 B 第 5 条列明四项材料：申请表、当学期在读证明、盖章成绩单、家庭收入证明。", "keywords": ["材料", "申请表", "在读", "成绩单", "收入证明", "准备"]},
        {"id": "B_OLD_TIME", "title": "办法原载的办理时点", "body": "文件 B 第 4 条原载：2026 年 11 月 20 日 24:00 前通过官方申报平台提交，邮件不受理。该条随后受到文件 C 的更正。", "keywords": ["原载", "原定", "原文", "11月20", "11 月 20", "文件B第4条"]},
        {"id": "C_TIME", "title": "截止时间更正", "body": "文件 C 是主管部门于 2026 年 9 月 10 日发布的更正公告，仅更正文件 B 第 4 条：官方申报平台的截止时点改为 2026 年 11 月 18 日 17:00；逾期或邮件提交不受理。", "keywords": ["截止", "时间", "最新", "更正", "11月18", "11 月 18", "平台", "提交"]},
        {"id": "D_FAQ", "title": "收入证明与文件格式说明", "body": "主管部门文件 D 解释：家庭月人均可支配收入依据最近三个月的家庭收入证明计算；上传材料应为清晰 PDF。D 仅作解释，不修改 B 的资格门槛或 C 的截止时点。", "keywords": ["最近三个月", "计算", "证明", "PDF", "格式", "FAQ"]},
        {"id": "E_MEDIA", "title": "自媒体说法的来源", "body": "文件 E 是非主管部门的校园自媒体文章，沿用了 2025 年旧指南的 3.0 门槛和邮件提交说法，未引用现行办法或更正公告。", "keywords": ["自媒体", "校园互助", "3.0", "邮件", "来源", "可信"]},
        {"id": "F_FORUM", "title": "论坛说法的来源", "body": "文件 F 是论坛帖子，声称拿过市级奖金就不能申请，但没有文件链接或发布机构，不能代替主管部门的正式规则。", "keywords": ["论坛", "传闻", "市级奖金", "不能申请", "可信", "来源"]},
    ],
    "decision_options": [
        {"id": "complete_application", "text": "在 11 月 18 日 17:00 前通过官方申报平台提交申请表、当学期在读证明、盖章成绩单和最近三个月的家庭收入证明。", "score": 100},
        {"id": "omit_income_document", "text": "在 11 月 18 日 17:00 前通过官方申报平台提交申请表、当学期在读证明和盖章成绩单；家庭收入证明待平台要求时再补。", "score": 75},
        {"id": "wait_for_repeat_check", "text": "先询问主管部门竞赛奖金是否属于重复资助，得到答复后再决定是否准备材料和提交申请。", "score": 50},
        {"id": "old_deadline", "text": "在 11 月 20 日 24:00 前通过官方申报平台提交申请表、当学期在读证明、盖章成绩单和家庭收入证明。", "score": 20},
        {"id": "reject_due_to_prize", "text": "因申请人曾获得市级竞赛奖金，本次不准备材料，也不提交数字研修资助申请。", "score": 0},
    ],
}


_V5_EVIDENCE_BODIES = {
    "A_SCOPE": "星河市教育事务中心《2025 年数字研修资助申请指南》（2025 年 8 月 20 日发布）只适用于 2025 年秋季批次。它写的绩点 3.0 和邮件提交方式不能用于 2026 年秋季申请。",
    "B_SCOPE": "星河市教育事务中心《2026 年秋季数字研修资助办法》（2026 年 8 月 28 日发布，9 月 1 日生效）适用于本次 2026 年秋季申请。资格和材料应按这份现行办法核查。",
    "B_BASIC": "星河市教育事务中心《2026 年秋季数字研修资助办法》规定：申请人须为本市户籍，申请截止日年龄 18—25 岁，全日制本科二或三年级，绩点不低于 3.2。",
    "B_INCOME": "星河市教育事务中心《2026 年秋季数字研修资助办法》规定：家庭月人均可支配收入不得高于 6,000 元。这是本次申请的一项必要条件。",
    "B_REPEAT": "星河市教育事务中心《2026 年秋季数字研修资助办法》规定：此前不得获得过本项目‘数字研修资助’；一次性学科竞赛奖金不属于本项目的重复资助。",
    "B_MATERIALS": "星河市教育事务中心《2026 年秋季数字研修资助办法》第 5 条列明四项申请材料：申请表、当学期在读证明、盖章成绩单、家庭收入证明。",
    "B_OLD_TIME": "星河市教育事务中心《2026 年秋季数字研修资助办法》原文第 4 条写的是 2026 年 11 月 20 日 24:00 前通过官方申报平台提交，邮件不受理。主管部门后来发布了时间更正公告，办理前还需核对更正后的截止时点。",
    "C_TIME": "星河市教育事务中心《2026 年秋季批次时间更正公告》（2026 年 9 月 10 日发布）将本次官方申报平台的截止时间更正为 2026 年 11 月 18 日 17:00；逾期或邮件提交不受理。",
    "D_FAQ": "星河市教育事务中心《2026 年申请常见问题》（2026 年 9 月 12 日发布）说明：家庭月人均可支配收入按最近三个月的家庭收入证明计算；上传材料应为清晰 PDF。这份问答不改变正式办法中的资格门槛或更正公告中的截止时间。",
    "E_MEDIA": "《今年还是 3.0 就能申》出自非主管部门的校园自媒体，沿用了 2025 年旧指南的绩点 3.0 门槛和邮件提交说法，未引用 2026 年现行办法或更正公告，不能单独作为本次申请依据。",
    "F_FORUM": "某论坛帖子《问 AI 就行》声称拿过市级奖金就不能申请，但没有给出主管部门文件或发布机构，不能代替正式规则。",
}

PILOT_TASK_V5 = {
    **PILOT_TASK_V4,
    "id": "pilot-xinghe-learning-2026-v5",
    "evidence_cards": [
        {**card, "body": _V5_EVIDENCE_BODIES[card["id"]]}
        for card in PILOT_TASK_V4["evidence_cards"]
    ],
}


def seed_demo(path, mode: str = "demo") -> None:
    with connect(path) as db:
        db.execute(
            """INSERT OR IGNORE INTO tasks
            (id,version,title,instructions,shared_context,participant_facts,reference_text,rubric,slots_json,keywords_json,is_demo,active,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (DEMO_TASK["id"], 1, DEMO_TASK["title"], DEMO_TASK["instructions"],
             DEMO_TASK["shared_context"], DEMO_TASK["participant_facts"],
             DEMO_TASK["reference_text"], DEMO_TASK["rubric"],
             json.dumps(DEMO_TASK["slots"], ensure_ascii=False),
             json.dumps(DEMO_TASK["keywords"], ensure_ascii=False), 1, 1, now_utc()),
        )
        db.execute("UPDATE tasks SET age_band='18-24' WHERE id=?", (DEMO_TASK["id"],))
        if mode == "pilot":
            if db.execute("SELECT 1 FROM tasks WHERE id=?", (PILOT_TASK_V5["id"],)).fetchone() is None:
                db.execute(
                    """INSERT INTO tasks
                    (id,version,title,instructions,shared_context,participant_facts,reference_text,rubric,
                     evidence_json,decision_json,slots_json,keywords_json,is_demo,active,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (PILOT_TASK_V5["id"], 5, PILOT_TASK_V5["title"], PILOT_TASK_V5["instructions"],
                     PILOT_TASK_V5["shared_context"], PILOT_TASK_V5["participant_facts"],
                     PILOT_TASK_V5["reference_text"], PILOT_TASK_V5["rubric"],
                     json.dumps(PILOT_TASK_V5["evidence_cards"], ensure_ascii=False),
                     json.dumps(PILOT_TASK_V5["decision_options"], ensure_ascii=False),
                     json.dumps(PILOT_TASK_V5["slots"], ensure_ascii=False),
                     json.dumps(PILOT_TASK_V5["keywords"], ensure_ascii=False), 1, 0, now_utc()),
                )
            db.execute("UPDATE tasks SET age_band='18-24' WHERE id=?", (PILOT_TASK_V5["id"],))
        else:
            db.execute("UPDATE tasks SET active=1 WHERE id=?", (DEMO_TASK["id"],))
        db.execute("UPDATE tasks SET active=0 WHERE id IN (?,?)", (DEMO_TASK["id"], PILOT_TASK_V5["id"]))
        # Replace only bundled predecessors; researcher-created tasks stay active.
        replacing_bands = set()
        for age_band in AGE_BANDS:
            for version in (1, 2, 3):
                if db.execute("SELECT 1 FROM tasks WHERE id=? AND is_demo=1 AND active=1",
                              (f"pilot-age-{age_band}-v{version}",)).fetchone():
                    replacing_bands.add(age_band)
                db.execute("UPDATE tasks SET active=0 WHERE id=? AND is_demo=1",
                           (f"pilot-age-{age_band}-v{version}",))
        for age_band in AGE_BANDS:
            task = task_for(age_band)
            already_active = db.execute(
                "SELECT 1 FROM tasks WHERE age_band=? AND is_demo=1 AND active=1 LIMIT 1",
                (age_band,),
            ).fetchone() is not None
            db.execute(
                """INSERT OR IGNORE INTO tasks
                (id,version,title,instructions,shared_context,participant_facts,reference_text,rubric,
                 evidence_json,decision_json,slots_json,keywords_json,age_band,is_demo,active,created_at,scenario_variants_json,execution_options_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task["id"], 4, task["title"], task["instructions"], task["shared_context"],
                 task["participant_facts"], task["reference_text"], task["rubric"],
                 json.dumps(task["evidence_cards"], ensure_ascii=False),
                 json.dumps(task["decision_options"], ensure_ascii=False),
                 json.dumps(task["slots"], ensure_ascii=False),
                 json.dumps(task["keywords"], ensure_ascii=False), age_band, 1,
                 int(not already_active), now_utc(),
                 json.dumps(task.get("scenario_variants", []), ensure_ascii=False),
                 json.dumps(task.get("execution_options", []), ensure_ascii=False)),
            )
            if age_band in replacing_bands and not already_active:
                db.execute("UPDATE tasks SET active=1 WHERE id=? AND is_demo=1", (task["id"],))
