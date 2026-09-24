"""A shared password and a signed session cookie.

Enough to put Cohandle on the open internet without putting the whole
pipeline on the open internet. It is not user accounts: everyone with the
password is "operator", which matches how the approval trail already
records edits.

This is the interim measure. The real answer is Identity-Aware Proxy in
front of Cloud Run, where the check happens before the request reaches
this process at all. When IAP is on, leave APP_PASSWORD unset and this
module gets out of the way.

No new dependency: the cookie is signed with stdlib HMAC-SHA256. The
format is `payload.signature`, both base64url without padding, where the
payload carries the issue time so a stolen cookie expires on its own.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.config import settings

COOKIE = "cohandle_session"

# Paths that must answer before anyone has logged in. The probes are
# Cloud Run's, /static is the stylesheet the login page itself needs,
# and /internal carries its own token check in main.py.
OPEN = ("/healthz", "/readyz", "/login", "/logout", "/static/", "/internal/")


def enabled() -> bool:
    """Auth is on when a password is set, off when it is not.

    Off is the right local default and the right behaviour behind IAP.
    Boot logs which one is in force, so it is never a silent state.
    """
    return bool(settings().app_password)


def _key() -> bytes:
    """The HMAC key.

    SESSION_SECRET if set. Otherwise derived from the password, which
    costs nothing: anyone who can guess the password can log in anyway,
    so a separate secret adds no strength over a weak password. Deriving
    also means changing the password signs everyone out, which is what
    you want on the day you change it.
    """
    explicit = settings().session_secret
    if explicit:
        return explicit.encode()
    return hashlib.sha256(b"cohandle-session-v1|" + settings().app_password.encode()).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue() -> str:
    payload = f"v1:{int(time.time())}".encode()
    sig = hmac.new(_key(), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(sig)}"


def valid(cookie: str | None) -> bool:
    if not cookie or "." not in cookie:
        return False
    body, sig = cookie.rsplit(".", 1)
    try:
        payload, given = _unb64(body), _unb64(sig)
    except Exception:
        return False

    expected = hmac.new(_key(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(given, expected):
        return False

    # Signature holds, so the payload is ours and can be parsed without
    # guarding against hostile input.
    version, _, issued = payload.decode().partition(":")
    if version != "v1":
        return False
    age = time.time() - int(issued)
    return 0 <= age <= settings().session_max_age_days * 86400


def check_password(given: str) -> bool:
    """Constant-time, so the comparison does not leak the prefix."""
    return hmac.compare_digest(given.encode(), settings().app_password.encode())


def set_cookie(response: Response, request: Request) -> None:
    response.set_cookie(
        COOKIE, issue(),
        max_age=settings().session_max_age_days * 86400,
        httponly=True,          # the cookie is never read by JavaScript
        samesite="lax",         # survives a normal link, not a cross-site POST
        secure=_is_https(request),
        path="/",
    )


def clear_cookie(response: Response) -> None:
    """Sign out this browser.

    Nothing is stored server side, so this drops the cookie rather than
    revoking the token: a copy taken before signing out would still
    verify until it expires. To cut every session at once, change
    APP_PASSWORD, which changes the signing key.
    """
    response.delete_cookie(COOKIE, path="/")


def _is_https(request: Request) -> bool:
    """Secure flag on in production, off on http://localhost.

    Cloud Run terminates TLS upstream, so the scheme this process sees is
    http unless uvicorn is trusting proxy headers. Read the header too
    rather than depend on that flag being set.
    """
    forwarded = request.headers.get("x-forwarded-proto", "")
    return request.url.scheme == "https" or forwarded.split(",")[0].strip() == "https"


# Per-IP failure counter. In-memory, so it resets on deploy and does not
# span Cloud Run instances. It is a speed bump against someone guessing
# the password, not a lockout you could rely on.
_LOCKOUT_AFTER = 8
_LOCKOUT_FOR = 300
_failures: dict[str, list[float]] = {}


def note_failure(ip: str) -> None:
    now = time.time()
    hits = [t for t in _failures.get(ip, []) if now - t < _LOCKOUT_FOR]
    hits.append(now)
    _failures[ip] = hits


def locked_out(ip: str) -> int:
    """Seconds remaining, or 0."""
    now = time.time()
    hits = [t for t in _failures.get(ip, []) if now - t < _LOCKOUT_FOR]
    _failures[ip] = hits
    if len(hits) < _LOCKOUT_AFTER:
        return 0
    return int(_LOCKOUT_FOR - (now - hits[0])) or 1


def clear_failures(ip: str) -> None:
    _failures.pop(ip, None)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def gate(request: Request, call_next):
    """Middleware. Everything but OPEN needs a valid cookie."""
    if not enabled() or request.url.path.startswith(OPEN):
        return await call_next(request)

    if valid(request.cookies.get(COOKIE)):
        return await call_next(request)

    if request.method == "GET":
        # Come back to the page they asked for, not the queue.
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)

    # A POST with a dead session is usually a form submitted after the
    # cookie expired. Say so plainly rather than redirecting, which would
    # silently drop what they typed.
    return JSONResponse({"detail": "session expired, reload and sign in again"}, status_code=401)
