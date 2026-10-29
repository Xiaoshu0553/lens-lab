"""Offline checks of sampling and HTTP recovery; these are not live API evidence."""
from collections import Counter
import importlib.util
import json
from pathlib import Path

import pytest

from app.translation_materials import FORM_ITEMS, SELF_ITEMS, FORM_VERSION, TASK_VERSION, public_form, score_form

SPEC = importlib.util.spec_from_file_location("translation_sample_generator", Path(__file__).parents[1] / "scripts" / "generate_translation_samples.py")
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)


def test_sample_plan_has_complete_reproducible_questionnaires_and_predeclared_strata():
    plans = generator.make_plans(24, 20261030)
    assert plans == generator.make_plans(24, 20261030)
    assert Counter(plan["target_band"] for plan in plans) == {"low": 6, "medium": 12, "high": 6}
    assert len({plan["start_payload"]["request_id"] for plan in plans}) == 24
    # Equal objective patterns (especially a perfect score) can legitimately recur.
    assert len({json.dumps(plan["start_payload"]["answers"], sort_keys=True) for plan in plans}) >= 12
    for plan in plans:
        payload = plan["start_payload"]
        assert len(payload["answers"]) == 18 and len(payload["self_answers"]) == 6
        assert score_form(payload["answers"], payload["self_answers"]) == plan["ability"]
        assert plan["ability"]["band"] == plan["target_band"]
        assert payload["education_stage"] == "bachelor"
        assert payload["age_band"] in {"18", "19", "20", "21", "22", "23-plus"}
        assert payload["undergraduate_year"] in {"year-1", "year-2", "year-3", "year-4", "year-5-plus"}
        assert 2 <= plan["planned_turns"] <= 4
        assert 1 <= payload["translation_experience"] <= 5
        assert not {"access_code", "preview_condition", "condition", "assessment", "score"}.intersection(payload)
    assert "not an estimate" in generator.METHOD["distribution_basis"]
    assert "No target translation scores" in generator.METHOD["quality"]


def test_seed_changes_questionnaire_draws_without_changing_target_quotas():
    first, second = generator.make_plans(24, 1), generator.make_plans(24, 2)
    assert first != second
    assert Counter(plan["target_band"] for plan in first) == Counter(plan["target_band"] for plan in second)
    assert len({plan["ability"]["score"] for plan in first}) > 6


@pytest.mark.parametrize("count", [0, 19, 31])
def test_out_of_scope_batch_sizes_are_rejected(count):
    with pytest.raises(generator.SampleError):
        generator.make_plans(count, 1)


def test_manifest_is_atomic_private_resumable_and_does_not_store_execution_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "PRIVATE-ADMIN-SECRET")
    monkeypatch.setenv("PARTICIPANT_ACCESS_CODE", "PRIVATE-INVITE-SECRET")
    path = tmp_path / "private" / "batch.json"
    original = generator.open_manifest(path, "https://study.example.org", 24, 7)
    original["samples"][0]["session_id"] = "stable-existing-session"
    generator.save_manifest(path, original)
    restored = generator.open_manifest(path, "https://study.example.org", 24, 7)
    assert restored == original
    assert path.stat().st_mode & 0o777 == 0o600
    assert "PRIVATE-ADMIN-SECRET" not in path.read_text()
    assert "PRIVATE-INVITE-SECRET" not in path.read_text()
    assert not list(path.parent.glob(".translation-samples-*"))
    with pytest.raises(generator.SampleError):
        generator.open_manifest(path, "https://other.example.org", 24, 7)
    with pytest.raises(generator.SampleError):
        generator.open_manifest(path, "https://study.example.org", 24, 8)


def test_manifest_lock_prevents_a_concurrent_writer_and_releases_on_exit(tmp_path):
    path = tmp_path / "batch.json"
    with generator.manifest_lock(path):
        with pytest.raises(generator.SampleError):
            with generator.manifest_lock(path):
                pytest.fail("a second writer entered")
    with generator.manifest_lock(path):
        pass


def _preflight_info():
    return {"data_purpose": "demonstration", "demonstration_mode": True,
            "study_mode": "pilot", "dialogue_backend": "model_api", "access_code_required": True,
            "versions": {"form": FORM_VERSION, "task": TASK_VERSION},
            "participant_scope": {"education_stage": "bachelor", "minimum_age": 18,
                                  "age_bands": ["18", "19", "20", "21", "22", "23-plus"],
                                  "undergraduate_years": ["year-1", "year-2", "year-3", "year-4", "year-5-plus"]}}


