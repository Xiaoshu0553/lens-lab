"""Authenticated real-model previews stay separate from showcase fixtures."""

import copy
import json
from dataclasses import replace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app.config as config_module
from app.db import connect
from app.main import create_app
from app.model import answer_system_prompt, prompt_only_system_prompt
from test_flow import auth, settings, start_payload


class PreviewProvider:
    def __init__(self):
        self.calls = []

    async def complete(self, messages, *, temperature=None):
        self.calls.append(copy.deepcopy(messages))
        if "此前从未" in messages[-1]["content"]:
            return "陈禾应选择登记甲和弹性安排，因为此前未获得名额且每周最多到场一次。"
        if "额外的澄清策略（主动澄清组）" in messages[0]["content"]:
            return "【澄清提问】陈禾此前获得过本项目活动名额吗？"
        return "登记类别取决于此前是否获得名额，参加安排取决于每周最多可到场次数。"


@pytest.mark.parametrize("prefix", ["", "/preview-api"])
def test_preview_html_supplies_only_the_configured_same_origin_api_prefix(tmp_path, prefix):
    client = TestClient(create_app(replace(settings(tmp_path, "pilot"), preview_api_prefix=prefix)))
    assert client.get("/admin/preview", follow_redirects=False).status_code == 303
    page = client.get("/admin/preview", headers=auth())
    assert page.status_code == 200
    assert 'data-researcher-preview="true"' in page.text
    if prefix:
        assert f'data-api-prefix="{prefix}"' in page.text
    else:
        assert "data-api-prefix" not in page.text
    assert "data-api-prefix" not in client.get("/").text


@pytest.mark.parametrize("minor_code", ["", "minor-invitation"])
def test_only_authenticated_preview_info_opens_fictional_age_forms(tmp_path, minor_code):
    config = replace(settings(tmp_path, "pilot"), participant_access_code="participant-invitation",
                     minor_access_code=minor_code)
    client = TestClient(create_app(config))
    ordinary = client.get("/api/info").json()
    assert ordinary["researcher_preview"] is False
    assert ordinary["minor_recruitment_open"] is False
    assert ordinary["under14_recruitment_open"] is False
    assert ordinary["minor_access_code_required"] is False
    assert ordinary["access_code_required"] is True

    assert client.get("/api/info?researcher_preview=true").status_code == 401
    response = client.get("/api/info?researcher_preview=true", headers=auth())
    assert response.status_code == 200
    preview = response.json()
    assert preview["researcher_preview"] is True
    assert preview["study_mode"] == "pilot" and preview["showcase_mode"] is False
    assert preview["minor_recruitment_open"] is False
    assert preview["under14_recruitment_open"] is False
    assert preview["minor_access_code_required"] is False
    assert preview["access_code_required"] is False


@pytest.mark.parametrize("age", ["18-20", "24-25"])
def test_authenticated_fictional_preview_bypasses_recruitment_codes_not_consent(tmp_path, age):
    config = replace(settings(tmp_path, "pilot"), participant_access_code="participant-invitation")
    client = TestClient(create_app(config))
    payload = start_payload(age_band=age)
    # The participant route is still closed even if its ordinary invitation is supplied.
    assert client.post("/api/start", json=payload).status_code == 403
    preview = {**payload, "researcher_preview": True}
    assert client.post("/api/start", json=preview).status_code == 401
    assert client.post("/api/start", headers=auth(), json={**preview, "consent": False}).status_code == 400
    started = client.post("/api/start", headers=auth(), json=preview)
    assert started.status_code == 200, started.text
    assert started.json()["run_mode"] == "pilot"
    with connect(config.database_path) as db:
        row = dict(db.execute("SELECT * FROM sessions").fetchone())
    assert row["record_kind"] == "researcher_preview"
    assert row["run_mode"] == "pilot" and row.get("is_synthetic", 0) == 0


@pytest.mark.parametrize("unchecked", ["consent", "student_confirmed"])
def test_student_preview_still_requires_consent_and_current_enrollment(tmp_path, unchecked):
    client = TestClient(create_app(settings(tmp_path, "pilot")))
    payload = {**start_payload(age_band="18-20"), "researcher_preview": True, unchecked: False}
    assert client.post("/api/start", headers=auth(), json=payload).status_code == (400 if unchecked == "consent" else 422)


