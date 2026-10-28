from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class Expense(Base):
    __tablename__ = "expenses"
    __table_args__ = (
        # Serves GET /expenses: WHERE user_id = ? ORDER BY spent_on DESC, id DESC + keyset cursor.
        Index("ix_expenses_user_spent_on_id", "user_id", "spent_on", "id"),
        # Serves the monthly report / category spend: WHERE user_id = ? AND category_id = ? AND spent_on in month.
        Index("ix_expenses_user_category_spent_on", "user_id", "category_id", "spent_on"),
        # CSV import dedup: the same file row can only be inserted once per user (ON CONFLICT DO NOTHING).
        # Partial, so normal expenses (import_key NULL) don't take space in it.
        Index(
            "uq_expenses_user_import_key", "user_id", "import_key",
            unique=True, postgresql_where=text("import_key IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="RESTRICT"))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))  # NUMERIC, never float
    currency: Mapped[str] = mapped_column(String(3))
    description: Mapped[str] = mapped_column(String(500), default="")
    spent_on: Mapped[date] = mapped_column(Date)
    receipt_key: Mapped[str | None] = mapped_column(String(255))
    # "<sha256 of the CSV file>:<row number>" for imported rows, NULL otherwise.
    import_key: Mapped[str | None] = mapped_column(String(80))
    # Optimistic locking: bumped on every update, exposed as the ETag.
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class IdempotencyKey(Base):
    """One row per (user, Idempotency-Key) for POST /expenses. Stores the first response so a retry gets it back.
    The UNIQUE constraint is what makes two parallel requests with the same key safe (see create_expense)."""
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_idempotency_keys_user_key"),
        Index("ix_idempotency_keys_created_at", "created_at"),  # the worker's "delete keys older than 24h"
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(255))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
