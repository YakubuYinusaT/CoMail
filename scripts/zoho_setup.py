"""Set Zoho up in one sitting.

Double-click zoho-setup.cmd in the CoHandle folder. It runs from its own
folder, so it works wherever you start it. Running this file by hand only
works if you are already inside the project:

    cd "C:/Users/USER/Documents/Connected Intelligence/CoHandle"
    python scripts/zoho_setup.py

Zoho needs four things from you and works out a fifth for itself. This
walks through them in order and writes each one into .env as it goes, so
nothing is typed twice and nothing is pasted into a chat window.

What you do first, in a browser, before running this:

  1. Go to api-console.zoho.com and sign in as the account that owns the
     mailbox you want to send from.
  2. Choose "Self Client". That is the one for a program with no login
     screen of its own, which is what Cohandle is. Create it.
  3. Open the "Generate Code" tab. In the scope box paste exactly:

         ZohoMail.messages.CREATE,ZohoMail.accounts.READ

     Give it a description, pick the longest duration offered, and
     generate. Zoho shows you a short code.

The code expires in a few minutes, so run this straight afterwards. It
trades the code for a refresh token, which does not expire, and that is
the thing Cohandle actually stores.

Nothing here is echoed to the terminal and nothing is passed as a command
line argument, where it would sit in your shell history.
"""

from __future__ import annotations

import sys
from getpass import getpass
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# scripts/ is not a package, so setkey is loaded by path rather than
# imported. Reusing it matters: it is the code that refuses to write when
# .env is not gitignored.
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "setkey", Path(__file__).resolve().parent / "setkey.py")
_setkey = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_setkey)
ignored, upsert = _setkey.ignored, _setkey.upsert

SCOPE = "ZohoMail.messages.CREATE,ZohoMail.accounts.READ"

REGIONS = {
    "1": ("com", "United States and most of the world, mail.zoho.com"),
    "2": ("eu", "Europe, mail.zoho.eu"),
    "3": ("in", "India, mail.zoho.in"),
    "4": ("com.au", "Australia, mail.zoho.com.au"),
}


def ask(label: str, secret: bool = True) -> str:
    value = (getpass(f"{label}: ") if secret else input(f"{label}: ")).strip()
    if not value:
        print("Nothing entered. Stopping so you can start again.")
        raise SystemExit(1)
    return value


def pick_region() -> str:
    print("\nWhich Zoho region is your account in? If you are not sure, look at")
    print("the address bar when you are signed in to Zoho Mail.\n")
    for key, (_, description) in REGIONS.items():
        print(f"  {key}. {description}")
    choice = input("\nNumber [1]: ").strip() or "1"
    if choice not in REGIONS:
        print("That was not one of the options.")
        raise SystemExit(1)
    return REGIONS[choice][0]


def exchange(region: str, client_id: str, client_secret: str, code: str) -> str:
    """Trade the short-lived code for a refresh token that does not expire."""
    response = httpx.post(
        f"https://accounts.zoho.{region}/oauth/v2/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
        },
        timeout=30.0,
    )
    try:
        payload = response.json()
    except ValueError:
        print(f"\nZoho replied with something that was not JSON:\n{response.text[:400]}")
        raise SystemExit(1) from None

    if "refresh_token" not in payload:
        print(f"\nZoho would not give a refresh token. It said: "
              f"{payload.get('error', payload)}")
        if payload.get("error") == "invalid_code":
            print("\nThat usually means the code had already expired. Generate a")
            print("fresh one in the API console and run this again straight away.")
        raise SystemExit(1)
    return payload["refresh_token"]


def main() -> int:
    if not ignored():
        print("Refusing to write: .env is not ignored by git in this checkout.")
        print("Fix .gitignore first, or these end up in a commit.")
        return 1

    print(__doc__)
    if input("Have you generated the code in the API console? [y/N]: ").strip().lower() \
            not in ("y", "yes"):
        print("Do that first, then run this again.")
        return 0

    region = pick_region()
    print("\nThe next three are hidden as you type or paste them.\n")
    client_id = ask("Client ID")
    client_secret = ask("Client Secret")
    code = ask("Generated code")

    print("\nTrading the code for a refresh token...")
    refresh = exchange(region, client_id, client_secret, code)
    print("Zoho accepted it.\n")

    upsert("ZOHO_API_BASE", f"https://mail.zoho.{region}")
    upsert("ZOHO_CLIENT_ID", client_id)
    upsert("ZOHO_CLIENT_SECRET", client_secret)
    upsert("ZOHO_REFRESH_TOKEN", refresh)
    print("Written to .env: ZOHO_API_BASE, ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET,")
    print("ZOHO_REFRESH_TOKEN.\n")

    sender = input("Which address will the emails come from? ").strip()
    if sender:
        upsert("ZOHO_FROM_ADDRESS", sender)
        print("Written to .env: ZOHO_FROM_ADDRESS.\n")

    # The account id is the one value you should not have to hunt for.
    print("Asking Zoho which mailboxes this token can see...")
    from app import zoho

    try:
        found = zoho.accounts()
    except Exception as exc:  # noqa: BLE001 - this is the message that matters
        print(f"\nThe token works but the mailbox list failed: {exc}")
        print("\nIf this says the API is not available on your plan, that is the")
        print("free plan limit. Nothing else you do here will get around it.")
        return 1

    if not found:
        print("Zoho returned no mailboxes for this token.")
        return 1

    for account in found:
        print(f"  {account.get('accountId')}  {account.get('primaryEmailAddress')}")

    if len(found) == 1:
        upsert("ZOHO_ACCOUNT_ID", str(found[0].get("accountId")))
        print("\nWritten to .env: ZOHO_ACCOUNT_ID.")
    else:
        chosen = input("\nWhich account id? ").strip()
        if chosen:
            upsert("ZOHO_ACCOUNT_ID", chosen)
            print("Written to .env: ZOHO_ACCOUNT_ID.")

    print("\nZoho is set up. Nothing will send yet, because SEND_ENABLED is")
    print("still false. Check it over with:\n")
    print("    python -m app.cli zoho\n")
    print("and when you are ready to let email actually leave, set")
    print("SEND_ENABLED=true in .env and restart Cohandle.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
