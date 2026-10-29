from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from categories.urls import router as categories_router
from core.redis_ops import close_redis
from core.utils import error_response
from expenses.urls import router as expenses_router
from imports.urls import router as imports_router
from notifications.urls import router as notifications_router
from reports.urls import router as reports_router
from users.urls import router as users_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Code before `yield` runs on startup, after `yield` on shutdown.
    yield
    await close_redis()


app = FastAPI(
    lifespan=lifespan,
    title="Expense tracker",
    description="Core Expense Tracker. REST API where users track their spending by category, in more than one currency.\
        Every user has a base currency (for example PKR or AED). Reports are always shown in that base currency",
    version="1.0.0",
)

# Only 2 handlers, for errors that never reach a view's `return`:
# 1. Pydantic rejected the input before the view ran (bad body / query / header).
@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    details = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()]
    return error_response(422, "Invalid input", details, code="validation_error")


# 2. HTTPException raised by a dependency (get_current_user -> 401) or by FastAPI itself (unknown URL -> 404).
@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException):
    return error_response(exc.status_code, str(exc.detail), headers=getattr(exc, "headers", None))

# Project-level urls.py: every app router is mounted under /api/v1 (like include() in Django)
api_v1 = APIRouter(prefix="/api/v1")
api_v1.include_router(users_router)
api_v1.include_router(categories_router)
api_v1.include_router(imports_router)
api_v1.include_router(expenses_router)
api_v1.include_router(notifications_router)
api_v1.include_router(reports_router)

app.include_router(api_v1)


# Load balancer / ECS health check. Outside /api/v1 and without auth on purpose.
@app.get("/health", tags=["Health"])
async def health() -> dict:
    return {"status": "ok"}
