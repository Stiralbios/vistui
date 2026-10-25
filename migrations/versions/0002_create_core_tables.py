"""Create core memory tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-25

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from paradedb.sqlalchemy.vector import Vector
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
from sqlalchemy.dialects.postgresql import TSVECTOR

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "chatgroup",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=1024), nullable=True),
        sa.Column("config", PG_JSONB(), nullable=False),
        sa.Column("prompts", PG_JSONB(), nullable=False),
        sa.Column("ranking_formula", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "chat",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("chatgroup_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["chatgroup_id"], ["chatgroup.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "message",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("chat_id", sa.Uuid(), nullable=False),
        sa.Column("prev_message_id", sa.Uuid(), nullable=True),
        sa.Column("full_text", sa.Text(), nullable=False),
        sa.Column("search_vector", TSVECTOR(), nullable=True),
        sa.Column("embedding", Vector(768), nullable=True),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("salience_score", sa.Float(), nullable=True),
        sa.Column("processing_state", PG_JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["chat_id"], ["chat.id"]),
        sa.ForeignKeyConstraint(["prev_message_id"], ["message.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("chat_id", sa.Uuid(), nullable=False),
        sa.Column("full_text", sa.Text(), nullable=False),
        sa.Column("search_vector", TSVECTOR(), nullable=True),
        sa.Column("embedding", Vector(768), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("salience_score", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["chat_id"], ["chat.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "topic",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("chatgroup_id", sa.Uuid(), nullable=False),
        sa.Column("full_text", sa.Text(), nullable=False),
        sa.Column("search_vector", TSVECTOR(), nullable=True),
        sa.Column("embedding", Vector(768), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_edited_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("salience_score", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["chatgroup_id"], ["chatgroup.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "fact",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("chatgroup_id", sa.Uuid(), nullable=False),
        sa.Column("full_text", sa.Text(), nullable=False),
        sa.Column("search_vector", TSVECTOR(), nullable=True),
        sa.Column("embedding", Vector(768), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_edited_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("salience_score", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["chatgroup_id"], ["chatgroup.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "event_message_links",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["event.id"]),
        sa.ForeignKeyConstraint(["message_id"], ["message.id"]),
        sa.PrimaryKeyConstraint("event_id", "message_id"),
    )

    op.create_table(
        "topic_message_links",
        sa.Column("topic_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["topic_id"], ["topic.id"]),
        sa.ForeignKeyConstraint(["message_id"], ["message.id"]),
        sa.PrimaryKeyConstraint("topic_id", "message_id"),
    )

    op.create_table(
        "fact_message_links",
        sa.Column("fact_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["fact_id"], ["fact.id"]),
        sa.ForeignKeyConstraint(["message_id"], ["message.id"]),
        sa.PrimaryKeyConstraint("fact_id", "message_id"),
    )

    op.create_index("ix_chatgroup_user_id", "chatgroup", ["user_id"], unique=False)
    op.create_index("ix_chat_chatgroup_id", "chat", ["chatgroup_id"], unique=False)
    op.create_index("ix_message_chat_id", "message", ["chat_id"], unique=False)
    op.create_index("ix_event_chat_id", "event", ["chat_id"], unique=False)
    op.create_index("ix_topic_chatgroup_id", "topic", ["chatgroup_id"], unique=False)
    op.create_index("ix_fact_chatgroup_id", "fact", ["chatgroup_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_fact_chatgroup_id", table_name="fact")
    op.drop_index("ix_topic_chatgroup_id", table_name="topic")
    op.drop_index("ix_event_chat_id", table_name="event")
    op.drop_index("ix_message_chat_id", table_name="message")
    op.drop_index("ix_chat_chatgroup_id", table_name="chat")
    op.drop_index("ix_chatgroup_user_id", table_name="chatgroup")

    op.drop_table("fact_message_links")
    op.drop_table("topic_message_links")
    op.drop_table("event_message_links")
    op.drop_table("fact")
    op.drop_table("topic")
    op.drop_table("event")
    op.drop_table("message")
    op.drop_table("chat")
    op.drop_table("chatgroup")
    op.execute("DROP EXTENSION IF EXISTS vector")
