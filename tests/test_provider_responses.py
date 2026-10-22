"""Provider response validation must not expose truncated or empty answers."""

import asyncio
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

import app.config as config_module
import app.model as model_module
from app.main import create_app
from app.model import LiveProvider, ModelError
from test_flow import settings, start_payload


def mock_response(monkeypatch, *, content, finish_reason="stop"):
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
            return httpx.Response(200, request=httpx.Request("POST", url), json={
                "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
            })

    monkeypatch.setattr(model_module.httpx, "AsyncClient", ResponseClient)
    return calls


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


def test_truncated_response_is_not_saved_and_does_not_consume_a_turn(tmp_path, monkeypatch):
    mock_response(monkeypatch, content="本次需要的材料包括报名表、", finish_reason="length")
    app = create_app(settings(tmp_path, "pilot"))
    client = TestClient(app)
    started = client.post("/api/start", json=start_payload())
    assert started.status_code == 200, started.text
    sid = started.json()["session_id"]
    reply = client.post(f"/api/session/{sid}/message", json={"content": "本次需要什么材料？"})
    assert reply.status_code == 503
    restored = client.get(f"/api/session/{sid}").json()
    assert restored["messages"] == []
    assert restored["completed_turns"] == 0 and restored["pending_reply"] is False


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
