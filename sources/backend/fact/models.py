import datetime as dt
import uuid
from typing import Any

from backend.database import Base
from pgvector.sqlalchemy import VECTOR
from sqlalchemy import DateTime, Float, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column


class FactDO(Base):
    __tablename__ = "fact"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    chatgroup_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chatgroup.id"), nullable=False)
    full_text: Mapped[str] = mapped_column(Text, nullable=False)
    search_vector: Mapped[Any] = mapped_column(TSVECTOR, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(VECTOR(768), nullable=True)
    start_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_edited_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    salience_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())


class FactMessageLinkDO(Base):
    __tablename__ = "fact_message_links"

    fact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fact.id"), primary_key=True)
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("message.id"), primary_key=True)
