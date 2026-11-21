import datetime as dt
import uuid

from backend.database import Base
from paradedb.sqlalchemy.indexing import ParadeDBField
from paradedb.sqlalchemy.vector import Vector
from sqlalchemy import DateTime, Float, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class MessageDO(Base):
    __tablename__ = "message"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat.id"), nullable=False)
    prev_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("message.id"), nullable=True)
    full_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(768), nullable=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    salience_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    processing_state: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # When the text was last written: set at creation (default now()), bumped by every
    # later full_text write. Batch stages never rewrite content, so it survives
    # embedding/salience/consolidation rounds untouched.
    edited_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())


# BM25 full-text index over the raw text (ParadeDB pg_search). It is appended after the
# class because ParadeDBField needs the mapped Table columns, not the class attributes.
# The key_field value is embedded in quotes because sqlalchemy-paradedb re-renders WITH
# options verbatim.
MessageDO.__table__.append_constraint(
    Index(
        "message_bm25_idx",
        ParadeDBField(MessageDO.__table__.c.id),
        ParadeDBField(MessageDO.__table__.c.full_text),
        ParadeDBField(MessageDO.__table__.c.chat_id),
        postgresql_using="bm25",
        postgresql_with={"key_field": "'id'"},
    )
)
