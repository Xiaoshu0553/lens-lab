"""AI clarification/guidance reports are recomputable from saved assistant actions."""
import copy
import csv
import io
import json

import pytest
from fastapi.testclient import TestClient

from app.clean_exports import DIALOGUE_COLUMNS, RECORD_COLUMNS
from app.db import connect
from app.evidence import broad_reply, overview_reply
from app.main import create_app
from app.record_reporting import ai_prompt_counts, ai_prompt_totals
from test_automatic_cohort import archive_records, completed_fixture, record_aliases
from test_flow import auth, decision_payload, settings, start_payload, start_two


FIELDS = ("ai_prompt_count", "ai_clarification_count", "ai_guidance_count")


def test_only_assistant_clarification_and_task_guidance_actions_count():
    messages = [{"role": role, "action": action, "content": "你可以再说一下吗？"} for role, action in [
        ("assistant", "module_clarification"), ("assistant", "prompt_clarification"),
        ("assistant", "task_guidance"), ("assistant", "task_guidance"),
        ("assistant", "evidence"), ("assistant", "answer"), ("assistant", "greeting"),
        ("assistant", "guidance"), ("user", "module_clarification"),
        ("user", "task_guidance"), ("system", "prompt_clarification")]]
    before = copy.deepcopy(messages)
    assert ai_prompt_counts(messages) == dict(zip(FIELDS, [4, 2, 2]))
    assert messages == before
    assert ai_prompt_counts([]) == dict.fromkeys(FIELDS, 0)


def test_totals_include_zero_prompt_records_without_assigning_prompts_to_them():
    counts = [dict(zip(FIELDS, [4, 2, 2])), dict.fromkeys(FIELDS, 0), dict(zip(FIELDS, [1, 0, 1]))]
    assert ai_prompt_totals(counts) == {**dict(zip(FIELDS, [5, 2, 3])), "records_with_ai_prompt": 2, "record_count": 3}
    assert ai_prompt_totals([]) == {**dict.fromkeys(FIELDS, 0), "records_with_ai_prompt": 0, "record_count": 0}


def append_actions(db, sid, actions):
    db.executemany("INSERT INTO messages(session_id,role,content,action,created_at) VALUES (?,?,?,?,?)", [
        (sid, role, f"消息{index}：请看这条资料。", action, f"2026-10-20T00:01:{index:02d}+00:00")
        for index, (role, action) in enumerate(actions)])


def get(client, path):
    response = client.get(path, headers=auth())
    assert response.status_code == 200, response.text
    return response.json()


def counts(row, columns=None):
    headers = dict(columns) if columns is not None else {}
    return {key: int(row[headers.get(key, key)]) for key in FIELDS}


def test_records_exports_and_summaries_recompute_the_same_actions_for_regular_sessions(tmp_path):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    first = completed_fixture(client, cfg)
    second = completed_fixture(client, cfg)
    older = completed_fixture(client, cfg, age="65-74")
    unfinished = client.post("/api/start", json=start_payload()).json()["session_id"]
    with connect(cfg.database_path) as db:
        rows = {row["group_id"]: row["id"] for row in db.execute(
            "SELECT id,group_id FROM sessions WHERE id IN (?,?)", (first, second))}
        assert set(rows) == {"S", "A"}
        prompted, no_prompt = rows["S"], rows["A"]
        # A standard-arm session can contain recorded prompts; an adaptive-arm
        # session with no such assistant actions must still report zero.
        append_actions(db, prompted, [
            ("user", "module_clarification"), ("assistant", "module_clarification"),
            ("assistant", "prompt_clarification"), ("assistant", "task_guidance"),
            ("assistant", "evidence"), ("assistant", "guidance"), ("user", "task_guidance")])
        append_actions(db, no_prompt, [("user", "user_message"), ("assistant", "evidence")])
        append_actions(db, older, [("assistant", "task_guidance"), ("assistant", "task_guidance")])
        append_actions(db, unfinished, [("assistant", "module_clarification")] * 4)
        before = [tuple(row) for row in db.execute("SELECT * FROM sessions ORDER BY id")]
    expected = {prompted: dict(zip(FIELDS, [3, 2, 1])), no_prompt: dict.fromkeys(FIELDS, 0),
                older: dict(zip(FIELDS, [2, 0, 2]))}
    aliases = record_aliases(client)
    review = {row["record_id"]: row for row in get(client, "/api/admin/review-queue")}
    analysis = {row["record_id"]: row for row in get(client, "/api/admin/export?kind=analysis&format=json")}
    raw = {row["record_id"]: row for row in get(client, "/api/admin/export?kind=raw&format=json")}
    archived_raw = {row["id"]: row for row in archive_records(client)}
    dialogues = {row["record_id"]: row for row in get(client, "/api/admin/dialogue-export?format=json")}
    for sid, actual in expected.items():
        alias = aliases[sid]
        assert counts(review[alias]) == counts(analysis[alias]) == counts(raw[alias]) == counts(dialogues[alias]) == actual
        assert counts(archived_raw[sid]) == actual
        assert ai_prompt_counts(review[alias]["messages"]) == actual
        assert ai_prompt_counts(json.loads(archived_raw[sid]["messages_json"])) == actual
        assert ai_prompt_counts(dialogues[alias]["messages"]) == actual
    assert counts(raw[aliases[unfinished]]) == counts(archived_raw[unfinished]) == dict(zip(FIELDS, [4, 4, 0]))
    assert aliases[unfinished] not in analysis and aliases[unfinished] not in review and aliases[unfinished] not in dialogues
    # Keep the existing exported clarification_count calculation unchanged.
    assert archived_raw[prompted]["clarification_count"] == 3
    assert archived_raw[prompted]["ai_clarification_count"] == 2
    for kind in ["raw", "analysis"]:
        response = client.get(f"/api/admin/export?kind={kind}&format=csv", headers=auth())
        csv_rows = {row[dict(RECORD_COLUMNS)["record_id"]]: row
                    for row in csv.DictReader(io.StringIO(response.text.lstrip("\ufeff")))}
        for sid, actual in expected.items():
            assert counts(csv_rows[aliases[sid]], RECORD_COLUMNS) == actual
    dialogue_csv = client.get("/api/admin/dialogue-export?format=csv", headers=auth())
    expected_aliases = {aliases[sid]: actual for sid, actual in expected.items()}
    for row in csv.DictReader(io.StringIO(dialogue_csv.text.lstrip("\ufeff"))):
        assert counts(row, DIALOGUE_COLUMNS) == expected_aliases[row[dict(DIALOGUE_COLUMNS)["record_id"]]]
    summary = get(client, "/api/admin/summary?age_band=18-24")
    assert summary["ai_prompt_totals"] == {**dict(zip(FIELDS, [3, 2, 1])), "records_with_ai_prompt": 1, "record_count": 2}
    assert summary["ai_prompt_totals_all_ages"] == {**dict(zip(FIELDS, [5, 2, 3])), "records_with_ai_prompt": 2, "record_count": 3}
    older_summary = get(client, "/api/admin/summary?age_band=65-74")
    assert older_summary["ai_prompt_totals"] == {**dict(zip(FIELDS, [2, 0, 2])), "records_with_ai_prompt": 1, "record_count": 1}
    with connect(cfg.database_path) as db:
        assert before == [tuple(row) for row in db.execute("SELECT * FROM sessions ORDER BY id")]
        assert all(json.loads(row[0]) == [] for row in db.execute("SELECT flags_json FROM sessions"))