def _platform_for_preflight(info=None, dialogue=None):
    platform = generator.Platform("http://127.0.0.1:8004", "not-a-real-password", "not-a-real-invite")
    platform.close()
    calls = []
    def request(method, path, payload=None, *, admin=False):
        calls.append((method, path, admin))
        return {"/api/translation/info": info or _preflight_info(),
                "/api/translation/form": public_form(),
                "/api/admin/translation/models": {"dialogue": dialogue or {
                    "base_url": "https://api.example.org/v1", "model": "different-cloud-model", "api_key_configured": True}}}[path]
    platform.request = request
    return platform, calls


@pytest.mark.parametrize("change", [{"data_purpose": "research"}, {"demonstration_mode": False}, {"study_mode": "demo"}, {"dialogue_backend": "rule_simulation"}])
def test_preflight_refuses_research_data_or_rule_simulation_before_any_mutation(change):
    platform, calls = _platform_for_preflight({**_preflight_info(), **change})
    with pytest.raises(generator.SampleError):
        platform.preflight()
    assert all(method == "GET" for method, _, _ in calls)


def test_preflight_requires_current_undergraduate_scope_and_a_remote_cloud_model():
    platform, _ = _platform_for_preflight(dialogue={"base_url": "http://127.0.0.1:11434/v1", "model": "local", "api_key_configured": False})
    with pytest.raises(generator.SampleError):
        platform.preflight()
    platform, calls = _platform_for_preflight()
    result = platform.preflight()
    assert result["data_purpose"] == "demonstration" and result["model"] == "different-cloud-model"
    assert calls[-1] == ("GET", "/api/admin/translation/models", True)
    wrong = _preflight_info()
    wrong["participant_scope"]["education_stage"] = "graduate"
    platform, _ = _platform_for_preflight(wrong)
    with pytest.raises(generator.SampleError):
        platform.preflight()


@pytest.mark.parametrize("url", ["https://admin:password@example.org", "https://example.org/?key=secret", "https://example.org/preview", "http://example.org", "file:///tmp/database"])
def test_platform_origin_cannot_embed_secrets_or_send_remote_plaintext_credentials(url):
    with pytest.raises(generator.SampleError):
        generator.normalize_base_url(url)


def test_context_messages_use_only_the_current_participant_view_cards_and_track_which_were_shared():
    plan = generator.make_plans(24, 6)[0]
    plan["target_band"] = "high"
    view = {"task": {"context_cards": [{"id": "characters", "title": "可见人物资料", "body": "此会话才有的事实。第二句。第三句。"}]},
            "messages": [{"role": "assistant", "action": "clarification", "content": "they是几个人？"}]}
    content, shared = generator.make_message(plan, view, 2, final=True)
    assert "此会话才有的事实" in content and shared == ["characters"]
    assert generator.FINAL_BEGIN in content and generator.FINAL_END in content
    assert "玛拉母亲" not in content and "银制徽记" not in content
    plan["provided_card_ids"] = shared
    content, shared = generator.make_message(plan, view, 3, final=True)
    assert "此会话才有的事实" not in content and shared == []


TRANSLATION = ("玛拉来到钟楼宅时，罗温正在门边等候，手里握着那枚小银狐。‘大人来得真早。’罗温说。"
               "玛拉越过罗温，望向那间灯仍亮着的屋子。‘那我们最好别吵醒她。’罗温将银狐在掌心翻了个面。"
               "‘它已经不属于她了。’一时间，旧日的约定横在两人之间。随后，钟响了两声，而两个人谁也没有动。")


def test_final_translation_uses_the_models_exact_text_and_rejects_clarification_or_explanations():
    message = {"action": "answer", "content": generator.FINAL_BEGIN + "\n" + TRANSLATION + "\n" + generator.FINAL_END}
    assert generator.extract_translation(message) == TRANSLATION
    for invalid in [{**message, "action": "clarification"}, {**message, "content": "请问Rowan是谁？"},
                    {**message, "content": "说明：" + message["content"]},
                    {**message, "content": generator.FINAL_BEGIN + "很短的中文。" + generator.FINAL_END}]:
        with pytest.raises(generator.SampleError):
            generator.extract_translation(invalid)


