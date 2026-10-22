"""Independent university-only synthetic records for the isolated showcase.

Nothing in this generator reads the legacy 600/126 rows. Answers and decisions
are sampled once from the frozen student materials before arm assignment. The
seed is fixed, not retried to obtain an intervention effect. Conversations are
authored UI fixtures, not model output or observed participant behaviour.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import random
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from .config import ROOT
from .db import task_dict
from .process_alerts import QUALITY_VERSION, derive_process_alerts
from .record_reporting import ai_prompt_counts
from .student_materials import (AGE_BANDS, DESIGN_VERSION, EDUCATION_STAGES,
                                FORM_VERSION, form_for, score_form, task_for)


BATCH_ID = "student-126-20261022-v2"
GENERATOR_VERSION = "student-independent-simulation-v2"
GENERATOR_SEED = "university-v1-independent-student-fixtures-20261022"
ARM_SEED = "university-v1-arm-assignment-20261022"
RENDER_VERSION = "student-authored-dialogue-v2"
RECORD_COUNT = 126
PER_AGE = 42
SCENARIOS = ("first_3", "first_1", "return_3", "return_1")
BASE_TIME = datetime(2026, 10, 22, 0, 0, tzinfo=timezone.utc)
EVIDENCE_CARD_IDS = ("B_SCOPE", "B_REPEAT", "B_SCHEDULE", "C_TIME", "B_MATERIALS")
EVIDENCE_POLICY = (
    "人工模拟假设：A组在补齐两项个案事实后展示5条目标来源，S组按当前提问展示其中3条，"
    "按S组记录顺序轮转来源。每次线索均有对应模板回复和evidence_reveal事件。"
    "这是预设的过程展示差异，不是真实模型或受试者的观测效果；不据此调整答案或任务分数。"
)
NOTICE = (
    "全部为新生成的126条18–25岁在读专科或本科大学生合成记录，不是真实受试者数据。"
    "对话为人工模板，未调用模型；日期、时长和身份均为模拟值，不进入正式分析。"
    "A组更多资料线索为人工设定的模拟假设，不代表真实干预效果。"
)

PROVENANCE_COLUMNS = {
    "is_synthetic": "INTEGER NOT NULL DEFAULT 0",
    "data_origin": "TEXT", "dataset_variant": "TEXT", "source_batch_id": "TEXT",
    "synthetic_source_json": "TEXT", "synthetic_pretest_time_seconds": "REAL",
    "timestamp_is_simulated": "INTEGER", "platform_analysis_eligible": "INTEGER",
    "fixture_conversation": "INTEGER NOT NULL DEFAULT 0", "fixture_render_version": "TEXT",
    "student_confirmed": "INTEGER NOT NULL DEFAULT 0", "exact_age": "INTEGER",
    "process_alert_details_json": "TEXT NOT NULL DEFAULT '[]'",
}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _rng(seed: str, key: str) -> random.Random:
    return random.Random(int.from_bytes(hashlib.sha256(f"{seed}:{key}".encode()).digest(), "big"))


def guard_student_showcase_database(settings, conn: sqlite3.Connection | None = None) -> None:
    """Permit only the isolated demo DB, checking the actual connection too."""
    path = Path(settings.database_path).resolve()
    if settings.study_mode != "demo" or path.name != "showcase.sqlite3":
        raise ValueError("大学生合成数据仅可导入demo模式的独立showcase.sqlite3")
    for forbidden in (ROOT / "data" / "lens.sqlite3", ROOT / "showcase-data" / "showcase.sqlite3"):
        if path == forbidden.resolve() or (path.exists() and forbidden.exists() and path.samefile(forbidden)):
            raise ValueError("不能覆盖真实受试者数据库或旧版跨年龄展示数据库")
    if conn is not None:
        actual = next((row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"), "")
        if not actual or Path(actual).resolve() != path:
            raise ValueError("连接与配置的独立大学生展示数据库不一致")


def _snapshot(age: str, scenario: str) -> dict:
    task = copy.deepcopy(task_for(age, scenario))
    variants = task.pop("scenario_variants", [])
    selected = next((item for item in variants if item["id"] == scenario), None)
    if selected:
        task["participant_facts"] = selected["participant_facts"]
        task["decision_options"] = copy.deepcopy(selected["decision_options"])
    task.pop("scenario_values", None)
    task["scenario_id"] = scenario
    return task


def _sample_choice(options: list[dict], proficiency: float, rng: random.Random) -> dict:
    """The same stochastic decision rule applies before either arm is known."""
    maximum = max(option["score"] for option in options)
    best = [option for option in options if option["score"] == maximum]
    other = [option for option in options if option["score"] != maximum]
    return rng.choice(best if rng.random() < 0.25 + 0.65 * proficiency or not other else other)


def _dialogue(row: dict, snapshot: dict) -> tuple[list[dict], list[dict]]:
    start = datetime.fromisoformat(row["created_at"])
    stop = datetime.fromisoformat(row["decision_started_at"])
    by_id = {card["id"]: card for card in snapshot["evidence_cards"]}
    if not set(EVIDENCE_CARD_IDS) <= set(by_id):
        raise ValueError("V2资料展示假设缺少冻结的目标来源")
    weekly = int(row["scenario_id"].rsplit("_", 1)[1])
    prior = row["scenario_id"].startswith("return_")
    prior_reply = f"陈禾{'曾获得过' if prior else '从未获得过'}本项目的活动名额。"
    weekly_reply = f"陈禾每周最多能到场{weekly}次。"
    question = "请帮我核对这个校园项目怎样选择登记类别、参加安排和办理方案。"
    if row["group_id"] == "A":
        chosen = [by_id[key] for key in EVIDENCE_CARD_IDS]
        # Two actual clarification turns, one necessary fact per turn. These
        # are authored fixtures demonstrating the policy, never model output.
        exchanges = [
            (question, "陈禾此前是否曾获得过本项目的活动名额？", "prompt_clarification", None),
            (prior_reply, "陈禾每周最多能到场参加几次活动？", "prompt_clarification", None),
            (weekly_reply, "两项个案事实已补齐，我将继续按现行资料回答原问题。", "task_answer", None),
        ]
    else:
        # Rotate three cards through the 21 standard records in each age band.
        # Every card is therefore shown fewer than 21 times, without editing a
        # summary number or changing any sampled questionnaire/decision score.
        offset = row["fixture_arm_index"] % len(EVIDENCE_CARD_IDS)
        chosen = [by_id[EVIDENCE_CARD_IDS[(offset + index) % len(EVIDENCE_CARD_IDS)]] for index in range(3)]
        exchanges = [
            (question, "登记类别取决于此前是否获得本项目名额，参加安排取决于每周最多可到场次数；未提供的情况不能默认满足。", "task_answer", None),
            (prior_reply + weekly_reply, "已收到两项案例情况，下面依据你提出的资料问题核对。", "task_answer", None),
        ]
    for index, card in enumerate(chosen):
        answer = f"资料 {card['id']}｜{card['title']}\n{card['body']}"
        if index == len(chosen) - 1:
            category = "登记乙" if prior else "登记甲"
            plan = "现场安排" if weekly >= 2 else "弹性安排"
            answer += (f"\n结合已提供的案例事实，参加方案应为{category}和{plan}。"
                       "办理应按更正后的11月18日17:00期限，到活动服务点提交报名表和参加计划。")
        exchanges.append((f"请依据现行资料说明：{card['title']}。", answer, "evidence", card["id"]))
    messages, events = [], []
    provenance = {"is_synthetic": 1, "source": "student_authored_fixture", "source_batch_id": BATCH_ID,
                  "conversation_source": "authored_template_not_model_output", "timestamp_is_simulated": 1,
                  "evidence_policy_is_simulated_assumption": True, "render_version": RENDER_VERSION}
    for index, (question, answer, action, card_id) in enumerate(exchanges):
        user_at = (start + (stop - start) * ((2 * index + 1) / (2 * len(exchanges) + 1))).isoformat(timespec="seconds")
        reply_at = (start + (stop - start) * ((2 * index + 2) / (2 * len(exchanges) + 1))).isoformat(timespec="seconds")
        messages.extend([{"role": "user", "content": question, "action": "synthetic_user_message", "created_at": user_at},
                         {"role": "assistant", "content": answer, "action": action, "created_at": reply_at}])
        if card_id:
            events.append({"event_type": "evidence_reveal", "detail": {**provenance, "card_id": card_id}, "created_at": reply_at})
        if action == "prompt_clarification":
            events.append({"event_type": "clarification_decision", "detail": {**provenance, "need_clarification": True}, "created_at": reply_at})
        events.append({"event_type": "student_fixture_turn", "detail": {**provenance, "turn": index + 1}, "created_at": reply_at})
    return messages, events


@lru_cache(maxsize=1)
def _generated_dataset() -> dict:
    if len(AGE_BANDS) != 3 or set(EDUCATION_STAGES) != {"college", "bachelor"}:
        raise ValueError("大学生模拟批次的年龄与在读学历范围不一致")
    materials, records = {}, []
    for age in AGE_BANDS:
        version, items = form_for(age)
        if version != FORM_VERSION or len(items) != 30:
            raise ValueError("大学生模拟批次必须使用冻结的30题大学生问卷")
        tasks = {scenario: _snapshot(age, scenario) for scenario in SCENARIOS}
        materials[age] = {"form_version": version, "items": copy.deepcopy(items), "tasks": tasks}
        arm_ids = ["S"] * (PER_AGE // 2) + ["A"] * (PER_AGE // 2)
        _rng(ARM_SEED, age).shuffle(arm_ids)
        arm_indices = Counter()
        for index in range(PER_AGE):
            record_id = f"SYNTH-STUDENT-V2-{age}-{index + 1:03d}"
            rng = _rng(GENERATOR_SEED, record_id)
            proficiency = rng.uniform(0.32, 0.95)
            answers = {}
            for item in items:
                wrong = [value for value in range(len(item["options"])) if value != item["correct"]]
                answers[item["id"]] = item["correct"] if rng.random() < proficiency else rng.choice(wrong)
            raw, percent, band, anchor = score_form(age, answers, 10)
            scenario = SCENARIOS[index % len(SCENARIOS)]
            snapshot = tasks[scenario]
            decision = _sample_choice(snapshot["decision_options"], proficiency, rng)
            execution = _sample_choice(snapshot["execution_options"], proficiency, rng)
            start = BASE_TIME + timedelta(minutes=len(records) * 3)
            dialogue_seconds, decision_seconds = rng.randint(150, 540), rng.randint(35, 150)
            low_age, high_age = (int(value) for value in age.split("-"))
            # Arm is copied only after every answer, choice and random timing
            # has been sampled; no score/response parameter depends on it.
            row = {
                "id": record_id, "source_batch_id": BATCH_ID, "is_synthetic": 1,
                "data_origin": "synthetic", "record_kind": "synthetic", "run_mode": "demo", "is_demo": 1,
                "dataset_variant": GENERATOR_VERSION, "design_version": DESIGN_VERSION,
                "platform_analysis_eligible": 0, "student_confirmed": 1,
                "exact_age": rng.randint(low_age, high_age), "age_band": age,
                "education_stage": rng.choice(tuple(EDUCATION_STAGES)), "topic_familiarity": rng.randint(1, 5),
                "literacy_instrument_version": version, "literacy_answers": answers,
                "literacy_score": raw, "literacy_percent": percent, "literacy_band": band,
                "literacy_band_width": 10, "anchor_score": anchor,
                "task_id": snapshot["id"], "scenario_id": scenario,
                "decision_choice_id": decision["id"], "execution_choice_id": execution["id"],
                "score": decision["score"] + execution["score"],
                "decision_feasible": int(bool(decision.get("success")) and bool(execution.get("success"))),
                "decision_optimal": int(bool(decision.get("goal_met")) and bool(execution.get("success"))),
                "score_components": {**decision.get("score_components", {}), **execution.get("score_components", {})},
                "status": "completed", "timestamp_is_simulated": 1,
                "created_at": start.isoformat(timespec="seconds"),
                "decision_started_at": (start + timedelta(seconds=dialogue_seconds)).isoformat(timespec="seconds"),
                "ended_at": (start + timedelta(seconds=dialogue_seconds + decision_seconds)).isoformat(timespec="seconds"),
                "synthetic_pretest_time_seconds": None,
                "task_completion_seconds": dialogue_seconds + decision_seconds,
                "dialogue_elapsed_seconds": dialogue_seconds, "decision_elapsed_seconds": decision_seconds,
                "mean_reply_seconds": None, "actual_model_latency_ms": None,
                "fixture_conversation": 1, "fixture_render_version": RENDER_VERSION,
                "conversation_source": "authored_template_not_model_output",
            }
            row["group_id"] = arm_ids[index]
            row["interaction_condition"] = "standard" if row["group_id"] == "S" else "adaptive"
            row["fixture_arm_index"] = arm_indices[row["group_id"]]
            arm_indices[row["group_id"]] += 1
            row["messages"], row["model_events"] = _dialogue(row, snapshot)
            row["user_turns"] = sum(message["role"] == "user" for message in row["messages"])
            row["clarification_count"] = sum(message["action"] == "prompt_clarification" for message in row["messages"])
            row["evidence_reveals"] = sum(event["event_type"] == "evidence_reveal" for event in row["model_events"])
            row.update(ai_prompt_counts(row["messages"]))
            selected_text = f"参加方案：{decision['text']}\n办理方案：{execution['text']}"
            row["submission"] = selected_text
            row["process_alert_details"] = derive_process_alerts(
                {**row, "literacy_answers_json": answers, "task_snapshot_json": snapshot, "submission": selected_text}, row["messages"])
            row["flags"] = [item["code"] for item in row["process_alert_details"]]
            records.append(row)
    manifest = {
        "source_batch_id": BATCH_ID, "design_version": DESIGN_VERSION, "form_version": FORM_VERSION,
        "generator_version": GENERATOR_VERSION, "generator_seed": GENERATOR_SEED, "arm_seed": ARM_SEED,
        "render_version": RENDER_VERSION, "record_count": len(records),
        "age_counts": dict(Counter(row["age_band"] for row in records)),
        "arm_counts": dict(Counter(row["group_id"] for row in records)),
        "education_counts": dict(Counter(row["education_stage"] for row in records)),
        "age_range": [18, 25], "student_confirmed": True,
        "eligible_education_stages": list(EDUCATION_STAGES),
        "is_synthetic": True, "platform_analysis_eligible": False,
        "no_legacy_records_reused": True, "no_group_score_effect": True,
        "no_target_means_or_seed_retries": True, "timestamps_simulated": True,
        "actual_model_calls": 0, "actual_model_latency_ms": None,
        "generation_rule": "new answer vectors and decisions sampled before independent balanced arm assignment; fixed seed, no outcome calibration",
        "conversation_source": "authored_template_not_model_output",
        "evidence_policy": EVIDENCE_POLICY,
        "evidence_policy_is_simulated_assumption": True,
        "evidence_cards_per_record": {"S": 3, "A": 5},
        "evidence_card_ids": list(EVIDENCE_CARD_IDS),
        "pretest_duration_recorded": False,
        "process_quality_version": QUALITY_VERSION,
        "process_alert_record_count": sum(bool(row["process_alert_details"]) for row in records),
        "process_alert_code_counts": dict(Counter(detail["code"] for row in records for detail in row["process_alert_details"])),
        "materials_sha256": _digest(materials), "records_sha256": _digest(records), "notice": NOTICE,
    }
    return {"records": records, "manifest": manifest, "frozen_materials": materials}


def get_student_dataset() -> dict:
    """Return an independent copy so API callers cannot mutate the frozen batch."""
    return copy.deepcopy(_generated_dataset())


def _ensure_provenance_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    for name, definition in PROVENANCE_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {definition}")


def seed_student_showcase(conn: sqlite3.Connection, settings, model_config_id: str,
                          cohort_prompts: tuple[str, str], strategy: str = "prompt",
                          design_version: str = DESIGN_VERSION) -> dict:
    """Seed only a new student showcase DB; never commit or rewrite history.

    Call after init_db and seed_student_materials. Subsequent calls verify the
    exact fixture payload and runtime envelope, retaining any interactive rows.
    A changed batch/config or edited fixture raises instead of silently rewriting.
    """
    guard_student_showcase_database(settings, conn)
    if strategy != "prompt" or design_version != DESIGN_VERSION or len(cohort_prompts) != 2:
        raise ValueError("大学生展示批次必须使用university-v1双组prompt设计")
    if settings.literacy_band_width != 10:
        raise ValueError("大学生冻结模拟批次使用10分宽素养区间")
    dataset = get_student_dataset()
    manifest = dataset["manifest"]
    envelope = {"model_config_id": model_config_id, "cohort_prompts": list(cohort_prompts),
                "strategy": strategy, "assignment_seed": settings.assignment_seed,
                "model_temperature": settings.model_temperature,
                "model_thinking": settings.model_thinking, "model_max_tokens": settings.model_max_tokens}
    receipt = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='student_showcase_imports'").fetchone()
    if receipt:
        saved = conn.execute("SELECT manifest_json FROM student_showcase_imports WHERE batch_id=?", (BATCH_ID,)).fetchone()
        if saved and json.loads(saved[0]) != {**manifest, "runtime": envelope}:
            raise ValueError("大学生展示批次或运行配置已变化，请创建新数据库以保留原始记录")
    else:
        saved = None
    if not saved and conn.execute("SELECT 1 FROM sessions LIMIT 1").fetchone():
        raise ValueError("全新大学生合成批次不能写入已有会话的数据库；请归档旧库并使用新库")
    # Check every task before adding columns or writing any records.
    for age, frozen in dataset["frozen_materials"].items():
        row = conn.execute("SELECT * FROM tasks WHERE id=? AND active=1 AND is_demo=1", (frozen["tasks"][SCENARIOS[0]]["id"],)).fetchone()
        if row is None:
            raise ValueError(f"缺少当前大学生任务：{age}")
        active = task_dict(row)
        expected = task_for(age)
        for key in ("title", "instructions", "shared_context", "participant_facts", "reference_text", "rubric",
                    "slots", "keywords", "evidence_cards", "decision_options", "execution_options", "scenario_variants"):
            if active.get(key) != expected.get(key):
                raise ValueError(f"大学生任务与冻结材料不一致：{age}/{key}")
    _ensure_provenance_columns(conn)
    for source in dataset["records"]:
        values = {key: source[key] for key in (
            "id", "task_id", "group_id", "interaction_condition", "run_mode", "is_demo", "record_kind", "design_version",
            "literacy_score", "literacy_percent", "literacy_band", "literacy_band_width", "anchor_score",
            "literacy_instrument_version", "age_band", "exact_age", "education_stage", "student_confirmed", "topic_familiarity",
            "created_at", "decision_started_at", "ended_at", "decision_choice_id", "execution_choice_id",
            "decision_feasible", "decision_optimal", "score", "scenario_id", "is_synthetic", "data_origin", "dataset_variant",
            "source_batch_id", "timestamp_is_simulated", "platform_analysis_eligible", "fixture_conversation", "fixture_render_version",
            "synthetic_pretest_time_seconds")}
        snapshot = dataset["frozen_materials"][source["age_band"]]["tasks"][source["scenario_id"]]
        selected = next(option for option in snapshot["decision_options"] if option["id"] == source["decision_choice_id"])
        execution = next(option for option in snapshot["execution_options"] if option["id"] == source["execution_choice_id"])
        values.update({"expression_condition": "natural", "assignment_seed": settings.assignment_seed,
                       "model_name": "SYNTHETIC-AUTHORED", "model_base_url": "NOT-CALLED",
                       "model_temperature": settings.model_temperature, "model_thinking": settings.model_thinking,
                       "model_max_tokens": settings.model_max_tokens, "model_config_id": model_config_id,
                       "prompt_version": cohort_prompts[0 if source["group_id"] == "S" else 1],
                       "clarification_strategy": strategy, "guardian_consented": 0,
                       "literacy_answers_json": _json(source["literacy_answers"]), "task_snapshot_json": _json(snapshot),
                       "synthetic_source_json": _json(source),
                       "submission": f"参加方案：{selected['text']}\n办理方案：{execution['text']}",
                       "review_status": "valid", "reviewer": "student-synthetic-generator", "review_reason": "synthetic_fixture_only",
                       "reviewed_at": source["ended_at"], "score_note": "合成选择自动复算 student-decision-v1；非真实观测",
                       "flags_json": _json(source["flags"]),
                       "process_alert_details_json": _json(source["process_alert_details"]), "feedback_json": "{}"})
        messages = [(source["id"], item["role"], item["content"], item["action"], item["created_at"]) for item in source["messages"]]
        events = [(source["id"], item["event_type"], _json(item["detail"]), item["created_at"]) for item in source["model_events"]]
        history = [(source["id"], "valid", "synthetic_fixture_only", "student-synthetic-generator", source["ended_at"])]
        if saved:
            actual = conn.execute("SELECT * FROM sessions WHERE id=?", (source["id"],)).fetchone()
            if actual is None or any(actual[key] != value for key, value in values.items()):
                raise ValueError(f"大学生合成记录已修改或缺失：{source['id']}；不会自动覆盖")
            for table, fields, expected_rows in (
                ("messages", "session_id,role,content,action,created_at", messages),
                ("model_events", "session_id,event_type,detail_json,created_at", events),
                ("review_history", "session_id,status,reason,reviewer,created_at", history)):
                actual_rows = [tuple(item) for item in conn.execute(f"SELECT {fields} FROM {table} WHERE session_id=? ORDER BY id", (source["id"],))]
                if actual_rows != expected_rows:
                    raise ValueError(f"大学生合成记录{table}已修改：{source['id']}；不会自动覆盖")
            continue
        names = list(values)
        conn.execute(f"INSERT INTO sessions ({','.join(names)}) VALUES ({','.join('?' for _ in names)})", [values[name] for name in names])
        conn.executemany("INSERT INTO messages(session_id,role,content,action,created_at) VALUES (?,?,?,?,?)", messages)
        conn.executemany("INSERT INTO model_events(session_id,event_type,detail_json,created_at) VALUES (?,?,?,?)", events)
        conn.executemany("INSERT INTO review_history(session_id,status,reason,reviewer,created_at) VALUES (?,?,?,?,?)", history)
    if not saved:
        conn.execute("CREATE TABLE IF NOT EXISTS student_showcase_imports (batch_id TEXT PRIMARY KEY, manifest_json TEXT NOT NULL)")
        conn.execute("INSERT INTO student_showcase_imports VALUES (?,?)", (BATCH_ID, _json({**manifest, "runtime": envelope})))
    return {**manifest, "runtime": envelope}


def main() -> None:
    parser = argparse.ArgumentParser(description="Write the new independent university-only synthetic artifact batch; never opens a study DB")
    parser.add_argument("output_directory", type=Path)
    args = parser.parse_args()
    from .student_synthetic_exports import write_student_artifacts
    result = write_student_artifacts(args.output_directory)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
