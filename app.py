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
# DOWNLOAD_PRICE removed
PREMIUM_PRICE = 99.00

PREMIUM_DAYS = 30



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
    os.environ.get("BREVO_FROM_NAME", "Tomesh Movies")
) or "Tomesh Movies"
BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"

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

        cur.execute("""
            CREATE TABLE IF NOT EXISTS customer_movie_list (
                customer_id TEXT NOT NULL,
                movie_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (customer_id, movie_id),
                FOREIGN KEY (movie_id)
                REFERENCES movies(id)
                ON DELETE CASCADE
            )
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
                url_for("login")
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
        "Tomesh Movies login verification\n\n"
        "Your one-time verification code is: "
        + otp_text
        + "\n\n"
        + "This OTP expires in "
        + str(OTP_EXPIRY_MINUTES)
        + " minutes.\n"
        + "Do not share this code with anyone.\n\n"
        + "If you did not request this code, you can ignore this email.\n\n"
        + "Tomesh Movies"
    )

    html_content = (
        "<!doctype html>"
        "<html><body style=\"margin:0;padding:24px;background:#080808;"
        "font-family:Arial,sans-serif;color:#ffffff;\">"
        "<div style=\"max-width:520px;margin:auto;background:#121212;"
        "border:1px solid #2b2b2b;border-radius:16px;padding:28px;\">"
        "<h2 style=\"margin:0 0 12px;color:#ffc400;\">Tomesh Movies</h2>"
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
        "subject": "Tomesh Movies - Your Login OTP",
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
            "user-agent": "Tomesh-Movies/1.0",
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

    email = normalize_email(
        session.get("otp_email", "")
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
        "Email verified. Welcome to Tomesh Movies!",
        "success",
    )

    return redirect(
        url_for("member_home")
    )


# ============================================================
# MEMBER HOME + MY LIST
# ============================================================

def customer_login_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not session.get("customer_logged_in"):
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)
    return wrapper


def get_member_movies_data():
    customer_id = session.get("customer_id")

    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()

        if customer_id:
            cur.execute(
                """
                SELECT
                    m.*,
                    CASE
                        WHEN cml.movie_id IS NOT NULL THEN TRUE
                        ELSE FALSE
                    END AS in_my_list
                FROM movies m
                LEFT JOIN customer_movie_list cml
                    ON cml.movie_id = m.id
                   AND cml.customer_id = %s
                ORDER BY m.id DESC
                """,
                (customer_id,),
            )
        else:
            cur.execute(
                """
                SELECT
                    m.*,
                    FALSE AS in_my_list
                FROM movies m
                ORDER BY m.id DESC
                """
            )

        movies = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    for movie in movies:
        try:
            movie["poster_url"] = (
                media_url(movie.get("poster"))
                if movie.get("poster")
                else None
            )
        except Exception:
            movie["poster_url"] = None

        movie["in_my_list"] = bool(
            movie.get("in_my_list")
        )
        movie["views"] = int(
            movie.get("views") or 0
        )

    return movies
@app.route("/user-details", methods=["GET", "POST"])
@customer_login_required
def user_details():
    customer_id = session.get("customer_id")

    if not customer_id:
        return redirect(url_for("login"))

    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        mobile = re.sub(
            r"\D",
            "",
            (request.form.get("mobile") or "").strip(),
        )

        if not full_name:
            flash("Please enter your full name.", "error")
            return render_template(
                "user_details.html",
                full_name=full_name,
                mobile=mobile,
                customer_email=session.get("customer_email", ""),
            )

        if not re.fullmatch(r"[6-9]\d{9}", mobile):
            flash(
                "Please enter a valid 10-digit mobile number.",
                "error",
            )
            return render_template(
                "user_details.html",
                full_name=full_name,
                mobile=mobile,
                customer_email=session.get("customer_email", ""),
            )

        conn = get_db_connection()

        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE customer_users
                    SET full_name = %s,
                        mobile = %s
                    WHERE customer_id = %s
                    """,
                    (
                        full_name,
                        mobile,
                        customer_id,
                    ),
                )

            conn.commit()

        finally:
            conn.close()

        return redirect(
            url_for("member_home")
        )

    conn = get_db_connection()

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT full_name, mobile, email
                FROM customer_users
                WHERE customer_id = %s
                LIMIT 1
                """,
                (customer_id,),
            )
            user = cur.fetchone()

    finally:
        conn.close()

    return render_template(
        "user_details.html",
        full_name=(user or {}).get("full_name", ""),
        mobile=(user or {}).get("mobile", ""),
        customer_email=(
            (user or {}).get("email")
            or session.get("customer_email", "")
        ),
    )


@app.route("/member")
@customer_login_required
def member_home():
    movies = get_member_movies_data()
    saved_movies = [
        movie for movie in movies
        if movie.get("in_my_list")
    ]

    categories = []
    seen_categories = set()

    for movie in movies:
        raw_category = str(
            movie.get("category") or ""
        ).strip()

        if not raw_category:
            continue

        for part in raw_category.split(","):
            category = part.strip()

            if category and category.lower() not in seen_categories:
                seen_categories.add(category.lower())
                categories.append(category)

    return render_template(
        "member_home.html",
        movies=movies,
        saved_movies=saved_movies,
        my_list_movies=saved_movies,
        saved_movie_ids=[
            int(movie["id"])
            for movie in saved_movies
        ],
        categories=categories,
        customer_email=session.get("customer_email", ""),
        premium=has_active_premium(),
    )
@customer_login_required
def member_home():
    movies = get_member_movies_data()
    saved_movies = [
        movie for movie in movies
        if movie.get("in_my_list")
    ]

    categories = []
    seen_categories = set()
    for movie in movies:
        raw_category = str(
            movie.get("category") or ""
        ).strip()
        if not raw_category:
            continue
        for part in raw_category.split(","):
            category = part.strip()
            if category and category.lower() not in seen_categories:
                seen_categories.add(category.lower())
                categories.append(category)

    return render_template(
        "member_home.html",
        movies=movies,
        saved_movies=saved_movies,
        my_list_movies=saved_movies,
        saved_movie_ids=[
            int(movie["id"])
            for movie in saved_movies
        ],
        categories=categories,
        customer_email=session.get("customer_email", ""),
        premium=has_active_premium(),
    )


@app.route(
    "/api/my-list/<int:movie_id>",
    methods=["POST", "DELETE"],
)
@customer_login_required
def api_my_list(movie_id):
    customer_id = session.get("customer_id")

    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()

        cur.execute(
            "SELECT id FROM movies WHERE id = %s",
            (movie_id,),
        )
        movie = cur.fetchone()

        if not movie:
            cur.close()
            return json_error("Movie not found.", 404)

        if request.method == "POST":
            cur.execute(
                """
                INSERT INTO customer_movie_list
                (customer_id, movie_id)
                VALUES(%s, %s)
                ON CONFLICT(customer_id, movie_id)
                DO NOTHING
                """,
                (customer_id, movie_id),
            )
            conn.commit()
            cur.close()
            return json_ok(
                saved=True,
                movie_id=movie_id,
                message="Added to My List.",
            )

        cur.execute(
            """
            DELETE FROM customer_movie_list
            WHERE customer_id = %s
              AND movie_id = %s
            """,
            (customer_id, movie_id),
        )
        removed = cur.rowcount > 0
        conn.commit()
        cur.close()

        return json_ok(
            saved=False,
            removed=removed,
            movie_id=movie_id,
            message="Removed from My List.",
        )

    except Exception as exc:
        conn.rollback()
        print("MY LIST ERROR:", repr(exc))
        return json_error(
            "My List update failed: " + str(exc),
            500,
        )
    finally:
        conn.close()


@app.route("/api/my-list", methods=["GET"])
@customer_login_required
def api_my_list_all():
    customer_id = session.get("customer_id")

    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT m.*
            FROM movies m
            INNER JOIN customer_movie_list cml
                ON cml.movie_id = m.id
            WHERE cml.customer_id = %s
            ORDER BY cml.created_at DESC, m.id DESC
            """,
            (customer_id,),
        )
        movies = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    for movie in movies:
        try:
            movie["poster_url"] = (
                media_url(movie.get("poster"))
                if movie.get("poster")
                else None
            )
        except Exception:
            movie["poster_url"] = None

    return json_ok(
        movies=movies,
        movie_ids=[int(movie["id"]) for movie in movies],
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

    customer_id = session.get("customer_id")

    if not customer_id:
        return {
            "watch": False,
            "download": False,
            "premium": False,
        }

    conn = get_db(dict_rows=True)

    try:
        cur = conn.cursor()

        # Account-wide access:
        # movie_id IS NULL
        #
        # Permanent ₹1 Watch:
        # watch_until IS NULL
        #
        # Premium:
        # premium_until > NOW()

        cur.execute(
            """
            SELECT
                watch_until,
                download_until,
                premium_until
            FROM customer_access
            WHERE customer_id = %s
              AND movie_id IS NULL
            ORDER BY id DESC
            """,
            (customer_id,),
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

        # Permanent ₹1 Watch access
        if (
            row["watch_until"] is None
            and row["premium_until"] is None
        ):
            permanent_watch = True

        # Legacy/temporary watch access
        if (
            row["watch_until"] is not None
            and row["watch_until"] > now
        ):
            temporary_watch = True

        # Active Premium
        if (
            row["premium_until"] is not None
            and row["premium_until"] > now
        ):
            premium = True

    watch = (
        permanent_watch
        or temporary_watch
        or premium
    )

    # Download is ONLY Premium.
    download = premium

    return {
        "watch": watch,
        "download": download,
        "premium": premium,
    }

def grant_access(
    customer_id,
    movie_id,
    payment_type,
):

    conn = get_db()

    try:
        cur = conn.cursor()
        now = datetime.now()

        if payment_type == "watch":

            # ₹1 Watch = permanent account-wide access.
            # movie_id is intentionally NULL.

            cur.execute(
                """
                SELECT id
                FROM customer_access
                WHERE customer_id = %s
                  AND movie_id IS NULL
                  AND watch_until IS NULL
                  AND premium_until IS NULL
                LIMIT 1
                """,
                (customer_id,),
            )

            existing = cur.fetchone()

            if not existing:
                cur.execute(
                    """
                    INSERT INTO customer_access
                    (
                        customer_id,
                        movie_id,
                        watch_until,
                        download_until,
                        premium_until
                    )
                    VALUES(%s, NULL, NULL, NULL, NULL)
                    """,
                    (customer_id,),
                )

        elif payment_type == "premium":

            # ₹99 Premium = 30 days.
            # Account-wide access.

            cur.execute(
                """
                SELECT MAX(premium_until)
                FROM customer_access
                WHERE customer_id = %s
                  AND movie_id IS NULL
                """,
                (customer_id,),
            )

            row = cur.fetchone()

            current_until = (
                row[0]
                if row and row[0]
                else None
            )

            if (
                current_until
                and current_until > now
            ):
                base_time = current_until
            else:
                base_time = now

            until = (
                base_time
                + timedelta(days=PREMIUM_DAYS)
            )

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
                (
                    customer_id,
                    movie_id,
                    watch_until,
                    download_until,
                    premium_until
                )
                VALUES(%s, NULL, NULL, NULL, %s)
                """,
                (
                    customer_id,
                    until,
                ),
            )

        else:

            raise ValueError(
                "Unsupported payment type: "
                + str(payment_type)
            )

        conn.commit()
        cur.close()

    except Exception:
        conn.rollback()
        raise

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

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    movie_id = data.get("movie_id")

    payment_type = str(
        data.get(
            "payment_type",
            "",
        )
    ).strip().lower()

    phone = re.sub(
        r"\D",
        "",
        str(
            data.get(
                "phone",
                "",
            )
        ),
    )

    try:
        movie_id = int(movie_id)
    except Exception:
        return json_error(
            "Invalid movie."
        )

    if payment_type not in {
        "watch",
        "premium",
    }:
        return json_error(
            "Invalid payment type."
        )

    if not re.fullmatch(
        r"[6-9]\d{9}",
        phone,
    ):
        return json_error(
            "Enter a valid 10 digit Indian mobile number."
        )

    conn = get_db(
        dict_rows=True
    )

    try:

        cur = conn.cursor()

        cur.execute(
            """
            SELECT id, title
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
        return json_error(
            "Movie not found.",
            404,
        )

    if payment_type == "watch":
        
        amount = WATCH_PRICE

        description = (
            "Watch - "
            + movie["title"]
        )

    elif payment_type == "premium":

        amount = PREMIUM_PRICE

        description = (
            "Tomesh Movies Premium - 30 Days"
        )

    customer_id = get_customer_id()

    order_id = (
        "tm_"
        + payment_type
        + "_"
        + str(movie_id)
        + "_"
        + secrets.token_hex(8)
    )

    return_url = url_for(
        "cashfree_return",
        movie_id=movie_id,
        _external=True,
    )

    payload = {
        "order_id": order_id,
        "order_amount": amount,
        "order_currency": "INR",

        "customer_details": {
            "customer_id": customer_id,
            "customer_phone": phone,
        },

        "order_meta": {
            "return_url": return_url,
        },

        "order_note": description,

        "order_tags": {
            "movie_id": str(movie_id),
            "payment_type": payment_type,
        },
    }

    try:

        result = cashfree_request(
            "POST",
            "/orders",
            payload,
        )

        payment_session_id = result.get(
            "payment_session_id"
        )

        if not payment_session_id:

            return json_error(
                "Cashfree did not return payment session.",
                502,
                cashfree=result,
            )

        conn = get_db()

        try:

            cur = conn.cursor()

            cur.execute(
                """
                INSERT INTO payment_orders
                (
                    order_id,
                    customer_id,
                    movie_id,
                    payment_type,
                    amount,
                    status
                )
                VALUES(%s,%s,%s,%s,%s,%s)
                """,
                (
                    order_id,
                    customer_id,
                    movie_id,
                    payment_type,
                    amount,
                    "ACTIVE",
                ),
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

        print(
            "CREATE PAYMENT ERROR:",
            repr(exc),
        )

        return json_error(
            str(exc),
            500,
        )

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

            session[
                "payment_success"
            ] = True

            flash(
    "Payment successful. Access activated.",
    "success",
)

if local_order["payment_type"] == "watch":
    return redirect(
    url_for("user_details")
)

return redirect(
    url_for(
        "movie_page",
        movie_id=local_order["movie_id"],
    )
)

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

        # FAST PLAYBACK: browser fetches the video directly from R2
        # using a time-limited presigned URL.
        movie["video_url"] = r2_presigned_url(
            video_key,
            expires=PRESIGNED_EXPIRES,
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
    methods=["GET", "POST"],
)
def login():

    if request.method == "POST":

        login_type = str(
            request.form.get(
                "login_type",
                "admin",
            )
        ).strip().lower()

        if login_type == "customer":
            return redirect(
                url_for(
                    "request_otp"
                )
            )

        username = request.form.get(
            "username",
            "",
        ).strip()

        password = request.form.get(
            "password",
            "",
        )

        if (
            username == ADMIN_USER
            and password == ADMIN_PASSWORD
        ):

            session[
                "admin_logged_in"
            ] = True

            return redirect(
                url_for("admin")
            )

        flash(
            "Invalid username or password.",
            "error",
        )

    step = str(
        request.args.get(
            "step",
            "",
        )
    ).strip().lower()

    if step not in {
        "email",
        "otp",
    }:
        step = (
            "otp"
            if session.get("otp_email")
            else "email"
        )

    email = normalize_email(
        request.args.get(
            "email",
            "",
        )
        or session.get(
            "otp_email",
            "",
        )
    )

    return render_template(
        "login.html",
        step=step,
        email=email,
        masked_email=mask_email(email),
    )


# LOGOUT
# ============================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("login")
    )


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
# ADMIN CONTROL CENTER MODULES
# ============================================================

def _admin_module_page(title, subtitle, rows=None, search_box=False):
    rows = rows or []
    html_rows = ""
    for row in rows:
        html_rows += "<tr>" + "".join("<td>" + ("" if v is None else str(v)) + "</td>" for v in row) + "</tr>"
    if rows:
        table = '<div class="table-wrap"><table><tbody>' + html_rows + '</tbody></table></div>'
    else:
        table = '<div class="empty">No records available yet.</div>'
    search = ''
    if search_box:
        search = '<form class="search" method="get"><input name="q" value="" placeholder="Search movies, email or transaction ID"><button>Search</button></form>'
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>' + title + ' | Tomesh Movies</title>'
        '<style>*{box-sizing:border-box}body{margin:0;background:#07070b;color:#fff;font-family:Arial,sans-serif}'
        '.wrap{max-width:1200px;margin:auto;padding:28px 18px 70px}.top{display:flex;justify-content:space-between;align-items:center;gap:15px;flex-wrap:wrap;margin-bottom:22px}'
        '.brand{font-size:24px;font-weight:900}.brand span{color:#c084fc}.back{display:inline-block;padding:11px 16px;border-radius:11px;text-decoration:none;color:#fff;background:#17171f;border:1px solid #333;font-weight:800}'
        '.card{background:linear-gradient(145deg,#121218,#0c0c11);border:1px solid #292932;border-radius:18px;padding:22px;margin-bottom:18px;box-shadow:0 15px 50px rgba(0,0,0,.3)}'
        'h1{margin:0 0 8px;font-size:30px}.sub{color:#aaa;line-height:1.6}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse}'
        'td{padding:12px;border-bottom:1px solid #25252c;color:#ddd}tr:first-child td{font-weight:800;color:#fff;background:#15151c}'
        '.empty{padding:35px;text-align:center;color:#999;border:1px dashed #3a3a44;border-radius:14px}.search{display:flex;gap:10px;margin-bottom:18px}'
        '.search input{flex:1;background:#09090d;border:1px solid #34343d;color:#fff;padding:13px;border-radius:10px}'
        'button{border:0;border-radius:10px;padding:13px 18px;background:linear-gradient(135deg,#7c3aed,#db2777);color:#fff;font-weight:900;cursor:pointer}'
         '@media(max-width:650px){h1{font-size:24px}.search{flex-direction:column}}</style></head><body>'
        '<main class="wrap"><div class="top"><div class="brand">TOMESH <span>MOVIES</span></div>'
        '<a class="back" href="/admin">← Admin Dashboard</a></div>'
        '<section class="card"><h1>' + title + '</h1><div class="sub">' + subtitle + '</div></section>'
        + search + '<section class="card">' + table + '</section></main></body></html>'
    )


@app.route("/admin/users")
@admin_required
def admin_users():
    conn=get_db(dict_rows=True)
    try:
        cur=conn.cursor(); cur.execute("SELECT id,email,customer_id,created_at,last_login_at FROM customer_users ORDER BY id DESC"); rows=cur.fetchall(); cur.close()
    finally: conn.close()
    data=[("ID","Email","Customer ID","Created","Last Login")]+[tuple(r.get(k) for k in ("id","email","customer_id","created_at","last_login_at")) for r in rows]
    return _admin_module_page("👥 User Center","Customer accounts and login activity.",data)

@app.route("/admin/payments")
@admin_required
def admin_payments():
    conn=get_db(dict_rows=True)
    try:
        cur=conn.cursor(); cur.execute("SELECT order_id,customer_id,movie_id,payment_type,amount,status,created_at,paid_at FROM payment_orders ORDER BY id DESC"); rows=cur.fetchall(); cur.close()
    finally: conn.close()
    data=[("Order ID","Customer","Movie","Type","Amount","Status","Created","Paid")]+[tuple(r.get(k) for k in ("order_id","customer_id","movie_id","payment_type","amount","status","created_at","paid_at")) for r in rows]
    return _admin_module_page("💳 Payments","Cashfree payment orders and statuses.",data)

@app.route("/admin/subscriptions")
@admin_required
def admin_subscriptions():
    conn=get_db(dict_rows=True)
    try:
        cur=conn.cursor(); cur.execute("SELECT customer_id,movie_id,watch_until,download_until,premium_until,created_at FROM customer_access ORDER BY id DESC"); rows=cur.fetchall(); cur.close()
    finally: conn.close()
    data=[("Customer","Movie","Watch Until","Download Until","Premium Until","Created")]+[tuple(r.get(k) for k in ("customer_id","movie_id","watch_until","download_until","premium_until","created_at")) for r in rows]
    return _admin_module_page("🔄 AutoPay & Subscriptions","Customer premium/access records.",data)

@app.route("/admin/watch-activity")
@admin_required
def admin_watch_activity():
    conn=get_db(dict_rows=True)
    try:
        cur=conn.cursor(); cur.execute("SELECT id,title,category,views,created_at FROM movies ORDER BY views DESC,id DESC"); rows=cur.fetchall(); cur.close()
    finally: conn.close()
    data=[("Movie ID","Movie","Category","Views","Created")]+[tuple(r.get(k) for k in ("id","title","category","views","created_at")) for r in rows]
    return _admin_module_page("▶️ Watch Activity","Current movie view activity.",data)

@app.route("/admin/analytics")
@admin_required
def admin_analytics():
    conn=get_db(dict_rows=True)
    try:
        cur=conn.cursor(); cur.execute("SELECT COUNT(*) AS c,COALESCE(SUM(views),0) AS v FROM movies"); m=cur.fetchone(); cur.execute("SELECT COUNT(*) AS c FROM customer_users"); u=cur.fetchone(); cur.execute("SELECT COUNT(*) AS c,COALESCE(SUM(amount),0) AS r FROM payment_orders WHERE UPPER(status)='PAID'"); pay=cur.fetchone(); cur.close()
    finally: conn.close()
    data=[("Metric","Value"),("Total Movies",m["c"]),("Total Views",m["v"]),("Registered Customers",u["c"]),("Paid Orders",pay["c"]),("Revenue","₹"+str(pay["r"]))]
    return _admin_module_page("📈 Analytics","Current database totals and movie performance.",data)

@app.route("/admin/live-activity")
@admin_required
def admin_live_activity():
    conn=get_db(dict_rows=True)
    try:
        cur=conn.cursor(); cur.execute("SELECT email,created_at,last_login_at FROM customer_users ORDER BY COALESCE(last_login_at,created_at) DESC LIMIT 25"); users=cur.fetchall(); cur.execute("SELECT order_id,payment_type,status,created_at FROM payment_orders ORDER BY id DESC LIMIT 25"); pays=cur.fetchall(); cur.close()
    finally: conn.close()
    data=[("Customer","Created","Last Login")]+[tuple(r.get(k) for k in ("email","created_at","last_login_at")) for r in users]
    data += [("Payment: "+str(r.get("order_id")),r.get("payment_type"),str(r.get("status"))+" · "+str(r.get("created_at"))) for r in pays]
    return _admin_module_page("🔴 Live Activity","Recent customer and payment events.",data)

@app.route("/admin/notifications")
@admin_required
def admin_notifications():
    return _admin_module_page("🔔 Notifications","Notification center is ready. No notification records are stored yet.")

@app.route("/admin/reports")
@admin_required
def admin_reports():
    conn=get_db(dict_rows=True)
    try:
        cur=conn.cursor(); cur.execute("SELECT COUNT(*) AS c,COALESCE(SUM(views),0) AS v FROM movies"); m=cur.fetchone(); cur.execute("SELECT COUNT(*) AS c,COALESCE(SUM(amount),0) AS r FROM payment_orders WHERE UPPER(status)='PAID'"); pay=cur.fetchone(); cur.execute("SELECT COUNT(*) AS c FROM customer_users"); u=cur.fetchone(); cur.close()
    finally: conn.close()
    data=[("Report","Value"),("Users",u["c"]),("Movies",m["c"]),("Views",m["v"]),("Paid Orders",pay["c"]),("Revenue","₹"+str(pay["r"]))]
    return _admin_module_page("📑 Reports","Summary report from PostgreSQL.",data)

@app.route("/admin/security")
@admin_required
def admin_security():
    data=[("Setting","Status"),("Admin authentication","Enabled"),("Session protection","Enabled"),("R2 upload API","Admin protected"),("Payment API","Customer endpoint"),("Database","PostgreSQL")]
    return _admin_module_page("🔐 Security","Current application security configuration.",data)

@app.route("/admin/settings")
@admin_required
def admin_settings():
    data=[("Setting","Current"),("Max video upload","4 GB"),("Video formats","MP4, MKV, WebM, MOV"),("Poster formats","JPG, JPEG, PNG, WEBP"),("Storage","Cloudflare R2"),("Database","PostgreSQL"),("Payment","Cashfree")]
    return _admin_module_page("🛠️ Website Settings","Application configuration overview. Secrets remain hidden.",data)

@app.route("/admin/search")
@admin_required
def admin_search():
    q=request.args.get("q","",type=str).strip()
    if not q:
        return _admin_module_page("🔎 Global Search","Search movies, customer emails and transaction IDs.",search_box=True)
    conn=get_db(dict_rows=True)
    try:
        cur=conn.cursor(); like="%"+q+"%"; cur.execute("SELECT id,title,category,views FROM movies WHERE title ILIKE %s OR category ILIKE %s ORDER BY id DESC LIMIT 50",(like,like)); movies=cur.fetchall(); cur.execute("SELECT email,customer_id FROM customer_users WHERE email ILIKE %s OR customer_id ILIKE %s LIMIT 50",(like,like)); users=cur.fetchall(); cur.execute("SELECT order_id,customer_id,status,amount FROM payment_orders WHERE order_id ILIKE %s OR customer_id ILIKE %s LIMIT 50",(like,like)); pays=cur.fetchall(); cur.close()
    finally: conn.close()
    data=[("Type","Result","Details","Value")]
    data += [("Movie",r.get("title"),r.get("category"),r.get("views")) for r in movies]
    data += [("User",r.get("email"),r.get("customer_id"),"") for r in users]
    data += [("Payment",r.get("order_id"),r.get("status"),r.get("amount")) for r in pays]
    return _admin_module_page("🔎 Global Search","Results for: "+q,data,search_box=True)

@app.route("/admin/r2")
@admin_required
def admin_r2():
    try:
        client=get_r2_client(); resp=client.list_objects_v2(Bucket=R2_BUCKET,MaxKeys=1000); objs=resp.get("Contents",[]); rows=[("Key","Size","Last Modified")]+[(o.get("Key"),o.get("Size"),o.get("LastModified")) for o in objs]; subtitle="Bucket: "+R2_BUCKET+" · Showing up to 1000 objects."
    except Exception as exc:
        rows=[]; subtitle="R2 error: "+str(exc)
    return _admin_module_page("☁️ R2 Storage",subtitle,rows)

@app.route("/admin/system-health")
@admin_required
def admin_system_health():
    db_status="OK"
    try:
        conn=get_db(); cur=conn.cursor(); cur.execute("SELECT 1"); cur.fetchone(); cur.close(); conn.close()
    except Exception as exc: db_status="ERROR: "+str(exc)
    try:
        get_r2_client().list_objects_v2(Bucket=R2_BUCKET,MaxKeys=1); r2_status="OK"
    except Exception as exc: r2_status="ERROR: "+str(exc)
    data=[("Component","Status"),("Flask application","OK"),("PostgreSQL",db_status),("Cloudflare R2",r2_status)]
    return _admin_module_page("⚙️ System Health","Application health checks.",data)


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
        return render_template("admin_add.html")

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

        url_map = dict(urls)
        return json_ok(
            urls=urls,
            url_map=url_map,
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
        "app": "Tomesh Movies",
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
            <title>404 | Tomesh Movies</title>
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
            <title>500 | Tomesh Movies</title>
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
