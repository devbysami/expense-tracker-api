"""
Reusable dependencies — the closest thing FastAPI has to Django's
request.user / middleware / @login_required.

A view declares what it needs in its signature:
    async def my_view(user: CurrentUser, db: DBSession): ...
and FastAPI resolves those before calling it.
"""
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from core.security import decode_token
from database import get_db
from users.models import User

bearer_scheme = HTTPBearer(auto_error=False)

DBSession = Annotated[AsyncSession, Depends(get_db)]


async def get_current_user(
    db: DBSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    # Dependencies can't `return` an error response to the client, so they raise; main.py formats it.
    if credentials is None:
        raise HTTPException(401, "Missing bearer token")
    try:
        payload = decode_token(credentials.credentials, expected_type="access")
    except (jwt.PyJWTError, ValueError):
        raise HTTPException(401, "Invalid or expired token")

    user = await db.get(User, int(payload["sub"]))
    if user is None:
        raise HTTPException(401, "User no longer exists")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
