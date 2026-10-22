"""Isolation, reproducibility and score integrity of the showcase UI fixture."""
import csv
import copy
import hashlib
import json
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import app.main as main_module
import app.showcase_seed as showcase
import app.showcase_rendering as rendering
import app.student_showcase as student_showcase
from app.age_forms import AGE_BANDS, form_for, score_form
from app.age_tasks import task_for
from app.db import connect, init_db
from app.seed import seed_demo
from app.synthetic_exports import BATCH_ID, CONTENT_DIRECTORY, EXPORT_FILES
from test_flow import settings


def fixture_artifacts(root):
    """Build portable fixtures from the real current instruments and options."""
    content = root / BATCH_ID / CONTENT_DIRECTORY
    content.mkdir(parents=True)
    frozen = {age: {"form_version": form_for(age)[0], "items": form_for(age)[1],
                    "tasks": {scenario: task_for(age, scenario)
                              for scenario in ["first_3", "first_1", "return_3", "return_1"]}}
              for age in AGE_BANDS}
    cases = [("高", "S", [80] * 48), ("高", "A", [90, 100] * 25),
             ("中", "S", [70] * 154), ("中", "A", [80] * 148),
             ("低", "S", [60] * 97), ("低", "A", [60] * 63 + [70] * 27),
             ("低", "S", [None] * 13)]
    records, pretests = [], []
    for level, group, scores in cases:
        for score in scores:
            index = len(records)
            age = list(AGE_BANDS)[index % len(AGE_BANDS)]
            version, items = form_for(age)
            raw_target = {"高": 26, "中": 19, "低": 13}[level]
            answers = {item["id"]: item["correct"] if n < raw_target else 3 for n, item in enumerate(items)}
            raw, percent, band, anchor = score_form(age, answers)
            scenario = ["first_3", "first_1", "return_3", "return_1"][index % 4]
            if score in {60, 70, 90}:
                scenario = "first_3" if index % 2 else "return_3"
            snapshot = frozen[age]["tasks"][scenario]
            selected, execution = (next((a, b) for a in snapshot["decision_options"]
                                       for b in snapshot["execution_options"] if a["score"] + b["score"] == score)
                                   if score is not None else (None, None))
            components = dict(selected["score_components"]) if selected else {}
            if execution:
                components.update(execution["score_components"])
            start = datetime(2030, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index * 10)
            turns, clarifications = 5 + index % 3, int(group == "A")
            row = {"id": f"SYN-{index + 1:04d}", "is_synthetic": 1, "data_origin": "synthetic",
                   "record_kind": "synthetic", "run_mode": "simulation", "is_demo": 1,
                   "dataset_variant": "target_calibrated", "platform_analysis_eligible": 0,
                   "task_id": snapshot["id"], "group_id": group,
                   "interaction_condition": "standard" if group == "S" else "adaptive",
                   "age_band": age, "education_stage": "other", "topic_familiarity": 3,
                   "literacy_score": raw, "literacy_percent": percent, "literacy_band": band,
                   "anchor_score": anchor, "literacy_instrument_version": version,
                   "sim_literacy_group": level, "sim_response_profile": ["正常", "认真", "随意"][index % 3],
                   "synthetic_pretest_time_seconds": 400 + index, "sim_qc_flag": 0,
                   "scenario_id": scenario, "score": score, "status": "completed" if selected else "dropout",
                   "decision_choice_id": selected["id"] if selected else None,
                   "execution_choice_id": execution["id"] if execution else None,
                   "decision_components_json": components,
                   "decision_feasible": int(selected["success"] and execution["success"]) if selected else None,
                   "decision_optimal": int(selected["goal_met"] and execution["success"]) if selected else None,
                   "created_at": start.isoformat(),
                   "decision_started_at": (start + timedelta(seconds=270)).isoformat() if selected else None,
                   "ended_at": (start + timedelta(seconds=300)).isoformat() if selected else None,
                   "timestamp_is_simulated": 1, "task_completion_seconds": 300 if selected else None,
                   "user_turns": turns, "clarification_count": clarifications,
                   "evidence_reveals": 3, "distinct_evidence_cards": 3}
            records.append(row)
            pretests.append({"id": row["id"], "is_synthetic": 1, "data_origin": "synthetic",
                             "age_band": age, "form_version": version, **answers,
                             "literacy_score": raw, "literacy_percent": percent, "anchor_score": anchor})
    (content / "SYNTHETIC_冻结题目与评分.json").write_text(json.dumps(frozen, ensure_ascii=False))
    (content / "SYNTHETIC_校准演示_逐人记录.json").write_text(json.dumps(records, ensure_ascii=False))
    with (content / "SYNTHETIC_前测30题答案.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(pretests[0]))
        writer.writeheader()
        writer.writerows(pretests)
    return records


@pytest.fixture
def showcase_input(tmp_path):
    cfg = replace(settings(tmp_path), database_path=tmp_path / "showcase.sqlite3")
    init_db(cfg.database_path)
    seed_demo(cfg.database_path)
    root = tmp_path / "synthetic-datasets"
    records = fixture_artifacts(root)
    return cfg, root, records


def seed(cfg, root, **options):
    showcase.seed_showcase(cfg, "fixture-model-config", ("fixture-standard", "fixture-adaptive"),
                           "module", root, "literacy-age-v5", **options)


def test_all_126_records_keep_provenance_and_recompute_answers_and_frozen_scores(showcase_input):
    cfg, root, source = showcase_input
    selected, expected_manifest = showcase.select_showcase_subset(source)
    seed(cfg, root)
    with connect(cfg.database_path) as db:
        rows = list(db.execute("SELECT * FROM sessions ORDER BY id"))
        assert len(rows) == 126
        assert sum(row["review_status"] == "valid" for row in rows) == 126
        assert sum(row["ended_at"] is None for row in rows) == 0
        assert [row["id"] for row in rows] == expected_manifest["selected_ids"]
        assert Counter(row["age_band"] for row in rows) == Counter(row["age_band"] for row in selected)
        for row, original in zip(rows, selected):
            assert row["record_kind"] == "synthetic" and row["run_mode"] == "demo"
            assert row["is_synthetic"] == row["is_demo"] == row["timestamp_is_simulated"] == 1
            assert row["platform_analysis_eligible"] == 0 and row["fixture_conversation"] == 1
            assert row["model_config_id"] == "fixture-model-config" and row["model_name"] == "DEMO-RULE"
            assert row["prompt_version"] == ("fixture-standard" if row["group_id"] == "S" else "fixture-adaptive")
            assert json.loads(row["synthetic_source_json"]) == original
            adapted = rendering.adapted_row(original)
            assert row["created_at"] == adapted["created_at"] and row["ended_at"] == adapted["ended_at"]
            assert row["fixture_render_version"] == rendering.RENDER_VERSION
            assert row["fixture_time_shift_days"] == rendering.TIME_SHIFT_DAYS
            assert row["fixture_qc_derived"] == 0
            answers = json.loads(row["literacy_answers_json"])
            raw, percent, band, anchor = score_form(row["age_band"], answers)
            assert len(answers) == 30 and (raw, percent, band, anchor) == (
                row["literacy_score"], row["literacy_percent"], row["literacy_band"], row["anchor_score"])
            snapshot = json.loads(row["task_snapshot_json"])
            assert snapshot["scenario_id"] == row["scenario_id"] and "scenario_variants" not in snapshot
            if row["score"] is not None:
                decision = next(item for item in snapshot["decision_options"] if item["id"] == row["decision_choice_id"])
                execution = next(item for item in snapshot["execution_options"] if item["id"] == row["execution_choice_id"])
                assert decision["score"] + execution["score"] == row["score"] == original["score"]
                assert row["reviewer"] == "demo-fixture"
        means = {(row[0], row[1]): row[2] for row in db.execute(
            "SELECT sim_literacy_group,group_id,AVG(score) FROM sessions GROUP BY 1,2")}
        for (level, arm), value in means.items():
            scores = [row["score"] for row in selected if row["sim_literacy_group"] == level and row["group_id"] == arm]
            assert value == sum(scores) / len(scores)
        manifest = json.loads(db.execute("SELECT manifest_json FROM showcase_selections").fetchone()[0])
        assert manifest["selected_ids"] == expected_manifest["selected_ids"]
        assert manifest["no_mean_calibration"] is True and manifest["operation"] == "fresh_natural_v2_126_import"
        assert db.execute("SELECT COUNT(*) FROM sessions WHERE guardian_consented<>0").fetchone()[0] == 0


def test_fixture_dialogue_and_events_match_source_counts_without_claiming_model_latency(showcase_input):
    cfg, root, source = showcase_input
    source = showcase.select_showcase_subset(source)[0]
    seed(cfg, root)
    with connect(cfg.database_path) as db:
        for original in source:
            adapted = rendering.adapted_row(original)
            sid = original["id"]
            messages = list(db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY id", (sid,)))
            assert sum(row["role"] == "user" for row in messages) == original["user_turns"]
            assert sum(row["action"] in {"module_clarification", "prompt_clarification"} for row in messages) == original["clarification_count"]
            events = list(db.execute("SELECT * FROM model_events WHERE session_id=?", (sid,)))
            reveals = [json.loads(row["detail_json"]) for row in events if row["event_type"] == "evidence_reveal"]
            assert len(reveals) == original["evidence_reveals"]
            assert len({event["card_id"] for event in reveals}) == original["distinct_evidence_cards"]
            assert all(json.loads(row["detail_json"])["is_synthetic"] == 1 for row in events)
            assert not any(row["event_type"] == "reply_latency" for row in events)
            assert all(row["content"] for row in messages)
            assert all(row["created_at"] >= adapted["created_at"] for row in messages)
            if adapted["decision_started_at"]:
                assert all(row["created_at"] <= adapted["decision_started_at"] for row in messages)
        first = source[0]
        cards = {card["id"]: card for card in task_for(first["age_band"], first["scenario_id"])["evidence_cards"]}
        reveals = [json.loads(row[0])["card_id"] for row in db.execute(
            "SELECT detail_json FROM model_events WHERE session_id=? AND event_type='evidence_reveal' ORDER BY id", (first["id"],))]
        bodies = [row[0] for row in db.execute(
            "SELECT content FROM messages WHERE session_id=? AND action='evidence' ORDER BY id", (first["id"],))]
        assert len(reveals) == len(bodies) and all(cards[key]["body"] in text for key, text in zip(reveals, bodies))


def test_restart_is_idempotent_and_never_overwrites_demo_edits(showcase_input):
    cfg, root, source = showcase_input
    source = showcase.select_showcase_subset(source)[0]
    seed(cfg, root)
    with connect(cfg.database_path) as db:
        db.execute("UPDATE sessions SET review_status='invalid',reviewer='edited-reviewer' WHERE id=?", (source[0]["id"],))
        db.execute("UPDATE messages SET content='edited fixture message' WHERE id=(SELECT MIN(id) FROM messages)")
        db.execute("UPDATE tasks SET title='edited demo task' WHERE id=?", (source[0]["task_id"],))
        before = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ["sessions", "messages", "model_events", "review_history", "showcase_imports"]}
    # A completed import remains usable after the source mount becomes absent.
    (root / BATCH_ID / EXPORT_FILES["calibrated_json"][0]).unlink()
    seed(cfg, root)
    with connect(cfg.database_path) as db:
        assert before == {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}
        assert tuple(db.execute("SELECT review_status,reviewer FROM sessions WHERE id=?", (source[0]["id"],)).fetchone()) == ("invalid", "edited-reviewer")
        assert db.execute("SELECT content FROM messages ORDER BY id LIMIT 1").fetchone()[0] == "edited fixture message"
        assert db.execute("SELECT title FROM tasks WHERE id=?", (source[0]["task_id"],)).fetchone()[0] == "edited demo task"


@pytest.mark.parametrize("mode", ["pilot", "live"])
def test_real_collection_modes_are_rejected_without_touching_database(showcase_input, mode):
    cfg, root, _source = showcase_input
    with pytest.raises(ValueError, match="独立 showcase"):
        seed(replace(cfg, study_mode=mode), root)
    with connect(cfg.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert "is_synthetic" not in {row["name"] for row in db.execute("PRAGMA table_info(sessions)")}


def test_wrong_database_name_and_symlink_to_real_database_are_rejected(showcase_input, tmp_path):
    cfg, root, _source = showcase_input
    other = tmp_path / "lens.sqlite3"
    with pytest.raises(ValueError, match="独立 showcase"):
        seed(replace(cfg, database_path=other), root)
    assert not other.exists()
    target = tmp_path / "protected.sqlite3"
    target.write_bytes(b"must stay untouched")
    link = tmp_path / "linked" / "showcase.sqlite3"
    link.parent.mkdir()
    link.symlink_to(target)
    with pytest.raises(ValueError, match="独立 showcase"):
        seed(replace(cfg, database_path=link), root)
    assert target.read_bytes() == b"must stay untouched"


def test_create_app_rejects_hardlink_to_production_before_initialization(tmp_path, monkeypatch):
    protected_root = tmp_path / "protected-platform"
    production = protected_root / "data" / "lens.sqlite3"
    production.parent.mkdir(parents=True)
    production.write_bytes(b"existing participant database must stay untouched")
    demo = tmp_path / "demo" / "showcase.sqlite3"
    demo.parent.mkdir()
    demo.hardlink_to(production)
    assert demo.samefile(production)
    monkeypatch.setattr(student_showcase, "ROOT", protected_root)
    cfg = replace(settings(tmp_path), database_path=demo, showcase_mode=True)

    def forbidden_initialization(*_args, **_kwargs):
        pytest.fail("database initialization ran before the showcase hardlink guard")

    monkeypatch.setattr(main_module, "init_db", forbidden_initialization)
    monkeypatch.setattr(main_module, "seed_student_materials", forbidden_initialization)
    with pytest.raises(ValueError, match="不能覆盖真实受试者数据库"):
        main_module.create_app(cfg)
    assert production.read_bytes() == b"existing participant database must stay untouched"


def test_a_failure_mid_import_rolls_back_every_record_and_schema_change(showcase_input, monkeypatch):
    cfg, root, _source = showcase_input
    fixture = showcase._fixture_dialogue
    count = 0

    def interrupted(*args):
        nonlocal count
        count += 1
        if count == 6:
            raise RuntimeError("fixture import interrupted")
        return fixture(*args)

    monkeypatch.setattr(showcase, "_fixture_dialogue", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        seed(cfg, root)
    with connect(cfg.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert "is_synthetic" not in {row["name"] for row in db.execute("PRAGMA table_info(sessions)")}
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='showcase_imports'").fetchone()
    monkeypatch.setattr(showcase, "_fixture_dialogue", fixture)
    seed(cfg, root)
    with connect(cfg.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 126


@pytest.mark.parametrize("corruption", ["score", "answers", "missing_pretest"])
def test_invalid_source_is_rejected_before_any_schema_or_record_mutation(showcase_input, corruption):
    cfg, root, source = showcase_input
    content = root / BATCH_ID / CONTENT_DIRECTORY
    if corruption == "score":
        source[-14]["score"] += 1
        (content / "SYNTHETIC_校准演示_逐人记录.json").write_text(json.dumps(source))
    elif corruption == "missing_pretest":
        (content / "SYNTHETIC_前测30题答案.csv").unlink()
    else:
        path = content / "SYNTHETIC_前测30题答案.csv"
        path.write_text(path.read_text(encoding="utf-8-sig").replace(
            "SYN-0001,1,synthetic", "SYN-0001,0,synthetic", 1), encoding="utf-8-sig")
    with pytest.raises(ValueError):
        seed(cfg, root)
    with connect(cfg.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert "is_synthetic" not in {row["name"] for row in db.execute("PRAGMA table_info(sessions)")}


def seed_legacy_600(cfg, root, monkeypatch):
    records, pretests, frozen, digests = showcase._load_artifacts(root)
    with connect(cfg.database_path) as db:
        instruments = showcase._validate_instruments(db, frozen)
        showcase._ensure_provenance_columns(db)
        for row in records:
            value = showcase._validated_row(row, pretests[row["id"]], frozen, instruments, cfg)
            showcase._insert_fixture(db, row, value, cfg, "fixture-model-config", ("fixture-standard", "fixture-adaptive"),
                                     "module", "literacy-age-v5", natural=False)
        db.execute("CREATE TABLE showcase_imports (batch_id TEXT PRIMARY KEY, source_sha256_json TEXT NOT NULL, record_count INTEGER NOT NULL, provenance TEXT NOT NULL)")
        db.execute("INSERT INTO showcase_imports VALUES (?,?,?,?)", (BATCH_ID, showcase._json(digests), 600, "legacy 600 test fixture"))


def seed_legacy_v1(cfg, root, monkeypatch):
    seed_legacy_600(cfg, root, monkeypatch)
    records, _pretests, _frozen, digests = showcase._load_artifacts(root)
    _selected, manifest = showcase._select_legacy_subset(records, digests)
    with connect(cfg.database_path) as db:
        for sid in manifest["excluded_source_ids"]:
            for table in ("messages", "model_events", "review_history"):
                db.execute(f"DELETE FROM {table} WHERE session_id=?", (sid,))
            db.execute("DELETE FROM sessions WHERE id=?", (sid,))
        showcase._save_selection(db, manifest)


def test_migration_selects_identical_ids_preserves_source_and_interactive_sessions(showcase_input, monkeypatch):
    cfg, root, source = showcase_input
    selected, manifest = showcase.select_showcase_subset(source)
    seed_legacy_600(cfg, root, monkeypatch)
    with connect(cfg.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 600
        original = {row["id"]: dict(row) for row in db.execute("SELECT * FROM sessions")}
        interactive = dict(next(iter(original.values())))
        interactive.update(id="interactive-demo", dataset_variant="interactive_demo", source_batch_id=None,
                           fixture_conversation=0, synthetic_source_json=None)
        columns = list(interactive)
        db.execute(f"INSERT INTO sessions ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", list(interactive.values()))
        db.execute("INSERT INTO messages(session_id,role,content,action,created_at) VALUES (?,?,?,?,?)",
                   ("interactive-demo", "user", "keep this interactive message", "user", interactive["created_at"]))
    seed(cfg, root)
    with connect(cfg.database_path) as db:
        fixtures = [dict(row) for row in db.execute("SELECT * FROM sessions WHERE fixture_conversation=1 ORDER BY id")]
        assert [row["id"] for row in fixtures] == manifest["selected_ids"]
        assert len(fixtures) == 126 and db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 127
        presentation = {"created_at", "decision_started_at", "ended_at", "reviewed_at", "flags_json",
                        "fixture_render_version", "fixture_time_shift_days", "fixture_selection_id", "fixture_qc_derived",
                        "process_alert_details_json"}
        for row in fixtures:
            assert {key: value for key, value in row.items() if key not in presentation} == {
                key: value for key, value in original[row["id"]].items() if key not in presentation}
            assert row["fixture_render_version"] == rendering.RENDER_VERSION
        current_interactive = dict(db.execute("SELECT * FROM sessions WHERE id='interactive-demo'").fetchone())
        assert {key: current_interactive[key] for key in interactive} == interactive
        assert db.execute("SELECT content FROM messages WHERE session_id='interactive-demo'").fetchone()[0] == "keep this interactive message"
        receipt = json.loads(db.execute("SELECT manifest_json FROM showcase_selections").fetchone()[0])
        assert receipt["selected_ids"] == manifest["selected_ids"]
        assert len(receipt["removed_ids"]) == 474 and receipt["operation"] == "migrate_unedited_600_to_natural_v2"
        assert len(receipt["removed_review_history"]) == 461
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        # The original import receipt remains an audit of the prior full import.
        assert db.execute("SELECT record_count FROM showcase_imports").fetchone()[0] == 600
    seed(cfg, root)
    with connect(cfg.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 127


@pytest.mark.parametrize("edited", ["review", "message", "event", "source", "history"])
def test_migration_refuses_to_delete_edited_fixtures_atomically(showcase_input, monkeypatch, edited):
    cfg, root, source = showcase_input
    seed_legacy_600(cfg, root, monkeypatch)
    removed = showcase.select_showcase_subset(source)[1]["excluded_source_ids"][0]
    with connect(cfg.database_path) as db:
        if edited == "review":
            db.execute("UPDATE sessions SET reviewer='administrator' WHERE id=?", (removed,))
        elif edited == "message":
            db.execute("UPDATE messages SET content='edited' WHERE id=(SELECT MIN(id) FROM messages WHERE session_id=?)", (removed,))
        elif edited == "event":
            db.execute("UPDATE model_events SET detail_json='{}' WHERE id=(SELECT MIN(id) FROM model_events WHERE session_id=?)", (removed,))
        elif edited == "source":
            db.execute("UPDATE sessions SET education_stage='changed' WHERE id=?", (removed,))
        else:
            db.execute("INSERT INTO review_history(session_id,status,reason,reviewer,created_at) VALUES (?,?,?,?,?)",
                       (removed, "valid", "extra review", "administrator", source[0]["created_at"]))
        before = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("sessions", "messages", "model_events", "review_history")}
    with pytest.raises(ValueError, match="修改"):
        seed(cfg, root)
    with connect(cfg.database_path) as db:
        assert before == {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='showcase_selections'").fetchone()


def test_selection_is_order_independent_and_does_not_rank_scores_or_profiles(showcase_input):
    _cfg, _root, source = showcase_input
    selected, manifest = showcase.select_showcase_subset(source)
    changed = copy.deepcopy(list(reversed(source)))
    for index, row in enumerate(changed):
        row["score"] = index % 101
        row["sim_response_profile"] = "changed profile"
    other, _ = showcase.select_showcase_subset(changed)
    assert [row["id"] for row in selected] == [row["id"] for row in other]
    assert set(manifest["profile_counts"]) == {"正常", "认真", "随意"}
    assert all(row["status"] == "completed" for row in selected)
    expected = sorted([row for row in source if row["status"] == "completed"],
                      key=lambda row: (hashlib.sha256(f"{showcase.SUBSET_SELECTION_SEED}:{row['id']}".encode()).hexdigest(), row["id"]))[:126]
    assert manifest["selected_ids"] == sorted(row["id"] for row in expected)


def test_insufficient_complete_candidates_fail_without_partial_import(showcase_input):
    _cfg, _root, source = showcase_input
    remaining = source[:125]
    with pytest.raises(ValueError, match="不足126条"):
        showcase.select_showcase_subset(remaining)


def test_migration_failure_during_deletion_rolls_back_receipt_and_all_children(showcase_input, monkeypatch):
    import sqlite3
    cfg, root, source = showcase_input
    seed_legacy_600(cfg, root, monkeypatch)
    removed = showcase.select_showcase_subset(source)[1]["excluded_source_ids"][0]
    with connect(cfg.database_path) as db:
        db.execute(f"""CREATE TRIGGER fail_fixture_delete BEFORE DELETE ON messages
            WHEN OLD.session_id='{removed}' BEGIN SELECT RAISE(ABORT,'test interruption'); END""")
        counts = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("sessions", "messages", "model_events", "review_history")}
    with pytest.raises(sqlite3.IntegrityError, match="test interruption"):
        seed(cfg, root)
    with connect(cfg.database_path) as db:
        assert counts == {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in counts}
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='showcase_selections'").fetchone()


def test_age_scope_is_only_an_eligibility_filter_and_never_rebalances_categories(showcase_input):
    _cfg, _root, source = showcase_input
    ages = ["18-24", "25-34", "35-44"]
    selected, manifest = showcase.select_showcase_subset(source, allowed_ages=ages)
    eligible = [row for row in source if row["status"] == "completed" and row["age_band"] in ages]
    expected = sorted(eligible, key=lambda row: (
        hashlib.sha256(f"{showcase.SUBSET_SELECTION_SEED}:{row['id']}".encode()).hexdigest(), row["id"]))[:126]
    assert manifest["selected_ids"] == sorted(row["id"] for row in expected)
    assert set(row["age_band"] for row in selected).issubset(ages)
    assert manifest["allowed_ages"] == ages
    for bad in ([], ["unknown"], "18-24"):
        with pytest.raises(ValueError, match="allowed_ages"):
            showcase.select_showcase_subset(source, allowed_ages=bad)


def test_v1_to_v2_restores_new_source_ids_rerenders_retained_and_keeps_v1_audit(showcase_input, monkeypatch):
    cfg, root, source = showcase_input
    seed_legacy_v1(cfg, root, monkeypatch)
    with connect(cfg.database_path) as db:
        old_receipt = db.execute("SELECT manifest_json FROM showcase_selections WHERE selection_id=?",
                                 (showcase.LEGACY_SUBSET_SELECTION_ID,)).fetchone()[0]
        old_rows = {row["id"]: dict(row) for row in db.execute("SELECT * FROM sessions")}
        old_first_messages = {sid: db.execute("SELECT content FROM messages WHERE session_id=? ORDER BY id LIMIT 1", (sid,)).fetchone()[0]
                              for sid in old_rows}
    desired = showcase.select_showcase_subset(source)[1]["selected_ids"]
    seed(cfg, root)
    original = {row["id"]: row for row in source}
    with connect(cfg.database_path) as db:
        rows = {row["id"]: dict(row) for row in db.execute("SELECT * FROM sessions")}
        assert sorted(rows) == desired and len(rows) == 126
        assert db.execute("SELECT manifest_json FROM showcase_selections WHERE selection_id=?",
                          (showcase.LEGACY_SUBSET_SELECTION_ID,)).fetchone()[0] == old_receipt
        receipt = json.loads(db.execute("SELECT manifest_json FROM showcase_selections WHERE selection_id=?",
                                        (showcase.SUBSET_SELECTION_ID,)).fetchone()[0])
        assert receipt["operation"] == "migrate_v1_126_to_natural_v2"
        assert receipt["restored_ids"] == sorted(set(rows) - set(old_rows))
        assert receipt["removed_ids"] == sorted(set(old_rows) - set(rows))
        assert receipt["retained_ids"] == sorted(set(rows) & set(old_rows))
        assert receipt["restored_ids"] and receipt["removed_ids"] and receipt["retained_ids"]
        assert len(receipt["prior_fixture_review_history"]) == 126
        for sid, row in rows.items():
            assert json.loads(row["synthetic_source_json"]) == original[sid]
            assert row["score"] == original[sid]["score"]
            assert row["decision_choice_id"] == original[sid]["decision_choice_id"]
            assert row["execution_choice_id"] == original[sid]["execution_choice_id"]
            assert row["created_at"] == rendering.adapted_row(original[sid])["created_at"]
            assert row["fixture_render_version"] == rendering.RENDER_VERSION
        for sid in receipt["retained_ids"]:
            assert rows[sid]["literacy_answers_json"] == old_rows[sid]["literacy_answers_json"]
            assert rows[sid]["score"] == old_rows[sid]["score"]
            assert db.execute("SELECT content FROM messages WHERE session_id=? ORDER BY id LIMIT 1", (sid,)).fetchone()[0] != old_first_messages[sid]
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    seed(cfg, root)
    with connect(cfg.database_path) as db:
        assert sorted(row[0] for row in db.execute("SELECT id FROM sessions")) == desired


def test_v1_migration_rejects_restored_id_collision_with_interactive_record(showcase_input, monkeypatch):
    cfg, root, source = showcase_input
    seed_legacy_v1(cfg, root, monkeypatch)
    desired = set(showcase.select_showcase_subset(source)[1]["selected_ids"])
    with connect(cfg.database_path) as db:
        old_ids = {row[0] for row in db.execute("SELECT id FROM sessions")}
        collision = sorted(desired - old_ids)[0]
        row = dict(db.execute("SELECT * FROM sessions LIMIT 1").fetchone())
        row.update(id=collision, dataset_variant="interactive_demo", source_batch_id=None,
                   fixture_conversation=0, synthetic_source_json=None)
        names = list(row)
        db.execute(f"INSERT INTO sessions ({','.join(names)}) VALUES ({','.join('?' for _ in names)})", list(row.values()))
    with pytest.raises(ValueError, match="互动记录"):
        seed(cfg, root)
    with connect(cfg.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 127
        assert dict(db.execute("SELECT * FROM sessions WHERE id=?", (collision,)).fetchone()) == row
        assert not db.execute("SELECT 1 FROM showcase_selections WHERE selection_id=?", (showcase.SUBSET_SELECTION_ID,)).fetchone()


@pytest.mark.parametrize("enabled", [False, True])
def test_derived_qc_is_explicit_preserves_samples_and_uses_actual_answer_positions(showcase_input, enabled):
    cfg, root, source = showcase_input
    expected = showcase.select_showcase_subset(source)[1]["selected_ids"]
    seed(cfg, root, enable_derived_qc=enabled)
    with connect(cfg.database_path) as db:
        rows = list(db.execute("SELECT * FROM sessions ORDER BY id"))
        assert [row["id"] for row in rows] == expected
        assert all(row["fixture_qc_derived"] == int(enabled) for row in rows)
        # The candidate fixture has sim_qc_flag=0, but low-score forms contain a
        # recorded run of 17 identical positions. Enabled QC must use answers.
        low = next(row for row in rows if row["sim_literacy_group"] == "低")
        assert low["sim_qc_flag"] == 0
        assert "pretest_long_identical_run" in json.loads(low["flags_json"])
        # The v2 base toggle remains unchanged. A separate observed-answer
        # quality receipt now adds long-run alerts even when timing QC is off.
        if not enabled:
            assert db.execute("SELECT 1 FROM showcase_quality_migrations").fetchone()
        assert len(rows) == 126 and all(row["review_status"] == "valid" for row in rows)
    seed(cfg, root, enable_derived_qc=enabled)
    with pytest.raises(ValueError, match="配置已变更"):
        seed(cfg, root, enable_derived_qc=not enabled)


def test_default_task_flags_are_recomputed_without_unobserved_pretest_speed(showcase_input, monkeypatch):
    cfg, root, _source = showcase_input
    payloads = rendering.fixture_payloads

    def repeated_users(*args, **kwargs):
        messages, events = payloads(*args, **kwargs)
        return [(sid, role, "重复" if role == "user" else content, action, at)
                for sid, role, content, action, at in messages], events

    monkeypatch.setattr(rendering, "fixture_payloads", repeated_users)
    seed(cfg, root)
    with connect(cfg.database_path) as db:
        rows = list(db.execute("SELECT * FROM sessions"))
        assert len(rows) == 126
        assert all("repeated_user_messages" in json.loads(row["flags_json"]) for row in rows)
        assert not any(flag == "pretest_fast_completion" for row in rows for flag in json.loads(row["flags_json"]))
        assert all(row["review_status"] == "valid" for row in rows)
