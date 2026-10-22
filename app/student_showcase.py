"""Independent university-only synthetic records for the isolated showcase.

Nothing in this generator reads the legacy cross-age 600/126 rows. V3 preserves
the V2 student profiles using their original random keys and independently
varies the authored conversations. Seeds are fixed, never retried to obtain an
intervention effect. These are not model output or observed participant data.
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


BATCH_ID = "student-126-20261022-v3"
PARENT_BATCH_ID = "student-126-20261022-v2"
GENERATOR_VERSION = "student-independent-simulation-v3"
GENERATOR_SEED = "university-v1-independent-student-fixtures-20261022"
ARM_SEED = "university-v1-arm-assignment-20261022"
DIALOGUE_SEED = "student-dialogue-v3"
RENDER_VERSION = "student-authored-dialogue-v3"
RECORD_COUNT = 126
PER_AGE = 42
SCENARIOS = ("first_3", "first_1", "return_3", "return_1")
BASE_TIME = datetime(2026, 10, 22, 0, 0, tzinfo=timezone.utc)
EVIDENCE_CARD_IDS = ("B_SCOPE", "B_REPEAT", "B_SCHEDULE", "C_TIME", "B_MATERIALS")
EVIDENCE_COUNT_WEIGHTS = {"A": {2: 1, 3: 2, 4: 3, 5: 5, 6: 4, 7: 2, 8: 1},
                          "S": {1: 2, 2: 4, 3: 5, 4: 3, 5: 1}}
EVIDENCE_INTEREST_WEIGHTS = {"18-20": (2, 1, 3, 2, 2), "21-23": (3, 2, 2, 2, 1),
                             "24-25": (2, 3, 1, 2, 3)}
INITIAL_FACT_WEIGHTS = {"none": 5, "prior": 2, "weekly": 2, "both": 2}
PARENT_PROFILE_FIELDS = (
    "age_band", "exact_age", "education_stage", "student_confirmed", "topic_familiarity",
    "literacy_answers", "literacy_score", "literacy_percent", "literacy_band", "anchor_score",
    "group_id", "interaction_condition", "scenario_id", "decision_choice_id", "execution_choice_id",
    "decision_feasible", "decision_optimal", "score", "created_at", "decision_started_at", "ended_at",
)
EVIDENCE_POLICY = (
    "人工模拟假设：固定逐人seed生成A组每条2–8次、S组每条1–5次资料线索；"
    "按年龄兴趣权重选择来源，同一来源最多展示两次且不连续重复。"
    "起始已提供事实的完整度决定A组0–2次单事实澄清，S组由用户主动补充事实。"
    "每次线索均有对应模板回复和evidence_reveal事件；汇总由事件计算，允许两组逐人次数重叠。"
    "这是预设的过程展示差异，不是真实模型或受试者的观测效果；不据此调整答案或任务分数。"
)
NOTICE = (
    "全部为126条18–25岁在读专科或本科大学生合成记录，不是真实受试者数据。"
    "V3沿用V2合成个人资料与作答，仅重新编写对话过程。"
    "对话为人工模板，未调用模型；日期、时长和身份均为模拟值，不进入正式分析。"
    "A组更多资料线索为人工设定的模拟假设，不代表真实干预效果。"
)

PROVENANCE_COLUMNS = {
    "is_synthetic": "INTEGER NOT NULL DEFAULT 0",
    "data_origin": "TEXT", "dataset_variant": "TEXT", "source_batch_id": "TEXT",
    "parent_source_batch_id": "TEXT", "parent_record_id": "TEXT",
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


def _dialogue_plan(row: dict) -> dict:
    """Sample process assumptions once, separately from all personal outcomes.

    Keep these draws in this order. Wording uses a separate stream so adding an
    authored phrase cannot alter a participant's evidence path or initial facts.
    """
    rng = _rng(DIALOGUE_SEED, row["parent_record_id"])
    count_weights = EVIDENCE_COUNT_WEIGHTS[row["group_id"]]
    count = rng.choices(list(count_weights), weights=list(count_weights.values()))[0]
    weights = dict(zip(EVIDENCE_CARD_IDS, EVIDENCE_INTEREST_WEIGHTS[row["age_band"]]))
    chosen, seen = [], Counter()
    for _ in range(count):
        options = [key for key in EVIDENCE_CARD_IDS
                   if seen[key] < 2 and (not chosen or key != chosen[-1])]
        key = rng.choices(options, weights=[weights[key] for key in options])[0]
        chosen.append(key)
        seen[key] += 1
    initial = rng.choices(list(INITIAL_FACT_WEIGHTS), weights=list(INITIAL_FACT_WEIGHTS.values()))[0]
    return {"card_ids": chosen, "initial_facts": initial,
            "source_seed_key": row["parent_record_id"], "is_simulated_assumption": True}


def _dialogue(row: dict, snapshot: dict) -> tuple[list[dict], list[dict]]:
    start = datetime.fromisoformat(row["created_at"])
    stop = datetime.fromisoformat(row["decision_started_at"])
    by_id = {card["id"]: card for card in snapshot["evidence_cards"]}
    if not set(EVIDENCE_CARD_IDS) <= set(by_id):
        raise ValueError("V3资料展示假设缺少冻结的目标来源")
    plan = row["fixture_dialogue_plan"]
    rng = _rng(DIALOGUE_SEED + "-wording", row["parent_record_id"])
    weekly = int(row["scenario_id"].rsplit("_", 1)[1])
    prior = row["scenario_id"].startswith("return_")
    category, arrangement = ("登记乙" if prior else "登记甲"), ("现场安排" if weekly >= 2 else "弹性安排")
    facts = {
        "prior": rng.choice((
            f"陈禾{'曾获得过' if prior else '从未获得过'}本项目的活动名额。",
            f"补充经历：陈禾此前{'已经拿到过' if prior else '没有拿到过'}本项目活动名额。",
            f"关于原来的情况，陈禾{'有过' if prior else '没有过'}本项目活动名额。")),
        "weekly": rng.choice((
            f"陈禾每周最多能到场{weekly}次。",
            f"可到场时间是每周最多{weekly}次，陈禾可以完成纸质独立练习。",
            f"再说明时间：陈禾一周能来现场{weekly}次，独立练习也能完成。")),
    }
    questions = {
        "prior": ("陈禾此前是否曾获得过本项目的活动名额？",
                  "为了确定登记类别，陈禾以前拿到过本项目活动名额吗？",
                  "先核对一项经历：陈禾之前有没有获得过本项目活动名额？"),
        "weekly": ("陈禾每周最多能到场参加几次活动？",
                   "要判断参加安排，陈禾一周最多可以来现场几次？",
                   "还有一项时间条件：陈禾每周最多能参加几次现场活动？"),
    }
    acknowledgements = (
        f"两项个案事实已补齐：陈禾应选{category}和{arrangement}。接着按现行资料核对原问题中的办理要求。",
        f"这些条件够用了，登记类别应选{category}，参加方式选{arrangement}。再继续查办理期限和材料依据。",
        f"根据你提供的经历与时间，陈禾应选择{category}与{arrangement}。下面继续核对怎样按时办理。",
        f"可以据此判断了：选择{category}、{arrangement}符合这两项条件。接下来逐项核对有效期限和提交要求。",
    )
    initial = plan["initial_facts"]
    known = {"prior", "weekly"} if initial == "both" else set() if initial == "none" else {initial}
    question = rng.choice((
        "请帮我核对这个校园项目怎样选择登记类别、参加安排和办理方案。",
        "我想帮陈禾选校园活动的报名方案，应该怎样登记、参加和交材料？",
        "请根据本次资料说明陈禾应选什么类别和安排，以及怎样按时办理。",
        "能帮我梳理陈禾的校园活动报名方案吗？我需要核对类别、安排和办理要求。",
        "陈禾想多参加现场交流，请说明本次如何报名和选择参加方式。",
        "我在比较校园活动的两种登记和参加方式，陈禾该怎样选并完成办理？",
    ))
    if known:
        question += "\n" + "".join(facts[key] for key in ("prior", "weekly") if key in known)
    exchanges = []
    missing = [key for key in ("prior", "weekly") if key not in known]
    rng.shuffle(missing)
    if row["group_id"] == "A":
        # Ask only missing facts, one per turn, then resume the user's task.
        for key in missing:
            exchanges.append((question, rng.choice(questions[key]), "prompt_clarification", None))
            question = facts[key]
            known.add(key)
        exchanges.append((question, rng.choice(acknowledgements), "answer", None))
    else:
        # Standard dialogue never supplies proactive clarification questions.
        # Unknown case conditions stay conditional until the user offers them.
        if missing:
            clauses = [f"已知的经历对应{category}。" if "prior" in known else
                       "此前未获本项目名额须登记甲，已获过须登记乙。",
                       f"已知每周能到场{weekly}次，可选{arrangement}。" if "weekly" in known else
                       "现场安排每周须到场2次；弹性安排到场1次并完成1次独立练习。"]
            exchanges.append((question, "".join(clauses) + "未提供的个案条件暂按规则分别说明，不能默认满足。", "answer", None))
            # A spontaneous follow-up may offer both missing facts together or
            # in separate messages; neither is elicited by a standard-arm AI.
            if len(missing) == 2 and rng.choice((False, True)):
                key = missing.pop(0)
                exchanges.append((facts[key], "收到这项补充，其余未说明的条件仍按规则分情况理解。", "answer", None))
            question = "我再补充案例信息：" + "".join(facts[key] for key in missing)
            exchanges.append((question, rng.choice(acknowledgements), "answer", None))
        else:
            exchanges.append((question, rng.choice(acknowledgements), "answer", None))
    revealed = Counter()
    for index, card_id in enumerate(plan["card_ids"]):
        card = by_id[card_id]
        repeated = revealed[card_id] > 0
        if repeated:
            question = rng.choice((
                f"想再核对一次：{card['title']}，请把依据重列一下。",
                f"刚才的“{card['title']}”我还想确认，请再对照原文说明。",
                f"回到{card['title']}，我想复查是否漏看了条件。",
                f"最后再检查这项依据：{card['title']}。"))
            prefix = rng.choice(("再对照同一来源：", "可以，重新核对原文：", "这次复查仍依据以下材料："))
        else:
            question = rng.choice((
                f"请依据现行资料说明：{card['title']}。",
                f"关于{card['title']}，哪里有明确规定？",
                f"接下来想查{card['title']}，请给我相应依据。",
                f"我需要核实{card['title']}，请列出适用的资料。",
                f"帮我看看{card['title']}，不要只给结论。"))
            if row["group_id"] == "A" and rng.choice((False, False, True)):
                question = rng.choice(("这些条件下还有什么报名规定需要核对？", "请继续查原问题涉及的有效资料。",
                                       "接着把还需要确认的依据说明一下。"))
            prefix = rng.choice(("可以核对这条现行来源：", "对应依据如下：", "请看这项资料：", "这一点有明确材料："))
        answer = f"{prefix}\n资料 {card['id']}｜{card['title']}\n{card['body']}"
        if index == len(plan["card_ids"]) - 1:
            answer += rng.choice((
                f"\n结合已提供的案例事实，参加方案应为{category}和{arrangement}。",
                f"\n回到原问题，陈禾应选{category}，参加方式选{arrangement}。",
                f"\n按你补充的经历和时间，建议选择{category}与{arrangement}。"))
            answer += "办理应按更正后的11月18日17:00期限，到活动服务点提交报名表和参加计划。"
        exchanges.append((question, answer, "evidence", card_id))
        revealed[card_id] += 1
    if len(exchanges) > 12:
        raise ValueError("合成对话超过12轮，不能静默截断来源记录")
    messages, events = [], []
    provenance = {"is_synthetic": 1, "source": "student_authored_fixture", "source_batch_id": BATCH_ID,
                  "parent_source_batch_id": PARENT_BATCH_ID, "parent_record_id": row["parent_record_id"],
                  "conversation_source": "authored_template_not_model_output", "timestamp_is_simulated": 1,
                  "evidence_policy_is_simulated_assumption": True, "render_version": RENDER_VERSION}
    for index, (question, answer, action, card_id) in enumerate(exchanges):
        user_at = (start + (stop - start) * ((2 * index + 1) / (2 * len(exchanges) + 1))).isoformat(timespec="seconds")
        reply_at = (start + (stop - start) * ((2 * index + 2) / (2 * len(exchanges) + 1))).isoformat(timespec="seconds")
        messages.extend([{"role": "user", "content": question, "action": "fixture_user_message", "created_at": user_at},
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
            parent_id = f"SYNTH-STUDENT-V2-{age}-{index + 1:03d}"
            record_id = f"SYNTH-STUDENT-V3-{age}-{index + 1:03d}"
            # Preserve the original V2 personal random key. Public V3 IDs must
            # not resample answers, decisions, demographics, or task timing.
            rng = _rng(GENERATOR_SEED, parent_id)
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
                "parent_source_batch_id": PARENT_BATCH_ID, "parent_record_id": parent_id,
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
            row["parent_profile_sha256"] = _digest({key: row[key] for key in PARENT_PROFILE_FIELDS})
            row["fixture_dialogue_plan"] = _dialogue_plan(row)
            row["messages"], row["model_events"] = _dialogue(row, snapshot)
            row["user_turns"] = sum(message["role"] == "user" for message in row["messages"])
            row["clarification_count"] = sum(message["action"] == "prompt_clarification" for message in row["messages"])
            row["evidence_reveals"] = sum(event["event_type"] == "evidence_reveal" for event in row["model_events"])
            row["distinct_evidence_cards"] = len({event["detail"]["card_id"] for event in row["model_events"]
                                                  if event["event_type"] == "evidence_reveal"})
            row.update(ai_prompt_counts(row["messages"]))
            selected_text = f"参加方案：{decision['text']}\n办理方案：{execution['text']}"
            row["submission"] = selected_text
            row["process_alert_details"] = derive_process_alerts(
                {**row, "literacy_answers_json": answers, "task_snapshot_json": snapshot, "submission": selected_text}, row["messages"])
            row["flags"] = [item["code"] for item in row["process_alert_details"]]
            records.append(row)
    evidence_counts = Counter((row["age_band"], row["group_id"], event["detail"]["card_id"])
                              for row in records for event in row["model_events"]
                              if event["event_type"] == "evidence_reveal")
    parent_profiles = [{"parent_record_id": row["parent_record_id"],
                        **{key: row[key] for key in PARENT_PROFILE_FIELDS}} for row in records]
    manifest = {
        "source_batch_id": BATCH_ID, "design_version": DESIGN_VERSION, "form_version": FORM_VERSION,
        "generator_version": GENERATOR_VERSION, "generator_seed": GENERATOR_SEED, "arm_seed": ARM_SEED,
        "dialogue_seed": DIALOGUE_SEED, "parent_source_batch_id": PARENT_BATCH_ID,
        "parent_profile_fields": list(PARENT_PROFILE_FIELDS), "parent_profiles_sha256": _digest(parent_profiles),
        "parent_profiles_preserved": True, "personal_random_key_version": "SYNTH-STUDENT-V2",
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
        "generation_rule": "preserve V2 student profiles with original V2 random keys; independently vary authored V3 dialogue; fixed seeds, no outcome calibration or seed retries",
        "conversation_source": "authored_template_not_model_output",
        "evidence_policy": EVIDENCE_POLICY,
        "evidence_policy_is_simulated_assumption": True,
        "evidence_count_weights": EVIDENCE_COUNT_WEIGHTS,
        "evidence_interest_weights": {age: dict(zip(EVIDENCE_CARD_IDS, weights))
                                      for age, weights in EVIDENCE_INTEREST_WEIGHTS.items()},
        "evidence_reveals_per_record_ranges": {"A": [2, 8], "S": [1, 5]},
        "evidence_reveals_per_record_histograms": {
            group: dict(sorted(Counter(row["evidence_reveals"] for row in records if row["group_id"] == group).items()))
            for group in ("A", "S")},
        "evidence_reveal_totals": {group: sum(row["evidence_reveals"] for row in records if row["group_id"] == group)
                                   for group in ("A", "S")},
        "evidence_reveal_distribution": [{"age_band": age, "group_id": group, "card_id": card, "reveals": count}
                                         for (age, group, card), count in sorted(evidence_counts.items())],
        "source_repetition_rule": "at most 2 per card per record, never consecutive; each appearance has its own saved reply and event",
        "repeated_source_record_counts": {group: sum(row["evidence_reveals"] > row["distinct_evidence_cards"]
                                                    for row in records if row["group_id"] == group)
                                          for group in ("A", "S")},
        "initial_fact_weights": INITIAL_FACT_WEIGHTS,
        "initial_facts_distribution": {group: dict(Counter(row["fixture_dialogue_plan"]["initial_facts"]
                                                          for row in records if row["group_id"] == group))
                                       for group in ("A", "S")},
        "clarification_count_histograms": {group: dict(sorted(Counter(row["clarification_count"]
                                                                    for row in records if row["group_id"] == group).items()))
                                            for group in ("A", "S")},
        "user_turns_histograms": {group: dict(sorted(Counter(row["user_turns"] for row in records
                                                            if row["group_id"] == group).items())) for group in ("A", "S")},
        "evidence_card_ids": list(EVIDENCE_CARD_IDS),
        "pretest_duration_recorded": False,
        "process_quality_version": QUALITY_VERSION,
        "process_alert_record_count": sum(bool(row["process_alert_details"]) for row in records),
        "process_alert_code_counts": dict(Counter(detail["code"] for row in records for detail in row["process_alert_details"])),
        "materials_sha256": _digest(materials), "records_sha256": _digest(records), "notice": NOTICE,
    }
    # Histograms have numeric positions but JSON object keys are strings. Use
    # the on-disk shape here too so restart receipts compare exactly as stored.
    return {"records": records, "manifest": json.loads(_json(manifest)), "frozen_materials": materials}


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
            "source_batch_id", "parent_source_batch_id", "parent_record_id", "timestamp_is_simulated", "platform_analysis_eligible", "fixture_conversation", "fixture_render_version",
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
