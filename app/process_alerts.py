"""Read-only process notes derived from saved observations.

These notes describe response patterns and task records. They never determine
analysis eligibility, and they do not diagnose invalid participation. In
particular, a missing evidence-reveal event is not evidence that a real-model
answer supplied no useful information.
"""
from __future__ import annotations

import json
from datetime import datetime

from .student_materials import AGE_BANDS, FORM_VERSION, form_for


QUALITY_VERSION = "observed-student-process-v1"
IDENTICAL_RUN_THRESHOLD = 12
FAST_TASK_SECONDS = 45


def _decoded(value, expected_type, fallback):
    if isinstance(value, expected_type):
        return value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return fallback
        if isinstance(value, expected_type):
            return value
    return fallback


def _elapsed(record):
    try:
        start = datetime.fromisoformat(record["created_at"])
        end = datetime.fromisoformat(record["ended_at"])
        if start.utcoffset() is None or end.utcoffset() is None:
            return None
        seconds = (end - start).total_seconds()
        return seconds if seconds >= 0 else None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def derive_process_alerts(record: dict, messages: list[dict]) -> list[dict]:
    """Return auditable notes without changing the record or its source data.

    Only the exact current student instrument is interpreted for pretest notes.
    Task timing uses saved, timezone-aware task timestamps. The synthetic
    generator's pretest duration is deliberately never read.

    The caller may merge this list with preserved historical/manual details by
    ``code``. A note absent here should not erase an existing stored annotation.
    """
    record = dict(record)
    simulated = int(record.get("is_synthetic") == 1
                    or record.get("timestamp_is_simulated") == 1
                    or record.get("record_kind") == "synthetic"
                    or record.get("data_origin") == "synthetic")
    details = []

    def add(code, message, rule, operator, threshold, observed, fields, *, severity="review", **extra):
        details.append({"code": code, "message": message, "rule": rule,
                        "operator": operator, "threshold": threshold,
                        "observed_value": observed, "source_fields": fields,
                        "is_simulated": simulated, "severity": severity,
                        "affects_analysis": False, "quality_version": QUALITY_VERSION, **extra})

    if (record.get("literacy_instrument_version") == FORM_VERSION
            and record.get("age_band") in AGE_BANDS):
        _, items = form_for(record["age_band"])
        answers = _decoded(record.get("literacy_answers_json"), dict, None)
        if (answers is not None and set(answers) == {item["id"] for item in items}
                and all(type(answers[item["id"]]) is int
                        and 0 <= answers[item["id"]] < len(item["options"]) for item in items)):
            uncertain = [item["id"] for item in items
                         if item["options"][answers[item["id"]]] == "我暂时不会选"]
            if uncertain:
                add("pretest_uncertain_answers", f"前测有{len(uncertain)}题选择“我暂时不会选”。",
                    "saved_uncertain_answer_count>0", ">", 0, len(uncertain),
                    ["literacy_answers_json", "literacy_instrument_version"], severity="info",
                    item_ids=uncertain)
            longest = run = 0
            previous = None
            run_start = longest_start = longest_end = 0
            for index, item in enumerate(items):
                position = answers[item["id"]]
                if position == previous:
                    run += 1
                else:
                    run, run_start = 1, index
                if run > longest:
                    longest, longest_start, longest_end = run, run_start, index
                previous = position
            if longest >= IDENTICAL_RUN_THRESHOLD:
                add("pretest_long_identical_run",
                    f"前测最长连续选择同一位置为{longest}题（阈值{IDENTICAL_RUN_THRESHOLD}题），仅供复核。",
                    "longest_identical_answer_position_run>=12", ">=", IDENTICAL_RUN_THRESHOLD,
                    longest, ["literacy_answers_json", "literacy_instrument_version"],
                    run_start_item=items[longest_start]["id"], run_end_item=items[longest_end]["id"])

    users = [message["content"].strip() for message in messages
             if message.get("role") == "user" and isinstance(message.get("content"), str)]
    submission = record.get("submission")
    submission = submission.strip() if isinstance(submission, str) else ""
    combined = " ".join([*users, submission])
    # Do not judge a just-created or unfinished session for not having chatted
    # or submitted yet. Existing technical flags can still be reported below.
    if record.get("ended_at"):
        if not users:
            add("no_user_message", "已提交记录未保存用户提问。", "saved_user_message_count=0", "=", 0, 0,
                ["messages.role", "ended_at"])
        if not submission and not record.get("decision_choice_id"):
            add("empty_submission", "已提交记录未保存最终答案或决策选项。",
                "empty_submission_and_no_choice", "=", 0, 0,
                ["submission", "decision_choice_id", "ended_at"])
        characters = len("".join(users).strip())
        if characters < 12:
            add("very_short_dialogue", f"保存的用户提问合计{characters}字，低于12字。",
                "saved_user_message_characters<12", "<", 12, characters, ["messages.content"])
        elapsed = _elapsed(record)
        if elapsed is not None and elapsed < FAST_TASK_SECONDS:
            add("very_fast_completion", f"任务阶段经过{elapsed:g}秒，低于{FAST_TASK_SECONDS}秒。",
                "saved_task_elapsed_seconds<45", "<", FAST_TASK_SECONDS, elapsed,
                ["created_at", "ended_at"])
        if len(users) >= 2 and len(set(users)) == 1:
            add("repeated_user_messages", f"保存的{len(users)}条用户提问内容完全相同。",
                "user_count>=2_and_distinct_user_messages=1", ">=", 2, len(users), ["messages.content"])
        distinct = len(set(combined.replace(" ", "")))
        if len(combined) >= 10 and distinct <= 4:
            add("low_character_diversity", f"保存的对话及提交仅含{distinct}种不同字符。",
                "combined_length>=10_and_distinct_characters<=4", "<=", 4, distinct,
                ["messages.content", "submission"])
        snapshot = _decoded(record.get("task_snapshot_json"), dict, {})
        keywords = snapshot.get("keywords")
        if (isinstance(keywords, list) and keywords
                and all(isinstance(word, str) and word for word in keywords)
                and not any(word in combined for word in keywords)):
            add("low_keyword_overlap", "保存的对话及提交未出现任务关键词，需结合上下文复核。",
                "nonempty_task_keywords_and_no_keyword_match", "=", 0, 0,
                ["messages.content", "submission", "task_snapshot_json.keywords"])

    stored_flags = _decoded(record.get("flags_json"), list, [])
    for code, message in (("technical_interruption", "已保存技术中断标记。"),
                          ("model_fallback_used", "已保存模型回退标记。")):
        if code in stored_flags:
            add(code, message, "stored_flag_present", "=", 1, 1, ["flags_json"])
    return details
