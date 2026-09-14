import os
import uuid
import mimetypes
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import quote

import boto3
import psycopg2
import requests

from botocore.client import Config
from psycopg2.extras import RealDictCursor

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


# =========================================================
# APP
# =========================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "change-this-secret-key"
)

app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024


# =========================================================
# ADMIN
# =========================================================

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get(
    "ADMIN_PASSWORD",
    "change-me-now"
)


# =========================================================
# CASHFREE
# =========================================================

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


CASHFREE_APP_ID = clean_env_value(
    os.environ.get("CASHFREE_APP_ID", "")
)

CASHFREE_SECRET_KEY = clean_env_value(
    os.environ.get("CASHFREE_SECRET_KEY", "")
)

CASHFREE_ENV = clean_env_value(
    os.environ.get("CASHFREE_ENV", "sandbox")
).lower()

# CURRENT CASHFREE API VERSION
CASHFREE_API_VERSION = "2026-01-01"


if CASHFREE_ENV == "production":
    CASHFREE_API_URL = "https://api.cashfree.com/pg"
    CASHFREE_JS_MODE = "production"
else:
    CASHFREE_API_URL = "https://sandbox.cashfree.com/pg"
    CASHFREE_JS_MODE = "sandbox"


WATCH_PRICE = 1.00
DOWNLOAD_PRICE = 9.00
PREMIUM_PRICE = 109.00

WATCH_HOURS = 24
DOWNLOAD_DAYS = 30
PREMIUM_DAYS = 365


# =========================================================
# R2
# =========================================================

R2_ACCOUNT_ID = clean_env_value(
    os.environ.get("R2_ACCOUNT_ID", "")
)

R2_ACCESS_KEY_ID = clean_env_value(
    os.environ.get("R2_ACCESS_KEY_ID", "")
)

R2_SECRET_ACCESS_KEY = clean_env_value(
    os.environ.get("R2_SECRET_ACCESS_KEY", "")
)

R2_BUCKET = clean_env_value(
    os.environ.get("R2_BUCKET", "tomesh-movies")
)

R2_ENDPOINT = clean_env_value(
    os.environ.get("R2_ENDPOINT", "")
)

R2_PUBLIC_URL = clean_env_value(
    os.environ.get("R2_PUBLIC_URL", "")
).rstrip("/")


# =========================================================
# DATABASE
# =========================================================

DATABASE_URL = clean_env_value(
    os.environ.get("DATABASE_URL", "")
)


# =========================================================
# R2 CLIENT
# =========================================================

r2 = None

if (
    R2_ACCOUNT_ID
    and R2_ACCESS_KEY_ID
    and R2_SECRET_ACCESS_KEY
):
    try:
        if not R2_ENDPOINT:
            R2_ENDPOINT = (
                f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
            )

        r2 = boto3.client(
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
            ),
        )
    except Exception as e:
        print("R2 CLIENT ERROR:", repr(e))
        r2 = None


# =========================================================
# DATABASE HELPERS
# =========================================================

def get_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing.")

    return psycopg2.connect(
        DATABASE_URL,
        sslmode="require"
    )


def init_db():
    if not DATABASE_URL:
        print("DATABASE_URL missing. Database init skipped.")
        return

    conn = None

    try:
        conn = get_db()

        with conn.cursor() as cur:

            cur.execute("""
                CREATE TABLE IF NOT EXISTS movies (
                    id SERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    category TEXT DEFAULT '',
                    description TEXT DEFAULT '',
                    poster TEXT DEFAULT '',
                    video TEXT DEFAULT '',
                    views INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT DEFAULT ''
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS payment_orders (
                    id SERIAL PRIMARY KEY,
                    order_id TEXT UNIQUE NOT NULL,
                    customer_id TEXT NOT NULL,
                    movie_id INTEGER,
                    payment_type TEXT NOT NULL,
                    amount NUMERIC(10,2) NOT NULL,
                    status TEXT DEFAULT 'CREATED',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    paid_at TIMESTAMP
                )
            """)

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

            conn.commit()

        print("DATABASE INITIALIZED")

    except Exception as e:
        print("DATABASE INIT ERROR:", repr(e))

        if conn:
            conn.rollback()

    finally:
        if conn:
            conn.close()


# =========================================================
# INIT DATABASE
# =========================================================

try:
    init_db()
except Exception as e:
    print("INIT ERROR:", repr(e))


# =========================================================
# BASIC HELPERS
# =========================================================

VIDEO_EXTENSIONS = {
    "mp4",
    "mkv",
    "webm",
    "mov",
}

POSTER_EXTENSIONS = {
    "jpg",
    "jpeg",
    "png",
    "webp",
}


