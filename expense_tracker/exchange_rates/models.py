from datetime import date
from decimal import Decimal

from sqlalchemy import Date, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class ExchangeRate(Base):
    """1 unit of `currency` = rate_to_usd USD. One row per currency per day.
    The composite PK (currency, valid_on) is also the index the report needs to find
    "latest rate on or before date X" for a currency."""
    __tablename__ = "exchange_rates"

    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    valid_on: Mapped[date] = mapped_column(Date, primary_key=True)
    rate_to_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8))
