"""Research-design invariants for candidate tasks, not evidence of validity."""

import pytest

from app.age_forms import AGE_BANDS
from app.age_tasks import DEFAULT_SCENARIO, SCENARIOS, task_for


_EXPECTED_BEST = {
    "first_3": "new_onsite",
    "first_1": "new_flexible",
    "return_3": "return_onsite",
    "return_1": "return_flexible",
}


@pytest.mark.parametrize("age_band", list(AGE_BANDS))
def test_private_case_changes_the_optimal_real_action(age_band):
    """Each private fact must matter, holding the other fact constant."""
    tasks = {scenario: task_for(age_band, variant=scenario) for scenario in _EXPECTED_BEST}
    for scenario, expected in _EXPECTED_BEST.items():
        options = tasks[scenario]["decision_options"]
        best = [option["id"] for option in options if option["score"] == 80]
        assert best == [expected]
        assert all(0 <= option["score"] <= 80 for option in options)
        assert all(sum(option["score_components"].values()) == option["score"] for option in options)
    # Both facts independently change the decision: this is not a cosmetic
    # scenario label whose answer remains the same for every participant.
    for prior in ("first", "return"):
        assert _EXPECTED_BEST[f"{prior}_1"] != _EXPECTED_BEST[f"{prior}_3"]
    for weekly in (1, 3):
        assert _EXPECTED_BEST[f"first_{weekly}"] != _EXPECTED_BEST[f"return_{weekly}"]


@pytest.mark.parametrize("age_band", list(AGE_BANDS))
def test_case_variants_do_not_change_sources_or_show_an_answer_cue(age_band):
    tasks = [task_for(age_band, variant=scenario) for scenario in _EXPECTED_BEST]
    reference = tasks[0]
    stable = ("id", "title", "instructions", "shared_context", "reference_text", "evidence_cards", "slots", "execution_options")
    for task in tasks[1:]:
        assert all(task[key] == reference[key] for key in stable)
        assert [(option["id"], option["text"]) for option in task["decision_options"]] == [
            (option["id"], option["text"]) for option in reference["decision_options"]]
    assert len({task["participant_facts"] for task in tasks}) == 4
    # Selecting the longest response cannot be a universal solution.
    longest = max(reference["decision_options"], key=lambda item: len(item["text"]))["id"]
    assert sum(_EXPECTED_BEST[task["scenario_id"]] == longest for task in tasks) <= 1
    # The key frequency rule must come from the AI evidence channel, rather
    # than being handed to everyone by the final answer menu.
    assert all("每周" not in option["text"] for option in reference["decision_options"][:4])
    assert all("新参加" not in option["text"] and "再次参加" not in option["text"]
               for option in reference["decision_options"])
    assert all("11月" not in option["text"] and "报名表" not in option["text"]
               for option in reference["decision_options"])


def test_all_age_contexts_have_equivalent_decision_components_without_special_knowledge():
    task_ids, topic_names = set(), set()
    for age_band in AGE_BANDS:
        task = task_for(age_band)
        task_ids.add(task["id"])
        topic_names.add(task["title"])
        assert task["scenario_id"] == DEFAULT_SCENARIO
        assert task["id"] == f"pilot-age-{age_band}-v4"
        assert "无需实际报名" in task["instructions"]
        assert "目标" in task["instructions"] and "现场合作交流" in task["instructions"]
        assert [item["maximum"] for item in task["scoring_dimensions"]] == [40, 40, 20]
        assert len(task["scenario_variants"]) == len(SCENARIOS) == 4
        assert {variant["id"] for variant in task["scenario_variants"]} == set(_EXPECTED_BEST)
        cards = {card["id"]: card for card in task["evidence_cards"]}
        assert len(cards) == len(task["evidence_cards"])
        for slot in task["slots"]:
            assert slot["evidence_card_id"] in cards
        assert "每周必须到场2次" in cards["B_SCHEDULE"]["body"]
        assert "每周必须到场1次" in cards["B_SCHEDULE"]["body"]
        assert "登记甲" in cards["B_REPEAT"]["body"] and "登记乙" in cards["B_REPEAT"]["body"]
        assert "11月18日17:00" in cards["C_TIME"]["body"]
        assert "邮件和逾期材料不受理" in cards["C_TIME"]["body"]
        assert "无需自备手机" in cards["D_FAQ"]["body"]
        # The AI starts with public identity facts, not either private answer.
        assert "每周最多可到场" not in task["shared_context"]
        assert "获得过本项目" not in task["shared_context"]
        assert "候选" in task["rubric"] and "尚待预测试" in task["rubric"]
    assert len(task_ids) == len(topic_names) == 9


@pytest.mark.parametrize("age_band", list(AGE_BANDS))
def test_execution_is_a_separate_machine_scored_decision(age_band):
    task = task_for(age_band)
    options = task["execution_options"]
    assert len(options) == 5 and len({option["id"] for option in options}) == 5
    assert [option["id"] for option in options if option["score"] == 20] == ["service_current_complete"]
    assert all(sum(option["score_components"].values()) == option["score"] for option in options)
    assert all(0 <= option["score"] <= 20 for option in options)
    assert all(option["feasible"] == option["success"] == (option["score"] == 20) for option in options)
    # The most common channel is a distractor. The answer menu no longer
    # repeats the correct complete procedure in four competing case plans.
    assert sum("邮件" in option["text"] for option in options) == 3
    assert sum("活动服务点" in option["text"] for option in options) == 2
    assert "分别选择一项参加方案和一项办理方案" in task["instructions"]
    for scenario in _EXPECTED_BEST:
        variant = task_for(age_band, scenario)
        assert variant["execution_options"] == options
        best_main = next(option for option in variant["decision_options"] if option["score"] == 80)
        assert best_main["score"] + max(option["score"] for option in options) == 100


def test_success_and_goal_are_distinct_from_partial_quality_points():
    high_time = {item["id"]: item for item in task_for("35-44", "first_3")["decision_options"]}
    assert high_time["new_onsite"]["success"] and high_time["new_onsite"]["goal_met"]
    assert high_time["new_flexible"]["success"] and not high_time["new_flexible"]["goal_met"]
    assert not high_time["return_onsite"]["success"]
    low_time = {item["id"]: item for item in task_for("35-44", "return_1")["decision_options"]}
    assert low_time["return_onsite"]["score"] == 40 and not low_time["return_onsite"]["feasible"]
    assert low_time["return_flexible"]["score"] == 80 and low_time["return_flexible"]["success"]
    assert low_time["return_flexible"]["goal_met"]


def test_candidate_data_are_fresh_and_invalid_scenarios_fail():
    changed = task_for("35-44", "return_1")
    changed["decision_options"][0]["score"] = -1
    changed["scenario_variants"][0]["decision_options"].clear()
    changed["scoring_dimensions"][0]["maximum"] = -1
    fresh = task_for("35-44", "return_1")
    assert fresh["decision_options"][0]["score"] >= 0
    assert len(fresh["scenario_variants"][0]["decision_options"]) == 5
    assert fresh["scoring_dimensions"][0]["maximum"] == 40
    with pytest.raises(ValueError, match="年龄组"):
        task_for("unknown")
    with pytest.raises(ValueError, match="案例版本"):
        task_for("35-44", "unknown")
