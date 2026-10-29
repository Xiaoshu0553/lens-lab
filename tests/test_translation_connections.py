"""Private credentials, honest HTTP probes and frozen provider-generation checks."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
import os
import stat
import threading
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.model import ModelError
from app.translation_connections import ConnectionRegistry, JSONModelProvider, clean_connection
from app.translation_quality import RUBRIC
from app.translation_materials import FORM_ITEMS, SELF_ITEMS, FORM_VERSION
from test_flow import auth, settings
from translation_multi_model_helpers import configure_panel, panel_models


SECRET = "private-credential-canary-6471"


def connection(**changes):
    return clean_connection(dict(id="j1",label="模型一",model="distinct-ai-one",base_url="https://api.example.invalid/v1",
                                 api_key=SECRET,weight=1,max_tokens=4096,**changes))


def patch_http(monkeypatch, handler):
    original = httpx.AsyncClient
    initialization = []
    def factory(**kwargs):
        initialization.append(dict(kwargs))
        return original(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr("app.translation_connections.httpx.AsyncClient",factory)
    return initialization


def test_registry_stores_credentials_privately_and_exposes_only_configuration_flags(tmp_path):
    registry=ConnectionRegistry(settings(tmp_path))
    judges=panel_models()
    for item in judges:item["api_key"]=SECRET
    saved=registry.update({"judges":judges})
    assert SECRET not in json.dumps(saved)
    assert all(row["api_key_configured"] for row in saved["judges"])
    assert "api_key" not in saved["judges"][0]
    assert registry.path.exists()
    assert stat.S_IMODE(registry.path.stat().st_mode)==0o600
    assert SECRET in registry.path.read_text()
    restarted=ConnectionRegistry(settings(tmp_path))
    assert restarted.connections()["judges"][0]["api_key"]==SECRET
    assert all(row["verified_at"] is None for row in restarted.snapshot()["judges"])


def test_blank_key_preserves_only_same_endpoint_and_identifier_and_clear_is_explicit():
    first=connection()
    edited=clean_connection({**first,"api_key":"","label":"新名称"},first)
    assert edited["api_key"]==SECRET
    moved=clean_connection({**first,"api_key":"","base_url":"https://different.example.invalid/v1"},first)
    assert moved["api_key"]==""
    renamed=clean_connection({**first,"id":"j2","api_key":""},first)
    assert renamed["api_key"]==""
    cleared=clean_connection({**first,"api_key":"","clear_api_key":True},first)
    assert cleared["api_key"]==""


def test_key_rotation_invalidates_success_without_changing_public_model_identity(tmp_path):
    registry=ConnectionRegistry(settings(tmp_path))
    judges=panel_models()
    registry.update({"judges":judges})
    initial=registry.connections()["judges"][0]
    registry.mark_success(initial)
    assert registry.snapshot()["judges"][0]["verified_at"]
    judges[0]["api_key"]="new-credential-canary"
    result=registry.update({"judges":judges})
    assert result["judges"][0]["verified_at"] is None
    assert "new-credential-canary" not in json.dumps(result)
    assert result["judges"][0]["model"]==initial["model"]


@pytest.mark.parametrize("body", ["not-json", "[]", '{"ok":false}', '{"ok":"true"}', '{"ok":1}', '{}'])
def test_connectivity_probe_requires_actual_true_json_and_never_invents_success(tmp_path,monkeypatch,body):
    registry=ConnectionRegistry(settings(tmp_path))
    patch_http(monkeypatch,lambda request:httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"content":body}}]}))
    result=asyncio.run(registry.check("dialogue"))
    assert len(result["checks"])==1 and result["checks"][0]["ok"] is False
    assert result["dialogue"]["verified_at"] is None
    assert result["model_api_verified"] is False


def test_connectivity_probe_marks_only_requested_actual_response_and_keeps_keys_secret(tmp_path,monkeypatch):
    registry=ConnectionRegistry(settings(tmp_path))
    registry.update({"dialogue":{**registry.connections()["dialogue"],"api_key":SECRET}})
    requests=[]
    def handler(request):
        requests.append(request)
        assert request.headers["authorization"]=="Bearer "+SECRET
        return httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"content":'{"ok":true}'}}]})
    patch_http(monkeypatch,handler)
    result=asyncio.run(registry.check("dialogue"))
    assert len(requests)==1
    assert result["checks"][0]["ok"] and result["model_api_verified"]
    assert result["dialogue"]["verified_at"]
    assert SECRET not in json.dumps(result)
    assert not result["panel"]["ready"]  # One working dialogue model is not a multi-AI panel.


def test_http_failure_diagnostics_do_not_echo_upstream_response_or_credentials(tmp_path,monkeypatch):
    registry=ConnectionRegistry(settings(tmp_path))
    registry.update({"dialogue":{**registry.connections()["dialogue"],"api_key":SECRET}})
    patch_http(monkeypatch,lambda request:httpx.Response(401,text="server echoed "+SECRET))
    result=asyncio.run(registry.check("dialogue"))
    assert result["checks"][0]["ok"] is False
    assert result["checks"][0]["error"]=="provider_http_error HTTP 401"
    assert SECRET not in json.dumps(result)


def test_cloud_request_uses_configured_model_json_and_does_not_follow_redirects(monkeypatch):
    received=[]
    def handler(request):
        received.append(request)
        return httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"content":'{"ok":true}'}}]})
    initialized=patch_http(monkeypatch,handler)
    result=asyncio.run(JSONModelProvider(connection()).complete([{"role":"user","content":"probe"}],temperature=0))
    assert json.loads(result)=={"ok":True}
    payload=json.loads(received[0].content)
    assert payload["model"]=="distinct-ai-one" and payload["stream"] is False
    assert payload["response_format"]=={"type":"json_object"}
    assert payload["temperature"]==0 and payload["max_tokens"]==4096
    assert str(received[0].url)=="https://api.example.invalid/v1/chat/completions"
    assert initialized[0]["follow_redirects"] is False


def test_local_ollama_uses_native_endpoint_no_proxy_and_separates_final_content(monkeypatch):
    received=[]
    def handler(request):
        received.append(request)
        return httpx.Response(200,json={"done":True,"done_reason":"stop","message":{"thinking":"hidden reasoning","content":'{"ok":true}'}})
    initialized=patch_http(monkeypatch,handler)
    local=clean_connection(dict(id="j1",model="local-ai:4b",base_url="http://127.0.0.1:11434/v1",weight=1,max_tokens=4096))
    result=asyncio.run(JSONModelProvider(local).complete([{"role":"user","content":"probe"}]))
    assert json.loads(result)=={"ok":True} and "thinking" not in result
    request=received[0]
    assert str(request.url)=="http://127.0.0.1:11434/api/chat"
    body=json.loads(request.content)
    assert body["think"] is False and body["format"]=="json"
    assert body["options"]["num_predict"]==4096
    assert "authorization" not in request.headers
    assert initialized[0]["trust_env"] is False


@pytest.mark.parametrize("body", [
    {"done":False,"message":{"content":'{"ok":true}'}},
    {"done":True,"done_reason":"length","message":{"content":'{"ok":true}'}},
    {"done":True,"done_reason":"stop","message":{"thinking":"only reasoning","content":""}},
])
def test_local_partial_or_reasoning_only_replies_are_not_success(monkeypatch,body):
    patch_http(monkeypatch,lambda request:httpx.Response(200,json=body))
    local=clean_connection(dict(id="j1",model="local-ai",base_url="http://127.0.0.1:11434/v1"))
    with pytest.raises(ModelError):
        asyncio.run(JSONModelProvider(local).complete([{"role":"user","content":"probe"}]))


def test_no_credentials_in_participant_admin_material_record_or_export_responses(tmp_path):
    app=create_app(settings(tmp_path))
    client=TestClient(app)
    judges=panel_models()
    for model in judges:model["api_key"]=SECRET
    assert client.put("/api/admin/translation/models",json={"judges":judges},headers=auth()).status_code==200
    payload=dict(consent=True,university_confirmed=True,age_band="21",education_stage="bachelor",undergraduate_year="year-3",translation_experience=3,
                 pretest_version=FORM_VERSION,answers={item["id"]:item["correct"] for item in FORM_ITEMS},
                 self_answers={item["id"]:2 for item in SELF_ITEMS},request_id=str(uuid.uuid4()))
    started=client.post("/api/translation/start",json=payload)
    assert started.status_code==200
    sid=started.json()["id"]
    for path in ("/api/translation/info",f"/api/translation/session/{sid}","/api/admin/translation/models",
                 "/api/admin/translation/materials",f"/api/admin/translation/records/{sid}",
                 "/api/admin/translation/export?format=json", "/api/admin/translation/export?format=csv"):
        response=client.get(path,headers=auth())
        assert response.status_code==200,path
        assert SECRET not in response.text,path
        assert '"api_key":' not in response.text,path


def test_model_configuration_endpoints_require_auth_and_reject_cross_origin_changes(tmp_path):
    client=TestClient(create_app(settings(tmp_path)))
    assert client.get("/api/admin/translation/models").status_code==401
    assert client.put("/api/admin/translation/models",json={"judges":[]}).status_code==401
    assert client.post("/api/admin/translation/models/check",json={"target":"dialogue"}).status_code==401
    bad={**auth(),"Origin":"https://outside.example.invalid"}
    assert client.put("/api/admin/translation/models",json={"judges":[]},headers=bad).status_code==403
    assert client.post("/api/admin/translation/models/check",json={"target":"dialogue"},headers=bad).status_code==403


def test_inflight_evaluation_keeps_old_provider_generation_when_configuration_is_changed(tmp_path):
    app=create_app(settings(tmp_path,"pilot"))
    client=TestClient(app)
    configured=configure_panel(app,client)
    class Dialogue:
        async def complete(self,messages,*,temperature=None):
            return json.dumps({"kind":"answer","content":"玛拉来到钟楼宅时，罗温在门边等候，手里握着银狐徽记。"},ensure_ascii=False)
    app.state.translation_provider=Dialogue()
    payload=dict(consent=True,university_confirmed=True,age_band="21",education_stage="bachelor",undergraduate_year="year-3",translation_experience=3,
                 pretest_version=FORM_VERSION,answers={item["id"]:item["correct"] for item in FORM_ITEMS},
                 self_answers={item["id"]:2 for item in SELF_ITEMS},request_id=str(uuid.uuid4()),
                 researcher_preview=True,preview_condition="S")
    response=client.post("/api/translation/start",json=payload,headers=auth())
    assert response.status_code==200
    sid=response.json()["id"]
    assert client.post(f"/api/translation/session/{sid}/message",json={"content":"请翻译。","request_id":str(uuid.uuid4())},headers=auth()).status_code==200
    assert client.post(f"/api/translation/session/{sid}/submit",json={"submission":"玛拉来到钟楼宅时，罗温正在门边等候，手里握着那枚银狐徽记。"},headers=auth()).status_code==200
    entered=threading.Event();release=threading.Event();calls=[]
    class OldJudge:
        def __init__(self,model_id):self.model_id=model_id
        async def complete(self,messages,*,temperature=None):
            calls.append(self.model_id)
            entered.set()
            assert await asyncio.to_thread(release.wait,5)
            return json.dumps({"scores":{item["id"]:80 for item in RUBRIC},"evidence":["保持原文主要动作及人物指代。"],"critical_errors":[]},ensure_ascii=False)
    app.state.translation_judge_providers={item["id"]:OldJudge(item["id"]) for item in configured}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(client.post,f"/api/admin/translation/records/{sid}/evaluate",headers=auth())
        assert entered.wait(5)
        changed=deepcopy(configured)
        for model in changed:model["model"] += "-new-generation"
        updated=client.put("/api/admin/translation/models",json={"judges":changed},headers=auth())
        assert updated.status_code==200
        release.set()
        evaluated=future.result(timeout=10)
    assert evaluated.status_code==200,evaluated.text
    assessment=evaluated.json()
    assert len(calls)==5 and set(calls)=={item["id"] for item in configured}
    assert {item["model"] for item in assessment["ratings"]}=={item["model"] for item in configured}
    assert not any(item["model"].endswith("new-generation") for item in assessment["ratings"])
    row=client.get(f"/api/admin/translation/records/{sid}",headers=auth()).json()
    assert row["current_cohort"] is False and row["assessment"]["score"]==80
