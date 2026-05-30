from __future__ import annotations

from datetime import date
from pathlib import Path

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from markupsafe import Markup

from expenses_tracker.auth import (
    current_user,
    hash_password,
    login_user,
    logout_user,
    validate_password,
    verify_password,
)
from expenses_tracker.background_sync import sync_status_payload, try_start_background_sync
from expenses_tracker.bucket_matcher import BucketMatcher, analyze_merchants
from expenses_tracker.buckets import (
    build_bucket_tree,
    flatten_bucket_select_options,
    resolve_suggested_bucket_id,
)
from expenses_tracker.config import Settings, get_settings, resolve_gmail_credentials_path
from expenses_tracker.display import format_merchant
from expenses_tracker.gmail_client import GmailClient
from expenses_tracker.models import ExpenseStatus, MatchType, NotificationType
from expenses_tracker.notifications import format_notification_time, notification_to_dict
from expenses_tracker.scheduled_sync import run_scheduled_sync_all
from expenses_tracker.services import build_global_db, build_services
from expenses_tracker.tenancy import resolve_tenant_id

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _bucket_options(db, *, assignable_only: bool = False):
    tree = build_bucket_tree(db.list_buckets())
    return flatten_bucket_select_options(tree, assignable_only=assignable_only)


def _safe_next_url(raw: str) -> str:
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return url_for("review")


def _expense_filter_redirect_kwargs(form) -> dict[str, str | int]:
    kwargs: dict[str, str | int] = {
        "month": form.get("month", date.today().strftime("%Y-%m")),
        "q": form.get("q", "").strip(),
        "status": form.get("status", "").strip(),
    }
    person = form.get("person", "").strip()
    if person:
        kwargs["person"] = person
    filter_bucket_id = form.get("filter_bucket_id", "").strip()
    if filter_bucket_id:
        kwargs["bucket_id"] = filter_bucket_id
    if form.get("unassigned") == "1":
        kwargs["unassigned"] = 1
    return kwargs


PUBLIC_ENDPOINTS = frozenset({"login", "signup", "logout", "static", "health", "internal_sync_scheduled"})

