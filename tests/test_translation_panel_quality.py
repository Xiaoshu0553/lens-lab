"""Distinct-model completeness, identity and weighted trimmed aggregation."""
from copy import deepcopy
import json
import math

import pytest

from app.translation_materials import TASK
from app.translation_panel_quality import (
    MODEL_MIN, MODEL_MAX, PANEL_VERSION, aggregate_model_ratings, scoring_prompt, validate_panel,
)
from app.translation_quality import RUBRIC


def panel(count=5):
    return [{"id": f"m{i}", "label": f"模型{i}", "model": f"family-{i}:4b",
             "base_url": "http://127.0.0.1:11434/v1", "weight": 1}
            for i in range(count)]


def ratings(values=(0, 60, 70, 80, 100)):
    return [{"model_id": f"m{i}", "scores": {d["id"]: value for d in RUBRIC},
             "evidence": ["译文准确保留了原文的动作与否定关系。"], "critical_errors": []}
            for i, value in enumerate(values)]


def test_every_model_receives_the_same_complete_five_dimension_prompt():
    first = scoring_prompt(TASK)
    assert first == scoring_prompt(deepcopy(TASK))
    assert TASK["source_text"] in first and TASK["reference_translation"] in first
    assert all(card["body"] in first for card in TASK["context_cards"])
    assert all(d["id"] in first and d["description"] in first for d in RUBRIC)
    assert "不能只评价某一专长" in first and "完全相同" in first
    assert "其他模型的分数" in first and "不能按字词重合度评分" in first
    assert all(key not in first for key in ("ability_score", "condition", "pretest_answers"))
    assert PANEL_VERSION in first


def test_validation_returns_only_nonsecret_whitelisted_fields_and_equal_default_weights():
    models = panel()
    for item in models:
        item.pop("weight")
        item.update(api_key="secret-value", password="secret-password", arbitrary={"token": "secret"})
    clean = validate_panel(models)
    assert all(item["weight"] == 1.0 for item in clean)
    assert "secret" not in json.dumps(clean)
    assert len(models[0]) > len(clean[0])
    assert MODEL_MIN == 5 and MODEL_MAX == 9


@pytest.mark.parametrize("count", [0, 1, 3, 4, 10])
def test_panel_must_leave_at_least_three_models_after_trimming(count):
    with pytest.raises(ValueError):
        validate_panel(panel(count))


@pytest.mark.parametrize("bad", [True, False, 0, -1, float("nan"), float("inf"), "1", None, 1000001])
def test_invalid_model_weights_cannot_change_aggregation(bad):
    models = panel()
    models[0]["weight"] = bad
    with pytest.raises(ValueError):
        validate_panel(models)


@pytest.mark.parametrize("bad_url", ["", "ftp://host", "https://user:secret@host/v1", "https://host/v1?key=secret", "https://host/#secret", "https://host:bad/v1"])
def test_endpoint_validation_rejects_embedded_credentials_and_invalid_urls(bad_url):
    models = panel()
    models[0]["base_url"] = bad_url
    with pytest.raises(ValueError):
        validate_panel(models)


def test_endpoint_and_label_normalization_preserve_distinct_checkpoints():
    models = panel()
    models[0].update(base_url=" HTTPS://EXAMPLE.COM:443/v1/ ", label=" 模型一 ")
    clean = validate_panel(models)
    assert clean[0]["base_url"] == "https://example.com/v1"
    assert clean[0]["label"] == "模型一"
    models[0]["model"] = "qwen3:4b"
    models[1]["model"] = "qwen3:8b"
    assert len(validate_panel(models)) == 5


@pytest.mark.parametrize("duplicate", ["family-0:4b", "FAMILY-0:4B", "models/family-0:4b"])
def test_same_model_cannot_count_twice_through_different_endpoints(duplicate):
    models = panel()
    models[1].update(model=duplicate, base_url="https://another-gateway.example/v1")
    with pytest.raises(ValueError):
        validate_panel(models)


@pytest.mark.parametrize("name1,name2", [("qwen3", "qwen3:latest"), ("qwen3:4b-q4_K_M", "qwen3:4b-q8_0"), ("qwen3:4b-fp16", "qwen3:4b")])
def test_aliases_and_quantizations_do_not_create_different_ai_models(name1, name2):
    models = panel()
    models[0]["model"], models[1]["model"] = name1, name2
    with pytest.raises(ValueError):
        validate_panel(models)


def test_duplicate_ids_are_rejected_even_with_distinct_models():
    models = panel()
    models[1]["id"] = models[0]["id"]
    with pytest.raises(ValueError):
        validate_panel(models)


