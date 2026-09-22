import os
import json
import secrets
import mimetypes
import re
import time
import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import boto3
import psycopg2
from psycopg2.extras import RealDictCursor
from botocore.client import Config

from flask import (
    Flask,
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


# ============================================================
# APP
# ============================================================

app = Flask(__name__)

app.secret_key = (
    os.environ.get("SECRET_KEY", "").strip()
    or secrets.token_hex(32)
)

app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024


# ============================================================
# ADMIN
# ============================================================

ADMIN_USER = os.environ.get(
    "ADMIN_USER", "admin"
).strip() or "admin"

ADMIN_PASSWORD = os.environ.get(
    "ADMIN_PASSWORD", "change-me-now"
).strip() or "change-me-now"


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
PREMIUM_PRICE = 99.00
PREMIUM_DAYS = 30
SUBSCRIPTION_AUTH_AMOUNT = 1.00
SUBSCRIPTION_PLAN_NAME = os.environ.get(
    "CASHFREE_SUBSCRIPTION_PLAN_NAME",
    "CINEMA WORLD Premium Monthly",
).strip() or "CINEMA WORLD Premium Monthly"
SUBSCRIPTION_MAX_CYCLES = 120
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
PARALLEL_PARTS = 3
PRESIGNED_EXPIRES = 3600
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

    raise ValueError("Invalid R2 object prefix.")


# ============================================================
# DATABASE
# ============================================================

def get_db(dict_rows=False):
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not configured."
        )

    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=(
            RealDictCursor if dict_rows else None
        ),
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
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

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


