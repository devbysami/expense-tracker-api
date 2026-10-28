from datetime import date
from typing import Annotated

from fastapi import Query
from sqlalchemy import text

from core.dates import next_month, previous_month
from core.dependencies import CurrentUser, DBSession
from exchange_rates.queries import CONVERTED_EXPENSES
from reports.schemas import CategoryReportRow, MonthlyReportOut

# ONE query: convert (CTE + LATERAL rate lookups) -> total per category per month (JOIN + GROUP BY)
# for the requested AND the previous month -> LAG() gives last month's total next to this month's.
MONTHLY_REPORT = text(f"""
WITH converted AS ({CONVERTED_EXPENSES}),
months AS (
    SELECT CAST(:prev_start AS date) AS month
    UNION ALL
    SELECT CAST(:month_start AS date)
),
monthly AS (
    SELECT c.id AS category_id, c.name AS category_name, c.monthly_budget AS budget, m.month,
           COALESCE(SUM(cv.amount_base), 0) AS total
    FROM categories c
    CROSS JOIN months m
    LEFT JOIN converted cv ON cv.category_id = c.id AND cv.month = m.month
    WHERE c.user_id = :user_id
    GROUP BY c.id, c.name, c.monthly_budget, m.month
),
compared AS (
    SELECT *, LAG(total) OVER (PARTITION BY category_id ORDER BY month) AS previous_total
    FROM monthly
)
SELECT
    category_id,
    category_name,
    budget,
    ROUND(total, 2) AS total,
    CASE WHEN budget > 0 THEN ROUND(total * 100 / budget, 2) END AS percent_used,
    ROUND(previous_total, 2) AS previous_month_total,
    ROUND(total - previous_total, 2) AS change_from_previous,
    CASE WHEN previous_total > 0 THEN ROUND((total - previous_total) * 100 / previous_total, 2) END
        AS change_percent
FROM compared
WHERE month = CAST(:month_start AS date)
ORDER BY category_name
""")


async def monthly_report(
    year: Annotated[int, Query(ge=2000, le=2100)],
    month: Annotated[int, Query(ge=1, le=12)],
    user: CurrentUser,
    db: DBSession,
) -> MonthlyReportOut:
    start = date(year, month, 1)
    prev_start = previous_month(start)
    rows = await db.execute(MONTHLY_REPORT, {
        "user_id": user.id,
        "month_start": start,
        "prev_start": prev_start,
        "date_from": prev_start,
        "date_to": next_month(start),
    })
    return MonthlyReportOut(
        year=year,
        month=month,
        base_currency=user.base_currency,
        categories=[CategoryReportRow.model_validate(r, from_attributes=True) for r in rows],
    )
