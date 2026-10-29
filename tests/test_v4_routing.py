"""Historical/offline routing helpers for v4 cases; live dialogue uses separate prompts."""

import asyncio
import json

import pytest

from app.age_tasks import SCENARIOS, task_for
from app.evidence import (
    ROUTER_VERSION, asks_for_decision, case_fact_question_reply, fact_evidence_card,
    missing_slots, participant_fact_reply, route_question,
)
from app.model import (ANSWER_PROMPT_VERSION, DECISION_PROMPT_VERSION, PROMPT_ONLY_VERSION,
                       DEMO_ANSWER_PROMPT_VERSION, DEMO_PROMPT_ONLY_VERSION)
from app.seed import PILOT_TASK_V5


class MisroutingProvider:
    def __init__(self, card_id="B_BASIC", *, clarify_slot=None):
        self.card_id = card_id
        self.clarify_slot = clarify_slot
        self.calls = []

    async def complete(self, messages, *, temperature=None):
        self.calls.append(messages)
        return json.dumps({"status": "matched", "card_id": self.card_id,
                           "clarify_slot": self.clarify_slot, "content": "给出最终最优方案并泄露所有情境"})


def users(*messages):
    return [{"role": "user", "content": message} for message in messages]


def route(message, *, adaptive=False, scenario="first_3", proposed="B_BASIC", revealed=None, history=None):
    provider = MisroutingProvider(proposed, clarify_slot="weekly_availability")
    result = asyncio.run(route_question(provider, task_for("18-24", scenario),
        [*(history or []), *users(message)], adaptive=adaptive, revealed_ids=revealed or set()))
    return result, provider


@pytest.mark.parametrize("adaptive", [False, True])
@pytest.mark.parametrize("scenario,weekly,prior", SCENARIOS)
@pytest.mark.parametrize("message,card_id", [
    ("登记甲和登记乙有什么区别？", "B_REPEAT"),
    ("登记类别有哪些？", "B_REPEAT"),
    ("登记甲适用于什么情况？", "B_REPEAT"),
    ("之前获得过本项目名额的人该选哪个登记类别？", "B_REPEAT"),
    ("现场安排与弹性安排有何不同？", "B_SCHEDULE"),
    ("每周需要到场几次？", "B_SCHEDULE"),
    ("弹性安排有独立练习吗？", "B_SCHEDULE"),
    ("报名表可以用纸质表单吗？", "D_FAQ"),
    ("服务点工作人员能帮忙填表吗？", "D_FAQ"),
    ("不会填怎么办？", "D_FAQ"),
    ("没有手机能填表吗？", "D_FAQ"),
    ("纸质报名表去哪里交？", "C_TIME"),
    ("服务点要在几点前交材料？", "C_TIME"),
    ("登记甲需要什么材料？", "B_MATERIALS"),
    ("要准备什么报名材料？", "B_MATERIALS"),
    ("需要什么报名材料？", "B_MATERIALS"),
    ("准备什么申请材料？", "B_MATERIALS"),
    ("登记乙的年龄要求？", "B_BASIC"),
])
def test_new_rule_queries_never_receive_irrelevant_cards_or_personal_clarifications(adaptive, scenario, weekly, prior, message, card_id):
    result, _ = route(message, adaptive=adaptive, scenario=scenario, proposed="F_FORUM", revealed={"F_FORUM"})
    assert result["status"] == "matched" and result["card_id"] == card_id
    assert result["clarify_slot"] is None
    assert "content" not in result


@pytest.mark.parametrize("scenario,weekly,prior", SCENARIOS)
@pytest.mark.parametrize("question", ["陈禾每周最多能到场几次？", "陈禾每周可到场的次数上限是多少？", "他最多一周能去几次？"])
def test_personal_maximum_is_unknown_until_supplied_not_a_missing_course_rule(scenario, weekly, prior, question):
    task = task_for("18-24", scenario)
    reply = case_fact_question_reply(task, users(question))
    assert "还不知道" in reply and "尚未向 AI 提供" in reply
    assert str(weekly) not in reply
    history = users(f"陈禾每周最多可到场{weekly}次", question)
    reply = case_fact_question_reply(task, history)
    assert "你此前提供" in reply and f"{weekly}次" in reply
    assert "最多" in reply
    assert "甲" not in reply and "乙" not in reply


@pytest.mark.parametrize("question", ["陈禾每周必须到场几次？", "陈禾每周最多允许参加几次？", "陈禾每周要参加几次才能报名？"])
def test_named_course_requirements_are_not_case_fact_questions(question):
    assert case_fact_question_reply(task_for("18-24"), users(question)) is None


