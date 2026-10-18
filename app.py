import os
import threading
import json
import secrets
import mimetypes
import re
import time
import base64
import hashlib
import hmac
import traceback
from collections import deque
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from io import BytesIO

import boto3
import psycopg2
from psycopg2 import pool as psycopg2_pool
from psycopg2.extras import RealDictCursor
from botocore.client import Config

from flask import (
    Flask,
    g,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    abort,
    Response,
    jsonify,
)

from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash


# ============================================================
# APP
# ============================================================

app = Flask(__name__)

# Format admin-facing timestamps in India Standard Time without changing stored data.
IST = timezone(timedelta(hours=5, minutes=30))

@app.template_filter("istdate")
def _template_ist_date(value, fmt="%d %b %Y"):
    if not value:
        return "—"
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(IST).strftime(fmt)
    except (TypeError, ValueError, AttributeError):
        return str(value)

@app.context_processor
def _inject_cinema_clock():
    return {"cinema_now_ist": datetime.now(timezone.utc).astimezone(IST)}

app.secret_key = (
    os.environ.get("SECRET_KEY", "").strip()
    or secrets.token_hex(32)
)

app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024

# Harden browser session cookies. HTTPS is used on the production Render site.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = True
# Keep customer login active across browser restarts; logout clears the session.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=365)


# Log only slow HTTP requests to help pinpoint performance bottlenecks in Render.
@app.before_request
def _start_request_timer():
    g._cinema_request_started_at = time.monotonic()
    # Refresh a persistent customer session on each visit so closing the browser
    # does not immediately send the customer back to the login page.
    if session.get("customer_logged_in"):
        session.permanent = True


@app.after_request
def _log_slow_request(response):
    started = getattr(g, "_cinema_request_started_at", None)
    if started is not None:
        elapsed = time.monotonic() - started
        if elapsed >= 2.0:
            print(
                "SLOW_REQUEST:",
                request.method,
                request.path,
                "status=" + str(response.status_code),
                "seconds=" + format(elapsed, ".2f"),
            )
    return response


# ============================================================
# ADMIN ERROR MONITOR
# ============================================================

# Recent failures stay in memory so the Owner panel can open even if the DB is down.
# Entries reset when the Render process restarts.
_CINEMA_ERROR_LOGS = deque(maxlen=200)
_CINEMA_ERROR_LOGS_LOCK = threading.Lock()


def _record_cinema_error(status, message, source="request", trace_text=""):
    entry = {
        "time": datetime.now(timezone.utc).astimezone(IST).strftime("%d %b %Y · %A · %I:%M:%S %p IST"),
        "status": int(status or 500),
        "method": str(getattr(request, "method", "SYSTEM"))[:12],
        "path": str(getattr(request, "path", "unknown"))[:300],
        "source": str(source)[:80],
        "message": str(message or "Server error")[:2000],
        "traceback": str(trace_text or "")[-8000:],
    }
    with _CINEMA_ERROR_LOGS_LOCK:
        _CINEMA_ERROR_LOGS.appendleft(entry)


@app.after_request
def _capture_server_error_response(response):
    # Capture server failures and common actionable HTTP errors, but skip 404s
    # because routine scans and mistyped URLs can otherwise flood the log.
    if response.status_code >= 500 or response.status_code in (400, 401, 403, 408, 413, 429):
        _record_cinema_error(
            response.status_code,
            "HTTP response returned an error (status %s)." % response.status_code,
            source="HTTP response",
        )
    return response


# ============================================================
# ADMIN
# ============================================================

ADMIN_USER = os.environ.get(
    "ADMIN_USER", "admin"
).strip() or "admin"

ADMIN_PASSWORD = os.environ.get(
    "ADMIN_PASSWORD", "change-me-now"
).strip() or "change-me-now"

# Lightweight brute-force protection for the owner login.
_ADMIN_LOGIN_GUARD = {}
_ADMIN_LOGIN_GUARD_LOCK = threading.Lock()
_ADMIN_LOGIN_WINDOW = 10 * 60
_ADMIN_LOGIN_MAX_FAILURES = 5


# ============================================================
# DATABASE
# ============================================================

DATABASE_URL = os.environ.get(
    "DATABASE_URL", ""
).strip()


# ============================================================
# CASHFREE
# ============================================================

CASHFREE_APP_ID = os.environ.get(
    "CASHFREE_APP_ID", ""
).strip()

CASHFREE_SECRET_KEY = os.environ.get(
    "CASHFREE_SECRET_KEY", ""
).strip()

CASHFREE_ENV = os.environ.get(
    "CASHFREE_ENV", "sandbox"
).strip().lower()

CASHFREE_API_VERSION = "2025-01-01"

if CASHFREE_ENV == "production":
    CASHFREE_API_URL = "https://api.cashfree.com/pg"
    CASHFREE_JS_MODE = "production"
else:
    CASHFREE_API_URL = "https://sandbox.cashfree.com/pg"
    CASHFREE_JS_MODE = "sandbox"


# ============================================================
# PRICING
# ============================================================

WATCH_PRICE = 1.00
DOWNLOAD_PRICE = 9.00
SHARE_PRICE = 49.00
PREMIUM_PRICE = 199.00
PREMIUM_DAYS = 365
ACTIVATION_PRICE = 1.00
FREE_WATCH_HOURS = 24
SUBSCRIPTION_AUTH_AMOUNT = 1.00
SUBSCRIPTION_PLAN_NAME = os.environ.get(
    "CASHFREE_SUBSCRIPTION_PLAN_NAME",
    "CINEMA WORLD Premium Monthly",
).strip() or "CINEMA WORLD Premium Monthly"
SUBSCRIPTION_MAX_CYCLES = 120
REFERRAL_REWARD_AMOUNT = 25.00
REFERRAL_PENDING_HOURS = 24
WITHDRAWAL_MIN_AMOUNT = 100.00
WITHDRAWAL_FEE_RATE = 0.05
WITHDRAWAL_PIN_LENGTH = 6
WITHDRAWAL_MAX_AMOUNT = 5000.00
SUPPORT_MAX_SCREENSHOT_BYTES = 5 * 1024 * 1024
CASHFREE_WEBHOOK_SECRET = os.environ.get(
    "CASHFREE_WEBHOOK_SECRET",
    "",
).strip()


# ============================================================
# FILE SETTINGS
# ============================================================

ALLOWED_VIDEOS = {
    "mp4",
    "mkv",
    "webm",
    "mov",
}

ALLOWED_POSTERS = {
    "jpg",
    "jpeg",
    "png",
    "webp",
}

MAX_VIDEO_SIZE = 4 * 1024 * 1024 * 1024
MAX_POSTER_SIZE = 25 * 1024 * 1024


# ============================================================
# R2
# ============================================================

R2_ACCOUNT_ID = os.environ.get(
    "R2_ACCOUNT_ID", ""
)

R2_ACCESS_KEY_ID = os.environ.get(
    "R2_ACCESS_KEY_ID", ""
)

R2_SECRET_ACCESS_KEY = os.environ.get(
    "R2_SECRET_ACCESS_KEY", ""
)

R2_BUCKET = os.environ.get(
    "R2_BUCKET", "tomesh-movies"
)

R2_ENDPOINT = os.environ.get(
    "R2_ENDPOINT", ""
)

R2_PUBLIC_URL = os.environ.get(
    "R2_PUBLIC_URL", ""
)


PART_SIZE = 10 * 1024 * 1024
PARALLEL_PARTS = 2
PRESIGNED_EXPIRES = 43200
MAX_MULTIPART_PARTS = 10000

VIDEO_PREFIX = "videos/"
POSTER_PREFIX = "posters/"


# ============================================================
# CLEAN ENV
# ============================================================

def clean_env_value(value):
    if value is None:
        return ""

    value = str(value)
    value = value.replace("\r", "")
    value = value.replace("\n", "")
    value = value.strip()

    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in ("'", '"')
    ):
        value = value[1:-1]

    return value.strip()


def clean_endpoint(value):
    value = clean_env_value(value).rstrip("/")

    bucket_suffix = "/" + R2_BUCKET

    if value.endswith(bucket_suffix):
        value = value[:-len(bucket_suffix)]

    return value.rstrip("/")


R2_ACCOUNT_ID = clean_env_value(R2_ACCOUNT_ID)
R2_ACCESS_KEY_ID = clean_env_value(R2_ACCESS_KEY_ID)
R2_SECRET_ACCESS_KEY = clean_env_value(R2_SECRET_ACCESS_KEY)
R2_BUCKET = clean_env_value(R2_BUCKET)
R2_ENDPOINT = clean_endpoint(R2_ENDPOINT)
R2_PUBLIC_URL = clean_env_value(R2_PUBLIC_URL).rstrip("/")

CASHFREE_APP_ID = clean_env_value(CASHFREE_APP_ID)
CASHFREE_SECRET_KEY = clean_env_value(CASHFREE_SECRET_KEY)
MESSAGE_CENTRAL_CUSTOMER_ID = clean_env_value(os.environ.get("MESSAGE_CENTRAL_CUSTOMER_ID", ""))
MESSAGE_CENTRAL_AUTH_TOKEN = clean_env_value(os.environ.get("MESSAGE_CENTRAL_AUTH_TOKEN", ""))

# ============================================================
# EMAIL OTP SETTINGS
# ============================================================

# SMTP variables are kept for compatibility with the existing Render
# environment, but OTP delivery now uses Brevo's HTTPS API instead of
# an SMTP socket. This avoids the Render SMTP connection timeout.
SMTP_HOST = clean_env_value(os.environ.get("SMTP_HOST", ""))
SMTP_PORT_RAW = clean_env_value(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = clean_env_value(os.environ.get("SMTP_USER", ""))
SMTP_PASSWORD = clean_env_value(os.environ.get("SMTP_PASSWORD", ""))
SMTP_FROM = clean_env_value(os.environ.get("SMTP_FROM", "")) or SMTP_USER
SMTP_USE_TLS = clean_env_value(
    os.environ.get("SMTP_USE_TLS", "true")
).lower() != "false"

try:
    SMTP_PORT = int(SMTP_PORT_RAW or "587")
except ValueError:
    SMTP_PORT = 587

BREVO_API_KEY = clean_env_value(
    os.environ.get("BREVO_API_KEY", "")
)
BREVO_FROM = clean_env_value(
    os.environ.get("BREVO_FROM", "")
) or SMTP_FROM
BREVO_FROM_NAME = clean_env_value(
    os.environ.get("BREVO_FROM_NAME", "CINEMA WORLD")
) or "CINEMA WORLD"
BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"

# ============================================================
# MOBILE OTP - MESSAGE CENTRAL
# ============================================================
MESSAGE_CENTRAL_BASE_URL = "https://cpaas.messagecentral.com"

OTP_LENGTH = 6
OTP_EXPIRY_MINUTES = 10
OTP_RESEND_SECONDS = 60
OTP_MAX_ATTEMPTS = 5
EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$"
)


# ============================================================
# JSON
# ============================================================

def json_ok(**kwargs):
    data = {"ok": True}
    data.update(kwargs)
    return jsonify(data)


def json_error(message, status=400, **kwargs):
    data = {
        "ok": False,
        "error": message,
    }
    data.update(kwargs)
    return jsonify(data), status


# ============================================================
# FILE HELPERS
# ============================================================

def get_extension(filename):
    filename = str(filename or "")

    if "." not in filename:
        return ""

    return filename.rsplit(
        ".", 1
    )[1].lower().strip()


def allowed_video(filename):
    return get_extension(filename) in ALLOWED_VIDEOS


def allowed_poster(filename):
    return get_extension(filename) in ALLOWED_POSTERS


def safe_filename(filename):
    filename = secure_filename(
        str(filename or "")
    )

    return filename or "file"


def content_type_for_key(key):
    ext = get_extension(key)

    mapping = {
        "mp4": "video/mp4",
        "mkv": "video/x-matroska",
        "webm": "video/webm",
        "mov": "video/quicktime",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }

    return (
        mapping.get(ext)
        or mimetypes.guess_type(str(key or ""))[0]
        or "application/octet-stream"
    )


# ============================================================
# R2 VALIDATION
# ============================================================

def validate_r2_key(key):
    key = str(key or "").strip()

    if not key:
        raise ValueError("R2 object key missing.")

    if "\r" in key or "\n" in key:
        raise ValueError("Invalid R2 object key.")

    if key.startswith(VIDEO_PREFIX):
        return key

    if key.startswith(POSTER_PREFIX):
        return key

    if key.startswith("home/"):
        return key

    if key.startswith("support/"):
        return key

    raise ValueError("Invalid R2 object prefix.")


# ============================================================
# DATABASE
# ============================================================

# Reuse PostgreSQL connections inside each Gunicorn worker instead of
# opening a brand-new TLS/database connection for every query.  The public
# get_db() API stays the same, so the rest of the application is unchanged.
_DB_POOL = None
_DB_POOL_LOCK = threading.Lock()


class _PooledConnection:
    def __init__(self, db_pool, conn, dict_rows=False):
        self._db_pool = db_pool
        self._conn = conn
        self._dict_rows = dict_rows
        self._returned = False

    def cursor(self, *args, **kwargs):
        if self._dict_rows and "cursor_factory" not in kwargs:
            kwargs["cursor_factory"] = RealDictCursor
        return self._conn.cursor(*args, **kwargs)

    def close(self):
        if self._returned:
            return

        conn = self._conn
        self._returned = True
        self._conn = None

        try:
            if conn is not None and not conn.closed:
                # A request that did not commit may have left an open or
                # failed transaction. Reset it before another request uses
                # this pooled connection.
                try:
                    conn.rollback()
                except Exception:
                    pass

            if conn is not None:
                self._db_pool.putconn(
                    conn,
                    close=bool(conn.closed),
                )
        except Exception:
            try:
                if conn is not None and not conn.closed:
                    conn.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def __getattr__(self, name):
        conn = object.__getattribute__(self, "_conn")
        if conn is None:
            raise RuntimeError("Database connection is already closed.")
        return getattr(conn, name)


def _get_db_pool():
    global _DB_POOL

    if _DB_POOL is not None:
        return _DB_POOL

    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not configured."
        )

    with _DB_POOL_LOCK:
        if _DB_POOL is None:
            _DB_POOL = psycopg2_pool.ThreadedConnectionPool(
                1,
                5,
                DATABASE_URL,
                connect_timeout=5,
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=10,
                keepalives_count=3,
            )

    return _DB_POOL


def get_db(dict_rows=False):
    db_pool = _get_db_pool()

    try:
        conn = db_pool.getconn()
    except Exception as exc:
        raise RuntimeError(
            "Unable to obtain a database connection: " + str(exc)
        ) from exc

    return _PooledConnection(
        db_pool,
        conn,
        dict_rows=dict_rows,
    )


def init_db():
    conn = get_db()

    try:
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS movies (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                category TEXT,
                description TEXT,
                poster TEXT,
                video TEXT,
                views INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute("""
            ALTER TABLE movies
            ADD COLUMN IF NOT EXISTS trending_position INTEGER
        """)
        # Existing records remain in the Movies catalogue by default.
        cur.execute("""
            ALTER TABLE movies
            ADD COLUMN IF NOT EXISTS content_type TEXT NOT NULL DEFAULT 'MOVIE'
        """)
        cur.execute("""
            UPDATE movies SET content_type='MOVIE'
            WHERE content_type IS NULL OR UPPER(content_type) NOT IN ('MOVIE','DRAMA')
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_movies_content_type_created
            ON movies(content_type, id DESC)
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        cur.execute("""
            ALTER TABLE movies ADD COLUMN IF NOT EXISTS creator_customer_id TEXT
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS creator_submissions (
                id SERIAL PRIMARY KEY,
                customer_id TEXT NOT NULL,
                title TEXT NOT NULL,
                content_type TEXT NOT NULL CHECK (content_type IN ('MOVIE','DRAMA')),
                category TEXT,
                description TEXT,
                poster TEXT,
                video TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                admin_note TEXT,
                movie_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_creator_submissions_status_created
            ON creator_submissions(status, created_at DESC)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS creator_view_events (
                id SERIAL PRIMARY KEY,
                movie_id INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
                customer_id TEXT NOT NULL,
                view_date DATE NOT NULL DEFAULT CURRENT_DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(movie_id, customer_id, view_date)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS creator_earnings (
                id SERIAL PRIMARY KEY,
                customer_id TEXT NOT NULL,
                movie_id INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
                view_date DATE NOT NULL,
                valid_views INTEGER NOT NULL DEFAULT 0,
                amount NUMERIC(12,2) NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(customer_id, movie_id, view_date)
            )
        """)
        cur.execute("ALTER TABLE creator_earnings ALTER COLUMN amount TYPE NUMERIC(14,4)")
        cur.execute("INSERT INTO settings(key,value) VALUES('creator_movie_rate_per_1000','50') ON CONFLICT(key) DO NOTHING")
        cur.execute("INSERT INTO settings(key,value) VALUES('creator_drama_rate_per_1000','30') ON CONFLICT(key) DO NOTHING")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS creator_payout_requests (
                id SERIAL PRIMARY KEY,
                customer_id TEXT NOT NULL,
                amount NUMERIC(12,2) NOT NULL CHECK (amount > 0),
                payout_method TEXT NOT NULL CHECK (payout_method IN ('UPI','BANK')),
                payout_details TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','APPROVED','PAID','REJECTED')),
                admin_note TEXT,
                transaction_ref TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TIMESTAMP,
                paid_at TIMESTAMP
            )
        """)
        # Add structured bank fields safely for existing deployments.
        cur.execute("ALTER TABLE creator_payout_requests ADD COLUMN IF NOT EXISTS bank_account_holder TEXT")
        cur.execute("ALTER TABLE creator_payout_requests ADD COLUMN IF NOT EXISTS bank_name TEXT")
        cur.execute("ALTER TABLE creator_payout_requests ADD COLUMN IF NOT EXISTS bank_account_number TEXT")
        cur.execute("ALTER TABLE creator_payout_requests ADD COLUMN IF NOT EXISTS bank_ifsc TEXT")
        cur.execute("ALTER TABLE creator_payout_requests ADD COLUMN IF NOT EXISTS bank_branch TEXT")
        cur.execute("ALTER TABLE creator_payout_requests ADD COLUMN IF NOT EXISTS bank_verified BOOLEAN NOT NULL DEFAULT FALSE")
        cur.execute("ALTER TABLE creator_payout_requests ADD COLUMN IF NOT EXISTS bank_verified_at TIMESTAMP")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_creator_payout_requests_status ON creator_payout_requests(status, created_at DESC)")
        cur.execute("INSERT INTO settings(key,value) VALUES('creator_min_payout','100') ON CONFLICT(key) DO NOTHING")



        # ----------------------------------------------------
        # MOVIE ENGAGEMENT
        # ----------------------------------------------------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS movie_likes (
                id SERIAL PRIMARY KEY,
                movie_id INTEGER NOT NULL,
                customer_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(movie_id, customer_id),
                FOREIGN KEY (movie_id) REFERENCES movies(id) ON DELETE CASCADE
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_movie_likes_movie ON movie_likes(movie_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS movie_reports (
                id SERIAL PRIMARY KEY,
                movie_id INTEGER NOT NULL,
                customer_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                details TEXT,
                status TEXT NOT NULL DEFAULT 'OPEN',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (movie_id) REFERENCES movies(id) ON DELETE CASCADE
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_movie_reports_status ON movie_reports(status, created_at DESC)")

        # ----------------------------------------------------
        # PAYMENT ORDERS
        # ----------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS payment_orders (
                id SERIAL PRIMARY KEY,
                order_id TEXT UNIQUE NOT NULL,
                customer_id TEXT NOT NULL,
                movie_id INTEGER,
                payment_type TEXT NOT NULL,
                amount NUMERIC(10,2) NOT NULL,
                status TEXT DEFAULT 'ACTIVE',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                paid_at TIMESTAMP
            )
        """)

        # Remove only duplicate unfinished activation orders when a real
        # activation payment already exists. This keeps Recent Payments clean
        # without touching successful payments or any other payment type.
        cur.execute("""
            DELETE FROM payment_orders p
            WHERE p.payment_type = 'activation'
              AND p.status = 'ACTIVE'
              AND EXISTS (
                  SELECT 1
                  FROM payment_orders paid
                  WHERE paid.customer_id = p.customer_id
                    AND paid.payment_type = 'activation'
                    AND paid.status = 'PAID'
              )
        """)

        # ----------------------------------------------------
        # CUSTOMER ACCESS
        # ----------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS customer_access (
                id SERIAL PRIMARY KEY,
                customer_id TEXT NOT NULL,
                movie_id INTEGER,
                watch_until TIMESTAMP,
                download_until TIMESTAMP,
                premium_until TIMESTAMP,
                share_until TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("ALTER TABLE customer_access ADD COLUMN IF NOT EXISTS share_until TIMESTAMP")

        # ----------------------------------------------------
        # CUSTOMER EMAIL ACCOUNTS
        # ----------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS customer_users (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                customer_id TEXT UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login_at TIMESTAMP
            )
        """)

        cur.execute("""
            ALTER TABLE customer_users
            ADD COLUMN IF NOT EXISTS full_name TEXT
        """)

        cur.execute("""
            ALTER TABLE customer_users
            ADD COLUMN IF NOT EXISTS mobile TEXT
        """)

        cur.execute("""
            ALTER TABLE customer_users
            ADD COLUMN IF NOT EXISTS password_hash TEXT
        """)

        cur.execute("""
            ALTER TABLE customer_users
            ADD COLUMN IF NOT EXISTS is_blocked BOOLEAN DEFAULT FALSE
        """)

        cur.execute("""
            ALTER TABLE customer_users
            ADD COLUMN IF NOT EXISTS block_reason TEXT
        """)

        cur.execute("""
            ALTER TABLE customer_users
            ADD COLUMN IF NOT EXISTS profile_photo TEXT
        """)

        cur.execute("""
            ALTER TABLE customer_users
            ADD COLUMN IF NOT EXISTS referral_code TEXT
        """)

        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_customer_users_referral_code
            ON customer_users(referral_code)
            WHERE referral_code IS NOT NULL
        """)

        cur.execute("""
            UPDATE customer_users
            SET referral_code = 'CW' || UPPER(SUBSTRING(MD5(customer_id || RANDOM()::text) FROM 1 FOR 10))
            WHERE referral_code IS NULL
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS referral_attributions (
                id SERIAL PRIMARY KEY,
                referrer_customer_id TEXT NOT NULL,
                referred_customer_id TEXT UNIQUE NOT NULL,
                referral_code TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_referral_attributions_referrer
            ON referral_attributions(referrer_customer_id)
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS referral_rewards (
                id SERIAL PRIMARY KEY,
                payment_order_id INTEGER UNIQUE NOT NULL,
                referrer_customer_id TEXT NOT NULL,
                referred_customer_id TEXT NOT NULL,
                amount NUMERIC(10,2) NOT NULL DEFAULT 25.00,
                status TEXT NOT NULL DEFAULT 'PENDING',
                available_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reversed_at TIMESTAMP,
                reason TEXT
            )
        """)

        # Keep the default aligned with the configured referral reward.
        cur.execute("ALTER TABLE referral_rewards ALTER COLUMN amount SET DEFAULT 25.00")

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_referral_rewards_referrer
            ON referral_rewards(referrer_customer_id, status, available_at)
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS withdrawal_requests (
                id SERIAL PRIMARY KEY,
                customer_id TEXT NOT NULL,
                amount NUMERIC(10,2) NOT NULL,
                payout_method TEXT NOT NULL DEFAULT 'UPI',
                payout_details TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                admin_note TEXT,
                transaction_ref TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_withdrawal_requests_customer
            ON withdrawal_requests(customer_id, id DESC)
        """)

        cur.execute("""ALTER TABLE customer_users ADD COLUMN IF NOT EXISTS withdrawal_pin_hash TEXT""")
        cur.execute("""ALTER TABLE customer_users ADD COLUMN IF NOT EXISTS withdrawal_pin_set_at TIMESTAMP""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS customer_bank_accounts (
                id SERIAL PRIMARY KEY, customer_id TEXT NOT NULL,
                account_holder_name TEXT NOT NULL, account_number TEXT NOT NULL,
                ifsc TEXT NOT NULL, bank_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING', verification_note TEXT,
                verification_ref TEXT, verified_at TIMESTAMP, rejected_at TIMESTAMP,
                last_verified_at TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""CREATE INDEX IF NOT EXISTS idx_customer_bank_accounts_customer ON customer_bank_accounts(customer_id, id DESC)""")
        cur.execute("""ALTER TABLE customer_bank_accounts ADD COLUMN IF NOT EXISTS withdrawal_hold_until TIMESTAMP""")
        cur.execute("""ALTER TABLE withdrawal_requests ADD COLUMN IF NOT EXISTS bank_account_id INTEGER""")
        cur.execute("""ALTER TABLE withdrawal_requests ADD COLUMN IF NOT EXISTS withdrawal_fee NUMERIC(10,2) NOT NULL DEFAULT 0""")
        cur.execute("""ALTER TABLE withdrawal_requests ADD COLUMN IF NOT EXISTS net_amount NUMERIC(10,2)""")
        cur.execute("""ALTER TABLE withdrawal_requests ADD COLUMN IF NOT EXISTS total_debit NUMERIC(10,2)""")
        cur.execute("""ALTER TABLE withdrawal_requests ADD COLUMN IF NOT EXISTS utr TEXT""")
        cur.execute("""ALTER TABLE withdrawal_requests ADD COLUMN IF NOT EXISTS approved_at TIMESTAMP""")
        cur.execute("""ALTER TABLE withdrawal_requests ADD COLUMN IF NOT EXISTS paid_at TIMESTAMP""")
        cur.execute("""ALTER TABLE withdrawal_requests ADD COLUMN IF NOT EXISTS rejection_reason TEXT""")
        cur.execute("""ALTER TABLE withdrawal_requests ADD COLUMN IF NOT EXISTS idempotency_key TEXT""")
        cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_withdrawal_idempotency ON withdrawal_requests(idempotency_key) WHERE idempotency_key IS NOT NULL""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wallet_ledger (
                id BIGSERIAL PRIMARY KEY, customer_id TEXT NOT NULL,
                entry_type TEXT NOT NULL, direction TEXT NOT NULL, amount NUMERIC(12,2) NOT NULL,
                reference_type TEXT, reference_id TEXT, description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""CREATE INDEX IF NOT EXISTS idx_wallet_ledger_customer ON wallet_ledger(customer_id, id DESC)""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS customer_notifications (
                id BIGSERIAL PRIMARY KEY, customer_id TEXT NOT NULL,
                title TEXT NOT NULL, message TEXT NOT NULL,
                notification_type TEXT NOT NULL DEFAULT 'INFO',
                is_read BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""CREATE INDEX IF NOT EXISTS idx_customer_notifications_customer ON customer_notifications(customer_id, id DESC)""")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS support_tickets (
                id SERIAL PRIMARY KEY,
                customer_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                screenshot_key TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                resolved_at TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_support_tickets_customer
            ON support_tickets(customer_id, id DESC)
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS support_messages (
                id SERIAL PRIMARY KEY,
                ticket_id INTEGER NOT NULL,
                sender_type TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (ticket_id) REFERENCES support_tickets(id) ON DELETE CASCADE
            )
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_support_messages_ticket
            ON support_messages(ticket_id, id)
        """)

        # Mobile OTP login uses the verified mobile as the primary login identity.
        # Existing email accounts remain compatible.
        cur.execute("""
            ALTER TABLE customer_users
            ALTER COLUMN email DROP NOT NULL
        """)

        cur.execute("""
            DROP INDEX IF EXISTS idx_customer_users_mobile
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS customer_subscriptions (
                id SERIAL PRIMARY KEY,
                customer_id TEXT NOT NULL,
                subscription_id TEXT UNIQUE NOT NULL,
                cf_subscription_id TEXT,
                subscription_session_id TEXT,
                status TEXT DEFAULT 'INITIALIZED',
                auth_status TEXT DEFAULT 'PENDING',
                premium_until TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_customer_subscriptions_customer
            ON customer_subscriptions(customer_id)
        """)

        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_customer_subscriptions_customer_active
            ON customer_subscriptions(customer_id)
            WHERE status IN ('INITIALIZED','ACTIVE','BANK_APPROVAL_PENDING')
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS customer_movie_list (
                customer_id TEXT NOT NULL,
                movie_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (customer_id, movie_id),
                FOREIGN KEY (movie_id) REFERENCES movies(id) ON DELETE CASCADE
            )
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_customer_movie_list_customer
            ON customer_movie_list(customer_id)
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS customer_activity (
                id SERIAL PRIMARY KEY,
                customer_id TEXT NOT NULL,
                action TEXT NOT NULL,
                path TEXT,
                method TEXT,
                user_agent TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ----------------------------------------------------
        # MOVIE SEARCH DEMAND
        # ----------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS movie_search_demands (
                id SERIAL PRIMARY KEY,
                search_term TEXT NOT NULL,
                normalized_term TEXT UNIQUE NOT NULL,
                search_count INTEGER NOT NULL DEFAULT 0,
                unique_users INTEGER NOT NULL DEFAULT 0,
                first_searched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_searched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_customer_id TEXT
            )
        """)

        cur.execute("""
            ALTER TABLE movie_search_demands
            ADD COLUMN IF NOT EXISTS fulfilled_movie_id INTEGER
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS movie_search_demand_users (
                id SERIAL PRIMARY KEY,
                demand_id INTEGER NOT NULL REFERENCES movie_search_demands(id) ON DELETE CASCADE,
                customer_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(demand_id, customer_id)
            )
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_movie_search_demands_count
            ON movie_search_demands(search_count DESC, last_searched_at DESC)
        """)

        # ----------------------------------------------------
        # MOVIE RATINGS
        # ----------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS movie_ratings (
                id SERIAL PRIMARY KEY,
                movie_id INTEGER NOT NULL,
                customer_id TEXT NOT NULL,
                rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(movie_id, customer_id),
                FOREIGN KEY (movie_id) REFERENCES movies(id) ON DELETE CASCADE
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_movie_ratings_movie
            ON movie_ratings(movie_id)
        """)

        # ----------------------------------------------------
        # MOVIE COMMENTS
        # ----------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS movie_comments (
                id SERIAL PRIMARY KEY,
                movie_id INTEGER NOT NULL,
                customer_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                comment TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (movie_id) REFERENCES movies(id) ON DELETE CASCADE
            )
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_movie_comments_movie
            ON movie_comments(movie_id, id DESC)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_movie_comments_customer
            ON movie_comments(customer_id)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_customer_activity_customer
            ON customer_activity(customer_id)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_customer_activity_created
            ON customer_activity(created_at DESC)
        """)

        # ----------------------------------------------------
        # EMAIL OTP CODES
        # ----------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS email_otps (
                id SERIAL PRIMARY KEY,
                email TEXT NOT NULL,
                otp_hash TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                attempts INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                used_at TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_customer_access_customer
            ON customer_access(customer_id)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_email_otps_email
            ON email_otps(email)
        """)

        # Message Central handles the OTP value itself. This table stores only the
        # mobile number, request id, timing and attempt state for our login flow.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mobile_otps (
                id SERIAL PRIMARY KEY,
                mobile TEXT NOT NULL,
                request_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                attempts INTEGER DEFAULT 0,
                used_at TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_mobile_otps_mobile
            ON mobile_otps(mobile)
        """)


        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_payment_orders_order
            ON payment_orders(order_id)
        """)

        conn.commit()
        cur.close()

    finally:
        conn.close()


