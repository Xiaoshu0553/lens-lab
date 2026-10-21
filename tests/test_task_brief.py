"""Participant instructions and task upgrades must preserve the experiment."""
import json

from fastapi.testclient import TestClient

from app.age_forms import AGE_BANDS, form_for
from app.age_tasks import task_for
from app.db import connect
from app.main import create_app
from app.config import Settings
from app.seed import seed_demo


def settings(tmp_path, mode='demo'):
    return Settings(database_path=tmp_path/'brief.sqlite3', study_mode=mode,
                    admin_password='test-only', model_api_key='', model_base_url='https://example.invalid',
                    model_name='test', model_temperature=0, assignment_seed='fixed')


def start_payload(age='35-44'):
    version, items = form_for(age)
    return dict(consent=True, age_band=age, education_stage='other', topic_familiarity=3,
                pretest_version=version, pretest_answers={item['id']: item['correct'] for item in items})


def test_brief_states_person_goal_and_deliverable_without_revealing_intervention():
    for age in AGE_BANDS:
        task = task_for(age)
        assert task['id'].endswith('-v4') and '陈禾' in task['title']
        assert '预测试' not in task['title']
        text = task['instructions']
        assert all(part in text for part in ('陈禾', '目标：', '登记类别', '参加安排', '参加方案', '办理方案', '无需实际报名'))
        assert not any(part in text for part in ('澄清', '实验组', '对照组', '2 次', '11 月 18'))


def test_both_arms_receive_identical_briefs_and_pilot_remains_outside_analysis(tmp_path):
    cfg = settings(tmp_path, 'pilot')
    client = TestClient(create_app(cfg))
    a, b = (client.post('/api/start', json=start_payload()).json() for _ in range(2))
    assert {a['group_id'], b['group_id']} == {'S','A'}
    assert {k:v for k,v in a['task'].items() if k!='participant_facts'} == {k:v for k,v in b['task'].items() if k!='participant_facts'}  # Cases vary; instructions and evidence budget do not.
    assert a['run_mode'] == b['run_mode'] == 'pilot' and a['is_demo'] and b['is_demo']
    assert client.get('/api/info').json()['study_mode'] == 'pilot'


def copy_task(db, original, new_id, *, active):
    columns=[row['name'] for row in db.execute('PRAGMA table_info(tasks)')]
    record=dict(db.execute('SELECT * FROM tasks WHERE id=?', (original,)).fetchone())
    record.update(id=new_id, active=active)
    db.execute('INSERT INTO tasks ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',
               [record[col] for col in columns])


def test_seed_upgrade_replaces_only_bundled_tasks_and_keeps_snapshot_and_custom(tmp_path):
    cfg=settings(tmp_path)
    client=TestClient(create_app(cfg))
    sid=client.post('/api/start',json=start_payload()).json()['session_id']
    with connect(cfg.database_path) as db:
        frozen=json.loads(db.execute('SELECT task_snapshot_json FROM sessions WHERE id=?',(sid,)).fetchone()[0])
        current=task_for('35-44')['id']
        copy_task(db,current,'pilot-age-35-44-v2',active=1)
        db.execute('DELETE FROM tasks WHERE id=?',(task_for('45-54')['id'],))
        copy_task(db,task_for('18-24')['id'],'pilot-age-45-54-v2',active=1)
        db.execute("UPDATE tasks SET age_band='45-54' WHERE id='pilot-age-45-54-v2'")
        db.execute('DELETE FROM tasks WHERE id=?',(task_for('55-64')['id'],))
        copy_task(db,current,'researcher-custom',active=1)
        db.execute("UPDATE tasks SET age_band='55-64' WHERE id='researcher-custom'")
        # Exercise upgrade from demo to pilot when the old grant task is absent.
        db.execute("DELETE FROM tasks WHERE id='pilot-xinghe-learning-2026-v5'")
    seed_demo(cfg.database_path,'pilot')
    with connect(cfg.database_path) as db:
        assert db.execute("SELECT active FROM tasks WHERE id='pilot-age-35-44-v2'").fetchone()[0]==0
        assert db.execute("SELECT active FROM tasks WHERE id='researcher-custom'").fetchone()[0]==1

        assert db.execute('SELECT active FROM tasks WHERE id=?',(task_for('45-54')['id'],)).fetchone()[0]==1
        assert db.execute('SELECT active FROM tasks WHERE id=?',(task_for('55-64')['id'],)).fetchone()[0]==0
        assert json.loads(db.execute('SELECT task_snapshot_json FROM sessions WHERE id=?',(sid,)).fetchone()[0])==frozen
    # Starting the server again must not create duplicates or replace a custom task.
    seed_demo(cfg.database_path,'pilot')
    with connect(cfg.database_path) as db:
        assert db.execute('SELECT count(*) FROM tasks WHERE id=?',(task_for('45-54')['id'],)).fetchone()[0]==1
        assert db.execute("SELECT active FROM tasks WHERE id='researcher-custom'").fetchone()[0]==1


def test_seed_recovers_an_existing_inactive_replacement_but_respects_manual_stop(tmp_path):
    cfg=settings(tmp_path)
    create_app(cfg)
    current=task_for('35-44')['id']
    with connect(cfg.database_path) as db:
        copy_task(db,current,'pilot-age-35-44-v2',active=1)
        db.execute('UPDATE tasks SET active=0 WHERE id=?',(current,))
    seed_demo(cfg.database_path)
    with connect(cfg.database_path) as db:
        assert db.execute('SELECT active FROM tasks WHERE id=?',(current,)).fetchone()[0]==1
        db.execute('UPDATE tasks SET active=0 WHERE id=?',(current,))
    seed_demo(cfg.database_path)
    with connect(cfg.database_path) as db:
        assert db.execute('SELECT active FROM tasks WHERE id=?',(current,)).fetchone()[0]==0


def test_personal_fact_and_course_rule_remain_distinct_in_both_arms(tmp_path):
    client=TestClient(create_app(settings(tmp_path)))
    sessions=[client.post('/api/start',json=start_payload()).json() for _ in range(2)]
    assert {session['group_id'] for session in sessions}=={'S','A'}
    for session in sessions:
        endpoint=f"/api/session/{session['session_id']}/message"
        unknown=client.post(endpoint,json={'content':'你知道陈禾每周参加几次活动吗'}).json()
        assert unknown['action']=='guidance' and '还不知道' in unknown['content']
        assert '规定' not in unknown['content'] and '3' not in unknown['content']
        assert client.post(endpoint,json={'content':'我现在告诉你，陈禾每周可参加三次活动'}).status_code==200
        known=client.post(endpoint,json={'content':'陈禾每周可以参加几次？'}).json()
        assert '三次' in known['content'] and '你此前提供' in known['content']
        rule=client.post(endpoint,json={'content':'课程要求每周至少参加几次？'}).json()
        assert rule['action']=='evidence' and '每周必须到场2次' in rule['content']
