#!/usr/bin/env python3
"""Create presentation samples through the complete live HTTP translation flow.

Questionnaires and participant messages are simulated, cloud replies are real.
The destination must explicitly declare data_purpose=demonstration. This script
never accesses SQLite, invents assistant replies, changes timestamps, chooses an
experimental condition, evaluates translations, or creates quality scores.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import random
import re
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx

from app.config import load_dotenv
from app.translation_materials import FORM_ITEMS, SELF_ITEMS, FORM_VERSION, TASK_VERSION, PROMPT_VERSION, score_form

GENERATOR_VERSION = "translation-http-presentation-v1"
FORMAT_PROTOCOL = "json-content-field-v2"
METHOD = {
    "purpose": "demonstration",
    "questionnaires": "Seeded, probability-sampled simulated answers, conditional on predeclared ability strata.",
    "participant_messages": "Scripted requests with variable detail drawn only from the publicly available context cards.",
    "assistant_replies": "Actual HTTP platform requests to the configured remote model API; no canned assistant replies.",
    "distribution_basis": "Illustrative undergraduate scenarios, not an estimate of the distribution of real CUMT students.",
    "quality": "No target translation scores, no simulated ratings, no fitted fairness effect.",
    "timing": "Actual server UTC timestamps; no backdating or manufactured completion durations.",
}
FINAL_BEGIN = "【最终译文】"
FINAL_END = "【译文结束】"
FINAL_JSON_INSTRUCTION = (
    "请继续遵守系统要求，只输出一个合法JSON对象，kind字段填answer；"
    "content字段的字符串内容只包含下列译文起止标记和完整译文，不包含说明、标题或译注。"
    "这里的起止标记位于JSON的content字符串内部，不是要求在JSON对象外输出文字。"
    "JSON格式示例（将占位文字替换为整段完整中文译文）："
    + json.dumps({"content": FINAL_BEGIN + "此处替换为整段完整中文译文" + FINAL_END,
                  "kind": "answer"}, ensure_ascii=False)
)
_LOCAL_HOSTS = {"localhost", "0.0.0.0", "127.0.0.1", "::1", "host.docker.internal"}
_CARD_CUES = {
    "characters": ("they", "them", "their", "单数", "几个人", "非二元", "性别", "人物指代"),
    "nickname": ("lordship", "爵位", "贵族", "称呼", "昵称", "调侃"),
    "fox": ("silver fox", "银狐", "狐狸", "徽记", "掌心", "物件"),
    "mother": ("wake", "hers", "母亲", "亮灯", "睡觉", "吵醒", "房间"),
    "promise": ("promise", "约定", "承诺", "恋爱", "爱情"),
    "bell": ("钟声", "两声", "渡船", "码头", "bell rang"),
}
_LOG_LOCK = threading.Lock()


class SampleError(RuntimeError):
    """A safe error description that never includes credentials or HTTP bodies."""


class APIError(SampleError):
    def __init__(self, method: str, path: str, status: int | None):
        self.status = status
        super().__init__(f"{method} {path}: " + (f"HTTP {status}" if status else "network request failed"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(value: dict) -> None:
    with _LOG_LOCK:
        print(json.dumps(value, ensure_ascii=False), flush=True)


def stable_id(seed: int, index: int, step: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{GENERATOR_VERSION}:{seed}:{index}:{step}"))


def normalize_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise SampleError("Invalid platform base URL") from exc
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise SampleError("The base URL must be a plain HTTP(S) origin without credentials, a path, query or fragment")
    if parsed.scheme == "http" and parsed.hostname.lower() not in _LOCAL_HOSTS:
        raise SampleError("Use HTTPS for a remote platform; HTTP is permitted only for loopback deployment checks")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}" + (f":{port}" if port else "")


def quotas(count: int) -> dict[str, int]:
    if not 20 <= count <= 30:
        raise SampleError("Presentation batches must contain 20 to 30 samples")
    edge = round(count / 4)
    return {"low": edge, "medium": count - 2 * edge, "high": edge}


def _weighted(rng: random.Random, values: list, weights: list[float]):
    return rng.choices(values, weights=weights, k=1)[0]


def _background(rng: random.Random) -> dict:
    year = _weighted(rng, [1, 2, 3, 4, 5], [27, 27, 25, 19, 2])
    age = _weighted(rng, [17 + year, 18 + year, 19 + year], [61, 34, 5])
    return {
        "age_band": str(age) if age <= 22 else "23-plus",
        "education_stage": "bachelor",
        "undergraduate_year": f"year-{year}" if year <= 4 else "year-5-plus",
    }


def make_plans(count: int, seed: int) -> list[dict]:
    """Draw a reproducible illustrative sample; never assert empirical realism."""
    rng = random.Random(seed)
    bands = [band for band, number in quotas(count).items() for _ in range(number)]
    rng.shuffle(bands)
    plans = []
    for index, band in enumerate(bands, 1):
        probability = {"low": .36, "medium": .63, "high": .87}[band]
        probability = min(.96, max(.12, rng.gauss(probability, .055)))
        for _ in range(5000):
            answers = {}
            for item in FORM_ITEMS:
                correct = rng.random() < min(.98, max(.03, probability + rng.gauss(0, .08)))
                wrong = [option for option in range(len(item["options"])) if option != item["correct"]]
                answers[item["id"]] = item["correct"] if correct else _weighted(rng, wrong, [1, 1, .65])
            self_center = {"low": 1.35, "medium": 2.1, "high": 2.85}[band] + rng.gauss(0, .45)
            self_answers = {item["id"]: max(0, min(4, round(rng.gauss(self_center, .9)))) for item in SELF_ITEMS}
            ability = score_form(answers, self_answers)
            if ability["band"] == band:
                break
        else:
            raise SampleError("Could not sample questionnaire answers in the requested ability stratum")
        background = _background(rng)
        plans.append({
            "index": index, "sample_id": f"P{index:03d}", "target_band": band,
            "ability": ability, "planned_turns": _weighted(rng, [2, 3, 4], [58, 35, 7]),
            "profile_seed": rng.randrange(2**32),
            "start_payload": {
                "consent": True, "university_confirmed": True, **background,
                "translation_experience": max(1, min(5, self_answers["s06"] + 1 + rng.choice([-1, 0, 0, 1]))),
                "pretest_version": FORM_VERSION, "answers": answers, "self_answers": self_answers,
                "request_id": stable_id(seed, index, "start"), "researcher_preview": False,
            },
            "session_id": None, "rounds": [], "provided_card_ids": [],
            "status": "planned", "submission": None,
        })
    return plans


def save_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".translation-samples-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def manifest_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SampleError("Another generator process is using this manifest") from exc
        yield
    finally:
        os.close(descriptor)


def open_manifest(path: Path, base_url: str, count: int, seed: int, pace_seconds: float = 0) -> dict:
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        expected = {"generator_version": GENERATOR_VERSION, "base_url": base_url,
                    "count": count, "seed": seed, "pace_seconds": pace_seconds,
                    "form_version": FORM_VERSION, "task_version": TASK_VERSION}
        if any(value.get(key) != item for key, item in expected.items()):
            raise SampleError("The manifest belongs to a different origin, plan, generator or material version")
        if len(value.get("samples", [])) != count:
            raise SampleError("The manifest sample count is damaged")
        validate_manifest_prompt(value)
        value["prompt_version"] = PROMPT_VERSION
        return value
    result = {
        "generator_version": GENERATOR_VERSION, "base_url": base_url, "count": count, "seed": seed,
        "pace_seconds": pace_seconds,
        "form_version": FORM_VERSION, "task_version": TASK_VERSION, "prompt_version": PROMPT_VERSION,
        "created_at": utc_now(),
        "method": METHOD, "target_strata": quotas(count), "samples": make_plans(count, seed),
    }
    for plan in result["samples"]:
        plan["pace_total_seconds"] = round(random.Random(plan["profile_seed"] + 101).uniform(.7, 1.8) * pace_seconds, 2)
    save_manifest(path, result)
    return result


def validate_manifest_prompt(manifest: dict) -> None:
    preflight = manifest.get("preflight")
    recorded = preflight.get("versions", {}).get("prompt") if isinstance(preflight, dict) else None
    version = manifest.get("prompt_version") if "prompt_version" in manifest else recorded
    if version != PROMPT_VERSION or (preflight is not None and recorded != PROMPT_VERSION):
        raise SampleError("The manifest prompt version differs from this generator; keep the existing batch unchanged and use a new manifest and seed")


class Platform:
    def __init__(self, base_url: str, admin_password: str, access_code: str, *, request_timeout: float = 210,
                 poll_seconds: float = 2, pending_timeout: float = 650):
        self.base_url = base_url
        self.admin_password = admin_password
        self.access_code = access_code
        self.poll_seconds = poll_seconds
        self.pending_timeout = pending_timeout
        self.client = httpx.Client(base_url=base_url, timeout=httpx.Timeout(request_timeout, connect=15),
                                   follow_redirects=False, trust_env=urlsplit(base_url).hostname not in _LOCAL_HOSTS)

    def close(self) -> None:
        self.client.close()

    def request(self, method: str, path: str, payload: dict | None = None, *, admin: bool = False) -> dict:
        try:
            response = self.client.request(method, path, json=payload,
                auth=("admin", self.admin_password) if admin else None,
                headers={"Cache-Control": "no-store"})
        except httpx.RequestError as exc:
            raise APIError(method, path, None) from exc
        if not response.is_success:
            raise APIError(method, path, response.status_code)
        try:
            value = response.json()
        except ValueError as exc:
            raise SampleError(f"{method} {path}: response was not JSON") from exc
        if not isinstance(value, dict):
            raise SampleError(f"{method} {path}: response was not an object")
        return value

    def preflight(self) -> dict:
        info = self.request("GET", "/api/translation/info")
        if info.get("data_purpose") != "demonstration" or info.get("demonstration_mode") is not True:
            raise SampleError("Refusing to generate samples outside an explicitly declared demonstration instance")
        if info.get("study_mode") != "pilot" or info.get("dialogue_backend") != "model_api":
            raise SampleError("Presentation generation requires pilot mode with actual model API dialogue")
        scope = info.get("participant_scope", {})
        if (scope.get("education_stage") != "bachelor" or scope.get("minimum_age") != 18
                or set(scope.get("age_bands", [])) != {"18", "19", "20", "21", "22", "23-plus"}
                or set(scope.get("undergraduate_years", [])) != {"year-1", "year-2", "year-3", "year-4", "year-5-plus"}):
            raise SampleError("The destination has not deployed the current undergraduate eligibility schema")
        form = self.request("GET", "/api/translation/form")
        if (form.get("version") != FORM_VERSION or info.get("versions", {}).get("task") != TASK_VERSION
                or info.get("versions", {}).get("prompt") != PROMPT_VERSION
                or [(item.get("id"), item.get("options")) for item in form.get("items", [])]
                != [(item["id"], item["options"]) for item in FORM_ITEMS]
                or [(item.get("id"), item.get("options")) for item in form.get("self_items", [])]
                != [(item["id"], item["options"]) for item in SELF_ITEMS]):
            raise SampleError("The destination questionnaire, translation task or prompt version differs from this generator")
        config = self.request("GET", "/api/admin/translation/models", admin=True)
        dialogue = config.get("dialogue", {})
        provider = urlsplit(dialogue.get("base_url", ""))
        if (provider.scheme != "https" or not provider.hostname or provider.hostname.lower() in _LOCAL_HOSTS
                or not dialogue.get("api_key_configured") or not dialogue.get("model")):
            raise SampleError("The destination must have a configured remote HTTPS cloud model connection")
        if info.get("access_code_required") and not self.access_code:
            raise SampleError("Set PARTICIPANT_ACCESS_CODE or TRANSLATION_SAMPLE_ACCESS_CODE in the execution environment")
        return {"data_purpose": "demonstration", "study_mode": info["study_mode"],
                "versions": info["versions"], "model": dialogue["model"], "checked_at": utc_now()}

    def session(self, session_id: str) -> dict:
        return self.request("GET", f"/api/translation/session/{session_id}")

    def record(self, session_id: str) -> dict:
        return self.request("GET", f"/api/admin/translation/records/{session_id}?raw=1", admin=True)

    def start(self, payload: dict) -> dict:
        for attempt in range(3):
            try:
                return self.request("POST", "/api/translation/start", {**payload, "access_code": self.access_code})
            except APIError as exc:
                if attempt == 2 or exc.status not in {None, 429, 500, 502, 503, 504}:
                    raise
                time.sleep(1 + attempt)
        raise AssertionError("unreachable")

    def wait_for_pending(self, view: dict) -> dict:
        deadline = time.monotonic() + self.pending_timeout
        while view.get("pending_reply"):
            if time.monotonic() >= deadline:
                raise SampleError("An accepted model request is still pending; resume with the same manifest later")
            time.sleep(self.poll_seconds)
            view = self.session(view["id"])
        return view

    def complete_round(self, session_id: str, turn: dict) -> dict:
        """Recover first; every retry uses exactly the persisted ID and content."""
        for attempt in range(3):
            view = self.wait_for_pending(self.session(session_id))
            if turn["request_id"] in view.get("completed_request_ids", []):
                return view
            if view.get("ended_at"):
                raise SampleError("The session was already submitted before the current planned request")
            if view.get("remaining_seconds", 0) <= 0:
                raise SampleError("The session dialogue time expired; no replacement session will be created")
            try:
                return self.request("POST", f"/api/translation/session/{session_id}/message",
                                    {"content": turn["content"], "request_id": turn["request_id"]})
            except APIError as exc:
                # A lost response may still have committed. Always sync before a retry.
                if attempt == 2 or exc.status not in {None, 409, 429, 500, 502, 503, 504}:
                    raise
                time.sleep(1 + attempt)
        raise AssertionError("unreachable")


def _last_assistant(view: dict) -> dict:
    for message in reversed(view.get("messages", [])):
        if message.get("role") == "assistant":
            return message
    raise SampleError("The completed request contains no assistant reply")


def _round_reply(view: dict, turn: dict, offset: int) -> dict:
    # A recovered GET may already contain later turns. Select this turn's own
    # reply rather than accidentally copying the latest assistant into it.
    messages = view.get("messages", [])
    position = offset * 2
    if (position + 1 >= len(messages) or messages[position].get("role") != "user"
            or messages[position].get("content") != turn["content"]
            or messages[position + 1].get("role") != "assistant"):
        raise SampleError("The session dialogue differs from the persisted request sequence")
    return messages[position + 1]


def _context_excerpt(card: dict, detailed: bool) -> str:
    body = str(card.get("body", "")).strip()
    if not detailed:
        # Truncate only at sentence boundaries; never invent an abridged fact.
        sentences = re.findall(r"[^。！？]+[。！？]?", body)
        body = "".join(sentences[:2]).strip() if sentences else body
    return f"{card.get('title', card['id'])}：{body}"


def make_message(plan: dict, view: dict, round_number: int, *, final: bool) -> tuple[str, list[str]]:
    """Use only cards actually exposed by this session's participant API."""
    rng = random.Random(plan["profile_seed"] + round_number * 7919)
    band = plan["target_band"]
    cards = view.get("task", {}).get("context_cards", [])
    available = [card for card in cards if card["id"] not in plan["provided_card_ids"]]
    previous = _last_assistant(view) if round_number > 1 else {}
    text = previous.get("content", "").lower()
    ranked = sorted(available, key=lambda card: (
        -sum(cue in text for cue in _CARD_CUES.get(card["id"], ())), rng.random()))
    if round_number == 1:
        number = {"low": rng.choice([0, 0, 0, 1]), "medium": rng.choice([0, 0, 1, 1]),
                  "high": rng.choice([1, 1, 2])}[band]
        lead = rng.choice(["请帮我把这段英文译成中文。", "我想完成页面上的小说英译中任务，请协助翻译。", "请按照页面委托翻译这段小说，中文自然一些。"])
    else:
        number = {"low": 1, "medium": rng.choice([1, 1, 2]), "high": rng.choice([2, 2, 3])}[band]
        lead = ("我查看了页面补充资料，能确认以下内容。" if previous.get("action") == "clarification"
                else rng.choice(["我又查了页面资料，请据此核对并修改译文。", "补充这些背景，请帮我检查前一版表达。", "这些是作者提供的信息，请结合它们修订。 "]))
    selected = ranked[:number]
    detailed = rng.random() < {"low": .25, "medium": .55, "high": .85}[band]
    pieces = [lead, *(_context_excerpt(card, detailed) for card in selected)]
    if not final and round_number > 1:
        pieces.append(rng.choice(["请不要增加原文没有的情节。", "我希望保留原文的悬念，人物名字也请统一。", "请保留对话简短、克制的语气。"]))
    if final:
        pieces.append(
            "我目前无法再补充其他信息，请根据已经确认的内容，直接给出整段从头到尾的完整中文译文。"
            "未确定之处保留原文的含混，不再提问，不添加情节，也不要将背景解释扩写进正文。"
            + FINAL_JSON_INSTRUCTION)
    return "\n".join(pieces), [card["id"] for card in selected]