def allowed_video(filename):
    if not filename:
        return False

    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower()
        in VIDEO_EXTENSIONS
    )


def allowed_poster(filename):
    if not filename:
        return False

    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower()
        in POSTER_EXTENSIONS
    )


def get_extension(filename):
    if not filename or "." not in filename:
        return ""

    return filename.rsplit(".", 1)[1].lower()


def customer_id():
    if "customer_id" not in session:
        session["customer_id"] = (
            "tm_" + secrets.token_hex(12)
        )

    return session["customer_id"]


def now_utc():
    return datetime.now(timezone.utc).replace(
        tzinfo=None
    )


# =========================================================
# ADMIN LOGIN
# =========================================================

def admin_required(func):

    @wraps(func)
    def wrapper(*args, **kwargs):

        if not session.get("admin_logged_in"):
            return redirect(url_for("login"))

        return func(*args, **kwargs)

    return wrapper


# =========================================================
# CASHFREE REQUEST
# =========================================================

def cashfree_request(
    method,
    path,
    payload=None
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
        "x-request-id": str(uuid.uuid4()),
    }

    # Cashfree recommends an idempotency key for create-order
    if method.upper() == "POST":
        headers["x-idempotency-key"] = str(
            uuid.uuid4()
        )

    print(
        "CASHFREE REQUEST:",
        method.upper(),
        url
    )

    try:

        response = requests.request(
            method=method.upper(),
            url=url,
            headers=headers,
            json=payload,
            timeout=30,
        )

    except requests.RequestException as e:

        print(
            "CASHFREE NETWORK ERROR:",
            repr(e)
        )

        raise RuntimeError(
            "Cashfree connection failed."
        )

    print(
        "CASHFREE STATUS:",
        response.status_code
    )

    try:
        data = response.json()
    except Exception:
        data = {
            "message": response.text
        }

    if not response.ok:

        print(
            "CASHFREE ERROR:",
            response.status_code,
            data
        )

        message = data.get(
            "message",
            "Cashfree API request failed."
        )

        raise RuntimeError(
            f"Cashfree API {response.status_code}: "
            f"{data}"
        )

    return data


# =========================================================
# CASHFREE CREATE ORDER
# =========================================================

def create_cashfree_order(
    amount,
    payment_type,
    movie_id=None
):

    cid = customer_id()

    # Cashfree customer phone should be valid-looking.
    phone = "9999999999"

    order_id = (
        "tm_"
        + uuid.uuid4().hex[:24]
    )

    description = {
        "watch": "Tomesh Movies - 24 Hour Watch Access",
        "download": "Tomesh Movies - Movie Download Access",
        "premium": "Tomesh Movies - Premium 1 Year",
    }.get(
        payment_type,
        "Tomesh Movies Payment"
    )

    return_url = url_for(
        "payment_return",
        order_id=order_id,
        _external=True
    )

    payload = {
        "order_id": order_id,
        "order_amount": float(amount),
        "order_currency": "INR",

        "customer_details": {
            "customer_id": cid,
            "customer_phone": phone,
        },

        "order_meta": {
            "return_url": return_url,
        },

        "order_note": description,

        "order_tags": {
            "movie_id": str(movie_id or ""),
            "payment_type": payment_type,
        },
    }

    result = cashfree_request(
        "POST",
        "/orders",
        payload
    )

    payment_session_id = result.get(
        "payment_session_id"
    )

    if not payment_session_id:
        raise RuntimeError(
            "Cashfree did not return payment_session_id."
        )

    conn = None

    try:
        conn = get_db()

        with conn.cursor() as cur:
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
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                (
                    order_id,
                    cid,
                    movie_id,
                    payment_type,
                    float(amount),
                    "CREATED",
                )
            )

            conn.commit()

    except Exception:
        if conn:
            conn.rollback()
        raise

    finally:
        if conn:
            conn.close()

    return {
        "order_id": order_id,
        "payment_session_id": payment_session_id,
        "amount": float(amount),
    }


# =========================================================
# CASHFREE VERIFY ORDER
# =========================================================

def verify_cashfree_order(order_id):

    result = cashfree_request(
        "GET",
        "/orders/" + quote(
            order_id,
            safe=""
        )
    )

    return result


# =========================================================
# APPLY PAID ACCESS
# =========================================================

