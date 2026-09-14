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


# =========================================================
# APP
# =========================================================

app = Flask(__name__)

app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY",
    secrets.token_hex(32)
)

# 4 GB
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024


# =========================================================
# ADMIN
# =========================================================

ADMIN_USER = os.environ.get("ADMIN_USER", "admin").strip()
ADMIN_PASSWORD = os.environ.get(
    "ADMIN_PASSWORD",
    "change-me-now"
).strip()


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

# Current Cashfree API version
CASHFREE_API_VERSION = "2026-01-01"

if CASHFREE_ENV == "production":
    CASHFREE_API_URL = "https://api.cashfree.com/pg"
    CASHFREE_JS_MODE = "production"
else:
    CASHFREE_ENV = "sandbox"
    CASHFREE_API_URL = "https://sandbox.cashfree.com/pg"
    CASHFREE_JS_MODE = "sandbox"


def cashfree_request(method, path, payload=None):
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

    if method.upper() == "POST":
        headers["x-idempotency-key"] = str(uuid.uuid4())

    response = requests.request(
        method=method.upper(),
        url=url,
        headers=headers,
        json=payload,
        timeout=60,
    )

    print(
        "Cashfree:",
        method.upper(),
        path,
        "status=",
        response.status_code
    )

    if response.status_code >= 400:
        try:
            data = response.json()
        except Exception:
            data = {
                "message": response.text[:1000]
            }

        raise RuntimeError(
            f"Cashfree API {response.status_code}: {data}"
        )

    try:
        return response.json()
    except Exception:
        return {}


# =========================================================
# DATABASE
# =========================================================

DATABASE_URL = clean_env_value(
    os.environ.get("DATABASE_URL", "")
)


def get_db():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is missing."
        )

    return psycopg2.connect(
        DATABASE_URL,
        sslmode="require"
    )


def init_db():
    conn = get_db()

    try:
        with conn.cursor() as cur:

            cur.execute("""
                CREATE TABLE IF NOT EXISTS movies (
                    id SERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    category TEXT DEFAULT 'Hindi',
                    description TEXT DEFAULT '',
                    poster_url TEXT DEFAULT '',
                    video_url TEXT DEFAULT '',
                    video_key TEXT DEFAULT '',
                    poster_key TEXT DEFAULT '',
                    views INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id SERIAL PRIMARY KEY,
                    order_id TEXT UNIQUE NOT NULL,
                    movie_id INTEGER,
                    customer_id TEXT,
                    payment_type TEXT,
                    amount NUMERIC(10,2),
                    status TEXT DEFAULT 'ACTIVE',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    paid_at TIMESTAMP NULL,
                    expires_at TIMESTAMP NULL
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS watch_access (
                    id SERIAL PRIMARY KEY,
                    movie_id INTEGER NOT NULL,
                    customer_id TEXT NOT NULL,
                    order_id TEXT UNIQUE NOT NULL,
                    expires_at TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    id SERIAL PRIMARY KEY,
                    setting_key TEXT UNIQUE NOT NULL,
                    setting_value TEXT DEFAULT ''
                )
            """)

        conn.commit()

    finally:
        conn.close()


try:
    init_db()
    print("Database initialized successfully")
except Exception as e:
    print("Database initialization error:", e)


# =========================================================
# HELPERS
# =========================================================

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


def allowed_file(filename, allowed):
    if not filename:
        return False

    if "." not in filename:
        return False

    ext = filename.rsplit(".", 1)[1].lower()

    return ext in allowed


def get_extension(filename):
    if not filename or "." not in filename:
        return ""

    return filename.rsplit(".", 1)[1].lower()


def admin_required(fn):

    @wraps(fn)
    def wrapper(*args, **kwargs):

        if not session.get("admin_logged_in"):
            return redirect(
                url_for("admin_login")
            )

        return fn(*args, **kwargs)

    return wrapper