# ============================================================
# SETTINGS
# ============================================================

def get_setting(key, default=""):
    conn = get_db()

    try:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT value
            FROM settings
            WHERE key = %s
            """,
            (key,),
        )

        row = cur.fetchone()
        cur.close()

        if not row:
            return default

        return row[0] or default

    finally:
        conn.close()


def set_setting(key, value):
    conn = get_db()

    try:
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO settings(key, value)
            VALUES(%s, %s)
            ON CONFLICT(key)
            DO UPDATE SET value = EXCLUDED.value
            """,
            (key, value),
        )

        conn.commit()
        cur.close()

    finally:
        conn.close()


# ============================================================
# MASTER WEBSITE ON/OFF CONTROL
# ============================================================

# Cache the maintenance switch briefly to avoid a database read on every
# public request. The persistent database value remains the source of truth.
_WEBSITE_SWITCH_CACHE = {"value": "1", "checked_at": 0.0}
_WEBSITE_SWITCH_CACHE_LOCK = threading.Lock()
_WEBSITE_SWITCH_CACHE_TTL = 15.0

@app.before_request
def master_website_switch():
    # Keep the owner panel reachable so the site can always be switched back on.
    if request.path.startswith("/admin") or request.path.startswith("/static/"):
        return None

    # Payment webhooks must continue processing even while the public site is down.
    if request.path.startswith("/webhooks/") or request.endpoint in {"health_check"}:
        return None

    try:
        now = time.monotonic()
        with _WEBSITE_SWITCH_CACHE_LOCK:
            if now - _WEBSITE_SWITCH_CACHE["checked_at"] >= _WEBSITE_SWITCH_CACHE_TTL:
                _WEBSITE_SWITCH_CACHE["value"] = get_setting("website_enabled", "1")
                _WEBSITE_SWITCH_CACHE["checked_at"] = now
            enabled = _WEBSITE_SWITCH_CACHE["value"]
    except Exception as exc:
        # Fail open if the settings database is temporarily unavailable.
        print("WEBSITE SWITCH CHECK ERROR:", repr(exc))
        return None

    if str(enabled).strip().lower() in {"0", "false", "off", "no"}:
        page = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CINEMA WORLD — Maintenance</title>
<style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#050506;color:#fff;font-family:Arial,Helvetica,sans-serif;padding:24px}
main{max-width:560px;text-align:center;padding:38px 28px;border:1px solid #29292f;border-radius:20px;background:linear-gradient(145deg,#141418,#09090c);box-shadow:0 24px 80px #0008}
.logo{font-size:14px;font-weight:900;letter-spacing:2px;color:#ff3340}h1{font-size:30px;margin:18px 0 10px}p{color:#b5b5bf;line-height:1.65;margin:0}
</style></head><body><main><div class="logo">CINEMA WORLD</div>
<h1>We’ll be back soon</h1><p>Website par abhi maintenance chal raha hai. Kripya kuch der baad dobara visit karein.</p>
</main></body></html>"""
        return Response(page, status=503, content_type="text/html; charset=utf-8")

    return None


# Cache ad settings briefly so every homepage request does not open
# three separate database queries. Admin changes appear within 30 seconds.
_ADS_CACHE = {"value": None, "checked_at": 0.0}
_ADS_CACHE_LOCK = threading.Lock()
_ADS_CACHE_TTL = 30.0


def get_ads():
    now = time.monotonic()
    with _ADS_CACHE_LOCK:
        if (
            _ADS_CACHE["value"] is None
            or now - _ADS_CACHE["checked_at"] >= _ADS_CACHE_TTL
        ):
            _ADS_CACHE["value"] = {
                "top": get_setting("ad_top", ""),
                "player": get_setting("ad_player", ""),
                "bottom": get_setting("ad_bottom", ""),
            }
            _ADS_CACHE["checked_at"] = now
        return dict(_ADS_CACHE["value"])


# ============================================================
# R2 CLIENT
# ============================================================

def get_r2_client():

    if not R2_ACCOUNT_ID:
        raise RuntimeError("R2_ACCOUNT_ID missing.")

    if not R2_ACCESS_KEY_ID:
        raise RuntimeError("R2_ACCESS_KEY_ID missing.")

    if not R2_SECRET_ACCESS_KEY:
        raise RuntimeError("R2_SECRET_ACCESS_KEY missing.")

    if not R2_BUCKET:
        raise RuntimeError("R2_BUCKET missing.")

    if not R2_ENDPOINT:
        raise RuntimeError("R2_ENDPOINT missing.")

    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            s3={
                "addressing_style": "path"
            },
            retries={
                "max_attempts": 5,
                "mode": "standard",
            },
        ),
    )


# ============================================================
# R2 URL
# ============================================================

def r2_public_url(key):

    if not key or not R2_PUBLIC_URL:
        return None

    return (
        R2_PUBLIC_URL.rstrip("/")
        + "/"
        + quote(
            str(key).lstrip("/"),
            safe="/",
        )
    )


def r2_presigned_url(
    key,
    expires=PRESIGNED_EXPIRES,
):
    key = validate_r2_key(key)

    client = get_r2_client()

    return client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": R2_BUCKET,
            "Key": key,
        },
        ExpiresIn=expires,
    )


def media_url(key):
    if not key:
        return None

    try:
        return r2_presigned_url(key)
    except Exception:
        public = r2_public_url(key)

        if public:
            return public

        raise


def r2_head(key):
    key = validate_r2_key(key)

    return get_r2_client().head_object(
        Bucket=R2_BUCKET,
        Key=key,
    )


def r2_delete(key):

    if not key:
        return False

    key = validate_r2_key(key)

    get_r2_client().delete_object(
        Bucket=R2_BUCKET,
        Key=key,
    )

    return True


# ============================================================
# ADMIN AUTH
# ============================================================

def admin_required(view_func):

    @wraps(view_func)
    def wrapper(*args, **kwargs):

        if not session.get(
            "admin_logged_in"
        ):
            return redirect(
                url_for("admin_login")
            )

        return view_func(
            *args,
            **kwargs
        )

    return wrapper

def creator_customer_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not session.get("customer_logged_in"):
            return redirect(url_for("login"))
        customer_id = session.get("customer_id")
        if customer_is_blocked(customer_id):
            session.clear()
            flash("Your account has been restricted by the administrator.", "error")
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)
    return wrapper



def r2_upload_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not session.get("admin_logged_in") and not session.get("customer_logged_in"):
            return json_error("Please login before uploading.", 401)
        if session.get("customer_logged_in") and customer_is_blocked(session.get("customer_id")):
            session.clear()
            return json_error("Account restricted.", 403)
        return view_func(*args, **kwargs)
    return wrapper


def enforce_r2_upload_key(key):
    # Customer sessions may only upload into isolated pending-content prefixes.
    if session.get("admin_logged_in"):
        return
    if not session.get("customer_logged_in"):
        raise ValueError("Login required.")
    if not (key.startswith("videos/creator-pending/") or key.startswith("posters/creator-pending/")):
        raise ValueError("Creator uploads must use the pending-content folder.")


# ============================================================
# CUSTOMER ID
# ============================================================

def get_customer_id():

    customer_id = session.get(
        "customer_id"
    )

    if not customer_id:
        customer_id = (
            "tm_"
            + secrets.token_hex(16)
        )

        session[
            "customer_id"
        ] = customer_id

    return customer_id


# ============================================================
# REFERRAL / EARNINGS HELPERS
# ============================================================

def normalize_referral_code(value):
    value = str(value or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{6,20}", value):
        return ""
    return value


def ensure_referral_code(customer_id):
    customer_id = str(customer_id or "").strip()
    if not customer_id:
        return ""

    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT referral_code FROM customer_users WHERE customer_id=%s LIMIT 1",
            (customer_id,),
        )
        row = cur.fetchone()
        if row and row.get("referral_code"):
            return str(row["referral_code"])

        for _ in range(5):
            code = "CW" + secrets.token_hex(5).upper()
            try:
                cur.execute(
                    "UPDATE customer_users SET referral_code=%s WHERE customer_id=%s AND referral_code IS NULL",
                    (code, customer_id),
                )
                if cur.rowcount:
                    conn.commit()
                    return code
                conn.rollback()
                cur.execute(
                    "SELECT referral_code FROM customer_users WHERE customer_id=%s LIMIT 1",
                    (customer_id,),
                )
                row = cur.fetchone()
                if row and row.get("referral_code"):
                    return str(row["referral_code"])
            except Exception:
                conn.rollback()
        return ""
    finally:
        conn.close()


def apply_referral_attribution(customer_id):
    customer_id = str(customer_id or "").strip()
    code = normalize_referral_code(session.get("referral_code"))
    if not customer_id or not code:
        return False

    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT customer_id FROM customer_users WHERE referral_code=%s LIMIT 1",
            (code,),
        )
        referrer = cur.fetchone()
        if not referrer or referrer["customer_id"] == customer_id:
            session.pop("referral_code", None)
            return False

        cur.execute(
            "SELECT 1 FROM referral_attributions WHERE referred_customer_id=%s LIMIT 1",
            (customer_id,),
        )
        if cur.fetchone():
            session.pop("referral_code", None)
            return False

        cur.execute(
            """
            INSERT INTO referral_attributions
            (referrer_customer_id, referred_customer_id, referral_code)
            VALUES(%s,%s,%s)
            ON CONFLICT (referred_customer_id) DO NOTHING
            """,
            (referrer["customer_id"], customer_id, code),
        )
        conn.commit()
        created = cur.rowcount > 0
        session.pop("referral_code", None)
        return created
    finally:
        conn.close()


def create_referral_reward_for_order(local_order):
    if not local_order or str(local_order.get("payment_type") or "").lower() != "premium":
        return False

    payment_order_id = local_order.get("id")
    referred_customer_id = str(local_order.get("customer_id") or "").strip()
    if not payment_order_id or not referred_customer_id:
        return False

    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT referrer_customer_id FROM referral_attributions WHERE referred_customer_id=%s LIMIT 1",
            (referred_customer_id,),
        )
        attribution = cur.fetchone()
        if not attribution:
            return False

        cur.execute(
            "SELECT 1 FROM referral_rewards WHERE payment_order_id=%s LIMIT 1",
            (payment_order_id,),
        )
        if cur.fetchone():
            return False

        cur.execute(
            """
            INSERT INTO referral_rewards
            (payment_order_id, referrer_customer_id, referred_customer_id, amount, status, available_at)
            VALUES(%s,%s,%s,%s,'PENDING',NOW() + (%s || ' hours')::interval)
            ON CONFLICT (payment_order_id) DO NOTHING
            """,
            (payment_order_id, attribution["referrer_customer_id"], referred_customer_id,
             REFERRAL_REWARD_AMOUNT, REFERRAL_PENDING_HOURS),
        )
        created = cur.rowcount > 0
        if created:
            referrer = attribution["referrer_customer_id"]
            cur.execute("""
                INSERT INTO wallet_ledger
                (customer_id,entry_type,direction,amount,reference_type,reference_id,description)
                VALUES(%s,'REFERRAL_PENDING','CREDIT',%s,'REFERRAL_REWARD',%s,'₹25 referral reward pending for 24 hours')
                ON CONFLICT DO NOTHING
            """,(referrer,REFERRAL_REWARD_AMOUNT,str(payment_order_id)))
            cur.execute("""
                INSERT INTO customer_notifications(customer_id,title,message,notification_type)
                VALUES(%s,'Referral reward pending',%s,'EARNINGS')
            """,(referrer,f"₹{REFERRAL_REWARD_AMOUNT:.0f} referral reward is pending for 24 hours."))
        conn.commit()
        return created
    finally:
        conn.close()


def settle_due_referral_rewards(customer_id=None):
    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        if customer_id:
            cur.execute("SELECT id,referrer_customer_id,amount FROM referral_rewards WHERE referrer_customer_id=%s AND status='PENDING' AND available_at<=NOW() FOR UPDATE",(customer_id,))
        else:
            cur.execute("SELECT id,referrer_customer_id,amount FROM referral_rewards WHERE status='PENDING' AND available_at<=NOW() FOR UPDATE")
        rows = cur.fetchall()
        for row in rows:
            cur.execute("UPDATE referral_rewards SET status='AVAILABLE',updated_at=NOW() WHERE id=%s AND status='PENDING'",(row["id"],))
            if cur.rowcount:
                cur.execute("""
                    INSERT INTO wallet_ledger(customer_id,entry_type,direction,amount,reference_type,reference_id,description)
                    VALUES(%s,'REFERRAL_AVAILABLE','CREDIT',%s,'REFERRAL_REWARD',%s,'Referral reward became available')
                    ON CONFLICT DO NOTHING
                """,(row["referrer_customer_id"],row["amount"],str(row["id"])))
                cur.execute("""
                    INSERT INTO customer_notifications(customer_id,title,message,notification_type)
                    VALUES(%s,'Referral reward available',%s,'EARNINGS')
                """,(row["referrer_customer_id"],f"₹{float(row['amount']):.0f} referral reward is now available to withdraw."))
        conn.commit()
    finally:
        conn.close()


def get_earnings_summary(customer_id):
    settle_due_referral_rewards(customer_id)
    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN status IN ('PENDING','AVAILABLE') THEN amount ELSE 0 END),0) AS total_earned,
                COALESCE(SUM(CASE WHEN status='PENDING' THEN amount ELSE 0 END),0) AS pending,
                COALESCE(SUM(CASE WHEN status='AVAILABLE' THEN amount ELSE 0 END),0) AS credited
            FROM referral_rewards
            WHERE referrer_customer_id=%s
            """,
            (customer_id,),
        )
        rewards = cur.fetchone() or {}
        cur.execute("""
            SELECT COALESCE(SUM(COALESCE(total_debit,amount)),0) AS reserved
            FROM withdrawal_requests
            WHERE customer_id=%s AND status IN ('PENDING','PROCESSING','APPROVED','PAID')
        """,(customer_id,))
        reserved = cur.fetchone() or {}
        credited = float(rewards.get("credited") or 0)
        reserved_amount = float(reserved.get("reserved") or 0)
        available = max(0.0, credited - reserved_amount)
        cur.execute("""
            SELECT COALESCE(SUM(CASE WHEN status='PAID' THEN COALESCE(net_amount,amount) ELSE 0 END),0) AS withdrawn,
                   COALESCE(SUM(CASE WHEN status='PAID' THEN COALESCE(withdrawal_fee,0) ELSE 0 END),0) AS fees
            FROM withdrawal_requests WHERE customer_id=%s
        """,(customer_id,))
        paid_stats = cur.fetchone() or {}

        cur.execute(
            "SELECT COUNT(*) AS n FROM referral_attributions WHERE referrer_customer_id=%s",
            (customer_id,),
        )
        referrals = int((cur.fetchone() or {}).get("n") or 0)

        return {
            "total_earned": float(rewards.get("total_earned") or 0),
            "pending": float(rewards.get("pending") or 0),
            "credited": credited,
            "reserved": reserved_amount,
            "available": available,
            "withdrawn": float(paid_stats.get("withdrawn") or 0),
            "withdrawal_fees": float(paid_stats.get("fees") or 0),
            "referrals": referrals,
        }
    finally:
        conn.close()


# ============================================================
# EMAIL OTP LOGIN
# ============================================================

def normalize_email(value):
    return str(value or "").strip().lower()


def valid_email(email):
    return bool(EMAIL_RE.fullmatch(email or ""))


def mask_email(email):
    email = normalize_email(email)
    if "@" not in email:
        return email

    local, domain = email.split("@", 1)

    if len(local) <= 2:
        masked_local = local[:1] + "*"
    else:
        masked_local = local[:1] + "***" + local[-1:]

    return masked_local + "@" + domain


def generate_otp():
    return str(secrets.randbelow(900000) + 100000)


def otp_hash(email, otp):
    payload = (
        normalize_email(email)
        + ":"
        + str(otp)
    ).encode("utf-8")

    return hmac.new(
        str(app.secret_key).encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()


def send_otp_email(email, otp):
    """
    Send the login OTP through Brevo's HTTPS API.

    The old SMTP implementation opened a socket from Render and could hang
    during socket.create_connection(). Brevo's transactional API uses HTTPS,
    so the OTP request does not depend on an SMTP port being reachable.
    """
    if not BREVO_API_KEY:
        raise RuntimeError(
            "BREVO_API_KEY missing. Add BREVO_API_KEY in Render Environment."
        )

    if not BREVO_FROM:
        raise RuntimeError(
            "BREVO_FROM missing. Add the verified Brevo sender email in Render Environment."
        )

    otp_text = str(otp)
    text_content = (
        "CINEMA WORLD login verification\n\n"
        "Your one-time verification code is: "
        + otp_text
        + "\n\n"
        + "This OTP expires in "
        + str(OTP_EXPIRY_MINUTES)
        + " minutes.\n"
        + "Do not share this code with anyone.\n\n"
        + "If you did not request this code, you can ignore this email.\n\n"
        + "CINEMA WORLD"
    )

    html_content = (
        "<!doctype html>"
        "<html><body style=\"margin:0;padding:24px;background:#080808;"
        "font-family:Arial,sans-serif;color:#ffffff;\">"
        "<div style=\"max-width:520px;margin:auto;background:#121212;"
        "border:1px solid #2b2b2b;border-radius:16px;padding:28px;\">"
        "<h2 style=\"margin:0 0 12px;color:#ffc400;\">CINEMA WORLD</h2>"
        "<p style=\"color:#dddddd;\">Your login verification code is:</p>"
        "<div style=\"font-size:34px;font-weight:700;letter-spacing:8px;"
        "color:#ffffff;background:#1d1d1d;border-radius:12px;padding:18px;"
        "text-align:center;\">"
        + otp_text
        + "</div>"
        + "<p style=\"color:#aaaaaa;margin-top:18px;\">This OTP expires in "
        + str(OTP_EXPIRY_MINUTES)
        + " minutes.</p>"
        + "<p style=\"color:#888888;font-size:13px;\">Do not share this code with anyone.</p>"
        + "</div></body></html>"
    )

    payload = {
        "sender": {
            "name": BREVO_FROM_NAME,
            "email": BREVO_FROM,
        },
        "to": [
            {
                "email": email,
            }
        ],
        "subject": "CINEMA WORLD - Your Login OTP",
        "textContent": text_content,
        "htmlContent": html_content,
    }

    body = json.dumps(payload).encode("utf-8")

    req = Request(
        BREVO_API_URL,
        data=body,
        headers={
            "accept": "application/json",
            "api-key": BREVO_API_KEY,
            "content-type": "application/json",
            "user-agent": "Cinema-World/1.0",
        },
        method="POST",
    )

    try:
        with urlopen(req, timeout=15) as response:
            raw = response.read().decode(
                "utf-8",
                errors="replace",
            )

        if not raw:
            return

        try:
            result = json.loads(raw)
        except Exception:
            result = {}

        if isinstance(result, dict) and result.get("messageId"):
            print(
                "OTP EMAIL SENT:",
                result.get("messageId"),
            )

    except HTTPError as exc:
        raw = exc.read().decode(
            "utf-8",
            errors="replace",
        )
        print(
            "BREVO HTTP ERROR:",
            exc.code,
            raw,
        )
        raise RuntimeError(
            "Brevo email API "
            + str(exc.code)
            + ": "
            + raw
        )

    except URLError as exc:
        print(
            "BREVO CONNECTION ERROR:",
            repr(exc),
        )
        raise RuntimeError(
            "Brevo connection failed: "
            + str(exc)
        )

    except Exception as exc:
        print(
            "BREVO EMAIL ERROR:",
            repr(exc),
        )
        raise


def normalize_mobile(value):
    mobile = re.sub(r"\D", "", str(value or ""))
    if mobile.startswith("91") and len(mobile) == 12:
        mobile = mobile[2:]
    return mobile


def valid_mobile(mobile):
    return bool(re.fullmatch(r"[6-9]\d{9}", mobile or ""))


def mask_mobile(mobile):
    mobile = normalize_mobile(mobile)
    if len(mobile) != 10:
        return mobile
    return mobile[:2] + "******" + mobile[-2:]


def send_mobile_otp(mobile):
    if not MESSAGE_CENTRAL_CUSTOMER_ID:
        raise RuntimeError(
            "MESSAGE_CENTRAL_CUSTOMER_ID missing. Add it in Render Environment."
        )

    if not MESSAGE_CENTRAL_AUTH_TOKEN:
        raise RuntimeError(
            "MESSAGE_CENTRAL_AUTH_TOKEN missing. Add it in Render Environment."
        )

    mobile = normalize_mobile(mobile)

    if not valid_mobile(mobile):
        raise RuntimeError("Invalid mobile number.")

    url = (
        MESSAGE_CENTRAL_BASE_URL
        + "/verification/v3/send"
        + "?countryCode=91"
        + "&customerId="
        + quote(MESSAGE_CENTRAL_CUSTOMER_ID)
        + "&flowType=SMS"
        + "&mobileNumber="
        + quote(mobile)
        + "&otpLength="
        + str(OTP_LENGTH)
    )

    req = Request(
        url,
        data=b"",
        headers={
            "accept": "application/json",
            "authToken": MESSAGE_CENTRAL_AUTH_TOKEN,
            "user-agent": "Cinema-World/1.0",
        },
        method="POST",
    )

    try:
        with urlopen(req, timeout=20) as response:
            raw = response.read().decode(
                "utf-8",
                errors="replace",
            )
    except HTTPError as exc:
        raw = exc.read().decode(
            "utf-8",
            errors="replace",
        )
        print(
            "MESSAGE CENTRAL SEND HTTP ERROR:",
            exc.code,
            raw,
        )
        raise RuntimeError(
            "Message Central OTP send failed: "
            + str(exc.code)
            + " "
            + raw
        )
    except URLError as exc:
        print(
            "MESSAGE CENTRAL SEND CONNECTION ERROR:",
            repr(exc),
        )
        raise RuntimeError(
            "Message Central connection failed: "
            + str(exc)
        )

    try:
        result = json.loads(raw or "{}")
    except Exception:
        result = {}

    print(
        "MESSAGE CENTRAL SEND RESPONSE:",
        result,
    )

    data = result.get("data") or {}
    response_code = str(result.get("responseCode", ""))

    verification_id = (
        data.get("verificationId")
        or data.get("verficationId")
        or result.get("verificationId")
        or result.get("verficationId")
    )

    if response_code != "200" or not verification_id:
        raise RuntimeError(
            "Message Central OTP send failed: "
            + str(result)
        )

    return str(verification_id)


def verify_mobile_otp(mobile, otp):
    if not MESSAGE_CENTRAL_AUTH_TOKEN:
        raise RuntimeError(
            "MESSAGE_CENTRAL_AUTH_TOKEN missing."
        )

    mobile = normalize_mobile(mobile)
    otp = str(otp or "").strip()

    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT request_id
            FROM mobile_otps
            WHERE mobile = %s
              AND used_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (mobile,),
        )
        row = cur.fetchone()
        cur.close()
    finally:
        conn.close()

    if not row or not row.get("request_id"):
        return False, {
            "message": "OTP verification session not found."
        }

    verification_id = str(row["request_id"])

    url = (
        MESSAGE_CENTRAL_BASE_URL
        + "/verification/v3/validateOtp"
        + "?verificationId="
        + quote(verification_id)
        + "&code="
        + quote(otp)
    )

    req = Request(
        url,
        headers={
            "accept": "application/json",
            "authToken": MESSAGE_CENTRAL_AUTH_TOKEN,
            "user-agent": "Cinema-World/1.0",
        },
        method="GET",
    )

    try:
        with urlopen(req, timeout=20) as response:
            raw = response.read().decode(
                "utf-8",
                errors="replace",
            )
    except HTTPError as exc:
        raw = exc.read().decode(
            "utf-8",
            errors="replace",
        )
        print(
            "MESSAGE CENTRAL VERIFY HTTP ERROR:",
            exc.code,
            raw,
        )
        return False, raw
    except URLError as exc:
        print(
            "MESSAGE CENTRAL VERIFY CONNECTION ERROR:",
            repr(exc),
        )
        raise RuntimeError(
            "Message Central connection failed: "
            + str(exc)
        )

    try:
        result = json.loads(raw or "{}")
    except Exception:
        result = {}

    print(
        "MESSAGE CENTRAL VERIFY RESPONSE:",
        result,
    )

    data = result.get("data") or {}
    verification_status = str(
        data.get("verificationStatus", "")
    ).upper()

    ok = (
        str(result.get("responseCode", "")) == "200"
        and verification_status == "VERIFICATION_COMPLETED"
    )

    return ok, result


def bind_customer_mobile(mobile):
    """Create/restore a customer identity after mobile OTP verification.

    A mobile number is not a unique account key. Every new verified login can
    have its own customer_id; payment and subscription records remain linked
    to that customer_id.
    """
    mobile = normalize_mobile(mobile)
    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor(); cur.execute("SELECT COUNT(*) AS n FROM customer_users WHERE mobile=%s", (mobile,)); count=int((cur.fetchone() or {}).get("n") or 0)
    finally: conn.close()
    if count >= 5:
        raise RuntimeError("This mobile number has already reached the maximum limit of 5 accounts.")

    customer_id = "tm_" + secrets.token_hex(16)

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO customer_users
            (email, customer_id, mobile, last_login_at, referral_code)
            VALUES(NULL, %s, %s, NOW(), %s)
            """,
            (customer_id, mobile, "CW" + secrets.token_hex(5).upper()),
        )
        conn.commit()
        cur.close()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    session["customer_id"] = customer_id
    session["customer_logged_in"] = True
    session["customer_mobile"] = mobile
    session["customer_email"] = ""
    apply_referral_attribution(customer_id)
    return customer_id


@app.route("/login/request-mobile-otp", methods=["POST"])
def request_mobile_otp():
    mobile = normalize_mobile(request.form.get("mobile", ""))

    if not valid_mobile(mobile):
        flash("Please enter a valid 10-digit mobile number.", "error")
        return redirect(url_for("login"))

    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT created_at
            FROM mobile_otps
            WHERE mobile = %s AND used_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (mobile,),
        )
        previous = cur.fetchone()
        cur.close()
    finally:
        conn.close()

    if previous and previous.get("created_at"):
        elapsed = (datetime.now() - previous["created_at"]).total_seconds()
        if elapsed < OTP_RESEND_SECONDS:
            flash(
                "Please wait " + str(max(1, int(OTP_RESEND_SECONDS - elapsed))) + " seconds before requesting another OTP.",
                "error",
            )
            return redirect(url_for("login", step="otp"))

    try:
        request_id = send_mobile_otp(mobile)
    except Exception as exc:
        print("MOBILE OTP SEND ERROR:", repr(exc))
        flash(str(exc), "error")
        return redirect(url_for("login"))

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE mobile_otps
            SET used_at = NOW()
            WHERE mobile = %s AND used_at IS NULL
            """,
            (mobile,),
        )
        cur.execute(
            """
            INSERT INTO mobile_otps
            (mobile, request_id, expires_at, attempts)
            VALUES(%s, %s, %s, 0)
            """,
            (
                mobile,
                request_id,
                datetime.now() + timedelta(minutes=OTP_EXPIRY_MINUTES),
            ),
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()

    session["otp_mobile"] = mobile
    session["otp_mobile_sent_at"] = datetime.now().isoformat()
    flash("OTP sent to " + mask_mobile(mobile) + ".", "success")
    return redirect(url_for("login", step="otp"))


@app.route("/login/verify-mobile-otp", methods=["POST"])
def verify_mobile_otp_route():
    mobile = normalize_mobile(session.get("otp_mobile", ""))
    otp = str(request.form.get("otp", "")).strip()

    if not valid_mobile(mobile):
        flash("OTP session expired. Please request a new OTP.", "error")
        return redirect(url_for("login"))

    if not re.fullmatch(r"\d{4,9}", otp):
        flash("Enter the OTP received on your mobile.", "error")
        return redirect(url_for("login", step="otp"))

    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, expires_at, attempts
            FROM mobile_otps
            WHERE mobile = %s AND used_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (mobile,),
        )
        row = cur.fetchone()
        if not row:
            cur.close()
            flash("OTP expired or not found. Please request a new OTP.", "error")
            return redirect(url_for("login"))

        if int(row.get("attempts") or 0) >= OTP_MAX_ATTEMPTS:
            cur.execute("UPDATE mobile_otps SET used_at = NOW() WHERE id = %s", (row["id"],))
            conn.commit()
            cur.close()
            flash("Too many wrong attempts. Request a new OTP.", "error")
            return redirect(url_for("login"))

        if row["expires_at"] <= datetime.now():
            cur.execute("UPDATE mobile_otps SET used_at = NOW() WHERE id = %s", (row["id"],))
            conn.commit()
            cur.close()
            flash("OTP expired. Please request a new OTP.", "error")
            return redirect(url_for("login"))

        ok, result = verify_mobile_otp(mobile, otp)

        if not ok:
            new_attempts = int(row.get("attempts") or 0) + 1
            if new_attempts >= OTP_MAX_ATTEMPTS:
                cur.execute(
                    "UPDATE mobile_otps SET attempts = %s, used_at = NOW() WHERE id = %s",
                    (new_attempts, row["id"]),
                )
            else:
                cur.execute(
                    "UPDATE mobile_otps SET attempts = %s WHERE id = %s",
                    (new_attempts, row["id"]),
                )
            conn.commit()
            cur.close()
            remaining = max(0, OTP_MAX_ATTEMPTS - new_attempts)
            flash(
                "Wrong OTP. " + str(remaining) + " attempts left." if remaining else "Too many wrong attempts. Request a new OTP.",
                "error",
            )
            return redirect(url_for("login", step="otp" if remaining else "mobile"))

        cur.execute("UPDATE mobile_otps SET used_at = NOW() WHERE id = %s", (row["id"],))
        conn.commit()
        cur.close()
    finally:
        conn.close()

    try:
        bind_customer_mobile(mobile)
    except Exception as exc:
        print("CUSTOMER MOBILE BIND ERROR:", repr(exc))
        flash("Mobile verified, but account setup failed. Please try again.", "error")
        return redirect(url_for("login"))

    session.pop("otp_mobile", None)
    session.pop("otp_mobile_sent_at", None)
    flash("Mobile number verified. Welcome to CINEMA WORLD!", "success")
    return redirect(url_for("user_details"))


