"""Neutral IDs and stable, readable exports keep wide provenance in the archive."""
import csv
import io
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.clean_exports import DIALOGUE_COLUMNS, DISPLAY_VALUES, RECORD_COLUMNS, process_alert_details, record_id_map
from app.db import connect
from app.main import create_app
from test_automatic_cohort import completed_fixture
from test_flow import auth, decision_payload, settings, start_payload


def get(client, path):
    response = client.get(path, headers=auth())
    assert response.status_code == 200, response.text
    return response


def csv_rows(response):
    return list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))


@pytest.fixture
def clean_client(tmp_path):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    first = completed_fixture(client, cfg)
    second = completed_fixture(client, cfg, score=0)
    historical = completed_fixture(client, cfg)
    unfinished = client.post("/api/start", json=start_payload()).json()["session_id"]
    source = {"id": "SYN-0600", "created_at": "2030-01-15T00:00:00+00:00", "internal_fixture": "preserve-me"}
    details = [{"code": "pretest_long_identical_run", "message": "前测连续同一选项12题，建议核对作答过程。",
                "quality_version": "internal-policy", "source_fields": ["literacy_answers_json"],
                "observed_value": 12, "threshold": 12}]
    with connect(cfg.database_path) as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(sessions)")}
        for name in ["synthetic_source_json", "process_alert_details_json"]:
            if name not in columns:
                db.execute(f"ALTER TABLE sessions ADD COLUMN {name} TEXT")
        db.execute("UPDATE sessions SET synthetic_source_json=?,process_alert_details_json=? WHERE id=?",
                   (json.dumps(source), json.dumps(details, ensure_ascii=False), first))
        # Later sorting changes cannot renumber aliases assigned at /api/start.
        db.execute("UPDATE sessions SET ended_at='2026-10-20T01:00:00+00:00' WHERE id=?", (first,))
        db.execute("UPDATE sessions SET ended_at='2026-10-20T09:00:00+08:00',decision_feasible=0,decision_optimal=0 WHERE id=?", (second,))
        db.execute("UPDATE sessions SET ended_at='2026-10-21T00:00:00+00:00',model_config_id='old-config' WHERE id=?", (historical,))
        db.executemany("INSERT INTO messages(session_id,role,content,action,created_at) VALUES (?,?,?,?,?)", [
            (first, "user", '请确认报名材料。\n我怕漏掉。', "user_message", "2026-10-20T00:01:00+00:00"),
            (first, "assistant", "请先核对案例，再分别选择。", "task_guidance", "2026-10-20T00:01:02+00:00")])
    return client, cfg, {"first": first, "second": second, "historical": historical, "unfinished": unfinished}, source


def test_record_ids_are_persistent_and_join_all_reports_without_real_ids(clean_client):
    client, cfg, ids, _source = clean_client
    archive = get(client, "/api/admin/archive-export?kind=raw&format=json").json()
    aliases = {row["id"]: row["record_id"] for row in archive}
    assert aliases[ids["first"]] == "R0001"
    assert aliases[ids["second"]] == "R0002"
    assert aliases[ids["historical"]] == "R0003"
    assert aliases[ids["unfinished"]] == "R0004"
    raw = get(client, "/api/admin/export?kind=raw&format=json").json()
    analysis = get(client, "/api/admin/export?kind=analysis&format=json").json()
    dialogues = get(client, "/api/admin/dialogue-export?format=json").json()
    review = get(client, "/api/admin/review-queue").json()
    expected_analysis = sorted([aliases[ids["first"]], aliases[ids["second"]]])
    assert [row["record_id"] for row in raw] == ["R0001", "R0002", "R0003", "R0004"]
    assert [row["record_id"] for row in analysis] == expected_analysis
    assert [row["record_id"] for row in dialogues] == expected_analysis
    assert [row["record_id"] for row in review] == ["R0003", *[aliases[sid] for sid in sorted([ids["first"], ids["second"]])]]
    assert all(row["id"] == row["record_id"] for row in review)
    for rows in [raw, analysis, dialogues]:
        assert all("id" not in row and "session_id" not in row for row in rows)
        assert all(re.fullmatch(r"R\d{4}", row["record_id"]) for row in rows)
    summary = get(client, "/api/admin/summary").json()
    questions = get(client, "/api/admin/pretest-analysis").json()
    assert summary["cohort_session_ids"] == summary["condition_comparison"]["cohort_session_ids"] == expected_analysis
    assert questions["cohort_session_ids"] == expected_analysis
    assert {row["id"] for row in archive} == set(ids.values())
    with connect(cfg.database_path) as db:
        assert {row["id"] for row in db.execute("SELECT id FROM sessions")} == set(ids.values())