RULES_PER_PAGE = 25


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or get_settings()
    app = Flask(__name__, template_folder=str(TEMPLATE_DIR), static_folder=str(STATIC_DIR))
    app.secret_key = settings.secret_key
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = settings.session_cookie_secure

    def _services():
        return build_services(settings)

    @app.before_request
    def require_login():
        if settings.auth_disabled:
            return None
        endpoint = request.endpoint
        if endpoint in PUBLIC_ENDPOINTS or endpoint is None:
            return None
        auth_db = build_global_db(settings)
        if current_user(auth_db) is None:
            if session.get("user_id") is not None:
                logout_user()
            return redirect(url_for("login", next=request.path))

    def _tenant_id() -> int:
        return resolve_tenant_id(settings)

    @app.context_processor
    def inject_helpers():
        auth_db = build_global_db(settings)
        user = None if settings.auth_disabled else current_user(auth_db)
        helpers = {
            "format_merchant": format_merchant,
            "format_notification_time": format_notification_time,
            "current_user": user,
            "auth_enabled": not settings.auth_disabled,
            "allow_signup": settings.allow_signup,
            "recent_notifications": [],
            "notifications_unread_count": 0,
            "current_tenant": None,
            "sync_in_progress": False,
        }

        if settings.auth_disabled or user is not None:
            db, sync = _services()
            helpers.update(
                {
                    "recent_notifications": db.list_notifications(limit=8),
                    "notifications_unread_count": db.count_unread_notifications(),
                    "current_tenant": sync.tenant,
                    "sync_in_progress": db.is_sync_in_progress(),
                }
            )

        return helpers

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.post("/internal/sync-scheduled")
    def internal_sync_scheduled():
        if not settings.cron_secret:
            return jsonify({"error": "CRON_SECRET is not configured."}), 503
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.removeprefix("Bearer ").strip()
        if token != settings.cron_secret:
            secret_header = request.headers.get("X-Cron-Secret", "")
            if secret_header != settings.cron_secret:
                return jsonify({"error": "Unauthorized."}), 401
        results = run_scheduled_sync_all(settings, only_if_stale=True)
        return jsonify({"results": results})

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if settings.auth_disabled:
            return redirect(url_for("review"))
        auth_db = build_global_db(settings)
        if current_user(auth_db) is not None:
            return redirect(_safe_next_url(request.args.get("next", "")))
        if request.method == "POST":
            email = request.form.get("email", "").strip()
            password = request.form.get("password", "")
            credentials = auth_db.get_user_password_hash(email)
            if credentials is None or not verify_password(password, credentials[1]):
                flash("Invalid email or password.", "error")
            else:
                login_user(credentials[0].id)
                flash(f"Welcome back, {credentials[0].email}.", "success")
                return redirect(_safe_next_url(request.form.get("next") or request.args.get("next", "")))
        return render_template(
            "login.html",
            next_url=request.args.get("next", ""),
            hide_nav=True,
        )

    @app.route("/signup", methods=["GET", "POST"])
    def signup():
        if settings.auth_disabled:
            return redirect(url_for("review"))
        if not settings.allow_signup:
            flash("Signup is disabled.", "error")
            return redirect(url_for("login"))
        auth_db = build_global_db(settings)
        if current_user(auth_db) is not None:
            return redirect(url_for("review"))
        if request.method == "POST":
            email = request.form.get("email", "").strip()
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")
            invite_code = request.form.get("invite_code", "").strip()
            household_name = request.form.get("household_name", "").strip()
            password_error = validate_password(password)
            if password_error:
                flash(password_error, "error")
            elif password != confirm_password:
                flash("Passwords do not match.", "error")
            elif not email:
                flash("Email is required.", "error")
            else:
                try:
                    if invite_code:
                        tenant = auth_db.get_tenant_by_invite_code(invite_code)
                        if tenant is None:
                            raise ValueError("Invalid invite code.")
                        tenant_id = tenant.id
                    else:
                        tenant = auth_db.create_tenant(household_name or "My household")
                        tenant_id = tenant.id
                    user = auth_db.create_user(email, hash_password(password), tenant_id)
                    login_user(user.id)
                    flash("Account created.", "success")
                    return redirect(url_for("review"))
                except ValueError as exc:
                    flash(str(exc), "error")
        return render_template("signup.html", hide_nav=True)

    @app.post("/logout")
    def logout():
        logout_user()
        flash("You have been logged out.", "success")
        return redirect(url_for("login"))

    @app.route("/")
    def review():
        db, _ = _services()
        pending = db.list_expenses(status=ExpenseStatus.PENDING)
        latest_sync = db.get_latest_sync_notification_with_imports()
        bucket_options = _bucket_options(db, assignable_only=True)
        return render_template(
            "review.html",
            pending=pending,
            latest_sync=latest_sync,
            bucket_options=bucket_options,
            page="review",
        )

    @app.post("/expenses/<int:expense_id>/confirm")
    def confirm_expense(expense_id: int):
        _, sync = _services()
        bucket_id_raw = request.form.get("bucket_id", "").strip()
        create_rule = request.form.get("create_rule") == "on"
        sync_notification_id = request.form.get("sync_notification_id", "").strip()
        redirect_target = request.form.get("redirect_to", "").strip()
        if not bucket_id_raw:
            flash("Pick a bucket first.", "error")
            if redirect_target == "sync_review" and sync_notification_id:
                return redirect(url_for("review_sync", notification_id=int(sync_notification_id)))
            return redirect(url_for("review"))
        try:
            sync.confirm_expense_by_id(expense_id, int(bucket_id_raw), create_rule=create_rule)
            db, _ = _services()
            bucket = db.get_bucket(int(bucket_id_raw))
            label = bucket.display_path if bucket else "bucket"
            flash(f"Assigned to {label}.", "success")
        except (ValueError, TypeError) as exc:
            flash(str(exc), "error")
        if redirect_target == "sync_review" and sync_notification_id:
            return redirect(url_for("review_sync", notification_id=int(sync_notification_id)))
        return redirect(url_for("review"))

    @app.route("/review/sync/<int:notification_id>")
    def review_sync(notification_id: int):
        db, _ = _services()
        notification = db.get_notification(notification_id)
        if notification is None:
            flash("Sync notification not found.", "error")
            return redirect(url_for("review"))
        expenses = db.list_expenses(sync_notification_id=notification_id)
        if not expenses:
            flash("No transactions linked to this sync.", "error")
            return redirect(url_for("notifications_page"))
        bucket_options = _bucket_options(db, assignable_only=True)
        pending_count = sum(1 for expense in expenses if expense.status == ExpenseStatus.PENDING)
        assigned_count = len(expenses) - pending_count
        return render_template(
            "review_sync.html",
            notification=notification,
            expenses=expenses,
            bucket_options=bucket_options,
            pending_count=pending_count,
            assigned_count=assigned_count,
            page="review",
        )

    @app.route("/review/new")
    def review_new():
        db, _ = _services()
        notification = db.get_latest_sync_notification_with_imports()
        if notification is None:
            flash("No recent sync imports to review.", "info")
            return redirect(url_for("review"))
        return redirect(url_for("review_sync", notification_id=notification.id))

    @app.post("/sync/start")
    def sync_start():
        only_if_stale = request.args.get("only_if_stale") == "1"
        result = try_start_background_sync(
            settings,
            _tenant_id(),
            only_if_stale=only_if_stale,
        )
        status_code = 202 if result.get("started") else 200
        return jsonify(result), status_code

    @app.get("/sync/status")
    def sync_status():
        payload = sync_status_payload(settings, _tenant_id())
        last_result = payload.get("last_result") or {}
        notification_id = last_result.get("notification_id")
        if notification_id:
            payload["review_url"] = url_for("review_sync", notification_id=int(notification_id))
        else:
            payload["review_url"] = None
        return jsonify(payload)

    @app.post("/sync")
    def sync_inbox():
        result = try_start_background_sync(settings, _tenant_id(), only_if_stale=False)
        if result.get("started"):
            flash("Syncing Gmail in the background. You'll get a notification when it's done.", "info")
        elif result.get("reason") == "not_connected":
            flash("Connect Gmail in Settings before syncing.", "error")
        elif result.get("reason") == "already_running":
            flash("A Gmail sync is already running.", "info")
        else:
            flash("Could not start Gmail sync.", "error")
        return redirect(request.referrer or url_for("review"))

    @app.route("/notifications")
    def notifications_page():
        db, _ = _services()
        notifications = db.list_notifications(limit=100)
        return render_template(
            "notifications.html",
            notifications=notifications,
            page="notifications",
        )

    @app.get("/notifications/recent")
    def notifications_recent():
        db, _ = _services()
        notifications = db.list_notifications(limit=8)
        items = []
        for notification in notifications:
            review_url = None
            if notification.type == NotificationType.SYNC and notification.import_count:
                review_url = url_for("review_sync", notification_id=notification.id)
            items.append(notification_to_dict(notification, review_url=review_url))
        return jsonify(
            {
                "notifications": items,
                "unread_count": db.count_unread_notifications(),
            }
        )

    @app.post("/notifications/mark-all-read")
    def mark_all_notifications_read():
        db, _ = _services()
        updated = db.mark_all_notifications_read()
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify(updated=updated, unread_count=db.count_unread_notifications())
        flash(f"Marked {updated} notification(s) as read.", "success")
        return redirect(request.referrer or url_for("notifications_page"))

    @app.route("/expenses")
    def expenses_page():
        db, _ = _services()
        month = request.args.get("month", date.today().strftime("%Y-%m"))
        status_filter = request.args.get("status", "").strip()
        search = request.args.get("q", "").strip()
        person = request.args.get("person", "").strip()
        unassigned = request.args.get("unassigned") == "1"
        bucket_id_raw = request.args.get("bucket_id", "").strip()
        bucket_id = int(bucket_id_raw) if bucket_id_raw else None
        status = ExpenseStatus(status_filter) if status_filter else None
        expenses = db.list_expenses(
            status=status,
            month=month or None,
            search=search or None,
            bucket_id=bucket_id,
            unassigned=unassigned,
            card_holder=person or None,
        )
        bucket_options = _bucket_options(db, assignable_only=True)
        bucket_filter_name = None
        if unassigned:
            bucket_filter_name = "Unassigned"
        elif bucket_id is not None:
            bucket = db.get_bucket(bucket_id)
            bucket_filter_name = bucket.display_path if bucket else None
        return render_template(
            "expenses.html",
            expenses=expenses,
            bucket_options=bucket_options,
            month=month,
            status_filter=status_filter,
            search=search,
            person=person,
            bucket_id=bucket_id,
            unassigned=unassigned,
            bucket_filter_name=bucket_filter_name,
            page="expenses",
        )

    @app.post("/expenses/<int:expense_id>/update")
    def update_expense(expense_id: int):
        db, _ = _services()
        matcher = BucketMatcher(db)
        redirect_kwargs = _expense_filter_redirect_kwargs(request.form)
        bucket_id_raw = request.form.get("bucket_id", "").strip()
        exclude_from_report = request.form.get("exclude_from_report") == "on"
        confirm_rule_update = request.form.get("confirm_rule_update") == "1"
        update_rule = request.form.get("update_rule") == "1"
        try:
            expense = db.get_expense(expense_id)
            if expense is None:
                raise ValueError(f"Expense {expense_id} not found")
            bucket_id = int(bucket_id_raw) if bucket_id_raw else None
            matched_rule = matcher.match(expense.merchant)
            bucket_changed = bucket_id != expense.bucket_id
            rule_conflict = (
                bucket_changed
                and bucket_id is not None
                and matched_rule is not None
                and matched_rule.bucket_id != bucket_id
            )

            if rule_conflict and not confirm_rule_update:
                new_bucket = db.get_bucket(bucket_id)
                if new_bucket is None:
                    raise ValueError(f"Bucket {bucket_id} not found")
                return render_template(
                    "expense_rule_confirm.html",
                    expense=expense,
                    matched_rule=matched_rule,
                    new_bucket=new_bucket,
                    exclude_from_report=exclude_from_report,
                    redirect_kwargs=redirect_kwargs,
                    page="expenses",
                )

            db.update_expense(
                expense_id,
                bucket_id=bucket_id,
                exclude_from_report=exclude_from_report,
                confirm=bucket_id is not None,
            )
            if (
                update_rule
                and matched_rule is not None
                and bucket_id is not None
                and matched_rule.bucket_id != bucket_id
            ):
                db.upsert_merchant_rule(
                    merchant_pattern=expense.merchant,
                    bucket_id=bucket_id,
                    match_type=matched_rule.match_type,
                    confirmed_by_user=True,
                )
                flash("Expense updated and merchant rule changed.", "success")
            else:
                flash("Expense updated.", "success")
        except (ValueError, TypeError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("expenses_page", **redirect_kwargs))

    @app.post("/expenses/<int:expense_id>/delete")
    def delete_expense(expense_id: int):
        db, _ = _services()
        month = request.form.get("month", date.today().strftime("%Y-%m"))
        status_filter = request.form.get("status", "").strip()
        search = request.form.get("q", "").strip()
        person = request.form.get("person", "").strip()
        unassigned = request.form.get("unassigned") == "1"
        filter_bucket_id = request.form.get("filter_bucket_id", "").strip()
        try:
            expense = db.get_expense(expense_id)
            if expense is None:
                raise ValueError(f"Expense {expense_id} not found")
            db.delete_expense(expense_id)
            flash(f"Deleted {format_merchant(expense.merchant)}.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(
            url_for(
                "expenses_page",
                month=month,
                status=status_filter,
                q=search,
                person=person,
                bucket_id=filter_bucket_id or None,
                unassigned=1 if unassigned else None,
            )
        )

    @app.route("/groups")
    def groups():
        db, _ = _services()
        merchants = db.distinct_merchants(pending_only=True)
        dismissed = db.list_dismissed_group_keys()
        suggestions = (
            analyze_merchants(merchants, dismissed_keys=dismissed) if merchants else []
        )
        bucket_options = _bucket_options(db, assignable_only=True)
        enriched = [
            {
                "suggestion": suggestion,
                "default_bucket_id": resolve_suggested_bucket_id(
                    suggestion.suggested_bucket,
                    bucket_options,
                ),
            }
            for suggestion in suggestions
        ]
        return render_template(
            "groups.html",
            enriched_suggestions=enriched,
            bucket_options=bucket_options,
            page="groups",
        )

    @app.post("/groups/apply")
    def apply_group():
        _, sync = _services()
        merchant_list = [item.strip() for item in request.form.getlist("merchants") if item.strip()]
        bucket_id_raw = request.form.get("bucket_id", "").strip()
        if not merchant_list or not bucket_id_raw:
            flash("Select at least one merchant and a bucket.", "error")
            return redirect(url_for("groups"))
        try:
            updated = sync.apply_group_suggestion_by_id(
                merchant_list,
                int(bucket_id_raw),
                MatchType.EXACT,
            )
            flash(f"Applied bucket to {updated} expense(s) and saved rule(s).", "success")
        except (ValueError, TypeError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("groups"))

    @app.post("/groups/dismiss")
    def dismiss_group():
        db, _ = _services()
        group_key = request.form.get("group_key", "").strip()
        if not group_key:
            flash("Missing group to dismiss.", "error")
            return redirect(url_for("groups"))
        db.dismiss_merchant_group(group_key)
        flash("Suggestion dismissed. Assign these merchants individually in Review.", "success")
        return redirect(url_for("groups"))

    @app.route("/buckets")
    def buckets_page():
        db, _ = _services()
        tree = build_bucket_tree(db.list_buckets())
        parent_options = db.list_top_level_buckets()
        return render_template(
            "buckets.html",
            bucket_tree=tree,
            parent_options=parent_options,
            page="buckets",
        )

    @app.post("/buckets/create")
    def create_bucket():
        db, _ = _services()
        name = request.form.get("name", "").strip()
        parent_raw = request.form.get("parent_id", "").strip()
        parent_id = int(parent_raw) if parent_raw else None
        exclude_from_report = request.form.get("exclude_from_report") == "on"
        try:
            bucket = db.create_bucket(
                name,
                parent_id=parent_id,
                exclude_from_report=exclude_from_report,
            )
            flash(f"Created bucket '{bucket.display_path}'.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("buckets_page"))

    @app.post("/buckets/<int:bucket_id>/edit")
    def edit_bucket(bucket_id: int):
        db, _ = _services()
        name = request.form.get("name", "").strip()
        parent_raw = request.form.get("parent_id", "").strip()
        exclude_from_report = request.form.get("exclude_from_report") == "on"
        try:
            if parent_raw == "none":
                bucket = db.update_bucket(
                    bucket_id,
                    name=name,
                    clear_parent=True,
                    exclude_from_report=exclude_from_report,
                )
            elif parent_raw:
                bucket = db.update_bucket(
                    bucket_id,
                    name=name,
                    parent_id=int(parent_raw),
                    exclude_from_report=exclude_from_report,
                )
            else:
                bucket = db.update_bucket(
                    bucket_id,
                    name=name,
                    exclude_from_report=exclude_from_report,
                )
            flash(f"Updated bucket '{bucket.display_path}'.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("buckets_page"))

    @app.post("/buckets/<int:bucket_id>/delete")
    def delete_bucket(bucket_id: int):
        db, _ = _services()
        try:
            db.delete_bucket(bucket_id)
            flash("Bucket deleted.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("buckets_page"))

    @app.route("/report")
    def report():
        db, _ = _services()
        month = request.args.get("month", date.today().strftime("%Y-%m"))
        person = request.args.get("person") or None
        totals = db.monthly_totals(month, card_holder=person)
        excluded_totals = db.monthly_excluded_totals(month, card_holder=person)
        grand_total = sum(row[2] for row in totals)
        transaction_count = sum(row[3] for row in totals)
        excluded_total = sum(row[1] for row in excluded_totals)
        chart_slices = [
            {
                "label": bucket_name,
                "value": round(total, 2),
            }
            for _bucket_id, bucket_name, total, _count in totals
            if total > 0
        ]
        holders = sorted(
            {
                expense.card_holder
                for expense in db.list_expenses()
                if expense.card_holder
            }
        )
        return render_template(
            "report.html",
            totals=totals,
            excluded_totals=excluded_totals,
            excluded_total=excluded_total,
            month=month,
            person=person,
            holders=holders,
            grand_total=grand_total,
            transaction_count=transaction_count,
            chart_slices=chart_slices,
            page="report",
        )

    @app.route("/rules")
    def rules():
        db, _ = _services()
        search = request.args.get("q", "").strip()
        try:
            current_page = max(1, int(request.args.get("page", "1")))
        except ValueError:
            current_page = 1
        total_rules = db.count_merchant_rules(search=search or None)
        total_pages = max(1, (total_rules + RULES_PER_PAGE - 1) // RULES_PER_PAGE)
        current_page = min(current_page, total_pages)
        offset = (current_page - 1) * RULES_PER_PAGE
        rules_list = db.list_merchant_rules(
            search=search or None,
            limit=RULES_PER_PAGE,
            offset=offset,
        )
        bucket_options = _bucket_options(db, assignable_only=True)
        return render_template(
            "rules.html",
            rules=rules_list,
            bucket_options=bucket_options,
            search=search,
            current_page=current_page,
            total_pages=total_pages,
            total_rules=total_rules,
            rules_per_page=RULES_PER_PAGE,
            page="rules",
        )

    @app.post("/rules/<int:rule_id>/update")
    def update_rule(rule_id: int):
        db, _ = _services()
        search = request.form.get("q", "").strip()
        page = request.form.get("page", "1").strip()
        bucket_id_raw = request.form.get("bucket_id", "").strip()
        try:
            if not bucket_id_raw:
                raise ValueError("Pick a bucket first.")
            rule = db.update_merchant_rule_bucket(rule_id, int(bucket_id_raw))
            flash(f"Updated rule for {format_merchant(rule.merchant_pattern)}.", "success")
        except (ValueError, TypeError) as exc:
            flash(str(exc), "error")
        redirect_kwargs: dict[str, str | int] = {}
        if search:
            redirect_kwargs["q"] = search
        if page.isdigit() and int(page) > 1:
            redirect_kwargs["page"] = int(page)
        return redirect(url_for("rules", **redirect_kwargs))

    @app.route("/settings", methods=["GET", "POST"])
    def settings_page():
        db, sync = _services()
        tenant = sync.tenant
        auth_db = build_global_db(settings)
        user = None if settings.auth_disabled else current_user(auth_db)
        if request.method == "POST":
            from expenses_tracker.config import _parse_card_holders

            name = request.form.get("household_name", "").strip()
            card_holders_raw = request.form.get("card_holders", "").strip()
            gmail_query = request.form.get("gmail_search_query", "").strip()
            holders = (
                _parse_card_holders(card_holders_raw)
                if card_holders_raw
                else tenant.card_holders
            )
            auth_db = build_global_db(settings)
            auth_db.update_tenant_settings(
                tenant.id,
                name=name or tenant.name,
                card_holders=holders,
                gmail_search_query=gmail_query or tenant.gmail_search_query,
            )
            flash("Household settings saved.", "success")
            return redirect(url_for("settings_page"))
        card_holders_text = ",".join(
            f"{last_four}:{name}" for last_four, name in sorted(tenant.card_holders.items())
        )
        return render_template(
            "settings.html",
            tenant=tenant,
            card_holders_text=card_holders_text,
            gmail_connected=bool(tenant.gmail_token_json),
            notify_email=user.notify_email if user is not None else False,
            page="settings",
        )

    @app.route("/settings/notifications", methods=["POST"])
    def settings_notifications():
        if settings.auth_disabled:
            flash("Notification preferences require login.", "error")
            return redirect(url_for("settings_page"))
        auth_db = build_global_db(settings)
        user = current_user(auth_db)
        if user is None:
            return redirect(url_for("login", next=url_for("settings_page")))
        notify_email = request.form.get("notify_email") == "1"
        auth_db.update_user_notification_prefs(user.id, notify_email=notify_email)
        flash("Notification preferences saved.", "success")
        return redirect(url_for("settings_page"))

    @app.route("/settings/gmail/connect")
    def gmail_connect():
        _, sync = _services()
        tenant_id = sync.tenant.id
        redirect_uri = url_for("gmail_oauth_callback", _external=True)
        flow = GmailClient.create_web_flow(resolve_gmail_credentials_path(settings), redirect_uri)
        authorization_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        session["gmail_oauth_state"] = state
        session["gmail_oauth_tenant_id"] = tenant_id
        session["gmail_oauth_code_verifier"] = flow.code_verifier
        return redirect(authorization_url)

    @app.route("/settings/gmail/callback")
    def gmail_oauth_callback():
        state = session.pop("gmail_oauth_state", None)
        tenant_id = session.pop("gmail_oauth_tenant_id", None)
        code_verifier = session.pop("gmail_oauth_code_verifier", None)
        if not state or state != request.args.get("state") or tenant_id is None:
            flash("Gmail authorization failed.", "error")
            return redirect(url_for("settings_page"))
        if not code_verifier:
            flash("Gmail authorization failed: session expired. Try connecting again.", "error")
            return redirect(url_for("settings_page"))
        redirect_uri = url_for("gmail_oauth_callback", _external=True)
        flow = GmailClient.create_web_flow(
            resolve_gmail_credentials_path(settings),
            redirect_uri,
            code_verifier=code_verifier,
        )
        try:
            flow.fetch_token(authorization_response=request.url)
        except Exception as exc:
            flash(f"Gmail authorization failed: {exc}", "error")
            return redirect(url_for("settings_page"))
        credentials = flow.credentials
        auth_db = build_global_db(settings)
        auth_db.update_tenant_gmail_token(tenant_id, credentials.to_json())
        flash("Gmail connected for this household.", "success")
        return redirect(url_for("settings_page"))

    return app
