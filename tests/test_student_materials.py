"""Student-cohort consistency and preservation checks, not validation of items."""
from collections import Counter
import json

import pytest

from app.age_forms import form_for as legacy_form_for
from app.db import connect, init_db, task_dict
from app.literacy import DOMAINS, ITEMS as LEGACY_ITEMS
from app.seed import seed_demo
from app.student_materials import (
    AGE_BANDS, ANCHOR_IDS, DESIGN_VERSION, EDUCATION_STAGES, FORM_VERSION,
    SCENARIOS, form_for, public_form, score_form, task_for,
)
from app.student_review import inventory, render_review_html
from app.student_seed import seed_student_materials


def test_one_new_form_serves_every_eligible_demographic_stratum():
    assert list(AGE_BANDS) == ["18-20", "21-23", "24-25"]
    assert set(EDUCATION_STAGES) == {"college", "bachelor"}
    assert DESIGN_VERSION == "university-v1"
    reference = form_for("18-20")
    assert reference[0] == FORM_VERSION == "student-literacy-v1-30"
    for band in AGE_BANDS:
        version, items = form_for(band)
        assert (version, items) == reference
        assert [item["id"] for item in items] == [item["id"] for item in LEGACY_ITEMS]
        assert Counter(item["domain"] for item in items) == {domain: 6 for domain in DOMAINS}
        assert Counter(item["correct"] for item in items) == {0: 10, 1: 10, 2: 10}
        assert {item["draft_difficulty"] for item in items} == {"unrated"}
        assert all(len(item["options"]) == 4 and len(set(item["options"])) == 4 for item in items)
        assert all(item["options"][-1] == "我暂时不会选" for item in items)
        assert all(item["rationale"] and item["expected_knowledge"] for item in items)
    assert legacy_form_for("18-24")[0] != FORM_VERSION
    assert not {item["question"] for item in reference[1]} & {item["question"] for item in LEGACY_ITEMS}
    reference[1][0]["options"][0] = "外部修改"
    assert "外部修改" not in form_for("18-20")[1][0]["options"]


@pytest.mark.parametrize("band", AGE_BANDS)
def test_scoring_validates_exact_answers_and_preserves_compatibility_shape(band):
    _, items = form_for(band)
    correct = {item["id"]: item["correct"] for item in items}
    assert score_form(band, correct) == (30, 100.0, "090-100", 18)
    uncertain = {item["id"]: 3 for item in items}
    assert score_form(band, uncertain) == (0, 0.0, "000-010", 0)
    component = {item["id"]: item["correct"] if item["id"] in ANCHOR_IDS else 3 for item in items}
    assert score_form(band, component) == (18, 60.0, "060-070", 18)
    for answers in ({}, {**correct, "extra": 0}, {**correct, "s01": True},
                    {**correct, "s01": -1}, {**correct, "s01": 4}):
        with pytest.raises(ValueError):
            score_form(band, answers)
    for width in (True, 10.0, 0, -10, 3):
        with pytest.raises(ValueError):
            score_form(band, correct, width)


def test_public_form_omits_keys_and_rejects_inactive_legacy_age_bands():
    for band in AGE_BANDS:
        public = public_form(band)
        assert public["item_count"] == 30
        assert all(set(item) == {"id", "domain", "question", "options"} for item in public["items"])
        assert not any(key in json.dumps(public) for key in ("correct", "rationale", "review_note", "skill"))
        public["items"][0]["options"].clear()
        assert len(public_form(band)["items"][0]["options"]) == 4
    for band in ("12-14", "18-24", "25-34", "75-plus", ""):
        with pytest.raises(ValueError, match="年龄组"):
            public_form(band)
        with pytest.raises(ValueError, match="年龄组"):
            task_for(band)


def test_campus_task_is_identical_between_ages_and_both_private_facts_change_optimum():
    expected = {"first_3": "new_onsite", "first_1": "new_flexible",
                "return_3": "return_onsite", "return_1": "return_flexible"}
    reference = task_for("18-20")
    for band in AGE_BANDS:
        task = task_for(band)
        assert task["id"] == f"student-campus-{band}-v1"
        assert task_for(band, is_demo=False)["id"] == task["id"] + "-live"
        assert {key: value for key, value in task.items() if key not in ("id", "age_band")} == {
            key: value for key, value in reference.items() if key not in ("id", "age_band")}
        assert "21岁" in task["shared_context"] and "在读本科生" in task["shared_context"]
        assert "18–25岁" in task["reference_text"] and "在读专科生或本科生" in task["reference_text"]
        assert "研究生不符合" in task["reference_text"]
        assert "每周最多可到场" not in task["shared_context"]
        assert "获得过本项目" not in task["shared_context"]
        assert {slot["key"] for slot in task["slots"]} == {"weekly_availability", "prior_project_place"}
        for scenario, best_id in expected.items():
            case = task_for(band, scenario)
            assert [option["id"] for option in case["decision_options"] if option["score"] == 80] == [best_id]
            assert case["reference_text"] == task["reference_text"]
            assert case["shared_context"] == task["shared_context"]
            assert case["execution_options"] == task["execution_options"]
            assert [(option["id"], option["text"]) for option in case["decision_options"]] == [
                (option["id"], option["text"]) for option in task["decision_options"]]
            assert all(sum(option["score_components"].values()) == option["score"]
                       for option in case["decision_options"] + case["execution_options"])
        assert [option["id"] for option in task["execution_options"] if option["score"] == 20] == ["service_current_complete"]
    with pytest.raises(ValueError, match="案例版本"):
        task_for("18-20", "unknown")
    changed = task_for("18-20")
    changed["scenario_variants"][0]["decision_options"].clear()
    assert len(task_for("18-20")["scenario_variants"][0]["decision_options"]) == 5


