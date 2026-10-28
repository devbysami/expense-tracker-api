"""Shared Pydantic field types (money and currency rules live in ONE place)."""
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field

# gt=0 rejects 0 and negatives; decimal_places=2 rejects 10.123. Never a float.
Money = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]
OptionalBudget = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]

# Only currencies we seed exchange rates for; anything else couldn't be converted in reports.
# Add a code here AND seed its rates to support a new currency.
Currency = Literal["PKR", "AED", "USD", "EUR"]
