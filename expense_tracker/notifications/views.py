from typing import Annotated

from fastapi import Query
from sqlalchemy import select

from core.dependencies import CurrentUser, DBSession
from notifications.models import Notification


async def list_notifications(
    user: CurrentUser, db: DBSession, limit: Annotated[int, Query(ge=1, le=100)] = 50
) -> list[Notification]:
    result = await db.scalars(
        select(Notification)
        .where(Notification.user_id == user.id)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(limit)
    )
    return list(result)
