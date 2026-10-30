"""Provider-boundary, frozen-cohort and concurrent-transaction regression checks."""
from __future__ import annotations

import asyncio
import csv
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
import io
import threading
import uuid

from fastapi.testclient import TestClient

from app.main import create_app
from app.translation_materials import FORM_ITEMS, SELF_ITEMS, FORM_VERSION, TASK
from app.translation_model import ACTIVE_CLARIFICATION_POLICY, dialogue_message_protocol, dialogue_prompt
from app.translation_quality import RUBRIC
from test_flow import auth, settings
from translation_multi_model_helpers import configure_panel


SUBMISSION = "玛拉来到钟楼宅时，罗温正在门边等着，掌心握着小小的银狐。钟响了两声，两个人谁也没动。"


def _payload(**changes):
    value = dict(consent=True, university_confirmed=True, age_band="21", education_stage="bachelor", undergraduate_year="year-3",
                 translation_experience=3, pretest_version=FORM_VERSION,
                 answers={item["id"]: item["correct"] for item in FORM_ITEMS},
                 self_answers={item["id"]: 2 for item in SELF_ITEMS}, request_id=str(uuid.uuid4()))
    return {**value, **changes}


def _start(client, condition="S", *, preview=True, **changes):
    if preview:
        changes.update(researcher_preview=True, preview_condition=condition)
    response = client.post("/api/translation/start", json=_payload(**changes), headers=auth() if preview else {})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _message(client, sid, request_id=None, content="请翻译这段小说。DIALOGUE_CANARY_81"):
    return client.post(f"/api/translation/session/{sid}/message", json={
        "request_id": request_id or str(uuid.uuid4()), "content": content}, headers=auth())


def _submit(client, sid):
    response = client.post(f"/api/translation/session/{sid}/submit", json={"submission": SUBMISSION}, headers=auth())
    assert response.status_code == 200, response.text


def _record(client, sid):
    response = client.get(f"/api/admin/translation/records/{sid}", headers=auth())
    assert response.status_code == 200, response.text
    return response.json()


class RecordingProvider:
    def __init__(self):
        self.calls = []
        self.bad_dialogue = False
        self.bad_judge = None

    async def complete(self, messages, *, temperature=None):
        self.calls.append(dict(messages=deepcopy(messages), temperature=temperature))
        system = messages[0]["content"]
        if system.startswith("你是翻译语境检查模块"):
            return json.dumps({"ready": False, "gaps": [{"issue": "指代", "question": "罗温是几个人？",
                             "options": ["一个人", "多个人", "不确定"]}]}, ensure_ascii=False)
        if system.startswith("你是英译中成稿的盲评模型"):
            if self.bad_judge:
                self.bad_judge = None
                return "{}"
            return json.dumps({"scores": {item["id"]: 80 for item in RUBRIC},
                               "evidence": ["译文保留了钟响两声与两人未动的动作。"],
                               "critical_errors": []}, ensure_ascii=False)
        return json.dumps({"kind": [] if self.bad_dialogue else "answer", "content": SUBMISSION}, ensure_ascii=False)


class BlockingProvider(RecordingProvider):
    def __init__(self, block_judges=False):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.block_judges = block_judges

    async def complete(self, messages, *, temperature=None):
        is_judge = messages[0]["content"].startswith("你是英译中成稿的盲评模型")
        if is_judge == self.block_judges:
            self.started.set()
            if not await asyncio.to_thread(self.release.wait, 5):
                raise RuntimeError("test gate timed out")
        return await super().complete(messages, temperature=temperature)


def test_baseline_clarification_never_calls_optimizer_or_reads_hidden_answers(tmp_path):
    app = create_app(settings(tmp_path, "pilot"))
    provider = RecordingProvider()
    client = TestClient(app)
    configure_panel(app, client, provider=provider)
    app.state.translation_provider = provider
    sid = _start(client, "A")
    assert _message(client, sid).status_code == 200
    assert len(provider.calls) == 1
    dialogue = provider.calls[0]["messages"]
    assert dialogue[0]["content"].startswith("你是英译中翻译助手")
    assert "优化模块" not in dialogue[0]["content"]
    wire = json.dumps(provider.calls[0], ensure_ascii=False)
    assert sid not in wire and FORM_VERSION not in wire
    assert TASK["reference_translation"] not in wire
    assert all(card["body"] not in wire for card in TASK["context_cards"])
    assert not any(f'"{key}"' in wire for key in ("band", "condition", "answers", "self_answers", "ability_score"))
    materials = client.get("/api/admin/translation/materials", headers=auth()).json()
    assert set(materials["policies"]) == {"S", "A"}
    assert "optimizer_prompt" not in materials and "optimizer" not in materials["versions"]
    assert not _record(client, sid)["events"][-1].get("optimizer")


