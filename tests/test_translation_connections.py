"""Private credentials, honest HTTP probes and frozen provider-generation checks."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import hashlib
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
from app.translation_model import parse_reply
from app.translation_panel_quality import DISTINCT_MODE, REPEATED_MODE
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


def test_json_whitespace_stop_recovers_once_without_mutating_messages_or_model_configuration(monkeypatch):
    received=[]
    expected='{"kind":"answer","content":"这是模型返回的译文。"}'
    def handler(request):
        received.append(json.loads(request.content))
        content=' '*137 if len(received)==1 else expected
        return httpx.Response(200,json={"model":"distinct-ai-one","choices":[{
            "finish_reason":"stop","message":{"content":content,"reasoning_content":"不能代替最终正文"}}]})
    patch_http(monkeypatch,handler)
    provider=JSONModelProvider(connection())
    messages=[{"role":"system","content":"请输出规定结构的JSON对象。"},{"role":"user","content":"只给译文，不要解释。"}]
    original=deepcopy(messages);configuration=deepcopy(provider.connection)
    async def run():
        result=await provider.complete(messages,temperature=.25)
        assert provider.format_retry_count==1
        return result
    result=asyncio.run(run())
    assert result==expected and parse_reply(result)['action']=='answer'
    assert len(received)==2 and received[0]['messages']==original
    assert received[1]['messages'][:-1]==original
    reminder=received[1]['messages'][-1]
    assert reminder['role']=='system' and 'JSON 字符串字段' in reminder['content'] and '不要输出空白' in reminder['content']
    assert {key:value for key,value in received[0].items() if key!='messages'}=={key:value for key,value in received[1].items() if key!='messages'}
    assert received[1]['temperature']==.25 and received[1]['model']==configuration['model']
    assert messages==original and provider.connection==configuration
    assert '不能代替最终正文' not in result


def test_json_whitespace_recovery_has_no_third_format_attempt_or_reasoning_fallback(monkeypatch):
    received=[]
    def handler(request):
        received.append(request)
        return httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{
            "content":" \n\t ","reasoning_content":'{"kind":"answer","content":"不是最终正文"}'}}]})
    patch_http(monkeypatch,handler)
    provider=JSONModelProvider(connection())
    async def run():
        with pytest.raises(ModelError) as error:
            await provider.complete([{"role":"user","content":"translate"}])
        assert error.value.code=='empty_content' and provider.format_retry_count==1
    asyncio.run(run())
    assert len(received)==2


def test_additional_protocol_reminder_is_not_resent_after_a_transport_error(monkeypatch):
    received=[]
    def handler(request):
        received.append(request)
        if len(received)==1:
            return httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"content":" "}}]})
        return httpx.Response(503,text='upstream temporarily unavailable')
    patch_http(monkeypatch,handler)
    async def run():
        provider=JSONModelProvider(connection())
        with pytest.raises(ModelError) as error:
            await provider.complete([{'role':'user','content':'translate'}])
        assert error.value.code=='provider_http_error' and error.value.http_status==503
        assert provider.format_retry_count==1
    asyncio.run(run())
    assert len(received)==2


@pytest.mark.parametrize('finish,refusal,json_mode,content',[
    ('length',None,True,' '),('content_filter',None,True,' '),
    ('stop','request refused',True,' '),(None,None,True,' '),
    ('stop',None,False,' '),('stop',None,True,None),
])
def test_format_recovery_excludes_interruption_refusal_non_json_and_missing_final_content(monkeypatch,finish,refusal,json_mode,content):
    received=[]
    def handler(request):
        received.append(request)
        return httpx.Response(200,json={"choices":[{"finish_reason":finish,"message":{
            "content":content,"refusal":refusal,"reasoning_content":"reasoning is not the final answer"}}]})
    patch_http(monkeypatch,handler)
    provider=JSONModelProvider({**connection(),'json_mode':json_mode})
    async def run():
        with pytest.raises(ModelError):
            await provider.complete([{"role":"user","content":"translate"}])
        assert provider.format_retry_count==0
    asyncio.run(run())
    assert len(received)==1


def test_recovered_json_still_requires_the_application_reply_schema(monkeypatch):
    received=[]
    def handler(request):
        received.append(request)
        return httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{
            "content":" " if len(received)==1 else '{"content":"缺少kind字段。"}'}}]})
    patch_http(monkeypatch,handler)
    async def run():
        provider=JSONModelProvider(connection())
        raw=await provider.complete([{"role":"user","content":"translate"}])
        assert provider.format_retry_count==1
        with pytest.raises(ModelError) as error:
            parse_reply(raw)
        assert error.value.code=='invalid_translation_reply'
    asyncio.run(run())
    assert len(received)==2


def test_format_attempts_share_one_total_timeout_budget(monkeypatch):
    received=[];deadlines=[]
    original_timeout=asyncio.timeout
    def shortened_timeout(seconds):
        deadlines.append(seconds)
        return original_timeout(.02)
    async def handler(request):
        received.append(request)
        if len(received)==1:
            return httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"content":" "}}]})
        await asyncio.Future()  # The shared deadline must cancel this response.
    patch_http(monkeypatch,handler)
    monkeypatch.setattr('app.translation_connections.asyncio.timeout',shortened_timeout)
    async def run():
        provider=JSONModelProvider(connection())
        with pytest.raises(ModelError) as error:
            await provider.complete([{"role":"user","content":"translate"}])
        assert error.value.code=='provider_timeout' and provider.format_retry_count==1
    asyncio.run(run())
    assert deadlines==[120] and len(received)==2


def test_format_retry_diagnostics_are_isolated_for_concurrent_calls_and_reset_per_call(monkeypatch):
    async def run():
        recovering=asyncio.Event();release=asyncio.Event();received=[]
        async def handler(request):
            payload=json.loads(request.content);received.append(payload)
            if payload['messages'][0]['content']=='recover':
                if len(payload['messages'])==1:
                    return httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"content":" "}}]})
                recovering.set();await release.wait()
            else:
                await recovering.wait();release.set()
            return httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"content":'{"ok":true}'}}]})
        patch_http(monkeypatch,handler)
        provider=JSONModelProvider(connection())
        async def call(message):
            await provider.complete([{'role':'user','content':message}])
            return provider.format_retry_count
        counts=await asyncio.gather(call('recover'),call('plain'))
        assert counts==[1,0] and provider.format_retry_count==0
        assert await call('plain')==0
        assert len(received)==4
    asyncio.run(run())


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


def test_repeated_mode_clones_existing_dialogue_privately_without_overwriting_configured_judges(tmp_path):
    registry=ConnectionRegistry(settings(tmp_path,'pilot'))
    registry.update({'dialogue':{**registry.connections()['dialogue'],'model':'one-base-ai','api_key':SECRET},
                     'judges':panel_models()})
    original=registry.connections()
    result=registry.update({'evaluation_mode':REPEATED_MODE,'evaluation_repetitions':7})
    effective=registry.evaluation_connections()
    assert result['evaluation_mode']==REPEATED_MODE and result['evaluation_repetitions']==7
    assert result['panel']['ready'] and result['panel']['mode']==REPEATED_MODE
    assert result['panel']['count']==7 and result['panel']['independent_model_count']==1
    assert [item['id'] for item in effective]==[f'session-{index}' for index in range(1,8)]
    assert all(item['model']=='one-base-ai' and item['weight']==1 and item['api_key']==SECRET for item in effective)
    assert all(item['label']==f'评审会话{index}' for index,item in enumerate(effective,1))
    assert SECRET not in json.dumps(result)
    assert registry.connections()['judges']==original['judges']
    assert len(result['configured_judges'])==5
    effective[0]['api_key']='changed-only-in-return-value'
    assert registry.evaluation_connections()[0]['api_key']==SECRET
    assert registry.connections()['dialogue']['api_key']==SECRET
    restarted=ConnectionRegistry(settings(tmp_path,'pilot'))
    assert restarted.connections()['evaluation_mode']==REPEATED_MODE
    assert restarted.connections()['evaluation_repetitions']==7
    assert restarted.evaluation_connections()[0]['api_key']==SECRET
    restored=registry.update({'evaluation_mode':DISTINCT_MODE})
    assert restored['panel']['independent_model_count']==5 and len(restored['judges'])==5
    assert registry.connections()['judges']==original['judges']


@pytest.mark.parametrize('bad',[4,10,True,5.0,'5',None])
def test_repeated_session_count_is_a_strict_bounded_integer(tmp_path,bad):
    registry=ConnectionRegistry(settings(tmp_path))
    original=registry.connections()
    with pytest.raises(ValueError):
        registry.update({'evaluation_mode':REPEATED_MODE,'evaluation_repetitions':bad})
    assert registry.connections()==original


@pytest.mark.parametrize('bad',['roles','',None,True,[]])
def test_unknown_registry_evaluation_modes_cannot_silently_enable_repetition(tmp_path,bad):
    registry=ConnectionRegistry(settings(tmp_path))
    with pytest.raises(ValueError):
        registry.update({'evaluation_mode':bad})
    assert registry.connections()['evaluation_mode']==DISTINCT_MODE


def test_repeated_mode_does_not_make_remote_missing_credentials_ready(tmp_path):
    registry=ConnectionRegistry(settings(tmp_path,'pilot'))
    result=registry.update({'evaluation_mode':REPEATED_MODE})
    assert len(result['judges'])==5 and not result['panel']['ready']
    assert '密钥' in result['panel']['error']


def test_distinct_config_duplicate_model_rejection_is_not_disabled_by_active_repeated_mode(tmp_path):
    registry=ConnectionRegistry(settings(tmp_path))
    models=panel_models()
    for model in models:model['model']='one-model'
    with pytest.raises(ValueError):
        registry.update({'evaluation_mode':REPEATED_MODE,'judges':models})
    assert registry.connections()['evaluation_mode']==DISTINCT_MODE


def test_check_repeated_judges_makes_five_fresh_requests_with_one_server_side_key(tmp_path,monkeypatch):
    registry=ConnectionRegistry(settings(tmp_path,'pilot'))
    registry.update({'dialogue':{**registry.connections()['dialogue'],'model':'one-base-ai','api_key':SECRET},
                     'evaluation_mode':REPEATED_MODE})
    requests=[]
    def handler(request):
        requests.append(request)
        assert request.headers['authorization']=='Bearer '+SECRET
        return httpx.Response(200,json={'id':f'probe-{len(requests)}','model':'one-base-ai',
            'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15},
            'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]})
    patch_http(monkeypatch,handler)
    result=asyncio.run(registry.check('judges'))
    assert len(requests)==len(result['checks'])==5
    assert {item['id'] for item in result['checks']}=={f'session-{i}' for i in range(1,6)}
    payloads=[json.loads(request.content) for request in requests]
    assert all(payload['model']=='one-base-ai' and len(payload['messages'])==2 for payload in payloads)
    assert all(payload['messages']==payloads[0]['messages'] for payload in payloads)
    assert all(item['ok'] and item['reported_model']=='one-base-ai' for item in result['checks'])
    assert all(item['verified_at'] for item in result['judges'])
    assert not result['model_api_verified']  # A judges-only check is not a dialogue request.
    assert SECRET not in json.dumps(result)


def test_response_metadata_contains_only_safe_provenance_and_final_content_hash(monkeypatch):
    content='{"ok":true}'
    patch_http(monkeypatch,lambda request:httpx.Response(200,json={
        'id':'response-123','model':'actual-base','usage':{'prompt_tokens':12,'completion_tokens':4,'total_tokens':16,
            'api_key':SECRET,'input_tokens':True,'output_tokens':-1},
        'choices':[{'finish_reason':'stop','message':{'content':'  '+content+'  '}}]}))
    async def run():
        provider=JSONModelProvider(connection())
        assert await provider.complete([{'role':'user','content':'probe'}])==content
        metadata=provider.last_response_metadata
        assert metadata==provider.response_metadata
        assert metadata['id']=='response-123' and metadata['model']=='actual-base'
        assert metadata['usage']=={'prompt_tokens':12,'completion_tokens':4,'total_tokens':16}
        assert metadata['finish_reason']=='stop' and metadata['http_status']==200
        assert metadata['content_sha256']==hashlib.sha256(content.encode()).hexdigest()
        assert 'content' not in metadata and SECRET not in json.dumps(metadata)
        metadata['usage']['prompt_tokens']=999
        assert provider.last_response_metadata['usage']['prompt_tokens']==12
    asyncio.run(run())


def test_response_metadata_and_reported_model_are_isolated_for_concurrent_calls(monkeypatch):
    async def handler(request):
        marker=json.loads(request.content)['messages'][0]['content']
        await asyncio.sleep(.01 if marker=='one' else 0)
        return httpx.Response(200,json={'id':'response-'+marker,'model':'actual-'+marker,
            'usage':{'total_tokens':1 if marker=='one' else 2},
            'choices':[{'finish_reason':'stop','message':{'content':'{"marker":"'+marker+'"}'}}]})
    patch_http(monkeypatch,handler)
    async def run():
        provider=JSONModelProvider(connection())
        async def one(marker):
            content=await provider.complete([{'role':'user','content':marker}])
            await asyncio.sleep(.02 if marker=='two' else 0)
            return content,provider.last_response_metadata,provider.last_response_model
        results=await asyncio.gather(one('one'),one('two'))
        for marker,(content,metadata,reported) in zip(('one','two'),results):
            assert metadata['id']=='response-'+marker and metadata['model']==reported=='actual-'+marker
            assert metadata['content_sha256']==hashlib.sha256(content.encode()).hexdigest()
        assert provider.last_response_metadata=={} and provider.last_response_model is None
    asyncio.run(run())


def test_failed_call_drops_stale_previous_response_metadata_and_never_retains_error_body(monkeypatch):
    count=0
    def handler(request):
        nonlocal count
        count+=1
        if count==1:
            return httpx.Response(200,json={'id':'previous-response','model':'previous-model',
                'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]})
        return httpx.Response(401,text=SECRET)
    patch_http(monkeypatch,handler)
    async def run():
        provider=JSONModelProvider(connection())
        await provider.complete([{'role':'user','content':'first'}])
        assert provider.last_response_metadata['id']=='previous-response'
        with pytest.raises(ModelError):
            await provider.complete([{'role':'user','content':'second'}])
        assert provider.last_response_metadata=={'http_status':401}
        assert provider.last_response_model is None
        assert SECRET not in json.dumps(provider.last_response_metadata)
    asyncio.run(run())
