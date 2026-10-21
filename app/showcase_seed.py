"""Import approved synthetic fixtures only into the isolated showcase database.

Conversation text and event timelines are deterministic UI fixtures, never model
responses or observed participant behavior. Original simulation rows remain in
``synthetic_source_json`` so the demo adaptation can always be audited.
"""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from .age_forms import AGE_BANDS, form_for, score_form
from .config import ROOT
from .db import connect, task_dict
from . import showcase_rendering as rendering
from .showcase_quality import apply_process_alert_migration, quality_receipt_exists
from .synthetic_exports import BATCH_ID, CONTENT_DIRECTORY, EXPORT_FILES, _approved_path, _batch_verification


PROVENANCE_COLUMNS = {
    "is_synthetic": "INTEGER NOT NULL DEFAULT 0",
    "data_origin": "TEXT", "dataset_variant": "TEXT", "source_batch_id": "TEXT",
    "synthetic_source_json": "TEXT", "synthetic_pretest_time_seconds": "REAL",
    "sim_literacy_group": "TEXT", "sim_response_profile": "TEXT", "sim_qc_flag": "INTEGER",
    "timestamp_is_simulated": "INTEGER", "platform_analysis_eligible": "INTEGER",
    "fixture_conversation": "INTEGER NOT NULL DEFAULT 0",
    "fixture_render_version": "TEXT", "fixture_time_shift_days": "INTEGER",
    "fixture_selection_id": "TEXT", "fixture_qc_derived": "INTEGER NOT NULL DEFAULT 0",
}

LEGACY_SUBSET_SELECTION_ID = "20261021-showcase-126-v1"
LEGACY_SUBSET_SELECTION_SEED = "showcase-126-age-literacy-arm-v1"
SUBSET_SELECTION_ID = "20261021-showcase-126-natural-v2"
SUBSET_SELECTION_SEED = "showcase-126-unstratified-v2"
SUBSET_SIZE = 126
SUBSET_PER_AGE_ARM = 7
# All source ages are eligible. Optional *pretest* QC stays off; existing task
# flags are recomputed from rendered messages regardless of this setting.
ALLOWED_AGES = None
ENABLE_DERIVED_QC = False
TASK_FLAG_POLICY = "existing_task_rules_from_rendered_dialogue-v1"
SOURCE_SESSION_FIELDS = (
    "task_id", "group_id", "interaction_condition", "created_at", "ended_at",
    "decision_started_at", "decision_choice_id", "execution_choice_id", "decision_feasible", "decision_optimal",
    "score", "literacy_score", "literacy_percent", "anchor_score", "age_band", "education_stage", "topic_familiarity",
    "literacy_instrument_version", "scenario_id", "data_origin", "dataset_variant", "sim_literacy_group",
    "sim_response_profile", "synthetic_pretest_time_seconds", "sim_qc_flag", "timestamp_is_simulated",
)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _guard_database(settings) -> None:
    path = Path(settings.database_path)
    if settings.study_mode != "demo" or path.name != "showcase.sqlite3" or path.resolve().name != "showcase.sqlite3":
        raise ValueError("演示数据只能导入 demo 模式的独立 showcase.sqlite3 数据库")
    production = ROOT / "data" / "lens.sqlite3"
    if path.exists() and production.exists() and path.samefile(production):
        raise ValueError("演示数据不能导入现有受试者数据库")


# create_app must run the same guard before init_db/seed_demo can write anything.
guard_showcase_database = _guard_database


def _load_artifacts(dataset_root):
    if not _batch_verification(dataset_root)[0]:
        raise ValueError("演示数据批次未通过600条合成标记及人数核验")
    paths = {"records": _approved_path(dataset_root, EXPORT_FILES["calibrated_json"][0]),
             "answers": _approved_path(dataset_root, EXPORT_FILES["pretest_csv"][0]),
             "frozen": _approved_path(dataset_root, CONTENT_DIRECTORY / "SYNTHETIC_冻结题目与评分.json")}
    if any(path is None for path in paths.values()):
        raise ValueError("演示数据缺少逐人记录、逐题答案或冻结评分文件")
    records = json.loads(paths["records"].read_text(encoding="utf-8-sig"))
    frozen = json.loads(paths["frozen"].read_text(encoding="utf-8-sig"))
    with paths["answers"].open(encoding="utf-8-sig", newline="") as handle:
        pretests = list(csv.DictReader(handle))
    record_ids = [row["id"] for row in records]
    answer_ids = [row["id"] for row in pretests]
    if (len(set(record_ids)) != 600 or len(answer_ids) != 600 or len(set(answer_ids)) != 600
            or set(record_ids) != set(answer_ids)):
        raise ValueError("演示记录与逐题答案的600个唯一编号必须一一对应")
    digests = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in paths.items()}
    return records, {row["id"]: row for row in pretests}, frozen, digests


