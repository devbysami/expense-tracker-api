from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from core.types import OptionalBudget


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    monthly_budget: OptionalBudget | None = None


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    monthly_budget: Decimal | None


class CategoryWithSpendOut(CategoryOut):
    spent_this_month: Decimal  # in the user's base currency
    percent_used: Decimal | None  # None when the category has no budget
