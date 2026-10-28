"""Import every model here so Alembic's autogenerate sees all tables (like Django's app registry)."""
from categories.models import Category  # noqa: F401
from database import Base  # noqa: F401
from exchange_rates.models import ExchangeRate  # noqa: F401
from expenses.models import Expense, IdempotencyKey  # noqa: F401
from imports.models import ImportJob  # noqa: F401
from notifications.models import Notification  # noqa: F401
from outbox.models import OutboxEvent  # noqa: F401
from users.models import RefreshToken, User  # noqa: F401