def test_main_download_schema_is_fixed_ordered_and_excludes_internal_provenance(clean_client):
    client, _cfg, ids, source = clean_client
    records = get(client, "/api/admin/export?kind=raw&format=json").json()
    expected_keys = [key for key, _label in RECORD_COLUMNS]
    assert all(list(row) == expected_keys for row in records)
    assert len(expected_keys) < 80 and expected_keys[0] == "record_id"
    forbidden = {"id", "session_id", "synthetic_source_json", "task_snapshot_json", "model_config_id", "model_base_url",
                 "fixture_conversation", "record_kind", "is_synthetic", "messages_json", "model_events_json",
                 "review_history_json", "review_status", "simulation_seed", "synthetic_pretest_time_seconds"}
    assert not (forbidden & set(expected_keys))
    body = json.dumps(records, ensure_ascii=False)
    assert "SYN-" not in body and "2030" not in body and "preserve-me" not in body
    response = get(client, "/api/admin/export?kind=raw&format=csv")
    reader = csv.DictReader(io.StringIO(response.text.lstrip("\ufeff")))
    assert reader.fieldnames == [label for _key, label in RECORD_COLUMNS]
    assert reader.fieldnames.count("记录编号") == 1
    assert "SYN-" not in response.text and "2030" not in response.text
    archive = {row["id"]: row for row in get(client, "/api/admin/archive-export?kind=raw&format=json").json()}
    assert json.loads(archive[ids["first"]]["synthetic_source_json"]) == source
    assert archive[ids["first"]]["task_snapshot_json"] and "messages_json" in archive[ids["first"]]
    clean = next(row for row in records if row["record_id"] == archive[ids["first"]]["record_id"])
    assert clean["process_alert_details"] == [{"code": "pretest_long_identical_run", "message": "前测连续同一选项12题，建议核对作答过程。"}]
    assert clean["pretest_s01_choice"] in {1, 2, 3, 4}
    original_answers = json.loads(archive[ids["first"]]["literacy_answers_json"])
    assert all(clean[f"pretest_{key}_choice"] == choice + 1 for key, choice in original_answers.items())


