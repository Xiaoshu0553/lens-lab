from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]


def validate_preview_api_prefix(value: str) -> str:
    if value and not re.fullmatch(r"/(?:[A-Za-z0-9_-]+)(?:/[A-Za-z0-9_-]+)*", value):
        raise ValueError("PREVIEW_API_PREFIX 必须为空或同源绝对路径前缀")
    return value


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    database_path: Path
    study_mode: str
    admin_password: str
    model_api_key: str
    model_base_url: str
    model_name: str
    model_temperature: float
    assignment_seed: str
    max_turns: int = 12
    max_minutes: int = 30
    group_cap: int = 0
    clarification_strategy: str = "prompt"
    literacy_band_width: int = 10
    model_thinking: str = "disabled"
    model_max_tokens: int = 1600
    participant_access_code: str = ""
    minor_access_code: str = ""
    under14_privacy_notice_url: str = ""
    showcase_mode: bool = False
    synthetic_dataset_root: Path | None = None
    preview_api_prefix: str = ""
    researcher_preview_only: bool = False
    translation_models_path: Path | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        mode = os.getenv("STUDY_MODE", "demo").lower()
        if mode not in {"demo", "pilot", "live"}:
            raise ValueError("STUDY_MODE 只能是 demo、pilot 或 live")
        showcase_flag = os.getenv("SHOWCASE_MODE", "0")
        if showcase_flag not in {"0", "1"}:
            raise ValueError("SHOWCASE_MODE 只能是 0 或 1")
        showcase_mode = showcase_flag == "1"
        preview_only_flag = os.getenv("RESEARCHER_PREVIEW_ONLY", "0")
        if preview_only_flag not in {"0", "1"}:
            raise ValueError("RESEARCHER_PREVIEW_ONLY 只能是 0 或 1")
        preview_prefix = validate_preview_api_prefix(os.getenv("PREVIEW_API_PREFIX", ""))
        if showcase_mode and mode != "demo":
            raise ValueError("演示成品只能使用 STUDY_MODE=demo")
        strategy = os.getenv("CLARIFICATION_STRATEGY", "prompt").lower()
        if strategy not in {"module", "prompt"}:
            raise ValueError("CLARIFICATION_STRATEGY 只能是 module 或 prompt")
        # Accept old deployment files while always using the two-arm prompt design.
        strategy = "prompt"
        band_width = int(os.getenv("LITERACY_BAND_WIDTH", "10"))
        if not 1 <= band_width <= 100 or 100 % band_width:
            raise ValueError("LITERACY_BAND_WIDTH 必须是能整除 100 的正整数")
        password = os.getenv("ADMIN_PASSWORD", "")
        if not password:
            raise ValueError("请先运行 python scripts/setup.py，生成管理员密码")
        key = os.getenv("MODEL_API_KEY", "").strip()
        if key in {"自行填写API", "请自行填写API", "YOUR_API_KEY"}:
            key = ""
        base_url = os.getenv("MODEL_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        local_provider = urlparse(base_url).hostname in {"localhost", "127.0.0.1", "::1"}
        if mode in {"pilot", "live"} and not key and not local_provider:
            raise ValueError("请自行填写API：远程模型服务必须配置 MODEL_API_KEY；本地 Ollama 可以留空")
        db = Path(os.getenv("DATABASE_PATH", str(ROOT / "translation-data" / "lens.sqlite3")))
        if not db.is_absolute():
            db = ROOT / db
        model_name = os.getenv("MODEL_NAME", "example-model-id")
        if mode in {"pilot", "live"} and model_name == "example-model-id":
            raise ValueError("真实模型模式必须把 MODEL_NAME 改为实际模型名称")
        thinking = os.getenv("MODEL_THINKING", "disabled").lower()
        if thinking not in {"enabled", "disabled"}:
            raise ValueError("MODEL_THINKING 只能是 enabled 或 disabled")
        max_tokens = int(os.getenv("MODEL_MAX_TOKENS", "1600"))
        if not 1 <= max_tokens <= 8192:
            raise ValueError("MODEL_MAX_TOKENS 必须在 1–8192 之间")
        max_turns = int(os.getenv("MAX_TURNS", "12"))
        max_minutes = int(os.getenv("MAX_MINUTES", "30"))
        if not 2 <= max_turns <= 100:
            raise ValueError("MAX_TURNS 必须在 2–100 之间")
        if not 1 <= max_minutes <= 180:
            raise ValueError("MAX_MINUTES 必须在 1–180 之间")
        temperature = float(os.getenv("MODEL_TEMPERATURE", "0"))
        if not math.isfinite(temperature) or not 0 <= temperature <= 2:
            raise ValueError("MODEL_TEMPERATURE 必须在 0–2 之间")
        access_code = os.getenv("PARTICIPANT_ACCESS_CODE", "")
        if mode == "live" and not access_code:
            raise ValueError("正式模式必须设置 PARTICIPANT_ACCESS_CODE")
        return cls(
            database_path=db,
            study_mode=mode,
            admin_password=password,
            model_api_key=key,
            model_base_url=base_url,
            model_name=model_name,
            model_temperature=temperature,
            max_turns=max_turns,
            max_minutes=max_minutes,
            assignment_seed=os.getenv("ASSIGNMENT_SEED", "lens-local-seed"),
            group_cap=max(0, int(os.getenv("GROUP_CAP", "0"))),
            clarification_strategy=strategy,
            literacy_band_width=band_width,
            model_thinking=thinking,
            model_max_tokens=max_tokens,
            participant_access_code=access_code,
            minor_access_code=os.getenv("MINOR_ACCESS_CODE", ""),
            under14_privacy_notice_url=os.getenv("UNDER14_PRIVACY_NOTICE_URL", ""),
            showcase_mode=showcase_mode,
            synthetic_dataset_root=Path(os.environ["SYNTHETIC_DATASET_ROOT"]) if os.getenv("SYNTHETIC_DATASET_ROOT") else None,
            preview_api_prefix=preview_prefix,
            researcher_preview_only=preview_only_flag == "1",
            translation_models_path=Path(os.getenv("TRANSLATION_MODELS_PATH", str(ROOT / ".translation-models.json"))),
        )
