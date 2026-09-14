import os
import math
import mimetypes
import secrets
import time
from functools import wraps
from urllib.parse import quote

import boto3
import psycopg2
from psycopg2.extras import RealDictCursor
from botocore.client import Config
from botocore.exceptions import ClientError

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
    "tomesh-movies-change-this-secret-key"
)

# 4 GB maximum request size
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024


# =========================================================
# ENVIRONMENT
# =========================================================

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me-now")

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "").strip()
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()

R2_BUCKET = os.environ.get(
    "R2_BUCKET",
    "tomesh-movies"
).strip()

R2_ENDPOINT = os.environ.get(
    "R2_ENDPOINT",
    f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
).strip()

R2_PUBLIC_URL = os.environ.get(
    "R2_PUBLIC_URL",
    ""
).strip().rstrip("/")


# =========================================================
# FILE SETTINGS
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


# =========================================================
# R2 MULTIPART SETTINGS
# =========================================================

PART_SIZE = 10 * 1024 * 1024       # 10 MB
MAX_PARTS = 10000
PARALLEL_PARTS = 3
PRESIGNED_EXPIRES = 3600


# =========================================================
# BASIC HELPERS
# =========================================================

def allowed_file(filename, allowed_extensions):
    if not filename:
        return False

    filename = filename.lower().strip()

    if "." not in filename:
        return False

    ext = filename.rsplit(".", 1)[1]

    return ext in allowed_extensions


def file_extension(filename):
    if not filename or "." not in filename:
        return ""

    return filename.rsplit(".", 1)[1].lower().strip()


def get_video_mime(filename):
    ext = file_extension(filename)

    mime_map = {
        "mp4": "video/mp4",
        "mkv": "video/x-matroska",
        "webm": "video/webm",
        "mov": "video/quicktime",
    }

    return mime_map.get(
        ext,
        mimetypes.guess_type(filename)[0] or "application/octet-stream"
    )


def get_poster_mime(filename):
    ext = file_extension(filename)

    mime_map = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }

    return mime_map.get(
        ext,
        mimetypes.guess_type(filename)[0] or "application/octet-stream"
    )


# =========================================================
# DATABASE
# =========================================================

def get_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")

    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor,
        connect_timeout=15,
    )


