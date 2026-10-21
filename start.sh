#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
if ! .venv/bin/python -c 'import fastapi,uvicorn,httpx' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r requirements.txt
fi
.venv/bin/python scripts/setup.py
echo "参与者页面：http://127.0.0.1:8000/"
echo "研究者后台：http://127.0.0.1:8000/admin"
.venv/bin/python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