def bind_customer_email(email):
    """
    Connect the verified email to the current customer identity.

    If the visitor already paid while anonymous, their existing
    customer_id is preserved. If the email already has an account,
    old anonymous payment/access rows are moved to that account so
    the user does not lose access after OTP login.
    """

    email = normalize_email(email)
    old_customer_id = session.get("customer_id")

    if not old_customer_id:
        old_customer_id = (
            "tm_"
            + secrets.token_hex(16)
        )

    conn = get_db(
        dict_rows=True
    )

    try:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT id, customer_id
            FROM customer_users
            WHERE email = %s
            FOR UPDATE
            """,
            (email,),
        )

        user = cur.fetchone()

        if user:
            target_customer_id = user["customer_id"]

            if old_customer_id != target_customer_id:
                cur.execute(
                    """
                    UPDATE customer_access
                    SET customer_id = %s
                    WHERE customer_id = %s
                    """,
                    (
                        target_customer_id,
                        old_customer_id,
                    ),
                )

                cur.execute(
                    """
                    UPDATE payment_orders
                    SET customer_id = %s
                    WHERE customer_id = %s
                    """,
                    (
                        target_customer_id,
                        old_customer_id,
                    ),
                )

            cur.execute(
                """
                UPDATE customer_users
                SET last_login_at = NOW()
                WHERE id = %s
                """,
                (user["id"],),
            )

        else:
            target_customer_id = old_customer_id

            cur.execute(
                """
                INSERT INTO customer_users
                (
                    email,
                    customer_id,
                    last_login_at,
                    referral_code
                )
                VALUES(%s, %s, NOW(), %s)
                """,
                (
                    email,
                    target_customer_id,
                    "CW" + secrets.token_hex(5).upper(),
                ),
            )

        conn.commit()
        cur.close()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

    session["customer_id"] = target_customer_id
    session["customer_logged_in"] = True
    session["customer_email"] = email
    if not user:
        apply_referral_attribution(target_customer_id)

    return target_customer_id


@app.route("/login/start", methods=["POST"])
def login_start():
    # Clear stale OTP/login-routing state so a fresh login never loops
    # back to an old OTP screen.
    for _key in ("otp_email", "otp_sent_at", "otp_mobile", "otp_mobile_sent_at", "login_identifier"):
        session.pop(_key, None)

    value = str(request.form.get("email", "") or request.form.get("identifier", "")).strip()
    email = normalize_email(value)
    if valid_email(email):
        conn=get_db(dict_rows=True)
        try:
            cur=conn.cursor(); cur.execute("SELECT password_hash FROM customer_users WHERE email=%s LIMIT 1",(email,)); row=cur.fetchone()
        finally: conn.close()
        if row and row.get("password_hash"):
            session["login_identifier"] = email
            return redirect(url_for("login", step="password", identifier=email))
        return redirect(url_for("request_otp", email=email))
    mobile=normalize_mobile(value)
    if valid_mobile(mobile):
        conn=get_db(dict_rows=True)
        try:
            cur=conn.cursor(); cur.execute("SELECT password_hash FROM customer_users WHERE mobile=%s AND password_hash IS NOT NULL AND password_hash<>'' LIMIT 1",(mobile,)); row=cur.fetchone()
        finally: conn.close()
        if row and row.get("password_hash"):
            session["login_identifier"] = mobile
            return redirect(url_for("login", step="password", identifier=mobile))
        return _start_mobile_otp(mobile)
    flash("Please enter a valid email address or 10-digit mobile number.", "error")
    return redirect(url_for("login"))


def _start_mobile_otp(mobile):
    conn=get_db(dict_rows=True)
    try:
        cur=conn.cursor(); cur.execute("SELECT created_at FROM mobile_otps WHERE mobile=%s AND used_at IS NULL ORDER BY id DESC LIMIT 1",(mobile,)); previous=cur.fetchone()
    finally: conn.close()
    if previous and previous.get("created_at"):
        elapsed=(datetime.now()-previous["created_at"]).total_seconds()
        if elapsed < OTP_RESEND_SECONDS:
            flash("Please wait " + str(max(1,int(OTP_RESEND_SECONDS-elapsed))) + " seconds before requesting another OTP.", "error")
            return redirect(url_for("login", step="otp", mobile=mobile))
    try: request_id=send_mobile_otp(mobile)
    except Exception as exc:
        print("MOBILE OTP SEND ERROR:",repr(exc)); flash(str(exc),"error"); return redirect(url_for("login"))
    conn=get_db()
    try:
        cur=conn.cursor(); cur.execute("UPDATE mobile_otps SET used_at=NOW() WHERE mobile=%s AND used_at IS NULL",(mobile,)); cur.execute("INSERT INTO mobile_otps(mobile,request_id,expires_at,attempts) VALUES(%s,%s,%s,0)",(mobile,request_id,datetime.now()+timedelta(minutes=OTP_EXPIRY_MINUTES))); conn.commit()
    finally: conn.close()
    session["otp_mobile"]=mobile; session["otp_mobile_sent_at"]=datetime.now().isoformat()
    flash("OTP sent to " + mask_mobile(mobile) + ".","success")
    return redirect(url_for("login",step="otp",mobile=mobile))


@app.route("/login/password", methods=["POST"])
def customer_password_login():
    identifier=str(request.form.get("identifier","")).strip()
    password=str(request.form.get("password", ""))
    email=normalize_email(identifier); mobile=normalize_mobile(identifier)
    conn=get_db(dict_rows=True)
    try:
        cur=conn.cursor()
        if valid_email(email):
            cur.execute("SELECT customer_id,email,mobile,password_hash,is_blocked,block_reason FROM customer_users WHERE email=%s LIMIT 1",(email,)); rows=cur.fetchall()
        elif valid_mobile(mobile):
            cur.execute("SELECT customer_id,email,mobile,password_hash,is_blocked,block_reason FROM customer_users WHERE mobile=%s AND password_hash IS NOT NULL AND password_hash<>'' ORDER BY id ASC",(mobile,)); rows=cur.fetchall()
        else: rows=[]
    finally: conn.close()
    matched=None
    for row in rows:
        try:
            if row.get("password_hash") and check_password_hash(row["password_hash"], password): matched=row; break
        except Exception: pass
    if not matched:
        flash("Invalid email/mobile or password.","error")
        return redirect(url_for("login",step="password",identifier=identifier))
    if matched.get("is_blocked"):
        flash("Your account has been restricted by the administrator." + ((" Reason: " + str(matched.get("block_reason"))) if matched.get("block_reason") else ""), "error")
        return redirect(url_for("login"))
    session["customer_id"]=matched["customer_id"]; session["customer_logged_in"]=True; session["customer_email"]=matched.get("email") or ""; session["customer_mobile"]=matched.get("mobile") or ""
    conn=get_db()
    try:
        cur=conn.cursor(); cur.execute("UPDATE customer_users SET last_login_at=NOW() WHERE customer_id=%s",(matched["customer_id"],)); conn.commit()
    finally: conn.close()
    if not customer_has_completed_initial_payment(matched["customer_id"]): return redirect(url_for("user_details"))
    # Existing paid/authorized members should land directly on Member Home.
    return redirect(url_for("member_home"))


@app.route(
    "/login/request-otp",
    methods=["GET", "POST"],
)
def request_otp():

    email = normalize_email(
        request.values.get("email", "")
    )

    if not valid_email(email):
        flash(
            "Please enter a valid email address.",
            "error",
        )
        return redirect(
            url_for("login")
        )

    conn = get_db(
        dict_rows=True
    )

    otp_row_id = None

    try:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT created_at
            FROM email_otps
            WHERE email = %s
              AND used_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (email,),
        )

        previous = cur.fetchone()

        if previous and previous["created_at"]:
            elapsed = (
                datetime.now()
                - previous["created_at"]
            ).total_seconds()

            if elapsed < OTP_RESEND_SECONDS:
                wait_seconds = max(
                    1,
                    int(
                        OTP_RESEND_SECONDS
                        - elapsed
                    ),
                )

                cur.close()
                # Keep the email in session even when the resend cooldown
                # blocks a new OTP request. Otherwise the OTP page can
                # lose the email identity and verification shows
                # "OTP session expired".
                session["otp_email"] = email
                session["otp_sent_at"] = previous["created_at"].isoformat()
                return redirect(
                    url_for(
                        "login",
                        step="otp",
                        email=email,
                    )
                )

        # Invalidate older active OTPs for this email.
        cur.execute(
            """
            UPDATE email_otps
            SET used_at = NOW()
            WHERE email = %s
              AND used_at IS NULL
            """,
            (email,),
        )

        otp = generate_otp()
        expires_at = (
            datetime.now()
            + timedelta(
                minutes=OTP_EXPIRY_MINUTES
            )
        )

        cur.execute(
            """
            INSERT INTO email_otps
            (
                email,
                otp_hash,
                expires_at,
                attempts
            )
            VALUES(%s, %s, %s, 0)
            RETURNING id
            """,
            (
                email,
                otp_hash(email, otp),
                expires_at,
            ),
        )

        otp_row = cur.fetchone()
        otp_row_id = otp_row["id"]

        conn.commit()
        cur.close()

    except Exception:
        conn.rollback()
        conn.close()
        raise

    finally:
        try:
            conn.close()
        except Exception:
            pass

    try:
        send_otp_email(
            email,
            otp,
        )
    except Exception as exc:
        print(
            "OTP EMAIL SEND ERROR:",
            repr(exc),
        )

        conn = get_db()
        try:
            cur = conn.cursor()
            if otp_row_id:
                cur.execute(
                    """
                    UPDATE email_otps
                    SET used_at = NOW()
                    WHERE id = %s
                    """,
                    (otp_row_id,),
                )
            conn.commit()
            cur.close()
        finally:
            conn.close()

        flash(
            "OTP email send nahi hua. Render me BREVO_API_KEY aur BREVO_FROM check karo.",
            "error",
        )
        return redirect(
            url_for("login")
        )

    session["otp_email"] = email
    session["otp_sent_at"] = datetime.now().isoformat()

    flash(
        "OTP sent to " + mask_email(email) + ".",
        "success",
    )

    return redirect(
        url_for(
            "login",
            step="otp",
            email=email,
        )
    )


@app.route(
    "/login/verify-otp",
    methods=["POST"],
)
def verify_otp():

    email = normalize_email(
        session.get("otp_email", "")
        or request.form.get("email", "")
        or request.args.get("email", "")
    )

    otp = str(
        request.form.get("otp", "")
    ).strip()

    if not email or not valid_email(email):
        flash(
            "OTP session expired. Please request a new OTP.",
            "error",
        )
        return redirect(
            url_for("login")
        )

    if not re.fullmatch(
        r"\d{6}",
        otp,
    ):
        flash(
            "Enter the 6 digit OTP.",
            "error",
        )
        return redirect(
            url_for(
                "login",
                step="otp",
            )
        )

    conn = get_db(
        dict_rows=True
    )

    try:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT
                id,
                otp_hash,
                expires_at,
                attempts
            FROM email_otps
            WHERE email = %s
              AND used_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (email,),
        )

        row = cur.fetchone()

        if not row:
            cur.close()
            flash(
                "OTP expired or not found. Please request a new OTP.",
                "error",
            )
            return redirect(
                url_for("login")
            )

        if int(row["attempts"] or 0) >= OTP_MAX_ATTEMPTS:
            cur.execute(
                """
                UPDATE email_otps
                SET used_at = NOW()
                WHERE id = %s
                """,
                (row["id"],),
            )
            conn.commit()
            cur.close()
            flash(
                "Too many wrong attempts. Request a new OTP.",
                "error",
            )
            return redirect(
                url_for("login")
            )

        if row["expires_at"] <= datetime.now():
            cur.execute(
                """
                UPDATE email_otps
                SET used_at = NOW()
                WHERE id = %s
                """,
                (row["id"],),
            )
            conn.commit()
            cur.close()
            flash(
                "OTP expired. Please request a new OTP.",
                "error",
            )
            return redirect(
                url_for("login")
            )

        expected_hash = otp_hash(
            email,
            otp,
        )

        if not hmac.compare_digest(
            str(row["otp_hash"]),
            expected_hash,
        ):
            new_attempts = int(
                row["attempts"] or 0
            ) + 1

            if new_attempts >= OTP_MAX_ATTEMPTS:
                cur.execute(
                    """
                    UPDATE email_otps
                    SET
                        attempts = %s,
                        used_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        new_attempts,
                        row["id"],
                    ),
                )
            else:
                cur.execute(
                    """
                    UPDATE email_otps
                    SET attempts = %s
                    WHERE id = %s
                    """,
                    (
                        new_attempts,
                        row["id"],
                    ),
                )

            conn.commit()
            cur.close()

            remaining = max(
                0,
                OTP_MAX_ATTEMPTS - new_attempts,
            )

            if remaining:
                flash(
                    "Wrong OTP. "
                    + str(remaining)
                    + " attempts left.",
                    "error",
                )
            else:
                flash(
                    "Too many wrong attempts. Request a new OTP.",
                    "error",
                )

            return redirect(
                url_for(
                    "login",
                    step="otp" if remaining else "email",
                )
            )

        cur.execute(
            """
            UPDATE email_otps
            SET used_at = NOW()
            WHERE id = %s
            """,
            (row["id"],),
        )

        conn.commit()
        cur.close()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

    try:
        bind_customer_email(email)
    except Exception as exc:
        print(
            "CUSTOMER EMAIL BIND ERROR:",
            repr(exc),
        )
        flash(
            "Email verified, but account setup failed. Please try again.",
            "error",
        )
        return redirect(
            url_for("login")
        )

    session.pop(
        "otp_email",
        None,
    )
    session.pop(
        "otp_sent_at",
        None,
    )

    flash(
        "Email verified. Welcome to CINEMA WORLD!",
        "success",
    )

    # Existing paid/authorized members should land directly on Member Home.
    customer_id = session.get("customer_id")
    if customer_has_completed_initial_payment(customer_id):
        return redirect(
            url_for("member_home")
        )

    return redirect(
        url_for("user_details")
    )


# ============================================================
# CUSTOMER ACCESS
# ============================================================

def make_stream_token(movie_id, ttl_seconds=24 * 60 * 60):
    customer_id = get_customer_id()
    expires = int(time.time()) + int(ttl_seconds)

    payload = f"{movie_id}|{customer_id}|{expires}".encode("utf-8")
    data = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    signature = hmac.new(
        str(app.secret_key).encode("utf-8"),
        data.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()

    return data + "." + signature


def customer_is_blocked(customer_id):
    customer_id = str(customer_id or "").strip()
    if not customer_id:
        return False
    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT is_blocked FROM customer_users WHERE customer_id=%s LIMIT 1",
            (customer_id,),
        )
        row = cur.fetchone()
        return bool(row and row.get("is_blocked"))
    finally:
        conn.close()


def verify_stream_token(token, movie_id):
    try:
        token = str(token or "").strip()

        if "." not in token:
            return False

        data, signature = token.rsplit(".", 1)

        expected = hmac.new(
            str(app.secret_key).encode("utf-8"),
            data.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(signature, expected):
            return False

        padded = data + ("=" * (-len(data) % 4))
        payload = base64.urlsafe_b64decode(padded).decode("utf-8")
        token_movie_id, customer_id, expires = payload.split("|", 2)

        if int(token_movie_id) != int(movie_id):
            return False

        if int(expires) <= int(time.time()):
            return False

        if not customer_id or not customer_id.startswith("tm_"):
            return False

        if customer_is_blocked(customer_id):
            return False

        return True

    except Exception:
        return False


def access_for_movie(movie_id):
    customer_id = get_customer_id()

    if customer_is_blocked(customer_id):
        return {
            "watch": False,
            "download": False,
            "premium": False,
            "share": False,
        }

    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT movie_id, watch_until, download_until, premium_until, share_until
            FROM customer_access
            WHERE customer_id = %s
              AND (movie_id = %s OR movie_id IS NULL)
            ORDER BY id DESC
            """,
            (customer_id, movie_id),
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    now = datetime.now()
    permanent_watch = False
    temporary_watch = False
    premium = False
    share = False

    for row in rows:
        if (
            row.get("movie_id") is None
            and row["watch_until"] is None
            and row["premium_until"] is None
            and row["download_until"] is None
            and row.get("share_until") is None
        ):
            permanent_watch = True
        # Watch payments are account-wide for 24 hours. Ignore old
        # movie-specific watch rows so a previous movie payment cannot
        # accidentally unlock that movie again after its intended window.
        if (
            row.get("movie_id") is None
            and row["watch_until"] is not None
            and row["watch_until"] > now
        ):
            temporary_watch = True
        if row["premium_until"] is not None and row["premium_until"] > now:
            premium = True
        if row.get("share_until") is not None and row["share_until"] > now:
            share = True
        elif row.get("movie_id") is not None and row.get("share_until") is None and row.get("watch_until") is None and row.get("download_until") is None and row.get("premium_until") is None:
            share = True

    return {
        "watch": permanent_watch or temporary_watch or premium,
        "download": premium or any(r.get("download_until") is not None and r["download_until"] > now for r in rows),
        "premium": premium,
        "share": share,
    }


def grant_access(customer_id, movie_id, payment_type):
    conn=get_db()
    try:
        cur=conn.cursor(); now=datetime.now()
        if payment_type == "activation":
            until=now+timedelta(hours=FREE_WATCH_HOURS)
            cur.execute("INSERT INTO customer_access(customer_id,movie_id,watch_until,download_until,premium_until,share_until) VALUES(%s,NULL,%s,NULL,NULL,NULL)",(customer_id,until))
        elif payment_type == "watch":
            # One ₹1 Watch payment unlocks the whole account for 24 hours.
            # movie_id is intentionally NULL: every movie is watchable during
            # this active 24-hour window, including repeat views.
            until=now+timedelta(hours=FREE_WATCH_HOURS)
            cur.execute(
                "INSERT INTO customer_access(customer_id,movie_id,watch_until,download_until,premium_until,share_until) VALUES(%s,NULL,%s,NULL,NULL,NULL)",
                (customer_id,until),
            )
        elif payment_type == "download":
            until=now+timedelta(days=30)
            cur.execute("INSERT INTO customer_access(customer_id,movie_id,watch_until,download_until,premium_until,share_until) VALUES(%s,%s,NULL,%s,NULL,NULL)",(customer_id,movie_id,until))
        elif payment_type == "share":
            cur.execute("INSERT INTO customer_access(customer_id,movie_id,watch_until,download_until,premium_until,share_until) VALUES(%s,%s,NULL,NULL,NULL,NULL)",(customer_id,movie_id))
        elif payment_type == "premium":
            until=now+timedelta(days=PREMIUM_DAYS)
            cur.execute("DELETE FROM customer_access WHERE customer_id=%s AND movie_id IS NULL AND premium_until IS NOT NULL",(customer_id,))
            cur.execute("INSERT INTO customer_access(customer_id,movie_id,watch_until,download_until,premium_until,share_until) VALUES(%s,NULL,NULL,NULL,%s,NULL)",(customer_id,until))
        else:
            raise ValueError("Unsupported payment type.")
        conn.commit()
    finally: conn.close()


# ============================================================
# PREMIUM ACCESS CHECK
# ============================================================

def has_active_premium():

    customer_id = get_customer_id()

    conn = get_db()

    try:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT 1
            FROM customer_access
            WHERE customer_id = %s
              AND premium_until IS NOT NULL
              AND premium_until > NOW()
            LIMIT 1
            """,
            (customer_id,),
        )

        row = cur.fetchone()

        cur.close()

        return bool(row)

    finally:
        conn.close()


# ============================================================
# CASHFREE HTTP
# ============================================================

def cashfree_request(
    method,
    path,
    payload=None,
):

    if not CASHFREE_APP_ID:
        raise RuntimeError(
            "CASHFREE_APP_ID is missing."
        )

    if not CASHFREE_SECRET_KEY:
        raise RuntimeError(
            "CASHFREE_SECRET_KEY is missing."
        )

    url = (
        CASHFREE_API_URL.rstrip("/")
        + "/"
        + path.lstrip("/")
    )

    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "x-api-version": CASHFREE_API_VERSION,
        "x-client-id": CASHFREE_APP_ID,
        "x-client-secret": CASHFREE_SECRET_KEY,
    }

    body = None

    if payload is not None:
        body = json.dumps(
            payload
        ).encode("utf-8")

    req = Request(
        url,
        data=body,
        headers=headers,
        method=method.upper(),
    )

    try:

        with urlopen(
            req,
            timeout=30,
        ) as response:

            raw = response.read().decode(
                "utf-8"
            )

            if not raw:
                return {}

            return json.loads(raw)

    except HTTPError as exc:

        raw = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        print(
            "CASHFREE HTTP ERROR:",
            exc.code,
            raw,
        )

        try:
            detail = json.loads(raw)
        except Exception:
            detail = {
                "message": raw
            }

        raise RuntimeError(
            "Cashfree API "
            + str(exc.code)
            + ": "
            + str(detail)
        )

    except URLError as exc:

        raise RuntimeError(
            "Cashfree connection failed: "
            + str(exc)
        )


# ============================================================
# CREATE CASHFREE ORDER
# ============================================================

@app.route("/api/payment/create", methods=["POST"])
def create_payment():
    data=request.get_json(silent=True) or {}; payment_type=str(data.get("payment_type","")).strip().lower(); movie_id=data.get("movie_id"); phone=re.sub(r"\D","",str(data.get("phone", "")))
    if payment_type not in {"watch","download","share","premium","activation"}: return json_error("Invalid payment type.")
    if not re.fullmatch(r"[6-9]\d{9}",phone): return json_error("Enter a valid 10 digit Indian mobile number.")
    if payment_type == "activation" and customer_has_completed_initial_payment(session.get("customer_id")):
        return json_ok(already_paid=True, redirect_url=url_for("member_home"))
    if payment_type == "activation":
        # Do not create multiple activation orders when the user double-clicks
        # or refreshes the payment page. Old abandoned orders are harmless and
        # are cleared after 30 minutes so a genuinely failed attempt can retry.
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM payment_orders WHERE customer_id=%s AND payment_type='activation' AND status='ACTIVE' AND created_at < NOW() - INTERVAL '30 minutes'",
                (session.get("customer_id"),),
            )
            cur.execute(
                "SELECT order_id FROM payment_orders WHERE customer_id=%s AND payment_type='activation' AND status='ACTIVE' ORDER BY id DESC LIMIT 1",
                (session.get("customer_id"),),
            )
            existing_activation = cur.fetchone()
            conn.commit()
            if existing_activation:
                return json_error("Activation payment is already in progress. Please finish that payment first.", 409)
        finally:
            conn.close()
    if payment_type == "activation":
        movie=None; movie_id=None; amount=ACTIVATION_PRICE; description="CINEMA WORLD 24 Hour Activation"
    else:
        try: movie_id=int(movie_id)
        except Exception: return json_error("Invalid movie.")
        conn=get_db(dict_rows=True)
        try:
            cur=conn.cursor(); cur.execute("SELECT id,title FROM movies WHERE id=%s",(movie_id,)); movie=cur.fetchone()
        finally: conn.close()
        if not movie: return json_error("Movie not found.",404)
        amount={"watch":WATCH_PRICE,"download":DOWNLOAD_PRICE,"share":SHARE_PRICE,"premium":PREMIUM_PRICE}[payment_type]
        description=f"CINEMA WORLD {payment_type.title()} - {movie['title']}"
    customer_id=get_customer_id(); order_id="tm_"+payment_type+"_"+secrets.token_hex(10)
    return_url=url_for("cashfree_return",order_id=order_id,movie_id=movie_id or 0,_external=True)
    payload={"order_id":order_id,"order_amount":amount,"order_currency":"INR","customer_details":{"customer_id":customer_id,"customer_phone":phone},"order_meta":{"return_url":return_url},"order_note":description,"order_tags":{"movie_id":str(movie_id or ""),"payment_type":payment_type}}
    try:
        result=cashfree_request("POST","/orders",payload); payment_session_id=result.get("payment_session_id")
        if not payment_session_id: return json_error("Cashfree did not return payment session.",502,cashfree=result)
        conn=get_db()
        try:
            cur=conn.cursor(); cur.execute("INSERT INTO payment_orders(order_id,customer_id,movie_id,payment_type,amount,status) VALUES(%s,%s,%s,%s,%s,'ACTIVE')",(order_id,customer_id,movie_id,payment_type,amount)); conn.commit()
        finally: conn.close()
        return json_ok(order_id=order_id,payment_session_id=payment_session_id,amount=amount,mode=CASHFREE_JS_MODE)
    except Exception as exc:
        print("CREATE PAYMENT ERROR:",repr(exc)); return json_error(str(exc),500)

# ============================================================
# CASHFREE RETURN / VERIFY
# ============================================================

@app.route(
    "/payment/return"
)
def cashfree_return():

    order_id = (request.args.get("order_id", "").strip())

    movie_id = request.args.get(
        "movie_id",
        "",
    )

    if not order_id:
        flash(
            "Payment order ID missing.",
            "error",
        )

        return redirect(
            url_for(
                "home"
            )
        )

    conn = get_db(
        dict_rows=True
    )

    try:

        cur = conn.cursor()

        cur.execute(
            """
            SELECT *
            FROM payment_orders
            WHERE order_id = %s
            """,
            (order_id,),
        )

        local_order = cur.fetchone()

        cur.close()

    finally:
        conn.close()

    if not local_order:

        flash(
            "Payment order not found.",
            "error",
        )

        return redirect(
            url_for(
                "home"
            )
        )

    try:

        result = cashfree_request(
            "GET",
            "/orders/"
            + quote(
                order_id,
                safe="",
            ),
        )

        order_status = str(
            result.get(
                "order_status",
                "",
            )
        ).upper()

        cashfree_amount = float(
            result.get(
                "order_amount",
                0,
            )
        )

        local_amount = float(
            local_order["amount"]
        )

        if abs(
            cashfree_amount
            - local_amount
        ) > 0.001:

            raise RuntimeError(
                "Payment amount mismatch."
            )

        if order_status == "PAID":

            # ----------------------------------------------
            # Prevent duplicate granting
            # ----------------------------------------------

            if local_order["status"] != "PAID":

                conn = get_db()

                try:

                    cur = conn.cursor()

                    cur.execute(
                        """
                        UPDATE payment_orders
                        SET
                            status = 'PAID',
                            paid_at = NOW()
                        WHERE order_id = %s
                        """,
                        (order_id,),
                    )

                    conn.commit()
                    cur.close()

                finally:
                    conn.close()

                grant_access(
                    local_order[
                        "customer_id"
                    ],
                    local_order[
                        "movie_id"
                    ],
                    local_order[
                        "payment_type"
                    ],
                )
                create_referral_reward_for_order(local_order)

            # Restore the paid customer identity in the current browser session.
            # This is critical when the user returns from Cashfree or opens the
            # existing successful order manually: access_for_movie() checks this
            # session customer_id.
            session[
                "customer_id"
            ] = local_order[
                "customer_id"
            ]

            session["customer_id"] = local_order["customer_id"]
            session["customer_logged_in"] = True
            session["payment_success"] = True

            flash(
                "Payment successful. Access activated.",
                "success",
            )

            if local_order["payment_type"] == "activation":
                return redirect(url_for("member_home"))
            return redirect(url_for("movie_page", movie_id=local_order["movie_id"]))

        flash(
            "Payment was not completed. Status: "
            + order_status,
            "error",
        )

    except Exception as exc:

        print(
            "PAYMENT VERIFY ERROR:",
            repr(exc),
        )

        flash(
            "Payment verification failed.",
            "error",
        )

    try:
        target_movie = int(movie_id)
    except Exception:
        target_movie = local_order[
            "movie_id"
        ]

    return redirect(
        url_for(
            "movie_page",
            movie_id=target_movie,
        )
    )


# ============================================================
# ============================================================
# PUBLIC MOVIE REQUEST BOX
# ============================================================

@app.route("/request-movie-legacy", methods=["POST"])
def request_movie():
    search_term = re.sub(r"\s+", " ", str(request.form.get("movie_name") or "").strip())[:120]
    honeypot = str(request.form.get("website") or "").strip()
    from_movie_page = request.form.get("source") == "moviepage"
    try:
        return_movie_id = int(request.form.get("movie_id") or 0)
    except (TypeError, ValueError):
        return_movie_id = 0

    def request_return_target():
        if from_movie_page and return_movie_id > 0:
            return url_for("movie_page", movie_id=return_movie_id) + "#requestMovie"
        return url_for("home") + "#tmMovieRequest"

    if honeypot:
        return redirect(request_return_target())
    if len(search_term) < 2:
        flash("Movie ka naam kam se kam 2 characters ka likho.", "error")
        return redirect(request_return_target())

    normalized = search_term.casefold()
    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM movies WHERE LOWER(title) = LOWER(%s) LIMIT 1",
            (search_term,)
        )
        if cur.fetchone():
            conn.rollback()
            cur.close()
            flash("Ye movie CINEMA WORLD par pehle se available hai.", "error")
            return redirect(request_return_target())

        cur.execute("""
            INSERT INTO movie_search_demands
                (search_term, normalized_term, search_count, unique_users, last_customer_id)
            VALUES (%s, %s, 1, 0, NULL)
            ON CONFLICT (normalized_term)
            DO UPDATE SET
                search_count = movie_search_demands.search_count + 1,
                last_searched_at = CURRENT_TIMESTAMP,
                search_term = EXCLUDED.search_term,
                last_customer_id = NULL
            RETURNING id
        """, (search_term, normalized))
        conn.commit()
        cur.close()
        flash("Movie request bhej diya! CINEMA WORLD team ise review karegi.", "success")
    except Exception:
        conn.rollback()
        flash("Request abhi save nahi ho payi. Thodi der baad dobara try karo.", "error")
    finally:
        conn.close()
    return redirect(request_return_target())

