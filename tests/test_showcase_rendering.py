"""Conversation fixtures preserve facts/counts while varying ordinary language."""
import copy
import json
from datetime import datetime, timedelta

import pytest

from app.age_forms import AGE_BANDS, form_for
from app.age_tasks import task_for
from app.main import _flags
from app.showcase_rendering import (RENDER_VERSION, TIME_SHIFT_DAYS, TIME_SHIFT_SECONDS, adapted_row,
                                    derived_fixture_flags, fixture_payloads)


def source_row(**changes):
    row = {"id": "SYN-0001", "age_band": "18-24", "scenario_id": "first_3",
           "sim_response_profile": "正常", "created_at": "2030-01-01T00:18:00+00:00",
           "decision_started_at": "2030-01-01T00:23:06+00:00", "ended_at": "2030-01-01T00:23:38+00:00",
           "reviewed_at": "2030-01-01T00:23:38+00:00", "timestamp_is_simulated": 1,
           "user_turns": 7, "clarification_count": 2, "evidence_reveals": 3, "distinct_evidence_cards": 3,
           "score": 100, "decision_choice_id": "new_onsite", "execution_choice_id": "service_current_complete",
           "synthetic_pretest_time_seconds": 600, "sim_qc_flag": 0,
           "synthetic_source_json": '{"created_at":"2030-01-01T00:18:00+00:00"}',
           "nested_source": {"timestamps": ["2030-01-01"]}}
    row.update(changes)
    return row


def normal_answers(age="18-24"):
    return {item["id"]: index % 2 for index, item in enumerate(form_for(age)[1])}


def test_adaptation_preserves_source_and_durations_and_is_idempotent():
    original = source_row()
    before = copy.deepcopy(original)
    shifted = adapted_row(original)
    assert original == before and shifted is not original
    assert shifted["created_at"] == "2026-10-06T00:18:00+00:00"
    assert shifted["fixture_render_version"] == RENDER_VERSION
    assert shifted["fixture_time_shift_days"] == TIME_SHIFT_DAYS == -1183
    assert TIME_SHIFT_SECONDS == -1183 * 86400
    for key in ["created_at", "decision_started_at", "ended_at", "reviewed_at"]:
        assert datetime.fromisoformat(shifted[key]) - datetime.fromisoformat(original[key]) == timedelta(days=-1183)
    assert shifted["score"] == original["score"] and shifted["synthetic_source_json"] == original["synthetic_source_json"]
    shifted["nested_source"]["timestamps"].append("local edit")
    assert original["nested_source"] == before["nested_source"]
    assert adapted_row(shifted) == shifted


def test_last_original_timestamp_moves_to_october_20_without_fractional_time_loss():
    original = source_row(ended_at="2030-01-15T13:32:37.125000+00:00")
    assert adapted_row(original)["ended_at"] == "2026-10-20T13:32:37.125000+00:00"
    assert adapted_row(source_row(ended_at=None))["ended_at"] is None
    with pytest.raises(ValueError, match="timezone"):
        adapted_row(source_row(created_at="2030-01-01T00:18:00"))
    invalid = adapted_row(source_row())
    invalid["fixture_time_shift_days"] = -1
    with pytest.raises(ValueError, match="inconsistent"):
        adapted_row(invalid)


