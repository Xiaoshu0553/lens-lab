"""Provider response validation must not expose truncated or empty answers."""

import asyncio
import json
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

import app.config as config_module
import app.model as model_module
from app.main import create_app
from app.db import connect
from app.model import (CLARIFICATION_MARKER, DIALOGUE_OUTPUT_CONTRACT, LiveProvider, ModelError,
                       answer_system_prompt)
from app.student_materials import task_for
from test_flow import settings, start_payload


def mock_body(monkeypatch, body, status=200):
    calls = []

    class ResponseClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            calls.append({"url": url, **kwargs})
            return httpx.Response(status, request=httpx.Request("POST", url), json=body)

    monkeypatch.setattr(model_module.httpx, "AsyncClient", ResponseClient)
    return calls


def mock_response(monkeypatch, *, content, finish_reason="stop"):
    return mock_body(monkeypatch, {
        "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
    })


def test_complete_stop_response_returns_the_model_text(tmp_path, monkeypatch):
    calls = mock_response(monkeypatch, content="  根据资料，本次需要报名表和参加计划。\n")
    provider = LiveProvider(settings(tmp_path, "pilot"))
    messages = [{"role": "system", "content": "请直接回答。"}, {"role": "user", "content": "需要什么材料？"}]
    assert asyncio.run(provider.complete(messages)) == "根据资料，本次需要报名表和参加计划。"
    assert len(calls) == 1 and calls[0]["json"]["messages"] == messages


def test_length_limited_response_is_a_model_error_even_when_content_is_nonempty(tmp_path, monkeypatch):
    mock_response(monkeypatch, content="本次需要的材料包括报名表、", finish_reason="length")
    provider = LiveProvider(settings(tmp_path, "pilot"))
    with pytest.raises(ModelError):
        asyncio.run(provider.complete([{"role": "user", "content": "需要什么材料？"}]))


@pytest.mark.parametrize("content", ["", "   \n", None])
def test_empty_stop_response_remains_a_model_error(tmp_path, monkeypatch, content):
    mock_response(monkeypatch, content=content)
    provider = LiveProvider(settings(tmp_path, "pilot"))
    with pytest.raises(ModelError):
        asyncio.run(provider.complete([{"role": "user", "content": "需要什么材料？"}]))


@pytest.mark.parametrize("finish_reason", ["length", "content_filter", "tool_calls", "function_call", "unexpected"])
def test_incomplete_response_is_retryable_without_saving_or_consuming_a_turn(tmp_path, monkeypatch, finish_reason):
    mock_response(monkeypatch, content="本次需要的材料包括报名表、", finish_reason=finish_reason)
    app = create_app(settings(tmp_path, "pilot"))
    client = TestClient(app)
    started = client.post("/api/start", json=start_payload())
    assert started.status_code == 200, started.text
    sid = started.json()["session_id"]
    request = {"content": "本次需要什么材料？", "request_id": "incomplete-reply"}
    reply = client.post(f"/api/session/{sid}/message", json=request)
    assert reply.status_code == 503
    restored = client.get(f"/api/session/{sid}").json()
    assert restored["messages"] == []
    assert restored["completed_turns"] == 0 and restored["pending_reply"] is False
    assert restored["completed_request_ids"] == []
    mock_response(monkeypatch, content=json.dumps({"kind": "answer", "content": "需要报名表和参加计划。"}))
    retried = client.post(f"/api/session/{sid}/message", json=request)
    assert retried.status_code == 200, retried.text
    assert retried.json()["content"] == "需要报名表和参加计划。"
    assert retried.json()["completed_turns"] == 1
    assert retried.json()["user_turns"] == 1


@pytest.mark.parametrize("finish_fields", [{}, {"finish_reason": None}])
def test_compatible_provider_can_omit_the_finish_reason(tmp_path, monkeypatch, finish_fields):
    mock_body(monkeypatch, {"choices": [{**finish_fields, "message": {"content": "需要报名表和参加计划。"}}]})
    provider = LiveProvider(settings(tmp_path, "pilot"))
    assert asyncio.run(provider.complete([{"role": "user", "content": "需要什么材料？"}])) == "需要报名表和参加计划。"


