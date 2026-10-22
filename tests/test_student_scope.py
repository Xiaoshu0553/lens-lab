"""University eligibility is explicit; old records never acquire student status."""
import csv
import io
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.age_forms import form_for as legacy_form_for
from app.age_tasks import task_for as legacy_task_for
from app.db import connect
from app.main import create_app
from app.seed import seed_demo
from app.student_materials import AGE_BANDS, FORM_VERSION
from test_automatic_cohort import completed_fixture
from test_flow import auth, settings, start_payload


def get(client, path):
    response = client.get(path, headers=auth())
    assert response.status_code == 200, response.text
    return response


def excluded_from_current_reports(client, session_id):
    archive = get(client, "/api/admin/archive-export?kind=raw&format=json").json()
    record = next(row for row in archive if row["id"] == session_id)
    assert record["analysis_eligible"] is False and record["in_current_cohort"] is False
    alias = record["record_id"]
    for age in AGE_BANDS:
        summary = get(client, f"/api/admin/summary?age_band={age}").json()
        assert alias not in summary["cohort_session_ids"]
        assert alias not in summary["condition_comparison"]["cohort_session_ids"]
        questions = get(client, f"/api/admin/pretest-analysis?age_band={age}").json()
        assert alias not in questions["cohort_session_ids"]
    for path in ("/api/admin/export?kind=analysis&format=json", "/api/admin/archive-export?kind=analysis&format=json",
                 "/api/admin/dialogue-export?format=json"):
        assert alias not in {row["record_id"] for row in get(client, path).json()}
    for row in get(client, "/api/admin/review-queue").json():
        if row["record_id"] == alias:
            assert row["analysis_eligible"] is False
    return record


