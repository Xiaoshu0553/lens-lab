"""Automatic objective scoring and one cohort for questions, outcomes and exports."""
import csv
import io
import json

import pytest
from fastapi.testclient import TestClient

from app.clean_exports import DIALOGUE_CSV_COLUMNS
from app.db import connect
from app.main import create_app
from test_flow import auth, decision_payload, settings, start_payload


def completed_fixture(client, cfg, *, score=100, review_status="pending", age="18-20"):
    sid = client.post("/api/start", json=start_payload(age_band=age)).json()["session_id"]
    with connect(cfg.database_path) as db:
        task = json.loads(db.execute("SELECT task_snapshot_json FROM sessions WHERE id=?", (sid,)).fetchone()[0])
        selected, execution = next((selected, execution) for selected in task["decision_options"]
                                   for execution in task["execution_options"]
                                   if selected["score"] + execution["score"] == score)
        db.execute("""UPDATE sessions SET created_at='2026-10-20T00:00:00+00:00',
            decision_started_at='2026-10-20T00:04:00+00:00',ended_at='2026-10-20T00:05:00+00:00',
            decision_choice_id=?,execution_choice_id=?,score=?,review_status=? WHERE id=?""",
            (selected["id"], execution["id"], score, review_status, sid))
    return sid


def get(client, path):
    response = client.get(path, headers=auth())
    assert response.status_code == 200, response.text
    return response.json()


def archive_records(client, kind="raw"):
    return get(client, f"/api/admin/archive-export?kind={kind}&format=json")


def record_aliases(client):
    aliases = {row["id"]: row["record_id"] for row in archive_records(client)}
    assert len(set(aliases.values())) == len(aliases)
    assert all(value.startswith("R") and len(value) >= 5 and value[1:].isdigit() for value in aliases.values())
    return aliases


@pytest.mark.parametrize("corruption", ["unfinished", "answers", "score_mismatch", "legacy_manual_score",
                                        "stored_pretest_score", "model", "prompt", "form", "mode", "preview"])
def test_every_analysis_and_dialogue_export_use_exactly_the_same_completed_ids(tmp_path, corruption):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    pending = completed_fixture(client, cfg)
    historical_invalid = completed_fixture(client, cfg, score=0, review_status="invalid")
    bad = completed_fixture(client, cfg)
    with connect(cfg.database_path) as db:
        if corruption == "unfinished":
            db.execute("UPDATE sessions SET ended_at=NULL,score=NULL WHERE id=?", (bad,))
        elif corruption == "answers":
            db.execute("UPDATE sessions SET literacy_answers_json='not-json' WHERE id=?", (bad,))
        elif corruption == "score_mismatch":
            db.execute("UPDATE sessions SET score=99 WHERE id=?", (bad,))
        elif corruption == "legacy_manual_score":
            snapshot = json.loads(db.execute("SELECT task_snapshot_json FROM sessions WHERE id=?", (bad,)).fetchone()[0])
            snapshot["decision_options"] = []
            db.execute("UPDATE sessions SET task_snapshot_json=? WHERE id=?", (json.dumps(snapshot), bad))
        elif corruption == "stored_pretest_score":
            db.execute("UPDATE sessions SET literacy_score=29 WHERE id=?", (bad,))
        else:
            field, value = {"model": ("model_config_id", "old-model"), "prompt": ("prompt_version", "old-prompt"),
                            "form": ("literacy_instrument_version", "old-form"), "mode": ("run_mode", "pilot"),
                            "preview": ("record_kind", "researcher_preview")}[corruption]
            db.execute(f"UPDATE sessions SET {field}=? WHERE id=?", (value, bad))
    summary = get(client, "/api/admin/summary")
    questions = get(client, "/api/admin/pretest-analysis")
    analysis = get(client, "/api/admin/export?kind=analysis&format=json")
    dialogues = get(client, "/api/admin/dialogue-export?format=json")
    aliases = record_aliases(client)
    expected = {aliases[pending], aliases[historical_invalid]}
    assert set(summary["cohort_session_ids"]) == expected
    assert set(summary["condition_comparison"]["cohort_session_ids"]) == expected
    assert set(questions["cohort_session_ids"]) == expected
    assert {row["record_id"] for row in analysis} == expected
    assert {row["record_id"] for row in dialogues} == expected
    assert summary["analysis_n"] == questions["n"] == len(analysis) == 2
    assert sum(row["sessions"] for row in summary["strata"]) == 2
    assert sum(row["sessions"] for row in summary["literacy_distribution"]) == 2
    assert sum(row["sessions"] for row in summary["score_distribution"]) == 2
    assert any(row["decision_quality_score"] == 0 for row in analysis)
    raw = get(client, "/api/admin/export?kind=raw&format=json")
    assert {row["record_id"] for row in raw} == expected | {aliases[bad]}
    assert next(row for row in raw if row["record_id"] == aliases[bad])["analysis_eligible"] is False
    assert all("id" not in row and "messages_json" not in row for row in raw)
    archived = archive_records(client)
    assert {row["id"] for row in archived} == {pending, historical_invalid, bad}
    # Automatic inclusion never invents a reviewer or rewrites historical review status.
    with connect(cfg.database_path) as db:
        assert db.execute("SELECT review_status FROM sessions WHERE id=?", (pending,)).fetchone()[0] == "pending"
        assert db.execute("SELECT review_status FROM sessions WHERE id=?", (historical_invalid,)).fetchone()[0] == "invalid"


