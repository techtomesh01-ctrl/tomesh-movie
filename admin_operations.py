"""Read-only owner operations dashboard for CINEMA WORLD."""
from flask import Blueprint, render_template, session, redirect, url_for, request

operations_bp = Blueprint("admin_operations", __name__)


@operations_bp.before_request
def _owner_only():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login", next=request.path))


@operations_bp.route("/admin/operations")
def admin_operations_home():
    # Import at request time to avoid an import cycle while app.py registers this blueprint.
    from app import (
        get_db, DATABASE_URL, R2_ACCOUNT_ID, R2_ACCESS_KEY_ID,
        R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_ENDPOINT,
        CASHFREE_APP_ID, CASHFREE_SECRET_KEY, BREVO_API_KEY, BREVO_FROM,
        MESSAGE_CENTRAL_CUSTOMER_ID, MESSAGE_CENTRAL_AUTH_TOKEN,
        get_r2_client,
    )

    stats = {
        "users": 0, "blocked_users": 0, "movies": 0, "views": 0,
        "orders": 0, "paid_orders": 0, "pending_orders": 0,
        "failed_orders": 0, "revenue": 0, "pending_uploads": 0,
        "active_subscriptions": 0,
    }
    recent_payments, recent_users, payment_statuses = [], [], []
    database_ok, database_message = False, "Not checked"
    r2_ok, r2_message = None, "Use the Check integrations button to test storage."
    conn = None
    try:
        conn = get_db(dict_rows=True)
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        database_ok, database_message = True, "Database connection successful."

        cur.execute("SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE COALESCE(is_blocked,FALSE)) AS blocked FROM customer_users")
        row = cur.fetchone() or {}
        stats["users"] = int(row.get("total") or 0)
        stats["blocked_users"] = int(row.get("blocked") or 0)

        cur.execute("SELECT COUNT(*) AS total, COALESCE(SUM(views),0) AS views FROM movies")
        row = cur.fetchone() or {}
        stats["movies"] = int(row.get("total") or 0)
        stats["views"] = int(row.get("views") or 0)

        cur.execute("""
            SELECT COUNT(*) AS total,
              COUNT(*) FILTER (WHERE UPPER(COALESCE(status,'')) IN ('PAID','SUCCESS','COMPLETED')) AS paid,
              COUNT(*) FILTER (WHERE UPPER(COALESCE(status,'')) IN ('FAILED','CANCELLED','EXPIRED','ERROR','REJECTED')) AS failed,
              COUNT(*) FILTER (WHERE UPPER(COALESCE(status,'')) NOT IN ('PAID','SUCCESS','COMPLETED','FAILED','CANCELLED','EXPIRED','ERROR','REJECTED')) AS pending,
              COALESCE(SUM(amount) FILTER (WHERE UPPER(COALESCE(status,'')) IN ('PAID','SUCCESS','COMPLETED')),0) AS revenue
            FROM payment_orders
        """)
        row = cur.fetchone() or {}
        stats["orders"] = int(row.get("total") or 0)
        stats["paid_orders"] = int(row.get("paid") or 0)
        stats["failed_orders"] = int(row.get("failed") or 0)
        stats["pending_orders"] = int(row.get("pending") or 0)
        stats["revenue"] = float(row.get("revenue") or 0)

        cur.execute("SELECT COUNT(*) AS n FROM creator_submissions WHERE UPPER(COALESCE(status,''))='PENDING'")
        stats["pending_uploads"] = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute("SELECT COUNT(*) AS n FROM customer_subscriptions WHERE status IN ('ACTIVE','BANK_APPROVAL_PENDING')")
        stats["active_subscriptions"] = int((cur.fetchone() or {}).get("n") or 0)

        cur.execute("""
            SELECT status, COUNT(*) AS n, COALESCE(SUM(amount),0) AS amount
            FROM payment_orders GROUP BY status ORDER BY COUNT(*) DESC LIMIT 12
        """)
        payment_statuses = cur.fetchall()
        cur.execute("""
            SELECT p.order_id,p.customer_id,p.payment_type,p.amount,p.status,p.created_at,
                   COALESCE(u.full_name,u.email,p.customer_id) AS customer_label,u.email
            FROM payment_orders p LEFT JOIN customer_users u ON u.customer_id=p.customer_id
            ORDER BY p.id DESC LIMIT 15
        """)
        recent_payments = cur.fetchall()
        cur.execute("""
            SELECT full_name,email,mobile,created_at,last_login_at,COALESCE(is_blocked,FALSE) AS is_blocked
            FROM customer_users ORDER BY id DESC LIMIT 15
        """)
        recent_users = cur.fetchall()
        cur.close()
    except Exception:
        database_ok, database_message = False, "Database check failed. Review Admin → System Health / Errors and Render logs."
    finally:
        if conn is not None:
            conn.close()

    if request.args.get("check") == "1":
        try:
            get_r2_client().list_objects_v2(Bucket=R2_BUCKET, MaxKeys=1)
            r2_ok, r2_message = True, "R2 bucket connection successful."
        except Exception:
            r2_ok, r2_message = False, "R2 connection failed. Check Admin → R2 Storage and Render logs."

    integrations = [
        {"name": "PostgreSQL", "configured": bool(DATABASE_URL), "ok": database_ok, "detail": database_message},
        {"name": "Cloudflare R2", "configured": bool(R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET and R2_ENDPOINT), "ok": r2_ok, "detail": r2_message},
        {"name": "Cashfree", "configured": bool(CASHFREE_APP_ID and CASHFREE_SECRET_KEY), "ok": None, "detail": "Credentials configured; live payment verification not run here."},
        {"name": "Brevo OTP", "configured": bool(BREVO_API_KEY and BREVO_FROM), "ok": None, "detail": "Configuration presence only; no OTP sent."},
        {"name": "Message Central", "configured": bool(MESSAGE_CENTRAL_CUSTOMER_ID and MESSAGE_CENTRAL_AUTH_TOKEN), "ok": None, "detail": "Configuration presence only; no SMS sent."},
    ]
    return render_template(
        "admin_operations.html", stats=stats, recent_payments=recent_payments,
        recent_users=recent_users, payment_statuses=payment_statuses,
        integrations=integrations, database_ok=database_ok,
    )


def register_admin_operations(app):
    app.register_blueprint(operations_bp)
