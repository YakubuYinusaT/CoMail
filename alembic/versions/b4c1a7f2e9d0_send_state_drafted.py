"""send state: drafted

Cohandle can hand an email to Zoho's Drafts folder instead of delivering
it, so the operator sends it themselves from the web client and gets
their designed signature and formatting.

That outcome is neither "queued" nor "sent". Recording it as "sent" would
put a claim in the database that nothing supports: nobody received
anything, and the message may sit in Drafts forever. It gets its own
state so the row keeps telling the truth.

Revision ID: b4c1a7f2e9d0
Revises: a1efeb55b652
Create Date: 2026-09-25

"""
from typing import Sequence, Union

from alembic import op

revision: str = "b4c1a7f2e9d0"
down_revision: Union[str, Sequence[str], None] = "a1efeb55b652"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STATES_AFTER = ("queued", "drafted", "sent", "bounced", "failed", "cancelled")
STATES_BEFORE = ("queued", "sent", "bounced", "failed", "cancelled")


def _values(states: Sequence[str]) -> str:
    return ", ".join(f"'{s}'" for s in states)


def upgrade() -> None:
    op.drop_constraint("ck_state", "send", type_="check")
    op.create_check_constraint(
        "ck_state", "send", f"state IN ({_values(STATES_AFTER)})")


def downgrade() -> None:
    # Anything already handed to Drafts would violate the old constraint,
    # and silently deleting those rows would lose the record of work that
    # was actually done. Park them as cancelled, which is the closest
    # honest reading: Cohandle did not deliver them.
    op.execute("UPDATE send SET state = 'cancelled' WHERE state = 'drafted'")
    op.drop_constraint("ck_state", "send", type_="check")
    op.create_check_constraint(
        "ck_state", "send", f"state IN ({_values(STATES_BEFORE)})")
