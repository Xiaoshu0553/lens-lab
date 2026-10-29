"""Translation-study privacy, assignment, submission and audit integration tests."""
from dataclasses import replace
import csv
import io
import json
import uuid

from fastapi.testclient import TestClient

from app.main import create_app
from app.translation_materials import FORM_ITEMS, SELF_ITEMS, FORM_VERSION
from test_flow import auth, settings
from translation_multi_model_helpers import configure_panel, panel_models


def payload(**changes):
    value = {
        'consent': True, 'university_confirmed': True,
        'age_band': '21-23', 'education_stage': 'bachelor',
        'translation_experience': 3, 'pretest_version': FORM_VERSION,
        'answers': {item['id']: item['correct'] for item in FORM_ITEMS},
        'self_answers': {item['id']: 2 for item in SELF_ITEMS},
        'request_id': str(uuid.uuid4()),
    }
    return {**value, **changes}


def start(client, **changes):
    response = client.post('/api/translation/start', json=payload(**changes))
    assert response.status_code == 200, response.text
    return response.json()


def admin_records(client):
    response = client.get('/api/admin/translation/records', headers=auth())
    assert response.status_code == 200, response.text
    return response.json()['records']


def dialogue(client, sid):
    response = client.post(f'/api/translation/session/{sid}/message',
                           json={'content': '请协助我把任务中的英语段落翻译成中文。',
                                 'request_id': str(uuid.uuid4())})
    assert response.status_code == 200, response.text
    return response.json()


