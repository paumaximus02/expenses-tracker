from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request

from expenses_tracker.dates import format_month_label, format_ytd_range_label, ytd_start_month
from expenses_tracker.models import Tenant

DEFAULT_INSIGHTS_API_BASE = "https://api.openai.com/v1"
DEFAULT_INSIGHTS_MODEL = "gpt-4o-mini"
INSIGHTS_LINE_CAP = 500
INSIGHTS_TIMEOUT_SECONDS = 120

INSIGHTS_SYSTEM_PROMPT = (
    "You review one household's income and expenses and name the specific charges "
    "and income entries they should look into. Use only the JSON. "
    "Do not invent transactions, merchants, dates, or amounts. "
    "Start with one sentence on whether the period left money over or fell short, using the totals. "
    "Then list only the items worth checking, most important first. "
    "Each item must name the merchant or income source, the date, and the amount, and say why it deserves a look. "
    "Use look_into for that: a charge that is large beside the other expenses, the same merchant more than once, "
    "two charges for the same merchant on the same day, or a line still marked Unassigned. "
    "Do not list every transaction. Skip a charge when it is small and similar to the other charges in its bucket. "
    "Do not give general budgeting advice. "
    "If nothing in the data is worth a closer look, say that in one sentence. "
    "excluded_expense_total is money left out of spending, not extra spending. "
    "look_into is already narrowed to the charges worth checking. "
    "If omitted_line_count is greater than zero, that many smaller transactions were left out of it."
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


def _line_ref(line: dict) -> dict:
    return {
        "date": line["date"],
        "name": line["name"],
        "amount": line["amount"],
        "bucket": line["bucket"],
        "person": line["person"],
    }


def _merchant_key(name: str) -> str:
    return " ".join(name.casefold().split())


def _display_name(group: list[dict]) -> str:
    cleaned = [line["name"].strip() for line in group if line["name"].strip()]
    if not cleaned:
        return ""
    counts: dict[str, int] = {}
    for name in cleaned:
        counts[name] = counts.get(name, 0) + 1
    return sorted(counts, key=lambda name: (-counts[name], name.casefold(), name))[0]


def _expense_share(amount: float, expense_total: float) -> float:
    if expense_total <= 0:
        return 0.0
    return round(100 * amount / expense_total, 1)


def build_look_into(lines: list[dict], expense_total: float) -> dict:
    """Group the charges that are worth a closer look.

    The model is asked to cite these groups instead of summarizing the totals.
    """
    expenses = [line for line in lines if line["type"] == "expense"]
    largest = sorted(expenses, key=lambda line: (-float(line["amount"]), line["date"], line["name"]))[:8]

    grouped: dict[str, list[dict]] = {}
    for line in expenses:
        grouped.setdefault(_merchant_key(line["name"]), []).append(line)
    repeats: list[dict] = []
    for group in grouped.values():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda line: (line["date"], -float(line["amount"]), line["name"]))
        total = _money(sum(float(line["amount"]) for line in ordered))
        repeats.append(
            {
                "name": _display_name(ordered),
                "count": len(ordered),
                "total": total,
                "share_of_expenses": _expense_share(total, expense_total),
                "charges": [_line_ref(line) for line in ordered[:6]],
            }
        )
    repeats.sort(key=lambda item: (-item["total"], -item["count"], item["name"]))

    by_day: dict[tuple[str, str], list[dict]] = {}
    for line in expenses:
        by_day.setdefault((_merchant_key(line["name"]), line["date"]), []).append(line)
    same_day: list[dict] = []
    for group in by_day.values():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda line: (-float(line["amount"]), line["name"]))
        same_day.append(
            {
                "date": ordered[0]["date"],
                "name": _display_name(ordered),
                "amounts": [_money(line["amount"]) for line in ordered],
                "person": ordered[0]["person"],
                "bucket": ordered[0]["bucket"],
            }
        )
    same_day.sort(key=lambda item: (item["date"], item["name"]))

    unassigned = [
        {
            "type": line["type"],
            "date": line["date"],
            "name": line["name"],
            "amount": line["amount"],
            "person": line["person"],
        }
        for line in lines
        if (line.get("bucket") or "Unassigned") == "Unassigned"
    ]

    return {
        "largest_expenses": [
            {**_line_ref(line), "share_of_expenses": _expense_share(float(line["amount"]), expense_total)}
            for line in largest
        ],
        "repeat_merchants": repeats[:8],
        "same_day_repeats": same_day[:8],
        "unassigned": unassigned[:12],
    }


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
        "look_into": build_look_into(lines, expense_total),
    }