def extract_translation(message: dict) -> str:
    if message.get("action") != "answer":
        raise SampleError("The last model reply is a clarification, not a submitted translation")
    content = message.get("content", "")
    if content.count(FINAL_BEGIN) != 1 or content.count(FINAL_END) != 1:
        raise SampleError("The model did not delimit a final translation unambiguously")
    before, remainder = content.split(FINAL_BEGIN)
    translation, after = remainder.split(FINAL_END)
    translation = translation.strip()
    if before.strip() or after.strip():
        raise SampleError("The model included extra content outside the final translation")
    if (not 100 <= len(translation) <= 12000 or len(re.findall(r"[\u3400-\u9fff]", translation)) < 90
            or any(name not in translation for name in ("玛拉", "罗温", "钟楼宅"))
            or any(cue in translation for cue in ("请提供更多", "请补充背景", "无法完成翻译", "请确认以下"))):
        raise SampleError("The model output does not pass the minimum complete-translation checks")
    return translation


def _validate_record(record: dict, plan: dict, *, completed: bool) -> None:
    if record.get("versions", {}).get("prompt") != PROMPT_VERSION:
        raise SampleError(f"{plan['sample_id']}: server record prompt version differs from this generator")
    if (record.get("data_purpose") != "demonstration" or record.get("record_kind") != "participant"
            or record.get("study_mode") != "pilot" or record.get("condition") not in {"S", "A"}
            or record.get("band") != plan["target_band"] or record.get("ability_score") != plan["ability"]["score"]):
        raise SampleError(f"{plan['sample_id']}: server record purpose, assignment or ability differs from the plan")
    payload = plan["start_payload"]
    for field in ("answers", "self_answers", "age_band", "education_stage", "undergraduate_year", "translation_experience"):
        if record.get(field) != payload[field]:
            raise SampleError(f"{plan['sample_id']}: server questionnaire or intake does not match the manifest")
    if not completed:
        return
    assistants = [message for message in record.get("messages", []) if message.get("role") == "assistant"]
    reply_events = [event for event in record.get("events", []) if event.get("type") == "reply"]
    if (not record.get("ended_at") or not 2 <= len(assistants) <= 4
            or len(reply_events) != len(assistants) or record.get("submission") != plan["submission"]
            or not isinstance(record.get("submission"), str) or not record["submission"].strip()):
        raise SampleError(f"{plan['sample_id']}: incomplete dialogue, model reply audit or final submission")
    if any(type(event.get("seconds")) not in {int, float} or event["seconds"] < 0 for event in reply_events):
        raise SampleError(f"{plan['sample_id']}: missing actual API response timing")
    if extract_translation(assistants[-1]) != record["submission"]:
        raise SampleError(f"{plan['sample_id']}: submission differs from the actual model's final translation")
    created = datetime.fromisoformat(record["created_at"])
    ended = datetime.fromisoformat(record["ended_at"])
    if (created.tzinfo is None or ended.tzinfo is None or created.utcoffset().total_seconds() != 0
            or ended.utcoffset().total_seconds() != 0 or ended < created):
        raise SampleError(f"{plan['sample_id']}: invalid server UTC timestamps")


