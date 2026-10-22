import base64
import csv
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.config import Settings
from app.main import create_app
from app.db import connect
from app.student_materials import AGE_BANDS, ITEMS as STUDENT_ITEMS, form_for, score_form
ANCHOR_ITEMS = [item for item in STUDENT_ITEMS if item["anchor"]]
from app.student_materials import task_for
from app.model import (ANSWER_PROMPT_VERSION, LiveProvider, PROMPT_ONLY_VERSION,
                       answer_system_prompt, prompt_only_system_prompt)
from app.seed import PILOT_TASK, PILOT_TASK_V4, PILOT_TASK_V5


VERSION, ITEMS = form_for("18-20")
ANSWERS = {item["id"]: item["correct"] for item in ITEMS}
AGE_TASK_18 = task_for("18-20")


def start_payload(answers=None, age_band="18-20"):
    version, items = form_for(age_band)
    return {"consent": True, "age_band": age_band, "education_stage": "bachelor",
            "topic_familiarity": 3, "student_confirmed": True,
            "pretest_version": version,
            "pretest_answers": ({item["id"]: item["correct"] for item in items}
                                if answers is None and age_band != "18-20" else ANSWERS if answers is None else answers)}


def start(client, answers=None):
    return client.post("/api/start", json=start_payload(answers))


def settings(tmp_path, mode="demo"):
    return Settings(
        database_path=tmp_path / "lens.sqlite3",
        study_mode=mode,
        admin_password="test-password",
        model_api_key="test-key" if mode == "live" else "",
        model_base_url="https://example.invalid/v1",
        model_name="test-model",
        model_temperature=0,
        assignment_seed="fixed-test-seed",
    )


def auth():
    encoded = base64.b64encode(b"admin:test-password").decode()
    return {"Authorization": f"Basic {encoded}"}


def record_id_for(client, session_id):
    """Join the private audit ID to the stable identifier used by default views."""
    records = client.get("/api/admin/archive-export?kind=raw&format=json", headers=auth()).json()
    return next(record["record_id"] for record in records if record["id"] == session_id)


def start_two(client):
    starts = [start(client).json() for _ in range(2)]
    assert {s["group_id"] for s in starts} == {"S", "A"}
    return starts


def choice_token(started, option_id, task=AGE_TASK_18):
    text = next(option["text"] for option in task["decision_options"] if option["id"] == option_id)
    return next(option["id"] for option in started["decision_options"] if option["text"] == text)


def frozen_task(client, sid):
    # Only the researcher-facing snapshot provides the actual assigned case.
    # Never infer a correct plan from its position or a participant-facing ID.
    rows = client.get("/api/admin/archive-export?kind=raw&format=json", headers=auth()).json()
    return json.loads(next(row for row in rows if row["id"] == sid)["task_snapshot_json"])


def decision_payload(client, sid, decisions, main_pick="best", execution_pick="service_current_complete"):
    task = frozen_task(client, sid)
    options = task["decision_options"]
    chosen = (max(options, key=lambda item: item["score"]) if main_pick == "best" else
              next(option for option in options if option["id"] == main_pick))
    payload = {"decision_choice_id": choice_token(decisions, chosen["id"], task)}
    if task.get("execution_options"):
        execution = next(option for option in task["execution_options"] if option["id"] == execution_pick)
        payload["execution_choice_id"] = next(option["id"] for option in decisions["execution_options"]
                                               if option["text"] == execution["text"])
    return payload


@pytest.fixture
def task_clock(monkeypatch):
    current = [datetime(2026, 10, 18, 2, 0, tzinfo=timezone.utc)]

    class ControlledDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0].astimezone(tz) if tz else current[0].replace(tzinfo=None)

    monkeypatch.setattr(main_module, "datetime", ControlledDateTime)
    monkeypatch.setattr(main_module, "now_utc", lambda: current[0].isoformat(timespec="seconds"))
    return current


