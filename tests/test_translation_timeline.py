"""A private presentation clock never changes recorded time or model evidence."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json

from app.db import connect
from app.translation_timeline import PresentationTimeline
from test_flow import auth
from test_translation_data_evaluation import api, _body, _complete, _evaluate, _repeat, _summary
from test_translation_exports import stored_records


def plan_entry(row, offset):
    return dict(recorded_created_at=row['created_at'], recorded_ended_at=row['ended_at'],
        submission_sha256=hashlib.sha256(row['submission'].encode()).hexdigest(),
        created_at=(datetime.fromisoformat(row['created_at']) + offset).isoformat())


def test_output_clock_is_stable_consistent_and_never_used_for_task_time_or_reassessment(api):
    app, client, wire = api
    sids = [_complete(client) for _ in range(2)]
    preview = _complete(client, preview=True)
    _repeat(client)
    for sid in sids:
        assert _evaluate(client, sid, _body(client, sid)).status_code == 200
    with connect(app.state.settings.database_path) as db:
        originals = {sid: json.loads(raw) for sid, raw in db.execute(
            'SELECT id,record_json FROM translation_sessions')}
    timeline = PresentationTimeline(app.state.settings)
    entries = {sid: plan_entry(originals[sid], timedelta(hours=index - 3))
        for index, sid in enumerate(sids)}
    timeline.path.write_text(json.dumps({'version': 1, 'records': entries}))
    before, summary = stored_records(app), _summary(client)
    call_count = len(wire.judge_calls), len(wire.dialogue_calls)
    listed = client.get('/api/admin/translation/records', headers=auth()).json()['records']
    assert all('study_mode' not in row for row in listed)
    listed = {row['id']: row for row in listed}
    exported = client.get('/api/admin/translation/export', headers=auth()).json()
    exported = {row['id']: row for row in exported}
    dialogue = client.get('/api/admin/translation/export?kind=dialogue', headers=auth()).json()
    for sid in sids:
        detail = client.get(f'/api/admin/translation/records/{sid}', headers=auth()).json()
        raw = client.get(f'/api/admin/translation/records/{sid}?raw=1', headers=auth()).json()
        assert 'study_mode' not in detail and raw['study_mode'] == 'pilot'
        assert raw['created_at'] == originals[sid]['created_at']
        assert detail['created_at'] == listed[sid]['created_at'] == exported[sid]['created_at'] == entries[sid]['created_at']
        assert detail['ended_at'] == exported[sid]['ended_at'] and detail['duration_seconds'] == raw['duration_seconds']
        assert detail['assessment'] == raw['assessment'] and detail['events'] == raw['events']
        messages = [message for message in dialogue if message['id'] == sid]
        assert [message['created_at'] for message in messages] == [message['created_at'] for message in detail['messages']]
        assert detail['messages'][0]['content'] == raw['messages'][0]['content']
        actual_view = client.get(f'/api/translation/session/{sid}').json()
        assert actual_view['created_at'] == originals[sid]['created_at']
        assert _evaluate(client, sid, _body(client, sid)).json() == raw['assessment']
    assert listed[preview]['created_at'] == originals[preview]['created_at']
    for kind in ('records', 'dialogue', 'ratings'):
        for format in ('json', 'csv'):
            path = f'/api/admin/translation/export?kind={kind}&format={format}'
            assert client.get(path, headers=auth()).content == client.get(path, headers=auth()).content
    assert client.get(f'/api/admin/translation/records/{sids[0]}?raw=1').status_code == 401
    assert stored_records(app) == before and _summary(client) == summary
    assert (len(wire.judge_calls), len(wire.dialogue_calls)) == call_count


def test_presentation_clock_is_opt_in_bound_to_source_and_preserves_message_offsets(api):
    app, client, _ = api
    sid = _complete(client)
    with connect(app.state.settings.database_path) as db:
        original = json.loads(db.execute('SELECT record_json FROM translation_sessions WHERE id=?', (sid,)).fetchone()[0])
    before = deepcopy(original)
    timeline = PresentationTimeline(app.state.settings)
    entry = plan_entry(original, timedelta(hours=-1))
    projected = timeline.project(original, {sid: entry})
    assert projected['created_at'] != original['created_at'] and original == before
    for message, recorded in zip(projected['messages'], original['messages']):
        assert datetime.fromisoformat(message['created_at']) - datetime.fromisoformat(projected['created_at']) == (
            datetime.fromisoformat(recorded['created_at']) - datetime.fromisoformat(original['created_at']))
    disabled = PresentationTimeline(replace(app.state.settings, translation_showcase_mode=False))
    assert disabled.project(original, {sid: entry}) == original
    for change in ({'data_purpose': 'research'}, {'pending': {'request_id': 'active'}},
                   {'ended_at': None}, {'record_kind': 'researcher_preview'}):
        row = {**original, **change}
        assert timeline.project(row, {sid: entry}) == row
    for change in ({'recorded_created_at': 'wrong'}, {'submission_sha256': 'wrong'}, {'created_at': 'invalid'}):
        assert timeline.project(original, {sid: {**entry, **change}}) == original
    timeline.path.write_text('invalid JSON')
    assert timeline.snapshot() == {} and timeline.project(original) == original
