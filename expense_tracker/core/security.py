"""Password hashing (argon2) and JWT helpers."""
import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from dotenv import load_dotenv
from pwdlib import PasswordHash

load_dotenv()

JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALGORITHM = os.environ["JWT_ALGORITHM"]
ACCESS_TOKEN_MINUTES = int(os.environ["ACCESS_TOKEN_MINUTES"])
REFRESH_TOKEN_DAYS = int(os.environ["REFRESH_TOKEN_DAYS"])

password_hasher = PasswordHash.recommended()  # argon2


# argon2 is slow ON PURPOSE (~50 ms of CPU). Run in a thread so it doesn't block the event loop.
async def hash_password(password: str) -> str:
    return await asyncio.to_thread(password_hasher.hash, password)


async def verify_password(password: str, password_hash: str) -> bool:
    return await asyncio.to_thread(password_hasher.verify, password, password_hash)


def create_token(user_id: int, token_type: str, expires_delta: timedelta) -> tuple[str, str, datetime]:
    """Returns (encoded_token, jti, expires_at). jti = unique token id, used to track refresh tokens."""
    jti = uuid.uuid4().hex
    expires_at = datetime.now(UTC) + expires_delta
    payload = {"sub": str(user_id), "type": token_type, "jti": jti, "exp": expires_at}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM), jti, expires_at


def create_access_token(user_id: int) -> str:
    token, _, _ = create_token(user_id, "access", timedelta(minutes=ACCESS_TOKEN_MINUTES))
    return token


def create_refresh_token(user_id: int) -> tuple[str, str, datetime]:
    return create_token(user_id, "refresh", timedelta(days=REFRESH_TOKEN_DAYS))


def decode_token(token: str, expected_type: str) -> dict:
    """Raises jwt.PyJWTError (or ValueError for wrong type) — callers turn that into 401."""
    payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    if payload.get("type") != expected_type:
        raise ValueError("wrong token type")
    return payload
