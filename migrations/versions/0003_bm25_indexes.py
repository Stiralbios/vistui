"""Swap search_vector for ParadeDB BM25 indexes

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-15

"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Chat-scoped tables index chat_id, chatgroup-scoped tables index chatgroup_id.
# The tsvector columns were never populated; pg_search indexes the raw text directly.
_TABLES = {
    "message": "chat_id",
    "event": "chat_id",
    "topic": "chatgroup_id",
    "fact": "chatgroup_id",
}


def upgrade() -> None:
    """Upgrade schema."""
    for table, _scope_column in _TABLES.items():
        op.execute(f"DROP INDEX IF EXISTS {table}_search_idx")
        op.drop_column(table, "search_vector")
        # key_field is required by pg_search; it marks the row-identity primary key.
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {table}_bm25_idx ON {table} "
            f"USING bm25 (id, full_text, {_TABLES[table]}) WITH (key_field = 'id')"
        )


def downgrade() -> None:
    """Downgrade schema."""
    for table, _scope_column in _TABLES.items():
        op.execute(f"DROP INDEX IF EXISTS {table}_bm25_idx")
        op.add_column(table, TSVECTOR(name="search_vector", nullable=True))
