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


def test_v3_source_policy_is_explicit_and_timestamps_are_not_future_pretest_observations():
    data = showcase.get_student_dataset()
    assert showcase.BATCH_ID == "student-126-20261022-v3"
    manifest = data["manifest"]
    assert manifest["evidence_policy_is_simulated_assumption"] is True
    assert manifest["evidence_reveals_per_record_ranges"] == {"S": [1, 5], "A": [2, 8]}
    assert manifest["dialogue_seed"] == "student-dialogue-v3"
    assert manifest["parent_source_batch_id"] == "student-126-20261022-v2"
    assert "人工模拟假设" in manifest["evidence_policy"] and "不是真实模型" in manifest["evidence_policy"]
    assert manifest["no_group_score_effect"] is True and manifest["pretest_duration_recorded"] is False
    from datetime import datetime, timezone
    for row in data["records"]:
        assert row["id"].startswith("SYNTH-STUDENT-V3-")
        assert row["source_batch_id"] == showcase.BATCH_ID
        assert row["fixture_render_version"] == "student-authored-dialogue-v3"
        assert row["synthetic_pretest_time_seconds"] is None
        assert datetime.fromisoformat(row["ended_at"]) < datetime(2026, 10, 22, 7, tzinfo=timezone.utc)


def test_v3_preserves_the_independently_frozen_v2_personal_profiles():
    import hashlib
    # This fingerprint was frozen from V2 before the process generator changed.
    # An ID-prefix change must not silently generate new answers or demographics.
    fields = ("age_band", "exact_age", "education_stage", "student_confirmed", "topic_familiarity",
              "literacy_answers", "literacy_score", "literacy_percent", "literacy_band", "anchor_score",
              "group_id", "interaction_condition", "scenario_id", "decision_choice_id", "execution_choice_id",
              "decision_feasible", "decision_optimal", "score", "created_at", "decision_started_at", "ended_at")
    def digest(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":")).encode()).hexdigest()
    data = showcase.get_student_dataset()
    profiles = []
    for row in data["records"]:
        assert row["parent_source_batch_id"] == "student-126-20261022-v2"
        assert row["parent_record_id"] == row["id"].replace("STUDENT-V3-", "STUDENT-V2-", 1)
        assert row["parent_profile_sha256"] == digest({key: row[key] for key in fields})
        profiles.append({"parent_record_id": row["parent_record_id"], **{key: row[key] for key in fields}})
    assert digest(profiles) == data["manifest"]["parent_profiles_sha256"] == (
        "01b5c2361e08d7a165623978732f6e4d5a16950732e11d1b3a489091f4daee40")


def test_more_adaptive_clues_are_actual_card_replies_in_every_age_band():
    data = showcase.get_student_dataset()
    totals = Counter()
    for row in data["records"]:
        cards = {card["id"]: card for card in
                 data["frozen_materials"][row["age_band"]]["tasks"][row["scenario_id"]]["evidence_cards"]}
        reveals = [event for event in row["model_events"] if event["event_type"] == "evidence_reveal"]
        replies = [message for message in row["messages"] if message["role"] == "assistant" and message["action"] == "evidence"]
        assert row["evidence_reveals"] == len(reveals) == len(replies)
        assert (2 <= len(reveals) <= 8) if row["group_id"] == "A" else (1 <= len(reveals) <= 5)
        source_ids = [event["detail"]["card_id"] for event in reveals]
        assert source_ids == row["fixture_dialogue_plan"]["card_ids"]
        assert max(Counter(source_ids).values()) <= 2
        assert all(left != right for left, right in zip(source_ids, source_ids[1:]))
        assert row["distinct_evidence_cards"] == len(set(source_ids))
        for event in reveals:
            card_id = event["detail"]["card_id"]
            matching = [reply for reply in replies if reply["created_at"] == event["created_at"]]
            assert len(matching) == 1
            assert f"资料 {card_id}｜" in matching[0]["content"]
            assert cards[card_id]["body"] in matching[0]["content"]
            assert event["detail"]["source_batch_id"] == showcase.BATCH_ID
            assert event["detail"]["parent_record_id"] == row["parent_record_id"]
            totals[(row["age_band"], row["group_id"], card_id)] += 1
    for age in AGE_BANDS:
        for card_id in showcase.EVIDENCE_CARD_IDS:
            assert totals[(age, "A", card_id)] > totals[(age, "S", card_id)] > 0
    assert sum(value for (_age, group, _card), value in totals.items() if group == "A") == 308
    assert sum(value for (_age, group, _card), value in totals.items() if group == "S") == 195
    assert [totals[("18-20", "A", key)] for key in showcase.EVIDENCE_CARD_IDS] == [16, 16, 30, 22, 19]
    assert [totals[("21-23", "A", key)] for key in showcase.EVIDENCE_CARD_IDS] == [27, 22, 19, 15, 14]
    assert [totals[("24-25", "A", key)] for key in showcase.EVIDENCE_CARD_IDS] == [21, 28, 14, 19, 26]
    assert [totals[("18-20", "S", key)] for key in showcase.EVIDENCE_CARD_IDS] == [14, 7, 19, 14, 12]
    assert [totals[("21-23", "S", key)] for key in showcase.EVIDENCE_CARD_IDS] == [16, 12, 16, 8, 7]
    assert [totals[("24-25", "S", key)] for key in showcase.EVIDENCE_CARD_IDS] == [11, 21, 7, 9, 22]


