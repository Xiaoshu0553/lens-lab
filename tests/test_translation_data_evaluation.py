"""API integration with controlled HTTP responses, never live model evidence.

The real JSONModelProvider parses the mock transport responses, so response
identity, metadata, errors and independent request contexts cross the production
boundary.  No test calls an external API or creates participant research data.
"""
from __future__ import annotations

from copy import deepcopy
import csv
from dataclasses import replace
from datetime import datetime
import hashlib
import io
import json
import uuid

from fastapi.testclient import TestClient
import httpx
import pytest

from app.main import create_app
from app.translation_connections import JSONModelProvider
from app.translation_quality import RUBRIC
from test_flow import auth, settings
from test_translation_flow import payload
from translation_multi_model_helpers import panel_models


TEXT = '玛拉抵达钟楼宅时，罗温已在门边等候，掌心攥着那枚小小的银狐徽记。钟响了两声，两个人谁也没有动。'


class Wire:
    def __init__(self):
        self.judge_calls = []
        self.dialogue_calls = []
        self.fail_call_numbers = set()
        self.identity = 'normal'

    def __call__(self, request):
        body = json.loads(request.content)
        judging = (len(body['messages']) == 2
                   and body['messages'][1]['content'].startswith('以下为待评译文数据'))
        if judging:
            self.judge_calls.append(deepcopy(body))
            number = len(self.judge_calls)
            if number in self.fail_call_numbers:
                return httpx.Response(400, request=request, json={'error': {'message': 'offline fixture error'}})
            content = {'scores': {dimension['id']: 80 for dimension in RUBRIC},
                       'evidence': ['译文保留人物指代、物件和两次钟声之间的关系。'], 'critical_errors': []}
        else:
            self.dialogue_calls.append(deepcopy(body))
            number = len(self.dialogue_calls)
            content = {'kind': 'answer', 'content': TEXT}
        response = {'id': f'offline-completion-{number}', 'model': body['model'],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30},
                    'choices': [{'finish_reason': 'stop', 'message': {
                        'content': json.dumps(content, ensure_ascii=False)}}]}
        if judging and self.identity == 'missing':
            del response['model']
        elif judging and self.identity == 'repeated_conflict' and number == 5:
            response['model'] = 'a-different-routed-model'
        elif judging and self.identity == 'distinct_collision':
            response['model'] = 'same-actual-model-behind-all-aliases'
        return httpx.Response(200, request=request, json=response)


@pytest.fixture
def api(tmp_path, monkeypatch):
    wire = Wire()
    transport = httpx.MockTransport(wire)
    original = httpx.AsyncClient
    def factory(**kwargs):
        return original(transport=transport, **kwargs)
    monkeypatch.setattr('app.translation_connections.httpx.AsyncClient', factory)
    config = replace(settings(tmp_path, 'pilot'), translation_showcase_mode=True,
                     model_api_key='offline-test-key')
    app = create_app(config)
    with TestClient(app) as client:
        yield app, client, wire


def _get(client, path):
    response = client.get(path, headers=auth())
    assert response.status_code == 200, response.text
    return response.json()


def _record(client, sid):
    return _get(client, f'/api/admin/translation/records/{sid}')


def _summary(client):
    return _get(client, '/api/admin/translation/summary')


def _materials(client):
    return _get(client, '/api/admin/translation/materials')


def _complete(client, *, preview=False, **extra):
    started = client.post('/api/translation/start', json=payload(researcher_preview=preview, **extra),
                          **({'headers': auth()} if preview else {}))
    assert started.status_code == 200, started.text
    sid = started.json()['id']
    headers = auth() if preview else {}
    reply = client.post(f'/api/translation/session/{sid}/message', headers=headers, json={
        'content': '请翻译原文。仅对话可见 DIALOGUE_PRIVATE_MARKER。', 'request_id': str(uuid.uuid4())})
    assert reply.status_code == 200, reply.text
    submitted = client.post(f'/api/translation/session/{sid}/submit', headers=headers, json={'submission': TEXT})
    assert submitted.status_code == 200, submitted.text
    return sid


def _repeat(client, repetitions=5):
    response = client.put('/api/admin/translation/models', headers=auth(), json={
        'evaluation_mode': 'repeated_sessions', 'evaluation_repetitions': repetitions})
    assert response.status_code == 200, response.text
    assert response.json()['panel']['ready']
    assert len(response.json()['judges']) == repetitions
    return response.json()


