"""Regression checks for administrator record times and cohort consistency."""
import csv
import io
import json

from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app
from test_flow import AGE_TASK_18, auth, record_id_for, settings, start_payload


def completed_record(client, cfg, *, reviewed=True):
    sid = client.post('/api/start', json=start_payload()).json()['session_id']
    with connect(cfg.database_path) as db:
        snapshot = json.loads(db.execute('SELECT task_snapshot_json FROM sessions WHERE id=?', (sid,)).fetchone()[0])
        selected = max(snapshot['decision_options'], key=lambda o: o['score'])
        execution = max(snapshot['execution_options'], key=lambda o: o['score'])
        db.execute("""UPDATE sessions SET created_at='2026-10-19T15:59:30+00:00',
            decision_started_at='2026-10-19T16:00:30+00:00', ended_at='2026-10-19T16:02:00+00:00',
            decision_choice_id=?,execution_choice_id=?,score=100,review_status=? WHERE id=?""",
            (selected['id'], execution['id'], 'valid' if reviewed else 'pending', sid))
    return sid


def test_admin_times_cross_midnight_without_changing_utc_or_inventing_pretest_time(tmp_path):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    sid = completed_record(client, cfg)
    record = client.get('/api/admin/review-queue', headers=auth()).json()[0]
    assert record['created_at'] == '2026-10-19T15:59:30+00:00'
    assert record['created_at_beijing'] == '2026-10-19T23:59:30+08:00'
    assert record['ended_at_beijing'] == '2026-10-20T00:02:00+08:00'
    assert record['task_completion_seconds'] == 150
    assert record['dialogue_elapsed_seconds'] == 60
    assert record['decision_elapsed_seconds'] == 90
    assert record['pretest_time_seconds'] is None
    assert record['display_timezone'] == 'Asia/Shanghai'
    exported = list(csv.DictReader(io.StringIO(client.get('/api/admin/archive-export', headers=auth()).text.lstrip('\ufeff'))))[0]
    assert exported['ended_at'] == '2026-10-19T16:02:00+00:00'
    assert exported['ended_at_beijing'] == '2026-10-20T00:02:00+08:00'
    with connect(cfg.database_path) as db:
        assert db.execute('SELECT ended_at FROM sessions WHERE id=?', (sid,)).fetchone()[0] == '2026-10-19T16:02:00+00:00'


def test_old_and_wrong_arm_prompts_never_inflate_current_summary(tmp_path):
    cfg = settings(tmp_path, 'live')
    client = TestClient(create_app(cfg))
    client.post('/api/admin/tasks', headers=auth(), json=AGE_TASK_18)
    old = completed_record(client, cfg)
    current = completed_record(client, cfg)
    wrong_arm = completed_record(client, cfg, reviewed=False)
    with connect(cfg.database_path) as db:
        db.execute("UPDATE sessions SET prompt_version='old-router' WHERE id=?", (old,))
        row = db.execute('SELECT interaction_condition FROM sessions WHERE id=?', (wrong_arm,)).fetchone()
        # Both prompt strings are current but belong to opposite experimental arms.
        from app.model import ANSWER_PROMPT_VERSION, DECISION_PROMPT_VERSION
        swapped = ANSWER_PROMPT_VERSION if row[0] == 'adaptive' else ANSWER_PROMPT_VERSION + '+' + DECISION_PROMPT_VERSION
        db.execute('UPDATE sessions SET prompt_version=? WHERE id=?', (swapped, wrong_arm))
    summary = client.get('/api/admin/summary', headers=auth()).json()
    assert sum(g['sessions'] for g in summary['groups']) == 1
    assert sum(s['sessions'] for s in summary['strata']) == 1
    assert sum(s['sessions'] for s in summary['literacy_distribution']) == 1
    assert sum(s['valid_n'] for s in summary['band_outcomes']) == 1
    assert sum(s['sessions'] for s in summary['score_distribution']) == 1
    assert summary['pending_reviews'] == 0
    rows = client.get('/api/admin/archive-export?kind=analysis&format=json', headers=auth()).json()
    assert [r['id'] for r in rows] == [current]
    queue = {r['id']: r for r in client.get('/api/admin/review-queue', headers=auth()).json()}
    assert len(queue) == 3  # Historical data stays accessible for review.
    assert queue[record_id_for(client, wrong_arm)]['prompt_version'] == swapped
    assert queue[record_id_for(client, wrong_arm)]['prompt_version_blinded'] is False
    assert queue[record_id_for(client, wrong_arm)]['analysis_eligible'] is False
    assert queue[record_id_for(client, current)]['in_current_cohort'] is True
    assert queue[record_id_for(client, old)]['in_current_cohort'] is False
    assert '提示词版本已更新或与实验组不匹配' in queue[record_id_for(client, old)]['cohort_exclusion_reasons']


def test_historical_form_model_and_task_have_explicit_exclusion_reasons(tmp_path):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    sid = completed_record(client, cfg)
    with connect(cfg.database_path) as db:
        db.execute("UPDATE sessions SET literacy_instrument_version='old-form',model_config_id='old-model' WHERE id=?", (sid,))
        db.execute('UPDATE tasks SET active=0')
    row = client.get('/api/admin/review-queue', headers=auth()).json()[0]
    assert row['in_current_cohort'] is False
    assert set(row['cohort_exclusion_reasons']) == {'问卷版本已更新', '模型配置已更新', '任务已停用或不属于当前任务'}
    assert row['run_mode'] == 'demo'
    assert row['model_config_id'] == 'old-model'
    assert row['literacy_instrument_version'] == 'old-form'


def test_export_same_named_score_components_add_instead_of_overwriting(tmp_path):
    cfg = settings(tmp_path, 'live')
    client = TestClient(create_app(cfg))
    task = json.loads(json.dumps(AGE_TASK_18))
    for options in [task['decision_options'], task['execution_options']] + [v['decision_options'] for v in task['scenario_variants']]:
        for option in options:
            option['score_components'] = {'shared_dimension': option['score']}
    assert client.post('/api/admin/tasks', headers=auth(), json=task).status_code == 200
    completed_record(client, cfg)
    row = client.get('/api/admin/archive-export?format=json', headers=auth()).json()[0]
    assert row['score'] == 100
    assert json.loads(row['decision_components_json']) == {'shared_dimension': 100}
