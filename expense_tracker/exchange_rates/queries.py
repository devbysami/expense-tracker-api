"""
SQL that converts expenses to the user's base currency. Shared by the monthly report,
GET /categories and the budget-alert worker, so the conversion rule lives in ONE place.

Rule: use the rate for the expense date; if that day is missing, the latest rate before it.
rate_to_usd = how many USD one unit is worth, so: amount_in_base = amount * rate(cur) / rate(base).
Expenses already in the base currency skip the lookup. An expense with no rate at all on or before
its date gets amount_base NULL (SUM ignores it) instead of a wrong number.

Params: :user_id, :date_from (inclusive), :date_to (exclusive).
"""

CONVERTED_EXPENSES = """
SELECT
    e.id,
    e.category_id,
    e.spent_on,
    CAST(date_trunc('month', e.spent_on) AS date) AS month,
    CASE
        WHEN e.currency = u.base_currency THEN e.amount
        ELSE e.amount * r_from.rate_to_usd / r_base.rate_to_usd
    END AS amount_base
FROM expenses e
JOIN users u ON u.id = e.user_id
-- "latest rate on or before spent_on": served by the exchange_rates PK (currency, valid_on), scanned backwards
LEFT JOIN LATERAL (
    SELECT rate_to_usd FROM exchange_rates
    WHERE currency = e.currency AND valid_on <= e.spent_on
    ORDER BY valid_on DESC LIMIT 1
) r_from ON e.currency <> u.base_currency
LEFT JOIN LATERAL (
    SELECT rate_to_usd FROM exchange_rates
    WHERE currency = u.base_currency AND valid_on <= e.spent_on
    ORDER BY valid_on DESC LIMIT 1
) r_base ON e.currency <> u.base_currency
WHERE e.user_id = :user_id AND e.spent_on >= :date_from AND e.spent_on < :date_to
"""