def apply_paid_access(order_id):

    conn = None

    try:

        conn = get_db()

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT *
                FROM payment_orders
                WHERE order_id = %s
                LIMIT 1
                """,
                (order_id,)
            )

            payment = cur.fetchone()

            if not payment:
                raise RuntimeError(
                    "Payment order not found."
                )

            if payment["status"] == "PAID":
                return payment

            cf_order = verify_cashfree_order(
                order_id
            )

            status = str(
                cf_order.get(
                    "order_status",
                    ""
                )
            ).upper()

            print(
                "CASHFREE ORDER STATUS:",
                status
            )

            if status != "PAID":
                cur.execute(
                    """
                    UPDATE payment_orders
                    SET status = %s
                    WHERE order_id = %s
                    """,
                    (
                        status or "UNKNOWN",
                        order_id,
                    )
                )

                conn.commit()

                return {
                    "paid": False,
                    "status": status,
                }

            cur.execute(
                """
                UPDATE payment_orders
                SET
                    status = 'PAID',
                    paid_at = CURRENT_TIMESTAMP
                WHERE order_id = %s
                """,
                (order_id,)
            )

            customer = payment["customer_id"]
            movie_id = payment["movie_id"]
            payment_type = payment["payment_type"]

            current = now_utc()

            watch_until = None
            download_until = None
            premium_until = None

            if payment_type == "watch":
                watch_until = current + timedelta(
                    hours=WATCH_HOURS
                )

            elif payment_type == "download":
                download_until = current + timedelta(
                    days=DOWNLOAD_DAYS
                )

            elif payment_type == "premium":
                premium_until = current + timedelta(
                    days=PREMIUM_DAYS
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
                VALUES (%s,%s,%s,%s,%s)
                """,
                (
                    customer,
                    movie_id,
                    watch_until,
                    download_until,
                    premium_until,
                )
            )

            conn.commit()

            return {
                "paid": True,
                "status": "PAID",
                "payment_type": payment_type,
                "movie_id": movie_id,
            }

    finally:
        if conn:
            conn.close()


# =========================================================
# ACCESS CHECKS
# =========================================================

def get_customer_access(movie_id):

    cid = customer_id()

    conn = get_db()

    try:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT *
                FROM customer_access
                WHERE customer_id = %s
                AND (
                    movie_id = %s
                    OR movie_id IS NULL
                )
                ORDER BY id DESC
                """,
                (
                    cid,
                    movie_id,
                )
            )

            rows = cur.fetchall()

            result = {
                "watch": False,
                "download": False,
                "premium": False,
            }

            current = now_utc()

            for row in rows:

                if (
                    row["premium_until"]
                    and row["premium_until"] > current
                ):
                    result["premium"] = True
                    result["watch"] = True
                    result["download"] = True

                if (
                    row["movie_id"] == movie_id
                    and row["watch_until"]
                    and row["watch_until"] > current
                ):
                    result["watch"] = True

                if (
                    row["movie_id"] == movie_id
                    and row["download_until"]
                    and row["download_until"] > current
                ):
                    result["download"] = True

            return result

    finally:
        conn.close()


# =========================================================
# HOME
# =========================================================

@app.route("/", endpoint="home")
def index():

    conn = get_db()

    try:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT *
                FROM movies
                ORDER BY id DESC
                """
            )

            movies = cur.fetchall()

        return render_template(
            "index.html",
            movies=movies
        )

    finally:
        conn.close()


# =========================================================
# MOVIE PAGE
# =========================================================

