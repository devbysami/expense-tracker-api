from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from categories.models import Category
from categories.schemas import CategoryIn, CategoryWithSpendOut
from core.dates import month_start, next_month, today_utc
from core.dependencies import CurrentUser, DBSession
from core.utils import error_response
from exchange_rates.queries import CONVERTED_EXPENSES

# One query for all categories: LEFT JOIN so categories with no spend this month still show up.
CATEGORIES_WITH_SPEND = text(f"""
WITH converted AS ({CONVERTED_EXPENSES})
SELECT
    c.id,
    c.name,
    c.monthly_budget,
    ROUND(COALESCE(SUM(cv.amount_base), 0), 2) AS spent_this_month,
    CASE WHEN c.monthly_budget > 0
         THEN ROUND(COALESCE(SUM(cv.amount_base), 0) * 100 / c.monthly_budget, 2)
    END AS percent_used
FROM categories c
LEFT JOIN converted cv ON cv.category_id = c.id
WHERE c.user_id = :user_id
GROUP BY c.id, c.name, c.monthly_budget
ORDER BY c.name
""")


async def create_category(payload: CategoryIn, user: CurrentUser, db: DBSession) -> Category | JSONResponse:
    category = Category(user_id=user.id, **payload.model_dump())
    db.add(category)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return error_response(409, "Category with this name already exists")
    return category


async def list_categories(user: CurrentUser, db: DBSession) -> list[CategoryWithSpendOut]:
    start = month_start(today_utc())
    rows = await db.execute(
        CATEGORIES_WITH_SPEND, {"user_id": user.id, "date_from": start, "date_to": next_month(start)}
    )
    return [CategoryWithSpendOut.model_validate(row, from_attributes=True) for row in rows]
