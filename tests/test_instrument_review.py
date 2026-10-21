"""Content-safety checks for draft forms, not evidence of psychometric validity."""
from collections import Counter
import json

import pytest

from app.age_forms import AGE_BANDS, ANCHOR_ITEMS, form_for, public_form, score_form
from app.instrument_audit import LEGACY_REVIEW, review_inventory
from app.literacy import ANCHOR_IDS, DOMAINS, ITEMS, public_items, score_answers


@pytest.mark.parametrize("age_band", AGE_BANDS)
def test_each_age_form_has_complete_balanced_material_and_review(age_band):
    version, items = form_for(age_band)
    assert "v4-30" in version and version.endswith("form-draft3")
    assert len(items) == 30 and len({item["id"] for item in items}) == 30
    assert Counter(item["domain"] for item in items) == {domain: 6 for domain in DOMAINS}
    assert sum(item["anchor"] for item in items) == 18
    assert {item["draft_difficulty"] for item in items} == {"unrated"}
    for item in items:
        assert len(item["options"]) == 4 and item["options"][-1] == "我暂时不会选"
        assert len(set(item["options"])) == 4
        assert 0 <= item["correct"] < 3
        assert len(item["question"]) <= 110
        assert max(map(len, item["question"].splitlines())) <= 90  # Structured materials may be longer overall.
        assert all(item[field] for field in ("rationale", "skill", "expected_knowledge", "review_note"))
        assert "未经过实测" in item["difficulty_basis"]
        assert item["age_band"] == ("shared" if item["anchor"] else age_band)


@pytest.mark.parametrize("age_band", AGE_BANDS)
def test_answer_position_and_length_do_not_recreate_old_guessing_shortcuts(age_band):
    _, items = form_for(age_band)
    # A/B/C must be usable; the old bank paired the obvious longer sentence with
    # a repeating key. This only detects those editing artifacts, not validity.
    assert Counter(item["correct"] for item in items) == {0: 10, 1: 10, 2: 10}
    strict_longest = sum(len(item["options"][item["correct"]]) >
                         max(len(choice) for j, choice in enumerate(item["options"][:3])
                             if j != item["correct"]) for item in items)
    assert strict_longest <= 12
    assert sum(item["correct"] == max(range(3), key=lambda j: len(item["options"][j]))
               for item in items) <= 15
    assert any(item["correct"] == min(range(3), key=lambda j: len(item["options"][j]))
               for item in items)


def test_anchors_really_are_identical_but_adaptations_are_not_only_a_course_name():
    reference = {item["id"]: item for item in ANCHOR_ITEMS}
    adapted_questions = {item_id: set() for item_id in {item["id"] for item in ITEMS} - ANCHOR_IDS}
    for age_band in AGE_BANDS:
        _, items = form_for(age_band)
        assert {item["id"]: item for item in items if item["anchor"]} == reference
        for item in items:
            if not item["anchor"]:
                adapted_questions[item["id"]].add(item["question"])
    assert all(len(questions) == 9 for questions in adapted_questions.values())
    assert len({form_for(age_band)[0] for age_band in AGE_BANDS}) == 9


def test_public_endpoints_omit_keys_review_metadata_and_do_not_mutate_bank():
    allowed = {"id", "domain", "question", "options"}
    for age_band in AGE_BANDS:
        payload = public_form(age_band)
        assert payload["anchor_count"] == 18
        assert all(set(item) == allowed for item in payload["items"])
        text = json.dumps(payload, ensure_ascii=False)
        assert not any(field in text for field in ("correct", "rationale", "expected_knowledge", "draft_difficulty"))
        payload["items"][0]["options"][0] = "外部修改"
        assert "外部修改" not in public_form(age_band)["items"][0]["options"]
    assert all(set(item) == allowed for item in public_items())


