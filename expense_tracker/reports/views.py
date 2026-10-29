from datetime import date
from typing import Annotated

from fastapi import Query
from sqlalchemy import Date, and_, case, cast, column, func, select, true, values

from categories.models import Category
from core.dates import next_month, previous_month
from core.dependencies import CurrentUser, DBSession
from exchange_rates.queries import amount_in_base
from expenses.models import Expense
from reports.schemas import CategoryReportRow, MonthlyReportOut


def monthly_report_query(user_id: int, base_currency: str, start: date):
    """ONE query: total per category for the requested AND the previous month (every expense
    converted to the base currency), then LAG() puts last month's total next to this month's."""
    prev_start = previous_month(start)

    # The two months as a tiny inline table, so a category gets a row (total 0) even in a month with no spend.
    months = values(column("month", Date), name="months").data([(prev_start,), (start,)])

    monthly = (
        select(
            Category.id.label("category_id"),
            Category.name.label("category_name"),
            Category.monthly_budget.label("budget"),
            months.c.month,
            func.coalesce(func.sum(amount_in_base(base_currency)), 0).label("total"),
        )
        .select_from(Category)
        .join(months, true())  # CROSS JOIN: every category x both months
        .outerjoin(Expense, and_(
            Expense.category_id == Category.id,
            Expense.spent_on >= prev_start,  # both months (uses the index)...
            Expense.spent_on < next_month(start),
            cast(func.date_trunc("month", Expense.spent_on), Date) == months.c.month,  # ...this row's month
        ))
        .where(Category.user_id == user_id)
        .group_by(Category.id, months.c.month)
        .subquery("monthly")
    )

    previous_total = func.lag(monthly.c.total).over(partition_by=monthly.c.category_id, order_by=monthly.c.month)
    compared = select(monthly, previous_total.label("previous_total")).subquery("compared")
    c = compared.c

    return (
        select(
            c.category_id,
            c.category_name,
            c.budget,
            func.round(c.total, 2).label("total"),
            case((c.budget > 0, func.round(c.total * 100 / c.budget, 2))).label("percent_used"),
            func.round(c.previous_total, 2).label("previous_month_total"),
            func.round(c.total - c.previous_total, 2).label("change_from_previous"),
            case((c.previous_total > 0, func.round((c.total - c.previous_total) * 100 / c.previous_total, 2)))
            .label("change_percent"),
        )
        .where(c.month == start)
        .order_by(c.category_name)
    )


async def monthly_report(
    year: Annotated[int, Query(ge=2000, le=2100)],
    month: Annotated[int, Query(ge=1, le=12)],
    user: CurrentUser,
    db: DBSession,
) -> MonthlyReportOut:
    
    rows = await db.execute(monthly_report_query(user.id, user.base_currency, date(year, month, 1)))
    
    return MonthlyReportOut(
        year=year,
        month=month,
        base_currency=user.base_currency,
        categories=[CategoryReportRow.model_validate(r, from_attributes=True) for r in rows],
    )
