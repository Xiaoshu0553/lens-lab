from __future__ import annotations

import asyncio
import json

import httpx
from urllib.parse import urlparse


ANSWER_PROMPT_VERSION = "answer-dialogue-v7"
DECISION_PROMPT_VERSION = "clarify-v3"
PROMPT_ONLY_VERSION = "clarify-dialogue-v5"
DEMO_ANSWER_PROMPT_VERSION = "answer-dialogue-v6"
DEMO_PROMPT_ONLY_VERSION = "clarify-dialogue-v4"
CLARIFICATION_MARKER = "【澄清提问】"
DIALOGUE_OUTPUT_CONTRACT = "任务对话JSON输出协议v1"


def answer_system_prompt(task: dict) -> str:
    fact_names = "；".join(slot["description"] for slot in task.get("slots", [])) or "以资料中涉及的个案条件为准"
    return (
        "你是实验平台中的任务助手，使用简洁中文回答用户本轮问题，结合完整对话理解指代和更正。\n"
        "先判断本轮意图，按以下优先级处理：\n"
        "1. 用户只是问候：简短回应，邀请提问，不索取个案事实。\n"
        "2. 用户已说不知道、查不到或无法补充某项事实：不再索取该事实，"
        "直接说明判断条件和不确定之处；不能一边说不追问，一边继续问。\n"
        "3. 用户询问一般规则、报名流程（如‘怎么报名’）、材料、期限、渠道、差异或来源是否可靠："
        "直接据资料回答，不要求个案事实。前文有未完成的个案推荐，也不改变本轮意图。\n"
        "4. 用户请求个案推荐或继续补充个案事实：据已经明确的信息给出结论与依据，"
        "尚缺的事实不能猜测，也不能把你之前的推测当作用户事实。\n"
        "每轮尽量控制在300汉字内，多个问题逐项答全，避免每轮复述全套办理；不为缩短而遗漏所问结论。"
        "不用另选话题或粘贴资料代替解答。"
        "任务规则只来自统一资料，不添加资料未写明的条件、材料、格式、渠道或办理要求；未提供的细节直接说明未提供。"
        "‘本人确认选择’不等于必须本人亲自递交，资料未要求的不得添加。"
        "资料冲突时按发布主体、适用批次、生效时间和更正关系判断，引用文件编号说明依据。"
        "保留规则的关键对象和否定词：‘获得过名额’不等于‘参加过活动’。"
        "个案事实只来自共同背景和用户消息，更正以最新说明为准。"
        "回答是非问题时，首句的‘是’或‘否’必须与后续结论和资料依据一致。"
        "区分截止日期与包含钟点的完整截止时点；资料说只更正截止时间时，直接说明‘截止时点更正，其他规则继续有效’。"
        "不评价用户表达能力，不泄露评分标准，不把实验虚构规则说成现实政策。\n\n"
        f"任务：{task['title']}\n"
        f"任务目标：{task.get('instructions', '')}\n"
        f"已公开给你和参与者的背景：{task['shared_context']}\n"
        f"用于个案判断的事实名称（不是事实取值）：{fact_names}。"
        "规则规定了条件，不等于该个案已满足条件。\n"
        f"统一参考资料：{task['reference_text']}\n\n"
        f"{DIALOGUE_OUTPUT_CONTRACT}：只输出一个JSON对象，先写content，再根据正文填写kind。"
        "content是非空的自然中文回复；kind只能是answer或clarification。"
        "检查正文：凡是要求用户提供、补充或确认个案事实（包括先解释后说‘请确认’），kind必须为clarification；"
        "没有这些请求就为answer，包含规则说明、条件性建议和已经确定的方案。"
        "仅确认‘第一个’指哪段内容或用户想了解哪个话题时，kind为answer，不属于索取个案事实。"
        '字段顺序及格式示例：{"content":"自然中文回复","kind":"answer"}。'
        "不输出代码围栏或JSON以外的文字，content不加分类标记。"
    )


def prompt_only_system_prompt(task: dict) -> str:
    return answer_system_prompt(task) + (
        "\n额外的澄清策略（主动澄清组）：\n"
        "共同规则的前3项优先：问候、用户无法补充、纯规则或一般流程问题均直接回答，不澄清个案事实。"
        "只有本轮明确请求针对个案的判断或推荐，或正在回答该推荐的上一轮澄清，"
        "且尚缺会改变结论、用户也没有表示无法提供的事实时，才先提问。"
        "‘先不要假定’只禁止猜测，不表示事实无法补充；此时仍应询问一个缺失事实。"
        "澄清时content只写关于一个尚缺事实的直接问句，不提其他缺失事实、不列条件方案。"
        "已经明确给出的事实绝不重复追问；用户补充后继续完成原推荐，信息足够就给出方案。"
        "用户无法补充时改给有依据的条件性建议。不能因为口语、短句或缺少专业词汇而追问。"
    )


