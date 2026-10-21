from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import secrets
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, StrictInt, field_validator

from .clarify import decide_clarification
from .clean_exports import (DIALOGUE_COLUMNS, DIALOGUE_METADATA_KEYS, RECORD_COLUMNS, chinese_csv,
                            assign_record_alias, clean_record, initialize_record_aliases, order_by_record_id,
                            process_alert_details, record_id_map, utc_timestamp)
from .admin_auth import (ADMIN_PAGES, BROWSER_COOKIE, SESSION_COOKIE, SESSION_SECONDS, is_https, issue_session,
                         require_same_origin, valid_session)
from .age_forms import AGE_BANDS, form_for, public_form, score_form
from .config import ROOT, Settings
from .db import connect, init_db, now_utc, task_dict
from .evidence import (asks_for_decision, asks_for_overview, broad_reply, case_fact_question_reply, evidence_reply, fact_evidence_card,
                       greeting_reply, is_greeting, missing_slots, overview_reply, participant_fact_reply, route_question,
                       uncovered_question, unmatched_reply)
from .literacy import VERSION as LITERACY_VERSION, ITEMS as LITERACY_ITEMS, public_items, score_answers
from .item_analysis import analyze as analyze_pretest
from .instrument_review import inventory, render_review_html
from .outcomes import compare_conditions
from .record_reporting import (ai_prompt_counts, ai_prompt_totals, automatic_analysis_reasons,
                               cohort_exclusion_reasons, record_times)
from .model import (ANSWER_PROMPT_VERSION, CLARIFICATION_MARKER, DECISION_PROMPT_VERSION,
                    PROMPT_ONLY_VERSION, DemoProvider, LiveProvider, ModelError,
                    answer_system_prompt, prompt_only_system_prompt)
from .seed import seed_demo
from .synthetic_exports import register_synthetic_exports
from .showcase_seed import guard_showcase_database, seed_showcase


DESIGN_VERSION = "literacy-age-v5"
GROUPS = {"S": "standard", "A": "adaptive"}
security = HTTPBasic(auto_error=False)


async def optional_basic(request: Request) -> HTTPBasicCredentials | None:
    try:
        return await security(request)
    except HTTPException:
        return None


class AdminLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=1024)


class StartRequest(BaseModel):
    consent: bool
    age_band: str
    education_stage: str = Field(min_length=1, max_length=80)
    topic_familiarity: StrictInt = Field(ge=1, le=5)
    participant_assent: bool = False
    guardian_consent: bool = False
    minor_access_code: str = ""
    pretest_version: str
    pretest_answers: dict[str, StrictInt]
    access_code: str = ""
    request_id: str | None = Field(default=None, min_length=1, max_length=80)
    researcher_preview: bool = False

    @field_validator("education_stage")
    @classmethod
    def education_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("请选择教育阶段")
        return value.strip()


class MessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    request_id: str | None = Field(default=None, min_length=1, max_length=80)


class SourceSearchRequest(BaseModel):
    query: str = Field(default="", max_length=120)


class SubmitRequest(BaseModel):
    decision_choice_id: str | None = None
    execution_choice_id: str | None = None
    submission: str = Field(default="", max_length=6000)
    burden: int | None = Field(default=None, ge=1, le=5)
    helpfulness: int | None = Field(default=None, ge=1, le=5)


class Slot(BaseModel):
    key: str = Field(min_length=1, max_length=60)
    description: str = Field(min_length=4, max_length=300)
    question: str = Field(min_length=4, max_length=300)
    aliases: list[str] = Field(default_factory=list)
    evidence_card_id: str | None = None


class EvidenceCard(BaseModel):
    id: str = Field(min_length=2, max_length=60, pattern=r"^[A-Za-z0-9_]+$")
    title: str = Field(min_length=2, max_length=120)
    body: str = Field(min_length=10, max_length=1200)
    keywords: list[str] = Field(min_length=1, max_length=20)


class DecisionOption(BaseModel):
    id: str = Field(min_length=2, max_length=60, pattern=r"^[A-Za-z0-9_]+$")
    text: str = Field(min_length=10, max_length=600)
    score: float = Field(ge=0, le=100)
    score_components: dict[str, float] = Field(default_factory=dict)
    feasible: bool | None = None
    success: bool | None = None
    goal_met: bool | None = None


class ScenarioVariant(BaseModel):
    id: str = Field(min_length=1, max_length=60, pattern=r"^[A-Za-z0-9_]+$")
    participant_facts: str = Field(min_length=10, max_length=3000)
    decision_options: list[DecisionOption] = Field(min_length=5, max_length=5)
    scenario_values: dict[str, str | int | bool] = Field(default_factory=dict)


class TaskCreate(BaseModel):
    age_band: str
    title: str = Field(min_length=4, max_length=150)
    instructions: str = Field(min_length=10, max_length=2000)
    shared_context: str = Field(max_length=3000)
    participant_facts: str = Field(max_length=3000)
    reference_text: str = Field(min_length=30, max_length=15000)
    rubric: str = Field(min_length=10, max_length=5000)
    evidence_cards: list[EvidenceCard] = Field(default_factory=list, max_length=30)
    decision_options: list[DecisionOption] = Field(default_factory=list, max_length=5)
    execution_options: list[DecisionOption] = Field(default_factory=list, max_length=5)
    slots: list[Slot] = Field(default_factory=list, max_length=20)
    keywords: list[str] = Field(default_factory=list, max_length=30)
    scenario_variants: list[ScenarioVariant] = Field(default_factory=list, max_length=12)

    @field_validator("title", "instructions", "reference_text", "rubric")
    @classmethod
    def required_text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("任务必填文字不能为空")
        return value.strip()


class ReviewRequest(BaseModel):
    status: str
    code: str = ""
    reason: str = Field(default="", max_length=1000)
    reviewer: str = Field(min_length=1, max_length=80)

    @field_validator("reviewer")
    @classmethod
    def reviewer_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("请填写审核者")
        return value.strip()


class ScoreRequest(BaseModel):
    score: float = Field(ge=0, le=100)
    note: str = Field(default="", max_length=1000)


def _choice_token(session_id: str, option_id: str, assignment_seed: str) -> str:
    digest = hmac.new(assignment_seed.encode(),
                      f"choice:{session_id}:{option_id}".encode(), hashlib.sha256).hexdigest()
    return f"choice_{digest[:24]}"


def _apply_scenario(task: dict, scenario_id: str) -> dict:
    snapshot = json.loads(json.dumps(task, ensure_ascii=False))
    variants = snapshot.pop("scenario_variants", [])
    snapshot.pop("scenario_values", None)
    if variants:
        variant = next(item for item in variants if item["id"] == scenario_id)
        snapshot["participant_facts"] = variant["participant_facts"]
        snapshot["decision_options"] = variant["decision_options"]
    snapshot["scenario_id"] = scenario_id
    return snapshot


def _public_task(task: dict, session_id: str, assignment_seed: str, *, decision_stage: bool = False) -> dict:
    options = sorted(task.get("decision_options", []) if decision_stage else [], key=lambda option: hmac.new(
        assignment_seed.encode(), f"{session_id}:{option['id']}".encode(), hashlib.sha256
    ).digest())
    execution = sorted(task.get("execution_options", []) if decision_stage else [], key=lambda option: hmac.new(
        assignment_seed.encode(), f"{session_id}:execution:{option['id']}".encode(), hashlib.sha256
    ).digest())
    return {
        "id": task["id"], "title": task["title"],
        "instructions": task["instructions"],
        "shared_context": task["shared_context"],
        "participant_facts": task["participant_facts"],
        "is_demo": bool(task["is_demo"]),
        "decision_options": [
            {"id": _choice_token(session_id, option["id"], assignment_seed),
             "label": chr(65 + index), "text": option["text"]}
            for index, option in enumerate(options)
        ],
        "execution_options": [
            {"id": _choice_token(session_id, "execution:" + option["id"], assignment_seed),
             "label": chr(65 + index), "text": option["text"]}
            for index, option in enumerate(execution)
        ],
    }


def _session_status(row: dict, messages: list[dict], settings: Settings) -> dict:
    elapsed = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(row["created_at"])).total_seconds())
    completed = sum(message["role"] == "assistant" for message in messages)
    pending = bool(row.get("pending_request_id"))
    timed_out = elapsed >= settings.max_minutes * 60
    return {
        "completed_turns": completed,
        "user_turns": sum(message["role"] == "user" for message in messages),
        "pending_reply": pending,
        "pending_request_id": row.get("pending_request_id"),
        "can_enter_decision": not row["ended_at"] and not pending and (completed >= 2 or timed_out),
        "timed_out": timed_out,
        "max_turns": settings.max_turns,
        "max_minutes": settings.max_minutes,
        "remaining_seconds": max(0, int(settings.max_minutes * 60 - elapsed)),
    }


