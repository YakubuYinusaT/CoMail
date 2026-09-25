"""Turning a written email into HTML, and no further.

Zoho does not apply the signature configured in its web client to mail
created through the API. That held for sending, and a draft opened in the
web composer showed it holds there too. So the designed signature has to
travel with the message, which means the message has to be HTML.

What this deliberately does not do is make the email look designed. The
body becomes paragraphs and nothing else: no fonts, no colours, no
tables, no tracking. A first approach to an investor that arrives styled
reads as a campaign, and the point of this tool is that it does not read
as one. The only designed thing in the message is the signature at the
bottom, which is where a designed thing belongs.

Plain text is still generated alongside, because a recipient whose client
blocks HTML should get the letter, not an empty window.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Written email is plain text with blank lines between paragraphs, which
# is how the model is told to write and how the operator edits it.
PARAGRAPH_BREAK = re.compile(r"\n\s*\n")

# A body kept at the width of a readable column. Anything wider is harder
# to read on a laptop and scrolls sideways on a phone.
WRAPPER = (
    '<div style="font-family:-apple-system,\'Segoe UI\',Arial,Helvetica,'
    'sans-serif;font-size:15px;line-height:1.55;color:#151515;'
    'max-width:600px;">'
)


def body_to_html(text: str) -> str:
    """Paragraphs, and single newlines kept as line breaks.

    Escaped first. The body is written by a model and edited by a person,
    and an ampersand or an angle bracket in either should show up as
    itself rather than breaking the message.
    """
    paragraphs = []
    for block in PARAGRAPH_BREAK.split((text or "").strip()):
        if not block.strip():
            continue
        escaped = html.escape(block.strip()).replace("\n", "<br>")
        paragraphs.append(f'<p style="margin:0 0 16px;">{escaped}</p>')
    return "\n".join(paragraphs)


def compose_html(body: str, signature_html: str = "") -> str:
    """The email as HTML: the letter, then the signature block."""
    parts = [WRAPPER, body_to_html(body)]
    if signature_html.strip():
        parts.append('<div style="margin-top:24px;">')
        parts.append(signature_html.strip())
        parts.append("</div>")
    parts.append("</div>")
    return "\n".join(parts)


def load_signature_html(path: str | Path | None = None) -> str:
    """Read the designed signature from the brand assets folder.

    Kept as a file read rather than pasted into the database, so changing
    the signature is changing the one file the brand folder already calls
    the source of truth.
    """
    if path is None:
        path = ROOT.parent / "Brand Assets" / "Email Signature" / "signature.html"
    file = Path(path)
    if not file.exists():
        return ""
    text = file.read_text(encoding="utf-8")
    # The file opens with an explanatory comment for whoever pastes it
    # into Zoho by hand. It does not belong in a sent message.
    return re.sub(r"^\s*<!--.*?-->\s*", "", text, count=1, flags=re.S).strip()