def pace_round(platform: Platform, plan: dict, turn: dict, persist) -> None:
    if not plan.get("pace_total_seconds"):
        return
    view = platform.wait_for_pending(platform.session(plan["session_id"]))
    if turn["request_id"] in view.get("completed_request_ids", []):
        return
    target = plan["pace_total_seconds"] / plan["planned_turns"]
    waited = turn.get("pace_waited_seconds", 0)
    if waited >= target:
        return
    log({"sample": plan["sample_id"], "status": "pacing", "remaining_wait_seconds": round(target - waited, 2)})
    while waited < target:
        interval = min(30, target - waited)
        time.sleep(interval)
        waited = min(target, waited + interval)
        turn["pace_waited_seconds"] = waited
        persist()


def repair_final_format(platform: Platform, manifest: dict, plan: dict, turn: dict,
                        number: int, persist) -> None:
    """Preserve legacy requests; replace only failed/unsent final requests.

    The original ID/content pair remains in the server audit and a full local
    history snapshot. Completed or accepted pending requests must resolve before
    deciding whether a replacement is safe. A replacement itself never changes
    its ID or content on subsequent retries.
    """
    if not turn.get("final_requested") or turn.get("format_protocol") == FORMAT_PROTOCOL:
        return
    previous_id = turn["request_id"]
    current_view = platform.wait_for_pending(platform.session(plan["session_id"]))
    if previous_id in current_view.get("completed_request_ids", []):
        return
    current_record = platform.record(plan["session_id"])
    previous_request = current_record.get("requests", {}).get(previous_id)
    previous_status = previous_request.get("status") if previous_request else None
    if previous_status == "completed":
        return
    if current_record.get("pending") or previous_status not in {None, "failed"}:
        raise SampleError("A legacy final request has not conclusively failed; keep its original ID and sync first")
    replacement_id = stable_id(manifest["seed"], plan["index"], f"message-{number}-json-format-retry")
    if replacement_id == previous_id:
        raise SampleError("The final format-retry request is missing its protocol metadata")
    previous_snapshot = deepcopy({key: value for key, value in turn.items() if key != "format_retries"})
    previous_snapshot.update(server_request_status=previous_status or "not_sent",
                             replaced_at=utc_now(), reason="explicit-json-content-field-format")
    history = turn.setdefault("format_retries", [])
    history.append(previous_snapshot)
    turn.update(request_id=replacement_id, content=turn["content"] + "\n" + FINAL_JSON_INSTRUCTION,
                status="planned", format_protocol=FORMAT_PROTOCOL, replaces=previous_id)
    turn.pop("reply", None)
    persist()  # The replacement ID/content pair must reach disk before a POST.


