import os
import re
import secrets
import mimetypes
from functools import wraps
from urllib.parse import quote

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

# Browser -> R2 direct upload means this is mainly for normal
# form requests, not the large movie upload itself.
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024


# ============================================================
# CONFIG
# ============================================================

ADMIN_USER = (
    os.environ.get("ADMIN_USER", "admin").strip()
    or "admin"
)

ADMIN_PASSWORD = (
    os.environ.get("ADMIN_PASSWORD", "change-me-now").strip()
    or "change-me-now"
)

DATABASE_URL = (
    os.environ.get("DATABASE_URL", "").strip()
)


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
# R2 MULTIPART SETTINGS
# ============================================================

PART_SIZE = 10 * 1024 * 1024
PARALLEL_PARTS = 3
PRESIGNED_EXPIRES = 3600
MAX_MULTIPART_PARTS = 10000

VIDEO_PREFIX = "videos/"
POSTER_PREFIX = "posters/"


# ============================================================
# ENV CLEANING
# ============================================================

def clean_env_value(value):
    """
    Removes accidental spaces, CR/LF and wrapping quotes from
    Render environment variables.

    This is important because an accidental newline in an AWS/R2
    credential can create an Invalid header value error.
    """
    if value is None:
        return ""

    value = str(value)

    # Remove actual CR/LF
    value = value.replace("\r", "").replace("\n", "")

    # Remove surrounding spaces/tabs
    value = value.strip()

    # Remove wrapping quotes
    if len(value) >= 2:
        if (
            (value.startswith('"') and value.endswith('"'))
            or
            (value.startswith("'") and value.endswith("'"))
        ):
            value = value[1:-1].strip()

    # Remove accidental CR/LF again
    value = value.replace("\r", "").replace("\n", "")

    return value


def clean_endpoint(value):
    """
    Normalizes Cloudflare R2 S3 endpoint.

    Expected:
    https://ACCOUNT_ID.r2.cloudflarestorage.com

    Do NOT put /tomesh-movies at the end.
    """
    value = clean_env_value(value)

    if not value:
        return ""

    value = value.rstrip("/")

    # Remove accidental bucket path if someone pasted it.
    bucket_path = "/" + clean_env_value(
        os.environ.get("R2_BUCKET", "tomesh-movies")
    )

    if value.lower().endswith(bucket_path.lower()):
        value = value[: -len(bucket_path)].rstrip("/")

    return value


R2_ACCOUNT_ID = clean_env_value(
    os.environ.get("R2_ACCOUNT_ID", "")
)

R2_ACCESS_KEY_ID = clean_env_value(
    os.environ.get("R2_ACCESS_KEY_ID", "")
)

R2_SECRET_ACCESS_KEY = clean_env_value(
    os.environ.get("R2_SECRET_ACCESS_KEY", "")
)

R2_BUCKET = (
    clean_env_value(
        os.environ.get("R2_BUCKET", "tomesh-movies")
    )
    or "tomesh-movies"
)

R2_ENDPOINT = clean_endpoint(
    os.environ.get("R2_ENDPOINT", "")
)

R2_PUBLIC_URL = clean_env_value(
    os.environ.get("R2_PUBLIC_URL", "")
).rstrip("/")


# ============================================================
# BASIC HELPERS
# ============================================================

def json_error(message, status=400, **extra):
    data = {
        "ok": False,
        "error": message,
    }
    data.update(extra)
    return jsonify(data), status


def json_ok(**data):
    data.setdefault("ok", True)
    return jsonify(data)


def get_extension(filename):
    if not filename:
        return ""

    filename = secure_filename(str(filename))

    if "." not in filename:
        return ""

    return filename.rsplit(".", 1)[1].lower()


def allowed_video(filename):
    return get_extension(filename) in ALLOWED_VIDEOS


def allowed_poster(filename):
    return get_extension(filename) in ALLOWED_POSTERS


