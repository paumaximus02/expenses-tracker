from __future__ import annotations

import csv
import io

from expenses_tracker.dates import ytd_start_month

CSV_COLUMNS = (
    "type",
    "date",
    "month",
    "person",
    "name",
    "amount",
    "currency",
    "bucket",
    "excluded",
)


def _amount(value: float) -> str:
    return f"{float(value):.2f}"


def build_export_rows(
    db,
    *,
    month: str,
    view: str,
    person: str | None,
    scope: str,
) -> list[dict[str, str]]:
    """One row per income or expense for the report filters, or all history.

    Excluded expenses stay in the file with excluded=yes so a spreadsheet can
    filter them. Amounts stay positive; type distinguishes income from spending.
    """
    if scope == "all":
        expenses = db.list_expenses(card_holder=person)
        incomes = db.list_incomes(person=person)
    elif view == "ytd":
        month_from = ytd_start_month(month)
        expenses = db.list_expenses(month=month, month_from=month_from, card_holder=person)
        incomes = db.list_incomes(month=month, month_from=month_from, person=person)
    else:
        expenses = db.list_expenses(month=month, card_holder=person)
        incomes = db.list_incomes(month=month, person=person)

    rows: list[dict[str, str]] = []
    for expense in expenses:
        rows.append(
            {
                "type": "expense",
                "date": expense.transaction_date.isoformat(),
                "month": expense.transaction_date.strftime("%Y-%m"),
                "person": expense.card_holder or "",
                "name": expense.merchant,
                "amount": _amount(expense.amount),
                "currency": expense.currency,
                "bucket": expense.bucket_name or "Unassigned",
                "excluded": "yes" if expense.excluded_from_report else "no",
            }
        )
    for income in incomes:
        rows.append(
            {
                "type": "income",
                "date": income.received_date.isoformat(),
                "month": income.allocated_month,
                "person": income.person or "",
                "name": income.source,
                "amount": _amount(income.amount),
                "currency": income.currency,
                "bucket": income.bucket_name or "Unassigned",
                "excluded": "no",
            }
        )
    rows.sort(key=lambda row: (row["date"], row["type"], row["name"], row["amount"]))
    return rows


def render_csv(rows: list[dict[str, str]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def export_filename(*, month: str, view: str, scope: str) -> str:
    if scope == "all":
        return "expenses-all.csv"
    if view == "ytd" and len(month) >= 4:
        return f"expenses-{month[:4]}-ytd.csv"
    return f"expenses-{month}.csv"