@app.route("/movie/<int:movie_id>")
def movie(movie_id):

    conn = get_db()

    try:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT *
                FROM movies
                WHERE id = %s
                LIMIT 1
                """,
                (movie_id,)
            )

            movie_data = cur.fetchone()

            if not movie_data:
                abort(404)

            cur.execute(
                """
                UPDATE movies
                SET views = COALESCE(views,0) + 1
                WHERE id = %s
                """,
                (movie_id,)
            )

            conn.commit()

    finally:
        conn.close()

    access = get_customer_access(
        movie_id
    )

    return render_template(
        "movie.html",
        movie=movie_data,
        access=access,
        watch_price=WATCH_PRICE,
        download_price=DOWNLOAD_PRICE,
        premium_price=PREMIUM_PRICE,
        cashfree_mode=CASHFREE_JS_MODE,
    )


# =========================================================
# PAYMENT CREATE
# =========================================================

@app.route(
    "/payment/create",
    methods=["POST"]
)
def payment_create():

    try:

        payment_type = (
            request.form.get(
                "payment_type",
                ""
            )
            .strip()
            .lower()
        )

        movie_id_raw = request.form.get(
            "movie_id",
            ""
        ).strip()

        movie_id = None

        if movie_id_raw:
            try:
                movie_id = int(movie_id_raw)
            except ValueError:
                movie_id = None

        if payment_type == "watch":
            amount = WATCH_PRICE

        elif payment_type == "download":
            amount = DOWNLOAD_PRICE

        elif payment_type == "premium":
            amount = PREMIUM_PRICE

        else:
            return jsonify({
                "ok": False,
                "message": "Invalid payment type."
            }), 400

        if payment_type in (
            "watch",
            "download",
        ) and not movie_id:

            return jsonify({
                "ok": False,
                "message": "Movie ID is required."
            }), 400

        result = create_cashfree_order(
            amount=amount,
            payment_type=payment_type,
            movie_id=movie_id,
        )

        return jsonify({
            "ok": True,
            "order_id": result["order_id"],
            "payment_session_id": result[
                "payment_session_id"
            ],
            "amount": result["amount"],
            "mode": CASHFREE_JS_MODE,
        })

    except Exception as e:

        print(
            "PAYMENT CREATE ERROR:",
            repr(e)
        )

        return jsonify({
            "ok": False,
            "message": str(e),
        }), 500


# =========================================================
# PAYMENT RETURN
# =========================================================

@app.route("/payment/return")
def payment_return():

    order_id = request.args.get(
        "order_id",
        ""
    ).strip()

    if not order_id:
        flash(
            "Payment order ID missing.",
            "error"
        )

        return redirect(
            url_for("index")
        )

    try:

        result = apply_paid_access(
            order_id
        )

        if result.get("paid"):

            movie_id = result.get(
                "movie_id"
            )

            flash(
                "Payment successful. Access activated.",
                "success"
            )

            if movie_id:
                return redirect(
                    url_for(
                        "movie",
                        movie_id=movie_id
                    )
                )

            return redirect(
                url_for("index")
            )

        flash(
            "Payment is not completed yet.",
            "warning"
        )

    except Exception as e:

        print(
            "PAYMENT RETURN ERROR:",
            repr(e)
        )

        flash(
            "Payment verification failed: "
            + str(e),
            "error"
        )

    return redirect(
        url_for("index")
    )


# =========================================================
# PAYMENT VERIFY API
# =========================================================

@app.route(
    "/api/payment/verify",
    methods=["POST"]
)
def payment_verify():

    try:

        data = request.get_json(
            silent=True
        ) or {}

        order_id = str(
            data.get("order_id", "")
        ).strip()

        if not order_id:
            return jsonify({
                "ok": False,
                "message": "order_id is required."
            }), 400

        result = apply_paid_access(
            order_id
        )

        if result.get("paid"):

            return jsonify({
                "ok": True,
                "paid": True,
                "status": "PAID",
                "movie_id": result.get(
                    "movie_id"
                ),
                "payment_type": result.get(
                    "payment_type"
                ),
            })

        return jsonify({
            "ok": True,
            "paid": False,
            "status": result.get(
                "status"
            ),
        })

    except Exception as e:

        print(
            "PAYMENT VERIFY ERROR:",
            repr(e)
        )

        return jsonify({
            "ok": False,
            "message": str(e),
        }), 500


# =========================================================
# CASHFREE WEBHOOK
# =========================================================

@app.route(
    "/api/cashfree/webhook",
    methods=["POST"]
)
def cashfree_webhook():

    try:

        payload = request.get_json(
            silent=True
        ) or {}

        print(
            "CASHFREE WEBHOOK RECEIVED:",
            payload
        )

        data = payload.get(
            "data",
            {}
        )

        order_data = data.get(
            "order",
            {}
        )

        order_id = order_data.get(
            "order_id"
        )

        if order_id:

            try:
                apply_paid_access(
                    order_id
                )
            except Exception as e:
                print(
                    "WEBHOOK APPLY ERROR:",
                    repr(e)
                )

        return jsonify({
            "ok": True
        })

    except Exception as e:

        print(
            "WEBHOOK ERROR:",
            repr(e)
        )

        return jsonify({
            "ok": False,
            "message": str(e)
        }), 500


# =========================================================
# STREAM VIDEO
# =========================================================

@app.route(
    "/stream/<int:movie_id>"
)
def stream_movie(movie_id):

    access = get_customer_access(
        movie_id
    )

    if not (
        access["watch"]
        or access["premium"]
    ):
        return jsonify({
            "ok": False,
            "message": (
                "Watch access required."
            )
        }), 403

    conn = get_db()

    try:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT *
                FROM movies
                WHERE id = %s
                LIMIT 1
                """,
                (movie_id,)
            )

            movie_data = cur.fetchone()

    finally:
        conn.close()

    if not movie_data:
        abort(404)

    video_key = (
        movie_data.get("video")
        or ""
    ).strip()

    if not video_key:
        abort(404)

    if not r2:
        return jsonify({
            "ok": False,
            "message": "R2 is not configured."
        }), 500

    try:

        head = r2.head_object(
            Bucket=R2_BUCKET,
            Key=video_key
        )

        file_size = int(
            head["ContentLength"]
        )

        content_type = (
            head.get("ContentType")
            or mimetypes.guess_type(
                video_key
            )[0]
            or "video/mp4"
        )

        range_header = request.headers.get(
            "Range"
        )

        start = 0
        end = file_size - 1

        if range_header:

            try:

                range_value = (
                    range_header
                    .replace("bytes=", "")
                    .split(",")[0]
                    .strip()
                )

                if "-" in range_value:

                    start_text, end_text = (
                        range_value.split(
                            "-",
                            1
                        )
                    )

                    if start_text:
                        start = int(
                            start_text
                        )

                    if end_text:
                        end = int(
                            end_text
                        )
                    else:
                        end = file_size - 1

                    if start > end:
                        return Response(
                            status=416,
                            headers={
                                "Content-Range":
                                f"bytes */{file_size}"
                            }
                        )

                    if end >= file_size:
                        end = file_size - 1

            except Exception:

                return Response(
                    status=416,
                    headers={
                        "Content-Range":
                        f"bytes */{file_size}"
                    }
                )

        length = (
            end - start + 1
        )

        obj = r2.get_object(
            Bucket=R2_BUCKET,
            Key=video_key,
            Range=f"bytes={start}-{end}"
        )

        body = obj["Body"]

        def generate():

            try:

                while True:

                    chunk = body.read(
                        1024 * 1024
                    )

                    if not chunk:
                        break

                    yield chunk

            finally:

                try:
                    body.close()
                except Exception:
                    pass

        headers = {
            "Content-Type": content_type,
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Cache-Control": "public, max-age=3600",
            "Content-Disposition": (
                "inline; filename=\""
                + secure_filename(
                    movie_data["title"]
                )
                + "."
                + get_extension(video_key)
                + "\""
            ),
        }

        if range_header:

            headers["Content-Range"] = (
                f"bytes {start}-{end}/{file_size}"
            )

            return Response(
                generate(),
                status=206,
                headers=headers,
                direct_passthrough=True,
            )

        return Response(
            generate(),
            status=200,
            headers=headers,
            direct_passthrough=True,
        )

    except Exception as e:

        print(
            "STREAM ERROR:",
            repr(e)
        )

        return jsonify({
            "ok": False,
            "message": "Video stream failed."
        }), 500


