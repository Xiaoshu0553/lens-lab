"""Natural-language UI fixtures with an auditable, fixed historical timebase.

These are authored test conversations, not participant observations or model
calls. Source rows and instrument scores are never modified by this module.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime, timedelta

from .age_forms import form_for
from .synthetic_exports import BATCH_ID


RENDER_VERSION = "showcase-natural-v2"
TIME_SHIFT_DAYS = -1183
TIME_SHIFT_SECONDS = TIME_SHIFT_DAYS * 86400
TIME_SHIFT = {"days": TIME_SHIFT_DAYS, "seconds": TIME_SHIFT_SECONDS,
              "original_timebase": "2030-01-01 UTC", "rendered_timebase": "2026-10-06 UTC"}

TOPIC_ROUTES = (
    ("B_SCOPE", "B_BASIC", "B_REPEAT", "B_SCHEDULE", "B_MATERIALS", "C_TIME", "D_FAQ", "B_OLD_TIME", "A_SCOPE", "E_MEDIA", "F_FORUM"),
    ("C_TIME", "B_MATERIALS", "B_SCOPE", "B_REPEAT", "B_SCHEDULE", "B_BASIC", "D_FAQ", "B_OLD_TIME", "A_SCOPE", "E_MEDIA", "F_FORUM"),
    ("B_SCHEDULE", "B_REPEAT", "B_BASIC", "B_SCOPE", "B_MATERIALS", "C_TIME", "D_FAQ", "A_SCOPE", "B_OLD_TIME", "F_FORUM", "E_MEDIA"),
    ("B_MATERIALS", "C_TIME", "D_FAQ", "B_REPEAT", "B_SCHEDULE", "B_SCOPE", "B_BASIC", "A_SCOPE", "B_OLD_TIME", "E_MEDIA", "F_FORUM"),
    ("B_BASIC", "B_REPEAT", "B_SCHEDULE", "B_SCOPE", "B_MATERIALS", "C_TIME", "D_FAQ", "F_FORUM", "A_SCOPE", "E_MEDIA", "B_OLD_TIME"),
    ("E_MEDIA", "A_SCOPE", "B_SCOPE", "B_OLD_TIME", "C_TIME", "B_REPEAT", "B_SCHEDULE", "B_BASIC", "B_MATERIALS", "D_FAQ", "F_FORUM"),
    ("F_FORUM", "B_REPEAT", "B_BASIC", "B_SCHEDULE", "B_SCOPE", "B_MATERIALS", "C_TIME", "D_FAQ", "A_SCOPE", "E_MEDIA", "B_OLD_TIME"),
)

# Short, ordinary questions about each source's actual subject. Wording varies
# by conversation, never by age, education, or assumptions about ability.
QUESTIONS = {
    "A_SCOPE": ("去年那份说明今年还能用吗", "我看到一份2025年的指南，今年还能照着办吗", "先确认一下，旧版是不是不能直接用了", "有两个年份的介绍，我该先看哪一份"),
    "B_SCOPE": ("今年以哪份通知为准", "这次活动用哪份正式办法，还有些什么要知道的", "今年活动具体做什么，需要自己准备材料吗", "那本次报名应该看哪份文件"),
    "B_BASIC": ("报名有什么基本条件", "身份和年龄要满足什么要求", "登记甲和登记乙，基本条件是一样的吗", "我再问下，谁可以参加这次活动"),
    "B_SCHEDULE": ("现场和弹性安排，一周各要来几次", "两种安排的到场次数差在哪，我想先弄清楚", "弹性安排除了到场，还要做什么", "如果想多参加现场交流，这两种安排有什么区别"),
    "B_REPEAT": ("登记甲和乙怎么分", "以前拿没拿过这个项目名额，会影响登记类别吗", "参加过其他项目，也算这里说的拿过名额吗", "这两个登记类别到底按什么来选"),
    "B_MATERIALS": ("报名要交哪些材料", "参加计划也得交吗，还是只填报名表就行", "最后要准备哪几张表，我怕漏了", "类别和安排都要自己确认后写进表里吗"),
    "B_OLD_TIME": ("原办法写的是几号截止", "我想对一下，2026年原文里的截止时间是哪天", "原通知写的时间和提交方式是什么", "先看原来的说法，报名原定到什么时候"),
    "C_TIME": ("最后到底哪天截止", "后来有更正吗，我怕把截止时间记错了", "材料是交到服务点，还是发邮件", "再确认下，最新截止时间和交材料的地方是什么"),
    "D_FAQ": ("表格不会填，现场能帮忙吗", "没有手机或者账号，会不会就办不了", "表格能用纸质的吗，我想看看有没有人能协助填写", "要是填表拿不准，能找工作人员帮忙吗"),
    "E_MEDIA": ("转发文章还说邮件报名，这个可靠吗", "网上转发那篇和正式办法不一样，该按哪份来", "那篇转发文用的是哪年的消息", "我看到转发里还是旧安排，是不是不能照着做"),
    "F_FORUM": ("帖子说以前拿过名额就不能来，是真的吗", "论坛那个说法有正式出处吗", "有人说参加过本项目的人都不能报，和登记乙怎么对得上", "这个帖子和正式规则好像不一样，需要信它吗"),
}
RECHECKS = {
    "A_SCOPE": ("所以去年那份不能直接套到今年，对吧", "我怕看混了，再确认下旧指南的适用年份"),
    "B_SCOPE": ("我再确认一下，这次是按2026年的正式办法来吧", "现行文件是哪一份，麻烦再给我看一下"),
    "B_BASIC": ("刚才那条基本条件能再看一遍吗", "我把身份和年龄要求再对一下"),
    "B_SCHEDULE": ("两种安排的每周次数，再帮我对一下", "我再看看现场和弹性安排，怕把次数记反了"),
    "B_REPEAT": ("甲和乙的区别能再说一遍吗", "我想再核对一次，登记类别是按本项目名额来分吧"),
    "B_MATERIALS": ("我再对下材料，报名表和参加计划都要交吧", "刚才那两项材料能再列一下吗"),
    "B_OLD_TIME": ("原定时间我想再对一遍", "再看下原通知，免得和后来的更正混了"),
    "C_TIME": ("最终时间和交材料的地方，再确认一次", "我怕记错，最新的更正那条能再看下吗"),
    "D_FAQ": ("填表拿不准的地方，工作人员可以协助对吧", "纸质表单和现场协助的说明能再看一下吗"),
    "E_MEDIA": ("所以转发文引用的是旧资料，不是今年的依据吧", "那篇转发和正式文件的区别，再帮我核对一次"),
    "F_FORUM": ("帖子说不能再参加，但正式办法有登记乙，是这样吧", "我想再确认，论坛那句不能代替正式规则吧"),
}


def _pick(row, key, choices):
    digest = hashlib.sha256(f"{RENDER_VERSION}:{row['id']}:{key}".encode()).digest()
    return choices[int.from_bytes(digest[:8], "big") % len(choices)]


def adapted_row(source: dict) -> dict:
    """Deep-copy a source row and shift every session time by one fixed delta.

    Repeated adaptation is idempotent. The caller must retain the original row
    in its provenance field; this function never edits it or nested source JSON.
    """
    row = copy.deepcopy(source)
    if row.get("fixture_render_version") == RENDER_VERSION:
        if row.get("fixture_time_shift_days") != TIME_SHIFT_DAYS:
            raise ValueError("Fixture rendering has an inconsistent time shift")
        return row
    for key in ("created_at", "decision_started_at", "ended_at", "reviewed_at"):
        if row.get(key) is None:
            continue
        timestamp = datetime.fromisoformat(row[key])
        if timestamp.tzinfo is None:
            raise ValueError("Fixture source times must contain a timezone")
        row[key] = (timestamp + timedelta(days=TIME_SHIFT_DAYS)).isoformat()
    row["fixture_render_version"] = RENDER_VERSION
    row["fixture_time_shift_days"] = TIME_SHIFT_DAYS
    return row


def _user_style(row):
    profile = row.get("sim_response_profile", "正常")
    marks = ("？", "？", "?") if profile == "认真" else ("", "？", "?") if profile == "随意" else ("？", "", "？", "?")
    return {"profile": profile, "question_mark": _pick(row, "punctuation", marks),
            "hesitant": _pick(row, "hesitation", (False, False, False, True)),
            "polite": _pick(row, "polite", (False, False, False, True))}


def _ordered_cards(row, cards):
    """A stable individual topic route; scores and latent profiles are not read."""
    by_id = {card["id"]: card for card in cards}
    if len(by_id) != len(cards):
        raise ValueError("Fixture card IDs must be unique")
    route = _pick(row, "topic-route", TOPIC_ROUTES)
    ordered = [by_id[key] for key in route if key in by_id]
    selected_ids = {card["id"] for card in ordered}
    ordered.extend(sorted((card for card in cards if card["id"] not in selected_ids),
                          key=lambda card: hashlib.sha256(f"{row['id']}:{card['id']}".encode()).digest()))
    return ordered


def _question(text, style):
    return text.rstrip("。？！?! ") + style["question_mark"]


def _case_values(row, snapshot):
    scenario = row.get("scenario_id") or snapshot.get("scenario_id")
    values = {"first_3": (3, False), "first_1": (1, False), "return_3": (3, True), "return_1": (1, True)}
    if scenario not in values:
        raise ValueError("Unknown fixture scenario")
    return values[scenario]


def _answer_slot(row, snapshot, slot, style):
    weekly, prior = _case_values(row, snapshot)
    casual = style["profile"] == "随意"
    if slot["key"] == "weekly_availability":
        return f"最多{weekly}次" if casual else f"他每周最多能来{weekly}次。" if style["profile"] == "认真" else f"一周最多{weekly}次。"
    if slot["key"] == "prior_project_place":
        if prior:
            return "这个项目以前拿过" if casual else "他以前拿过这个项目的活动名额。"
        return "这个项目没拿过" if casual else "这个项目的名额他以前没拿过。"
    raise ValueError("Unsupported fixture clarification slot")


def _clarification(row, slot, index):
    choices = {"weekly_availability": ("陈禾每周最多能到场几次？", "先确认一下，他一周最多能来几次？"),
               "prior_project_place": ("他以前拿过这个项目的活动名额吗？", "还有一项，他以前获得过本项目名额吗？")}
    return _pick(row, f"clarification-{index}", choices.get(slot["key"], (slot["question"],)))


def _source_question(row, card, index, repeat, style):
    if repeat:
        choices = RECHECKS.get(card["id"], ("刚才这条规定能再看一遍吗",))
    else:
        pool = QUESTIONS.get(card["id"], ("这条规定具体是怎么说的",))
        # A conversation keeps its short/full style; it does not switch register
        # on each turn. Every profile can still hesitate or ask a careful check.
        choices = pool[:2] if style["profile"] == "随意" else pool[1:] if style["profile"] == "认真" else pool
    text = _pick(row, f"card-{card['id']}-turn-{index}", choices)
    if index == row["clarification_count"] and not text.startswith(("先", "再", "那", "我", "麻烦", "等", "嗯")):
        if style["hesitant"]:
            text = "我有点拿不准，" + text
        elif style["polite"]:
            text = "麻烦问下，" + text
    return _question(text, style)


def _guidance_turn(row, index, style):
    profile = style["profile"]
    if index == 0:
        options = ("我想帮陈禾选一下报名和参加方案，先从哪看起", "这几种方案我还没分清，怎么开始核对")
        return _question(_pick(row, "opening-guidance", options), style)
    options = (("我先把登记类别和参加安排分开对一下。", "我再看看案例，免得把两项选择弄反。", "我想想，先按核对到的规则分别选择。")
               if profile == "认真" else ("好 我先看看", "嗯 先选吧", "行 我再看一眼", "我想一下")
               if profile == "随意" else ("好，那我先对一下两项选择。", "等下，我再看一眼。", "嗯，我想想。", "知道了，我先选。"))
    text = options[(int.from_bytes(hashlib.sha256(row["id"].encode()).digest()[:2], "big") + index) % len(options)]
    return text if style["question_mark"] else text.rstrip("。")


def fixture_payloads(source, snapshot, strategy):
    """Return message/event tuples matching the original fixture seed's shape.

    Counts and source bodies are preserved. Topic order is authored per person
    because the original simulation contains no observed card order. A last-turn clarification stays
    unanswered when no user turn remains; no reply or fact is invented for it.
    """
    if strategy not in {"module", "prompt"}:
        raise ValueError("Unknown clarification strategy")
    row = adapted_row(source)
    turns, clarifications, reveals, distinct = (row[key] for key in
        ("user_turns", "clarification_count", "evidence_reveals", "distinct_evidence_cards"))
    cards, slots = _ordered_cards(row, snapshot["evidence_cards"]), snapshot["slots"]
    if (any(type(value) is not int or value < 0 for value in (turns, clarifications, reveals, distinct))
            or clarifications > min(len(slots), turns) or clarifications + reveals > turns
            or distinct > min(reveals, len(cards)) or (reveals > 0 and distinct == 0)
            or (reveals == 0 and distinct != 0)):
        raise ValueError("Inconsistent fixture counts")
    start = datetime.fromisoformat(row["created_at"])
    stop = (datetime.fromisoformat(row["decision_started_at"]) if row.get("decision_started_at")
            else start + timedelta(seconds=max(30, turns * 30)))
    span = (stop - start).total_seconds()
    if span < 0:
        raise ValueError("Fixture decision time precedes the start")
    style = _user_style(row)
    detail_base = {"is_synthetic": 1, "source": "showcase_fixture", "batch_id": BATCH_ID,
                   "conversation_source": "authored_fixture_not_model_response", "timestamp_is_simulated": 1,
                   "render_version": RENDER_VERSION, "time_shift": dict(TIME_SHIFT)}
    messages, events = [], []
    for index in range(turns):
        user_at = (start + timedelta(seconds=span * (2 * index + 1) / (2 * turns + 1))).isoformat(timespec="seconds")
        reply_at = (start + timedelta(seconds=span * (2 * index + 2) / (2 * turns + 1))).isoformat(timespec="seconds")
        prefix = (_answer_slot(row, snapshot, slots[index - 1], style)
                  if 0 < index <= clarifications else "")
        if index < clarifications:
            if prefix:
                question = prefix
            else:
                openings = (("我想把报名和参加安排弄清楚，能先帮我看看吗", "我有点拿不准，想按陈禾的情况核对一下")
                            if style["profile"] == "认真" else ("帮我看看报名怎么选", "这个活动我该怎么选")
                            if style["profile"] == "随意" else ("我想帮他选参加方案，先看哪些条件", "能帮我看看陈禾怎么报名吗", "我有点没分清这几种安排"))
                question = _question(_pick(row, "opening", openings), style)
            slot = slots[index]
            reply, action = _clarification(row, slot, index), f"{strategy}_clarification"
            events.append((row["id"], "clarification_decision", _json({**detail_base, "slot_key": slot["key"],
                "need_clarification": True, "answer_present_in_later_user_message": index + 1 < turns}), reply_at))
        elif index - clarifications < reveals:
            card_index = index - clarifications
            card = cards[card_index % distinct]
            question = _source_question(row, card, index, card_index >= distinct, style)
            if prefix:
                question = prefix.rstrip("。") + ("。" if style["question_mark"] else "，") + question
            introduction = _pick(row, f"evidence-introduction-{card['id']}-{index}",
                                 ("", "我查到的原文是：", "相关资料写的是：", "这里的规定是："))
            reply, action = (f"{introduction}\n{card['body']}" if introduction else card["body"]), "evidence"
            events.append((row["id"], "evidence_reveal", _json({**detail_base, "card_id": card["id"]}), reply_at))
        else:
            question = _guidance_turn(row, index, style)
            if prefix:
                question = prefix.rstrip("。") + ("。" if style["question_mark"] else "，") + question
            reply, action = _pick(row, f"guidance-{index}", ("好，按案例情况和刚才核对的规则，分别作出两项选择。", "可以，再把登记类别、参加安排和办理要求对一下。", "你可以先核对案例和已有资料，再独立选择。")), "task_guidance"
        messages.extend([(row["id"], "user", question, "fixture_user_message", user_at),
                         (row["id"], "assistant", reply, action, reply_at)])
        events.append((row["id"], "showcase_fixture_turn", _json({**detail_base, "turn": index + 1}), reply_at))
    return messages, events


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _default_submission(row, snapshot):
    main = next((option for option in snapshot["decision_options"] if option["id"] == row["decision_choice_id"]), None)
    execution = next((option for option in snapshot["execution_options"] if option["id"] == row["execution_choice_id"]), None)
    return f"参加方案：{main['text']}\n办理方案：{execution['text']}" if main and execution else ""


def derived_fixture_flags(source, snapshot, answers, *, messages=None, submission=None, strategy="module"):
    """Recompute QC from answer positions, source durations and actual messages.

    This helper is opt-in. It does not exclude samples or infer quality from the
    latent response profile; the pretest duration is explicitly a simulated one.
    """
    row = adapted_row(source)
    if messages is None:
        messages = fixture_payloads(row, snapshot, strategy)[0]
    users = [(message["content"] if isinstance(message, dict) else message[2]).strip()
             for message in messages if (message["role"] if isinstance(message, dict) else message[1]) == "user"]
    submission = _default_submission(row, snapshot) if submission is None else submission
    combined = " ".join(users + [submission.strip()])
    flags = []
    if not users:
        flags.append("no_user_message")
    if not submission.strip() and not row.get("decision_choice_id"):
        flags.append("empty_submission")
    if row.get("decision_choice_id") and row["distinct_evidence_cards"] == 0:
        flags.append("no_evidence_retrieved")
    if len("".join(users).strip()) < 12:
        flags.append("very_short_dialogue")
    if row.get("ended_at") and (datetime.fromisoformat(row["ended_at"]) - datetime.fromisoformat(row["created_at"])).total_seconds() < 45:
        flags.append("very_fast_completion")
    if len(users) >= 2 and len(set(users)) == 1:
        flags.append("repeated_user_messages")
    if len(combined) >= 10 and len(set(combined.replace(" ", ""))) <= 4:
        flags.append("low_character_diversity")
    if snapshot["keywords"] and not any(word in combined for word in snapshot["keywords"]):
        flags.append("low_keyword_overlap")
    _version, items = form_for(row["age_band"])
    if (not isinstance(answers, dict) or set(answers) != {item["id"] for item in items}
            or any(type(answers[item["id"]]) is not int or not 0 <= answers[item["id"]] < len(item["options"])
                   for item in items)):
        raise ValueError("QC requires the actual complete 30-item answer vector")
    longest = run = 0
    previous = None
    for item in items:
        position = answers[item["id"]]
        run = run + 1 if position == previous else 1
        longest = max(longest, run)
        previous = position
    duration = row.get("synthetic_pretest_time_seconds")
    if type(duration) in {int, float} and math.isfinite(duration) and 0 <= duration < 120:
        flags.append("pretest_fast_completion")
    if longest >= 12:
        flags.append("pretest_long_identical_run")
    return flags