# HOME
# ============================================================

# Cache the public homepage movie list briefly to avoid repeated DB reads
# during refreshes. Search and category results remain live and uncached.
_HOME_MOVIES_CACHE = {"at": 0.0, "movies": []}
_HOME_MOVIES_CACHE_LOCK = threading.Lock()
_HOME_MOVIES_CACHE_TTL = 10.0


@app.route("/")
def home():
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()

    # Always show the homepage at /. Opening a movie directly remains supported
    # through /movie/<movie_id>; do not redirect homepage visitors to last_movie_id.

    movies = None
    if not q and not category:
        now = time.monotonic()
        with _HOME_MOVIES_CACHE_LOCK:
            if now - _HOME_MOVIES_CACHE["at"] < _HOME_MOVIES_CACHE_TTL:
                movies = [dict(movie) for movie in _HOME_MOVIES_CACHE["movies"]]

    if movies is None:
        conn = get_db(dict_rows=True)
        try:
            cur = conn.cursor()

            if q:
                cur.execute(
                    """
                    SELECT * FROM movies
                    WHERE UPPER(COALESCE(content_type,'MOVIE'))='MOVIE'
                      AND (title ILIKE %s OR category ILIKE %s OR description ILIKE %s)
                    ORDER BY id DESC
                    """,
                    (f"%{q}%", f"%{q}%", f"%{q}%")
                )
            elif category:
                cur.execute(
                    """
                    SELECT * FROM movies
                    WHERE UPPER(COALESCE(content_type,'MOVIE'))='MOVIE'
                      AND category ILIKE %s
                    ORDER BY id DESC
                    """,
                    (f"%{category}%",)
                )
            else:
                cur.execute(
                    """
                    SELECT * FROM movies
                    WHERE UPPER(COALESCE(content_type,'MOVIE'))='MOVIE'
                    ORDER BY CASE WHEN trending_position IS NULL THEN 999999 ELSE trending_position END ASC, id DESC
                    """
                )

            movies = [dict(movie) for movie in cur.fetchall()]
            cur.close()
        finally:
            conn.close()

        if not q and not category:
            with _HOME_MOVIES_CACHE_LOCK:
                _HOME_MOVIES_CACHE["at"] = time.monotonic()
                _HOME_MOVIES_CACHE["movies"] = [dict(movie) for movie in movies]

    for movie in movies:
        poster_key = movie.get("poster")

        try:
            movie["poster_url"] = (
                media_url(poster_key)
                if poster_key
                else None
            )
        except Exception:
            movie["poster_url"] = None

    hero_image_key = get_setting("home_hero_image_key", "")
    try:
        hero_image_url = media_url(hero_image_key) if hero_image_key else ""
    except Exception:
        hero_image_url = ""

    return render_template(
        "index.html",
        movies=movies,
        ads=get_ads(),
        q=q,
        category=category,
        hero_image_url=hero_image_url,
    )


try:
    app.add_url_rule(
        "/",
        endpoint="index",
        view_func=home,
    )
except AssertionError:
    pass
    
# ============================================================
# MOVIE PAGE
# ============================================================

@app.route(
    "/share/movie/<int:movie_id>"
)
def shared_movie(movie_id):
    # A shared link opens the same movie page, but the movie page itself
    # requires a logged-in customer (or admin). No movie details/player
    # are exposed to visitors who are not logged in.
    return redirect(url_for("movie_page", movie_id=movie_id))


@app.route(
    "/movie/<int:movie_id>"
)
def movie_page(movie_id):

    # Remember the last movie so the customer can continue there on the next visit.
    if session.get("customer_logged_in"):
        session["last_movie_id"] = int(movie_id)
        session.permanent = True

    # Movie pages are private. A shared URL may point to this page, but
    # visitors must log in before the movie page is shown.
    if not session.get("customer_logged_in") and not session.get("admin_logged_in"):
        flash("Please login to view this movie.", "error")
        return redirect(url_for("login"))

    conn = get_db(
        dict_rows=True
    )

    try:

        cur = conn.cursor()

        cur.execute(
            """
            SELECT *
            FROM movies
            WHERE id = %s
            """,
            (movie_id,),
        )

        movie = cur.fetchone()

        if not movie:
            cur.close()
            abort(404)

        cur.execute(
            """
            UPDATE movies
            SET views = COALESCE(views,0) + 1
            WHERE id = %s
            """,
            (movie_id,),
        )

        conn.commit()
        cur.close()

    finally:
        conn.close()

    access = access_for_movie(
        movie_id
    )

    # Only accrue creator earnings when the logged-in viewer has watch access.
    creator_id = str(movie.get("creator_customer_id") or "")
    viewer_id = str(session.get("customer_id") or "")
    if access.get("watch") and creator_id and viewer_id and viewer_id != creator_id and session.get("customer_logged_in"):
        earnings_conn = get_db(dict_rows=True)
        try:
            earnings_cur = earnings_conn.cursor()
            earnings_cur.execute("""
                INSERT INTO creator_view_events(movie_id,customer_id,view_date)
                VALUES(%s,%s,CURRENT_DATE)
                ON CONFLICT(movie_id,customer_id,view_date) DO NOTHING
            """, (movie_id, viewer_id))
            if earnings_cur.rowcount:
                rate_key = "creator_drama_rate_per_1000" if str(movie.get("content_type") or "MOVIE").upper() == "DRAMA" else "creator_movie_rate_per_1000"
                earnings_cur.execute("SELECT value FROM settings WHERE key=%s", (rate_key,))
                rate_row = earnings_cur.fetchone()
                try:
                    rate = max(0.0, float((rate_row or {}).get("value") or 0))
                except (TypeError, ValueError):
                    rate = 0.0
                earnings_cur.execute("""
                    INSERT INTO creator_earnings(customer_id,movie_id,view_date,valid_views,amount)
                    VALUES(%s,%s,CURRENT_DATE,1,%s)
                    ON CONFLICT(customer_id,movie_id,view_date)
                    DO UPDATE SET valid_views=creator_earnings.valid_views+1,
                                  amount=creator_earnings.amount+EXCLUDED.amount
                """, (creator_id, movie_id, round(rate / 1000.0, 4)))
            earnings_conn.commit()
        except Exception as exc:
            earnings_conn.rollback()
            print("CREATOR EARNINGS ERROR:", repr(exc))
        finally:
            earnings_conn.close()

    video_key = movie.get(
        "video"
    )

    poster_key = movie.get(
        "poster"
    )

    movie["video_mime"] = (
        content_type_for_key(
            video_key
        )
        if video_key
        else "video/mp4"
    )

    # --------------------------------------------------------
    # Only give stream URL when access exists.
    # --------------------------------------------------------

    if video_key and access["watch"]:

        movie["video_url"] = url_for(
            "stream_movie",
            movie_id=movie_id,
            access_token=make_stream_token(movie_id),
        )

    else:

        movie["video_url"] = None

    if poster_key:

        try:
            movie["poster_url"] = media_url(
                poster_key
            )
        except Exception:
            movie["poster_url"] = None

    else:
        movie["poster_url"] = None

    movie["views"] = int(
        movie.get("views") or 0
    )

    rating_summary = {"average_rating": 0, "rating_count": 0}
    my_rating = 0
    ratings_conn = get_db(dict_rows=True)
    try:
        ratings_cur = ratings_conn.cursor()
        ratings_cur.execute("""
            SELECT COALESCE(ROUND(AVG(rating)::numeric, 1), 0) AS average_rating,
                   COUNT(*) AS rating_count
            FROM movie_ratings
            WHERE movie_id = %s
        """, (movie_id,))
        rating_summary = dict(ratings_cur.fetchone() or rating_summary)
        if session.get("customer_logged_in"):
            ratings_cur.execute("""
                SELECT rating FROM movie_ratings
                WHERE movie_id = %s AND customer_id = %s
            """, (movie_id, str(session.get("customer_id") or "")))
            own = ratings_cur.fetchone()
            my_rating = int((own or {}).get("rating") or 0)
        ratings_cur.close()
    finally:
        ratings_conn.close()

    like_summary = {"like_count": 0, "my_like": False}
    like_conn = get_db(dict_rows=True)
    try:
        lc = like_conn.cursor()
        lc.execute("SELECT COUNT(*) AS n FROM movie_likes WHERE movie_id=%s", (movie_id,))
        like_summary["like_count"] = int((lc.fetchone() or {}).get("n") or 0)
        if session.get("customer_logged_in"):
            lc.execute("SELECT 1 FROM movie_likes WHERE movie_id=%s AND customer_id=%s LIMIT 1", (movie_id, str(session.get("customer_id") or "")))
            like_summary["my_like"] = bool(lc.fetchone())
        lc.close()
    finally:
        like_conn.close()

    comments = []

    comments_conn = get_db(dict_rows=True)
    try:
        comments_cur = comments_conn.cursor()
        comments_cur.execute(
            """
            SELECT id, display_name, comment, created_at
            FROM movie_comments
            WHERE movie_id = %s
            ORDER BY id DESC
            LIMIT 100
            """,
            (movie_id,),
        )
        comments = comments_cur.fetchall()
        comments_cur.close()
    finally:
        comments_conn.close()

    comment_csrf = hmac.new(
        str(app.secret_key).encode("utf-8"),
        (str(session.get("customer_id", "")) + "|" + str(movie_id)).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return render_template(
        "movie.html",
        movie=movie,
        ads=get_ads(),
        access=access,
        comments=comments,
        comment_csrf=comment_csrf,
        rating_summary=rating_summary,
        my_rating=my_rating,
        like_summary=like_summary,
        cashfree_mode=CASHFREE_JS_MODE,
    )




# ============================================================
# CREATOR UPLOADS, MODERATION AND VIEW-BASED EARNINGS
# ============================================================

@app.route("/creator/upload", methods=["GET", "POST"])
@creator_customer_required
def creator_upload():
    if request.method == "POST":
        flash("Upload form submit nahi hua. Please form ko dobara submit karein.", "error")
        return redirect(url_for("creator_upload"))
    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT cs.id,cs.title,cs.content_type,cs.category,cs.description,cs.poster,cs.video,cs.status,cs.admin_note,cs.created_at,cs.movie_id,
                   COALESCE((SELECT m.views FROM movies m WHERE m.id=cs.movie_id),0) AS views,
                   COALESCE((SELECT COUNT(*) FROM movie_likes ml WHERE ml.movie_id=cs.movie_id),0) AS likes
            FROM creator_submissions cs WHERE cs.customer_id=%s
            ORDER BY cs.id DESC
        """, (str(session.get("customer_id") or ""),))
        submissions = [dict(row) for row in cur.fetchall()]
        for item in submissions:
            try:
                item["poster_url"] = media_url(item.get("poster")) if item.get("poster") else None
                item["video_url"] = media_url(item.get("video")) if item.get("video") else None
            except Exception:
                item["poster_url"] = None
                item["video_url"] = None
        customer_id = str(session.get("customer_id") or "")
        cur.execute("""
            SELECT COALESCE(SUM(amount),0) AS total,
                   COALESCE(SUM(valid_views),0) AS valid_views
            FROM creator_earnings WHERE customer_id=%s
        """, (customer_id,))
        earnings = dict(cur.fetchone() or {})
        cur.execute("""
            SELECT COALESCE(SUM(amount),0) AS reserved
            FROM creator_payout_requests
            WHERE customer_id=%s AND status IN ('PENDING','APPROVED','PAID')
        """, (customer_id,))
        reserved = float((cur.fetchone() or {}).get("reserved") or 0)
        total_earned = float(earnings.get("total") or 0)
        earnings["available"] = max(0.0, round(total_earned - reserved, 2))
        cur.execute("""
            SELECT id,amount,payout_method,payout_details,status,admin_note,transaction_ref,created_at,paid_at,
                   bank_account_holder,bank_name,bank_account_number,bank_ifsc,bank_branch,bank_verified,bank_verified_at
            FROM creator_payout_requests WHERE customer_id=%s ORDER BY id DESC LIMIT 50
        """, (customer_id,))
        payouts = [dict(row) for row in cur.fetchall()]
        cur.close()
    finally:
        conn.close()
    return render_template("creator_upload.html", submissions=submissions, earnings=earnings, payouts=payouts,
        movie_rate=get_setting("creator_movie_rate_per_1000", "50"),
        drama_rate=get_setting("creator_drama_rate_per_1000", "30"),
        min_payout=get_setting("creator_min_payout", "100"))



@app.route("/creator/payout-request", methods=["POST"])
@creator_customer_required
def creator_payout_request():
    customer_id = str(session.get("customer_id") or "")
    try:
        amount = round(float(request.form.get("amount") or 0), 2)
    except (TypeError, ValueError):
        flash("Enter a valid payout amount.", "error")
        return redirect(url_for("creator_upload"))
    method = str(request.form.get("payout_method") or "UPI").upper()
    details = str(request.form.get("payout_details") or "").strip()
    bank_holder = str(request.form.get("bank_account_holder") or "").strip()[:120]
    bank_name = str(request.form.get("bank_name") or "").strip()[:120]
    bank_account = "".join(ch for ch in str(request.form.get("bank_account_number") or "") if ch.isdigit())
    bank_ifsc = str(request.form.get("bank_ifsc") or "").strip().upper()
    bank_branch = str(request.form.get("bank_branch") or "").strip()[:120]
    min_payout = max(0.0, float(get_setting("creator_min_payout", "100") or 100))
    if amount <= 0 or amount < min_payout:
        flash("Minimum payout request is ₹%.2f." % min_payout, "error")
        return redirect(url_for("creator_upload"))
    if method not in {"UPI", "BANK"}:
        flash("Choose UPI or Bank Transfer.", "error")
        return redirect(url_for("creator_upload"))
    if method == "UPI":
        if not details or len(details) > 250 or "@" not in details:
            flash("Enter a valid UPI ID, for example name@bank.", "error")
            return redirect(url_for("creator_upload"))
    else:
        import re
        if not bank_holder or not bank_name or not (6 <= len(bank_account) <= 18) or not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", bank_ifsc):
            flash("Enter account holder, bank name, valid account number (6–18 digits) and IFSC (e.g. SBIN0001234).", "error")
            return redirect(url_for("creator_upload"))
        details = "Bank account submitted for admin verification."
    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        # Lock the creator's account row so concurrent requests cannot reserve the same balance.
        cur.execute("SELECT customer_id FROM customer_users WHERE customer_id=%s FOR UPDATE", (customer_id,))
        cur.execute("SELECT COALESCE(SUM(amount),0) AS total FROM creator_earnings WHERE customer_id=%s", (customer_id,))
        earned = float((cur.fetchone() or {}).get("total") or 0)
        cur.execute("""
            SELECT COALESCE(SUM(amount),0) AS reserved FROM creator_payout_requests
            WHERE customer_id=%s AND status IN ('PENDING','APPROVED','PAID')
        """, (customer_id,))
        reserved = float((cur.fetchone() or {}).get("reserved") or 0)
        available = max(0.0, round(earned - reserved, 2))
        if amount > available:
            conn.rollback()
            flash("Requested amount ₹%.2f is more than your available balance ₹%.2f." % (amount, available), "error")
            return redirect(url_for("creator_upload"))
        cur.execute("""
            INSERT INTO creator_payout_requests(
                customer_id,amount,payout_method,payout_details,
                bank_account_holder,bank_name,bank_account_number,bank_ifsc,bank_branch,bank_verified
            ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE)
        """, (customer_id, amount, method, details,
              bank_holder if method == "BANK" else None,
              bank_name if method == "BANK" else None,
              bank_account if method == "BANK" else None,
              bank_ifsc if method == "BANK" else None,
              bank_branch if method == "BANK" else None))
        conn.commit()
        flash("Payout request submitted. Admin will review and pay manually.", "success")
    except Exception as exc:
        conn.rollback()
        print("CREATOR PAYOUT REQUEST ERROR:", repr(exc))
        flash("Payout request could not be submitted.", "error")
    finally:
        conn.close()
    return redirect(url_for("creator_upload"))


@app.route("/api/creator/submission/save", methods=["POST"])
@creator_customer_required
def api_creator_submission_save():
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "").strip()
    category = str(data.get("category") or "").strip()
    description = str(data.get("description") or "").strip()
    content_type = str(data.get("content_type") or "MOVIE").strip().upper()
    video_key = str(data.get("video_key") or "").strip()
    poster_key = str(data.get("poster_key") or "").strip()
    if not title:
        return json_error("Title required.")
    if content_type not in {"MOVIE", "DRAMA"}:
        return json_error("Choose Movies or Dramas.")
    if not video_key.startswith("videos/creator-pending/"):
        return json_error("Invalid creator video key.")
    if poster_key and not poster_key.startswith("posters/creator-pending/"):
        return json_error("Invalid creator poster key.")
    try:
        video_head = r2_head(validate_r2_key(video_key))
        if int(video_head.get("ContentLength") or 0) <= 0:
            return json_error("Uploaded video is empty.")
        if int(video_head.get("ContentLength") or 0) > MAX_VIDEO_SIZE:
            return json_error("Video exceeds 4 GB.")
        if poster_key and int(r2_head(validate_r2_key(poster_key)).get("ContentLength") or 0) <= 0:
            return json_error("Poster is empty.")
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO creator_submissions
                (customer_id,title,content_type,category,description,poster,video,status)
                VALUES(%s,%s,%s,%s,%s,%s,'PENDING')
                RETURNING id
            """, (str(session.get("customer_id") or ""),title,content_type,category,description,poster_key or None,video_key))
            submission_id = cur.fetchone()[0]
            conn.commit()
            cur.close()
        finally:
            conn.close()
        return json_ok(message="Upload submitted for Admin approval.", submission_id=submission_id)
    except Exception as exc:
        print("CREATOR SUBMISSION SAVE ERROR:", repr(exc))
        return json_error("Submission could not be saved.", 500)


@app.route("/admin/creator-studio", methods=["GET", "POST"])
@admin_required
def admin_creator_studio():
    if request.method == "POST":
        try:
            movie_rate = max(0.0, min(float(request.form.get("movie_rate", "50")), 1000000.0))
            drama_rate = max(0.0, min(float(request.form.get("drama_rate", "30")), 1000000.0))
            min_payout = max(1.0, min(float(request.form.get("min_payout", "100")), 1000000.0))
            set_setting("creator_movie_rate_per_1000", str(movie_rate))
            set_setting("creator_drama_rate_per_1000", str(drama_rate))
            set_setting("creator_min_payout", str(min_payout))
            flash("Creator rates and minimum payout saved.", "success")
        except Exception:
            flash("Rates valid number mein enter karein.", "error")
        return redirect(url_for("admin_creator_studio"))
    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT cs.id,cs.customer_id,cs.title,cs.content_type,cs.category,cs.description,cs.poster,cs.video,cs.status,cs.admin_note,cs.created_at,
                   cu.email AS creator_email,cu.full_name AS creator_name,cu.mobile AS creator_mobile
            FROM creator_submissions cs
            LEFT JOIN customer_users cu ON cu.customer_id=cs.customer_id
            WHERE cs.status='PENDING' ORDER BY cs.created_at ASC
        """)
        pending = [dict(row) for row in cur.fetchall()]
        for item in pending:
            try:
                item["video_url"] = media_url(item.get("video")) if item.get("video") else None
                item["poster_url"] = media_url(item.get("poster")) if item.get("poster") else None
            except Exception:
                item["video_url"] = None
                item["poster_url"] = None
        cur.execute("""
            SELECT cs.id,cs.customer_id,cs.title,cs.content_type,cs.category,cs.description,cs.poster,cs.video,
                   cs.status,cs.admin_note,cs.created_at,cs.reviewed_at,cs.movie_id,
                   cu.email AS creator_email,cu.full_name AS creator_name,cu.mobile AS creator_mobile,
                   COALESCE(m.views,0) AS views,
                   COALESCE((SELECT COUNT(*) FROM movie_likes ml WHERE ml.movie_id=cs.movie_id),0) AS likes
            FROM creator_submissions cs
            LEFT JOIN movies m ON m.id=cs.movie_id
            LEFT JOIN customer_users cu ON cu.customer_id=cs.customer_id
            WHERE cs.status <> 'PENDING' ORDER BY cs.id DESC
        """)
        reviewed = [dict(row) for row in cur.fetchall()]
        for item in reviewed:
            try:
                item["video_url"] = media_url(item.get("video")) if item.get("video") else None
                item["poster_url"] = media_url(item.get("poster")) if item.get("poster") else None
            except Exception:
                item["video_url"] = None
                item["poster_url"] = None
        cur.execute("""
            SELECT p.id,p.customer_id,p.amount,p.payout_method,p.payout_details,p.status,p.admin_note,p.transaction_ref,p.created_at,p.reviewed_at,p.paid_at,
                   p.bank_account_holder,p.bank_name,p.bank_account_number,p.bank_ifsc,p.bank_branch,p.bank_verified,p.bank_verified_at,
                   cu.email AS creator_email,cu.full_name AS creator_name,cu.mobile AS creator_mobile
            FROM creator_payout_requests p
            LEFT JOIN customer_users cu ON cu.customer_id=p.customer_id
            ORDER BY CASE p.status WHEN 'PENDING' THEN 0 WHEN 'APPROVED' THEN 1 ELSE 2 END, p.id DESC
        """)
        payout_requests = [dict(row) for row in cur.fetchall()]
        cur.close()
    finally:
        conn.close()
    return render_template("admin_creator_studio.html", pending=pending, reviewed=reviewed, payout_requests=payout_requests,
        movie_rate=get_setting("creator_movie_rate_per_1000", "50"),
        drama_rate=get_setting("creator_drama_rate_per_1000", "30"),
        min_payout=get_setting("creator_min_payout", "100"))



@app.route("/admin/creator-payout/<int:payout_id>/<action>", methods=["POST"])
@admin_required
def admin_creator_payout_action(payout_id, action):
    if action not in {"approve", "reject", "paid", "verify-bank"}:
        abort(400)
    note = str(request.form.get("admin_note") or "").strip()[:1000]
    reference = str(request.form.get("transaction_ref") or "").strip()[:250]
    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM creator_payout_requests WHERE id=%s FOR UPDATE", (payout_id,))
        payout = cur.fetchone()
        if not payout:
            conn.rollback()
            abort(404)
        status = str(payout.get("status") or "").upper()
        method = str(payout.get("payout_method") or "").upper()
        if action == "verify-bank" and method == "BANK" and status == "PENDING":
            if not (payout.get("bank_account_number") and payout.get("bank_account_holder") and payout.get("bank_name") and payout.get("bank_ifsc")):
                conn.rollback()
                flash("Bank details are incomplete. Ask the creator to update them.", "error")
                return redirect(url_for("admin_creator_studio"))
            cur.execute("UPDATE creator_payout_requests SET bank_verified=TRUE,bank_verified_at=NOW(),admin_note=%s WHERE id=%s", (note,payout_id))
            flash("Bank details marked verified. You can now approve this payout.", "success")
        elif action == "approve" and status == "PENDING":
            if method == "BANK" and not payout.get("bank_verified"):
                conn.rollback()
                flash("Verify the bank details first, then approve the payout.", "error")
                return redirect(url_for("admin_creator_studio"))
            cur.execute("UPDATE creator_payout_requests SET status='APPROVED',admin_note=%s,reviewed_at=NOW() WHERE id=%s", (note,payout_id))
            flash("Payout request approved. Send the money manually, then mark it paid.", "success")
        elif action == "reject" and status in {"PENDING","APPROVED"}:
            cur.execute("UPDATE creator_payout_requests SET status='REJECTED',admin_note=%s,reviewed_at=NOW() WHERE id=%s", (note,payout_id))
            flash("Payout request rejected. Its balance is available again.", "success")
        elif action == "paid" and status == "APPROVED":
            if method == "BANK" and not payout.get("bank_verified"):
                conn.rollback()
                flash("Bank details must be verified before payment.", "error")
                return redirect(url_for("admin_creator_studio"))
            if not reference:
                conn.rollback()
                flash("Enter UPI/bank transaction reference before marking paid.", "error")
                return redirect(url_for("admin_creator_studio"))
            cur.execute("UPDATE creator_payout_requests SET status='PAID',transaction_ref=%s,admin_note=%s,paid_at=NOW() WHERE id=%s", (reference,note,payout_id))
            flash("Payout marked as paid and transaction reference saved.", "success")
        else:
            conn.rollback()
            flash("Invalid payout status transition.", "error")
            return redirect(url_for("admin_creator_studio"))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        print("ADMIN CREATOR PAYOUT ERROR:", repr(exc))
        flash("Could not update payout. Check server logs.", "error")
    finally:
        conn.close()
    return redirect(url_for("admin_creator_studio"))


@app.route("/admin/creator-submission/<int:submission_id>/<action>", methods=["POST"])
@admin_required
def admin_review_creator_submission(submission_id, action):
    if action not in {"approve", "reject"}:
        abort(400)
    note = str(request.form.get("admin_note") or "").strip()[:1000]
    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM creator_submissions WHERE id=%s FOR UPDATE", (submission_id,))
        sub = cur.fetchone()
        if not sub:
            conn.rollback()
            abort(404)
        if str(sub.get("status") or "").upper() != "PENDING":
            conn.rollback()
            flash("This submission was already reviewed.", "error")
            return redirect(url_for("admin_creator_studio"))
        if action == "approve":
            cur.execute("""
                INSERT INTO movies(title,category,description,poster,video,content_type,creator_customer_id)
                VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (sub["title"],sub.get("category"),sub.get("description"),sub.get("poster"),sub["video"],sub["content_type"],sub["customer_id"]))
            movie_id = cur.fetchone()["id"]
            cur.execute("""
                UPDATE creator_submissions SET status='APPROVED',admin_note=%s,movie_id=%s,reviewed_at=NOW()
                WHERE id=%s
            """, (note,movie_id,submission_id))
            flash("Approved. Content is now listed in its selected section.", "success")
        else:
            cur.execute("""
                UPDATE creator_submissions SET status='REJECTED',admin_note=%s,reviewed_at=NOW()
                WHERE id=%s
            """, (note,submission_id))
            flash("Creator submission rejected.", "success")
        conn.commit()
    except Exception as exc:
        conn.rollback()
        print("CREATOR REVIEW ERROR:", repr(exc))
        flash("Could not review submission. Check server logs.", "error")
    finally:
        conn.close()
    return redirect(url_for("admin_creator_studio"))


# ============================================================
# DRAMAS CATALOGUE (separate from Movies)
# ============================================================

@app.route("/dramas")
def dramas_page():
    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM movies
            WHERE UPPER(COALESCE(content_type,'MOVIE'))='DRAMA'
            ORDER BY id DESC
        """)
        dramas = [dict(row) for row in cur.fetchall()]
        cur.close()
    finally:
        conn.close()
    for drama in dramas:
        try:
            drama["poster_url"] = media_url(drama.get("poster")) if drama.get("poster") else None
        except Exception:
            drama["poster_url"] = None
    return render_template("dramas.html", dramas=dramas)


# ============================================================
# MOVIE LIKE
# ============================================================

@app.route("/movie/<int:movie_id>/like", methods=["POST"])
def like_movie(movie_id):
    customer_id = session.get("customer_id")
    if not customer_id or not session.get("customer_logged_in"):
        flash("Please login to like this movie.", "error")
        return redirect(url_for("movie_page", movie_id=movie_id))
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM movies WHERE id=%s", (movie_id,))
        if not cur.fetchone(): abort(404)
        cur.execute("SELECT 1 FROM movie_likes WHERE movie_id=%s AND customer_id=%s", (movie_id, str(customer_id)))
        if cur.fetchone():
            cur.execute("DELETE FROM movie_likes WHERE movie_id=%s AND customer_id=%s", (movie_id, str(customer_id)))
        else:
            cur.execute("INSERT INTO movie_likes(movie_id,customer_id) VALUES(%s,%s) ON CONFLICT(movie_id,customer_id) DO NOTHING", (movie_id, str(customer_id)))
        conn.commit(); cur.close()
    finally:
        conn.close()
    return redirect(url_for("movie_page", movie_id=movie_id))


# ============================================================
# MOVIE RATINGS
# ============================================================


@app.route("/movie/<int:movie_id>/rating", methods=["POST"])
def rate_movie(movie_id):
    customer_id = session.get("customer_id")
    if not customer_id or not session.get("customer_logged_in"):
        flash("Please login to rate this movie.", "error")
        return redirect(url_for("movie_page", movie_id=movie_id))

    try:
        rating = int(request.form.get("rating", "0"))
    except (TypeError, ValueError):
        rating = 0
    if rating < 1 or rating > 5:
        flash("Please select a rating from 1 to 5 stars.", "error")
        return redirect(url_for("movie_page", movie_id=movie_id))

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM movies WHERE id = %s", (movie_id,))
        if not cur.fetchone():
            cur.close()
            abort(404)
        cur.execute("""
            INSERT INTO movie_ratings (movie_id, customer_id, rating)
            VALUES (%s, %s, %s)
            ON CONFLICT (movie_id, customer_id)
            DO UPDATE SET rating = EXCLUDED.rating, updated_at = CURRENT_TIMESTAMP
        """, (movie_id, str(customer_id), rating))
        conn.commit()
        cur.close()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    flash("Your rating has been saved.", "success")
    return redirect(url_for("movie_page", movie_id=movie_id))


# ============================================================
# MOVIE COMMENTS
# ============================================================

@app.route("/movie/<int:movie_id>/comment", methods=["POST"])
def add_movie_comment(movie_id):
    customer_id = session.get("customer_id")
    comment = str(request.form.get("comment", "")).strip()

    if not customer_id or not session.get("customer_logged_in"):
        flash("Please login to comment.", "error")
        return redirect(url_for("movie_page", movie_id=movie_id))

    if len(comment) < 2:
        flash("Comment thoda aur likho.", "error")
        return redirect(url_for("movie_page", movie_id=movie_id))

    if len(comment) > 500:
        flash("Comment maximum 500 characters ka ho sakta hai.", "error")
        return redirect(url_for("movie_page", movie_id=movie_id))

    csrf_token = str(request.form.get("comment_csrf", "")).strip()
    expected_token = hmac.new(
        str(app.secret_key).encode("utf-8"),
        (str(customer_id) + "|" + str(movie_id)).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not csrf_token or not hmac.compare_digest(csrf_token, expected_token):
        flash("Comment request invalid. Please try again.", "error")
        return redirect(url_for("movie_page", movie_id=movie_id))

    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()

        cur.execute("SELECT id FROM movies WHERE id = %s", (movie_id,))
        if not cur.fetchone():
            cur.close()
            abort(404)

        cur.execute(
            """
            SELECT created_at
            FROM movie_comments
            WHERE customer_id = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (customer_id,),
        )
        last_comment = cur.fetchone()

        if last_comment and last_comment.get("created_at"):
            elapsed = (datetime.now() - last_comment["created_at"]).total_seconds()
            if elapsed < 30:
                cur.close()
                flash("Please wait 30 seconds before posting another comment.", "error")
                return redirect(url_for("movie_page", movie_id=movie_id))

        cur.execute(
            """
            SELECT full_name, email, mobile
            FROM customer_users
            WHERE customer_id = %s
            LIMIT 1
            """,
            (customer_id,),
        )
        user = cur.fetchone() or {}

        display_name = str(
            user.get("full_name")
            or user.get("email")
            or user.get("mobile")
            or "CINEMA WORLD User"
        ).strip()[:100]

        cur.execute(
            """
            INSERT INTO movie_comments
            (movie_id, customer_id, display_name, comment)
            VALUES(%s, %s, %s, %s)
            """,
            (movie_id, customer_id, display_name, comment),
        )

        conn.commit()
        cur.close()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    flash("Comment posted successfully.", "success")
    return redirect(url_for("movie_page", movie_id=movie_id))


