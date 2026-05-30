from __future__ import annotations

import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from expenses_tracker.buckets import format_bucket_path
from expenses_tracker.display import format_merchant
from expenses_tracker.models import (
    Bucket,
    Expense,
    ExpenseStatus,
    MatchType,
    MerchantRule,
    Notification,
    NotificationLevel,
    NotificationType,
    Tenant,
    User,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS buckets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS merchant_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant_pattern TEXT NOT NULL,
    bucket_id INTEGER NOT NULL REFERENCES buckets(id) ON DELETE CASCADE,
    match_type TEXT NOT NULL DEFAULT 'exact',
    priority INTEGER NOT NULL DEFAULT 0,
    confirmed_by_user INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(merchant_pattern, match_type)
);

CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gmail_message_id TEXT NOT NULL UNIQUE,
    transaction_date TEXT NOT NULL,
    merchant TEXT NOT NULL,
    merchant_normalized TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    bucket_id INTEGER REFERENCES buckets(id),
    suggested_bucket_id INTEGER REFERENCES buckets(id),
    status TEXT NOT NULL DEFAULT 'pending',
    email_subject TEXT,
    email_from TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_expenses_status ON expenses(status);
CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(transaction_date);
CREATE INDEX IF NOT EXISTS idx_merchant_rules_priority ON merchant_rules(priority DESC);
"""


class Database:
    def __init__(self, path: Path, tenant_id: int | None = None) -> None:
        self.path = path
        self.tenant_id = tenant_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _require_tenant(self) -> int:
        if self.tenant_id is None:
            raise RuntimeError("Operation requires a tenant-scoped database connection")
        return self.tenant_id

    def _bind_tenant(self, params: list[object] | None = None) -> list[object]:
        return [*(params or []), self._require_tenant()]

    def _init_schema(self) -> None:
        with self.connection() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(expenses)")}
        if "card_last_four" not in columns:
            conn.execute("ALTER TABLE expenses ADD COLUMN card_last_four TEXT")
        if "card_holder" not in columns:
            conn.execute("ALTER TABLE expenses ADD COLUMN card_holder TEXT")
        bucket_columns = {row[1] for row in conn.execute("PRAGMA table_info(buckets)")}
        if "exclude_from_report" not in bucket_columns:
            conn.execute(
                "ALTER TABLE buckets ADD COLUMN exclude_from_report INTEGER NOT NULL DEFAULT 0"
            )
        if "exclude_from_report" not in columns:
            conn.execute(
                "ALTER TABLE expenses ADD COLUMN exclude_from_report INTEGER NOT NULL DEFAULT 0"
            )
        if "sync_notification_id" not in columns:
            conn.execute("ALTER TABLE expenses ADD COLUMN sync_notification_id INTEGER")
        self._migrate_tenancy(conn)
        self._migrate_buckets_hierarchy(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dismissed_merchant_groups (
                group_key TEXT PRIMARY KEY,
                dismissed_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                level TEXT NOT NULL DEFAULT 'info',
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                read_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_notifications_created ON notifications(created_at DESC)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        self._migrate_users_tenant(conn)
        self._migrate_user_notification_prefs(conn)
        for table in ("notifications",):
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
                (table,),
            ).fetchone():
                columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                if "tenant_id" not in columns:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN tenant_id INTEGER REFERENCES tenants(id)"
                    )
                    conn.execute(
                        f"UPDATE {table} SET tenant_id = 1 WHERE tenant_id IS NULL"
                    )
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'idx_expenses_tenant_gmail'"
        ).fetchone() is None:
            self._ensure_tenant_unique_indexes(conn)
        if conn.execute(
            "SELECT value FROM sync_state WHERE tenant_id = ? AND key = ?",
            (1, "merchant_rules_deduped"),
        ).fetchone() is None:
            self._dedupe_merchant_rules(conn, tenant_id=1)
            conn.execute(
                """
                INSERT INTO sync_state (tenant_id, key, value) VALUES (?, ?, ?)
                ON CONFLICT(tenant_id, key) DO UPDATE SET value = excluded.value
                """,
                (1, "merchant_rules_deduped", "true"),
            )

    def _dedupe_merchant_rules(self, conn: sqlite3.Connection, *, tenant_id: int) -> None:
        rows = conn.execute(
            """
            SELECT id, merchant_pattern, bucket_id, match_type, priority, confirmed_by_user
            FROM merchant_rules
            WHERE tenant_id = ?
            """,
            (tenant_id,),
        ).fetchall()

        groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in rows:
            canonical = format_merchant(row["merchant_pattern"])
            if not canonical:
                continue
            key = (canonical.lower(), row["match_type"])
            groups.setdefault(key, []).append(row)

        for members in groups.values():
            canonical = format_merchant(members[0]["merchant_pattern"])
            ranked = sorted(
                members,
                key=lambda row: (
                    -int(row["confirmed_by_user"]),
                    -row["priority"],
                    row["id"],
                ),
            )
            keeper = ranked[0]
            for duplicate in ranked[1:]:
                conn.execute("DELETE FROM merchant_rules WHERE id = ?", (duplicate["id"],))
            conn.execute(
                """
                UPDATE merchant_rules
                SET merchant_pattern = ?,
                    bucket_id = ?,
                    priority = ?,
                    confirmed_by_user = ?
                WHERE id = ?
                """,
                (
                    canonical,
                    keeper["bucket_id"],
                    len(canonical),
                    int(keeper["confirmed_by_user"]),
                    keeper["id"],
                ),
            )

    def _migrate_buckets_hierarchy(self, conn: sqlite3.Connection) -> None:
        bucket_columns = {row[1] for row in conn.execute("PRAGMA table_info(buckets)")}
        if "parent_id" in bucket_columns:
            self._dedupe_buckets(conn)
            return

        has_exclude = "exclude_from_report" in bucket_columns
        has_tenant = "tenant_id" in bucket_columns
        exclude_select = "exclude_from_report" if has_exclude else "0"
        exclude_insert = "exclude_from_report," if has_exclude else ""
        tenant_select = "tenant_id" if has_tenant else "1"
        tenant_insert = "tenant_id," if has_tenant else ""
        tenant_column = (
            "tenant_id INTEGER NOT NULL DEFAULT 1 REFERENCES tenants(id),"
            if has_tenant
            else ""
        )
        exclude_column = (
            "exclude_from_report INTEGER NOT NULL DEFAULT 0,"
            if has_exclude or has_tenant
            else ""
        )
        conn.executescript(
            f"""
            PRAGMA foreign_keys=OFF;
            CREATE TABLE buckets_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                parent_id INTEGER REFERENCES buckets_new(id) ON DELETE RESTRICT,
                {exclude_column}
                {tenant_column}
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO buckets_new (id, name, parent_id, {exclude_insert} {tenant_insert} created_at)
                SELECT id, name, NULL, {exclude_select}, {tenant_select}, created_at FROM buckets;
            DROP TABLE buckets;
            ALTER TABLE buckets_new RENAME TO buckets;
            CREATE INDEX idx_buckets_parent ON buckets(parent_id);
            PRAGMA foreign_keys=ON;
            """
        )
        self._dedupe_buckets(conn)

    def _migrate_tenancy(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tenants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                invite_code TEXT NOT NULL UNIQUE,
                card_holders TEXT NOT NULL DEFAULT '{}',
                gmail_search_query TEXT NOT NULL DEFAULT '',
                gmail_token_json TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        if conn.execute("SELECT 1 FROM tenants LIMIT 1").fetchone() is None:
            token_json = None
            token_path = Path(os.getenv("GMAIL_TOKEN_PATH", "token.json"))
            if token_path.exists():
                token_json = token_path.read_text(encoding="utf-8")
            from expenses_tracker.config import _parse_card_holders

            holders = _parse_card_holders(os.getenv("CARD_HOLDERS", "4149:Juan,1201:Debora"))
            gmail_query = os.getenv(
                "GMAIL_SEARCH_QUERY",
                "from:(citibank.com OR citi.com) after:2026/04/01 subject:transaction",
            )
            invite_code = secrets.token_urlsafe(6)[:8].upper()
            conn.execute(
                """
                INSERT INTO tenants (name, invite_code, card_holders, gmail_search_query, gmail_token_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "Default household",
                    invite_code,
                    json.dumps(holders),
                    gmail_query,
                    token_json,
                ),
            )

        default_tenant_id = 1
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone():
            user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
            if "tenant_id" not in user_columns:
                conn.execute("ALTER TABLE users ADD COLUMN tenant_id INTEGER REFERENCES tenants(id)")
                conn.execute(
                    "UPDATE users SET tenant_id = ? WHERE tenant_id IS NULL",
                    (default_tenant_id,),
                )

        for table in ("buckets", "merchant_rules", "expenses", "notifications"):
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
                (table,),
            ).fetchone():
                continue
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if "tenant_id" not in columns:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN tenant_id INTEGER REFERENCES tenants(id)"
                )
                conn.execute(
                    f"UPDATE {table} SET tenant_id = ? WHERE tenant_id IS NULL",
                    (default_tenant_id,),
                )

        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dismissed_merchant_groups'"
        ).fetchone():
            dismissed_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(dismissed_merchant_groups)")
            }
            if "tenant_id" not in dismissed_columns:
                conn.executescript(
                    """
                    CREATE TABLE dismissed_merchant_groups_new (
                        tenant_id INTEGER NOT NULL REFERENCES tenants(id),
                        group_key TEXT NOT NULL,
                        dismissed_at TEXT NOT NULL DEFAULT (datetime('now')),
                        PRIMARY KEY (tenant_id, group_key)
                    );
                    INSERT INTO dismissed_merchant_groups_new (tenant_id, group_key, dismissed_at)
                        SELECT 1, group_key, dismissed_at FROM dismissed_merchant_groups;
                    DROP TABLE dismissed_merchant_groups;
                    ALTER TABLE dismissed_merchant_groups_new RENAME TO dismissed_merchant_groups;
                    """
                )

        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_state'"
        ).fetchone():
            sync_columns = {row[1] for row in conn.execute("PRAGMA table_info(sync_state)")}
            if "tenant_id" not in sync_columns:
                conn.executescript(
                    """
                    CREATE TABLE sync_state_new (
                        tenant_id INTEGER NOT NULL REFERENCES tenants(id),
                        key TEXT NOT NULL,
                        value TEXT NOT NULL,
                        PRIMARY KEY (tenant_id, key)
                    );
                    INSERT INTO sync_state_new (tenant_id, key, value)
                        SELECT 1, key, value FROM sync_state;
                    DROP TABLE sync_state;
                    ALTER TABLE sync_state_new RENAME TO sync_state;
                    """
                )

        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='expenses'"
        ).fetchone() and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'idx_expenses_tenant_gmail'"
        ).fetchone() is None and "parent_id" in {
            row[1] for row in conn.execute("PRAGMA table_info(buckets)")
        }:
            self._ensure_tenant_unique_indexes(conn)

    def _migrate_users_tenant(self, conn: sqlite3.Connection) -> None:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone():
            return
        user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        if "tenant_id" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN tenant_id INTEGER REFERENCES tenants(id)")
            conn.execute("UPDATE users SET tenant_id = 1 WHERE tenant_id IS NULL")

    def _migrate_user_notification_prefs(self, conn: sqlite3.Connection) -> None:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone():
            return
        user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        if "notify_email" not in user_columns:
            conn.execute(
                "ALTER TABLE users ADD COLUMN notify_email INTEGER NOT NULL DEFAULT 0"
            )
        if "notify_sms" not in user_columns:
            conn.execute(
                "ALTER TABLE users ADD COLUMN notify_sms INTEGER NOT NULL DEFAULT 0"
            )
        if "phone" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN phone TEXT")
        if "monthly_alert_threshold" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN monthly_alert_threshold REAL")

    def _ensure_tenant_unique_indexes(self, conn: sqlite3.Connection) -> None:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'idx_expenses_tenant_gmail'"
        ).fetchone():
            return

        conn.executescript(
            """
            PRAGMA foreign_keys=OFF;
            CREATE TABLE expenses_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL REFERENCES tenants(id),
                gmail_message_id TEXT NOT NULL,
                transaction_date TEXT NOT NULL,
                merchant TEXT NOT NULL,
                merchant_normalized TEXT NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT 'USD',
                bucket_id INTEGER REFERENCES buckets(id),
                suggested_bucket_id INTEGER REFERENCES buckets(id),
                status TEXT NOT NULL DEFAULT 'pending',
                email_subject TEXT,
                email_from TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                card_last_four TEXT,
                card_holder TEXT,
                exclude_from_report INTEGER NOT NULL DEFAULT 0,
                sync_notification_id INTEGER,
                UNIQUE(tenant_id, gmail_message_id)
            );
            INSERT INTO expenses_new (
                id, tenant_id, gmail_message_id, transaction_date, merchant, merchant_normalized,
                amount, currency, bucket_id, suggested_bucket_id, status, email_subject, email_from,
                created_at, card_last_four, card_holder, exclude_from_report, sync_notification_id
            )
            SELECT
                id, COALESCE(tenant_id, 1), gmail_message_id, transaction_date, merchant,
                merchant_normalized, amount, currency, bucket_id, suggested_bucket_id, status,
                email_subject, email_from, created_at, card_last_four, card_holder,
                exclude_from_report, sync_notification_id
            FROM expenses;
            DROP TABLE expenses;
            ALTER TABLE expenses_new RENAME TO expenses;
            CREATE INDEX IF NOT EXISTS idx_expenses_status ON expenses(tenant_id, status);
            CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(tenant_id, transaction_date);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_expenses_tenant_gmail
                ON expenses(tenant_id, gmail_message_id);

            CREATE TABLE merchant_rules_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL REFERENCES tenants(id),
                merchant_pattern TEXT NOT NULL,
                bucket_id INTEGER NOT NULL REFERENCES buckets(id) ON DELETE CASCADE,
                match_type TEXT NOT NULL DEFAULT 'exact',
                priority INTEGER NOT NULL DEFAULT 0,
                confirmed_by_user INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(tenant_id, merchant_pattern, match_type)
            );
            INSERT INTO merchant_rules_new (
                id, tenant_id, merchant_pattern, bucket_id, match_type, priority,
                confirmed_by_user, created_at
            )
            SELECT
                id, COALESCE(tenant_id, 1), merchant_pattern, bucket_id, match_type, priority,
                confirmed_by_user, created_at
            FROM merchant_rules;
            DROP TABLE merchant_rules;
            ALTER TABLE merchant_rules_new RENAME TO merchant_rules;
            CREATE INDEX IF NOT EXISTS idx_merchant_rules_priority
                ON merchant_rules(tenant_id, priority DESC);

            DROP INDEX IF EXISTS idx_buckets_parent_name;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_buckets_tenant_parent_name
                ON buckets(tenant_id, COALESCE(parent_id, -1), name);
            PRAGMA foreign_keys=ON;
            """
        )

    def _dedupe_buckets(self, conn: sqlite3.Connection) -> None:
        bucket_columns = {row[1] for row in conn.execute("PRAGMA table_info(buckets)")}
        if "tenant_id" in bucket_columns:
            conn.execute(
                """
                DELETE FROM buckets
                WHERE id NOT IN (
                    SELECT MIN(id)
                    FROM buckets
                    GROUP BY tenant_id, COALESCE(parent_id, -1), name
                )
                """
            )
        else:
            conn.execute(
                """
                DELETE FROM buckets
                WHERE id NOT IN (
                    SELECT MIN(id)
                    FROM buckets
                    GROUP BY COALESCE(parent_id, -1), name
                )
                """
            )
        conn.execute("DROP INDEX IF EXISTS idx_buckets_parent_name")
        conn.execute("DROP INDEX IF EXISTS idx_buckets_tenant_parent_name")
        if "tenant_id" in bucket_columns:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_buckets_tenant_parent_name "
                "ON buckets(tenant_id, COALESCE(parent_id, -1), name)"
            )
        else:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_buckets_parent_name "
                "ON buckets(COALESCE(parent_id, -1), name)"
            )

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def ensure_default_buckets(self) -> None:
        tenant_id = self._require_tenant()
        if self.get_sync_value("defaults_seeded"):
            return

        defaults = ["Groceries", "Gas", "Dining", "Shopping", "Utilities", "Other"]
        with self.connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM buckets WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()[0]
            if count == 0:
                for name in defaults:
                    conn.execute(
                        "INSERT INTO buckets (name, parent_id, tenant_id) VALUES (?, NULL, ?)",
                        (name, tenant_id),
                    )
        self.set_sync_value("defaults_seeded", "true")

    def _row_to_bucket(self, row: sqlite3.Row) -> Bucket:
        parent_name = row["parent_name"] if "parent_name" in row.keys() else None
        parent_id = row["parent_id"] if "parent_id" in row.keys() else None
        exclude_from_report = bool(row["exclude_from_report"]) if "exclude_from_report" in row.keys() else False
        return Bucket(
            id=row["id"],
            name=row["name"],
            parent_id=parent_id,
            parent_name=parent_name,
            exclude_from_report=exclude_from_report,
        )

    def list_buckets(self) -> list[Bucket]:
        tenant_id = self._require_tenant()
        query = """
            SELECT b.id, b.name, b.parent_id, b.exclude_from_report, p.name AS parent_name
            FROM buckets b
            LEFT JOIN buckets p ON p.id = b.parent_id AND p.tenant_id = b.tenant_id
            WHERE b.tenant_id = ?
            ORDER BY COALESCE(p.name, b.name), b.name
        """
        with self.connection() as conn:
            rows = conn.execute(query, (tenant_id,)).fetchall()
        return [self._row_to_bucket(row) for row in rows]

    def list_top_level_buckets(self) -> list[Bucket]:
        return [bucket for bucket in self.list_buckets() if bucket.parent_id is None]

    def bucket_has_children(self, bucket_id: int) -> bool:
        tenant_id = self._require_tenant()
        with self.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM buckets WHERE parent_id = ? AND tenant_id = ? LIMIT 1",
                (bucket_id, tenant_id),
            ).fetchone()
        return row is not None

    def get_bucket(self, bucket_id: int) -> Bucket | None:
        tenant_id = self._require_tenant()
        query = """
            SELECT b.id, b.name, b.parent_id, b.exclude_from_report, p.name AS parent_name
            FROM buckets b
            LEFT JOIN buckets p ON p.id = b.parent_id AND p.tenant_id = b.tenant_id
            WHERE b.id = ? AND b.tenant_id = ?
        """
        with self.connection() as conn:
            row = conn.execute(query, (bucket_id, tenant_id)).fetchone()
        if row is None:
            return None
        return self._row_to_bucket(row)

    def get_bucket_by_name(self, name: str, *, parent_id: int | None = None) -> Bucket | None:
        matches = self.find_buckets_by_name(name, parent_id=parent_id)
        if not matches:
            return None
        return matches[0]

    def find_buckets_by_name(
        self,
        name: str,
        *,
        parent_id: int | None = None,
    ) -> list[Bucket]:
        tenant_id = self._require_tenant()
        query = """
            SELECT b.id, b.name, b.parent_id, b.exclude_from_report, p.name AS parent_name
            FROM buckets b
            LEFT JOIN buckets p ON p.id = b.parent_id AND p.tenant_id = b.tenant_id
            WHERE lower(b.name) = lower(?) AND b.tenant_id = ?
        """
        params: list[object] = [name.strip(), tenant_id]
        if parent_id is not None:
            query += " AND b.parent_id = ?"
            params.append(parent_id)
        query += " ORDER BY b.id"
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_bucket(row) for row in rows]

    def resolve_bucket_reference(self, ref: str) -> Bucket:
        cleaned = ref.strip()
        if cleaned.isdigit():
            bucket = self.get_bucket(int(cleaned))
            if bucket is None:
                raise ValueError(f"Bucket id {cleaned} not found.")
            return bucket

        if " › " in cleaned:
            parent_name, child_name = cleaned.rsplit(" › ", 1)
            parent = self.get_bucket_by_name(parent_name.strip(), parent_id=None)
            if parent is None:
                raise ValueError(f"Parent bucket '{parent_name}' not found.")
            bucket = self.get_bucket_by_name(child_name.strip(), parent_id=parent.id)
            if bucket is None:
                raise ValueError(f"Bucket '{cleaned}' not found.")
            return bucket

        matches = self.find_buckets_by_name(cleaned)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            paths = ", ".join(match.display_path for match in matches)
            raise ValueError(f"Ambiguous bucket '{cleaned}'. Use a full path: {paths}")
        raise ValueError(f"Bucket '{cleaned}' not found.")

    def create_bucket(
        self,
        name: str,
        *,
        parent_id: int | None = None,
        exclude_from_report: bool = False,
    ) -> Bucket:
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("Bucket name is required.")
        if parent_id is not None:
            if self.get_bucket(parent_id) is None:
                raise ValueError("Parent bucket not found.")
        with self.connection() as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO buckets (name, parent_id, exclude_from_report, tenant_id)
                    VALUES (?, ?, ?, ?)
                    """,
                    (cleaned, parent_id, int(exclude_from_report), self._require_tenant()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("A bucket with that name already exists at this level.") from exc
            bucket_id = cursor.lastrowid
        bucket = self.get_bucket(bucket_id)
        if bucket is None:
            raise RuntimeError("Failed to load created bucket.")
        return bucket

    def update_bucket(
        self,
        bucket_id: int,
        *,
        name: str | None = None,
        parent_id: int | None = None,
        clear_parent: bool = False,
        exclude_from_report: bool | None = None,
    ) -> Bucket:
        bucket = self.get_bucket(bucket_id)
        if bucket is None:
            raise ValueError(f"Bucket {bucket_id} not found.")

        new_name = name.strip() if name is not None else bucket.name
        if not new_name:
            raise ValueError("Bucket name is required.")

        if clear_parent:
            new_parent_id = None
        elif parent_id is not None:
            if parent_id == bucket_id:
                raise ValueError("A bucket cannot be its own parent.")
            if self._is_descendant(bucket_id, parent_id):
                raise ValueError("A bucket cannot be moved under one of its sub-buckets.")
            if self.get_bucket(parent_id) is None:
                raise ValueError("Parent bucket not found.")
            new_parent_id = parent_id
        else:
            new_parent_id = bucket.parent_id

        new_exclude = bucket.exclude_from_report if exclude_from_report is None else exclude_from_report

        with self.connection() as conn:
            try:
                conn.execute(
                    """
                    UPDATE buckets
                    SET name = ?, parent_id = ?, exclude_from_report = ?
                    WHERE id = ? AND tenant_id = ?
                    """,
                    (new_name, new_parent_id, int(new_exclude), bucket_id, self._require_tenant()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("A bucket with that name already exists at this level.") from exc
        updated = self.get_bucket(bucket_id)
        if updated is None:
            raise RuntimeError("Failed to load updated bucket.")
        return updated

    def delete_bucket(self, bucket_id: int) -> None:
        bucket = self.get_bucket(bucket_id)
        if bucket is None:
            raise ValueError(f"Bucket {bucket_id} not found.")
        with self.connection() as conn:
            tenant_id = self._require_tenant()
            child = conn.execute(
                "SELECT 1 FROM buckets WHERE parent_id = ? AND tenant_id = ? LIMIT 1",
                (bucket_id, tenant_id),
            ).fetchone()
            if child is not None:
                raise ValueError("Delete sub-buckets first.")
            expense = conn.execute(
                """
                SELECT 1 FROM expenses
                WHERE tenant_id = ? AND (bucket_id = ? OR suggested_bucket_id = ?)
                LIMIT 1
                """,
                (tenant_id, bucket_id, bucket_id),
            ).fetchone()
            if expense is not None:
                raise ValueError("Bucket is used by expenses and cannot be deleted.")
            rule = conn.execute(
                "SELECT 1 FROM merchant_rules WHERE bucket_id = ? AND tenant_id = ? LIMIT 1",
                (bucket_id, tenant_id),
            ).fetchone()
            if rule is not None:
                raise ValueError("Bucket is used by merchant rules and cannot be deleted.")
            conn.execute(
                "DELETE FROM buckets WHERE id = ? AND tenant_id = ?",
                (bucket_id, tenant_id),
            )

    def _is_descendant(self, ancestor_id: int, candidate_parent_id: int) -> bool:
        current = self.get_bucket(candidate_parent_id)
        while current is not None:
            if current.id == ancestor_id:
                return True
            if current.parent_id is None:
                return False
            current = self.get_bucket(current.parent_id)
        return False

    def list_merchant_rules(
        self,
        *,
        search: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[MerchantRule]:
        tenant_id = self._require_tenant()
        query = """
            SELECT r.id, r.merchant_pattern, r.bucket_id,
                   CASE
                       WHEN p.name IS NOT NULL THEN p.name || ' › ' || b.name
                       ELSE b.name
                   END AS bucket_name,
                   r.match_type, r.priority, r.confirmed_by_user
            FROM merchant_rules r
            JOIN buckets b ON b.id = r.bucket_id AND b.tenant_id = r.tenant_id
            LEFT JOIN buckets p ON p.id = b.parent_id AND p.tenant_id = b.tenant_id
            WHERE r.tenant_id = ?
        """
        params: list[object] = [tenant_id]
        if search:
            term = f"%{search.strip()}%"
            query += """
                AND (
                    r.merchant_pattern LIKE ?
                    OR b.name LIKE ?
                    OR COALESCE(p.name, '') LIKE ?
                )
            """
            params.extend([term, term, term])
        query += " ORDER BY r.merchant_pattern COLLATE NOCASE ASC, r.match_type ASC"
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            MerchantRule(
                id=row["id"],
                merchant_pattern=row["merchant_pattern"],
                bucket_id=row["bucket_id"],
                bucket_name=row["bucket_name"],
                match_type=MatchType(row["match_type"]),
                priority=row["priority"],
                confirmed_by_user=bool(row["confirmed_by_user"]),
            )
            for row in rows
        ]

    def count_merchant_rules(self, *, search: str | None = None) -> int:
        tenant_id = self._require_tenant()
        query = """
            SELECT COUNT(*) AS count
            FROM merchant_rules r
            JOIN buckets b ON b.id = r.bucket_id AND b.tenant_id = r.tenant_id
            LEFT JOIN buckets p ON p.id = b.parent_id AND p.tenant_id = b.tenant_id
            WHERE r.tenant_id = ?
        """
        params: list[object] = [tenant_id]
        if search:
            term = f"%{search.strip()}%"
            query += """
                AND (
                    r.merchant_pattern LIKE ?
                    OR b.name LIKE ?
                    OR COALESCE(p.name, '') LIKE ?
                )
            """
            params.extend([term, term, term])
        with self.connection() as conn:
            row = conn.execute(query, params).fetchone()
        return int(row["count"])

    def get_merchant_rule(self, rule_id: int) -> MerchantRule | None:
        tenant_id = self._require_tenant()
        query = """
            SELECT r.id, r.merchant_pattern, r.bucket_id,
                   CASE
                       WHEN p.name IS NOT NULL THEN p.name || ' › ' || b.name
                       ELSE b.name
                   END AS bucket_name,
                   r.match_type, r.priority, r.confirmed_by_user
            FROM merchant_rules r
            JOIN buckets b ON b.id = r.bucket_id AND b.tenant_id = r.tenant_id
            LEFT JOIN buckets p ON p.id = b.parent_id AND p.tenant_id = b.tenant_id
            WHERE r.id = ? AND r.tenant_id = ?
        """
        with self.connection() as conn:
            row = conn.execute(query, (rule_id, tenant_id)).fetchone()
        if row is None:
            return None
        return MerchantRule(
            id=row["id"],
            merchant_pattern=row["merchant_pattern"],
            bucket_id=row["bucket_id"],
            bucket_name=row["bucket_name"],
            match_type=MatchType(row["match_type"]),
            priority=row["priority"],
            confirmed_by_user=bool(row["confirmed_by_user"]),
        )

    def update_merchant_rule_bucket(self, rule_id: int, bucket_id: int) -> MerchantRule:
        rule = self.get_merchant_rule(rule_id)
        if rule is None:
            raise ValueError(f"Rule {rule_id} not found.")
        bucket = self.get_bucket(bucket_id)
        if bucket is None:
            raise ValueError(f"Bucket {bucket_id} not found.")
        self.upsert_merchant_rule(
            merchant_pattern=rule.merchant_pattern,
            bucket_id=bucket_id,
            match_type=rule.match_type,
            confirmed_by_user=True,
        )
        updated = self.get_merchant_rule(rule_id)
        if updated is None:
            raise RuntimeError("Failed to load updated rule.")
        return updated

    def upsert_merchant_rule(
        self,
        merchant_pattern: str,
        bucket_id: int,
        match_type: MatchType = MatchType.EXACT,
        confirmed_by_user: bool = True,
    ) -> None:
        cleaned = format_merchant(merchant_pattern.strip())
        if not cleaned:
            raise ValueError("Merchant pattern is required.")
        priority = len(cleaned)
        tenant_id = self._require_tenant()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO merchant_rules (
                    tenant_id, merchant_pattern, bucket_id, match_type, priority, confirmed_by_user
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, merchant_pattern, match_type) DO UPDATE SET
                    bucket_id = excluded.bucket_id,
                    priority = excluded.priority,
                    confirmed_by_user = excluded.confirmed_by_user
                """,
                (
                    tenant_id,
                    cleaned,
                    bucket_id,
                    match_type.value,
                    priority,
                    int(confirmed_by_user),
                ),
            )

    def expense_exists(self, gmail_message_id: str) -> bool:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM expenses WHERE gmail_message_id = ? AND tenant_id = ?",
                (gmail_message_id, self._require_tenant()),
            ).fetchone()
        return row is not None

    def insert_expense(
        self,
        *,
        gmail_message_id: str,
        transaction_date: date,
        merchant: str,
        merchant_normalized: str,
        amount: float,
        currency: str,
        bucket_id: int | None,
        suggested_bucket_id: int | None,
        status: ExpenseStatus,
        email_subject: str | None,
        email_from: str | None,
        card_last_four: str | None = None,
        card_holder: str | None = None,
    ) -> int:
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO expenses (
                    tenant_id, gmail_message_id, transaction_date, merchant, merchant_normalized,
                    amount, currency, bucket_id, suggested_bucket_id, status,
                    email_subject, email_from, card_last_four, card_holder
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._require_tenant(),
                    gmail_message_id,
                    transaction_date.isoformat(),
                    merchant,
                    merchant_normalized,
                    amount,
                    currency,
                    bucket_id,
                    suggested_bucket_id,
                    status.value,
                    email_subject,
                    email_from,
                    card_last_four,
                    card_holder,
                ),
            )
            return cursor.lastrowid

    def link_expenses_to_sync_notification(
        self,
        expense_ids: list[int],
        notification_id: int,
    ) -> None:
        if not expense_ids:
            return
        placeholders = ", ".join("?" for _ in expense_ids)
        tenant_id = self._require_tenant()
        with self.connection() as conn:
            conn.execute(
                f"""
                UPDATE expenses
                SET sync_notification_id = ?
                WHERE tenant_id = ? AND id IN ({placeholders})
                """,
                [notification_id, tenant_id, *expense_ids],
            )

    def count_expenses_for_sync_notification(self, notification_id: int) -> int:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM expenses
                WHERE sync_notification_id = ? AND tenant_id = ?
                """,
                (notification_id, self._require_tenant()),
            ).fetchone()
        return int(row["count"])

    def get_latest_sync_notification_with_imports(self) -> Notification | None:
        tenant_id = self._require_tenant()
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT n.*
                FROM notifications n
                WHERE n.type = ? AND n.tenant_id = ?
                  AND EXISTS (
                      SELECT 1
                      FROM expenses e
                      WHERE e.sync_notification_id = n.id AND e.tenant_id = n.tenant_id
                  )
                ORDER BY n.created_at DESC, n.id DESC
                LIMIT 1
                """,
                (NotificationType.SYNC.value, tenant_id),
            ).fetchall()
        if not rows:
            return None
        return self._row_to_notification(rows[0])

    def _bucket_path_from_row(
        self,
        row: sqlite3.Row,
        *,
        id_key: str,
        name_key: str,
        parent_key: str,
    ) -> str | None:
        if not row[id_key]:
            return None
        return format_bucket_path(row[name_key], row[parent_key])

    def _row_to_expense(self, row: sqlite3.Row) -> Expense:
        bucket_name = self._bucket_path_from_row(
            row,
            id_key="bucket_id",
            name_key="bucket_name",
            parent_key="bucket_parent_name",
        )
        suggested_bucket_name = self._bucket_path_from_row(
            row,
            id_key="suggested_bucket_id",
            name_key="suggested_bucket_name",
            parent_key="suggested_bucket_parent_name",
        )
        return Expense(
            id=row["id"],
            gmail_message_id=row["gmail_message_id"],
            transaction_date=date.fromisoformat(row["transaction_date"]),
            merchant=row["merchant"],
            merchant_normalized=row["merchant_normalized"],
            amount=row["amount"],
            currency=row["currency"],
            bucket_id=row["bucket_id"],
            bucket_name=bucket_name,
            suggested_bucket_id=row["suggested_bucket_id"],
            suggested_bucket_name=suggested_bucket_name,
            status=ExpenseStatus(row["status"]),
            email_subject=row["email_subject"],
            email_from=row["email_from"],
            card_last_four=row["card_last_four"],
            card_holder=row["card_holder"],
            exclude_from_report=bool(row["exclude_from_report"]),
            bucket_excluded_from_report=bool(row["bucket_exclude_from_report"]),
        )

    def _expense_select_sql(self) -> str:
        return """
            SELECT e.*,
                   b.name AS bucket_name,
                   bp.name AS bucket_parent_name,
                   COALESCE(b.exclude_from_report, 0) AS bucket_exclude_from_report,
                   sb.name AS suggested_bucket_name,
                   sbp.name AS suggested_bucket_parent_name
            FROM expenses e
            LEFT JOIN buckets b ON b.id = e.bucket_id AND b.tenant_id = e.tenant_id
            LEFT JOIN buckets bp ON bp.id = b.parent_id AND bp.tenant_id = e.tenant_id
            LEFT JOIN buckets sb ON sb.id = e.suggested_bucket_id AND sb.tenant_id = e.tenant_id
            LEFT JOIN buckets sbp ON sbp.id = sb.parent_id AND sbp.tenant_id = e.tenant_id
        """

    _REPORTABLE_WHERE = """
        AND COALESCE(e.exclude_from_report, 0) = 0
        AND COALESCE(b.exclude_from_report, 0) = 0
        AND e.tenant_id = ?
    """

    def list_expenses(
        self,
        *,
        status: ExpenseStatus | None = None,
        month: str | None = None,
        search: str | None = None,
        bucket_id: int | None = None,
        unassigned: bool = False,
        card_holder: str | None = None,
        sync_notification_id: int | None = None,
    ) -> list[Expense]:
        query = self._expense_select_sql() + " WHERE e.tenant_id = ?"
        params: list[object] = [self._require_tenant()]
        if status is not None:
            query += " AND e.status = ?"
            params.append(status.value)
        if month:
            query += " AND strftime('%Y-%m', e.transaction_date) = ?"
            params.append(month)
        if unassigned:
            query += " AND e.bucket_id IS NULL"
        elif bucket_id is not None:
            query += " AND e.bucket_id = ?"
            params.append(bucket_id)
        if card_holder:
            query += " AND e.card_holder = ?"
            params.append(card_holder)
        if sync_notification_id is not None:
            query += " AND e.sync_notification_id = ?"
            params.append(sync_notification_id)
        if search:
            term = search.strip()
            if term:
                merchant_like = f"%{term.lower()}%"
                amount_text = term.replace("$", "").replace(",", "").strip()
                query += (
                    " AND (LOWER(e.merchant) LIKE ?"
                    " OR printf('%.2f', e.amount) LIKE ?)"
                )
                params.extend([merchant_like, f"{amount_text}%"])
        query += " ORDER BY e.transaction_date DESC, e.id DESC"

        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_expense(row) for row in rows]

    def get_expense(self, expense_id: int) -> Expense | None:
        query = self._expense_select_sql() + " WHERE e.id = ? AND e.tenant_id = ?"
        with self.connection() as conn:
            row = conn.execute(query, (expense_id, self._require_tenant())).fetchone()
        if row is None:
            return None
        return self._row_to_expense(row)

    def confirm_expense(self, expense_id: int, bucket_id: int) -> Expense | None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE expenses
                SET bucket_id = ?, suggested_bucket_id = ?, status = ?
                WHERE id = ? AND tenant_id = ?
                """,
                (bucket_id, bucket_id, ExpenseStatus.CONFIRMED.value, expense_id, self._require_tenant()),
            )
        return self.get_expense(expense_id)

    def delete_expense(self, expense_id: int) -> None:
        with self.connection() as conn:
            cursor = conn.execute(
                "DELETE FROM expenses WHERE id = ? AND tenant_id = ?",
                (expense_id, self._require_tenant()),
            )
        if cursor.rowcount == 0:
            raise ValueError(f"Expense {expense_id} not found")

    def update_expense(
        self,
        expense_id: int,
        *,
        bucket_id: int | None = None,
        exclude_from_report: bool | None = None,
        confirm: bool = False,
    ) -> Expense | None:
        expense = self.get_expense(expense_id)
        if expense is None:
            raise ValueError(f"Expense {expense_id} not found")

        new_bucket_id = expense.bucket_id if bucket_id is None else bucket_id
        new_exclude = expense.exclude_from_report if exclude_from_report is None else exclude_from_report
        new_status = expense.status
        if confirm and new_bucket_id is not None:
            new_status = ExpenseStatus.CONFIRMED

        with self.connection() as conn:
            conn.execute(
                """
                UPDATE expenses
                SET bucket_id = ?,
                    suggested_bucket_id = COALESCE(?, suggested_bucket_id),
                    exclude_from_report = ?,
                    status = ?
                WHERE id = ? AND tenant_id = ?
                """,
                (
                    new_bucket_id,
                    new_bucket_id,
                    int(new_exclude),
                    new_status.value,
                    expense_id,
                    self._require_tenant(),
                ),
            )
        return self.get_expense(expense_id)

    def update_expense_card(
        self,
        expense_id: int,
        *,
        card_last_four: str | None,
        card_holder: str | None,
    ) -> Expense | None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE expenses
                SET card_last_four = ?, card_holder = ?
                WHERE id = ? AND tenant_id = ?
                """,
                (card_last_four, card_holder, expense_id, self._require_tenant()),
            )
        return self.get_expense(expense_id)

    def distinct_merchants(self, *, pending_only: bool = False) -> list[str]:
        tenant_id = self._require_tenant()
        query = "SELECT DISTINCT merchant FROM expenses WHERE tenant_id = ?"
        params: list[object] = [tenant_id]
        if pending_only:
            query += " AND status = ?"
            params.append(ExpenseStatus.PENDING.value)
        query += " ORDER BY merchant"
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [row["merchant"] for row in rows]

    def monthly_totals(
        self,
        month: str,
        *,
        card_holder: str | None = None,
    ) -> list[tuple[int | None, str, float, int]]:
        tenant_id = self._require_tenant()
        query = f"""
            SELECT e.bucket_id,
                   CASE
                       WHEN bp.name IS NOT NULL THEN bp.name || ' › ' || b.name
                       ELSE COALESCE(b.name, 'Unassigned')
                   END AS bucket_name,
                   SUM(e.amount) AS total,
                   COUNT(*) AS count
            FROM expenses e
            LEFT JOIN buckets b ON b.id = e.bucket_id AND b.tenant_id = e.tenant_id
            LEFT JOIN buckets bp ON bp.id = b.parent_id AND bp.tenant_id = e.tenant_id
            WHERE strftime('%Y-%m', e.transaction_date) = ?
            {self._REPORTABLE_WHERE}
        """
        params: list[object] = [month, tenant_id]
        if card_holder is not None:
            query += " AND e.card_holder = ?"
            params.append(card_holder)
        query += " GROUP BY e.bucket_id ORDER BY total DESC"
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            (row["bucket_id"], row["bucket_name"], row["total"], row["count"])
            for row in rows
        ]

    def monthly_category_totals(
        self,
        month: str,
        *,
        card_holder: str | None = None,
    ) -> list[tuple[str, float, int]]:
        tenant_id = self._require_tenant()
        query = f"""
            WITH RECURSIVE bucket_roots AS (
                SELECT id, name, parent_id, id AS root_id, name AS root_name, tenant_id
                FROM buckets
                WHERE parent_id IS NULL AND tenant_id = ?
                UNION ALL
                SELECT b.id, b.name, b.parent_id, r.root_id, r.root_name, b.tenant_id
                FROM buckets b
                JOIN bucket_roots r ON b.parent_id = r.id AND b.tenant_id = r.tenant_id
            )
            SELECT COALESCE(r.root_name, 'Unassigned') AS category_name,
                   SUM(e.amount) AS total,
                   COUNT(*) AS count
            FROM expenses e
            LEFT JOIN bucket_roots r ON r.id = e.bucket_id AND r.tenant_id = e.tenant_id
            LEFT JOIN buckets b ON b.id = e.bucket_id AND b.tenant_id = e.tenant_id
            WHERE strftime('%Y-%m', e.transaction_date) = ?
            {self._REPORTABLE_WHERE}
        """
        params: list[object] = [tenant_id, month, tenant_id]
        if card_holder is not None:
            query += " AND e.card_holder = ?"
            params.append(card_holder)
        query += " GROUP BY category_name ORDER BY total DESC"
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [(row["category_name"], row["total"], row["count"]) for row in rows]

    def monthly_excluded_totals(
        self,
        month: str,
        *,
        card_holder: str | None = None,
    ) -> list[tuple[str, float, int]]:
        tenant_id = self._require_tenant()
        query = """
            SELECT CASE
                       WHEN bp.name IS NOT NULL THEN bp.name || ' › ' || b.name
                       WHEN b.name IS NOT NULL THEN b.name
                       ELSE 'Unassigned'
                   END AS bucket_name,
                   SUM(e.amount) AS total,
                   COUNT(*) AS count
            FROM expenses e
            LEFT JOIN buckets b ON b.id = e.bucket_id AND b.tenant_id = e.tenant_id
            LEFT JOIN buckets bp ON bp.id = b.parent_id AND bp.tenant_id = e.tenant_id
            WHERE strftime('%Y-%m', e.transaction_date) = ?
              AND e.tenant_id = ?
              AND (
                    COALESCE(e.exclude_from_report, 0) = 1
                    OR COALESCE(b.exclude_from_report, 0) = 1
                  )
        """
        params: list[object] = [month, tenant_id]
        if card_holder is not None:
            query += " AND e.card_holder = ?"
            params.append(card_holder)
        query += " GROUP BY bucket_name ORDER BY total DESC"
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [(row["bucket_name"], row["total"], row["count"]) for row in rows]

    def list_dismissed_group_keys(self) -> set[str]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT group_key FROM dismissed_merchant_groups WHERE tenant_id = ?",
                (self._require_tenant(),),
            ).fetchall()
        return {row["group_key"] for row in rows}

    def dismiss_merchant_group(self, group_key: str) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO dismissed_merchant_groups (tenant_id, group_key)
                VALUES (?, ?)
                ON CONFLICT(tenant_id, group_key) DO NOTHING
                """,
                (self._require_tenant(), group_key.strip()),
            )

    def create_notification(
        self,
        *,
        type: NotificationType,
        title: str,
        message: str,
        level: NotificationLevel = NotificationLevel.INFO,
    ) -> Notification:
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO notifications (tenant_id, type, level, title, message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    self._require_tenant(),
                    type.value,
                    level.value,
                    title.strip(),
                    message.strip(),
                ),
            )
            notification_id = int(cursor.lastrowid)
        notification = self.get_notification(notification_id)
        if notification is None:
            raise RuntimeError("Failed to create notification.")
        return notification

    def get_notification(self, notification_id: int) -> Notification | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM notifications WHERE id = ? AND tenant_id = ?",
                (notification_id, self._require_tenant()),
            ).fetchone()
        if row is None:
            return None
        return self._attach_import_count(row)

    def list_notifications(self, *, limit: int = 50) -> list[Notification]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM notifications
                WHERE tenant_id = ?
                ORDER BY datetime(created_at) DESC, id DESC
                LIMIT ?
                """,
                (self._require_tenant(), limit),
            ).fetchall()
        return [self._attach_import_count(row) for row in rows]

    def _attach_import_count(self, row: sqlite3.Row) -> Notification:
        notification = self._row_to_notification(row)
        if notification.type == NotificationType.SYNC:
            notification.import_count = self.count_expenses_for_sync_notification(notification.id)
        return notification

    def count_unread_notifications(self) -> int:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM notifications
                WHERE read_at IS NULL AND tenant_id = ?
                """,
                (self._require_tenant(),),
            ).fetchone()
        return int(row["count"])

    def mark_notification_read(self, notification_id: int) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE notifications
                SET read_at = COALESCE(read_at, datetime('now'))
                WHERE id = ? AND tenant_id = ?
                """,
                (notification_id, self._require_tenant()),
            )

    def mark_all_notifications_read(self) -> int:
        with self.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE notifications
                SET read_at = datetime('now')
                WHERE read_at IS NULL AND tenant_id = ?
                """,
                (self._require_tenant(),),
            )
        return cursor.rowcount

    def _parse_db_datetime(self, value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _row_to_notification(self, row: sqlite3.Row) -> Notification:
        read_at = row["read_at"]
        return Notification(
            id=row["id"],
            type=NotificationType(row["type"]),
            level=NotificationLevel(row["level"]),
            title=row["title"],
            message=row["message"],
            created_at=self._parse_db_datetime(row["created_at"]),
            read_at=self._parse_db_datetime(read_at) if read_at else None,
        )

    def get_sync_value(self, key: str) -> str | None:
        tenant_id = self._require_tenant()
        with self.connection() as conn:
            row = conn.execute(
                "SELECT value FROM sync_state WHERE tenant_id = ? AND key = ?",
                (tenant_id, key),
            ).fetchone()
        if row is None:
            return None
        return row["value"]

    def set_sync_value(self, key: str, value: str) -> None:
        tenant_id = self._require_tenant()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO sync_state (tenant_id, key, value) VALUES (?, ?, ?)
                ON CONFLICT(tenant_id, key) DO UPDATE SET value = excluded.value
                """,
                (tenant_id, key, value),
            )

    def last_sync_at(self) -> datetime | None:
        value = self.get_sync_value("last_sync_at")
        if not value:
            return None
        return self._parse_db_datetime(value)

    def is_sync_in_progress(self) -> bool:
        return self.get_sync_value("sync_in_progress") == "true"

    def set_sync_in_progress(self, in_progress: bool) -> None:
        if in_progress:
            self.set_sync_value("sync_in_progress", "true")
        else:
            with self.connection() as conn:
                conn.execute(
                    "DELETE FROM sync_state WHERE tenant_id = ? AND key = ?",
                    (self._require_tenant(), "sync_in_progress"),
                )

    def is_sync_stale(self, stale_hours: int) -> bool:
        last_sync = self.last_sync_at()
        if last_sync is None:
            return True
        now = datetime.now(timezone.utc)
        if last_sync.tzinfo is None:
            last_sync = last_sync.replace(tzinfo=timezone.utc)
        return now - last_sync > timedelta(hours=stale_hours)

    def get_last_sync_result(self) -> dict | None:
        raw = self.get_sync_value("last_sync_result")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def set_last_sync_result(self, result: dict) -> None:
        self.set_sync_value("last_sync_result", json.dumps(result))

    def count_users(self) -> int:
        with self.connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        return int(row["count"])

    def _row_to_tenant(self, row: sqlite3.Row) -> Tenant:
        card_holders_raw = row["card_holders"] or "{}"
        try:
            card_holders = json.loads(card_holders_raw)
        except json.JSONDecodeError:
            card_holders = {}
        if not isinstance(card_holders, dict):
            card_holders = {}
        return Tenant(
            id=row["id"],
            name=row["name"],
            invite_code=row["invite_code"],
            card_holders={str(k): str(v) for k, v in card_holders.items()},
            gmail_search_query=row["gmail_search_query"],
            gmail_token_json=row["gmail_token_json"],
            created_at=self._parse_db_datetime(row["created_at"]),
        )

    def get_tenant(self, tenant_id: int) -> Tenant | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT id, name, invite_code, card_holders, gmail_search_query,
                       gmail_token_json, created_at
                FROM tenants
                WHERE id = ?
                """,
                (tenant_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_tenant(row)

    def get_tenant_by_invite_code(self, invite_code: str) -> Tenant | None:
        cleaned = invite_code.strip().upper()
        if not cleaned:
            return None
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT id, name, invite_code, card_holders, gmail_search_query,
                       gmail_token_json, created_at
                FROM tenants
                WHERE invite_code = ?
                """,
                (cleaned,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_tenant(row)

    def list_tenants_with_gmail(self) -> list[Tenant]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, name, invite_code, card_holders, gmail_search_query,
                       gmail_token_json, created_at
                FROM tenants
                WHERE gmail_token_json IS NOT NULL AND TRIM(gmail_token_json) != ''
                ORDER BY id
                """
            ).fetchall()
        return [self._row_to_tenant(row) for row in rows]

    def get_default_tenant_id(self) -> int:
        with self.connection() as conn:
            row = conn.execute("SELECT id FROM tenants ORDER BY id LIMIT 1").fetchone()
        if row is None:
            raise RuntimeError("No tenants found.")
        return int(row["id"])

    def create_tenant(
        self,
        name: str,
        *,
        card_holders: dict[str, str] | None = None,
        gmail_search_query: str | None = None,
    ) -> Tenant:
        from expenses_tracker.config import get_settings

        settings = get_settings()
        cleaned_name = name.strip() or "My household"
        holders = card_holders if card_holders is not None else dict(settings.card_holders)
        query = gmail_search_query or settings.gmail_search_query
        invite_code = secrets.token_urlsafe(6)[:8].upper()
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO tenants (name, invite_code, card_holders, gmail_search_query)
                VALUES (?, ?, ?, ?)
                """,
                (cleaned_name, invite_code, json.dumps(holders), query),
            )
            tenant_id = int(cursor.lastrowid)
        tenant = self.get_tenant(tenant_id)
        if tenant is None:
            raise RuntimeError("Failed to create tenant.")
        scoped = Database(self.path, tenant_id=tenant_id)
        scoped.ensure_default_buckets()
        return tenant

    def update_tenant_settings(
        self,
        tenant_id: int,
        *,
        name: str | None = None,
        card_holders: dict[str, str] | None = None,
        gmail_search_query: str | None = None,
    ) -> Tenant:
        tenant = self.get_tenant(tenant_id)
        if tenant is None:
            raise ValueError(f"Tenant {tenant_id} not found.")
        new_name = name.strip() if name is not None else tenant.name
        if not new_name:
            raise ValueError("Household name is required.")
        holders = card_holders if card_holders is not None else tenant.card_holders
        query = gmail_search_query if gmail_search_query is not None else tenant.gmail_search_query
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE tenants
                SET name = ?, card_holders = ?, gmail_search_query = ?
                WHERE id = ?
                """,
                (new_name, json.dumps(holders), query, tenant_id),
            )
        updated = self.get_tenant(tenant_id)
        if updated is None:
            raise RuntimeError("Failed to update tenant.")
        return updated

    def update_tenant_gmail_token(self, tenant_id: int, token_json: str) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE tenants SET gmail_token_json = ? WHERE id = ?",
                (token_json, tenant_id),
            )

    def create_user(self, email: str, password_hash: str, tenant_id: int) -> User:
        normalized_email = email.strip().lower()
        if not normalized_email:
            raise ValueError("Email is required.")
        if self.get_tenant(tenant_id) is None:
            raise ValueError("Household not found.")
        try:
            with self.connection() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO users (email, password_hash, tenant_id)
                    VALUES (?, ?, ?)
                    """,
                    (normalized_email, password_hash, tenant_id),
                )
                user_id = cursor.lastrowid
        except sqlite3.IntegrityError as exc:
            raise ValueError("An account with that email already exists.") from exc
        user = self.get_user_by_id(user_id)
        if user is None:
            raise RuntimeError("Failed to create user.")
        return user

    def get_user_by_email(self, email: str) -> User | None:
        normalized_email = email.strip().lower()
        if not normalized_email:
            return None
        with self.connection() as conn:
            row = conn.execute(
                self._user_select_sql() + " WHERE email = ?",
                (normalized_email,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_user(row)

    def get_user_by_id(self, user_id: int) -> User | None:
        with self.connection() as conn:
            row = conn.execute(
                self._user_select_sql() + " WHERE id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_user(row)

    def list_users_by_tenant(self, tenant_id: int) -> list[User]:
        with self.connection() as conn:
            rows = conn.execute(
                self._user_select_sql() + " WHERE tenant_id = ? ORDER BY email",
                (tenant_id,),
            ).fetchall()
        return [self._row_to_user(row) for row in rows]

    def update_user_notification_prefs(
        self,
        user_id: int,
        *,
        notify_email: bool | None = None,
        notify_sms: bool | None = None,
        phone: str | None = None,
        monthly_alert_threshold: float | None = None,
        clear_monthly_alert_threshold: bool = False,
    ) -> User:
        user = self.get_user_by_id(user_id)
        if user is None:
            raise ValueError(f"User {user_id} not found.")

        next_notify_email = user.notify_email if notify_email is None else notify_email
        next_notify_sms = user.notify_sms if notify_sms is None else notify_sms
        next_phone = user.phone if phone is None else phone.strip() or None
        if clear_monthly_alert_threshold:
            next_threshold = None
        elif monthly_alert_threshold is None:
            next_threshold = user.monthly_alert_threshold
        else:
            next_threshold = monthly_alert_threshold

        with self.connection() as conn:
            conn.execute(
                """
                UPDATE users
                SET notify_email = ?, notify_sms = ?, phone = ?, monthly_alert_threshold = ?
                WHERE id = ?
                """,
                (
                    int(next_notify_email),
                    int(next_notify_sms),
                    next_phone,
                    next_threshold,
                    user_id,
                ),
            )
        updated = self.get_user_by_id(user_id)
        if updated is None:
            raise RuntimeError("Failed to update user notification preferences.")
        return updated

    def get_user_password_hash(self, email: str) -> tuple[User, str] | None:
        normalized_email = email.strip().lower()
        if not normalized_email:
            return None
        with self.connection() as conn:
            row = conn.execute(
                self._user_select_sql(include_password=True) + " WHERE email = ?",
                (normalized_email,),
            ).fetchone()
        if row is None:
            return None
        user = self._row_to_user(row)
        return user, row["password_hash"]

    def _user_select_sql(self, *, include_password: bool = False) -> str:
        columns = [
            "id",
            "email",
            "tenant_id",
            "created_at",
            "notify_email",
            "notify_sms",
            "phone",
            "monthly_alert_threshold",
        ]
        if include_password:
            columns.insert(3, "password_hash")
        return "SELECT " + ", ".join(columns) + " FROM users"

    def _row_to_user(self, row: sqlite3.Row) -> User:
        tenant_id = row["tenant_id"] if "tenant_id" in row.keys() else 1
        keys = row.keys()
        return User(
            id=row["id"],
            email=row["email"],
            tenant_id=int(tenant_id),
            created_at=self._parse_db_datetime(row["created_at"]),
            notify_email=bool(row["notify_email"]) if "notify_email" in keys else False,
            notify_sms=bool(row["notify_sms"]) if "notify_sms" in keys else False,
            phone=row["phone"] if "phone" in keys else None,
            monthly_alert_threshold=(
                float(row["monthly_alert_threshold"])
                if "monthly_alert_threshold" in keys and row["monthly_alert_threshold"] is not None
                else None
            ),
        )
