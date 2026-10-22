"""Process notes must be traceable to saved observations, never expected effects."""
from copy import deepcopy
import json

import pytest

from app.process_alerts import derive_process_alerts
from app.student_materials import FORM_VERSION, form_for


@pytest.fixture
def observation():
    _, items = form_for("18-20")
    record = {"literacy_instrument_version": FORM_VERSION, "age_band": "18-20",
              "literacy_answers_json": json.dumps({item["id"]: item["correct"] for item in items}),
              "created_at": "2026-10-22T01:00:00+00:00", "ended_at": "2026-10-22T01:05:00+00:00",
              "decision_choice_id": "new_flexible", "submission": "选择登记甲和弹性安排。",
              "task_snapshot_json": json.dumps({"keywords": ["登记", "安排"]}), "flags_json": "[]"}
    messages = [{"role": "user", "content": "请解释本次登记类别和参加安排的区别。"},
                {"role": "assistant", "content": "请依据当前报名办法核对。"},
                {"role": "user", "content": "报名表和参加计划要在什么时候交到哪里？"}]
    return record, messages


def by_code(record, messages):
    return {note["code"]: note for note in derive_process_alerts(record, messages)}


def test_clear_completed_record_remains_without_any_note(observation):
    assert derive_process_alerts(*observation) == []


def test_uncertain_answers_are_informational_counts_not_invalidity(observation):
    record, messages = observation
    answers = json.loads(record["literacy_answers_json"])
    uncertain = ["s01", "a07", "v30"]
    answers.update({key: 3 for key in uncertain})
    record["literacy_answers_json"] = json.dumps(answers)
    detail = by_code(record, messages)["pretest_uncertain_answers"]
    assert detail["observed_value"] == 3 and detail["item_ids"] == uncertain
    assert detail["severity"] == "info" and detail["affects_analysis"] is False
    assert detail["operator"] == ">" and detail["threshold"] == 0
    assert detail["source_fields"] == ["literacy_answers_json", "literacy_instrument_version"]
    assert detail["is_simulated"] == 0
    assert "无效" not in detail["message"]


def test_identical_position_run_has_exact_recomputable_boundaries(observation):
    record, messages = observation
    _, items = form_for("18-20")
    answers = {item["id"]: index % 3 for index, item in enumerate(items)}
    answers.update({item["id"]: 0 for item in items[:12]})
    answers[items[12]["id"]] = 1
    record["literacy_answers_json"] = json.dumps(answers)
    detail = by_code(record, messages)["pretest_long_identical_run"]
    assert detail["observed_value"] == detail["threshold"] == 12
    assert detail["run_start_item"] == items[0]["id"]
    assert detail["run_end_item"] == items[11]["id"]


@pytest.mark.parametrize("change", ["missing", "extra", "bool", "too_high", "negative", "bad_json", "not_object"])
def test_invalid_or_incomplete_answer_vectors_do_not_produce_pretest_claims(observation, change):
    record, messages = observation
    answers = json.loads(record["literacy_answers_json"])
    answers["s01"] = 3
    if change == "missing":
        del answers["s02"]
    elif change == "extra":
        answers["unknown"] = 3
    elif change == "bool":
        answers["s02"] = True
    elif change == "too_high":
        answers["s02"] = 4
    elif change == "negative":
        answers["s02"] = -1
    record["literacy_answers_json"] = ("not json" if change == "bad_json" else "[]" if change == "not_object"
                                        else json.dumps(answers))
    assert derive_process_alerts(record, messages) == []


@pytest.mark.parametrize("version,age", [("old", "18-20"), (None, "18-20"), (FORM_VERSION, "18-24")])
def test_legacy_or_unknown_forms_are_not_reinterpreted_using_student_items(observation, version, age):
    record, messages = observation
    answers = json.loads(record["literacy_answers_json"])
    answers.update({key: 3 for key in answers})
    record.update(literacy_instrument_version=version, age_band=age, literacy_answers_json=json.dumps(answers))
    assert derive_process_alerts(record, messages) == []


def test_simulated_pretest_duration_is_never_used_as_an_observed_timer(observation):
    record, messages = observation
    answers = json.loads(record["literacy_answers_json"])
    answers["s01"] = 3
    record.update(literacy_answers_json=json.dumps(answers), is_synthetic=1, record_kind="synthetic",
                  timestamp_is_simulated=1, synthetic_pretest_time_seconds=1)
    notes = derive_process_alerts(record, messages)
    assert len(notes) == 1 and notes[0]["code"] == "pretest_uncertain_answers"
    assert notes[0]["is_simulated"] == 1
    assert all("synthetic_pretest_time_seconds" not in note["source_fields"] for note in notes)


@pytest.mark.parametrize("end,expected", [("2026-10-22T01:00:44+00:00", 44),
                                          ("2026-10-22T01:00:45+00:00", None),
                                          ("2026-10-22T00:59:59+00:00", None),
                                          ("2026-10-22T01:00:01", None), ("bad", None), (None, None)])
def test_only_valid_saved_task_timestamps_can_trigger_fast_completion(observation, end, expected):
    record, messages = observation
    record["ended_at"] = end
    notes = by_code(record, messages)
    if expected is None:
        assert "very_fast_completion" not in notes
    else:
        assert notes["very_fast_completion"]["observed_value"] == expected
        assert notes["very_fast_completion"]["source_fields"] == ["created_at", "ended_at"]


def test_unfinished_session_does_not_get_no_chat_or_no_submission_notes(observation):
    record, _ = observation
    record.update(ended_at=None, submission="", decision_choice_id=None)
    assert derive_process_alerts(record, []) == []


def test_real_model_zero_reveal_events_are_not_judged_as_no_information(observation):
    record, messages = observation
    record.update(run_mode="pilot", prompt_version="answer-dialogue-v6", evidence_reveals=0,
                  distinct_evidence_cards=0, model_events_json="[]")
    assert derive_process_alerts(record, messages) == []


def test_existing_technical_markers_are_traceable_and_do_not_gate_analysis(observation):
    record, messages = observation
    record["flags_json"] = '["technical_interruption", "technical_interruption", "model_fallback_used"]'
    notes = derive_process_alerts(record, messages)
    assert {note["code"] for note in notes} == {"technical_interruption", "model_fallback_used"}
    assert len(notes) == 2
    assert all(note["source_fields"] == ["flags_json"] and note["affects_analysis"] is False for note in notes)


def test_derivation_never_mutates_stored_record_messages_or_existing_details(observation):
    record, messages = observation
    record["process_alert_details_json"] = '[{"code":"manual_note","message":"保留原始说明"}]'
    before = deepcopy((record, messages))
    derive_process_alerts(record, messages)
    assert (record, messages) == before


def test_current_natural_dataset_gets_observed_uncertainty_notes_without_forcing_anomalies():
    from app.student_showcase import get_student_dataset
    rows = get_student_dataset()["records"]
    count = 0
    for row in rows:
        row["literacy_answers_json"] = json.dumps(row["literacy_answers"])
        notes = derive_process_alerts(row, row["messages"])
        uncertainty = [note for note in notes if note["code"] == "pretest_uncertain_answers"]
        if uncertainty:
            count += 1
            expected = sum(position == 3 for position in row["literacy_answers"].values())
            assert uncertainty[0]["observed_value"] == expected and uncertainty[0]["is_simulated"] == 1
        assert "pretest_fast_completion" not in {note["code"] for note in notes}
    assert 0 < count < len(rows)
