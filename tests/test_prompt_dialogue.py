"""Both study arms answer the conversation; clarification is a prompt intervention."""

import copy
import json
from dataclasses import replace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.student_materials import task_for
import app.config as config_module
from app.db import connect
from app.main import create_app
from app.model import (CLARIFICATION_MARKER, DIALOGUE_OUTPUT_CONTRACT,
                       answer_system_prompt, prompt_only_system_prompt)
from app.record_reporting import ai_prompt_counts
from test_flow import auth, settings, start_payload


class ConversationProvider:
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.calls = []

    async def complete(self, messages, *, temperature=None):
        self.calls.append(copy.deepcopy(messages))
        return self.replies.pop(0) if self.replies else f"模型针对当前问题的自然回答，第{len(self.calls)}次。"


def setup_conversations(tmp_path, *, mode="pilot", configured_strategy="module", replies=()):
    config = replace(settings(tmp_path, mode), clarification_strategy=configured_strategy)
    app = create_app(config)
    provider = ConversationProvider(replies)
    app.state.provider = provider
    client = TestClient(app)
    if mode == "live":
        response = client.post("/api/admin/tasks", headers=auth(), json=task_for("18-20"))
        assert response.status_code == 200, response.text
    sessions = {}
    for _ in range(2):
        response = client.post("/api/start", json=start_payload())
        assert response.status_code == 200, response.text
        sessions[response.json()["group_id"]] = response.json()
    assert set(sessions) == {"S", "A"}
    return config, client, provider, sessions


def send(client, session_id, content, **extra):
    response = client.post(f"/api/session/{session_id}/message", json={"content": content, **extra})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("make_prompt", [answer_system_prompt, prompt_only_system_prompt])
def test_both_arms_share_intent_priority_json_output_and_consistency_rules(make_prompt):
    prompt = make_prompt(task_for("21-23"))
    assert "用户只是问候：简短回应，邀请提问，不索取个案事实" in prompt
    assert "不知道、查不到或无法补充某项事实：不再索取该事实" in prompt
    assert "报名流程（如‘怎么报名’）" in prompt
    assert "前文有未完成的个案推荐，也不改变本轮意图" in prompt
    assert "每轮尽量控制在300汉字内，多个问题逐项答全" in prompt
    assert "不为缩短而遗漏所问结论" in prompt
    assert "‘本人确认选择’不等于必须本人亲自递交" in prompt
    assert DIALOGUE_OUTPUT_CONTRACT in prompt
    assert "kind只能是answer或clarification" in prompt
    assert "先写content，再根据正文填写kind" in prompt
    assert "要求用户提供、补充或确认个案事实" in prompt
    assert "kind必须为clarification" in prompt
    assert "仅确认‘第一个’指哪段内容或用户想了解哪个话题时，kind为answer" in prompt
    assert CLARIFICATION_MARKER not in prompt
    assert "首句的‘是’或‘否’必须与后续结论和资料依据一致" in prompt
    assert "截止日期与包含钟点的完整截止时点" in prompt
    assert "截止时点更正，其他规则继续有效" in prompt


def test_adaptive_prompt_requires_one_missing_fact_and_stops_when_unavailable():
    prompt = prompt_only_system_prompt(task_for("21-23"))
    assert "共同规则的前3项优先" in prompt
    assert "只有本轮明确请求针对个案的判断或推荐" in prompt
    assert "用户也没有表示无法提供的事实时，才先提问" in prompt
    assert "‘先不要假定’只禁止猜测，不表示事实无法补充" in prompt
    assert "content只写关于一个尚缺事实的直接问句" in prompt
    assert "不提其他缺失事实、不列条件方案" in prompt
    assert "用户无法补充时改给有依据的条件性建议" in prompt
    assert "有依据的条件性建议" in prompt


