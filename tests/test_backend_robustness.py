"""Real API regressions for concurrent requests, retry, and cohort boundaries."""
import asyncio
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
from app.student_materials import form_for
from app.config import Settings
from app.db import connect
from app.main import create_app
from app.model import ModelError


def config(tmp_path, **overrides):
    defaults = dict(database_path=tmp_path / "robust.sqlite3", study_mode="demo",
                    admin_password="test-password", model_api_key="", model_base_url="https://example.invalid/v1",
                    model_name="test-model", model_temperature=0, assignment_seed="fixed")
    return Settings(**(defaults | overrides))


def payload(**overrides):
    version, items = form_for("18-20")
    return dict(consent=True, age_band="18-20", education_stage="bachelor", topic_familiarity=3, student_confirmed=True,
                pretest_version=version, pretest_answers={item["id"]: item["correct"] for item in items}) | overrides


def auth():
    return {"Authorization": "Basic " + base64.b64encode(b"admin:test-password").decode()}


def route_result(card_id="B_MATERIALS"):
    return {"card_id": card_id, "clarify_slot": None, "source": "test", "status": "matched"}


def blocking_route(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    async def route(*args, **kwargs):
        entered.set()
        for _ in range(500):
            if release.is_set():
                return route_result()
            await asyncio.sleep(0.01)
        raise AssertionError("test did not release model")

    monkeypatch.setattr(main_module, "route_question", route)
    return entered, release


def test_start_retry_is_idempotent_and_payload_changes_are_rejected(tmp_path):
    app = create_app(config(tmp_path))
    client = TestClient(app)
    data = payload(request_id="same-start")
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [pool.submit(client.post, "/api/start", json=data) for _ in range(2)]
        results = [call.result(timeout=5) for call in calls]
    assert all(result.status_code == 200 for result in results)
    sid = results[0].json()["session_id"]
    assert {result.json()["session_id"] for result in results} == {sid}
    assert results[0].json()["messages"] == []
    assert results[0].json()["max_minutes"] == 30
    assert client.post("/api/start", json={**data, "topic_familiarity": 4}).status_code == 409
    with connect(app.state.settings.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_pending_turn_blocks_other_messages_and_decision_and_can_retry_idempotently(tmp_path, monkeypatch):
    app = create_app(config(tmp_path, max_turns=3))
    client = TestClient(app)
    sid = client.post("/api/start", json=payload()).json()["session_id"]
    for text in ("你好", "您好"):
        assert client.post(f"/api/session/{sid}/message", json={"content": text}).status_code == 200
    entered, release = blocking_route(monkeypatch)
    request = {"content": "报名材料有哪些？", "request_id": "third"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, f"/api/session/{sid}/message", json=request)
        assert entered.wait(3)
        try:
            state = client.get(f"/api/session/{sid}").json()
            assert state["pending_reply"] and state["pending_request_id"] == "third"
            assert state["completed_turns"] == 2 and state["user_turns"] == 3
            assert not state["can_enter_decision"]
            assert client.post(f"/api/session/{sid}/message", json=request).status_code == 409
            assert client.post(f"/api/session/{sid}/message", json={"content": "截止呢"}).status_code == 409
            assert client.post(f"/api/session/{sid}/decision").status_code == 409
            assert client.post(f"/api/session/{sid}/submit", json={}).status_code == 409
        finally:
            release.set()
        response = future.result(timeout=5)
    assert response.status_code == 200
    assert response.json()["completed_turns"] == 3 and response.json()["turns_remaining"] == 0
    assert client.post(f"/api/session/{sid}/message", json=request).status_code == 200
    assert len(client.get(f"/api/session/{sid}").json()["messages"]) == 6
    assert client.post(f"/api/session/{sid}/message", json={**request, "content": "不一样"}).status_code == 409
    assert client.post(f"/api/session/{sid}/message", json={"content": "第四轮"}).status_code == 409
    assert client.post(f"/api/session/{sid}/decision").status_code == 200


def test_deleting_session_during_reply_does_not_write_or_raise_foreign_key_error(tmp_path, monkeypatch):
    app = create_app(config(tmp_path))
    client = TestClient(app)
    sid = client.post("/api/start", json=payload()).json()["session_id"]
    entered, release = blocking_route(monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, f"/api/session/{sid}/message", json={"content": "报名材料有哪些"})
        assert entered.wait(3)
        try:
            assert client.delete(f"/api/session/{sid}").status_code == 200
        finally:
            release.set()
        assert future.result(timeout=5).status_code == 410
    assert client.get(f"/api/session/{sid}").status_code == 404
    with connect(app.state.settings.database_path) as db:
        for table in ("sessions", "messages", "model_events", "message_requests", "start_requests"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_failed_reply_does_not_consume_turn_or_leave_unanswered_user_and_same_id_retries(tmp_path, monkeypatch):
    app = create_app(config(tmp_path))
    client = TestClient(app)
    sid = client.post("/api/start", json=payload()).json()["session_id"]

    async def fail(*args, **kwargs):
        raise ModelError("simulated timeout")

    monkeypatch.setattr(main_module, "route_question", fail)
    data = {"content": "材料有哪些", "request_id": "retry-after-error"}
    assert client.post(f"/api/session/{sid}/message", json=data).status_code == 503
    state = client.get(f"/api/session/{sid}").json()
    assert state["messages"] == [] and state["completed_turns"] == 0 and not state["pending_reply"]
    raw = client.get("/api/admin/archive-export?format=json", headers=auth()).json()[0]
    attempts = json.loads(raw["message_requests_json"])
    assert attempts[0]["content"] == data["content"] and attempts[0]["status"] == "failed"

    async def succeed(*args, **kwargs):
        return route_result()

    monkeypatch.setattr(main_module, "route_question", succeed)
    response = client.post(f"/api/session/{sid}/message", json=data)
    assert response.status_code == 200 and response.json()["completed_turns"] == 1
    assert len(client.get(f"/api/session/{sid}").json()["messages"]) == 2


def test_expired_request_can_retry_without_old_attempt_overwriting_new_one(tmp_path, monkeypatch):
    app = create_app(config(tmp_path))
    client = TestClient(app)
    sid = client.post("/api/start", json=payload()).json()["session_id"]
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    calls = []

    async def delayed(*args, **kwargs):
        index = len(calls)
        calls.append(index)
        entered[index].set()
        for _ in range(500):
            if release[index].is_set():
                return route_result()
            await asyncio.sleep(0.01)
        raise AssertionError("test did not release model")

    monkeypatch.setattr(main_module, "route_question", delayed)
    data = {"content": "材料有哪些", "request_id": "recover"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(client.post, f"/api/session/{sid}/message", json=data)
        assert entered[0].wait(3)
        with connect(app.state.settings.database_path) as db:
            db.execute("UPDATE sessions SET pending_started_at=? WHERE id=?",
                       ((datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat(), sid))
        recovered = client.get(f"/api/session/{sid}").json()
        assert recovered["messages"] == [] and not recovered["pending_reply"]
        new = pool.submit(client.post, f"/api/session/{sid}/message", json=data)
        assert entered[1].wait(3)
        release[0].set()
        try:
            assert old.result(timeout=5).status_code == 409
            state = client.get(f"/api/session/{sid}").json()
            assert state["pending_reply"] and state["user_turns"] == 1
        finally:
            release[1].set()
        assert new.result(timeout=5).status_code == 200
    state = client.get(f"/api/session/{sid}").json()
    assert state["completed_turns"] == 1 and state["user_turns"] == 1 and not state["pending_reply"]


def test_timeout_allows_final_decision_even_before_two_turns(tmp_path):
    cfg = config(tmp_path, max_minutes=1)
    client = TestClient(create_app(cfg))
    sid = client.post("/api/start", json=payload()).json()["session_id"]
    with connect(cfg.database_path) as db:
        db.execute("UPDATE sessions SET created_at=? WHERE id=?",
                   ((datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat(), sid))
    state = client.get(f"/api/session/{sid}").json()
    assert state["timed_out"] and state["remaining_seconds"] == 0 and state["can_enter_decision"]
    assert client.post(f"/api/session/{sid}/message", json={"content": "你好"}).status_code == 409
    response = client.post(f"/api/session/{sid}/decision")
    assert response.status_code == 200
    assert client.post(f"/api/session/{sid}/submit", json={
        "decision_choice_id": response.json()["decision_options"][0]["id"], "execution_choice_id": response.json()["execution_options"][0]["id"]}).status_code == 200


def test_model_configuration_change_starts_fresh_assignment_cohort(tmp_path):
    cfg = config(tmp_path, group_cap=1)
    client = TestClient(create_app(cfg))
    for _ in range(2):
        assert client.post("/api/start", json=payload()).status_code == 200
    assert client.post("/api/start", json=payload()).status_code == 409
    changed = TestClient(create_app(replace(cfg, model_name="another-model")))
    response = changed.post("/api/start", json=payload())
    assert response.status_code == 200
    assert response.json()["model_config_id"] != client.get("/api/info").json()["model_config_id"]


def test_pretest_rejects_coerced_boolean_and_numeric_string_and_blank_education(tmp_path):
    client = TestClient(create_app(config(tmp_path)))
    for invalid in (True, "1", 1.0):
        data = payload()
        data["pretest_answers"][next(iter(data["pretest_answers"]))] = invalid
        assert client.post("/api/start", json=data).status_code == 422
    assert client.post("/api/start", json=payload(education_stage="  ")).status_code == 422


def test_unicode_access_code_does_not_raise_type_error(tmp_path):
    client = TestClient(create_app(config(tmp_path, participant_access_code="实验邀请码")))
    assert client.post("/api/start", json=payload(access_code="错误")).status_code == 403
    assert client.post("/api/start", json=payload(access_code="实验邀请码")).status_code == 200


def test_csv_export_neutralizes_spreadsheet_formulas_and_preserves_json(tmp_path):
    cfg = config(tmp_path)
    client = TestClient(create_app(cfg))
    assert client.post("/api/start", json=payload(education_stage="=1+1")).status_code == 422
    sid = client.post("/api/start", json=payload()).json()["session_id"]
    # Imported historical text still requires escaping even though new intake
    # cannot accept arbitrary education values.
    with connect(cfg.database_path) as db:
        db.execute("UPDATE sessions SET education_stage='=1+1' WHERE id=?", (sid,))
    raw_json = client.get("/api/admin/export?format=json", headers=auth()).json()[0]
    assert raw_json["education_stage"] == "=1+1"
    raw_csv = client.get("/api/admin/export?format=csv", headers=auth()).text.lstrip("\ufeff")
    row = next(csv.DictReader(io.StringIO(raw_csv)))
    assert row["教育阶段"] == "'=1+1"
    archived_csv = client.get("/api/admin/archive-export?format=csv", headers=auth()).text.lstrip("\ufeff")
    assert next(csv.DictReader(io.StringIO(archived_csv)))["education_stage"] == "'=1+1"


@pytest.mark.parametrize("failure_mode,expected_flag", [("unavailable", "technical_interruption"),
                                                        ("fallback", "model_fallback_used")])
def test_router_technical_failure_and_fallback_are_visible_to_reviewers(tmp_path, monkeypatch, failure_mode, expected_flag):
    app = create_app(config(tmp_path))
    client = TestClient(app)
    sid = client.post("/api/start", json=payload()).json()["session_id"]

    async def route(*args, **kwargs):
        if failure_mode == "unavailable":
            return {"card_id": None, "status": "unavailable", "source": "keyword_fallback", "error_type": "ModelError"}
        return {**route_result(), "source": "keyword_fallback", "error_type": "ValueError"}

    monkeypatch.setattr(main_module, "route_question", route)
    response = client.post(f"/api/session/{sid}/message", json={"content": "材料有哪些", "request_id": "failure-mode"})
    if failure_mode == "unavailable":
        assert response.status_code == 503
        state = client.get(f"/api/session/{sid}").json()
        assert state["user_turns"] == state["completed_turns"] == 0 and not state["pending_reply"]
        assert state["completed_request_ids"] == []
    else:
        assert response.status_code == 200 and response.json()["action"] == "evidence"
        assert client.get(f"/api/session/{sid}").json()["completed_request_ids"] == ["failure-mode"]
    for text in ("你好", "您好"):
        assert client.post(f"/api/session/{sid}/message", json={"content": text}).status_code == 200
    decisions = client.post(f"/api/session/{sid}/decision").json()
    assert client.post(f"/api/session/{sid}/submit", json={"decision_choice_id": decisions["decision_options"][0]["id"], "execution_choice_id": decisions["execution_options"][0]["id"]}).status_code == 200
    review = client.get("/api/admin/review-queue", headers=auth()).json()[0]
    assert expected_flag in review["flags"]
