"""All-age language guardrails; not evidence that participants understand."""
import re

import pytest

from app.age_forms import AGE_BANDS, _CONTEXT_BANKS, form_for, public_form
from app.literacy import ANCHOR_IDS

REVIEWED = {"s04", "s05", "s06", "a11", "a12", "e17", "e18", "u22", "u23", "u24", "v29", "v30"}
RAW_KEYS = {"s04": 1, "s05": 1, "s06": 2, "a11": 0, "a12": 1, "e17": 2, "e18": 0, "u22": 1, "u23": 2, "u24": 0, "v29": 1, "v30": 2}


def context(band, key):
    return next(row for row in _CONTEXT_BANKS[band] if row[0] == key)


@pytest.mark.parametrize("band", AGE_BANDS)
def test_every_age_context_item_uses_short_visible_lines_and_keeps_keys(band):
    for key in REVIEWED:
        row = context(band, key)
        question, options, correct = row[2:5]
        assert '\n' in question
        assert len(question.splitlines()) <= 6
        assert max(map(len, question.splitlines())) <= 36
        assert len(set(options)) == 3 and correct == RAW_KEYS[key]
        assert all(len(option) <= 23 for option in options)
        assert not re.search(r"字段|原始|来源链|全称|未验证|无效|定位目标|适用|时段|现场示意|甲|乙|丙", question + ''.join(options))
    _, form = form_for(band)
    assert len(form) == 30 and sum(item['id'] in ANCHOR_IDS for item in form) == 18
    assert form_for(band)[0].endswith("form-draft3")
    assert all(len(item['options']) == 4 for item in public_form(band)['items'])


@pytest.mark.parametrize("band", AGE_BANDS)
def test_two_object_check_still_excludes_the_disallowed_item_and_corrects_ai(band):
    row = context(band, "v30")
    lines = row[2].splitlines()
    assert '两' in lines[0] and ('不能' in lines[0] or '不放' in lines[0] or '不播' in lines[0] or '不搬' in lines[0] or '不放入' in lines[0])
    # The middle three visible lines describe real named objects and status;
    # readers no longer have to translate A/B/C pairs back to a separate list.
    objects = [line.split('：', 1)[0] for line in lines[1:4]]
    assert len(set(objects)) == 3
    for option in row[3]:
        assert len(option.split('和')) == 2
        assert all(name in objects for name in option.split('和'))
    correct_objects = set(row[3][row[4]].split('和'))
    assert correct_objects == {objects[0], objects[2]}
    assert objects[1] not in correct_objects
    assert objects[0] in lines[-1] and objects[1] in lines[-1]
    assert all(objects[1] in option for index, option in enumerate(row[3]) if index != row[4])


@pytest.mark.parametrize("band,next_step,optional", [
    ("12-14", "贴图案", "边框"), ("15-17", "排版", "覆膜"),
    ("18-24", "查页数", "装订"), ("25-34", "有无损坏", "拍照"),
    ("35-44", "折好推车", "车把"), ("45-54", "耳机齐不齐", "外壳"),
    ("55-64", "数份数", "书签"), ("65-74", "折好袋子", "标签"),
    ("75-plus", "数份数", "书签"),
])
def test_required_sequence_and_optional_operation_are_both_preserved(band, next_step, optional):
    row = context(band, 'u23')
    assert '先' in row[2] and '再' in row[2] and '最后' in row[2]
    assert '可以' in row[2] and '也可以不' in row[2] and optional in row[2]
    correct = row[3][row[4]]
    assert next_step in correct and optional not in correct
    assert any(optional in option for index, option in enumerate(row[3]) if index != row[4])


@pytest.mark.parametrize("band,classification,completed", [
    ("12-14", ("薄", "厚"), "压平"), ("15-17", ("横版", "竖版"), "压平"),
    ("18-24", ("大包", "小包"), "包好"), ("25-34", ("长", "短"), "套好"),
    ("35-44", ("折叠",), "擦干"), ("45-54", ("耳机",), "充好"),
    ("55-64", ("报纸", "杂志"), "展平"), ("65-74", ("雨伞", "袋子"), "擦干"),
    ("75-plus", ("报纸", "杂志"), "展平"),
])
def test_missing_information_item_keeps_two_rules_and_the_completed_condition(band, classification, completed):
    row = context(band, 'u24')
    assert ';' in row[2] or '；' in row[2]
    assert '已' in row[2] and completed in row[2]
    assert '还没说' in row[2]
    correct = row[3][row[4]]
    assert all(word in correct for word in classification)
    assert completed not in correct
    assert any(completed in option for index, option in enumerate(row[3]) if index != row[4])


@pytest.mark.parametrize("band", AGE_BANDS)
def test_search_and_scope_checks_preserve_the_information_operation(band):
    row = context(band, 's05')
    assert '结果里' in row[2] and '继续找' in row[2]
    assert '＋' in row[3][row[4]]
    stop = context(band, 's06')
    assert '今天的' in stop[2] and '只想知道' in stop[2]
    assert '这些信息够了' in stop[3][stop[4]]
    scope = context(band, 'a12')
    assert ('只给' in scope[2] or '别校地址' in scope[2] or '青石市' in scope[2])
    assert '还要另找' in scope[3][scope[4]]
    current = context(band, 'a11')
    assert '今天' in current[2] and '今天' in current[3][current[4]]


def test_unfamiliar_equipment_is_explained_without_requiring_device_use():
    for key in ('s04', 'a11'):
        question = context('45-54', key)[2]
        assert '播放展品介绍' in question
    assert '播放讲解的设备' in context('45-54', 'e17')[2]