@pytest.mark.parametrize("scenario", ["first_3", "first_1", "return_3", "return_1"])
@pytest.mark.parametrize("clarifications", [0, 1, 2])
@pytest.mark.parametrize("profile", ["正常", "认真", "随意"])
def test_natural_dialogue_preserves_counts_actions_card_bodies_and_case_fact_boundaries(scenario, clarifications, profile):
    row = source_row(scenario_id=scenario, clarification_count=clarifications, sim_response_profile=profile)
    task = task_for(row["age_band"], scenario)
    original = copy.deepcopy(row)
    messages, events = fixture_payloads(row, task, "module")
    users = [message[2] for message in messages if message[1] == "user"]
    assistants = [message for message in messages if message[1] == "assistant"]
    assert row == original and len(users) == len(assistants) == row["user_turns"]
    assert sum(message[3] == "module_clarification" for message in assistants) == clarifications
    evidence_messages = [message for message in assistants if message[3] == "evidence"]
    reveals = [json.loads(event[2]) for event in events if event[1] == "evidence_reveal"]
    assert len(evidence_messages) == len(reveals) == row["evidence_reveals"]
    assert len({event["card_id"] for event in reveals}) == row["distinct_evidence_cards"]
    for message, reveal in zip(evidence_messages, reveals):
        card = next(card for card in task["evidence_cards"] if card["id"] == reveal["card_id"])
        assert card["body"] in message[2]
    for event in events:
        detail = json.loads(event[2])
        assert detail["is_synthetic"] == 1 and detail["render_version"] == RENDER_VERSION
        assert detail["time_shift"]["days"] == -1183
        assert event[1] != "reply_latency"
    assert all(adapted_row(row)["created_at"] <= message[4] <= adapted_row(row)["decision_started_at"] for message in messages)
    assert all("请说明" not in user and "你还掌握" not in user and "还有什么要核对" not in user
               and "我想逐项核实案例" not in user for user in users)
    if clarifications:
        weekly = 1 if scenario.endswith("_1") else 3
        assert f"{weekly}次" in users[1]
        if clarifications == 2:
            assert "名额" not in users[1] and "以前" not in users[1]
        else:
            assert not any(fact in users[1] for fact in ("他以前拿过", "名额他以前没拿过", "这个项目没拿过"))
    if clarifications == 2:
        assert "这个项目" in users[2] and ("没拿过" in users[2] if scenario.startswith("first") else "拿过" in users[2])
        first_clause = users[2].split("。", 1)[0] if "。" in users[2] else users[2].split("，", 1)[0]
        assert "最多" not in first_clause and "每周" not in first_clause
    assert fixture_payloads(adapted_row(row), task, "module") == (messages, events)


def test_last_turn_clarification_stays_unanswered_when_the_source_has_no_user_reply():
    row = source_row(user_turns=2, clarification_count=2, evidence_reveals=0, distinct_evidence_cards=0)
    messages, events = fixture_payloads(row, task_for("18-24"), "module")
    assert len(messages) == 4
    assert "3次" in messages[2][2] and "名额" not in messages[2][2]
    assert "名额" in messages[-1][2] and messages[-1][3] == "module_clarification"
    clarifications = [json.loads(event[2]) for event in events if event[1] == "clarification_decision"]
    assert [event["answer_present_in_later_user_message"] for event in clarifications] == [True, False]


def test_repeated_source_reveals_use_followup_questions_and_prompt_actions_are_preserved():
    row = source_row(evidence_reveals=5, distinct_evidence_cards=3)
    messages, events = fixture_payloads(row, task_for("18-24"), "prompt")
    assert sum(message[3] == "prompt_clarification" for message in messages) == 2
    users = [message[2] for message in messages if message[1] == "user"]
    assert any(word in users[-2] for word in ("再", "刚才", "所以"))
    assert any(word in users[-1] for word in ("再", "刚才", "所以"))
    assert len([event for event in events if event[1] == "evidence_reveal"]) == 5


def test_language_style_is_independent_of_age_and_varies_between_individuals():
    by_age = []
    for age in AGE_BANDS:
        row = source_row(age_band=age)
        by_age.append([message[2] for message in fixture_payloads(row, task_for(age), "module")[0] if message[1] == "user"])
    assert all(users == by_age[0] for users in by_age)
    questions = set()
    for index in range(100):
        row = source_row(id=f"SYN-{index:04d}", clarification_count=0)
        questions.add(fixture_payloads(row, task_for("18-24"), "module")[0][0][2])
    assert len(questions) >= 10


