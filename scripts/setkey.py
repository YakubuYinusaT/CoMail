"""Write a secret into .env without it being seen.

    python scripts/setkey.py OPENAI_API_KEY

The value is read with getpass, so it is not echoed to the terminal, not
kept in shell history, and never passed as an argument where `ps` or a
transcript could pick it up. The file it writes is gitignored.

Run with no arguments for the list of keys it knows about.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from getpass import getpass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"

KNOWN = {
    "OPENAI_API_KEY": "OpenAI key, used when LLM_PROVIDER=openai",
    "ANTHROPIC_API_KEY": "Anthropic key, used when LLM_PROVIDER=anthropic",
    "LLM_PROVIDER": "anthropic or openai (not secret, but set it here too)",
    "LLM_MODEL": "model override, e.g. gpt-5-mini (not secret)",
    "DATABASE_URL": "Neon connection string",
    "ZOHO_CLIENT_ID": "Zoho OAuth client id",
    "ZOHO_CLIENT_SECRET": "Zoho OAuth client secret",
    "ZOHO_REFRESH_TOKEN": "Zoho OAuth refresh token",
    "ZOHO_ACCOUNT_ID": "Zoho account id, from the accounts API",
    "ZOHO_FROM_ADDRESS": "the mailbox that sends",
    "INTERNAL_TOKEN": "shared secret between the API and the poller",
    "APP_PASSWORD": "shared password for the review queue; empty disables login",
    "SESSION_SECRET": "cookie signing key; empty derives it from APP_PASSWORD",
}
NOT_SECRET = {"LLM_PROVIDER", "LLM_MODEL", "ZOHO_FROM_ADDRESS"}


def ignored() -> bool:
    """Refuse to write a secret into a file git would track."""
    try:
        subprocess.run(
            ["git", "check-ignore", "-q", str(ENV)],
            cwd=ROOT, check=True, capture_output=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def upsert(key: str, value: str) -> str:
    lines = ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = f"{key}={value}"
            action = "updated"
            break
    else:
        lines.append(f"{key}={value}")
        action = "added"

    ENV.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    try:
        os.chmod(ENV, 0o600)      # no-op on Windows, correct on the server
    except OSError:
        pass
    return action


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        print("Keys:\n")
        for key, why in KNOWN.items():
            print(f"  {key:22} {why}")
        return 0

    key = argv[0].strip().upper()
    if key not in KNOWN:
        print(f"{key} is not one this project uses. Run with no arguments for the list.")
        return 2

    if not ignored():
        print("Refusing to write: .env is not ignored by git in this checkout.")
        print("Fix .gitignore first, or the secret ends up in a commit.")
        return 1

    if key in NOT_SECRET:
        value = input(f"{key}: ").strip()
    else:
        # On some terminals getpass cannot suppress echo and silently
        # falls back to a version that does not. Refuse rather than let a
        # key appear on screen, where it can be scrolled back to, copied
        # into a chat, or captured by a session recording.
        import getpass as _gp

        if _gp.getpass is _gp.fallback_getpass:
            print(
                "This terminal cannot hide typed input, so the key would be\n"
                "echoed on screen. Not asking for it here.\n\n"
                "Open .env in an editor and set the line directly:\n"
                f"    {ENV}\n"
                f"    {key}=<paste here>\n\n"
                "The file is gitignored. Nothing else needs to change."
            )
            return 1

        value = getpass(f"{key} (input hidden, nothing is echoed): ").strip()
        confirm = getpass("again, to catch a typo: ").strip()
        if value != confirm:
            print("The two entries differ. Nothing written.")
            return 1

    if not value:
        print("Empty. Nothing written.")
        return 1

    action = upsert(key, value)
    # Length and last four only: enough to tell a paste went wrong,
    # not enough to reconstruct anything.
    tail = value[-4:] if len(value) > 8 else "****"
    print(f"{action} {key} in .env  ({len(value)} chars, ending {tail})")
    print("Not printed in full anywhere, and .env is gitignored.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
