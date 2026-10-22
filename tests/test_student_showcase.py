"""The new university fixtures are independent, reproducible and isolated."""
import copy
import json
from collections import Counter
from dataclasses import replace

import pytest

import app.student_showcase as showcase
from app.config import ROOT, Settings
from app.db import connect, init_db
from app.student_materials import AGE_BANDS, FORM_VERSION, score_form
from app.student_seed import seed_student_materials
from app.process_alerts import derive_process_alerts


def student_settings(tmp_path):
    return Settings(database_path=tmp_path / "university-showcase-data" / "showcase.sqlite3",
                    study_mode="demo", admin_password="test-admin", model_api_key="",
                    model_base_url="DEMO", model_name="DEMO-RULE", model_temperature=0,
                    assignment_seed="test-student-seed", showcase_mode=True)


def seed(conn, settings):
    return showcase.seed_student_showcase(conn, settings, "student-test-config", ("standard-prompt", "adaptive-prompt"))


def test_new126_are_students_and_all_scores_recompute_from_new_materials():
    data = showcase.get_student_dataset()
    rows = data["records"]
    assert len(rows) == 126 and len({row["id"] for row in rows}) == 126
    assert data["manifest"]["no_legacy_records_reused"] is True
    assert data["manifest"]["no_target_means_or_seed_retries"] is True
    assert Counter(row["age_band"] for row in rows) == {age: 42 for age in AGE_BANDS}
    for age in AGE_BANDS:
        assert Counter(row["group_id"] for row in rows if row["age_band"] == age) == {"S": 21, "A": 21}
    for row in rows:
        assert row["education_stage"] in {"college", "bachelor"} and row["student_confirmed"] == 1
        low, high = map(int, row["age_band"].split("-"))
        assert 18 <= low <= row["exact_age"] <= high <= 25
        assert row["literacy_instrument_version"] == FORM_VERSION
        assert score_form(row["age_band"], row["literacy_answers"]) == (
            row["literacy_score"], row["literacy_percent"], row["literacy_band"], row["anchor_score"])
        task = data["frozen_materials"][row["age_band"]]["tasks"][row["scenario_id"]]
        decision = next(item for item in task["decision_options"] if item["id"] == row["decision_choice_id"])
        execution = next(item for item in task["execution_options"] if item["id"] == row["execution_choice_id"])
        assert row["score"] == decision["score"] + execution["score"] == sum(row["score_components"].values())
        assert row["decision_feasible"] == int(decision["success"] and execution["success"])
        assert row["decision_optimal"] == int(decision["goal_met"] and execution["success"])
        assert row["is_synthetic"] == 1 and row["platform_analysis_eligible"] == 0
        assert row["actual_model_latency_ms"] is None and row["mean_reply_seconds"] is None
        assert row["timestamp_is_simulated"] == 1
        assert not any(event["event_type"] == "reply_latency" for event in row["model_events"])
        assert all(event["detail"]["conversation_source"] == "authored_template_not_model_output"
                   for event in row["model_events"])


def test_mutating_return_value_does_not_change_frozen_batch():
    expected = showcase.get_student_dataset()
    modified = showcase.get_student_dataset()
    modified["records"][0]["score"] = -999
    modified["frozen_materials"].clear()
    assert showcase.get_student_dataset() == expected


def test_v2_source_policy_is_explicit_and_timestamps_are_not_future_pretest_observations():
    data = showcase.get_student_dataset()
    assert showcase.BATCH_ID == "student-126-20261022-v2"
    manifest = data["manifest"]
    assert manifest["evidence_policy_is_simulated_assumption"] is True
    assert manifest["evidence_cards_per_record"] == {"S": 3, "A": 5}
    assert "人工模拟假设" in manifest["evidence_policy"] and "不是真实模型" in manifest["evidence_policy"]
    assert manifest["no_group_score_effect"] is True and manifest["pretest_duration_recorded"] is False
    from datetime import datetime, timezone
    for row in data["records"]:
        assert row["id"].startswith("SYNTH-STUDENT-V2-")
        assert row["source_batch_id"] == showcase.BATCH_ID
        assert row["fixture_render_version"] == "student-authored-dialogue-v2"
        assert row["synthetic_pretest_time_seconds"] is None
        assert datetime.fromisoformat(row["ended_at"]) < datetime(2026, 10, 22, 7, tzinfo=timezone.utc)