def test_dialogue_wire_order_is_frozen_and_only_active_adds_a_trailing_system_reminder(tmp_path):
    for condition in ("S", "A"):
        location = tmp_path / condition
        location.mkdir()
        app = create_app(settings(location, "pilot"))
        provider = RecordingProvider()
        client = TestClient(app)
        app.state.translation_provider = provider
        sid = _start(client, condition)
        first_text = "请帮助完成这段小说的翻译。"
        second_text = "我明确无法再补充背景，请据已知信息给完整译文。"
        assert _message(client, sid, content=first_text).status_code == 200
        first_record = _record(client, sid)
        assert _message(client, sid, content=second_text).status_code == 200
        wire = provider.calls[1]["messages"]
        original_history = [{key: message[key] for key in ("role", "content")}
                            for message in first_record["messages"]] + [{"role": "user", "content": second_text}]
        protocol = first_record["material_snapshot"]["message_protocol"]
        assert protocol == dialogue_message_protocol()
        assert wire == [{"role": "system", "content": dialogue_prompt(TASK, condition)}] + original_history + protocol["conditions"][condition]["trailing_messages"]
        if condition == "A":
            assert wire[-1] == {"role": "system", "content": ACTIVE_CLARIFICATION_POLICY}
        else:
            assert wire[-1] == original_history[-1]
        latest = _record(client, sid)
        assert len(latest["messages"]) == 4
        assert all(message["role"] in {"user", "assistant"} for message in latest["messages"])
        materials = client.get("/api/admin/translation/materials", headers=auth()).json()
        assert materials["message_protocol"] == protocol
        assert materials["versions"]["prompt"] == "translation-dialogue-v3"


def test_five_judges_receive_same_blind_submission_and_frozen_common_background(tmp_path):
    app = create_app(settings(tmp_path, "pilot"))
    provider = RecordingProvider()
    client = TestClient(app)
    configure_panel(app, client, provider=provider)
    app.state.translation_provider = provider
    sid = _start(client, "A")
    assert _message(client, sid).status_code == 200
    _submit(client, sid)
    dialogue_calls = len(provider.calls)
    response = client.post(f"/api/admin/translation/records/{sid}/evaluate", headers=auth())
    assert response.status_code == 200, response.text
    calls = provider.calls[dialogue_calls:]
    assert len(calls) == 5
    assert len({call["messages"][0]["content"] for call in calls}) == 1
    assert len({call["messages"][1]["content"] for call in calls}) == 1
    for call in calls:
        assert call["temperature"] == 0
        system = call["messages"][0]["content"]
        assert TASK["source_text"] in system and TASK["reference_translation"] in system
        assert all(card["body"] in system for card in TASK["context_cards"])
        wire = json.dumps(call, ensure_ascii=False)
        assert sid not in wire and "DIALOGUE_CANARY_81" not in wire and FORM_VERSION not in wire
        assert not any(f'"{key}"' in wire for key in ("band", "condition", "answers", "self_answers", "ability_score"))
        data = call["messages"][1]["content"].split("\n", 1)[1]
        assert json.loads(data) == {"translation": SUBMISSION}
    assessment = response.json()
    assert assessment["score"] == 80 and len(assessment["ratings"]) == 5
    replay = client.post(f"/api/admin/translation/records/{sid}/evaluate", headers=auth())
    assert replay.json() == assessment and len(provider.calls) == dialogue_calls + 5


def test_invalid_dialogue_rolls_back_then_same_request_can_retry_without_duplication(tmp_path):
    app = create_app(settings(tmp_path, "pilot"))
    provider = RecordingProvider()
    client = TestClient(app)
    configure_panel(app, client, provider=provider)
    app.state.translation_provider = provider
    sid = _start(client)
    request_id = str(uuid.uuid4())
    provider.bad_dialogue = True
    failed = _message(client, sid, request_id)
    assert failed.status_code == 502
    row = _record(client, sid)
    assert row["messages"] == [] and row["pending"] is None
    assert row["requests"][request_id]["status"] == "failed"
    provider.bad_dialogue = False
    retried = _message(client, sid, request_id)
    assert retried.status_code == 200 and len(retried.json()["messages"]) == 2
    count = len(provider.calls)
    assert _message(client, sid, request_id).status_code == 200
    assert len(provider.calls) == count
    assert _message(client, sid, request_id, "不同内容").status_code == 409


