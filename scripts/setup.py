from __future__ import annotations

import secrets
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"


def main() -> None:
    if ENV.exists():
        print(f"已有配置文件：{ENV}")
        print("如需查看管理员密码，请在本机打开 .env 中的 ADMIN_PASSWORD。")
        return
    password = secrets.token_urlsafe(18)
    seed = secrets.token_hex(24)
    ENV.write_text(
        "# 本地实验配置；不要上传公开仓库\n"
        "STUDY_DOMAIN=\n"
        "STUDY_MODE=demo\n"
        "DATABASE_PATH=./data/lens.sqlite3\n"
        f"ADMIN_PASSWORD={password}\n"
        f"ASSIGNMENT_SEED={seed}\n"
        "PARTICIPANT_ACCESS_CODE=\n"
        "GROUP_CAP=0\n"
        "LITERACY_BAND_WIDTH=10\n"
        "MODEL_BASE_URL=https://api.deepseek.com\n"
        "MODEL_API_KEY=\n"
        "MODEL_NAME=deepseek-flash\n"
        "MODEL_TEMPERATURE=0\n"
        "MODEL_THINKING=disabled\n"
        "MODEL_MAX_TOKENS=800\n",
        encoding="utf-8",
    )
    try:
        ENV.chmod(0o600)
    except OSError:
        pass
    print(f"已生成：{ENV}")
    print(f"研究者用户名：admin\n研究者密码：{password}")
    print("请妥善保存密码。当前为 DEMO 模式，不能用于正式研究分析。")


if __name__ == "__main__":
    main()
