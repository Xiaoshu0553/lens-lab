"""Private review export coverage, correctness boundaries and HTML escaping."""
from copy import deepcopy
from html import escape
import json
from pathlib import Path
import subprocess
import sys

from app.age_forms import AGE_BANDS, public_form
from app.age_tasks import SCENARIOS, task_for
from app.instrument_review import inventory, render_review_html


def test_inventory_covers_all_forms_and_all_case_decisions_with_sources():
    result = inventory()
    assert result["legacy_candidate_count"] == 48
    assert result["form_item_count"] == 270
    assert result["unique_current_material_count"] == 126
    assert result["task_count"] == 9 and result["task_scenario_count"] == 36
    assert {form["age_band"] for form in result["forms"]} == set(AGE_BANDS)
    assert {task["age_band"] for task in result["tasks"]} == set(AGE_BANDS)
    for task in result["tasks"]:
        assert len(task["scenarios"]) == 4
        assert len(task["execution_options"]) == 5
        assert task["evidence_cards"] == task_for(task["age_band"])["evidence_cards"]
        assert task["scoring_dimensions"] and task["answer_basis"]
        for case in task["scenarios"]:
            expected = task_for(task["age_band"], case["scenario_id"])
            assert case["decision_options"] == expected["decision_options"]
            assert case["participant_facts"] == expected["participant_facts"]
            assert case["scenario_values"] == expected["scenario_values"]
            assert len(case["decision_options"]) == 5
        check_by_id = {check["id"]: check for check in task["content_checks"]}
        assert check_by_id["four_unique_optima"]["status"] == "通过"
        assert check_by_id["independent_execution"]["status"] == "通过"
        assert check_by_id["scoring_weights"]["status"] == "待验证"
        assert check_by_id["age_context"]["status"] == "待验证"
        assert check_by_id["deadline_action_wording"]["status"] != "需文字复核"


def test_inventory_does_not_hide_keys_but_participant_payload_does():
    report = inventory()
    assert "含答案" in report["access"]
    assert all("correct" in item and "rationale" in item and "expected_knowledge" in item
               for form in report["forms"] for item in form["items"])
    assert all("correct" not in item and "rationale" not in item
               for band in AGE_BANDS for item in public_form(band)["items"])
    report["forms"][0]["items"][0]["question"] = "审查专用改动"
    report["tasks"][0]["scenarios"][0]["participant_facts"] = "审查专用改动"
    refreshed = inventory()
    assert refreshed["forms"][0]["items"][0]["question"] != "审查专用改动"
    assert refreshed["tasks"][0]["scenarios"][0]["participant_facts"] != "审查专用改动"


def test_html_contains_all_question_and_task_material_without_external_resources():
    report = inventory()
    document = render_review_html(report)
    assert document.startswith("<!doctype html>") and document.endswith("</html>")
    assert 'lang="zh-CN"' in document and 'noindex,nofollow' in document
    assert document.count('<article class="question">') == 270 + 48
    for form in report["forms"]:
        for item in form["items"]:
            assert escape(item["question"], quote=True) in document
            assert escape(item["rationale"], quote=True) in document
            assert escape(item["expected_knowledge"], quote=True) in document
    for task in report["tasks"]:
        for card in task["evidence_cards"]:
            assert escape(card["body"], quote=True) in document
        for case in task["scenarios"]:
            assert escape(case["participant_facts"], quote=True) in document
            for option in case["decision_options"]:
                assert escape(option["text"], quote=True) in document
        for option in task["execution_options"]:
            assert escape(option["text"], quote=True) in document
    assert '<script' not in document and '<iframe' not in document
    assert 'src="http' not in document and 'href="http' not in document
    assert "非公开研究材料" in document and "不是实测难度" in document
    assert "不能证明同龄者常识一致" in document
    assert "75岁及以上" in document


def test_every_dynamic_field_is_text_escaped_including_attribute_anchors():
    report = deepcopy(inventory())
    payload = '<img src=x onerror="alert(1)">&\'"'
    report["access"] = payload
    report["review_version"] = payload
    report["generated_at"] = payload
    report["research_notes"][0] = payload
    for item in report["forms"][0]["items"][:1]:
        for field in ("id", "domain", "question", "rationale", "skill", "expected_knowledge", "review_note", "difficulty_basis"):
            item[field] = payload
        item["options"][0] = payload
    task = report["tasks"][0]
    for field in ("title", "task_id", "task_version", "instructions", "shared_context", "rubric"):
        task[field] = payload
    task["scenarios"][0]["participant_facts"] = payload
    task["scenarios"][0]["decision_options"][0]["text"] = payload
    task["execution_options"][0]["text"] = payload
    task["evidence_cards"][0]["body"] = payload
    report["legacy_items"][0]["reason"] = payload
    old_band = report["forms"][0]["age_band"]
    new_band = old_band + '"><script>alert(2)</script>'
    report["forms"][0]["age_band"] = new_band
    report["tasks"][0]["age_band"] = new_band
    document = render_review_html(report)
    assert payload not in document
    assert '<img src=x' not in document
    assert '<script>' not in document
    assert escape(payload, quote=True) in document
    assert 'id="age-' + escape(new_band, quote=True) + '"' in document


def test_export_script_creates_only_private_standalone_files(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "export_instrument_review.py"
    proc = subprocess.run([sys.executable, str(script), "--output-dir", str(tmp_path)],
                          capture_output=True, text=True, check=True)
    report_path = tmp_path / "全部问卷与任务审查.json"
    html_path = tmp_path / "全部问卷与任务审查.html"
    assert report_path.is_file() and html_path.is_file()
    assert (report_path.stat().st_mode & 0o777) == 0o600
    assert (html_path.stat().st_mode & 0o777) == 0o600
    report = json.loads(report_path.read_text())
    assert report["task_scenario_count"] == 36
    assert "旧题48；新表单题270；任务案例36" in proc.stdout
    assert render_review_html(report) == html_path.read_text()