def test_recovered_completed_request_never_sends_a_duplicate_message():
    platform = generator.Platform("http://127.0.0.1:8004", "test-password", "test-code")
    platform.close()
    turn = {"request_id": "persisted-request", "content": "已保存的消息"}
    view = {"id": "known-session", "pending_reply": False, "completed_request_ids": [turn["request_id"]]}
    requests = []
    platform.request = lambda *args, **kwargs: requests.append(args)
    platform.session = lambda session_id: view
    assert platform.complete_round("known-session", turn) is view
    assert requests == []


def test_lost_message_response_is_recovered_using_the_original_request_id(monkeypatch):
    monkeypatch.setattr(generator.time, "sleep", lambda _: None)
    platform = generator.Platform("http://127.0.0.1:8004", "test-password", "test-code")
    platform.close()
    turn = {"request_id": "same-id-after-timeout", "content": "同一消息"}
    views = iter([{"id": "session", "pending_reply": False, "remaining_seconds": 1200, "completed_request_ids": []},
                  {"id": "session", "pending_reply": False, "remaining_seconds": 1199, "completed_request_ids": [turn["request_id"]]}])
    platform.session = lambda _: next(views)
    sent = []
    def lost_response(method, path, payload):
        sent.append(payload)
        raise generator.APIError(method, path, None)
    platform.request = lost_response
    recovered = platform.complete_round("session", turn)
    assert recovered["completed_request_ids"] == [turn["request_id"]]
    assert sent == [{"content": turn["content"], "request_id": turn["request_id"]}]


def test_recovered_view_selects_each_rounds_own_reply_even_when_later_turns_exist():
    view = {"messages": [{"role": "user", "content": "first"}, {"role": "assistant", "content": "first reply"},
                         {"role": "user", "content": "second"}, {"role": "assistant", "content": "second reply"}]}
    assert generator._round_reply(view, {"content": "first"}, 0)["content"] == "first reply"
    assert generator._round_reply(view, {"content": "second"}, 1)["content"] == "second reply"
    with pytest.raises(generator.SampleError):
        generator._round_reply(view, {"content": "changed"}, 0)


def test_start_retry_preserves_request_id_and_does_not_mutate_manifest_payload(monkeypatch):
    monkeypatch.setattr(generator.time, "sleep", lambda _: None)
    platform = generator.Platform("http://127.0.0.1:8004", "test-password", "private-invite")
    platform.close()
    payload = generator.make_plans(24, 3)[0]["start_payload"]
    sent = []
    def retry(method, path, body):
        sent.append(body)
        if len(sent) == 1:
            raise generator.APIError(method, path, None)
        return {"id": "one-session"}
    platform.request = retry
    assert platform.start(payload)["id"] == "one-session"
    assert len(sent) == 2 and sent[0] == sent[1]
    assert sent[0]["request_id"] == payload["request_id"]
    assert sent[0]["access_code"] == "private-invite" and "access_code" not in payload


def test_pacing_is_actual_bounded_sleep_and_completed_requests_are_not_waited_again(monkeypatch):
    plan = {"sample_id": "P001", "session_id": "session", "planned_turns": 2, "pace_total_seconds": 150}
    turn = {"request_id": "id"}
    slept, persisted = [], []
    monkeypatch.setattr(generator.time, "sleep", lambda seconds: slept.append(seconds))
    class Platform:
        def session(self, _):
            return {"completed_request_ids": []}
        def wait_for_pending(self, view):
            return view
    platform = Platform()
    generator.pace_round(platform, plan, turn, lambda: persisted.append(dict(turn)))
    assert slept == [30, 30, 15] and turn["pace_waited_seconds"] == 75
    assert len(persisted) == 3
    generator.pace_round(platform, plan, turn, lambda: pytest.fail("must not wait twice"))
    assert slept == [30, 30, 15]
    platform.session = lambda _: {"completed_request_ids": ["id"]}
    generator.pace_round(platform, plan, {"request_id": "id"}, lambda: pytest.fail("completed request must not wait"))
    assert slept == [30, 30, 15]