@pytest.mark.parametrize("body", [
    None, [], {}, {"choices": None}, {"choices": {}}, {"choices": "invalid"},
    {"choices": []}, {"choices": [None]}, {"choices": ["invalid"]},
    {"choices": [{"message": None}]}, {"choices": [{"message": []}]},
    {"choices": [{"message": "invalid"}]}, {"choices": [{"message": {}}]},
])
def test_malformed_provider_response_is_a_retryable_error_instead_of_500(tmp_path, monkeypatch, body):
    mock_body(monkeypatch, body)
    app = create_app(settings(tmp_path, "pilot"))
    client = TestClient(app)
    started = client.post("/api/start", json=start_payload())
    assert started.status_code == 200, started.text
    sid = started.json()["session_id"]
    reply = client.post(f"/api/session/{sid}/message", json={"content": "本次需要什么材料？"})
    assert reply.status_code == 503, reply.text
    restored = client.get(f"/api/session/{sid}").json()
    assert restored["messages"] == []
    assert restored["user_turns"] == restored["completed_turns"] == 0
    assert restored["pending_reply"] is False


@pytest.mark.parametrize("configured_tokens,expected", [(None, 1600), ("800", 800)])
def test_token_budget_default_increases_without_overriding_explicit_configuration(monkeypatch, configured_tokens, expected):
    monkeypatch.setattr(config_module, "load_dotenv", lambda: None)
    environment = {"ADMIN_PASSWORD": "test-password"}
    if configured_tokens is not None:
        environment["MODEL_MAX_TOKENS"] = configured_tokens
    with patch.dict("os.environ", environment, clear=True):
        config = config_module.Settings.from_env()
    assert config.model_max_tokens == expected
    assert LiveProvider(config).request_payload([{"role": "user", "content": "你好"}])["max_tokens"] == expected


@pytest.mark.parametrize('kind', ['answer', 'clarification'])
def test_dialogue_json_protocol_returns_only_visible_content_and_the_recorded_kind(tmp_path, monkeypatch, kind):
    text = '此前获得过本项目名额吗？' if kind == 'clarification' else '需要报名表和参加计划。'
    calls = mock_response(monkeypatch, content=json.dumps({'content': text, 'kind': kind}, ensure_ascii=False))
    provider = LiveProvider(settings(tmp_path, 'pilot'))
    messages = [{'role': 'system', 'content': answer_system_prompt(task_for('21-23'))},
                {'role': 'user', 'content': '该怎样选？'},
                {'role': 'assistant', 'content': CLARIFICATION_MARKER + '每周能到场几次？'},
                {'role': 'user', 'content': '一次'}]
    original = json.loads(json.dumps(messages))
    answer = asyncio.run(provider.complete(messages))
    assert answer == (CLARIFICATION_MARKER if kind == 'clarification' else '') + text
    assert messages == original
    payload = calls[0]['json']
    assert payload['response_format'] == {'type': 'json_object'}
    assert json.loads(payload['messages'][2]['content']) == {
        'content': '每周能到场几次？', 'kind': 'clarification'}
    assert payload['messages'][-1]['content'] == '一次'


