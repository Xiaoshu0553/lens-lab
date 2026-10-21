"""Explicit download schemas and one neutral record index for every report.

The internal session IDs, source snapshots and provenance remain in the database
and authenticated archive export. Standard downloads project only these columns.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone

from .literacy import ITEMS


QUESTION_IDS = tuple(item["id"] for item in ITEMS)
RECORD_COLUMNS = (
    ("record_id", "记录编号"),
    ("age_band", "年龄区间"), ("education_stage", "教育阶段"), ("topic_familiarity", "信息核对熟悉度(1-5)"),
    ("group_id", "实验组(S标准/A澄清)"), ("interaction_condition", "交互条件"),
    ("clarification_strategy", "澄清策略"), ("task_title", "任务名称"), ("scenario_id", "任务情境"),
    ("questionnaire_version", "问卷版本"), ("literacy_score", "前测答对题数(0-30)"),
    ("literacy_percent", "前测得分(%)"), ("literacy_band", "素养得分区间"),
    ("literacy_group", "素养分组"), ("anchor_score", "公共锚题答对数(0-18)"),
) + tuple((f"pretest_{key}_choice", f"前测{key}选项(1-4)") for key in QUESTION_IDS) + (
    ("completion_status", "完成状态"), ("analysis_eligible", "纳入当前分析"),
    ("analysis_exclusion_reasons", "未纳入原因"), ("decision_choice", "参加方案"),
    ("execution_choice", "办理方案"), ("decision_quality_score", "决策质量分(0-100)"),
    ("decision_feasible", "方案可行(1是/0否)"), ("decision_optimal", "达到最优目标(1是/0否)"),
    ("submission", "补充说明"), ("user_turns", "参与者提问轮数"),
    ("ai_prompt_count", "AI主动提示次数"), ("ai_clarification_count", "AI澄清次数"),
    ("ai_guidance_count", "AI任务引导次数"), ("evidence_reveals", "资料线索次数"),
    ("distinct_evidence_cards", "不同资料线索数"), ("process_alert_details", "过程提醒"),
    ("task_completion_seconds", "任务总耗时(秒)"), ("dialogue_elapsed_seconds", "对话阶段耗时(秒)"),
    ("decision_elapsed_seconds", "决策阶段耗时(秒)"), ("mean_reply_seconds", "AI平均回复耗时(秒)"),
    ("created_at", "任务开始(UTC)"), ("decision_started_at", "进入决策(UTC)"), ("ended_at", "提交时间(UTC)"),
    ("created_at_beijing", "任务开始(北京时间)"), ("decision_started_at_beijing", "进入决策(北京时间)"),
    ("ended_at_beijing", "提交时间(北京时间)"),
)
DIALOGUE_METADATA_KEYS = (
    "record_id", "age_band", "education_stage", "group_id", "interaction_condition", "clarification_strategy",
    "task_title", "scenario_id", "literacy_score", "literacy_percent", "literacy_band",
    "ai_prompt_count", "ai_clarification_count", "ai_guidance_count",
)
DIALOGUE_COLUMNS = tuple(column for column in RECORD_COLUMNS if column[0] in DIALOGUE_METADATA_KEYS) + (
    ("has_messages", "有对话(1是/0否)"), ("message_index", "消息序号"), ("role", "发言者"),
    ("content", "对话内容"), ("action", "消息类型"), ("created_at", "消息时间(UTC)"),
    ("created_at_beijing", "消息时间(北京时间)"),
)


def assign_record_alias(db, session_id):
    """Append once inside the session-creation transaction; never reuse a number."""
    existing = db.execute("SELECT alias_number FROM record_aliases WHERE session_id=?", (session_id,)).fetchone()
    if existing is not None:
        return f"R{existing['alias_number']:04d}"
    if db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone() is None:
        raise ValueError("Cannot assign a record alias to a missing session")
    cursor = db.execute("INSERT INTO record_aliases(session_id) VALUES (?)", (session_id,))
    return f"R{cursor.lastrowid:04d}"


def initialize_record_aliases(db):
    """Backfill once at startup, after seeding, inside BEGIN IMMEDIATE.

    Existing aliases survive sorting changes. Deletion removes the mapping,
    while AUTOINCREMENT preserves its high-water mark and prevents number reuse.
    """
    db.execute("""CREATE TABLE IF NOT EXISTS record_aliases (
        alias_number INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id) ON DELETE CASCADE)""")
    rows = db.execute("""SELECT s.id FROM sessions s
        WHERE NOT EXISTS (SELECT 1 FROM record_aliases a WHERE a.session_id=s.id)
        ORDER BY julianday(s.ended_at) DESC,s.id ASC""").fetchall()
    for row in rows:
        assign_record_alias(db, row["id"])


def record_id_map(db):
    """Read the persistent full-collection mapping without any writes."""
    missing = db.execute("""SELECT s.id FROM sessions s WHERE NOT EXISTS
        (SELECT 1 FROM record_aliases a WHERE a.session_id=s.id) LIMIT 1""").fetchone()
    if missing is not None:
        raise RuntimeError("Record alias is missing; initialize aliases before serving reports")
    rows = db.execute("""SELECT a.session_id,a.alias_number FROM record_aliases a
        JOIN sessions s ON s.id=a.session_id ORDER BY a.alias_number""")
    return {row["session_id"]: f"R{row['alias_number']:04d}" for row in rows}


def order_by_record_id(records):
    return sorted(records, key=lambda row: int(row["record_id"][1:]))


def parsed_json(value, fallback):
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def utc_timestamp(value):
    """Match explicitly labelled UTC columns without altering stored audit text."""
    try:
        date = datetime.fromisoformat(value) if value else None
        return date.astimezone(timezone.utc).isoformat(timespec="seconds") if date and date.tzinfo else None
    except (TypeError, ValueError):
        return None


LEGACY_PROCESS_ALERT_MESSAGES = {
    "no_user_message": "没有提问", "empty_submission": "未提交答案", "very_short_dialogue": "对话极短",
    "very_fast_completion": "任务完成较快", "repeated_user_messages": "重复消息",
    "low_character_diversity": "字符重复过多", "low_keyword_overlap": "关键词重合少",
    "no_evidence_retrieved": "没有取得资料线索", "technical_interruption": "技术中断",
    "model_fallback_used": "使用了模型回退回复",
}


def process_alert_details(record):
    """Keep detailed evidence; support ordinary submissions storing legacy flags.

    Only the pre-existing task/process flags receive display labels. Do not infer
    questionnaire timing, thresholds or observations from simulated source data.
    """
    details = parsed_json(record.get("process_alert_details_json"), [])
    if isinstance(details, list) and details:
        return details
    flags = parsed_json(record.get("flags_json"), [])
    if not isinstance(flags, list):
        return []
    return [{"code": code, "message": LEGACY_PROCESS_ALERT_MESSAGES[code]}
            for code in dict.fromkeys(flag for flag in flags if isinstance(flag, str))
            if code in LEGACY_PROCESS_ALERT_MESSAGES]


def clean_record(record):
    """Project one prepared archive record; never copy arbitrary source fields."""
    snapshot = parsed_json(record.get("task_snapshot_json"), {})
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    answers = parsed_json(record.get("literacy_answers_json"), {})
    answers = answers if isinstance(answers, dict) else {}
    selected = next((item for item in snapshot.get("decision_options", [])
                     if item.get("id") == record.get("decision_choice_id")), None)
    execution = next((item for item in snapshot.get("execution_options", [])
                      if item.get("id") == record.get("execution_choice_id")), None)
    score = record.get("literacy_score")
    level = ("高" if score >= 24 else "中" if score >= 15 else "低") if type(score) is int and 0 <= score <= 30 else None
    values = {key: record.get(key) for key, _label in RECORD_COLUMNS}
    values.update({
        "task_title": snapshot.get("title"), "questionnaire_version": record.get("literacy_instrument_version"),
        "literacy_group": level, "completion_status": "completed" if record.get("ended_at") else "incomplete",
        "analysis_exclusion_reasons": parsed_json(record.get("analysis_exclusion_reasons_json"), []),
        "decision_choice": selected.get("text") if selected else None,
        "execution_choice": execution.get("text") if execution else None,
        "process_alert_details": [{"code": str(item.get("code", "")), "message": str(item.get("message", ""))}
                                  for item in record.get("process_alert_details", process_alert_details(record))
                                  if isinstance(item, dict) and item.get("message")],
    })
    for key in QUESTION_IDS:
        position = answers.get(key)
        values[f"pretest_{key}_choice"] = position + 1 if type(position) is int and 0 <= position < 4 else None
    for key in ("created_at", "decision_started_at", "ended_at"):
        values[key] = utc_timestamp(record.get(key))
    return {key: values[key] for key, _label in RECORD_COLUMNS}


DISPLAY_VALUES = {
    "education_stage": {"primary": "小学", "junior": "初中", "senior": "高中或中职", "college": "专科",
                        "bachelor": "本科", "graduate": "研究生及以上", "other": "其他或不便说明"},
    "interaction_condition": {"standard": "标准交互", "adaptive": "澄清交互"},
    "clarification_strategy": {"module": "澄清模块", "prompt": "澄清提示词"},
    "completion_status": {"completed": "已完成", "incomplete": "未完成"},
    "role": {"user": "参与者", "assistant": "AI", "system": "系统"},
    "action": {"user_message": "参与者提问", "fixture_user_message": "参与者提问", "evidence": "资料查询",
               "module_clarification": "AI主动澄清(模块)", "prompt_clarification": "AI主动澄清(提示词)",
               "task_guidance": "AI任务引导", "guidance": "一般答复", "answer": "一般答复"},
}


def _csv_value(key, value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return int(value)
    if key == "process_alert_details":
        return "；".join(item["message"] for item in value)
    if isinstance(value, list):
        return "；".join(str(item) for item in value)
    return DISPLAY_VALUES.get(key, {}).get(value, value)


def chinese_csv(records, columns):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(label for _key, label in columns)
    for record in records:
        values = []
        for key, _label in columns:
            value = _csv_value(key, record.get(key))
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                value = "'" + value
            values.append(value)
        writer.writerow(values)
    return "\ufeff" + buffer.getvalue()
