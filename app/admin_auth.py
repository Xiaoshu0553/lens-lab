"""Short-lived, password-bound administrator sessions without browser auth dialogs."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from urllib.parse import urlparse

from fastapi import HTTPException, Request


SESSION_COOKIE = "lens_admin_session"
BROWSER_COOKIE = "lens_admin_browser"
SESSION_SECONDS = 8 * 60 * 60
ADMIN_PAGES = {"/admin", "/admin/preview", "/admin/instruments"}


def issue_session(password: str, *, now: float | None = None) -> str:
    expires = int(time.time() if now is None else now) + SESSION_SECONDS
    payload = f"v1.{expires}.{secrets.token_hex(24)}"
    signature = hmac.new(password.encode("utf-8"), ("lens-admin-session:" + payload).encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def valid_session(token: str | None, password: str, *, now: float | None = None) -> bool:
    if not token or not re.fullmatch(r"v1\.[0-9]{1,12}\.[0-9a-f]{48}\.[0-9a-f]{64}", token):
        return False
    payload, signature = token.rsplit(".", 1)
    expires = int(payload.split(".")[1])
    current = time.time() if now is None else now
    if not current < expires <= current + SESSION_SECONDS:
        return False
    expected = hmac.new(password.encode("utf-8"), ("lens-admin-session:" + payload).encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def is_https(request: Request) -> bool:
    # The existing Nginx proxy overwrites this header before forwarding to Docker.
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is None:
        return  # Non-browser clients may omit Origin; browser fetches send it on POST.
    try:
        supplied = urlparse(origin)
        expected = urlparse(f"{'https' if is_https(request) else 'http'}://{request.headers.get('host', '')}")
        def identity(url):
            return url.scheme, url.hostname, url.port or (443 if url.scheme == "https" else 80)
        valid = (supplied.scheme in {"http", "https"} and not supplied.username and not supplied.password
                 and not supplied.path and not supplied.query and not supplied.fragment
                 and identity(supplied) == identity(expected))
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(403, "请在本站页面登录并操作")
