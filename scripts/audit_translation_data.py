#!/usr/bin/env python3
"""Audit a local SQLite collection; explicitly apply verified source annotations.

This is a trusted local administrator tool, not a public HTTP endpoint.  It
does not load environment files, use credentials, call APIs, alter purpose,
change timestamps, rewrite translations, or create assessment scores.

Default: read-only dry-run and JSON on stdout.  ``--apply`` requires a manifest
or an explicit operator attestation, takes a private SQLite backup before any
change, and updates all matched rows atomically.  Source evidence is appended;
existing incompatible attestations cannot be overwritten.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.translation_provenance import (PROVENANCE_VERSION, audit_records,
    evidence_sha256, normalized_provenance)


GENERATOR_VERSION = "translation-http-presentation-v1"
FINAL_BEGIN = "【最终译文】"
FINAL_END = "【译文结束】"


class AuditError(RuntimeError):
    """Safe descriptions exclude raw questionnaire, dialogue and credentials."""


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _sha(value) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(path: Path, *, writable: bool = False) -> sqlite3.Connection:
    if not path.is_file():
        raise AuditError("Database file does not exist")
    db = sqlite3.connect(path.resolve().as_uri() + ("?mode=rw" if writable else "?mode=ro"),
                         uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=15000")
    return db


def _rows(db: sqlite3.Connection) -> dict[str, dict]:
    result = {}
    for stored in db.execute("SELECT id,record_json FROM translation_sessions ORDER BY id"):
        try:
            row = json.loads(stored["record_json"])
        except (TypeError, ValueError) as exc:
            raise AuditError("A translation record is not valid JSON") from exc
        if not isinstance(row, dict) or row.get("id") != stored["id"]:
            raise AuditError("A translation record ID differs from its database key")
        result[row["id"]] = row
    return result


def _require(condition: bool, index: int, description: str) -> None:
    if not condition:
        raise AuditError(f"Manifest sample {index}: {description}")


def _payload_hash(payload: dict) -> str:
    # Match TranslationStart.model_dump(exclude={'request_id','access_code'})
    # including model defaults, without importing or changing live app settings.
    keys = {"consent", "university_confirmed", "age_band", "education_stage", "undergraduate_year",
            "translation_experience", "pretest_version", "answers", "self_answers", "researcher_preview",
            "preview_condition", "request_id"}
    if set(payload) - keys:
        raise AuditError("Manifest start payload contains an unsupported field")
    body = {key: value for key, value in payload.items() if key != "request_id"}
    body.setdefault("researcher_preview", False)
    body.setdefault("preview_condition", None)
    return _sha(body)


def verify_manifest(db: sqlite3.Connection, rows: dict[str, dict], manifest: dict) -> list[tuple[dict, dict]]:
    """Match the full persisted request/reply sequence before attributing source."""
    if not isinstance(manifest, dict) or manifest.get("generator_version") != GENERATOR_VERSION:
        raise AuditError("Unsupported generator manifest")
    samples = manifest.get("samples")
    if (not isinstance(samples, list) or not samples or type(manifest.get("count")) is not int
            or manifest["count"] != len(samples)):
        raise AuditError("Manifest sample count is invalid")
    seen_sessions, seen_requests, matches = set(), set(), []
    for index, plan in enumerate(samples, 1):
        _require(isinstance(plan, dict), index, "sample is invalid")
        sid, payload = plan.get("session_id"), plan.get("start_payload")
        _require(isinstance(sid, str) and sid in rows and sid not in seen_sessions, index,
                 "session is missing, unknown or repeated")
        _require(isinstance(payload, dict), index, "start payload is missing")
        request_id = payload.get("request_id")
        _require(isinstance(request_id, str) and request_id and request_id not in seen_requests, index,
                 "start request is missing or repeated")
        seen_sessions.add(sid)
        seen_requests.add(request_id)
        start = db.execute("SELECT payload_hash,session_id FROM translation_starts WHERE request_id=?",
                           (request_id,)).fetchone()
        _require(start is not None and start["session_id"] == sid
                 and start["payload_hash"] == _payload_hash(payload), index, "start request mapping or payload hash differs")
        row = rows[sid]
        _require(row.get("record_kind") == "participant" and payload.get("researcher_preview") is False
                 and payload.get("consent") is True and payload.get("university_confirmed") is True,
                 index, "participant workflow differs")
        for field in ("answers", "self_answers", "age_band", "education_stage", "undergraduate_year",
                      "translation_experience", "university_confirmed"):
            _require(field in payload and row.get(field) == payload[field], index, f"{field} differs")
        versions = row.get("versions", {})
        _require(isinstance(versions, dict), index, "material versions are invalid")
        for manifest_key, row_key in (("form_version", "form"), ("task_version", "task"), ("prompt_version", "prompt")):
            expected = manifest.get(manifest_key)
            if manifest_key == "prompt_version" and expected is None:
                preflight = manifest.get("preflight")
                if isinstance(preflight, dict) and isinstance(preflight.get("versions"), dict):
                    expected = preflight["versions"].get("prompt")
            _require(isinstance(expected, str) and versions.get(row_key) == expected, index, f"{row_key} version differs")
        _require(payload.get("pretest_version") == versions.get("form"), index, "questionnaire version differs")
        _require(isinstance(plan.get("ability"), dict), index, "ability evidence is missing")
        _require(plan.get("target_band") == row.get("band") and plan.get("ability") == row.get("ability")
                 and row.get("ability_score") == plan.get("ability", {}).get("score"), index, "ability assignment differs")
        if "condition" in plan:
            _require(plan["condition"] == row.get("condition"), index, "condition differs")
        _require(plan.get("status") == "completed" and bool(row.get("ended_at")), index, "session is incomplete")
        rounds, messages = plan.get("rounds"), row.get("messages")
        _require(isinstance(rounds, list) and bool(rounds) and isinstance(messages, list)
                 and all(isinstance(message, dict) for message in messages)
                 and len(messages) == len(rounds) * 2, index, "dialogue length differs")
        row_requests = row.get("requests", {})
        events = row.get("events", [])
        _require(isinstance(row_requests, dict) and all(isinstance(value, dict) for value in row_requests.values())
                 and isinstance(events, list) and all(isinstance(event, dict) for event in events),
                 index, "request or event audit is malformed")
        reply_events = [event for event in events if event.get("type") == "reply"]
        _require(len(reply_events) == len(rounds), index, "reply audit count differs")
        seen_round_requests = set()
        for number, turn in enumerate(rounds):
            _require(isinstance(turn, dict), index, "round is invalid")
            user, assistant = messages[2 * number:2 * number + 2]
            rid = turn.get("request_id")
            _require(isinstance(rid, str) and rid and rid not in seen_round_requests, index, "round request repeats or is missing")
            seen_round_requests.add(rid)
            _require(turn.get("status") == "completed" and user.get("role") == "user"
                     and user.get("content") == turn.get("content") and assistant.get("role") == "assistant"
                     and assistant == turn.get("reply"), index, "persisted dialogue differs")
            request = row_requests.get(rid, {})
            _require(request.get("status") == "completed" and request.get("content") == turn.get("content"),
                     index, "completed message request differs")
            _require(reply_events[number].get("request_id") == rid, index, "reply audit request differs")
        submission = row.get("submission")
        _require(isinstance(submission, str) and bool(submission.strip()) and submission == plan.get("submission"),
                 index, "final submission differs")
        final_content = messages[-1].get("content", "")
        _require(isinstance(final_content, str) and final_content.count(FINAL_BEGIN) == 1
                 and final_content.count(FINAL_END) == 1, index, "final assistant translation delimiters differ")
        extracted = final_content.split(FINAL_BEGIN, 1)[1].split(FINAL_END, 1)[0].strip()
        _require(extracted == submission, index, "submission is not the recorded final assistant translation")
        for field in ("created_at", "ended_at"):
            if field in plan:
                _require(plan[field] == row.get(field), index, f"{field} differs")
        if "model_reply_events" in plan:
            _require(plan["model_reply_events"] == reply_events, index, "saved model reply events differ")
        if "actual_turns" in plan:
            _require(plan["actual_turns"] == len(rounds), index, "actual turn count differs")
        matches.append((row, plan))
    return matches


def _annotate(row: dict, method: str, batch_id: str | None, evidence: dict) -> dict:
    current = normalized_provenance(row)
    if row.get("provenance") is not None and current != row["provenance"]:
        raise AuditError("Existing provenance is malformed; refusing to replace it")
    old_method = current["collection_method"]
    if old_method not in {"unverified", method} or (old_method == method and current["batch_id"] != batch_id):
        raise AuditError("Existing source evidence conflicts; refusing to overwrite it")
    existing_evidence_types = {item.get("type") for item in current["evidence"] if isinstance(item.get("type"), str)}
    if ((method == "manual_attested" and "generator_manifest_verified" in existing_evidence_types)
            or (method == "automated_script" and "operator_attestation" in existing_evidence_types)):
        raise AuditError("Existing source evidence conflicts; refusing to overwrite it")
    for item in current["evidence"]:
        if item.get("evidence_id") == evidence["evidence_id"]:
            return deepcopy(row)
    result = deepcopy(row)
    current.update(version=PROVENANCE_VERSION, collection_method=method, batch_id=batch_id)
    current["evidence"].append(deepcopy(evidence))
    result["provenance"] = current
    return result


def plan_annotations(db: sqlite3.Connection, rows: dict[str, dict], *, manifest: dict | None = None,
                     manifest_sha256: str | None = None, manifest_name: str | None = None,
                     manual_ids: list[str] | None = None, attestation_note: str | None = None,
                     verified_at: str | None = None) -> dict[str, dict]:
    """Pure plan against a read snapshot; any mismatch prevents all updates."""
    result = deepcopy(rows)
    verified_at = verified_at or _now()
    if manifest is not None:
        if not isinstance(manifest_sha256, str) or len(manifest_sha256) != 64:
            raise AuditError("A manifest SHA256 is required")
        batch_id = "manifest-sha256:" + manifest_sha256
        for row, plan in verify_manifest(db, rows, manifest):
            evidence = {
                "type": "generator_manifest_verified", "manifest_sha256": manifest_sha256,
                "manifest_name": manifest_name, "generator_version": manifest["generator_version"],
                "sample_id": plan.get("sample_id"), "start_request_id": plan["start_payload"]["request_id"],
                "record_evidence_sha256": evidence_sha256(row), "verified_at": verified_at,
                "checks": ["session_id", "start_request_mapping", "start_payload_hash", "intake_answers",
                           "material_versions", "completed_request_dialogue", "assistant_final_submission"],
            }
            evidence["evidence_id"] = _sha({"type": evidence["type"], "session_id": row["id"],
                                            "manifest_sha256": manifest_sha256})
            result[row["id"]] = _annotate(result[row["id"]], "automated_script", batch_id, evidence)
    manual_ids = manual_ids or []
    if manual_ids and (not isinstance(attestation_note, str) or not attestation_note.strip()):
        raise AuditError("Manual attestation requires an explicit operator note")
    for sid in dict.fromkeys(manual_ids):
        if sid not in result:
            raise AuditError("A manual attestation session does not exist")
        if result[sid].get("record_kind") != "participant":
            raise AuditError("Manual participant attestation cannot replace a preview workflow")
        evidence = {"type": "operator_attestation", "note": attestation_note.strip(),
                    "attested_at": verified_at, "record_evidence_sha256": evidence_sha256(rows[sid]),
                    "scope": "Operator attests manual platform use; no change to data purpose or research eligibility."}
        evidence["evidence_id"] = _sha({"type": evidence["type"], "session_id": sid, "note": evidence["note"]})
        result[sid] = _annotate(result[sid], "manual_attested", None, evidence)
    if any(evidence_sha256(rows[sid]) != evidence_sha256(row) for sid, row in result.items()):
        raise AuditError("Original record evidence changed while planning annotations")
    return result


def _backup(database: Path, destination: Path) -> None:
    if destination.resolve() == database.resolve():
        raise AuditError("Backup must differ from the source database")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise AuditError("Backup already exists; it will not be overwritten") from exc
    os.close(descriptor)
    try:
        with _connect(database) as source, sqlite3.connect(destination) as target:
            source.backup(target)
        os.chmod(destination, 0o600)
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def run_audit(database: Path, *, manifest_path: Path | None = None, apply: bool = False,
              backup_path: Path | None = None, manual_ids: list[str] | None = None,
              attestation_note: str | None = None) -> dict:
    if apply and not (manifest_path or manual_ids):
        raise AuditError("--apply requires verified manifest annotations or explicit manual attestation")
    if attestation_note and not manual_ids:
        raise AuditError("An attestation note requires at least one manual session ID")
    manifest = manifest_sha = manifest_name = None
    if manifest_path is not None:
        raw = manifest_path.read_bytes()
        try:
            manifest = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise AuditError("Manifest is not valid JSON") from exc
        manifest_sha = hashlib.sha256(raw).hexdigest()
        manifest_name = manifest_path.name
    with _connect(database, writable=apply) as db:
        db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        before = _rows(db)
        after = plan_annotations(db, before, manifest=manifest, manifest_sha256=manifest_sha,
                                 manifest_name=manifest_name, manual_ids=manual_ids,
                                 attestation_note=attestation_note)
        changed = sorted(sid for sid in before if before[sid] != after[sid])
        backup = None
        if apply and changed:
            backup = backup_path or database.with_name(database.name + ".provenance-backup-" +
                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".sqlite3")
            _backup(database, backup)
            for sid in changed:
                db.execute("UPDATE translation_sessions SET record_json=? WHERE id=?", (_dump(after[sid]), sid))
            stored = _rows(db)
            if stored != after or any(evidence_sha256(stored[sid]) != evidence_sha256(before[sid]) for sid in before):
                raise AuditError("Stored evidence invariant failed; transaction rolled back")
        report = {"mode": "apply" if apply else "dry_run", "verified_at": _now(),
                  "manifest_sha256": manifest_sha, "matched_manifest_records": len(manifest["samples"]) if manifest else 0,
                  "changed_session_ids": changed, "changed_records": len(changed),
                  "backup_path": str(backup) if backup else None,
                  "original_evidence_unchanged": True,
                  "before": audit_records(before.values()), "after": audit_records(after.values())}
        if apply:
            db.commit()
        else:
            db.rollback()
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--attest-manual-id", action="append", default=[])
    parser.add_argument("--attestation-note")
    parser.add_argument("--apply", action="store_true", help="Apply all verified annotations atomically after a private SQLite backup")
    parser.add_argument("--backup", type=Path, help="New backup path; existing files are never replaced")
    args = parser.parse_args(argv)
    try:
        report = run_audit(args.database, manifest_path=args.manifest, apply=args.apply,
                           backup_path=args.backup, manual_ids=args.attest_manual_id,
                           attestation_note=args.attestation_note)
    except Exception as exc:
        # Generic system/format errors may contain raw file data or filesystem
        # details.  Only our deliberately safe AuditError descriptions are shown.
        message = str(exc) if isinstance(exc, AuditError) else "Audit failed; no annotation transaction was committed"
        print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