def _validate_instruments(db, frozen):
    instruments = {}
    if set(frozen) != set(AGE_BANDS):
        raise ValueError("冻结文件必须覆盖九个年龄组")
    for age in AGE_BANDS:
        version, items = form_for(age)
        source = frozen[age]
        if (source["form_version"] != version
                or [{key: item[key] for key in ("id", "question", "options", "correct", "anchor")}
                    for item in source["items"]] !=
                [{key: item[key] for key in ("id", "question", "options", "correct", "anchor")} for item in items]):
            raise ValueError("冻结问卷与当前演示问卷不一致")
        for scenario, snapshot in source["tasks"].items():
            row = db.execute("SELECT * FROM tasks WHERE id=?", (snapshot["id"],)).fetchone()
            if row is None or row["age_band"] != age or row["is_demo"] != 1:
                raise ValueError("演示数据库尚未初始化对应年龄任务")
            task = task_dict(row)
            variant = next((item for item in task["scenario_variants"] if item["id"] == scenario), None)
            if (variant is None or variant["decision_options"] != snapshot["decision_options"]
                    or task["execution_options"] != snapshot["execution_options"]):
                raise ValueError("冻结评分与演示任务的当前权重或选项不一致")
        instruments[age] = (version, items)
    return instruments


def _validated_row(source, pretest, frozen, instruments, settings):
    age = source["age_band"]
    version, items = instruments[age]
    if (pretest.get("is_synthetic") != "1" or pretest.get("data_origin") != "synthetic"
            or pretest["age_band"] != age or pretest["form_version"] != version
            or source["literacy_instrument_version"] != version):
        raise ValueError("逐题答案的合成标记或问卷版本不匹配")
    answers = {item["id"]: int(pretest[item["id"]]) for item in items}
    raw, percent, band, anchor = score_form(age, answers, settings.literacy_band_width)
    if (raw != source["literacy_score"] or raw != int(pretest["literacy_score"])
            or percent != source["literacy_percent"] or percent != float(pretest["literacy_percent"])
            or anchor != source["anchor_score"] or anchor != int(pretest["anchor_score"])):
        raise ValueError("演示问卷答案不能复算出原合成得分")
    snapshot = json.loads(_json(frozen[age]["tasks"][source["scenario_id"]]))
    snapshot.pop("scenario_variants", None)
    snapshot.pop("scenario_values", None)
    snapshot["is_demo"] = True
    level = "高" if raw >= 24 else "中" if raw >= 15 else "低"
    if (source["task_id"] != snapshot["id"] or source["dataset_variant"] != "target_calibrated"
            or source["sim_literacy_group"] != level):
        raise ValueError("演示任务编号、校准版本或素养层级不匹配")
    completed = source["status"] == "completed"
    selected = next((item for item in snapshot["decision_options"]
                     if item["id"] == source["decision_choice_id"]), None)
    execution = next((item for item in snapshot["execution_options"]
                      if item["id"] == source["execution_choice_id"]), None)
    if completed:
        if selected is None or execution is None or selected["score"] + execution["score"] != source["score"]:
            raise ValueError("演示决策分数不能由冻结选项复算")
        components = dict(selected["score_components"])
        for key, value in execution["score_components"].items():
            components[key] = components.get(key, 0) + value
        feasible = int(bool(selected.get("success")) and bool(execution.get("success")))
        optimal = int(bool(selected.get("goal_met")) and bool(execution.get("success")))
        if (components != source["decision_components_json"] or feasible != source["decision_feasible"]
                or optimal != source["decision_optimal"]):
            raise ValueError("演示分解得分或可行性与冻结选项不一致")
    elif any(source.get(key) is not None for key in ("score", "ended_at", "decision_choice_id", "execution_choice_id")):
        raise ValueError("脱落记录不能具有完成分数或最终选择")
    dates = [datetime.fromisoformat(source[key]) for key in ("created_at", "decision_started_at", "ended_at")
             if source.get(key)]
    if any(date.tzinfo is None for date in dates) or dates != sorted(dates):
        raise ValueError("模拟时点必须带时区并按先后排列")
    if completed and len(dates) != 3:
        raise ValueError("完成记录必须保留原模拟开始、决策和提交时点")
    if (source["group_id"] not in {"S", "A"}
            or source["interaction_condition"] != {"S": "standard", "A": "adaptive"}[source["group_id"]]
            or not 0 <= source["clarification_count"] <= source["user_turns"]
            or source["evidence_reveals"] + source["clarification_count"] > source["user_turns"]
            or not 0 <= source["distinct_evidence_cards"] <= len(snapshot["evidence_cards"])
            or (source["group_id"] == "S" and source["clarification_count"])):
        raise ValueError("模拟条件、轮次与事件计数不一致")
    return answers, snapshot, band


