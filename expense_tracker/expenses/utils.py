import base64
import hashlib
import json
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from expenses.models import Expense


async def get_owned_expense(db: AsyncSession, user_id: int, expense_id: int) -> Expense | None:
    return await db.scalar(
        select(Expense).where(Expense.id == expense_id, Expense.user_id == user_id)
    )


def make_etag(version: int) -> str:
    return f'"{version}"'


def parse_if_match(value: str) -> int | None:
    """'"3"' or 'W/"3"' -> 3. Returns None if it isn't a valid version."""
    value = value.strip().removeprefix("W/").strip('"')
    return int(value) if value.isdigit() else None


def encode_cursor(spent_on: date, expense_id: int) -> str:
    """The cursor is the sort key of the last row on the page (opaque to the client)."""
    return base64.urlsafe_b64encode(f"{spent_on.isoformat()}|{expense_id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[date, int] | None:
    try:
        spent_on, expense_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return date.fromisoformat(spent_on), int(expense_id)
    except (ValueError, UnicodeDecodeError):
        return None


def request_hash(payload: dict) -> str:
    """Same body (in any key order) -> same hash. Used to spot a reused Idempotency-Key with a different body."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