# =========================================================
# DOWNLOAD
# =========================================================

@app.route(
    "/download/<int:movie_id>"
)
def download_movie(movie_id):

    access = get_customer_access(
        movie_id
    )

    if not (
        access["download"]
        or access["premium"]
    ):
        return jsonify({
            "ok": False,
            "message": (
                "Download access required."
            )
        }), 403

    conn = get_db()

    try:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT *
                FROM movies
                WHERE id = %s
                LIMIT 1
                """,
                (movie_id,)
            )

            movie_data = cur.fetchone()

    finally:
        conn.close()

    if not movie_data:
        abort(404)

    video_key = (
        movie_data.get("video")
        or ""
    ).strip()

    if not video_key:
        abort(404)

    if not r2:
        return jsonify({
            "ok": False,
            "message": "R2 is not configured."
        }), 500

    try:

        url = r2.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": R2_BUCKET,
                "Key": video_key,
                "ResponseContentDisposition":
                    "attachment; filename=\""
                    + secure_filename(
                        movie_data["title"]
                    )
                    + "."
                    + get_extension(video_key)
                    + "\"",
            },
            ExpiresIn=3600,
        )

        return redirect(url)

    except Exception as e:

        print(
            "DOWNLOAD ERROR:",
            repr(e)
        )

        return jsonify({
            "ok": False,
            "message": "Download failed."
        }), 500


# =========================================================
# R2 HEALTH
# =========================================================

@app.route("/r2-health")
def r2_health():

    if not r2:

        return jsonify({
            "ok": False,
            "message": "R2 client not configured.",
            "bucket": R2_BUCKET,
        }), 500

    try:

        r2.head_bucket(
            Bucket=R2_BUCKET
        )

        return jsonify({
            "ok": True,
            "message": "R2 OK",
            "bucket": R2_BUCKET,
        })

    except Exception as e:

        print(
            "R2 HEALTH ERROR:",
            repr(e)
        )

        return jsonify({
            "ok": False,
            "message": str(e),
            "bucket": R2_BUCKET,
        }), 500


# =========================================================
# DB HEALTH
# =========================================================

@app.route("/db-health")
def db_health():

    conn = None

    try:

        conn = get_db()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1"
            )

        return jsonify({
            "ok": True,
            "message": "Database OK"
        })

    except Exception as e:

        return jsonify({
            "ok": False,
            "message": str(e)
        }), 500

    finally:

        if conn:
            conn.close()


# =========================================================
# HEALTH
# =========================================================

@app.route("/health")
def health():

    return jsonify({
        "ok": True,
        "service": "tomesh-movie",
        "cashfree_env": CASHFREE_ENV,
        "cashfree_api_version":
            CASHFREE_API_VERSION,
        "r2_configured":
            bool(r2),
        "database_configured":
            bool(DATABASE_URL),
    })


# =========================================================
# CASHFREE CONFIG HEALTH
# =========================================================

@app.route("/cashfree-health")
def cashfree_health():

    return jsonify({
        "ok": bool(
            CASHFREE_APP_ID
            and CASHFREE_SECRET_KEY
        ),
        "environment": CASHFREE_ENV,
        "api_url": CASHFREE_API_URL,
        "api_version":
            CASHFREE_API_VERSION,
        "app_id_configured":
            bool(CASHFREE_APP_ID),
        "secret_configured":
            bool(CASHFREE_SECRET_KEY),
        "app_id_length":
            len(CASHFREE_APP_ID),
        "secret_length":
            len(CASHFREE_SECRET_KEY),
    })


# =========================================================
# R2 MULTIPART CREATE
# =========================================================

@app.route(
    "/api/r2/multipart/create",
    methods=["POST"]
)
def r2_multipart_create():

    if not r2:
        return jsonify({
            "ok": False,
            "message": "R2 is not configured."
        }), 500

    try:

        data = request.get_json(
            silent=True
        ) or {}

        key = str(
            data.get("key", "")
        ).strip()

        content_type = str(
            data.get(
                "content_type",
                "application/octet-stream"
            )
        ).strip()

        if not key:
            return jsonify({
                "ok": False,
                "message": "key is required."
            }), 400

        if not (
            key.startswith("videos/")
            or key.startswith("posters/")
        ):
            return jsonify({
                "ok": False,
                "message": "Invalid object key."
            }), 400

        response = r2.create_multipart_upload(
            Bucket=R2_BUCKET,
            Key=key,
            ContentType=content_type,
        )

        upload_id = response.get(
            "UploadId"
        )

        if not upload_id:
            return jsonify({
                "ok": False,
                "message": "UploadId missing."
            }), 500

        return jsonify({
            "ok": True,
            "upload_id": upload_id,
            "key": key,
        })

    except Exception as e:

        print(
            "R2 MULTIPART CREATE ERROR:",
            repr(e)
        )

        return jsonify({
            "ok": False,
            "message": str(e)
        }), 500


# =========================================================
# R2 MULTIPART PRESIGNED URLS
# =========================================================

@app.route(
    "/api/r2/multipart/urls",
    methods=["POST"]
)
def r2_multipart_urls():

    if not r2:
        return jsonify({
            "ok": False,
            "message": "R2 is not configured."
        }), 500

    try:

        data = request.get_json(
            silent=True
        ) or {}

        key = str(
            data.get("key", "")
        ).strip()

        upload_id = str(
            data.get("upload_id", "")
        ).strip()

        part_numbers = data.get(
            "part_numbers",
            []
        )

        if not key:
            return jsonify({
                "ok": False,
                "message": "key is required."
            }), 400

        if not upload_id:
            return jsonify({
                "ok": False,
                "message": "upload_id is required."
            }), 400

        if not (
            key.startswith("videos/")
            or key.startswith("posters/")
        ):
            return jsonify({
                "ok": False,
                "message": "Invalid object key."
            }), 400

        if not isinstance(
            part_numbers,
            list
        ):
            return jsonify({
                "ok": False,
                "message": "part_numbers must be a list."
            }), 400

        if len(part_numbers) > 10000:
            return jsonify({
                "ok": False,
                "message": "Too many parts."
            }), 400

        urls = []

        for part_number in part_numbers:

            part_number = int(
                part_number
            )

            if part_number < 1:
                return jsonify({
                    "ok": False,
                    "message":
                        "Invalid part number."
                }), 400

            if part_number > 10000:
                return jsonify({
                    "ok": False,
                    "message":
                        "Invalid part number."
                }), 400

            url = r2.generate_presigned_url(
                "upload_part",
                Params={
                    "Bucket": R2_BUCKET,
                    "Key": key,
                    "UploadId": upload_id,
                    "PartNumber": part_number,
                },
                ExpiresIn=3600,
            )

            urls.append({
                "part_number": part_number,
                "url": url,
            })

        return jsonify({
            "ok": True,
            "urls": urls,
        })

    except Exception as e:

        print(
            "R2 MULTIPART URL ERROR:",
            repr(e)
        )

        return jsonify({
            "ok": False,
            "message": str(e)
        }), 500


# =========================================================
# R2 MULTIPART COMPLETE
# =========================================================

@app.route(
    "/api/r2/multipart/complete",
    methods=["POST"]
)
def r2_multipart_complete():

    if not r2:
        return jsonify({
            "ok": False,
            "message": "R2 is not configured."
        }), 500

    try:

        data = request.get_json(
            silent=True
        ) or {}

        key = str(
            data.get("key", "")
        ).strip()

        upload_id = str(
            data.get("upload_id", "")
        ).strip()

        parts = data.get(
            "parts",
            []
        )

        if not key or not upload_id:
            return jsonify({
                "ok": False,
                "message":
                    "key and upload_id are required."
            }), 400

        if not (
            key.startswith("videos/")
            or key.startswith("posters/")
        ):
            return jsonify({
                "ok": False,
                "message": "Invalid object key."
            }), 400

        if not isinstance(
            parts,
            list
        ) or not parts:

            return jsonify({
                "ok": False,
                "message":
                    "parts are required."
            }), 400

        clean_parts = []

        for part in parts:

            part_number = int(
                part.get(
                    "PartNumber",
                    part.get(
                        "part_number",
                        0
                    )
                )
            )

            etag = str(
                part.get(
                    "ETag",
                    part.get(
                        "etag",
                        ""
                    )
                )
            ).strip()

            if not part_number or not etag:
                return jsonify({
                    "ok": False,
                    "message":
                        "Invalid part data."
                }), 400

            clean_parts.append({
                "PartNumber": part_number,
                "ETag": etag,
            })

        clean_parts.sort(
            key=lambda x: x["PartNumber"]
        )

        response = r2.complete_multipart_upload(
            Bucket=R2_BUCKET,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": clean_parts
            },
        )

        return jsonify({
            "ok": True,
            "key": key,
            "location":
                response.get("Location"),
            "etag":
                response.get("ETag"),
        })

    except Exception as e:

        print(
            "R2 MULTIPART COMPLETE ERROR:",
            repr(e)
        )

        return jsonify({
            "ok": False,
            "message": str(e)
        }), 500


# =========================================================
# R2 MULTIPART ABORT
# =========================================================

@app.route(
    "/api/r2/multipart/abort",
    methods=["POST"]
)
def r2_multipart_abort():

    if not r2:
        return jsonify({
            "ok": False,
            "message": "R2 is not configured."
        }), 500

    try:

        data = request.get_json(
            silent=True
        ) or {}

        key = str(
            data.get("key", "")
        ).strip()

        upload_id = str(
            data.get("upload_id", "")
        ).strip()

        if not key or not upload_id:
            return jsonify({
                "ok": False,
                "message":
                    "key and upload_id are required."
            }), 400

        r2.abort_multipart_upload(
            Bucket=R2_BUCKET,
            Key=key,
            UploadId=upload_id,
        )

        return jsonify({
            "ok": True
        })

    except Exception as e:

        print(
            "R2 MULTIPART ABORT ERROR:",
            repr(e)
        )

        return jsonify({
            "ok": False,
            "message": str(e)
        }), 500


# =========================================================
# SAVE MOVIE AFTER R2 UPLOAD
# =========================================================

@app.route(
    "/api/movie/save",
    methods=["POST"]
)
@admin_required
def api_movie_save():

    try:

        data = request.get_json(
            silent=True
        ) or {}

        title = str(
            data.get("title", "")
        ).strip()

        category = str(
            data.get("category", "")
        ).strip()

        description = str(
            data.get("description", "")
        ).strip()

        poster = str(
            data.get("poster", "")
        ).strip()

        video = str(
            data.get("video", "")
        ).strip()

        if not title:
            return jsonify({
                "ok": False,
                "message":
                    "Movie title is required."
            }), 400

        if not video:
            return jsonify({
                "ok": False,
                "message":
                    "Video upload is required."
            }), 400

        if not video.startswith(
            "videos/"
        ):
            return jsonify({
                "ok": False,
                "message":
                    "Invalid video key."
            }), 400

        if poster and not poster.startswith(
            "posters/"
        ):
            return jsonify({
                "ok": False,
                "message":
                    "Invalid poster key."
            }), 400

        conn = get_db()

        try:

            with conn.cursor(
                cursor_factory=RealDictCursor
            ) as cur:

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
                    VALUES (%s,%s,%s,%s,%s)
                    RETURNING id
                    """,
                    (
                        title,
                        category,
                        description,
                        poster,
                        video,
                    )
                )

                movie_id = cur.fetchone()[
                    "id"
                ]

                conn.commit()

        finally:
            conn.close()

        return jsonify({
            "ok": True,
            "movie_id": movie_id,
        })

    except Exception as e:

        print(
            "MOVIE SAVE ERROR:",
            repr(e)
        )

        return jsonify({
            "ok": False,
            "message": str(e)
        }), 500