# Longer than the maximum two 90-second provider calls in a single turn.
PENDING_LEASE_SECONDS = 300


def _fail_pending_request(db, session_id: str, request_id: str, event: str,
                          *, expected_attempt_id: str | None = None, error_type: str | None = None) -> None:
    row = db.execute("SELECT pending_request_id,pending_attempt_id FROM sessions WHERE id=?", (session_id,)).fetchone()
    if (row is None or row["pending_request_id"] != request_id or
            (expected_attempt_id is not None and row["pending_attempt_id"] != expected_attempt_id)):
        return
    request = db.execute("SELECT user_message_id FROM message_requests WHERE session_id=? AND request_id=?",
                         (session_id, request_id)).fetchone()
    if request is not None and request["user_message_id"] is not None:
        db.execute("DELETE FROM messages WHERE id=? AND session_id=? AND role='user'",
                   (request["user_message_id"], session_id))
    db.execute("UPDATE message_requests SET status='failed',user_message_id=NULL WHERE session_id=? AND request_id=?",
               (session_id, request_id))
    db.execute("UPDATE sessions SET pending_request_id=NULL,pending_started_at=NULL,pending_attempt_id=NULL WHERE id=?", (session_id,))
    _add_event(db, session_id, event, {"request_id": request_id, **({"error_type": error_type} if error_type else {})})


def _recover_expired_request(db, session: dict) -> dict:
    if session.get("pending_request_id") and session.get("pending_started_at"):
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(session["pending_started_at"])).total_seconds()
        if elapsed >= PENDING_LEASE_SECONDS:
            _fail_pending_request(db, session["id"], session["pending_request_id"], "request_expired")
            session["pending_request_id"] = None
            session["pending_started_at"] = None
            session["pending_attempt_id"] = None
    return session


def _session_view(row: dict, task: dict, messages: list[dict], settings: Settings) -> dict:
    return {
        **_session_status(row, messages, settings),
        "id": row["id"], "group_id": row["group_id"],
        "design_version": row["design_version"],
        "run_mode": row["run_mode"],
        "clarification_strategy": row["clarification_strategy"],
        "prompt_version": row["prompt_version"],
        "literacy_instrument_version": row["literacy_instrument_version"],
        "age_band": row["age_band"],
        "literacy_band_width": row["literacy_band_width"],
        "model_config_id": row["model_config_id"],
        "interaction_condition": row["interaction_condition"],
        "is_demo": bool(row["is_demo"]),
        "task": _public_task(task, row["id"], row["assignment_seed"],
                             decision_stage=bool(row["decision_started_at"])), "messages": messages,
        "created_at": row["created_at"], "ended_at": row["ended_at"],
        "decision_stage": bool(row["decision_started_at"]),
        "submission": row["submission"],
        "decision_choice_id": row.get("decision_choice_id"),
        "execution_choice_id": row.get("execution_choice_id"),
    }


def _load_session(db, session_id: str):
    row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "找不到该实验会话")
    task = json.loads(row["task_snapshot_json"])
    return dict(row), task


def _messages(db, session_id: str) -> list[dict]:
    return [dict(row) for row in db.execute(
        "SELECT role,content,action,created_at FROM messages WHERE session_id=? ORDER BY id", (session_id,)
    )]


def _add_event(db, session_id: str, kind: str, detail: dict) -> None:
    db.execute(
        "INSERT INTO model_events(session_id,event_type,detail_json,created_at) VALUES (?,?,?,?)",
        (session_id, kind, json.dumps(detail, ensure_ascii=False), now_utc()),
    )


def _flags(messages: list[dict], submission: str, start: str, end: str, keywords: list[str],
           decision_choice_id: str | None = None, evidence_count: int = 0) -> list[str]:
    users = [m["content"].strip() for m in messages if m["role"] == "user"]
    combined = " ".join(users + [submission.strip()])
    flags = []
    if not users:
        flags.append("no_user_message")
    if not submission.strip() and not decision_choice_id:
        flags.append("empty_submission")
    if decision_choice_id and evidence_count == 0:
        flags.append("no_evidence_retrieved")
    if len("".join(users).strip()) < 12:
        flags.append("very_short_dialogue")
    elapsed = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    if elapsed < 45:
        flags.append("very_fast_completion")
    if len(users) >= 2 and len(set(users)) == 1:
        flags.append("repeated_user_messages")
    if len(combined) >= 10 and len(set(combined.replace(" ", ""))) <= 4:
        flags.append("low_character_diversity")
    if keywords and not any(word in combined for word in keywords):
        flags.append("low_keyword_overlap")
    return flags


