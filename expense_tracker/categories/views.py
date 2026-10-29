from fastapi.responses import JSONResponse
from sqlalchemy import and_, case, func, select
from sqlalchemy.exc import IntegrityError

from categories.models import Category
from categories.schemas import CategoryIn, CategoryWithSpendOut
from core.dates import month_start, next_month, today_utc
from core.dependencies import CurrentUser, DBSession
from core.utils import error_response
from exchange_rates.queries import amount_in_base
from expenses.models import Expense


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
    spent = func.coalesce(func.sum(amount_in_base(user.base_currency)), 0)

    # One query for all categories: OUTER JOIN so categories with no spend this month still show up.
    stmt = (
        select(
            Category.id,
            Category.name,
            Category.monthly_budget,
            func.round(spent, 2).label("spent_this_month"),
            case((Category.monthly_budget > 0, func.round(spent * 100 / Category.monthly_budget, 2)))
            .label("percent_used"),
        )
        .outerjoin(Expense, and_(
            Expense.category_id == Category.id,
            Expense.spent_on >= start,
            Expense.spent_on < next_month(start),
        ))
        .where(Category.user_id == user.id)
        .group_by(Category.id)
        .order_by(Category.name)
    )
    rows = await db.execute(stmt)
    return [CategoryWithSpendOut.model_validate(row, from_attributes=True) for row in rows]
