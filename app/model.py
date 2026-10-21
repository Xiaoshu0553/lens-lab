from __future__ import annotations

import httpx
from urllib.parse import urlparse


ANSWER_PROMPT_VERSION = "evidence-router-v5"
DECISION_PROMPT_VERSION = "clarify-v3"
PROMPT_ONLY_VERSION = "clarify-router-v3"
CLARIFICATION_MARKER = "【澄清提问】"


def answer_system_prompt(task: dict) -> str:
    return (
        "你是实验平台中的任务助手。只根据下列统一资料回答，不要编造规则。"
        "如资料无法支持某结论，明确说出不确定之处。"
        "不要添加参考资料未写明的提交渠道、材料格式、办理要求或资格条件；"
        "请区分资格条件、申请材料和截止日期。"
        "资料有冲突时，按文件的发布主体、适用批次、生效时间和更正关系判断，引用文件编号说明依据；"
        "不要把自媒体、论坛说法当作正式规则。"
        "请使用清楚、简洁的中文，不评价用户的表达能力。"
        "允许正常的多轮交流；不要泄露内部评分标准。\n\n"
        f"任务：{task['title']}\n"
        f"已公开给你和参与者的背景：{task['shared_context']}\n"
        f"统一参考资料：{task['reference_text']}"
    )


def prompt_only_system_prompt(task: dict) -> str:
    return answer_system_prompt(task) + (
        "\n额外的澄清策略：先判断用户这一轮是否真的在请求某项任务结论。"
        "若只是问候或尚未提出任务问题，请简短回应并邀请用户提问，不主动追问资格条件。"
        "请求结论时，先核对共同背景和此前对话；已经明确给出的事实绝不重复追问。"
        "仅在缺少一个会改变当前结论的关键个案事实、无法可靠判断时，先只问一个简短而具体的问题；"
        "不要同时给出未确定的结论。澄清问题的开头必须写【澄清提问】。"
        "信息足够或可以直接给出明确条件性回答时，直接回答，不写该标记。"
        "不能因为用户用口语、短句或缺少专业词汇而追问。"
        "用户回答后，结合全部对话与统一资料继续完成任务。"
    )


class ModelError(RuntimeError):
    pass


class LiveProvider:
    def __init__(self, settings):
        self.settings = settings

    def request_payload(self, messages: list[dict], *, temperature: float | None = None) -> dict:
        payload = {
            "model": self.settings.model_name,
            "messages": messages,
            "temperature": self.settings.model_temperature if temperature is None else temperature,
            "max_tokens": self.settings.model_max_tokens,
        }
        if urlparse(self.settings.model_base_url).hostname == "api.deepseek.com":
            payload["thinking"] = {"type": self.settings.model_thinking}
        return payload

    async def complete(self, messages: list[dict], *, temperature: float | None = None) -> str:
        payload = self.request_payload(messages, temperature=temperature)
        url = f"{self.settings.model_base_url}/chat/completions"
        try:
            headers = {"Authorization": f"Bearer {self.settings.model_api_key}"} if self.settings.model_api_key else {}
            async with httpx.AsyncClient(timeout=90.0) as client:
                response = await client.post(
                    url,
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
            content = body["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ModelError("模型返回了空内容")
            return content.strip()
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelError(f"模型接口调用失败：{type(exc).__name__}") from exc


class DemoProvider:
    async def complete(self, messages: list[dict], *, temperature: float | None = None) -> str:
        last = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        if any(word in last for word in ("材料", "准备什么")):
            return "【演示回答】按模拟规则，需要在读证明、成绩单和申请表。截止日期是 2026 年 11 月 20 日。"
        if any(word in last for word in ("截止", "日期", "什么时候")):
            return "【演示回答】模拟任务的申请截止日期是 2026 年 11 月 20 日。"
        return (
            "【演示回答】已知申请人是全日制本科二年级、绩点 3.4。"
            "如果尚未获得过同类支持金，则符合模拟申请条件；若获得过则不符合。"
            "材料为在读证明、成绩单、申请表，截止日期为 2026 年 11 月 20 日。"
        )
