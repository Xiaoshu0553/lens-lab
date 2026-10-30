"""Distinct-model API boundaries, private configuration and weighted panel evidence.

HTTP responses here are controlled test data, not claims of live connectivity.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import os
import stat
import uuid

from fastapi.testclient import TestClient
import httpx

from app.config import Settings
from app.main import create_app
from app.translation_connections import ConnectionRegistry, JSONModelProvider
from app.translation_quality import RUBRIC
from test_flow import auth, settings
from test_translation_flow import payload
from translation_multi_model_helpers import panel_models, configure_panel


TEXT = '玛拉来到钟楼宅时，罗温已在门边等着，掌心握着那枚小小的银狐徽记。钟响了两声，两个人谁也没动。'


def mock_http(monkeypatch, handler):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr('app.translation_connections.httpx.AsyncClient',
                        lambda **kwargs: original(transport=transport, **kwargs))


def completion(request, content):
    return httpx.Response(200, request=request, json={
        'model': json.loads(request.content)['model'],
        'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(content, ensure_ascii=False)}}]})


def begin_and_submit(client):
    started = client.post('/api/translation/start', json=payload(
        researcher_preview=True, preview_condition='A'), headers=auth())
    assert started.status_code == 200, started.text
    sid = started.json()['id']
    reply = client.post(f'/api/translation/session/{sid}/message', json={
        'content': '请翻译原文。仅对话可见 DIALOGUE_PRIVATE_MARKER。',
        'request_id': str(uuid.uuid4())}, headers=auth())
    assert reply.status_code == 200, reply.text
    submitted = client.post(f'/api/translation/session/{sid}/submit',
                            json={'submission': TEXT}, headers=auth())
    assert submitted.status_code == 200, submitted.text
    return sid


def test_private_registry_masks_keys_preserves_only_same_endpoint_and_uses_restricted_file(tmp_path):
    registry = ConnectionRegistry(settings(tmp_path, 'pilot'))
    models = panel_models()
    snapshot = registry.update({'judges': models})
    serialized = json.dumps(snapshot)
    assert snapshot['panel']['ready']
    assert all(model['api_key_configured'] for model in snapshot['judges'])
    assert all(model['api_key'] not in serialized for model in models)
    assert stat.S_IMODE(registry.path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob('.translation-models-*'))  # No atomic-write leftovers.
    edited = deepcopy(models)
    edited[0]['api_key'] = ''
    registry.update({'judges': edited})
    assert registry.connections()['judges'][0]['api_key'] == models[0]['api_key']
    edited[0]['base_url'] = 'https://changed.example.invalid/v1'
    registry.update({'judges': edited})
    assert registry.connections()['judges'][0]['api_key'] == ''
    assert not registry.snapshot()['panel']['ready']
    restored = ConnectionRegistry(settings(tmp_path, 'pilot'))
    assert restored.connections()['judges'][0]['base_url'] == edited[0]['base_url']
    assert restored.snapshot()['judges'][0]['verified_at'] is None


def test_models_configuration_requires_admin_and_draft_never_becomes_ready(tmp_path):
    client = TestClient(create_app(settings(tmp_path, 'pilot')))
    for method, path, body in [
        ('get', '/api/admin/translation/models', None),
        ('put', '/api/admin/translation/models', {'judges': panel_models()}),
        ('post', '/api/admin/translation/models/check', {'target': 'all'}),
    ]:
        response = getattr(client, method)(path, **({'json': body} if body is not None else {}))
        assert response.status_code == 401
    draft = client.put('/api/admin/translation/models', json={'judges': panel_models()[:1]}, headers=auth())
    assert draft.status_code == 200, draft.text
    assert not draft.json()['panel']['ready'] and len(draft.json()['judges']) == 1
    assert 'test-secret' not in draft.text
    duplicated = deepcopy(panel_models())
    duplicated[-1]['id'] = duplicated[0]['id']
    assert client.put('/api/admin/translation/models', json={'judges': duplicated}, headers=auth()).status_code == 422


def test_actual_http_probe_verifies_every_distinct_connection_without_exposing_credentials(tmp_path, monkeypatch):
    requests = []
    def respond(request):
        body = json.loads(request.content)
        requests.append((request, body))
        return completion(request, {'ok': True})
    mock_http(monkeypatch, respond)
    app = create_app(settings(tmp_path, 'pilot'))
    client = TestClient(app)
    configure_panel(app, client)
    before = client.get('/api/admin/translation/models', headers=auth()).json()
    assert not before['model_api_verified']
    assert all(model['verified_at'] is None for model in before['judges'])
    checked = client.post('/api/admin/translation/models/check', json={'target': 'all'}, headers=auth())
    assert checked.status_code == 200, checked.text
    data = checked.json()
    assert len(data['checks']) == 6 and all(item['ok'] for item in data['checks'])
    assert len({body['model'] for _, body in requests}) == 6
    assert data['model_api_verified'] and all(item['verified_at'] for item in data['judges'])
    assert 'test-secret' not in checked.text
    judge_requests = [(request, body) for request, body in requests if body['model'] != 'test-model']
    assert all(request.headers['authorization'].startswith('Bearer test-secret-') for request, _ in judge_requests)
    assert all(body.get('response_format') == {'type': 'json_object'} for _, body in requests)


def test_multi_model_panel_sends_identical_five_dimension_blind_prompt_and_normalizes_retained_weights(tmp_path, monkeypatch):
    requests = []
    values = {f'distinct-model-{i}': score for i, score in enumerate([10, 70, 80, 90, 100], 1)}
    def respond(request):
        body = json.loads(request.content)
        requests.append((request, body))
        if body['model'] == 'test-model':
            return completion(request, {'kind': 'answer', 'content': TEXT})
        score = values[body['model']]
        return completion(request, {'scores': {item['id']: score for item in RUBRIC},
                                    'evidence': ['译文保留原文人物与钟响两声的关系。'], 'critical_errors': []})
    mock_http(monkeypatch, respond)
    app = create_app(settings(tmp_path, 'pilot'))
    client = TestClient(app)
    models = panel_models()
    for model, weight in zip(models, [1, 1, 2, 3, 1]):
        model['weight'] = weight
    saved = client.put('/api/admin/translation/models', json={'judges': models}, headers=auth())
    assert saved.status_code == 200, saved.text
    sid = begin_and_submit(client)
    result = client.post(f'/api/admin/translation/records/{sid}/evaluate', headers=auth())
    assert result.status_code == 200, result.text
    assessment = result.json()
    assert assessment['score'] == 83.33  # (70*1 + 80*2 + 90*3) / 6.
    assert set(assessment['trimmed_model_ids']) == {'judge1', 'judge5'}
    assert len(assessment['ratings']) == 5
    judge_calls = [body for _, body in requests if body['model'] != 'test-model']
    assert len(judge_calls) == 5 and len({body['model'] for body in judge_calls}) == 5
    assert len({body['messages'][0]['content'] for body in judge_calls}) == 1
    assert len({body['messages'][1]['content'] for body in judge_calls}) == 1
    for body in judge_calls:
        prompt = body['messages'][0]['content']
        wire = json.dumps(body, ensure_ascii=False)
        assert all(item['id'] in prompt for item in RUBRIC)
        assert TEXT in body['messages'][1]['content']
        assert sid not in wire and 'DIALOGUE_PRIVATE_MARKER' not in wire
        assert not any(key in wire for key in ('ability_score', 'self_answers', 'preview_condition'))
    exported = client.get('/api/admin/translation/export?format=json&kind=records', headers=auth())
    assert 'test-secret' not in exported.text
    ratings = client.get('/api/admin/translation/export?format=json&kind=ratings', headers=auth()).json()
    assert {item['model_id'] for item in ratings} == {item['id'] for item in models}
    assert all(item['total'] == values[item['model']] for item in ratings)
    assert {item['model_id'] for item in ratings if item['trimmed']} == {'judge1', 'judge5'}


def test_unconfigured_panel_cannot_fall_back_to_repeated_calls_of_dialogue_model(tmp_path, monkeypatch):
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return completion(request, {'kind': 'answer', 'content': TEXT})
    mock_http(monkeypatch, respond)
    client = TestClient(create_app(settings(tmp_path, 'pilot')))
    sid = begin_and_submit(client)
    calls = len(requests)
    failed = client.post(f'/api/admin/translation/records/{sid}/evaluate', headers=auth())
    assert failed.status_code == 409
    assert len(requests) == calls
    row = client.get(f'/api/admin/translation/records/{sid}', headers=auth()).json()
    assert row['assessment'] is None


def test_model_configuration_changes_do_not_rebind_an_inflight_review(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from test_translation_robustness import BlockingProvider, RecordingProvider, _start, _message, _submit

    app = create_app(settings(tmp_path, 'pilot'))
    client = TestClient(app)
    judge_provider = BlockingProvider(block_judges=True)
    configure_panel(app, client, provider=judge_provider)
    app.state.translation_provider = RecordingProvider()
    sid = _start(client)
    assert _message(client, sid).status_code == 200
    _submit(client, sid)
    endpoint = f'/api/admin/translation/records/{sid}/evaluate'
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, endpoint, headers=auth())
        try:
            assert judge_provider.started.wait(3)
            replacement = panel_models()
            replacement[2]['weight'] = 7
            updated = client.put('/api/admin/translation/models', json={'judges': replacement}, headers=auth())
            assert updated.status_code in (200, 409), updated.text
        finally:
            judge_provider.release.set()
        response = pending.result(timeout=5)
    assert response.status_code == 200, response.text
    assessment = response.json()
    assert assessment['score'] == 80
    assert assessment['model_weights']['judge3'] == 1  # Frozen before this review began.
    assert len(judge_provider.calls) == 5
    row = client.get(f'/api/admin/translation/records/{sid}', headers=auth()).json()
    assert row['assessment']['evaluator_config'][2]['weight'] == 1
    if updated.status_code == 200:
        assert not row['current_cohort']
