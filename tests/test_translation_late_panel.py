"""Late panel setup uses controlled model doubles, never fabricated API evidence."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
import threading
import uuid

from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app
from app.translation import _hash
from app.translation_quality import RUBRIC
from test_flow import auth, settings
from test_translation_flow import payload
from translation_multi_model_helpers import panel_models


TEXT = '玛拉抵达钟楼宅时，罗温已在门边等候，手里攥着那枚小小的银狐徽记。钟响了两声，两人谁也没有动。'


class Dialogue:
    async def complete(self, messages, *, temperature=None):
        return json.dumps({'kind': 'answer', 'content': TEXT}, ensure_ascii=False)


class Judge:
    def __init__(self, model_id, calls, *, entered=None, release=None, fail=False):
        self.model_id = model_id
        self.calls = calls
        self.entered = entered
        self.release = release
        self.fail = fail

    async def complete(self, messages, *, temperature=None):
        self.calls.append((self.model_id, deepcopy(messages), temperature))
        if self.entered is not None:
            import asyncio
            self.entered.set()
            assert await asyncio.to_thread(self.release.wait, 5)
        if self.fail:
            return '{"scores": {"accuracy": 80}}'
        return json.dumps({'scores': {dimension['id']: 80 for dimension in RUBRIC},
            'evidence': ['译文保留人物指代与钟响两声的动作关系。'], 'critical_errors': []}, ensure_ascii=False)


def materials(client):
    response = client.get('/api/admin/translation/materials', headers=auth())
    assert response.status_code == 200
    return response.json()


def summary(client):
    response = client.get('/api/admin/translation/summary', headers=auth())
    assert response.status_code == 200
    return response.json()


def record(client, sid):
    response = client.get(f'/api/admin/translation/records/{sid}', headers=auth())
    assert response.status_code == 200
    return response.json()


def complete_task(app, client):
    app.state.translation_provider = Dialogue()
    response = client.post('/api/translation/start', json=payload())
    assert response.status_code == 200, response.text
    sid = response.json()['id']
    response = client.post(f'/api/translation/session/{sid}/message', json={
        'content': '请将任务中的英语段落译为中文。', 'request_id': str(uuid.uuid4())})
    assert response.status_code == 200, response.text
    assert client.put(f'/api/translation/session/{sid}/draft', json={'draft': TEXT}).status_code == 200
    response = client.post(f'/api/translation/session/{sid}/submit', json={'submission': TEXT})
    assert response.status_code == 200, response.text
    return sid


def set_panel(app, client, *, models=None, calls=None, **judge_options):
    models = panel_models() if models is None else models
    response = client.put('/api/admin/translation/models', json={'judges': models}, headers=auth())
    assert response.status_code == 200, response.text
    assert response.json()['panel']['ready']
    calls = [] if calls is None else calls
    app.state.translation_judge_providers = {
        model['id']: Judge(model['id'], calls, **judge_options) for model in models}
    return calls


def evaluate(client, sid):
    return client.post(f'/api/admin/translation/records/{sid}/evaluate', headers=auth())


def test_24_completed_samples_keep_original_empty_panel_cohort_and_can_be_scored_after_setup(tmp_path):
    cfg = replace(settings(tmp_path, 'pilot'), translation_showcase_mode=True)
    app = create_app(cfg)
    with TestClient(app) as client:
        before = materials(client)
        assert before['models'] == []
        sids = [complete_task(app, client) for _ in range(24)]
        original = record(client, sids[0])
        original_snapshot = deepcopy(original['material_snapshot'])
        # This is the exact pre-fix cohort formula, not a new compatibility ID.
        legacy_id = _hash(dict(material_id=_hash(original_snapshot), model=original['model_config'],
            mode=original['study_mode'], limits=original['limits'], data_purpose='demonstration'))
        assert legacy_id == original['cohort_id'] == before['cohort_id']
        assert summary(client)['evaluation_batch_id'] is None
        assert evaluate(client, sids[0]).status_code == 409
        calls = set_panel(app, client)
        after = materials(client)
        assert after['cohort_id'] == legacy_id
        assert after['material_id'] != before['material_id']
        assert summary(client)['counts']['total'] == summary(client)['counts']['submitted'] == 24
        batch = summary(client)['evaluation_batch_id']
        assert batch is not None
        for sid in sids:
            response = evaluate(client, sid)
            assert response.status_code == 200, response.text
            assessment = response.json()
            assert assessment['evaluation_batch_id'] == batch
            assert len(assessment['ratings']) == 5
            assert all(set(rating['scores']) == {dimension['id'] for dimension in RUBRIC}
                       for rating in assessment['ratings'])
            assert 'api_key' not in json.dumps(assessment) and 'test-secret' not in json.dumps(assessment)
        assert len(calls) == 24 * 5
        assert {call[0] for call in calls} == {f'judge{i}' for i in range(1, 6)}
        assert {call[1][0]['content'] for call in calls} == {original_snapshot['scoring_prompt']}
        assert all(call[2] == 0 for call in calls)
        assert record(client, sids[0])['material_snapshot'] == original_snapshot
        assert summary(client)['counts']['scored'] == 24
        old_assessment = record(client, sids[0])['assessment']
        replacement = panel_models()
        replacement[2]['weight'] = 7
        set_panel(app, client, models=replacement)
        changed = summary(client)
        assert changed['cohort_id'] == legacy_id and changed['evaluation_batch_id'] != batch
        assert changed['counts']['total'] == changed['counts']['submitted'] == 24
        assert changed['counts']['scored'] == changed['counts']['needs_review'] == 0
        assert all(cell['mean_score'] is None and cell['scored'] == 0 for cell in changed['cells'])
        assert all(effect['clarification_gain'] is None for effect in changed['effects'])
        assert evaluate(client, sids[0]).json() == old_assessment
        assert len(calls) == 120  # No silent overwrite or automatic re-evaluation.
        assert client.put('/api/admin/translation/models', json={'judges': []}, headers=auth()).status_code == 200
        no_panel = summary(client)
        assert no_panel['evaluation_batch_id'] is None and no_panel['counts']['total'] == 24
        assert no_panel['counts']['scored'] == 0
        # Missing legacy batch metadata must not equal the no-panel None batch.
        with connect(cfg.database_path) as db:
            row = json.loads(db.execute('SELECT record_json FROM translation_sessions WHERE id=?', (sids[0],)).fetchone()[0])
            del row['assessment']['evaluation_batch_id']
            db.execute('UPDATE translation_sessions SET record_json=? WHERE id=?', (json.dumps(row), sids[0]))
        assert summary(client)['counts']['scored'] == 0


def test_research_panel_change_still_invalidates_freeze_and_blocks_old_unscored_record(tmp_path):
    app = create_app(settings(tmp_path, 'live'))
    with TestClient(app) as client:
        set_panel(app, client)
        assert client.post('/api/admin/translation/materials/freeze', json={'confirmed': True}, headers=auth()).status_code == 200
        before = materials(client)
        assert before['frozen'] and before['ready']
        sid = complete_task(app, client)
        original = record(client, sid)
        replacement = panel_models()
        replacement[2]['weight'] = 7
        set_panel(app, client, models=replacement)
        changed = materials(client)
        assert changed['cohort_id'] != before['cohort_id']
        assert not changed['frozen'] and not changed['ready']
        assert evaluate(client, sid).status_code == 409
        assert client.post('/api/translation/start', json=payload()).status_code == 409
        assert not record(client, sid)['current_cohort']
        assert record(client, sid)['material_snapshot'] == original['material_snapshot']


def test_inflight_late_panel_review_keeps_original_batch_and_provider_generation(tmp_path):
    app = create_app(replace(settings(tmp_path, 'pilot'), translation_showcase_mode=True))
    with TestClient(app) as client:
        sid = complete_task(app, client)
        entered = threading.Event()
        release = threading.Event()
        calls = set_panel(app, client, entered=entered, release=release)
        original_batch = summary(client)['evaluation_batch_id']
        original_cohort = materials(client)['cohort_id']
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(evaluate, client, sid)
            try:
                assert entered.wait(3)
                in_progress = record(client, sid)['evaluation_pending']
                assert in_progress['evaluation_batch_id'] == original_batch
                replacement = panel_models()
                replacement[2]['weight'] = 7
                new_calls = set_panel(app, client, models=replacement)
                assert summary(client)['evaluation_batch_id'] != original_batch
            finally:
                release.set()
            response = pending.result(timeout=5)
        assert response.status_code == 200, response.text
        assessment = response.json()
        assert assessment['evaluation_batch_id'] == original_batch
        assert assessment['evaluator_config'][2]['weight'] == 1
        assert len(calls) == 5 and not new_calls
        assert record(client, sid)['current_cohort']
        current = summary(client)
        assert current['cohort_id'] == original_cohort and current['counts']['total'] == 1
        assert current['counts']['scored'] == 0
        assert evaluate(client, sid).json() == assessment
        assert len(calls) == 5 and not new_calls


def test_failed_late_panel_review_preserves_batch_and_does_not_publish_partial_scores(tmp_path):
    app = create_app(replace(settings(tmp_path, 'pilot'), translation_showcase_mode=True))
    with TestClient(app) as client:
        sid = complete_task(app, client)
        calls = set_panel(app, client)
        app.state.translation_judge_providers['judge3'] = Judge('judge3', calls, fail=True)
        batch = summary(client)['evaluation_batch_id']
        assert evaluate(client, sid).status_code == 502
        row = record(client, sid)
        assert row['assessment'] is None and row['evaluation_pending'] is None
        failure = next(event for event in reversed(row['events']) if event['type'] == 'evaluation_failed')
        assert failure['evaluation_batch_id'] == batch
        assert len(failure['evaluator_config']) == 5 and len(failure['partial_ratings']) == 4
        assert failure['failures'] == [{'model_id': 'judge3', 'code': 'invalid_model_rating'}]
        assert summary(client)['counts']['scored'] == 0
        assert len(calls) == 5
