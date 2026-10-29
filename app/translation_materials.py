"""Versioned, original candidate instruments for the translation study.

These items are a pilot instrument, not a validated translation proficiency test.
Self-reports are retained separately and never change the objective score or arm.
All participants receive the same source and the same accessible context cards.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib


PROJECT_TITLE = '《从“直接翻译”到“主动澄清”：面向翻译使用者的信息公平性研究与促进——基于生成式人工智能轻量化优化模块的设计与实证》——以中国矿业大学翻译用户为例'
DESIGN_VERSION = "translation-equity-baseline-v2"
FORM_VERSION = "translation-literacy-candidate-v1-18"
TASK_VERSION = "bell-house-en-zh-v1"
RUBRIC_VERSION = "translation-rubric-candidate-v1"
PROMPT_VERSION = "translation-dialogue-v2"
OPTIMIZER_VERSION = "translation-scaffold-v1"
MATERIAL_STATUS = "候选研究材料：问卷、能力阈值、任务及评分量规需经专家审查和预测试后冻结；软件与演示不构成效应证据。"

_BANK = [
    ("l01", "翻译素养", "委托只说‘把这段英文翻成中文’，没有说明给谁阅读。若读者身份可能影响用语，首先补充哪项最有用？", ["读者及使用目的", "译者的姓名", "把全文中的词都换成长词"], 0, "目标读者和使用目的直接影响表达选择。"),
    ("l02", "翻译素养", "你在翻译一部自创小说，词典中同一词有多个常见义项。怎样判断本处含义更稳妥？", ["总选词典第一义", "结合本句、前后文及作品设定", "挑中文最长的义项"], 1, "词典提供候选义，具体含义取决于语境。"),
    ("l03", "翻译素养", "AI给出了很流畅的译文，但原文有否定词，译文中没有。应优先做什么？", ["只检查标点", "流畅即可采用", "核对否定关系并纠正遗漏"], 2, "流畅不能代替忠实传达否定关系。"),
    ("l04", "翻译素养", "小说中一个称呼可能是真实爵位，也可能是朋友间的玩笑；现有文本无法确定。哪个做法更合适？", ["询问人物关系与称呼背景，无法获得时说明不确定性", "直接编造爵位家谱", "删除称呼，不再说明"], 0, "影响意义的背景缺失时，应核实而非编造。"),
    ("l05", "翻译素养", "同一人物姓名在一段译文中出现了三个不同中文译名，任务要求保持称谓一致。应怎样处理？", ["保留变化以显文采", "确定并统一该人物译名", "删去所有人名"], 1, "明确的一致性要求应贯穿译文。"),
    ("l06", "翻译素养", "使用在线AI协助翻译一份含手机号的访谈，手机号与翻译任务无关。哪种处理更合适？", ["先发送原始手机号让AI记住", "把全部访谈公开", "提交前去除无关身份信息"], 2, "任务无关的身份信息不需要发送到翻译服务。"),
    ("k01", "翻译常识", "原文：‘Not all the doors were locked.’ 哪句准确保留了数量和否定范围？", ["并非所有的门都锁着。", "所有的门都没锁。", "所有的门都锁着。"], 0, "not all表示并非全部，不等于全部没有。"),
    ("k02", "翻译常识", "给定词义说明：此处‘bank’指河岸，场景为两人坐在河边。‘They sat on the bank.’ 应选哪句？", ["他们坐在银行里。", "他们坐在河岸上。", "他们坐在银行卡上。"], 1, "题干已确定bank为河岸，不能改用金融义。"),
    ("k03", "翻译常识", "原文：‘She might return tomorrow.’ 哪个译文保留了说话人的不确定程度？", ["她明天一定回来。", "她明天绝不回来。", "她明天可能回来。"], 2, "might表达可能性，不是确定承诺或否定。"),
    ("k04", "翻译常识", "原文中的‘they’已被作者说明指一位不使用男性或女性称谓的人，任务要求避免强加性别。哪种做法适合？", ["视句法用该人物名字或省略代词，保持单数指向", "一律译成‘他们’并添加多个人物", "直接指定为男性"], 0, "英文单数they不应擅自改成人数或性别信息。"),
    ("k05", "翻译常识", "原文：‘Only Lin knew the code.’ 哪句保留了‘only’限定的人物？", ["林只知道密码，其他什么都不知道。", "只有林知道密码。", "林也许知道密码。"], 1, "only限定Lin，表示知情者只有林，而非知识范围。"),
    ("k06", "翻译常识", "题干约定：本段‘the Crown’借指王室机构，不指实物。‘The Crown refused.’ 哪个译法符合约定？", ["一顶王冠摇头拒绝了。", "珠宝商拒绝了。", "王室拒绝了。"], 2, "已有明确转喻设定，应采用机构义。"),
    ("j01", "译文判断", "委托要求：用自然中文保留‘雨停了’这一事实，不增加情节。原文‘The rain had stopped.’ 哪句更符合委托？", ["雨已经停了。", "雨已经停了，她终于等到了失散的父亲。", "天空永远不会再下雨。"], 0, "第二、三项添加了原文未提供的情节或绝对判断。"),
    ("j02", "译文判断", "委托要求：角色说话简短、克制，不改成抒情独白。原文‘Wait here,’ he said. 哪句最符合该要求？", ["他用一生的哀愁诉说着等待。", "‘在这儿等。’他说。", "‘无论天地如何变幻，我都将永恒等待。’他说。"], 1, "直接、简短的引语符合题干所给人物声音。"),
    ("j03", "译文判断", "小说原文故意没有说明箱内是什么，委托要求保留悬念。原文‘She opened the box and smiled.’ 哪句更符合要求？", ["她打开箱子，看见了金戒指，笑了。", "她打开装有遗嘱的箱子，笑了。", "她打开箱子，笑了。"], 2, "不得将未揭示的箱内物品补写为确定事实。"),
    ("j04", "译文判断", "委托给定中文人物名‘季宁’，要求文中统一。下面哪个修订理由最充分？", ["把同一人物偶尔出现的‘季凝’统一为‘季宁’", "每句换一个译名以增加变化", "删掉姓名会让作品自动更好"], 0, "此题评判明确的委托一致性，不把个人审美当作唯一答案。"),
    ("j05", "译文判断", "委托要求：保留原文的疑问和犹豫。原文‘Could this be the right door?’ 哪句更符合要求？", ["这肯定就是那扇门。", "会是这扇门吗？", "这扇门已经永远消失了。"], 1, "问句保留不确定性，其余选项改变语气或事实。"),
    ("j06", "译文判断", "原文只有‘He lowered his voice.’，没有说明原因；委托要求不添加心理动机。哪句更符合要求？", ["因为暗恋她，他压低了声音。", "他心怀杀意，压低了声音。", "他压低了声音。"], 2, "原文未给动机，译文不应擅自确定爱情或敌意。"),
]

def _make_item(entry: tuple) -> dict:
    key, domain, stem, options, correct, rationale = entry
    order = sorted(range(len(options)), key=lambda index: hashlib.sha256(
        f"{FORM_VERSION}:{key}:{index}".encode()).digest())
    return {"id": key, "domain": domain, "stem": stem,
            "options": [*(options[index] for index in order), "我暂时无法判断"],
            "correct": order.index(correct), "rationale": rationale}


FORM_ITEMS = [_make_item(entry) for entry in _BANK]

_SELF_OPTIONS = ["1 · 完全不符合", "2 · 不太符合", "3 · 一般", "4 · 比较符合", "5 · 非常符合"]
SELF_ITEMS = [
    {"id": key, "stem": stem, "options": list(_SELF_OPTIONS)}
    for key, stem in [
        ("s01", "我平时能够独立理解一个普通英语小说段落的主要意思。"),
        ("s02", "我能发现译文中遗漏、增添或误译的重要信息。"),
        ("s03", "我能根据读者、用途和人物设定调整中文表达。"),
        ("s04", "我能比较两份意思接近的译文，并具体说明表达差异。"),
        ("s05", "我知道如何向AI补充翻译所需的背景和要求。"),
        ("s06", "我经常使用AI辅助英译中任务。"),
    ]
]


def public_form() -> dict:
    return {
        "version": FORM_VERSION, "item_count": len(FORM_ITEMS),
        "items": [{k: deepcopy(v) for k, v in item.items() if k not in {"correct", "rationale"}}
                  for item in FORM_ITEMS],
        "self_items": deepcopy(SELF_ITEMS), "status": MATERIAL_STATUS,
        "scoring_note": "能力分组仅按客观题答对比例；自评单独保存。候选阈值：低于50%、50%至不足75%、75%及以上。",
    }


def _validate_answers(answers: dict, items: list[dict], label: str) -> None:
    if not isinstance(answers, dict) or set(answers) != {item["id"] for item in items}:
        raise ValueError(f"请完整填写{label}，且不要添加题目以外的字段")
    for item in items:
        value = answers[item["id"]]
        if type(value) is not int or not 0 <= value < len(item["options"]):
            raise ValueError(f"{label}中{item['id']}的选项无效")


def score_form(answers: dict, self_answers: dict) -> dict:
    _validate_answers(answers, FORM_ITEMS, "翻译能力客观题")
    _validate_answers(self_answers, SELF_ITEMS, "翻译使用自评")
    score = sum(answers[item["id"]] == item["correct"] for item in FORM_ITEMS)
    percent = round(score / len(FORM_ITEMS) * 100, 2)
    domain_scores = {}
    for domain in dict.fromkeys(item["domain"] for item in FORM_ITEMS):
        items = [item for item in FORM_ITEMS if item["domain"] == domain]
        correct = sum(answers[item["id"]] == item["correct"] for item in items)
        domain_scores[domain] = {"correct": correct, "total": len(items), "percent": round(correct / len(items) * 100, 2)}
    return {"score": score, "percent": percent,
            "band": "low" if percent < 50 else "medium" if percent < 75 else "high",
            "domain_scores": domain_scores,
            "self_report": {key: value + 1 for key, value in self_answers.items()},
            "self_report_mean": round(sum(value + 1 for value in self_answers.values()) / len(SELF_ITEMS), 2),
            "band_thresholds": {"medium_min_percent": 50, "high_min_percent": 75}}


TASK = {
    "id": TASK_VERSION,
    "title": "小说节选英译中 · 钟楼前的约定",
    "instructions": "你是为中文小说读者工作的翻译使用者。请与AI对话，将下列英语段落译成自然、准确的中文，最终提交你认可的一份完整译文。可自行查阅本页补充资料，并按需要把信息告诉AI。所有参与者使用同一原文和补充资料。",
    "source_text": 'When Mara reached Bell House, Rowan was waiting by the gate, holding the little silver fox. “Your lordship is early,” they said. Mara looked past them at the room where the light was still on. “Then we’d better not wake her.” Rowan turned the fox over in their palm. “It isn’t hers anymore.” For a moment, the old promise stood between them. Then the bell rang twice, and neither moved.',
    "brief": "自创架空小说节选；英译中，用于面向成年普通读者的中文小说选刊。保持第三人称叙述、对话简短克制，保留原文的悬念，不补写解释或后续情节。人名统一使用‘玛拉’和‘罗温’，Bell House统一为‘钟楼宅’。这些是研究用虚构材料，不依赖任何现实作品知识。",
    "context_cards": [
        {"id": "characters", "title": "作者资料 · 人物与指代", "body": "玛拉是一名女性。罗温是单个非二元性别人物；原文的they、them、their均指罗温，不指一群人。中文可使用‘罗温’或合乎语法的省略，避免擅自使用男性或女性代词。"},
        {"id": "nickname", "title": "作者资料 · 对话中的称呼", "body": "玛拉没有贵族身份。‘Your lordship’是罗温打趣玛拉的旧昵称，意在调侃她习惯发号施令；不是正式爵位。译文应传达带有熟悉感的调侃，不能补成真实贵族礼仪。"},
        {"id": "fox", "title": "作者资料 · 银狐物件", "body": "‘the little silver fox’是一枚狐狸形状的银制徽记，可放在掌心翻转，不是活着的狐狸。它原属于玛拉的母亲，当夜已经正式交给玛拉；本段的‘hers’指玛拉母亲。无需在译文中解释徽记的完整来历。"},
        {"id": "mother", "title": "前文摘要 · 亮灯的房间", "body": "钟楼宅那间仍亮着灯的房间里，玛拉的母亲正在睡觉。‘wake her’中的her指玛拉母亲。灯亮不表示房内的人醒着。"},
        {"id": "promise", "title": "前文摘要 · 旧约定", "body": "玛拉与罗温儿时曾约定一起离开这座城。这是共同出走的承诺，文本未设定两人有恋爱关系。‘the old promise stood between them’呈现约定带来的迟疑，不宜增写爱情、背叛或争吵。"},
        {"id": "bell", "title": "场景资料 · 两声钟响", "body": "宅外码头的钟连续响两下，表示当晚最后一班渡船即将离岸；不是门铃，也不是报两点的时钟。玛拉与罗温听见了离城的信号，却仍没有移动。原文没有直接解释钟声含义，译文可保留‘钟响了两声’。"},
    ],
    "reference_translation": "玛拉赶到钟楼宅时，罗温已在门边等着，手里握着那枚小小的银狐徽记。‘大人来得可真早。’罗温说。玛拉越过罗温，望向那间还亮着灯的屋子。‘那我们最好别吵醒她。’罗温把银狐徽记在掌心翻了个面。‘这已经不属于她了。’一时间，那个旧约定横在两人之间。接着，钟响了两声，两个人谁也没动。",
    "critical_points": [
        {"id": "singular_they", "description": "罗温为单个人物；不得误增多人或擅定性别。"},
        {"id": "silver_object", "description": "银狐是掌心中的银制徽记，不是活狐；‘枚/徽记’等表达可接受。"},
        {"id": "ironic_address", "description": "称呼具有熟人调侃意味，不得将玛拉设定为真实贵族；不强求唯一称呼。"},
        {"id": "pronouns", "description": "wake her及hers指玛拉母亲；可保留‘她’，无须补明姓名或身份，但不能改为玛拉或罗温。"},
        {"id": "possession", "description": "保留‘不再属于她’的否定和归属变化，不必在正文补出徽记已交给玛拉。"},
        {"id": "unresolved_promise", "description": "保留旧约定与迟疑；不得无据增写恋爱、背叛、决裂或确定心理动机。"},
        {"id": "bell_and_stillness", "description": "保留两声钟响、随后两人都未移动；不得改为门铃、两点报时或已经登船。"},
    ],
    "status": MATERIAL_STATUS,
}


def public_task(task: dict = TASK) -> dict:
    return {key: deepcopy(task[key]) for key in
            ("id", "title", "instructions", "source_text", "brief", "context_cards", "status")
            if key in task}
