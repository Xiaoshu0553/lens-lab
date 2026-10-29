import json
import hashlib
from copy import deepcopy

import pytest

from app.model import ModelError
from app.translation_materials import PROMPT_VERSION, TASK
from app.translation_model import (ACTIVE_CLARIFICATION_POLICY, BASELINE_CONDITIONS,
    audit_prompt, build_dialogue_messages, demo_reply, dialogue_message_protocol,
    dialogue_prompt, parse_plan, parse_reply)


def plan(**changes):
    return {"ready": False, "gaps": [{"issue": "指代人数", "question": "罗温指几个人？",
                                       "options": ["一个人", "多个人", "不确定"]}], **changes}


@pytest.mark.parametrize("kind", [[], {}, None, 1, True, "Answer", "answer "])
def test_reply_kind_rejects_wrong_type_and_unknown_values_as_model_error(kind):
    with pytest.raises(ModelError) as error:
        parse_reply(json.dumps({"content": "译文", "kind": kind}))
    assert error.value.code == "invalid_translation_reply"


@pytest.mark.parametrize("content", [[], {}, None, 1, True, "", " \n\t", "译" * 16001])
def test_reply_content_rejects_wrong_type_empty_and_oversize(content):
    with pytest.raises(ModelError):
        parse_reply(json.dumps({"content": content, "kind": "answer"}))


@pytest.mark.parametrize("raw", [None, 123, {}, [], b"{}", "[]", '"text"', "true", "null", "not json",
                                  '{"content":"first","content":"second","kind":"answer"}',
                                  '{"content":"译文","kind":"answer","score":100}'])
def test_reply_rejects_malformed_json_extra_and_duplicate_fields(raw):
    with pytest.raises(ModelError):
        parse_reply(raw)


def test_reply_parses_plain_json_and_normalizes_visible_content_only():
    assert parse_reply(json.dumps({"content": "  这是译文。\n  ", "kind": "answer"})) == {
        "content": "这是译文。", "action": "answer"}
    assert parse_reply(json.dumps({"content": "可以确认指代吗？", "kind": "clarification"}))["action"] == "clarification"


def test_excessively_nested_model_payload_is_a_retryable_model_error():
    nested = "[" * 2000 + "0" + "]" * 2000
    for parser in (parse_reply, parse_plan):
        with pytest.raises(ModelError):
            parser(nested)


@pytest.mark.parametrize("value", [True, 0, 1, "false", None, [], {}])
def test_plan_ready_must_be_strict_boolean_and_consistent_with_gaps(value):
    with pytest.raises(ModelError) as error:
        parse_plan(json.dumps(plan(ready=value)))
    assert error.value.code == "invalid_optimizer"


def test_plan_accepts_ready_or_up_to_two_normalized_uncertain_gaps():
    value = plan()
    value["gaps"][0]["options"] = [" 一个人 ", " 多个人 ", " 不确定 "]
    value["gaps"][0]["question"] = " 罗温指几个人？ "
    parsed = parse_plan(json.dumps(value))
    assert parsed["gaps"][0]["options"] == ["一个人", "多个人", "不确定"]
    assert parsed["gaps"][0]["question"] == "罗温指几个人？"
    assert parse_plan('{"ready":true,"gaps":[]}') == {"ready": True, "gaps": []}
    two = plan()
    two["gaps"].append({"issue": "物件", "question": "银狐是指什么？", "options": ["动物", "物件", "不确定"]})
    assert len(parse_plan(json.dumps(two))["gaps"]) == 2


@pytest.mark.parametrize("change", [
    {"gaps": []}, {"gaps": {}}, {"gaps": [True]}, {"gaps": ["gap"]}, {"gaps": None},
    {"score": 100}, {"condition": "O"},
])
def test_plan_rejects_inconsistent_empty_wrong_type_or_extra_fields(change):
    with pytest.raises(ModelError):
        parse_plan(json.dumps(plan(**change)))


@pytest.mark.parametrize("field,value", [
    ("issue", " "), ("question", []), ("question", "问题" * 251),
    ("options", []), ("options", ["one"]), ("options", ["one", "two"]),
    ("options", ["同一个", " 同一个 ", "不确定"]), ("options", [[], "不确定"]),
    ("options", [" ", "不确定"]), ("options", ["解释" * 126, "不确定"]),
])
def test_plan_rejects_bad_gap_fields(field, value):
    value_plan = plan()
    value_plan["gaps"][0][field] = value
    with pytest.raises(ModelError):
        parse_plan(json.dumps(value_plan))