def _validate_target_means(records):
    targets = {"高": {"S": 80, "A": 95}, "中": {"S": 70, "A": 80}, "低": {"S": 60, "A": 63}}
    for level, groups in targets.items():
        for group, target in groups.items():
            scores = [row["score"] for row in records if row["status"] == "completed"
                      and row["sim_literacy_group"] == level and row["group_id"] == group]
            if not scores or abs(sum(scores) / len(scores) - target) > 1e-9:
                raise ValueError("已批准演示数据的分组均值不匹配；不能修改分数来修复")


def _dialogue_counts_consistent(row) -> bool:
    """Quality is completeness and reproducibility, never score or profile."""
    counts = [row.get(key) for key in ("user_turns", "clarification_count", "evidence_reveals", "distinct_evidence_cards")]
    if any(type(value) is not int or value < 0 for value in counts):
        return False
    turns, clarifications, reveals, distinct = counts
    return (turns >= 2 and clarifications + reveals <= turns and distinct <= reveals
            and ((reveals == 0 and distinct == 0) or (reveals > 0 and distinct > 0)))


def _select_legacy_subset(records, source_digests=None):
    """Choose balanced strata by stable ID hashes, without reading task scores.

    Callers must first validate the source answers and frozen score arithmetic.
    Each age/arm receives seven complete records. A least-filled literacy bin
    is used whenever it has capacity; ties favor the least-filled overall bin
    and then a fixed hash. Profiles and task score levels do not affect ranking.
    """
    eligible = [row for row in records if row.get("status") == "completed" and _dialogue_counts_consistent(row)]
    levels = ("高", "中", "低")
    overall = Counter()
    selected = []
    strata = []

    def rank(*parts):
        return hashlib.sha256(":".join((LEGACY_SUBSET_SELECTION_SEED, *parts)).encode()).hexdigest()

    for age in AGE_BANDS:
        for arm in ("S", "A"):
            bins = {level: sorted((row for row in eligible if row["age_band"] == age
                                   and row["group_id"] == arm and row["sim_literacy_group"] == level),
                                  key=lambda row: (rank(age, arm, level, row["id"]), row["id"]))
                    for level in levels}
            if sum(map(len, bins.values())) < SUBSET_PER_AGE_ARM:
                raise ValueError(f"{age}/{arm}不足7条完整且对话计数一致的候选记录，不能凑足126条")
            counts = Counter()
            for _ in range(SUBSET_PER_AGE_ARM):
                available = [level for level in levels if counts[level] < len(bins[level])]
                level = min(available, key=lambda value: (counts[value], overall[value], rank(age, arm, value)))
                selected.append(bins[level][counts[level]])
                counts[level] += 1
                overall[level] += 1
            strata.append({"age_band": age, "group_id": arm,
                           "available": {level: len(bins[level]) for level in levels},
                           "selected": {level: counts[level] for level in levels}})
    selected.sort(key=lambda row: row["id"])
    selected_ids = {row["id"] for row in selected}
    if len(selected_ids) != SUBSET_SIZE:
        raise ValueError("126条选样编号必须唯一")
    means = []
    for level in levels:
        cell = {"literacy_group": level}
        for arm in ("S", "A"):
            scores = [row["score"] for row in selected if row["sim_literacy_group"] == level and row["group_id"] == arm]
            cell[arm + "_n"] = len(scores)
            cell[arm + "_mean"] = sum(scores) / len(scores) if scores else None
        means.append(cell)
    manifest = {"selection_id": LEGACY_SUBSET_SELECTION_ID, "selection_seed": LEGACY_SUBSET_SELECTION_SEED,
                "source_batch_id": BATCH_ID, "source_sha256": source_digests or {},
                "source_count": len(records), "eligible_count": len(eligible), "selected_count": len(selected),
                "quality_rules": ["status=completed", "pretest_and_frozen_scores_recomputed_before_selection",
                                  "dialogue_and_event_counts_consistent"],
                "sampling_rule": "9 ages x 2 arms x 7; least-filled available literacy bin; stable hash ID order",
                "no_score_or_profile_ranking": True, "no_mean_calibration": True,
                "selected_ids": [row["id"] for row in selected],
                "excluded_source_ids": sorted(row["id"] for row in records if row["id"] not in selected_ids),
                "strata": strata, "literacy_counts": dict(overall),
                "profile_counts": dict(Counter(row["sim_response_profile"] for row in selected)),
                "group_means_descriptive_only": means}
    return selected, manifest