def safe_filename(filename):
    """
    Keeps the original extension but creates a safe unique filename.
    """
    original = secure_filename(filename or "file")

    ext = get_extension(original)

    if ext:
        return f"{secrets.token_hex(16)}.{ext}"

    return secrets.token_hex(16)


def content_type_for_key(key):
    """
    Returns a sensible Content-Type for R2 uploads.
    """
    key = str(key or "")

    guessed, _ = mimetypes.guess_type(key)

    if guessed:
        return guessed

    ext = get_extension(key)

    if ext == "mkv":
        return "video/x-matroska"

    if ext == "webm":
        return "video/webm"

    if ext == "mov":
        return "video/quicktime"

    if ext == "mp4":
        return "video/mp4"

    if ext in {"jpg", "jpeg"}:
        return "image/jpeg"

    if ext == "png":
        return "image/png"

    if ext == "webp":
        return "image/webp"

    return "application/octet-stream"


# ============================================================
# DATABASE
# ============================================================

def get_db(dict_rows=False):
    """
    PostgreSQL connection.

    dict_rows=True is important because templates use:
        movie.id
        movie.title
        movie.views

    Normal psycopg2 cursors return tuples, which caused:
        'tuple object' has no attribute 'views'
    """
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is missing in Render environment variables."
        )

    if dict_rows:
        return psycopg2.connect(
            DATABASE_URL,
            cursor_factory=RealDictCursor,
        )

    return psycopg2.connect(DATABASE_URL)


def init_db():
    """
    Creates the required tables if they do not exist.
    """
    conn = get_db()

    try:
        cur = conn.cursor()

        cur.execute(
            """
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
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )

        conn.commit()
        cur.close()

    finally:
        conn.close()


def get_settings():
    """
    Returns settings as a dictionary.
    """
    conn = get_db(dict_rows=True)

    try:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT key, value
            FROM settings
            """
        )

        rows = cur.fetchall()
        cur.close()

    finally:
        conn.close()

    settings = {}

    for row in rows:
        settings[row["key"]] = row["value"] or ""

    return settings


def get_ads():
    """
    Converts DB settings into the names expected by templates.
    """
    settings = get_settings()

    return {
        "ad_top": settings.get("ad_top", ""),
        "ad_player": settings.get("ad_player", ""),
        "ad_bottom": settings.get("ad_bottom", ""),
    }


# ============================================================
# R2 CLIENT
# ============================================================

def validate_r2_config():
    missing = []

    if not R2_ACCOUNT_ID:
        missing.append("R2_ACCOUNT_ID")

    if not R2_ACCESS_KEY_ID:
        missing.append("R2_ACCESS_KEY_ID")

    if not R2_SECRET_ACCESS_KEY:
        missing.append("R2_SECRET_ACCESS_KEY")

    if not R2_BUCKET:
        missing.append("R2_BUCKET")

    if not R2_ENDPOINT:
        missing.append("R2_ENDPOINT")

    if missing:
        raise RuntimeError(
            "Missing R2 environment variables: "
            + ", ".join(missing)
        )

    if "\n" in R2_ENDPOINT or "\r" in R2_ENDPOINT:
        raise RuntimeError(
            "R2_ENDPOINT contains a newline."
        )

    if "/tomesh-movies" in R2_ENDPOINT.lower():
        raise RuntimeError(
            "R2_ENDPOINT must not end with /tomesh-movies. "
            "Use only the R2 S3 endpoint."
        )


def get_r2_client():
    validate_r2_config()

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
                "max_attempts": 4,
                "mode": "standard",
            },
        ),
    )


# ============================================================
# R2 URL HELPERS
# ============================================================