def run_sample(platform: Platform, manifest: dict, plan: dict, persist) -> None:
    if not plan.get("session_id"):
        # The start request ID is on disk before the first network mutation.
        persist()
        view = platform.start(plan["start_payload"])
        plan["session_id"] = view["id"]
        plan["status"] = "started"
        persist()
    else:
        view = platform.session(plan["session_id"])
    record = platform.record(plan["session_id"])
    _validate_record(record, plan, completed=False)
    plan["condition"] = record["condition"]
    if record.get("ended_at"):
        _validate_record(record, plan, completed=True)
        plan["status"] = "completed"
        plan["verified_at"] = utc_now()
        persist()
        return
    for offset in range(4):
        number = offset + 1
        if offset < len(plan["rounds"]):
            turn = plan["rounds"][offset]
        else:
            final = number >= plan["planned_turns"]
            content, cards = make_message(plan, view, number, final=final)
            turn = {"request_id": stable_id(manifest["seed"], plan["index"], f"message-{number}"),
                    "content": content, "final_requested": final, "card_ids": cards, "status": "planned"}
            if final:
                turn["format_protocol"] = FORMAT_PROTOCOL
            plan["rounds"].append(turn)
            plan["provided_card_ids"].extend(cards)
            persist()
        repair_final_format(platform, manifest, plan, turn, number, persist)
        pace_round(platform, plan, turn, persist)
        view = platform.complete_round(plan["session_id"], turn)
        reply = _round_reply(view, turn, offset)
        turn.update(status="completed", reply=reply)
        persist()
        log({"sample": plan["sample_id"], "round": number,
             "reply_kind": reply.get("action"), "status": "reply_saved"})
        if turn["final_requested"]:
            try:
                plan["submission"] = extract_translation(reply)
                break
            except SampleError:
                if number == 4:
                    raise
    if not plan.get("submission"):
        raise SampleError(f"{plan['sample_id']}: no complete final translation after four model rounds")
    plan["status"] = "ready_to_submit"
    persist()
    platform.request("PUT", f"/api/translation/session/{plan['session_id']}/draft", {"draft": plan["submission"]})
    feedback_rng = random.Random(plan["profile_seed"] + 193)
    platform.request("POST", f"/api/translation/session/{plan['session_id']}/submit", {
        "submission": plan["submission"], "burden": feedback_rng.choice([2, 2, 3, 3, 4]),
        "helpfulness": feedback_rng.choice([2, 3, 3, 4, 4, 5])})
    record = platform.record(plan["session_id"])
    _validate_record(record, plan, completed=True)
    plan.update(status="completed", verified_at=utc_now(), created_at=record["created_at"],
                ended_at=record["ended_at"], actual_turns=len([m for m in record["messages"] if m["role"] == "assistant"]),
                model_reply_events=[event for event in record.get("events", []) if event.get("type") == "reply"])
    plan.pop("last_error", None)
    persist()