def test_more_adaptive_clues_are_actual_card_replies_in_every_age_band():
    data = showcase.get_student_dataset()
    totals = Counter()
    for row in data["records"]:
        cards = {card["id"]: card for card in
                 data["frozen_materials"][row["age_band"]]["tasks"][row["scenario_id"]]["evidence_cards"]}
        reveals = [event for event in row["model_events"] if event["event_type"] == "evidence_reveal"]
        replies = [message for message in row["messages"] if message["role"] == "assistant" and message["action"] == "evidence"]
        assert row["evidence_reveals"] == len(reveals) == len(replies) == (5 if row["group_id"] == "A" else 3)
        assert len({event["detail"]["card_id"] for event in reveals}) == len(reveals)
        for event in reveals:
            card_id = event["detail"]["card_id"]
            matching = [reply for reply in replies if reply["created_at"] == event["created_at"]]
            assert len(matching) == 1
            assert f"资料 {card_id}｜" in matching[0]["content"]
            assert cards[card_id]["body"] in matching[0]["content"]
            totals[(row["age_band"], row["group_id"], card_id)] += 1
    for age in AGE_BANDS:
        for card_id in showcase.EVIDENCE_CARD_IDS:
            assert totals[(age, "A", card_id)] == 21 > totals[(age, "S", card_id)] > 0
    assert sum(value for (_age, group, _card), value in totals.items() if group == "A") == 315
    assert sum(value for (_age, group, _card), value in totals.items() if group == "S") == 189


def test_adaptive_fixture_clarifies_one_fact_per_turn_and_resumes_original_request():
    for row in showcase.get_student_dataset()["records"]:
        messages = row["messages"]
        clarifications = [message for message in messages if message["action"] == "prompt_clarification"]
        if row["group_id"] == "S":
            assert not clarifications
            continue
        assert len(clarifications) == row["ai_clarification_count"] == 2
        assert messages[1] == clarifications[0] and messages[3] == clarifications[1]
        assert "名额" in messages[1]["content"] and "每周" not in messages[1]["content"]
        assert "每周" in messages[3]["content"] and "名额" not in messages[3]["content"]
        assert messages[2]["role"] == messages[4]["role"] == "user"
        assert "名额" in messages[2]["content"] and "每周" in messages[4]["content"]
        assert "继续" in messages[5]["content"] and "原问题" in messages[5]["content"]
        assert "参加方案应为" in messages[-1]["content"] and "办理应按" in messages[-1]["content"]


def test_source_process_alerts_are_recomputed_from_saved_observations():
    data = showcase.get_student_dataset()
    with_notes = without_notes = 0
    for row in data["records"]:
        snapshot = data["frozen_materials"][row["age_band"]]["tasks"][row["scenario_id"]]
        actual = derive_process_alerts({**row, "literacy_answers_json": row["literacy_answers"],
                                        "task_snapshot_json": snapshot}, row["messages"])
        assert actual == row["process_alert_details"]
        assert row["flags"] == [detail["code"] for detail in actual]
        uncertain = sum(choice == 3 for choice in row["literacy_answers"].values())
        note = next((detail for detail in actual if detail["code"] == "pretest_uncertain_answers"), None)
        assert bool(note) == bool(uncertain)
        if note:
            assert note["observed_value"] == uncertain
        assert all(detail["is_simulated"] == 1 and detail["affects_analysis"] is False for detail in actual)
        with_notes += bool(actual)
        without_notes += not actual
    assert with_notes > 0 and without_notes > 0


def test_reassigning_arms_cannot_change_outcomes_or_answers(monkeypatch):
    original = showcase.get_student_dataset()
    try:
        monkeypatch.setattr(showcase, "ARM_SEED", "independent-audit-arm-seed")
        showcase._generated_dataset.cache_clear()
        reassigned = showcase.get_student_dataset()
        assert any(a["group_id"] != b["group_id"] for a, b in zip(original["records"], reassigned["records"]))
        fields = ("literacy_answers", "literacy_score", "score", "decision_choice_id", "execution_choice_id",
                  "exact_age", "education_stage", "created_at", "ended_at", "synthetic_pretest_time_seconds")
        assert [[row[key] for key in fields] for row in original["records"]] == [
            [row[key] for key in fields] for row in reassigned["records"]]
    finally:
        monkeypatch.undo()
        showcase._generated_dataset.cache_clear()


