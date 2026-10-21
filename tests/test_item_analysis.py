"""Synthetic unit tests of calculations, not empirical questionnaire validation."""

import math

import pytest

from app.item_analysis import analyze


def form(k=2):
    return [{"id": f"q{i}", "domain": "test", "question": f"Question {i}",
             "options": ["right", "wrong", "不确定"], "correct": 0,
             "draft_difficulty": "medium"} for i in range(k)]


def responses(rows):
    return [{f"q{i}": 0 if hit else 1 for i, hit in enumerate(row)} for row in rows]


def test_item_rest_correlation_excludes_the_item_itself():
    report = analyze(form(), responses([[1, 1], [1, 0], [0, 1], [0, 0]]), "synthetic")
    assert report["n"] == 4
    assert report["mean_percent"] == 50
    assert report["kr20"] == 0
    assert all(item["corrected_item_total_r"] == 0 for item in report["items"])
    assert all(item["high_low_discrimination"] == 0 for item in report["items"])
    assert all(item["discrimination_unstable"] for item in report["items"])
    assert report["validation_status"] == "unvalidated"


def test_kr20_is_binary_alpha_and_does_not_validate_even_perfect_consistency():
    report = analyze(form(3), responses([[1, 1, 1]] * 60 + [[0, 0, 0]] * 60), "synthetic")
    assert report["kr20"] == 1
    assert report["reliability"]["value"] == 1
    assert report["sample_status"] == "descriptive_only"
    assert report["validation_status"] == "unvalidated"
    assert all(item["corrected_item_total_r"] == 1 for item in report["items"])
    assert all(item["high_low_discrimination"] == 1 for item in report["items"])
    assert all(not item["discrimination_unstable"] for item in report["items"])


def test_negative_consistency_is_preserved_as_a_diagnostic_not_clamped():
    report = analyze(form(3), responses([[1, 0, 0]] * 2 + [[0, 1, 1]] * 2), "synthetic")
    assert report["kr20"] == -3
    assert report["reliability"]["negative_estimate"] is True
    assert report["items"][0]["corrected_item_total_r"] == -1


def test_constant_and_no_data_are_explicitly_uncomputable():
    empty = analyze(form(), [], "synthetic")
    assert empty["n"] == 0 and empty["sample_status"] == "no_data"
    assert empty["kr20"] is None
    assert all(item["p_correct"] is None and item["p_correct_ci95"] is None for item in empty["items"])
    report = analyze(form(), responses([[1, 1]] * 10), "synthetic")
    assert report["kr20"] is None
    assert report["reliability"]["not_computable_reason"] == "zero_total_variance"
    assert report["reliability"]["constant_item_count"] == 2
    assert all(item["corrected_item_total_r"] is None for item in report["items"])
    assert all(item["high_low_discrimination"] is None for item in report["items"])


def test_one_item_does_not_produce_internal_consistency_or_item_rest_discrimination():
    report = analyze(form(1), responses([[1], [0], [1], [0]]), "synthetic")
    assert report["reliability"]["not_computable_reason"] == "fewer_than_two_items"
    assert report["items"][0]["corrected_item_total_r"] is None
    assert report["items"][0]["high_low_discrimination"] is None


def test_invalid_and_different_form_responses_are_excluded_with_counts():
    report = analyze(form(), [{"q0": 0, "q1": 0}, {"q0": 0}, {"q0": 0, "q1": 0, "q2": 0},
                              {"q0": True, "q1": 0}, {"q0": "0", "q1": 0},
                              {"q0": 3, "q1": 0}, None], "synthetic")
    assert report["n"] == 1 and report["input_count"] == 7
    assert report["excluded_count"] == 6
    assert report["excluded_reasons"] == {"incomplete_or_different_form": 3, "invalid_option": 3}


