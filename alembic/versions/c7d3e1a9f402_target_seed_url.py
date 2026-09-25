"""target seed_url

Ingest keys on domain, which is right for deduplication and wrong for
crawling. Pasting https://www.ventra.global/ventra-capital produced a
target for ventra.global and then crawled the homepage, the about page
and the contact page, none of which were the capital arm. The one page
that was named explicitly is the one page that was never fetched.

The pasted address is kept so the crawl can start from it.

Revision ID: c7d3e1a9f402
Revises: b4c1a7f2e9d0
Create Date: 2026-09-25

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c7d3e1a9f402"
down_revision: Union[str, Sequence[str], None] = "b4c1a7f2e9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("target", sa.Column("seed_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("target", "seed_url")