@app.route("/admin/comments/delete/<int:comment_id>", methods=["POST"])
@admin_required
def admin_delete_comment(comment_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM movie_comments WHERE id = %s",
            (comment_id,),
        )
        deleted = cur.rowcount > 0
        conn.commit()
        cur.close()
    finally:
        conn.close()

    flash(
        "Comment deleted." if deleted else "Comment not found.",
        "success" if deleted else "error",
    )
    return redirect(url_for("admin", section="comments"))


# ============================================================
# STREAM MOVIE
# ============================================================

@app.route(
    "/stream/<int:movie_id>",
    methods=["GET"],
)
def stream_movie(movie_id):

    access_token = request.args.get(
        "access_token",
        "",
    ).strip()

    # --------------------------------------------------------
    # Verify paid watch access.
    # --------------------------------------------------------
    if access_token:

        if not verify_stream_token(
            access_token,
            movie_id,
        ):
            return Response(
                "Invalid or expired stream access.",
                status=403,
            )

    else:

        # Owner/Admin can preview any movie without customer payment.
        # Normal customers continue to use the existing paid-access rules.
        if not session.get("admin_logged_in"):
            access = access_for_movie(
                movie_id
            )

            if not access["watch"]:
                return Response(
                    "Payment required.",
                    status=403,
                )

    # --------------------------------------------------------
    # Get movie/video key.
    # --------------------------------------------------------
    conn = get_db(
        dict_rows=True
    )

    try:

        cur = conn.cursor()

        cur.execute(
            """
            SELECT id, title, video
            FROM movies
            WHERE id = %s
            """,
            (movie_id,),
        )

        movie = cur.fetchone()
        cur.close()

    finally:
        conn.close()

    if not movie:
        return Response(
            "Movie not found.",
            status=404,
        )

    video_key = movie.get(
        "video"
    )

    if not video_key:
        return Response(
            "Video not found.",
            status=404,
        )

    try:
        video_key = validate_r2_key(
            video_key
        )
    except Exception:
        return Response(
            "Invalid video object.",
            status=400,
        )

    # --------------------------------------------------------
    # Verify the object exists before handing the browser a
    # direct R2 URL. R2 handles HTTP Range/206 natively, which
    # is more reliable for browser MP4 playback than proxying
    # every media range through the Render Flask worker.
    # --------------------------------------------------------
    try:

        head = r2_head(video_key)

    except Exception as exc:

        print(
            "R2 HEAD ERROR:",
            repr(exc),
        )

        return Response(
            "Video object not found in R2.",
            status=404,
        )

    total_size = int(
        head.get(
            "ContentLength",
            0,
        )
    )

    if total_size <= 0:
        return Response(
            "Video file is empty.",
            status=404,
        )

    content_type = (
        content_type_for_key(video_key)
        or head.get("ContentType")
        or "video/mp4"
    )

    # --------------------------------------------------------
    # Direct presigned R2 URL. Browser talks to R2 directly and
    # gets native Range support, correct Content-Type and inline
    # playback behavior.
    # --------------------------------------------------------
    try:

        client = get_r2_client()

        url = client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": R2_BUCKET,
                "Key": video_key,
                "ResponseContentType": content_type,
                "ResponseContentDisposition": "inline",
                "ResponseCacheControl": "private, max-age=300",
            },
            ExpiresIn=min(
                PRESIGNED_EXPIRES,
                3600,
            ),
        )

        return redirect(
            url,
            code=302,
        )

    except Exception as exc:

        print(
            "R2 STREAM URL ERROR:",
            repr(exc),
        )

        return Response(
            "Unable to prepare video stream.",
            status=502,
        )


# ============================================================
# DOWNLOAD
# ============================================================

@app.route(
    "/download/<int:movie_id>"
)
def download_movie(movie_id):

    access = access_for_movie(
        movie_id
    )

    if not access["download"]:

        return Response(
            "Download payment required.",
            status=403,
        )

    conn = get_db(
        dict_rows=True
    )

    try:

        cur = conn.cursor()

        cur.execute(
            """
            SELECT id, title, video
            FROM movies
            WHERE id = %s
            """,
            (movie_id,),
        )

        movie = cur.fetchone()
        cur.close()

    finally:
        conn.close()

    if not movie:
        return Response(
            "Movie not found.",
            status=404,
        )

    video_key = movie.get(
        "video"
    )

    if not video_key:
        return Response(
            "Video not found.",
            status=404,
        )

    try:

        url = r2_presigned_url(
            video_key,
            expires=600,
        )

        return redirect(url)

    except Exception as exc:

        print(
            "DOWNLOAD ERROR:",
            repr(exc),
        )

        return Response(
            "Download unavailable.",
            status=500,
        )


# ============================================================
# LOGIN
# ============================================================

@app.route(
    "/login",
    methods=["GET"],
)
def login():
    requested_step = str(request.args.get("step", "")).strip().lower()

    # Always allow the explicit Sign In / Get Started route to show the login
    # screen, even if a stale customer session flag is present. The previous
    # redirect sent /login straight back to Home and trapped users there.
    step = requested_step

    # Direct /login always starts at the identifier screen. Never infer the
    # screen from stale OTP session keys.
    if step not in {"mobile", "otp", "password", "set_password"}:
        step = "mobile"

    identifier = str(
        request.args.get("identifier", "")
        or session.get("login_identifier", "")
    ).strip()

    mobile = normalize_mobile(
        request.args.get("mobile", "")
        or session.get("otp_mobile", "")
        or (identifier if step in {"password", "otp"} else "")
    )

    email = normalize_email(
        request.args.get("email", "")
        or session.get("otp_email", "")
        or (identifier if step in {"password", "otp"} else "")
    )

    return render_template(
        "login.html",
        step=step,
        mobile=mobile,
        masked_mobile=mask_mobile(mobile),
        email=email,
        identifier=identifier or email or mobile,
    )


@app.route("/set-language", methods=["POST"])
def set_language():
    language = str(request.form.get("language", "en")).strip().lower()
    if language not in {"en", "hi"}:
        language = "en"
    session["language"] = language
    target = request.referrer or url_for("home")
    if not target.startswith(request.host_url):
        target = url_for("home")
    return redirect(target)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        client_key = (request.remote_addr or "unknown") + "|" + username.lower()
        now_ts = time.time()

        with _ADMIN_LOGIN_GUARD_LOCK:
            state = _ADMIN_LOGIN_GUARD.get(client_key, [])
            state = [ts for ts in state if now_ts - ts < _ADMIN_LOGIN_WINDOW]
            if len(state) >= _ADMIN_LOGIN_MAX_FAILURES:
                _ADMIN_LOGIN_GUARD[client_key] = state
                flash("Too many failed login attempts. Please try again later.", "error")
                return render_template("admin_login.html")

        if username == ADMIN_USER and password == ADMIN_PASSWORD:
            with _ADMIN_LOGIN_GUARD_LOCK:
                _ADMIN_LOGIN_GUARD.pop(client_key, None)
            session.clear()
            session["admin_logged_in"] = True
            return redirect(url_for("admin"))

        with _ADMIN_LOGIN_GUARD_LOCK:
            state = _ADMIN_LOGIN_GUARD.get(client_key, [])
            state = [ts for ts in state if now_ts - ts < _ADMIN_LOGIN_WINDOW]
            state.append(now_ts)
            _ADMIN_LOGIN_GUARD[client_key] = state

        flash("Invalid username or password.", "error")

    return render_template("admin_login.html")


# LOGOUT
# ============================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("login")
    )


def customer_login_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not session.get("customer_logged_in"):
            return redirect(url_for("login"))

        customer_id = session.get("customer_id")
        if customer_is_blocked(customer_id):
            session.clear()
            flash("Your account has been restricted by the administrator.", "error")
            return redirect(url_for("login"))

        return view_func(*args, **kwargs)
    return wrapper


@app.route("/customer/set-password", methods=["POST"])
@customer_login_required
def customer_set_password():
    customer_id=session.get("customer_id")
    password=str(request.form.get("password", "")); confirm=str(request.form.get("confirm_password", ""))
    if len(password)<6: flash("Password minimum 6 characters ka hona chahiye.","error"); return redirect(url_for("user_details"))
    if password != confirm: flash("Passwords match nahi karte.","error"); return redirect(url_for("user_details"))
    conn=get_db()
    try:
        cur=conn.cursor(); cur.execute("UPDATE customer_users SET password_hash=%s WHERE customer_id=%s",(generate_password_hash(password),customer_id)); conn.commit()
    finally: conn.close()
    flash("Password saved successfully.","success")
    return redirect(url_for("user_details"))


# Avoid writing duplicate page-view rows for repeated refreshes of the same
# page by the same signed-in customer within a short window. POST actions are
# always logged. This reduces synchronous database writes on normal browsing.
_CUSTOMER_ACTIVITY_RECENT = {}
_CUSTOMER_ACTIVITY_RECENT_LOCK = threading.Lock()
_CUSTOMER_ACTIVITY_DEDUPE_SECONDS = 30.0


def log_customer_activity():
    if not session.get("customer_logged_in"):
        return

    path = str(request.path or "")
    if (
        path.startswith("/static/")
        or path.startswith("/api/r2/")
        or path.startswith("/admin")
    ):
        return

    customer_id = session.get("customer_id")
    if not customer_id:
        return

    # Keep every POST/action log, but coalesce repeated GET page views for
    # the same customer and path for 30 seconds.
    if request.method == "GET":
        now = time.monotonic()
        key = (str(customer_id), path)
        with _CUSTOMER_ACTIVITY_RECENT_LOCK:
            last_logged = _CUSTOMER_ACTIVITY_RECENT.get(key, 0.0)
            if now - last_logged < _CUSTOMER_ACTIVITY_DEDUPE_SECONDS:
                return
            _CUSTOMER_ACTIVITY_RECENT[key] = now
            if len(_CUSTOMER_ACTIVITY_RECENT) > 5000:
                cutoff = now - _CUSTOMER_ACTIVITY_DEDUPE_SECONDS
                for old_key, timestamp in list(_CUSTOMER_ACTIVITY_RECENT.items()):
                    if timestamp < cutoff:
                        _CUSTOMER_ACTIVITY_RECENT.pop(old_key, None)

    try:
        user_agent = (request.user_agent.string or "")[:500]
        action = (
            "POST " + path
            if request.method == "POST"
            else "OPEN " + path
        )

        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO customer_activity
                (customer_id, action, path, method, user_agent)
                VALUES(%s,%s,%s,%s,%s)
                """,
                (
                    customer_id,
                    action[:300],
                    path[:500],
                    request.method[:20],
                    user_agent,
                ),
            )
            conn.commit()
            cur.close()
        finally:
            conn.close()
    except Exception as exc:
        print("CUSTOMER ACTIVITY LOG ERROR:", repr(exc))


@app.before_request
def capture_referral_link():
    code = normalize_referral_code(request.args.get("ref"))
    if code and not session.get("customer_logged_in"):
        session["referral_code"] = code


@app.before_request
def track_customer_activity():
    log_customer_activity()


# ============================================================
# CUSTOMER MEMBER / USER DETAILS / MY LIST
# ============================================================

@app.route("/user-details", methods=["GET", "POST"])
def user_details():
    if not session.get("customer_id"):
        session["customer_id"]="tm_"+secrets.token_hex(16); session["customer_logged_in"]=True
    customer_id=session["customer_id"]
    if request.method=="POST":
        full_name=(request.form.get("full_name") or "").strip(); mobile=normalize_mobile(request.form.get("mobile") or session.get("customer_mobile", "")); email=normalize_email(request.form.get("email") or session.get("customer_email", "")); password=str(request.form.get("password", "")); confirm=str(request.form.get("confirm_password", ""))
        if not full_name: flash("Please enter your full name.","error"); return redirect(url_for("user_details"))
        if not valid_mobile(mobile): flash("Please enter a valid 10-digit mobile number.","error"); return redirect(url_for("user_details"))
        if not valid_email(email): flash("Please enter a valid email address.","error"); return redirect(url_for("user_details"))
        if len(password)<6: flash("Password minimum 6 characters ka hona chahiye.","error"); return redirect(url_for("user_details"))
        if password!=confirm: flash("Passwords match nahi karte.","error"); return redirect(url_for("user_details"))
        created_new_account = False
        conn=get_db(dict_rows=True)
        try:
            cur=conn.cursor(); cur.execute("SELECT id,customer_id FROM customer_users WHERE email=%s AND customer_id<>%s LIMIT 1",(email,customer_id)); existing_email=cur.fetchone()
            if existing_email: flash("This email is already registered. Please login with that email.","error"); return redirect(url_for("user_details"))
            cur.execute("SELECT COUNT(*) AS n FROM customer_users WHERE mobile=%s AND customer_id<>%s",(mobile,customer_id)); count=int((cur.fetchone() or {}).get("n") or 0)
            if count>=5: flash("This mobile number has already reached the maximum limit of 5 accounts.","error"); return redirect(url_for("user_details"))
            cur.execute("UPDATE customer_users SET email=%s,full_name=%s,mobile=%s,password_hash=%s,last_login_at=NOW() WHERE customer_id=%s",(email,full_name,mobile,generate_password_hash(password),customer_id))
            if cur.rowcount==0:
                cur.execute("INSERT INTO customer_users(email,customer_id,full_name,mobile,password_hash,last_login_at,referral_code) VALUES(%s,%s,%s,%s,%s,NOW(),%s)",(email,customer_id,full_name,mobile,generate_password_hash(password),"CW"+secrets.token_hex(5).upper()))
                created_new_account = True
            conn.commit()
        finally: conn.close()
        session["customer_email"]=email; session["customer_mobile"]=mobile; session["customer_logged_in"]=True
        if created_new_account:
            apply_referral_attribution(customer_id)
        if customer_has_completed_initial_payment(customer_id): return redirect(url_for("member_home"))
        return redirect(url_for("membership_checkout"))
    conn=get_db(dict_rows=True)
    try:
        cur=conn.cursor(); cur.execute("SELECT full_name,mobile,email,password_hash FROM customer_users WHERE customer_id=%s LIMIT 1",(customer_id,)); user=cur.fetchone() or {}
    finally: conn.close()
    return render_template("user_details.html",full_name=user.get("full_name", ""),mobile=user.get("mobile", ""),customer_email=user.get("email") or session.get("customer_email", ""),has_password=bool(user.get("password_hash")))


def get_customer_profile(customer_id=None):
    customer_id = customer_id or session.get("customer_id")
    if not customer_id:
        return {
            "full_name": "",
            "email": "",
            "mobile": "",
            "profile_photo": "",
            "created_at": None,
            "last_login_at": None,
        }

    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT full_name, email, mobile, profile_photo, created_at, last_login_at
            FROM customer_users
            WHERE customer_id = %s
            LIMIT 1
            """,
            (customer_id,),
        )
        row = cur.fetchone() or {}
        cur.close()
    finally:
        conn.close()

    return row


@app.route("/profile", methods=["GET", "POST"])
@customer_login_required
def customer_profile():
    customer_id = session.get("customer_id")

    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        profile_photo = (request.form.get("profile_photo") or "").strip()

        if len(full_name) > 100:
            flash("Name bahut lamba hai.", "error")
            return redirect(url_for("customer_profile"))

        if profile_photo and (
            not profile_photo.startswith("data:image/")
            or len(profile_photo) > 900000
        ):
            flash("Profile photo valid nahi hai ya bahut badi hai.", "error")
            return redirect(url_for("customer_profile"))

        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE customer_users
                SET full_name = %s, profile_photo = %s
                WHERE customer_id = %s
                """,
                (full_name, profile_photo, customer_id),
            )
            conn.commit()
            cur.close()
        finally:
            conn.close()

        session["customer_name"] = full_name
        flash("Profile updated successfully.", "success")
        return redirect(url_for("customer_profile"))

    profile = get_customer_profile(customer_id)

    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()

        cur.execute(
            "SELECT COUNT(*) AS n FROM customer_movie_list WHERE customer_id = %s",
            (customer_id,),
        )
        my_list_count = int((cur.fetchone() or {}).get("n") or 0)

        cur.execute(
            "SELECT COUNT(*) AS n FROM payment_orders WHERE customer_id = %s AND status = 'PAID'",
            (customer_id,),
        )
        paid_count = int((cur.fetchone() or {}).get("n") or 0)

        cur.execute(
            """
            SELECT payment_type, amount, status, created_at, paid_at, movie_id
            FROM payment_orders
            WHERE customer_id = %s
            ORDER BY id DESC
            LIMIT 10
            """,
            (customer_id,),
        )
        payments = cur.fetchall()

        cur.execute(
            """
            SELECT MAX(premium_until) AS premium_until
            FROM customer_access
            WHERE customer_id = %s
            """,
            (customer_id,),
        )
        premium_row = cur.fetchone() or {}

        cur.close()
    finally:
        conn.close()

    premium_until = premium_row.get("premium_until")
    premium_active = bool(
        premium_until and premium_until > datetime.now()
    )

    return render_template(
        "profile.html",
        profile=profile,
        my_list_count=my_list_count,
        paid_count=paid_count,
        payments=payments,
        premium_until=premium_until,
        premium_active=premium_active,
    )


# Short per-customer cache for the member catalogue. The list is personalized
# by saved movies and ratings, so each customer has a separate cache entry.
_MEMBER_MOVIES_CACHE = {}
_MEMBER_MOVIES_CACHE_LOCK = threading.Lock()
_MEMBER_MOVIES_CACHE_TTL = 10.0


def get_member_movies_data():
    customer_id = session.get("customer_id")
    cache_key = str(customer_id or "")
    now = time.monotonic()
    with _MEMBER_MOVIES_CACHE_LOCK:
        cached = _MEMBER_MOVIES_CACHE.get(cache_key)
        if cached and now - cached[0] < _MEMBER_MOVIES_CACHE_TTL:
            return [dict(movie) for movie in cached[1]]

    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT m.*, CASE WHEN cml.movie_id IS NOT NULL THEN TRUE ELSE FALSE END AS in_my_list,
                   COALESCE(ROUND(AVG(mr.rating)::numeric, 1), 0) AS average_rating,
                   COUNT(mr.id) AS rating_count
            FROM movies m
            LEFT JOIN customer_movie_list cml
              ON cml.movie_id = m.id AND cml.customer_id = %s
            LEFT JOIN movie_ratings mr ON mr.movie_id = m.id
            GROUP BY m.id, cml.movie_id
            ORDER BY m.id DESC
            """,
            (customer_id,),
        )
        movies = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    # Normalize every row before caching the customer-specific result.
    normalized = []
    for movie in movies:
        movie = dict(movie)
        try:
            movie["poster_url"] = media_url(movie.get("poster")) if movie.get("poster") else None
        except Exception:
            movie["poster_url"] = None
        movie["in_my_list"] = bool(movie.get("in_my_list"))
        movie["views"] = int(movie.get("views") or 0)
        movie["average_rating"] = float(movie.get("average_rating") or 0)
        movie["rating_count"] = int(movie.get("rating_count") or 0)
        normalized.append(movie)

    with _MEMBER_MOVIES_CACHE_LOCK:
        _MEMBER_MOVIES_CACHE[cache_key] = (time.monotonic(), [dict(movie) for movie in normalized])
    return normalized

def customer_has_completed_initial_payment(customer_id=None):
    customer_id=customer_id or session.get("customer_id")
    if not customer_id: return False
    conn=get_db(dict_rows=True)
    try:
        cur=conn.cursor(); cur.execute("SELECT 1 FROM payment_orders WHERE customer_id=%s AND payment_type='activation' AND status='PAID' LIMIT 1",(customer_id,));
        if cur.fetchone(): return True
        cur.execute("SELECT 1 FROM customer_subscriptions WHERE customer_id=%s AND auth_status='SUCCESS' AND status IN ('ACTIVE','BANK_APPROVAL_PENDING') LIMIT 1",(customer_id,)); return bool(cur.fetchone())
    finally: conn.close()


def subscription_customer_name(customer_id):
    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT full_name, email, mobile FROM customer_users WHERE customer_id = %s LIMIT 1",
            (customer_id,),
        )
        row = cur.fetchone() or {}
        return (
            row.get("full_name") or "CINEMA WORLD Member",
            row.get("email") or session.get("customer_email") or "",
            row.get("mobile") or session.get("customer_mobile") or "",
        )
    finally:
        conn.close()


def create_cashfree_subscription(customer_id):
    name, email, phone = subscription_customer_name(customer_id)
    if not re.fullmatch(r"[6-9]\d{9}", re.sub(r"\D", "", phone or "")):
        raise RuntimeError("A valid mobile number is required before payment.")
    phone = re.sub(r"\D", "", phone)
    if not valid_email(email):
        email = "member@cinemaworld.com"

    subscription_id = "tm_sub_" + secrets.token_hex(12)
    now = datetime.now(timezone.utc)
    first_charge = now + timedelta(days=30)
    expiry = now + timedelta(days=30 * SUBSCRIPTION_MAX_CYCLES)
    return_url = url_for("cashfree_subscription_return", _external=True)

    payload = {
        "subscription_id": subscription_id,
        "customer_details": {
            "customer_name": name[:100],
            "customer_email": email,
            "customer_phone": phone,
        },
        "plan_details": {
            "plan_name": SUBSCRIPTION_PLAN_NAME,
            "plan_type": "PERIODIC",
            "plan_amount": PREMIUM_PRICE,
            "plan_max_amount": PREMIUM_PRICE,
            "plan_max_cycles": SUBSCRIPTION_MAX_CYCLES,
            "plan_intervals": 1,
            "plan_currency": "INR",
            "plan_interval_type": "MONTH",
            "plan_note": "CINEMA WORLD Premium Monthly",
        },
        "authorization_details": {
            "authorization_amount": SUBSCRIPTION_AUTH_AMOUNT,
            "authorization_amount_refund": True,
            "payment_methods": ["upi", "card", "enach"],
        },
        "subscription_meta": {
            "return_url": return_url,
            "notification_channel": ["EMAIL", "SMS"],
        },
        "subscription_first_charge_time": first_charge.isoformat().replace("+00:00", "Z"),
        "subscription_expiry_time": expiry.isoformat().replace("+00:00", "Z"),
        "subscription_tags": {
            "customer_id": customer_id,
            "product": "cinema_world_premium",
        },
    }

    result = cashfree_request("POST", "/subscriptions", payload)
    subscription_session_id = result.get("subscription_session_id")
    if not subscription_session_id:
        raise RuntimeError("Cashfree did not return subscription session.")

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO customer_subscriptions
            (customer_id, subscription_id, cf_subscription_id, subscription_session_id, status, auth_status)
            VALUES(%s,%s,%s,%s,%s,%s)
            ON CONFLICT(subscription_id) DO UPDATE SET
                subscription_session_id = EXCLUDED.subscription_session_id,
                cf_subscription_id = EXCLUDED.cf_subscription_id,
                updated_at = NOW()
            """,
            (
                customer_id,
                subscription_id,
                str(result.get("cf_subscription_id") or ""),
                subscription_session_id,
                str(result.get("subscription_status") or "INITIALIZED"),
                "PENDING",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return subscription_id, subscription_session_id


def activate_customer_subscription(customer_id, subscription_id, status="ACTIVE"):
    now = datetime.now()
    premium_until = now + timedelta(days=PREMIUM_DAYS)
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE customer_subscriptions
            SET status=%s, auth_status='SUCCESS', premium_until=%s, updated_at=NOW()
            WHERE subscription_id=%s AND customer_id=%s
            """,
            (status, premium_until, subscription_id, customer_id),
        )
        cur.execute(
            """
            INSERT INTO customer_access
            (customer_id, movie_id, watch_until, download_until, premium_until)
            VALUES(%s, NULL, NULL, NULL, %s)
            ON CONFLICT DO NOTHING
            """,
            (customer_id, premium_until),
        )
        # Keep permanent watch access separately so the one-time authorization
        # remains sufficient for account watch access even after a subscription ends.
        cur.execute(
            """
            SELECT id FROM customer_access
            WHERE customer_id=%s AND movie_id IS NULL
              AND watch_until IS NULL AND download_until IS NULL AND premium_until IS NULL
            LIMIT 1
            """,
            (customer_id,),
        )
        if not cur.fetchone():
            cur.execute(
                """
                INSERT INTO customer_access(customer_id, movie_id, watch_until, download_until, premium_until)
                VALUES(%s,NULL,NULL,NULL,NULL)
                """,
                (customer_id,),
            )
        conn.commit()
    finally:
        conn.close()


def extend_subscription_premium(customer_id, subscription_id):
    now = datetime.now()
    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT premium_until FROM customer_subscriptions WHERE subscription_id=%s AND customer_id=%s LIMIT 1",
            (subscription_id, customer_id),
        )
        row = cur.fetchone() or {}
        current = row.get("premium_until")
        base = current if current and current > now else now
        until = base + timedelta(days=PREMIUM_DAYS)
        cur.execute(
            "UPDATE customer_subscriptions SET status='ACTIVE', premium_until=%s, updated_at=NOW() WHERE subscription_id=%s AND customer_id=%s",
            (until, subscription_id, customer_id),
        )
        cur.execute(
            """
            UPDATE customer_access
            SET premium_until=%s
            WHERE customer_id=%s AND movie_id IS NULL AND premium_until IS NOT NULL
            """,
            (until, customer_id),
        )
        if cur.rowcount == 0:
            cur.execute(
                """
                INSERT INTO customer_access(customer_id,movie_id,watch_until,download_until,premium_until)
                VALUES(%s,NULL,NULL,NULL,%s)
                """,
                (customer_id, until),
            )
        conn.commit()
    finally:
        conn.close()


@app.route("/membership")
@customer_login_required
def membership_checkout():
    if customer_has_completed_initial_payment(session.get("customer_id")):
        return redirect(url_for("member_home"))
    return redirect(url_for("membership_start"))


@app.route("/api/subscription/create", methods=["POST"])
@customer_login_required
def create_subscription_route():
    customer_id = session.get("customer_id")
    if customer_has_completed_initial_payment(customer_id):
        return json_ok(already_active=True, redirect_url=url_for("member_home"))
    try:
        subscription_id, session_id = create_cashfree_subscription(customer_id)
        return json_ok(subscription_id=subscription_id, subscription_session_id=session_id, mode=CASHFREE_JS_MODE)
    except Exception as exc:
        print("CREATE SUBSCRIPTION ERROR:", repr(exc))
        return json_error(str(exc), 500)


@app.route("/membership/start")
@customer_login_required
def membership_start():
    customer_id=session.get("customer_id")
    if customer_has_completed_initial_payment(customer_id): return redirect(url_for("member_home"))
    return Response("""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>CINEMA WORLD</title><script src='https://sdk.cashfree.com/js/v3/cashfree.js'></script><style>body{margin:0;background:#050507;color:#fff;font-family:Arial;display:grid;place-items:center;min-height:100vh}.card{width:min(560px,92vw);padding:36px;border:1px solid #292933;border-radius:24px;background:linear-gradient(145deg,#17171f,#09090d);box-shadow:0 30px 90px #000}.brand{color:#e50914;font-weight:900;letter-spacing:2px}.price{font-size:52px;font-weight:900;margin:22px 0 4px}.muted{color:#aaa;line-height:1.6}.btn{width:100%;padding:16px;border:0;border-radius:12px;background:#e50914;color:#fff;font-size:17px;font-weight:800;cursor:pointer;margin-top:24px}.status{margin-top:15px;color:#aaa}</style></head><body><main class='card'><div class='brand'>CINEMA WORLD</div><h1>Activate Your Account</h1><p class='muted'>Ek baar ka activation payment complete karein. Payment successful hone ke baad <b>24 hours FREE</b> me kisi bhi movie ko watch kar sakte hain.</p><div class='price'>₹1</div><div class='muted'>One-time activation • 24-hour all-movie watch access</div><button id='pay' class='btn'>Pay ₹1 & Continue</button><div id='status' class='status'></div></main><script>const cashfree=Cashfree({mode:"""+json.dumps(CASHFREE_JS_MODE)+"""});document.getElementById('pay').onclick=async()=>{const s=document.getElementById('status');s.textContent='Secure checkout opening…';try{const r=await fetch('/api/payment/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({payment_type:'activation',phone:"""+json.dumps(session.get("customer_mobile", ""))+"""})});const d=await r.json();if(!r.ok||!d.ok)throw new Error(d.error||'Payment order failed');await cashfree.checkout({paymentSessionId:d.payment_session_id,redirectTarget:'_self'});}catch(e){s.textContent=e.message||'Payment could not be opened.'}};</script></body></html>""")