def _redact_secrets(text: str, *secrets: str | None) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "••••")
    return redacted


def _provider_error_detail(body: str, *secrets: str | None) -> str:
    text = (body or "").strip()
    if not text:
        return ""
    detail = text
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, str) and error.strip():
            detail = error.strip()
        elif isinstance(error, dict):
            message = error.get("message") or error.get("error")
            if isinstance(message, str) and message.strip():
                detail = message.strip()
    detail = _redact_secrets(detail, *secrets)
    if len(detail) > 400:
        detail = detail[:400].rstrip() + "…"
    return detail


def _request_payload(payload: dict) -> dict:
    """Drop the raw transaction list. look_into already names what to cite."""
    return {key: value for key, value in payload.items() if key != "lines"}


def _uses_responses_api(api_base: str) -> bool:
    """Grok 4 returns its answer from /v1/responses. Chat completions stays empty."""
    return urllib.parse.urlparse(api_base).netloc.lower().endswith(".x.ai")


def _text_from_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part.strip())
        elif isinstance(part, dict):
            text = part.get("text") or part.get("content")
            if isinstance(text, str):
                parts.append(text.strip())
    return "\n".join(part for part in parts if part).strip()


def _insight_text(data: dict) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    output = data.get("output")
    if isinstance(output, list):
        messages: list[str] = []
        for item in output:
            if not isinstance(item, dict) or item.get("type") not in (None, "message"):
                continue
            text = _text_from_content(item.get("content"))
            if text:
                messages.append(text)
        if messages:
            return "\n".join(messages).strip()
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            return _text_from_content(message.get("content"))
    return ""


def request_insights(tenant: Tenant, payload: dict) -> str:
    api_key = clean_api_key(tenant.insights_api_key)
    if not api_key:
        raise InsightsError("Add an API key in Settings before requesting insights.")
    validate_api_key_for_base(tenant.insights_api_base or DEFAULT_INSIGHTS_API_BASE, api_key)

    base = normalize_api_base(tenant.insights_api_base or DEFAULT_INSIGHTS_API_BASE)
    model = (tenant.insights_model or DEFAULT_INSIGHTS_MODEL).strip() or DEFAULT_INSIGHTS_MODEL
    user_content = (
        "Name the specific charges and income entries to look into.\n"
        + json.dumps(_request_payload(payload), ensure_ascii=True)
    )
    # Reasoning models such as Grok reject temperature with HTTP 400.
    # Grok 4.7 defaults to a long reasoning pass and, on chat completions,
    # can finish with reasoning tokens and no answer text.
    if _uses_responses_api(base):
        url = f"{base}/responses"
        body = {
            "model": model,
            "reasoning": {"effort": "low"},
            "input": [
                {"role": "system", "content": INSIGHTS_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        }
    else:
        url = f"{base}/chat/completions"
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": INSIGHTS_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
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
        try:
            provider_body = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:
            provider_body = ""
        detail = _provider_error_detail(provider_body, api_key, tenant.insights_api_key)
        message = f"The insights service returned HTTP {exc.code}."
        if detail:
            message = f"{message} {detail}"
        raise InsightsError(message) from exc
    except TimeoutError as exc:
        raise InsightsError("The insights service timed out.") from exc
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
            raise InsightsError("The insights service timed out.") from exc
        raise InsightsError("Could not reach the insights service.") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InsightsError("The insights service returned an unexpected response.") from exc
    if not isinstance(data, dict):
        raise InsightsError("The insights service returned an unexpected response.")
    text = _insight_text(data)
    if not text:
        raise InsightsError("The insights service returned an empty briefing.")
    return text
