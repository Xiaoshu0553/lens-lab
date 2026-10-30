"""Offline HTTP fixtures; no generated score here is real API evidence."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import httpx
import pytest


SPEC = importlib.util.spec_from_file_location('translation_evaluation_cli', Path(__file__).parents[1] / 'scripts/evaluate_translation_records.py')
cli = importlib.util.module_from_spec(SPEC)


@pytest.fixture(autouse=True)
def load_cli():
    SPEC.loader.exec_module(cli)


class FixtureAPI:
    base_url = 'https://study.example.invalid'
    def __init__(self, count=2):
        self.summary = dict(cohort_id='cohort-a', evaluation_batch_id='batch-a', panel_ready=True,
                            evaluation_mode='repeated_sessions', study_mode='pilot', evaluation_session_count=5)
        self.records = {f'r{i}': dict(id=f'r{i}', cohort_id='cohort-a', current_cohort=True,
            record_kind='participant', ended_at='2026-10-30T01:00:00+00:00', submission='这是完整的已提交译文。' * 4,
            assessment=None, assessment_status='pending', evaluation_pending=None) for i in range(count)}
        self.posts = []; self.on_post = None; self.on_get_record = None
    def request(self, method, path, payload=None):
        if path.endswith('/summary'):
            return deepcopy(self.summary)
        sid = path.split('/')[-2] if path.endswith('/evaluate') else path.split('/')[-1]
        if method == 'GET':
            if self.on_get_record:
                self.on_get_record(sid)
            return deepcopy(self.records[sid])
        self.posts.append((sid, deepcopy(payload)))
        if self.on_post:
            return self.on_post(sid, payload)
        return self.finish(sid, payload)
    def finish(self, sid, payload):
        assert payload['evaluation_batch_id'] == self.summary['evaluation_batch_id']
        assert payload['submission_sha256'] == cli.submission_hash(self.records[sid]['submission'])
        previous = self.records[sid]['assessment']
        if previous and previous['evaluation_batch_id'] != payload['evaluation_batch_id']:
            assert payload['allow_reassessment'] is True
            self.records[sid].setdefault('assessment_history', []).append(deepcopy(previous))
        result = dict(evaluation_batch_id=payload['evaluation_batch_id'], evaluation_mode='repeated_sessions',
                      score=80, ratings=[{'model_id': f'session-{i}', 'scores': {'accuracy': 80}} for i in range(5)])
        self.records[sid].update(assessment=deepcopy(result), assessment_status='completed', evaluation_pending=None)
        return deepcopy(result)


def run(api, path, **kwargs):
    return cli.evaluate_records(api, sorted(api.records), path, poll_seconds=0, sleep=lambda _: None, **kwargs)


def test_manifest_is_atomic_private_and_resumption_skips_same_batch_without_new_posts(tmp_path):
    api = FixtureAPI(25); path = tmp_path / 'private' / 'evaluation.json'
    result = run(api, path)
    assert result['status'] == 'completed' and len(result['records']) == 25
    assert len(api.posts) == 25 and all(item['status'] == 'completed' for item in result['records'])
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(path.parent.glob('.translation-evaluation-*'))
    assert run(api, path)['status'] == 'completed' and len(api.posts) == 25


def test_selected_ids_are_the_only_records_evaluated(tmp_path):
    api = FixtureAPI(3)
    result = cli.evaluate_records(api, ['r1'], tmp_path / 'm.json', poll_seconds=0, sleep=lambda _: None)
    assert [sid for sid, _ in api.posts] == ['r1'] and result['ids'] == ['r1']


@pytest.mark.parametrize('change', [{'record_kind': 'researcher_preview'}, {'ended_at': None},
    {'cohort_id': 'other'}, {'current_cohort': False}, {'submission': ''}])
def test_ineligible_selected_record_stops_before_any_model_request(tmp_path, change):
    api = FixtureAPI(); api.records['r1'].update(change)
    with pytest.raises(cli.EvaluationError): run(api, tmp_path / 'm.json')
    assert not api.posts


def test_configuration_change_stops_with_finished_result_preserved(tmp_path):
    api = FixtureAPI(); path = tmp_path / 'm.json'
    def changed(sid, payload):
        result = api.finish(sid, payload); api.summary['evaluation_batch_id'] = 'batch-b'; return result
    api.on_post = changed
    with pytest.raises(cli.EvaluationError, match='batch'):
        run(api, path)
    assert len(api.posts) == 1
    saved = json.loads(path.read_text())
    assert saved['evaluation_batch_id'] == 'batch-a' and saved['records'][0]['status'] == 'completed'
    assert saved['records'][1]['status'] != 'completed'
    with pytest.raises(cli.EvaluationError): run(api, path)
    assert len(api.posts) == 1


def test_changed_submission_is_not_silently_scored_on_resume(tmp_path):
    api = FixtureAPI(); path = tmp_path / 'm.json'
    run(api, path)
    api.records['r1']['submission'] += '新增内容。'
    before = len(api.posts)
    with pytest.raises(cli.EvaluationError, match='submission'):
        run(api, path)
    assert len(api.posts) == before


def test_other_batch_is_preserved_and_requires_explicit_reassessment(tmp_path):
    api = FixtureAPI(1)
    old = {'evaluation_batch_id': 'old-batch', 'score': 71}
    api.records['r0'].update(assessment=deepcopy(old), assessment_status='completed')
    with pytest.raises(cli.EvaluationError, match='reassessment'):
        run(api, tmp_path / 'blocked.json')
    assert not api.posts and api.records['r0']['assessment'] == old
    result = run(api, tmp_path / 'allowed.json', allow_reassessment=True)
    assert result['status'] == 'completed' and api.posts[0][1]['allow_reassessment'] is True
    assert api.records['r0']['assessment_history'] == [old]


def test_same_batch_history_is_reused_when_the_active_configuration_returns_to_it(tmp_path):
    api = FixtureAPI(1)
    api.records['r0'].update(assessment={'evaluation_batch_id': 'other-batch', 'score': 70},
        assessment_history=[{'evaluation_batch_id': 'batch-a', 'score': 80}])
    assert run(api, tmp_path / 'm.json')['status'] == 'completed'
    assert not api.posts and api.records['r0']['assessment']['evaluation_batch_id'] == 'other-batch'


def test_server_active_assessment_is_preferred_and_skips_repeat_billing(tmp_path):
    api = FixtureAPI(1)
    api.records['r0'].update(assessment={'evaluation_batch_id': 'other-batch', 'score': 70},
        active_assessment={'evaluation_batch_id': 'batch-a', 'score': 80})
    assert run(api, tmp_path / 'm.json')['status'] == 'completed'
    assert not api.posts


@pytest.mark.parametrize('status', [401, 403, 400, 422, 500, 503])
def test_auth_or_unknown_failures_are_not_retried(tmp_path, status):
    api = FixtureAPI(1)
    def failed(*_): raise cli.APIError('POST', '/evaluate', status)
    api.on_post = failed
    with pytest.raises(cli.EvaluationError): run(api, tmp_path / 'm.json')
    assert len(api.posts) == 1


def test_502_is_retried_at_most_twice_and_retry_budget_persists(tmp_path):
    api = FixtureAPI(1); path = tmp_path / 'm.json'
    def failed(*_): raise cli.APIError('POST', '/evaluate', 502)
    api.on_post = failed
    with pytest.raises(cli.EvaluationError): run(api, path)
    assert len(api.posts) == 3
    with pytest.raises(cli.EvaluationError): run(api, path)
    assert len(api.posts) == 3 and json.loads(path.read_text())['records'][0]['post_attempts'] == 3


def test_timeout_synchronizes_pending_to_completion_without_duplicate_post(tmp_path):
    api = FixtureAPI(1); polls = []
    def timed_out(sid, payload):
        api.records[sid]['evaluation_pending'] = {'evaluation_batch_id': 'batch-a'}
        def poll(current):
            polls.append(current)
            if len(polls) == 2:
                api.finish(sid, payload)
        api.on_get_record = poll
        raise cli.APIError('POST', '/evaluate', None)
    api.on_post = timed_out
    assert run(api, tmp_path / 'm.json')['status'] == 'completed'
    assert len(api.posts) == 1 and len(polls) >= 2


def test_api_credentials_are_used_only_in_basic_auth_and_never_in_manifest(tmp_path):
    seen = []
    def response(request):
        seen.append(request)
        return httpx.Response(200, json={'ok': True})
    api = cli.Platform('https://study.example.invalid', 'PRIVATE-CLI-PASSWORD', transport=httpx.MockTransport(response))
    assert api.request('GET', '/api/admin/translation/summary') == {'ok': True}
    api.close()
    assert seen[0].headers['Authorization'].startswith('Basic ')
    assert b'PRIVATE-CLI-PASSWORD' not in seen[0].content


@pytest.mark.parametrize('value', ['https://admin:secret@example.org', 'http://remote.example.org',
    'https://example.org/?api_key=secret', 'https://example.org/other'])
def test_unsafe_platform_origins_are_rejected(value):
    with pytest.raises(cli.EvaluationError): cli.normalize_origin(value)


def test_ids_file_is_explicit_unique_and_accepts_a_json_list_or_ids_object(tmp_path):
    path = tmp_path / 'ids.json'; path.write_text('["r2","r1"]')
    assert cli.load_ids(path) == ['r1', 'r2']
    path.write_text('{"ids":["r1"]}')
    assert cli.load_ids(path) == ['r1']
    path.write_text('["r1","r1"]')
    with pytest.raises(cli.EvaluationError): cli.load_ids(path)
