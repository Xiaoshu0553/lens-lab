"""Observed-answer alerts never modify sample, outcomes, dialogue or timing."""
import itertools
import json
import sqlite3

import pytest

import app.showcase_seed as showcase
import app.showcase_quality as quality
from app.age_forms import form_for
from app.db import connect
from app.synthetic_exports import BATCH_ID, CONTENT_DIRECTORY
from test_showcase_seed import showcase_input, seed


def base_v2(showcase_input, monkeypatch):
    cfg, root, source = showcase_input
    with monkeypatch.context() as patch:
        patch.setattr(showcase, "_run_observed_quality_migration", lambda *args, **kwargs: None)
        seed(cfg, root)
    return cfg, root, source


def apply(cfg, root):
    records, pretests, frozen, _digests = showcase._load_artifacts(root)
    with connect(cfg.database_path) as db:
        instruments = showcase._validate_instruments(db, frozen)
        values = {row["id"]: showcase._validated_row(row, pretests[row["id"]], frozen, instruments, cfg) for row in records}
        quality.apply_process_alert_migration(db, selection_id=showcase.SUBSET_SELECTION_ID,
                                             expected_sources={row["id"]: row for row in records},
                                             expected_answers={sid: value[0] for sid, value in values.items()},
                                             expected_snapshots={sid: value[1] for sid, value in values.items()})


