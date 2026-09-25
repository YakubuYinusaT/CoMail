"""Zoho Mail, the only part of Cohandle that talks to the outside world.

Everything else in this project can be re-run. This cannot: a sent email
is sent. So the client is deliberately narrow. It refreshes a token, it
reads the account list, and it posts one message. It does not delete,
move, or modify anything in the mailbox.

Two guards live here rather than further up, because they are the ones
that must hold even if a caller is wrong:

    send() refuses unless SEND_ENABLED is true. Credentials alone are not
    permission. Setting them up and arming the thing are separate
    decisions, taken on separate days.

    send() refuses a recipient it was not given explicitly. There is no
    default, no fallback, no "to the address on file".

Zoho is regional. An account created under .eu or .in answers on
mail.zoho.eu and accounts.zoho.eu, and calling .com with those
credentials fails with an error that reads like a scope problem and is
not one. ZOHO_API_BASE carries the region and the accounts host is
derived from it.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.config import settings

log = logging.getLogger("cohandle.zoho")

TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class ZohoError(RuntimeError):
    """Anything Zoho refused, with enough detail to act on."""


class NotArmed(RuntimeError):
    """Credentials exist but sending has not been switched on."""


@dataclass
class Token:
    value: str
    expires_at: float

    @property
    def fresh(self) -> bool:
        # A minute of slack: a token that expires mid-flight reads as an
        # authentication bug rather than as a clock.
        return bool(self.value) and time.time() < self.expires_at - 60


_token: Token | None = None
_lock = threading.Lock()


def accounts_base() -> str:
    """accounts.zoho.<region>, derived from the mail host."""
    host = urlsplit(settings().zoho_api_base).netloc or "mail.zoho.com"
    return "https://" + host.replace("mail.", "accounts.", 1)


def _require(*names: str) -> None:
    cfg = settings()
    missing = [n for n in names if not getattr(cfg, n)]
    if missing:
        raise ZohoError("not configured: " + ", ".join(
            n.upper() for n in missing) + ". Use scripts/setkey.py.")


def access_token(force: bool = False) -> str:
    """A live access token, refreshed at most once across threads."""
    global _token
    with _lock:
        if not force and _token is not None and _token.fresh:
            return _token.value

        _require("zoho_client_id", "zoho_client_secret", "zoho_refresh_token")
        cfg = settings()
        response = httpx.post(
            f"{accounts_base()}/oauth/v2/token",
            data={
                "refresh_token": cfg.zoho_refresh_token,
                "client_id": cfg.zoho_client_id,
                "client_secret": cfg.zoho_client_secret,
                "grant_type": "refresh_token",
            },
            timeout=TIMEOUT,
        )
        payload = _json(response)
        # Zoho answers 200 with an error field rather than a 4xx.
        if "access_token" not in payload:
            raise ZohoError(f"token refresh refused: "
                            f"{payload.get('error', payload)}")
        _token = Token(payload["access_token"],
                       time.time() + float(payload.get("expires_in", 3600)))
        return _token.value


def _json(response: httpx.Response) -> dict:
    try:
        return response.json()
    except ValueError:
        raise ZohoError(f"HTTP {response.status_code}, and the body was not "
                        f"JSON: {response.text[:300]}") from None


def _call(method: str, path: str, json: dict | None = None) -> dict:
    token = access_token()
    url = settings().zoho_api_base.rstrip("/") + path
    response = httpx.request(
        method, url, json=json, timeout=TIMEOUT,
        headers={"Authorization": f"Zoho-oauthtoken {token}",
                 "Accept": "application/json"},
    )
    if response.status_code == 401:
        # The cached token went stale early. One retry, then give up.
        response = httpx.request(
            method, url, json=json, timeout=TIMEOUT,
            headers={"Authorization": f"Zoho-oauthtoken {access_token(force=True)}",
                     "Accept": "application/json"},
        )

    payload = _json(response)
    status = payload.get("status") or {}
    if response.status_code >= 400 or status.get("code", 200) >= 400:
        raise ZohoError(f"HTTP {response.status_code}: "
                        f"{status.get('description') or payload}")
    return payload


def accounts() -> list[dict]:
    """The mailboxes this token can see. A read, safe to call anytime.

    This is what `cohandle zoho check` uses: it proves the credentials,
    the region and the account id without putting anything in anyone's
    inbox.
    """
    return _call("GET", "/api/accounts").get("data") or []


def own_addresses() -> list[str]:
    """Every address this account is allowed to send as, and receive at.

    Used to decide whether a message is going to the operator themselves,
    which is the one case where sending is allowed while unarmed.
    """
    addresses: list[str] = []
    for account in accounts():
        primary = account.get("primaryEmailAddress")
        if primary:
            addresses.append(primary.lower())
        for detail in account.get("sendMailDetails") or []:
            address = detail.get("fromAddress")
            if address:
                addresses.append(address.lower())
    return sorted(set(addresses))


def send(to: str, subject: str, body: str,
         schedule_at: str | None = None, timezone: str | None = None,
         to_self: bool = False) -> dict:
    """Send one plaintext email. Returns Zoho's data block.

    `schedule_at` is Zoho's own format, MM/DD/YYYY HH:MM:SS, and requires
    `timezone`. Handing scheduling to Zoho rather than holding the
    message here means a laptop that goes to sleep does not silently stop
    a send that was already decided.
    """
    if not settings().send_enabled:
        # One exception, and it is narrow on purpose. A message to an
        # address on your own Zoho account cannot reach anybody else, and
        # proving delivery before arming outreach is worth more than the
        # purity of a single rule. The caller asking is not enough: the
        # recipient is checked against the account.
        if not to_self:
            raise NotArmed("SEND_ENABLED is not true; refusing to send")
        if to.strip().lower() not in own_addresses():
            raise NotArmed(
                f"SEND_ENABLED is not true, and {to} is not one of your own "
                f"addresses, so this is not a test you can run unarmed")
    if not to or "@" not in to:
        raise ZohoError(f"refusing to send to {to!r}")
    _require("zoho_account_id", "zoho_from_address")

    cfg = settings()
    payload = {
        "fromAddress": cfg.zoho_from_address,
        "toAddress": to,
        "subject": subject,
        "content": body,
        "mailFormat": "plaintext",
        "askReceipt": "no",
    }
    if schedule_at:
        if not timezone:
            raise ZohoError("scheduling needs a timezone")
        payload |= {"isSchedule": True, "scheduleType": 6,
                    "scheduleTime": schedule_at, "timeZone": timezone}

    log.info("sending to %s%s", to, f" at {schedule_at}" if schedule_at else "")
    data = _call("POST", f"/api/accounts/{cfg.zoho_account_id}/messages",
                 json=payload).get("data") or {}
    return data
