from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.security import create_access_token, create_refresh_token
from users.models import RefreshToken
from users.schemas import TokenPairOut


async def issue_token_pair(db: AsyncSession, user_id: int) -> TokenPairOut:
    """Create an access + refresh token and remember the refresh token's jti in the DB.
    Caller is responsible for db.commit() (so it can be part of a bigger transaction)."""
    refresh_token, jti, expires_at = create_refresh_token(user_id)
    db.add(RefreshToken(jti=jti, user_id=user_id, expires_at=expires_at))
    return TokenPairOut(access_token=create_access_token(user_id), refresh_token=refresh_token)

async def has_active_refresh_token(db: AsyncSession, user_id: int):
    
    """Check if the user has any active (not revoked, not expired) refresh tokens."""
    
    return await db.scalar(
        select(
            select(RefreshToken.jti).where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > datetime.now(UTC)
            ).exists()
        )
    )


def is_refresh_token_valid(token: RefreshToken | None):
    
    """Check if the refresh token is valid (not revoked and not expired). Returns (valid, expired)."""
    
    if not token:
        return False, False
    
    if token.revoked_at is not None:
        return False, False
    
    if token.expires_at < datetime.now(UTC):
        return False, True
    
    return True, False