def init_db():
    """
    Initialize PostgreSQL.

    Important:
    Existing old ads table may have an incompatible schema.
    This function automatically detects it and renames it to
    ads_legacy_<random>, then creates the correct ads table.

    No Render Shell is required.
    """

    db = get_db()

    try:
        cur = db.cursor()

        # -------------------------------------------------
        # MOVIES TABLE
        # -------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS movies (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                category TEXT DEFAULT 'Other',
                description TEXT DEFAULT '',
                poster TEXT DEFAULT '',
                video TEXT DEFAULT '',
                views INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # -------------------------------------------------
        # MOVIES TABLE - SAFE COLUMN REPAIR
        # -------------------------------------------------

        movie_columns = {
            row["column_name"]
            for row in cur.fetchall()
        } if False else set()

        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'movies'
        """)

        movie_columns = {
            row["column_name"]
            for row in cur.fetchall()
        }

        if "category" not in movie_columns:
            cur.execute("""
                ALTER TABLE movies
                ADD COLUMN category TEXT DEFAULT 'Other'
            """)

        if "description" not in movie_columns:
            cur.execute("""
                ALTER TABLE movies
                ADD COLUMN description TEXT DEFAULT ''
            """)

        if "poster" not in movie_columns:
            cur.execute("""
                ALTER TABLE movies
                ADD COLUMN poster TEXT DEFAULT ''
            """)

        if "video" not in movie_columns:
            cur.execute("""
                ALTER TABLE movies
                ADD COLUMN video TEXT DEFAULT ''
            """)

        if "views" not in movie_columns:
            cur.execute("""
                ALTER TABLE movies
                ADD COLUMN views INTEGER DEFAULT 0
            """)

        if "created_at" not in movie_columns:
            cur.execute("""
                ALTER TABLE movies
                ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            """)

        # -------------------------------------------------
        # ADS TABLE - IMPORTANT FIX
        # -------------------------------------------------

        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'ads'
        """)

        ads_columns = {
            row["column_name"]
            for row in cur.fetchall()
        }

        # If ads exists but is the OLD/WRONG structure,
        # preserve it as a legacy table.
        if ads_columns and not {"slot", "code"}.issubset(ads_columns):

            legacy_name = (
                f"ads_legacy_"
                f"{int(time.time())}_"
                f"{secrets.token_hex(3)}"
            )

            cur.execute(
                f'ALTER TABLE "ads" RENAME TO "{legacy_name}"'
            )

            print(
                f"Old incompatible ads table renamed to: {legacy_name}"
            )

        # -------------------------------------------------
        # CREATE CORRECT ADS TABLE
        # -------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS ads (
                id SERIAL PRIMARY KEY,
                slot TEXT UNIQUE NOT NULL,
                code TEXT DEFAULT ''
            )
        """)

        # -------------------------------------------------
        # DEFAULT ADS
        # -------------------------------------------------

        default_ads = [
            ("top", ""),
            ("player", ""),
            ("bottom", ""),
        ]

        for slot, code in default_ads:
            cur.execute("""
                INSERT INTO ads (slot, code)
                VALUES (%s, %s)
                ON CONFLICT (slot) DO NOTHING
            """, (slot, code))

        db.commit()

        print("PostgreSQL initialized successfully.")

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


# =========================================================
# R2
# =========================================================

def get_r2_client():
    if not R2_ACCOUNT_ID:
        raise RuntimeError("R2_ACCOUNT_ID is not configured")

    if not R2_ACCESS_KEY_ID:
        raise RuntimeError("R2_ACCESS_KEY_ID is not configured")

    if not R2_SECRET_ACCESS_KEY:
        raise RuntimeError(
            "R2_SECRET_ACCESS_KEY is not configured"
        )

    endpoint = R2_ENDPOINT

    if not endpoint:
        endpoint = (
            f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
        )

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
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


def clean_r2_key(key):
    if not key:
        return ""

    key = str(key).strip()

    key = key.replace("\\", "/")

    while key.startswith("/"):
        key = key[1:]

    return key


def r2_object_url(key):
    key = clean_r2_key(key)

    if not key:
        return ""

    if not R2_PUBLIC_URL:
        return ""

    return (
        f"{R2_PUBLIC_URL}/"
        f"{quote(key, safe='/')}"
    )


def r2_presigned_url(key, expires=PRESIGNED_EXPIRES):
    key = clean_r2_key(key)

    if not key:
        return ""

    client = get_r2_client()

    return client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": R2_BUCKET,
            "Key": key,
        },
        ExpiresIn=expires,
    )


def get_video_url(video_key):
    video_key = clean_r2_key(video_key)

    if not video_key:
        return ""

    # Prefer public R2 URL
    public_url = r2_object_url(video_key)

    if public_url:
        return public_url

    # Fallback to signed URL
    try:
        return r2_presigned_url(video_key)
    except Exception as exc:
        print("Video presigned URL error:", exc)
        return ""


def validate_r2_key(key, prefix=None):
    key = clean_r2_key(key)

    if not key:
        return False

    if len(key) > 1024:
        return False

    if ".." in key:
        return False

    if prefix and not key.startswith(prefix):
        return False

    return True


def r2_object_exists(key):
    key = clean_r2_key(key)

    if not key:
        return False

    try:
        client = get_r2_client()

        client.head_object(
            Bucket=R2_BUCKET,
            Key=key,
        )

        return True

    except ClientError as exc:
        error_code = (
            exc.response
            .get("Error", {})
            .get("Code", "")
        )

        if error_code in {
            "404",
            "NoSuchKey",
            "NotFound",
        }:
            return False

        print("R2 head error:", exc)
        return False

    except Exception as exc:
        print("R2 object check error:", exc)
        return False


def delete_r2_object(key):
    key = clean_r2_key(key)

    if not key:
        return False

    try:
        client = get_r2_client()

        client.delete_object(
            Bucket=R2_BUCKET,
            Key=key,
        )

        return True

    except Exception as exc:
        print("R2 delete error:", exc)
        return False


# =========================================================
# AUTH
# =========================================================

def admin_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):

        if not session.get("admin_logged_in"):
            return redirect(url_for("login"))

        return view_func(*args, **kwargs)

    return wrapper


# =========================================================
# ADS
# =========================================================

def get_ads():
    ads = {
        "top": "",
        "player": "",
        "bottom": "",
    }

    db = None

    try:
        db = get_db()
        cur = db.cursor()

        cur.execute("""
            SELECT slot, code
            FROM ads
            ORDER BY id ASC
        """)

        rows = cur.fetchall()

        for row in rows:
            slot = row.get("slot")

            if slot in ads:
                ads[slot] = row.get("code") or ""

    except Exception as exc:
        print("Ads error:", exc)

    finally:
        if db:
            db.close()

    return ads


# =========================================================
# HOME
# =========================================================

@app.route("/")
def index():

    db = get_db()

    try:
        cur = db.cursor()

        cur.execute("""
            SELECT
                id,
                title,
                category,
                description,
                poster,
                video,
                views,
                created_at
            FROM movies
            ORDER BY id DESC
        """)

        movies = cur.fetchall()

    finally:
        db.close()

    for movie in movies:
        movie["poster_url"] = r2_object_url(
            movie.get("poster", "")
        )

    ads = get_ads()

    return render_template(
        "index.html",
        movies=movies,
        ads=ads,
    )


# =========================================================
# MOVIE PAGE
# =========================================================

@app.route("/movie/<int:movie_id>")
def movie(movie_id):

    db = get_db()

    try:
        cur = db.cursor()

        cur.execute("""
            SELECT
                id,
                title,
                category,
                description,
                poster,
                video,
                views,
                created_at
            FROM movies
            WHERE id = %s
        """, (movie_id,))

        movie_data = cur.fetchone()

        if not movie_data:
            abort(404)

        cur.execute("""
            UPDATE movies
            SET views = COALESCE(views, 0) + 1
            WHERE id = %s
        """, (movie_id,))

        db.commit()

        cur.execute("""
            SELECT
                id,
                title,
                category,
                description,
                poster,
                video,
                views,
                created_at
            FROM movies
            WHERE id != %s
            ORDER BY id DESC
            LIMIT 12
        """, (movie_id,))

        related_movies = cur.fetchall()

    finally:
        db.close()

    movie_data["views"] = (
        movie_data.get("views", 0) or 0
    ) + 1

    poster_key = movie_data.get("poster", "") or ""
    video_key = movie_data.get("video", "") or ""

    movie_data["poster_url"] = r2_object_url(
        poster_key
    )

    movie_data["video_url"] = get_video_url(
        video_key
    )

    movie_data["video_mime"] = get_video_mime(
        video_key
    )

    for item in related_movies:
        item["poster_url"] = r2_object_url(
            item.get("poster", "")
        )

    ads = get_ads()

    return render_template(
        "movie.html",
        movie=movie_data,
        related_movies=related_movies,
        ads=ads,
    )


# =========================================================
# LOGIN
# =========================================================

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        username = (
            request.form.get("username", "")
            .strip()
        )

        password = (
            request.form.get("password", "")
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

    return render_template("login.html")


# =========================================================
# LOGOUT
# =========================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("login")
    )


# =========================================================
# ADMIN DASHBOARD
# =========================================================

@app.route("/admin")
@admin_required
def admin():

    db = get_db()

    try:
        cur = db.cursor()

        cur.execute("""
            SELECT
                id,
                title,
                category,
                description,
                poster,
                video,
                views,
                created_at
            FROM movies
            ORDER BY id DESC
        """)

        movies = cur.fetchall()

    finally:
        db.close()

    for item in movies:

        item["poster_url"] = r2_object_url(
            item.get("poster", "")
        )

        item["video_url"] = get_video_url(
            item.get("video", "")
        )

    ads = get_ads()

    return render_template(
        "admin.html",
        movies=movies,
        ads=ads,
    )


# =========================================================
# NORMAL SERVER UPLOAD
# Compatibility route
# =========================================================

@app.route("/admin/add", methods=["POST"])
@admin_required
def admin_add():

    title = (
        request.form.get("title", "")
        .strip()
    )

    category = (
        request.form.get("category", "Other")
        .strip()
    )

    description = (
        request.form.get("description", "")
        .strip()
    )

    poster = request.files.get("poster")
    video = request.files.get("video")

    if not title:
        flash(
            "Movie title is required.",
            "error"
        )
        return redirect(url_for("admin"))

    if not video or not video.filename:
        flash(
            "Video is required.",
            "error"
        )
        return redirect(url_for("admin"))

    if not allowed_file(
        video.filename,
        ALLOWED_VIDEOS
    ):
        flash(
            "Video केवल MP4, MKV, WebM या MOV होनी चाहिए.",
            "error"
        )
        return redirect(url_for("admin"))

    if poster and poster.filename:

        if not allowed_file(
            poster.filename,
            ALLOWED_POSTERS
        ):
            flash(
                "Poster JPG, JPEG, PNG या WEBP होना चाहिए.",
                "error"
            )
            return redirect(url_for("admin"))

    video_key = ""
    poster_key = ""

    client = get_r2_client()

    try:

        safe_video = secure_filename(
            video.filename
        )

        video_key = (
            f"videos/"
            f"{secrets.token_hex(12)}_"
            f"{safe_video}"
        )

        video_mime = get_video_mime(
            video.filename
        )

        client.upload_fileobj(
            video,
            R2_BUCKET,
            video_key,
            ExtraArgs={
                "ContentType": video_mime,
                "CacheControl": "public, max-age=31536000",
            },
        )

        if poster and poster.filename:

            safe_poster = secure_filename(
                poster.filename
            )

            poster_key = (
                f"posters/"
                f"{secrets.token_hex(12)}_"
                f"{safe_poster}"
            )

            poster_mime = get_poster_mime(
                poster.filename
            )

            client.upload_fileobj(
                poster,
                R2_BUCKET,
                poster_key,
                ExtraArgs={
                    "ContentType": poster_mime,
                    "CacheControl": "public, max-age=31536000",
                },
            )

        if not r2_object_exists(video_key):
            raise RuntimeError(
                "Video upload verification failed."
            )

        if poster_key and not r2_object_exists(
            poster_key
        ):
            raise RuntimeError(
                "Poster upload verification failed."
            )

        db = get_db()

        try:
            cur = db.cursor()

            cur.execute("""
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
            """, (
                title,
                category,
                description,
                poster_key,
                video_key,
            ))

            movie_id = cur.fetchone()["id"]

            db.commit()

        finally:
            db.close()

        flash(
            f"Movie added successfully. ID: {movie_id}",
            "success"
        )

    except Exception as exc:

        print("Admin add error:", repr(exc))

        if video_key:
            delete_r2_object(video_key)

        if poster_key:
            delete_r2_object(poster_key)

        flash(
            f"Upload failed: {exc}",
            "error"
        )

    return redirect(
        url_for("admin")
    )


# =========================================================
# DELETE MOVIE
# =========================================================

@app.route(
    "/admin/delete/<int:movie_id>",
    methods=["POST"]
)
@admin_required
def admin_delete_movie(movie_id):

    db = get_db()

    poster_key = ""
    video_key = ""

    try:

        cur = db.cursor()

        cur.execute("""
            SELECT poster, video
            FROM movies
            WHERE id = %s
        """, (movie_id,))

        movie_data = cur.fetchone()

        if not movie_data:
            flash(
                "Movie not found.",
                "error"
            )
            return redirect(url_for("admin"))

        poster_key = movie_data.get(
            "poster", ""
        ) or ""

        video_key = movie_data.get(
            "video", ""
        ) or ""

        cur.execute("""
            DELETE FROM movies
            WHERE id = %s
        """, (movie_id,))

        db.commit()

    except Exception as exc:

        db.rollback()

        print(
            "Delete movie DB error:",
            repr(exc)
        )

        flash(
            "Movie delete failed.",
            "error"
        )

        return redirect(
            url_for("admin")
        )

    finally:
        db.close()

    if video_key:
        delete_r2_object(video_key)

    if poster_key:
        delete_r2_object(poster_key)

    flash(
        "Movie deleted successfully.",
        "success"
    )

    return redirect(
        url_for("admin")
    )


# =========================================================
# ADS SAVE
# =========================================================

@app.route(
    "/admin/ads",
    methods=["POST"]
)
@admin_required
def admin_ads():

    top = request.form.get(
        "top",
        ""
    )

    player = request.form.get(
        "player",
        ""
    )

    bottom = request.form.get(
        "bottom",
        ""
    )

    db = get_db()

    try:

        cur = db.cursor()

        values = [
            ("top", top),
            ("player", player),
            ("bottom", bottom),
        ]

        for slot, code in values:

            cur.execute("""
                INSERT INTO ads (slot, code)
                VALUES (%s, %s)
                ON CONFLICT (slot)
                DO UPDATE SET code = EXCLUDED.code
            """, (
                slot,
                code,
            ))

        db.commit()

        flash(
            "Ads settings saved.",
            "success"
        )

    except Exception as exc:

        db.rollback()

        print(
            "Ads save error:",
            repr(exc)
        )

        flash(
            f"Ads save failed: {exc}",
            "error"
        )

    finally:
        db.close()

    return redirect(
        url_for("admin")
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
            "message": "R2 OK",
            "bucket": R2_BUCKET,
        })

    except Exception as exc:

        print(
            "R2 health error:",
            repr(exc)
        )

        return jsonify({
            "ok": False,
            "message": str(exc),
            "bucket": R2_BUCKET,
        }), 500


# =========================================================
# APP HEALTH
# =========================================================

@app.route("/health")
def health():

    try:

        db = get_db()

        try:
            cur = db.cursor()

            cur.execute(
                "SELECT 1"
            )

            cur.fetchone()

        finally:
            db.close()

        return jsonify({
            "ok": True,
            "database": "ok",
        })

    except Exception as exc:

        return jsonify({
            "ok": False,
            "database": "error",
            "error": str(exc),
        }), 500


# =========================================================
# R2 MULTIPART CREATE
# =========================================================

@app.route(
    "/api/r2/multipart/create",
    methods=["POST"]
)
@admin_required
def r2_multipart_create():

    data = request.get_json(
        silent=True
    ) or {}

    filename = (
        data.get("filename")
        or ""
    ).strip()

    content_type = (
        data.get("content_type")
        or ""
    ).strip()

    try:
        file_size = int(
            data.get("size", 0)
        )
    except Exception:
        file_size = 0

    if not filename:
        return jsonify({
            "ok": False,
            "error": "Filename is required."
        }), 400

    if not allowed_file(
        filename,
        ALLOWED_VIDEOS
    ):
        return jsonify({
            "ok": False,
            "error": (
                "Video केवल MP4, MKV, WebM या MOV "
                "होनी चाहिए."
            )
        }), 400

    if file_size <= 0:
        return jsonify({
            "ok": False,
            "error": "Invalid file size."
        }), 400

    max_size = (
        4 * 1024 * 1024 * 1024
    )

    if file_size > max_size:
        return jsonify({
            "ok": False,
            "error": "Maximum video size is 4 GB."
        }), 400

    safe_name = secure_filename(
        filename
    )

    if not safe_name:
        safe_name = (
            f"video_{secrets.token_hex(8)}"
        )

    video_key = (
        f"videos/"
        f"{secrets.token_hex(16)}_"
        f"{safe_name}"
    )

    mime = (
        content_type
        or get_video_mime(filename)
    )

    try:

        client = get_r2_client()

        result = client.create_multipart_upload(
            Bucket=R2_BUCKET,
            Key=video_key,
            ContentType=mime,
            CacheControl=(
                "public, max-age=31536000"
            ),
        )

        upload_id = result.get(
            "UploadId"
        )

        if not upload_id:
            raise RuntimeError(
                "R2 did not return UploadId."
            )

        total_parts = math.ceil(
            file_size / PART_SIZE
        )

        if total_parts > MAX_PARTS:
            raise RuntimeError(
                "File requires too many multipart parts."
            )

        return jsonify({
            "ok": True,
            "key": video_key,
            "upload_id": upload_id,
            "part_size": PART_SIZE,
            "parts": total_parts,
            "content_type": mime,
        })

    except Exception as exc:

        print(
            "R2 multipart create failed:",
            repr(exc)
        )

        return jsonify({
            "ok": False,
            "error": str(exc),
        }), 500


# =========================================================
# R2 MULTIPART PRESIGNED URLS
# =========================================================

@app.route(
    "/api/r2/multipart/urls",
    methods=["POST"]
)
@admin_required
def r2_multipart_urls():

    data = request.get_json(
        silent=True
    ) or {}

    key = clean_r2_key(
        data.get("key", "")
    )

    upload_id = (
        data.get("upload_id")
        or ""
    ).strip()

    try:
        parts = int(
            data.get("parts", 0)
        )
    except Exception:
        parts = 0

    if not validate_r2_key(
        key,
        "videos/"
    ):
        return jsonify({
            "ok": False,
            "error": "Invalid video key."
        }), 400

    if not upload_id:
        return jsonify({
            "ok": False,
            "error": "Upload ID is required."
        }), 400

    if parts < 1 or parts > MAX_PARTS:
        return jsonify({
            "ok": False,
            "error": "Invalid number of parts."
        }), 400

    try:

        client = get_r2_client()

        urls = []

        for part_number in range(
            1,
            parts + 1
        ):

            signed_url = (
                client.generate_presigned_url(
                    "upload_part",
                    Params={
                        "Bucket": R2_BUCKET,
                        "Key": key,
                        "UploadId": upload_id,
                        "PartNumber": part_number,
                    },
                    ExpiresIn=PRESIGNED_EXPIRES,
                )
            )

            urls.append({
                "part_number": part_number,
                "url": signed_url,
            })

        return jsonify({
            "ok": True,
            "urls": urls,
        })

    except Exception as exc:

        print(
            "R2 multipart URL error:",
            repr(exc)
        )

        return jsonify({
            "ok": False,
            "error": str(exc),
        }), 500


# =========================================================
# R2 MULTIPART COMPLETE
# =========================================================

@app.route(
    "/api/r2/multipart/complete",
    methods=["POST"]
)
@admin_required
def r2_multipart_complete():

    data = request.get_json(
        silent=True
    ) or {}

    key = clean_r2_key(
        data.get("key", "")
    )

    upload_id = (
        data.get("upload_id")
        or ""
    ).strip()

    parts = data.get(
        "parts",
        []
    )

    if not validate_r2_key(
        key,
        "videos/"
    ):
        return jsonify({
            "ok": False,
            "error": "Invalid video key."
        }), 400

    if not upload_id:
        return jsonify({
            "ok": False,
            "error": "Upload ID is required."
        }), 400

    if not isinstance(parts, list):
        return jsonify({
            "ok": False,
            "error": "Parts must be a list."
        }), 400

    if not parts:
        return jsonify({
            "ok": False,
            "error": "No uploaded parts received."
        }), 400

    normalized_parts = []

    seen_numbers = set()

    try:

        for item in parts:

            if not isinstance(item, dict):
                raise ValueError(
                    "Invalid part data."
                )

            part_number = int(
                item.get("PartNumber")
            )

            etag = (
                item.get("ETag")
                or ""
            ).strip()

            if (
                part_number < 1
                or part_number > MAX_PARTS
            ):
                raise ValueError(
                    "Invalid part number."
                )

            if not etag:
                raise ValueError(
                    f"Missing ETag for part "
                    f"{part_number}."
                )

            if part_number in seen_numbers:
                raise ValueError(
                    f"Duplicate part "
                    f"{part_number}."
                )

            seen_numbers.add(
                part_number
            )

            normalized_parts.append({
                "ETag": etag,
                "PartNumber": part_number,
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
                "Parts": normalized_parts
            },
        )

        # ---------------------------------------------
        # VERY IMPORTANT:
        # Verify completed object exists
        # ---------------------------------------------

        head = client.head_object(
            Bucket=R2_BUCKET,
            Key=key,
        )

        object_size = int(
            head.get("ContentLength", 0)
        )

        if object_size <= 0:
            raise RuntimeError(
                "R2 completed object has zero size."
            )

        public_url = r2_object_url(
            key
        )

        return jsonify({
            "ok": True,
            "message": "Upload completed successfully.",
            "key": key,
            "size": object_size,
            "etag": result.get("ETag", ""),
            "public_url": public_url,
        })

    except Exception as exc:

        print(
            "R2 multipart complete failed:",
            repr(exc)
        )

        return jsonify({
            "ok": False,
            "error": str(exc),
        }), 500


# =========================================================
# R2 MULTIPART ABORT
# =========================================================

@app.route(
    "/api/r2/multipart/abort",
    methods=["POST"]
)
@admin_required
def r2_multipart_abort():

    data = request.get_json(
        silent=True
    ) or {}

    key = clean_r2_key(
        data.get("key", "")
    )

    upload_id = (
        data.get("upload_id")
        or ""
    ).strip()

    if not validate_r2_key(
        key,
        "videos/"
    ):
        return jsonify({
            "ok": False,
            "error": "Invalid video key."
        }), 400

    if not upload_id:
        return jsonify({
            "ok": False,
            "error": "Upload ID is required."
        }), 400

    try:

        client = get_r2_client()

        client.abort_multipart_upload(
            Bucket=R2_BUCKET,
            Key=key,
            UploadId=upload_id,
        )

        return jsonify({
            "ok": True,
            "message": "Multipart upload aborted.",
        })

    except Exception as exc:

        print(
            "R2 multipart abort error:",
            repr(exc)
        )

        return jsonify({
            "ok": False,
            "error": str(exc),
        }), 500


# =========================================================
# R2 DELETE API
# =========================================================

@app.route(
    "/api/r2/delete",
    methods=["POST"]
)
@admin_required
def api_r2_delete():

    data = request.get_json(
        silent=True
    ) or {}

    key = clean_r2_key(
        data.get("key", "")
    )

    if not validate_r2_key(
        key
    ):
        return jsonify({
            "ok": False,
            "error": "Invalid R2 key."
        }), 400

    # Only allow our own folders
    if not (
        key.startswith("videos/")
        or key.startswith("posters/")
    ):
        return jsonify({
            "ok": False,
            "error": "R2 key is not allowed."
        }), 400

    try:

        client = get_r2_client()

        client.delete_object(
            Bucket=R2_BUCKET,
            Key=key,
        )

        return jsonify({
            "ok": True,
            "message": "R2 object deleted.",
            "key": key,
        })

    except Exception as exc:

        print(
            "R2 API delete error:",
            repr(exc)
        )

        return jsonify({
            "ok": False,
            "error": str(exc),
        }), 500


# =========================================================
# SAVE MOVIE AFTER DIRECT R2 UPLOAD
# =========================================================

@app.route(
    "/api/movie/save",
    methods=["POST"]
)
@admin_required
def api_movie_save():

    data = request.get_json(
        silent=True
    ) or {}

    title = (
        data.get("title")
        or ""
    ).strip()

    category = (
        data.get("category")
        or "Other"
    ).strip()

    description = (
        data.get("description")
        or ""
    ).strip()

    video_key = clean_r2_key(
        data.get("video_key")
        or data.get("video")
        or ""
    )

    poster_key = clean_r2_key(
        data.get("poster_key")
        or data.get("poster")
        or ""
    )

    if not title:
        return jsonify({
            "ok": False,
            "error": "Movie title is required."
        }), 400

    if not validate_r2_key(
        video_key,
        "videos/"
    ):
        return jsonify({
            "ok": False,
            "error": "Invalid video key."
        }), 400

    if poster_key and not validate_r2_key(
        poster_key,
        "posters/"
    ):
        return jsonify({
            "ok": False,
            "error": "Invalid poster key."
        }), 400

    try:

        # ---------------------------------------------
        # Verify video really exists in R2
        # ---------------------------------------------

        client = get_r2_client()

        video_head = client.head_object(
            Bucket=R2_BUCKET,
            Key=video_key,
        )

        video_size = int(
            video_head.get(
                "ContentLength",
                0
            )
        )

        if video_size <= 0:
            return jsonify({
                "ok": False,
                "error": "Video object is empty."
            }), 400

        # ---------------------------------------------
        # Verify poster if supplied
        # ---------------------------------------------

        if poster_key:

            poster_head = client.head_object(
                Bucket=R2_BUCKET,
                Key=poster_key,
            )

            poster_size = int(
                poster_head.get(
                    "ContentLength",
                    0
                )
            )

            if poster_size <= 0:
                return jsonify({
                    "ok": False,
                    "error": "Poster object is empty."
                }), 400

        # ---------------------------------------------
        # Save in PostgreSQL
        # ---------------------------------------------

        db = get_db()

        try:

            cur = db.cursor()

            cur.execute("""
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
            """, (
                title,
                category,
                description,
                poster_key,
                video_key,
            ))

            movie_id = cur.fetchone()["id"]

            db.commit()

        except Exception:
            db.rollback()
            raise

        finally:
            db.close()

        return jsonify({
            "ok": True,
            "message": "Movie saved successfully.",
            "movie_id": movie_id,
            "video_key": video_key,
            "poster_key": poster_key,
            "video_url": get_video_url(
                video_key
            ),
            "poster_url": r2_object_url(
                poster_key
            ),
        })

    except Exception as exc:

        print(
            "Movie save error:",
            repr(exc)
        )

        return jsonify({
            "ok": False,
            "error": str(exc),
        }), 500


# =========================================================
# ADS.TXT
# =========================================================

@app.route("/ads.txt")
def ads_txt():

    publisher_id = "pub-8697157365303435"

    return Response(
        f"google.com, {publisher_id}, DIRECT, f08c47fec0942fa0\n",
        mimetype="text/plain"
    )


# =========================================================
# ERROR HANDLERS
# =========================================================

@app.errorhandler(413)
def request_entity_too_large(error):

    return jsonify({
        "ok": False,
        "error": "File is too large. Maximum size is 4 GB."
    }), 413


@app.errorhandler(404)
def not_found(error):

    if request.path.startswith("/api/"):
        return jsonify({
            "ok": False,
            "error": "Not found."
        }), 404

    return render_template(
        "404.html"
    ), 404


@app.errorhandler(500)
def internal_error(error):

    print(
        "Internal server error:",
        repr(error)
    )

    if request.path.startswith("/api/"):
        return jsonify({
            "ok": False,
            "error": "Internal server error."
        }), 500

    return render_template(
        "500.html"
    ), 500


# =========================================================
# STARTUP DATABASE
# =========================================================

try:
    init_db()

except Exception as exc:
    print(
        "Database initialization error:",
        repr(exc)
    )


# =========================================================
# LOCAL RUN
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