def test_uncertainty_and_distractors_are_not_conflated():
    rows = [{"q0": 0, "q1": 0}] * 60 + [{"q0": 1, "q1": 1}] * 4 + [{"q0": 2, "q1": 1}] * 36
    item = analyze(form(), rows, "synthetic")["items"][0]
    assert item["p_correct"] == .6
    assert item["uncertain_count"] == 36 and item["uncertain_rate"] == .36
    assert item["options"][1]["selection_rate"] == .04
    assert item["options"][1]["nonfunctioning_candidate"] is True
    assert item["options"][2]["role"] == "uncertain"
    assert item["options"][2]["nonfunctioning_candidate"] is None
    assert item["options"][0]["nonfunctioning_candidate"] is None
    assert "low_selection_distractor_review" in item["review_flags"]


def test_small_samples_do_not_label_a_distractor_nonfunctioning():
    item = analyze(form(), responses([[1, 1]] * 10), "synthetic")["items"][0]
    assert all(option["nonfunctioning_candidate"] is None for option in item["options"])
    assert "sample_limited" in item["review_flags"]


def test_tail_boundary_ties_are_included_and_order_does_not_change_results():
    # For q0, item-rest scores are 0 (6 people), 1 (4), 2 (6). The requested
    # 27% tails contain 5 each, but including all ties yields 6 each.
    rows = [[0, 0, 0]] * 6 + [[1, 1, 0]] * 4 + [[1, 1, 1]] * 6
    report = analyze(form(3), responses(rows), "synthetic")
    reversed_report = analyze(form(3), responses(list(reversed(rows))), "synthetic")
    item = report["items"][0]
    assert item["low_n"] == 6 and item["high_n"] == 6
    assert item["high_low_discrimination"] == 1
    assert report == reversed_report


def test_overlapping_ties_are_not_arbitrarily_split():
    rows = [[1, 0, 0]] + [[0, 1, 0]] * 8 + [[1, 1, 1]]
    item = analyze(form(3), responses(rows), "synthetic")["items"][0]
    assert item["high_low_discrimination"] is None
    assert item["discrimination_not_computable_reason"] == "overlapping_score_ties"
    assert item["low_n"] == item["high_n"] == 0


def test_wilson_interval_has_finite_boundaries_even_at_extremes():
    zero, one = analyze(form(), responses([[0, 1]] * 10), "synthetic")["items"]
    assert zero["p_correct_ci95"] == [0, .278]
    assert one["p_correct_ci95"] == [.722, 1]
    assert all(math.isfinite(value) for item in (zero, one) for value in item["p_correct_ci95"])


def test_no_uncertainty_option_is_not_assumed_to_be_the_last_option():
    items = form()
    for item in items:
        item["options"] = ["right", "wrong", "another wrong answer"]
    report = analyze(items, [{"q0": 2, "q1": 2}] * 100, "synthetic")
    item = report["items"][0]
    assert item["uncertain_count"] == 0
    assert item["options"][2]["role"] == "distractor"


@pytest.mark.parametrize("items", [[], [{"id": "q", "correct": 1, "options": ["one"]}],
                                   [{"id": "q", "correct": True, "options": ["a", "b"]}],
                                   [{"id": "q", "correct": 2, "options": ["a", "b"]}],
                                   [{"id": "q", "correct": 0, "options": ["a", "b"]}] * 2])
def test_invalid_bank_is_not_silently_analyzed(items):
    with pytest.raises(ValueError):
        analyze(items, [], "synthetic")


def test_personal_unsure_label_is_not_confused_with_insufficient_evidence_answer():
    items = [{'id':'e16','domain':'证据判断',
              'options':['肯定一样多','肯定更少','资料还不能判断人数','我暂时不会选'],
              'correct':2}]
    report=analyze(items,[{'e16':2},{'e16':3}], 'readability-candidate')
    item=report['items'][0]
    assert item['p_correct']==0.5 and item['uncertain_count']==1
    assert item['options'][2]['role']=='correct'
    assert item['options'][3]['role']=='uncertain'