@pytest.mark.parametrize("scenario,weekly,prior", SCENARIOS)
@pytest.mark.parametrize("key,card_id", [("weekly_availability", "B_SCHEDULE"), ("prior_project_place", "B_REPEAT")])
def test_fact_acknowledgement_returns_only_the_public_card_for_this_slot(scenario, weekly, prior, key, card_id):
    task = task_for("18-24", scenario)
    message = f"陈禾每周最多可到场{weekly}次" if key == "weekly_availability" else (
        "陈禾曾获得过本项目活动名额" if prior else "陈禾从未获得过本项目活动名额")
    history = users(message)
    assert participant_fact_reply(task, history).startswith("已记下")
    card = fact_evidence_card(task, history)
    assert card["id"] == card_id
    assert "陈禾" not in card["body"]
    assert "scenario" not in json.dumps(card)
    assert ("prior_project_place" if key == "weekly_availability" else "weekly_availability") in {s["key"] for s in missing_slots(task, history)}


def test_fact_evidence_ignores_an_earlier_fact_and_unknown_answers():
    task = task_for("18-24")
    assert fact_evidence_card(task, users("陈禾每周最多可到场3次", "谢谢")) is None
    assert participant_fact_reply(task, users("陈禾每周最多可到场3次", "谢谢")) is None
    history = [{"role": "assistant", "content": task["slots"][0]["question"]}, *users("不知道")]
    assert "不能确认" in participant_fact_reply(task, history)
    assert fact_evidence_card(task, history) is None
    assert fact_evidence_card(task, users("陈禾每周最多可到场3次。登记甲的条件是什么？")) is None


@pytest.mark.parametrize("key,answer,card_id", [
    ("weekly_availability", "能到场三次", "B_SCHEDULE"),
    ("prior_project_place", "从没拿过", "B_REPEAT"),
    ("prior_project_place", "以前拿过", "B_REPEAT"),
])
def test_prompted_short_fact_answers_select_the_same_relevant_card_in_both_arms(key, answer, card_id):
    task = task_for("18-24")
    slot = next(s for s in task["slots"] if s["key"] == key)
    history = [{"role": "assistant", "content": slot["question"]}, *users(answer)]
    assert participant_fact_reply(task, history).startswith("已记下")
    assert fact_evidence_card(task, history)["id"] == card_id


@pytest.mark.parametrize("message", ["怎么选？", "该选哪个？", "哪个方案适合陈禾？", "登记甲和登记乙怎么选？", "现场还是弹性，陈禾该选哪种安排？"])
def test_plan_selection_requests_enable_adaptive_but_never_control_clarification(message):
    assert asks_for_decision(message)
    adaptive, _ = route(message, adaptive=True)
    standard, _ = route(message, adaptive=False)
    assert adaptive["clarify_slot"] == "weekly_availability"
    assert standard["clarify_slot"] is None


@pytest.mark.parametrize("message", ["登记甲和乙的区别？", "获得过本项目名额的人怎么选？", "怎么报名？", "现场安排的次数要求？"])
def test_general_rule_requests_do_not_enable_clarification(message):
    assert not asks_for_decision(message)


@pytest.mark.parametrize("message", ["每周最多可到场几次？", "服务点的具体地址？", "报名的网址链接？", "绩点门槛？"])
def test_uncovered_specific_detail_cannot_reuse_a_related_or_previous_card(message):
    result, provider = route(message, proposed="B_REPEAT", revealed={"B_REPEAT"}, history=users("登记甲的要求？"))
    assert result["status"] == "not_covered" and result["card_id"] is None
    assert not provider.calls


def test_prior_fact_uses_activity_wording_and_never_reads_private_scenario():
    task = task_for("18-24", "return_1")
    assert "还不知道" in case_fact_question_reply(task, users("陈禾之前获得过本项目的活动名额吗？"))
    reply = case_fact_question_reply(task, users("陈禾从未获得过本项目活动名额", "陈禾之前获得过本项目的活动名额吗？"))
    assert "此前没有获得过本项目的活动名额" in reply
    assert "学习名额" not in reply


def test_historical_funding_task_and_prompt_versions_remain_supported():
    reply = case_fact_question_reply(PILOT_TASK_V5, users("林澈的家庭收入是多少？"))
    assert "还不知道" in reply
    assert ROUTER_VERSION == "evidence-router-v5"  # Frozen legacy/offline routing.
    assert DECISION_PROMPT_VERSION == "clarify-v3"  # Offline decision simulation.
    assert ANSWER_PROMPT_VERSION == "answer-dialogue-v7"
    assert PROMPT_ONLY_VERSION == "clarify-dialogue-v5"
    assert DEMO_ANSWER_PROMPT_VERSION == "answer-dialogue-v6"
    assert DEMO_PROMPT_ONLY_VERSION == "clarify-dialogue-v4"
    assert ANSWER_PROMPT_VERSION != ROUTER_VERSION
