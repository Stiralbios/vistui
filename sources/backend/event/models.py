import datetime as dt
import uuid

from backend.database import Base
from paradedb.sqlalchemy.indexing import ParadeDBField
from paradedb.sqlalchemy.vector import Vector
from sqlalchemy import DateTime, Float, ForeignKey, Index, Text, func
from sqlalchemy.orm import Mapped, mapped_column


class EventDO(Base):
    __tablename__ = "event"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat.id"), nullable=False)
    full_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(768), nullable=True)
    start_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    salience_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # When the text was last written: set at creation (default now()), bumped by every
    # later full_text write.
    edited_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())


# BM25 full-text index over the raw text (ParadeDB pg_search). It is appended after the
# class because ParadeDBField needs the mapped Table columns, not the class attributes.
# The key_field value is embedded in quotes because sqlalchemy-paradedb re-renders WITH
# options verbatim.
EventDO.__table__.append_constraint(
    Index(
        "event_bm25_idx",
        ParadeDBField(EventDO.__table__.c.id),
        ParadeDBField(EventDO.__table__.c.full_text),
        ParadeDBField(EventDO.__table__.c.chat_id),
        postgresql_using="bm25",
        postgresql_with={"key_field": "'id'"},
    )
)


class EventMessageLinkDO(Base):
    __tablename__ = "event_message_links"

    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("event.id"), primary_key=True)
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("message.id"), primary_key=True)
