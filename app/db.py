from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=15, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=15000")
    return db


def init_db(path: Path) -> None:
    with connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                version INTEGER NOT NULL DEFAULT 1,
                title TEXT NOT NULL,
                instructions TEXT NOT NULL,
                shared_context TEXT NOT NULL,
                participant_facts TEXT NOT NULL,
                reference_text TEXT NOT NULL,
                rubric TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '[]',
                decision_json TEXT NOT NULL DEFAULT '[]',
                slots_json TEXT NOT NULL,
                keywords_json TEXT NOT NULL,
                age_band TEXT,
                is_demo INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id),
                group_id TEXT NOT NULL,
                expression_condition TEXT NOT NULL,
                interaction_condition TEXT NOT NULL,
                run_mode TEXT NOT NULL DEFAULT 'demo',
                is_demo INTEGER NOT NULL,
                assignment_seed TEXT NOT NULL,
                model_name TEXT NOT NULL,
                model_temperature REAL NOT NULL,
                model_base_url TEXT NOT NULL,
                model_thinking TEXT,
                model_max_tokens INTEGER,
                model_config_id TEXT,
                prompt_version TEXT NOT NULL,
                task_snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                ended_at TEXT,
                decision_started_at TEXT,
                submission TEXT,
                decision_choice_id TEXT,
                flags_json TEXT NOT NULL DEFAULT '[]',
                review_status TEXT NOT NULL DEFAULT 'pending',
                review_code TEXT,
                review_reason TEXT,
                reviewer TEXT,
                reviewed_at TEXT,
                style_match TEXT NOT NULL DEFAULT 'unclear',
                feedback_json TEXT NOT NULL DEFAULT '{}',
                score REAL,
                score_note TEXT,
                design_version TEXT NOT NULL DEFAULT 'style-v1',
                literacy_score INTEGER,
                literacy_band TEXT,
                literacy_answers_json TEXT,
                literacy_instrument_version TEXT,
                clarification_strategy TEXT NOT NULL DEFAULT 'module',
                literacy_percent REAL,
                literacy_band_width INTEGER,
                age_band TEXT,
                education_stage TEXT,
                topic_familiarity INTEGER,
                anchor_score INTEGER,
                guardian_consented INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                action TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS model_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                event_type TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS start_requests (
                request_id TEXT PRIMARY KEY,
                payload_hash TEXT NOT NULL,
                session_id TEXT NOT NULL REFERENCES sessions(id)
            );
            CREATE TABLE IF NOT EXISTS message_requests (
                session_id TEXT NOT NULL REFERENCES sessions(id),
                request_id TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL,
                user_message_id INTEGER,
                started_at TEXT NOT NULL,
                response_json TEXT,
                PRIMARY KEY (session_id, request_id)
            );
            CREATE TABLE IF NOT EXISTS review_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                status TEXT NOT NULL,
                reason TEXT,
                reviewer TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_review ON sessions(is_demo, review_status, ended_at);
            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
            CREATE INDEX IF NOT EXISTS idx_model_events_session ON model_events(session_id, id);
            CREATE INDEX IF NOT EXISTS idx_review_history_session ON review_history(session_id, id);
            """
        )
        existing = {row["name"] for row in db.execute("PRAGMA table_info(sessions)")}
        for name, definition in {
            "review_code": "TEXT",
            "style_match": "TEXT NOT NULL DEFAULT 'unclear'",
            "feedback_json": "TEXT NOT NULL DEFAULT '{}'",
            "run_mode": "TEXT NOT NULL DEFAULT 'demo'",
            "design_version": "TEXT NOT NULL DEFAULT 'style-v1'",
            "literacy_score": "INTEGER",
            "literacy_band": "TEXT",
            "literacy_answers_json": "TEXT",
            "literacy_instrument_version": "TEXT",
            "clarification_strategy": "TEXT NOT NULL DEFAULT 'module'",
            "literacy_percent": "REAL",
            "literacy_band_width": "INTEGER",
            "model_thinking": "TEXT",
            "model_max_tokens": "INTEGER",
            "model_config_id": "TEXT",
            "decision_choice_id": "TEXT",
            "execution_choice_id": "TEXT",
            "decision_feasible": "INTEGER",
            "decision_optimal": "INTEGER",
            "decision_started_at": "TEXT",
            "age_band": "TEXT",
            "education_stage": "TEXT",
            "topic_familiarity": "INTEGER",
            "anchor_score": "INTEGER",
            "guardian_consented": "INTEGER NOT NULL DEFAULT 0",
            "pending_request_id": "TEXT",
            "pending_started_at": "TEXT",
            "pending_attempt_id": "TEXT",
            "scenario_id": "TEXT NOT NULL DEFAULT 'default'",
            "record_kind": "TEXT NOT NULL DEFAULT 'unclassified'",
        }.items():
            if name not in existing:
                db.execute(f"ALTER TABLE sessions ADD COLUMN {name} {definition}")
        task_columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
        for name in ("evidence_json", "decision_json", "age_band", "scenario_variants_json", "execution_options_json"):
            if name not in task_columns:
                definition = "TEXT" if name == "age_band" else "TEXT NOT NULL DEFAULT '[]'"
                db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")


def row_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def task_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["slots"] = json.loads(data.pop("slots_json"))
    data["keywords"] = json.loads(data.pop("keywords_json"))
    data["evidence_cards"] = json.loads(data.pop("evidence_json"))
    data["decision_options"] = json.loads(data.pop("decision_json"))
    data["scenario_variants"] = json.loads(data.pop("scenario_variants_json", "[]"))
    data["execution_options"] = json.loads(data.pop("execution_options_json", "[]"))
    return data
