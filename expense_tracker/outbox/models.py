from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class OutboxEvent(Base):
    """Transactional outbox: written in the SAME transaction as the change that caused it,
    so an event exists if and only if the change was committed. The worker processes it later."""
    __tablename__ = "outbox_events"
    __table_args__ = (
        # The worker only ever looks at unprocessed rows, oldest first. Partial index = stays tiny.
        Index("ix_outbox_events_pending", "id", postgresql_where=text("processed_at IS NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
