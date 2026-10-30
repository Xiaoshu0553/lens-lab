"""Bounded reply-envelope recovery with local providers and temporary databases."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.model import ModelError
from app.translation_materials import FORM_ITEMS, FORM_VERSION, SELF_ITEMS, TASK
from app.translation_model import (
    REPLY_CONTRACT_REMINDER,
    build_dialogue_messages,
    complete_dialogue_reply,
    dialogue_message_protocol,
)
from test_flow import auth, settings


VALID_RAW = '{"kind":"answer","content":"罗温说。"}'
BAD_RAW = '{"content":"UNSAVED_BAD_REPLY"}'


class SequenceProvider:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages, *, temperature=None):
        self.calls.append(deepcopy(messages))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _messages(condition="A"):
    return build_dialogue_messages(TASK, condition, [
        {"role": "user", "content": "只翻译 they said，不给相邻引语。"},
    ])


def _local_session(tmp_path, provider, condition):
    app = create_app(settings(tmp_path, "pilot"))
    app.state.translation_provider = provider
    client = TestClient(app)
    started = client.post("/api/translation/start", headers=auth(), json={
        "consent": True, "university_confirmed": True, "age_band": "21",
        "education_stage": "bachelor", "undergraduate_year": "year-3",
        "translation_experience": 3, "pretest_version": FORM_VERSION,
        "answers": {item["id"]: item["correct"] for item in FORM_ITEMS},
        "self_answers": {item["id"]: 2 for item in SELF_ITEMS},
        "request_id": str(uuid.uuid4()), "researcher_preview": True,
        "preview_condition": condition,
    })
    assert started.status_code == 200, started.text
    return client, started.json()["id"]


@pytest.mark.parametrize("kind", ("answer", "clarification"))
def test_valid_envelope_returns_after_one_call_without_touching_input(kind):
    raw = json.dumps({"kind": kind, "content": "  本轮局部回复。  "}, ensure_ascii=False)
    provider = SequenceProvider(raw)
    messages = _messages()
    original = deepcopy(messages)
    reply, retries, returned_raw = asyncio.run(complete_dialogue_reply(provider, messages))
    assert reply == {"action": kind, "content": "本轮局部回复。"}
    assert retries == 0 and returned_raw == raw
    assert provider.calls == [original] and messages == original


@pytest.mark.parametrize("condition", ("S", "A"))
def test_missing_kind_retries_only_the_contract_and_never_inserts_the_bad_reply(condition):
    provider = SequenceProvider(BAD_RAW, VALID_RAW)
    messages = _messages(condition)
    original = deepcopy(messages)
    reply, retries, raw = asyncio.run(complete_dialogue_reply(provider, messages))
    assert reply == {"action": "answer", "content": "罗温说。"}
    assert retries == 1 and raw == VALID_RAW and messages == original
    assert provider.calls == [original, [*original, {"role": "system", "content": REPLY_CONTRACT_REMINDER}]]
    assert "UNSAVED_BAD_REPLY" not in json.dumps(provider.calls, ensure_ascii=False)
    assert not any(message["role"] == "assistant" for message in provider.calls[-1][len(original):])


def test_provider_request_mutation_cannot_modify_the_callers_messages():
    class MutatingProvider(SequenceProvider):
        async def complete(self, messages, *, temperature=None):
            result = await super().complete(messages, temperature=temperature)
            messages[0]["content"] = "PROVIDER_MUTATED_REQUEST"
            messages.append({"role": "assistant", "content": "UNSAVED_PROVIDER_APPEND"})
            return result

    provider = MutatingProvider(BAD_RAW, VALID_RAW)
    messages = _messages()
    original = deepcopy(messages)
    asyncio.run(complete_dialogue_reply(provider, messages))
    assert messages == original
    assert provider.calls[1][:-1] == original


@pytest.mark.parametrize("bad", (
    BAD_RAW,
    '{"kind":"answer"}',
    '{"kind":"answer","content":"ok","explanation":"extra"}',
    '{"kind":"answer","content":"first","content":"duplicate"}',
    "not-json",
))
def test_persistent_schema_errors_fail_after_two_calls_without_relaxing_the_parser(bad):
    provider = SequenceProvider(bad, bad, VALID_RAW)
    messages = _messages()
    original = deepcopy(messages)
    with pytest.raises(ModelError) as caught:
        asyncio.run(complete_dialogue_reply(provider, messages))
    assert caught.value.code == "invalid_translation_reply"
    assert caught.value.reply_contract_retries == 1
    assert len(provider.calls) == 2 and provider.responses == [VALID_RAW]
    assert messages == original


@pytest.mark.parametrize("code", ("provider_http_error", "provider_timeout", "truncated", "invalid_model_identity"))
def test_provider_failures_are_not_reclassified_as_reply_schema_retries(code):
    failure = ModelError("transport failure", code)
    provider = SequenceProvider(failure, VALID_RAW)
    with pytest.raises(ModelError) as caught:
        asyncio.run(complete_dialogue_reply(provider, _messages()))
    assert caught.value is failure and caught.value.code == code
    assert caught.value.reply_contract_retries == 0
    assert len(provider.calls) == 1 and provider.responses == [VALID_RAW]


def test_one_overall_deadline_covers_both_attempts(monkeypatch):
    original_timeout = asyncio.timeout
    deadlines, calls = [], []

    def short_timeout(seconds):
        deadlines.append(seconds)
        return original_timeout(.02)

    class WaitingProvider:
        async def complete(self, messages):
            calls.append(deepcopy(messages))
            if len(calls) == 1:
                return BAD_RAW
            await asyncio.Future()

    monkeypatch.setattr("app.translation_model.asyncio.timeout", short_timeout)
    with pytest.raises(TimeoutError) as caught:
        asyncio.run(complete_dialogue_reply(WaitingProvider(), _messages()))
    assert deadlines == [120] and len(calls) == 2
    assert caught.value.reply_contract_retries == 1


def test_recovery_limits_and_reminder_are_part_of_the_frozen_message_protocol():
    expected = {
        "maximum_retries": 1,
        "overall_timeout_seconds": 120,
        "message": {"role": "system", "content": REPLY_CONTRACT_REMINDER},
    }
    assert dialogue_message_protocol()["reply_contract_recovery"] == expected
    assert "保持原对话的当前目标、范围和澄清策略" in REPLY_CONTRACT_REMINDER
    assert "不添加其他字段" in REPLY_CONTRACT_REMINDER
    changed = dialogue_message_protocol()
    changed["reply_contract_recovery"]["message"]["content"] = "MUTATED"
    assert dialogue_message_protocol()["reply_contract_recovery"] == expected


@pytest.mark.parametrize("condition", ("S", "A"))
def test_api_recovery_saves_one_message_pair_one_turn_and_one_reply_event(tmp_path, condition):
    provider = SequenceProvider(BAD_RAW, VALID_RAW)
    client, sid = _local_session(tmp_path, provider, condition)
    body = {"request_id": str(uuid.uuid4()), "content": "这里they指罗温一个人；只译they said，不给相邻引语。"}
    sent = client.post(f"/api/translation/session/{sid}/message", headers=auth(), json=body)
    assert sent.status_code == 200, sent.text
    assert sent.json()["completed_turns"] == 1
    assert sent.json()["completed_request_ids"] == [body["request_id"]]
    detail = client.get(f"/api/admin/translation/records/{sid}", headers=auth()).json()
    assert [message["role"] for message in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][0]["content"] == body["content"]
    assert detail["messages"][1]["content"] == "罗温说。"
    assert detail["requests"][body["request_id"]]["status"] == "completed"
    assert detail["pending"] is None
    events = [event for event in detail["events"] if event["type"] == "reply"]
    assert len(events) == 1 and events[0]["reply_contract_retries"] == 1
    assert not [event for event in detail["events"] if event["type"] == "model_error"]
    assert "UNSAVED_BAD_REPLY" not in json.dumps(detail, ensure_ascii=False)
    assert provider.calls[1][:-1] == provider.calls[0]
    repeated = client.post(f"/api/translation/session/{sid}/message", headers=auth(), json=body)
    assert repeated.status_code == 200 and repeated.json()["completed_turns"] == 1
    assert len(provider.calls) == 2


def test_api_persistent_invalid_replies_leave_no_phantom_messages_or_completed_turn(tmp_path):
    provider = SequenceProvider(BAD_RAW, BAD_RAW)
    client, sid = _local_session(tmp_path, provider, "A")
    body = {"request_id": str(uuid.uuid4()), "content": "只解释 gate 的意思。"}
    sent = client.post(f"/api/translation/session/{sid}/message", headers=auth(), json=body)
    assert sent.status_code == 502
    assert len(provider.calls) == 2
    detail = client.get(f"/api/admin/translation/records/{sid}", headers=auth()).json()
    assert detail["messages"] == [] and detail["pending"] is None
    assert detail["requests"][body["request_id"]]["status"] == "failed"
    assert not [event for event in detail["events"] if event["type"] == "reply"]
    failures = [event for event in detail["events"] if event["type"] == "model_error"]
    assert len(failures) == 1 and failures[0]["reply_contract_retries"] == 1
    assert "UNSAVED_BAD_REPLY" not in json.dumps(detail, ensure_ascii=False)
    loaded = client.get(f"/api/translation/session/{sid}", headers=auth()).json()
    assert loaded["completed_turns"] == 0 and loaded["completed_request_ids"] == []
