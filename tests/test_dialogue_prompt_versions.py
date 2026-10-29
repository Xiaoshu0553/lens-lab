"""Real-model prompt fixes must not relabel frozen offline/demo records."""
import json

from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app
from app.model import (ANSWER_PROMPT_VERSION, DEMO_ANSWER_PROMPT_VERSION,
                       DEMO_PROMPT_ONLY_VERSION, PROMPT_ONLY_VERSION)
from test_flow import auth, settings, start_payload


def test_demo_versions_and_cohort_survive_real_model_prompt_revision(tmp_path):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    info = client.get('/api/info').json()
    assert info['answer_prompt_version'] == DEMO_ANSWER_PROMPT_VERSION == 'answer-dialogue-v6'
    assert DEMO_PROMPT_ONLY_VERSION == 'clarify-dialogue-v4'
    sessions = [client.post('/api/start', json=start_payload()).json() for _ in range(2)]
    assert {row['group_id'] for row in sessions} == {'S', 'A'}
    for row in sessions:
        expected = DEMO_ANSWER_PROMPT_VERSION + ('+' + DEMO_PROMPT_ONLY_VERSION if row['group_id'] == 'A' else '')
        assert row['prompt_version'] == expected
    with connect(cfg.database_path) as db:
        before = [tuple(row) for row in db.execute('SELECT * FROM sessions ORDER BY id')]
    restarted = TestClient(create_app(cfg))
    with connect(cfg.database_path) as db:
        after = [tuple(row) for row in db.execute('SELECT * FROM sessions ORDER BY id')]
    assert after == before
    for row in sessions:
        response = restarted.post(f"/api/session/{row['session_id']}/message", json={'content': '需要什么材料？'})
        assert response.status_code == 200, response.text


def test_new_real_dialogues_use_new_prompts_and_preserve_old_history(tmp_path):
    cfg = settings(tmp_path, 'pilot')
    client = TestClient(create_app(cfg))
    assert client.get('/api/info').json()['answer_prompt_version'] == ANSWER_PROMPT_VERSION == 'answer-dialogue-v7'
    sessions = [client.post('/api/start', json=start_payload()).json() for _ in range(2)]
    assert {row['prompt_version'] for row in sessions} == {
        ANSWER_PROMPT_VERSION, ANSWER_PROMPT_VERSION + '+' + PROMPT_ONLY_VERSION}
    old = sessions[0]
    sid = old['session_id']
    legacy = DEMO_ANSWER_PROMPT_VERSION + ('+' + DEMO_PROMPT_ONLY_VERSION if old['group_id'] == 'A' else '')
    with connect(cfg.database_path) as db:
        db.execute('UPDATE sessions SET prompt_version=? WHERE id=?', (legacy, sid))
        db.execute("INSERT INTO messages(session_id,role,content,action,created_at) VALUES (?, 'user', '你好', 'user_message', '2026-10-27T01:00:00+00:00')", (sid,))
        db.execute("INSERT INTO messages(session_id,role,content,action,created_at) VALUES (?, 'assistant', '你好，请提问。', 'answer', '2026-10-27T01:00:01+00:00')", (sid,))
    restored = client.get(f'/api/session/{sid}')
    assert restored.status_code == 200
    assert restored.json()['messages'][-1]['content'] == '你好，请提问。'
    refused = client.post(f'/api/session/{sid}/message', json={'content': '接着说'})
    assert refused.status_code == 409
    records = client.get('/api/admin/archive-export?kind=raw&format=json', headers=auth()).json()
    assert next(row for row in records if row['id'] == sid)['prompt_version'] == legacy