def _first_reply_action(record: dict, plan: dict) -> str:
    """Audit the actual first reply to the script's original, non-final request."""
    if not plan.get("rounds"):
        raise SampleError(f"{plan['sample_id']}: the persisted first request is missing")
    first = plan["rounds"][0]
    expected, shared = make_message({**plan, "provided_card_ids": []},
        {"task": record.get("task", {}), "messages": []}, 1, final=False)
    if (first.get("content") != expected or first.get("card_ids") != shared
            or len(shared) > 2 or first.get("final_requested") is not False):
        raise SampleError(f"{plan['sample_id']}: first request is not the generator's default request with at most two background cards; original records were retained")
    reply = _round_reply(record, first, 0)
    action = reply.get("action")
    return action if isinstance(action, str) else "missing"


def verify_batch(platform: Platform, manifest: dict) -> dict:
    validate_manifest_prompt(manifest)
    conditions, strata, ages, years, turns = Counter(), Counter(), Counter(), Counter(), Counter()
    first_actions = {condition: Counter() for condition in ("S", "A")}
    action_failures = []
    for plan in manifest["samples"]:
        if plan.get("status") != "completed" or not plan.get("session_id"):
            raise SampleError("The batch contains an unfinished sample")
        record = platform.record(plan["session_id"])
        _validate_record(record, plan, completed=True)
        action = _first_reply_action(record, plan)
        first_actions[record["condition"]][action] += 1
        expected_action = "clarification" if record["condition"] == "A" else "answer"
        if action != expected_action:
            action_failures.append(f"{plan['sample_id']}:{record['condition']}:{action}")
        conditions[record["condition"]] += 1
        strata[record["band"]] += 1
        ages[record["age_band"]] += 1
        years[record["undergraduate_year"]] += 1
        turns[sum(message["role"] == "assistant" for message in record["messages"])] += 1
    result = {"verified_at": utc_now(), "count": len(manifest["samples"]), "conditions": dict(conditions),
              "strata": dict(strata), "ages": dict(ages), "undergraduate_years": dict(years),
              "dialogue_turns": dict(turns), "scores_generated": 0, "prompt_version": PROMPT_VERSION,
              "first_reply_actions": {condition: dict(counts) for condition, counts in first_actions.items()}}
    if action_failures:
        raise SampleError("First-reply protocol failed (A requires clarification; S requires answer); "
            "original records and actions were retained. Counts: "
            + json.dumps(result["first_reply_actions"], sort_keys=True)
            + "; samples: " + ", ".join(action_failures))
    if dict(strata) != manifest["target_strata"]:
        raise SampleError("The server's ability strata do not match the predeclared presentation plan")
    if manifest["count"] == 24 and conditions != Counter({"S": 12, "A": 12}):
        raise SampleError("The backend assigned a different S/A balance; records were retained without overriding assignment")
    return result