def get_ads():
    return {
        "top": get_setting("ad_top", ""),
        "player": get_setting("ad_player", ""),
        "bottom": get_setting("ad_bottom", ""),
    }


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
    customer_id = "tm_" + secrets.token_hex(16)

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO customer_users
            (email, customer_id, mobile, last_login_at)
            VALUES(NULL, %s, %s, NOW())
            """,
            (customer_id, mobile),
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
                    last_login_at
                )
                VALUES(%s, %s, NOW())
                """,
                (
                    email,
                    target_customer_id,
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

    return target_customer_id


@app.route(
    "/login/request-otp",
    methods=["POST"],
)
def request_otp():

    email = normalize_email(
        request.form.get("email", "")
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
                return redirect(
                    url_for(
                        "login",
                        step="otp",
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
        )
    )


@app.route(
    "/login/verify-otp",
    methods=["POST"],
)
def verify_otp():
    """
    Verify the latest email OTP and complete the customer login.

    This version deliberately keeps the OTP active until the customer
    account binding succeeds. It also lets PostgreSQL perform the expiry
    check, avoiding any Render/Python clock mismatch.
    """
    email = normalize_email(session.get("otp_email", ""))
    otp = str(request.form.get("otp", "")).strip()

    print(
        "OTP VERIFY START:",
        "email=", email,
        "otp_length=", len(otp),
    )

    if not email or not valid_email(email):
        print("OTP VERIFY FAILED: missing/invalid session email")
        flash("OTP session expired. Please request a new OTP.", "error")
        return redirect(url_for("login"))

    if not re.fullmatch(r"\d{6}", otp):
        print("OTP VERIFY FAILED: invalid OTP format")
        flash("Enter the 6 digit OTP.", "error")
        return redirect(url_for("login", step="otp"))

    conn = get_db(dict_rows=True)

    try:
        cur = conn.cursor()

        # Lock the latest active OTP so two simultaneous submissions cannot
        # consume the same code.
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
            FOR UPDATE
            """,
            (email,),
        )

        row = cur.fetchone()

        if not row:
            cur.close()
            print("OTP VERIFY FAILED: no active OTP row")
            flash("OTP expired or not found. Please request a new OTP.", "error")
            return redirect(url_for("login"))

        attempts = int(row.get("attempts") or 0)
        print(
            "OTP VERIFY ROW:",
            "id=", row["id"],
            "attempts=", attempts,
            "expires_at=", row["expires_at"],
        )

        if attempts >= OTP_MAX_ATTEMPTS:
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
            print("OTP VERIFY FAILED: max attempts")
            flash("Too many wrong attempts. Request a new OTP.", "error")
            return redirect(url_for("login"))

        # Let PostgreSQL compare its own current time with the stored expiry.
        cur.execute(
            "SELECT (expires_at <= NOW()) AS expired FROM email_otps WHERE id = %s",
            (row["id"],),
        )
        expiry_row = cur.fetchone()

        if expiry_row and expiry_row["expired"]:
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
            print("OTP VERIFY FAILED: expired")
            flash("OTP expired. Please request a new OTP.", "error")
            return redirect(url_for("login"))

        expected_hash = otp_hash(email, otp)

        if not hmac.compare_digest(str(row["otp_hash"]), expected_hash):
            new_attempts = attempts + 1

            if new_attempts >= OTP_MAX_ATTEMPTS:
                cur.execute(
                    """
                    UPDATE email_otps
                    SET attempts = %s,
                        used_at = NOW()
                    WHERE id = %s
                    """,
                    (new_attempts, row["id"]),
                )
            else:
                cur.execute(
                    """
                    UPDATE email_otps
                    SET attempts = %s
                    WHERE id = %s
                    """,
                    (new_attempts, row["id"]),
                )

            conn.commit()
            cur.close()

            remaining = max(0, OTP_MAX_ATTEMPTS - new_attempts)
            print(
                "OTP VERIFY FAILED: wrong OTP; remaining=",
                remaining,
            )

            if remaining:
                flash(
                    "Wrong OTP. " + str(remaining) + " attempts left.",
                    "error",
                )
                return redirect(url_for("login", step="otp"))

            flash("Too many wrong attempts. Request a new OTP.", "error")
            return redirect(url_for("login"))

        # OTP is correct. First bind/create the customer account while the
        # database transaction is still available. If account setup fails,
        # the OTP remains usable instead of trapping the user outside login.
        print("OTP VERIFY SUCCESS: hash matched; binding customer")

        cur.close()
        conn.commit()

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    try:
        customer_id = bind_customer_email(email)
        print(
            "OTP LOGIN SUCCESS:",
            "email=", email,
            "customer_id=", customer_id,
        )
    except Exception as exc:
        print("CUSTOMER EMAIL BIND ERROR:", repr(exc))
        flash(
            "Email verified, but account setup failed. Please try again.",
            "error",
        )
        return redirect(url_for("login", step="otp"))

    # Only consume the OTP after account binding has succeeded.
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE email_otps
            SET used_at = NOW()
            WHERE email = %s
              AND used_at IS NULL
            """,
            (email,),
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()

    session.pop("otp_email", None)
    session.pop("otp_sent_at", None)
    session["customer_logged_in"] = True
    session["customer_id"] = customer_id
    session["customer_email"] = email
    session.modified = True

    flash("Email verified. Welcome to CINEMA WORLD!", "success")

    return redirect(url_for("user_details"))


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

        return True

    except Exception:
        return False


def access_for_movie(movie_id):
    customer_id = get_customer_id()

    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT watch_until, download_until, premium_until
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

    for row in rows:
        if (
            row["watch_until"] is None
            and row["premium_until"] is None
            and row["download_until"] is None
        ):
            permanent_watch = True
        if row["watch_until"] is not None and row["watch_until"] > now:
            temporary_watch = True
        if row["premium_until"] is not None and row["premium_until"] > now:
            premium = True

    return {
        "watch": permanent_watch or temporary_watch or premium,
        "download": premium,
        "premium": premium,
    }


def grant_access(customer_id, movie_id, payment_type):
    conn = get_db()
    try:
        cur = conn.cursor()
        now = datetime.now()

        if payment_type == "watch":
            # ₹1 gives permanent account-wide watch access.
            cur.execute(
                """
                SELECT id FROM customer_access
                WHERE customer_id = %s
                  AND movie_id IS NULL
                  AND watch_until IS NULL
                  AND premium_until IS NULL
                  AND download_until IS NULL
                LIMIT 1
                """,
                (customer_id,),
            )
            if not cur.fetchone():
                cur.execute(
                    """
                    INSERT INTO customer_access
                    (customer_id, movie_id, watch_until, download_until, premium_until)
                    VALUES(%s, NULL, NULL, NULL, NULL)
                    """,
                    (customer_id,),
                )

        elif payment_type == "premium":
            cur.execute(
                """
                SELECT premium_until
                FROM customer_access
                WHERE customer_id = %s
                  AND movie_id IS NULL
                  AND premium_until IS NOT NULL
                ORDER BY premium_until DESC
                LIMIT 1
                """,
                (customer_id,),
            )
            row = cur.fetchone()
            current_until = row[0] if row else None
            base = current_until if current_until and current_until > now else now
            until = base + timedelta(days=PREMIUM_DAYS)

            cur.execute(
                """
                DELETE FROM customer_access
                WHERE customer_id = %s
                  AND movie_id IS NULL
                  AND premium_until IS NOT NULL
                """,
                (customer_id,),
            )
            cur.execute(
                """
                INSERT INTO customer_access
                (customer_id, movie_id, watch_until, download_until, premium_until)
                VALUES(%s, NULL, NULL, NULL, %s)
                """,
                (customer_id, until),
            )

        else:
            raise ValueError("Unsupported payment type.")

        conn.commit()
        cur.close()
    finally:
        conn.close()


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

@app.route(
    "/api/payment/create",
    methods=["POST"],
)
def create_payment():
    data = request.get_json(silent=True) or {}
    payment_type = str(data.get("payment_type", "")).strip().lower()
    movie_id = data.get("movie_id")
    phone = re.sub(r"\D", "", str(data.get("phone", "")))

    if payment_type not in {"watch", "premium"}:
        return json_error("Invalid payment type. Use watch or premium.")

    if not re.fullmatch(r"[6-9]\d{9}", phone):
        return json_error("Enter a valid 10 digit Indian mobile number.")

    try:
        movie_id = int(movie_id)
    except Exception:
        return json_error("Invalid movie.")

    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, title FROM movies WHERE id = %s", (movie_id,))
        movie = cur.fetchone()
        cur.close()
    finally:
        conn.close()

    if not movie:
        return json_error("Movie not found.", 404)

    if payment_type == "watch":
        amount = WATCH_PRICE
        description = "CINEMA WORLD Watch Access"
    else:
        amount = PREMIUM_PRICE
        description = "CINEMA WORLD 30 Day Premium"

    customer_id = get_customer_id()
    order_id = "tm_" + payment_type + "_" + str(movie_id) + "_" + secrets.token_hex(8)
    return_url = url_for("cashfree_return", movie_id=movie_id, _external=True)

    payload = {
        "order_id": order_id,
        "order_amount": amount,
        "order_currency": "INR",
        "customer_details": {
            "customer_id": customer_id,
            "customer_phone": phone,
        },
        "order_meta": {"return_url": return_url},
        "order_note": description,
        "order_tags": {
            "movie_id": str(movie_id),
            "payment_type": payment_type,
        },
    }

    try:
        result = cashfree_request("POST", "/orders", payload)
        payment_session_id = result.get("payment_session_id")
        if not payment_session_id:
            return json_error("Cashfree did not return payment session.", 502, cashfree=result)

        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO payment_orders
                (order_id, customer_id, movie_id, payment_type, amount, status)
                VALUES(%s,%s,%s,%s,%s,%s)
                """,
                (order_id, customer_id, movie_id, payment_type, amount, "ACTIVE"),
            )
            conn.commit()
            cur.close()
        finally:
            conn.close()

        return json_ok(
            order_id=order_id,
            payment_session_id=payment_session_id,
            amount=amount,
            mode=CASHFREE_JS_MODE,
        )
    except Exception as exc:
        print("CREATE PAYMENT ERROR:", repr(exc))
        return json_error(str(exc), 500)

# ============================================================
# CASHFREE RETURN / VERIFY
# ============================================================

@app.route(
    "/payment/return"
)
def cashfree_return():

    order_id = (
        request.args.get(
            "order_id",
            "",
        ).strip()
    )

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

            return redirect(url_for("member_home"))

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
# HOME
# ============================================================

@app.route("/")
def home():
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()

    conn = get_db(dict_rows=True)

    try:
        cur = conn.cursor()

        if q:
            cur.execute(
                """
                SELECT *
                FROM movies
                WHERE
                    title ILIKE %s
                    OR category ILIKE %s
                    OR description ILIKE %s
                ORDER BY id DESC
                """,
                (
                    f"%{q}%",
                    f"%{q}%",
                    f"%{q}%",
                )
            )

        elif category:
            cur.execute(
                """
                SELECT *
                FROM movies
                WHERE category ILIKE %s
                ORDER BY id DESC
                """,
                (
                    f"%{category}%",
                )
            )

        else:
            cur.execute(
                """
                SELECT *
                FROM movies
                ORDER BY id DESC
                """
            )

        movies = cur.fetchall()
        cur.close()

    finally:
        conn.close()

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

    return render_template(
        "index.html",
        movies=movies,
        ads=get_ads(),
        q=q,
        category=category,
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
    "/movie/<int:movie_id>"
)
def movie_page(movie_id):

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

    return render_template(
        "movie.html",
        movie=movie,
        ads=get_ads(),
        access=access,
        cashfree_mode=CASHFREE_JS_MODE,
    )


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
    step = str(request.args.get("step", "")).strip().lower()

    if step not in {"mobile", "otp"}:
        step = "otp" if session.get("otp_mobile") else "mobile"

    mobile = normalize_mobile(
        request.args.get("mobile", "")
        or session.get("otp_mobile", "")
    )

    return render_template(
        "login.html",
        step=step,
        mobile=mobile,
        masked_mobile=mask_mobile(mobile),
    )


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if username == ADMIN_USER and password == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            return redirect(url_for("admin"))

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


# ============================================================
# CUSTOMER MEMBER / USER DETAILS / MY LIST
# ============================================================

def customer_login_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not session.get("customer_logged_in"):
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)
    return wrapper


@app.route("/user-details", methods=["GET", "POST"])
@customer_login_required
def user_details():
    customer_id = session.get("customer_id")
    if not customer_id:
        return redirect(url_for("login"))

    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        mobile = normalize_mobile(
            request.form.get("mobile")
            or session.get("customer_mobile", "")
        )
        if not full_name:
            flash("Please enter your full name.", "error")
            return redirect(url_for("user_details"))
        if not re.fullmatch(r"[6-9]\d{9}", mobile):
            flash("Please enter a valid 10-digit mobile number.", "error")
            return redirect(url_for("user_details"))

        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE customer_users
                SET full_name = %s, mobile = %s
                WHERE customer_id = %s
                """,
                (full_name, mobile, customer_id),
            )
            conn.commit()
            cur.close()
        finally:
            conn.close()

        session["customer_mobile"] = mobile
        if customer_has_completed_initial_payment(customer_id):
            return redirect(url_for("member_home"))
        return redirect(url_for("membership_checkout"))

    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT full_name, mobile, email FROM customer_users WHERE customer_id = %s LIMIT 1",
            (customer_id,),
        )
        user = cur.fetchone() or {}
        cur.close()
    finally:
        conn.close()

    return render_template(
        "user_details.html",
        full_name=user.get("full_name", ""),
        mobile=user.get("mobile", ""),
        customer_email=user.get("email") or session.get("customer_email", ""),
    )