def _age_scope(allowed_ages):
    if allowed_ages is None:
        return tuple(AGE_BANDS)
    if isinstance(allowed_ages, (str, bytes)):
        raise ValueError("allowed_ages必须为年龄区间集合或None")
    requested = set(allowed_ages)
    if not requested or not requested.issubset(AGE_BANDS):
        raise ValueError("allowed_ages包含无效年龄区间或为空")
    return tuple(age for age in AGE_BANDS if age in requested)


def select_showcase_subset(records, source_digests=None, *, allowed_ages=None):
    """One fixed, unstratified draw from eligible source IDs, without replacement.

    Age is only an explicitly supplied scope restriction. No age, arm,
    literacy, response-profile or score quotas/ranks/retries are applied.
    Answer and score arithmetic must be validated by the caller first.
    """
    ages = _age_scope(allowed_ages)
    eligible = [row for row in records if row.get("status") == "completed"
                and row.get("age_band") in ages and _dialogue_counts_consistent(row)]
    if len(eligible) < SUBSET_SIZE:
        raise ValueError("所选年龄范围不足126条完整且对话计数一致的候选记录")
    ranked = sorted(eligible, key=lambda row: (
        hashlib.sha256(f"{SUBSET_SELECTION_SEED}:{row['id']}".encode()).hexdigest(), row["id"]))
    selected = sorted(ranked[:SUBSET_SIZE], key=lambda row: row["id"])
    selected_ids = {row["id"] for row in selected}
    if len(selected_ids) != SUBSET_SIZE:
        raise ValueError("126条选样编号必须唯一")
    means = []
    for level in ("高", "中", "低"):
        cell = {"literacy_group": level}
        for arm in ("S", "A"):
            scores = [row["score"] for row in selected if row["sim_literacy_group"] == level and row["group_id"] == arm]
            cell[arm + "_n"] = len(scores)
            cell[arm + "_mean"] = sum(scores) / len(scores) if scores else None
        means.append(cell)
    manifest = {"selection_id": SUBSET_SELECTION_ID, "selection_seed": SUBSET_SELECTION_SEED,
                "source_batch_id": BATCH_ID, "source_sha256": source_digests or {},
                "source_count": len(records), "eligible_count": len(eligible), "selected_count": len(selected),
                "allowed_ages": list(ages), "sampling_rule": "overall SHA256(seed:id) order; first 126 eligible IDs; no quotas or seed retries",
                "quality_rules": ["status=completed", "pretest_and_frozen_scores_recomputed_before_selection",
                                  "dialogue_and_event_counts_consistent"],
                "no_score_or_profile_ranking": True, "no_mean_calibration": True,
                "selected_ids": [row["id"] for row in selected],
                "excluded_source_ids": sorted(row["id"] for row in records if row["id"] not in selected_ids),
                "age_counts": dict(Counter(row["age_band"] for row in selected)),
                "arm_counts": dict(Counter(row["group_id"] for row in selected)),
                "literacy_counts": dict(Counter(row["sim_literacy_group"] for row in selected)),
                "profile_counts": dict(Counter(row["sim_response_profile"] for row in selected)),
                "group_means_descriptive_only": means}
    return selected, manifest


