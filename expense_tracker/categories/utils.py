from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from categories.models import Category


async def get_owned_category(db: AsyncSession, user_id: int, category_id: int) -> Category | None:
    """Always filter by user_id: someone else's category looks exactly like a missing one (404, not 403)."""
    return await db.scalar(
        select(Category).where(Category.id == category_id, Category.user_id == user_id)
    )
