"""Derived administrator fields and shared automatic analysis eligibility."""
import json
import math
from datetime import datetime, timedelta, timezone


BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")


def ai_prompt_counts(messages) -> dict[str, int]:
    """Count saved assistant actions, never infer prompts from group or text.

    Evidence retrieval and ordinary answers are not proactive guidance. Keep the
    two clarification actions together and task_guidance separate for auditing.
    """
    clarifications = guidance = 0
    for message in messages:
        if message["role"] != "assistant":
            continue
        if message["action"] in {"module_clarification", "prompt_clarification"}:
            clarifications += 1
        elif message["action"] == "task_guidance":
            guidance += 1
    return {"ai_prompt_count": clarifications + guidance,
            "ai_clarification_count": clarifications, "ai_guidance_count": guidance}


def ai_prompt_totals(counts_by_session) -> dict[str, int]:
    """Aggregate the same per-record counts for an explicitly selected cohort."""
    totals = {"ai_prompt_count": 0, "ai_clarification_count": 0, "ai_guidance_count": 0,
              "records_with_ai_prompt": 0, "record_count": 0}
    for counts in counts_by_session:
        totals["record_count"] += 1
        totals["records_with_ai_prompt"] += int(counts["ai_prompt_count"] > 0)
        for key in ("ai_prompt_count", "ai_clarification_count", "ai_guidance_count"):
            totals[key] += counts[key]
    return totals


def record_times(row) -> dict:
    parsed = {}
    result = {"display_timezone": "Asia/Shanghai", "timestamp_storage_timezone": "UTC",
              "pretest_time_seconds": None,
              "elapsed_time_scope": "task_after_pretest_including_wait_and_idle"}
    issues = []
    for key in ("created_at", "decision_started_at", "ended_at", "reviewed_at"):
        value = row[key] if key in row.keys() else None
        try:
            date = datetime.fromisoformat(value) if value else None
            if date is not None and date.tzinfo is None:
                raise ValueError("Timezone is unknown")
        except (ValueError, TypeError):
            date = None
            issues.append(key + ":invalid_or_unknown_timezone")
        parsed[key] = date
        result[key + "_beijing"] = date.astimezone(BEIJING).isoformat(timespec="seconds") if date else None

    def elapsed(start, end):
        if parsed[start] is None or parsed[end] is None:
            return None
        seconds = (parsed[end] - parsed[start]).total_seconds()
        if seconds < 0:
            issues.append(start + ":after:" + end)
            return None
        return seconds

    result["task_completion_seconds"] = elapsed("created_at", "ended_at")
    result["dialogue_elapsed_seconds"] = elapsed("created_at", "decision_started_at")
    result["decision_elapsed_seconds"] = elapsed("decision_started_at", "ended_at")
    result["timing_issues"] = issues
    return result


def cohort_exclusion_reasons(row, *, settings, design_version, strategy,
                             model_config_id, active_task_ids, form_versions, prompts,
                             record_kind="participant") -> list[str]:
    reasons = []
    comparisons = (
        (row["record_kind"] == record_kind, "非参与者实验记录"),
        (row["run_mode"] == settings.study_mode, "运行模式已变化"),
        (bool(row["is_demo"]) == (settings.study_mode != "live"), "演示来源与运行模式不匹配"),
        (row["design_version"] == design_version, "实验设计版本已更新"),
        (row["clarification_strategy"] == strategy, "澄清策略已更新"),
        (row["literacy_instrument_version"] == form_versions.get(row["age_band"]), "问卷版本已更新"),
        (row["literacy_band_width"] == settings.literacy_band_width, "信息素养分层区间已变化"),
        (row["model_config_id"] == model_config_id, "模型配置已更新"),
        (row["task_id"] == active_task_ids.get(row["age_band"]), "任务已停用或不属于当前任务"),
        (row["prompt_version"] == prompts.get(row["interaction_condition"]), "提示词版本已更新或与实验组不匹配"),
        ({"S": "standard", "A": "adaptive"}.get(row["group_id"]) == row["interaction_condition"], "实验组与交互条件不匹配"),
    )
    for matches, reason in comparisons:
        if not matches:
            reasons.append(reason)
    return reasons


def automatic_analysis_reasons(row, items) -> list[str]:
    """Objective completion checks; historical manual reviews never gate data.

    The caller additionally applies cohort_exclusion_reasons for current source
    and instrument configuration. A zero decision score remains eligible.
    """
    reasons = []
    if not row["ended_at"]:
        reasons.append("任务尚未提交")
    score = row["score"]
    if type(score) not in {int, float} or not math.isfinite(score) or not 0 <= score <= 100:
        reasons.append("缺少有效的自动任务评分")
    try:
        snapshot = json.loads(row["task_snapshot_json"])
        selected = next((option for option in snapshot.get("decision_options", [])
                         if option["id"] == row["decision_choice_id"]), None)
        execution_options = snapshot.get("execution_options", [])
        execution = next((option for option in execution_options
                          if option["id"] == row["execution_choice_id"]), None)
        if selected is None or (execution_options and execution is None):
            raise ValueError("No frozen objective choices")
        calculated = selected["score"] + (execution["score"] if execution else 0)
        if type(calculated) not in {int, float} or not math.isfinite(calculated) or calculated != score:
            raise ValueError("Objective score mismatch")
    except (TypeError, ValueError, KeyError, AttributeError):
        reasons.append("未保存可复算的客观任务选项与自动评分")
    try:
        answers = json.loads(row["literacy_answers_json"])
        if (len(items) != 30 or not isinstance(answers, dict) or set(answers) != {item["id"] for item in items}
                or any(type(answers[item["id"]]) is not int or not 0 <= answers[item["id"]] < len(item["options"])
                       for item in items)):
            raise ValueError("Invalid response vector")
        raw = sum(answers[item["id"]] == item["correct"] for item in items)
        if row["literacy_score"] != raw or row["literacy_percent"] != round(raw * 100 / len(items), 2):
            reasons.append("前测得分与本问卷逐题答案不一致")
    except (TypeError, ValueError):
        reasons.append("前测须为本问卷全部30题的合法作答")
    return reasons
