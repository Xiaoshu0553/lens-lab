import json
from copy import deepcopy

import pytest

from app.model import ModelError
from app.translation_materials import TASK
from app.translation_model import audit_prompt, demo_reply, dialogue_prompt, parse_plan, parse_reply


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