def customer_id():
    if "customer_id" not in session:
        session["customer_id"] = (
            "cust_"
            + secrets.token_hex(12)
        )

    return session["customer_id"]


def db_fetchone(query, params=()):
    conn = get_db()

    try:
        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:
            cur.execute(query, params)
            return cur.fetchone()

    finally:
        conn.close()


def db_fetchall(query, params=()):
    conn = get_db()

    try:
        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:
            cur.execute(query, params)
            return cur.fetchall()

    finally:
        conn.close()


def db_execute(query, params=(), returning=False):
    conn = get_db()

    try:
        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(query, params)

            result = None

            if returning:
                result = cur.fetchone()

            conn.commit()

            return result

    finally:
        conn.close()


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
    os.environ.get(
        "R2_BUCKET",
        "tomesh-movies"
    )
)

R2_ENDPOINT = clean_env_value(
    os.environ.get("R2_ENDPOINT", "")
)

R2_PUBLIC_URL = clean_env_value(
    os.environ.get("R2_PUBLIC_URL", "")
)


def get_r2_client():

    if not R2_ENDPOINT:
        raise RuntimeError(
            "R2_ENDPOINT is missing."
        )

    if not R2_ACCESS_KEY_ID:
        raise RuntimeError(
            "R2_ACCESS_KEY_ID is missing."
        )

    if not R2_SECRET_ACCESS_KEY:
        raise RuntimeError(
            "R2_SECRET_ACCESS_KEY is missing."
        )

    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=Config(
            signature_version="s3v4"
        ),
    )


# =========================================================
# R2 HEALTH
# =========================================================

@app.route("/r2-health")
def r2_health():

    try:
        client = get_r2_client()

        client.head_bucket(
            Bucket=R2_BUCKET
        )

        return jsonify({
            "ok": True,
            "bucket": R2_BUCKET,
            "message": "R2 OK"
        })

    except Exception as e:

        return jsonify({
            "ok": False,
            "message": str(e)
        }), 500


# =========================================================
# CASHFREE HEALTH
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
        "api_version": CASHFREE_API_VERSION,
        "app_id_configured": bool(
            CASHFREE_APP_ID
        ),
        "secret_configured": bool(
            CASHFREE_SECRET_KEY
        ),
        "app_id_length": len(
            CASHFREE_APP_ID
        ),
        "secret_length": len(
            CASHFREE_SECRET_KEY
        ),
    })


# =========================================================
# HEALTH
# =========================================================

@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "service": "tomesh-movie"
    })


@app.route("/db-health")
def db_health():

    try:
        conn = get_db()

        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()

        finally:
            conn.close()

        return jsonify({
            "ok": True,
            "message": "Database OK"
        })

    except Exception as e:

        return jsonify({
            "ok": False,
            "message": str(e)
        }), 500


# =========================================================
# HOME
# IMPORTANT:
# endpoint="home"
# =========================================================

@app.route("/", endpoint="home")
def index():

    try:
        movies = db_fetchall("""
            SELECT *
            FROM movies
            ORDER BY created_at DESC
        """)

    except Exception as e:

        print("Home error:", e)

        movies = []

    return render_template(
        "index.html",
        movies=movies
    )


# =========================================================
# MOVIE PAGE
# IMPORTANT:
# endpoint="movie_page"
# This keeps old templates working.
# =========================================================

@app.route(
    "/movie/<int:movie_id>",
    endpoint="movie_page"
)
def movie(movie_id):

    movie_data = db_fetchone("""
        SELECT *
        FROM movies
        WHERE id = %s
    """, (movie_id,))

    if not movie_data:
        abort(404)

    db_execute("""
        UPDATE movies
        SET views = COALESCE(views, 0) + 1
        WHERE id = %s
    """, (movie_id,))

    movie_data["views"] = (
        movie_data.get("views", 0) or 0
    ) + 1

    access = get_watch_access(
        movie_id,
        customer_id()
    )

    return render_template(
        "movie.html",
        movie=movie_data,
        access=access,
        cashfree_mode=CASHFREE_JS_MODE
    )


