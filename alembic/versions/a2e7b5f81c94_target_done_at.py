"""target done_at

The Add screen is the dashboard, and a target you have finished with
still sits on it. Four funds want forms filled in; once one is submitted
there is nothing more Cohandle can tell you about it, and leaving it
between the ones still needing work is how the ones still needing work
get missed.

A timestamp rather than a flag, so the board can say when rather than
only whether.

Revision ID: a2e7b5f81c94
Revises: f1c6d90a4e37
Create Date: 2026-09-26

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a2e7b5f81c94"
down_revision: Union[str, Sequence[str], None] = "f1c6d90a4e37"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("target", sa.Column(
        "done_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_target_done_at", "target", ["done_at"])


def downgrade() -> None:
    op.drop_index("ix_target_done_at", table_name="target")
    op.drop_column("target", "done_at")