def test_translation_form_and_session_hide_answers_condition_and_quality_keys(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    form = client.get('/api/translation/form').json()
    assert form['version'] == FORM_VERSION
    assert len(form['items']) == len(FORM_ITEMS)
    assert all('correct' not in item and 'rationale' not in item for item in form['items'])
    view = start(client)
    assert view['id'] and view['messages'] == []
    assert not {'band', 'condition', 'ability_score', 'score', 'assessment', 'answers'} & set(view)
    assert 'reference_translation' not in view['task']
    assert 'critical_points' not in view['task']
    assert view['task']['source_text'] and view['task']['context_cards']


def test_translation_ability_strata_use_same_task_and_balance_two_conditions(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    views = [start(client) for _ in range(6)]
    assert len({v['task']['id'] for v in views}) == 1
    rows = admin_records(client)
    assert {row['condition'] for row in rows} == {'S', 'A'}
    assert all(sum(row['condition'] == arm for row in rows) == 3 for arm in ['S', 'A'])
    low = start(client, answers={item['id']: len(item['options']) - 1 for item in FORM_ITEMS})
    assert low['task'] == views[0]['task']
    rows = admin_records(client)
    assert next(row for row in rows if row['id'] == low['id'])['band'] == 'low'


def test_translation_objective_score_does_not_follow_self_report(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    a = start(client, self_answers={item['id']: 0 for item in SELF_ITEMS})
    b = start(client, self_answers={item['id']: 4 for item in SELF_ITEMS})
    rows = {row['id']: row for row in admin_records(client)}
    assert rows[a['id']]['ability_score'] == rows[b['id']]['ability_score']
    assert rows[a['id']]['band'] == rows[b['id']]['band'] == 'high'


def test_translation_start_idempotency_and_conflicting_replay(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    body = payload()
    first = client.post('/api/translation/start', json=body)
    assert first.status_code == 200
    replay = client.post('/api/translation/start', json=body)
    assert replay.status_code == 200 and replay.json()['id'] == first.json()['id']
    conflicting = client.post('/api/translation/start', json={**body, 'translation_experience': 4})
    assert conflicting.status_code == 409
    assert len(admin_records(client)) == 1


def test_translation_refuses_partial_answers_and_condition_override(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    body = payload()
    body['answers'].pop(FORM_ITEMS[0]['id'])
    assert client.post('/api/translation/start', json=body).status_code == 422
    body = payload()
    body['answers'][FORM_ITEMS[0]['id']] = True
    assert client.post('/api/translation/start', json=body).status_code == 422
    response = client.post('/api/translation/start', json=payload(preview_condition='O'))
    assert response.status_code in (403, 422)
    assert admin_records(client) == []


def test_translation_draft_resume_submission_freeze_and_delete(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    sid = start(client)['id']
    draft = '码头的钟响了两声。玛拉把那枚银制徽记放在手掌里，一时没有说话。'
    saved = client.put(f'/api/translation/session/{sid}/draft', json={'draft': draft})
    assert saved.status_code == 200 and saved.json()['draft'] == draft
    assert client.get(f'/api/translation/session/{sid}').json()['draft'] == draft
    dialogue(client, sid)
    submitted = client.post(f'/api/translation/session/{sid}/submit',
                            json={'submission': draft, 'burden': 2, 'helpfulness': 4})
    assert submitted.status_code == 200 and submitted.json()['ended_at']
    assert submitted.json()['submission'] == draft
    assert submitted.json()['assessment_status'] == 'demo_unscored'
    assert client.put(f'/api/translation/session/{sid}/draft', json={'draft': '修改草稿'}).status_code == 409
    assert client.post(f'/api/translation/session/{sid}/submit',
                       json={'submission': draft + '追加句子。'}).status_code == 409
    assert client.post(f'/api/translation/session/{sid}/message',
                       json={'content': '继续翻译', 'request_id': str(uuid.uuid4())}).status_code == 409
    assert client.post(f'/api/admin/translation/records/{sid}/evaluate', headers=auth()).status_code == 409
    assert client.delete(f'/api/translation/session/{sid}').status_code == 200
    assert client.get(f'/api/translation/session/{sid}').status_code == 404
    assert admin_records(client) == []


def test_translation_preview_requires_admin_and_is_excluded_from_summary(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    body = payload(researcher_preview=True, preview_condition='A')
    assert client.post('/api/translation/start', json=body).status_code == 401
    created = client.post('/api/translation/start', json=body, headers=auth())
    assert created.status_code == 200
    rows = admin_records(client)
    assert rows[0]['record_kind'] == 'researcher_preview' and rows[0]['condition'] == 'A'
    summary = client.get('/api/admin/translation/summary', headers=auth()).json()
    assert summary['counts']['previews'] == 1
    assert summary['counts']['total'] == 0
    assert all(cell['n'] == 0 and cell['mean_score'] is None for cell in summary['cells'])


def test_translation_admin_api_is_protected_and_public_info_has_no_keys(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    for endpoint in ['summary', 'records', 'materials', 'export?format=json&kind=records']:
        assert client.get('/api/admin/translation/' + endpoint).status_code == 401
    info = client.get('/api/translation/info')
    assert info.status_code == 200
    assert not {'model_api_key', 'admin_password', 'assignment_seed', 'model_base_url'} & set(info.json())
    assert 'test-password' not in info.text
    assert info.json()['dialogue_backend'] == 'rule_simulation'
    assert info.json()['evaluation_backend'] == 'disabled_demo'
    assert info.json()['model_api_verified'] is False


def test_translation_live_start_waits_for_versioned_material_freeze(tmp_path):
    cfg = replace(settings(tmp_path, 'live'), participant_access_code='join-live')
    app = create_app(cfg)
    client = TestClient(app)
    body = payload(access_code='join-live')
    assert client.post('/api/translation/start', json=body).status_code == 409
    assert client.post('/api/admin/translation/materials/freeze', json={'confirmed': True}, headers=auth()).status_code == 409
    configure_panel(app, client)
    freeze = client.post('/api/admin/translation/materials/freeze', json={'confirmed': True}, headers=auth())
    assert freeze.status_code == 200 and freeze.json()['ready']
    assert client.post('/api/translation/start', json=body).status_code == 200
    assert client.post('/api/translation/start', json=payload(access_code='wrong')).status_code == 403


def test_translation_exports_keep_quoted_text_and_neutralize_csv_formulas(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    sid = start(client)['id']
    dialogue(client, sid)
    text = '=HYPERLINK("https://example.invalid")\n这是保留中文、逗号和引号的译文文本。'
    assert client.post(f'/api/translation/session/{sid}/submit', json={'submission': text}).status_code == 200
    exported = client.get('/api/admin/translation/export?format=json&kind=records', headers=auth())
    assert exported.status_code == 200
    def values(value):
        if isinstance(value, dict):
            return [v for item in value.values() for v in values(item)]
        if isinstance(value, list):
            return [v for item in value for v in values(item)]
        return [value]
    assert text in values(exported.json())
    response = client.get('/api/admin/translation/export?format=csv&kind=records', headers=auth())
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
    assert len(rows) == 1
    assert not any(value and value.startswith('=HYPERLINK') for value in rows[0].values())


class RecordingTranslationProvider:
    def __init__(self, *, fail_message=False, fail_judge=False):
        self.calls = []
        self.fail_message = fail_message
        self.fail_judge = fail_judge

    async def complete(self, messages, *, temperature=None):
        self.calls.append(messages)
        prompt = messages[0]['content']
        if '你是英译中成稿的盲评模型' in prompt:
            if self.fail_judge:
                self.fail_judge = False
                return '{"scores": {"accuracy": 99}}'
            return json.dumps({'scores': {'accuracy': 85, 'context': 80, 'fluency': 90,
                                           'style': 80, 'completeness': 90},
                               'evidence': ['语义与原文对应，人物设定一致。'], 'critical_errors': []},
                              ensure_ascii=False)
        if '你是翻译语境检查模块' in prompt:
            return json.dumps({'gaps': [], 'ready': True})
        if self.fail_message:
            self.fail_message = False
            raise RuntimeError('simulated outage')
        return json.dumps({'content': '这是根据任务原文形成的中文译文，保留原文的叙述和人物关系。', 'kind': 'answer'},
                          ensure_ascii=False)


def pilot_preview(tmp_path, *, provider=None, condition='S'):
    app = create_app(settings(tmp_path, 'pilot'))
    provider = provider or RecordingTranslationProvider()
    client = TestClient(app)
    configure_panel(app, client, provider=provider)
    app.state.translation_provider = provider
    response = client.post('/api/translation/start',
                           json=payload(researcher_preview=True, preview_condition=condition),
                           headers=auth())
    assert response.status_code == 200, response.text
    return client, response.json()['id'], provider


def pilot_message(client, sid, *, request_id=None, text='请帮我翻译原文。'):
    return client.post(f'/api/translation/session/{sid}/message',
                       json={'content': text, 'request_id': request_id or str(uuid.uuid4())}, headers=auth())


def test_translation_live_model_message_replay_and_history_preserve_original_request(tmp_path):
    client, sid, provider = pilot_preview(tmp_path)
    request_id = str(uuid.uuid4())
    first = pilot_message(client, sid, request_id=request_id)
    assert first.status_code == 200
    repeated = pilot_message(client, sid, request_id=request_id)
    assert repeated.status_code == 200 and repeated.json()['messages'] == first.json()['messages']
    assert len(provider.calls) == 1
    assert pilot_message(client, sid, request_id=request_id, text='不同内容').status_code == 409
    second = pilot_message(client, sid, text='请检查语气。')
    assert second.status_code == 200
    assert provider.calls[1][1:3] == [
        {'role': 'user', 'content': '请帮我翻译原文。'},
        {'role': 'assistant', 'content': first.json()['messages'][1]['content']},
    ]


def test_translation_failed_model_turn_can_retry_without_duplicate_or_partial_messages(tmp_path):
    provider = RecordingTranslationProvider(fail_message=True)
    client, sid, _ = pilot_preview(tmp_path, provider=provider)
    request_id = str(uuid.uuid4())
    failed = pilot_message(client, sid, request_id=request_id)
    assert failed.status_code == 502
    recovered = client.get(f'/api/translation/session/{sid}', headers=auth()).json()
    assert recovered['messages'] == [] and not recovered['pending_reply']
    retried = pilot_message(client, sid, request_id=request_id)
    assert retried.status_code == 200 and len(retried.json()['messages']) == 2
    assert len(provider.calls) == 2


def test_translation_baseline_has_two_conditions_and_no_optimizer_calls(tmp_path):
    from app.translation_materials import TASK
    client, sid, provider = pilot_preview(tmp_path, condition='A')
    response = pilot_message(client, sid)
    assert response.status_code == 200 and len(provider.calls) == 1
    prompt = provider.calls[0][0]['content']
    assert '主动澄清' in prompt and '你是翻译语境检查模块' not in prompt
    assert TASK['source_text'] in prompt and TASK['reference_translation'] not in prompt
    assert all(card['body'] not in prompt for card in TASK['context_cards'])
    detailed = client.get(f'/api/admin/translation/records/{sid}', headers=auth()).json()
    assert not detailed['events'][-1].get('optimizer')
    info = client.get('/api/translation/info').json()
    assert info['study_phase'] == 'baseline_fairness'
    assert set(info['conditions']) == {'S', 'A'}
    assert info['dialogue_backend'] == 'model_api'
    assert not info['model_api_verified']  # A fake provider is not a real connectivity check.
    assert 'optimizer' not in info['versions']
    rejected = client.post('/api/translation/start', json=payload(researcher_preview=True, preview_condition='O'), headers=auth())
    assert rejected.status_code == 422
    summary = client.get('/api/admin/translation/summary', headers=auth()).json()
    assert len(summary['cells']) == 6 and {item['condition'] for item in summary['gaps']} == {'S', 'A'}
    assert all('optimization_gain' not in item for item in summary['effects'])


def test_translation_panel_blinds_participant_metadata_and_freezes_complete_ratings(tmp_path):
    client, sid, provider = pilot_preview(tmp_path)
    assert pilot_message(client, sid).status_code == 200
    text = '玛拉来到钟楼宅时，罗温正等在门边，手里拿着小小的银狐徽记。钟响了两声，两人都没有移动。'
    assert client.post(f'/api/translation/session/{sid}/submit', json={'submission': text}, headers=auth()).status_code == 200
    assessed = client.post(f'/api/admin/translation/records/{sid}/evaluate', headers=auth())
    assert assessed.status_code == 200, assessed.text
    result = assessed.json()
    assert result['score'] == 84.25
    assert len(result['ratings']) == 5
    assert len(result['trimmed_model_ids']) == 2
    judge_calls = provider.calls[1:]
    assert len(judge_calls) == 5
    for call in judge_calls:
        assert len(call) == 2
        assert text in call[1]['content']
        assert sid not in json.dumps(call, ensure_ascii=False)
        assert not any(key in json.dumps(call, ensure_ascii=False) for key in ['ability_score', 'self_answers', 'preview_condition'])
        assert '请帮我翻译原文。' not in json.dumps(call, ensure_ascii=False)
    repeated = client.post(f'/api/admin/translation/records/{sid}/evaluate', headers=auth())
    assert repeated.status_code == 200 and repeated.json() == result
    assert len(provider.calls) == 6
    detailed = client.get(f'/api/admin/translation/records/{sid}', headers=auth()).json()
    assert detailed['assessment_status'] == 'completed' and detailed['assessment']['ratings'] == result['ratings']
    ratings_json = client.get('/api/admin/translation/export?format=json&kind=ratings', headers=auth())
    assert ratings_json.status_code == 200
    exported = ratings_json.json()
    assert len(exported) == 5 and {item['model_id'] for item in exported} == {e['id'] for e in panel_models()}
    assert all(item['total'] == result['model_totals'][item['model_id']] for item in exported)
    assert {item['model_id'] for item in exported if item['trimmed']} == set(result['trimmed_model_ids'])
    assert all(item['record_kind'] == 'researcher_preview' and item['study_mode'] == 'pilot' for item in exported)
    ratings_csv = client.get('/api/admin/translation/export?format=csv&kind=ratings', headers=auth())
    flat = list(csv.DictReader(io.StringIO(ratings_csv.content.decode('utf-8-sig'))))
    assert len(flat) == 5 and all(float(item['total']) == 84.25 for item in flat)


def test_translation_incomplete_panel_does_not_publish_a_partial_average(tmp_path):
    client, sid, provider = pilot_preview(tmp_path, provider=RecordingTranslationProvider(fail_judge=True))
    assert pilot_message(client, sid).status_code == 200
    assert client.post(f'/api/translation/session/{sid}/submit',
                       json={'submission': '这是已经提交的最终译文。保留人物关系及原文信息，等待统一模型评分。'}, headers=auth()).status_code == 200
    first = client.post(f'/api/admin/translation/records/{sid}/evaluate', headers=auth())
    assert first.status_code == 502
    row = client.get(f'/api/admin/translation/records/{sid}', headers=auth()).json()
    assert row['assessment'] is None and row['submission']
    retried = client.post(f'/api/admin/translation/records/{sid}/evaluate', headers=auth())
    assert retried.status_code == 200 and len(retried.json()['ratings']) == 5
