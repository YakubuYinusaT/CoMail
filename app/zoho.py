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


class NeedsWiderScope(ZohoError):
    """The token can send but not read.

    Raised so a caller can tell a missing permission from a broken
    mailbox. The remedy is a new token, not a retry, and the message
    should say so rather than leaving somebody refreshing a page.
    """


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
    """Zoho's body as a dict, whatever shape it arrived in.

    Errors on some endpoints come back as a list, `[2, {...}]`, rather
    than the documented object. Assuming a dict turned a clear message
    like INVALID_OAUTHSCOPE into an AttributeError several layers away
    from the cause.
    """
    try:
        payload = response.json()
    except ValueError:
        raise ZohoError(f"HTTP {response.status_code}, and the body was not "
                        f"JSON: {response.text[:300]}") from None

    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                code = item.get("errorCode") or item.get("status")
                message = item.get("msg") or item.get("message") or item
                if str(code) == "INVALID_OAUTHSCOPE":
                    raise NeedsWiderScope(
                        "this Zoho token may send but not read. Re-run "
                        "zoho-setup.cmd to make one that can.")
                raise ZohoError(f"{code}: {message}" if code else str(message))
    raise ZohoError(f"HTTP {response.status_code}, unexpected body: "
                    f"{str(payload)[:300]}")


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


def folders() -> list[dict]:
    """Every folder in the mailbox. Read only, and needs folders.READ."""
    return _call("GET", f"/api/accounts/{settings().zoho_account_id}/folders").get("data") or []


def sent_folder_id() -> str | None:
    """The Sent folder's id, whatever the account calls it.

    Zoho marks system folders with a folderType, which survives renaming
    and translation. The name is only a fallback.
    """
    found = folders()
    for folder in found:
        if str(folder.get("folderType", "")).lower() == "sent":
            return str(folder.get("folderId"))
    for folder in found:
        if str(folder.get("folderName", "")).strip().lower() in ("sent", "sent items"):
            return str(folder.get("folderId"))
    return None


def messages(folder_id: str, limit: int = 50, start: int = 1) -> list[dict]:
    """Message headers from one folder, newest first.

    Headers only. Cohandle never needs the body of something you sent,
    and asking for less is one fewer thing to store by accident.
    """
    path = (f"/api/accounts/{settings().zoho_account_id}/messages/view"
            f"?folderId={folder_id}&limit={int(limit)}&start={int(start)}")
    return _call("GET", path).get("data") or []


def create_draft(to: str, subject: str, body: str, html: bool = False) -> dict:
    """Leave the email in Zoho's Drafts folder instead of delivering it.

    This does not check SEND_ENABLED, because a draft reaches nobody.
    That is the whole point of the mode: Cohandle does the research and
    the writing, and a person presses send in Zoho, where their designed
    signature and any last edit are applied.

    The response is checked rather than trusted. If Zoho ever stops
    honouring `mode`, the difference between a draft and a delivered
    email is the difference between a private note and a cold email an
    investor has already read, so a reply that does not say "draft" is
    treated as a failure loud enough to notice.
    """
    if not to or "@" not in to:
        raise ZohoError(f"refusing to draft to {to!r}")
    _require("zoho_account_id", "zoho_from_address")

    cfg = settings()
    log.info("drafting to %s", to)
    data = _call("POST", f"/api/accounts/{cfg.zoho_account_id}/messages", json={
        "fromAddress": cfg.zoho_from_address,
        "toAddress": to,
        "subject": subject,
        "content": body,
        "mailFormat": "html" if html else "plaintext",
        "askReceipt": "no",
        "mode": "draft",
    }).get("data") or {}

    if str(data.get("mode", "")).lower() != "draft":
        raise ZohoError(
            "Zoho did not confirm draft mode, so this message may have been "
            f"delivered rather than saved. It answered: {data!r}")
    return data


def send(to: str, subject: str, body: str,
         schedule_at: str | None = None, timezone: str | None = None,
         to_self: bool = False, mode: str | None = None,
         html: bool = False) -> dict:
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
        "mailFormat": "html" if html else "plaintext",
        "askReceipt": "no",
    }
    if mode:
        # "draft" leaves the message in the Drafts folder instead of
        # sending it, which is the difference between Cohandle delivering
        # and Cohandle handing you something to send yourself.
        payload["mode"] = mode
    if schedule_at:
        if not timezone:
            raise ZohoError("scheduling needs a timezone")
        payload |= {"isSchedule": True, "scheduleType": 6,
                    "scheduleTime": schedule_at, "timeZone": timezone}

    log.info("sending to %s%s", to, f" at {schedule_at}" if schedule_at else "")
    data = _call("POST", f"/api/accounts/{cfg.zoho_account_id}/messages",
                 json=payload).get("data") or {}
    return data