def test_reporting_uses_current_saved_messages_instead_of_cached_counts(tmp_path):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    sid = completed_fixture(client, cfg)
    assert counts(get(client, "/api/admin/review-queue")[0]) == dict.fromkeys(FIELDS, 0)
    with connect(cfg.database_path) as db:
        append_actions(db, sid, [("assistant", "task_guidance")])
    assert counts(get(client, "/api/admin/review-queue")[0]) == dict(zip(FIELDS, [1, 0, 1]))
    assert get(client, "/api/admin/summary")["ai_prompt_totals"]["ai_prompt_count"] == 1
    with connect(cfg.database_path) as db:
        db.execute("UPDATE messages SET action='evidence' WHERE session_id=?", (sid,))
    assert counts(get(client, "/api/admin/dialogue-export?format=json")[0]) == dict.fromkeys(FIELDS, 0)


def test_empty_dialogue_export_row_contains_explicit_zero_counts(tmp_path):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    sid = completed_fixture(client, cfg)
    row = get(client, "/api/admin/dialogue-export?format=json")[0]
    alias = record_aliases(client)[sid]
    assert row["record_id"] == alias and row["messages"] == [] and counts(row) == dict.fromkeys(FIELDS, 0)
    response = client.get("/api/admin/dialogue-export?format=csv", headers=auth())
    csv_rows = list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))
    assert len(csv_rows) == 1 and counts(csv_rows[0], DIALOGUE_COLUMNS) == dict.fromkeys(FIELDS, 0)
    assert csv_rows[0][dict(DIALOGUE_COLUMNS)["record_id"]] == alias


@pytest.mark.parametrize("branch,arm", [("overview", "S"), ("overview", "A"), ("broad", "S")])
def test_real_task_guidance_requests_are_saved_and_counted_after_submit(tmp_path, branch, arm):
    cfg = settings(tmp_path)
    client = TestClient(create_app(cfg))
    started = next(row for row in start_two(client) if row["group_id"] == arm)
    sid = started["session_id"]

    def message(content):
        response = client.post(f"/api/session/{sid}/message", json={"content": content})
        assert response.status_code == 200, response.text
        return response.json()

    assert message("你好")["action"] == "guidance"
    if branch == "overview":
        guided = message("办理要求如何？")
        assert guided["content"] == overview_reply()
    else:
        guided = message("直接告诉我能否报名")
        assert guided["content"] == broad_reply()
    assert guided["action"] == "task_guidance"
    assert message("本次正式报名材料是什么？")["action"] == "evidence"
    assert message("陈禾每周最多能到场几次？")["action"] == "guidance"
    assert message("绩点门槛？")["action"] == "guidance"
    choices = client.post(f"/api/session/{sid}/decision").json()
    assert client.post(f"/api/session/{sid}/submit", json=decision_payload(client, sid, choices)).status_code == 200
    expected = dict(zip(FIELDS, [1, 0, 1]))
    alias = record_aliases(client)[sid]
    record = next(row for row in get(client, "/api/admin/review-queue") if row["record_id"] == alias)
    assert counts(record) == expected
    assert sum(message["role"] == "assistant" and message["action"] == "task_guidance"
               for message in record["messages"]) == 1
    for kind in ["raw", "analysis"]:
        exported = next(row for row in get(client, f"/api/admin/export?kind={kind}&format=json")
                        if row["record_id"] == alias)
        assert counts(exported) == expected
        archived = next(row for row in archive_records(client, kind) if row["id"] == sid)
        assert counts(archived) == expected and archived["clarification_count"] == 0
    dialogue = next(row for row in get(client, "/api/admin/dialogue-export?format=json") if row["record_id"] == alias)
    assert counts(dialogue) == expected
    assert get(client, "/api/admin/summary")["ai_prompt_totals"]["ai_guidance_count"] == 1