def _csv_safe(value):
    # Spreadsheets may evaluate quoted CSV cells as formulas on open.
    if isinstance(value, str) and (value.lstrip().startswith(("=", "+", "-", "@"))
                                   or value.startswith(("\t", "\r", "\n"))):
        return "'" + value
    return value


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if settings.showcase_mode and (settings.study_mode != "demo"
            or settings.database_path.resolve().name != "showcase.sqlite3"):
        raise ValueError("演示成品必须使用 demo 模式与独立的 showcase.sqlite3")
    if settings.showcase_mode:
        guard_showcase_database(settings)
    dataset_root = settings.synthetic_dataset_root or ROOT / "synthetic-datasets"
    cohort_record_kind = "synthetic" if settings.showcase_mode else "participant"
    model_config_id = hashlib.sha256(json.dumps({
        "base_url": settings.model_base_url, "model": settings.model_name,
        "temperature": settings.model_temperature, "thinking": settings.model_thinking,
        "max_tokens": settings.model_max_tokens,
    }, sort_keys=True).encode()).hexdigest()[:16]
    init_db(settings.database_path)
    if settings.study_mode != "live":
        seed_demo(settings.database_path, settings.study_mode)
    app = FastAPI(title="LENS Lab 实验平台", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.provider = DemoProvider() if settings.study_mode == "demo" else LiveProvider(settings)
    strategy = "module" if settings.study_mode == "demo" else settings.clarification_strategy
    cohort_prompts = (ANSWER_PROMPT_VERSION, ANSWER_PROMPT_VERSION + "+" + (
        PROMPT_ONLY_VERSION if strategy == "prompt" else DECISION_PROMPT_VERSION))
    if settings.showcase_mode:
        seed_showcase(settings, model_config_id=model_config_id, cohort_prompts=cohort_prompts,
                      strategy=strategy, dataset_root=dataset_root, design_version=DESIGN_VERSION)
    with connect(settings.database_path) as db:
        db.execute("BEGIN IMMEDIATE")
        initialize_record_aliases(db)
    current_prompt_filter = "((interaction_condition='standard' AND prompt_version=?) OR (interaction_condition='adaptive' AND prompt_version=?))"

    form_versions = {age: form_for(age)[0] for age in AGE_BANDS}

    def active_task_ids(db):
        return {task['age_band']: task['id'] for task in db.execute(
            "SELECT id,age_band FROM tasks WHERE active=1 AND is_demo=?", (int(settings.study_mode != 'live'),))}

    def showcase_record_totals(db):
        if not settings.showcase_mode:
            return None
        totals = db.execute("SELECT COUNT(*) AS sessions, SUM(ended_at IS NOT NULL) AS submitted FROM sessions WHERE record_kind='synthetic'").fetchone()
        return {"sessions": totals["sessions"], "submitted": totals["submitted"] or 0,
                "unsubmitted": totals["sessions"] - (totals["submitted"] or 0)}

    def showcase_comparison(rows):
        if not settings.showcase_mode:
            return None
        cells = {}
        for row in rows:
            level = "高" if row["literacy_score"] >= 24 else "中" if row["literacy_score"] >= 15 else "低"
            cells.setdefault((level, row["group_id"]), []).append(row["score"])
        groups = []
        for level in ("高", "中", "低"):
            standard, adaptive = cells.get((level, "S"), []), cells.get((level, "A"), [])
            standard_mean = sum(standard) / len(standard) if standard else None
            adaptive_mean = sum(adaptive) / len(adaptive) if adaptive else None
            groups.append({"literacy_group": level, "standard_n": len(standard),
                           "standard_mean": standard_mean, "adaptive_n": len(adaptive),
                           "adaptive_mean": adaptive_mean,
                           "difference_points": adaptive_mean - standard_mean if standard and adaptive else None})
        gaps = {}
        for group in ("S", "A"):
            high, low = cells.get(("高", group)), cells.get(("低", group))
            gaps[group] = sum(high) / len(high) - sum(low) / len(low) if high and low else None
        return {"groups": groups, "gap_standard": gaps["S"], "gap_adaptive": gaps["A"],
                "cohort_session_ids": [row["id"] for row in rows], "analysis_n": len(rows)}

    def current_cohort_reasons(row, active):
        return cohort_exclusion_reasons(row, settings=settings, design_version=DESIGN_VERSION,
            strategy=strategy, model_config_id=model_config_id, active_task_ids=active,
            form_versions=form_versions,
            prompts=dict(zip(('standard', 'adaptive'), cohort_prompts)), record_kind=cohort_record_kind)

    def analysis_reasons(row, active):
        reasons = current_cohort_reasons(row, active)
        items = form_for(row["age_band"])[1] if row["age_band"] in AGE_BANDS else []
        return reasons + automatic_analysis_reasons(row, items)

    def current_analysis_rows(db, age_band=None):
        """One ordered ID cohort for questions, outcomes and every analysis export."""
        active = active_task_ids(db)
        current, completed = [], []
        rows = db.execute("SELECT * FROM sessions WHERE record_kind=? AND run_mode=? ORDER BY created_at,id",
                          (cohort_record_kind, settings.study_mode))
        for source in rows:
            row = dict(source)
            if (age_band is not None and row["age_band"] != age_band) or current_cohort_reasons(row, active):
                continue
            current.append(row)
            if not analysis_reasons(row, active):
                completed.append(row)
        return current, completed

    @app.middleware("http")
    async def private_admin_responses(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(("/admin", "/api/admin/")):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "same-origin"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
        return response

    def require_admin(request: Request, credentials: HTTPBasicCredentials | None = Depends(optional_basic)) -> str:
        if valid_session(request.cookies.get(SESSION_COOKIE), settings.admin_password):
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                require_same_origin(request)
            return "admin"
        # Once a browser uses web login, cached legacy Basic credentials must not
        # silently sign it back in after logout or cookie expiry. Scripts without
        # browser cookies retain their original Basic authentication behavior.
        browser_login = request.cookies.get(BROWSER_COOKIE) == "1" or bool(request.cookies.get(SESSION_COOKIE))
        valid = not browser_login and bool(credentials) and secrets.compare_digest(credentials.username.encode("utf-8"), b"admin") and secrets.compare_digest(
            credentials.password.encode("utf-8"), settings.admin_password.encode("utf-8")
        )
        if not valid:
            if request.url.path in ADMIN_PAGES:
                raise HTTPException(303, headers={"Location": "/admin/login?next=" + quote(request.url.path, safe="")})
            raise HTTPException(401, "需要管理员登录")
        return credentials.username

    register_synthetic_exports(app, require_admin, dataset_root)

    @app.get("/admin/login")
    def admin_login_page():
        return FileResponse(ROOT / "static" / "admin-login.html")

    @app.post("/api/admin/login")
    def admin_login(body: AdminLoginRequest, request: Request, response: Response):
        require_same_origin(request)
        valid_user = secrets.compare_digest(body.username.encode("utf-8"), b"admin")
        valid_password = secrets.compare_digest(body.password.encode("utf-8"), settings.admin_password.encode("utf-8"))
        if not (valid_user and valid_password):
            raise HTTPException(401, "用户名或密码错误")
        response.set_cookie(SESSION_COOKIE, issue_session(settings.admin_password), max_age=SESSION_SECONDS,
                            httponly=True, secure=is_https(request), samesite="strict", path="/")
        response.set_cookie(BROWSER_COOKIE, "1", httponly=True, secure=is_https(request), samesite="strict", path="/")
        return {"ok": True}

    @app.post("/api/admin/logout")
    def admin_logout(request: Request, response: Response):
        require_same_origin(request)
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=is_https(request), samesite="strict")
        response.set_cookie(BROWSER_COOKIE, "1", httponly=True, secure=is_https(request), samesite="strict", path="/")
        return {"ok": True}

    @app.get("/")
    def participant_page():
        return FileResponse(ROOT / "static" / "index.html")

    @app.get("/static/{name}")
    def static_file(name: str):
        if name not in {"app.js", "admin.js", "admin-login.js", "admin-synthetic.js", "style.css"}:
            raise HTTPException(404)
        return FileResponse(ROOT / "static" / name)

    @app.get("/admin")
    def admin_page(admin: str = Depends(require_admin)):
        return FileResponse(ROOT / "static" / "admin.html")

    @app.get("/admin/preview")
    def preview_page(admin: str = Depends(require_admin)):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("<body>", '<body data-researcher-preview="true">'))

    @app.get("/api/info")
    def info():
        with connect(settings.database_path) as db:
            count = db.execute("SELECT COUNT(*) FROM tasks WHERE active=1 AND is_demo=?", (int(settings.study_mode != "live"),)).fetchone()[0]
            showcase_totals = showcase_record_totals(db)
        local_provider = urlparse(settings.model_base_url).hostname in {"localhost", "127.0.0.1", "::1"}
        return {"study_mode": settings.study_mode, "showcase_mode": settings.showcase_mode,
                "showcase_totals": showcase_totals, "has_active_task": bool(count),
                "max_turns": settings.max_turns, "max_minutes": settings.max_minutes, "provider_local": local_provider,
                "clarification_strategy": strategy, "answer_prompt_version": ANSWER_PROMPT_VERSION,
                "design_version": DESIGN_VERSION, "literacy_instrument_version": LITERACY_VERSION,
                "age_bands": [{"id": key, **value} for key, value in AGE_BANDS.items()],
                "minor_recruitment_open": settings.study_mode == "demo" or bool(settings.minor_access_code),
                "minor_access_code_required": bool(settings.minor_access_code),
                "under14_recruitment_open": settings.study_mode == "demo" or bool(
                    settings.minor_access_code and settings.under14_privacy_notice_url.startswith("https://")),
                "under14_privacy_notice_url": settings.under14_privacy_notice_url,
                "literacy_band_width": settings.literacy_band_width,
                "model_config_id": model_config_id,
                "access_code_required": bool(settings.participant_access_code)}

    @app.get("/api/pretest")
    def pretest(age_band: str):
        try:
            return public_form(age_band)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/start")
    def start(body: StartRequest, request: Request, credentials: HTTPBasicCredentials | None = Depends(optional_basic)):
        if body.researcher_preview:
            require_admin(request, credentials)
        record_kind = "researcher_preview" if body.researcher_preview else cohort_record_kind
        if not body.consent:
            raise HTTPException(400, "只有明确同意后才能参加")
        if body.age_band not in AGE_BANDS:
            raise HTTPException(422, "年龄组无效")
        if AGE_BANDS[body.age_band]["family"] == "child":
            if not body.participant_assent or not body.guardian_consent:
                raise HTTPException(400, "未成年参与者需本人同意及监护人同意")
            if settings.study_mode != "demo" and not settings.minor_access_code:
                raise HTTPException(403, "未成年人预测试暂未开放；研究者需先配置监护人同意流程及专用邀请码")
            if (body.age_band == "12-14" and settings.study_mode != "demo"
                    and not settings.under14_privacy_notice_url.startswith("https://")):
                raise HTTPException(403, "12–14 岁组暂未开放；研究者需先发布未满十四周岁个人信息专门处理规则")
            if settings.minor_access_code and not secrets.compare_digest(
                    body.minor_access_code.encode("utf-8"), settings.minor_access_code.encode("utf-8")):
                raise HTTPException(403, "未成年人专用邀请码不正确")
        if settings.participant_access_code and not secrets.compare_digest(
                body.access_code.encode("utf-8"), settings.participant_access_code.encode("utf-8")):
            raise HTTPException(403, "参与邀请码不正确")
        form_version, _ = form_for(body.age_band)
        if body.pretest_version != form_version:
            raise HTTPException(409, "信息素养问卷版本已更新，请刷新页面重做")
        try:
            literacy_score, literacy_percent, literacy_band, anchor_score = score_form(
                body.age_band, body.pretest_answers, settings.literacy_band_width)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        sid = str(uuid.uuid4())
        request_id = body.request_id or str(uuid.uuid4())
        payload_hash = hashlib.sha256(json.dumps(body.model_dump(exclude={"request_id"}),
                                                sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        is_demo = int(settings.study_mode != "live")
        with connect(settings.database_path) as db:
            db.execute("BEGIN IMMEDIATE")
            prior_start = db.execute("SELECT * FROM start_requests WHERE request_id=?", (request_id,)).fetchone()
            if prior_start is not None:
                if not secrets.compare_digest(prior_start["payload_hash"], payload_hash):
                    raise HTTPException(409, "同一开始请求编号不能用于不同问卷")
                existing, existing_task = _load_session(db, prior_start["session_id"])
                existing = _recover_expired_request(db, existing)
                return {"session_id": existing["id"],
                        **_session_view(existing, existing_task, _messages(db, existing["id"]), settings)}
            task_row = db.execute(
                """SELECT t.*, COUNT(s.id) AS assigned FROM tasks t
                   LEFT JOIN sessions s ON s.task_id=t.id AND s.design_version=? AND s.clarification_strategy=?
                   AND s.literacy_instrument_version=? AND s.literacy_band_width=?
                   AND s.model_config_id=? AND s.run_mode=? AND s.prompt_version IN (?,?) AND s.record_kind=?
                   WHERE t.active=1 AND t.is_demo=? AND t.age_band=?
                   GROUP BY t.id ORDER BY assigned ASC,t.created_at ASC LIMIT 1""",
                (DESIGN_VERSION, strategy, form_version, settings.literacy_band_width,
                 model_config_id, settings.study_mode, *cohort_prompts, record_kind, is_demo, body.age_band),
            ).fetchone()
            if task_row is None:
                raise HTTPException(409, "当前没有可用任务，请管理员先创建任务")
            task = task_dict(task_row)
            if settings.study_mode != "demo" and (len(task["evidence_cards"]) < 3
                                                    or len(task["decision_options"]) != 5):
                raise HTTPException(409, "当前任务尚未配置对话线索和五个机器评分选项")
            assignment_rows = [dict(item) for item in db.execute(
                "SELECT scenario_id,group_id,literacy_band,COUNT(*) AS n FROM sessions WHERE task_id=? AND design_version=? AND clarification_strategy=? AND literacy_instrument_version=? AND literacy_band_width=? AND model_config_id=? AND run_mode=? AND prompt_version IN (?,?) AND record_kind=? GROUP BY scenario_id,group_id,literacy_band",
                (task["id"], DESIGN_VERSION, strategy, form_version, settings.literacy_band_width,
                 model_config_id, settings.study_mode, *cohort_prompts, record_kind),
            )]
            totals = {g: sum(item["n"] for item in assignment_rows if item["group_id"] == g) for g in GROUPS}
            eligible = [g for g in GROUPS if not settings.group_cap or totals[g] < settings.group_cap]
            if not eligible:
                raise HTTPException(409, "两组均已达到人数上限，请联系研究者")
            scenario_ids = [item["id"] for item in task.get("scenario_variants", [])] or ["default"]
            def scenario_order(scenario):
                relevant = [item for item in assignment_rows if item["scenario_id"] == scenario]
                return (sum(item["n"] for item in relevant if item["literacy_band"] == literacy_band),
                        sum(item["n"] for item in relevant),
                        hmac.new(settings.assignment_seed.encode(), f"{sid}:scenario:{scenario}".encode(), hashlib.sha256).digest())
            scenario_id = min(scenario_ids, key=scenario_order)
            counts = {g: {"band": 0, "all": 0} for g in GROUPS}
            for item in assignment_rows:
                if item["scenario_id"] == scenario_id:
                    counts[item["group_id"]]["all"] += item["n"]
                    if item["literacy_band"] == literacy_band:
                        counts[item["group_id"]]["band"] += item["n"]
            group = min(eligible, key=lambda g: (
                counts[g]["band"], counts[g]["all"], totals[g],
                hmac.new(settings.assignment_seed.encode(), f"{sid}:{g}".encode(), hashlib.sha256).digest()))
            task = _apply_scenario(task, scenario_id)
            interaction = GROUPS[group]
            prompt_version = ANSWER_PROMPT_VERSION
            if interaction == "adaptive":
                prompt_version += "+" + (PROMPT_ONLY_VERSION if strategy == "prompt" else DECISION_PROMPT_VERSION)
            db.execute(
                """INSERT INTO sessions
                (id,task_id,group_id,expression_condition,interaction_condition,run_mode,is_demo,assignment_seed,
                 model_name,model_temperature,model_base_url,prompt_version,task_snapshot_json,created_at,
                 design_version,literacy_score,literacy_band,literacy_answers_json,literacy_instrument_version,clarification_strategy,
                 literacy_percent,literacy_band_width,model_thinking,model_max_tokens,model_config_id,
                 age_band,education_stage,topic_familiarity,anchor_score,guardian_consented,scenario_id,record_kind)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sid, task["id"], group, "natural", interaction, settings.study_mode, is_demo,
                 settings.assignment_seed, settings.model_name if settings.study_mode != "demo" else "DEMO-RULE",
                 settings.model_temperature, settings.model_base_url if settings.study_mode != "demo" else "DEMO",
                 prompt_version, json.dumps(task, ensure_ascii=False), now_utc(), DESIGN_VERSION,
                 literacy_score, literacy_band, json.dumps(body.pretest_answers, ensure_ascii=False), form_version, strategy,
                 literacy_percent, settings.literacy_band_width, settings.model_thinking,
                 settings.model_max_tokens, model_config_id, body.age_band, body.education_stage.strip(),
                 body.topic_familiarity, anchor_score, int(body.guardian_consent), scenario_id, record_kind),
            )
            if settings.showcase_mode:
                db.execute("UPDATE sessions SET is_synthetic=1,data_origin='synthetic',dataset_variant='interactive_demo',platform_analysis_eligible=0 WHERE id=?", (sid,))
            assign_record_alias(db, sid)
            db.execute("INSERT INTO start_requests(request_id,payload_hash,session_id) VALUES (?,?,?)",
                       (request_id, payload_hash, sid))
            session, _ = _load_session(db, sid)
            result = {"session_id": sid, **_session_view(session, task, [], settings)}
        return result

    @app.get("/api/session/{session_id}")
    def get_session(session_id: str):
        with connect(settings.database_path) as db:
            db.execute("BEGIN IMMEDIATE")
            session, task = _load_session(db, session_id)
            session = _recover_expired_request(db, session)
            view = _session_view(session, task, _messages(db, session_id), settings)
            view["completed_request_ids"] = [row["request_id"] for row in db.execute(
                "SELECT request_id FROM message_requests WHERE session_id=? AND status='completed' ORDER BY user_message_id DESC LIMIT 20",
                (session_id,))]
            return view

    @app.delete("/api/session/{session_id}")
    def delete_session(session_id: str):
        with connect(settings.database_path) as db:
            db.execute("BEGIN IMMEDIATE")
            _load_session(db, session_id)
            db.execute("DELETE FROM start_requests WHERE session_id=?", (session_id,))
            db.execute("DELETE FROM message_requests WHERE session_id=?", (session_id,))
            db.execute("DELETE FROM review_history WHERE session_id=?", (session_id,))
            db.execute("DELETE FROM model_events WHERE session_id=?", (session_id,))
            db.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        return {"deleted": True}

    @app.post("/api/session/{session_id}/decision")
    def enter_decision(session_id: str):
        with connect(settings.database_path) as db:
            db.execute("BEGIN IMMEDIATE")
            session, task = _load_session(db, session_id)
            session = _recover_expired_request(db, session)
            if session.get("pending_request_id"):
                raise HTTPException(409, "AI 正在回复，请等待本轮完成")
            if session["ended_at"]:
                raise HTTPException(409, "任务已经提交")
            completed_turns = db.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id=? AND role='assistant'", (session_id,)
            ).fetchone()[0]
            if completed_turns < 2 and not _session_status(session, _messages(db, session_id), settings)["timed_out"]:
                raise HTTPException(409, "请先完成至少两轮 AI 对话")
            if not session["decision_started_at"]:
                db.execute("UPDATE sessions SET decision_started_at=? WHERE id=?", (now_utc(), session_id))
                _add_event(db, session_id, "decision_stage_started", {"completed_turns": completed_turns})
        public = _public_task(task, session_id, session["assignment_seed"], decision_stage=True)
        return {"decision_stage": True, "decision_options": public["decision_options"],
                "execution_options": public["execution_options"]}

    @app.post("/api/session/{session_id}/sources/search")
    def sources_search(session_id: str, body: SourceSearchRequest):
        raise HTTPException(410, "资料只通过 AI 对话逐项提供")

    @app.get("/api/session/{session_id}/sources/{source_id}")
    def source_open(session_id: str, source_id: str):
        raise HTTPException(410, "资料只通过 AI 对话逐项提供")

    @app.post("/api/session/{session_id}/message")
    async def message(session_id: str, body: MessageRequest):
        content = body.content.strip()
        if not content:
            raise HTTPException(422, "消息不能为空")
        request_id = body.request_id or str(uuid.uuid4())
        attempt_id = str(uuid.uuid4())
        with connect(settings.database_path) as db:
            db.execute("BEGIN IMMEDIATE")
            session, task = _load_session(db, session_id)
            session = _recover_expired_request(db, session)
            prior_request = db.execute("SELECT * FROM message_requests WHERE session_id=? AND request_id=?",
                                       (session_id, request_id)).fetchone()
            if prior_request is not None:
                if prior_request["content"] != content:
                    raise HTTPException(409, "同一请求编号不能用于不同消息")
                if prior_request["status"] == "completed":
                    return {**json.loads(prior_request["response_json"]),
                            **_session_status(session, _messages(db, session_id), settings)}
            if session.get("pending_request_id"):
                raise HTTPException(409, "AI 正在回复，请等待本轮完成")
            if session["ended_at"]:
                raise HTTPException(409, "任务已经提交")
            if session["decision_started_at"]:
                raise HTTPException(409, "已进入决策阶段，不能继续提问")
            expected_prompt = ANSWER_PROMPT_VERSION + (
                "+" + (PROMPT_ONLY_VERSION if strategy == "prompt" else DECISION_PROMPT_VERSION)
                if session["interaction_condition"] == "adaptive" else ""
            )
            if (session["run_mode"] != settings.study_mode or session["clarification_strategy"] != strategy
                    or session["prompt_version"] != expected_prompt
                    or session["model_config_id"] != model_config_id):
                raise HTTPException(409, "实验配置已更新；此会话不能继续与模型对话")
            state = _session_status(session, _messages(db, session_id), settings)
            if state["timed_out"]:
                raise HTTPException(409, "本次任务已超过时间上限，请进入最终决策")
            completed_turns = state["completed_turns"]
            if completed_turns >= settings.max_turns:
                raise HTTPException(409, "已达到最大对话轮次，请进入最终决策")
            started_at = now_utc()
            cursor = db.execute("INSERT INTO messages(session_id,role,content,action,created_at) VALUES (?,?,?,?,?)",
                                (session_id, "user", content, "user_message", started_at))
            db.execute("""INSERT INTO message_requests(session_id,request_id,content,status,user_message_id,started_at)
                       VALUES (?,?,?,'processing',?,?)
                       ON CONFLICT(session_id,request_id) DO UPDATE SET status='processing',
                       user_message_id=excluded.user_message_id,started_at=excluded.started_at,response_json=NULL""",
                       (session_id, request_id, content, cursor.lastrowid, started_at))
            db.execute("UPDATE sessions SET pending_request_id=?,pending_started_at=?,pending_attempt_id=? WHERE id=?",
                       (request_id, started_at, attempt_id, session_id))
            history = _messages(db, session_id)
        try:
            action = "answer"
            decision = None
            route = None
            evidence_id = None
            use_prompt = session["interaction_condition"] == "adaptive" and session["clarification_strategy"] == "prompt"
            prompt_count = sum(m["action"] == "prompt_clarification" for m in history)
            response_started = time.perf_counter()
            if task.get("evidence_cards"):
                fact_question_reply = case_fact_question_reply(task, history)
                uncovered = uncovered_question(task, history)
                if fact_question_reply is not None:
                    answer = fact_question_reply
                    action = "guidance"
                elif uncovered:
                    route = uncovered
                    answer = unmatched_reply(route)
                    action = "guidance"
                elif is_greeting(content):
                    answer = greeting_reply()
                    action = "guidance"
                elif (fact_reply := participant_fact_reply(task, history)) is not None:
                    answer = fact_reply
                    action = "guidance"
                    card = fact_evidence_card(task, history)
                    if card:
                        answer += "\n\n" + evidence_reply(card)
                        evidence_id = card["id"]
                        action = "evidence"
                elif asks_for_overview(content):
                    answer = overview_reply()
                    action = "task_guidance"
                else:
                    broad = asks_for_decision(content)
                    if broad and session["interaction_condition"] == "adaptive" and session["clarification_strategy"] == "module":
                        module_count = sum(m["action"] == "module_clarification" for m in history)
                        if module_count < 2:
                            decision = await decide_clarification(app.state.provider, task, history, is_demo=session["run_mode"] == "demo")
                            missing_keys = {slot["key"] for slot in missing_slots(task, history)}
                            if decision["need_clarification"] and decision["slot_key"] in missing_keys:
                                slot = next(s for s in task["slots"] if s["key"] == decision["slot_key"])
                                answer = slot["question"]
                                action = "module_clarification"
                    if action == "answer":
                        with connect(settings.database_path) as db:
                            revealed_ids = {json.loads(row["detail_json"])["card_id"] for row in db.execute(
                                "SELECT detail_json FROM model_events WHERE session_id=? AND event_type='evidence_reveal'",
                                (session_id,),
                            )}
                        route = await route_question(app.state.provider, task, history,
                                                     adaptive=use_prompt and prompt_count < 2,
                                                     revealed_ids=revealed_ids)
                        if route.get("status") == "unavailable":
                            raise ModelError("资料路由暂时不可用")
                        if broad:
                            missing = {slot["key"]: slot for slot in missing_slots(task, history)}
                            slot_key = route["clarify_slot"] if use_prompt and prompt_count < 2 else None
                            if slot_key in missing:
                                answer = missing[slot_key]["question"]
                                action = "prompt_clarification"
                            else:
                                answer = broad_reply()
                                action = "task_guidance"
                        else:
                            card = next((item for item in task["evidence_cards"]
                                         if item["id"] == route["card_id"]), None)
                            if card:
                                evidence_id = card["id"]
                                answer = evidence_reply(card)
                                action = "evidence"
                            else:
                                answer = unmatched_reply(route)
                                action = "guidance"
            else:
                if session["interaction_condition"] == "adaptive" and session["clarification_strategy"] == "module":
                    module_count = sum(m["action"] == "module_clarification" for m in history)
                    if module_count < 2:
                        decision = await decide_clarification(app.state.provider, task, history, is_demo=session["run_mode"] == "demo")
                    else:
                        decision = {"need_clarification": False, "slot_key": None,
                                    "reason": "已达到模块追问上限", "source": "clarification_cap"}
                    if decision["need_clarification"]:
                        slot = next(s for s in task["slots"] if s["key"] == decision["slot_key"])
                        answer = slot["question"]
                        action = "module_clarification"
                if action == "answer":
                    chat_history = [{"role": m["role"], "content": m["content"]} for m in history]
                    system_prompt = prompt_only_system_prompt(task) if use_prompt and prompt_count < 2 else answer_system_prompt(task)
                    answer = await app.state.provider.complete(
                        [{"role": "system", "content": system_prompt}] + chat_history
                    )
                    if use_prompt and answer.lstrip().startswith(CLARIFICATION_MARKER):
                        answer = answer.lstrip()[len(CLARIFICATION_MARKER):].strip()
                        action = "prompt_clarification"
                    if not answer:
                        answer = "请补充你希望我判断的具体情况。"
            with connect(settings.database_path) as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
                if current is None:
                    raise HTTPException(410, "本次实验已退出，回复不再保存")
                if current["pending_request_id"] != request_id or current["pending_attempt_id"] != attempt_id:
                    raise HTTPException(409, "本轮回复已失效，请刷新后重试")
                if current["ended_at"] or current["decision_started_at"]:
                    raise HTTPException(409, "会话已进入最终决策，回复不再保存")
                _add_event(db, session_id, "reply_latency", {
                    "latency_ms": round((time.perf_counter() - response_started) * 1000), "action": action,
                    "model_config_id": model_config_id,
                })
                if decision is not None:
                    _add_event(db, session_id, "clarification_decision", decision)
                if route is not None:
                    _add_event(db, session_id, "evidence_route", route)
                if evidence_id is not None:
                    _add_event(db, session_id, "evidence_reveal", {"card_id": evidence_id})
                if session["interaction_condition"] == "adaptive" and session["clarification_strategy"] == "prompt":
                    _add_event(db, session_id, "prompt_strategy", {"version": PROMPT_ONLY_VERSION,
                               "used_extra_prompt": prompt_count < 2, "detected_clarification": action == "prompt_clarification"})
                db.execute("INSERT INTO messages(session_id,role,content,action,created_at) VALUES (?,?,?,?,?)",
                           (session_id, "assistant", answer, action, now_utc()))
                db.execute("UPDATE sessions SET pending_request_id=NULL,pending_started_at=NULL,pending_attempt_id=NULL WHERE id=?", (session_id,))
                current = dict(current)
                current["pending_request_id"] = None
                current["pending_started_at"] = None
                response = {"content": answer, "action": action, "request_id": request_id,
                            "turns_remaining": max(0, settings.max_turns - completed_turns - 1),
                            **_session_status(current, _messages(db, session_id), settings)}
                db.execute("UPDATE message_requests SET status='completed',response_json=? WHERE session_id=? AND request_id=?",
                           (json.dumps(response, ensure_ascii=False), session_id, request_id))
            return response
        except BaseException as exc:
            with connect(settings.database_path) as db:
                db.execute("BEGIN IMMEDIATE")
                _fail_pending_request(db, session_id, request_id, "provider_error" if isinstance(exc, ModelError) else "request_failed",
                                      expected_attempt_id=attempt_id, error_type=type(exc).__name__)
            if isinstance(exc, ModelError):
                raise HTTPException(503, "模型暂时不可用，本轮不计入次数，请重试") from exc
            raise

    @app.post("/api/session/{session_id}/submit")
    def submit(session_id: str, body: SubmitRequest):
        with connect(settings.database_path) as db:
            db.execute("BEGIN IMMEDIATE")
            session, task = _load_session(db, session_id)
            if session["ended_at"]:
                raise HTTPException(409, "已经提交过任务")
            options = task.get("decision_options", [])
            session = _recover_expired_request(db, session)
            if session.get("pending_request_id"):
                raise HTTPException(409, "AI 正在回复，请等待本轮完成")
            if options and not session["decision_started_at"]:
                raise HTTPException(409, "请先完成资料搜集并进入决策阶段")
            selected = next((option for option in options
                             if _choice_token(session_id, option["id"], session["assignment_seed"])
                             == body.decision_choice_id), None)
            if options and selected is None:
                raise HTTPException(422, "请先选择一个最终行动方案")
            execution_options = task.get("execution_options", [])
            execution = next((option for option in execution_options
                if _choice_token(session_id, "execution:" + option["id"], session["assignment_seed"])
                == body.execution_choice_id), None)
            if execution_options and execution is None:
                raise HTTPException(422, "请同时选择参加方案和办理方案")
            quality = (selected["score"] + (execution["score"] if execution else 0)) if selected else None
            feasible = (int(bool(selected.get("success")) and bool(execution.get("success")))
                        if execution and selected.get("success") is not None and execution.get("success") is not None else None)
            optimal = (int(bool(selected.get("goal_met")) and bool(execution.get("success")))
                       if execution and selected.get("goal_met") is not None and execution.get("success") is not None else None)
            evidence_count = db.execute(
                "SELECT COUNT(DISTINCT json_extract(detail_json,'$.card_id')) FROM model_events WHERE session_id=? AND event_type='evidence_reveal'",
                (session_id,),
            ).fetchone()[0]
            end = now_utc()
            flags = _flags(_messages(db, session_id), body.submission, session["created_at"], end,
                           task["keywords"], body.decision_choice_id, evidence_count)
            events = [(row["event_type"], json.loads(row["detail_json"])) for row in db.execute(
                "SELECT event_type,detail_json FROM model_events WHERE session_id=?", (session_id,))]
            if any(kind in {"provider_error", "request_expired", "request_failed"} for kind, _ in events):
                flags.append("technical_interruption")
            if any((kind == "evidence_route" and detail.get("source") == "keyword_fallback") or
                   (kind == "clarification_decision" and detail.get("source") == "decision_error")
                   for kind, detail in events):
                flags.append("model_fallback_used")
            feedback = {"burden": body.burden, "helpfulness": body.helpfulness}
            db.execute("""UPDATE sessions SET ended_at=?,submission=?,decision_choice_id=?,execution_choice_id=?,decision_feasible=?,decision_optimal=?,score=?,score_note=?,
                       flags_json=?,feedback_json=? WHERE id=?""",
                       (end, body.submission.strip(), selected["id"] if selected else None,
                        execution["id"] if execution else None, feasible, optimal, quality,
                        ("机器评分 decision-age-v4" if selected.get("score_components") else "机器评分 decision-v1") if selected else None,
                        json.dumps(flags, ensure_ascii=False), json.dumps(feedback, ensure_ascii=False), session_id))
        return {"submitted": True, "is_demo": bool(session["is_demo"]), "review_status": "pending"}

    @app.get("/api/admin/summary")
    def summary(age_band: str = "18-24", admin: str = Depends(require_admin)):
        if age_band not in AGE_BANDS:
            raise HTTPException(422, "年龄组无效")
        form_version, _ = form_for(age_band)
        with connect(settings.database_path) as db:
            aliases = record_id_map(db)
            active_task = db.execute("SELECT * FROM tasks WHERE active=1 AND is_demo=? AND age_band=? LIMIT 1",
                                     (int(settings.study_mode != "live"), age_band)).fetchone()
            task_id = active_task["id"] if active_task else ""
            all_current, all_completed = current_analysis_rows(db)
            current = [row for row in all_current if row["age_band"] == age_band]
            scored = [row for row in all_completed if row["age_band"] == age_band]
            groups = []
            for group in sorted({row["group_id"] for row in current}):
                assigned = [row for row in current if row["group_id"] == group]
                included = [row for row in scored if row["group_id"] == group]
                mean_score = sum(row["score"] for row in included) / len(included) if included else None
                groups.append({"group_id": group, "sessions": len(assigned),
                               "submitted": sum(bool(row["ended_at"]) for row in assigned),
                               "completed_n": len(included), "valid": len(included),
                               "mean_score": mean_score, "valid_mean_score": mean_score})
            strata_counts = Counter((row["literacy_band"], row["group_id"]) for row in scored)
            strata = [{"literacy_band": band, "group_id": group, "sessions": count}
                      for (band, group), count in sorted(strata_counts.items())]
            band_outcomes = []
            for band, group in sorted(strata_counts):
                subset = [row for row in scored if (row["literacy_band"], row["group_id"]) == (band, group)]
                band_outcomes.append({"literacy_band": band, "group_id": group, "valid_n": len(subset),
                                      "mean_score": round(sum(row["score"] for row in subset) / len(subset), 2)})
            literacy_counts = Counter((row["literacy_score"], row["group_id"]) for row in scored)
            literacy_distribution = [{"literacy_score": score, "group_id": group, "sessions": count}
                                     for (score, group), count in sorted(literacy_counts.items())]
            scored_groups = {row["id"]: row["group_id"] for row in scored}
            latencies, evidence_counts = [], Counter()
            if scored_groups:
                # Source counts and latency use the same completed IDs as outcomes.
                for event in db.execute("SELECT session_id,event_type,detail_json FROM model_events ORDER BY id"):
                    if event["session_id"] not in scored_groups:
                        continue
                    try:
                        detail = json.loads(event["detail_json"])
                        if event["event_type"] == "reply_latency":
                            latencies.append(detail["latency_ms"])
                        elif event["event_type"] == "evidence_reveal":
                            evidence_counts[(detail["card_id"], scored_groups[event["session_id"]])] += 1
                    except (TypeError, ValueError, KeyError):
                        continue
            showcase_totals = showcase_record_totals(db)
            all_record_comparison = showcase_comparison(all_completed)
            if all_record_comparison is not None:
                all_record_comparison["cohort_session_ids"] = sorted(
                    (aliases[row["id"]] for row in all_completed), key=lambda value: int(value[1:]))
            messages_by_session = {row["id"]: [] for row in all_completed}
            for message in db.execute("SELECT session_id,role,action FROM messages ORDER BY id"):
                if message["session_id"] in messages_by_session:
                    messages_by_session[message["session_id"]].append(message)
            prompt_counts_by_session = {sid: ai_prompt_counts(messages)
                                        for sid, messages in messages_by_session.items()}
            age_prompt_totals = ai_prompt_totals(prompt_counts_by_session[row["id"]] for row in scored)
            all_prompt_totals = ai_prompt_totals(prompt_counts_by_session.values())
        score_distribution = [
            {"start": start, "end": min(start + 20, 100), "group_id": group,
             "sessions": sum(1 for row in scored if row["group_id"] == group
                             and start <= row["score"] < start + 20 + (start == 80))}
            for start in range(0, 100, 20) for group in GROUPS
        ]
        comparison = compare_conditions(scored,
            [item["id"] for item in task_dict(active_task).get("scenario_variants", [])] if active_task else [])
        cohort_ids = sorted((aliases[row["id"]] for row in scored), key=lambda value: int(value[1:]))
        comparison["cohort_session_ids"] = cohort_ids
        return {"study_mode": settings.study_mode, "clarification_strategy": strategy,
                "showcase_mode": settings.showcase_mode, "showcase_totals": showcase_totals,
                "showcase_comparison": all_record_comparison,
                "age_band": age_band,
                "active_task_id": task_id, "active_task_title": active_task["title"] if active_task else None,
                "groups": groups, "strata": strata, "band_width": settings.literacy_band_width,
                "band_outcomes": band_outcomes, "condition_comparison": comparison,
                "instrument_version": form_version, "pending_reviews": 0,
                "analysis_n": len(scored), "completed_n": len(scored), "cohort_session_ids": cohort_ids,
                "ai_prompt_totals": age_prompt_totals, "ai_prompt_totals_all_ages": all_prompt_totals,
                "literacy_distribution": literacy_distribution, "score_distribution": score_distribution,
                "evidence_reveal_distribution": [
                    {"card_id": card_id, "group_id": group_id, "reveals": count}
                    for (card_id, group_id), count in sorted(evidence_counts.items())],
                "mean_reply_seconds": round(sum(latencies) / len(latencies) / 1000, 2) if latencies else None}

    @app.get("/api/admin/tasks")
    def admin_tasks(admin: str = Depends(require_admin)):
        with connect(settings.database_path) as db:
            return [task_dict(row) for row in db.execute("SELECT * FROM tasks ORDER BY created_at DESC")]

    @app.get("/admin/instruments", response_class=HTMLResponse)
    def instrument_review(admin: str = Depends(require_admin)):
        return HTMLResponse(render_review_html(), headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})

    @app.get("/api/admin/instruments")
    def instrument_inventory(admin: str = Depends(require_admin)):
        return Response(json.dumps(inventory(), ensure_ascii=False), media_type="application/json",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/admin/pretest-analysis")
    def pretest_analysis(age_band: str = "18-24", admin: str = Depends(require_admin)):
        try:
            form_version, items = form_for(age_band)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        with connect(settings.database_path) as db:
            aliases = record_id_map(db)
            current, completed = current_analysis_rows(db, age_band)
            responses = [json.loads(row["literacy_answers_json"]) for row in completed]
            exclusions = Counter(reason for row in current for reason in
                                 automatic_analysis_reasons(row, items))
        report = analyze_pretest(items, responses, form_version)
        report.update({"cohort_session_ids": sorted((aliases[row["id"]] for row in completed), key=lambda value: int(value[1:])),
                       "analysis_n": len(completed), "cohort_input_count": len(current),
                       "cohort_excluded_count": len(current) - len(completed),
                       "cohort_exclusion_reasons": dict(exclusions)})
        report["source_note"] = ("与本年龄的素养分层和澄清结果使用同一批当前配置、已提交且前测30题合法完整的记录；"
                                 "不需要人工审核，不按任务分数高低筛选。未完成及不符合当前配置的记录保留在原始导出。")
        return report

    @app.post("/api/admin/tasks")
    def create_task(body: TaskCreate, admin: str = Depends(require_admin)):
        task_id = str(uuid.uuid4())
        if body.age_band not in AGE_BANDS:
            raise HTTPException(422, "年龄组无效")
        if len({s.key for s in body.slots}) != len(body.slots):
            raise HTTPException(422, "关键条件 key 不能重复")
        if len(body.evidence_cards) < 3 or len({card.id for card in body.evidence_cards}) != len(body.evidence_cards):
            raise HTTPException(422, "请配置至少三条、编号不重复的对话线索")
        if len(body.decision_options) != 5 or len({option.id for option in body.decision_options}) != 5:
            raise HTTPException(422, "请配置五个编号不重复的决策选项")
        if body.execution_options and (len(body.execution_options) != 5 or len({option.id for option in body.execution_options}) != 5):
            raise HTTPException(422, "办理方案须为五个编号不重复的选项")
        cards = {card.id for card in body.evidence_cards}
        if any(slot.evidence_card_id and slot.evidence_card_id not in cards for slot in body.slots):
            raise HTTPException(422, "条件关联的资料编号不存在")
        option_texts = {option.id: option.text for option in body.decision_options}
        if len({variant.id for variant in body.scenario_variants}) != len(body.scenario_variants):
            raise HTTPException(422, "情境编号不能重复")
        all_options = list(body.decision_options) + list(body.execution_options)
        for variant in body.scenario_variants:
            if len({option.id for option in variant.decision_options}) != 5 or {
                option.id: option.text for option in variant.decision_options
            } != option_texts:
                raise HTTPException(422, "同一任务各情境必须使用相同的五个选项编号与文字")
            all_options.extend(variant.decision_options)
        execution_max = max((option.score for option in body.execution_options), default=0)
        if any(max(option.score for option in options) + execution_max > 100 for options in
               [body.decision_options] + [variant.decision_options for variant in body.scenario_variants]):
            raise HTTPException(422, "参加方案与办理方案的最高总分不能超过100")
        if any(option.score_components and (
            any(not 0 <= value <= 100 for value in option.score_components.values())
            or abs(sum(option.score_components.values()) - option.score) > 0.000001
        ) for option in all_options):
            raise HTTPException(422, "选项维度分必须在0–100内，且合计等于选项总分")
        with connect(settings.database_path) as db:
            db.execute("UPDATE tasks SET active=0 WHERE is_demo=? AND age_band=?",
                       (int(settings.study_mode != "live"), body.age_band))
            db.execute(
                """INSERT INTO tasks
                (id,version,title,instructions,shared_context,participant_facts,reference_text,rubric,
                 evidence_json,decision_json,slots_json,keywords_json,age_band,is_demo,active,created_at,scenario_variants_json,execution_options_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, 1, body.title.strip(), body.instructions.strip(), body.shared_context.strip(),
                 body.participant_facts.strip(), body.reference_text.strip(), body.rubric.strip(),
                 json.dumps([card.model_dump() for card in body.evidence_cards], ensure_ascii=False),
                 json.dumps([option.model_dump() for option in body.decision_options], ensure_ascii=False),
                 json.dumps([s.model_dump() for s in body.slots], ensure_ascii=False),
                 json.dumps(body.keywords, ensure_ascii=False), body.age_band,
                 int(settings.study_mode != "live"), 1, now_utc(),
                 json.dumps([variant.model_dump() for variant in body.scenario_variants], ensure_ascii=False),
                 json.dumps([option.model_dump() for option in body.execution_options], ensure_ascii=False)),
            )
        return {"id": task_id, "is_demo": settings.study_mode != "live"}

    @app.post("/api/admin/tasks/{task_id}/active/{active}")
    def set_task_active(task_id: str, active: int, admin: str = Depends(require_admin)):
        if active not in {0, 1}:
            raise HTTPException(422, "active 只能是 0 或 1")
        with connect(settings.database_path) as db:
            task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise HTTPException(404)
            if active:
                candidate = task_dict(task)
                if settings.study_mode != "demo" and (len(candidate["evidence_cards"]) < 3
                                                      or len(candidate["decision_options"]) != 5):
                    raise HTTPException(409, "旧任务没有配置新版对话线索和机器评分选项")
                db.execute("UPDATE tasks SET active=0 WHERE is_demo=? AND age_band=?",
                           (task["is_demo"], task["age_band"]))
            db.execute("UPDATE tasks SET active=? WHERE id=?", (active, task_id))
        return {"id": task_id, "active": bool(active)}

    @app.get("/api/admin/review-queue")
    def review_queue(admin: str = Depends(require_admin)):
        with connect(settings.database_path) as db:
            active = active_task_ids(db)
            aliases = record_id_map(db)
            rows = db.execute(
                "SELECT * FROM sessions WHERE record_kind=? AND ended_at IS NOT NULL AND design_version=? AND clarification_strategy=? AND run_mode=? AND literacy_band_width=? ORDER BY julianday(ended_at) DESC,id ASC",
                (cohort_record_kind, DESIGN_VERSION, strategy, settings.study_mode, settings.literacy_band_width),
            )
            records = []
            for row in rows:
                # This is now a read-only record view. Historical review fields
                # remain available, but do not hide automatically scored data.
                fields = ("id", "task_id", "age_band", "is_demo", "run_mode", "created_at", "decision_started_at",
                          "ended_at", "reviewed_at", "submission", "decision_choice_id", "execution_choice_id",
                          "flags_json", "review_status", "review_code", "review_reason", "reviewer", "score", "score_note",
                          "literacy_instrument_version", "model_config_id")
                record = {key: row[key] for key in fields}
                record["id"] = record["record_id"] = aliases[row["id"]]
                record["process_alert_details"] = process_alert_details(dict(row))
                task = json.loads(row["task_snapshot_json"])
                reasons = analysis_reasons(row, active)
                record.update(record_times(row))
                record.update({"in_current_cohort": not reasons, "cohort_exclusion_reasons": reasons,
                               "analysis_eligible": not reasons, "analysis_exclusion_reasons": reasons,
                               "completion_status": "submitted", "group_id": row["group_id"],
                               "literacy_score": row["literacy_score"], "literacy_band": row["literacy_band"],
                               "prompt_version": row["prompt_version"], "prompt_version_blinded": False})
                record.update({"flags": json.loads(row["flags_json"]), "messages": _messages(db, row["id"]),
                               "rubric": task["rubric"], "reference_text": task["reference_text"],
                               "machine_scored": bool(task.get("decision_options"))})
                record.update(ai_prompt_counts(record["messages"]))
                selected = next((option for option in task.get("decision_options", [])
                                 if option["id"] == row["decision_choice_id"]), None)
                record["decision_choice_text"] = selected["text"] if selected else None
                execution = next((option for option in task.get("execution_options", [])
                                  if option["id"] == row["execution_choice_id"]), None)
                record["execution_choice_text"] = execution["text"] if execution else None
                record["score"] = (selected["score"] + (execution["score"] if execution else 0)
                                   if selected and (not task.get("execution_options") or execution) else None)
                records.append(record)
            return records

    @app.post("/api/admin/sessions/{session_id}/review")
    def review(session_id: str, body: ReviewRequest, admin: str = Depends(require_admin)):
        if body.status not in {"valid", "invalid", "pending"}:
            raise HTTPException(422, "状态只能是 valid、invalid 或 pending")
        valid_codes = {"off_task", "uninterpretable", "no_substantive_dialogue", "protocol_violation", "technical_failure", "other"}
        if body.status == "invalid" and (body.code not in valid_codes or not body.reason.strip()):
            raise HTTPException(422, "判为无效时必须选择原因代码并写明具体依据")
        with connect(settings.database_path) as db:
            session, _ = _load_session(db, session_id)
            if session["design_version"] != DESIGN_VERSION:
                raise HTTPException(409, "旧实验记录不能在新版审核队列中操作")
            if not session["ended_at"]:
                raise HTTPException(409, "参与者尚未提交")
            date = now_utc()
            db.execute("UPDATE sessions SET review_status=?,review_code=?,review_reason=?,reviewer=?,reviewed_at=? WHERE id=?",
                       (body.status, body.code if body.status == "invalid" else None,
                        body.reason.strip(), body.reviewer.strip(), date, session_id))
            db.execute("INSERT INTO review_history(session_id,status,reason,reviewer,created_at) VALUES (?,?,?,?,?)",
                       (session_id, body.status, f"{body.code}: {body.reason.strip()}",
                        body.reviewer.strip(), date))
        return {"id": session_id, "status": body.status}

    @app.post("/api/admin/sessions/{session_id}/score")
    def score(session_id: str, body: ScoreRequest, admin: str = Depends(require_admin)):
        with connect(settings.database_path) as db:
            session, task = _load_session(db, session_id)
            if task.get("decision_options"):
                raise HTTPException(409, "本任务按预设决策选项自动计分，不接受人工改分")
            if session["design_version"] != DESIGN_VERSION:
                raise HTTPException(409, "旧实验记录不能在新版审核队列中操作")
            if not session["ended_at"]:
                raise HTTPException(409, "参与者尚未提交")
            if session["review_status"] != "valid":
                raise HTTPException(409, "请先将记录审核为有效，再进行评分")
            db.execute("UPDATE sessions SET score=?,score_note=? WHERE id=?",
                       (body.score, body.note.strip(), session_id))
        return {"id": session_id, "score": body.score}

    def prepared_export_records(db, kind):
        active = active_task_ids(db)
        aliases = record_id_map(db)
        rows = (current_analysis_rows(db)[1] if kind == "analysis" else
                db.execute("SELECT s.* FROM sessions s JOIN tasks t ON t.id=s.task_id ORDER BY s.created_at,s.id").fetchall())
        records = []
        for row in rows:
            record = dict(row)
            record["record_id"] = aliases[row["id"]]
            record["process_alert_details"] = process_alert_details(record)
            if settings.showcase_mode:
                record["showcase_mode"] = True
            snapshot = json.loads(row["task_snapshot_json"])
            selected = next((item for item in snapshot.get("decision_options", [])
                             if item["id"] == row["decision_choice_id"]), None)
            execution = next((item for item in snapshot.get("execution_options", [])
                              if item["id"] == row["execution_choice_id"]), None)
            components = dict(selected.get("score_components", {})) if selected else {}
            if execution:
                for key, value in execution.get("score_components", {}).items():
                    components[key] = components.get(key, 0) + value
            record["decision_components_json"] = json.dumps(components, ensure_ascii=False)
            record["decision_quality_score"] = row["score"]
            record["score_semantics"] = "decision_quality_points_not_success_probability" if execution else "legacy_score"
            record.update(record_times(row))
            record["timing_issues_json"] = json.dumps(record.pop("timing_issues"), ensure_ascii=False)
            reasons = current_cohort_reasons(row, active)
            record["in_current_cohort"] = not reasons
            record["cohort_exclusion_reasons_json"] = json.dumps(reasons, ensure_ascii=False)
            eligibility_reasons = analysis_reasons(row, active)
            record["analysis_eligible"] = not eligibility_reasons
            record["analysis_exclusion_reasons_json"] = json.dumps(eligibility_reasons, ensure_ascii=False)
            messages = _messages(db, row["id"])
            record.update(ai_prompt_counts(messages))
            record["messages_json"] = json.dumps(messages, ensure_ascii=False)
            record["message_requests_json"] = json.dumps([dict(request) for request in db.execute(
                "SELECT request_id,content,status,started_at FROM message_requests WHERE session_id=? ORDER BY started_at,request_id",
                (row["id"],))], ensure_ascii=False)
            events = [dict(e) for e in db.execute(
                "SELECT event_type,detail_json,created_at FROM model_events WHERE session_id=? ORDER BY id", (row["id"],)
            )]
            record["completion_score_pct"] = row["score"]
            record["completion_time_seconds"] = (
                (datetime.fromisoformat(row["ended_at"]) - datetime.fromisoformat(row["created_at"])).total_seconds()
                if row["ended_at"] else None
            )
            record["user_turns"] = sum(message["role"] == "user" for message in messages)
            record["clarification_count"] = sum(message["action"] in {"module_clarification", "prompt_clarification"} for message in messages)
            record["source_searches"] = sum(event["event_type"] == "source_search" for event in events)
            record["source_opens"] = sum(event["event_type"] == "source_open" for event in events)
            record["evidence_reveals"] = sum(event["event_type"] == "evidence_reveal" for event in events)
            record["distinct_evidence_cards"] = len({json.loads(event["detail_json"])["card_id"]
                                                    for event in events if event["event_type"] == "evidence_reveal"})
            reply_ms = [json.loads(event["detail_json"])["latency_ms"] for event in events
                        if event["event_type"] == "reply_latency"]
            record["mean_reply_seconds"] = round(sum(reply_ms) / len(reply_ms) / 1000, 2) if reply_ms else None
            record["model_events_json"] = json.dumps(events, ensure_ascii=False)
            record["review_history_json"] = json.dumps([dict(e) for e in db.execute(
                "SELECT status,reason,reviewer,created_at FROM review_history WHERE session_id=? ORDER BY id", (row["id"],)
            )], ensure_ascii=False)
            records.append(record)
        return records

    @app.get("/api/admin/dialogue-export")
    def dialogue_export(format: str = "csv", admin: str = Depends(require_admin)):
        if format not in {"csv", "json"}:
            raise HTTPException(422, "format 只能是 csv/json")
        records = []
        with connect(settings.database_path) as db:
            for prepared in order_by_record_id(prepared_export_records(db, "analysis")):
                clean = clean_record(prepared)
                metadata = {key: clean[key] for key in DIALOGUE_METADATA_KEYS}
                messages = []
                for index, message in enumerate(json.loads(prepared["messages_json"]), 1):
                    messages.append({"message_index": index, **message,
                                     "created_at": utc_timestamp(message.get("created_at")),
                                     "created_at_beijing": record_times(message)["created_at_beijing"]})
                records.append({**metadata, "has_messages": bool(messages), "messages": messages})
        if format == "json":
            content = json.dumps(records, ensure_ascii=False, indent=2)
            media = "application/json; charset=utf-8"
        else:
            flat_records = []
            for record in records:
                metadata = {key: record[key] for key in (*DIALOGUE_METADATA_KEYS, "has_messages")}
                # An empty row preserves cohort membership without inventing a message.
                for message in record["messages"] or [{}]:
                    flat_records.append({**metadata, **message})
            content = chinese_csv(flat_records, DIALOGUE_COLUMNS)
            media = "text/csv; charset=utf-8"
        filename = f"lens-dialogues-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-UTC.{format}"
        return Response(content, media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    def validate_export_query(kind, format):
        if kind not in {"raw", "analysis"} or format not in {"csv", "json"}:
            raise HTTPException(422, "kind 只能是 raw/analysis，format 只能是 csv/json")

    @app.get("/api/admin/archive-export")
    def archive_export(kind: str = "raw", format: str = "csv", admin: str = Depends(require_admin)):
        """Authenticated internal audit archive, separate from standard downloads."""
        validate_export_query(kind, format)
        with connect(settings.database_path) as db:
            records = prepared_export_records(db, kind)
        if format == "json":
            content = json.dumps(records, ensure_ascii=False, indent=2)
            media = "application/json; charset=utf-8"
        else:
            buffer = io.StringIO()
            fields = list(records[0]) if records else ["id", "record_id", "task_id", "review_status", "is_demo"]
            writer = csv.DictWriter(buffer, fieldnames=fields)
            writer.writeheader()
            writer.writerows({key: _csv_safe(value) for key, value in record.items()} for record in records)
            content = "\ufeff" + buffer.getvalue()
            media = "text/csv; charset=utf-8"
        filename = f"lens-archive-{kind}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-UTC.{format}"
        return Response(content, media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/api/admin/export")
    def export(kind: str = "raw", format: str = "csv", admin: str = Depends(require_admin)):
        validate_export_query(kind, format)
        with connect(settings.database_path) as db:
            records = order_by_record_id([clean_record(record) for record in prepared_export_records(db, kind)])
        if format == "json":
            content = json.dumps(records, ensure_ascii=False, indent=2)
            media = "application/json; charset=utf-8"
        else:
            content = chinese_csv(records, RECORD_COLUMNS)
            media = "text/csv; charset=utf-8"
        filename = f"lens-{kind}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-UTC.{format}"
        return Response(content, media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    return app