def test_preview_arms_use_real_dialogue_and_stay_outside_participant_analysis(tmp_path):
    config = replace(settings(tmp_path, "pilot"), preview_api_prefix="/preview-api",
                     participant_access_code="participant-invitation", researcher_preview_only=True)
    app = create_app(config)
    provider = PreviewProvider()
    app.state.provider = provider
    client = TestClient(app)
    starts = [client.post("/api/start", headers=auth(), json={
        **start_payload(age_band="21-23"), "researcher_preview": True}) for _ in range(2)]
    assert all(response.status_code == 200 for response in starts)
    sessions = [response.json() for response in starts]
    assert {session["group_id"] for session in sessions} == {"S", "A"}

    for session in sessions:
        sid = session["session_id"]
        assert session["run_mode"] == "pilot"
        with connect(config.database_path) as db:
            saved = dict(db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())
        assert saved["record_kind"] == "researcher_preview" and saved.get("is_synthetic", 0) == 0
        task = json.loads(saved["task_snapshot_json"])
        system = prompt_only_system_prompt(task) if session["group_id"] == "A" else answer_system_prompt(task)
        question = "陈禾应该选哪种登记类别和参加安排？"
        first = client.post(f"/api/session/{sid}/message", headers=auth(), json={"content": question})
        assert first.status_code == 200, first.text
        assert provider.calls[-1] == [{"role": "system", "content": system}, {"role": "user", "content": question}]
        assert first.json()["action"] == ("prompt_clarification" if session["group_id"] == "A" else "answer")
        facts = "陈禾此前从未获得过本项目活动名额，而且每周最多能到场一次。请继续回答原问题。"
        second = client.post(f"/api/session/{sid}/message", headers=auth(), json={"content": facts})
        assert second.status_code == 200, second.text
        assert second.json()["action"] == "answer" and "登记甲和弹性安排" in second.json()["content"]
        assert provider.calls[-1] == [
            {"role": "system", "content": system}, {"role": "user", "content": question},
            {"role": "assistant", "content": ("【澄清提问】" if first.json()["action"] == "prompt_clarification" else "") + first.json()["content"]},
            {"role": "user", "content": facts}]
        decisions = client.post(f"/api/session/{sid}/decision", headers=auth())
        assert decisions.status_code == 200, decisions.text
        options = decisions.json()
        submitted = client.post(f"/api/session/{sid}/submit", headers=auth(), json={
            "decision_choice_id": options["decision_options"][0]["id"],
            "execution_choice_id": options["execution_options"][0]["id"]})
        assert submitted.status_code == 200, submitted.text

    assert len(provider.calls) == 4
    summary = client.get("/api/admin/summary?age_band=21-23", headers=auth()).json()
    assert summary["analysis_n"] == 0 and summary["groups"] == []
    assert client.get("/api/admin/export?kind=analysis&format=json", headers=auth()).json() == []
    archive = client.get("/api/admin/archive-export?kind=raw&format=json", headers=auth()).json()
    assert len(archive) == 2 and all(row["record_kind"] == "researcher_preview" for row in archive)


def test_preview_only_instance_authenticates_every_api_and_rejects_participant_creation(tmp_path):
    config = settings(tmp_path, "pilot")
    ordinary = TestClient(create_app(config))
    started = ordinary.post("/api/start", json=start_payload())
    assert started.status_code == 200
    sid = started.json()["session_id"]
    client = TestClient(create_app(replace(config, researcher_preview_only=True)))
    for path in ("/api/info", "/api/info?researcher_preview=true", "/api/pretest?age_band=18-20",
                 f"/api/session/{sid}"):
        response = client.get(path)
        assert response.status_code == 401, (path, response.text)
    for path, body in (("/api/start", start_payload()),
                       ("/api/start", {**start_payload(), "researcher_preview": True}),
                       (f"/api/session/{sid}/message", {"content": "需要什么材料？"})):
        assert client.post(path, json=body).status_code == 401
    assert client.get(f"/api/session/{sid}", headers=auth()).status_code == 200
    assert client.post("/api/start", headers=auth(), json=start_payload()).status_code == 403
    assert client.post("/api/start", headers=auth(), json={
        **start_payload(), "researcher_preview": True}).status_code == 200
    # The login endpoint remains available to obtain the required browser session.
    assert client.post("/api/admin/login", json={"username": "admin", "password": "test-password"}).status_code == 200


@pytest.mark.parametrize("prefix", ["https://other.invalid", "//other.invalid", "/preview/..",
                                    "/preview?target=other", "/preview#other", "/preview\nother"])
def test_unsafe_preview_prefixes_are_rejected_from_env_and_direct_settings(tmp_path, monkeypatch, prefix):
    monkeypatch.setattr(config_module, "load_dotenv", lambda: None)
    with patch.dict("os.environ", {"ADMIN_PASSWORD": "test-password", "PREVIEW_API_PREFIX": prefix}, clear=True):
        with pytest.raises(ValueError):
            config_module.Settings.from_env()
    with pytest.raises(ValueError):
        create_app(replace(settings(tmp_path), preview_api_prefix=prefix))


@pytest.mark.parametrize("flag,expected", [("0", False), ("1", True)])
def test_preview_only_environment_flag_and_same_origin_prefix_are_explicit(monkeypatch, flag, expected):
    monkeypatch.setattr(config_module, "load_dotenv", lambda: None)
    with patch.dict("os.environ", {"ADMIN_PASSWORD": "test-password", "RESEARCHER_PREVIEW_ONLY": flag,
                                   "PREVIEW_API_PREFIX": "/research/preview-api_2"}, clear=True):
        config = config_module.Settings.from_env()
    assert config.researcher_preview_only is expected
    assert config.preview_api_prefix == "/research/preview-api_2"


@pytest.mark.parametrize("flag", ["", "true", "2"])
def test_invalid_preview_only_environment_flag_is_rejected(monkeypatch, flag):
    monkeypatch.setattr(config_module, "load_dotenv", lambda: None)
    with patch.dict("os.environ", {"ADMIN_PASSWORD": "test-password", "RESEARCHER_PREVIEW_ONLY": flag}, clear=True):
        with pytest.raises(ValueError):
            config_module.Settings.from_env()