@pytest.mark.parametrize("mode", ["pilot", "live"])
def test_task_time_and_choice_score_survive_refresh_and_export(tmp_path, task_clock, mode):
    app = create_app(settings(tmp_path, mode))
    app.state.provider = FakePilotProvider()
    client = TestClient(app)
    if mode == "live":
        assert client.post("/api/admin/tasks", headers=auth(), json=AGE_TASK_18).status_code == 200
    sid = start(client).json()["session_id"]
    started_at = task_clock[0].isoformat(timespec="seconds")

    raw = client.get("/api/admin/archive-export?kind=raw&format=json", headers=auth()).json()[0]
    assert raw["completion_time_seconds"] is None and raw["completion_score_pct"] is None
    assert raw["user_turns"] == 0 and raw["clarification_count"] == 0
    raw_csv = list(csv.DictReader(io.StringIO(client.get(
        "/api/admin/archive-export?kind=raw&format=csv", headers=auth()).text.lstrip("\ufeff"))))[0]
    assert raw_csv["completion_time_seconds"] == "" and raw_csv["completion_score_pct"] == ""

    task_clock[0] += timedelta(seconds=30)
    restored = client.get(f"/api/session/{sid}").json()
    assert restored["created_at"] == started_at and restored["ended_at"] is None
    for greeting in ("你好", "您好"):
        assert client.post(f"/api/session/{sid}/message", json={"content": greeting}).status_code == 200
    decisions = client.post(f"/api/session/{sid}/decision").json()
    task_clock[0] += timedelta(seconds=95)
    chosen_id = max(frozen_task(client, sid)["decision_options"], key=lambda item: item["score"])["id"]
    assert client.post(f"/api/session/{sid}/submit", json=decision_payload(
        client, sid, decisions, execution_pick="email_current_omit_plan")).status_code == 200
    ended_at = task_clock[0].isoformat(timespec="seconds")

    task_clock[0] += timedelta(seconds=60)
    assert client.post(f"/api/session/{sid}/submit", json=decision_payload(
        client, sid, decisions)).status_code == 409
    restored = client.get(f"/api/session/{sid}").json()
    assert restored["created_at"] == started_at and restored["ended_at"] == ended_at
    assert restored["decision_choice_id"] == chosen_id
    assert restored["execution_choice_id"] == "email_current_omit_plan"

    kinds = ["raw", "analysis"]
    for kind in kinds:
        record = client.get(f"/api/admin/archive-export?kind={kind}&format=json", headers=auth()).json()[0]
        assert record["completion_time_seconds"] == 125
        assert record["completion_score_pct"] == record["score"] == 90
        assert record["decision_choice_id"] == chosen_id and record["user_turns"] == 2
        assert record["execution_choice_id"] == "email_current_omit_plan"
        record_csv = list(csv.DictReader(io.StringIO(client.get(
            f"/api/admin/archive-export?kind={kind}&format=csv", headers=auth()).text.lstrip("\ufeff"))))[0]
        assert float(record_csv["completion_time_seconds"]) == 125
        assert float(record_csv["completion_score_pct"]) == 90
        assert record_csv["execution_choice_id"] == "email_current_omit_plan"


def test_concurrent_submit_preserves_first_choice_score_and_end(tmp_path, task_clock, monkeypatch):
    app = create_app(settings(tmp_path))
    client = TestClient(app)
    sid = start(client).json()["session_id"]
    for greeting in ("你好", "您好"):
        assert client.post(f"/api/session/{sid}/message", json={"content": greeting}).status_code == 200
    decisions = client.post(f"/api/session/{sid}/decision").json()
    task_clock[0] += timedelta(seconds=120)
    chosen_id = max(frozen_task(client, sid)["decision_options"], key=lambda item: item["score"])["id"]
    first_payload = decision_payload(client, sid, decisions, execution_pick="email_current_omit_plan")
    second_payload = decision_payload(client, sid, decisions)
    first_loaded = threading.Event()
    second_attempted = threading.Event()
    first_thread = {}
    original_load = main_module._load_session
    original_connect = main_module.connect

    def observed_connect(path):
        db = original_connect(path)

        def trace(sql):
            if (first_loaded.is_set() and threading.get_ident() != first_thread["id"]
                    and sql.upper().startswith("BEGIN IMMEDIATE")):
                second_attempted.set()

        db.set_trace_callback(trace)
        return db

    def pause_first_read(db, session_id):
        result = original_load(db, session_id)
        if not first_loaded.is_set():
            first_thread["id"] = threading.get_ident()
            first_loaded.set()
            assert second_attempted.wait(5), "第二次提交未开始"
        elif threading.get_ident() != first_thread["id"]:
            # Without a write lock, both requests would have read the unfinished row.
            second_attempted.set()
        return result

    monkeypatch.setattr(main_module, "connect", observed_connect)
    monkeypatch.setattr(main_module, "_load_session", pause_first_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.post, f"/api/session/{sid}/submit", json=first_payload)
        assert first_loaded.wait(5), "第一次提交未读取会话"
        second = pool.submit(client.post, f"/api/session/{sid}/submit", json=second_payload)
        assert first.result(timeout=10).status_code == 200
        assert second.result(timeout=10).status_code == 409

    record = client.get("/api/admin/archive-export?kind=raw&format=json", headers=auth()).json()[0]
    assert record["decision_choice_id"] == chosen_id and record["completion_score_pct"] == 90
    assert record["execution_choice_id"] == "email_current_omit_plan"
    assert record["ended_at"] == task_clock[0].isoformat(timespec="seconds")
    assert record["completion_time_seconds"] == 120