def r2_public_url(key):
    """
    Converts a stored R2 object key such as:

        videos/movie.mp4

    into:

        https://PUBLIC-R2-URL/videos/movie.mp4

    IMPORTANT:
    If the value is already a complete HTTP/HTTPS URL,
    it is returned unchanged.
    """
    if not key:
        return None

    key = str(key).strip()

    if key.startswith("http://") or key.startswith("https://"):
        return key

    if not R2_PUBLIC_URL:
        raise RuntimeError(
            "R2_PUBLIC_URL is missing in Render environment variables."
        )

    key = key.lstrip("/")

    return (
        R2_PUBLIC_URL
        + "/"
        + quote(key, safe="/")
    )


def media_url(value):
    """
    Safe media URL builder.

    This is the important fix for the previous request:

        /movie/videos/filename.mp4

    The movie page will now receive a full R2 URL.
    """
    if not value:
        return None

    value = str(value).strip()

    if value.startswith("http://") or value.startswith("https://"):
        return value

    return r2_public_url(value)


def r2_presigned_url(key, expires=PRESIGNED_EXPIRES):
    if not key:
        return None

    key = str(key).strip()

    if key.startswith("http://") or key.startswith("https://"):
        return key

    client = get_r2_client()

    return client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": R2_BUCKET,
            "Key": key,
        },
        ExpiresIn=expires,
    )


def r2_delete(key):
    if not key:
        return False

    key = str(key).strip()

    if key.startswith("http://") or key.startswith("https://"):
        return False

    client = get_r2_client()

    client.delete_object(
        Bucket=R2_BUCKET,
        Key=key,
    )

    return True


def r2_head(key):
    if not key:
        return None

    key = str(key).strip()

    if key.startswith("http://") or key.startswith("https://"):
        return None

    client = get_r2_client()

    return client.head_object(
        Bucket=R2_BUCKET,
        Key=key,
    )


# ============================================================
# R2 KEY VALIDATION
# ============================================================

def valid_r2_key(key):
    if not key:
        return False

    key = str(key).strip()

    if len(key) > 1024:
        return False

    if key.startswith("http://") or key.startswith("https://"):
        return False

    if key.startswith(VIDEO_PREFIX):
        return allowed_video(key)

    if key.startswith(POSTER_PREFIX):
        return allowed_poster(key)

    return False


def valid_video_key(key):
    if not key:
        return False

    key = str(key).strip()

    return (
        key.startswith(VIDEO_PREFIX)
        and allowed_video(key)
    )


def valid_poster_key(key):
    if not key:
        return False

    key = str(key).strip()

    return (
        key.startswith(POSTER_PREFIX)
        and allowed_poster(key)
    )


# ============================================================
# LOGIN
# ============================================================

def is_logged_in():
    return bool(session.get("admin_logged_in"))


