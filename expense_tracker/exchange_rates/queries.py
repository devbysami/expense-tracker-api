"""
Currency conversion as SQLAlchemy expressions. Shared by the monthly report, GET /categories
and the budget-alert check, so the conversion rule lives in ONE place.

Rule: use the rate for the expense date; if that day is missing, the latest rate before it.
rate_to_usd = how many USD one unit is worth, so: amount_in_base = amount * rate(cur) / rate(base).
Expenses already in the base currency skip the lookup. An expense with no rate at all on or before
its date gets NULL (SUM ignores it) instead of a wrong number.
"""
from sqlalchemy import case, select

from exchange_rates.models import ExchangeRate
from expenses.models import Expense


def latest_rate(currency):
    """The latest rate for `currency` on or before the expense's date 
    Served by the exchange_rates PK (currency, valid_on), scanned backwards.."""
    return (
        select(ExchangeRate.rate_to_usd)
        .where(ExchangeRate.currency == currency, ExchangeRate.valid_on <= Expense.spent_on)
        .order_by(ExchangeRate.valid_on.desc())
        .limit(1)
        .scalar_subquery()
    )


def amount_in_base(base_currency: str):
    """Expense.amount converted to `base_currency`, as a SQL expression to use inside select()/func.sum().
    NULL when a rate is missing, so SUM skips it instead of adding a wrong number."""
    
    # case() is the SQL if/else: Postgres checks it for EVERY row. A Python `if` can't be used here,
    # because Expense.currency is the column, not a row's value (the `if` would always be False).
    return case(
        (Expense.currency == base_currency, Expense.amount),
        else_=Expense.amount * latest_rate(Expense.currency) / latest_rate(base_currency),
    )