def test_natural_openings_do_not_stack_prefaces_and_evidence_introductions_vary():
    task = task_for("18-24")
    introductions = set()
    for index in range(100):
        row = source_row(id=f"SYN-{index:04d}")
        messages, _events = fixture_payloads(row, task, "module")
        for message in messages:
            if message[1] == "user":
                assert not any(prefix in message[2] for prefix in (
                    "麻烦问下，先", "麻烦问下，我", "麻烦问下，再", "我有点拿不准，先", "我有点拿不准，我"))
            elif message[3] == "evidence":
                body = next(card["body"] for card in task["evidence_cards"] if card["body"] in message[2])
                introductions.add(message[2].removesuffix(body))
    assert "" in introductions and len(introductions) >= 3


def test_topic_routes_vary_by_individual_not_score_profile_or_age():
    task = task_for("18-24")
    first_cards = set()
    for index in range(100):
        row = source_row(id=f"SYN-{index:04d}")
        _messages, events = fixture_payloads(row, task, "module")
        first_cards.add(next(json.loads(event[2])["card_id"] for event in events if event[1] == "evidence_reveal"))
    assert len(first_cards) >= 5 and "C_TIME" in first_cards and "B_MATERIALS" in first_cards
    baseline = fixture_payloads(source_row(), task, "module")[1]
    for changes in [{"score": 0}, {"sim_response_profile": "随意"}, {"age_band": "75-plus"}]:
        alternate = fixture_payloads(source_row(**changes), task, "module")[1]
        route = lambda events: [json.loads(event[2])["card_id"] for event in events if event[1] == "evidence_reveal"]
        assert route(alternate) == route(baseline)


def test_process_qc_matches_existing_platform_rules_and_does_not_use_profile_as_a_label():
    row = source_row(evidence_reveals=0, distinct_evidence_cards=0, ended_at="2030-01-01T00:18:30+00:00")
    task = task_for("18-24")
    messages = [{"role": "user", "content": "哈哈哈哈哈哈"}, {"role": "user", "content": "哈哈哈哈哈哈"}]
    expected = _flags(messages, "", row["created_at"], row["ended_at"], task["keywords"], row["decision_choice_id"], 0)
    assert derived_fixture_flags(row, task, normal_answers(), messages=messages, submission="") == expected
    for profile in ["随意", "认真", "正常"]:
        alternate = {**row, "sim_response_profile": profile, "sim_qc_flag": 1}
        assert derived_fixture_flags(alternate, task, normal_answers(), messages=messages, submission="") == expected


def test_pretest_qc_recomputes_canonical_item_order_and_source_duration_instead_of_copying_sim_qc():
    task = task_for("18-24")
    items = form_for("18-24")[1]
    answers = {item["id"]: 0 if index < 12 else 1 + index % 2 for index, item in enumerate(items)}
    reversed_answers = dict(reversed(list(answers.items())))
    row = source_row(sim_response_profile="认真", sim_qc_flag=0, synthetic_pretest_time_seconds=119.9)
    flags = derived_fixture_flags(row, task, reversed_answers)
    assert "pretest_fast_completion" in flags and "pretest_long_identical_run" in flags
    assert "pretest_fast_completion" not in derived_fixture_flags({**row, "synthetic_pretest_time_seconds": 120}, task, normal_answers())
    assert "pretest_long_identical_run" not in derived_fixture_flags(row, task, normal_answers())
    with pytest.raises(ValueError, match="complete 30-item"):
        derived_fixture_flags(row, task, {"s01": 0})


@pytest.mark.parametrize("changes", [{"clarification_count": 3}, {"distinct_evidence_cards": 0},
                                      {"user_turns": 1}, {"distinct_evidence_cards": 12}])
def test_invalid_counts_are_rejected_without_inventing_turns(changes):
    with pytest.raises(ValueError, match="counts"):
        fixture_payloads(source_row(**changes), task_for("18-24"), "module")
