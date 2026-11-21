"""Drop the redundant embedding/salience keys from message.processing_state

The NULL embedding / salience_score columns are the queue markers (same semantics as
memory objects), so the two JSONB keys are redundant. This migration strips the keys
from existing rows; the remaining keys (event, topic, fact, retry_count,
last_failure_at) are untouched.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-21

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("UPDATE message SET processing_state = processing_state - 'embedding' - 'salience'")


def downgrade() -> None:
    """Downgrade schema: re-derive both keys from the columns."""
    op.execute(
        """
        UPDATE message SET processing_state = jsonb_set(
            jsonb_set(
                processing_state,
                '{embedding}',
                to_jsonb(CASE WHEN embedding IS NULL THEN 'waiting' ELSE 'done' END)
            ),
            '{salience}',
            to_jsonb(CASE WHEN salience_score IS NULL THEN 'waiting' ELSE 'done' END)
        )
        """
    )
