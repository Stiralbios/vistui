import datetime as dt
import uuid
from typing import Any

from backend.database import Base
from pgvector.sqlalchemy import VECTOR
from sqlalchemy import DateTime, Float, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column


class MessageDO(Base):
    __tablename__ = "message"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat.id"), nullable=False)
    prev_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("message.id"), nullable=True)
    full_text: Mapped[str] = mapped_column(Text, nullable=False)
    search_vector: Mapped[Any] = mapped_column(TSVECTOR, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(VECTOR(768), nullable=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    salience_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    processing_state: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())
