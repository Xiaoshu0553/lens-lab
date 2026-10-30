"""Business downloads stay small without changing source evidence or scores."""
from copy import deepcopy
import csv
import io
import json

from app.db import connect
from app.translation_exports import COLUMNS, DIMENSIONS, csv_download, export_record
from test_flow import auth
from test_translation_data_evaluation import api, _body, _complete, _evaluate, _repeat
from test_translation_flow import assert_standard_export_fields


def stored_records(app):
    with connect(app.state.settings.database_path) as db:
        return [tuple(row) for row in db.execute(
            'SELECT id,record_json FROM translation_sessions ORDER BY id')]


def test_all_six_downloads_share_business_schema_without_writing_or_calling_models(api):
    app, client, wire = api
    sid = _complete(client)
    preview = _complete(client, preview=True)
    _repeat(client)
    assert _evaluate(client, sid, _body(client, sid)).status_code == 200
    baseline = stored_records(app)
    model_calls = len(wire.judge_calls), len(wire.dialogue_calls)
    for kind, columns in COLUMNS.items():
        json_response = client.get('/api/admin/translation/export',
            params={'kind': kind, 'format': 'json'}, headers=auth())
        csv_response = client.get('/api/admin/translation/export',
            params={'kind': kind, 'format': 'csv'}, headers=auth())
        assert json_response.status_code == csv_response.status_code == 200
        rows = json_response.json()
        reader = csv.DictReader(io.StringIO(csv_response.content.decode('utf-8-sig')))
        assert reader.fieldnames == list(columns)
        flat = list(reader)
        assert len(rows) == len(flat)
        assert all(set(row) == set(columns) for row in rows)
        assert_standard_export_fields(rows)
        assert_standard_export_fields(flat)
        assert all(response.headers['cache-control'] == 'no-store'
            and 'attachment;' in response.headers['content-disposition']
            for response in (json_response, csv_response))
        assert client.get('/api/admin/translation/export',
            params={'kind': kind, 'format': 'json'}).status_code == 401
        if kind == 'records':
            assert {row['id']: row['record_kind'] for row in rows} == {
                sid: 'participant', preview: 'researcher_preview'}
            scored = next(row for row in rows if row['id'] == sid)
            assert scored['evaluation_session_count'] == 5 and scored['independent_model_count'] == 1
            assert scored['evaluation_mode'] == 'repeated_sessions'
            assert all(scored[key] == 80 for key in DIMENSIONS)
        if kind == 'ratings':
            assert len(rows) == 5 and {row['model_id'] for row in rows} == {
                f'session-{index}' for index in range(1, 6)}
            assert len({row['reported_model'] for row in rows}) == 1
    assert stored_records(app) == baseline
    assert (len(wire.judge_calls), len(wire.dialogue_calls)) == model_calls


def test_record_download_selects_restored_active_review_and_numbers_history(api):
    app, client, wire = api
    sid = _complete(client)
    _repeat(client)
    assert _evaluate(client, sid, _body(client, sid)).status_code == 200
    _repeat(client, repetitions=7)
    assert _evaluate(client, sid, _body(client, sid, allow_reassessment=True)).status_code == 200
    _repeat(client, repetitions=5)
    baseline = stored_records(app)
    records = client.get('/api/admin/translation/export', headers=auth()).json()
    assert len(records) == 1
    assert records[0]['evaluation_round'] == 1 and records[0]['evaluation_session_count'] == 5
    ratings = client.get('/api/admin/translation/export?kind=ratings', headers=auth()).json()
    assert len(ratings) == 12
    assert sum(row['evaluation_round'] == 1 for row in ratings) == 5
    assert sum(row['evaluation_round'] == 2 for row in ratings) == 7
    assert stored_records(app) == baseline and len(wire.judge_calls) == 12


def test_record_projection_preserves_zero_null_and_text_without_nested_audit_payloads():
    source = dict(id='test-session', record_kind='participant', condition='S', band='low',
        created_at='2026-10-30T01:00:00+00:00', ended_at='2026-10-30T01:00:00+00:00',
        ability_score=0, ability={'percent': 0, 'self_report_mean': 0,
            'domain_scores': {'翻译素养': {'correct': 0}}},
        submission='=中文,"原译文"\n第二行', answers={}, self_answers={},
        task={'source_text': 'Original paragraph', 'unexpected_private_payload': 'private'},
        data_purpose='demonstration', provenance={'collection_method': 'automated_script'},
        arbitrary_new_internal_field='private')
    assessment = dict(score=0, dimensions={key: 0 for key in DIMENSIONS},
        needs_review=False, ratings=[], evaluation_mode='repeated_sessions',
        independent_model_count=1, session_count=5, raw_response='private',
        scoring_prompt_sha256='private', evaluator_config=[{'api_key': 'private'}])
    source['assessment'] = assessment
    before = deepcopy(source)
    exported = export_record(source, assessment)
    assert exported['ability_score'] == exported['ability_percent'] == exported['score'] == 0
    assert exported['duration_seconds'] == exported['translation_literacy_score'] == 0
    assert exported['translation_knowledge_score'] is None and exported['needs_review'] is False
    assert exported['submission'] == source['submission']
    assert_standard_export_fields(exported)
    assert 'private' not in json.dumps(exported)
    flat = list(csv.DictReader(io.StringIO(csv_download([exported], 'records').lstrip('\ufeff'))))[0]
    assert flat['submission'] == "'" + source['submission']
    assert flat['score'] == '0' and flat['translation_knowledge_score'] == ''
    assert source == before
