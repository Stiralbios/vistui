"""edited_at is set at creation and bumped on every text write; use_salience boolean

`edited_at` records when the text was last written: creation writes the text too, so
the column is NOT NULL with a default of now() on message, event, topic and fact
(event gains the column). Historical rows carry their creation time. The chatgroup
config's `use_salience` is a plain boolean: legacy JSON nulls normalize to false.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-21

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Content tables carrying the edited_at column. The event column is added first so
# the shared backfill covers all four in one pass.
_EDITED_AT_TABLES = ("event", "message", "topic", "fact")


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("event", sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True))
    for table in _EDITED_AT_TABLES:
        # Rows existing before the column existed were written at creation time and
        # (for message) rows edited later already carry their edit time.
        op.execute(f"UPDATE {table} SET edited_at = created_at WHERE edited_at IS NULL")
        op.alter_column(
            table,
            "edited_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        )
    # use_salience is a plain boolean (true/false, missing means the default true):
    # normalize legacy JSON nulls to an explicit opt-out.
    op.execute(
        "UPDATE chatgroup "
        "SET config = jsonb_set(config, '{use_salience}', 'false'::jsonb) "
        "WHERE config ? 'use_salience' AND config->>'use_salience' IS NULL"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column(
        "message", "edited_at", existing_type=sa.DateTime(timezone=True), nullable=True, server_default=None
    )
    # Rows never rewritten after creation carry edited_at = created_at.
    op.execute("UPDATE message SET edited_at = NULL WHERE edited_at = created_at")
    op.alter_column("topic", "edited_at", existing_type=sa.DateTime(timezone=True), nullable=False, server_default=None)
    op.alter_column("fact", "edited_at", existing_type=sa.DateTime(timezone=True), nullable=False, server_default=None)
    op.drop_column("event", "edited_at")
    # The use_salience normalization is not reversible: a normalized null and an
    # explicit false are indistinguishable afterwards.
