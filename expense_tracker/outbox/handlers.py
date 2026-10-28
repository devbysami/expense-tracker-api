"""What the worker does with each event type. Every handler must be safe to run twice."""
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from categories.models import Category
from core.dates import next_month
from core.storage import Storage
from exchange_rates.queries import CONVERTED_EXPENSES
from notifications.models import Notification
from outbox.utils import BUDGET_CHECK, RECEIPT_DELETE

ALERT_LEVELS = (80, 100)

CATEGORY_MONTH_TOTAL = text(f"""
WITH converted AS ({CONVERTED_EXPENSES})
SELECT COALESCE(SUM(amount_base), 0) FROM converted WHERE category_id = :category_id
""")


async def check_budget(db: AsyncSession, payload: dict) -> None:
    category = await db.get(Category, payload["category_id"])
    if category is None or not category.monthly_budget:
        return  # category gone or no budget -> nothing to alert about
    month = date.fromisoformat(payload["month"])

    total: Decimal = await db.scalar(CATEGORY_MONTH_TOTAL, {
        "user_id": category.user_id, "category_id": category.id,
        "date_from": month, "date_to": next_month(month),
    })
    percent = total * 100 / category.monthly_budget

    for level in ALERT_LEVELS:
        if percent >= level:
            # The unique constraint (user, category, month, level) makes this exactly-once,
            # no matter how many times the event is processed or how many workers run.
            await db.execute(
                pg_insert(Notification)
                .values(
                    user_id=category.user_id,
                    category_id=category.id,
                    month=month,
                    level=level,
                    message=f"{category.name}: {percent:.0f}% of your {month:%B %Y} budget used",
                )
                .on_conflict_do_nothing(constraint="uq_notifications_once")
            )


async def handle_event(db: AsyncSession, event_type: str, payload: dict, storage: Storage) -> None:
    if event_type == BUDGET_CHECK:
        await check_budget(db, payload)
    elif event_type == RECEIPT_DELETE:
        await storage.delete(payload["key"])
    else:
        raise ValueError(f"unknown event type {event_type}")