def test_zero_null_messages_and_chinese_csv_values_remain_unambiguous(clean_client):
    client, _cfg, ids, _source = clean_client
    archive = {row["id"]: row for row in get(client, "/api/admin/archive-export?format=json").json()}
    records = {row["record_id"]: row for row in get(client, "/api/admin/export?kind=analysis&format=json").json()}
    zero = records[archive[ids["second"]]["record_id"]]
    assert zero["decision_quality_score"] == zero["decision_feasible"] == zero["decision_optimal"] == 0
    assert zero["ai_prompt_count"] == 0 and zero["mean_reply_seconds"] is None
    csv_records = {row["记录编号"]: row for row in csv_rows(get(client, "/api/admin/export?kind=analysis&format=csv"))}
    assert csv_records[zero["record_id"]]["决策质量分(0-100)"] == "0.0"
    assert csv_records[zero["record_id"]]["方案可行(1是/0否)"] == "0"
    assert csv_records[zero["record_id"]]["AI主动提示次数"] == "0"
    assert csv_records[zero["record_id"]]["AI平均回复耗时(秒)"] == ""
    assert csv_records[zero["record_id"]]["教育阶段"] == "本科"
    assert zero["ended_at"] == "2026-10-20T01:00:00+00:00"
    assert zero["ended_at_beijing"] == "2026-10-20T09:00:00+08:00"
    assert archive[ids["second"]]["ended_at"] == "2026-10-20T09:00:00+08:00"
    dialogues = {row["record_id"]: row for row in get(client, "/api/admin/dialogue-export?format=json").json()}
    empty = dialogues[zero["record_id"]]
    assert empty["messages"] == [] and empty["has_messages"] is False
    csv_dialogues = csv_rows(get(client, "/api/admin/dialogue-export?format=csv"))
    empty_row = next(row for row in csv_dialogues if row["记录编号"] == empty["record_id"])
    assert empty_row["有对话(1是/0否)"] == "0" and empty_row["消息序号"] == empty_row["对话内容"] == ""


def test_dialogue_downloads_are_clean_and_retain_order_content_and_cross_table_ids(clean_client):
    client, _cfg, ids, _source = clean_client
    archive = {row["id"]: row for row in get(client, "/api/admin/archive-export?format=json").json()}
    alias = archive[ids["first"]]["record_id"]
    response = get(client, "/api/admin/dialogue-export?format=json")
    record = next(row for row in response.json() if row["record_id"] == alias)
    assert set(message["role"] for message in record["messages"]) == {"user", "assistant"}
    assert [message["message_index"] for message in record["messages"]] == [1, 2]
    assert record["messages"][0]["content"] == "请确认报名材料。\n我怕漏掉。"
    assert record["messages"][0]["created_at_beijing"] == "2026-10-20T08:01:00+08:00"
    assert "SYN-" not in response.text and "2030" not in response.text
    for key in ["synthetic_source_json", "fixture_conversation", "task_snapshot_json", "model_config_id", "record_kind", "id", "session_id"]:
        assert key not in record
    csv_response = get(client, "/api/admin/dialogue-export?format=csv")
    reader = csv.DictReader(io.StringIO(csv_response.text.lstrip("\ufeff")))
    assert reader.fieldnames == [label for _key, label in DIALOGUE_COLUMNS]
    rows = list(reader)
    assert next(row for row in rows if row["记录编号"] == alias and row["发言者"] == "参与者")["对话内容"] == record["messages"][0]["content"]
    assert {row["记录编号"] for row in rows} == {row["record_id"] for row in response.json()}


def test_csv_injection_guards_apply_to_both_record_and_dialogue_text(clean_client):
    client, cfg, ids, _source = clean_client
    text = '=SUM(1,2)\n中文,"引号"'
    with connect(cfg.database_path) as db:
        db.execute("UPDATE sessions SET submission=? WHERE id=?", (text, ids["first"]))
        db.execute("UPDATE messages SET content=? WHERE session_id=? AND role='user'", (text, ids["first"]))
    main_json = get(client, "/api/admin/export?format=json").json()
    assert any(row["submission"] == text for row in main_json)
    assert any(row["补充说明"] == "'" + text for row in csv_rows(get(client, "/api/admin/export?format=csv")))
    assert any(row["对话内容"] == "'" + text for row in csv_rows(get(client, "/api/admin/dialogue-export?format=csv")))