def get_member_movies_data():
    customer_id = session.get("customer_id")
    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT m.*, CASE WHEN cml.movie_id IS NOT NULL THEN TRUE ELSE FALSE END AS in_my_list
            FROM movies m
            LEFT JOIN customer_movie_list cml
              ON cml.movie_id = m.id AND cml.customer_id = %s
            ORDER BY m.id DESC
            """,
            (customer_id,),
        )
        movies = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    for movie in movies:
        try:
            movie["poster_url"] = media_url(movie.get("poster")) if movie.get("poster") else None
        except Exception:
            movie["poster_url"] = None
        movie["in_my_list"] = bool(movie.get("in_my_list"))
        movie["views"] = int(movie.get("views") or 0)
    return movies


def customer_has_completed_initial_payment(customer_id=None):
    customer_id = customer_id or session.get("customer_id")
    if not customer_id:
        return False
    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT 1
            FROM customer_subscriptions
            WHERE customer_id = %s
              AND auth_status = 'SUCCESS'
              AND status IN ('ACTIVE', 'BANK_APPROVAL_PENDING')
            LIMIT 1
            """,
            (customer_id,),
        )
        return bool(cur.fetchone())
    finally:
        conn.close()


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
    if customer_has_completed_initial_payment(customer_id):
        return redirect(url_for("member_home"))
    try:
        subscription_id, session_id = create_cashfree_subscription(customer_id)
    except Exception as exc:
        print("MEMBERSHIP START ERROR:",repr(exc))
        return Response("<h2>Payment setup failed</h2><p>"+str(exc).replace("<","&lt;")+"</p><p>Please check Cashfree keys and try again.</p>",status=500)
    return Response("""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>CINEMA WORLD Membership</title><script src='https://sdk.cashfree.com/js/v3/cashfree.js'></script><style>body{margin:0;background:#08080d;color:#fff;font-family:Arial,sans-serif;min-height:100vh;display:grid;place-items:center}.card{width:min(560px,92vw);padding:34px;border:1px solid #292936;border-radius:24px;background:linear-gradient(145deg,#15151e,#0b0b10);box-shadow:0 25px 80px #000}.gold{color:#f5c451}.price{font-size:44px;font-weight:800;margin-top:25px}.muted{color:#aaa;line-height:1.6}.btn{width:100%;border:0;border-radius:14px;padding:16px;background:linear-gradient(90deg,#f5c451,#ffdf80);font-size:17px;font-weight:800;cursor:pointer;margin-top:24px}.status{margin-top:16px;color:#aaa}</style></head><body><main class='card'><div class='gold'>🎬 CINEMA WORLD</div><h1>Premium Membership</h1><p class='muted'>Complete the initial authorization to activate your Premium membership.</p><div class='price'>₹1</div><div class='muted'>Initial authorization • ₹99/month recurring membership</div><button id='pay' class='btn'>Continue with Cashfree</button><div id='status' class='status'></div></main><script>const cashfree=Cashfree({mode:"""+json.dumps(CASHFREE_JS_MODE)+"""});document.getElementById('pay').onclick=async()=>{const s=document.getElementById('status');s.textContent='Opening secure checkout…';try{const r=await cashfree.subscriptionsCheckout({subsSessionId:"""+json.dumps(session_id)+""",redirectTarget:'_self'});if(r&&r.error)s.textContent=r.error.message||'Checkout could not be opened.'}catch(e){s.textContent=e.message||'Checkout could not be opened.'}};</script></body></html>""")


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
        saved_movies=saved_movies,
        my_list_movies=saved_movies,
        saved_movie_ids=[int(m["id"]) for m in saved_movies],
        categories=categories,
        customer_email=session.get("customer_email", ""),
        premium=has_active_premium(),
    )

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
# Only added to prevent admin.html BuildError
# ============================================================