def test_adaptive_fixture_clarifies_one_fact_per_turn_and_resumes_original_request():
    clarification_totals = Counter()
    paths, openings, question_orders = set(), set(), set()
    for row in showcase.get_student_dataset()["records"]:
        messages = row["messages"]
        clarifications = [message for message in messages if message["action"] == "prompt_clarification"]
        initial = row["fixture_dialogue_plan"]["initial_facts"]
        known = {"prior", "weekly"} if initial == "both" else set() if initial == "none" else {initial}
        assert row["user_turns"] <= 12
        assert len(messages) == 2 * row["user_turns"]
        assert len({message["created_at"] for message in messages}) == len(messages)
        assert [message["created_at"] for message in messages] == sorted(message["created_at"] for message in messages)
        assert row["created_at"] < messages[0]["created_at"] < messages[-1]["created_at"] < row["decision_started_at"]
        assert ("名额" in messages[0]["content"]) == ("prior" in known)
        assert any(word in messages[0]["content"] for word in ("每周", "一周")) == ("weekly" in known)
        paths.add((row["group_id"], initial))
        openings.add(messages[0]["content"])
        if row["group_id"] == "S":
            assert not clarifications
            assert all("？" not in message["content"] for message in messages if message["role"] == "assistant")
            assert ("未提供的个案条件" in messages[1]["content"]) == (initial != "both")
        else:
            expected = 2 - len(known)
            assert len(clarifications) == row["ai_clarification_count"] == expected
            slots = []
            for index, message in enumerate(messages):
                if message["action"] != "prompt_clarification":
                    continue
                prior = "名额" in message["content"]
                weekly = any(word in message["content"] for word in ("每周", "一周"))
                assert prior != weekly and message["content"].count("？") == 1
                slot = "prior" if prior else "weekly"
                assert slot not in known
                known.add(slot)
                slots.append(slot)
                assert messages[index + 1]["role"] == "user"
                assert ("名额" in messages[index + 1]["content"]) == prior
            question_orders.add(tuple(slots))
            clarification_totals[len(clarifications)] += 1
            assert known == {"prior", "weekly"}
        expected_category = "登记乙" if row["scenario_id"].startswith("return_") else "登记甲"
        expected_arrangement = "现场安排" if row["scenario_id"].endswith("_3") else "弹性安排"
        first_evidence_index = next(index for index, message in enumerate(messages) if message["action"] == "evidence")
        completed_facts_reply = messages[first_evidence_index - 2]
        assert completed_facts_reply["role"] == "assistant" and completed_facts_reply["action"] == "answer"
        assert expected_category in completed_facts_reply["content"] and expected_arrangement in completed_facts_reply["content"]
        assert expected_category in messages[-1]["content"] and expected_arrangement in messages[-1]["content"]
        assert "办理应按" in messages[-1]["content"] and "11月18日17:00" in messages[-1]["content"]
    assert clarification_totals == {0: 14, 1: 18, 2: 31}
    assert len(paths) == 8 and len(openings) > 40
    assert ("prior", "weekly") in question_orders and ("weekly", "prior") in question_orders


def test_record_level_sources_repeat_with_overlapping_counts_and_varied_paths():
    data = showcase.get_student_dataset()
    counts, repeats, paths = {}, {}, {}
    for group in ("A", "S"):
        rows = [row for row in data["records"] if row["group_id"] == group]
        counts[group] = Counter(row["evidence_reveals"] for row in rows)
        repeats[group] = sum(row["evidence_reveals"] > row["distinct_evidence_cards"] for row in rows)
        paths[group] = {tuple(row["fixture_dialogue_plan"]["card_ids"]) for row in rows}
        assert len(paths[group]) > 40
    assert counts == {"A": {2: 3, 3: 6, 4: 13, 5: 20, 6: 16, 7: 4, 8: 1},
                      "S": {1: 8, 2: 14, 3: 12, 4: 22, 5: 7}}
    assert set(counts["A"]) & set(counts["S"]) == {2, 3, 4, 5}
    assert repeats == data["manifest"]["repeated_source_record_counts"] == {"A": 47, "S": 25}
    assert {str(key): value for key, value in counts["A"].items()} == data["manifest"]["evidence_reveals_per_record_histograms"]["A"]
    assert {str(key): value for key, value in counts["S"].items()} == data["manifest"]["evidence_reveals_per_record_histograms"]["S"]


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