def _body(client, sid, *, batch=None, allow_reassessment=False):
    row = _record(client, sid)
    return {'evaluation_batch_id': batch or _summary(client)['evaluation_batch_id'],
            'submission_sha256': hashlib.sha256(row['submission'].encode()).hexdigest(),
            'allow_reassessment': allow_reassessment}


def _evaluate(client, sid, body):
    return client.post(f'/api/admin/translation/records/{sid}/evaluate', headers=auth(), json=body)


def test_24_collected_sessions_can_use_explicit_same_model_five_session_panel_without_reassignment(api):
    app, client, wire = api
    before = _materials(client)
    assert before['models'] == []
    sids = [_complete(client) for _ in range(24)]
    originals = {sid: _record(client, sid) for sid in sids}
    initial_dialogue = _get(client, '/api/admin/translation/models')['dialogue']
    _repeat(client)
    assert _get(client, '/api/admin/translation/models')['dialogue'] == initial_dialogue
    assert _materials(client)['cohort_id'] == before['cohort_id']
    providers = list(app.state.translation_judge_providers.values())
    assert len(providers) == len({id(provider) for provider in providers}) == 5
    assert all(isinstance(provider, JSONModelProvider) for provider in providers)
    batch = _summary(client)['evaluation_batch_id']
    for sid in sids:
        response = _evaluate(client, sid, _body(client, sid, batch=batch))
        assert response.status_code == 200, response.text
        assessment = response.json()
        assert assessment['evaluation_mode'] == 'repeated_sessions'
        assert assessment['evaluation_batch_id'] == batch
        assert assessment['response_identity_status'] == 'complete'
        assert assessment['reported_model_count'] == 1
        assert len(assessment['ratings']) == 5
        assert len({rating['model_id'] for rating in assessment['ratings']}) == 5
        assert all(set(rating['scores']) == {dimension['id'] for dimension in RUBRIC}
                   for rating in assessment['ratings'])
        for key in ('cohort_id', 'condition', 'band', 'ability', 'answers', 'self_answers',
                    'created_at', 'ended_at', 'submission', 'material_snapshot'):
            assert _record(client, sid)[key] == originals[sid][key]
    assert len(wire.dialogue_calls) == 24 and len(wire.judge_calls) == 120
    assert {body['model'] for body in wire.judge_calls} == {'test-model'}
    assert all(body['temperature'] == 0 and len(body['messages']) == 2 for body in wire.judge_calls)
    assert len({body['messages'][0]['content'] for body in wire.judge_calls}) == 1
    assert len({body['messages'][1]['content'] for body in wire.judge_calls}) == 1
    for body in wire.judge_calls:
        prompt = body['messages'][0]['content']
        serialized = json.dumps(body, ensure_ascii=False)
        assert all(dimension['id'] in prompt for dimension in RUBRIC)
        assert TEXT in body['messages'][1]['content']
        assert 'DIALOGUE_PRIVATE_MARKER' not in serialized
        assert not any(key in serialized for key in ('ability_score', 'self_answers', 'preview_condition'))
    assert _summary(client)['counts']['scored'] == 24


@pytest.mark.parametrize('field', ['evaluation_batch_id', 'submission_sha256'])
def test_expected_batch_and_submission_guards_fail_without_calling_any_judge(api, field):
    _, client, wire = api
    sid = _complete(client)
    _repeat(client)
    body = _body(client, sid)
    body[field] = '0' * 64 if body[field] != '0' * 64 else '1' * 64
    response = _evaluate(client, sid, body)
    assert response.status_code == 409, response.text
    assert wire.judge_calls == []
    row = _record(client, sid)
    assert row['assessment'] is None and row['assessment_status'] == 'pending'
    assert not any(event['type'] == 'evaluation_started' for event in row['events'])


def test_completed_exact_batch_is_idempotent_without_new_requests(api):
    _, client, wire = api
    sid = _complete(client)
    _repeat(client)
    body = _body(client, sid)
    first = _evaluate(client, sid, body)
    assert first.status_code == 200, first.text
    assert len(wire.judge_calls) == 5
    for _ in range(2):
        repeated = _evaluate(client, sid, body)
        assert repeated.status_code == 200 and repeated.json() == first.json()
    assert len(wire.judge_calls) == 5
    assert not _record(client, sid).get('assessment_history')