def test_seed_is_idempotent_and_preserves_independent_interactive_rows(tmp_path):
    settings = student_settings(tmp_path)
    init_db(settings.database_path)
    with connect(settings.database_path) as conn:
        seed_student_materials(conn)
        manifest = seed(conn, settings)
        first = [dict(row) for row in conn.execute("SELECT * FROM sessions ORDER BY id")]
        original = copy.deepcopy(first[0])
        original.update(id="independent-preview", source_batch_id=None, record_kind="researcher_preview")
        keys = list(original)
        conn.execute(f"INSERT INTO sessions ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", list(original.values()))
        assert seed(conn, settings) == manifest
        assert [dict(row) for row in conn.execute("SELECT * FROM sessions WHERE source_batch_id=? ORDER BY id", (showcase.BATCH_ID,))] == first
        assert conn.execute("SELECT record_kind FROM sessions WHERE id='independent-preview'").fetchone()[0] == "researcher_preview"
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 127
        assert conn.execute("SELECT COUNT(*) FROM model_events WHERE event_type='reply_latency'").fetchone()[0] == 0
        stored = json.loads(conn.execute("SELECT manifest_json FROM student_showcase_imports").fetchone()[0])
        assert stored["records_sha256"] == showcase.get_student_dataset()["manifest"]["records_sha256"]
        source_by_id = {row["id"]: row for row in showcase.get_student_dataset()["records"]}
        for row in first:
            source = source_by_id[row["id"]]
            assert json.loads(row["process_alert_details_json"]) == source["process_alert_details"]
            assert json.loads(row["flags_json"]) == source["flags"]


@pytest.mark.parametrize("change", ["score", "message", "runtime"])
def test_changed_fixture_or_runtime_is_rejected_without_overwriting(tmp_path, change):
    settings = student_settings(tmp_path)
    init_db(settings.database_path)
    with connect(settings.database_path) as conn:
        seed_student_materials(conn)
        seed(conn, settings)
        if change == "score":
            conn.execute("UPDATE sessions SET score=-999 WHERE id=(SELECT id FROM sessions ORDER BY id LIMIT 1)")
        elif change == "message":
            conn.execute("UPDATE messages SET content='administrator edit' WHERE id=(SELECT MIN(id) FROM messages)")
        elif change == "runtime":
            settings = replace(settings, assignment_seed="new-runtime-seed")
        with pytest.raises(ValueError, match="已修改|已变化"):
            seed(conn, settings)
        if change == "score":
            assert conn.execute("SELECT MIN(score) FROM sessions").fetchone()[0] == -999
        if change == "message":
            assert conn.execute("SELECT content FROM messages ORDER BY id LIMIT 1").fetchone()[0] == "administrator edit"


def test_old_nonempty_database_is_not_relabelled_or_overwritten(tmp_path):
    settings = student_settings(tmp_path)
    init_db(settings.database_path)
    with connect(settings.database_path) as conn:
        seed_student_materials(conn)
        seed(conn, settings)
        conn.execute("DROP TABLE student_showcase_imports")
        before = [tuple(row) for row in conn.execute("SELECT * FROM sessions ORDER BY id")]
        with pytest.raises(ValueError, match="已有会话"):
            seed(conn, settings)
        assert [tuple(row) for row in conn.execute("SELECT * FROM sessions ORDER BY id")] == before


def test_v1_receipt_is_not_reused_to_overwrite_an_existing_database(tmp_path):
    settings = student_settings(tmp_path)
    init_db(settings.database_path)
    with connect(settings.database_path) as conn:
        seed_student_materials(conn)
        seed(conn, settings)
        conn.execute("UPDATE student_showcase_imports SET batch_id='student-126-20261022-v1'")
        before = [tuple(row) for row in conn.execute("SELECT * FROM sessions ORDER BY id")]
        with pytest.raises(ValueError, match="已有会话"):
            seed(conn, settings)
        assert [tuple(row) for row in conn.execute("SELECT * FROM sessions ORDER BY id")] == before