@app.route("/membership/pay/<subscription_id>")
@customer_login_required
def membership_page_with_session(subscription_id):
    customer_id=session.get("customer_id")
    conn=get_db(dict_rows=True)
    try:
        cur=conn.cursor(); cur.execute("SELECT subscription_session_id FROM customer_subscriptions WHERE subscription_id=%s AND customer_id=%s",(subscription_id,customer_id)); row=cur.fetchone()
    finally:
        conn.close()
    if not row or not row.get("subscription_session_id"):
        flash("Payment session expired. Please try again.","error"); return redirect(url_for("membership_checkout"))
    session_id=row["subscription_session_id"]
    page=membership_checkout.__wrapped__() if hasattr(membership_checkout,"__wrapped__") else ""
    page=page.replace("sessionId = null", "sessionId = "+json.dumps(session_id))
    return page


@app.route("/subscription/return", methods=["GET","POST"])
def cashfree_subscription_return():
    subscription_id = (request.values.get("cf_subscriptionId") or request.values.get("subscription_id") or "").strip()
    if not subscription_id:
        flash("Subscription response was not received.","error")
        return redirect(url_for("membership_checkout"))
    conn=get_db(dict_rows=True)
    try:
        cur=conn.cursor(); cur.execute("SELECT customer_id FROM customer_subscriptions WHERE subscription_id=%s LIMIT 1",(subscription_id,)); row=cur.fetchone()
    finally: conn.close()
    if not row:
        flash("Subscription record not found.","error"); return redirect(url_for("login"))
    customer_id=row["customer_id"]
    try:
        result=cashfree_request("GET", "/subscriptions/"+quote(subscription_id,safe=""))
        status=str(result.get("subscription_status") or "").upper()
        auth=(result.get("authorization_details") or result.get("authorisation_details") or {})
        auth_status=str(auth.get("authorization_status") or "").upper()
        if status in {"ACTIVE", "BANK_APPROVAL_PENDING"} and auth_status in {"ACTIVE", "SUCCESS"}:
            activate_customer_subscription(customer_id, subscription_id, "ACTIVE")
            session["customer_id"]=customer_id; session["customer_logged_in"]=True
            flash("Membership activated successfully.","success")
            return redirect(url_for("member_home"))
        flash("Authorization is still pending. Please complete the Cashfree checkout.","error")
    except Exception as exc:
        print("SUBSCRIPTION RETURN VERIFY ERROR:",repr(exc)); flash("Payment verification failed.","error")
    session["customer_id"]=customer_id; session["customer_logged_in"]=True
    return redirect(url_for("membership_pay_status", subscription_id=subscription_id))


@app.route("/membership/status/<subscription_id>")
@customer_login_required
def membership_pay_status(subscription_id):
    return """<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>CINEMA WORLD</title><style>body{background:#08080d;color:#fff;font-family:Arial;display:grid;place-items:center;min-height:100vh}.box{padding:32px;text-align:center}</style></head><body><div class='box'><h1>Payment verification pending</h1><p>Cashfree is still processing the authorization.</p><a href='/membership/start'>Try again</a></div></body></html>"""


@app.route("/webhooks/cashfree/subscription", methods=["POST"])
def cashfree_subscription_webhook():
    if not CASHFREE_WEBHOOK_SECRET:
        return Response("Webhook secret not configured.", status=503)
    raw=request.get_data(as_text=True)
    signature=request.headers.get("x-webhook-signature","")
    timestamp=request.headers.get("x-webhook-timestamp","")
    expected=base64.b64encode(hmac.new(CASHFREE_WEBHOOK_SECRET.encode(), (timestamp+raw).encode(), hashlib.sha256).digest()).decode()
    if not signature or not timestamp or not hmac.compare_digest(expected,signature):
        return Response("Invalid signature",status=400)
    try: payload=json.loads(raw or "{}")
    except Exception: return Response("Invalid JSON",status=400)
    event_type=str(payload.get("type") or payload.get("event_type") or "").upper()
    data=payload.get("data") or {}
    subscription_id=str(data.get("subscription_id") or data.get("subscriptionId") or "")
    if not subscription_id:
        return jsonify(ok=True)
    conn=get_db(dict_rows=True)
    try:
        cur=conn.cursor(); cur.execute("SELECT customer_id FROM customer_subscriptions WHERE subscription_id=%s LIMIT 1",(subscription_id,)); row=cur.fetchone()
    finally: conn.close()
    if not row: return jsonify(ok=True)
    customer_id=row["customer_id"]
    if event_type in {"SUBSCRIPTION_AUTH_STATUS","SUBSCRIPTION_STATUS_CHANGE"}:
        auth_details = data.get("authorization_details") or data.get("authorisation_details") or {}
        auth_status=str(auth_details.get("authorization_status") or data.get("auth_status") or "").upper()
        status=str(data.get("subscription_status") or data.get("status") or "").upper()
        if auth_status in {"SUCCESS", "ACTIVE"} or status in {"ACTIVE", "BANK_APPROVAL_PENDING"}:
            activate_customer_subscription(customer_id,subscription_id,"ACTIVE" if status != "BANK_APPROVAL_PENDING" else "BANK_APPROVAL_PENDING")
    elif event_type=="SUBSCRIPTION_PAYMENT_SUCCESS":
        extend_subscription_premium(customer_id,subscription_id)
    elif event_type in {"SUBSCRIPTION_PAYMENT_FAILED","SUBSCRIPTION_PAYMENT_CANCELLED"}:
        conn=get_db();
        try:
            cur=conn.cursor(); cur.execute("UPDATE customer_subscriptions SET status=%s, updated_at=NOW() WHERE subscription_id=%s",("PAST_DUE" if event_type.endswith("FAILED") else "CANCELLED",subscription_id)); conn.commit()
        finally: conn.close()
    return jsonify(ok=True)


@app.route("/member")
@customer_login_required
def member_home():
    customer_id = session.get("customer_id")
    if not customer_has_completed_initial_payment(customer_id):
        return redirect(url_for("membership_start"))
    movies = get_member_movies_data()
    top_rated_movies = [m for m in movies if int(m.get("rating_count") or 0) > 0]
    top_rated_movies.sort(key=lambda m: (float(m.get("average_rating") or 0), int(m.get("rating_count") or 0), int(m.get("id") or 0)), reverse=True)
    top_rated_movies = top_rated_movies[:10]
    saved_movies = [m for m in movies if m.get("in_my_list")]
    categories = []
    seen = set()
    for movie in movies:
        for part in str(movie.get("category") or "").split(","):
            category = part.strip()
            if category and category.lower() not in seen:
                seen.add(category.lower())
                categories.append(category)
    return render_template(
        "member_home.html",
        movies=movies,
        top_rated_movies=top_rated_movies,
        saved_movies=saved_movies,
        my_list_movies=saved_movies,
        saved_movie_ids=[int(m["id"]) for m in saved_movies],
        categories=categories,
        customer_email=session.get("customer_email", ""),
        customer_profile=get_customer_profile(customer_id),
        premium=has_active_premium(),
    )

@app.route("/api/movie-search-demand", methods=["POST"])
@customer_login_required
def api_movie_search_demand():
    data = request.get_json(silent=True) or {}
    search_term = str(data.get("search_term") or "").strip()
    search_term = re.sub(r"\\s+", " ", search_term)[:120]
    normalized = search_term.casefold()
    if len(normalized) < 2:
        return json_error("Search term too short.", 400)

    # Do not create a demand for a movie that already exists in the catalog.
    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id FROM movies
            WHERE title ILIKE %s
            LIMIT 1
        """, (search_term,))
        if cur.fetchone():
            cur.close()
            return json_ok(recorded=False, reason="movie_exists")

        cur.execute("""
            INSERT INTO movie_search_demands
                (search_term, normalized_term, search_count, unique_users, last_customer_id)
            VALUES (%s, %s, 1, 0, %s)
            ON CONFLICT (normalized_term)
            DO UPDATE SET
                search_count = movie_search_demands.search_count + 1,
                last_searched_at = CURRENT_TIMESTAMP,
                search_term = EXCLUDED.search_term,
                last_customer_id = EXCLUDED.last_customer_id
            RETURNING id
        """, (search_term, normalized, session.get("customer_id")))
        demand_id = cur.fetchone()["id"]
        cur.execute("""
            INSERT INTO movie_search_demand_users(demand_id, customer_id)
            VALUES(%s, %s)
            ON CONFLICT(demand_id, customer_id) DO NOTHING
        """, (demand_id, session.get("customer_id")))
        cur.execute("""
            UPDATE movie_search_demands d
            SET unique_users = (
                SELECT COUNT(*) FROM movie_search_demand_users u
                WHERE u.demand_id = d.id
            )
            WHERE d.id = %s
        """, (demand_id,))
        conn.commit()
        cur.close()
        return json_ok(recorded=True)
    except Exception as exc:
        conn.rollback()
        return json_error("Search demand save failed: " + str(exc), 500)
    finally:
        conn.close()


@app.route("/api/my-list/<int:movie_id>", methods=["POST", "DELETE"])
@customer_login_required
def api_my_list(movie_id):
    customer_id = session.get("customer_id")
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM movies WHERE id = %s", (movie_id,))
        if not cur.fetchone():
            cur.close()
            return json_error("Movie not found.", 404)
        if request.method == "POST":
            cur.execute(
                """
                INSERT INTO customer_movie_list(customer_id, movie_id)
                VALUES(%s,%s) ON CONFLICT(customer_id,movie_id) DO NOTHING
                """,
                (customer_id, movie_id),
            )
            conn.commit()
            cur.close()
            return json_ok(saved=True, movie_id=movie_id)
        cur.execute(
            "DELETE FROM customer_movie_list WHERE customer_id = %s AND movie_id = %s",
            (customer_id, movie_id),
        )
        removed = cur.rowcount > 0
        conn.commit()
        cur.close()
        return json_ok(saved=False, removed=removed, movie_id=movie_id)
    except Exception as exc:
        conn.rollback()
        return json_error("My List update failed: " + str(exc), 500)
    finally:
        conn.close()


# ============================================================
# ADMIN DASHBOARD MISSING ENDPOINTS
@app.route("/admin/user-restriction/<int:user_id>", methods=["POST"])
@admin_required
def admin_user_restriction(user_id):
    action = str(request.form.get("action", "block")).strip().lower()
    reason = str(request.form.get("reason", "")).strip()[:500]

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT customer_id FROM customer_users WHERE id=%s LIMIT 1", (user_id,))
        row = cur.fetchone()
        if not row:
            cur.close()
            flash("User not found.", "error")
            return redirect(url_for("admin", section="users"))

        customer_id = row[0]

        if action == "unblock":
            cur.execute("UPDATE customer_users SET is_blocked=FALSE, block_reason=NULL WHERE id=%s", (user_id,))
            flash("User unblocked.", "success")
        elif action == "revoke_premium":
            cur.execute("UPDATE customer_access SET premium_until=CURRENT_TIMESTAMP WHERE customer_id=%s AND premium_until IS NOT NULL", (customer_id,))
            cur.execute("UPDATE customer_subscriptions SET status='CANCELLED', premium_until=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE customer_id=%s", (customer_id,))
            flash("Premium access revoked.", "success")
        else:
            cur.execute("UPDATE customer_users SET is_blocked=TRUE, block_reason=%s WHERE id=%s", (reason or "Restricted by Admin", user_id))
            flash("User blocked.", "success")

        conn.commit()
        cur.close()
    finally:
        conn.close()

    return redirect(url_for("admin", section="users"))


# Only added to prevent admin.html BuildError
# ============================================================

@app.route("/admin/users")
@admin_required
def admin_users():
    return redirect(url_for("admin", section="users"))


@app.route("/admin/payments")
@admin_required
def admin_payments():
    return redirect(url_for("admin", section="payments"))


@app.route("/admin/subscriptions")
@admin_required
def admin_subscriptions():
    return redirect(url_for("admin", section="subscriptions"))


@app.route("/admin/watch-activity")
@admin_required
def admin_watch_activity():
    return redirect(url_for("admin", section="watch"))


@app.route("/admin/analytics")
@admin_required
def admin_analytics():
    return redirect(url_for("admin", section="analytics"))


@app.route("/admin/live-activity")
@admin_required
def admin_live_activity():
    return redirect(url_for("admin", section="live"))


@app.route("/admin/notifications")
@admin_required
def admin_notifications():
    return redirect(url_for("admin", section="notifications"))


@app.route("/admin/report-csv/<report_name>")
@admin_required
def admin_report_csv(report_name):
    """Export the admin report tables as CSV without exposing secrets."""
    import csv
    import io

    allowed = {"subscriptions", "payments", "users", "movies", "access", "referrals", "withdrawals"}
    report_name = str(report_name or "").strip().lower()
    if report_name not in allowed:
        return Response("Unknown report.", status=404)

    queries = {
        "subscriptions": (
            "SELECT customer_id, subscription_id, cf_subscription_id, status, auth_status, premium_until, updated_at "
            "FROM customer_subscriptions ORDER BY updated_at DESC",
            "subscriptions.csv",
        ),
        "payments": (
            "SELECT * FROM payment_orders ORDER BY id DESC",
            "payments.csv",
        ),
        "users": (
            "SELECT id, email, mobile, customer_id, full_name, created_at, last_login_at FROM customer_users ORDER BY id DESC",
            "users.csv",
        ),
        "movies": (
            "SELECT id, title, category, views, created_at FROM movies ORDER BY id DESC",
            "movies.csv",
        ),
        "access": (
            "SELECT * FROM customer_access ORDER BY id DESC",
            "access.csv",
        ),
        "referrals": (
            "SELECT * FROM referral_rewards ORDER BY id DESC",
            "referral_rewards.csv",
        ),
        "withdrawals": (
            "SELECT * FROM withdrawal_requests ORDER BY id DESC",
            "withdrawals.csv",
        ),
    }

    sql, filename = queries[report_name]
    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    if rows:
        headers = list(rows[0].keys())
        # Payment exports keep the original database timestamps and add easy-to-read
        # India Standard Time date/day/time columns for both order creation and payment.
        payment_time_columns = []
        if report_name == "payments":
            payment_time_columns = [
                "created_date_ist", "created_day_ist", "created_time_ist",
                "paid_date_ist", "paid_day_ist", "paid_time_ist",
            ]
            headers = headers + [name for name in payment_time_columns if name not in headers]
        writer.writerow(headers)
        for row in rows:
            values = [row.get(h) for h in headers if h not in payment_time_columns]
            if report_name == "payments":
                created_at = row.get("created_at")
                paid_at = row.get("paid_at")
                values.extend([
                    _template_ist_date(created_at, "%d %b %Y") if created_at else "—",
                    _template_ist_date(created_at, "%A") if created_at else "—",
                    _template_ist_date(created_at, "%I:%M:%S %p") + " IST" if created_at else "—",
                    _template_ist_date(paid_at, "%d %b %Y") if paid_at else "—",
                    _template_ist_date(paid_at, "%A") if paid_at else "—",
                    _template_ist_date(paid_at, "%I:%M:%S %p") + " IST" if paid_at else "—",
                ])
            writer.writerow(values)
    else:
        headers = [report_name]
        if report_name == "payments":
            headers += [
                "created_date_ist", "created_day_ist", "created_time_ist",
                "paid_date_ist", "paid_day_ist", "paid_time_ist",
            ]
        writer.writerow(headers)

    response = Response(output.getvalue(), mimetype="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@app.route("/admin/reports")
@admin_required
def admin_reports():
    return redirect(url_for("admin", section="reports"))


@app.route("/admin/r2")
@admin_required
def admin_r2():
    return redirect(url_for("admin", section="r2"))


@app.route("/admin/system-health")
@admin_required
def admin_system_health():
    return redirect(url_for("admin", section="health"))


@app.route("/admin/security")
@admin_required
def admin_security():
    return redirect(url_for("admin", section="security"))


@app.route("/admin/settings")
@admin_required
def admin_settings():
    return redirect(url_for("admin", section="settings"))


@app.route("/admin/search")
@admin_required
def admin_search():
    return redirect(url_for("admin", section="search"))
# ============================================================
# ADMIN
# ============================================================

@app.route("/admin/activity")
@admin_required
def admin_activity():
    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                ca.created_at,
                ca.action,
                ca.path,
                ca.method,
                ca.user_agent,
                u.full_name,
                u.email,
                u.mobile
            FROM customer_activity ca
            LEFT JOIN customer_users u
              ON u.customer_id = ca.customer_id
            ORDER BY ca.id DESC
            LIMIT 300
            """
        )
        activities = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    return render_template(
        "admin_activity.html",
        activities=activities,
    )


_LIVE_SCREEN_TABLE_READY = False
_LIVE_SCREEN_TABLE_LOCK = threading.Lock()


def ensure_live_screen_table():
    global _LIVE_SCREEN_TABLE_READY
    if _LIVE_SCREEN_TABLE_READY:
        return

    with _LIVE_SCREEN_TABLE_LOCK:
        if _LIVE_SCREEN_TABLE_READY:
            return

        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute("""CREATE TABLE IF NOT EXISTS live_screen_sessions (
                id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'REQUESTED',
                offer JSONB,
                answer JSONB,
                admin_candidates JSONB NOT NULL DEFAULT '[]'::jsonb,
                user_candidates JSONB NOT NULL DEFAULT '[]'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_live_screen_customer ON live_screen_sessions(customer_id,status,created_at DESC)")
            conn.commit()
            _LIVE_SCREEN_TABLE_READY = True
        finally:
            conn.close()

@app.route("/admin/live-monitor")
@admin_required
def admin_live_monitor():
    ensure_live_screen_table()
    conn = get_db(dict_rows=True)
    try:
        cur=conn.cursor()
        cur.execute("""SELECT customer_id,full_name,email,mobile,last_login_at
                       FROM customer_users
                       WHERE COALESCE(is_blocked,FALSE)=FALSE
                       ORDER BY COALESCE(last_login_at,created_at) DESC NULLS LAST LIMIT 100""")
        users=cur.fetchall()
    finally:
        conn.close()
    return render_template("admin_live_monitor.html", monitor_users=users)

@app.route("/admin/live-monitor/request",methods=["POST"])
@admin_required
def admin_live_monitor_request():
    ensure_live_screen_table()
    d=request.get_json(silent=True) or {}
    cid=str(d.get("customer_id") or "").strip()
    if not cid: return json_error("Customer ID missing.")
    sid=secrets.token_hex(16)
    conn=get_db()
    try:
        cur=conn.cursor()
        cur.execute("UPDATE live_screen_sessions SET status='STOPPED',updated_at=NOW() WHERE customer_id=%s AND status IN ('REQUESTED','ACTIVE')",(cid,))
        cur.execute("INSERT INTO live_screen_sessions(id,customer_id,status) VALUES(%s,%s,'REQUESTED')",(sid,cid))
        conn.commit()
    finally: conn.close()
    return json_ok(session_id=sid)

@app.route("/admin/live-monitor/offer",methods=["POST"])
@admin_required
def admin_live_monitor_offer():
    ensure_live_screen_table()
    d=request.get_json(silent=True) or {}
    sid=str(d.get("session_id") or "")
    offer=d.get("offer")
    if not sid or not isinstance(offer,dict): return json_error("Invalid offer.")
    conn=get_db()
    try:
        cur=conn.cursor()
        cur.execute("UPDATE live_screen_sessions SET offer=%s::jsonb,updated_at=NOW() WHERE id=%s",(json.dumps(offer),sid))
        conn.commit()
    finally: conn.close()
    return json_ok()

# Cache pending screen-share checks briefly so multiple open tabs do not
# all hit PostgreSQL on every poll. A short TTL keeps new admin requests visible.
_LIVE_PENDING_CACHE = {}
_LIVE_PENDING_CACHE_LOCK = threading.Lock()
_LIVE_PENDING_CACHE_TTL = 4.0

@app.route("/api/live-monitor/pending")
@customer_login_required
def live_monitor_pending():
    ensure_live_screen_table()
    cid = str(session.get("customer_id") or "")
    now = time.monotonic()
    with _LIVE_PENDING_CACHE_LOCK:
        cached = _LIVE_PENDING_CACHE.get(cid)
        if cached and now - cached[0] < _LIVE_PENDING_CACHE_TTL:
            data = dict(cached[1])
            return json_ok(**data)

    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute("""SELECT id,status,offer,answer,admin_candidates
                      FROM live_screen_sessions
                      WHERE customer_id=%s AND status IN ('REQUESTED','ACTIVE')
                      ORDER BY created_at DESC LIMIT 1""", (cid,))
        row = cur.fetchone()
    finally:
        conn.close()

    data = {"requested": bool(row)}
    if row:
        data.update(dict(row))
    with _LIVE_PENDING_CACHE_LOCK:
        _LIVE_PENDING_CACHE[cid] = (time.monotonic(), data)
    return json_ok(**data)

@app.route("/api/live-monitor/answer",methods=["POST"])
@customer_login_required
def live_monitor_answer():
    ensure_live_screen_table()
    d=request.get_json(silent=True) or {}
    sid=str(d.get("session_id") or "")
    answer=d.get("answer")
    cid=str(session.get("customer_id") or "")
    if not sid or not isinstance(answer,dict): return json_error("Invalid answer.")
    conn=get_db()
    try:
        cur=conn.cursor()
        cur.execute("""UPDATE live_screen_sessions SET answer=%s::jsonb,status='ACTIVE',updated_at=NOW()
                       WHERE id=%s AND customer_id=%s AND status='REQUESTED'""",(json.dumps(answer),sid,cid))
        conn.commit()
    finally: conn.close()
    return json_ok()

@app.route("/api/live-monitor/candidate",methods=["POST"])
def live_monitor_candidate():
    ensure_live_screen_table()
    d=request.get_json(silent=True) or {}
    sid=str(d.get("session_id") or "")
    role=str(d.get("role") or "").lower()
    candidate=d.get("candidate")
    if role not in ("admin","user") or not sid or not isinstance(candidate,dict): return json_error("Invalid candidate.")
    if role=="admin" and not session.get("admin_logged_in"): return json_error("Admin login required.",403)
    if role=="user" and not session.get("customer_logged_in"): return json_error("Login required.",401)
    conn=get_db()
    try:
        cur=conn.cursor()
        column="admin_candidates" if role=="admin" else "user_candidates"
        where="id=%s"
        params=[json.dumps([candidate]),sid]
        if role=="user":
            where+=" AND customer_id=%s"; params.append(str(session.get("customer_id") or ""))
        cur.execute(f"UPDATE live_screen_sessions SET {column}=COALESCE({column},'[]'::jsonb)||%s::jsonb,updated_at=NOW() WHERE {where}",params)
        conn.commit()
    finally: conn.close()
    return json_ok()

@app.route("/api/live-monitor/state")
def live_monitor_state():
    ensure_live_screen_table()
    sid=str(request.args.get("session_id") or "")
    conn=get_db(dict_rows=True)
    try:
        cur=conn.cursor(); cur.execute("SELECT * FROM live_screen_sessions WHERE id=%s",(sid,)); row=cur.fetchone()
    finally: conn.close()
    if not row: return json_error("Session not found.",404)
    if not session.get("admin_logged_in") and str(session.get("customer_id") or "")!=str(row["customer_id"]):
        return json_error("Not authorized.",403)
    return json_ok(**dict(row))

@app.route("/api/live-monitor/stop",methods=["POST"])
def live_monitor_stop():
    ensure_live_screen_table()
    d=request.get_json(silent=True) or {}; sid=str(d.get("session_id") or "")
    conn=get_db()
    try:
        cur=conn.cursor(); cur.execute("SELECT customer_id FROM live_screen_sessions WHERE id=%s",(sid,)); row=cur.fetchone()
        if not row: return json_error("Session not found.",404)
        if not session.get("admin_logged_in") and str(session.get("customer_id") or "")!=str(row[0]): return json_error("Not authorized.",403)
        cur.execute("UPDATE live_screen_sessions SET status='STOPPED',updated_at=NOW() WHERE id=%s",(sid,)); conn.commit()
    finally: conn.close()
    return json_ok()


@app.route("/admin/website-control", methods=["POST"])
@admin_required
def admin_website_control():
    action = str(request.form.get("action", "")).strip().lower()
    if action == "off":
        set_setting("website_enabled", "0")
    elif action == "on":
        set_setting("website_enabled", "1")
    else:
        abort(400)
    with _WEBSITE_SWITCH_CACHE_LOCK:
        _WEBSITE_SWITCH_CACHE["value"] = "0" if action == "off" else "1"
        _WEBSITE_SWITCH_CACHE["checked_at"] = time.monotonic()
    return redirect(url_for("admin", section="website-control", saved=action))


@app.route("/admin/errors", methods=["GET", "POST"])
@admin_required
def admin_errors():
    if request.method == "POST" and request.form.get("action") == "clear":
        with _CINEMA_ERROR_LOGS_LOCK:
            _CINEMA_ERROR_LOGS.clear()
        flash("Visible error history cleared. This does not change website code or fix the underlying issue.", "success")
        return redirect(url_for("admin_errors"))

    with _CINEMA_ERROR_LOGS_LOCK:
        errors = list(_CINEMA_ERROR_LOGS)

    diagnostics = None
    if request.args.get("check") == "1":
        diagnostics = []
        try:
            conn = get_db()
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.fetchone()
                cur.close()
            finally:
                conn.close()
            diagnostics.append({"name": "PostgreSQL Database", "ok": True, "detail": "SELECT 1 successful."})
        except Exception as exc:
            detail = str(exc)[:1000]
            diagnostics.append({"name": "PostgreSQL Database", "ok": False, "detail": detail})
            _record_cinema_error(500, "Database diagnostic failed: " + detail, source="Admin diagnostic")

        try:
            get_r2_client().list_objects_v2(Bucket=R2_BUCKET, MaxKeys=1)
            diagnostics.append({"name": "Cloudflare R2", "ok": True, "detail": "Bucket connection successful."})
        except Exception as exc:
            detail = str(exc)[:1000]
            diagnostics.append({"name": "Cloudflare R2", "ok": False, "detail": detail})
            _record_cinema_error(500, "R2 diagnostic failed: " + detail, source="Admin diagnostic")

        with _CINEMA_ERROR_LOGS_LOCK:
            errors = list(_CINEMA_ERROR_LOGS)

    return render_template(
        "admin_errors.html",
        errors=errors,
        diagnostics=diagnostics,
        error_count=len(errors),
    )


@app.route("/admin")
@admin_required
def admin():
    section = str(request.args.get("section", "dashboard")).strip().lower()
    allowed_sections = {
        "dashboard","live-monitor","movies","users","payments","subscriptions","watch","home-photos",
        "analytics","live","ads","adsense","notifications","reports","r2","health",
        "security","settings","search","demand","comments","earnings","withdrawals","support","website-control","help"
    }
    if section not in allowed_sections:
        section = "dashboard"

    q = str(request.args.get("q", "") or "").strip()

    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) AS total_movies, COALESCE(SUM(views),0) AS total_views FROM movies")
        stats = cur.fetchone() or {}

        cur.execute("SELECT COUNT(*) AS n FROM customer_users")
        total_users = int((cur.fetchone() or {}).get("n") or 0)

        cur.execute("""
            SELECT COUNT(*) AS n FROM customer_subscriptions
            WHERE status IN ('ACTIVE','BANK_APPROVAL_PENDING')
        """)
        active_subscriptions = int((cur.fetchone() or {}).get("n") or 0)

        cur.execute("""
            SELECT COALESCE(SUM(amount),0) AS revenue
            FROM payment_orders
            WHERE UPPER(status) IN ('PAID','SUCCESS','COMPLETED')
        """)
        total_revenue = (cur.fetchone() or {}).get("revenue") or 0

        cur.execute("UPDATE referral_rewards SET status='AVAILABLE', updated_at=NOW() WHERE status='PENDING' AND available_at <= NOW()")
        conn.commit()

        cur.execute("SELECT COALESCE(SUM(amount),0) AS n FROM referral_rewards WHERE status IN ('PENDING','AVAILABLE')")
        total_referral_rewards = (cur.fetchone() or {}).get("n") or 0
        cur.execute("SELECT COALESCE(SUM(amount),0) AS n FROM withdrawal_requests WHERE status='PENDING'")
        pending_withdrawals_total=(cur.fetchone() or {}).get("n") or 0
        cur.execute("""SELECT
          COALESCE(SUM(CASE WHEN status IN ('PENDING','APPROVED') THEN COALESCE(total_debit,amount) ELSE 0 END),0) AS reserved,
          COALESCE(SUM(CASE WHEN status='PAID' THEN COALESCE(net_amount,amount) ELSE 0 END),0) AS paid,
          COALESCE(SUM(CASE WHEN status='PAID' THEN COALESCE(withdrawal_fee,0) ELSE 0 END),0) AS fees
          FROM withdrawal_requests""")
        withdrawal_financials=cur.fetchone() or {}
        cur.execute("SELECT COUNT(*) AS n FROM customer_bank_accounts WHERE status='PENDING'")
        pending_bank_verifications=int((cur.fetchone() or {}).get("n") or 0)

        # Load each large admin dataset only on the section that uses it.
        movies = []
        users = []
        payments = []
        payment_summary = {"total": 0, "successful": 0, "pending": 0, "failed": 0, "successful_amount": 0}
        subscriptions = []
        access = []
        top_movies = []
        top_rated_movies = []
        live_events = []
        referral_rewards_admin = []
        withdrawals_admin = []
        bank_accounts_admin = []

        if section == "movies":
            cur.execute("SELECT * FROM movies ORDER BY id DESC")
            movies = cur.fetchall()

        if section == "users":
            cur.execute("""
                SELECT id,email,mobile,customer_id,full_name,profile_photo,created_at,last_login_at
                FROM customer_users ORDER BY id DESC
            """)
            users = cur.fetchall()

        if section == "payments":
            cur.execute("""
                SELECT p.*,u.full_name,u.email,u.mobile
                FROM payment_orders p
                LEFT JOIN customer_users u ON u.customer_id = p.customer_id
                ORDER BY p.id DESC LIMIT 200
            """)
            payments = cur.fetchall()

            # Read-only payment summary for the admin UI; never alters orders or access.
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (
                        WHERE UPPER(COALESCE(status, '')) IN ('PAID','SUCCESS','COMPLETED')
                    ) AS successful,
                    COUNT(*) FILTER (
                        WHERE UPPER(COALESCE(status, '')) IN ('FAILED','CANCELLED','EXPIRED','ERROR','REJECTED')
                    ) AS failed,
                    COUNT(*) FILTER (
                        WHERE UPPER(COALESCE(status, '')) NOT IN
                            ('PAID','SUCCESS','COMPLETED','FAILED','CANCELLED','EXPIRED','ERROR','REJECTED')
                    ) AS pending,
                    COALESCE(SUM(amount) FILTER (
                        WHERE UPPER(COALESCE(status, '')) IN ('PAID','SUCCESS','COMPLETED')
                    ), 0) AS successful_amount
                FROM payment_orders
            """)
            payment_summary = cur.fetchone() or payment_summary

        if section == "subscriptions":
            cur.execute("""
                SELECT customer_id,subscription_id,cf_subscription_id,status,auth_status,premium_until,created_at,updated_at
                FROM customer_subscriptions ORDER BY updated_at DESC LIMIT 200
            """)
            subscriptions = cur.fetchall()

        if section == "watch":
            cur.execute("""
                SELECT ca.customer_id,ca.movie_id,m.title,ca.watch_until,ca.download_until,ca.premium_until,
                       u.full_name,u.email,u.mobile
                FROM customer_access ca
                LEFT JOIN movies m ON m.id=ca.movie_id
                LEFT JOIN customer_users u ON u.customer_id=ca.customer_id
                ORDER BY ca.id DESC LIMIT 200
            """)
            access = cur.fetchall()

        if section == "analytics":
            cur.execute("""
                SELECT m.*, COALESCE(ROUND(AVG(r.rating)::numeric, 1), 0) AS average_rating,
                       COUNT(r.id) AS rating_count
                FROM movies m
                LEFT JOIN movie_ratings r ON r.movie_id = m.id
                GROUP BY m.id
                ORDER BY m.views DESC NULLS LAST, m.id DESC LIMIT 10
            """)
            top_movies = cur.fetchall()
            cur.execute("""
                SELECT m.id, m.title, m.category, m.poster,
                       ROUND(AVG(r.rating)::numeric, 1) AS average_rating,
                       COUNT(r.id) AS rating_count
                FROM movies m
                JOIN movie_ratings r ON r.movie_id = m.id
                GROUP BY m.id
                HAVING COUNT(r.id) >= 1
                ORDER BY AVG(r.rating) DESC, COUNT(r.id) DESC, m.id DESC LIMIT 10
            """)
            top_rated_movies = cur.fetchall()

        if section in {"live", "notifications"}:
            cur.execute("""
                SELECT 'payment' AS event_type, order_id AS ref, customer_id, status, amount, created_at AS event_time
                FROM payment_orders
                UNION ALL
                SELECT 'subscription' AS event_type, subscription_id AS ref, customer_id, status, NULL AS amount, updated_at AS event_time
                FROM customer_subscriptions
                ORDER BY event_time DESC LIMIT 50
            """)
            live_events = cur.fetchall()

        if section == "earnings":
            cur.execute("""
                SELECT rr.id, rr.amount, rr.status, rr.available_at, rr.created_at,
                       rr.referrer_customer_id, rr.referred_customer_id,
                       u.full_name AS referrer_name, r.full_name AS referred_name
                FROM referral_rewards rr
                LEFT JOIN customer_users u ON u.customer_id=rr.referrer_customer_id
                LEFT JOIN customer_users r ON r.customer_id=rr.referred_customer_id
                ORDER BY rr.id DESC LIMIT 300
            """)
            referral_rewards_admin = cur.fetchall()

        if section == "withdrawals":
            cur.execute("""
                SELECT w.*,u.full_name,u.email,u.mobile,
                       b.account_holder_name,b.account_number,b.ifsc,b.bank_name,b.status AS bank_status
                FROM withdrawal_requests w
                LEFT JOIN customer_users u ON u.customer_id=w.customer_id
                LEFT JOIN customer_bank_accounts b ON b.id=w.bank_account_id
                ORDER BY w.id DESC LIMIT 300
            """)
            withdrawals_admin = cur.fetchall()
            cur.execute("""
                SELECT b.*,u.full_name,u.email,u.mobile
                FROM customer_bank_accounts b
                LEFT JOIN customer_users u ON u.customer_id=b.customer_id
                ORDER BY b.id DESC LIMIT 300
            """)
            bank_accounts_admin = cur.fetchall()

        # Support tickets and their message history are needed only on the
        # Support section. Loading them on every admin page caused up to 200
        # extra database queries per request, even on the dashboard.
        support_admin = []
        if section == "support":
            cur.execute("""
                SELECT id, customer_id, subject, status, screenshot_key, created_at, updated_at, resolved_at
                FROM support_tickets
                ORDER BY id DESC LIMIT 200
            """)
            support_admin = cur.fetchall()
            for ticket in support_admin:
                cur.execute("""
                    SELECT id, sender_type, message, created_at
                    FROM support_messages
                    WHERE ticket_id=%s
                    ORDER BY id ASC
                """, (ticket["id"],))
                ticket["messages"] = cur.fetchall()
                ticket["screenshot_url"] = media_url(ticket["screenshot_key"]) if ticket.get("screenshot_key") else None

        search_movies = []
        search_users = []
        search_payments = []
        movie_search_demands = []
        cw_movie_requests = []
        if section == "demand":
            cur.execute("""
                SELECT id, search_term, search_count, unique_users, first_searched_at,
                       last_searched_at, last_customer_id
                FROM movie_search_demands
                ORDER BY search_count DESC, last_searched_at DESC
                LIMIT 300
            """)
            movie_search_demands = cur.fetchall()

            # The homepage and movie-page request forms write to cw_movie_requests.
            # Keep these explicit requests visible alongside search-based demand.
            cur.execute("""
                SELECT r.id, r.customer_id, r.requested_title, r.normalized_title,
                       r.votes, r.status, r.created_at, r.updated_at,
                       u.full_name, u.email, u.mobile,
                       (SELECT COUNT(*)
                          FROM cw_movie_requests r2
                         WHERE r2.normalized_title = r.normalized_title) AS total_requesters
                FROM cw_movie_requests r
                LEFT JOIN customer_users u ON u.customer_id = r.customer_id
                ORDER BY r.updated_at DESC, r.id DESC
                LIMIT 500
            """)
            cw_movie_requests = cur.fetchall()

        if q:
            like = "%" + q + "%"
            cur.execute("""
                SELECT * FROM movies
                WHERE title ILIKE %s OR category ILIKE %s OR description ILIKE %s
                ORDER BY id DESC LIMIT 50
            """, (like,like,like))
            search_movies = cur.fetchall()

            cur.execute("""
                SELECT id,email,mobile,customer_id,full_name,created_at,last_login_at
                FROM customer_users
                WHERE COALESCE(full_name,'') ILIKE %s
                   OR COALESCE(email,'') ILIKE %s
                   OR COALESCE(mobile,'') ILIKE %s
                   OR customer_id ILIKE %s
                ORDER BY id DESC LIMIT 50
            """, (like,like,like,like))
            search_users = cur.fetchall()

            cur.execute("""
                SELECT * FROM payment_orders
                WHERE order_id ILIKE %s OR customer_id ILIKE %s OR payment_type ILIKE %s
                ORDER BY id DESC LIMIT 50
            """, (like,like,like))
            search_payments = cur.fetchall()

        cur.close()
    finally:
        conn.close()

    for movie in movies:
        try:
            movie["poster_url"] = media_url(movie.get("poster")) if movie.get("poster") else None
        except Exception:
            movie["poster_url"] = None

    comments_admin = []
    if section == "comments":
        cur_comments = get_db(dict_rows=True)
        try:
            cc = cur_comments.cursor()
            cc.execute(
                """
                SELECT id, movie_id, display_name, comment, created_at
                FROM movie_comments
                ORDER BY id DESC
                LIMIT 500
                """
            )
            comments_admin = cc.fetchall()
            cc.close()
        finally:
            cur_comments.close()

    # Admin R2 overview: read-only listing only. This does not upload,
    # delete, move, or modify any R2 object.
    r2_objects = []
    r2_error = ""
    if section == "r2":
        try:
            result = get_r2_client().list_objects_v2(
                Bucket=R2_BUCKET,
                MaxKeys=100,
            )
            for obj in result.get("Contents", []):
                r2_objects.append({
                    "key": obj.get("Key", ""),
                    "size": int(obj.get("Size") or 0),
                    "modified": obj.get("LastModified"),
                })
        except Exception as exc:
            r2_error = str(exc)
            print("ADMIN R2 LIST ERROR:", repr(exc))

    admin_config = {
        "DATABASE": bool(DATABASE_URL),
        "R2_BUCKET": R2_BUCKET,
        "SECRET_KEY": bool(app.secret_key),
        "CASHFREE": bool(CASHFREE_APP_ID and CASHFREE_SECRET_KEY),
        "R2": bool(R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET and R2_ENDPOINT),
        "BREVO": bool(BREVO_API_KEY and BREVO_FROM),
        "MESSAGE_CENTRAL": bool(MESSAGE_CENTRAL_CUSTOMER_ID and MESSAGE_CENTRAL_AUTH_TOKEN),
    }

    home_hero_image_key = get_setting("home_hero_image_key", "")
    try:
        home_hero_image_url = media_url(home_hero_image_key) if home_hero_image_key else ""
    except Exception:
        home_hero_image_url = ""

    return render_template(
        "admin.html",
        section=section,
        q=q,
        movies=movies,
        users=users,
        payments=payments,
        payment_summary=payment_summary,
        subscriptions=subscriptions,
        access=access,
        top_movies=top_movies,
        live_events=live_events,
        search_movies=search_movies,
        search_users=search_users,
        search_payments=search_payments,
        movie_search_demands=movie_search_demands,
        cw_movie_requests=cw_movie_requests,
        top_rated_movies=top_rated_movies,
        comments_admin=comments_admin,
        total_movies=int(stats.get("total_movies") or 0),
        total_users=total_users,
        active_subscriptions=active_subscriptions,
        total_views=int(stats.get("total_views") or 0),
        total_revenue=total_revenue,
        ads=get_ads(),
        config=admin_config,
        r2_objects=r2_objects,
        r2_error=r2_error,
        referral_rewards_admin=referral_rewards_admin,
        withdrawals_admin=withdrawals_admin,
        bank_accounts_admin=bank_accounts_admin,
        support_admin=support_admin,
        withdrawal_financials=withdrawal_financials,
        pending_bank_verifications=pending_bank_verifications,
        total_referral_rewards=total_referral_rewards,
        pending_withdrawals_total=pending_withdrawals_total,
        website_enabled=(str(get_setting("website_enabled", "1")).strip().lower() not in {"0", "false", "off", "no"}),
        home_hero_image_url=home_hero_image_url,
    )


