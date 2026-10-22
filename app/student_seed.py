"""Idempotent current-study task seeding without rewriting historical records."""
from __future__ import annotations

import json
import sqlite3

from .age_forms import AGE_BANDS as LEGACY_AGE_BANDS
from .db import now_utc
from .student_materials import AGE_BANDS, task_for

_LEGACY_TASK_IDS = (
    "demo-xinghe-grant", "pilot-xinghe-learning-2026-v3",
    "pilot-xinghe-learning-2026-v4", "pilot-xinghe-learning-2026-v5",
    *(f"pilot-age-{band}-v{version}" for band in LEGACY_AGE_BANDS for version in range(1, 5)),
)


def seed_student_materials(conn: sqlite3.Connection, is_demo: bool = True) -> None:
    """Seed three demographic task IDs; the caller owns the transaction.

    Demo and live IDs are distinct, so a mode switch cannot change a task's
    identity or classification. INSERT OR IGNORE preserves any existing task
    edits/activation decisions. Only exact bundled predecessor IDs are retired;
    researcher-created tasks and every session snapshot remain untouched.
    """
    if type(is_demo) is not bool:
        raise ValueError("任务演示标记无效")
    conn.executemany("UPDATE tasks SET active=0 WHERE id=? AND is_demo=?",
                     ((task_id, int(is_demo)) for task_id in _LEGACY_TASK_IDS))
    for band in AGE_BANDS:
        task = task_for(band, is_demo=is_demo)
        already_active = conn.execute(
            "SELECT 1 FROM tasks WHERE age_band=? AND is_demo=? AND active=1 LIMIT 1",
            (band, int(is_demo)),
        ).fetchone() is not None
        conn.execute(
            """INSERT OR IGNORE INTO tasks
            (id,version,title,instructions,shared_context,participant_facts,reference_text,rubric,
             evidence_json,decision_json,slots_json,keywords_json,age_band,is_demo,active,
             created_at,scenario_variants_json,execution_options_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task["id"], 1, task["title"], task["instructions"], task["shared_context"],
             task["participant_facts"], task["reference_text"], task["rubric"],
             json.dumps(task["evidence_cards"], ensure_ascii=False),
             json.dumps(task["decision_options"], ensure_ascii=False),
             json.dumps(task["slots"], ensure_ascii=False),
             json.dumps(task["keywords"], ensure_ascii=False), band, int(is_demo),
             int(not already_active), now_utc(),
             json.dumps(task["scenario_variants"], ensure_ascii=False),
             json.dumps(task["execution_options"], ensure_ascii=False)),
        )
