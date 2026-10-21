"""Instrument integration checks with synthetic records, not validity evidence."""
import json
from collections import Counter

import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.db import connect
from test_flow import settings, start_payload, auth


def test_cases_and_arms_balance_within_literacy_and_preview_is_separate(tmp_path):
    cfg=settings(tmp_path)
    client=TestClient(create_app(cfg))
    payload=start_payload()
    assert client.post('/api/start',json={**payload,'researcher_preview':True}).status_code==401
    preview_login=client.get('/admin/preview',follow_redirects=False)
    assert preview_login.status_code==303
    assert preview_login.headers['location']=='/admin/login?next=%2Fadmin%2Fpreview'
    assert 'data-researcher-preview' not in preview_login.text
    assert client.get('/admin/preview',headers=auth()).status_code==200
    for _ in range(11):
        assert client.post('/api/start',json={**payload,'researcher_preview':True},headers=auth()).status_code==200
    preview_only=client.get('/api/admin/summary',headers=auth()).json()
    assert preview_only['literacy_distribution']==[] and preview_only['groups']==[]
    for _ in range(16):
        assert client.post('/api/start',json=payload).status_code==200
    with connect(cfg.database_path) as db:
        rows=[dict(row) for row in db.execute("SELECT scenario_id,group_id,task_snapshot_json FROM sessions WHERE record_kind='participant'")]
    counts=Counter((r['scenario_id'],r['group_id']) for r in rows)
    assert len(counts)==8 and set(counts.values())=={2}
    for row in rows:
        snapshot=json.loads(row['task_snapshot_json'])
        assert snapshot['scenario_id']==row['scenario_id']
        assert 'scenario_variants' not in snapshot and 'scenario_values' not in snapshot
    summary=client.get('/api/admin/summary',headers=auth()).json()
    assert sum(g['sessions'] for g in summary['groups'])==16
    assert summary['literacy_distribution']==[] and summary['analysis_n']==0
    assert client.get('/api/admin/archive-export?format=json',headers=auth()).json().__len__()==27


@pytest.mark.parametrize('scenario', ['first_3','first_1','return_3','return_1'])
def test_two_independent_choices_frozen_score_private_keys_hidden(tmp_path, scenario):
    cfg=settings(tmp_path)
    client=TestClient(create_app(cfg))
    for _ in range(8):
        state=client.post('/api/start',json=start_payload()).json()
        sid=state['session_id']
        with connect(cfg.database_path) as db:
            snapshot=json.loads(db.execute('SELECT task_snapshot_json FROM sessions WHERE id=?',(sid,)).fetchone()[0])
        if snapshot['scenario_id']==scenario:
            break
    else:
        pytest.fail('All four cases should be assigned in the first block')
    assert state['task']['decision_options']==state['task']['execution_options']==[]
    for key in ['scenario_variants','score_components','correct','success','scenario_values','rubric']:
        assert key not in json.dumps(state['task'])
    for greeting in ['你好','您好']:
        assert client.post(f'/api/session/{sid}/message',json={'content':greeting}).status_code==200
    choices=client.post(f'/api/session/{sid}/decision').json()
    main=max(snapshot['decision_options'],key=lambda o:o['score'])
    execution=max(snapshot['execution_options'],key=lambda o:o['score'])
    token=lambda opts,text:next(o['id'] for o in opts if o['text']==text)
    body={'decision_choice_id':token(choices['decision_options'],main['text'])}
    assert client.post(f'/api/session/{sid}/submit',json=body).status_code==422
    # Group-specific tokens and score fields cannot be forged by source IDs.
    assert client.post(f'/api/session/{sid}/submit',json={**body,'execution_choice_id':execution['id']}).status_code==422
    body['execution_choice_id']=token(choices['execution_options'],execution['text'])
    assert client.post(f'/api/session/{sid}/submit',json=body).status_code==200
    with connect(cfg.database_path) as db:
        row=db.execute('SELECT score,decision_feasible,decision_optimal FROM sessions WHERE id=?',(sid,)).fetchone()
    assert tuple(row)==(100,1,1)
    result=next(r for r in client.get('/api/admin/archive-export?format=json',headers=auth()).json() if r['id']==sid)
    assert result['decision_quality_score']==100
    assert sum(json.loads(result['decision_components_json']).values())==100
    assert result['score_semantics']=='decision_quality_points_not_success_probability'


def test_pilot_item_analysis_excludes_researcher_preview_and_preserves_invalid_record_audit(tmp_path):
    cfg=settings(tmp_path,'pilot')
    client=TestClient(create_app(cfg))
    for preview in [False, True]:
        assert client.post('/api/start',json={**start_payload(),'researcher_preview':preview},headers=auth()).status_code==200
    with connect(cfg.database_path) as db:
        row=db.execute("SELECT id,task_snapshot_json FROM sessions WHERE record_kind='participant'").fetchone()
        task=json.loads(row['task_snapshot_json'])
        selected=max(task['decision_options'],key=lambda item:item['score'])
        execution=max(task['execution_options'],key=lambda item:item['score'])
        db.execute("UPDATE sessions SET ended_at=created_at,score=100,decision_choice_id=?,execution_choice_id=? WHERE id=?",
                   (selected['id'],execution['id'],row['id']))
    result=client.get('/api/admin/pretest-analysis',headers=auth()).json()
    assert result['n']==1 and result['sample_status']=='insufficient'
    assert result['validation_status']=='unvalidated'
    with connect(cfg.database_path) as db:
        db.execute("UPDATE sessions SET literacy_answers_json='not-json' WHERE record_kind='participant'")
    result=client.get('/api/admin/pretest-analysis',headers=auth()).json()
    assert result['n']==0 and result['cohort_excluded_count']==1