@pytest.mark.parametrize("age", ["12-14", "15-17", "18-24", "25-34", "35-44", "55-64", "75-plus", "17", "26", "18–20"])
def test_new_start_rejects_every_out_of_scope_or_legacy_age_without_record_creation(tmp_path, age):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    response = client.post("/api/start", json={**start_payload(), "age_band": age})
    assert response.status_code == 422
    with connect(cfg.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


@pytest.mark.parametrize("stage", ["primary", "junior", "senior", "graduate", "other", "alumni", "", "BACHELOR"])
def test_new_start_requires_current_junior_college_or_bachelor_stage(tmp_path, stage):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    response = client.post("/api/start", json={**start_payload(), "education_stage": stage})
    assert response.status_code == 422
    with connect(cfg.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


@pytest.mark.parametrize("value", [False, "true", "false", 1, 0, None])
def test_student_confirmation_is_a_required_strict_true_boolean(tmp_path, value):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    response = client.post("/api/start", json={**start_payload(), "student_confirmed": value})
    assert response.status_code == 422
    with connect(cfg.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_omitted_confirmation_never_defaults_to_eligible_student(tmp_path):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    payload = start_payload()
    payload.pop("student_confirmed")
    assert client.post("/api/start", json=payload).status_code == 422
    with connect(cfg.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


@pytest.mark.parametrize("age", list(AGE_BANDS))
@pytest.mark.parametrize("stage", ["college", "bachelor"])
def test_all_six_current_demographic_combinations_are_accepted_and_saved(tmp_path, age, stage):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    response = client.post("/api/start", json={**start_payload(age_band=age), "education_stage": f" {stage} "})
    assert response.status_code == 200, response.text
    assert response.json()["student_confirmed"] is True
    with connect(cfg.database_path) as db:
        row = db.execute("SELECT * FROM sessions").fetchone()
        assert row["student_confirmed"] == 1 and row["age_band"] == age and row["education_stage"] == stage
        assert row["literacy_instrument_version"] == FORM_VERSION


@pytest.mark.parametrize("field,value,reason", [
    ("student_confirmed", 0, "未确认当前在读大学生身份"),
    ("student_confirmed", 2, "未确认当前在读大学生身份"),
    ("student_confirmed", "true", "未确认当前在读大学生身份"),
    ("education_stage", "graduate", "不属于在读专科或本科阶段"),
    ("education_stage", "other", "不属于在读专科或本科阶段"),
    ("age_band", "18-24", "不属于18–25岁大学生年龄范围"),
    ("age_band", "25-34", "不属于18–25岁大学生年龄范围"),
])
def test_saved_completed_and_manually_valid_records_still_require_student_eligibility(tmp_path, field, value, reason):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    sid = completed_fixture(client, cfg, review_status="valid")
    with connect(cfg.database_path) as db:
        db.execute(f"UPDATE sessions SET {field}=? WHERE id=?", (value, sid))
        before = dict(db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())
    record = excluded_from_current_reports(client, sid)
    assert reason in json.loads(record["analysis_exclusion_reasons_json"])
    # Restarting or a historical manual 'valid' review cannot supply consent.
    client = TestClient(create_app(cfg))
    excluded_from_current_reports(client, sid)
    with connect(cfg.database_path) as db:
        assert dict(db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()) == before


def test_unconfirmed_existing_degree_is_not_relabelled_as_currently_enrolled_in_exports(tmp_path):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    sid = completed_fixture(client, cfg, review_status="valid")
    with connect(cfg.database_path) as db:
        db.execute("UPDATE sessions SET student_confirmed=0 WHERE id=?", (sid,))
    record = excluded_from_current_reports(client, sid)
    raw = get(client, "/api/admin/export?kind=raw&format=json").json()[0]
    assert raw["student_confirmed"] == 0 and raw["education_stage"] == "bachelor"
    flat = list(csv.DictReader(io.StringIO(get(client, "/api/admin/export?kind=raw&format=csv").text.lstrip("\ufeff"))))[0]
    assert flat["教育阶段"] == "本科" and "在读本科" not in flat.values()
    assert record["review_status"] == "valid"


def test_schema_upgrade_defaults_existing_completed_records_to_unconfirmed_and_preserves_answers(tmp_path):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    sid = completed_fixture(client, cfg, review_status="valid")
    with connect(cfg.database_path) as db:
        before = dict(db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())
        before.pop("student_confirmed")
        db.execute("ALTER TABLE sessions DROP COLUMN student_confirmed")
    client = TestClient(create_app(cfg))
    with connect(cfg.database_path) as db:
        after = dict(db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())
        assert after.pop("student_confirmed") == 0
        assert after == before
    excluded_from_current_reports(client, sid)


def test_legacy_participant_snapshot_answers_and_education_remain_historical_after_upgrade(tmp_path):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    sid = completed_fixture(client, cfg, review_status="valid")
    seed_demo(cfg.database_path)
    form_version, items = legacy_form_for("18-24")
    answers = {item["id"]: item["correct"] for item in items}
    snapshot = legacy_task_for("18-24", "first_3")
    with connect(cfg.database_path) as db:
        db.execute("""UPDATE sessions SET task_id=?,age_band='18-24',education_stage='bachelor',student_confirmed=0,
            design_version='literacy-age-v5',literacy_instrument_version=?,literacy_answers_json=?,task_snapshot_json=? WHERE id=?""",
                   (snapshot["id"], form_version, json.dumps(answers, ensure_ascii=False), json.dumps(snapshot, ensure_ascii=False), sid))
        before = dict(db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())
    client = TestClient(create_app(cfg))
    record = excluded_from_current_reports(client, sid)
    assert record["literacy_instrument_version"] == form_version != FORM_VERSION
    assert record["student_confirmed"] == 0
    with connect(cfg.database_path) as db:
        assert dict(db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()) == before
        assert db.execute("SELECT active FROM tasks WHERE id=?", (snapshot["id"],)).fetchone()[0] == 0


@pytest.mark.parametrize("field,value", [("is_synthetic", 1), ("data_origin", "synthetic"),
                                         ("record_kind", "synthetic"), ("platform_analysis_eligible", 0)])
def test_explicit_synthetic_or_ineligible_marker_never_enters_participant_reports(tmp_path, field, value):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    sid = completed_fixture(client, cfg, review_status="valid")
    with connect(cfg.database_path) as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(sessions)")}
        if field not in columns:
            db.execute(f"ALTER TABLE sessions ADD COLUMN {field} {'TEXT' if field == 'data_origin' else 'INTEGER'}")
        db.execute(f"UPDATE sessions SET {field}=? WHERE id=?", (value, sid))
    record = excluded_from_current_reports(client, sid)
    assert record["analysis_eligible"] is False and record["platform_analysis_eligible"] is False
    if field != "platform_analysis_eligible":
        assert record["is_synthetic"] is True and record["data_origin"] == "synthetic"
    clean = get(client, "/api/admin/export?kind=raw&format=json").json()[0]
    assert clean["analysis_eligible"] is False and clean["platform_analysis_eligible"] is False


def test_unconfirmed_saved_rows_do_not_fill_group_caps_or_bias_current_assignment(tmp_path):
    cfg = replace(settings(tmp_path), group_cap=1)
    client = TestClient(create_app(cfg))
    old_ids = [client.post("/api/start", json=start_payload()).json()["session_id"] for _ in range(2)]
    with connect(cfg.database_path) as db:
        assert {row[0] for row in db.execute("SELECT group_id FROM sessions")} == {"S", "A"}
        db.executemany("UPDATE sessions SET student_confirmed=0 WHERE id=?", ((sid,) for sid in old_ids))
    assert get(client, "/api/admin/summary").json()["groups"] == []
    response = client.post("/api/start", json=start_payload())
    assert response.status_code == 200, response.text
    assert response.json()["session_id"] not in old_ids


@pytest.mark.parametrize("value", [0, 2, "true"])
def test_session_response_does_not_truthiness_cast_invalid_confirmation_to_true(tmp_path, value):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    sid = completed_fixture(client, cfg)
    with connect(cfg.database_path) as db:
        db.execute("UPDATE sessions SET student_confirmed=? WHERE id=?", (value, sid))
    response = client.get(f"/api/session/{sid}")
    assert response.status_code == 200
    assert response.json()["student_confirmed"] is False
