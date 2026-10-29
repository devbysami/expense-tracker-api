"""
Redis connection — async version of the Django-style module-level `conn`.

Use it in a view like DBSession:
    async def login(payload: LoginIn, db: DBSession, redis: RedisSession):
        await redis.incr("login_fail:a@example.com")

Outside a request (workers, scripts) import `conn` directly. Every call must be awaited.
"""
import os
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends
from redis.asyncio import Redis

load_dotenv()


class TooManyRequests(Exception):
    pass


# One shared Redis client per process, used by everything (views, RateLimiter, workers).
# Nothing connects until the first command runs.
# protocol=2 (RESP2) works with every Redis version; redis-py's default RESP3 sends HELLO, which needs Redis 6+.
conn = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True, protocol=2)


async def get_redis() -> Redis:
    return conn


async def close_redis() -> None:
    await conn.aclose()


RedisSession = Annotated[Redis, Depends(get_redis)]


class RateLimiter:
    
    def __init__(self, resource, type, redis_conn: Redis, max_requests, expire_time=300):
        
        self._redis = redis_conn
        self._rate_limit_key = "{}_rate_limit:{}".format(type, resource)
        self._max_requests = max_requests
        self._expire = expire_time
        
    async def check_rate_limit(self):
        
        failed_attempts = await self._redis.get(self._rate_limit_key)
        
        if failed_attempts is None:
            failed_attempts = 0
        else:
            failed_attempts = int(failed_attempts)
        
        if failed_attempts >= self._max_requests:
            raise TooManyRequests()
        
        return True
        
    async def increment(self):
        
        await self._redis.incr(self._rate_limit_key)
        
        if self._expire:
            await self.check_expiry()
        
    async def reset_rate_limit(self):
        await self._redis.delete(self._rate_limit_key)
    
    async def set_expiry(self):
        await self._redis.expire(self._rate_limit_key, self._expire)
        
    async def check_expiry(self):
        
        ttl = await self._redis.ttl(self._rate_limit_key)
        
        if ttl == -1:
            await self.set_expiry()
            
    async def get_current_count(self):
        
        count = await self._redis.get(self._rate_limit_key)
        
        if count is None:
            return 0
        
        return int(count)
    
    async def attempts_left(self):
        
        current_count = await self.get_current_count()
        remaining = self._max_requests - current_count
        
        return max(0, remaining)

    async def retry_after(self):
        """Seconds until the counter expires and the user can try again (for the Retry-After header)."""

        ttl = await self._redis.ttl(self._rate_limit_key)

        if ttl == -2:
            return 0

        if ttl == -1:
            return self._expire

        return ttl