def execute_batch(manifest: dict, path: Path, platform_factory, workers: int) -> None:
    """Workers own their mutable plans and clients; only immutable copies merge."""
    write_lock = threading.RLock()
    failures = []

    def execute(original: dict) -> None:
        plan = deepcopy(original)
        platform = platform_factory()
        def persist():
            with write_lock:
                manifest["samples"][plan["index"] - 1] = deepcopy(plan)
                save_manifest(path, manifest)
        try:
            log({"sample": plan["sample_id"], "status": plan["status"]})
            run_sample(platform, manifest, plan, persist)
            log({"sample": plan["sample_id"], "status": "completed", "turns": plan.get("actual_turns")})
        except Exception as exc:
            # Keep completed work from other workers, but never claim a complete batch.
            description = str(exc) if isinstance(exc, SampleError) else type(exc).__name__
            plan["last_error"] = description
            persist()
            raise SampleError(f"{plan['sample_id']}: {description}") from None
        finally:
            platform.close()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        # Copy before submitting so merges never mutate a queued worker's input.
        futures = [executor.submit(execute, deepcopy(plan)) for plan in manifest["samples"]]
        for future in as_completed(futures):
            try:
                future.result()
            except SampleError as exc:
                failures.append(str(exc))
    if failures:
        raise SampleError(f"{len(failures)} sample(s) need resumption. First error: {failures[0]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Platform origin, e.g. http://127.0.0.1:8004")
    parser.add_argument("--count", type=int, default=24)
    parser.add_argument("--seed", type=int, default=20261030)
    parser.add_argument("--manifest", type=Path, required=True, help="Private resumable JSON manifest, outside static/")
    parser.add_argument("--verify-only", action="store_true", help="Read and verify a completed batch without creating data")
    parser.add_argument("--request-timeout", type=float, default=210)
    parser.add_argument("--poll-seconds", type=float, default=2)
    parser.add_argument("--pace-seconds", type=float, default=0,
                        help="Actual total per-session wait, with seeded 0.7–1.8x variation; never changes timestamps")
    parser.add_argument("--workers", type=int, default=1, help="Independent HTTP clients, 1 to 4 concurrent samples")
    args = parser.parse_args(argv)
    platform = None
    try:
        base_url = normalize_base_url(args.base_url)
        quotas(args.count)
        if not (math.isfinite(args.request_timeout) and 1 <= args.request_timeout <= 600
                and math.isfinite(args.poll_seconds) and .1 <= args.poll_seconds <= 30
                and math.isfinite(args.pace_seconds) and 0 <= args.pace_seconds <= 300
                and 1 <= args.workers <= 4):
            raise SampleError("Request timeout or polling interval is invalid")
        path = args.manifest.resolve()
        if (ROOT / "static").resolve() in path.parents:
            raise SampleError("The private manifest cannot be saved in the public static directory")
        load_dotenv()
        password = os.getenv("TRANSLATION_SAMPLE_ADMIN_PASSWORD") or os.getenv("ADMIN_PASSWORD", "")
        access_code = os.getenv("TRANSLATION_SAMPLE_ACCESS_CODE") or os.getenv("PARTICIPANT_ACCESS_CODE", "")
        if not password:
            raise SampleError("Set ADMIN_PASSWORD or TRANSLATION_SAMPLE_ADMIN_PASSWORD in the execution environment")
        platform = Platform(base_url, password, access_code, request_timeout=args.request_timeout, poll_seconds=args.poll_seconds)
        with manifest_lock(path):
            preflight = platform.preflight()
            if args.verify_only and not path.exists():
                raise SampleError("Verification requires an existing completed manifest")
            manifest = open_manifest(path, base_url, args.count, args.seed, args.pace_seconds)
            manifest["preflight"] = preflight
            manifest["format_protocol"] = FORMAT_PROTOCOL
            persist = lambda: save_manifest(path, manifest)
            if not args.verify_only:
                execute_batch(manifest, path, lambda: Platform(base_url, password, access_code,
                    request_timeout=args.request_timeout, poll_seconds=args.poll_seconds), args.workers)
            manifest["verification"] = verify_batch(platform, manifest)
            persist()
            log(manifest["verification"])
        return 0
    except (SampleError, ValueError, OSError, KeyError, TypeError, IndexError) as exc:
        # Never log exception causes/HTTP response bodies or environment contents.
        print(json.dumps({"error": str(exc) if isinstance(exc, SampleError) else type(exc).__name__,
                          "action": "Keep the manifest; resolve the error and rerun the same command to resume."}), file=sys.stderr, flush=True)
        return 1
    finally:
        if platform is not None:
            platform.close()


if __name__ == "__main__":
    raise SystemExit(main())