def admin_required():
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if not is_logged_in():
                return redirect(
                    url_for("login", next=request.path)
                )

            return func(*args, **kwargs)

        return wrapper

    return decorator


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():
    q = request.args.get("q", "").strip()
    selected_category = request.args.get(
        "category",
        ""
    ).strip()

    conn = get_db(dict_rows=True)

    try:
        cur = conn.cursor()

        if q and selected_category:
            cur.execute(
                """
                SELECT *
                FROM movies
                WHERE
                    (
                        title ILIKE %s
                        OR description ILIKE %s
                    )
                    AND category = %s
                ORDER BY id DESC
                """,
                (
                    f"%{q}%",
                    f"%{q}%",
                    selected_category,
                ),
            )

        elif q:
            cur.execute(
                """
                SELECT *
                FROM movies
                WHERE
                    title ILIKE %s
                    OR description ILIKE %s
                ORDER BY id DESC
                """,
                (
                    f"%{q}%",
                    f"%{q}%",
                ),
            )

        elif selected_category:
            cur.execute(
                """
                SELECT *
                FROM movies
                WHERE category = %s
                ORDER BY id DESC
                """,
                (selected_category,),
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

        cur.execute(
            """
            SELECT DISTINCT category
            FROM movies
            WHERE category IS NOT NULL
              AND TRIM(category) <> ''
            ORDER BY category
            """
        )

        category_rows = cur.fetchall()

        cur.close()

    finally:
        conn.close()

    categories = [
        row["category"]
        for row in category_rows
    ]

    ads = get_ads()

    return render_template(
        "index.html",
        movies=movies,
        categories=categories,
        ads=ads,
        q=q,
        selected_category=selected_category,
    )


# ============================================================
# MOVIE PAGE
# ============================================================

@app.route("/movie/<int:movie_id>")
def movie_page(movie_id):
    conn = get_db(dict_rows=True)

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

        # Increase view count
        cur.execute(
            """
            UPDATE movies
            SET views = COALESCE(views, 0) + 1
            WHERE id = %s
            """,
            (movie_id,),
        )

        conn.commit()

        cur.close()

    finally:
        conn.close()

    # Keep template compatible with movie.html
    # and provide FULL R2 URLs.
    movie["video_url"] = media_url(
        movie.get("video")
    )

    movie["poster_url"] = media_url(
        movie.get("poster")
    )

    # Reflect the increment immediately
    movie["views"] = (
        int(movie.get("views") or 0) + 1
    )

    ads = get_ads()

    return render_template(
        "movie.html",
        movie=movie,
        ads=ads,
    )


# ============================================================
# LEGACY / DIRECT MEDIA ROUTES
# ============================================================

@app.route("/poster/<path:name>")
def poster(name):
    try:
        return redirect(
            r2_presigned_url(
                name,
                expires=3600,
            )
        )

    except Exception as exc:
        app.logger.exception(
            "Poster URL error: %s",
            exc,
        )
        abort(404)


@app.route("/video/<path:name>")
def video(name):
    try:
        return redirect(
            r2_presigned_url(
                name,
                expires=3600,
            )
        )

    except Exception as exc:
        app.logger.exception(
            "Video URL error: %s",
            exc,
        )
        abort(404)


# ============================================================
# ADS.TXT
# ============================================================

@app.route("/ads.txt")
def ads_txt():
    return Response(
        "google.com, pub-8697157365303435, DIRECT, f08c47fec0942fa0\n",
        mimetype="text/plain",
    )


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        cur.close()
        conn.close()

        return jsonify({
            "ok": True,
            "database": "OK",
        })

    except Exception as exc:
        app.logger.exception(
            "Health check failed: %s",
            exc,
        )

        return jsonify({
            "ok": False,
            "error": str(exc),
        }), 500


@app.route("/r2-health")
def r2_health():
    try:
        client = get_r2_client()

        client.head_bucket(
            Bucket=R2_BUCKET
        )

        return jsonify({
            "ok": True,
            "message": "R2 OK",
            "bucket": R2_BUCKET,
        })

    except Exception as exc:
        app.logger.exception(
            "R2 health failed: %s",
            exc,
        )

        return jsonify({
            "ok": False,
            "error": str(exc),
        }), 500


# ============================================================
# LOGIN
# ============================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    if is_logged_in():
        return redirect(url_for("admin"))

    if request.method == "POST":
        username = (
            request.form.get("username", "")
            .strip()
        )

        password = (
            request.form.get("password", "")
        )

        if (
            secrets.compare_digest(
                username,
                ADMIN_USER,
            )
            and
            secrets.compare_digest(
                password,
                ADMIN_PASSWORD,
            )
        ):
            session.clear()
            session["admin_logged_in"] = True
            session["admin_user"] = username

            next_url = (
                request.args.get("next")
                or url_for("admin")
            )

            return redirect(next_url)

        flash(
            "Invalid username or password.",
            "error",
        )

    return render_template("login.html")


# ============================================================
# LOGOUT
# ============================================================

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ============================================================
# ADMIN DASHBOARD
# ============================================================

@app.route("/admin")
@admin_required()
def admin():
    conn = get_db(dict_rows=True)

    try:
        cur = conn.cursor()

        # IMPORTANT:
        # RealDictCursor prevents:
        # 'tuple object' has no attribute 'views'
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
                COALESCE(SUM(views), 0) AS total_views
            FROM movies
            """
        )

        stats = cur.fetchone()

        cur.close()

    finally:
        conn.close()

    settings = get_settings()

    total_movies = int(
        stats["total_movies"] or 0
    )

    total_views = int(
        stats["total_views"] or 0
    )

    return render_template(
        "admin.html",
        movies=movies,
        settings=settings,
        total_movies=total_movies,
        total_views=total_views,
    )


# ============================================================
# ADMIN ADD MOVIE - OPTIONAL SERVER FORM
# ============================================================

@app.route("/admin/add", methods=["GET", "POST"])
@admin_required()
def admin_add():
    if request.method == "GET":
        return render_template(
            "admin_add.html",
            movie=None,
        )

    title = (
        request.form.get("title", "")
        .strip()
    )

    category = (
        request.form.get("category", "")
        .strip()
    )

    description = (
        request.form.get("description", "")
        .strip()
    )

    poster_file = request.files.get("poster")
    video_file = request.files.get("video")

    if not title:
        flash(
            "Movie title is required.",
            "error",
        )
        return redirect(
            url_for("admin_add")
        )

    # This legacy server-side route is not recommended
    # for large videos on Render. Direct R2 APIs below
    # should be used by the Admin Panel.
    if not video_file:
        flash(
            "Video file is required.",
            "error",
        )
        return redirect(
            url_for("admin_add")
        )

    if not allowed_video(
        video_file.filename
    ):
        flash(
            "Video केवल MP4, MKV, WebM या MOV होनी चाहिए.",
            "error",
        )
        return redirect(
            url_for("admin_add")
        )

    poster_key = None
    video_key = None

    try:
        client = get_r2_client()

        if poster_file and poster_file.filename:
            if not allowed_poster(
                poster_file.filename
            ):
                flash(
                    "Poster JPG, JPEG, PNG या WEBP होना चाहिए.",
                    "error",
                )
                return redirect(
                    url_for("admin_add")
                )

            poster_key = (
                POSTER_PREFIX
                + safe_filename(
                    poster_file.filename
                )
            )

            client.upload_fileobj(
                poster_file,
                R2_BUCKET,
                poster_key,
                ExtraArgs={
                    "ContentType":
                        content_type_for_key(
                            poster_key
                        )
                },
            )

        video_key = (
            VIDEO_PREFIX
            + safe_filename(
                video_file.filename
            )
        )

        client.upload_fileobj(
            video_file,
            R2_BUCKET,
            video_key,
            ExtraArgs={
                "ContentType":
                    content_type_for_key(
                        video_key
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
                        video,
                        views
                    )
                VALUES
                    (%s, %s, %s, %s, %s, 0)
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
            "Movie added successfully.",
            "success",
        )

    except Exception as exc:
        app.logger.exception(
            "Admin add failed: %s",
            exc,
        )

        if video_key:
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
            f"Movie upload failed: {exc}",
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
@admin_required()
def r2_multipart_create():
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
                "",
            )
        ).strip()

        if not key:
            return json_error(
                "R2 object key is required."
            )

        if not valid_r2_key(key):
            return json_error(
                "Invalid R2 key."
            )

        if (
            not key.startswith(VIDEO_PREFIX)
            and
            not key.startswith(POSTER_PREFIX)
        ):
            return json_error(
                "Invalid object prefix."
            )

        if not content_type:
            content_type = (
                content_type_for_key(key)
            )

        client = get_r2_client()

        result = client.create_multipart_upload(
            Bucket=R2_BUCKET,
            Key=key,
            ContentType=content_type,
        )

        upload_id = result.get(
            "UploadId"
        )

        if not upload_id:
            return json_error(
                "R2 did not return UploadId.",
                500,
            )

        return json_ok(
            key=key,
            upload_id=upload_id,
            part_size=PART_SIZE,
            parallel=PARALLEL_PARTS,
            expires=PRESIGNED_EXPIRES,
        )

    except Exception as exc:
        app.logger.exception(
            "R2 multipart create failed: %s",
            exc,
        )

        return json_error(
            f"R2 multipart create failed: {exc}",
            500,
        )


# ============================================================
# R2 MULTIPART PRESIGNED PART URLS
# ============================================================

@app.route(
    "/api/r2/multipart/urls",
    methods=["POST"],
)
@admin_required()
def r2_multipart_urls():
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

        raw_parts = data.get(
            "part_numbers"
        )

        if raw_parts is None:
            raw_parts = data.get(
                "parts"
            )

        if not key:
            return json_error(
                "key is required."
            )

        if not upload_id:
            return json_error(
                "upload_id is required."
            )

        if not valid_r2_key(key):
            return json_error(
                "Invalid R2 key."
            )

        if not isinstance(
            raw_parts,
            list,
        ):
            return json_error(
                "part_numbers must be a list."
            )

        part_numbers = []

        for value in raw_parts:
            try:
                number = int(value)
            except Exception:
                continue

            if number < 1:
                continue

            if number > MAX_MULTIPART_PARTS:
                continue

            part_numbers.append(number)

        part_numbers = sorted(
            set(part_numbers)
        )

        if not part_numbers:
            return json_error(
                "No valid part numbers."
            )

        if len(part_numbers) > MAX_MULTIPART_PARTS:
            return json_error(
                "Too many parts."
            )

        client = get_r2_client()

        urls = {}

        for part_number in part_numbers:
            url = client.generate_presigned_url(
                "upload_part",
                Params={
                    "Bucket": R2_BUCKET,
                    "Key": key,
                    "UploadId": upload_id,
                    "PartNumber": part_number,
                },
                ExpiresIn=PRESIGNED_EXPIRES,
            )

            urls[str(part_number)] = url

        return json_ok(
            key=key,
            upload_id=upload_id,
            urls=urls,
            part_size=PART_SIZE,
            expires=PRESIGNED_EXPIRES,
        )

    except Exception as exc:
        app.logger.exception(
            "R2 multipart URL generation failed: %s",
            exc,
        )

        return json_error(
            f"R2 multipart URL generation failed: {exc}",
            500,
        )


# ============================================================
# R2 MULTIPART COMPLETE
# ============================================================

@app.route(
    "/api/r2/multipart/complete",
    methods=["POST"],
)
@admin_required()
def r2_multipart_complete():
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

        parts = data.get("parts")

        if not key:
            return json_error(
                "key is required."
            )

        if not upload_id:
            return json_error(
                "upload_id is required."
            )

        if not valid_r2_key(key):
            return json_error(
                "Invalid R2 key."
            )

        if not isinstance(parts, list):
            return json_error(
                "parts must be a list."
            )

        cleaned_parts = []

        for part in parts:
            if not isinstance(part, dict):
                continue

            try:
                part_number = int(
                    part.get(
                        "PartNumber",
                        part.get(
                            "part_number"
                        ),
                    )
                )
            except Exception:
                continue

            etag = (
                part.get("ETag")
                or
                part.get("etag")
            )

            if not etag:
                continue

            cleaned_parts.append(
                {
                    "PartNumber":
                        part_number,
                    "ETag":
                        str(etag),
                }
            )

        cleaned_parts.sort(
            key=lambda x: x["PartNumber"]
        )

        if not cleaned_parts:
            return json_error(
                "No valid completed parts."
            )

        # Validate part sequence
        seen = set()

        for part in cleaned_parts:
            number = part["PartNumber"]

            if number in seen:
                return json_error(
                    "Duplicate part number."
                )

            seen.add(number)

            if number < 1:
                return json_error(
                    "Invalid part number."
                )

            if number > MAX_MULTIPART_PARTS:
                return json_error(
                    "Part number too large."
                )

        client = get_r2_client()

        result = client.complete_multipart_upload(
            Bucket=R2_BUCKET,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": cleaned_parts
            },
        )

        location = result.get(
            "Location"
        )

        return json_ok(
            key=key,
            upload_id=upload_id,
            location=location,
            url=r2_public_url(key),
        )

    except Exception as exc:
        app.logger.exception(
            "R2 multipart complete failed: %s",
            exc,
        )

        return json_error(
            f"R2 multipart complete failed: {exc}",
            500,
        )


# ============================================================
# R2 MULTIPART ABORT
# ============================================================

@app.route(
    "/api/r2/multipart/abort",
    methods=["POST"],
)
@admin_required()
def r2_multipart_abort():
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

        if not key:
            return json_error(
                "key is required."
            )

        if not upload_id:
            return json_error(
                "upload_id is required."
            )

        if not valid_r2_key(key):
            return json_error(
                "Invalid R2 key."
            )

        client = get_r2_client()

        client.abort_multipart_upload(
            Bucket=R2_BUCKET,
            Key=key,
            UploadId=upload_id,
        )

        return json_ok(
            message="Multipart upload aborted."
        )

    except Exception as exc:
        app.logger.exception(
            "R2 multipart abort failed: %s",
            exc,
        )

        return json_error(
            f"R2 multipart abort failed: {exc}",
            500,
        )


# ============================================================
# R2 OBJECT DELETE
# ============================================================

@app.route(
    "/api/r2/object/delete",
    methods=["POST"],
)
@admin_required()
def r2_object_delete():
    try:
        data = request.get_json(
            silent=True
        ) or {}

        key = str(
            data.get("key", "")
        ).strip()

        if not key:
            return json_error(
                "key is required."
            )

        if not valid_r2_key(key):
            return json_error(
                "Invalid R2 key."
            )

        r2_delete(key)

        return json_ok(
            message="R2 object deleted.",
            key=key,
        )

    except Exception as exc:
        app.logger.exception(
            "R2 object delete failed: %s",
            exc,
        )

        return json_error(
            f"R2 object delete failed: {exc}",
            500,
        )


# ============================================================
# SAVE MOVIE AFTER DIRECT R2 UPLOAD
# ============================================================

@app.route(
    "/api/movie/save",
    methods=["POST"],
)
@admin_required()
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
            return json_error(
                "Movie title is required."
            )

        if not video:
            return json_error(
                "Video key is required."
            )

        if not valid_video_key(video):
            return json_error(
                "Invalid video R2 key."
            )

        if poster:
            if not valid_poster_key(poster):
                return json_error(
                    "Invalid poster R2 key."
                )

        # Make sure uploaded video actually exists.
        try:
            r2_head(video)
        except Exception as exc:
            return json_error(
                f"Video is not available in R2: {exc}",
                400,
            )

        if poster:
            try:
                r2_head(poster)
            except Exception:
                # Poster can be optional if the UI allows it.
                pass

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
                        views
                    )
                VALUES
                    (%s, %s, %s, %s, %s, 0)
                RETURNING id
                """,
                (
                    title,
                    category,
                    description,
                    poster or None,
                    video,
                ),
            )

            row = cur.fetchone()

            movie_id = (
                row[0]
                if row
                else None
            )

            conn.commit()
            cur.close()

        finally:
            conn.close()

        return json_ok(
            message="Movie saved successfully.",
            movie_id=movie_id,
            video=video,
            poster=poster or None,
            video_url=media_url(video),
            poster_url=(
                media_url(poster)
                if poster
                else None
            ),
        )

    except Exception as exc:
        app.logger.exception(
            "Movie save failed: %s",
            exc,
        )

        return json_error(
            f"Movie save failed: {exc}",
            500,
        )