@app.route("/admin/users")
@admin_required
def admin_users():
    return redirect(url_for("admin"))


@app.route("/admin/payments")
@admin_required
def admin_payments():
    return redirect(url_for("admin"))


@app.route("/admin/subscriptions")
@admin_required
def admin_subscriptions():
    return redirect(url_for("admin"))


@app.route("/admin/watch-activity")
@admin_required
def admin_watch_activity():
    return redirect(url_for("admin"))


@app.route("/admin/analytics")
@admin_required
def admin_analytics():
    return redirect(url_for("admin"))


@app.route("/admin/live-activity")
@admin_required
def admin_live_activity():
    return redirect(url_for("admin"))


@app.route("/admin/notifications")
@admin_required
def admin_notifications():
    return redirect(url_for("admin"))


@app.route("/admin/reports")
@admin_required
def admin_reports():
    return redirect(url_for("admin"))


@app.route("/admin/r2")
@admin_required
def admin_r2():
    return redirect(url_for("admin"))


@app.route("/admin/system-health")
@admin_required
def admin_system_health():
    return redirect(url_for("admin"))


@app.route("/admin/security")
@admin_required
def admin_security():
    return redirect(url_for("admin"))


@app.route("/admin/settings")
@admin_required
def admin_settings():
    return redirect(url_for("admin"))