def test_material_does_not_require_removed_technical_facts_or_rehearse_course_task():
    banned = ("DOI", "置信区间", "估计区间", "百分点", "绩点", "每周参加", "报名条件", "申请截止")
    for age_band in AGE_BANDS:
        _, items = form_for(age_band)
        for item in items:
            visible = item["question"] + "".join(item["options"])
            assert not any(word in visible for word in banned)
    # The oldest two contexts are shorter; shared anchors remain identical so
    # their raw totals still must not be declared cross-age calibrated scores.
    older = [item for item in form_for("75-plus")[1] if not item["anchor"]]
    assert max(max(map(len, item["question"].splitlines())) for item in older) <= 65


@pytest.mark.parametrize("age_band", AGE_BANDS)
def test_score_tracks_anchors_and_context_without_ambiguous_or_coerced_answers(age_band):
    _, items = form_for(age_band)
    correct = {item["id"]: item["correct"] for item in items}
    uncertain = {item["id"]: 3 for item in items}
    only_anchors = {item["id"]: item["correct"] if item["anchor"] else 3 for item in items}
    only_context = {item["id"]: 3 if item["anchor"] else item["correct"] for item in items}
    assert score_form(age_band, correct) == (30, 100.0, "090-100", 18)
    assert score_form(age_band, uncertain) == (0, 0.0, "000-010", 0)
    assert score_form(age_band, only_anchors) == (18, 60.0, "060-070", 18)
    assert score_form(age_band, only_context) == (12, 40.0, "040-050", 0)
    for malformed in ({}, {**correct, "extra": 0}, {**correct, items[0]["id"]: True},
                      {**correct, items[0]["id"]: -1}, {**correct, items[0]["id"]: 4}):
        with pytest.raises(ValueError):
            score_form(age_band, malformed)
    for invalid_width in (True, 10.0, 0, 3, -10):
        with pytest.raises(ValueError):
            score_form(age_band, correct, invalid_width)


def test_legacy_default_scoring_matches_the_default_age_form():
    correct = {item["id"]: item["correct"] for item in ITEMS}
    assert score_answers(correct) == score_form("18-24", correct)[:3]
    assert public_items() == public_form("18-24")["items"]


def test_content_counterexamples_resist_always_authority_or_always_more_search():
    _, items = form_for("18-24")
    by_id = {item["id"]: item for item in items}
    answer = lambda key: by_id[key]["options"][by_id[key]["correct"]]
    assert "照片" in answer("a07")  # actual event, not a planned opening table
    assert "信息够了" in answer("s06")  # information already sufficient
    assert "周二、周四" in answer("v26")  # use evidence, do not defer indefinitely
    for age_band in AGE_BANDS:
        item = next(item for item in form_for(age_band)[1] if item["id"] == "v30")
        assert "甲" not in item["question"] and "乙" not in item["question"]
        assert "AI选" in item["question"] and "\n" in item["question"]
        assert item["rationale"]  # Concrete objects still require applying both constraints.


def test_every_legacy_candidate_and_every_current_form_has_an_explicit_review():
    inventory = review_inventory()
    assert inventory["legacy_candidate_count"] == 48
    assert len({item["id"] for item in LEGACY_REVIEW}) == 48
    assert {item["id"] for item in LEGACY_REVIEW} >= {"a11", "e15", "e16", "c11", "d11", "o11"}
    assert all(item["reason"] and item["prior_knowledge_demand"] and item["replacement_item_ids"]
               for item in LEGACY_REVIEW)
    assert all(item["disposition"] == "移出原知识点并替换" for item in LEGACY_REVIEW
               if item["id"] in {"a11", "e15", "e16"})
    assert inventory["form_item_count"] == 270
    assert inventory["unique_current_material_count"] == 126
    old_stems = {item["question"] for item in LEGACY_REVIEW}
    assert not old_stems & {item["question"] for form in inventory["forms"] for item in form["items"]}
    # Export is defensive: a reviewer adding a note cannot edit the live bank.
    inventory["forms"][0]["items"][0]["options"][0] = "审查标记"
    assert "审查标记" not in form_for("12-14")[1][0]["options"]
