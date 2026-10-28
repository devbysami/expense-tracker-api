"""seed exchange rates

Revision ID: b7cba7866635
Revises: 292191776c44
Create Date: 2026-10-28 16:27:26.495707

"""
from typing import Sequence, Union

import math
from datetime import date, timedelta
from decimal import Decimal

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7cba7866635'
down_revision: Union[str, Sequence[str], None] = '292191776c44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Approximate USD values. AED is pegged to USD; PKR and EUR get a small deterministic wobble so
# the data looks like real daily rates. These are seed/demo values, not live market data.
BASE_RATES = {"USD": Decimal("1"), "AED": Decimal("0.27229"), "EUR": Decimal("1.08"), "PKR": Decimal("0.0035714")}
WOBBLE = {"USD": 0, "AED": 0, "EUR": 0.01, "PKR": 0.004}
DAYS = 90

exchange_rates = sa.table(
    "exchange_rates",
    sa.column("currency", sa.String),
    sa.column("valid_on", sa.Date),
    sa.column("rate_to_usd", sa.Numeric),
)


def upgrade() -> None:
    today = date.today()
    rows = []
    for offset in range(DAYS):
        day = today - timedelta(days=offset)
        for currency, base in BASE_RATES.items():
            factor = Decimal(str(1 + WOBBLE[currency] * math.sin(offset / 6)))
            rows.append({
                "currency": currency,
                "valid_on": day,
                "rate_to_usd": (base * factor).quantize(Decimal("0.00000001")),
            })
    op.bulk_insert(exchange_rates, rows)


def downgrade() -> None:
    op.execute("DELETE FROM exchange_rates")