@app.route("/admin/search")
@admin_required
def admin_search():
    return redirect(url_for("admin"))
# ============================================================
# ADMIN
# ============================================================

@app.route("/admin")
@admin_required
def admin():

    conn = get_db(
        dict_rows=True
    )

    try:

        cur = conn.cursor()

        cur.execute(
            """
            SELECT
                COUNT(*) AS total_movies,
                COALESCE(SUM(views),0)
                AS total_views
            FROM movies
            """
        )

        stats = cur.fetchone()

        cur.execute(
            """
            SELECT *
            FROM movies
            ORDER BY id DESC
            """
        )

        movies = cur.fetchall()

        cur.close()

    finally:
        conn.close()

    for movie in movies:

        try:

            movie["poster_url"] = (
                media_url(
                    movie.get("poster")
                )
                if movie.get("poster")
                else None
            )

        except Exception:

            movie["poster_url"] = None

    return render_template(
        "admin.html",
        movies=movies,
        total_movies=(
            stats["total_movies"]
            if stats else 0
        ),
        total_views=(
            stats["total_views"]
            if stats else 0
        ),
        ads=get_ads(),
    )


# ============================================================
# ADMIN DASHBOARD MISSING ENDPOINTS
# ============================================================














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
        return redirect(
            url_for("admin")
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
# ADMIN CONTROL CENTER - MISSING ROUTES
# ============================================================













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
@admin_required
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
@admin_required
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
@admin_required
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
@admin_required
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
                    video
                )
                VALUES(%s,%s,%s,%s,%s)
                RETURNING id
                """,
                (
                    title,
                    category,
                    description,
                    poster_key,
                    video_key,
                ),
            )

            movie_id = cur.fetchone()[0]

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
# DATABASE INIT
# ============================================================

try:

    if DATABASE_URL:
        init_db()
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
    
