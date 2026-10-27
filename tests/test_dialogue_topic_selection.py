"""Resolve actual rule requests and answers to the assistant's own topic menu."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.evidence import broad_reply, overview_reply, route_question
from app.main import create_app
from app.model import DemoProvider
from app.student_materials import task_for
from test_flow import settings, start_payload


TASK = task_for("18-20")


def _route(message, *, history=()):
    return asyncio.run(route_question(
        DemoProvider(), TASK,
        [*history, {"role": "user", "content": message}],
        adaptive=False, revealed_ids=set(),
    ))


def _menu_history(menu):
    return [
        {"role": "user", "content": "怎么报名？"},
        {"role": "assistant", "content": menu},
    ]


@pytest.mark.parametrize("message", [
    "查证资格条件", "查询资格条件", "先查资格条件", "资格条件", "资格要求", "报名条件", "报名资格要求是什么？",
    "具体条件", "所有资格条件", "申请人的资格要求",
])
def test_named_eligibility_request_retrieves_the_actual_basic_rule(message):
    route = _route(message)
    assert route["status"] == "matched"
    assert route["card_id"] == "B_BASIC"
    assert route["clarify_slot"] is None


@pytest.mark.parametrize("menu", [overview_reply(), broad_reply()])
@pytest.mark.parametrize(("message", "card_id"), [
    ("第一个", "B_BASIC"), ("先看第一项", "B_BASIC"),
    ("第二个", "B_MATERIALS"), ("第三项", "C_TIME"),
])
def test_ordinal_answer_uses_the_assistants_explicit_topic_menu(menu, message, card_id):
    route = _route(message, history=_menu_history(menu))
    assert route["status"] == "matched" and route["card_id"] == card_id
    assert route["clarify_slot"] is None


@pytest.mark.parametrize("message", [
    "资格条件里的绩点要求是多少？", "申请条件里的英语门槛是什么？",
    "资格" + "需要" * 1000 + "英语",
])
def test_eligibility_word_does_not_replace_an_uncovered_specific_subject(message):
    route = _route(message)
    assert route["card_id"] is None
    assert route["status"] in {"not_covered", "unclear"}


@pytest.mark.parametrize(("message", "card_id"), [
    ("资格条件中的年龄要求？", "B_BASIC"),
    ("报名资格要求多少费用？", "B_SCHEDULE"),
    ("以前获得过本项目名额的登记条件？", "B_REPEAT"),
    ("查证报名材料", "B_MATERIALS"),
    ("资格条件里提到的截止时间？", "C_TIME"),
])
def test_explicit_current_subject_has_priority_over_previous_menu(message, card_id):
    route = _route(message, history=_menu_history(overview_reply()))
    assert route["status"] == "matched" and route["card_id"] == card_id


@pytest.mark.parametrize("history", [
    [],
    [{"role": "assistant", "content": "你好。你想先核查哪一方面？"}],
    [{"role": "user", "content": overview_reply()}],
    [*_menu_history(overview_reply()),
     {"role": "user", "content": "报名需要手机号码吗？"},
     {"role": "assistant", "content": "资料没有提供手机号码要求。"}],
])
def test_ordinal_without_a_current_assistant_topic_menu_is_not_guessed(history):
    route = _route("第一个", history=history)
    assert route["card_id"] is None


def test_out_of_range_topic_selection_does_not_select_an_arbitrary_card():
    route = _route("第八个", history=_menu_history(overview_reply()))
    assert route["card_id"] is None


@pytest.mark.parametrize("menu", [overview_reply(), broad_reply()])
@pytest.mark.parametrize(("selection", "card_id"), [
    ("第一个", "B_BASIC"), ("第二个", "B_MATERIALS"), ("第三项", "C_TIME"),
])
def test_elliptical_followups_retain_the_previously_selected_menu_subject(menu, selection, card_id):
    history = [*_menu_history(menu),
               {"role": "user", "content": selection},
               {"role": "assistant", "content": "已查到对应规定。"}]
    for followup in ("那具体呢", "再说一遍"):
        assert _route(followup, history=history)["card_id"] == card_id
        history += [{"role": "user", "content": followup},
                    {"role": "assistant", "content": "已查到对应规定。"}]
    history += [{"role": "user", "content": "英语条件？"},
                {"role": "assistant", "content": "资料没有提供这项规定。"}]
    assert _route("那具体呢", history=history)["card_id"] is None


@pytest.mark.parametrize("selection", ["第一个", "查证资格条件", "资格条件"])
def test_screenshot_flow_returns_the_rule_in_both_arms_without_looping(tmp_path, selection):
    app = create_app(settings(tmp_path))
    client = TestClient(app)
    starts = [client.post("/api/start", json=start_payload()).json() for _ in range(2)]
    assert {started["group_id"] for started in starts} == {"S", "A"}
    for started in starts:
        endpoint = f"/api/session/{started['session_id']}/message"
        offered = client.post(endpoint, json={"content": "怎么报名？"})
        assert offered.status_code == 200
        assert "资格条件" in offered.json()["content"]
        response = client.post(endpoint, json={"content": selection})
        assert response.status_code == 200
        body = response.json()
        assert body["action"] == "evidence"
        assert body["content"] == next(card["body"] for card in TASK["evidence_cards"]
                                        if card["id"] == "B_BASIC")
        assert "18–25岁" in body["content"] and "在读专科生或本科生" in body["content"]
        assert "请说明要查询哪一项" not in body["content"]
