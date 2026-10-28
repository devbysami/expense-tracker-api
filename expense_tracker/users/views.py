"""
Views = plain async functions. FastAPI reads the type hints to decide where each
argument comes from:
  - a Pydantic model      -> JSON body
  - DBSession / CurrentUser -> dependency (see core/dependencies.py)
  - simple types (int, str) -> path or query params
Routes are attached to these functions in urls.py.
"""
from datetime import UTC, datetime

import jwt
from fastapi.responses import JSONResponse
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from core.dependencies import CurrentUser, DBSession
from core.security import decode_token, hash_password, verify_password
from core.redis_ops import RateLimiter, RedisSession, TooManyRequests
from core.utils import error_response
from users.models import User, RefreshToken
from users.schemas import LoginIn, RefreshIn, RegisterIn, TokenPairOut, UserOut
from users.utils import has_active_refresh_token, is_refresh_token_valid, issue_token_pair


async def register(payload: RegisterIn, db: DBSession) -> User | JSONResponse:
    user = User(
        email=payload.email.lower(),
        password_hash=await hash_password(payload.password),
        base_currency=payload.base_currency,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return error_response(409, "Email already registered")
    return user


async def login(payload: LoginIn, db: DBSession, redis: RedisSession) -> TokenPairOut | JSONResponse:
    
    rate_limiter = RateLimiter(
        resource=payload.email.lower(),
        type="login",
        redis_conn=redis,
        max_requests=5,
        expire_time=900
    )
    
    user = await db.scalar(select(User).where(User.email == payload.email.lower()))
    
    try:
        
        await rate_limiter.check_rate_limit()
        
        if user is None or not await verify_password(payload.password, user.password_hash):
            
            await rate_limiter.increment()
            
            return error_response(401, "Invalid email or password")
        
        await rate_limiter.reset_rate_limit()
        
    except TooManyRequests:
        return error_response(
            429,
            "Too many failed login attempts. Please try again later.",
            headers={"Retry-After": str(await rate_limiter.retry_after())}
        )

    tokens = await issue_token_pair(db, user.id)
    await db.commit()
    return tokens


async def refresh(payload: RefreshIn, db: DBSession) -> TokenPairOut | JSONResponse:

    try:
        claims = decode_token(payload.refresh_token, expected_type="refresh")
    except (jwt.PyJWTError, ValueError):
        return error_response(401, "Invalid or expired refresh token")
    
    token = await db.scalar(
        select(RefreshToken).where(RefreshToken.jti == claims["jti"]).with_for_update()
    )
    
    token_valid, expired = is_refresh_token_valid(token)
    
    if not token_valid:
        
        if token is not None and token.revoked_at is not None:
            
            if await has_active_refresh_token(db, token.user_id):
                await db.execute(
                    update(RefreshToken).where(
                        RefreshToken.user_id == token.user_id,
                        RefreshToken.revoked_at.is_(None)
                    ).values(revoked_at=datetime.now(UTC))
                )
            await db.commit()
            return error_response(401, "Refresh token was already used; please log in again")
        
        return error_response(401, "Refresh token expired" if expired else "Invalid refresh token")
    
    token.revoked_at = datetime.now(UTC)
    tokens = await issue_token_pair(db, token.user_id)
    await db.commit()
    return tokens

async def logout(payload: RefreshIn, db: DBSession) -> None:
    
    try:
        claims = decode_token(payload.refresh_token, expected_type="refresh")
    except (jwt.PyJWTError, ValueError):
        return error_response(401, "Invalid or expired refresh token")
    
    token = await db.scalar(
        select(RefreshToken).where(RefreshToken.jti == claims["jti"]).with_for_update()
    )
    
    if token:
        token.revoked_at = datetime.now(UTC)
        await db.commit()
    else:
        return error_response(401, "Refresh token not found")


async def me(user: CurrentUser) -> User:
    return user