def test_plan_rejects_too_many_gaps_extra_gap_fields_and_duplicate_json_keys():
    value = plan()
    value["gaps"] *= 3
    with pytest.raises(ModelError):
        parse_plan(json.dumps(value))
    value = plan()
    value["gaps"][0]["confirmed_secret"] = "not permitted"
    with pytest.raises(ModelError):
        parse_plan(json.dumps(value))
    with pytest.raises(ModelError):
        parse_plan('{"ready":false,"ready":true,"gaps":[]}')


def test_model_prompts_never_include_hidden_context_or_ability_condition_data():
    task = deepcopy(TASK)
    task.update({"context_cards": [{"body": "SECRET_CONTEXT_91"}],
                 "reference_translation": "SECRET_REFERENCE_92", "critical_points": ["SECRET_RUBRIC_93"],
                 "ability_score": "SECRET_ABILITY_94", "band": "SECRET_BAND_95",
                 "condition": "SECRET_CONDITION_96", "answers": {"key": "SECRET_ANSWERS_97"}})
    prompts = [audit_prompt(task)] + [dialogue_prompt(task, condition) for condition in ("S", "A", "O")]
    for prompt in prompts:
        assert task["source_text"] in prompt and task["brief"] in prompt
        assert all(f"SECRET_{name}" not in prompt for name in
                   ("CONTEXT", "REFERENCE", "RUBRIC", "ABILITY", "BAND", "CONDITION", "ANSWERS"))
    assert "不把助手提问中的候选当成已确认事实" in prompts[0]


def test_conditions_add_clarification_then_accessible_scaffolding_without_ability_routing():
    standard = dialogue_prompt(TASK, "S")
    active = dialogue_prompt(TASK, "A")
    optimized = dialogue_prompt(TASK, "O", plan())
    assert "不主动索取补充背景" in standard
    assert "先问一个最重要的具体问题" in active
    assert "候选解释" in optimized and "罗温指几个人" in optimized
    assert "不读取或推断问卷能力层" in optimized
    assert "罗温指几个人" not in dialogue_prompt(TASK, "A", plan())
    assert "不能把未知信息或最可能的解释当成事实" in optimized
    with pytest.raises(ValueError):
        dialogue_prompt(TASK, "unknown")


def test_active_priority_policy_follows_json_contract_and_does_not_enable_optimizer():
    active = dialogue_prompt(TASK, "A")
    assert active.endswith("\n" + ACTIVE_CLARIFICATION_POLICY)
    assert active.index('仅输出JSON对象：{"content"') < active.index(ACTIVE_CLARIFICATION_POLICY)
    assert ACTIVE_CLARIFICATION_POLICY not in dialogue_prompt(TASK, "S")
    assert ACTIVE_CLARIFICATION_POLICY not in dialogue_prompt(TASK, "O")
    assert tuple(BASELINE_CONDITIONS) == ("S", "A")
    assert "优化模块" not in active and "两三个候选解释" not in active
    assert PROMPT_VERSION == "translation-dialogue-v3"


def test_active_policy_prioritizes_one_missing_context_question_but_stops_when_user_cannot_supply_it():
    active = dialogue_prompt(TASK, "A")
    assert "不要用常见用法的推断替代尚未确认的作品背景" in active
    assert "本轮必须只提出一个由你自行选择的最重要的具体问题" in active
    assert "kind必须为clarification；不要同时给整段译文" in active
    assert "用户明确无法补充时，必须据已知信息给完整译文，kind为answer" in active
    assert "已经确认的信息不重复问" in active


def test_direct_translation_control_prompt_is_byte_identical_after_active_policy_revision():
    # Frozen before the v2 A-only policy change: the S control must remain intact.
    assert hashlib.sha256(dialogue_prompt(TASK, "S").encode()).hexdigest() == (
        "1fb43d1cba8a47041c3f46cc2b6303d27d655eb1ce9d92435c8cfdaf1dc2aa0d")


