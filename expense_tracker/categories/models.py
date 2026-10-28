from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class Category(Base):
    __tablename__ = "categories"
    # Same name twice for one user -> IntegrityError -> 409. Also serves lookups by (user_id, name),
    # which the CSV import needs.
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_categories_user_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    monthly_budget: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))  # in user's base currency