# =========================================================
# ADMIN LOGIN
# =========================================================

@app.route(
    "/login",
    methods=["GET", "POST"]
)
def login():

    if request.method == "POST":

        username = request.form.get(
            "username",
            ""
        ).strip()

        password = request.form.get(
            "password",
            ""
        )

        if (
            username == ADMIN_USER
            and password == ADMIN_PASSWORD
        ):

            session["admin_logged_in"] = True

            return redirect(
                url_for("admin")
            )

        flash(
            "Invalid username or password.",
            "error"
        )

    return render_template(
        "login.html"
    )


# =========================================================
# LOGOUT
# =========================================================

@app.route("/logout")
def logout():

    session.pop(
        "admin_logged_in",
        None
    )

    return redirect(
        url_for("login")
    )


# =========================================================
# ADMIN
# =========================================================

@app.route("/admin")
@admin_required
def admin():

    conn = get_db()

    try:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT *
                FROM movies
                ORDER BY id DESC
                """
            )

            movies = cur.fetchall()

            cur.execute(
                """
                SELECT
                    COUNT(*) AS total_movies,
                    COALESCE(
                        SUM(views),
                        0
                    ) AS total_views
                FROM movies
                """
            )

            stats = cur.fetchone()

        return render_template(
            "admin.html",
            movies=movies,
            stats=stats,
            cashfree_env=CASHFREE_ENV,
        )

    finally:
        conn.close()


# =========================================================
# ADMIN ADD PAGE
# =========================================================

@app.route(
    "/admin/add",
    methods=["GET"]
)
@admin_required
def admin_add():

    return render_template(
        "admin_add.html"
    )


# =========================================================
# ADMIN DELETE MOVIE
# =========================================================

@app.route(
    "/admin/delete/<int:movie_id>",
    methods=["POST", "GET"]
)
@admin_required
def admin_delete(movie_id):

    conn = get_db()

    movie_data = None

    try:

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT *
                FROM movies
                WHERE id = %s
                LIMIT 1
                """,
                (movie_id,)
            )

            movie_data = cur.fetchone()

            if not movie_data:
                flash(
                    "Movie not found.",
                    "error"
                )

                return redirect(
                    url_for("admin")
                )

            cur.execute(
                """
                DELETE FROM movies
                WHERE id = %s
                """,
                (movie_id,)
            )

            conn.commit()

    finally:
        conn.close()

    # Delete R2 objects too
    if r2 and movie_data:

        for key_name in (
            movie_data.get("video"),
            movie_data.get("poster"),
        ):

            if not key_name:
                continue

            try:

                r2.delete_object(
                    Bucket=R2_BUCKET,
                    Key=key_name,
                )

            except Exception as e:

                print(
                    "R2 DELETE ERROR:",
                    key_name,
                    repr(e)
                )

    flash(
        "Movie deleted successfully.",
        "success"
    )

    return redirect(
        url_for("admin")
    )