def test_concurrent_workers_use_private_clients_and_preserve_every_manifest_record(tmp_path, monkeypatch):
    import threading
    path = tmp_path / "batch.json"
    manifest = generator.open_manifest(path, "https://study.example.org", 24, 11, 180)
    created, closed = [], []
    lock = threading.Lock()
    class Client:
        def __init__(self):
            with lock:
                created.append(self)
        def close(self):
            with lock:
                closed.append(self)
    def run(platform, shared, plan, persist):
        plan["session_id"] = f"session-{plan['index']}"
        plan["status"] = "completed"
        plan["actual_turns"] = 2
        persist()
    monkeypatch.setattr(generator, "run_sample", run)
    generator.execute_batch(manifest, path, Client, 4)
    saved = json.loads(path.read_text())
    assert len(created) == len(closed) == 24
    assert len({id(client) for client in created}) == 24
    assert all(plan["status"] == "completed" for plan in saved["samples"])
    assert len({plan["session_id"] for plan in saved["samples"]}) == 24
    assert all(126 <= plan["pace_total_seconds"] <= 324 for plan in saved["samples"])


def test_full_http_protocol_submits_the_exact_final_reply_and_resume_makes_no_new_mutations():
    """An offline protocol fixture, deliberately not evidence of a real model call."""
    from copy import deepcopy
    plan = generator.make_plans(24, 123)[0]
    plan["planned_turns"] = 2
    manifest = {"seed": 123}
    class ProtocolFixture(generator.Platform):
        def __init__(self):
            self.access_code = "test-only-invitation"
            self.pending_timeout = 1
            self.poll_seconds = .1
            self.row = None
            self.writes = []
        def request(self, method, path, payload=None, *, admin=False):
            if method != "GET":
                self.writes.append((method, path, deepcopy(payload)))
            if path == "/api/translation/start":
                self.row = {**deepcopy(plan["start_payload"]), "id": "fixture-session",
                            "data_purpose": "demonstration", "record_kind": "participant", "study_mode": "pilot",
                            "condition": "A", "band": plan["target_band"], "ability_score": plan["ability"]["score"],
                            "created_at": "2026-10-30T00:00:00+00:00", "ended_at": None,
                            "draft": "", "submission": None, "messages": [], "events": [],
                            "completed_request_ids": [], "pending_reply": False, "remaining_seconds": 1200,
                            "task": {"context_cards": [{"id": "characters", "title": "人物", "body": "罗温是一个人。"}]}}
            elif path.endswith("/message"):
                turn = len(self.row["messages"]) // 2 + 1
                content = "罗温是一个人还是多人？" if turn == 1 else generator.FINAL_BEGIN + TRANSLATION + generator.FINAL_END
                self.row["messages"].extend([{"role": "user", "content": payload["content"]},
                    {"role": "assistant", "content": content, "action": "clarification" if turn == 1 else "answer"}])
                self.row["completed_request_ids"].append(payload["request_id"])
                self.row["events"].append({"type": "reply", "request_id": payload["request_id"], "seconds": 1.23})
            elif path.endswith("/draft"):
                self.row["draft"] = payload["draft"]
            elif path.endswith("/submit"):
                self.row.update(submission=payload["submission"], ended_at="2026-10-30T00:02:00+00:00")
            elif method != "GET":
                pytest.fail(f"Unexpected protocol mutation: {method} {path}")
            return deepcopy(self.row)
    platform = ProtocolFixture()
    saved = []
    generator.run_sample(platform, manifest, plan, lambda: saved.append(deepcopy(plan)))
    assert plan["status"] == "completed" and plan["actual_turns"] == 2
    assert plan["submission"] == platform.row["submission"] == TRANSLATION
    assert [method for method, _, _ in platform.writes] == ["POST", "POST", "POST", "PUT", "POST"]
    assert platform.writes[0][1] == "/api/translation/start"
    assert all(not path.endswith("/evaluate") for _, path, _ in platform.writes)
    assert len(plan["rounds"]) == 2 and plan["rounds"][1]["reply"]["action"] == "answer"
    previous_writes = deepcopy(platform.writes)
    generator.run_sample(platform, manifest, plan, lambda: saved.append(deepcopy(plan)))
    assert platform.writes == previous_writes
    assert saved[-1]["status"] == "completed"