def _fixture_payloads(row, snapshot, strategy):
    """Render frozen source excerpts; preserve counts, with no latency/model call."""
    turns, clarifications = row["user_turns"], row["clarification_count"]
    cards = snapshot["evidence_cards"]
    distinct = row["distinct_evidence_cards"]
    if row["evidence_reveals"] and not distinct:
        raise ValueError("资料揭示记录必须有对应的不同资料卡")
    start = datetime.fromisoformat(row["created_at"])
    stop = datetime.fromisoformat(row["decision_started_at"]) if row.get("decision_started_at") else start + timedelta(seconds=max(30, turns * 30))
    span = (stop - start).total_seconds()
    detail_base = {"is_synthetic": 1, "source": "showcase_fixture", "batch_id": BATCH_ID,
                   "conversation_source": "template_not_model_response", "timestamp_is_simulated": 1}
    messages, events = [], []
    for index in range(turns):
        user_at = (start + timedelta(seconds=span * (2 * index + 1) / (2 * turns + 1))).isoformat(timespec="seconds")
        reply_at = (start + timedelta(seconds=span * (2 * index + 2) / (2 * turns + 1))).isoformat(timespec="seconds")
        if index < clarifications:
            slot = snapshot["slots"][index % len(snapshot["slots"])]
            question = "请帮我核对这个案例的报名安排。" if index == 0 else snapshot["participant_facts"]
            reply, action = slot["question"], f"{strategy}_clarification"
            events.append((row["id"], "clarification_decision", _json({**detail_base, "slot_key": slot["key"],
                                                                      "need_clarification": True}), reply_at))
        elif index - clarifications < row["evidence_reveals"]:
            card = cards[(index - clarifications) % distinct]
            question = (f"请说明{card['title']}。" if row["sim_response_profile"] != "认真"
                        else f"我想逐项核实案例。请依据现行资料说明：{card['title']}。")
            reply, action = f"资料 {card['id']}｜{card['title']}\n{card['body']}", "evidence"
            events.append((row["id"], "evidence_reveal", _json({**detail_base, "card_id": card["id"]}), reply_at))
        else:
            question = "还有什么要核对？" if row["sim_response_profile"] != "随意" else "知道了，下一步呢？"
            reply, action = "请结合案例中的参加记录、每周到场次数与已核对资料，独立选择参加及办理方案。", "task_guidance"
        messages.extend([
            (row["id"], "user", question, "fixture_user_message", user_at),
            (row["id"], "assistant", reply, action, reply_at)])
        events.append((row["id"], "showcase_fixture_turn", _json({**detail_base, "turn": index + 1}), reply_at))
    return messages, events


def _fixture_dialogue(db, row, snapshot, strategy):
    messages, events = rendering.fixture_payloads(row, snapshot, strategy)
    db.executemany("INSERT INTO messages(session_id,role,content,action,created_at) VALUES (?,?,?,?,?)", messages)
    db.executemany("INSERT INTO model_events(session_id,event_type,detail_json,created_at) VALUES (?,?,?,?)", events)


def _verify_unedited_fixture(db, source, validated, strategy):
    """Reject migration if an existing fixture has any participant/admin edit."""
    answers, snapshot, band = validated
    row = db.execute("SELECT * FROM sessions WHERE id=?", (source["id"],)).fetchone()
    if (row is None or row["source_batch_id"] != BATCH_ID or row["record_kind"] != "synthetic"
            or row["fixture_conversation"] != 1 or row["is_synthetic"] != 1 or row["run_mode"] != "demo"
            or json.loads(row["synthetic_source_json"] or "null") != source
            or any(row[key] != source[key] for key in SOURCE_SESSION_FIELDS)
            or json.loads(row["literacy_answers_json"]) != answers or row["literacy_band"] != band
            or json.loads(row["task_snapshot_json"]) != snapshot):
        raise ValueError(f"{source['id']}来源或原始数据已修改，不能自动缩减演示批次")
    completed = source["status"] == "completed"
    expected_review = ("valid", "demo-fixture", source["ended_at"], "", None, "{}") if completed else ("pending", None, None, "", None, "{}")
    if tuple(row[key] for key in ("review_status", "reviewer", "reviewed_at", "review_reason", "review_code", "feedback_json")) != expected_review:
        raise ValueError(f"{source['id']}已有管理员审核或反馈修改，不能自动缩减演示批次")
    flags = (["no_evidence_retrieved"] if completed and source["evidence_reveals"] == 0 else [])
    if completed and source.get("task_completion_seconds", 45) < 45:
        flags.append("very_fast_completion")
    selected = next((item for item in snapshot["decision_options"] if item["id"] == source["decision_choice_id"]), None)
    execution = next((item for item in snapshot["execution_options"] if item["id"] == source["execution_choice_id"]), None)
    submission = f"参加方案：{selected['text']}\n办理方案：{execution['text']}" if completed else None
    if (json.loads(row["flags_json"]) != flags or row["submission"] != submission
            or row["score_note"] != ("机器评分 decision-age-v4" if completed else None)):
        raise ValueError(f"{source['id']}已有提交、标记或评分备注修改，不能自动缩减演示批次")
    expected_messages, expected_events = _fixture_payloads(source, snapshot, strategy)
    actual_messages = [tuple(item) for item in db.execute(
        "SELECT session_id,role,content,action,created_at FROM messages WHERE session_id=? ORDER BY id", (source["id"],))]
    actual_events = [tuple(item) for item in db.execute(
        "SELECT session_id,event_type,detail_json,created_at FROM model_events WHERE session_id=? ORDER BY id", (source["id"],))]
    history = [dict(item) for item in db.execute(
        "SELECT session_id,status,reason,reviewer,created_at FROM review_history WHERE session_id=? ORDER BY id", (source["id"],))]
    expected_history = [{"session_id": source["id"], "status": "valid", "reason": "showcase_fixture",
                         "reviewer": "demo-fixture", "created_at": source["ended_at"]}] if completed else []
    if (actual_messages != expected_messages or actual_events != expected_events or history != expected_history
            or any(db.execute(f"SELECT 1 FROM {table} WHERE session_id=? LIMIT 1", (source["id"],)).fetchone()
                   for table in ("start_requests", "message_requests"))):
        raise ValueError(f"{source['id']}已有对话、事件或审核历史修改，不能自动缩减演示批次")
    return history


