import datetime as dt
import uuid

from backend.database import Base
from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column


class ChatDO(Base):
    __tablename__ = "chat"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    chatgroup_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chatgroup.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())
