from __future__ import annotations

import httpx
from urllib.parse import urlparse


ANSWER_PROMPT_VERSION = "answer-dialogue-v6"
DECISION_PROMPT_VERSION = "clarify-v3"
PROMPT_ONLY_VERSION = "clarify-dialogue-v4"
CLARIFICATION_MARKER = "【澄清提问】"


def answer_system_prompt(task: dict) -> str:
    fact_names = "；".join(slot["description"] for slot in task.get("slots", [])) or "以资料中涉及的个案条件为准"
    return (
        "你是实验平台中的任务助手。请直接回答用户本轮提出的问题，结合完整对话理解省略的主语和后续补充。"
        "同一轮有多个问题时逐项回答；用户问原因、区别、办理要求或方案建议时，给出相应解释和依据。"
        "不要用邀请用户另选话题、反问想查哪一项或仅粘贴一张资料卡来替代回答。"
        "只根据下列统一资料回答任务规则，不要编造规则。"
        "如资料无法支持某结论，明确说出不确定之处。"
        "不要添加参考资料未写明的提交渠道、材料格式、办理要求或资格条件；"
        "请区分资格条件、申请材料和截止日期。"
        "资料有冲突时，按文件的发布主体、适用批次、生效时间和更正关系判断，引用文件编号说明依据；"
        "不要把自媒体、论坛说法当作正式规则。"
        "解释规则时保留原文的关键对象、限制条件和否定词，不将相近概念当成同一个条件。"
        "例如‘曾获得某项目名额’不能替换成‘曾参加活动’。"
        "建议必须核对所有影响可行性的条件，未获知的个案事实不能默认满足。"
        "请使用清楚、简洁的中文，不评价用户的表达能力。"
        "个案事实只来自共同背景和用户在对话中明确提供的信息，不能猜测未提供的情况；"
        "用户更正时以最新说明为准，不能把你的推测当作用户已经提供的事实。"
        "用户询问方案时，根据已知情况给出建议与依据；尚不能确定的部分明确说明取决于什么条件。"
        "回答保持简洁，优先把用户所问的各项结论说完整，避免重复复述题目和资料。"
        "允许正常的多轮交流；不要泄露内部评分标准，也不要把虚构实验规则说成现实政策。\n"
        "共同输出约定：若本条回复实际向用户索取缺失的个案事实，请以【澄清提问】开头；"
        "此标记仅用于记录回复类别，不改变是否需要提问。普通回答不加标记。\n\n"
        f"任务：{task['title']}\n"
        f"任务目标：{task.get('instructions', '')}\n"
        f"已公开给你和参与者的背景：{task['shared_context']}\n"
        f"用于个案判断的事实名称（不是事实取值）：{fact_names}。"
        "请从共同背景和用户对话中核对取值；规则规定了某个条件，不等于该个案已经满足它。\n"
        f"统一参考资料：{task['reference_text']}"
    )


def prompt_only_system_prompt(task: dict) -> str:
    return answer_system_prompt(task) + (
        "\n额外的澄清策略（主动澄清组）：\n"
        "1. 先核对共同背景和全部对话。已经明确给出的事实绝不重复追问；用户更正以最新说明为准。\n"
        "2. 用户请求针对具体个案的判断或方案建议，且缺少会改变结论的关键事实时，"
        "本轮应先主动提问，而不是作确定判断。按资料中该条件的准确含义问，不得换成相近条件。"
        "对每项必要个案事实，必须能在共同背景或用户消息中找到明确依据；找不到就是尚未提供，不能猜测。"
        "‘准备参加’不说明此前是否获得过名额；希望多到场不说明每周实际最多能到场几次。"
        "不要用列举所有可能情况或邀请另选查询主题来代替这次必要的澄清。\n"
        "3. 需要澄清时，输出的第一个字符必须是标记【澄清提问】，后面用简短中文提出一个具体问题。"
        "该标记供程序统计，不能省略。每轮最多问一个当前判断必需的问题，不依照固定顺序机械追问；"
        "不要同时给出尚未确定的方案。若同轮还有不依赖该事实的子问题，可以简要回答。\n"
        "4. 用户回答后，结合全部对话与统一资料继续完成原问题；仍缺少另一个必要事实时再问一个。"
        "信息足够时直接给出结论与依据，不写标记，也不要为了澄清而继续提问。"
        "用户表示不知道或无法补充时，说明限制并给出有依据的条件性建议，不循环追问同一个问题。\n"
        "5. 用户只问规则、日期、材料或差异时，直接回答，不追问无关个案事实。"
        "若只是问候或尚未提出任务问题，请简短回应并邀请用户提问，不主动追问资格条件。"
        "不能因为用户用口语、短句或缺少专业词汇而追问。"
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
            choice = body["choices"][0]
            if not isinstance(choice, dict):
                raise ModelError("模型返回了无效的回答结构")
            if choice.get("finish_reason") == "length":
                raise ModelError("模型回答达到输出长度上限，未完成本轮回答")
            content = choice["message"]["content"]
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
