"""Add message.edited_at, rename topic/fact last_edited_at to edited_at

`message.edited_at` records when the user actually replaced a message's content
(nullable: never-edited messages keep NULL; batch writes never touch it). Topic and
Fact renames align the three content types on one name — an LLM rewrite of a topic or
fact is a content edit, so the pipelines keep bumping the column.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-21

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("message", sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column("topic", "last_edited_at", new_column_name="edited_at")
    op.alter_column("fact", "last_edited_at", new_column_name="edited_at")


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column("topic", "edited_at", new_column_name="last_edited_at")
    op.alter_column("fact", "edited_at", new_column_name="last_edited_at")
    op.drop_column("message", "edited_at")