# ============================================================
# DELETE MOVIE
# ============================================================

@app.route(
    "/admin/delete/<int:movie_id>",
    methods=["POST", "GET"],
)
@admin_required()
def delete_movie(movie_id):
    poster_key = None
    video_key = None

    conn = get_db(dict_rows=True)

    try:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT poster, video
            FROM movies
            WHERE id = %s
            """,
            (movie_id,),
        )

        movie = cur.fetchone()

        if not movie:
            cur.close()
            conn.close()

            flash(
                "Movie not found.",
                "error",
            )

            return redirect(
                url_for("admin")
            )

        poster_key = movie.get(
            "poster"
        )

        video_key = movie.get(
            "video"
        )

        cur.execute(
            """
            DELETE FROM movies
            WHERE id = %s
            """,
            (movie_id,),
        )

        conn.commit()
        cur.close()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

    # Delete R2 files after DB deletion.
    if video_key:
        try:
            r2_delete(video_key)
        except Exception as exc:
            app.logger.exception(
                "Video R2 delete failed: %s",
                exc,
            )

    if poster_key:
        try:
            r2_delete(poster_key)
        except Exception as exc:
            app.logger.exception(
                "Poster R2 delete failed: %s",
                exc,
            )

    flash(
        "Movie deleted successfully.",
        "success",
    )

    return redirect(
        url_for("admin")
    )


# ============================================================
# ADS SETTINGS
# ============================================================

@app.route(
    "/admin/ads",
    methods=["GET", "POST"],
)
@admin_required()
def admin_ads():
    if request.method == "POST":
        ad_top = (
            request.form.get(
                "ad_top",
                "",
            )
            .strip()
        )

        ad_player = (
            request.form.get(
                "ad_player",
                "",
            )
            .strip()
        )

        ad_bottom = (
            request.form.get(
                "ad_bottom",
                "",
            )
            .strip()
        )

        conn = get_db()

        try:
            cur = conn.cursor()

            values = {
                "ad_top": ad_top,
                "ad_player": ad_player,
                "ad_bottom": ad_bottom,
            }

            for key, value in values.items():
                cur.execute(
                    """
                    INSERT INTO settings
                        (key, value)
                    VALUES
                        (%s, %s)
                    ON CONFLICT (key)
                    DO UPDATE SET
                        value = EXCLUDED.value
                    """,
                    (
                        key,
                        value,
                    ),
                )

            conn.commit()
            cur.close()

        finally:
            conn.close()

        flash(
            "Ads settings saved.",
            "success",
        )

        return redirect(
            url_for("admin_ads")
        )

    settings = get_settings()

    return render_template(
        "ads.html",
        settings=settings,
    )


# ============================================================
# 404
# ============================================================

@app.errorhandler(404)
def not_found(error):
    if request.path.startswith("/api/"):
        return json_error(
            "Not found.",
            404,
        )

    return (
        render_template(
            "404.html"
        ),
        404,
    )


# ============================================================
# 413
# ============================================================

@app.errorhandler(413)
def request_too_large(error):
    if request.path.startswith("/api/"):
        return json_error(
            "Request is too large.",
            413,
        )

    return (
        "File too large.",
        413,
    )


# ============================================================
# 500
# ============================================================

@app.errorhandler(500)
def internal_error(error):
    app.logger.exception(
        "Internal server error: %s",
        error,
    )

    if request.path.startswith("/api/"):
        return json_error(
            "Internal server error.",
            500,
        )

    return (
        "Internal Server Error",
        500,
    )


# ============================================================
# STARTUP
# ============================================================

try:
    init_db()
except Exception as startup_error:
    # Do not prevent Gunicorn from starting if DB is temporarily
    # unavailable during deployment. Requests will show the real
    # DB error instead.
    app.logger.exception(
        "Database initialization failed: %s",
        startup_error,
    )


# ============================================================
# LOCAL RUN
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