@pytest.mark.parametrize('content', [
    '半截回答', '```json\n{}\n```', 'null', '[]', '{}',
    '{"kind":"other","content":"回答"}', '{"kind":"answer","content":"  "}',
    '{"kind":"answer","content":null}', '{"kind":"answer","content":123}',
    '{"kind":"answer","content":"回答","extra":true}',
    '{"kind":"answer","content":"【澄清提问】每周几次？"}',
])
def test_invalid_dialogue_json_cannot_save_an_empty_or_unclassified_turn(tmp_path, monkeypatch, content):
    mock_response(monkeypatch, content=content)
    client = TestClient(create_app(settings(tmp_path, 'pilot')))
    sid = client.post('/api/start', json=start_payload()).json()['session_id']
    request = {'content': '如何选择？', 'request_id': 'json-protocol-retry'}
    assert client.post(f'/api/session/{sid}/message', json=request).status_code == 503
    state = client.get(f'/api/session/{sid}').json()
    assert state['messages'] == [] and state['completed_turns'] == 0 and not state['pending_reply']
    with connect(client.app.state.settings.database_path) as db:
        detail = json.loads(db.execute("SELECT detail_json FROM model_events WHERE session_id=? AND event_type='provider_error'", (sid,)).fetchone()[0])
    assert detail['error_code'] in {'invalid_dialogue_json', 'invalid_dialogue_fields'}
    assert 'content' not in detail
    mock_response(monkeypatch, content=json.dumps({'kind': 'clarification', 'content': '每周能到场几次？'}))
    retry = client.post(f'/api/session/{sid}/message', json=request)
    assert retry.status_code == 200
    assert retry.json()['action'] == 'prompt_clarification'
    assert retry.json()['content'] == '每周能到场几次？'


def test_upstream_failure_logs_safe_http_diagnostics_without_the_error_body(tmp_path, monkeypatch):
    mock_body(monkeypatch, {'error': {'message': 'PRIVATE_UPSTREAM_VALUE'}}, status=503)
    config = settings(tmp_path, 'pilot')
    client = TestClient(create_app(config))
    sid = client.post('/api/start', json=start_payload()).json()['session_id']
    response = client.post(f'/api/session/{sid}/message', json={'content': '需要什么材料？'})
    assert response.status_code == 503 and 'PRIVATE_UPSTREAM_VALUE' not in response.text
    with connect(config.database_path) as db:
        raw = db.execute("SELECT detail_json FROM model_events WHERE session_id=? AND event_type='provider_error'", (sid,)).fetchone()[0]
    detail = json.loads(raw)
    assert detail['error_code'] == 'provider_http_error' and detail['http_status'] == 503
    assert 'PRIVATE_UPSTREAM_VALUE' not in raw


@pytest.mark.parametrize('failure', ['transport', 429, 500, 502, 503, 504])
def test_transient_service_failure_retries_once_with_the_same_messages(tmp_path, monkeypatch, failure):
    calls = []
    sleeps = []
    async def sleep(delay):
        sleeps.append(delay)
    class FlakyClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            calls.append(kwargs['json'])
            if len(calls) == 1:
                if failure == 'transport':
                    raise httpx.RemoteProtocolError('upstream closed the connection')
                return httpx.Response(failure, request=httpx.Request('POST', url), headers={'Retry-After': '2'})
            return httpx.Response(200, request=httpx.Request('POST', url), json={
                'choices': [{'finish_reason': 'stop', 'message': {'content': '完整回复'}}]})
    monkeypatch.setattr(model_module.httpx, 'AsyncClient', FlakyClient)
    monkeypatch.setattr(model_module.asyncio, 'sleep', sleep)
    result = asyncio.run(LiveProvider(settings(tmp_path, 'pilot')).complete([{'role': 'user', 'content': '你好'}]))
    assert result == '完整回复'
    assert len(calls) == 2 and calls[0] == calls[1]
    assert sleeps == [1 if failure == 'transport' else 2]


@pytest.mark.parametrize('status', [400, 401, 403, 404])
def test_permanent_provider_error_is_not_retried(tmp_path, monkeypatch, status):
    calls = mock_body(monkeypatch, {'error': {}}, status=status)
    with pytest.raises(ModelError) as error:
        asyncio.run(LiveProvider(settings(tmp_path, 'pilot')).complete([{'role': 'user', 'content': '你好'}]))
    assert error.value.http_status == status
    assert len(calls) == 1
