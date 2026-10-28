"""Helpers that ADD outbox rows to the caller's session. They never commit: the caller commits the
outbox row together with the change that caused it (that is the whole point of the pattern)."""
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from core.dates import month_start
from outbox.models import OutboxEvent

BUDGET_CHECK = "budget.check"
RECEIPT_DELETE = "receipt.delete"


def add_budget_check(db: AsyncSession, user_id: int, category_id: int, spent_on: date) -> None:
    db.add(OutboxEvent(
        event_type=BUDGET_CHECK,
        payload={"user_id": user_id, "category_id": category_id, "month": month_start(spent_on).isoformat()},
    ))


def add_receipt_delete(db: AsyncSession, key: str) -> None:
    db.add(OutboxEvent(event_type=RECEIPT_DELETE, payload={"key": key}))
