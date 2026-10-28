from decimal import Decimal

from pydantic import BaseModel


class CategoryReportRow(BaseModel):
    category_id: int
    category_name: str
    total: Decimal                    # in base currency
    budget: Decimal | None
    percent_used: Decimal | None      # None when there is no budget
    previous_month_total: Decimal
    change_from_previous: Decimal     # total - previous_month_total
    change_percent: Decimal | None    # None when last month was 0


class MonthlyReportOut(BaseModel):
    year: int
    month: int
    base_currency: str
    categories: list[CategoryReportRow]