def test_failed_exact_batch_keeps_partial_evidence_and_retries_only_failed_session(api):
    _, client, wire = api
    sid = _complete(client)
    _repeat(client)
    body = _body(client, sid)
    wire.fail_call_numbers = {3}
    failed = _evaluate(client, sid, body)
    assert failed.status_code == 502, failed.text
    assert len(wire.judge_calls) == 5
    row = _record(client, sid)
    assert row['assessment_status'] == 'failed' and row['assessment'] is None
    assert row['evaluation_pending'] is None
    failure = next(event for event in reversed(row['events']) if event['type'] == 'evaluation_failed')
    assert failure['evaluation_batch_id'] == body['evaluation_batch_id']
    assert failure['submission_sha256'] == body['submission_sha256']
    assert len(failure['partial_ratings']) == 4 and len(failure['failures']) == 1
    assert failure['failures'][0]['code'] == 'provider_http_error'
    assert failure['failures'][0]['http_status'] == 400
    assert _summary(client)['counts']['scored'] == 0
    retried = _evaluate(client, sid, body)
    assert retried.status_code == 200, retried.text
    assert len(wire.judge_calls) == 6
    assert sum(rating['reused'] for rating in retried.json()['ratings']) == 4
    completion = next(event for event in reversed(_record(client, sid)['events']) if event['type'] == 'evaluation_completed')
    assert completion['reused_results'] == 4 and completion['new_calls'] == 1
    assert _evaluate(client, sid, body).json() == retried.json()
    assert len(wire.judge_calls) == 6


def test_partial_results_are_never_reused_after_batch_changes(api):
    _, client, wire = api
    sid = _complete(client)
    _repeat(client)
    old = _body(client, sid)
    wire.fail_call_numbers = {3}
    assert _evaluate(client, sid, old).status_code == 502
    assert len(wire.judge_calls) == 5
    _repeat(client, repetitions=7)
    current = _body(client, sid)
    assert current['evaluation_batch_id'] != old['evaluation_batch_id']
    assert _evaluate(client, sid, old).status_code == 409
    assert len(wire.judge_calls) == 5
    response = _evaluate(client, sid, current)
    assert response.status_code == 200, response.text
    assert len(wire.judge_calls) == 12
    assert len(response.json()['ratings']) == 7
    assert not any(rating['reused'] for rating in response.json()['ratings'])


@pytest.mark.parametrize('identity', ['missing', 'repeated_conflict'])
def test_retry_discards_partial_ratings_without_valid_real_response_identity(api, identity):
    _, client, wire = api
    sid = _complete(client)
    _repeat(client)
    body = _body(client, sid)
    wire.identity = identity
    wire.fail_call_numbers = {3}
    first = _evaluate(client, sid, body)
    assert first.status_code == 502 and len(wire.judge_calls) == 5
    assert _record(client, sid)['assessment'] is None
    wire.identity = 'normal'
    retried = _evaluate(client, sid, body)
    assert retried.status_code == 200, retried.text
    assert len(wire.judge_calls) == 10
    assert not any(rating['reused'] for rating in retried.json()['ratings'])
    assert retried.json()['response_identity_status'] == 'complete'


def test_new_batch_requires_explicit_reassessment_preserves_history_and_filters_summary(api):
    _, client, wire = api
    sid = _complete(client)
    original = _record(client, sid)
    _repeat(client)
    first_body = _body(client, sid)
    first = _evaluate(client, sid, first_body)
    assert first.status_code == 200, first.text
    assert _summary(client)['counts']['scored'] == 1
    _repeat(client, repetitions=7)
    assert _materials(client)['cohort_id'] == original['cohort_id']
    current_body = _body(client, sid)
    assert current_body['evaluation_batch_id'] != first_body['evaluation_batch_id']
    summary = _summary(client)
    assert summary['counts']['submitted'] == 1 and summary['counts']['scored'] == 0
    assert all(cell['mean_score'] is None for cell in summary['cells'])
    assert _record(client, sid)['assessment'] == first.json()
    blocked = _evaluate(client, sid, current_body)
    assert blocked.status_code == 409 and len(wire.judge_calls) == 5
    current_body['allow_reassessment'] = True
    second = _evaluate(client, sid, current_body)
    assert second.status_code == 200, second.text
    assert len(wire.judge_calls) == 12
    stored = _record(client, sid)
    assert stored['assessment'] == second.json()
    assert stored['assessment_history'] == [first.json()]
    assert stored['submission'] == original['submission'] and stored['created_at'] == original['created_at']
    assert _summary(client)['counts']['scored'] == 1
    assert _evaluate(client, sid, current_body).json() == second.json()
    assert len(wire.judge_calls) == 12
    # Returning to a previously completed protocol selects its retained result,
    # rather than paying for a third evaluation with the same (session, batch).
    _repeat(client, repetitions=5)
    restored = _body(client, sid)
    assert restored['evaluation_batch_id'] == first_body['evaluation_batch_id']
    assert _summary(client)['counts']['scored'] == 1
    active = _record(client, sid)
    assert active['assessment'] == second.json()  # Most recently completed audit remains intact.
    assert active['active_assessment'] == first.json()
    assert active['current_evaluation_batch'] is True
    assert _evaluate(client, sid, restored).json() == first.json()
    assert len(wire.judge_calls) == 12
    assert _record(client, sid)['assessment_history'] == [first.json()]


