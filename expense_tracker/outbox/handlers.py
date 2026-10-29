"""Processing outbox events (run as a FastAPI background task). Every handler must be safe to run twice."""
import logging
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from categories.models import Category
from core.dates import next_month
from database import SessionLocal
from exchange_rates.queries import amount_in_base
from expenses.models import Expense
from notifications.models import Notification
from outbox.models import OutboxEvent
from outbox.utils import BUDGET_CHECK
from users.models import User

log = logging.getLogger(__name__)

ALERT_LEVELS = (80, 100)


async def check_budget(db: AsyncSession, payload: dict) -> None:
    
    category = await db.get(Category, payload["category_id"])
    
    if category is None or not category.monthly_budget:
        return  # category gone or no budget -> nothing to alert about
    
    month = date.fromisoformat(payload["month"])

    user = await db.get(User, category.user_id)

    # This category's spend for the month, every expense converted to the user's base currency.
    total: Decimal = await db.scalar(
        select(func.coalesce(func.sum(amount_in_base(user.base_currency)), 0)).where(
            Expense.user_id == category.user_id,
            Expense.category_id == category.id,
            Expense.spent_on >= month,
            Expense.spent_on < next_month(month),
        )
    )
    
    percent = total * 100 / category.monthly_budget

    for level in ALERT_LEVELS:
        
        if percent >= level:
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


async def handle_event(db: AsyncSession, event_type: str, payload: dict) -> None:
    
    if event_type == BUDGET_CHECK:
        await check_budget(db, payload)
        
    else:
        raise ValueError(f"unknown event type {event_type}")


async def process_event(event_id: int) -> None:
    """Background task: handles one outbox event, then marks it processed.
    Safe to run twice: the row lock + processed_at check skip a done (or in-progress) event,
    and the notifications unique constraint stops a second alert."""

    async with SessionLocal() as db:

        event = await db.scalar(
            select(OutboxEvent)
            .where(OutboxEvent.id == event_id, OutboxEvent.processed_at.is_(None))
            .with_for_update(skip_locked=True)
        )
        if event is None:
            return  # already processed, or another task is processing it right now

        try:
            async with db.begin_nested():  # savepoint: a failed handler doesn't undo the bookkeeping below
                await handle_event(db, event.event_type, event.payload)
                
            event.processed_at = datetime.now(UTC)
            
        except Exception as exc:
            log.exception("outbox event %s failed", event_id)
            event.attempts += 1
            event.last_error = str(exc)[:1000]  # stays pending; run process_event(id) again to retry

        await db.commit()