def test_trimmed_model_weights_are_renormalized_after_removing_extremes():
    models = panel()
    for model, weight in zip(models, (999, 1, 2, 3, 999)):
        model["weight"] = weight
    result = aggregate_model_ratings(ratings(), models)
    assert result["score"] == pytest.approx((60 + 2 * 70 + 3 * 80) / 6, abs=.01)
    assert result["trimmed_model_ids"] == ["m0", "m4"]
    assert result["included_model_ids"] == ["m1", "m2", "m3"]
    assert sum(row["normalized_weight"] for row in result["ratings"]) == pytest.approx(1)
    assert result["ratings"][0]["normalized_weight"] == result["ratings"][-1]["normalized_weight"] == 0
    assert all(result["dimensions"][item["id"]] == result["score"] for item in RUBRIC)
    assert result["ratings"][0]["weight"] == 999
    assert len(result["ratings"]) == 5


def test_five_dimension_weights_decide_extremes_once_and_all_dimensions_use_same_models():
    values = ratings((80, 80, 80, 80, 80))
    # m0 loses 100*35%=35 points; m4 loses 100*10%=10 points.
    values[0]["scores"]["accuracy"] = 0
    values[4]["scores"]["completeness"] = 0
    values[3]["scores"]["accuracy"] = 100
    result = aggregate_model_ratings(values, panel())
    assert result["model_totals"]["m0"] == 52
    assert result["model_totals"]["m3"] == 87
    assert result["trimmed_model_ids"] == ["m0", "m3"]
    assert result["dimensions"]["accuracy"] == 80
    assert result["dimensions"]["completeness"] == pytest.approx(53.33)
    assert result["score"] == pytest.approx(77.33)


def test_total_is_not_rounded_before_trimming():
    values = ratings((80, 80, 80, 80, 80))
    values[0]["scores"]["accuracy"] += .001
    values[4]["scores"]["accuracy"] -= .001
    result = aggregate_model_ratings(values, panel())
    assert result["trimmed_model_ids"] == ["m4", "m0"]


def test_ties_are_deterministic_independent_of_input_order_or_model_weights():
    models = panel()
    models[0]["weight"] = 100
    first = aggregate_model_ratings(ratings((80,) * 5), models)
    second = aggregate_model_ratings(list(reversed(ratings((80,) * 5))), list(reversed(models)))
    assert first["trimmed_model_ids"] == second["trimmed_model_ids"] == ["m0", "m4"]
    assert first["score"] == second["score"] == 80


@pytest.mark.parametrize("failure", ["missing", "extra", "duplicate", "unknown", "invalid_dimension", "bool_score"])
def test_complete_panel_is_required_and_invalid_ratings_do_not_become_partial_averages(failure):
    values = ratings()
    if failure == "missing": values.pop()
    elif failure == "extra": values.append(deepcopy(values[0]))
    elif failure == "duplicate": values[1]["model_id"] = values[0]["model_id"]
    elif failure == "unknown": values[1]["model_id"] = "unknown"
    elif failure == "invalid_dimension": values[1]["scores"].pop("accuracy")
    else: values[1]["scores"]["accuracy"] = True
    with pytest.raises(ValueError):
        aggregate_model_ratings(values, panel())


def test_reviews_consider_all_models_including_trimmed_critical_errors():
    values = ratings((70, 75, 80, 85, 90))
    values[0]["critical_errors"] = ["关键人物指代发生反转。"]
    result = aggregate_model_ratings(values, panel())
    assert result["needs_review"] and "严重错误" in result["reason"]
    assert result["ratings"][0]["critical_errors"] == values[0]["critical_errors"]
    assert result["ratings"][0]["trimmed"] is True
    assert result["method"] == "distinct-model-trimmed-weighted-mean"
    assert "不保证偏差抵消" in result["limitation"]


def test_default_equal_weights_match_unweighted_trimmed_mean_and_preserve_all_scores():
    result = aggregate_model_ratings(ratings((65, 75, 80, 85, 95)), panel())
    assert result["score"] == 80
    assert result["model_totals"] == {"m0":65,"m1":75,"m2":80,"m3":85,"m4":95}
    assert not result["needs_review"] or result["range"] > 25
    assert all(set(row["scores"]) == {item["id"] for item in RUBRIC} for row in result["ratings"])
    assert len(result["model_weights"]) == 5


def test_larger_panel_retains_all_but_two_models_and_keeps_provenance():
    models = panel(9)
    result = aggregate_model_ratings(ratings(tuple(range(10,100,10))), models)
    assert result["score"] == 50
    assert len(result["included_model_ids"]) == 7
    assert len(result["ratings"]) == 9
    assert all(row["model"] == models[index]["model"] for index,row in enumerate(result["ratings"]))