# ============================================================
# ADMIN HOME PAGE PHOTO MANAGER
# ============================================================

@app.route("/admin/home-photos", methods=["POST"])
@admin_required
def admin_home_photos():
    image_file = request.files.get("hero_image")
    if not image_file or not image_file.filename:
        flash("Pehle Home Page ki photo select karein.", "error")
        return redirect(url_for("admin", section="home-photos"))

    ext = get_extension(image_file.filename)
    if ext not in ALLOWED_POSTERS:
        flash("Sirf JPG, JPEG, PNG ya WEBP photo upload karein.", "error")
        return redirect(url_for("admin", section="home-photos"))

    image_data = image_file.read(MAX_POSTER_SIZE + 1)
    if not image_data:
        flash("Selected photo khaali hai.", "error")
        return redirect(url_for("admin", section="home-photos"))
    if len(image_data) > MAX_POSTER_SIZE:
        flash("Home Page photo maximum 25 MB ki ho sakti hai.", "error")
        return redirect(url_for("admin", section="home-photos"))

    key = "home/hero-" + secrets.token_hex(16) + "." + ext
    try:
        get_r2_client().upload_fileobj(
            BytesIO(image_data),
            R2_BUCKET,
            key,
            ExtraArgs={
                "ContentType": content_type_for_key(key),
                "CacheControl": "public, max-age=86400",
            },
        )
        # Keep the previous R2 object as a backup; only the active setting changes.
        set_setting("home_hero_image_key", key)
        flash("Home Page photo publish ho gayi. Website refresh karke dekhein.", "success")
    except Exception as exc:
        print("HOME PHOTO UPLOAD ERROR:", repr(exc))
        flash("Photo publish nahi hui. R2 connection aur settings check karein.", "error")

    return redirect(url_for("admin", section="home-photos"))


# ============================================================
# USER EARNINGS
# ============================================================

@app.route("/earnings")
@customer_login_required
def user_earnings():
    customer_id=session.get("customer_id")
    referral_code=ensure_referral_code(customer_id)
    summary=get_earnings_summary(customer_id)
    conn=get_db(dict_rows=True)
    try:
        cur=conn.cursor()
        cur.execute("""SELECT rr.amount,rr.status,rr.available_at,rr.created_at,rr.referred_customer_id,u.full_name,u.email
                       FROM referral_rewards rr LEFT JOIN customer_users u ON u.customer_id=rr.referred_customer_id
                       WHERE rr.referrer_customer_id=%s ORDER BY rr.id DESC LIMIT 100""",(customer_id,))
        rewards=cur.fetchall()
        cur.execute("""SELECT id,amount,withdrawal_fee,net_amount,total_debit,status,admin_note,transaction_ref,utr,created_at,approved_at,paid_at,processed_at
                       FROM withdrawal_requests WHERE customer_id=%s ORDER BY id DESC LIMIT 50""",(customer_id,))
        withdrawals=cur.fetchall()
        cur.execute("""SELECT id,account_holder_name,account_number,ifsc,bank_name,status,verification_note,verification_ref,verified_at,created_at,updated_at
                       FROM customer_bank_accounts WHERE customer_id=%s ORDER BY id DESC LIMIT 1""",(customer_id,))
        bank_account=cur.fetchone()
        cur.execute("SELECT withdrawal_pin_hash IS NOT NULL AS pin_set FROM customer_users WHERE customer_id=%s",(customer_id,))
        pin_set=bool((cur.fetchone() or {}).get("pin_set"))
        cur.execute("""SELECT id,title,message,notification_type,is_read,created_at FROM customer_notifications
                       WHERE customer_id=%s ORDER BY id DESC LIMIT 20""",(customer_id,))
        notifications=cur.fetchall()
    finally:
        conn.close()
    referral_url=url_for("home",ref=referral_code,_external=True) if referral_code else ""
    return render_template("earnings.html",summary=summary,referral_code=referral_code,referral_url=referral_url,
        rewards=rewards,withdrawals=withdrawals,withdrawal_min=WITHDRAWAL_MIN_AMOUNT,
        withdrawal_fee_rate=WITHDRAWAL_FEE_RATE,withdrawal_max=WITHDRAWAL_MAX_AMOUNT,
        bank_account=bank_account,pin_set=pin_set,notifications=notifications)


@app.route("/earnings/pin/change",methods=["POST"])
@customer_login_required
def change_withdrawal_pin():
    customer_id=session.get("customer_id")
    current_pin=str(request.form.get("current_pin","") or "").strip()
    new_pin=str(request.form.get("new_pin","") or "").strip()
    confirm_pin=str(request.form.get("new_pin_confirm","") or "").strip()

    if not re.fullmatch(r"\d{6}", current_pin):
        flash("Current withdrawal PIN must be 6 digits.", "error")
        return redirect(url_for("user_earnings"))

    if not re.fullmatch(r"\d{6}", new_pin) or new_pin != confirm_pin:
        flash("New withdrawal PIN must be 6 digits and both entries must match.", "error")
        return redirect(url_for("user_earnings"))

    conn=get_db()
    try:
        cur=conn.cursor()
        cur.execute("SELECT withdrawal_pin_hash FROM customer_users WHERE customer_id=%s",(customer_id,))
        row=cur.fetchone()
        if not row or not row[0] or not check_password_hash(row[0], current_pin):
            flash("Current withdrawal PIN is incorrect.", "error")
            cur.close()
            return redirect(url_for("user_earnings"))
        cur.execute(
            "UPDATE customer_users SET withdrawal_pin_hash=%s, withdrawal_pin_set_at=CURRENT_TIMESTAMP WHERE customer_id=%s",
            (generate_password_hash(new_pin), customer_id),
        )
        conn.commit()
        cur.close()
        flash("Withdrawal PIN changed successfully.", "success")
    finally:
        conn.close()

    return redirect(url_for("user_earnings"))


@app.route("/earnings/pin",methods=["POST"])
@customer_login_required
def set_withdrawal_pin():
    customer_id=session.get("customer_id")
    pin=str(request.form.get("pin","") or "").strip()
    pin2=str(request.form.get("pin_confirm","") or "").strip()
    if not re.fullmatch(r"\d{6}",pin) or pin!=pin2:
        flash("Withdrawal PIN exactly 6 digits ka hona chahiye aur dono same hone chahiye.","error")
        return redirect(url_for("user_earnings"))
    pin_hash=hmac.new(str(app.secret_key).encode(),("withdrawal-pin:"+pin).encode(),hashlib.sha256).hexdigest()
    conn=get_db()
    try:
        cur=conn.cursor()
        cur.execute("UPDATE customer_users SET withdrawal_pin_hash=%s,withdrawal_pin_set_at=NOW() WHERE customer_id=%s",(pin_hash,customer_id))
        conn.commit()
    finally: conn.close()
    flash("Withdrawal PIN securely set ho gaya.","success")
    return redirect(url_for("user_earnings"))


@app.route("/earnings/bank/change",methods=["POST"])
@customer_login_required
def request_bank_change():
    customer_id=session.get("customer_id")
    conn=get_db(dict_rows=True)
    try:
        cur=conn.cursor()
        cur.execute("""SELECT id FROM withdrawal_requests
                       WHERE customer_id=%s AND status IN ('PENDING','APPROVED','PROCESSING')
                       LIMIT 1""",(customer_id,))
        if cur.fetchone():
            conn.rollback()
            flash("Pending/approved withdrawal complete hone tak bank change locked hai.","error")
            return redirect(url_for("user_earnings"))
        cur.execute("""SELECT id FROM customer_bank_accounts
                       WHERE customer_id=%s AND status='VERIFIED'
                       ORDER BY id DESC LIMIT 1 FOR UPDATE""",(customer_id,))
        bank=cur.fetchone()
        if not bank:
            conn.rollback()
            flash("Verified bank account nahi mila.","error")
            return redirect(url_for("user_earnings"))
        cur.execute("""UPDATE customer_bank_accounts
                       SET status='CHANGE_PENDING',verification_note='User requested bank change.',updated_at=NOW()
                       WHERE id=%s""",(bank["id"],))
        cur.execute("""INSERT INTO customer_notifications(customer_id,title,message,notification_type)
                       VALUES(%s,'Bank change requested','Your existing bank account is locked for withdrawal until the new bank account is verified.','BANK')""",(customer_id,))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally: conn.close()
    flash("Bank change request opened. Ab naya bank account submit karke re-verification kar sakte hain.","success")
    return redirect(url_for("user_earnings"))


@app.route("/earnings/bank",methods=["POST"])
@customer_login_required
def save_withdrawal_bank():
    customer_id=session.get("customer_id")
    holder=re.sub(r"\s+"," ",str(request.form.get("account_holder_name","") or "").strip())[:120]
    account=re.sub(r"\D","",str(request.form.get("account_number","") or ""))
    ifsc=str(request.form.get("ifsc","") or "").strip().upper()
    bank=re.sub(r"\s+"," ",str(request.form.get("bank_name","") or "").strip())[:120]
    if len(holder)<2 or not re.fullmatch(r"\d{8,20}",account):
        flash("Valid account-holder name aur bank account number dijiye.","error"); return redirect(url_for("user_earnings"))
    if not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}",ifsc):
        flash("Valid IFSC code dijiye.","error"); return redirect(url_for("user_earnings"))
    if len(bank)<2:
        flash("Bank name required hai.","error"); return redirect(url_for("user_earnings"))
    conn=get_db(dict_rows=True)
    try:
        cur=conn.cursor()
        cur.execute("SELECT id FROM customer_bank_accounts WHERE customer_id=%s AND status IN ('PENDING','VERIFIED') ORDER BY id DESC LIMIT 1",(customer_id,))
        if cur.fetchone():
            conn.rollback(); flash("Ek active bank account already hai. Pehle existing verification complete/change request process karein.","error"); return redirect(url_for("user_earnings"))
        cur.execute("""INSERT INTO customer_bank_accounts(customer_id,account_holder_name,account_number,ifsc,bank_name,status)
                       VALUES(%s,%s,%s,%s,%s,'PENDING')""",(customer_id,holder,account,ifsc,bank))
        cur.execute("""INSERT INTO customer_notifications(customer_id,title,message,notification_type)
                       VALUES(%s,'Bank verification pending','Your bank account has been submitted for manual verification.','BANK')""",(customer_id,))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally: conn.close()
    flash("Bank account submitted. Admin verification ke baad withdrawal enable hoga.","success")
    return redirect(url_for("user_earnings"))


@app.route("/earnings/withdraw",methods=["POST"])
@customer_login_required
def request_earnings_withdrawal():
    customer_id=session.get("customer_id")
    try: amount=float(str(request.form.get("amount","")).strip())
    except Exception: amount=0
    pin=str(request.form.get("withdrawal_pin","") or "").strip()
    if amount<WITHDRAWAL_MIN_AMOUNT or amount>WITHDRAWAL_MAX_AMOUNT:
        flash(f"Withdrawal ₹{int(WITHDRAWAL_MIN_AMOUNT)} se ₹{int(WITHDRAWAL_MAX_AMOUNT)} ke beech hona chahiye.","error")
        return redirect(url_for("user_earnings"))
    settle_due_referral_rewards(customer_id)
    conn=get_db(dict_rows=True)
    try:
        cur=conn.cursor()
        cur.execute("SELECT withdrawal_pin_hash FROM customer_users WHERE customer_id=%s",(customer_id,))
        user=cur.fetchone() or {}
        expected=user.get("withdrawal_pin_hash")
        if not expected:
            conn.rollback(); flash("Pehle 6-digit Withdrawal PIN set kijiye.","error"); return redirect(url_for("user_earnings"))
        supplied=hmac.new(str(app.secret_key).encode(),("withdrawal-pin:"+pin).encode(),hashlib.sha256).hexdigest()
        if not re.fullmatch(r"\d{6}",pin) or not hmac.compare_digest(supplied,str(expected)):
            conn.rollback(); flash("Withdrawal PIN incorrect hai.","error"); return redirect(url_for("user_earnings"))
        cur.execute("""SELECT id,account_holder_name,account_number,ifsc,bank_name,status,withdrawal_hold_until FROM customer_bank_accounts
                       WHERE customer_id=%s AND status='VERIFIED' ORDER BY id DESC LIMIT 1""",(customer_id,))
        bank=cur.fetchone()
        if not bank:
            conn.rollback(); flash("Verified bank account ke bina withdrawal nahi ho sakta.","error"); return redirect(url_for("user_earnings"))
        if bank.get("withdrawal_hold_until") and bank["withdrawal_hold_until"] > datetime.now():
            conn.rollback()
            flash("Bank verification ke baad security hold complete hone tak withdrawal temporarily locked hai.", "error")
            return redirect(url_for("user_earnings"))
        fee=round(amount*WITHDRAWAL_FEE_RATE,2)
        total_debit=round(amount+fee,2)
        cur.execute("""SELECT COALESCE((SELECT SUM(amount) FROM referral_rewards WHERE referrer_customer_id=%s AND status='AVAILABLE'),0)
                              -COALESCE((SELECT SUM(COALESCE(total_debit,amount)) FROM withdrawal_requests
                                         WHERE customer_id=%s AND status IN ('PENDING','PROCESSING','APPROVED','PAID')),0) AS available""",(customer_id,customer_id))
        available=float((cur.fetchone() or {}).get("available") or 0)
        if total_debit>available+0.001:
            conn.rollback(); flash(f"Insufficient available balance. ₹{total_debit:.2f} total debit ke liye balance chahiye.","error"); return redirect(url_for("user_earnings"))
        key="cw-withdraw-"+secrets.token_hex(16)
        cur.execute("""INSERT INTO withdrawal_requests(customer_id,amount,payout_method,payout_details,status,bank_account_id,withdrawal_fee,net_amount,total_debit,idempotency_key)
                       VALUES(%s,%s,'BANK',%s,'PENDING',%s,%s,%s,%s,%s)""",
                    (customer_id,amount,"BANK:"+str(bank["id"]),bank["id"],fee,amount,total_debit,key))
        cur.execute("""INSERT INTO wallet_ledger(customer_id,entry_type,direction,amount,reference_type,reference_id,description)
                       VALUES(%s,'WITHDRAWAL_RESERVED','DEBIT',%s,'WITHDRAWAL',%s,%s)""",
                    (customer_id,total_debit,key,f"Withdrawal ₹{amount:.2f} reserved; fee ₹{fee:.2f}"))
        cur.execute("""INSERT INTO customer_notifications(customer_id,title,message,notification_type)
                       VALUES(%s,'Withdrawal requested',%s,'WITHDRAWAL')""",
                    (customer_id,f"Withdrawal request for ₹{amount:.2f} submitted. You receive ₹{amount:.2f}; total balance debit is ₹{total_debit:.2f}."))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally: conn.close()
    flash("Withdrawal request submitted. Admin verification and actual payout confirmation required.","success")
    return redirect(url_for("user_earnings"))


# ============================================================
# USER SUPPORT
# ============================================================

@app.route("/help-support", methods=["GET", "POST"])
@customer_login_required
def help_support():
    customer_id = session.get("customer_id")

    if request.method == "POST":
        subject = str(request.form.get("subject", "") or "").strip()
        message = str(request.form.get("message", "") or "").strip()
        screenshot = request.files.get("screenshot")

        if len(subject) < 3 or len(subject) > 150:
            flash("Subject 3–150 characters ka hona chahiye.", "error")
            return redirect(url_for("help_support"))
        if len(message) < 5 or len(message) > 5000:
            flash("Problem 5–5000 characters ki honi chahiye.", "error")
            return redirect(url_for("help_support"))

        screenshot_key = None
        if screenshot and screenshot.filename:
            ext = get_extension(screenshot.filename)
            if ext not in {"jpg", "jpeg", "png", "webp"}:
                flash("Screenshot sirf JPG, PNG ya WEBP hona chahiye.", "error")
                return redirect(url_for("help_support"))
            data = screenshot.read(SUPPORT_MAX_SCREENSHOT_BYTES + 1)
            if len(data) > SUPPORT_MAX_SCREENSHOT_BYTES:
                flash("Screenshot maximum 5 MB ka ho sakta hai.", "error")
                return redirect(url_for("help_support"))
            screenshot_key = "support/" + secrets.token_hex(16) + "." + ext
            get_r2_client().upload_fileobj(
                BytesIO(data),
                R2_BUCKET,
                screenshot_key,
                ExtraArgs={"ContentType": content_type_for_key(screenshot.filename)},
            )

        conn = get_db(dict_rows=True)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO support_tickets(customer_id,subject,status,screenshot_key)
                VALUES(%s,%s,'OPEN',%s)
                RETURNING id
                """,
                (customer_id, subject, screenshot_key),
            )
            ticket_id = cur.fetchone()["id"]
            cur.execute(
                """
                INSERT INTO support_messages(ticket_id,sender_type,message)
                VALUES(%s,'USER',%s)
                """,
                (ticket_id, message),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        flash(f"Support ticket #{ticket_id} created.", "success")
        return redirect(url_for("help_support"))

    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id,subject,status,screenshot_key,created_at,updated_at,resolved_at
            FROM support_tickets
            WHERE customer_id=%s
            ORDER BY id DESC LIMIT 50
            """,
            (customer_id,),
        )
        tickets = cur.fetchall()
        for ticket in tickets:
            cur.execute(
                """
                SELECT id,sender_type,message,created_at
                FROM support_messages
                WHERE ticket_id=%s
                ORDER BY id ASC
                """,
                (ticket["id"],),
            )
            ticket["messages"] = cur.fetchall()
            ticket["screenshot_url"] = media_url(ticket["screenshot_key"]) if ticket.get("screenshot_key") else None
    finally:
        conn.close()

    return render_template("support.html", tickets=tickets)


# ============================================================
# ADMIN EARNINGS / WITHDRAWALS / SUPPORT
# ============================================================

@app.route("/notifications/read/<int:notification_id>",methods=["POST"])
@customer_login_required
def mark_customer_notification_read(notification_id):
    customer_id=session.get("customer_id")
    conn=get_db()
    try:
        cur=conn.cursor()
        cur.execute("UPDATE customer_notifications SET is_read=TRUE WHERE id=%s AND customer_id=%s",(notification_id,customer_id))
        conn.commit()
    finally: conn.close()
    return redirect(url_for("user_earnings"))


@app.route("/admin/bank/<int:bank_id>/process",methods=["POST"])
@admin_required
def admin_process_bank(bank_id):
    action=str(request.form.get("action","") or "").strip().lower()
    note=str(request.form.get("note","") or "").strip()[:1000]
    if action not in {"verify","reject"}:
        flash("Invalid bank verification action.","error"); return redirect(url_for("admin",section="withdrawals"))
    conn=get_db(dict_rows=True)
    try:
        cur=conn.cursor()
        cur.execute("SELECT * FROM customer_bank_accounts WHERE id=%s FOR UPDATE",(bank_id,))
        bank=cur.fetchone()
        if not bank:
            conn.rollback(); flash("Bank account not found.","error"); return redirect(url_for("admin",section="withdrawals"))
        new_status="VERIFIED" if action=="verify" else "REJECTED"
        cur.execute("""UPDATE customer_bank_accounts SET status=%s,verification_note=%s,verification_ref=%s,
                       verified_at=%s,rejected_at=%s,last_verified_at=%s,withdrawal_hold_until=%s,updated_at=NOW() WHERE id=%s""",
                    (new_status,note or None,"ADMIN-"+secrets.token_hex(8),
                     datetime.now() if action=="verify" else None,
                     datetime.now() if action=="reject" else None,
                     datetime.now() if action=="verify" else None,
                     datetime.now()+timedelta(hours=24) if action=="verify" else None,bank_id))
        cur.execute("""INSERT INTO customer_notifications(customer_id,title,message,notification_type)
                       VALUES(%s,%s,%s,'BANK')""",
                    (bank["customer_id"],"Bank verification "+("successful" if action=="verify" else "rejected"),
                     "Your bank account has been "+("verified. Withdrawal is now enabled after your PIN is set." if action=="verify" else "rejected. Please submit corrected bank details.")))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally: conn.close()
    flash("Bank verification updated.","success"); return redirect(url_for("admin",section="withdrawals"))