@pytest.mark.parametrize("mode", ["pilot", "live"])
@pytest.mark.parametrize("configured_strategy", ["module", "prompt"])
def test_every_real_question_reaches_the_model_with_public_evidence_and_full_history(
        tmp_path, mode, configured_strategy):
    config, client, provider, sessions = setup_conversations(
        tmp_path, mode=mode, configured_strategy=configured_strategy)
    questions = ["你好", "办理要求如何？", "直接告诉我能否报名", "陈禾每周最多能到场几次？", "材料如何上传？"]
    for group, started in sessions.items():
        sid = started["session_id"]
        assert started["clarification_strategy"] == "prompt"
        with connect(config.database_path) as db:
            task = json.loads(db.execute("SELECT task_snapshot_json FROM sessions WHERE id=?", (sid,)).fetchone()[0])
            assert task["evidence_cards"]
            # Distinct markers make accidental disclosure observable even if
            # the normal private facts happen to repeat the public context.
            task["participant_facts"] = "PRIVATE_CASE_FACT_NOT_YET_TOLD_TO_MODEL"
            task["rubric"] = "PRIVATE_SCORING_RUBRIC_NOT_FOR_MODEL"
            db.execute("UPDATE sessions SET task_snapshot_json=? WHERE id=?", (json.dumps(task), sid))
        expected_system = prompt_only_system_prompt(task) if group == "A" else answer_system_prompt(task)
        transcript = []
        for question in questions:
            calls_before = len(provider.calls)
            result = send(client, sid, question)
            assert len(provider.calls) == calls_before + 1
            assert result["content"] == f"模型针对当前问题的自然回答，第{len(provider.calls)}次。"
            assert result["action"] == "answer"
            transcript.append({"role": "user", "content": question})
            assert provider.calls[-1] == [{"role": "system", "content": expected_system}, *transcript]
            assert task["reference_text"] in expected_system
            assert all(slot["description"] in expected_system for slot in task["slots"])
            assert "PRIVATE_CASE_FACT_NOT_YET_TOLD_TO_MODEL" not in expected_system
            assert "PRIVATE_SCORING_RUBRIC_NOT_FOR_MODEL" not in expected_system
            transcript.append({"role": "assistant", "content": result["content"]})
        with connect(config.database_path) as db:
            events = [row[0] for row in db.execute(
                "SELECT event_type FROM model_events WHERE session_id=?", (sid,))]
            prompts = [json.loads(row[0]) for row in db.execute(
                "SELECT detail_json FROM model_events WHERE session_id=? AND event_type='dialogue_prompt' ORDER BY id", (sid,))]
        assert "evidence_route" not in events
        assert "clarification_decision" not in events
        assert len(prompts) == len(questions)
        assert all(event == {
            "version": started["prompt_version"], "system_prompt": expected_system,
            "used_extra_prompt": group == "A",
        } for event in prompts)


def test_clarification_prompt_survives_multiple_questions_and_context_is_retained(tmp_path):
    replies = [
        CLARIFICATION_MARKER + "此前获得过这个项目的名额吗？",
        CLARIFICATION_MARKER + "每周最多能到场几次？",
        "此前未获得过名额，且每周最多到场一次，应结合资料选择登记甲和弹性安排。",
    ]
    config, client, provider, sessions = setup_conversations(tmp_path, replies=replies)
    sid = sessions["A"]["session_id"]
    first = send(client, sid, "我能选哪种参加安排？")
    second = send(client, sid, "我以前从来没有获得过这个项目的名额。")
    third = send(client, sid, "我每周最多能到场一次，请现在回答该选什么。")
    assert first["action"] == second["action"] == "prompt_clarification"
    assert first["content"] == "此前获得过这个项目的名额吗？"
    assert second["content"] == "每周最多能到场几次？"
    assert third["content"] == replies[2] and third["action"] == "answer"
    assert len(provider.calls) == 3
    assert provider.calls[0][0] == provider.calls[1][0] == provider.calls[2][0]
    assert provider.calls[2][1:] == [
        {"role": "user", "content": "我能选哪种参加安排？"},
        {"role": "assistant", "content": CLARIFICATION_MARKER + first["content"]},
        {"role": "user", "content": "我以前从来没有获得过这个项目的名额。"},
        {"role": "assistant", "content": CLARIFICATION_MARKER + second["content"]},
        {"role": "user", "content": "我每周最多能到场一次，请现在回答该选什么。"},
    ]
    restored = client.get(f"/api/session/{sid}").json()
    assert [message["action"] for message in restored["messages"] if message["role"] == "assistant"] == [
        "prompt_clarification", "prompt_clarification", "answer"]
    with connect(config.database_path) as db:
        strategy_events = [json.loads(row[0]) for row in db.execute(
            "SELECT detail_json FROM model_events WHERE session_id=? AND event_type='prompt_strategy' ORDER BY id", (sid,))]
    assert len(strategy_events) == 3
    assert all(event["used_extra_prompt"] for event in strategy_events)


def test_control_arm_records_a_natural_clarification_without_receiving_the_intervention(tmp_path):
    config, client, provider, sessions = setup_conversations(
        tmp_path, replies=[CLARIFICATION_MARKER + "每周最多能到场几次？"])
    sid = sessions["S"]["session_id"]
    response = send(client, sid, "请帮我判断该选哪种参加安排。")
    assert response["content"] == "每周最多能到场几次？"
    assert response["action"] == "prompt_clarification"
    with connect(config.database_path) as db:
        task = json.loads(db.execute("SELECT task_snapshot_json FROM sessions WHERE id=?", (sid,)).fetchone()[0])
        prompt_event = json.loads(db.execute(
            "SELECT detail_json FROM model_events WHERE session_id=? AND event_type='dialogue_prompt'", (sid,)).fetchone()[0])
    assert provider.calls[0][0] == {"role": "system", "content": answer_system_prompt(task)}
    assert "额外的澄清策略（主动澄清组）" not in provider.calls[0][0]["content"]
    assert prompt_event["used_extra_prompt"] is False
    messages = client.get(f"/api/session/{sid}").json()["messages"]
    assert ai_prompt_counts(messages)["ai_clarification_count"] == 1