class ModelError(RuntimeError):
    def __init__(self, message: str, code: str = "model_error", *, http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class LiveProvider:
    def __init__(self, settings):
        self.settings = settings

    def request_payload(self, messages: list[dict], *, temperature: float | None = None) -> dict:
        dialogue = any(message.get("role") == "system" and DIALOGUE_OUTPUT_CONTRACT in message.get("content", "")
                       for message in messages)
        if dialogue:
            # Replay assistant turns in the same format as the requested output.
            # The UI stores plain text; unformatted history otherwise teaches
            # the model to drop the response classification on later turns.
            formatted = []
            for message in messages:
                if message.get("role") == "assistant":
                    content = message["content"]
                    clarification = content.startswith(CLARIFICATION_MARKER)
                    if clarification:
                        content = content[len(CLARIFICATION_MARKER):].strip()
                    message = {**message, "content": json.dumps({
                        "content": content, "kind": "clarification" if clarification else "answer",
                    }, ensure_ascii=False)}
                formatted.append(message)
            messages = formatted
        payload = {
            "model": self.settings.model_name,
            "messages": messages,
            "temperature": self.settings.model_temperature if temperature is None else temperature,
            "max_tokens": self.settings.model_max_tokens,
        }
        if dialogue:
            payload["response_format"] = {"type": "json_object"}
        if urlparse(self.settings.model_base_url).hostname == "api.deepseek.com":
            payload["thinking"] = {"type": self.settings.model_thinking}
        return payload

    async def complete(self, messages: list[dict], *, temperature: float | None = None) -> str:
        payload = self.request_payload(messages, temperature=temperature)
        url = f"{self.settings.model_base_url}/chat/completions"
        try:
            headers = {"Authorization": f"Bearer {self.settings.model_api_key}"} if self.settings.model_api_key else {}
            # Retry one transient transport/service failure within the original
            # total budget. Never retry invalid credentials or partial content.
            async with asyncio.timeout(90.0):
                async with httpx.AsyncClient(timeout=90.0) as client:
                    for attempt in range(2):
                        try:
                            response = await client.post(url, headers=headers, json=payload)
                            response.raise_for_status()
                            break
                        except httpx.HTTPError as exc:
                            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                            transient = isinstance(exc, httpx.TransportError) or status in {429, 500, 502, 503, 504}
                            if attempt or not transient:
                                raise
                            retry_after = exc.response.headers.get("Retry-After", "1") if status is not None else "1"
                            try:
                                delay = max(1.0, float(retry_after))
                            except ValueError:
                                raise exc
                            if delay > 10:
                                raise
                            await asyncio.sleep(delay)
                    body = response.json()
            if not isinstance(body, dict):
                raise ModelError("模型返回了无效的回答结构")
            choices = body.get("choices")
            if not isinstance(choices, list) or not choices:
                raise ModelError("模型返回了无效的回答结构")
            choice = choices[0]
            if not isinstance(choice, dict):
                raise ModelError("模型返回了无效的回答结构")
            finish_reason = choice.get("finish_reason")
            if finish_reason == "length":
                raise ModelError("模型回答达到输出长度上限，未完成本轮回答", "output_truncated")
            # Some compatible services omit finish_reason or return null. Keep
            # accepting their text, but never expose a known interrupted/tool
            # response as a completed answer to the participant.
            if finish_reason not in (None, "stop"):
                raise ModelError("模型未正常完成本轮回答", "output_interrupted")
            message = choice.get("message")
            if not isinstance(message, dict):
                raise ModelError("模型返回了无效的回答结构")
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ModelError("模型返回了空内容", "empty_content")
            content = content.strip()
            if "response_format" in payload:
                try:
                    reply = json.loads(content)
                except (ValueError, TypeError) as exc:
                    raise ModelError("模型返回了无效的对话格式", "invalid_dialogue_json") from exc
                if (not isinstance(reply, dict) or set(reply) != {"kind", "content"}
                        or reply["kind"] not in ("answer", "clarification")
                        or not isinstance(reply["content"], str) or not reply["content"].strip()
                        or CLARIFICATION_MARKER in reply["content"]):
                    raise ModelError("模型返回了无效的对话格式", "invalid_dialogue_fields")
                text = reply["content"].strip()
                return CLARIFICATION_MARKER + text if reply["kind"] == "clarification" else text
            return content
        except (httpx.HTTPError, TimeoutError, KeyError, IndexError, TypeError, ValueError) as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            code = "provider_timeout" if isinstance(exc, (httpx.TimeoutException, TimeoutError)) else "provider_http_error" if isinstance(exc, httpx.HTTPError) else "invalid_response"
            raise ModelError(f"模型接口调用失败：{type(exc).__name__}", code, http_status=status) from exc


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
