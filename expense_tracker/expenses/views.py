from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import BackgroundTasks, Header, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from categories.utils import get_owned_category
from core.dependencies import CurrentUser, DBSession
from core.utils import error_response
from expenses.models import Expense, IdempotencyKey
from expenses.schemas import ExpenseFilters, ExpenseIn, ExpensePage, ExpenseOut, ExpenseUpdate
from expenses.utils import (
    decode_cursor, encode_cursor, get_owned_expense, make_etag, parse_if_match, request_hash,
)
from outbox.handlers import process_event
from outbox.utils import add_budget_check

IDEMPOTENCY_TTL = timedelta(hours=24)


async def create_expense(
    payload: ExpenseIn,
    user: CurrentUser,
    db: DBSession,
    background: BackgroundTasks,
    idempotency_key: Annotated[str | None, Header(min_length=1, max_length=255)] = None,  # "Idempotency-Key"
) -> Expense | JSONResponse:
    
    user_id = user.id
    body_hash = request_hash(payload.model_dump(mode="json"))

    if idempotency_key:
        # Keys live 24h: drop an expired one so the key can be used again.
        await db.execute(delete(IdempotencyKey).where(
            IdempotencyKey.user_id == user_id,
            IdempotencyKey.key == idempotency_key,
            IdempotencyKey.created_at < datetime.now(UTC) - IDEMPOTENCY_TTL,
        ))

        claimed = await db.scalar(
            pg_insert(IdempotencyKey)
            .values(user_id=user_id, key=idempotency_key, request_hash=body_hash)
            .on_conflict_do_nothing(constraint="uq_idempotency_keys_user_key")
            .returning(IdempotencyKey.id)
        )
        
        if claimed is None:
            
            saved = await db.scalar(select(IdempotencyKey).where(
                IdempotencyKey.user_id == user_id, IdempotencyKey.key == idempotency_key
            ))
            
            await db.commit()
            
            if saved.request_hash != body_hash:
                
                return error_response(
                    422, "Idempotency-Key was already used with a different body", code="idempotency_key_reused"
                )
                
            return JSONResponse(saved.response_body, status_code=saved.response_status)

    if await get_owned_category(db, user_id, payload.category_id) is None:
        await db.rollback()  # also releases the claimed key, so a corrected retry can use it
        return error_response(404, "Category not found")

    expense = Expense(user_id=user_id, **payload.model_dump())
    db.add(expense)
    await db.flush()
    
    body = ExpenseOut.model_validate(expense).model_dump(mode="json")

    if idempotency_key:
        await db.execute(
            update(IdempotencyKey)
            .where(IdempotencyKey.id == claimed)
            .values(response_status=201, response_body=body)
        )
    event = add_budget_check(db, user_id, expense.category_id, expense.spent_on)
    await db.commit()  # expense + idempotency record + outbox event: all or nothing
    
    background.add_task(process_event, event.id)  # runs after the response is sent
    
    return JSONResponse(body, status_code=201)


async def list_expenses(
    filters: Annotated[ExpenseFilters, Query()], user: CurrentUser, db: DBSession
) -> ExpensePage | JSONResponse:
    
    stmt = select(Expense).where(Expense.user_id == user.id)
    
    if filters.category_id is not None:
        stmt = stmt.where(Expense.category_id == filters.category_id)
        
    if filters.date_from:
        stmt = stmt.where(Expense.spent_on >= filters.date_from)
        
    if filters.date_to:
        stmt = stmt.where(Expense.spent_on <= filters.date_to)
        
    if filters.min_amount is not None:
        stmt = stmt.where(Expense.amount >= filters.min_amount)
        
    if filters.max_amount is not None:
        stmt = stmt.where(Expense.amount <= filters.max_amount)
        
    if filters.currency:
        stmt = stmt.where(Expense.currency == filters.currency)

    if filters.cursor:
        position = decode_cursor(filters.cursor)
        if position is None:
            return error_response(400, "Invalid cursor")
        # Keyset: "rows that sort after the last one I saw". Unlike OFFSET, rows inserted
        # meanwhile can't shift the pages, so nothing is skipped or repeated.
        stmt = stmt.where(tuple_(Expense.spent_on, Expense.id) < tuple_(*position))

    stmt = stmt.order_by(Expense.spent_on.desc(), Expense.id.desc()).limit(filters.limit + 1)
    rows = (await db.scalars(stmt)).all()

    items = rows[: filters.limit]
    next_cursor = encode_cursor(items[-1].spent_on, items[-1].id) if len(rows) > filters.limit else None
    return ExpensePage(items=[ExpenseOut.model_validate(e) for e in items], next_cursor=next_cursor)


async def get_expense(
    expense_id: int, response: Response, user: CurrentUser, db: DBSession
) -> Expense | JSONResponse:
    
    expense = await get_owned_expense(db, user.id, expense_id)
    
    if expense is None:
        return error_response(404, "Expense not found")
    
    response.headers["ETag"] = make_etag(expense.version)
    
    return expense


async def update_expense(
    expense_id: int,
    payload: ExpenseUpdate,
    response: Response,
    user: CurrentUser,
    db: DBSession,
    background: BackgroundTasks,
    if_match: Annotated[str | None, Header()] = None,  # reads "If-Match"
) -> Expense | JSONResponse:
    
    if if_match is None:
        return error_response(428, "If-Match header is required")

    expected_version = parse_if_match(if_match)
    if expected_version is None:
        return error_response(400, "If-Match must be an ETag returned by GET /expenses/{id}")

    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    
    if changes.get("category_id") is not None:
        
        if await get_owned_category(db, user.id, changes["category_id"]) is None:
            return error_response(404, "Category not found")

    stmt = (
        update(Expense)
        .where(Expense.id == expense_id, Expense.user_id == user.id, Expense.version == expected_version)
        .values(**changes, version=Expense.version + 1)
        .returning(Expense)
    )
    
    expense = await db.scalar(stmt)
    
    if expense is None:
        
        if await get_owned_expense(db, user.id, expense_id) is None:  # doesn't exist / isn't ours
            return error_response(404, "Expense not found")
        
        return error_response(412, "Expense was changed by someone else; GET it again", code="version_conflict")

    event = None
    
    if changes.keys() & {"amount", "currency", "category_id", "spent_on"}:
        event = add_budget_check(db, user.id, expense.category_id, expense.spent_on)
        
    await db.commit()
    
    if event:
        background.add_task(process_event, event.id)
        
    response.headers["ETag"] = make_etag(expense.version)
    
    return expense


async def delete_expense(expense_id: int, user: CurrentUser, db: DBSession) -> JSONResponse | None:
    
    deleted = await db.scalar(
        delete(Expense)
        .where(Expense.id == expense_id, Expense.user_id == user.id)
        .returning(Expense.id)
    )
    
    if deleted is None:  # nothing deleted: doesn't exist or isn't ours
        return error_response(404, "Expense not found")
        
    await db.commit()
