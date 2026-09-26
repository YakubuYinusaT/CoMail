"""hook type none, for real this time

Revision 1d68501ec352 is called "allow hook_type none" and its upgrade is
`pass`. Alembic does not autogenerate changes to check constraints, so
the stub was created, never filled in, and the model and the database
have disagreed ever since.

It matters. An email written for a target with no usable hooks is stored
with hook_type "none", which the model permits and the database refuses.
Microtraction, which was read successfully but yielded no quotable
facts, would have failed on insert the moment somebody pressed Draft.
Found while testing follow-ups, which are always hook_type "none"
because a second email opens on the first one rather than on research.

Revision ID: e5a9c31d7b28
Revises: d8f2b6c4a105
Create Date: 2026-09-26

"""
from typing import Sequence, Union

from alembic import op

revision: str = "e5a9c31d7b28"
down_revision: Union[str, Sequence[str], None] = "d8f2b6c4a105"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

HOOKS = ("portfolio_adjacent", "thesis_quote", "recent_cluster",
         "publication", "fund_event")


def _values(names: Sequence[str]) -> str:
    return ", ".join(f"'{n}'" for n in names)


def upgrade() -> None:
    op.drop_constraint("ck_hook_type", "draft", type_="check")
    op.create_check_constraint(
        "ck_hook_type", "draft",
        f"hook_type IN ({_values((*HOOKS, 'none'))})")


def downgrade() -> None:
    op.execute("UPDATE draft SET hook_type = 'thesis_quote' "
               "WHERE hook_type = 'none'")
    op.drop_constraint("ck_hook_type", "draft", type_="check")
    op.create_check_constraint(
        "ck_hook_type", "draft", f"hook_type IN ({_values(HOOKS)})")