@pytest.mark.parametrize("old_batch", ["student-126-20261022-v1", "student-126-20261022-v2"])
def test_older_receipt_is_not_reused_to_overwrite_an_existing_database(tmp_path, old_batch):
    settings = student_settings(tmp_path)
    init_db(settings.database_path)
    with connect(settings.database_path) as conn:
        seed_student_materials(conn)
        seed(conn, settings)
        conn.execute("UPDATE student_showcase_imports SET batch_id=?", (old_batch,))
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
    source_by_id = {row["id"]: row for row in showcase.get_student_dataset()["records"]}
    source_by_alias = {}
    for archived in rows:
        source = source_by_id[archived["id"]]
        source_by_alias[archived["record_id"]] = source
        assert json.loads(archived["messages_json"]) == source["messages"]
        assert archived["evidence_reveals"] == source["evidence_reveals"]
        assert archived["distinct_evidence_cards"] == source["distinct_evidence_cards"]
        assert archived["parent_source_batch_id"] == source["parent_source_batch_id"]
        assert archived["parent_record_id"] == source["parent_record_id"]
        assert [json.loads(event["detail_json"]) for event in json.loads(archived["model_events_json"])] == (
            [event["detail"] for event in source["model_events"]])
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
        for exported_row in exported:
            source = source_by_alias[exported_row["record_id"]]
            if "dialogue-export" in path:
                assert [{key: message[key] for key in ("role", "content", "action", "created_at")}
                        for message in exported_row["messages"]] == source["messages"]
            else:
                assert exported_row["evidence_reveals"] == source["evidence_reveals"]
                assert exported_row["distinct_evidence_cards"] == source["distinct_evidence_cards"]
    import csv
    import io
    from app.clean_exports import DIALOGUE_CSV_COLUMNS, RECORD_CSV_COLUMNS
    for path, columns in (("/api/admin/export?kind=raw&format=csv", RECORD_CSV_COLUMNS),
                          ("/api/admin/export?kind=analysis&format=csv", RECORD_CSV_COLUMNS),
                          ("/api/admin/dialogue-export?format=csv", DIALOGUE_CSV_COLUMNS)):
        flat_response = client.get(path, headers=headers)
        assert flat_response.status_code == 200
        reader = csv.DictReader(io.StringIO(flat_response.text.lstrip("\ufeff")))
        assert reader.fieldnames == [label for _key, label in columns]
        assert not ({"记录类型", "合成数据(1是/0否)", "数据批次", "数据来源", "模拟时间(1是/0否)",
                     "纳入正式参与者分析", "可纳入正式参与者分析(1是/0否)",
                     "可用于模拟展示统计(1是/0否)", "未纳入原因"} & set(reader.fieldnames))
        flat_rows = list(reader)
        assert len({row["记录编号"] for row in flat_rows}) == 126
        assert all("合成" not in value and "模拟" not in value for row in flat_rows for value in row.values())
        assert all(row["教育阶段"] in {"在读专科", "在读本科"} for row in flat_rows)
        if "dialogue-export" in path:
            assert {row["消息类型"] for row in flat_rows} == {"参与者提问", "AI主动澄清(提示词)", "一般答复", "资料查询"}
        if "dialogue-export" not in path:
            assert len(flat_rows) == 126
            assert all(row["决策质量分(0-100)"] != "" and 1 <= int(row["资料线索次数"]) <= 8 for row in flat_rows)
            for flat in flat_rows:
                source = source_by_alias[flat["记录编号"]]
                assert int(flat["资料线索次数"]) == source["evidence_reveals"]
                assert int(flat["不同资料线索数"]) == source["distinct_evidence_cards"]
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
        expected_clues = Counter((row["group_id"], event["detail"]["card_id"])
                                 for row in showcase.get_student_dataset()["records"] if row["age_band"] == age
                                 for event in row["model_events"] if event["event_type"] == "evidence_reveal")
        assert clues == dict(expected_clues)
        for card_id in showcase.EVIDENCE_CARD_IDS:
            assert clues[("A", card_id)] > clues[("S", card_id)] > 0
    assert client.get("/api/admin/summary?age_band=18-24", headers=headers).status_code == 422
    # A restart checks the same batch and does not duplicate generated records.
    TestClient(create_app(settings))
    with connect(settings.database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 126