def test_empty_clarification_marker_is_retryable_model_failure(tmp_path):
    config, client, provider, sessions = setup_conversations(
        tmp_path, replies=[CLARIFICATION_MARKER + "   ", "请问此前是否获得过本项目名额？"])
    sid = sessions["A"]["session_id"]
    body = {"content": "请根据我还没说的情况判断参加安排。", "request_id": "empty-marker-retry"}
    failed = client.post(f"/api/session/{sid}/message", json=body)
    assert failed.status_code == 503
    restored = client.get(f"/api/session/{sid}").json()
    assert restored["messages"] == [] and restored["completed_turns"] == 0
    retried = client.post(f"/api/session/{sid}/message", json=body)
    assert retried.status_code == 200, retried.text
    assert retried.json()["content"] == "请问此前是否获得过本项目名额？"
    assert retried.json()["completed_turns"] == 1
    assert len(provider.calls) == 2


def test_natural_model_dialogue_does_not_require_legacy_card_reveal_events(tmp_path):
    config, client, provider, sessions = setup_conversations(tmp_path)
    sid = sessions["S"]["session_id"]
    send(client, sid, "本次办理的现行截止日期和材料是什么？")
    send(client, sid, "请再说明办理渠道，并根据资料解释来源冲突。")
    decision = client.post(f"/api/session/{sid}/decision")
    assert decision.status_code == 200, decision.text
    choices = decision.json()
    submitted = client.post(f"/api/session/{sid}/submit", json={
        "decision_choice_id": choices["decision_options"][0]["id"],
        "execution_choice_id": choices["execution_options"][0]["id"],
    })
    assert submitted.status_code == 200, submitted.text
    with connect(config.database_path) as db:
        flags = json.loads(db.execute("SELECT flags_json FROM sessions WHERE id=?", (sid,)).fetchone()[0])
    assert "no_evidence_retrieved" not in flags
    assert len(provider.calls) == 2


@pytest.mark.parametrize("legacy_group", ["S", "A"])
def test_legacy_prompt_session_remains_readable_but_cannot_chat_or_enter_current_cohort(tmp_path, legacy_group):
    config, client, provider, sessions = setup_conversations(tmp_path)
    old_sid = sessions[legacy_group]["session_id"]
    current_sid = sessions["A" if legacy_group == "S" else "S"]["session_id"]
    old_answer = send(client, old_sid, "请说明当前材料要求以及正式办理渠道。")
    old_version = "evidence-router-v5" + ("+clarify-router-v3" if legacy_group == "A" else "")
    with connect(config.database_path) as db:
        db.execute("UPDATE sessions SET prompt_version=? WHERE id=?", (old_version, old_sid))
    restored = client.get(f"/api/session/{old_sid}")
    assert restored.status_code == 200
    assert restored.json()["prompt_version"] == old_version
    assert restored.json()["messages"][-1]["content"] == old_answer["content"]
    call_count = len(provider.calls)
    refused = client.post(f"/api/session/{old_sid}/message", json={"content": "接着解释材料要求。"})
    assert refused.status_code == 409 and len(provider.calls) == call_count
    # Freeze two otherwise valid completed records. Only the legacy prompt
    # version should exclude the old one from the new analysis cohort.
    with connect(config.database_path) as db:
        for sid in (old_sid, current_sid):
            task = json.loads(db.execute("SELECT task_snapshot_json FROM sessions WHERE id=?", (sid,)).fetchone()[0])
            decision = max(task["decision_options"], key=lambda option: option["score"])
            execution = max(task["execution_options"], key=lambda option: option["score"])
            db.execute("""UPDATE sessions SET created_at='2026-10-22T01:00:00+00:00',
                decision_started_at='2026-10-22T01:04:00+00:00',ended_at='2026-10-22T01:05:00+00:00',
                decision_choice_id=?,execution_choice_id=?,score=? WHERE id=?""",
                (decision["id"], execution["id"], decision["score"] + execution["score"], sid))
    archive = client.get("/api/admin/archive-export?kind=raw&format=json", headers=auth())
    assert archive.status_code == 200
    aliases = {row["id"]: row["record_id"] for row in archive.json()}
    summary = client.get("/api/admin/summary", headers=auth())
    assert summary.status_code == 200
    assert summary.json()["analysis_n"] == 1
    assert summary.json()["cohort_session_ids"] == [aliases[current_sid]]
    analysis = client.get("/api/admin/export?kind=analysis&format=json", headers=auth())
    assert analysis.status_code == 200
    assert [row["record_id"] for row in analysis.json()] == [aliases[current_sid]]
    assert next(row for row in archive.json() if row["id"] == old_sid)["prompt_version"] == old_version


@pytest.mark.parametrize("configured_strategy", [None, "module", "prompt"])
def test_environment_defaults_and_legacy_module_configuration_use_prompt_intervention(monkeypatch, configured_strategy):
    monkeypatch.setattr(config_module, "load_dotenv", lambda: None)
    environment = {"ADMIN_PASSWORD": "test-password"}
    if configured_strategy is not None:
        environment["CLARIFICATION_STRATEGY"] = configured_strategy
    with patch.dict("os.environ", environment, clear=True):
        config = config_module.Settings.from_env()
    assert config.clarification_strategy == "prompt"