# =========================================================
# WATCH ACCESS
# =========================================================

def get_watch_access(movie_id, cust_id):

    row = db_fetchone("""
        SELECT *
        FROM watch_access
        WHERE movie_id = %s
          AND customer_id = %s
          AND expires_at > CURRENT_TIMESTAMP
        ORDER BY expires_at DESC
        LIMIT 1
    """, (movie_id, cust_id))

    return row


# =========================================================
# STREAM VIDEO FROM R2
# =========================================================

@app.route("/stream/<int:movie_id>")
def stream_movie(movie_id):

    movie_data = db_fetchone("""
        SELECT *
        FROM movies
        WHERE id = %s
    """, (movie_id,))

    if not movie_data:
        abort(404)

    access = get_watch_access(
        movie_id,
        customer_id()
    )

    if not access:
        return jsonify({
            "ok": False,
            "message": "Watch access required."
        }), 403

    video_key = movie_data.get(
        "video_key"
    )

    if not video_key:
        abort(404)

    client = get_r2_client()

    try:

        head = client.head_object(
            Bucket=R2_BUCKET,
            Key=video_key
        )

        total_size = head["ContentLength"]

        range_header = request.headers.get(
            "Range"
        )

        content_type = (
            head.get("ContentType")
            or mimetypes.guess_type(
                video_key
            )[0]
            or "video/mp4"
        )

        if not range_header:

            obj = client.get_object(
                Bucket=R2_BUCKET,
                Key=video_key
            )

            body = obj["Body"]

            def generate():

                while True:

                    chunk = body.read(
                        1024 * 1024
                    )

                    if not chunk:
                        break

                    yield chunk

            response = Response(
                generate(),
                status=200,
                mimetype=content_type
            )

            response.headers[
                "Content-Length"
            ] = str(total_size)

            response.headers[
                "Accept-Ranges"
            ] = "bytes"

            response.headers[
                "Cache-Control"
            ] = "public, max-age=3600"

            return response

        # -----------------------------------------
        # RANGE REQUEST
        # -----------------------------------------

        try:
            range_value = (
                range_header
                .replace("bytes=", "")
                .strip()
            )

            start_text, end_text = (
                range_value.split("-", 1)
            )

            start = int(start_text)

            if end_text:
                end = int(end_text)
            else:
                end = total_size - 1

        except Exception:

            return Response(
                status=416,
                headers={
                    "Content-Range":
                    f"bytes */{total_size}"
                }
            )

        if start >= total_size:
            return Response(
                status=416,
                headers={
                    "Content-Range":
                    f"bytes */{total_size}"
                }
            )

        end = min(
            end,
            total_size - 1
        )

        length = end - start + 1

        obj = client.get_object(
            Bucket=R2_BUCKET,
            Key=video_key,
            Range=f"bytes={start}-{end}"
        )

        body = obj["Body"]

        def generate_range():

            remaining = length

            while remaining > 0:

                chunk = body.read(
                    min(
                        1024 * 1024,
                        remaining
                    )
                )

                if not chunk:
                    break

                remaining -= len(chunk)

                yield chunk

        response = Response(
            generate_range(),
            status=206,
            mimetype=content_type
        )

        response.headers[
            "Content-Range"
        ] = (
            f"bytes {start}-{end}/{total_size}"
        )

        response.headers[
            "Accept-Ranges"
        ] = "bytes"

        response.headers[
            "Content-Length"
        ] = str(length)

        response.headers[
            "Cache-Control"
        ] = "public, max-age=3600"

        return response

    except Exception as e:

        print(
            "Stream error:",
            repr(e)
        )

        return jsonify({
            "ok": False,
            "message": str(e)
        }), 500


# =========================================================
# DOWNLOAD
# =========================================================