@app.route("/admin/withdrawal/<int:withdrawal_id>/process",methods=["POST"])
@admin_required
def admin_process_withdrawal(withdrawal_id):
    action=str(request.form.get("action","") or "").strip().lower()
    transaction_ref=str(request.form.get("transaction_ref","") or "").strip()[:150]
    admin_note=str(request.form.get("admin_note","") or "").strip()[:1000]
    if action not in {"approve","paid","reject"}:
        flash("Invalid withdrawal action.","error"); return redirect(url_for("admin",section="withdrawals"))
    conn=get_db(dict_rows=True)
    try:
        cur=conn.cursor()
        cur.execute("""SELECT w.*,b.account_holder_name,b.account_number,b.ifsc,b.bank_name,b.status AS bank_status
                       FROM withdrawal_requests w LEFT JOIN customer_bank_accounts b ON b.id=w.bank_account_id
                       WHERE w.id=%s FOR UPDATE""",(withdrawal_id,))
        row=cur.fetchone()
        if not row:
            conn.rollback(); flash("Withdrawal not found.","error"); return redirect(url_for("admin",section="withdrawals"))
        status=str(row.get("status") or "").upper()
        if action=="approve":
            if status!="PENDING":
                conn.rollback(); flash("Only Pending withdrawals can be approved.","error"); return redirect(url_for("admin",section="withdrawals"))
            if str(row.get("bank_status") or "").upper()!="VERIFIED":
                conn.rollback(); flash("Verified bank account required before approval.","error"); return redirect(url_for("admin",section="withdrawals"))
            cur.execute("""UPDATE withdrawal_requests SET status='APPROVED',admin_note=%s,approved_at=NOW(),processed_at=NOW()
                           WHERE id=%s AND status='PENDING'""",(admin_note or None,withdrawal_id))
            cur.execute("""INSERT INTO customer_notifications(customer_id,title,message,notification_type)
                           VALUES(%s,'Withdrawal approved',%s,'WITHDRAWAL')""",
                        (row["customer_id"],f"Withdrawal #{withdrawal_id} approved for ₹{float(row['net_amount'] or row['amount']):.2f}. Actual payout is still pending."))
            conn.commit(); flash("Approved. Actual bank transfer ke baad hi Paid mark karein.","success")
            return redirect(url_for("admin",section="withdrawals"))
        if action=="paid":
            if status!="APPROVED":
                conn.rollback(); flash("Only Approved withdrawals can be marked Paid.","error"); return redirect(url_for("admin",section="withdrawals"))
            if not transaction_ref:
                conn.rollback(); flash("Actual payout UTR/reference required before Paid.","error"); return redirect(url_for("admin",section="withdrawals"))
            cur.execute("""UPDATE withdrawal_requests SET status='PAID',admin_note=%s,transaction_ref=%s,utr=%s,paid_at=NOW(),processed_at=NOW()
                           WHERE id=%s AND status='APPROVED'""",(admin_note or None,transaction_ref,transaction_ref,withdrawal_id))
            if cur.rowcount!=1:
                conn.rollback(); flash("Withdrawal status changed; Paid was not applied.","error"); return redirect(url_for("admin",section="withdrawals"))
            cur.execute("""INSERT INTO customer_notifications(customer_id,title,message,notification_type)
                           VALUES(%s,'Withdrawal paid',%s,'WITHDRAWAL')""",
                        (row["customer_id"],f"Withdrawal #{withdrawal_id} of ₹{float(row['net_amount'] or row['amount']):.2f} is marked Paid. UTR/reference: {transaction_ref}."))
            conn.commit(); flash("Paid recorded with UTR/reference.","success")
            return redirect(url_for("admin",section="withdrawals"))
        if status not in {"PENDING","APPROVED"}:
            conn.rollback(); flash("This withdrawal cannot be rejected now.","error"); return redirect(url_for("admin",section="withdrawals"))
        reason=admin_note or "Withdrawal rejected by admin."
        cur.execute("""UPDATE withdrawal_requests SET status='REJECTED',admin_note=%s,rejection_reason=%s,processed_at=NOW()
                       WHERE id=%s AND status IN ('PENDING','APPROVED')""",(reason,reason,withdrawal_id))
        cur.execute("""INSERT INTO wallet_ledger(customer_id,entry_type,direction,amount,reference_type,reference_id,description)
                       VALUES(%s,'WITHDRAWAL_RELEASED','CREDIT',%s,'WITHDRAWAL',%s,'Rejected withdrawal released back to available balance')
                       ON CONFLICT DO NOTHING""",(row["customer_id"],row["total_debit"] or row["amount"],str(withdrawal_id)))
        cur.execute("""INSERT INTO customer_notifications(customer_id,title,message,notification_type)
                       VALUES(%s,'Withdrawal rejected',%s,'WITHDRAWAL')""",
                    (row["customer_id"],f"Withdrawal #{withdrawal_id} was rejected. Reserved amount has been released back to your available balance."))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally: conn.close()
    flash("Withdrawal rejected and reserved balance released.","success")
    return redirect(url_for("admin",section="withdrawals"))

@app.route("/admin/support/<int:ticket_id>/reply", methods=["POST"])
@admin_required
def admin_support_reply(ticket_id):
    message = str(request.form.get("message", "") or "").strip()
    status = str(request.form.get("status", "IN_PROGRESS") or "IN_PROGRESS").strip().upper()
    if len(message) < 2 or len(message) > 5000:
        flash("Reply 2–5000 characters ki honi chahiye.", "error")
        return redirect(url_for("admin", section="support"))

    if status not in {"OPEN", "IN_PROGRESS", "RESOLVED", "CLOSED"}:
        status = "IN_PROGRESS"

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM support_tickets WHERE id=%s FOR UPDATE", (ticket_id,))
        if not cur.fetchone():
            conn.rollback()
            flash("Support ticket not found.", "error")
            return redirect(url_for("admin", section="support"))
        cur.execute(
            "INSERT INTO support_messages(ticket_id,sender_type,message) VALUES(%s,'ADMIN',%s)",
            (ticket_id, message),
        )
        cur.execute(
            """
            UPDATE support_tickets
            SET status=%s, updated_at=NOW(), resolved_at=%s
            WHERE id=%s
            """,
            (status, datetime.now() if status in {"RESOLVED","CLOSED"} else None, ticket_id),
        )
        conn.commit()
    finally:
        conn.close()

    flash(f"Reply added to ticket #{ticket_id}.", "success")
    return redirect(url_for("admin", section="support"))


# ============================================================
# ADMIN TRENDING POSITION
# ============================================================

@app.route(
    "/admin/movie/<int:movie_id>/trending",
    methods=["POST"],
)
@admin_required
def admin_movie_trending(movie_id):
    raw = str(request.form.get("trending_position", "") or "").strip()

    if raw:
        try:
            position = int(raw)
        except ValueError:
            flash("Trending position must be a number from 1 to 12.", "error")
            return redirect(url_for("admin", section="movies"))

        if position < 1 or position > 12:
            flash("Trending position must be between 1 and 12.", "error")
            return redirect(url_for("admin", section="movies"))
    else:
        position = None

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE movies
            SET trending_position = %s
            WHERE id = %s
            """,
            (position, movie_id),
        )
        if cur.rowcount == 0:
            conn.rollback()
            cur.close()
            flash("Movie not found.", "error")
            return redirect(url_for("admin", section="movies"))
        conn.commit()
        cur.close()
    finally:
        conn.close()

    flash(
        f"Trending position {'removed' if position is None else position} saved.",
        "success",
    )
    return redirect(url_for("admin", section="movies"))


# ============================================================
# LEGACY ADMIN ADD
# ============================================================

@app.route(
    "/admin/add",
    methods=["GET", "POST"],
)
@admin_required
def admin_add():

    if request.method == "GET":
        return render_template(
            "admin_add.html"
        )

    title = request.form.get(
        "title",
        "",
    ).strip()

    category = request.form.get(
        "category",
        "",
    ).strip()

    description = request.form.get(
        "description",
        "",
    ).strip()

    video = request.files.get(
        "video"
    )

    poster = request.files.get(
        "poster"
    )

    if not title:

        flash(
            "Movie title required.",
            "error",
        )

        return redirect(
            url_for("admin")
        )

    if not video or not video.filename:

        flash(
            "Video required.",
            "error",
        )

        return redirect(
            url_for("admin")
        )

    if not allowed_video(
        video.filename
    ):

        flash(
            "Invalid video format.",
            "error",
        )

        return redirect(
            url_for("admin")
        )

    if poster and poster.filename:

        if not allowed_poster(
            poster.filename
        ):

            flash(
                "Invalid poster format.",
                "error",
            )

            return redirect(
                url_for("admin")
            )

    client = get_r2_client()

    video_key = (
        VIDEO_PREFIX
        + secrets.token_hex(16)
        + "."
        + get_extension(
            video.filename
        )
    )

    poster_key = None

    try:

        client.upload_fileobj(
            video,
            R2_BUCKET,
            video_key,
            ExtraArgs={
                "ContentType":
                    content_type_for_key(
                        video.filename
                    )
            },
        )

        if poster and poster.filename:

            poster_key = (
                POSTER_PREFIX
                + secrets.token_hex(16)
                + "."
                + get_extension(
                    poster.filename
                )
            )

            client.upload_fileobj(
                poster,
                R2_BUCKET,
                poster_key,
                ExtraArgs={
                    "ContentType":
                        content_type_for_key(
                            poster.filename
                        )
                },
            )

        conn = get_db()

        try:

            cur = conn.cursor()

            cur.execute(
                """
                INSERT INTO movies
                (
                    title,
                    category,
                    description,
                    poster,
                    video
                )
                VALUES(%s,%s,%s,%s,%s)
                """,
                (
                    title,
                    category,
                    description,
                    poster_key,
                    video_key,
                ),
            )

            conn.commit()
            cur.close()

        finally:
            conn.close()

        flash(
            "Movie uploaded successfully.",
            "success",
        )

    except Exception as exc:

        print(
            "ADMIN ADD ERROR:",
            repr(exc),
        )

        try:
            r2_delete(video_key)
        except Exception:
            pass

        if poster_key:

            try:
                r2_delete(poster_key)
            except Exception:
                pass

        flash(
            "Upload failed: "
            + str(exc),
            "error",
        )

    return redirect(
        url_for("admin")
    )



# ============================================================
# EDIT MOVIE
# ============================================================

@app.route(
    "/admin/edit/<int:movie_id>",
    methods=["GET"],
)
@admin_required
def admin_edit_movie(movie_id):
    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT *
            FROM movies
            WHERE id = %s
        """, (movie_id,))
        movie = cur.fetchone()
        cur.close()
    finally:
        conn.close()

    if not movie:
        flash("Movie not found.", "error")
        return redirect(url_for("admin"))

    return render_template("admin_edit.html", movie=movie)


@app.route(
    "/api/movie/update",
    methods=["POST"],
)
@admin_required
def admin_update_movie():
    try:
        data = request.get_json(silent=True) or {}
        movie_id = int(data.get("movie_id"))
    except Exception:
        return json_error("Invalid movie.")

    title = str(data.get("title") or "").strip()
    category = str(data.get("category") or "").strip()
    description = str(data.get("description") or "").strip()
    new_video = str(data.get("video_key") or "").strip()
    new_poster = str(data.get("poster_key") or "").strip()

    if not title:
        return json_error("Movie title required.")

    if new_video:
        try:
            validate_r2_key(new_video)
        except Exception as exc:
            return json_error(str(exc))
        if not new_video.startswith(VIDEO_PREFIX):
            return json_error("Invalid video key.")

    if new_poster:
        try:
            validate_r2_key(new_poster)
        except Exception as exc:
            return json_error(str(exc))
        if not new_poster.startswith(POSTER_PREFIX):
            return json_error("Invalid poster key.")

    conn = get_db(dict_rows=True)
    old_video = None
    old_poster = None
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT video, poster
            FROM movies
            WHERE id = %s
        """, (movie_id,))
        old = cur.fetchone()

        if not old:
            cur.close()
            return json_error("Movie not found.", 404)

        old_video = old.get("video")
        old_poster = old.get("poster")
        video_value = new_video or old_video
        poster_value = new_poster or old_poster

        cur.execute("""
            UPDATE movies
            SET title=%s,
                category=%s,
                description=%s,
                video=%s,
                poster=%s
            WHERE id=%s
        """, (
            title,
            category,
            description,
            video_value,
            poster_value,
            movie_id,
        ))
        conn.commit()
        cur.close()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()

    if new_video and old_video and new_video != old_video:
        try:
            r2_delete(old_video)
        except Exception as exc:
            print("OLD VIDEO DELETE ERROR:", repr(exc))

    if new_poster and old_poster and new_poster != old_poster:
        try:
            r2_delete(old_poster)
        except Exception as exc:
            print("OLD POSTER DELETE ERROR:", repr(exc))

    return json_ok(
        movie_id=movie_id,
        message="Movie updated successfully.",
    )


# ============================================================
# DELETE MOVIE
# ============================================================

@app.route(
    "/admin/delete/<int:movie_id>"
)
@admin_required
def admin_delete_movie(movie_id):

    conn = get_db(
        dict_rows=True
    )

    try:

        cur = conn.cursor()

        cur.execute(
            """
            SELECT *
            FROM movies
            WHERE id = %s
            """,
            (movie_id,),
        )

        movie = cur.fetchone()

        if movie:

            cur.execute(
                """
                DELETE FROM movies
                WHERE id = %s
                """,
                (movie_id,),
            )

            conn.commit()

        cur.close()

    finally:
        conn.close()

    if movie:

        for key in (
            movie.get("video"),
            movie.get("poster"),
        ):

            if key:

                try:
                    r2_delete(key)
                except Exception as exc:
                    print(
                        "R2 DELETE ERROR:",
                        repr(exc),
                    )

        flash(
            "Movie deleted successfully.",
            "success",
        )

    else:

        flash(
            "Movie not found.",
            "error",
        )

    return redirect(
        url_for("admin")
    )


# ============================================================
# R2 MULTIPART CREATE
# ============================================================

@app.route(
    "/api/r2/multipart/create",
    methods=["POST"],
)
@r2_upload_required
def r2_multipart_create():

    try:

        data = (
            request.get_json(
                silent=True
            )
            or {}
        )

        key = validate_r2_key(
            data.get("key")
        )
        enforce_r2_upload_key(key)

        content_type = (
            data.get("content_type")
            or content_type_for_key(key)
        )

        result = get_r2_client().create_multipart_upload(
            Bucket=R2_BUCKET,
            Key=key,
            ContentType=content_type,
        )

        return json_ok(
            upload_id=result["UploadId"],
            key=key,
            part_size=PART_SIZE,
            parallel=PARALLEL_PARTS,
            expires=PRESIGNED_EXPIRES,
        )

    except Exception as exc:

        print(
            "R2 CREATE ERROR:",
            repr(exc),
        )

        return json_error(
            "R2 multipart create failed: "
            + str(exc),
            500,
        )


# ============================================================
# R2 MULTIPART URLS
# ============================================================

@app.route(
    "/api/r2/multipart/urls",
    methods=["POST"],
)
@r2_upload_required
def r2_multipart_urls():

    try:

        data = (
            request.get_json(
                silent=True
            )
            or {}
        )

        key = validate_r2_key(
            data.get("key")
        )

        upload_id = str(
            data.get(
                "upload_id",
                "",
            )
        ).strip()

        if not upload_id:
            return json_error(
                "upload_id missing."
            )

        raw_parts = data.get(
            "part_numbers"
        ) or []

        part_numbers = []

        for value in raw_parts:

            number = int(value)

            if (
                number < 1
                or number > MAX_MULTIPART_PARTS
            ):
                raise ValueError(
                    "Invalid part number."
                )

            part_numbers.append(
                number
            )

        part_numbers = sorted(
            set(part_numbers)
        )

        if not part_numbers:
            return json_error(
                "part_numbers missing."
            )

        client = get_r2_client()

        urls = {}

        for number in part_numbers:

            urls[str(number)] = (
                client.generate_presigned_url(
                    "upload_part",
                    Params={
                        "Bucket": R2_BUCKET,
                        "Key": key,
                        "UploadId": upload_id,
                        "PartNumber": number,
                    },
                    ExpiresIn=PRESIGNED_EXPIRES,
                )
            )

        return json_ok(
            urls=urls,
            url_map=urls,
            part_urls=urls,
            part_size=PART_SIZE,
            parallel=PARALLEL_PARTS,
            expires=PRESIGNED_EXPIRES,
            total_parts=len(part_numbers),
        )

    except Exception as exc:

        print(
            "R2 URL ERROR:",
            repr(exc),
        )

        return json_error(
            "R2 multipart URLs failed: "
            + str(exc),
            500,
        )


# ============================================================
# R2 MULTIPART COMPLETE
# ============================================================

@app.route(
    "/api/r2/multipart/complete",
    methods=["POST"],
)
@r2_upload_required
def r2_multipart_complete():

    try:

        data = (
            request.get_json(
                silent=True
            )
            or {}
        )

        key = validate_r2_key(
            data.get("key")
        )

        upload_id = str(
            data.get(
                "upload_id",
                "",
            )
        ).strip()

        raw_parts = data.get(
            "parts"
        ) or []

        parts = []

        for item in raw_parts:

            if not isinstance(
                item,
                dict,
            ):
                continue

            number = (
                item.get("PartNumber")
                or item.get("part_number")
                or item.get("part")
            )

            etag = (
                item.get("ETag")
                or item.get("etag")
            )

            if number and etag:

                parts.append({
                    "PartNumber": int(number),
                    "ETag": str(etag),
                })

        parts.sort(
            key=lambda x: x["PartNumber"]
        )

        if not parts:
            return json_error(
                "No multipart parts supplied."
            )

        result = (
            get_r2_client()
            .complete_multipart_upload(
                Bucket=R2_BUCKET,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={
                    "Parts": parts
                },
            )
        )

        head = r2_head(key)

        size = int(
            head.get(
                "ContentLength",
                0,
            )
        )

        if size <= 0:
            return json_error(
                "R2 object is empty.",
                500,
            )

        return json_ok(
            key=key,
            size=size,
            etag=result.get("ETag"),
            public_url=r2_public_url(key),
        )

    except Exception as exc:

        print(
            "R2 COMPLETE ERROR:",
            repr(exc),
        )

        return json_error(
            "R2 multipart complete failed: "
            + str(exc),
            500,
        )


# ============================================================
# R2 MULTIPART ABORT
# ============================================================

@app.route(
    "/api/r2/multipart/abort",
    methods=["POST"],
)
@r2_upload_required
def r2_multipart_abort():

    try:

        data = (
            request.get_json(
                silent=True
            )
            or {}
        )

        key = validate_r2_key(
            data.get("key")
        )

        upload_id = str(
            data.get(
                "upload_id",
                "",
            )
        ).strip()

        get_r2_client().abort_multipart_upload(
            Bucket=R2_BUCKET,
            Key=key,
            UploadId=upload_id,
        )

        return json_ok(
            message="Multipart upload aborted."
        )

    except Exception as exc:

        return json_error(
            str(exc),
            500,
        )


# ============================================================
# SAVE MOVIE
# ============================================================

@app.route(
    "/api/movie/save",
    methods=["POST"],
)
@admin_required
def api_movie_save():

    try:

        data = (
            request.get_json(
                silent=True
            )
            or {}
        )

        title = str(
            data.get("title", "")
        ).strip()

        category = str(
            data.get("category", "")
        ).strip()

        description = str(
            data.get("description", "")
        ).strip()

        video_key = (
            data.get("video")
            or data.get("video_key")
        )

        poster_key = (
            data.get("poster")
            or data.get("poster_key")
        )
        content_type = str(data.get("content_type", "MOVIE") or "MOVIE").strip().upper()
        if content_type not in {"MOVIE", "DRAMA"}:
            return json_error("Invalid content section.")

        if not title:
            return json_error(
                "Movie title missing."
            )

        video_key = validate_r2_key(
            video_key
        )

        if not video_key.startswith(
            VIDEO_PREFIX
        ):
            return json_error(
                "Invalid video key."
            )

        if poster_key:

            poster_key = validate_r2_key(
                poster_key
            )

            if not poster_key.startswith(
                POSTER_PREFIX
            ):
                return json_error(
                    "Invalid poster key."
                )

        video_head = r2_head(
            video_key
        )

        video_size = int(
            video_head.get(
                "ContentLength",
                0,
            )
        )

        if video_size <= 0:
            return json_error(
                "Video R2 object is empty."
            )

        if video_size > MAX_VIDEO_SIZE:
            return json_error(
                "Video exceeds 4 GB."
            )

        if poster_key:

            poster_head = r2_head(
                poster_key
            )

            if int(
                poster_head.get(
                    "ContentLength",
                    0,
                )
            ) <= 0:
                return json_error(
                    "Poster is empty."
                )

        conn = get_db()

        try:

            cur = conn.cursor()

            cur.execute(
                """
                INSERT INTO movies
                (
                    title,
                    category,
                    description,
                    poster,
                    video,
                    content_type
                )
                VALUES(%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                (
                    title,
                    category,
                    description,
                    poster_key,
                    video_key,
                    content_type,
                ),
            )

            movie_id = cur.fetchone()[0]

            # Fulfil matching customer movie requests and create a real
            # in-app notification for every customer who requested it.
            normalized_title = re.sub(r"\\s+", " ", title).strip().casefold()
            cur.execute("""
                SELECT id
                FROM movie_search_demands
                WHERE normalized_term = %s
                  AND (fulfilled_movie_id IS NULL OR fulfilled_movie_id = %s)
                LIMIT 1
            """, (normalized_title, movie_id))
            demand_row = cur.fetchone()
            if demand_row:
                cur.execute("""
                    SELECT DISTINCT customer_id
                    FROM movie_search_demand_users
                    WHERE demand_id = %s
                """, (demand_row[0],))
                demand_users = cur.fetchall()
                for demand_user in demand_users:
                    cur.execute("""
                        INSERT INTO customer_notifications
                            (customer_id, title, message, notification_type)
                        VALUES
                            (%s, %s, %s, 'MOVIE')
                    """, (
                        demand_user[0],
                        "Movie is now available 🎬",
                        title + " is now available on CINEMA WORLD. Open it and start watching.",
                    ))
                cur.execute("""
                    UPDATE movie_search_demands
                    SET fulfilled_movie_id = %s,
                        last_searched_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                """, (movie_id, demand_row[0]))

            conn.commit()
            cur.close()

        finally:
            conn.close()

        return json_ok(
            movie_id=movie_id,
            video_key=video_key,
            poster_key=poster_key,
            video_url=url_for(
                "stream_movie",
                movie_id=movie_id,
            ),
        )

    except Exception as exc:

        print(
            "MOVIE SAVE ERROR:",
            repr(exc),
        )

        return json_error(
            "Movie save failed: "
            + str(exc),
            500,
        )


# ============================================================
# ADS
# ============================================================

@app.route(
    "/admin/ads",
    methods=["GET", "POST"],
)
@admin_required
def admin_ads():

    if request.method == "POST":

        set_setting(
            "ad_top",
            request.form.get(
                "ad_top",
                "",
            ),
        )

        set_setting(
            "ad_player",
            request.form.get(
                "ad_player",
                "",
            ),
        )

        set_setting(
            "ad_bottom",
            request.form.get(
                "ad_bottom",
                "",
            ),
        )

        flash(
            "Ads settings saved.",
            "success",
        )

        return redirect(
            url_for("admin_ads")
        )

    return render_template(
        "ads.html",
        ads=get_ads(),
    )


# ============================================================
# ADS TXT
# ============================================================

@app.route("/ads.txt")
def ads_txt():

    return Response(
        "google.com, pub-8697157365303435, DIRECT, f08c47fec0942fa0\n",
        mimetype="text/plain",
    )


# ============================================================
# R2 HEALTH
# ============================================================

@app.route("/r2-health")
def r2_health():

    try:

        get_r2_client().list_objects_v2(
            Bucket=R2_BUCKET,
            MaxKeys=1,
        )

        return json_ok(
            message="R2 OK",
            bucket=R2_BUCKET,
        )

    except Exception as exc:

        print(
            "R2 HEALTH ERROR:",
            repr(exc),
        )

        return json_error(
            "R2 ERROR: " + str(exc),
            500,
        )


# ============================================================
# DB HEALTH
# ============================================================

@app.route("/db-health")
def db_health():

    try:

        conn = get_db()

        try:

            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.close()

        finally:
            conn.close()

        return json_ok(
            message="Database OK"
        )

    except Exception as exc:

        return json_error(
            "Database ERROR: "
            + str(exc),
            500,
        )


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    return jsonify({
        "ok": True,
        "status": "ok",
        "app": "CINEMA WORLD",
    })


# ============================================================
# LEGACY POSTER
# ============================================================

@app.route(
    "/poster/<path:name>"
)
def legacy_poster(name):

    try:

        key = name

        if not key.startswith(
            POSTER_PREFIX
        ):
            key = POSTER_PREFIX + name

        return redirect(
            r2_presigned_url(key)
        )

    except Exception:

        return Response(
            "Poster not found.",
            status=404,
        )


# ============================================================
# LEGACY VIDEO
# ============================================================

@app.route(
    "/video/<path:name>"
)
def legacy_video(name):

    try:

        key = name

        if not key.startswith(
            VIDEO_PREFIX
        ):
            key = VIDEO_PREFIX + name

        return redirect(
            r2_presigned_url(key)
        )

    except Exception:

        return Response(
            "Video not found.",
            status=404,
        )


# ============================================================
# ERROR 413
# ============================================================

@app.errorhandler(413)
def request_entity_too_large(error):

    return Response(
        "File too large. Maximum 4 GB.",
        status=413,
    )


# ============================================================
# ERROR 404
# ============================================================

@app.errorhandler(404)
def not_found(error):

    return Response(
        """
        <!doctype html>
        <html>
        <head>
            <title>404 | CINEMA WORLD</title>
            <meta name="viewport"
                  content="width=device-width,initial-scale=1">
        </head>
        <body style="
            margin:0;
            background:#080808;
            color:white;
            font-family:Arial;
            display:flex;
            align-items:center;
            justify-content:center;
            min-height:100vh;
            text-align:center;
        ">
            <div>
                <h1 style="font-size:60px;margin:0">404</h1>
                <p>Page not found.</p>
                <a href="/"
                   style="color:#ffc400">
                    Go Home
                </a>
            </div>
        </body>
        </html>
        """,
        status=404,
        mimetype="text/html",
    )


# ============================================================
# ERROR 500
# ============================================================

@app.errorhandler(500)
def internal_error(error):

    original = getattr(error, "original_exception", None)
    trace_text = "".join(traceback.format_exception(type(original), original, original.__traceback__)) if original else ""
    _record_cinema_error(500, str(original or error), source="Flask exception", trace_text=trace_text)
    app.logger.error("500 ERROR: %r", error, exc_info=original is not None)

    print(
        "500 ERROR:",
        repr(error),
    )

    return Response(
        """
        <!doctype html>
        <html>
        <head>
            <title>500 | CINEMA WORLD</title>
            <meta name="viewport"
                  content="width=device-width,initial-scale=1">
        </head>
        <body style="
            margin:0;
            background:#080808;
            color:white;
            font-family:Arial;
            display:flex;
            align-items:center;
            justify-content:center;
            min-height:100vh;
            text-align:center;
        ">
            <div>
                <h1 style="
                    font-size:55px;
                    color:#ff315b;
                    margin:0;
                ">
                    500
                </h1>
                <p>Server error.</p>
                <a href="/"
                   style="color:#ffc400">
                    Go Home
                </a>
            </div>
        </body>
        </html>
        """,
        status=500,
        mimetype="text/html",
    )


# ============================================================
# ONE-TIME R2 MOVIE RECOVERY
# ============================================================

def recover_known_r2_movies():
    """Restore and repair the known R2 movie-to-poster mappings.

    This is database-only: it never uploads, deletes, renames, or modifies R2
    objects. The poster is always tied to its exact movie video key.
    """
    legacy_movies = [
        {
            "title": "Vishwanath & Sons | Full Movie in Hindi Dubbed | Suriya, Mamitha Baiju | Venky Atluri",
            "category": "Drama",
            "description": "Vishwanath & Sons movie",
            "poster": "posters/ed7160c7c44d4d86830f72ce6667c73c.png",
            "video": "videos/7143734e197443298a636a93349dd124.mp4",
        },
    ]

    conn = get_db()
    try:
        cur = conn.cursor()
        for movie in legacy_movies:
            # Repair an existing row if its poster was accidentally paired
            # with the other movie. Matching is done by the exact video key.
            # Keep the two existing movie banners swapped exactly as requested.
            # Movie 1 video -> Movie 2 poster
            # Movie 2 video -> Movie 1 poster
            poster_for_video = {
                "videos/7143734e197443298a636a93349dd124.mp4":
                    "posters/d4382d2e21e04d70896cba154b11185b.webp",
                "videos/ad2f2438ef2948d28000b4bbfc5e164e.mp4":
                    "posters/ed7160c7c44d4d86830f72ce6667c73c.png",
            }

            cur.execute(
                """
                UPDATE movies
                SET poster = %s
                WHERE video = %s
                """,
                (
                    poster_for_video.get(movie["video"], movie["poster"]),
                    movie["video"],
                ),
            )

            # If the movie row does not exist, restore it without creating a
            # duplicate when the exact video key is already present.
            cur.execute(
                """
                INSERT INTO movies (title, category, description, poster, video)
                SELECT %s, %s, %s, %s, %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM movies WHERE video = %s
                )
                """,
                (
                    movie["title"],
                    movie["category"],
                    movie["description"],
                    movie["poster"],
                    movie["video"],
                    movie["video"],
                ),
            )
        conn.commit()
        cur.close()
        print("R2 movie/poster mapping check completed.")
    finally:
        conn.close()




# Register the CINEMA WORLD feature routes after all app helpers/endpoints exist.
# cinema_features imports the already-created app object and adds request/admin features.
import cinema_features  # noqa: F401


# ============================================================
# DATABASE INIT
# ============================================================

try:

    if DATABASE_URL:
        init_db()
        recover_known_r2_movies()
        print(
            "Database initialized successfully."
        )
    else:
        print(
            "WARNING: DATABASE_URL missing."
        )

except Exception as exc:

    print(
        "Database initialization error:",
        repr(exc),
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "5000",
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
    )
    