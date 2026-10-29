from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        # "Only once per category per month per level": the DB enforces it, so a re-processed
        # event or a second task just hits ON CONFLICT DO NOTHING.
        UniqueConstraint("user_id", "category_id", "month", "level", name="uq_notifications_once"),
        Index("ix_notifications_user_created", "user_id", "created_at"),  # GET /notifications
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"))
    month: Mapped[date] = mapped_column(Date)  # first day of the month
    level: Mapped[int] = mapped_column(Integer)  # 80 or 100 (percent of budget)
    message: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