def test_downloads_do_not_mutate_database_or_source_audit(clean_client):
    client, cfg, _ids, _source = clean_client
    tables = ["sessions", "messages", "model_events", "review_history", "tasks", "record_aliases", "sqlite_sequence"]
    with connect(cfg.database_path) as db:
        before = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")] for table in tables}
    for route in ["/api/admin/export", "/api/admin/archive-export", "/api/admin/dialogue-export"]:
        for format in ["csv", "json"]:
            response = get(client, f"{route}?format={format}")
            assert "no-store" in response.headers["cache-control"]
    for route in ["/api/admin/summary", "/api/admin/pretest-analysis", "/api/admin/review-queue"]:
        get(client, route)
    with connect(cfg.database_path) as db:
        assert before == {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")] for table in tables}


def test_aliases_survive_new_sessions_sort_changes_restarts_and_deletion(clean_client):
    client, cfg, ids, _source = clean_client
    original = {row["id"]: row["record_id"] for row in get(client, "/api/admin/archive-export?format=json").json()}
    newest = completed_fixture(client, cfg)
    with connect(cfg.database_path) as db:
        db.execute("UPDATE sessions SET ended_at='2026-10-21T05:00:00+00:00' WHERE id=?", (ids["first"],))
    restarted = TestClient(create_app(cfg))
    current = {row["id"]: row["record_id"] for row in get(restarted, "/api/admin/archive-export?format=json").json()}
    assert {sid: current[sid] for sid in original} == original
    assert current[newest] == "R0005"
    assert restarted.delete(f"/api/session/{newest}").status_code == 200
    with connect(cfg.database_path) as db:
        assert db.execute("SELECT 1 FROM record_aliases WHERE session_id=?", (newest,)).fetchone() is None
    next_sid = restarted.post("/api/start", json=start_payload()).json()["session_id"]
    final = {row["id"]: row["record_id"] for row in get(restarted, "/api/admin/archive-export?format=json").json()}
    assert final[next_sid] == "R0006"
    assert {sid: final[sid] for sid in original} == original


def test_initial_backfill_uses_full_submission_order_then_never_reorders(clean_client):
    client, cfg, ids, _source = clean_client
    # Represent an existing database made before the alias-table migration.
    with connect(cfg.database_path) as db:
        db.execute("DROP TABLE record_aliases")
    restarted = TestClient(create_app(cfg))
    aliases = {row["id"]: row["record_id"] for row in get(restarted, "/api/admin/archive-export?format=json").json()}
    assert aliases[ids["historical"]] == "R0001"
    for position, sid in enumerate(sorted([ids["first"], ids["second"]]), 2):
        assert aliases[sid] == f"R{position:04d}"
    assert aliases[ids["unfinished"]] == "R0004"
    with connect(cfg.database_path) as db:
        db.execute("UPDATE sessions SET ended_at='2026-10-21T12:00:00+00:00' WHERE id=?", (ids["unfinished"],))
    assert {row["id"]: row["record_id"] for row in get(TestClient(create_app(cfg)), "/api/admin/archive-export?format=json").json()} == aliases


def test_missing_alias_lookup_explicitly_fails_without_allocating(clean_client):
    _client, cfg, ids, _source = clean_client
    with connect(cfg.database_path) as db:
        db.execute("DELETE FROM record_aliases WHERE session_id=?", (ids["first"],))
        before = list(db.execute("SELECT * FROM sqlite_sequence"))
        with pytest.raises(RuntimeError, match="Record alias is missing"):
            record_id_map(db)
        assert list(map(tuple, db.execute("SELECT * FROM sqlite_sequence"))) == list(map(tuple, before))
        assert db.execute("SELECT 1 FROM record_aliases WHERE session_id=?", (ids["first"],)).fetchone() is None


def test_replaying_start_request_allocates_only_one_alias(tmp_path):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    payload = {**start_payload(), "request_id": "a-stable-start-request"}
    first = client.post("/api/start", json=payload)
    second = client.post("/api/start", json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json()["session_id"] == second.json()["session_id"]
    with connect(cfg.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM record_aliases").fetchone()[0] == 1
        assert record_id_map(db) == {first.json()["session_id"]: "R0001"}


@pytest.mark.parametrize("route", ["export", "archive-export", "dialogue-export"])
def test_every_download_stays_private_and_rejects_unknown_format(tmp_path, route):
    client = TestClient(create_app(settings(tmp_path)))
    assert client.get(f"/api/admin/{route}?format=json").status_code == 401
    assert client.get(f"/api/admin/{route}?format=html", headers=auth()).status_code == 422


@pytest.mark.parametrize("route", ["export", "archive-export"])
def test_record_downloads_reject_unknown_scope(tmp_path, route):
    client = TestClient(create_app(settings(tmp_path)))
    assert client.get(f"/api/admin/{route}?kind=internal", headers=auth()).status_code == 422


def test_empty_standard_downloads_keep_the_same_chinese_headers(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    assert get(client, "/api/admin/export?format=json").json() == []
    main = get(client, "/api/admin/export?format=csv")
    dialogue = get(client, "/api/admin/dialogue-export?format=csv")
    assert next(csv.reader(io.StringIO(main.text.lstrip("\ufeff")))) == [label for _key, label in RECORD_COLUMNS]
    assert next(csv.reader(io.StringIO(dialogue.text.lstrip("\ufeff")))) == [label for _key, label in DIALOGUE_COLUMNS]


@pytest.mark.parametrize("has_empty_details_column", [False, True])
def test_ordinary_submit_process_flags_remain_visible_in_review_and_clean_exports(tmp_path, has_empty_details_column):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    if has_empty_details_column:
        with connect(cfg.database_path) as db:
            db.execute("ALTER TABLE sessions ADD COLUMN process_alert_details_json TEXT NOT NULL DEFAULT '[]'")
    sid = client.post("/api/start", json=start_payload()).json()["session_id"]
    for content in ["你好", "您好"]:
        assert client.post(f"/api/session/{sid}/message", json={"content": content}).status_code == 200
    choices = client.post(f"/api/session/{sid}/decision").json()
    assert client.post(f"/api/session/{sid}/submit", json=decision_payload(client, sid, choices)).status_code == 200
    archived = next(row for row in get(client, "/api/admin/archive-export?format=json").json() if row["id"] == sid)
    alias = archived["record_id"]
    review = next(row for row in get(client, "/api/admin/review-queue").json() if row["record_id"] == alias)
    clean = next(row for row in get(client, "/api/admin/export?kind=analysis&format=json").json() if row["record_id"] == alias)
    flags = json.loads(archived["flags_json"])
    assert {"no_evidence_retrieved", "very_short_dialogue"} <= set(flags)
    assert review["process_alert_details"] == clean["process_alert_details"] == archived["process_alert_details"]
    assert {detail["code"] for detail in clean["process_alert_details"]} == set(flags)
    assert all(set(detail) == {"code", "message"} for detail in clean["process_alert_details"])
    csv_record = next(row for row in csv_rows(get(client, "/api/admin/export?kind=analysis&format=csv")) if row["记录编号"] == alias)
    assert "没有取得资料线索" in csv_record["过程提醒"] and "对话极短" in csv_record["过程提醒"]


def test_saved_process_evidence_is_authoritative_and_no_pretest_speed_is_inferred():
    detail = {"code": "no_evidence_retrieved", "message": "保存的资料揭示记录为0条。", "observed_value": 0,
              "source_fields": ["model_events.evidence_reveal"]}
    record = {"process_alert_details_json": json.dumps([detail]), "flags_json": '["very_short_dialogue"]'}
    assert process_alert_details(record) == [detail]
    assert process_alert_details({"process_alert_details_json": "[]", "flags_json": '["pretest_fast_completion"]',
                                  "synthetic_pretest_time_seconds": 83}) == []


def test_education_display_labels_cover_every_current_form_code():
    html = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text()
    select = re.search(r'<select id="education-stage">(.*?)</select>', html).group(1)
    form_labels = dict(re.findall(r'<option value="([^"]+)">([^<]+)</option>', select))
    assert DISPLAY_VALUES["education_stage"] == form_labels
