# Expense Tracker API

Async FastAPI + SQLAlchemy 2.0 + PostgreSQL + Redis expense tracker with multi-currency reports, budget alerts and CSV import. **Everything in the assessment is implemented except 2.5 (receipts on S3).** Known shortcuts and how I would finish them are listed in [What is not finished](#what-is-not-finished).

| Part | Status |
|---|---|
| 1. Core API (auth, categories, expenses, reports, migrations, seed) | Done |
| 2.1 Idempotency-Key · 2.2 ETag/If-Match · 2.3 Outbox + background task · 2.4 CSV import | Done |
| 2.5 Receipts on S3 | Not done ([why](#25-receipts-on-s3-not-done)) |
| 3. Tests (pytest + httpx AsyncClient, real PostgreSQL) | 60 tests, 97% coverage |
| 4. Deployment notes and short answers | Below |
| Postman collection | [`postman/Expense Tracker.postman_collection.json`](postman/Expense%20Tracker.postman_collection.json) |

## Contents
- [Dependencies](#dependencies)
- [Setup and run](#setup-and-run)
- [Postman collection](#postman-collection)
- [Tests and coverage](#tests-and-coverage)
- [Project layout](#project-layout)
- [Design decisions](#design-decisions)
- [Database indexes](#database-indexes)
- [Deployment notes (AWS)](#deployment-notes-aws)
- [Short answers](#short-answers)
- [What is not finished](#what-is-not-finished)
- [Code review: bugs found and fixed](#code-review-bugs-found-and-fixed)
- [AI usage](#ai-usage)

## Dependencies
**Services**

| Service | Version | What it's used for | Who needs it |
|---|---|---|---|
| PostgreSQL | 12+ | All data. Two databases: `expense_tracker` (app) and `expense_tracker_test` (tests) | API, migrations, tests |
| Redis | 5.0+ | Only the login rate limiter: one counter per email, `login_rate_limit:{email}`, `INCR` on each failed login with a 15-minute TTL | API (`POST /auth/login` only). Tests use `fakeredis`, no server needed |

There is no AWS/S3 dependency (receipts are not implemented, see [2.5](#25-receipts-on-s3-not-done)), no Celery and no separate worker process: outbox events and CSV imports run as FastAPI background tasks inside the API process.

About Redis:
- Nothing is stored there that matters long-term. If Redis is flushed or restarted, the only effect is that failed-login counters start from zero. No persistence configuration is needed.
- If Redis is down, `POST /auth/login` returns 500 (it fails closed: nobody can log in, but nobody can brute-force either). Every other endpoint keeps working because nothing else uses Redis.
- Redis 5 works because the client uses `protocol=2` (RESP2), see setup below.

**Python packages** (pinned in `requirements.txt`)

| Package | Why |
|---|---|
| `fastapi`, `uvicorn`, `python-multipart` | Web framework, ASGI server, multipart form parsing for the CSV upload |
| `pydantic`, `email-validator` | Request/response schemas and validation (`EmailStr`) |
| `SQLAlchemy` 2 (async), `asyncpg`, `greenlet` | ORM and async PostgreSQL driver |
| `alembic` | Migrations (`makemigrations` / `migrate` equivalents) |
| `python-dotenv` | Loads config from `.env` |
| `redis` (`redis.asyncio`) | Async Redis client for the rate limiter |
| `PyJWT` | Access and refresh JWTs |
| `pwdlib`, `argon2-cffi` | argon2 password hashing |
| `pandas` | Chunked CSV reading for the import |
| `pytest`, `pytest-asyncio`, `pytest-cov`, `coverage`, `httpx`, `fakeredis` | Tests only |

**Environment variables** (`.env`, see `.env.example`)

| Key | Example | Notes |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://user:pass@localhost:5432/expense_tracker` | Must use the `+asyncpg` driver |
| `TEST_DATABASE_URL` | `…/expense_tracker_test` | Tests drop and recreate all tables here, never point it at real data |
| `REDIS_URL` | `redis://localhost:6379/0` | |
| `JWT_SECRET` | random 48+ chars | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `JWT_ALGORITHM` | `HS256` | |
| `ACCESS_TOKEN_MINUTES` / `REFRESH_TOKEN_DAYS` | `15` / `7` | Token lifetimes |

## Setup and run
Requirements: Python 3.11+, PostgreSQL 12+, Redis 5+.
```bash
python3.11 -m venv _py && source _py/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in DB/Redis URLs and a JWT_SECRET (see comments in the file)
createdb expense_tracker && createdb expense_tracker_test

alembic upgrade head            # creates all tables + seeds 90 days of PKR/AED/USD/EUR rates
uvicorn main:app --app-dir expense_tracker --reload      # API on :8000, docs on /docs
```
- **Redis** is required for the login rate limiter (the API starts without it, but `POST /auth/login` fails until it is reachable). Any Redis **5.0+** works:
  ```bash
  sudo apt install -y redis-server && sudo systemctl enable --now redis-server   # Ubuntu (20.04 ships 5.0.7)
  redis-cli ping                                                                   # -> PONG
  ```
  Set `REDIS_URL` in `.env` (default `redis://localhost:6379/0`). The client is created with `protocol=2` (RESP2) in `core/redis_ops.py`: redis-py's default RESP3 opens connections with `HELLO 3`, which Redis 5 rejects (`unknown command HELLO`). Tests don't need a Redis server (they use `fakeredis`).
- Config comes only from `.env` (git-ignored). `.env.example` lists every key.
- New migration: `alembic revision --autogenerate -m "..."`, review the file, then `alembic upgrade head`. Roll back one: `alembic downgrade -1`.

## Postman collection
`postman/Expense Tracker.postman_collection.json`, with `postman/sample_import.csv` next to it.

**Setup**
- Import the collection in Postman. It has three collection variables: `BASE_URL` (default `http://127.0.0.1:8000`; every request adds `/api/v1/...` itself), `access_token` and `refresh_token`.
- Start the API (and Redis, which login needs), then run **Auth - Register** once and **Auth - Login**. Login saves both tokens into the variables, and every other request sends `Authorization: Bearer {{access_token}}`. When the access token expires (15 minutes), run **Auth - Refresh Token**: it saves the new pair, and the old refresh token stops working.

**Requests**

| Request | What it shows |
|---|---|
| Auth - Register / Login / Refresh Token / Me | Sign-up with a base currency, JWT pair, refresh rotation, the current user |
| Categories - Create / Get | A category with a monthly budget; the list shows this month's spend and % of budget in the base currency |
| Expense - Create | Sends an `Idempotency-Key` header. Send it twice with the same key and body: the second response replays the first expense instead of creating another. Same key with a different body gives 422 |
| Expense - Get All | `GET /expenses` with optional filters (a disabled `category_id` param is ready to switch on); the response has `next_cursor` for the next page |
| Expense - Get Expense | Returns the `ETag` header (the expense's version) |
| Expense - Update Expense | `PATCH` with `If-Match`. A version that is out of date gives 412; no header gives 428 |
| Expense - Delete | 204, then 404 on a second try |
| Notifications - Get Notifications | Budget alerts at 80% and 100%, created by the outbox background task after an expense is saved |
| Imports - Add Imports / Poll Import | CSV upload (202 + `job_id`), then the job's status, counters and per-row errors |
| Reports - Monthly | Totals per category for `year`/`month` in the base currency, next to the previous month |

**IDs are written into the requests:** the expense requests use `/expenses/1` and `"category_id": 1`, **Poll Import** has a job id in its URL, and **Update Expense** sends `If-Match: 1`. On a new database, replace them with the ids returned by the create calls (and the `ETag` from **Get Expense**). **Add Imports** points at `sample_import.csv` with a path from my machine; re-select `postman/sample_import.csv` in the request's Body tab.

The sample CSV has good rows and rows that fail on purpose (amount 0, categories that don't exist yet), so **Poll Import** shows both the counters and the error list. Upload it a second time and every good row is reported as a duplicate instead of being imported again.

## Tests and coverage
```bash
pytest                 # 60 passed
pytest --cov           # 97% total (config in .coveragerc)
```
- Real PostgreSQL test DB (`TEST_DATABASE_URL`); tables are created from the models once and **truncated after every test**.
- Redis is replaced by `fakeredis`, so tests need no Redis server.
- `.coveragerc` sets `concurrency = greenlet, thread`: SQLAlchemy's async layer runs code in greenlets, which coverage otherwise misses.

| Required test | Where |
|---|---|
| Refresh token used twice revokes all tokens | `test_auth.py::test_refresh_token_used_twice_revokes_all_tokens` |
| 6th failed login returns 429 | `test_auth.py::test_sixth_failed_login_returns_429` |
| Another user's expense returns 404 | `test_expenses.py::test_other_users_expense_is_404` |
| Amount 0, negative, >2 decimals rejected | `test_expenses.py::test_bad_amount_rejected` |
| Cursor pagination with rows added between pages | `test_expenses.py::test_cursor_pagination_no_duplicates_or_gaps_while_rows_are_added` |
| Report with mixed currencies and a missing rate day | `test_reports.py::test_monthly_report_mixed_currencies_and_missing_rate_day` |
| Two parallel POSTs, same Idempotency-Key, one expense | `test_idempotency.py::test_parallel_posts_with_same_key_create_one_expense` |
| Two parallel PATCHes, same ETag: one 200, one 412 | `test_expenses.py::test_parallel_patches_one_wins_one_412` |
| Budget event processed twice, one notification | `test_budget_alerts.py::test_event_processed_twice_creates_one_notification` |
| CSV with good and bad rows gives right counts | `test_imports.py::test_import_good_and_bad_rows_gives_right_counts` |
| Fake file (.exe renamed .png) rejected | Not done: receipts (2.5) are not implemented |

## Project layout
Django-style apps: each has `models.py` (SQLAlchemy), `schemas.py` (Pydantic, like DRF serializers), `views.py` (async functions), `urls.py` (routes via `router.add_api_route`), `utils.py`.
```
expense_tracker/
  main.py            app, /api/v1 router, the 2 global error handlers, /health
  database.py        async engine + session dependency
  models.py          imports every model (for Alembic)
  core/              security (argon2, JWT), dependencies (CurrentUser, DBSession), redis_ops,
                     types (Money, Currency), utils (error_response), dates
  users/ categories/ expenses/ reports/ exchange_rates/ notifications/ imports/ outbox/
migrations/          Alembic (schema + rate seed)
tests/               one file per feature
postman/             collection + sample CSV
```
**One error format** everywhere: `{"error": {"code", "message", "details"}}`. Views `return error_response(status, message)`; two handlers in `main.py` cover what never reaches a view (Pydantic validation → 422, dependency/unknown-URL `HTTPException`).

## Design decisions
### Auth and rate limiting
- argon2 (pwdlib), run in a thread so hashing doesn't block the event loop. Access JWT 15 min, refresh JWT 7 days.
- Each refresh token's `jti` is a row in `refresh_tokens`. Refresh **rotates**: the row is locked (`SELECT … FOR UPDATE`), marked revoked, and a new pair is issued. A token that is already revoked coming back means it was copied, so **all** of the user's refresh tokens are revoked. The row lock makes two parallel refreshes with the same token safe (one 200, one 401).
- Login rate limit: Redis counter `login_rate_limit:{email}`, 5 failures per 15 min, then 429 with `Retry-After` (the key's TTL). Redis is shared, so it works across any number of API servers.

### Listing and cursor pagination
`GET /expenses` filters: `category_id`, `date_from`, `date_to`, `min_amount`, `max_amount`, `currency`, `limit` (1-100, default 20) and `cursor`.

Keyset on `(spent_on DESC, id DESC)`: the cursor is base64 of the last row's `(spent_on, id)` and the next page is `WHERE (spent_on, id) < (:d, :id)`. Inserting rows can't shift pages, unlike OFFSET. `id` is the tie-breaker for equal dates.

### Monthly report (one SQL query)
`reports/views.py::monthly_report_query` builds a single statement with SQLAlchemy (no raw SQL strings):
1. Conversion (`exchange_rates/queries.py::amount_in_base`): `CASE WHEN currency = base THEN amount ELSE amount * rate(cur) / rate(base) END`, where each rate is a correlated subquery for the latest rate on or before the expense date (`ORDER BY valid_on DESC LIMIT 1`).
2. `categories CROSS JOIN (VALUES last month, this month) LEFT JOIN expenses … GROUP BY` → total per category per month (categories with no spend still appear).
3. `LAG(total) OVER (PARTITION BY category ORDER BY month)` → last month next to this month; budget %, change and change % computed in SQL.

The same conversion powers `GET /categories` (this month's spend vs budget) and the budget check.

### 2.1 Idempotency-Key: DB unique constraint, no Redis lock
Table `idempotency_keys` with `UNIQUE (user_id, key)`, request hash, saved response. In the **same transaction** as the expense:
1. `INSERT … ON CONFLICT DO NOTHING RETURNING id` claims the key.
2. If a parallel request holds the key in an open transaction, Postgres makes this INSERT **wait** on the unique index until that transaction commits, then it conflicts → we read the saved response and replay it (or 422 if the body hash differs).
3. Otherwise create the expense, save the response on the key row, commit everything together.

Why only the DB: the key, the expense and the saved response commit atomically, so there is no window where the expense exists but the key doesn't (or the reverse). A Redis lock would add a second system that can't be committed together with Postgres, plus lock-expiry edge cases, and would still need the DB constraint as the real guarantee. Failed requests (e.g. category 404) roll back and don't burn the key. Keys expire after 24h: an expired key is deleted when it is reused (keys that are never reused stay in the table; see not finished).

### 2.2 No lost updates
`GET` returns `ETag: "<version>"`. `PATCH` requires `If-Match` (428 if missing, 400 if malformed) and runs one statement: `UPDATE … SET …, version = version + 1 WHERE id = :id AND user_id = :u AND version = :v RETURNING *`. No row → 404 if the expense isn't ours, else 412.

### 2.3 Budget alerts with the transactional outbox
- Creating an expense, or changing its amount/currency/category/date, adds an `outbox_events` row (`budget.check` for that category-month) **in the same transaction**. No alert is sent inside the request.
- After the commit, the view schedules a FastAPI **background task** with the outbox event id: `outbox/handlers.py::process_event(event_id)`. It runs after the response is sent, locks the event row with `FOR UPDATE SKIP LOCKED` (skipped if already processed or locked by another task), runs the handler in a savepoint and marks the event processed. A failing event keeps `processed_at = NULL` and gets `attempts + 1` and `last_error`, so it can be re-run with `process_event(id)`.
- The handler computes the category-month total in the base currency and inserts a notification for each crossed level (80, 100) with `ON CONFLICT DO NOTHING` on `UNIQUE (user_id, category_id, month, level)`. That constraint is what makes it **exactly once** per category per month per level, even if an event is processed again after a crash, or two tasks run at once.
- **Edits and deletes:** an edit re-checks the (new) category-month, so moving an expense or raising its amount can trigger an alert. A delete adds no budget event, because a total can only go down. An alert already sent is **not retracted** if the total later drops below the level, and is **not sent again** that month if it crosses again, because the rule is "once per category per month per level". The spend shown in `GET /categories` and the report is always computed live, so it is correct after edits either way.
- `GET /notifications` lists the user's alerts, newest first.

### 2.4 CSV import
- `POST /expenses/import` streams the upload to a temp file in 1 MB chunks, creates an `import_jobs` row and returns **202 + job_id**. The work runs as a FastAPI background task.
- The file is read with **pandas** `read_csv(chunksize=1000, dtype=str, keep_default_na=False)`: only one 1,000-row chunk is in memory, and every value stays the exact text (no float amounts). Each chunk is validated (date, amount > 0 with ≤ 2 decimals, supported currency, existing category, description length), good rows go in **one multi-row INSERT**, and the job counters are committed per chunk. `GET /imports/{job_id}` shows status, rows done/imported/duplicate/failed and the errors (row number + reason; first 1,000 stored).
- **Same file twice → no duplicates:** each valid row gets `import_key = category_id:row_number:amount:version:spent_on:file_name:user_id`. Before inserting a batch, one query finds which keys the user already has; those rows are skipped, counted in `rows_duplicate` and listed in the job's errors as `duplicate expense`. A unique constraint on `(user_id, import_key)` with `ON CONFLICT DO NOTHING` is the safety net when two imports of the same file run at once. Because the file name is part of the key, the same rows uploaded under a different file name are imported again. Imported rows also create budget-check events (one per category-month per batch), processed right after each batch is committed.
- Limits: 100,000 rows and 20 MB. Before any row is inserted, the file is checked: an empty file, missing columns or too many rows fail the job.

### 2.5 Receipts on S3: not done
Receipts are not part of this submission, for two reasons: I didn't have access to an AWS account (portal) to create a bucket and generate pre-signed S3 URLs, and I ran short of time within the 48 hours, so I put the time into finishing and testing the other parts. How I would build it:
- `POST /expenses/{id}/receipt/upload-url` returns a **pre-signed POST** (not PUT) with `content-length-range 1..5 MB` and a fixed `Content-Type` (jpg/png/pdf), for a server-generated key `receipts/{user_id}/{expense_id}/{uuid}.{ext}` in a private bucket.
- `POST …/receipt/confirm` checks the key belongs to the expense, `HEAD`s the object for its size, reads only the first bytes (Range GET) to check the magic number against the extension, deletes mismatches (e.g. `.exe` renamed `.png`), then saves the key on the expense.
- `GET …/receipt` returns a pre-signed download URL valid for 15 minutes.
- Deleting an expense or replacing a receipt deletes the old file through an outbox event, so a failed S3 call is not lost.

## Database indexes
| Index | Serves |
|---|---|
| `users.email` unique | Login lookup; duplicate email → 409 |
| `refresh_tokens` PK `jti`, index `user_id` | Refresh/logout lookup; "revoke all of this user's tokens" |
| `categories` unique `(user_id, name)` | Duplicate name → 409; name → id lookup for CSV import; listing by user |
| `expenses (user_id, spent_on, id)` | `GET /expenses` filter + `ORDER BY spent_on DESC, id DESC` + keyset `(spent_on, id) < …` (scanned backwards); date ranges in report/category spend |
| `expenses (user_id, category_id, spent_on)` | Category filter; category-month total in the budget check |
| `expenses` unique constraint `(user_id, import_key)` | CSV dedup: the "already imported?" lookup and `ON CONFLICT`; normal expenses have `import_key` NULL, which never clashes |
| `exchange_rates` PK `(currency, valid_on)` | "Latest rate on or before date" = one backwards index probe per rate lookup |
| `idempotency_keys` unique `(user_id, key)`, index `created_at` | Claiming a key (the race guard); deleting keys older than 24h |
| `outbox_events` partial `(id) WHERE processed_at IS NULL` | Finding pending events (backlog alarm, re-runs); stays small as processed rows pile up |
| `notifications` unique `(user_id, category_id, month, level)`, index `(user_id, created_at)` | Exactly-once alerts; `GET /notifications` |
| `import_jobs` index `user_id` | Job ownership check |

Exchange rates are seeded by the migration `seed exchange rates`: 90 days × PKR, AED, USD, EUR. The values are approximate and synthetic (USD = 1, AED pegged, small daily movement for PKR/EUR), not live market data.

## Deployment notes (AWS)
### 1. API
- **ECS Fargate** service in private subnets across 2 AZs.
  - **API:** 2 tasks minimum behind an ALB (health check `GET /health`), target-tracking autoscaling on CPU ~60% and ALB request count, up to e.g. 10 tasks.
  - Outbox events and CSV imports run as background tasks inside the API tasks, so there is no second service. Alarm on the number of pending outbox rows older than a few minutes (events whose task never ran or failed).
- ECS restarts failed tasks automatically (service desired count + ALB health checks).
- **Zero-downtime release:** rolling deployment with `minimumHealthyPercent=100`, `maximumPercent=200`. New tasks must pass the health check before old ones are drained (ALB deregistration delay ~30 s). On SIGTERM uvicorn finishes in-flight requests and their background tasks. Deployment circuit breaker with rollback enabled.

### 2. Database
- **RDS PostgreSQL**, Multi-AZ, in private DB subnets, no public access, encryption at rest, automated backups.
- **Security group `rds-sg`:** inbound **TCP 5432 only from `api-sg` and `migrate-sg`** (the one-off migration task). Nothing from the internet or other CIDRs.
- **Password:** stored in **Secrets Manager** (with rotation). The ECS task definition references it in `secrets` (`valueFrom` = secret ARN), so ECS injects `DATABASE_URL` at start. Only the task *execution* role has `secretsmanager:GetSecretValue` on that one ARN. Nothing is in the image or the repo.

### 3. Redis
- **ElastiCache for Redis** (replication group with a replica and automatic failover, in-transit encryption + AUTH token from Secrets Manager), in private subnets. `redis-sg` allows **TCP 6379 only from `api-sg`**.
- **If Redis goes down:**
  - **Rate limits:** login calls fail with a 500, so login **fails closed**: nobody can log in, but nobody can brute-force either. Refresh, expenses and everything else keep working because they don't use Redis. If availability mattered more, I'd catch Redis errors in the limiter and fail open with an alarm.
  - **Idempotency keys:** **not affected.** They are in PostgreSQL, deliberately, so a Redis outage can't create duplicate expenses.

### 4. S3
Receipts are not implemented (see 2.5); this is the setup I would use. Private bucket: Block Public Access on, SSE-S3 (or KMS), versioning off, CORS allowing `POST` from the app origins only (browser/mobile direct uploads), a bucket policy denying non-TLS requests, and a lifecycle rule for incomplete multipart uploads. The app uses the **ECS task role**, never access keys. The smallest policy the app needs:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReceiptObjects",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::expense-receipts/receipts/*"
    },
    {
      "Sid": "HeadReturns404NotFound403",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::expense-receipts",
      "Condition": {"StringLike": {"s3:prefix": "receipts/*"}}
    }
  ]
}
```
- `PutObject` is needed because pre-signed POSTs are signed with the role's permissions.
- `GetObject` covers HEAD, the 16-byte range read and pre-signed downloads; `DeleteObject` covers rejected and deleted receipts.
- `ListBucket` (prefix-limited) makes a HEAD on a missing key return 404 instead of 403, which the confirm step would rely on.

### 5. Migrations during a release
- Run `alembic upgrade head` as a **one-off ECS task** (same image, `migrate-sg`) in the pipeline **before** the service update, never on app startup. Three API tasks must not race to migrate.
- Every migration is **backward compatible** (expand/contract), so old tasks keep working while new ones roll out:
  - **Expand:** add tables and nullable columns or columns with defaults, and new indexes (`CREATE INDEX CONCURRENTLY` inside `op.get_context().autocommit_block()` on big tables). Deploy code that writes both shapes.
  - **Contract:** in a *later* release, once no old code is running, drop old columns or add `NOT NULL`.
- Renames are done as add + backfill + switch + drop, never `ALTER … RENAME` in one go.
- Set `lock_timeout` (e.g. 5 s) in migrations so a blocked `ALTER` fails fast instead of queueing every query behind it.

## Short answers
**1. Why a pre-signed upload URL instead of sending the file through the API? What can go wrong, and how do you guard against it?**

I didn't build receipts (see 2.5), so this is how I'd do it.

Sending a 5 MB photo through the API means our server holds those bytes in memory or on disk, keeps a connection busy for as long as a slow phone takes to upload, and we pay for the bandwidth twice (phone to API, API to S3). With a pre-signed URL the phone uploads straight to S3 and the API only hands out a short-lived, signed permission. Uploads then scale with S3, not with our containers.

What can go wrong:
- A leaked URL works for anyone until it expires. Keep the expiry short (about 10 minutes) and sign it for one key the server chooses (`receipts/{user_id}/{expense_id}/{uuid}`), so it can't be used to write anywhere else.
- A pre-signed PUT can't limit the file size. A pre-signed POST can, with a `content-length-range` condition, so S3 itself rejects anything over 5 MB.
- The client can lie about the type. `Content-Type` is just a header, so after the upload the API reads the first few bytes, checks the magic number (JPEG, PNG, PDF) and deletes anything that doesn't match. An `.exe` renamed to `.png` fails here.
- Clients upload and never confirm, leaving orphan files. A lifecycle rule on a `pending/` prefix deletes them after a day.
- Downloads: the bucket stays private and the app returns pre-signed GET links that expire after 15 minutes.

**2. `GET /expenses` gets slow at 20 million rows. How do you find the cause and fix it?**

I'd start by finding the exact slow query instead of guessing: `pg_stat_statements` (or the slow query log) shows which statement and which parameters are slow. Then I run that query with `EXPLAIN (ANALYZE, BUFFERS)`. `ANALYZE` actually executes it and shows real times and row counts; `BUFFERS` shows how many pages came from memory (`shared hit`) and how many from disk (`read`). What I look for: a `Seq Scan` on `expenses`, a `Sort` node (much worse if it says `external merge Disk`), `Rows Removed by Filter` far higher than the rows returned, and estimated rows far from actual rows, which means stale statistics and a quick `ANALYZE expenses`.

Usually the fix is the right index. The query is `WHERE user_id = ? … ORDER BY spent_on DESC, id DESC LIMIT 20`, so the index needs the equality column first and then the sort columns: `(user_id, spent_on, id)`. Postgres reads it backwards, already in the right order, and stops after 21 rows with no sort at all. That's the index this project has. A filter that's used all the time, like category, gets its own `(user_id, category_id, spent_on)`. Text search with `ILIKE '%coffee%'` can't use a normal b-tree index at all; it needs `pg_trgm` with a GIN index.

OFFSET pagination gets slow because the database still walks past every row it skips: `OFFSET 100000 LIMIT 20` reads 100,020 rows to return 20, so every page is slower than the one before. It also skips or repeats rows when new expenses are added while someone is paging. I use keyset pagination instead: the cursor holds the last `(spent_on, id)` and the next page is `WHERE (spent_on, id) < (cursor)`, one index seek no matter how deep you go.

N+1: if each expense in the response showed its category name by touching `expense.category`, 20 expenses would mean 21 queries. I return `category_id` only. Async SQLAlchemy refuses lazy loading anyway, so this would fail loudly instead of quietly being slow. If names were needed I'd use a join or `selectinload`, which is one extra query in total, not one per row.

**3. The API runs on 3 servers behind a load balancer. What breaks, and how did you make it safe?**

- **Tokens:** access tokens are JWTs, so any server can check them with the shared secret. Refresh tokens are rows in Postgres, and a refresh locks the row (`SELECT … FOR UPDATE`), so if the same refresh token hits two servers at once only one succeeds. A reused token revokes all of the user's tokens no matter which server sees it.
- **Rate limits:** a counter in each server's memory would give an attacker 5 attempts per server, 15 in total. The counter is in Redis, which all three servers share.
- **The worker:** there's no separate worker in my build. Budget checks run as background tasks on whichever server created the expense. If two ever handled the same event, the row lock with `SKIP LOCKED` gives it to one of them, and the unique constraint on notifications means the alert is created once anyway.
- **File uploads:** receipts would go straight to S3, so no server disk is involved. The CSV import is the weak spot: the file sits in a temp folder on the server that received it, and the background task runs there. If that server restarts mid-import, the job stays at `processing`. Uploading the file again is safe because rows already imported are reported as duplicates, not inserted twice.
- **Migrations:** they run once, as their own step in the deploy pipeline before the new code goes out, never on app startup. Otherwise three servers would race to migrate the same database.
- **Idempotency keys** are a Postgres unique constraint, so a retry works whichever server it lands on.

**4. The outbox worker is down for 2 hours and 500,000 events pile up. What happens when it comes back, and how do you protect the database?**

In my build there is no separate worker: each event is handled by a background task right after the request commits. The version of this problem I can actually hit is the API being killed (a deploy, a crash) after events were committed but before their tasks ran. Those events stay in `outbox_events` with `processed_at = NULL`. Nothing is lost, because they were written in the same transaction as the expenses.

When something comes back to drain 500,000 pending events (a worker, or the periodic re-run job listed under "not finished"), the risk is that it hammers the database trying to catch up. What I'd do:
- Work in fixed batches (about 100 events, one transaction each) and run one or two of them, not twenty. The number of workers is the throttle.
- Claim batches with `FOR UPDATE SKIP LOCKED` so workers never fight over the same rows. The partial index on pending rows keeps "give me the next batch" cheap even with millions of processed rows in the table.
- Coalesce. Most of those events say "check the budget for category X in month Y", and many are the same category-month. Running each distinct `(user, category, month)` once turns 500,000 SUM queries into a few thousand.
- Slow down when the database is busy (CPU or replication lag high) with a short pause between batches.
- Re-running is safe: the unique constraint on notifications means an event processed twice still creates one alert.
- Alert on the age of the oldest pending event, so an outage pages someone long before it reaches 500,000.

**5. One place where you chose something simple over something "perfect", and why.**

The CSV import. It runs as a FastAPI background task inside the API process and reads the upload from a temp file on that server's disk.

The "perfect" version would upload the file to S3, have a separate worker pick up the job with `SKIP LOCKED`, stream it back from S3, and resume from the last committed batch if the worker crashed. That's a lot more moving parts: another service to deploy and monitor, S3 in every test, and resume logic to get right.

What I have still covers what matters: the client gets a 202 straight away, the file is read 1,000 rows at a time so memory stays flat however big it is, each batch is one INSERT and one commit so progress shows up in `GET /imports/{id}`, and uploading the same file again doesn't create duplicates. The cost is that if the server dies mid-import, the job is stuck at `processing` and the user uploads the file again (rows already imported are skipped as duplicates). For an assessment and an early product that's a fair trade. I'd move it to a worker before real users depend on it.

## What is not finished
| Item | How I would build it |
|---|---|
| CSV import runs in the API process (see short answer 5) | Store the upload in S3, let a separate job runner claim `pending` jobs with `SKIP LOCKED`, stream the object, mark stuck `processing` jobs as resumable via `import_key` |
| Login rate limiter: `INCR` and `EXPIRE` are separate calls; parallel bad logins can all pass the check before any increment | One Redis pipeline / Lua script that does `INCR` + `EXPIRE NX` and returns the count, and decide from that atomic count |
| Receipts on S3 (2.5): no AWS account to test against, and not enough time (see [2.5](#25-receipts-on-s3-not-done)) | Pre-signed POST upload, confirm with size + magic-byte check, 15-minute download URL, outbox-driven file cleanup, and a lifecycle rule for unconfirmed uploads |
| Expenses dated before the oldest stored rate are left out of report totals (not guessed) | Fetch historical rates from a provider job, or fall back to the earliest rate and flag the row |
| Exchange rates are a synthetic 90-day seed | Daily job pulling rates from a provider into `exchange_rates` |
| Outbox events are processed only by the background task scheduled at commit; an event whose task never ran (process killed) or failed stays pending and nothing retries it automatically | A small periodic job that calls `process_event` for pending events older than a minute (`FOR UPDATE SKIP LOCKED` already makes parallel runs safe) |
| Idempotency keys that are never reused are not cleaned up | Periodic `DELETE … WHERE created_at < now() - interval '24 hours'` (the `created_at` index is already there) |
| Only create/list for categories (as specified) | `PATCH`/`DELETE /categories/{id}`; a budget change would emit a `budget.check` event |
| Alerts are not retracted when a total drops | Product decision; could add a "back under budget" notification |
| Postman collection was not run with newman in CI | `newman run postman/...` against a docker-compose stack in CI |

## Code review: bugs found and fixed
The code review was done with AI (see below). These are the real problems it found in the code during the project and how each was fixed.

| Where | Problem | Fix |
|---|---|---|
| Login | An email that doesn't exist crashed with a 500 (the code read `user.password_hash` on `None`) | Check `user is None or not await verify_password(...)` and return the same 401 as a wrong password, so the response doesn't reveal which emails exist |
| Login | A condition written with `and` instead of `or` let some wrong passwords through | Rewritten as the single check above; covered by tests |
| Login rate limit | The 5th failed attempt already returned 429; the requirement is that the **6th** does | Check the counter before the password check and increment only on failure; `test_sixth_failed_login_returns_429` |
| Refresh | The token checks were inverted (valid tokens rejected, revoked ones accepted), and the code used Django idioms (`timezone.now()`, `.update()`) and missed several `await`s | Fixed in place: decode, lock the row, detect reuse (revoke all), otherwise rotate |
| Redis | A sync Redis client blocked the event loop on every login | Switched to `redis.asyncio` |
| Redis | `ResponseError: unknown command HELLO` with Redis 5 | redis-py defaults to RESP3 (`HELLO 3`), which needs Redis 6+; the client now uses `protocol=2` |
| Passwords | argon2 hashing (deliberately slow) ran on the event loop and stalled other requests | Run it in a thread (`asyncio.to_thread`) |
| `PATCH /expenses` | `MissingGreenlet` crash after a version conflict: `db.rollback()` expired the loaded user, and async code can't lazy-load it again | Removed the rollback (the failed UPDATE changed nothing) |
| `DELETE /expenses` | The 204 route crashed on startup because of its return annotation | `response_model=None` on the route |
| Register | `base_currency` accepted any string | Restricted to `Literal["PKR", "AED", "USD", "EUR"]` |
| Budget/report totals | Converting with a Python `if` on a column (`if base_currency == Expense.currency`) is always false, because it compares a string with a SQL column object, not each row's value; expenses in the base currency then went through the rate lookup and could drop out of totals | SQL `CASE` (SQLAlchemy `case()`), evaluated per row by Postgres; `test_budget_total_converts_other_currencies` |
| CSV import | Re-uploading a file that had been edited even slightly imported every row again (the old key was based on a hash of the whole file) | New per-row key (`category_id:row_number:amount:version:spent_on:file_name:user_id`), a unique constraint, and duplicate rows listed in the job's errors |
| CSV import | A file with too many rows was read to the end, logging one error per extra row | The file is checked (header, empty, row count) before any row is inserted |
| Tests | Coverage missed code that runs inside SQLAlchemy's greenlets (87% shown) | `.coveragerc` with `concurrency = greenlet, thread` |

## AI usage
I used Claude Code (Anthropic) as a pair programmer throughout. I come from 4+ years of Django and this was my first FastAPI project. AI helped with the async scaffolding, implementing the Part 1/2 features, the test suite, reviewing my code (it caught several bugs) and drafting this README and the Postman collection. I set the project structure and conventions, designed the rate limiter and refresh-token flow, made the technical choices (e.g. pandas for CSV, returning errors instead of raising), and reviewed every change. I can walk through any part of the code.