@pytest.mark.parametrize('mode,identity', [
    ('repeated_sessions', 'missing'), ('repeated_sessions', 'repeated_conflict'),
    ('distinct_models', 'distinct_collision'),
])
def test_real_provider_missing_or_conflicting_response_identity_never_creates_a_total(api, mode, identity):
    app, client, wire = api
    sid = _complete(client)
    if mode == 'repeated_sessions':
        _repeat(client)
    else:
        configured = client.put('/api/admin/translation/models', headers=auth(), json={'judges': panel_models()})
        assert configured.status_code == 200 and configured.json()['panel']['ready']
    assert all(isinstance(provider, JSONModelProvider) for provider in app.state.translation_judge_providers.values())
    wire.identity = identity
    response = _evaluate(client, sid, _body(client, sid))
    assert response.status_code == 409, response.text
    assert len(wire.judge_calls) == 5
    row = _record(client, sid)
    assert row['assessment'] is None and row['assessment_status'] == 'failed'
    assert row['evaluation_pending'] is None
    failure = next(event for event in reversed(row['events']) if event['type'] == 'evaluation_failed')
    assert failure['identity_failure'] and failure['partial_ratings'] == []
    assert _summary(client)['counts']['scored'] == 0


def test_data_audit_and_technical_exports_keep_source_separate_from_purpose_and_duration(api):
    _, client, _ = api
    sid = _complete(client, provenance={'version': 1, 'collection_method': 'automated_script',
                                      'batch_id': 'forged-client-origin', 'evidence': []})
    preview = _complete(client, preview=True)
    assert client.get('/api/admin/translation/data-audit').status_code == 401
    audit = _get(client, '/api/admin/translation/data-audit')
    assert audit['counts']['records'] == 2
    assert audit['counts']['data_purpose'] == {'demonstration': 2}
    assert audit['counts']['collection_method'] == {'unverified': 1, 'researcher_preview': 1}
    assert 'not the whole questionnaire' in audit['time_semantics']['duration']
    row = _record(client, sid)
    assert row['collection_method'] == 'unverified' and row['collection_batch_id'] is None
    assert row['provenance']['collection_method'] == 'unverified'
    assert row['duration_scope'] == 'translation_session_after_questionnaire'
    actual = (datetime.fromisoformat(row['ended_at']) - datetime.fromisoformat(row['created_at'])).total_seconds()
    assert row['duration_seconds'] == round(actual, 2)
    exported = _get(client, '/api/admin/translation/export?format=json&kind=records')
    indexed = {item['id']: item for item in exported}
    assert indexed[sid]['collection_method'] == 'unverified'
    assert indexed[preview]['collection_method'] == 'researcher_preview'
    assert indexed[sid]['duration_scope'] == row['duration_scope']
    csv_response = client.get('/api/admin/translation/export?format=csv&kind=records', headers=auth())
    assert csv_response.status_code == 200
    csv_rows = list(csv.DictReader(io.StringIO(csv_response.text.lstrip('\ufeff'))))
    csv_indexed = {item['id']: item for item in csv_rows}
    assert csv_indexed[sid]['collection_method'] == 'unverified'
    assert csv_indexed[sid]['collection_batch_id'] == ''
    assert csv_indexed[sid]['duration_scope'] == row['duration_scope']
    assert json.loads(csv_indexed[sid]['provenance']) == row['provenance']
    assert 'offline-test-key' not in csv_response.text + json.dumps(exported)
