"""Offline SQLite migration fixtures; no model calls or participant data."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from app.translation import TranslationStart
from app.translation_materials import (FORM_ITEMS, SELF_ITEMS, FORM_VERSION,
    TASK_VERSION, PROMPT_VERSION, score_form)
from app.translation_provenance import evidence_sha256


SPEC = importlib.util.spec_from_file_location("translation_data_audit", Path(__file__).parents[1] / "scripts" / "audit_translation_data.py")
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def _fixture(tmp_path, count=24):
    rows, plans, starts = [], [], []
    for index in range(count):
        sid, rid = f"session-{index:03d}", f"request-start-{index:03d}"
        answers = {item["id"]: item["correct"] for item in FORM_ITEMS}
        self_answers = {item["id"]: 2 for item in SELF_ITEMS}
        ability = score_form(answers, self_answers)
        payload = {"consent": True, "university_confirmed": True, "age_band": "20", "education_stage": "bachelor",
                   "undergraduate_year": "year-2", "translation_experience": 3, "pretest_version": FORM_VERSION,
                   "answers": answers, "self_answers": self_answers, "request_id": rid, "researcher_preview": False}
        submission = "这是一段离线测试中的完整译文。人物在码头等候，随后听到钟声响起。"
        rounds, messages, requests, events = [], [], {}, []
        for number in (1, 2):
            mrid, content = f"message-{index}-{number}", f"离线测试请求{number}"
            reply = {"role": "assistant", "content": "请补充人物设定。" if number == 1 else audit.FINAL_BEGIN + submission + audit.FINAL_END,
                     "action": "clarification" if number == 1 else "answer", "created_at": "2026-10-30T00:00:10+00:00"}
            rounds.append({"request_id": mrid, "content": content, "status": "completed", "reply": deepcopy(reply)})
            messages.extend([{"role": "user", "content": content, "action": "message", "created_at": "2026-10-30T00:00:09+00:00"}, reply])
            requests[mrid] = {"content": content, "status": "completed"}
            events.append({"type": "reply", "request_id": mrid, "seconds": 1.2,
                           "reported_model": "offline-test-fixture", "created_at": "2026-10-30T00:00:10+00:00"})
        row = {"id": sid, "record_kind": "participant", "data_purpose": "demonstration", "study_mode": "pilot",
               "condition": "S" if index % 2 else "A", "answers": answers, "self_answers": self_answers,
               "age_band": "20", "education_stage": "bachelor", "undergraduate_year": "year-2", "translation_experience": 3,
               "university_confirmed": True, "band": ability["band"], "ability_score": ability["score"], "ability": ability,
               "versions": {"form": FORM_VERSION, "task": TASK_VERSION, "prompt": PROMPT_VERSION},
               "created_at": "2026-10-30T00:00:00+00:00", "ended_at": "2026-10-30T00:00:20+00:00",
               "messages": messages, "requests": requests, "events": events, "submission": submission,
               "assessment": None, "assessment_status": "pending", "draft": submission, "cohort_id": "original-collection"}
        plans.append({"sample_id": f"P{index + 1:03d}", "session_id": sid, "start_payload": payload,
                      "status": "completed", "rounds": rounds, "submission": submission, "condition": row["condition"],
                      "target_band": row["band"], "ability": ability, "created_at": row["created_at"],
                      "ended_at": row["ended_at"], "actual_turns": 2, "model_reply_events": deepcopy(events)})
        rows.append(row)
        starts.append((rid, audit._payload_hash(payload), sid))
    manual = {**deepcopy(rows[0]), "id": "manual-verified-by-operator"}
    preview = {**deepcopy(rows[0]), "id": "preview", "record_kind": "researcher_preview", "data_purpose": "research"}
    rows.extend([manual, preview])
    database = tmp_path / "collection.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE translation_sessions (id TEXT PRIMARY KEY, record_json TEXT NOT NULL)")
        db.execute("CREATE TABLE translation_starts (request_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, session_id TEXT NOT NULL)")
        db.executemany("INSERT INTO translation_sessions VALUES (?,?)", [(row["id"], audit._dump(row)) for row in rows])
        db.executemany("INSERT INTO translation_starts VALUES (?,?,?)", starts)
    manifest = {"generator_version": audit.GENERATOR_VERSION, "count": count, "form_version": FORM_VERSION,
                "task_version": TASK_VERSION, "prompt_version": PROMPT_VERSION, "samples": plans}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return database, path, rows, manifest


def _read(database):
    with audit._connect(database) as db:
        return audit._rows(db)


def test_start_hash_matches_actual_pydantic_defaults(tmp_path):
    _, _, _, manifest = _fixture(tmp_path, count=1)
    payload = manifest["samples"][0]["start_payload"]
    body = TranslationStart(**payload).model_dump(exclude={"request_id", "access_code"})
    assert audit._payload_hash(payload) == audit._sha(body)


def test_default_dry_run_verifies_24_without_writing_or_calling_any_api(tmp_path, monkeypatch):
    database, manifest_path, rows, _ = _fixture(tmp_path)
    before_bytes = database.read_bytes()
    report = audit.run_audit(database, manifest_path=manifest_path)
    assert report["mode"] == "dry_run" and report["matched_manifest_records"] == 24
    assert report["changed_records"] == 24 and report["backup_path"] is None
    assert report["after"]["counts"]["collection_method"] == {"automated_script": 24, "unverified": 1, "researcher_preview": 1}
    assert report["before"]["counts"]["data_purpose"] == report["after"]["counts"]["data_purpose"] == {"demonstration": 25, "research": 1}
    assert database.read_bytes() == before_bytes
    assert _read(database) == {row["id"]: row for row in rows}
    assert not list(tmp_path.glob("*backup*"))


def test_apply_backs_up_then_atomically_adds_sources_preserving_every_original_field(tmp_path):
    database, manifest_path, rows, _ = _fixture(tmp_path)
    backup = tmp_path / "private" / "original.sqlite3"
    report = audit.run_audit(database, manifest_path=manifest_path, apply=True, backup_path=backup,
        manual_ids=["manual-verified-by-operator"], attestation_note="本人手动完成的平台验证")
    assert report["changed_records"] == 25 and report["original_evidence_unchanged"] is True
    assert backup.stat().st_mode & 0o777 == 0o600
    assert _read(backup) == {row["id"]: row for row in rows}
    stored = _read(database)
    for original in rows:
        assert evidence_sha256(stored[original["id"]]) == evidence_sha256(original)
        assert stored[original["id"]]["data_purpose"] == original["data_purpose"]
        assert stored[original["id"]]["created_at"] == original["created_at"]
        assert stored[original["id"]]["ended_at"] == original["ended_at"]
        assert stored[original["id"]]["assessment"] is None
    automated = stored["session-000"]["provenance"]
    assert automated["collection_method"] == "automated_script" and automated["batch_id"].startswith("manifest-sha256:")
    assert automated["evidence"][0]["record_evidence_sha256"] == evidence_sha256(rows[0])
    manual = stored["manual-verified-by-operator"]["provenance"]
    assert manual["collection_method"] == "manual_attested" and manual["batch_id"] is None
    assert manual["evidence"][0]["note"] == "本人手动完成的平台验证"
    assert "attested_at" in manual["evidence"][0]
    assert "provenance" not in stored["preview"]


def test_second_apply_is_idempotent_and_preserves_first_attestation_time(tmp_path, monkeypatch):
    database, manifest_path, _, _ = _fixture(tmp_path)
    first = audit.run_audit(database, manifest_path=manifest_path, apply=True,
                           manual_ids=["manual-verified-by-operator"], attestation_note="confirmed")
    stored = _read(database)
    monkeypatch.setattr(audit, "_now", lambda: "2099-01-01T00:00:00+00:00")
    second = audit.run_audit(database, manifest_path=manifest_path, apply=True,
                            manual_ids=["manual-verified-by-operator"], attestation_note="confirmed")
    assert first["changed_records"] == 25
    assert second["changed_records"] == 0 and second["backup_path"] is None
    assert _read(database) == stored


@pytest.mark.parametrize("damage", ["final_submission", "start_hash", "reply", "reply_event", "duplicate_session", "timestamp"])
def test_manifest_mismatch_fails_before_any_backup_or_partial_mutation(tmp_path, damage):
    database, path, rows, manifest = _fixture(tmp_path)
    plan = manifest["samples"][1]
    if damage == "final_submission":
        plan["submission"] += "被修改"
    elif damage == "start_hash":
        plan["start_payload"]["translation_experience"] = 5
    elif damage == "reply":
        plan["rounds"][0]["reply"]["content"] = "不同回复"
    elif damage == "reply_event":
        plan["model_reply_events"][0]["seconds"] = 999
    elif damage == "duplicate_session":
        plan["session_id"] = manifest["samples"][0]["session_id"]
    elif damage == "timestamp":
        plan["ended_at"] = "2099-01-01T00:00:00+00:00"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    backup = tmp_path / "should-not-exist.sqlite3"
    with pytest.raises(audit.AuditError):
        audit.run_audit(database, manifest_path=path, apply=True, backup_path=backup)
    assert not backup.exists()
    assert _read(database) == {row["id"]: row for row in rows}


def test_manual_attestation_cannot_overwrite_generated_source_or_partially_apply(tmp_path):
    database, path, rows, _ = _fixture(tmp_path)
    with pytest.raises(audit.AuditError, match="conflicts"):
        audit.run_audit(database, manifest_path=path, apply=True, manual_ids=["session-000"], attestation_note="manual claim")
    assert _read(database) == {row["id"]: row for row in rows}
    audit.run_audit(database, manifest_path=path, apply=True)
    stored = _read(database)
    with pytest.raises(audit.AuditError, match="conflicts"):
        audit.run_audit(database, apply=True, manual_ids=["manual-verified-by-operator", "session-000"], attestation_note="manual claim")
    assert _read(database) == stored


def test_generated_evidence_cannot_be_hidden_by_changing_method_back_to_unverified(tmp_path):
    database, _, rows, _ = _fixture(tmp_path)
    row = rows[0]
    row["provenance"] = {"version": 1, "collection_method": "unverified", "batch_id": None,
                         "evidence": [{"type": "generator_manifest_verified", "evidence_id": "old-evidence"}]}
    with sqlite3.connect(database) as db:
        db.execute("UPDATE translation_sessions SET record_json=? WHERE id=?", (audit._dump(row), row["id"]))
    with pytest.raises(audit.AuditError, match="conflicts"):
        audit.run_audit(database, apply=True, manual_ids=[row["id"]], attestation_note="manual claim")
    assert _read(database)[row["id"]] == row


def test_backup_is_never_overwritten_and_write_failure_rolls_back_every_annotation(tmp_path, monkeypatch):
    database, path, rows, _ = _fixture(tmp_path)
    backup = tmp_path / "already-exists"
    backup.write_text("keep me", encoding="utf-8")
    with pytest.raises(audit.AuditError, match="already exists"):
        audit.run_audit(database, manifest_path=path, apply=True, backup_path=backup)
    assert backup.read_text() == "keep me"
    assert _read(database) == {row["id"]: row for row in rows}
    # Fail on the second row to prove that a preceding UPDATE cannot commit.
    with sqlite3.connect(database) as db:
        db.execute("CREATE TRIGGER prevent_second_update BEFORE UPDATE ON translation_sessions WHEN OLD.id='session-001' BEGIN SELECT RAISE(FAIL, 'fixture failure'); END")
    with pytest.raises(sqlite3.Error):
        audit.run_audit(database, manifest_path=path, apply=True)
    assert _read(database) == {row["id"]: row for row in rows}


def test_attestation_requires_a_note_and_cannot_turn_preview_into_a_participant(tmp_path):
    database, _, rows, _ = _fixture(tmp_path)
    with pytest.raises(audit.AuditError, match="explicit operator note"):
        audit.run_audit(database, apply=True, manual_ids=["manual-verified-by-operator"])
    with pytest.raises(audit.AuditError, match="preview"):
        audit.run_audit(database, apply=True, manual_ids=["preview"], attestation_note="manual")
    assert _read(database) == {row["id"]: row for row in rows}


def test_cli_is_read_only_by_default_and_safe_errors_do_not_echo_raw_data(tmp_path, capsys):
    database, path, rows, _ = _fixture(tmp_path)
    assert audit.main(["--database", str(database), "--manifest", str(path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "dry_run" and output["changed_records"] == 24
    assert _read(database) == {row["id"]: row for row in rows}
    path.write_text("SECRET malformed manifest", encoding="utf-8")
    assert audit.main(["--database", str(database), "--manifest", str(path), "--apply"]) == 1
    error = capsys.readouterr().err
    assert "SECRET" not in error and "not valid JSON" in error
