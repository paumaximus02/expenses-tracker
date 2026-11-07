from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from expenses_tracker.dates import format_month_label, format_ytd_range_label, ytd_start_month
from expenses_tracker.models import Tenant

DEFAULT_INSIGHTS_API_BASE = "https://api.openai.com/v1"
DEFAULT_INSIGHTS_MODEL = "gpt-4o-mini"
INSIGHTS_LINE_CAP = 500
INSIGHTS_TIMEOUT_SECONDS = 30

INSIGHTS_SYSTEM_PROMPT = (
    "You write a short household money briefing from the JSON the user sends. "
    "Cover whether the period left money over or fell short, where spending went, "
    "standout merchants or income sources, and anything that looks unusual. "
    "Write plain sentences. Do not invent transactions. "
    "excluded_expense_total is reimbursements or other amounts left out of spending, "
    "not extra spending. If omitted_line_count is greater than zero, the lines list "
    "is only the largest amounts and the rest were left out."
)


class InsightsError(Exception):
    """The insights request could not be completed."""


def clean_api_key(raw: str | None) -> str:
    """Normalize a pasted key without changing the secret itself."""
    key = (raw or "").strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in {'"', "'"}:
        key = key[1:-1].strip()
    return "".join(key.split())


def validate_api_key_for_base(api_base: str, api_key: str) -> None:
    host = urllib.parse.urlparse(api_base).netloc.lower()
    if host.endswith(".x.ai") and not api_key.startswith("xai-"):
        raise ValueError(
            "That value does not look like an xAI API key. "
            "In console.x.ai, copy the key that starts with xai-."
        )


def mask_api_key(api_key: str | None) -> str | None:
    if not api_key:
        return None
    if len(api_key) < 8:
        return "••••"
    return f"••••{api_key[-4:]}"


def normalize_api_base(raw: str) -> str:
    value = (raw or "").strip().rstrip("/")
    if not value:
        return DEFAULT_INSIGHTS_API_BASE
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("API base URL must start with http:// or https://.")
    return value


def _money(value: float) -> float:
    return round(float(value), 2)


def limit_insight_lines(lines: list[dict], cap: int = INSIGHTS_LINE_CAP) -> tuple[list[dict], int]:
    if len(lines) <= cap:
        return lines, 0
    ranked = sorted(
        lines,
        key=lambda line: (-float(line["amount"]), line["date"], line["type"], line["name"]),
    )
    return ranked[:cap], len(lines) - cap


def build_insight_payload(
    db,
    *,
    month: str,
    view: str,
    person: str | None,
    line_cap: int = INSIGHTS_LINE_CAP,
) -> dict:
    """Build the JSON sent to the model for the current report filters.

    Lines are the transactions the report counts. Email subject, sender, and
    body are never included. Reimbursements stay out of the line list.
    """
    if view == "ytd":
        month_from = ytd_start_month(month)
        expenses = db.list_expenses(month=month, month_from=month_from, card_holder=person)
        incomes = db.list_incomes(month=month, month_from=month_from, person=person)
        expense_totals = db.ytd_expense_totals(month, card_holder=person)
        income_totals = db.ytd_income_totals(month, person=person)
        excluded_totals = db.ytd_excluded_totals(month, card_holder=person)
        period = format_ytd_range_label(month)
    else:
        expenses = db.list_expenses(month=month, card_holder=person)
        incomes = db.list_incomes(month=month, person=person)
        expense_totals = db.monthly_totals(month, card_holder=person)
        income_totals = db.monthly_income_totals(month, person=person)
        excluded_totals = db.monthly_excluded_totals(month, card_holder=person)
        period = format_month_label(month)

    lines: list[dict] = []
    for expense in expenses:
        if expense.excluded_from_report:
            continue
        lines.append(
            {
                "type": "expense",
                "date": expense.transaction_date.isoformat(),
                "name": expense.merchant,
                "amount": _money(expense.amount),
                "bucket": expense.bucket_name or "Unassigned",
                "person": expense.card_holder or "",
            }
        )
    for income in incomes:
        lines.append(
            {
                "type": "income",
                "date": income.received_date.isoformat(),
                "name": income.source,
                "amount": _money(income.amount),
                "bucket": income.bucket_name or "Unassigned",
                "person": income.person or "",
            }
        )

    kept_lines, omitted = limit_insight_lines(lines, line_cap)
    income_total = _money(sum(row[2] for row in income_totals))
    expense_total = _money(sum(row[2] for row in expense_totals))
    return {
        "period": period,
        "view": "ytd" if view == "ytd" else "month",
        "person": person or "Everyone",
        "income_total": income_total,
        "expense_total": expense_total,
        "net": _money(income_total - expense_total),
        "excluded_expense_total": _money(sum(row[1] for row in excluded_totals)),
        "expense_buckets": [
            {"name": name, "total": _money(total), "count": count}
            for _bucket_id, name, total, count in expense_totals
            if total > 0
        ],
        "income_buckets": [
            {"name": name, "total": _money(total), "count": count}
            for _bucket_id, name, total, count in income_totals
            if total > 0
        ],
        "lines": kept_lines,
        "omitted_line_count": omitted,
    }


def _provider_error_detail(raw: bytes, api_key: str) -> str:
    """Short provider message safe to show in the app."""
    text = raw.decode("utf-8", errors="replace").strip()
    message = ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        message = text
    else:
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            message = str(error.get("message") or "")
        elif isinstance(error, str):
            message = error
    message = " ".join(message.split())
    if api_key:
        message = message.replace(api_key, "")
    if len(message) > 180:
        message = message[:177].rstrip() + "..."
    return message.strip()


def request_insights(tenant: Tenant, payload: dict) -> str:
    api_key = clean_api_key(tenant.insights_api_key)
    if not api_key:
        raise InsightsError("Add an API key in Settings before requesting insights.")
    validate_api_key_for_base(tenant.insights_api_base or DEFAULT_INSIGHTS_API_BASE, api_key)

    base = normalize_api_base(tenant.insights_api_base or DEFAULT_INSIGHTS_API_BASE)
    model = (tenant.insights_model or DEFAULT_INSIGHTS_MODEL).strip() or DEFAULT_INSIGHTS_MODEL
    url = f"{base}/chat/completions"
    # Reasoning models such as Grok reject temperature with HTTP 400.
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": INSIGHTS_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "Brief this period.\n" + json.dumps(payload, ensure_ascii=True),
            },
        ],
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=INSIGHTS_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = _provider_error_detail(exc.read(), api_key)
        except Exception:
            detail = ""
        message = f"The insights service returned HTTP {exc.code}."
        if detail:
            message = f"{message} {detail}"
        raise InsightsError(message) from exc
    except TimeoutError as exc:
        raise InsightsError("The insights service timed out.") from exc
    except urllib.error.URLError as exc:
        raise InsightsError("Could not reach the insights service.") from exc

    try:
        data = json.loads(raw)
        text = data["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise InsightsError("The insights service returned an unexpected response.") from exc
    if not isinstance(text, str) or not text.strip():
        raise InsightsError("The insights service returned an empty briefing.")
    return text.strip()
