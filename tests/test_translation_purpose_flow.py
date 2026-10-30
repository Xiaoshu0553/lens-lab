"""Same database can serve clean presentation rows without losing their purpose."""
from dataclasses import replace
import csv
import io

from fastapi.testclient import TestClient

from app.main import create_app
from test_flow import auth, settings
from test_translation_flow import payload, dialogue, assert_standard_export_fields


def test_presentation_flow_retains_technical_purpose_and_original_records(tmp_path):
    base = settings(tmp_path)
    with TestClient(create_app(base)) as research:
        original = research.post('/api/translation/start', json=payload()).json()['id']
    with TestClient(create_app(replace(base, translation_showcase_mode=True))) as client:
        assert client.get('/api/translation/info').json()['data_purpose'] == 'demonstration'
        started = client.post('/api/translation/start', json=payload())
        assert started.status_code == 200, started.text
        sid = started.json()['id']
        dialogue(client, sid)
        translation = '她站在门前等着。远处的钟声响起，雨已经停了。她打开箱子，轻轻地笑了。'
        assert client.put(f'/api/translation/session/{sid}/draft', json={'draft': translation}).status_code == 200
        assert client.post(f'/api/translation/session/{sid}/submit', json={'submission': translation}).status_code == 200
        summary = client.get('/api/admin/translation/summary', headers=auth()).json()
        assert summary['counts']['total'] == summary['counts']['submitted'] == 1
        assert summary['data_purpose'] == 'demonstration'
        rows = client.get('/api/admin/translation/export', headers=auth()).json()
        indexed = {row['id']: row for row in rows}
        assert set(indexed) == {original, sid}
        assert indexed[sid]['submission'] == translation
        assert_standard_export_fields(rows)
        for record_id, expected in ((original, 'research'), (sid, 'demonstration')):
            detail = client.get(f'/api/admin/translation/records/{record_id}', headers=auth()).json()
            assert detail['data_purpose'] == expected
        for kind in ('records', 'dialogue'):
            result = client.get('/api/admin/translation/export', params={'kind': kind, 'format': 'csv'}, headers=auth())
            exported = list(csv.DictReader(io.StringIO(result.text.lstrip('\ufeff'))))
            assert any(row['id'] == sid for row in exported)
            assert_standard_export_fields(exported)
    with TestClient(create_app(base)) as research:
        assert research.get('/api/admin/translation/summary', headers=auth()).json()['counts']['total'] == 1


def test_client_cannot_relabel_data_purpose(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        sid = client.post('/api/translation/start', json=payload(data_purpose='demonstration')).json()['id']
        record = client.get(f'/api/admin/translation/records/{sid}', headers=auth()).json()
        assert record['data_purpose'] == 'research'