def test_partial_judge_failure_never_produces_a_partial_average_and_can_retry(tmp_path):
    app = create_app(settings(tmp_path, "pilot"))
    provider = RecordingProvider()
    client = TestClient(app)
    configure_panel(app, client, provider=provider)
    app.state.translation_provider = provider
    sid = _start(client)
    assert _message(client, sid).status_code == 200
    _submit(client, sid)
    provider.bad_judge = True
    response = client.post(f"/api/admin/translation/records/{sid}/evaluate", headers=auth())
    assert response.status_code == 502
    row = _record(client, sid)
    assert row["assessment"] is None and row["evaluation_pending"] is None
    assert row["assessment_status"] == "failed"
    assert row["events"][-1]["type"] == "evaluation_failed" and row["events"][-1]["successes"] == 4
    provider.bad_judge = None
    response = client.post(f"/api/admin/translation/records/{sid}/evaluate", headers=auth())
    assert response.status_code == 200 and response.json()["score"] == 80


def test_inflight_message_blocks_duplicate_work_and_submit_but_preserves_saved_draft(tmp_path):
    app = create_app(settings(tmp_path, "pilot"))
    provider = BlockingProvider()
    client = TestClient(app)
    configure_panel(app, client, provider=provider)
    app.state.translation_provider = provider
    sid = _start(client)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(_message, client, sid)
        try:
            assert provider.started.wait(3)
            assert _message(client, sid).status_code == 409
            assert client.post(f"/api/translation/session/{sid}/submit", json={"submission": SUBMISSION}, headers=auth()).status_code == 409
            saved = client.put(f"/api/translation/session/{sid}/draft", json={"draft": "生成过程中保存的草稿"}, headers=auth())
            assert saved.status_code == 200
        finally:
            provider.release.set()
        result = first.result(timeout=5)
    assert result.status_code == 200 and result.json()["draft"] == "生成过程中保存的草稿"
    assert len(result.json()["messages"]) == 2 and len(provider.calls) == 1


def test_concurrent_panel_evaluation_only_runs_one_multi_model_panel(tmp_path):
    app = create_app(settings(tmp_path, "pilot"))
    provider = BlockingProvider(block_judges=True)
    client = TestClient(app)
    configure_panel(app, client, provider=provider)
    app.state.translation_provider = provider
    sid = _start(client)
    assert _message(client, sid).status_code == 200
    _submit(client, sid)
    endpoint = f"/api/admin/translation/records/{sid}/evaluate"
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.post, endpoint, headers=auth())
        try:
            assert provider.started.wait(3)
            assert client.post(endpoint, headers=auth()).status_code == 409
        finally:
            provider.release.set()
        result = first.result(timeout=5)
    assert result.status_code == 200 and len(result.json()["ratings"]) == 5
    assert len(provider.calls) == 6  # one translation and exactly five judgments


def test_model_change_invalidates_freeze_prevents_old_session_work_and_separates_cohort(tmp_path):
    cfg = replace(settings(tmp_path, "live"), participant_access_code="live-join")
    app = create_app(cfg)
    first = TestClient(app)
    provider = RecordingProvider()
    configure_panel(app, first, provider=provider)
    app.state.translation_provider = provider
    assert first.post("/api/admin/translation/materials/freeze", json={"confirmed": True}, headers=auth()).status_code == 200
    sid = _start(first, preview=False, access_code="live-join")
    assert _message(first, sid).status_code == 200
    _submit(first, sid)
    original = first.get("/api/admin/translation/materials", headers=auth()).json()
    # The private registry overrides environment defaults after a configuration
    # has been saved. Change the effective model through the supported hot API.
    public = first.get("/api/admin/translation/models", headers=auth()).json()["dialogue"]
    configured = first.put("/api/admin/translation/models", json={"dialogue": {
        "model": public["model"], "base_url": public["base_url"], "temperature": 0.5,
        "max_tokens": public["max_tokens"]}}, headers=auth())
    assert configured.status_code == 200, configured.text
    changed = TestClient(create_app(cfg))
    updated = changed.get("/api/admin/translation/materials", headers=auth()).json()
    assert original["material_id"] == updated["material_id"]
    assert original["cohort_id"] != updated["cohort_id"] and not changed.get("/api/translation/info").json()["ready"]
    assert _message(changed, sid).status_code == 409
    assert changed.post(f"/api/admin/translation/records/{sid}/evaluate", headers=auth()).status_code == 409
    summary = changed.get("/api/admin/translation/summary", headers=auth()).json()
    assert summary["counts"]["total"] == 0 and summary["counts"]["other_cohorts"] == 1


