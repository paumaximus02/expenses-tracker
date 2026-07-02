from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum


class ExpenseStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    AUTO = "auto"


class MatchType(str, Enum):
    EXACT = "exact"
    CONTAINS = "contains"


class NotificationType(str, Enum):
    SYNC = "sync"
    INFO = "info"
    ERROR = "error"


class NotificationLevel(str, Enum):
    SUCCESS = "success"
    INFO = "info"
    ERROR = "error"


@dataclass
class Tenant:
    id: int
    name: str
    invite_code: str
    card_holders: dict[str, str]
    gmail_search_query: str
    gmail_token_json: str | None
    created_at: datetime
    income_gmail_search_query: str = ""


@dataclass
class User:
    id: int
    email: str
    tenant_id: int
    created_at: datetime
    notify_email: bool = False
    notify_sms: bool = False
    phone: str | None = None
    monthly_alert_threshold: float | None = None


@dataclass
class Bucket:
    id: int
    name: str
    parent_id: int | None = None
    parent_name: str | None = None
    exclude_from_report: bool = False

    @property
    def display_path(self) -> str:
        if self.parent_name:
            return f"{self.parent_name} › {self.name}"
        return self.name


@dataclass
class MerchantRule:
    id: int
    merchant_pattern: str
    bucket_id: int
    bucket_name: str
    match_type: MatchType
    priority: int
    confirmed_by_user: bool


@dataclass
class Expense:
    id: int
    gmail_message_id: str
    transaction_date: date
    merchant: str
    merchant_normalized: str
    amount: float
    currency: str
    bucket_id: int | None
    bucket_name: str | None
    suggested_bucket_id: int | None
    suggested_bucket_name: str | None
    status: ExpenseStatus
    email_subject: str | None
    email_from: str | None
    card_last_four: str | None = None
    card_holder: str | None = None
    exclude_from_report: bool = False
    bucket_excluded_from_report: bool = False

    @property
    def excluded_from_report(self) -> bool:
        return self.exclude_from_report or self.bucket_excluded_from_report


@dataclass
class ParsedEmail:
    gmail_message_id: str
    transaction_date: date
    merchant: str
    amount: float
    currency: str
    email_subject: str
    email_from: str
    body_text: str
    card_last_four: str | None = None
    card_holder: str | None = None


@dataclass
class IncomeBucket:
    id: int
    name: str


@dataclass
class IncomeRule:
    id: int
    match_text: str
    source_name: str
    bucket_id: int | None
    bucket_name: str | None
    person: str | None
    # "deposit" imports the email as income; "withdrawal" imports it as an
    # expense assigned to expense_bucket_id.
    direction: str = "deposit"
    expense_bucket_id: int | None = None
    expense_bucket_name: str | None = None


@dataclass
class Income:
    id: int
    gmail_message_id: str | None
    received_date: date
    allocated_month: str
    source: str
    amount: float
    currency: str
    bucket_id: int | None
    bucket_name: str | None
    person: str | None
    email_subject: str | None
    email_from: str | None


@dataclass
class ParsedIncomeEmail:
    gmail_message_id: str
    received_date: date
    amount: float
    currency: str
    email_subject: str
    email_from: str
    rule: IncomeRule
    description: str | None = None


@dataclass
class MerchantGroupSuggestion:
    merchants: list[str]
    suggested_bucket: str | None
    reason: str
    group_key: str
    group_type: str


@dataclass
class Notification:
    id: int
    type: NotificationType
    level: NotificationLevel
    title: str
    message: str
    created_at: datetime
    read_at: datetime | None = None
    import_count: int | None = None

    @property
    def is_read(self) -> bool:
        return self.read_at is not None