def test_seed_guard_rejects_real_legacy_and_mismatched_database_paths(tmp_path):
    settings = student_settings(tmp_path)
    for path in (tmp_path / "lens.sqlite3", ROOT / "data" / "lens.sqlite3", ROOT / "showcase-data" / "showcase.sqlite3"):
        with pytest.raises(ValueError):
            showcase.guard_student_showcase_database(replace(settings, database_path=path))
    with pytest.raises(ValueError):
        showcase.guard_student_showcase_database(replace(settings, study_mode="pilot"))
    other = tmp_path / "other" / "showcase.sqlite3"
    init_db(other)
    with connect(other) as conn, pytest.raises(ValueError, match="连接与配置"):
        seed(conn, settings)


def test_current_application_displays_all126_current_fixtures_without_model_latency(tmp_path):
    import base64
    from fastapi.testclient import TestClient
    from app.main import create_app

    settings = student_settings(tmp_path)
    headers = {"Authorization": "Basic " + base64.b64encode(b"admin:test-admin").decode()}
    client = TestClient(create_app(settings))
    rows = client.get("/api/admin/archive-export?kind=raw&format=json", headers=headers).json()
    assert len(rows) == 126
    assert all(row["source_batch_id"] == showcase.BATCH_ID and row["in_current_cohort"]
               and row["platform_analysis_eligible"] == 0 for row in rows)
    assert all(row["mean_reply_seconds"] is None for row in rows)
    assert all(row["analysis_eligible"] is False and row["showcase_analysis_eligible"] is True for row in rows)
    expected_note_count = showcase.get_student_dataset()["manifest"]["process_alert_record_count"]
    assert sum(bool(row["process_alert_details"]) for row in rows) == expected_note_count
    assert all(detail["code"] == "pretest_uncertain_answers" and detail["is_simulated"] == 1
               for row in rows for detail in row["process_alert_details"])
    for path in ("/api/admin/export?kind=raw&format=json", "/api/admin/export?kind=analysis&format=json",
                 "/api/admin/dialogue-export?format=json"):
        exported = client.get(path, headers=headers).json()
        assert len(exported) == 126
        assert all(row["is_synthetic"] is True and row["data_origin"] == "synthetic"
                   and row["record_kind"] == "synthetic" and row["timestamp_is_simulated"] is True
                   and row["platform_analysis_eligible"] is False and row["analysis_eligible"] is False
                   and row["showcase_analysis_eligible"] is True for row in exported)
    flat_response = client.get("/api/admin/export?kind=analysis&format=csv", headers=headers)
    import csv
    import io
    flat_rows = list(csv.DictReader(io.StringIO(flat_response.text.lstrip("\ufeff"))))
    assert len(flat_rows) == 126
    assert all(row["合成数据(1是/0否)"] == row["模拟时间(1是/0否)"] == "1"
               and row["纳入正式参与者分析"] == row["可纳入正式参与者分析(1是/0否)"] == "0"
               and row["可用于模拟展示统计(1是/0否)"] == "1" for row in flat_rows)
    for age in AGE_BANDS:
        response = client.get("/api/admin/summary", params={"age_band": age}, headers=headers)
        assert response.status_code == 200
        summary = response.json()
        assert summary["showcase_totals"] == {"sessions": 126, "submitted": 126, "unsubmitted": 0}
        assert summary["completed_n"] == summary["analysis_n"] == 42
        assert {group["group_id"]: group["completed_n"] for group in summary["groups"]} == {"S": 21, "A": 21}
        assert summary["mean_reply_seconds"] is None
        clues = {(item["group_id"], item["card_id"]): item["reveals"]
                 for item in summary["evidence_reveal_distribution"]}
        for card_id in showcase.EVIDENCE_CARD_IDS:
            assert clues[("A", card_id)] == 21 > clues[("S", card_id)] > 0
    assert client.get("/api/admin/summary?age_band=18-24", headers=headers).status_code == 422
    # A restart checks the same batch and does not duplicate generated records.
    TestClient(create_app(settings))
    with connect(settings.database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 126
