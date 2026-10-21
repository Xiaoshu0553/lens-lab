"""Channel retrieval remains correct when the model declines a clear query."""

import asyncio
import json

import pytest

from fixtures.legacy_age_tasks import task_for  # Frozen v3 regression coverage.
from app.evidence import route_question


class DecliningProvider:
    def __init__(self):
        self.calls = []

    async def complete(self, messages, *, temperature=None):
        self.calls.append(messages)
        return json.dumps({"status": "not_covered", "card_id": None, "clarify_slot": None})


class WrongProvider(DecliningProvider):
    def __init__(self, card_id):
        super().__init__()
        self.card_id = card_id

    async def complete(self, messages, *, temperature=None):
        self.calls.append(messages)
        return json.dumps({"status": "matched", "card_id": self.card_id, "clarify_slot": None})


def route(message, *, task=None, provider=None):
    provider = provider or DecliningProvider()
    result = asyncio.run(route_question(provider, task or task_for("18-24"),
        [{"role": "user", "content": message}], adaptive=False, revealed_ids=set()))
    return result, provider


@pytest.mark.parametrize("message", [
    "材料怎么传？", "怎么传", "材料如何上传？", "从哪交？", "交到哪里？", "在什么平台？",
    "在哪上传材料？", "材料上传到哪儿？", "通过哪个平台交？", "用哪个平台上传PDF？",
])
def test_clear_channel_query_has_a_unique_frozen_answer_even_if_model_would_decline(message):
    result, provider = route(message)
    assert result["status"] == "matched" and result["card_id"] == "C_TIME"
    assert result["topic"] == "提交渠道" and result["source"] == "submission_channel_rule"
    assert not provider.calls
    card = next(card for card in task_for("18-24")["evidence_cards"] if card["id"] == result["card_id"])
    assert "官方报名平台" in card["body"] and "邮件提交均不受理" in card["body"]


@pytest.mark.parametrize("message", [
    "材料怎么上传，上传失败怎么办？", "打不开怎么办？", "材料如何上传，点哪个按钮？",
    "材料上传不了", "如何上传，操作步骤是什么？", "具体报名网址？",
])
def test_channel_card_does_not_supply_operating_steps_troubleshooting_or_a_real_url(message):
    result, provider = route(message)
    assert result["status"] == "not_covered" and result["card_id"] is None
    assert result["topic"] is not None
    assert not provider.calls


@pytest.mark.parametrize("message", ["上传失败怎么办？", "平台打不开", "点哪个按钮？", "注册步骤是什么？"])
@pytest.mark.parametrize("card_id", ["C_TIME", "B_MATERIALS"])
def test_wrong_model_card_cannot_override_missing_operating_or_failure_details(message, card_id):
    provider = WrongProvider(card_id)
    result, _ = route(message, provider=provider)
    assert result["card_id"] is None and result["status"] == "not_covered"
    assert result["topic"] == "平台操作或故障处理" and not provider.calls


def test_declined_format_query_retains_its_topic_in_diagnostics():
    result, provider = route("材料用什么格式？")
    assert provider.calls
    assert result["card_id"] is None and result["topic"] == "文件格式"


def test_missing_channel_source_cannot_be_replaced_with_a_pdf_only_card():
    task = task_for("18-24")
    task = {**task, "evidence_cards": [card for card in task["evidence_cards"] if card["id"] == "D_FAQ"]}
    result, provider = route("材料如何上传？", task=task)
    assert result["card_id"] is None and result["status"] == "not_covered"
    assert result["topic"] == "提交渠道" and not provider.calls
