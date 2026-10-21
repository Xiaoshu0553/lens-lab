"""Audited process alerts from saved answers and existing task records only.

This migration never uses the unobserved pretest start time or the generator's
synthetic pretest duration. It changes neither sample membership nor outcomes.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime

from .age_forms import form_for, score_form
from . import showcase_rendering as rendering
from .synthetic_exports import BATCH_ID


QUALITY_VERSION = "only_observed_answer_and_task_features-v1"
DETAIL_COLUMN = "process_alert_details_json"
IMMUTABLE_SOURCE_FIELDS = (
    "id", "task_id", "group_id", "interaction_condition", "decision_choice_id", "execution_choice_id",
    "decision_feasible", "decision_optimal", "score", "literacy_score", "literacy_percent", "anchor_score",
    "age_band", "education_stage", "topic_familiarity", "literacy_instrument_version", "scenario_id",
    "data_origin", "dataset_variant", "sim_literacy_group", "sim_response_profile",
    "synthetic_pretest_time_seconds", "sim_qc_flag", "timestamp_is_simulated",
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def quality_receipt_exists(db):
    table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='showcase_quality_migrations'").fetchone()
    return bool(table and db.execute("SELECT 1 FROM showcase_quality_migrations WHERE quality_version=?", (QUALITY_VERSION,)).fetchone())


def _longest_run(age_band, answers):
    _version, items = form_for(age_band)
    longest = run = 0
    previous = None
    start = longest_start = longest_end = None
    for index, item in enumerate(items):
        position = answers[item["id"]]
        if position == previous:
            run += 1
        else:
            run, start = 1, index
        if run > longest:
            longest, longest_start, longest_end = run, start, index
        previous = position
    return longest, items[longest_start]["id"], items[longest_end]["id"]


def observed_alerts(source, snapshot, answers, messages, submission, evidence_card_ids, strategy):
    """Return flags and explanations; simulated pretest timing is excluded."""
    observed_source = {key: value for key, value in source.items() if key != "synthetic_pretest_time_seconds"}
    flags = [flag for flag in rendering.derived_fixture_flags(
        observed_source, snapshot, answers, messages=messages, submission=submission, strategy=strategy)
        if flag != "pretest_fast_completion"]
    users = [item[2].strip() for item in messages if item[1] == "user"]
    combined = " ".join(users + [submission.strip()])
    row = rendering.adapted_row(source)
    longest, first, last = _longest_run(source["age_band"], answers)
    elapsed = ((datetime.fromisoformat(row["ended_at"]) - datetime.fromisoformat(row["created_at"])).total_seconds()
               if row.get("ended_at") else None)
    metrics = {
        "no_user_message": ("保存的用户消息为0条。", "user_message_count=0", "=", 0, len(users), ["messages.role"]),
        "empty_submission": ("未保存最终答案或决策选项。", "empty_submission_and_no_choice", "=", 0, len(submission.strip()), ["submission", "decision_choice_id"]),
        "no_evidence_retrieved": ("已提交方案，但保存的资料揭示记录为0条。", "submitted_and_distinct_evidence_cards=0", "=", 0, len(set(evidence_card_ids)), ["model_events.evidence_reveal"]),
        "very_short_dialogue": (f"保存的用户消息合计{len(''.join(users))}字，低于12字。", "user_message_characters<12", "<", 12, len("".join(users)), ["messages.content"]),
        "very_fast_completion": (f"任务阶段总经过时间为{elapsed:g}秒，低于45秒。" if elapsed is not None else "任务阶段时点待核对。", "task_elapsed_seconds<45", "<", 45, elapsed, ["created_at", "ended_at"]),
        "repeated_user_messages": (f"保存的{len(users)}条用户消息内容完全相同。", "user_count>=2_and_distinct_user_messages=1", ">=", 2, len(users), ["messages.content"]),
        "low_character_diversity": (f"对话及最终提交的不同字符仅{len(set(combined.replace(' ', '')))}种。", "combined_length>=10_and_distinct_characters<=4", "<=", 4, len(set(combined.replace(" ", ""))), ["messages.content", "submission"]),
        "low_keyword_overlap": ("保存的对话及最终提交未出现任务关键词，需结合上下文复核。", "nonempty_task_keywords_and_no_keyword_match", "=", 0, 0, ["messages.content", "submission", "task_snapshot_json.keywords"]),
        "pretest_long_identical_run": (f"30题答案中最长连续选择同一位置为{longest}题（阈值12题），仅供复核。", "longest_identical_answer_position_run>=12", ">=", 12, longest, ["literacy_answers_json"]),
    }
    details = []
    for code in flags:
        if code not in metrics:
            raise ValueError("过程提醒存在无可复算依据的规则")
        message, rule, operator, threshold, observed, fields = metrics[code]
        detail = {"code": code, "message": message, "rule": rule, "operator": operator,
                  "threshold": threshold, "observed_value": observed, "source_fields": fields,
                  "is_simulated": 1, "quality_version": QUALITY_VERSION}
        if code == "pretest_long_identical_run":
            detail.update(run_start_item=first, run_end_item=last)
        details.append(detail)
    return flags, details


def _record_plan(db, row, source, answers, snapshot, *, base_pretest_qc):
    sid = row["id"]
    if (json.loads(row["synthetic_source_json"] or "null") != source
            or any(row[key] != source[key] for key in IMMUTABLE_SOURCE_FIELDS)
            or row["record_kind"] != "synthetic" or row["fixture_conversation"] != 1
            or row["source_batch_id"] != BATCH_ID or row["is_synthetic"] != 1
            or row["run_mode"] != "demo" or row["is_demo"] != 1 or row["platform_analysis_eligible"] != 0
            or row["fixture_render_version"] != rendering.RENDER_VERSION
            or row["fixture_time_shift_days"] != rendering.TIME_SHIFT_DAYS
            or json.loads(row["literacy_answers_json"]) != answers
            or json.loads(row["task_snapshot_json"]) != snapshot):
        raise ValueError(f"{sid}原始数据、答案或冻结资料异常；过程提醒迁移已拒绝")
    raw, percent, band, anchor = score_form(row["age_band"], answers, row["literacy_band_width"])
    if (raw, percent, band, anchor) != (row["literacy_score"], row["literacy_percent"], row["literacy_band"], row["anchor_score"]):
        raise ValueError(f"{sid}问卷无法复算；过程提醒迁移已拒绝")
    adapted = rendering.adapted_row(source)
    if any(row[key] != adapted[key] for key in ("created_at", "decision_started_at", "ended_at")):
        raise ValueError(f"{sid}任务时点异常；过程提醒迁移已拒绝")
    selected = next(option for option in snapshot["decision_options"] if option["id"] == source["decision_choice_id"])
    execution = next(option for option in snapshot["execution_options"] if option["id"] == source["execution_choice_id"])
    if row["submission"] != f"参加方案：{selected['text']}\n办理方案：{execution['text']}":
        raise ValueError(f"{sid}最终提交异常；过程提醒迁移已拒绝")
    messages = [tuple(item) for item in db.execute(
        "SELECT session_id,role,content,action,created_at FROM messages WHERE session_id=? ORDER BY id", (sid,))]
    events = [tuple(item) for item in db.execute(
        "SELECT session_id,event_type,detail_json,created_at FROM model_events WHERE session_id=? ORDER BY id", (sid,))]
    expected_messages, expected_events = rendering.fixture_payloads(source, snapshot, row["clarification_strategy"])
    if (messages != expected_messages or events != expected_events
            or any(db.execute(f"SELECT 1 FROM {table} WHERE session_id=? LIMIT 1", (sid,)).fetchone()
                   for table in ("start_requests", "message_requests"))):
        raise ValueError(f"{sid}对话或事件异常；过程提醒迁移已拒绝")
    history = [dict(item) for item in db.execute(
        "SELECT session_id,status,reason,reviewer,created_at FROM review_history WHERE session_id=? ORDER BY id", (sid,))]
    expected_history = [{"session_id": sid, "status": "valid", "reason": "showcase_fixture",
                         "reviewer": "demo-fixture", "created_at": adapted["ended_at"]}]
    review = (row["review_status"], row["reviewer"], row["reviewed_at"], row["review_reason"], row["review_code"], row["feedback_json"])
    if (review != ("valid", "demo-fixture", adapted["ended_at"], "", None, "{}") or history != expected_history
            or row["score_note"] != "机器评分 decision-age-v4"):
        return {"id": sid, "skip_reason": "manual_review_or_feedback_preserved"}
    baseline = rendering.derived_fixture_flags(source, snapshot, answers, messages=messages,
                                               submission=row["submission"] or "", strategy=row["clarification_strategy"])
    if not base_pretest_qc:
        baseline = [flag for flag in baseline if not flag.startswith("pretest_")]
    before = json.loads(row["flags_json"])
    previous_details = json.loads(row[DETAIL_COLUMN] or "[]")
    if (not isinstance(before, list) or any(not isinstance(flag, str) for flag in before)
            or not isinstance(previous_details, list) or any(not isinstance(detail, dict) for detail in previous_details)):
        raise ValueError(f"{sid}过程提醒格式异常；迁移已拒绝")
    if before != baseline or previous_details:
        return {"id": sid, "skip_reason": "edited_process_alerts_preserved"}
    evidence = [json.loads(item[2])["card_id"] for item in events if item[1] == "evidence_reveal"]
    after, details = observed_alerts(source, snapshot, answers, messages, row["submission"] or "", evidence,
                                    row["clarification_strategy"])
    if any(flag not in after for flag in before):
        raise ValueError(f"{sid}已有过程提醒无法保留；迁移已拒绝")
    return {"id": sid, "flags_before": before, "flags_after": after,
            "details_before": previous_details, "details_after": details}


def _apply_process_alert_migration(db, *, selection_id, expected_sources=None, expected_answers=None, expected_snapshots=None):
    """Run in the caller's transaction; completed receipts make restarts no-op."""
    if quality_receipt_exists(db):
        return
    receipt = db.execute("SELECT manifest_json FROM showcase_selections WHERE selection_id=?", (selection_id,)).fetchone()
    if receipt is None or not all(isinstance(value, dict) for value in (expected_sources, expected_answers, expected_snapshots)):
        raise ValueError("过程提醒迁移需要已验证的选样凭据、原始source及完整答案")
    selection = json.loads(receipt["manifest_json"])
    ids = set(selection["selected_ids"])
    rows = list(db.execute("SELECT * FROM sessions WHERE source_batch_id=? AND record_kind='synthetic' AND fixture_conversation=1 ORDER BY id", (BATCH_ID,)))
    if len(ids) != 126 or {row["id"] for row in rows} != ids:
        raise ValueError("当前126条fixture与选样凭据不一致，不能更新过程提醒")
    columns = {column["name"] for column in db.execute("PRAGMA table_info(sessions)")}
    if DETAIL_COLUMN not in columns:
        db.execute(f"ALTER TABLE sessions ADD COLUMN {DETAIL_COLUMN} TEXT NOT NULL DEFAULT '[]'")
        rows = list(db.execute("SELECT * FROM sessions WHERE source_batch_id=? AND record_kind='synthetic' AND fixture_conversation=1 ORDER BY id", (BATCH_ID,)))
    preserved = [{key: value for key, value in dict(row).items() if key not in ("flags_json", DETAIL_COLUMN)} for row in rows]
    before_preservation = _digest(preserved)
    plans = []
    for row in rows:
        sid = row["id"]
        if sid not in expected_sources or sid not in expected_answers or sid not in expected_snapshots:
            raise ValueError("过程提醒缺少可复算的source或答案")
        plans.append(_record_plan(db, row, expected_sources[sid], expected_answers[sid], expected_snapshots[sid],
                                  base_pretest_qc=bool(selection.get("derived_qc_enabled"))))
    skipped = [plan for plan in plans if "skip_reason" in plan]
    eligible = [plan for plan in plans if "skip_reason" not in plan]
    changes = [plan for plan in eligible if plan["flags_before"] != plan["flags_after"] or plan["details_before"] != plan["details_after"]]
    counts = Counter(code for plan in eligible for code in plan["flags_after"])
    manifest = {"quality_version": QUALITY_VERSION, "selection_id": selection_id, "source_batch_id": BATCH_ID,
                "policy": QUALITY_VERSION, "render_version": rendering.RENDER_VERSION,
                "used_features": ["saved_30_item_answer_vector", "saved_task_messages", "saved_evidence_events", "saved_task_timestamps"],
                "unused_features": ["unobserved_pretest_start_time", "synthetic_pretest_time_seconds", "sim_response_profile"],
                "pretest_timing_used": False, "sample_membership_changed": False,
                "scores_answers_choices_dialogue_timestamps_changed": False,
                "selected_count": len(rows), "evaluated_unedited_count": len(eligible),
                "updated_record_count": len(changes), "flags_changed_record_count": sum(plan["flags_before"] != plan["flags_after"] for plan in changes),
                "alert_record_count": sum(bool(plan["flags_after"]) for plan in eligible),
                "unflagged_record_count": sum(not plan["flags_after"] for plan in eligible),
                "flag_counts": dict(counts), "skipped_edited_records": skipped,
                "before_after": changes, "preserved_record_fields_sha256": before_preservation}
    db.execute("""CREATE TABLE IF NOT EXISTS showcase_quality_migrations (
        quality_version TEXT PRIMARY KEY, selection_id TEXT NOT NULL, batch_id TEXT NOT NULL, manifest_json TEXT NOT NULL)""")
    db.execute("INSERT INTO showcase_quality_migrations VALUES (?,?,?,?)", (QUALITY_VERSION, selection_id, BATCH_ID, _json(manifest)))
    for plan in changes:
        db.execute(f"UPDATE sessions SET flags_json=?,{DETAIL_COLUMN}=? WHERE id=? AND source_batch_id=? AND fixture_conversation=1 AND record_kind='synthetic'",
                   (_json(plan["flags_after"]), _json(plan["details_after"]), plan["id"], BATCH_ID))
    after = list(db.execute("SELECT * FROM sessions WHERE source_batch_id=? AND record_kind='synthetic' AND fixture_conversation=1 ORDER BY id", (BATCH_ID,)))
    after_preserved = [{key: value for key, value in dict(row).items() if key not in ("flags_json", DETAIL_COLUMN)} for row in after]
    if _digest(after_preserved) != before_preservation:
        raise ValueError("过程提醒迁移改变了保护字段，必须回滚")


def apply_process_alert_migration(db, **options):
    """Use a savepoint so schema, receipt and all alert updates stay atomic."""
    db.execute("SAVEPOINT observed_process_alerts")
    try:
        _apply_process_alert_migration(db, **options)
    except Exception:
        db.execute("ROLLBACK TO observed_process_alerts")
        db.execute("RELEASE observed_process_alerts")
        raise
    else:
        db.execute("RELEASE observed_process_alerts")
