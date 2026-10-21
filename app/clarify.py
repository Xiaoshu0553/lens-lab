from __future__ import annotations

import json
import re

from .model import DECISION_PROMPT_VERSION, ModelError
from .evidence import asks_for_decision, clarifiable_slots


def _json_object(raw: str) -> dict:
    if not isinstance(raw, str):
        raise ValueError("分类器没有返回文本")
    value = raw.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.I)
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("分类器没有返回 JSON 对象")
    return parsed


def demo_decision(task: dict, history: list[dict]) -> dict:
    last = next((x["content"] for x in reversed(history) if x["role"] == "user"), "")
    asks_eligibility = asks_for_decision(last)
    missing = clarifiable_slots(task, history)
    slot = missing[0] if missing else None
    return {
        "need_clarification": bool(asks_eligibility and slot),
        "slot_key": slot["key"] if asks_eligibility and slot else None,
        "reason": "演示规则：资格判断缺少关键条件" if asks_eligibility and slot else "可直接提供现有资料",
        "source": "demo_rule",
    }


async def decide_clarification(provider, task: dict, history: list[dict], *, is_demo: bool) -> dict:
    if is_demo:
        return demo_decision(task, history)
    missing = clarifiable_slots(task, history)
    if not missing:
        return {"need_clarification": False, "slot_key": None, "reason": "关键个案条件已经给出或任务未配置条件", "source": "no_missing_slots"}
    allowed = [{"key": s["key"], "description": s["description"]} for s in missing]
    transcript = "\n".join(f"{m['role']}: {m['content']}" for m in history[-12:])
    prompt = (
        "你只判断是否必须先向用户澄清，不能回答任务。"
        "只有缺少某个关键条件、且这个条件会改变用户当前所求的结论时才追问。"
        "口语、短句、没有专业词汇本身绝不是追问理由。"
        "如果可直接回答当前问题，或可给出明确的条件性回答，就不追问。"
        "只能从给定关键条件中选一个。"
        "严格只输出 JSON："
        '{"need_clarification":true或false,"slot_key":"条件key或null","reason":"简短原因"}。\n'
        f"任务：{task['title']}\n共同背景：{task['shared_context']}\n"
        f"参考资料：{task['reference_text']}\n"
        f"任务关键条件：{json.dumps(allowed, ensure_ascii=False)}\n"
        f"对话：\n{transcript}"
    )
    try:
        raw = await provider.complete([{"role": "system", "content": prompt}], temperature=0)
        parsed = _json_object(raw)
        key = parsed.get("slot_key")
        keys = {slot["key"] for slot in missing}
        need = parsed.get("need_clarification") is True and isinstance(key, str) and key in keys
        return {
            "need_clarification": need,
            "slot_key": key if need else None,
            "reason": str(parsed.get("reason", ""))[:500],
            "source": DECISION_PROMPT_VERSION,
            "raw": raw[:2000],
        }
    except (ModelError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return {
            "need_clarification": False,
            "slot_key": None,
            "reason": f"澄清判定失败，退回普通回答：{type(exc).__name__}",
            "source": "decision_error",
        }