def test_demo_clarification_review_and_exports(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    assert client.post("/api/start", json={**start_payload(), "consent": False}).status_code == 400
    assert start(client, {"s01": 0}).status_code == 422
    assert start(client, {**ANSWERS, "s01": 4}).status_code == 422
    assert client.post("/api/start", json={**start_payload(), "pretest_version": "old"}).status_code == 409
    pretest = client.get("/api/pretest?age_band=18-20").json()
    assert pretest["item_count"] == 30
    assert "correct" not in json.dumps(pretest)
    starts = start_two(client)
    adaptive = next(s for s in starts if s["interaction_condition"] == "adaptive")
    sid = adaptive["session_id"]
    answer = client.post(f"/api/session/{sid}/message", json={"content": "我能报名这个课程吗？"}).json()
    assert answer["action"] == "prompt_clarification"
    assert "每周" in answer["content"]
    answer2 = client.post(f"/api/session/{sid}/message", json={"content": "我每周能参加三次。现行的参加频率条件是什么？"}).json()
    assert answer2["action"] == "evidence"
    assert "现场安排每周必须到场2次" in answer2["content"]
    messages = client.get(f"/api/session/{sid}").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    decisions = client.post(f"/api/session/{sid}/decision").json()
    assert client.post(f"/api/session/{sid}/submit", json={**decision_payload(client, sid, decisions),
        "submission": "按现行规则提交完整材料。", "burden": 2, "helpfulness": 5}).status_code == 200
    queue = client.get("/api/admin/review-queue", headers=auth()).json()
    reviewed = next(row for row in queue if row["id"] == record_id_for(client, sid))
    assert reviewed["review_status"] == "pending"
    assert reviewed["group_id"] == adaptive["group_id"] and reviewed["analysis_eligible"] is True
    assert "rubric" in reviewed and "reference_text" in reviewed
    assert reviewed["literacy_score"] == 30
    assert client.post(f"/api/admin/sessions/{sid}/review", headers=auth(),
                       json={"status": "invalid", "reason": "", "reviewer": "r1"}).status_code == 422
    assert client.post(f"/api/admin/sessions/{sid}/review", headers=auth(),
                       json={"status": "valid", "reason": "", "reviewer": "r1"}).status_code == 200
    assert client.post(f"/api/admin/sessions/{sid}/score", headers=auth(),
                       json={"score": 80, "note": "演示评分"}).status_code == 409
    raw = client.get("/api/admin/archive-export?kind=raw&format=json", headers=auth()).json()
    analysis = client.get("/api/admin/archive-export?kind=analysis&format=json", headers=auth()).json()
    assert any(row["id"] == sid and "clarification_decision" in row["model_events_json"]
               and "reply_latency" in row["model_events_json"] for row in raw)
    assert json.loads(next(row for row in raw if row["id"] == sid)["feedback_json"]) == {"burden": 2, "helpfulness": 5}
    assert [row["id"] for row in analysis] == [sid]
    assert analysis[0]["run_mode"] == "demo" and analysis[0]["is_demo"] == 1


def test_flags_preserve_legacy_review_history_and_withdrawal(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    sid = start(client).json()["session_id"]
    client.post(f"/api/session/{sid}/message", json={"content": "哈哈哈哈哈哈"})
    client.post(f"/api/session/{sid}/message", json={"content": "哈哈哈哈哈哈"})
    decisions = client.post(f"/api/session/{sid}/decision").json()
    assert client.post(f"/api/session/{sid}/submit", json=decision_payload(client, sid, decisions)).status_code == 200
    queue = client.get("/api/admin/review-queue", headers=auth()).json()
    row = next(x for x in queue if x["id"] == record_id_for(client, sid))
    assert row["flags"]
    assert row["review_status"] == "pending"
    assert client.post(f"/api/admin/sessions/{sid}/review", headers=auth(),
                       json={"status": "invalid", "code": "off_task", "reason": "全程重复无关词", "reviewer": "r1"}).status_code == 200
    assert client.delete(f"/api/session/{sid}").json() == {"deleted": True}
    assert client.get(f"/api/session/{sid}").status_code == 404


class FakeLiveProvider:
    def __init__(self):
        self.calls = []

    async def complete(self, messages, *, temperature=None):
        self.calls.append(messages)
        latest = messages[-1]["content"]
        if "材料" in latest:
            return "申请材料是申请表和成绩单。"
        if "额外的澄清策略（主动澄清组）" in messages[0]["content"]:
            return "【澄清提问】申请人曾领过同类资助吗？"
        return "申请人是本校二年级学生；如果未领过同类资助，就可以申请学习支持金。"


class FakePilotProvider(FakeLiveProvider):
    async def complete(self, messages, *, temperature=None):
        self.calls.append(messages)
        latest = messages[-1]["content"]
        if latest in {"你好", "您好"}:
            return "你好，我可以根据任务资料回答你的问题。"
        if "截止" in latest:
            return "依据 C_TIME 更正公告，本次截止时间为11月18日17:00，到活动服务点提交，邮件不受理。"
        if "现行" in latest:
            return "《2026 年秋季青年社区图像活动报名办法》适用于本次2026年秋季报名，2025年旧指南不适用。"
        if "办理要求" in latest:
            return "资格条件包括身份与年龄；按情况选择登记类别和参加安排，11月18日17:00前到活动服务点交报名表和参加计划。"
        if "额外的澄清策略（主动澄清组）" in messages[0]["content"]:
            return "【澄清提问】" + AGE_TASK_18["slots"][0]["question"]
        return "身份和年龄符合基本条件；登记类别取决于既往名额，参加安排取决于每周能到场次数。"


def test_live_uses_real_provider_path_and_automatic_completed_export(tmp_path):
    app = create_app(settings(tmp_path, "live"))
    provider = FakeLiveProvider()
    app.state.provider = provider
    client = TestClient(app)
    assert client.get("/api/admin/summary").status_code == 401
    task = {
        "age_band": "18-20",
        "title": "模拟资助资格判断", "instructions": "请判断申请资格并写出完整依据。",
        "shared_context": "申请人是本校二年级学生。", "participant_facts": "你知道申请人未领过同类资助。",
        "reference_text": "这是虚构制度。二年级学生且未领过同类资助，可以申请学习支持金。申请材料为申请表和成绩单。",
        "rubric": "按五项预设行动方案的完整程度给出 100、75、50、25、0 分。",
        "evidence_cards": [
            {"id": "RULES", "title": "资格规则", "body": "现行规定要求二年级学生且没有领过同类资助。", "keywords": ["资格", "规则"]},
            {"id": "MATERIALS", "title": "申请材料", "body": "现行规定要求申请表和成绩单两项正式材料。", "keywords": ["材料", "申请表"]},
            {"id": "TIME", "title": "办理时间", "body": "本批次办理截止时间为十一月十八日十七点。", "keywords": ["截止", "时间"]},
        ],
        "decision_options": [{"id": f"option_{i}", "text": f"第 {i} 种申请行动方案，内容完整程度不同。", "score": score}
                             for i, score in enumerate([100, 75, 50, 25, 0])],
        "slots": [{"key": "funding", "description": "是否领过同类资助；影响资格判断", "question": "申请人曾领过同类资助吗？", "aliases": []}],
        "keywords": ["资助", "资格", "申请"],
    }
    assert client.post("/api/admin/tasks", headers=auth(), json=task).status_code == 200
    starts = start_two(client)
    adaptive = next(s for s in starts if s["interaction_condition"] == "adaptive")
    standard = next(s for s in starts if s["interaction_condition"] == "standard")
    a = client.post(f"/api/session/{adaptive['session_id']}/message", json={"content": "我能申请吗？"}).json()
    s = client.post(f"/api/session/{standard['session_id']}/message", json={"content": "我能申请吗？"}).json()
    assert a["action"] == "prompt_clarification" and a["content"] == "申请人曾领过同类资助吗？"
    assert s["action"] == "answer" and "如果未领过同类资助" in s["content"]
    assert len(provider.calls) == 2
    assert provider.calls[0][0]["content"] == prompt_only_system_prompt(task)
    assert provider.calls[1][0]["content"] == answer_system_prompt(task)
    assert all(call[-1] == {"role": "user", "content": "我能申请吗？"} for call in provider.calls)
    assert all(task["participant_facts"] not in call[0]["content"] and task["rubric"] not in call[0]["content"]
               for call in provider.calls)
    sid = standard["session_id"]
    evidence = client.post(f"/api/session/{sid}/message", json={"content": "材料有哪些？"}).json()
    assert evidence["action"] == "answer" and "申请表和成绩单" in evidence["content"]
    assert client.post(f"/api/session/{sid}/decision").status_code == 200
    decisions = client.post(f"/api/session/{sid}/decision").json()
    assert client.post(f"/api/session/{sid}/submit", json={"submission": ""}).status_code == 422
    client.post(f"/api/session/{sid}/submit", json={"decision_choice_id": choice_token(decisions, "option_0", task)})
    assert client.post(f"/api/admin/sessions/{sid}/score", headers=auth(), json={"score": 80}).status_code == 409
    client.post(f"/api/admin/sessions/{sid}/review", headers=auth(),
                json={"status": "valid", "reason": "与任务相关", "reviewer": "r1"})
    analysis = client.get("/api/admin/archive-export?kind=analysis&format=json", headers=auth()).json()
    assert len(analysis) == 1 and analysis[0]["id"] == sid
    assert analysis[0]["completion_score_pct"] == 100
    assert analysis[0]["user_turns"] == 2
    assert analysis[0]["source_searches"] == 0 and analysis[0]["source_opens"] == 0
    assert analysis[0]["evidence_reveals"] == 0 and analysis[0]["distinct_evidence_cards"] == 0
    assert analysis[0]["mean_reply_seconds"] is not None
    summary = client.get("/api/admin/summary", headers=auth()).json()
    score_bins = summary["score_distribution"]
    assert next(row for row in score_bins if row["start"] == 80 and row["group_id"] == "S")["sessions"] == 1
    assert summary["band_outcomes"] == [{"literacy_band": "090-100", "group_id": "S", "valid_n": 1, "mean_score": 100.0}]


def test_group_cap_is_enforced(tmp_path):
    client = TestClient(create_app(replace(settings(tmp_path), group_cap=1)))
    start_two(client)
    response = start(client)
    assert response.status_code == 409


def test_pilot_calls_model_and_exports_its_own_completed_cohort(tmp_path):
    pilot = replace(settings(tmp_path), study_mode="pilot", model_name="local-test-model")
    app = create_app(pilot)
    provider = FakePilotProvider()
    app.state.provider = provider
    client = TestClient(app)
    starts = start_two(client)
    adaptive = next(s for s in starts if s["interaction_condition"] == "adaptive")
    sid = adaptive["session_id"]
    answer = client.post(f"/api/session/{sid}/message", json={"content": "我能报名吗？"}).json()
    assert answer["action"] == "prompt_clarification"
    assert len(provider.calls) == 1 and provider.calls[0][-1]["content"] == "我能报名吗？"
    assert client.post(f"/api/session/{sid}/decision").status_code == 409
    client.post(f"/api/session/{sid}/message", json={"content": "你好"})
    decisions = client.post(f"/api/session/{sid}/decision").json()
    assert client.post(f"/api/session/{sid}/submit", json=decision_payload(
        client, sid, decisions, main_pick="unclassified_random")).status_code == 200
    client.post(f"/api/admin/sessions/{sid}/review", headers=auth(),
                json={"status": "valid", "reason": "", "reviewer": "r1"})
    analysis = client.get("/api/admin/archive-export?kind=analysis&format=json", headers=auth()).json()
    assert [row["id"] for row in analysis] == [sid]
    assert analysis[0]["run_mode"] == "pilot" and analysis[0]["is_demo"] == 1


def test_literacy_stratification_and_legacy_separation(tmp_path):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    high = [start(client).json() for _ in range(2)]
    low_answers = {key: (value + 1) % 3 for key, value in ANSWERS.items()}
    low = [start(client, low_answers).json() for _ in range(2)]
    assert {row["group_id"] for row in high} == {"S", "A"}
    assert {row["group_id"] for row in low} == {"S", "A"}
    strata = client.get("/api/admin/summary", headers=auth()).json()["strata"]
    assert strata == []  # Analyses use the common completed cohort, not assignments.
    with connect(cfg.database_path) as db:
        rows = [dict(row) for row in db.execute("SELECT design_version,literacy_score,literacy_percent,literacy_band,literacy_answers_json,expression_condition FROM sessions")]
        assert {r["literacy_score"] for r in rows} == {0, 30}
        assert {r["literacy_percent"] for r in rows} == {0.0, 100.0}
        assert {r["literacy_band"] for r in rows} == {"000-010", "090-100"}
        assert {r["expression_condition"] for r in rows} == {"natural"}
        assert all(r["design_version"] == "university-v1" and r["literacy_answers_json"] for r in rows)
        db.execute("UPDATE sessions SET design_version='style-v1',review_status='valid',is_demo=0 WHERE id=?", (high[0]["session_id"],))
        db.execute("UPDATE sessions SET ended_at=created_at WHERE id=?", (high[0]["session_id"],))
    assert all(r["id"] != record_id_for(client, high[0]["session_id"]) for r in client.get("/api/admin/review-queue", headers=auth()).json())
    assert client.get("/api/admin/archive-export?kind=analysis&format=json", headers=auth()).json() == []
    assert any(r["id"] == high[0]["session_id"] for r in client.get("/api/admin/archive-export?kind=raw&format=json", headers=auth()).json())


def test_percent_score_and_pilot_item_analysis(tmp_path):
    assert score_form("18-20", ANSWERS, 10)[:3] == (30, 100.0, "090-100")
    wrong = {key: (value + 1) % 3 for key, value in ANSWERS.items()}
    assert score_form("18-20", wrong, 10)[:3] == (0, 0.0, "000-010")
    assert score_form("18-20", ANSWERS, 20)[2] == "080-100"
    app = create_app(settings(tmp_path, "pilot"))
    app.state.provider = FakePilotProvider()
    client = TestClient(app)
    for answers in [ANSWERS, ANSWERS, wrong, wrong]:
        started = start(client, answers)
        assert started.status_code == 200
        sid = started.json()["session_id"]
        for greeting in ["你好", "您好"]:
            assert client.post(f"/api/session/{sid}/message", json={"content": greeting}).status_code == 200
        decisions = client.post(f"/api/session/{sid}/decision").json()
        assert client.post(f"/api/session/{sid}/submit", json=decision_payload(client, sid, decisions)).status_code == 200
    assert client.get("/api/admin/pretest-analysis").status_code == 401
    report = client.get("/api/admin/pretest-analysis", headers=auth()).json()
    assert report["n"] == 4 and report["item_count"] == 30
    assert report["mean_percent"] == 50.0
    assert all(item["p_correct"] == 0.5 and item["corrected_item_total_r"] == 1.0 for item in report["items"])


def test_pilot_task_answers_directly_without_source_browser_and_keeps_machine_scoring(tmp_path):
    app = create_app(settings(tmp_path, "pilot"))
    app.state.provider = FakePilotProvider()
    client = TestClient(app)
    started = start(client).json()
    assert started["task"]["id"] == AGE_TASK_18["id"]
    assert started["task"]["decision_options"] == []
    assert "score" not in json.dumps(started["task"])
    assert "reference_text" not in started["task"] and "evidence_cards" not in started["task"]
    sid = started["session_id"]
    assert client.post(f"/api/session/{sid}/sources/search", json={"query": "截止"}).status_code == 410
    assert client.get(f"/api/session/{sid}/sources/C").status_code == 410
    broad = client.post(f"/api/session/{sid}/message", json={"content": "直接告诉我能否报名"}).json()
    assert broad["action"] != "evidence" and "每周可参加 3 次" not in broad["content"]
    found = client.post(f"/api/session/{sid}/message", json={"content": "最新截止时间是什么？"}).json()
    assert found["action"] == "answer" and "11月18日17:00" in found["content"]
    raw = client.get("/api/admin/archive-export?kind=raw&format=json", headers=auth()).json()
    events = next(row for row in raw if row["id"] == sid)["model_events_json"]
    assert "evidence_route" not in events and "evidence_reveal" not in events
    assert "dialogue_prompt" in events and "reply_latency" in events
    assert len(app.state.provider.calls) == 2
    assert app.state.provider.calls[-1][1:] == [
        {"role": "user", "content": "直接告诉我能否报名"},
        {"role": "assistant", "content": broad["content"]},
        {"role": "user", "content": "最新截止时间是什么？"},
    ]
    summary = client.get("/api/admin/summary", headers=auth()).json()
    assert summary["literacy_distribution"] == []
    assert sum(row["sessions"] for row in summary["score_distribution"]) == 0
    assert summary["evidence_reveal_distribution"] == []
    decisions = client.post(f"/api/session/{sid}/decision").json()
    assert len(decisions["decision_options"]) == 5
    assert client.post(f"/api/session/{sid}/message", json={"content": "再问一个问题"}).status_code == 409
    chosen_id = max(frozen_task(client, sid)["decision_options"], key=lambda item: item["score"])["id"]
    assert client.post(f"/api/session/{sid}/submit", json=decision_payload(client, sid, decisions)).status_code == 200
    submitted = client.get(f"/api/session/{sid}").json()
    assert submitted["decision_choice_id"] == chosen_id
    assert submitted["execution_choice_id"] == "service_current_complete"
    with connect(settings(tmp_path, "pilot").database_path) as db:
        assert db.execute("SELECT score FROM sessions WHERE id=?", (sid,)).fetchone()[0] == 100


def test_new_task_isolated_from_previous_task_dashboard(tmp_path):
    client = TestClient(create_app(settings(tmp_path, "pilot")))
    previous = start(client).json()
    body = {key: PILOT_TASK_V4[key] for key in
            ["title", "instructions", "shared_context", "participant_facts", "reference_text", "rubric", "slots", "keywords", "evidence_cards", "decision_options"]}
    body["age_band"] = "18-20"
    body["title"] = "预测试任务：第二版本资料包"
    new_id = client.post("/api/admin/tasks", headers=auth(), json=body).json()["id"]
    tasks = client.get("/api/admin/tasks", headers=auth()).json()
    assert {task["id"] for task in tasks if task["active"] and task["age_band"] == "18-20"} == {new_id}
    summary = client.get("/api/admin/summary", headers=auth()).json()
    assert summary["active_task_id"] == new_id and summary["literacy_distribution"] == []
    assert start(client).json()["task"]["id"] == new_id
    assert any(row["id"] == previous["session_id"] for row in
               client.get("/api/admin/archive-export?kind=raw&format=json", headers=auth()).json())


def test_participant_access_code_and_deepseek_payload(tmp_path):
    cfg = replace(settings(tmp_path, "pilot"), participant_access_code="invite-123",
                  model_base_url="https://api.deepseek.com", model_name="deepseek-flash")
    client = TestClient(create_app(cfg))
    assert client.get("/api/info").json()["access_code_required"] is True
    assert start(client).status_code == 403
    assert client.post("/api/start", json={**start_payload(), "access_code": "invite-123"}).status_code == 200
    payload = LiveProvider(cfg).request_payload([{"role": "user", "content": "你好"}])
    assert payload["model"] == "deepseek-flash"
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["max_tokens"] == 1600
    prompt = prompt_only_system_prompt(PILOT_TASK)
    assert "只是问候" in prompt and "已经明确给出的事实绝不重复追问" in prompt


def test_nearby_percent_scores_share_an_assignment_interval(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    wrong = {key: (value + 1) % 3 for key, value in ANSWERS.items()}
    keys = list(ANSWERS)
    three_correct = {**wrong, **{key: ANSWERS[key] for key in keys[:3]}}
    four_correct = {**three_correct, keys[3]: ANSWERS[keys[3]]}
    assert score_form("18-20", three_correct)[:3] == (3, 10.0, "010-020")
    assert score_form("18-20", four_correct)[:3] == (4, 13.33, "010-020")
    assigned = [start(client, answers).json() for answers in
                [three_correct, four_correct, three_correct, four_correct]]
    assert sorted(row["group_id"] for row in assigned) == ["A", "A", "S", "S"]
    with connect(settings(tmp_path).database_path) as db:
        rows = [dict(row) for row in db.execute(
            "SELECT literacy_percent,literacy_band,literacy_band_width FROM sessions")]
    assert {row["literacy_percent"] for row in rows} == {10.0, 13.33}
    assert {(row["literacy_band"], row["literacy_band_width"]) for row in rows} == {("010-020", 10)}


class FakePromptProvider(FakePilotProvider):
    pass


def test_prompt_only_strategy_uses_same_model_with_extra_instruction(tmp_path):
    cfg = replace(settings(tmp_path, "pilot"), clarification_strategy="prompt")
    app = create_app(cfg)
    provider = FakePromptProvider()
    app.state.provider = provider
    client = TestClient(app)
    starts = start_two(client)
    assert client.get("/api/admin/summary", headers=auth()).json()["clarification_strategy"] == "prompt"
    adaptive = next(s for s in starts if s["interaction_condition"] == "adaptive")
    standard = next(s for s in starts if s["interaction_condition"] == "standard")
    a = client.post(f"/api/session/{adaptive['session_id']}/message", json={"content": "我能申请吗？"}).json()
    s = client.post(f"/api/session/{standard['session_id']}/message", json={"content": "我能申请吗？"}).json()
    assert a["action"] == "prompt_clarification" and a["content"] == AGE_TASK_18["slots"][0]["question"]
    assert s["action"] == "answer" and "取决于每周能到场次数" in s["content"]
    assert len(provider.calls) == 2
    adaptive_prompt, standard_prompt = (call[0]["content"] for call in provider.calls)
    assert adaptive_prompt.startswith(standard_prompt + "\n额外的澄清策略（主动澄清组）")
    assert "额外的澄清策略（主动澄清组）" not in standard_prompt
    assert provider.calls[0][1:] == provider.calls[1][1:] == [{"role": "user", "content": "我能申请吗？"}]
    with connect(cfg.database_path) as db:
        rows = list(db.execute("SELECT group_id,clarification_strategy,prompt_version FROM sessions"))
        assert {r["clarification_strategy"] for r in rows} == {"prompt"}
        assert next(r for r in rows if r["group_id"] == "A")["prompt_version"] == ANSWER_PROMPT_VERSION + "+" + PROMPT_ONLY_VERSION
        assert next(r for r in rows if r["group_id"] == "S")["prompt_version"] == ANSWER_PROMPT_VERSION


def test_choice_is_stable_hidden_until_decision_and_scored_on_submit(tmp_path):
    app = create_app(settings(tmp_path, "pilot"))
    app.state.provider = FakePilotProvider()
    client = TestClient(app)
    started = start(client).json()
    sid = started["session_id"]
    assert started["task"]["decision_options"] == []
    assert client.post(f"/api/session/{sid}/decision").status_code == 409
    assert client.post(f"/api/session/{sid}/submit", json={"decision_choice_id": "unknown"}).status_code == 409
    client.post(f"/api/session/{sid}/message", json={"content": "你好"})
    client.post(f"/api/session/{sid}/message", json={"content": "您好"})
    decisions = client.post(f"/api/session/{sid}/decision").json()
    options = decisions["decision_options"]
    assert {option["id"] for option in options}.isdisjoint(
        {option["id"] for option in AGE_TASK_18["decision_options"]})
    assert all(option["id"].startswith("choice_") for option in options)
    restored = client.get(f"/api/session/{sid}").json()
    assert restored["decision_stage"] and options == restored["task"]["decision_options"]
    assert all(set(option) == {"id", "label", "text"} for option in options)
    assert client.post(f"/api/session/{sid}/submit", json={"decision_choice_id": "unknown"}).status_code == 422
    assert client.post(f"/api/session/{sid}/submit", json={"decision_choice_id": "complete"}).status_code == 422
    chosen_id = max(frozen_task(client, sid)["decision_options"], key=lambda item: item["score"])["id"]
    assert len(decisions["execution_options"]) == 5
    assert all(set(option) == {"id", "label", "text"} for option in decisions["execution_options"])
    chosen = decision_payload(client, sid, decisions)
    assert client.post(f"/api/session/{sid}/submit", json={"decision_choice_id": chosen["decision_choice_id"]}).status_code == 422
    assert client.post(f"/api/session/{sid}/submit", json={**chosen, "execution_choice_id": "unknown"}).status_code == 422
    assert client.post(f"/api/session/{sid}/submit", json=chosen).status_code == 200
    queue = client.get("/api/admin/review-queue", headers=auth()).json()
    row = next(item for item in queue if item["id"] == record_id_for(client, sid))
    assert row["machine_scored"] and row["score"] == 100 and row["decision_choice_id"] == chosen_id
    assert row["analysis_eligible"] is True and row["review_status"] == "pending"
    assert "no_evidence_retrieved" not in row["flags"]
    assert client.post(f"/api/admin/sessions/{sid}/review", headers=auth(),
                       json={"status": "valid", "reason": "符合预先定义的纳入规则", "reviewer": "r1"}).status_code == 200
    visible = next(item for item in client.get("/api/admin/review-queue", headers=auth()).json() if item["id"] == record_id_for(client, sid))
    assert visible["score"] == 100 and visible["decision_choice_id"] == chosen_id
    assert visible["execution_choice_id"] == "service_current_complete"
    assert client.post(f"/api/admin/sessions/{sid}/score", headers=auth(), json={"score": 0}).status_code == 409


class FakeVerdictProvider:
    def __init__(self):
        self.calls = []

    async def complete(self, messages, *, temperature=None):
        self.calls.append(messages)
        if "材料" in messages[-1]["content"]:
            return "需要提交报名表和参加计划，依据是 B_MATERIALS。"
        return "身份和年龄符合报名条件；参加方案仍取决于既往名额和每周到场次数。"


def test_freeform_model_verdict_and_followup_are_shown_without_replacement(tmp_path):
    cfg = replace(settings(tmp_path, "pilot"), clarification_strategy="prompt")
    app = create_app(cfg)
    provider = FakeVerdictProvider()
    app.state.provider = provider
    client = TestClient(app)
    standard = next(item for item in start_two(client) if item["group_id"] == "S")
    sid = standard["session_id"]
    broad = client.post(f"/api/session/{sid}/message", json={"content": "直接告诉我能否申请"})
    assert broad.status_code == 200
    verdict = broad.json()
    assert verdict["action"] == "answer"
    assert verdict["content"] == "身份和年龄符合报名条件；参加方案仍取决于既往名额和每周到场次数。"
    specific = client.post(f"/api/session/{sid}/message", json={"content": "申请材料有哪些？"}).json()
    assert specific["action"] == "answer" and specific["content"] == "需要提交报名表和参加计划，依据是 B_MATERIALS。"
    assert len(provider.calls) == 2
    assert provider.calls[1][1:] == [
        {"role": "user", "content": "直接告诉我能否申请"},
        {"role": "assistant", "content": verdict["content"]},
        {"role": "user", "content": "申请材料有哪些？"},
    ]
    task = frozen_task(client, sid)
    assert provider.calls[0][0]["content"] == answer_system_prompt(task)
    assert task["rubric"] not in provider.calls[0][0]["content"]
    assert task["participant_facts"] not in provider.calls[0][0]["content"]


def test_overview_answers_requirements_and_source_names_are_self_contained(tmp_path):
    app = create_app(settings(tmp_path, "pilot"))
    app.state.provider = FakePilotProvider()
    client = TestClient(app)
    sid = start(client).json()["session_id"]
    overview = client.post(f"/api/session/{sid}/message", json={"content": "办理要求如何？"}).json()
    assert overview["action"] == "answer"
    assert "资格条件" in overview["content"] and "文件 A" not in overview["content"]
    scope = client.post(f"/api/session/{sid}/message", json={"content": "现行办法适用于哪个批次？"}).json()
    assert scope["action"] == "answer"
    assert "《2026 年秋季青年社区图像活动报名办法》" in scope["content"]
    assert "文件 A" not in scope["content"] and "文件 B" not in scope["content"]
    assert all("文件 A" not in card["body"] and "文件 B" not in card["body"]
               for card in AGE_TASK_18["evidence_cards"])


def test_age_strata_have_parallel_forms_tasks_and_independent_assignment(tmp_path):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    assert client.get("/api/pretest?age_band=unknown").status_code == 422
    forms = {band: client.get("/api/pretest", params={"age_band": band}).json()
             for band in AGE_BANDS}
    assert len({form["version"] for form in forms.values()}) == 1
    assert all(form["items"] == forms["18-20"]["items"] for form in forms.values())
    assert all(form["item_count"] == 30 and form["anchor_count"] == len(ANCHOR_ITEMS) for form in forms.values())
    assert all("correct" not in json.dumps(form) for form in forms.values())
    anchors = [{item["id"]: item for item in form["items"] if item["id"] in
                {anchor["id"] for anchor in ANCHOR_ITEMS}} for form in forms.values()]
    assert all(anchor == anchors[0] for anchor in anchors)
    starts = {}
    for band in ["18-20", "24-25"]:
        starts[band] = [client.post("/api/start", json=start_payload(age_band=band)).json()
                        for _ in range(2)]
        assert {row["group_id"] for row in starts[band]} == {"S", "A"}
        assert all(row["task"]["id"] == starts[band][0]["task"]["id"] for row in starts[band])
    assert starts["18-20"][0]["task"]["id"] != starts["24-25"][0]["task"]["id"]
    adult_summary = client.get("/api/admin/summary?age_band=18-20", headers=auth()).json()
    older_summary = client.get("/api/admin/summary?age_band=24-25", headers=auth()).json()
    assert sum(row["sessions"] for row in adult_summary["groups"]) == 2
    assert sum(row["sessions"] for row in older_summary["groups"]) == 2
    assert adult_summary["active_task_id"] != older_summary["active_task_id"]
    with connect(cfg.database_path) as db:
        rows = [dict(row) for row in db.execute(
            "SELECT age_band,education_stage,topic_familiarity,anchor_score,guardian_consented FROM sessions")]
    assert {row["age_band"] for row in rows} == {"18-20", "24-25"}
    assert all(row["education_stage"] == "bachelor" and row["topic_familiarity"] == 3
               and row["anchor_score"] == len(ANCHOR_ITEMS) for row in rows)


@pytest.mark.parametrize("age", ["12-14", "15-17", "18-24", "25-34", "75-plus", "17", "26", ""])
def test_old_or_out_of_scope_age_cannot_be_enabled_by_legacy_minor_settings(tmp_path, age):
    config = replace(settings(tmp_path, "pilot"), minor_access_code="guardian-code",
                     under14_privacy_notice_url="https://example.org/under14-policy")
    client = TestClient(create_app(config))
    payload = {**start_payload(), "age_band": age, "minor_access_code": "guardian-code",
               "participant_assent": True, "guardian_consent": True}
    assert client.post("/api/start", json=payload).status_code == 422
    assert client.get("/api/pretest", params={"age_band": age}).status_code == 422
    with connect(config.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


@pytest.mark.parametrize("stage", ["primary", "senior", "graduate", "other", "employed", "", "本科"])
def test_start_rejects_non_student_education_stages(tmp_path, stage):
    client = TestClient(create_app(settings(tmp_path)))
    assert client.post("/api/start", json={**start_payload(), "education_stage": stage}).status_code == 422


@pytest.mark.parametrize("confirmation", [False, None, "true", 1])
def test_start_requires_explicit_boolean_current_enrollment_confirmation(tmp_path, confirmation):
    client = TestClient(create_app(settings(tmp_path)))
    assert client.post("/api/start", json={**start_payload(), "student_confirmed": confirmation}).status_code == 422


def test_start_requires_confirmation_even_when_field_is_omitted(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    payload = start_payload()
    del payload["student_confirmed"]
    assert client.post("/api/start", json=payload).status_code == 422
