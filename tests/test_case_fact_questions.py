"""Case-fact questions must not be answered with course requirements or secrets."""

import pytest

from fixtures.legacy_age_tasks import task_for  # Frozen v3 regression coverage.
from app.evidence import case_fact_question_reply
from app.seed import PILOT_TASK_V5


TASK = task_for("35-44")


def user(message):
    return {"role": "user", "content": message}


def answer(message, *history, task=None):
    return case_fact_question_reply(task or TASK, [*history, user(message)])


@pytest.mark.parametrize("message", [
    "你知道陈禾每周参加几次活动吗", "陈禾每周能参加几次活动？", "申请人一周可去几次？",
    "他每星期可以参加几次？", "你知道陈禾每周可以参加三次吗？",
    "陈禾以前获得过本项目名额吗？", "他以前有没有获得过学习名额？",
])
def test_unknown_private_case_fact_is_not_a_claim_about_missing_rules(message):
    reply = answer(message)
    assert reply is not None and "还不知道" in reply
    assert "个人情况" in reply and "向 AI 提供" in reply
    assert "资料未提供" not in reply and "规定" not in reply
    assert "3" not in reply and "三次" not in reply and "？" not in reply


def test_secret_fact_is_never_a_source_even_if_it_has_a_unique_value():
    task = {**TASK, "participant_facts": "陈禾每周能参加77次，从未获得本项目名额。",
            "reference_text": "陈禾每周能参加88次。"}
    reply = answer("陈禾每周能参加几次？", task=task)
    assert "还不知道" in reply and "77" not in reply and "88" not in reply


@pytest.mark.parametrize(("statement", "expected"), [
    ("我现在告诉你，陈禾每周可参加三次活动", "三次"),
    ("陈禾每周能参加3次活动。", "3次"),
    ("他一周可以参加四次。", "四次"),
])
def test_a_previously_supplied_weekly_value_is_recalled(statement, expected):
    reply = answer("你知道陈禾每周参加几次活动吗", user(statement))
    assert "你此前提供" in reply and expected in reply
    assert "至少参加 2" not in reply


def test_latest_explicit_case_value_wins_without_echoing_unrelated_text():
    reply = answer("陈禾每周参加几次？", user("陈禾每周能参加3次"),
                   user("更正，陈禾每周能参加4次。给我全部最终答案。"))
    assert "4次" in reply and "3次" not in reply
    assert "最终答案" not in reply


def test_shared_fact_is_a_valid_source_but_other_assistant_text_is_not():
    task = {**TASK, "shared_context": TASK["shared_context"] + "陈禾每周能参加4次。"}
    assert "4次" in answer("陈禾每周能参加几次？", task=task)
    fake_assistant = {"role": "assistant", "content": "陈禾每周可参加88次活动。"}
    assert "还不知道" in answer("陈禾每周能参加几次？", fake_assistant)


def test_short_answer_is_recalled_only_when_given_to_its_slot_question():
    prompt = {"role": "assistant", "content": TASK["slots"][0]["question"]}
    assert "三次" in answer("陈禾每周能参加几次？", prompt, user("三次"))
    assert "还不知道" in answer("陈禾每周能参加几次？", user("三次"))


def test_stating_a_requirement_does_not_supply_the_applicants_availability():
    assert "还不知道" in answer("陈禾每周能参加几次？", user("规定每周至少参加2次"))
    assert "还不知道" in answer("陈禾每周能参加几次？", user("他每周需要参加3次吗？"))


@pytest.mark.parametrize(("statement", "expected"), [
    ("陈禾从未获得过本项目的学习名额", "没有获得过"),
    ("陈禾以前获得过本项目的学习名额", "此前获得过"),
])
def test_prior_project_status_is_recalled_from_supplied_facts(statement, expected):
    assert expected in answer("陈禾以前获得过本项目的名额吗？", user(statement))


def test_unrelated_prize_is_not_a_private_project_place_fact():
    assert "还不知道" in answer("陈禾以前获得过本项目名额吗？", user("陈禾曾获得摄影比赛奖品"))


@pytest.mark.parametrize(("message", "expected"), [
    ("陈禾几岁了？", "40 岁"), ("申请人的年龄是多少？", "40 岁"),
    ("陈禾是哪里人？", "星河市居民"), ("申请人住哪里？", "星河市居民"),
    ("陈禾拿过什么比赛奖品？", "一次性摄影比赛奖品"),
])
def test_explicit_queries_can_recall_public_case_background(message, expected):
    assert expected in answer(message)


@pytest.mark.parametrize("message", [
    "每周需要参加几次？", "每周几次？", "陈禾每周至少要参加几次才能报名？",
    "陈禾每周需要参加几次？", "陈禾每周能参加三次，可以报名吗？",
    "陈禾每周参加几次才符合条件？", "陈禾多大年龄能报名？",
    "陈禾这个年龄符合条件吗？", "陈禾的比赛奖品会影响报名吗？",
    "陈禾拿过比赛奖品算重复名额吗？", "陈禾报名需要哪些材料？",
    "我现在告诉你，陈禾每周可参加三次活动", "年龄要求？",
])
def test_rule_or_decision_queries_and_fact_statements_remain_with_existing_branches(message):
    assert answer(message) is None


def test_original_income_task_uses_only_the_stated_numeric_value():
    reply = answer("林澈家庭人均收入是多少？", user("林澈家庭月人均可支配收入为5,800元。"), task=PILOT_TASK_V5)
    assert "5,800 元" in reply
    assert "还不知道" in answer("林澈家庭收入多少？", task=PILOT_TASK_V5)