@pytest.mark.parametrize("condition", ["S", "O"])
def test_dialogue_builder_preserves_the_original_control_and_historical_message_sequences(condition):
    history = [{"role": "user", "content": "请翻译。"},
               {"role": "assistant", "content": "已有回复。"},
               {"role": "user", "content": "请核对人物指代。"}]
    original = deepcopy(history)
    expected = [{"role": "system", "content": dialogue_prompt(TASK, condition)}] + original
    result = build_dialogue_messages(TASK, condition, history)
    assert json.dumps(result, ensure_ascii=False) == json.dumps(expected, ensure_ascii=False)
    assert history == original


@pytest.mark.parametrize("user_content", ["请翻译这段小说。", "我明确无法再提供背景，请据已知信息完成译文。"])
def test_active_wire_reminder_follows_the_unchanged_history_and_matches_the_frozen_protocol(user_content):
    history = [{"role": "user", "content": "第一次请求。"},
               {"role": "assistant", "content": "需要确认一个细节。"},
               {"role": "user", "content": user_content}]
    original = deepcopy(history)
    result = build_dialogue_messages(TASK, "A", history)
    protocol = dialogue_message_protocol()
    assert result[0] == {"role": "system", "content": dialogue_prompt(TASK, "A")}
    assert result[1:-1] == original and history == original
    assert result[-1:] == protocol["conditions"]["A"]["trailing_messages"]
    assert result[-1] == {"role": "system", "content": ACTIVE_CLARIFICATION_POLICY}
    assert protocol["conditions"]["A"]["order"] == ["initial_system", "history", "trailing_system"]
    assert protocol["conditions"]["S"]["trailing_messages"] == []
    assert "用户明确无法补充时，必须据已知信息给完整译文，kind为answer" in result[-1]["content"]
    assert "优化模块" not in json.dumps(result, ensure_ascii=False)


def test_dialogue_builder_drops_non_dialogue_roles_and_private_history_metadata_without_mutating_input():
    history = [{"role": "system", "content": "UNTRUSTED_HISTORY_SYSTEM"},
               {"role": "user", "content": "用户提供的背景。", "band": "SECRET_BAND", "ability_score": 18},
               {"role": "assistant", "content": "已有回复。", "action": "answer", "created_at": "PRIVATE_TIME"},
               {"role": "tool", "content": "UNTRUSTED_TOOL_DATA"}]
    original = deepcopy(history)
    result = build_dialogue_messages(TASK, "A", history)
    assert result[1:-1] == [{"role": "user", "content": "用户提供的背景。"},
                           {"role": "assistant", "content": "已有回复。"}]
    assert history == original
    wire = json.dumps(result, ensure_ascii=False)
    assert not any(value in wire for value in ("UNTRUSTED_HISTORY_SYSTEM", "UNTRUSTED_TOOL_DATA", "SECRET_BAND", "PRIVATE_TIME"))
    assert all(card["body"] not in wire for card in TASK["context_cards"])


def test_demo_never_reads_hidden_reference_and_same_candidate_is_used_across_arms():
    task = deepcopy(TASK)
    task["reference_translation"] = "MUST_NOT_BE_LEAKED"
    task["context_cards"] = [{"body": "HIDDEN_CANON"}]
    initial = [{"role": "user", "content": "请翻译。"}]
    later = initial + [{"role": "assistant", "content": "问题"}, {"role": "user", "content": "不确定。"}]
    assert demo_reply(task, "S", initial)["action"] == "answer"
    active = demo_reply(task, "A", initial)
    optimized = demo_reply(task, "O", initial)
    assert active["action"] == optimized["action"] == "clarification"
    assert "罗温（Rowan）" in active["content"] and "①" in optimized["content"]
    candidates = [demo_reply(task, condition, later)["content"] for condition in ("S", "A", "O")]
    assert len(set(candidates)) == 1
    assert all("MUST_NOT_BE_LEAKED" not in text and "HIDDEN_CANON" not in text for text in candidates)
    assert "不代表真实模型的翻译质量" in candidates[0]


def test_demo_does_not_reuse_character_names_for_unknown_material():
    custom_task = {"id": "different-source"}
    initial = [{"role": "user", "content": "请翻译"}]
    assert "罗温" not in demo_reply(custom_task, "A", initial)["content"]
    assert "玛拉" not in demo_reply(custom_task, "S", initial)["content"]
