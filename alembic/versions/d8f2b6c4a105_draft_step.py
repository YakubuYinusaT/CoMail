"""draft step

A follow-up is not a different kind of object. It is another email to the
same fund in the same campaign, and it wants exactly what the first one
gets: the review screen, the claims check, the house style check, and
the same trip into Zoho Drafts. So it is a draft with a step number
rather than a table of its own.

The unique constraint was on target and campaign, which allowed exactly
one email per fund forever. It now includes the step.

Revision ID: d8f2b6c4a105
Revises: c7d3e1a9f402
Create Date: 2026-09-26

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d8f2b6c4a105"
down_revision: Union[str, Sequence[str], None] = "c7d3e1a9f402"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("draft", sa.Column(
        "step", sa.Integer(), nullable=False, server_default="1"))
    op.drop_constraint("uq_draft_target_campaign", "draft", type_="unique")
    op.create_unique_constraint(
        "uq_draft_target_campaign_step", "draft",
        ["target_id", "campaign_id", "step"])
    op.create_check_constraint("ck_step", "draft", "step >= 1")


def downgrade() -> None:
    # Follow-ups would violate the old constraint. Deleting them loses
    # work that was reviewed and possibly sent, so the downgrade refuses
    # rather than quietly destroying it.
    count = op.get_bind().execute(
        sa.text("SELECT count(*) FROM draft WHERE step > 1")).scalar()
    if count:
        raise RuntimeError(
            f"{count} follow-up draft(s) exist. Deleting them would lose "
            "reviewed and possibly sent work; remove them deliberately first.")
    op.drop_constraint("ck_step", "draft", type_="check")
    op.drop_constraint("uq_draft_target_campaign_step", "draft", type_="unique")
    op.create_unique_constraint(
        "uq_draft_target_campaign", "draft", ["target_id", "campaign_id"])
    op.drop_column("draft", "step")