def _save_selection(db, manifest):
    db.execute("""CREATE TABLE IF NOT EXISTS showcase_selections (
        selection_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, selected_count INTEGER NOT NULL,
        manifest_json TEXT NOT NULL)""")
    db.execute("INSERT INTO showcase_selections VALUES (?,?,?,?)",
               (manifest["selection_id"], BATCH_ID, manifest["selected_count"], _json(manifest)))


def _ensure_provenance_columns(db):
    existing = {row["name"] for row in db.execute("PRAGMA table_info(sessions)")}
    for name, definition in PROVENANCE_COLUMNS.items():
        if name not in existing:
            db.execute(f"ALTER TABLE sessions ADD COLUMN {name} {definition}")


def _legacy_flags(row):
    flags = []
    if row["status"] == "completed" and row["evidence_reveals"] == 0:
        flags.append("no_evidence_retrieved")
    if row["status"] == "completed" and row.get("task_completion_seconds", 45) < 45:
        flags.append("very_fast_completion")
    return flags


def _fixture_values(source, validated, settings, model_config_id, cohort_prompts, strategy, design_version,
                    *, enable_derived_qc=False, natural=True):
    answers, snapshot, band = validated
    row = rendering.adapted_row(source) if natural else source
    completed = row["status"] == "completed"
    selected = next((item for item in snapshot["decision_options"] if item["id"] == row["decision_choice_id"]), None)
    execution = next((item for item in snapshot["execution_options"] if item["id"] == row["execution_choice_id"]), None)
    if natural:
        flags = rendering.derived_fixture_flags(row, snapshot, answers, strategy=strategy)
        if not enable_derived_qc:
            flags = [flag for flag in flags if not flag.startswith("pretest_")]
    else:
        flags = _legacy_flags(row)
    values = {key: row[key] for key in ("id", *SOURCE_SESSION_FIELDS)}
    values.update({
        "expression_condition": "natural", "run_mode": "demo", "is_demo": 1, "record_kind": "synthetic",
        "assignment_seed": settings.assignment_seed, "model_name": "DEMO-RULE", "model_base_url": "DEMO",
        "model_temperature": settings.model_temperature, "model_thinking": settings.model_thinking,
        "model_max_tokens": settings.model_max_tokens, "model_config_id": model_config_id,
        "prompt_version": cohort_prompts[0 if row["group_id"] == "S" else 1],
        "task_snapshot_json": _json(snapshot), "design_version": design_version, "literacy_band": band,
        "literacy_answers_json": _json(answers), "literacy_band_width": settings.literacy_band_width,
        "clarification_strategy": strategy, "guardian_consented": 0,
        "submission": (f"参加方案：{selected['text']}\n办理方案：{execution['text']}" if completed else None),
        "review_status": "valid" if completed else "pending", "reviewer": "demo-fixture" if completed else None,
        "reviewed_at": row["ended_at"] if completed else None, "review_reason": "",
        "score_note": "机器评分 decision-age-v4" if completed else None,
        "flags_json": _json(flags), "feedback_json": "{}", "is_synthetic": 1,
        "platform_analysis_eligible": 0, "source_batch_id": BATCH_ID,
        "synthetic_source_json": _json(source), "fixture_conversation": 1,
        "fixture_render_version": rendering.RENDER_VERSION if natural else None,
        "fixture_time_shift_days": rendering.TIME_SHIFT_DAYS if natural else None,
        "fixture_selection_id": SUBSET_SELECTION_ID if natural else None,
        "fixture_qc_derived": int(enable_derived_qc),
    })
    return values, row, snapshot


def _write_fixture_history(db, row):
    if row["status"] == "completed":
        db.execute("INSERT INTO review_history(session_id,status,reason,reviewer,created_at) VALUES (?,?,?,?,?)",
                   (row["id"], "valid", "showcase_fixture", "demo-fixture", row["ended_at"]))


