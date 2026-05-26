from __future__ import annotations

import re

from expenses_tracker.db import Database
from expenses_tracker.models import ExpenseStatus, MatchType, MerchantGroupSuggestion, MerchantRule

KEYWORD_BUCKETS: list[tuple[str, list[str]]] = [
    ("Gas", ["gas", "fuel", "shell", "chevron", "exxon", "bp ", "costco gas"]),
    ("Groceries", ["grocery", "grocer", "trader joe", "whole foods", "safeway", "kroger", "costco"]),
    ("Dining", ["restaurant", "cafe", "coffee", "starbucks", "mcdonald", "doordash", "uber eats"]),
    ("Shopping", ["amazon", "target", "walmart", "best buy", "shop"]),
    ("Utilities", ["electric", "water", "internet", "comcast", "verizon", "at&t"]),
]

WEAK_PREFIXES = {
    "sq",
    "sp",
    "tst",
    "sqs",
    "paypal",
    "venmo",
    "amzn",
    "pos",
    "pp",
}
MIN_PREFIX_LENGTH = 4


def normalize_merchant(name: str) -> str:
    cleaned = name.lower().strip()
    cleaned = re.sub(r"[^\w\s&'-]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def suggest_bucket_from_keywords(merchant: str) -> tuple[str | None, str | None]:
    normalized = normalize_merchant(merchant)
    for bucket, keywords in KEYWORD_BUCKETS:
        for keyword in keywords:
            if keyword in normalized:
                return bucket, f"keyword match: '{keyword}'"
    return None, None


def merchant_group_key(merchant: str) -> tuple[str, str, str]:
    normalized = normalize_merchant(merchant)
    bucket_hint, reason = suggest_bucket_from_keywords(merchant)
    if bucket_hint:
        return f"keyword:{bucket_hint.lower()}", "keyword", reason

    first_token = normalized.split()[0] if normalized.split() else normalized
    if len(first_token) < MIN_PREFIX_LENGTH or first_token in WEAK_PREFIXES:
        return f"merchant:{normalized}", "merchant", "review individually"

    return f"prefix:{first_token}", "prefix", f"grouped by prefix '{first_token}'"


class BucketMatcher:
    def __init__(self, db: Database) -> None:
        self.db = db

    def reload_rules(self) -> list[MerchantRule]:
        return self.db.list_merchant_rules()

    def match(self, merchant: str, rules: list[MerchantRule] | None = None) -> MerchantRule | None:
        rules = rules if rules is not None else self.reload_rules()
        normalized = normalize_merchant(merchant)

        for rule in rules:
            pattern = normalize_merchant(rule.merchant_pattern)
            if rule.match_type == MatchType.EXACT and normalized == pattern:
                return rule
            if rule.match_type == MatchType.CONTAINS and pattern in normalized:
                return rule
        return None

    def suggest_bucket(self, merchant: str, rules: list[MerchantRule] | None = None) -> tuple[int | None, str | None]:
        rules = rules if rules is not None else self.reload_rules()
        matched_rule = self.match(merchant, rules)
        if matched_rule:
            return matched_rule.bucket_id, f"existing rule: {matched_rule.merchant_pattern} -> {matched_rule.bucket_name}"

        keyword_bucket, reason = suggest_bucket_from_keywords(merchant)
        if keyword_bucket:
            bucket = self.db.get_bucket_by_name(keyword_bucket)
            if bucket:
                return bucket.id, reason

        normalized = normalize_merchant(merchant)
        confirmed_expenses = [
            expense
            for expense in self.db.list_expenses(status=ExpenseStatus.CONFIRMED)
            if expense.bucket_id is not None
        ]
        for expense in confirmed_expenses:
            other = expense.merchant_normalized
            if other in normalized or normalized in other:
                return expense.bucket_id, f"similar merchant: {expense.merchant} -> {expense.bucket_name}"

        return None, None


def analyze_merchants(
    merchants: list[str],
    *,
    dismissed_keys: set[str] | None = None,
) -> list[MerchantGroupSuggestion]:
    dismissed_keys = dismissed_keys or set()
    groups: dict[str, dict[str, object]] = {}

    for merchant in merchants:
        group_key, group_type, reason = merchant_group_key(merchant)
        if group_key in dismissed_keys:
            continue
        entry = groups.setdefault(
            group_key,
            {"merchants": [], "group_type": group_type, "reason": reason},
        )
        entry["merchants"].append(merchant)

    suggestions: list[MerchantGroupSuggestion] = []
    for group_key, entry in sorted(groups.items(), key=lambda item: item[0].lower()):
        unique_members = sorted(set(entry["merchants"]), key=str.lower)
        group_type = str(entry["group_type"])
        reason = str(entry["reason"])

        bucket_name = None
        if group_type == "keyword":
            bucket_name, _ = suggest_bucket_from_keywords(unique_members[0])
        elif group_type == "prefix":
            bucket_name, keyword_reason = suggest_bucket_from_keywords(unique_members[0])
            if bucket_name:
                reason = keyword_reason
            else:
                bucket_name = None
                reason = (
                    f"{reason} — assign each merchant separately "
                    "(e.g. Square payments can be dining, health, etc.)"
                )
        else:
            bucket_name, keyword_reason = suggest_bucket_from_keywords(unique_members[0])
            if bucket_name:
                reason = keyword_reason
            else:
                reason = "Review individually and pick a bucket per merchant"

        suggestions.append(
            MerchantGroupSuggestion(
                merchants=unique_members,
                suggested_bucket=bucket_name,
                reason=reason,
                group_key=group_key,
                group_type=group_type,
            )
        )
    return suggestions