def test_material_contents_are_fingerprinted_even_when_version_labels_match(tmp_path, monkeypatch):
    cfg = settings(tmp_path, "pilot")
    first = TestClient(create_app(cfg))
    original = first.get("/api/admin/translation/materials", headers=auth()).json()
    monkeypatch.setitem(FORM_ITEMS[0], "rationale", FORM_ITEMS[0]["rationale"] + " 候选修订。")
    changed = TestClient(create_app(cfg))
    updated = changed.get("/api/admin/translation/materials", headers=auth()).json()
    assert original["versions"] == updated["versions"]
    assert original["material_id"] != updated["material_id"]
    assert original["cohort_id"] != updated["cohort_id"]


def test_limits_change_invalidates_freeze_but_old_session_keeps_original_limits(tmp_path):
    cfg = replace(settings(tmp_path, "live"), participant_access_code="live-join", max_turns=8, max_minutes=20)
    app = create_app(cfg)
    first = TestClient(app)
    configure_panel(app, first)
    first.post("/api/admin/translation/materials/freeze", json={"confirmed": True}, headers=auth())
    sid = _start(first, preview=False, access_code="live-join")
    original = first.get("/api/admin/translation/materials", headers=auth()).json()
    changed = TestClient(create_app(replace(cfg, max_turns=10, max_minutes=25)))
    updated = changed.get("/api/admin/translation/materials", headers=auth()).json()
    assert original["material_id"] == updated["material_id"]
    assert original["cohort_id"] != updated["cohort_id"]
    assert not updated["frozen"] and not updated["ready"]
    restored = changed.get(f"/api/translation/session/{sid}")
    assert restored.status_code == 200
    assert restored.json()["limits"] == {"max_turns": 8, "max_minutes": 20}
    assert restored.json()["remaining_seconds"] <= 20 * 60


def test_rating_exports_keep_totals_and_researcher_preview_provenance(tmp_path):
    app = create_app(settings(tmp_path, "pilot"))
    client = TestClient(app)
    provider = RecordingProvider()
    configure_panel(app, client, provider=provider)
    app.state.translation_provider = provider
    sid = _start(client)
    assert _message(client, sid).status_code == 200
    _submit(client, sid)
    assert client.post(f"/api/admin/translation/records/{sid}/evaluate", headers=auth()).status_code == 200
    response = client.get("/api/admin/translation/export?kind=ratings&format=json", headers=auth())
    assert response.status_code == 200
    ratings = response.json()
    assert len(ratings) == 5
    assert all(row["record_kind"] == "researcher_preview" and row["study_mode"] == "pilot" for row in ratings)
    assert all(row["total"] == 80 for row in ratings)
    response = client.get("/api/admin/translation/export?kind=ratings&format=csv", headers=auth())
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert len(rows) == 5
    assert all(row["record_kind"] == "researcher_preview" and row["study_mode"] == "pilot" for row in rows)
    assert all(float(row["total"]) == 80 for row in rows)


def test_session_sync_reports_committed_request_ids_and_sensitive_responses_are_not_cached(tmp_path):
    app = create_app(settings(tmp_path, "pilot"))
    client = TestClient(app)
    provider = RecordingProvider()
    configure_panel(app, client, provider=provider)
    app.state.translation_provider = provider
    sid = _start(client)
    request_id = str(uuid.uuid4())
    assert _message(client, sid, request_id).status_code == 200
    response = client.get(f"/api/translation/session/{sid}", headers=auth())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["completed_request_ids"] == [request_id] and body["pending_request_id"] is None
    saved = client.put(f"/api/translation/session/{sid}/draft", json={"draft": SUBMISSION}, headers=auth())
    assert saved.status_code == 200 and saved.json()["draft_saved_at"]


def test_deleting_session_during_provider_work_cannot_recreate_data(tmp_path):
    app = create_app(settings(tmp_path, "pilot"))
    provider = BlockingProvider()
    client = TestClient(app)
    configure_panel(app, client, provider=provider)
    app.state.translation_provider = provider
    sid = _start(client)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(_message, client, sid)
        try:
            assert provider.started.wait(3)
            deleted = client.delete(f"/api/translation/session/{sid}", headers=auth())
            assert deleted.status_code == 200
        finally:
            provider.release.set()
        assert first.result(timeout=5).status_code == 404
    assert client.get(f"/api/translation/session/{sid}", headers=auth()).status_code == 404
    assert client.get("/api/admin/translation/records", headers=auth()).json()["records"] == []
