"""target brief

Why you are writing to a fund is not something Cohandle can read off
their website. A fund that publishes a form is often worth an email
first, and the email is a different email depending on the reason: are
you asking whether they fund pre-revenue companies in Nigeria before
spending an hour on their form, or are you introducing the round.

The brief is that reason, in the operator's own words, given when the
target is added and used when the email is written.

Revision ID: f1c6d90a4e37
Revises: e5a9c31d7b28
Create Date: 2026-09-26

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f1c6d90a4e37"
down_revision: Union[str, Sequence[str], None] = "e5a9c31d7b28"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("target", sa.Column("brief", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("target", "brief")