def _insert_fixture(db, source, validated, settings, model_config_id, cohort_prompts, strategy, design_version,
                    *, enable_derived_qc=False, natural=True):
    values, row, snapshot = _fixture_values(source, validated, settings, model_config_id, cohort_prompts, strategy,
                                          design_version, enable_derived_qc=enable_derived_qc, natural=natural)
    names = list(values)
    db.execute(f"INSERT INTO sessions ({','.join(names)}) VALUES ({','.join('?' for _ in names)})", [values[name] for name in names])
    if natural:
        _fixture_dialogue(db, row, snapshot, strategy)
    else:
        messages, events = _fixture_payloads(row, snapshot, strategy)
        db.executemany("INSERT INTO messages(session_id,role,content,action,created_at) VALUES (?,?,?,?,?)", messages)
        db.executemany("INSERT INTO model_events(session_id,event_type,detail_json,created_at) VALUES (?,?,?,?)", events)
    _write_fixture_history(db, row)


def _refresh_existing_fixture(db, source, validated, settings, model_config_id, cohort_prompts, strategy, design_version,
                              *, enable_derived_qc=False):
    values, row, snapshot = _fixture_values(source, validated, settings, model_config_id, cohort_prompts, strategy,
                                          design_version, enable_derived_qc=enable_derived_qc)
    # Scores, answer vectors, choices, original source JSON and runtime cohort
    # settings of retained fixtures remain untouched. Only presentation/QC and
    # the explicitly documented timebase are refreshed.
    changed = ("created_at", "decision_started_at", "ended_at", "reviewed_at", "flags_json",
               "fixture_render_version", "fixture_time_shift_days", "fixture_selection_id", "fixture_qc_derived")
    db.execute(f"UPDATE sessions SET {','.join(key+'=?' for key in changed)} WHERE id=?",
               [values[key] for key in changed] + [source["id"]])
    _fixture_dialogue(db, row, snapshot, strategy)
    _write_fixture_history(db, row)


def _run_observed_quality_migration(db, settings, dataset_root, *, records=None, validations=None, digests=None):
    if quality_receipt_exists(db):
        return
    selection = json.loads(db.execute("SELECT manifest_json FROM showcase_selections WHERE selection_id=?",
                                      (SUBSET_SELECTION_ID,)).fetchone()[0])
    if records is None:
        records, pretests, frozen, digests = _load_artifacts(Path(dataset_root))
        instruments = _validate_instruments(db, frozen)
        validations = {row["id"]: _validated_row(row, pretests[row["id"]], frozen, instruments, settings)
                       for row in records}
    if selection["source_sha256"] != digests:
        raise ValueError("过程提醒迁移的原source指纹不匹配")
    apply_process_alert_migration(db, selection_id=SUBSET_SELECTION_ID,
                                 expected_sources={row["id"]: row for row in records},
                                 expected_answers={sid: value[0] for sid, value in validations.items()},
                                 expected_snapshots={sid: value[1] for sid, value in validations.items()})