@app.route("/download/<int:movie_id>")
def download_movie(movie_id):

    movie_data = db_fetchone("""
        SELECT *
        FROM movies
        WHERE id = %s
    """, (movie_id,))

    if not movie_data:
        abort(404)

    video_key = movie_data.get(
        "video_key"
    )

    if not video_key:
        abort(404)

    client = get_r2_client()

    try:

        download_url = client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": R2_BUCKET,
                "Key": video_key,
                "ResponseContentDisposition":
                    "attachment; filename="
                    + quote(
                        secure_filename(
                            movie_data["title"]
                        )
                        + ".mp4"
                    ),
            },
            ExpiresIn=3600
        )

        return redirect(download_url)

    except Exception as e:

        print(
            "Download error:",
            repr(e)
        )

        return jsonify({
            "ok": False,
            "message": str(e)
        }), 500


# =========================================================
# R2 MULTIPART CREATE
# =========================================================

@app.route(
    "/api/r2/multipart/create",
    methods=["POST"]
)
def r2_multipart_create():

    data = request.get_json(
        silent=True
    ) or {}

    filename = str(
        data.get("filename", "")
    ).strip()

    content_type = str(
        data.get(
            "content_type",
            "application/octet-stream"
        )
    ).strip()

    file_type = str(
        data.get(
            "file_type",
            "video"
        )
    ).lower()

    if not filename:
        return jsonify({
            "ok": False,
            "message": "Filename missing."
        }), 400

    ext = get_extension(filename)

    if file_type == "video":

        if ext not in ALLOWED_VIDEOS:
            return jsonify({
                "ok": False,
                "message":
                "Video केवल MP4, MKV, WebM या MOV होनी चाहिए."
            }), 400

        prefix = "videos"

    elif file_type == "poster":

        if ext not in ALLOWED_POSTERS:
            return jsonify({
                "ok": False,
                "message":
                "Poster JPG, JPEG, PNG या WEBP होना चाहिए."
            }), 400

        prefix = "posters"

    else:

        return jsonify({
            "ok": False,
            "message": "Invalid file type."
        }), 400

    safe_name = secure_filename(
        filename
    )

    key = (
        f"{prefix}/"
        f"{uuid.uuid4().hex}_"
        f"{safe_name}"
    )

    try:

        client = get_r2_client()

        result = client.create_multipart_upload(
            Bucket=R2_BUCKET,
            Key=key,
            ContentType=content_type
        )

        return jsonify({
            "ok": True,
            "upload_id":
                result["UploadId"],
            "key": key
        })

    except Exception as e:

        print(
            "Multipart create error:",
            repr(e)
        )

        return jsonify({
            "ok": False,
            "message": str(e)
        }), 500


# =========================================================
# R2 MULTIPART URLS
# =========================================================

