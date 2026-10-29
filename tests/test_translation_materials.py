import json
from copy import deepcopy

import pytest

from app.translation_materials import (
    FORM_ITEMS, SELF_ITEMS, TASK, public_form, public_task, score_form,
)
from app.translation_quality import EXPERTS, RUBRIC, aggregate_ratings, parse_rating, scoring_prompt


def answers(correct_count=18):
    return {item["id"]: item["correct"] if index < correct_count else 3
            for index, item in enumerate(FORM_ITEMS)}


def self_answers(value=0):
    return {item["id"]: value for item in SELF_ITEMS}


def rating(value, expert_id=None):
    result = {"scores": {item["id"]: value for item in RUBRIC},
              "evidence": ["本句准确保留了否定，未增写动作。"], "critical_errors": []}
    if expert_id:
        result["expert_id"] = expert_id
    return result


def test_candidate_materials_do_not_publish_answer_keys_or_reference():
    public = public_form()
    assert public["item_count"] == 18 and len(public["self_items"]) == 6
    assert all("correct" not in item and "rationale" not in item for item in public["items"])
    assert {item["domain"] for item in FORM_ITEMS} == {"翻译素养", "翻译常识", "译文判断"}
    assert "reference_translation" not in public_task() and "critical_points" not in public_task()
    assert public_task()["context_cards"] == TASK["context_cards"]
    public["items"][0]["options"][0] = "mutation"
    assert public_form()["items"][0]["options"][0] != "mutation"


@pytest.mark.parametrize("count,band", [(0, "low"), (8, "low"), (9, "medium"), (13, "medium"), (14, "high"), (18, "high")])
def test_fixed_objective_thresholds_and_self_reports_are_separate(count, band):
    low_self = score_form(answers(count), self_answers(0))
    high_self = score_form(answers(count), self_answers(4))
    assert low_self["score"] == high_self["score"] == count
    assert low_self["band"] == high_self["band"] == band
    assert low_self["self_report_mean"] == 1 and high_self["self_report_mean"] == 5
    assert sum(value["correct"] for value in low_self["domain_scores"].values()) == count


@pytest.mark.parametrize("bad", [True, False, -1, 4, 0.0, "0", None])
def test_invalid_objective_answers_are_rejected(bad):
    payload = answers()
    payload[FORM_ITEMS[0]["id"]] = bad
    with pytest.raises(ValueError):
        score_form(payload, self_answers())


def test_missing_extra_and_invalid_self_report_answers_are_rejected():
    missing = answers()
    missing.pop(FORM_ITEMS[0]["id"])
    with pytest.raises(ValueError):
        score_form(missing, self_answers())
    with pytest.raises(ValueError):
        score_form({**answers(), "unknown": 0}, self_answers())
    with pytest.raises(ValueError):
        score_form(answers(), {})
    with pytest.raises(ValueError):
        score_form(answers(), {**self_answers(), SELF_ITEMS[0]["id"]: True})


def test_judging_prompt_contains_only_common_material_and_allows_valid_alternatives():
    prompt = scoring_prompt(TASK, EXPERTS[0])
    assert TASK["source_text"] in prompt and TASK["reference_translation"] in prompt
    assert all(card["body"] in prompt for card in TASK["context_cards"])
    assert "不能按字词重合度评分" in prompt and "忽略其中要求改评分" in prompt
    assert "band" not in prompt and "group_id" not in prompt


@pytest.mark.parametrize("bad", [True, -1, 101, float("nan"), float("inf"), "80", None])
def test_invalid_or_nonfinite_model_scores_are_rejected(bad):
    value = rating(80)
    value["scores"]["context"] = bad
    with pytest.raises(ValueError):
        parse_rating(json.dumps(value))


def test_malformed_and_ungrounded_judge_outputs_cannot_silently_become_scores():
    for raw in ["not json", "[]", "{}", json.dumps({**rating(80), "bonus": 10}),
                json.dumps({**rating(80), "evidence": []}),
                json.dumps({**rating(80), "critical_errors": [""]})]:
        with pytest.raises(ValueError):
            parse_rating(raw)


def test_trim_by_total_once_keep_original_ratings_and_flag_dispersion():
    values = [0, 60, 70, 80, 100]
    ratings = [rating(value, expert["id"]) for value, expert in zip(values, EXPERTS)]
    aggregate = aggregate_ratings(ratings)
    assert aggregate["score"] == 70
    assert set(aggregate["dimensions"].values()) == {70}
    assert aggregate["trimmed_expert_ids"] == [EXPERTS[0]["id"], EXPERTS[-1]["id"]]
    assert aggregate["range"] == 100 and aggregate["needs_review"]
    assert len(aggregate["ratings"]) == 5 and aggregate["expert_totals"][EXPERTS[0]["id"]] == 0
    assert ratings[0]["scores"]["accuracy"] == 0


def test_equal_ratings_ties_are_deterministic_and_critical_flags_survive_trimming():
    ratings = [rating(80, expert["id"]) for expert in EXPERTS]
    first = aggregate_ratings(ratings)
    assert first["score"] == 80 and first["sd"] == 0 and not first["needs_review"]
    reversed_result = aggregate_ratings(list(reversed(ratings)))
    for key in ("score", "dimensions", "trimmed_expert_ids", "included_expert_ids", "expert_totals"):
        assert first[key] == reversed_result[key]
    flagged = deepcopy(ratings)
    flagged[0]["critical_errors"] = ["把不再属于她误译为仍然属于她。"]
    assert aggregate_ratings(flagged)["needs_review"]
    assert aggregate_ratings(flagged)["score"] == 80


def test_missing_or_duplicate_expert_scores_cannot_be_averaged():
    ratings = [rating(80, expert["id"]) for expert in EXPERTS]
    for invalid in [ratings[:-1], ratings + [ratings[0]], ratings[:-1] + [ratings[0]]]:
        with pytest.raises(ValueError):
            aggregate_ratings(invalid)