def snapshot(db):
    return {table: [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("sessions", "messages", "model_events", "review_history")}


def longest_answer_run(row):
    answers = json.loads(row["literacy_answers_json"])
    positions = [answers[item["id"]] for item in form_for(row["age_band"])[1]]
    return max(len(list(group)) for _key, group in itertools.groupby(positions))


def test_alerts_recompute_actual_vectors_and_preserve_every_other_record_field(showcase_input, monkeypatch):
    cfg, root, _source = base_v2(showcase_input, monkeypatch)
    with connect(cfg.database_path) as db:
        before = snapshot(db)
        selection_before = db.execute("SELECT manifest_json FROM showcase_selections").fetchone()[0]
    apply(cfg, root)
    with connect(cfg.database_path) as db:
        after = snapshot(db)
        assert len(after["sessions"]) == 126
        for table in ("messages", "model_events", "review_history"):
            assert after[table] == before[table]
        for old, new in zip(before["sessions"], after["sessions"]):
            assert {key: new[key] for key in old if key != "flags_json"} == {key: value for key, value in old.items() if key != "flags_json"}
            flags = json.loads(new["flags_json"])
            assert ("pretest_long_identical_run" in flags) == (longest_answer_run(new) >= 12)
            assert "pretest_fast_completion" not in flags
            for detail in json.loads(new["process_alert_details_json"]):
                assert detail["quality_version"] == quality.QUALITY_VERSION
                assert "synthetic_pretest_time_seconds" not in detail["source_fields"]
                if detail["code"] == "pretest_long_identical_run":
                    assert detail["observed_value"] == longest_answer_run(new)
                    assert detail["threshold"] == 12
        assert db.execute("SELECT manifest_json FROM showcase_selections").fetchone()[0] == selection_before
        receipt = json.loads(db.execute("SELECT manifest_json FROM showcase_quality_migrations").fetchone()[0])
        assert receipt["pretest_timing_used"] is False
        assert receipt["sample_membership_changed"] is False
        assert receipt["scores_answers_choices_dialogue_timestamps_changed"] is False
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_simulated_pretest_speed_is_never_used_and_existing_evidence_alert_remains(showcase_input, monkeypatch):
    cfg, root, source = showcase_input
    selected = showcase.select_showcase_subset(source)[0]
    affected = selected[0]["id"]
    for row in source:
        row["synthetic_pretest_time_seconds"] = 1
        if row["id"] == affected:
            row["evidence_reveals"] = row["distinct_evidence_cards"] = 0
    path = root / BATCH_ID / CONTENT_DIRECTORY / "SYNTHETIC_校准演示_逐人记录.json"
    path.write_text(json.dumps(source, ensure_ascii=False))
    with monkeypatch.context() as patch:
        patch.setattr(showcase, "_run_observed_quality_migration", lambda *args, **kwargs: None)
        seed(cfg, root)
    apply(cfg, root)
    with connect(cfg.database_path) as db:
        rows = list(db.execute("SELECT * FROM sessions"))
        assert all(row["synthetic_pretest_time_seconds"] == 1 for row in rows)
        assert not any("pretest_fast_completion" in json.loads(row["flags_json"]) for row in rows)
        row = db.execute("SELECT * FROM sessions WHERE id=?", (affected,)).fetchone()
        assert "no_evidence_retrieved" in json.loads(row["flags_json"])
        evidence = next(detail for detail in json.loads(row["process_alert_details_json"]) if detail["code"] == "no_evidence_retrieved")
        assert evidence["observed_value"] == 0


def test_manual_review_and_interactive_records_are_preserved_and_audited(showcase_input, monkeypatch):
    cfg, root, _source = base_v2(showcase_input, monkeypatch)
    with connect(cfg.database_path) as db:
        original = dict(db.execute("SELECT * FROM sessions WHERE sim_literacy_group='低' LIMIT 1").fetchone())
        sid = original["id"]
        db.execute("UPDATE sessions SET review_status='invalid',reviewer='human',review_reason='keep manual decision' WHERE id=?", (sid,))
        interactive = dict(original)
        interactive.update(id="interactive-keep", fixture_conversation=0, source_batch_id=None, dataset_variant="interactive_demo")
        names = list(interactive)
        db.execute(f"INSERT INTO sessions ({','.join(names)}) VALUES ({','.join('?' for _ in names)})", list(interactive.values()))
    apply(cfg, root)
    with connect(cfg.database_path) as db:
        row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        assert row["review_status"] == "invalid" and row["reviewer"] == "human"
        assert row["flags_json"] == original["flags_json"] and json.loads(row["process_alert_details_json"]) == []
        current = dict(db.execute("SELECT * FROM sessions WHERE id='interactive-keep'").fetchone())
        assert {key: current[key] for key in interactive} == interactive
        receipt = json.loads(db.execute("SELECT manifest_json FROM showcase_quality_migrations").fetchone()[0])
        assert receipt["skipped_edited_records"] == [{"id": sid, "skip_reason": "manual_review_or_feedback_preserved"}]


@pytest.mark.parametrize("corruption", ["score", "answers", "message", "event", "time", "source"])
def test_anomalous_fixture_rejects_all_changes_atomically(showcase_input, monkeypatch, corruption):
    cfg, root, _source = base_v2(showcase_input, monkeypatch)
    with connect(cfg.database_path) as db:
        row = db.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        sid = row["id"]
        if corruption == "score":
            db.execute("UPDATE sessions SET score=score+1 WHERE id=?", (sid,))
        elif corruption == "answers":
            answers = json.loads(row["literacy_answers_json"])
            key = next(iter(answers)); answers[key] = (answers[key] + 1) % 4
            db.execute("UPDATE sessions SET literacy_answers_json=? WHERE id=?", (json.dumps(answers), sid))
        elif corruption == "message":
            db.execute("UPDATE messages SET content='edited' WHERE id=(SELECT MIN(id) FROM messages WHERE session_id=?)", (sid,))
        elif corruption == "event":
            db.execute("UPDATE model_events SET detail_json='{}' WHERE id=(SELECT MIN(id) FROM model_events WHERE session_id=?)", (sid,))
        elif corruption == "time":
            db.execute("UPDATE sessions SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?", (sid,))
        else:
            source = json.loads(row["synthetic_source_json"]); source["education_stage"] = "changed"
            db.execute("UPDATE sessions SET synthetic_source_json=? WHERE id=?", (json.dumps(source), sid))
        before = snapshot(db)
    with pytest.raises(ValueError, match="异常"):
        apply(cfg, root)
    with connect(cfg.database_path) as db:
        assert snapshot(db) == before
        assert "process_alert_details_json" not in {row["name"] for row in db.execute("PRAGMA table_info(sessions)")}
        assert not quality.quality_receipt_exists(db)


def test_write_failure_rolls_back_receipt_schema_and_previous_updates(showcase_input, monkeypatch):
    cfg, root, _source = base_v2(showcase_input, monkeypatch)
    with connect(cfg.database_path) as db:
        sid = next(row["id"] for row in db.execute("SELECT * FROM sessions ORDER BY id DESC") if longest_answer_run(row) >= 12)
        db.execute(f"CREATE TRIGGER fail_quality BEFORE UPDATE OF flags_json ON sessions WHEN OLD.id='{sid}' BEGIN SELECT RAISE(ABORT,'quality interruption'); END")
        before = snapshot(db)
    with pytest.raises(sqlite3.IntegrityError, match="quality interruption"):
        apply(cfg, root)
    with connect(cfg.database_path) as db:
        assert snapshot(db) == before
        assert "process_alert_details_json" not in {row["name"] for row in db.execute("PRAGMA table_info(sessions)")}
        assert not quality.quality_receipt_exists(db)


def test_restart_never_overwrites_later_manual_alerts_or_requires_source_again(showcase_input, monkeypatch):
    cfg, root, _source = base_v2(showcase_input, monkeypatch)
    apply(cfg, root)
    with connect(cfg.database_path) as db:
        sid = db.execute("SELECT id FROM sessions LIMIT 1").fetchone()[0]
        db.execute("UPDATE sessions SET flags_json='[\"manual_saved_flag\"]',process_alert_details_json='[]' WHERE id=?", (sid,))
        before = snapshot(db)
        receipt = db.execute("SELECT manifest_json FROM showcase_quality_migrations").fetchone()[0]
    (root / BATCH_ID / CONTENT_DIRECTORY / "SYNTHETIC_校准演示_逐人记录.json").unlink()
    seed(cfg, root)
    with connect(cfg.database_path) as db:
        assert snapshot(db) == before
        assert db.execute("SELECT manifest_json FROM showcase_quality_migrations").fetchone()[0] == receipt