@app.route(
    "/api/r2/multipart/urls",
    methods=["POST"]
)
def r2_multipart_urls():

    data = request.get_json(
        silent=True
    ) or {}

    upload_id = str(
        data.get("upload_id", "")
    ).strip()

    key = str(
        data.get("key", "")
    ).strip()

    try:
        part_numbers = data.get(
            "part_numbers",
            []
        )

        part_numbers = [
            int(x)
            for x in part_numbers
        ]

    except Exception:

        return jsonify({
            "ok": False,
            "message": "Invalid part numbers."
        }), 400

    if not upload_id or not key:

        return jsonify({
            "ok": False,
            "message":
                "upload_id or key missing."
        }), 400

    if not (
        key.startswith("videos/")
        or key.startswith("posters/")
    ):

        return jsonify({
            "ok": False,
            "message": "Invalid R2 key."
        }), 400

    if len(part_numbers) > 10000:

        return jsonify({
            "ok": False,
            "message":
                "Too many multipart parts."
        }), 400

    try:

        client = get_r2_client()

        urls = []

        for part_number in part_numbers:

            url = client.generate_presigned_url(
                "upload_part",
                Params={
                    "Bucket": R2_BUCKET,
                    "Key": key,
                    "UploadId": upload_id,
                    "PartNumber": part_number
                },
                ExpiresIn=3600
            )

            urls.append({
                "part_number":
                    part_number,
                "url": url
            })

        return jsonify({
            "ok": True,
            "urls": urls
        })

    except Exception as e:

        print(
            "Multipart URL error:",
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

    data = request.get_json(
        silent=True
    ) or {}

    upload_id = str(
        data.get("upload_id", "")
    ).strip()

    key = str(
        data.get("key", "")
    ).strip()

    parts = data.get(
        "parts",
        []
    )

    if not upload_id or not key:

        return jsonify({
            "ok": False,
            "message":
                "upload_id or key missing."
        }), 400

    if not isinstance(parts, list):

        return jsonify({
            "ok": False,
            "message": "Invalid parts."
        }), 400

    if not (
        key.startswith("videos/")
        or key.startswith("posters/")
    ):

        return jsonify({
            "ok": False,
            "message": "Invalid R2 key."
        }), 400

    try:

        normalized_parts = []

        for part in parts:

            normalized_parts.append({
                "ETag": part["etag"]
                if "etag" in part
                else part["ETag"],

                "PartNumber":
                    int(
                        part["part_number"]
                        if "part_number" in part
                        else part["PartNumber"]
                    )
            })

        normalized_parts.sort(
            key=lambda x: x["PartNumber"]
        )

        client = get_r2_client()

        result = client.complete_multipart_upload(
            Bucket=R2_BUCKET,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts":
                    normalized_parts
            }
        )

        public_url = ""

        if R2_PUBLIC_URL:
            public_url = (
                R2_PUBLIC_URL.rstrip("/")
                + "/"
                + quote(
                    key,
                    safe="/"
                )
            )

        return jsonify({
            "ok": True,
            "key": key,
            "url": public_url,
            "result": {
                "etag":
                    result.get("ETag")
            }
        })

    except Exception as e:

        print(
            "Multipart complete error:",
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

    data = request.get_json(
        silent=True
    ) or {}

    upload_id = str(
        data.get("upload_id", "")
    ).strip()

    key = str(
        data.get("key", "")
    ).strip()

    if not upload_id or not key:

        return jsonify({
            "ok": False,
            "message":
                "upload_id or key missing."
        }), 400

    if not (
        key.startswith("videos/")
        or key.startswith("posters/")
    ):

        return jsonify({
            "ok": False,
            "message": "Invalid R2 key."
        }), 400

    try:

        client = get_r2_client()

        client.abort_multipart_upload(
            Bucket=R2_BUCKET,
            Key=key,
            UploadId=upload_id
        )

        return jsonify({
            "ok": True
        })

    except Exception as e:

        print(
            "Multipart abort error:",
            repr(e)
        )

        return jsonify({
            "ok": False,
            "message": str(e)
        }), 500


# =========================================================
# SAVE MOVIE AFTER DIRECT R2 UPLOAD
# =========================================================

@app.route(
    "/api/movie/save",
    methods=["POST"]
)
def save_movie():

    data = request.get_json(
        silent=True
    ) or {}

    title = str(
        data.get("title", "")
    ).strip()

    category = str(
        data.get(
            "category",
            "Hindi"
        )
    ).strip()

    description = str(
        data.get(
            "description",
            ""
        )
    ).strip()

    video_key = str(
        data.get(
            "video_key",
            ""
        )
    ).strip()

    poster_key = str(
        data.get(
            "poster_key",
            ""
        )
    ).strip()

    video_url = str(
        data.get(
            "video_url",
            ""
        )
    ).strip()

    poster_url = str(
        data.get(
            "poster_url",
            ""
        )
    ).strip()

    if not title:

        return jsonify({
            "ok": False,
            "message": "Movie title required."
        }), 400

    if not video_key:

        return jsonify({
            "ok": False,
            "message":
                "Video upload complete nahi hua."
        }), 400

    if not video_key.startswith(
        "videos/"
    ):

        return jsonify({
            "ok": False,
            "message":
                "Invalid video key."
        }), 400

    if poster_key and not poster_key.startswith(
        "posters/"
    ):

        return jsonify({
            "ok": False,
            "message":
                "Invalid poster key."
        }), 400

    try:

        row = db_execute("""
            INSERT INTO movies (
                title,
                category,
                description,
                poster_url,
                video_url,
                video_key,
                poster_key
            )
            VALUES (
                %s, %s, %s, %s,
                %s, %s, %s
            )
            RETURNING id
        """, (
            title,
            category,
            description,
            poster_url,
            video_url,
            video_key,
            poster_key
        ), returning=True)

        return jsonify({
            "ok": True,
            "movie_id":
                row["id"]
        })

    except Exception as e:

        print(
            "Movie save error:",
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
    "/admin/login",
    methods=["GET", "POST"]
)
def admin_login():

    if request.method == "POST":

        username = str(
            request.form.get(
                "username",
                ""
            )
        ).strip()

        password = str(
            request.form.get(
                "password",
                ""
            )
        )

        if (
            secrets.compare_digest(
                username,
                ADMIN_USER
            )
            and
            secrets.compare_digest(
                password,
                ADMIN_PASSWORD
            )
        ):

            session[
                "admin_logged_in"
            ] = True

            return redirect(
                url_for("admin")
            )

        flash(
            "Invalid username or password.",
            "error"
        )

    return render_template(
        "admin_login.html"
    )


# =========================================================
# ADMIN
# =========================================================

@app.route("/admin")
@admin_required
def admin():

    movies = db_fetchall("""
        SELECT *
        FROM movies
        ORDER BY created_at DESC
    """)

    total_movies = len(movies)

    total_views = sum(
        int(
            m.get("views", 0)
            or 0
        )
        for m in movies
    )

    return render_template(
        "admin.html",
        movies=movies,
        total_movies=total_movies,
        total_views=total_views
    )


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
# ADMIN DELETE
# =========================================================

@app.route(
    "/admin/delete/<int:movie_id>",
    methods=["POST", "GET"]
)
@admin_required
def admin_delete(movie_id):

    movie_data = db_fetchone("""
        SELECT *
        FROM movies
        WHERE id = %s
    """, (movie_id,))

    if not movie_data:
        abort(404)

    client = None

    try:
        client = get_r2_client()

        keys = []

        if movie_data.get("video_key"):
            keys.append(
                movie_data["video_key"]
            )

        if movie_data.get("poster_key"):
            keys.append(
                movie_data["poster_key"]
            )

        for key in keys:

            try:

                client.delete_object(
                    Bucket=R2_BUCKET,
                    Key=key
                )

            except Exception as e:

                print(
                    "R2 delete warning:",
                    key,
                    repr(e)
                )

    except Exception as e:

        print(
            "R2 client delete warning:",
            repr(e)
        )

    db_execute("""
        DELETE FROM movies
        WHERE id = %s
    """, (movie_id,))

    flash(
        "Movie deleted successfully.",
        "success"
    )

    return redirect(
        url_for("admin")
    )


# =========================================================
# ADMIN LOGOUT
# =========================================================

@app.route("/admin/logout")
def admin_logout():

    session.pop(
        "admin_logged_in",
        None
    )

    return redirect(
        url_for("admin_login")
    )


# =========================================================
# CASHFREE CREATE ORDER
# =========================================================

def create_cashfree_order(
    movie_id,
    payment_type
):

    if payment_type == "watch":
        amount = 1
        description = (
            "Movie Watch Access - 24 Hours"
        )

    elif payment_type == "download":
        amount = 9
        description = (
            "Movie Download Access"
        )

    elif payment_type == "premium":
        amount = 109
        description = (
            "Tomesh Movies Premium - 1 Year"
        )

    else:
        raise ValueError(
            "Invalid payment type."
        )

    movie_data = db_fetchone("""
        SELECT *
        FROM movies
        WHERE id = %s
    """, (movie_id,))

    if not movie_data:
        raise ValueError(
            "Movie not found."
        )

    cust_id = customer_id()

    order_id = (
        "TM_"
        + payment_type.upper()
        + "_"
        + uuid.uuid4().hex[:24]
    )

    phone = "9999999999"

    return_url = (
        url_for(
            "cashfree_return",
            _external=True
        )
        + "?order_id="
        + quote(
            order_id,
            safe=""
        )
    )

    payload = {
        "order_id": order_id,
        "order_amount": amount,
        "order_currency": "INR",

        "customer_details": {
            "customer_id": cust_id,
            "customer_phone": phone,
        },

        "order_meta": {
            "return_url": return_url
        },

        "order_note": description,

        "order_tags": {
            "movie_id": str(movie_id),
            "payment_type":
                payment_type,
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
            "Cashfree payment_session_id missing."
        )

    db_execute("""
        INSERT INTO payments (
            order_id,
            movie_id,
            customer_id,
            payment_type,
            amount,
            status
        )
        VALUES (
            %s, %s, %s,
            %s, %s, %s
        )
        ON CONFLICT (order_id)
        DO NOTHING
    """, (
        order_id,
        movie_id,
        cust_id,
        payment_type,
        amount,
        "ACTIVE"
    ))

    return {
        "order_id": order_id,
        "payment_session_id":
            payment_session_id,
        "amount": amount,
        "payment_type":
            payment_type
    }


# =========================================================
# CREATE PAYMENT API
# =========================================================

@app.route(
    "/api/payment/create",
    methods=["POST"]
)
def payment_create():

    data = request.get_json(
        silent=True
    ) or {}

    try:

        movie_id = int(
            data.get("movie_id")
        )

    except Exception:

        return jsonify({
            "ok": False,
            "message": "Invalid movie ID."
        }), 400

    payment_type = str(
        data.get(
            "payment_type",
            "watch"
        )
    ).strip().lower()

    try:

        result = create_cashfree_order(
            movie_id,
            payment_type
        )

        return jsonify({
            "ok": True,
            **result,
            "mode":
                CASHFREE_JS_MODE
        })

    except Exception as e:

        print(
            "Payment create error:",
            repr(e)
        )

        return jsonify({
            "ok": False,
            "message": str(e)
        }), 500


# =========================================================
# VERIFY CASHFREE ORDER
# =========================================================

def verify_cashfree_order(order_id):

    result = cashfree_request(
        "GET",
        "/orders/"
        + quote(
            order_id,
            safe=""
        )
    )

    return result


# =========================================================
# CASHFREE RETURN
# =========================================================

@app.route(
    "/cashfree/return",
    methods=["GET"]
)
def cashfree_return():

    order_id = str(
        request.args.get(
            "order_id",
            ""
        )
    ).strip()

    if not order_id:

        return redirect(
            url_for("home")
        )

    try:

        result = verify_cashfree_order(
            order_id
        )

        status = str(
            result.get(
                "order_status",
                ""
            )
        ).upper()

        payment = db_fetchone("""
            SELECT *
            FROM payments
            WHERE order_id = %s
        """, (order_id,))

        if not payment:
            return redirect(
                url_for("home")
            )

        if status == "PAID":

            db_execute("""
                UPDATE payments
                SET status = %s,
                    paid_at = CURRENT_TIMESTAMP
                WHERE order_id = %s
            """, (
                "PAID",
                order_id
            ))

            payment_type = payment[
                "payment_type"
            ]

            movie_id = payment[
                "movie_id"
            ]

            cust_id = payment[
                "customer_id"
            ]

            if payment_type == "watch":

                expires_at = (
                    datetime.now(
                        timezone.utc
                    )
                    + timedelta(
                        hours=24
                    )
                )

                db_execute("""
                    INSERT INTO watch_access (
                        movie_id,
                        customer_id,
                        order_id,
                        expires_at
                    )
                    VALUES (
                        %s, %s, %s, %s
                    )
                    ON CONFLICT (order_id)
                    DO UPDATE SET
                        expires_at = EXCLUDED.expires_at
                """, (
                    movie_id,
                    cust_id,
                    order_id,
                    expires_at
                ))

            elif payment_type == "premium":

                # Premium is stored as a session entitlement.
                session[
                    "premium_until"
                ] = (
                    datetime.now(
                        timezone.utc
                    )
                    + timedelta(
                        days=365
                    )
                ).isoformat()

            return redirect(
                url_for(
                    "movie_page",
                    movie_id=movie_id
                )
            )

        return jsonify({
            "ok": False,
            "order_id": order_id,
            "status": status,
            "message":
                "Payment completed nahi hua."
        })

    except Exception as e:

        print(
            "Cashfree return error:",
            repr(e)
        )

        return jsonify({
            "ok": False,
            "message": str(e)
        }), 500


# =========================================================
# CASHFREE WEBHOOK
# =========================================================

@app.route(
    "/cashfree/webhook",
    methods=["POST"]
)
def cashfree_webhook():

    try:

        data = request.get_json(
            silent=True
        ) or {}

        order_data = data.get(
            "data",
            {}
        )

        order = order_data.get(
            "order",
            {}
        )

        order_id = str(
            order.get(
                "order_id",
                ""
            )
        ).strip()

        if not order_id:

            return jsonify({
                "ok": True
            })

        result = verify_cashfree_order(
            order_id
        )

        status = str(
            result.get(
                "order_status",
                ""
            )
        ).upper()

        if status != "PAID":

            return jsonify({
                "ok": True
            })

        payment = db_fetchone("""
            SELECT *
            FROM payments
            WHERE order_id = %s
        """, (order_id,))

        if not payment:

            return jsonify({
                "ok": True
            })

        db_execute("""
            UPDATE payments
            SET status = %s,
                paid_at = CURRENT_TIMESTAMP
            WHERE order_id = %s
        """, (
            "PAID",
            order_id
        ))

        if payment[
            "payment_type"
        ] == "watch":

            expires_at = (
                datetime.now(
                    timezone.utc
                )
                + timedelta(
                    hours=24
                )
            )

            db_execute("""
                INSERT INTO watch_access (
                    movie_id,
                    customer_id,
                    order_id,
                    expires_at
                )
                VALUES (
                    %s, %s, %s, %s
                )
                ON CONFLICT (order_id)
                DO UPDATE SET
                    expires_at =
                        EXCLUDED.expires_at
            """, (
                payment["movie_id"],
                payment["customer_id"],
                order_id,
                expires_at
            ))

        return jsonify({
            "ok": True
        })

    except Exception as e:

        print(
            "Webhook error:",
            repr(e)
        )

        return jsonify({
            "ok": False,
            "message": str(e)
        }), 500


# =========================================================
# ADS.TXT
# =========================================================

@app.route("/ads.txt")
def ads_txt():

    return Response(
        "google.com, "
        "pub-8697157365303435, "
        "DIRECT, "
        "f08c47fec0942fa0\n",
        mimetype="text/plain"
    )


# =========================================================
# ERROR HANDLERS
# =========================================================

@app.errorhandler(413)
def too_large(error):

    return jsonify({
        "ok": False,
        "message":
            "File 4 GB se bada hai."
    }), 413


@app.errorhandler(404)
def not_found(error):

    if request.path.startswith("/api/"):
        return jsonify({
            "ok": False,
            "message": "Not found."
        }), 404

    return render_template(
        "404.html"
    ), 404


@app.errorhandler(500)
def internal_error(error):

    print(
        "Internal Server Error:",
        repr(error)
    )

    if request.path.startswith("/api/"):
        return jsonify({
            "ok": False,
            "message":
                "Internal server error."
        }), 500

    return render_template(
        "500.html"
    ), 500


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                5000
            )
        ),
        debug=False
    )