def test_student_seeding_keeps_historical_rows_and_existing_task_edits(tmp_path):
    path = tmp_path / "study.sqlite3"
    init_db(path)
    seed_demo(path)
    with connect(path) as conn:
        conn.execute("""INSERT INTO sessions
            (id,task_id,group_id,expression_condition,interaction_condition,run_mode,is_demo,
             assignment_seed,model_name,model_temperature,model_base_url,prompt_version,
             task_snapshot_json,created_at,literacy_answers_json,literacy_score)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("legacy-session", "pilot-age-18-24-v4", "standard", "free", "standard", "demo", 1,
             "frozen", "old-model", 0.2, "https://example.invalid", "old-prompt", '{"old":true}',
             "2026-01-01", '{"s01":2}', 17))
        frozen = tuple(conn.execute("SELECT * FROM sessions WHERE id='legacy-session'").fetchone())
        old = dict(conn.execute("SELECT * FROM tasks WHERE id='pilot-age-18-24-v4'").fetchone())
        seed_student_materials(conn)
        assert tuple(conn.execute("SELECT * FROM sessions WHERE id='legacy-session'").fetchone()) == frozen
        archived = dict(conn.execute("SELECT * FROM tasks WHERE id='pilot-age-18-24-v4'").fetchone())
        assert archived == {**old, "active": 0}
        assert {row["age_band"] for row in conn.execute("SELECT * FROM tasks WHERE active=1")} == set(AGE_BANDS)
        for band in AGE_BANDS:
            stored = task_dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_for(band)["id"],)).fetchone())
            assert len(stored["scenario_variants"]) == len(SCENARIOS)
            assert stored["execution_options"] == task_for(band)["execution_options"]
        conn.execute("UPDATE tasks SET title='研究者编辑', active=0 WHERE id='student-campus-18-20-v1'")
        seed_student_materials(conn)
        row = conn.execute("SELECT * FROM tasks WHERE id='student-campus-18-20-v1'").fetchone()
        assert row["title"] == "研究者编辑" and row["active"] == 0
        seed_student_materials(conn, is_demo=False)
        assert conn.execute("SELECT count(*) FROM tasks WHERE id LIKE 'student-campus-%'").fetchone()[0] == 6
        assert conn.execute("SELECT count(*) FROM tasks WHERE is_demo=0 AND active=1").fetchone()[0] == 3
        assert tuple(conn.execute("SELECT * FROM sessions WHERE id='legacy-session'").fetchone()) == frozen


def test_seed_preserves_researcher_created_active_task(tmp_path):
    path = tmp_path / "study.sqlite3"
    init_db(path)
    with connect(path) as conn:
        seed_student_materials(conn)
        conn.execute("UPDATE tasks SET id='researcher-custom-student', title='自建' WHERE id='student-campus-18-20-v1'")
        seed_student_materials(conn)
        assert conn.execute("SELECT title,active FROM tasks WHERE id='researcher-custom-student'").fetchone()[:] == ("自建", 1)
        assert conn.execute("SELECT active FROM tasks WHERE id='student-campus-18-20-v1'").fetchone()[0] == 0


def test_review_counts_unique_materials_and_escapes_all_content():
    report = inventory()
    assert report["form_item_count"] == report["unique_current_material_count"] == 30
    assert report["unique_task_count"] == 1 and report["task_count"] == 3
    assert report["task_scenario_count"] == 4 and report["stored_task_scenario_count"] == 12
    assert len(report["forms"]) == 1 and report["legacy_items"] == []
    html = render_review_html(report)
    assert html.count('class="question"') == 30
    assert "75岁" not in html and "12–14" not in html and "18道共同题" not in html
    assert "不得按新材料重写" in html
    assert all(case[0] in html for case in SCENARIOS)
    report["forms"][0]["items"][0]["question"] = '<script>alert("x")</script>'
    escaped = render_review_html(report)
    assert '<script>' not in escaped and '&lt;script&gt;' in escaped
    assert '<script>' not in inventory()["forms"][0]["items"][0]["question"]