def seed_showcase(settings, model_config_id: str, cohort_prompts: tuple[str, str], strategy: str,
                  dataset_root: Path, design_version: str, *, allowed_ages=None,
                  enable_derived_qc: bool | None = None) -> None:
    """Seed natural v2, replacing only a verified legacy 600/v1 fixture set."""
    _guard_database(settings)
    if strategy not in {"module", "prompt"} or len(cohort_prompts) != 2:
        raise ValueError("演示策略和两组提示词版本无效")
    ages = _age_scope(ALLOWED_AGES if allowed_ages is None else allowed_ages)
    qc_enabled = ENABLE_DERIVED_QC if enable_derived_qc is None else enable_derived_qc
    if type(qc_enabled) is not bool:
        raise ValueError("enable_derived_qc必须为布尔值")
    with connect(settings.database_path) as db:
        db.execute("BEGIN IMMEDIATE")
        has_imports = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='showcase_imports'").fetchone()
        has_selections = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='showcase_selections'").fetchone()
        receipt = (db.execute("SELECT manifest_json FROM showcase_selections WHERE selection_id=?", (SUBSET_SELECTION_ID,)).fetchone()
                   if has_selections else None)
        if receipt:
            previous = json.loads(receipt["manifest_json"])
            if (previous.get("allowed_ages") != list(ages) or previous.get("derived_qc_enabled") != qc_enabled
                    or previous.get("render_version") != rendering.RENDER_VERSION
                    or previous.get("task_flag_policy") != TASK_FLAG_POLICY):
                raise ValueError("v2演示选样或QC配置已变更；须使用新的迁移版本，不能覆盖已保存记录")
            if not qc_enabled:
                _run_observed_quality_migration(db, settings, dataset_root)
            return
        records, pretests, frozen, digests = _load_artifacts(Path(dataset_root))
        instruments = _validate_instruments(db, frozen)
        validated = [_validated_row(row, pretests[row["id"]], frozen, instruments, settings) for row in records]
        _validate_target_means(records)
        selected_records, manifest = select_showcase_subset(records, digests, allowed_ages=ages)
        manifest.update(render_version=rendering.RENDER_VERSION, time_shift_days=rendering.TIME_SHIFT_DAYS,
                        derived_qc_enabled=qc_enabled, pretest_qc_enabled=qc_enabled, task_flag_policy=TASK_FLAG_POLICY)
        validations = {row["id"]: value for row, value in zip(records, validated)}
        sources = {row["id"]: row for row in records}
        previous_import = has_imports and db.execute("SELECT * FROM showcase_imports WHERE batch_id=?", (BATCH_ID,)).fetchone()
        if previous_import:
            current_ids = {row["id"] for row in db.execute(
                "SELECT id FROM sessions WHERE source_batch_id=? AND fixture_conversation=1 AND record_kind='synthetic'", (BATCH_ID,))}
            legacy_receipt = (db.execute("SELECT manifest_json FROM showcase_selections WHERE selection_id=?", (LEGACY_SUBSET_SELECTION_ID,)).fetchone()
                              if has_selections else None)
            prior = json.loads(legacy_receipt["manifest_json"]) if legacy_receipt else None
            expected_ids = set(prior["selected_ids"]) if prior else set(sources)
            expected_digests = prior.get("source_sha256") if prior else json.loads(previous_import["source_sha256_json"])
            if current_ids != expected_ids or expected_digests != digests:
                raise ValueError("原演示批次编号或source指纹不匹配，不能自动替换；请核对备份")
            histories = {sid: _verify_unedited_fixture(db, sources[sid], validations[sid], strategy) for sid in sorted(current_ids)}
            desired = {row["id"] for row in selected_records}
            removed, restored, retained = sorted(current_ids - desired), sorted(desired - current_ids), sorted(desired & current_ids)
            for sid in restored:
                if db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
                    raise ValueError("待恢复source编号与非本批记录冲突，不能覆盖互动记录")
            manifest.update(operation="migrate_v1_126_to_natural_v2" if prior else "migrate_unedited_600_to_natural_v2",
                            previous_selection_id=LEGACY_SUBSET_SELECTION_ID if prior else None,
                            removed_ids=removed, restored_ids=restored, retained_ids=retained,
                            rerendered_ids=sorted(desired),
                            removed_review_history=[item for sid in removed for item in histories[sid]],
                            prior_fixture_review_history=[item for sid in sorted(current_ids) for item in histories[sid]])
            _ensure_provenance_columns(db)
            _save_selection(db, manifest)
            # All selected legacy conversations are re-rendered on the new
            # historical timebase, including IDs retained across selections.
            for table in ("start_requests", "message_requests", "messages", "model_events", "review_history"):
                db.executemany(f"DELETE FROM {table} WHERE session_id=?", [(sid,) for sid in sorted(current_ids)])
            db.executemany("""DELETE FROM sessions WHERE id=? AND source_batch_id=?
                AND fixture_conversation=1 AND record_kind='synthetic'""", [(sid, BATCH_ID) for sid in removed])
            for source in selected_records:
                writer = _refresh_existing_fixture if source["id"] in current_ids else _insert_fixture
                writer(db, source, validations[source["id"]], settings, model_config_id, cohort_prompts, strategy,
                       design_version, enable_derived_qc=qc_enabled)
            if not qc_enabled:
                _run_observed_quality_migration(db, settings, dataset_root, records=records, validations=validations, digests=digests)
            return
        if any(db.execute("SELECT 1 FROM sessions WHERE id=?", (row["id"],)).fetchone() for row in records):
            raise ValueError("演示编号已存在且未有完整导入凭据；请使用独立干净演示库")
        _ensure_provenance_columns(db)
        for row in selected_records:
            _insert_fixture(db, row, validations[row["id"]], settings, model_config_id, cohort_prompts, strategy,
                            design_version, enable_derived_qc=qc_enabled)
        db.execute("CREATE TABLE IF NOT EXISTS showcase_imports (batch_id TEXT PRIMARY KEY, source_sha256_json TEXT NOT NULL, record_count INTEGER NOT NULL, provenance TEXT NOT NULL)")
        db.execute("INSERT INTO showcase_imports VALUES (?,?,?,?)", (
            BATCH_ID, _json(digests), len(selected_records), "126 unstratified natural-v2 fixtures from original 600; no participant/model observations"))
        manifest["operation"] = "fresh_natural_v2_126_import"
        manifest["removed_ids"] = []
        manifest["removed_review_history"] = []
        _save_selection(db, manifest)
        if not qc_enabled:
            _run_observed_quality_migration(db, settings, dataset_root, records=records, validations=validations, digests=digests)