def test_actual_submit_counts_immediately_and_records_expose_recomputed_objective_score(tmp_path):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    sid = client.post("/api/start", json=start_payload()).json()["session_id"]
    for content in ["你好", "您好"]:
        assert client.post(f"/api/session/{sid}/message", json={"content": content}).status_code == 200
    choices = client.post(f"/api/session/{sid}/decision").json()
    assert client.post(f"/api/session/{sid}/submit", json=decision_payload(client, sid, choices)).status_code == 200
    alias = record_aliases(client)[sid]
    assert get(client, "/api/admin/summary")["cohort_session_ids"] == [alias]
    row = get(client, "/api/admin/review-queue")[0]
    assert row["record_id"] == row["id"] == alias
    assert row["review_status"] == "pending" and row["score"] == 100 and row["analysis_eligible"] is True
    assert row["prompt_version"] and row["prompt_version_blinded"] is False
    assert row["decision_choice_text"] and row["execution_choice_text"]
    with connect(cfg.database_path) as db:
        db.execute("UPDATE sessions SET score=25 WHERE id=?", (sid,))
    row = get(client, "/api/admin/review-queue")[0]
    assert row["score"] == 100 and row["analysis_eligible"] is False


def test_age_slices_and_all_age_exports_share_one_union_of_completed_records(tmp_path):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    expected = {age: completed_fixture(client, cfg, age=age) for age in ["18-20", "24-25"]}
    aliases = record_aliases(client)
    for age, sid in expected.items():
        assert get(client, f"/api/admin/summary?age_band={age}")["cohort_session_ids"] == [aliases[sid]]
        assert get(client, f"/api/admin/pretest-analysis?age_band={age}")["cohort_session_ids"] == [aliases[sid]]
    assert {row["record_id"] for row in get(client, "/api/admin/export?kind=analysis&format=json")} == {
        aliases[sid] for sid in expected.values()}


def test_dialogue_exports_preserve_sequence_content_source_and_empty_conversations(tmp_path):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    with_messages = completed_fixture(client, cfg)
    empty = completed_fixture(client, cfg, score=0)
    question = '=SUM(1,2)\n中文,"问题"'
    answer = '第一行\n第二行，"答复"'
    with connect(cfg.database_path) as db:
        db.executemany("INSERT INTO messages(session_id,role,content,action,created_at) VALUES (?,?,?,?,?)", [
            (with_messages, "user", question, "user_message", "2026-10-20T00:01:00+00:00"),
            (with_messages, "assistant", answer, "evidence", "2026-10-20T00:01:05+00:00")])
        before = [tuple(row) for row in db.execute("SELECT * FROM sessions ORDER BY id")]
    response = client.get("/api/admin/dialogue-export?format=json", headers=auth())
    aliases = record_aliases(client)
    records = {row["record_id"]: row for row in response.json()}
    assert records[aliases[empty]]["has_messages"] is False and records[aliases[empty]]["messages"] == []
    messages = records[aliases[with_messages]]["messages"]
    assert [message["message_index"] for message in messages] == [1, 2]
    assert [message["content"] for message in messages] == [question, answer]
    assert messages[0]["created_at_beijing"] == "2026-10-20T08:01:00+08:00"
    assert all("session_id" not in row and row["record_kind"] == "participant" and row["is_synthetic"] is False for row in records.values())
    archived = {row["id"]: row for row in archive_records(client)}
    assert archived[with_messages]["record_kind"] == "participant"
    assert archived[with_messages]["run_mode"] == "demo" and archived[with_messages]["is_demo"] == 1
    csv_response = client.get("/api/admin/dialogue-export?format=csv", headers=auth())
    csv_rows = list(csv.DictReader(io.StringIO(csv_response.text.lstrip("\ufeff"))))
    headers = dict(DIALOGUE_CSV_COLUMNS)
    assert len(csv_rows) == 3 and {row[headers["record_id"]] for row in csv_rows} == set(records)
    user_row = next(row for row in csv_rows if row[headers["role"]] == "参与者")
    assert user_row[headers["content"]] == "'" + question
    assert next(row for row in csv_rows if row[headers["role"]] == "AI")[headers["content"]] == answer
    empty_row = next(row for row in csv_rows if row[headers["record_id"]] == aliases[empty])
    assert empty_row[headers["has_messages"]] == "0"
    assert empty_row[headers["message_index"]] == empty_row[headers["content"]] == ""
    for downloaded in [response, csv_response,
                       client.get("/api/admin/export?kind=analysis&format=json", headers=auth()),
                       client.get("/api/admin/export?kind=raw&format=csv", headers=auth())]:
        assert "no-store" in downloaded.headers["cache-control"]
        assert 'filename="lens-' in downloaded.headers["content-disposition"]
        assert "synthetic" not in downloaded.headers["content-disposition"]
    with connect(cfg.database_path) as db:
        assert before == [tuple(row) for row in db.execute("SELECT * FROM sessions ORDER BY id")]


@pytest.mark.parametrize("format", ["csv", "json"])
def test_dialogue_download_requires_admin(tmp_path, format):
    client = TestClient(create_app(settings(tmp_path)))
    assert client.get(f"/api/admin/dialogue-export?format={format}").status_code == 401


def test_dialogue_export_rejects_unknown_formats_and_empty_download_keeps_headers(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    assert client.get("/api/admin/dialogue-export?format=html", headers=auth()).status_code == 422
    assert get(client, "/api/admin/dialogue-export?format=json") == []
    response = client.get("/api/admin/dialogue-export?format=csv", headers=auth())
    assert response.status_code == 200
    headers = next(csv.reader(io.StringIO(response.text.lstrip("\ufeff"))))
    assert headers == [label for _key, label in DIALOGUE_CSV_COLUMNS]
