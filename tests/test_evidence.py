"""Regression cases for irrelevant frozen evidence, including a misrouting model."""

import asyncio
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from fixtures.legacy_age_tasks import task_for  # Frozen v3 regression coverage.
from app.config import Settings
from app.db import connect
from app.evidence import asks_for_overview, route_question
from app.main import create_app
from app.model import ModelError
from app.seed import PILOT_TASK_V5
from test_flow import start_payload


class RouteProvider:
    def __init__(self, result=None, *, fail=False):
        self.result = result or {"status": "matched", "card_id": "B_REPEAT", "clarify_slot": None}
        self.fail = fail
        self.calls = []

    async def complete(self, messages, *, temperature=None):
        self.calls.append(messages)
        if self.fail:
            raise ModelError("test outage")
        return self.result if isinstance(self.result, str) else json.dumps(self.result)


def route(provider, message, *, task=None, history=None, revealed_ids=None):
    return asyncio.run(route_question(provider, task or task_for("18-24"),
        [*(history or []), {"role": "user", "content": message}],
        adaptive=False, revealed_ids=revealed_ids or set()))


@pytest.mark.parametrize("message", ["绩点门槛？", "GPA要求", "成绩要多少", "报名费用多少钱？", "最低学历要求？"])
def test_uncovered_subject_does_not_reveal_an_unrelated_rule(message):
    provider = RouteProvider()
    result = route(provider, message, history=[{"role": "user", "content": "比赛奖品影响报名吗？"}])
    assert result["status"] == "not_covered" and result["card_id"] is None
    assert result["source"] == "topic_not_covered"
    assert not provider.calls  # Wrong model output cannot bypass this guard.


@pytest.mark.parametrize(("message", "expected"), [
    ("年龄要求？", "B_BASIC"), ("需要哪些材料？", "B_MATERIALS"),
    ("材料用什么格式？", "D_FAQ"), ("最新截止时间？", "C_TIME"),
    ("原定截止时间？", "B_OLD_TIME"), ("每周需要参加几次？", "B_SCHEDULE"),
])
def test_wrong_valid_card_is_replaced_with_evidence_on_the_requested_subject(message, expected):
    result = route(RouteProvider(), message, revealed_ids={expected})
    assert result["status"] == "matched" and result["card_id"] == expected
    assert result["rejected_card_id"] == "B_REPEAT"


def test_gpa_question_still_works_for_a_task_that_has_a_gpa_rule():
    result = route(RouteProvider(), "GPA门槛？", task=PILOT_TASK_V5)
    assert result["card_id"] == "B_BASIC"
    card = next(card for card in PILOT_TASK_V5["evidence_cards"] if card["id"] == result["card_id"])
    assert "3.2" in card["body"]


def test_unknown_subject_cannot_use_a_valid_id_or_jump_over_the_previous_question():
    provider = RouteProvider()
    assert route(provider, "需要英语基础吗？")["card_id"] is None
    assert route(provider, "英语条件？")["card_id"] is None
    history = [{"role": "user", "content": "年龄要求？"}]
    assert route(provider, "那学历要求呢？", history=history)["card_id"] is None
    history.append({"role": "user", "content": "报名需要手机号吗？"})
    assert route(provider, "那必须填吗？", history=history)["card_id"] is None


def test_colloquial_general_question_is_guided_without_swallowing_a_specific_subject():
    assert asks_for_overview("报名要啥条件？")
    assert not asks_for_overview("绩点门槛？")
    assert not asks_for_overview("最低学历要求？")


@pytest.mark.parametrize(("previous", "followup", "expected"), [
    ("每周需要参加几次？", "那最低是多少？", "B_SCHEDULE"),
    ("最新截止时间？", "具体几点？", "C_TIME"),
])
def test_elliptical_followup_uses_context(previous, followup, expected):
    provider = RouteProvider()
    history = [{"role": "user", "content": previous}, {"role": "assistant", "content": "已查到相关规定。"}]
    result = route(provider, followup, history=history)
    assert result["card_id"] == expected
    prompt = provider.calls[0][0]["content"]
    assert '"body"' in prompt and "已查到相关规定" in prompt


@pytest.mark.parametrize("result", [
    {"status": "not_covered", "card_id": None},
    {"status": "not_covered", "card_id": "B_REPEAT"},
    {"card_id": None},
])
def test_explicit_no_match_is_not_overridden_by_keyword_fallback(result):
    routed = route(RouteProvider(result), "需要哪些材料？")
    assert routed["status"] == "not_covered" and routed["card_id"] is None


@pytest.mark.parametrize("provider", [RouteProvider("not json"), RouteProvider(fail=True),
                                     RouteProvider({"card_id": "does_not_exist"})])
def test_failures_can_only_fall_back_to_relevant_evidence(provider):
    assert route(provider, "年龄要求？")["card_id"] == "B_BASIC"
    result = route(provider, "今天股市如何？")
    assert result["card_id"] is None


@pytest.mark.parametrize("strategy", ["module", "prompt"])
def test_both_arms_reply_uncovered_and_record_no_evidence_reveal(tmp_path, strategy):
    cfg = Settings(database_path=tmp_path / "routing.sqlite3", study_mode="pilot",
        admin_password="test-password", model_api_key="test-key", model_base_url="https://example.invalid/v1",
        model_name="test-model", model_temperature=0, assignment_seed="routing-test")
    app = create_app(replace(cfg, clarification_strategy=strategy))
    # The model now explains missing coverage itself. The participant must get
    # that answer, rather than a lexical gate suppressing the model call.
    provider = RouteProvider("当前任务资料未提供绩点门槛，无法确认；未载明不代表没有这项要求。")
    app.state.provider = provider
    client = TestClient(app)
    sessions = [client.post("/api/start", json=start_payload()).json() for _ in range(2)]
    assert {session["group_id"] for session in sessions} == {"S", "A"}
    for session in sessions:
        response = client.post(f"/api/session/{session['session_id']}/message",
                               json={"content": "绩点门槛？"}).json()
        assert response["action"] == "answer" and "未提供" in response["content"]
        assert "绩点" in response["content"] and "不代表没有" in response["content"]
        assert "重复名额" not in response["content"]
    assert len(provider.calls) == 2
    assert all(call[-1] == {"role": "user", "content": "绩点门槛？"} for call in provider.calls)
    with connect(cfg.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM model_events WHERE event_type='evidence_reveal'").fetchone()[0] == 0
        events = [json.loads(row[0]) for row in db.execute(
            "SELECT detail_json FROM model_events WHERE event_type='evidence_route'")]
    assert events == []
