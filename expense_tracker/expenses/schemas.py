from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from core.types import Currency, Money


class ExpenseIn(BaseModel):
    category_id: int
    amount: Money
    currency: Currency
    description: str = Field(default="", max_length=500)
    spent_on: date


class ExpenseUpdate(BaseModel):
    """PATCH body: every field optional. Use model_dump(exclude_unset=True) to get only what was sent."""
    category_id: int | None = None
    amount: Money | None = None
    currency: Currency | None = None
    description: str | None = Field(default=None, max_length=500)
    spent_on: date | None = None


class ExpenseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category_id: int
    amount: Decimal
    currency: str
    description: str
    spent_on: date
    version: int
    created_at: datetime
    updated_at: datetime


class ExpenseFilters(BaseModel):
    """Query params for GET /expenses. Used with `Annotated[ExpenseFilters, Query()]` in the view,
    so FastAPI reads each field from the query string and validates it."""
    category_id: int | None = None
    date_from: date | None = None
    date_to: date | None = None
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None
    currency: Currency | None = None
    cursor: str | None = None  # next_cursor from the previous page
    limit: int = Field(default=20, ge=1, le=100)


class ExpensePage(BaseModel):
    items: list[ExpenseOut]
    next_cursor: str | None
