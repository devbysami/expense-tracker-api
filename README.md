# Expense Tracker API

Async FastAPI + SQLAlchemy 2.0 + PostgreSQL + Redis expense tracker with multi-currency reports, budget alerts, CSV import and S3 receipts. **All four parts of the assessment are implemented; 69 tests pass with 97% coverage.** Known shortcuts and how I would finish them are listed in [What is not finished](#what-is-not-finished).

| Part | Status |
|---|---|
| 1. Core API (auth, categories, expenses, reports, migrations, seed) | Done |
| 2.1 Idempotency-Key · 2.2 ETag/If-Match · 2.3 Outbox + worker · 2.4 CSV import · 2.5 S3 receipts | Done |
| 3. Tests (pytest + httpx AsyncClient, real PostgreSQL, moto for S3) | 69 tests, 97% coverage |
| 4. Deployment notes and short answers | Below |
| Postman collection | [`postman/expense_tracker.postman_collection.json`](postman/expense_tracker.postman_collection.json) |

## Contents
- [Setup and run](#setup-and-run)
- [Tests and coverage](#tests-and-coverage)
- [Project layout](#project-layout)
- [Design decisions](#design-decisions)
- [Database indexes](#database-indexes)
- [Deployment notes (AWS)](#deployment-notes-aws)
- [Short answers](#short-answers)
- [What is not finished](#what-is-not-finished)
- [AI usage](#ai-usage)

## Setup and run
Requirements: Python 3.11+, PostgreSQL 12+, Redis 6+.
```bash
python3.11 -m venv _py && source _py/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in DB/Redis URLs and a JWT_SECRET (see comments in the file)
createdb expense_tracker && createdb expense_tracker_test

alembic upgrade head            # creates all tables + seeds 90 days of PKR/AED/USD/EUR rates
uvicorn main:app --app-dir expense_tracker --reload      # API on :8000, docs on /docs
python expense_tracker/worker.py                         # outbox worker (budget alerts, S3 cleanup)
```
- Config comes only from `.env` (git-ignored). `.env.example` lists every key.
- New migration: `alembic revision --autogenerate -m "..."`, review the file, then `alembic upgrade head`. Roll back one: `alembic downgrade -1`.
- Receipts locally: point `S3_ENDPOINT_URL` at MinIO/LocalStack and set `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in your shell. On AWS leave it empty; the IAM role supplies credentials.

## Tests and coverage
```bash
pytest                 # 69 passed
pytest --cov           # 97% total (config in .coveragerc)
```
- Real PostgreSQL test DB (`TEST_DATABASE_URL`); tables are created from the models once and **truncated after every test**.
- Redis is replaced by `fakeredis`; S3 by a local **moto** server (the real aioboto3 code path runs against it). No test calls AWS.
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
| Fake file (.exe renamed .png) rejected | `test_receipts.py::test_exe_renamed_to_png_is_rejected_and_deleted` |

## Project layout
Django-style apps: each has `models.py` (SQLAlchemy), `schemas.py` (Pydantic, like DRF serializers), `views.py` (async functions), `urls.py` (routes via `router.add_api_route`), `utils.py`.
```
expense_tracker/
  main.py            app, /api/v1 router, the 2 global error handlers, /health
  database.py        async engine + session dependency
  models.py          imports every model (for Alembic)
  worker.py          outbox worker process
  core/              security (argon2, JWT), dependencies (CurrentUser, DBSession), redis_ops,
                     storage (ALL S3 code), types (Money, Currency), utils (error_response), dates
  users/ categories/ expenses/ reports/ exchange_rates/ notifications/ imports/ receipts/ outbox/
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
Keyset on `(spent_on DESC, id DESC)`: the cursor is base64 of the last row's `(spent_on, id)` and the next page is `WHERE (spent_on, id) < (:d, :id)`. Inserting rows can't shift pages, unlike OFFSET. `id` is the tie-breaker for equal dates. `q` is `ILIKE` with `%`/`_` escaped.

### Monthly report (one SQL query)
`reports/views.py::MONTHLY_REPORT` is a single statement:
1. CTE `converted` (shared in `exchange_rates/queries.py`): each expense joined to the latest rate on or before its date via `LEFT JOIN LATERAL … ORDER BY valid_on DESC LIMIT 1`, for the expense currency and the base currency. `amount * rate(cur) / rate(base)`; same-currency rows skip the lookup.
2. `categories CROSS JOIN (this month, last month) LEFT JOIN converted … GROUP BY` → total per category per month (categories with no spend still appear).
3. `LAG(total) OVER (PARTITION BY category ORDER BY month)` → last month next to this month; budget %, change and change % computed in SQL.

The same conversion powers `GET /categories` (this month's spend vs budget) and the worker's budget check.

### 2.1 Idempotency-Key: DB unique constraint, no Redis lock
Table `idempotency_keys` with `UNIQUE (user_id, key)`, request hash, saved response. In the **same transaction** as the expense:
1. `INSERT … ON CONFLICT DO NOTHING RETURNING id` claims the key.
2. If a parallel request holds the key in an open transaction, Postgres makes this INSERT **wait** on the unique index until that transaction commits, then it conflicts → we read the saved response and replay it (or 422 if the body hash differs).
3. Otherwise create the expense, save the response on the key row, commit everything together.

Why only the DB: the key, the expense and the saved response commit atomically, so there is no window where the expense exists but the key doesn't (or the reverse). A Redis lock would add a second system that can't be committed together with Postgres, plus lock-expiry edge cases, and would still need the DB constraint as the real guarantee. Failed requests (e.g. category 404) roll back and don't burn the key. Keys expire after 24h: an expired key is deleted when reused, and the worker deletes old keys every 5 minutes.

### 2.2 No lost updates
`GET` returns `ETag: "<version>"`. `PATCH` requires `If-Match` (428 if missing, 400 if malformed) and runs one statement: `UPDATE … SET …, version = version + 1 WHERE id = :id AND user_id = :u AND version = :v RETURNING *`. No row → 404 if the expense isn't ours, else 412.

### 2.3 Budget alerts with the transactional outbox
- Creating an expense, or changing its amount/currency/category/date, adds an `outbox_events` row (`budget.check` for that category-month) **in the same transaction**. No alert is sent inside the request.
- `worker.py` claims batches with `FOR UPDATE SKIP LOCKED` (several workers never take the same row), handles each event in a savepoint, marks it processed. A failing event gets `attempts + 1` and `last_error` and is retried later (max 10) without blocking the others.
- The handler computes the category-month total in the base currency and inserts a notification for each crossed level (80, 100) with `ON CONFLICT DO NOTHING` on `UNIQUE (user_id, category_id, month, level)`. That constraint is what makes it **exactly once** per category per month per level, even if the worker crashes after the work and re-runs the event, or two workers run at once.
- **Edits and deletes:** an edit re-checks the (new) category-month, so moving an expense or raising its amount can trigger an alert. A delete adds no budget event, because a total can only go down. An alert already sent is **not retracted** if the total later drops below the level, and is **not sent again** that month if it crosses again, because the rule is "once per category per month per level". The spend shown in `GET /categories` and the report is always computed live, so it is correct after edits either way.
- `GET /notifications` lists the user's alerts, newest first.

### 2.4 CSV import
- `POST /expenses/import` streams the upload to a temp file in 1 MB chunks while hashing it (SHA-256), creates an `import_jobs` row and returns **202 + job_id**. The work runs as a FastAPI background task.
- The file is read with **pandas** `read_csv(chunksize=1000, dtype=str, keep_default_na=False)`: only one 1,000-row chunk is in memory, every value stays the exact text (no float amounts), and parsing runs in a thread (`asyncio.to_thread`). Each chunk is validated (date, amount > 0 with ≤ 2 decimals, supported currency, existing category, description length), good rows go in **one multi-row INSERT**, and the job counters are committed per chunk. `GET /imports/{job_id}` shows status, rows done/imported/duplicate/failed and the errors (row number + reason; first 1,000 stored).
- **Same file twice → no duplicates:** each row gets `import_key = <file sha256>:<row number>`, with a partial unique index on `(user_id, import_key)` and `ON CONFLICT DO NOTHING`. A second upload counts the rows as duplicates. Imported rows also create budget-check events.
- Limits: 100,000 rows (rows past that are reported as failed), 20 MB file.

### 2.5 Receipts on S3
- All S3 code is in `core/storage.py` (aioboto3). Keys: `receipts/{user_id}/{expense_id}/{uuid}.{ext}` in a private bucket.
- `upload-url` returns a **pre-signed POST** with `content-length-range 1..5 MB` and a fixed `Content-Type` (jpg/png/pdf only). S3 itself rejects bigger files, which a pre-signed PUT can't enforce.
- `confirm` checks that the key belongs to this expense, that the object exists (`HEAD`) and is ≤ 5 MB, and reads **only the first 16 bytes** (Range GET) to check the magic number (JPEG `FF D8 FF`, PNG `89 50 4E 47…`, PDF `%PDF-`) against the extension. A mismatch (e.g. `.exe` renamed `.png`) is deleted and rejected with 422. On success it saves `receipt_key` and bumps the version (new ETag). A replaced receipt's old file is queued for deletion.
- `GET …/receipt` returns a pre-signed download URL valid for 15 minutes.
- **Deleting an expense** writes a `receipt.delete` outbox event in the same transaction; the worker deletes the file. If S3 is down the event stays pending and is retried, so the file is cleaned up later, not forgotten (tested).

## Database indexes
| Index | Serves |
|---|---|
| `users.email` unique | Login lookup; duplicate email → 409 |
| `refresh_tokens` PK `jti`, index `user_id` | Refresh/logout lookup; "revoke all of this user's tokens" |
| `categories` unique `(user_id, name)` | Duplicate name → 409; name → id lookup for CSV import; listing by user |
| `expenses (user_id, spent_on, id)` | `GET /expenses` filter + `ORDER BY spent_on DESC, id DESC` + keyset `(spent_on, id) < …` (scanned backwards); date ranges in report/category spend |
| `expenses (user_id, category_id, spent_on)` | Category filter; category-month total in the budget worker |
| `expenses` partial unique `(user_id, import_key) WHERE import_key IS NOT NULL` | CSV dedup with `ON CONFLICT`; normal expenses don't take space in it |
| `exchange_rates` PK `(currency, valid_on)` | "Latest rate on or before date" = one backwards index probe per LATERAL lookup |
| `idempotency_keys` unique `(user_id, key)`, index `created_at` | Claiming a key (the race guard); deleting keys older than 24h |
| `outbox_events` partial `(id) WHERE processed_at IS NULL` | Worker's "oldest pending first"; stays small as processed rows pile up |
| `notifications` unique `(user_id, category_id, month, level)`, index `(user_id, created_at)` | Exactly-once alerts; `GET /notifications` |
| `import_jobs` index `user_id` | Job ownership check |

Exchange rates are seeded by the migration `seed exchange rates`: 90 days × PKR, AED, USD, EUR. The values are approximate and synthetic (USD = 1, AED pegged, small daily movement for PKR/EUR), not live market data.

## Deployment notes (AWS)
### 1. API and worker
- **ECS Fargate**, two services from the same image in private subnets across 2 AZs.
  - **API:** 2 tasks minimum behind an ALB (health check `GET /health`), target-tracking autoscaling on CPU ~60% and ALB request count, up to e.g. 10 tasks.
  - **Worker:** `python expense_tracker/worker.py`, 1 task (2 for availability; `SKIP LOCKED` + unique constraints make that safe). Scale on the outbox backlog (a CloudWatch metric of pending outbox rows).
- ECS restarts failed tasks automatically (service desired count + ALB health checks).
- **Zero-downtime release:** rolling deployment with `minimumHealthyPercent=100`, `maximumPercent=200`. New tasks must pass the health check before old ones are drained (ALB deregistration delay ~30 s). On SIGTERM uvicorn finishes in-flight requests, and the worker finishes its current batch. Deployment circuit breaker with rollback enabled.

### 2. Database
- **RDS PostgreSQL**, Multi-AZ, in private DB subnets, no public access, encryption at rest, automated backups.
- **Security group `rds-sg`:** inbound **TCP 5432 only from `api-sg`, `worker-sg` and `migrate-sg`** (the one-off migration task). Nothing from the internet or other CIDRs.
- **Password:** stored in **Secrets Manager** (with rotation). The ECS task definition references it in `secrets` (`valueFrom` = secret ARN), so ECS injects `DATABASE_URL` at start. Only the task *execution* role has `secretsmanager:GetSecretValue` on that one ARN. Nothing is in the image or the repo.

### 3. Redis
- **ElastiCache for Redis** (replication group with a replica and automatic failover, in-transit encryption + AUTH token from Secrets Manager), in private subnets. `redis-sg` allows **TCP 6379 only from `api-sg`**.
- **If Redis goes down:**
  - **Rate limits:** login calls fail with a 500, so login **fails closed**: nobody can log in, but nobody can brute-force either. Refresh, expenses and everything else keep working because they don't use Redis. If availability mattered more, I'd catch Redis errors in the limiter and fail open with an alarm.
  - **Idempotency keys:** **not affected.** They are in PostgreSQL, deliberately, so a Redis outage can't create duplicate expenses.

### 4. S3
Private bucket: Block Public Access on, SSE-S3 (or KMS), versioning off, CORS allowing `POST` from the app origins only (browser/mobile direct uploads), a bucket policy denying non-TLS requests, and a lifecycle rule for incomplete multipart uploads. The app uses the **ECS task role**, never access keys. The smallest policy the app needs:
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
- `ListBucket` (prefix-limited) makes a HEAD on a missing key return 404 instead of 403, which `confirm` relies on.

### 5. Migrations during a release
- Run `alembic upgrade head` as a **one-off ECS task** (same image, `migrate-sg`) in the pipeline **before** the service update, never on app startup. Three API tasks must not race to migrate.
- Every migration is **backward compatible** (expand/contract), so old tasks keep working while new ones roll out:
  - **Expand:** add tables and nullable columns or columns with defaults, and new indexes (`CREATE INDEX CONCURRENTLY` inside `op.get_context().autocommit_block()` on big tables). Deploy code that writes both shapes.
  - **Contract:** in a *later* release, once no old code is running, drop old columns or add `NOT NULL`.
- Renames are done as add + backfill + switch + drop, never `ALTER … RENAME` in one go.
- Set `lock_timeout` (e.g. 5 s) in migrations so a blocked `ALTER` fails fast instead of queueing every query behind it.

## Short answers
**1. Pre-signed upload URLs.**
- **Why:** the file goes straight from the phone to S3, so the API never holds megabytes in memory, doesn't tie up a worker for the whole upload on a slow mobile network, and doesn't pay for the bandwidth twice. S3 scales the uploads, not our containers.
- **Leaked URL:** it lets anyone upload until it expires. Mine expire after 10 minutes and are bound to one server-generated key under the user's prefix, so the client can't choose the path or overwrite other files.
- **Size and type:** a pre-signed PUT can't limit size, so I use a pre-signed POST with `content-length-range` and a fixed `Content-Type`.
- **Lying about the type:** `confirm` checks the real magic bytes and deletes mismatches.
- **Orphans:** uploads that are never confirmed should be removed by a lifecycle rule (see not finished).
- **Downloads:** download URLs expire in 15 minutes and the bucket stays private.

**2. `GET /expenses` slow at 20 million rows.**
- **Find it:** use `pg_stat_statements` / slow query logs to find the exact query and parameters, then `EXPLAIN (ANALYZE, BUFFERS)` it. Look for `Seq Scan` on expenses, a big `Sort` (especially `external merge` = spilling to disk), "Rows Removed by Filter" far above rows returned, and `shared read` (disk) vs `hit` buffers.
- **The right composite index:** equality column first, then the sort columns: `(user_id, spent_on DESC, id DESC)`, which I have as `(user_id, spent_on, id)` scanned backwards. Postgres then walks the index in order and stops after `LIMIT`, with no sort. A filter used constantly (category) gets its own `(user_id, category_id, spent_on, id)`. `q` with `ILIKE '%x%'` can't use a b-tree at all, so it needs a `pg_trgm` GIN index.
- **Why OFFSET gets slow:** `OFFSET 100000` still reads and throws away 100,000 rows, so page N costs O(N). Keyset (`WHERE (spent_on, id) < cursor`) is an index seek, so every page costs the same, and inserts don't shift pages.
- **N+1:** loading 50 expenses and then lazily touching `expense.category` fires 1 + 50 queries. I return `category_id` only (async SQLAlchemy forbids lazy loads anyway). If names were needed I'd use `selectinload`/a join for one extra query, not 50.
- Also check `ANALYZE` statistics are fresh and avoid `COUNT(*)` totals.

**3. Three servers behind a load balancer.**
- **Tokens:** JWTs are stateless and refresh-token state is in PostgreSQL with a row lock, so any server can verify or rotate them and parallel refreshes are serialized.
- **Rate limits:** an in-memory counter would give 5 attempts *per server*. Mine is in Redis, shared by all.
- **Worker:** it's a separate service. Even if 3 ran, `FOR UPDATE SKIP LOCKED` hands each event to one worker, and the unique constraint makes alerts exactly-once.
- **File uploads:** receipts never touch our disks (pre-signed S3). The weak spot is the CSV import: the temp file and the background task live on the server that received the upload, so if it restarts mid-import the job stalls (see not finished).
- **Migrations:** run once by the pipeline before rollout, never at startup, so three servers don't race.
- **Idempotency:** a Postgres constraint, so it works whichever server each retry hits.

**4. Worker down for 2 hours, 500,000 events piled up.**
- **When it comes back:** nothing is lost, since events were committed with the expenses. It starts draining oldest-first in batches of 100.
- **Re-running is safe:** handlers are idempotent, so re-running after another crash is harmless.
- **Protecting the database:**
  - *Bounded work:* fixed batch size, one transaction per batch, and a small number of workers (concurrency is the throttle, not "as fast as possible").
  - *Cheap pending scan:* the partial index on pending rows keeps "next batch" cheap even with millions of processed rows.
  - *Coalescing:* most of the 500k are `budget.check` for the same few thousand category-months. I'd coalesce them by claiming a batch and running each distinct `(user, category, month)` once, which turns 500k SUM queries into thousands.
  - *Throttling:* add a short sleep between batches when DB CPU or replication lag is high, and alert on outbox backlog age so 2 hours of downtime pages someone.
- **Housekeeping:** processed rows can be deleted or partitioned by day.

**5. Simple over "perfect".**
The CSV import runs as a FastAPI `BackgroundTask` in the API process, reading a temp file on that server's disk. "Perfect" would be: upload the CSV to S3, have the worker claim the job with `SKIP LOCKED`, stream it from S3, and resume after a crash. I chose the simple version because it is a fraction of the code, easy to test deterministically, and still meets the hard requirements:
- 202 immediately, with the work after the response.
- The event loop stays free: chunked pandas parsing in a thread, async batched INSERTs.
- Bounded memory.
- No duplicates on re-upload.

The cost: if that API task dies mid-import, the job stays `processing` and the temp file is gone. Re-uploading the same file is safe thanks to the per-row `import_key`, so the user can recover. I'd switch to the worker version before relying on imports in production.

## What is not finished
| Item | How I would build it |
|---|---|
| CSV import runs in the API process (see short answer 5) | Store the upload in S3, let the worker claim `pending` jobs with `SKIP LOCKED`, stream the object, mark stuck `processing` jobs as resumable via `import_key` |
| Login rate limiter: `INCR` and `EXPIRE` are separate calls; parallel bad logins can all pass the check before any increment | One Redis pipeline / Lua script that does `INCR` + `EXPIRE NX` and returns the count, and decide from that atomic count |
| Unconfirmed receipt uploads stay in S3 | Upload to a `pending/` prefix with a 1-day lifecycle expiry and copy to `receipts/` on confirm, or tag on confirm + lifecycle on untagged objects |
| `q` search uses `ILIKE '%x%'` (no index) | `pg_trgm` GIN index on `description`, or full-text search with a `tsvector` column |
| Expenses dated before the oldest stored rate are left out of report totals (not guessed) | Fetch historical rates from a provider job, or fall back to the earliest rate and flag the row |
| Exchange rates are a synthetic 90-day seed | Daily job pulling rates from a provider into `exchange_rates` |
| Worker polls the outbox every second | `LISTEN/NOTIFY` or a Redis wake-up signal on commit to cut latency, keeping polling as the fallback |
| Only create/list for categories (as specified) | `PATCH`/`DELETE /categories/{id}`; a budget change would emit a `budget.check` event |
| Alerts are not retracted when a total drops | Product decision; could add a "back under budget" notification |
| Postman collection was not run with newman in CI | `newman run postman/...` against a docker-compose stack in CI |
| Migrations folder contains an empty `add_phone_to_users` revision | Remove it and point the next revision's `down_revision` at `84041800c856` |

## AI usage
I used Claude Code (Anthropic) as a pair programmer throughout. I come from 4+ years of Django and this was my first FastAPI project. AI helped with the async scaffolding, implementing the Part 1/2 features, the test suite, reviewing my code (it caught several bugs) and drafting this README and the Postman collection. I set the project structure and conventions, designed the rate limiter and refresh-token flow, made the technical choices (e.g. pandas for CSV, returning errors instead of raising), and reviewed every change. I can walk through any part of the code.