# =========================================================
# ADS.TXT
# =========================================================

@app.route("/ads.txt")
def ads_txt():

    return Response(
        "google.com, pub-8697157365303435, DIRECT, f08c47fec0942fa0\n",
        mimetype="text/plain"
    )


# =========================================================
# LEGACY POSTER ROUTE
# =========================================================

@app.route(
    "/poster/<path:name>"
)
def legacy_poster(name):

    if not r2:
        abort(404)

    try:

        url = r2.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": R2_BUCKET,
                "Key": name,
            },
            ExpiresIn=3600,
        )

        return redirect(url)

    except Exception:
        abort(404)


# =========================================================
# LEGACY VIDEO ROUTE
# =========================================================

@app.route(
    "/video/<path:name>"
)
def legacy_video(name):

    if not r2:
        abort(404)

    try:

        url = r2.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": R2_BUCKET,
                "Key": name,
            },
            ExpiresIn=3600,
        )

        return redirect(url)

    except Exception:
        abort(404)


# =========================================================
# ERROR HANDLERS
# =========================================================

@app.errorhandler(413)
def too_large(error):

    return jsonify({
        "ok": False,
        "message":
            "File is too large. Maximum size is 4 GB."
    }), 413


@app.errorhandler(404)
def not_found(error):

    return render_template(
        "404.html"
    ), 404


@app.errorhandler(500)
def server_error(error):

    print(
        "SERVER 500 ERROR:",
        repr(error)
    )

    return render_template(
        "500.html"
    ), 500


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "5000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
    )
