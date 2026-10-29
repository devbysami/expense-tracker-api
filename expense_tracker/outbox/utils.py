"""Helpers that ADD outbox rows to the caller's session. They never commit: the caller commits the
outbox row together with the change that caused it (that is the whole point of the pattern),
then schedules handlers.process_event(event.id) as a background task."""
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from core.dates import month_start
from outbox.models import OutboxEvent

BUDGET_CHECK = "budget.check"


def add_budget_check(db: AsyncSession, user_id: int, category_id: int, spent_on: date) -> OutboxEvent:
    event = OutboxEvent(
        event_type=BUDGET_CHECK,
        payload={"user_id": user_id, "category_id": category_id, "month": month_start(spent_on).isoformat()},
    )
    db.add(event)
    return event

