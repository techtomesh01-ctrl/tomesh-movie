import os
import math
import mimetypes
import secrets
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


# ============================================================
# APP
# ============================================================

app = Flask(__name__)

app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY",
    "change-this-secret-key"
)

# 4 GB
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024


# ============================================================
# ENVIRONMENT
# ============================================================

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get(
    "ADMIN_PASSWORD",
    "change-me-now"
)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    ""
).strip()

R2_ACCOUNT_ID = os.environ.get(
    "R2_ACCOUNT_ID",
    ""
).strip()

R2_ACCESS_KEY_ID = os.environ.get(
    "R2_ACCESS_KEY_ID",
    ""
).strip()

R2_SECRET_ACCESS_KEY = os.environ.get(
    "R2_SECRET_ACCESS_KEY",
    ""
).strip()

R2_BUCKET = os.environ.get(
    "R2_BUCKET",
    "tomesh-movies"
).strip()

R2_ENDPOINT = os.environ.get(
    "R2_ENDPOINT",
    f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
).strip().rstrip("/")

R2_PUBLIC_URL = os.environ.get(
    "R2_PUBLIC_URL",
    ""
).strip().rstrip("/")


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

# 10 MB parts
PART_SIZE = 10 * 1024 * 1024

MAX_PARTS = 10000

PRESIGNED_EXPIRES = 3600

MAX_VIDEO_SIZE = 4 * 1024 * 1024 * 1024


# ============================================================
# BASIC HELPERS
# ============================================================

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

    return filename.rsplit(".", 1)[1].lower()


def get_video_mime(filename):
    ext = file_extension(filename)

    mime_map = {
        "mp4": "video/mp4",
        "webm": "video/webm",
        "mov": "video/quicktime",
        "mkv": "video/x-matroska",
    }

    return mime_map.get(
        ext,
        mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
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
        mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )


# ============================================================
# DATABASE
# ============================================================

def get_db():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not configured"
        )

    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor
    )


def init_db():
    db = get_db()

    try:
        cur = db.cursor()

        # ====================================================
        # MOVIES
        # ====================================================

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

        # ====================================================
        # ADS
        #
        # Your old database already has an "ads" table with
        # a different structure. CREATE TABLE IF NOT EXISTS
        # cannot repair an existing incompatible table.
        #
        # We safely rename the old table once and create the
        # correct table.
        # ====================================================

        cur.execute("""
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public'
                AND table_name = 'ads'
            ) AS exists
        """)

        ads_exists = cur.fetchone()["exists"]

        if ads_exists:

            cur.execute("""
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                    AND table_name = 'ads'
                    AND column_name = 'slot'
                ) AS exists
            """)

            slot_exists = cur.fetchone()["exists"]

            cur.execute("""
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                    AND table_name = 'ads'
                    AND column_name = 'code'
                ) AS exists
            """)

            code_exists = cur.fetchone()["exists"]

            if not slot_exists or not code_exists:

                legacy_name = (
                    "ads_legacy_"
                    + secrets.token_hex(4)
                )

                cur.execute(
                    f'ALTER TABLE "ads" RENAME TO "{legacy_name}"'
                )

                print(
                    "Old incompatible ads table renamed to:",
                    legacy_name
                )

        # Create clean ads table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ads (
                id SERIAL PRIMARY KEY,
                slot TEXT UNIQUE NOT NULL,
                code TEXT DEFAULT ''
            )
        """)

        # ====================================================
        # DEFAULT ADS
        # ====================================================

        for slot_name in (
            "top",
            "player",
            "bottom",
        ):

            cur.execute("""
                INSERT INTO ads (slot, code)
                VALUES (%s, %s)
                ON CONFLICT (slot) DO NOTHING
            """, (
                slot_name,
                ""
            ))

        db.commit()

        print(
            "PostgreSQL initialized successfully."
        )

    except Exception as exc:

        db.rollback()

        print(
            "Database initialization error:",
            repr(exc)
        )

        raise

    finally:
        db.close()


# ============================================================
# R2 CLIENT
# ============================================================

def get_r2_client():

    if not R2_ACCOUNT_ID:
        raise RuntimeError(
            "R2_ACCOUNT_ID is missing"
        )

    if not R2_ACCESS_KEY_ID:
        raise RuntimeError(
            "R2_ACCESS_KEY_ID is missing"
        )

    if not R2_SECRET_ACCESS_KEY:
        raise RuntimeError(
            "R2_SECRET_ACCESS_KEY is missing"
        )

    if not R2_ENDPOINT:
        raise RuntimeError(
            "R2_ENDPOINT is missing"
        )

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
            }
        )
    )


# ============================================================
# R2 URL
# ============================================================

def clean_r2_key(key):

    if not key:
        return ""

    return str(key).strip().lstrip("/")


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


def r2_presigned_url(
    key,
    expires=PRESIGNED_EXPIRES
):

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
        ExpiresIn=expires
    )


def get_video_url(key):

    key = clean_r2_key(key)

    if not key:
        return ""

    # Public R2 URL
    public_url = r2_object_url(key)

    if public_url:
        return public_url

    # Fallback
    try:
        return r2_presigned_url(key)
    except Exception as exc:
        print(
            "Presigned video URL error:",
            repr(exc)
        )
        return ""


# ============================================================
# R2 VALIDATION
# ============================================================

def validate_r2_key(
    key,
    prefix=None
):

    key = clean_r2_key(key)

    if not key:
        return False

    if len(key) > 1024:
        return False

    if prefix and not key.startswith(prefix):
        return False

    if ".." in key:
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
            Key=key
        )

        return True

    except ClientError as exc:

        print(
            "R2 HEAD error:",
            repr(exc)
        )

        return False

    except Exception as exc:

        print(
            "R2 object check error:",
            repr(exc)
        )

        return False


# ============================================================
# ADMIN AUTH
# ============================================================

def admin_required(func):

    @wraps(func)
    def wrapper(*args, **kwargs):

        if not session.get(
            "admin_logged_in"
        ):
            return redirect(
                url_for("login")
            )

        return func(*args, **kwargs)

    return wrapper


# ============================================================
# ADS
# ============================================================

def get_ads():

    ads = {
        "top": "",
        "player": "",
        "bottom": "",
    }

    try:

        db = get_db()

        try:

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
                    ads[slot] = (
                        row.get("code")
                        or ""
                    )

            return ads

        finally:
            db.close()

    except Exception as exc:

        print(
            "Ads error:",
            repr(exc)
        )

        return ads


# ============================================================
# HOME
# ============================================================

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

        for movie_data in movies:

            movie_data["poster_url"] = ""

            if movie_data.get("poster"):

                movie_data["poster_url"] = (
                    r2_object_url(
                        movie_data["poster"]
                    )
                )

        return render_template(
            "index.html",
            movies=movies,
            ads=get_ads()
        )

    finally:
        db.close()


# ============================================================
# MOVIE PAGE
# ============================================================

@app.route(
    "/movie/<int:movie_id>"
)
def movie(movie_id):

    db = get_db()

    try:

        cur = db.cursor()

        cur.execute("""
            SELECT *
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

        # Poster
        movie_data["poster_url"] = ""

        if movie_data.get("poster"):

            movie_data["poster_url"] = (
                r2_object_url(
                    movie_data["poster"]
                )
            )

        # Video
        video_key = clean_r2_key(
            movie_data.get(
                "video",
                ""
            )
        )

        movie_data["video_url"] = (
            get_video_url(video_key)
            if video_key
            else ""
        )

        movie_data["video_mime"] = (
            get_video_mime(video_key)
        )

        # Related movies
        cur.execute("""
            SELECT
                id,
                title,
                category,
                poster,
                video,
                views
            FROM movies
            WHERE id != %s
            ORDER BY id DESC
            LIMIT 8
        """, (movie_id,))

        related_movies = cur.fetchall()

        for item in related_movies:

            item["poster_url"] = ""

            if item.get("poster"):

                item["poster_url"] = (
                    r2_object_url(
                        item["poster"]
                    )
                )

        return render_template(
            "movie.html",
            movie=movie_data,
            related_movies=related_movies,
            ads=get_ads()
        )

    finally:
        db.close()


# ============================================================
# LOGIN
# ============================================================

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
        "login.html"
    )


# ============================================================
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

        for movie_data in movies:

            movie_data["poster_url"] = ""

            movie_data["video_url"] = ""

            if movie_data.get("poster"):

                movie_data["poster_url"] = (
                    r2_object_url(
                        movie_data["poster"]
                    )
                )

            if movie_data.get("video"):

                movie_data["video_url"] = (
                    r2_object_url(
                        movie_data["video"]
                    )
                )

        cur.execute("""
            SELECT COUNT(*) AS total
            FROM movies
        """)

        total_movies = (
            cur.fetchone()["total"]
        )

        cur.execute("""
            SELECT
                COALESCE(
                    SUM(views),
                    0
                ) AS total_views
            FROM movies
        """)

        total_views = (
            cur.fetchone()["total_views"]
        )

        return render_template(
            "admin.html",
            movies=movies,
            total_movies=total_movies,
            total_views=total_views,
            ads=get_ads()
        )

    finally:
        db.close()


# ============================================================
# NORMAL UPLOAD
# ============================================================

@app.route(
    "/admin/add",
    methods=["POST"]
)
@admin_required
def admin_add():

    title = request.form.get(
        "title",
        ""
    ).strip()

    category = request.form.get(
        "category",
        "Other"
    ).strip()

    description = request.form.get(
        "description",
        ""
    ).strip()

    poster = request.files.get(
        "poster"
    )

    video = request.files.get(
        "video"
    )

    if not title:

        flash(
            "Movie title is required.",
            "error"
        )

        return redirect(
            url_for("admin")
        )

    if not video or not video.filename:

        flash(
            "Video is required.",
            "error"
        )

        return redirect(
            url_for("admin")
        )

    if not allowed_file(
        video.filename,
        ALLOWED_VIDEOS
    ):

        flash(
            "Video केवल MP4, MKV, WebM या MOV होनी चाहिए.",
            "error"
        )

        return redirect(
            url_for("admin")
        )

    if poster and poster.filename:

        if not allowed_file(
            poster.filename,
            ALLOWED_POSTERS
        ):

            flash(
                "Poster JPG, JPEG, PNG या WEBP होना चाहिए.",
                "error"
            )

            return redirect(
                url_for("admin")
            )

    video_key = (
        "videos/"
        + secrets.token_hex(8)
        + "_"
        + secure_filename(
            video.filename
        )
    )

    poster_key = ""

    if poster and poster.filename:

        poster_key = (
            "posters/"
            + secrets.token_hex(8)
            + "_"
            + secure_filename(
                poster.filename
            )
        )

    client = get_r2_client()

    try:

        video.seek(0)

        client.upload_fileobj(
            video,
            R2_BUCKET,
            video_key,
            ExtraArgs={
                "ContentType":
                    get_video_mime(
                        video.filename
                    ),
                "CacheControl":
                    "public, max-age=31536000",
            }
        )

        if poster and poster.filename:

            poster.seek(0)

            client.upload_fileobj(
                poster,
                R2_BUCKET,
                poster_key,
                ExtraArgs={
                    "ContentType":
                        get_poster_mime(
                            poster.filename
                        ),
                    "CacheControl":
                        "public, max-age=31536000",
                }
            )

        if not r2_object_exists(
            video_key
        ):

            raise RuntimeError(
                "Video uploaded but R2 verification failed."
            )

        if poster_key:

            if not r2_object_exists(
                poster_key
            ):

                raise RuntimeError(
                    "Poster uploaded but R2 verification failed."
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
                (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    0
                )
            """, (
                title,
                category,
                description,
                poster_key,
                video_key
            ))

            db.commit()

        finally:
            db.close()

        flash(
            "Movie uploaded successfully.",
            "success"
        )

    except Exception as exc:

        print(
            "Normal upload error:",
            repr(exc)
        )

        try:

            client.delete_object(
                Bucket=R2_BUCKET,
                Key=video_key
            )

        except Exception:
            pass

        try:

            if poster_key:

                client.delete_object(
                    Bucket=R2_BUCKET,
                    Key=poster_key
                )

        except Exception:
            pass

        flash(
            f"Upload failed: {exc}",
            "error"
        )

    return redirect(
        url_for("admin")
    )


# ============================================================
# DELETE MOVIE
# ============================================================

@app.route(
    "/admin/delete/<int:movie_id>",
    methods=["POST"],
    endpoint="admin_delete_movie"
)
@admin_required
def admin_delete_movie(movie_id):

    db = get_db()

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

            return redirect(
                url_for("admin")
            )

        client = get_r2_client()

        for key in (
            movie_data.get("poster"),
            movie_data.get("video"),
        ):

            key = clean_r2_key(key)

            if not key:
                continue

            try:

                client.delete_object(
                    Bucket=R2_BUCKET,
                    Key=key
                )

                print(
                    "Deleted R2 object:",
                    key
                )

            except Exception as exc:

                print(
                    "R2 delete error:",
                    repr(exc)
                )

        cur.execute("""
            DELETE FROM movies
            WHERE id = %s
        """, (movie_id,))

        db.commit()

        flash(
            "Movie deleted successfully.",
            "success"
        )

    finally:
        db.close()

    return redirect(
        url_for("admin")
    )


# ============================================================
# ADS
# ============================================================

@app.route(
    "/admin/ads",
    methods=["POST"]
)
@admin_required
def admin_ads():

    data = {
        "top": request.form.get(
            "top",
            ""
        ),
        "player": request.form.get(
            "player",
            ""
        ),
        "bottom": request.form.get(
            "bottom",
            ""
        ),
    }

    db = get_db()

    try:

        cur = db.cursor()

        for slot_name, code_value in data.items():

            cur.execute("""
                INSERT INTO ads
                (
                    slot,
                    code
                )
                VALUES
                (
                    %s,
                    %s
                )
                ON CONFLICT (slot)
                DO UPDATE SET
                    code = EXCLUDED.code
            """, (
                slot_name,
                code_value
            ))

        db.commit()

        flash(
            "Ads settings saved.",
            "success"
        )

    finally:
        db.close()

    return redirect(
        url_for("admin")
    )


# ============================================================
# R2 HEALTH
# ============================================================

@app.route("/r2-health")
def r2_health():

    try:

        client = get_r2_client()

        client.head_bucket(
            Bucket=R2_BUCKET
        )

        return "R2 OK", 200

    except Exception as exc:

        print(
            "R2 health error:",
            repr(exc)
        )

        return (
            f"R2 ERROR: {exc}",
            500
        )


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():
    return "OK", 200


# ============================================================
# MULTIPART CREATE
# ============================================================

@app.route(
    "/api/r2/multipart/create",
    methods=["POST"]
)
@admin_required
def r2_multipart_create():

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    filename = str(
        data.get(
            "filename",
            ""
        )
    ).strip()

    content_type = str(
        data.get(
            "content_type",
            ""
        )
    ).strip()

    size = data.get("size")

    if not filename:

        return jsonify({
            "ok": False,
            "error": "Filename missing."
        }), 400

    if not allowed_file(
        filename,
        ALLOWED_VIDEOS
    ):

        return jsonify({
            "ok": False,
            "error":
                "Only MP4, MKV, WebM and MOV are allowed."
        }), 400

    try:

        size = int(size)

    except Exception:

        return jsonify({
            "ok": False,
            "error": "Invalid file size."
        }), 400

    if size <= 0:

        return jsonify({
            "ok": False,
            "error": "Invalid file size."
        }), 400

    if size > MAX_VIDEO_SIZE:

        return jsonify({
            "ok": False,
            "error":
                "Maximum video size is 4 GB."
        }), 400

    safe_name = secure_filename(
        filename
    )

    if not safe_name:

        safe_name = (
            "video_"
            + secrets.token_hex(8)
            + ".mp4"
        )

    video_key = (
        "videos/"
        + secrets.token_hex(12)
        + "_"
        + safe_name
    )

    if not content_type:

        content_type = get_video_mime(
            filename
        )

    if content_type == (
        "application/octet-stream"
    ):

        content_type = get_video_mime(
            filename
        )

    parts = math.ceil(
        size / PART_SIZE
    )

    if parts > MAX_PARTS:

        return jsonify({
            "ok": False,
            "error":
                "File requires too many parts."
        }), 400

    client = get_r2_client()

    try:

        response = (
            client.create_multipart_upload(
                Bucket=R2_BUCKET,
                Key=video_key,
                ContentType=content_type,
                CacheControl=
                    "public, max-age=31536000"
            )
        )

        upload_id = response[
            "UploadId"
        ]

        print(
            "Multipart CREATED:",
            video_key,
            "parts:",
            parts
        )

        return jsonify({
            "ok": True,
            "key": video_key,
            "upload_id": upload_id,
            "part_size": PART_SIZE,
            "parts": parts,
            "content_type": content_type
        })

    except Exception as exc:

        print(
            "Multipart create error:",
            repr(exc)
        )

        return jsonify({
            "ok": False,
            "error": str(exc)
        }), 500


# ============================================================
# MULTIPART URLS
# ============================================================

@app.route(
    "/api/r2/multipart/urls",
    methods=["POST"]
)
@admin_required
def r2_multipart_urls():

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    key = clean_r2_key(
        data.get(
            "key",
            ""
        )
    )

    upload_id = str(
        data.get(
            "upload_id",
            ""
        )
    ).strip()

    parts = data.get(
        "parts"
    )

    if not validate_r2_key(
        key,
        "videos/"
    ):

        return jsonify({
            "ok": False,
            "error":
                "Invalid R2 video key."
        }), 400

    if not upload_id:

        return jsonify({
            "ok": False,
            "error":
                "Upload ID missing."
        }), 400

    try:

        parts = int(parts)

    except Exception:

        return jsonify({
            "ok": False,
            "error":
                "Invalid parts count."
        }), 400

    if parts < 1 or parts > MAX_PARTS:

        return jsonify({
            "ok": False,
            "error":
                "Invalid parts count."
        }), 400

    client = get_r2_client()

    urls = []

    try:

        for part_number in range(
            1,
            parts + 1
        ):

            url = (
                client.generate_presigned_url(
                    "upload_part",
                    Params={
                        "Bucket":
                            R2_BUCKET,
                        "Key":
                            key,
                        "UploadId":
                            upload_id,
                        "PartNumber":
                            part_number,
                    },
                    ExpiresIn=
                        PRESIGNED_EXPIRES
                )
            )

            urls.append({
                "part_number":
                    part_number,
                "url":
                    url
            })

        return jsonify({
            "ok": True,
            "urls": urls
        })

    except Exception as exc:

        print(
            "Multipart URL error:",
            repr(exc)
        )

        return jsonify({
            "ok": False,
            "error": str(exc)
        }), 500


# ============================================================
# MULTIPART COMPLETE
# ============================================================

@app.route(
    "/api/r2/multipart/complete",
    methods=["POST"]
)
@admin_required
def r2_multipart_complete():

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    key = clean_r2_key(
        data.get(
            "key",
            ""
        )
    )

    upload_id = str(
        data.get(
            "upload_id",
            ""
        )
    ).strip()

    parts = data.get(
        "parts"
    )

    if not validate_r2_key(
        key,
        "videos/"
    ):

        return jsonify({
            "ok": False,
            "error":
                "Invalid R2 video key."
        }), 400

    if not upload_id:

        return jsonify({
            "ok": False,
            "error":
                "Upload ID missing."
        }), 400

    if not isinstance(
        parts,
        list
    ):

        return jsonify({
            "ok": False,
            "error":
                "Parts list missing."
        }), 400

    if not parts:

        return jsonify({
            "ok": False,
            "error":
                "No uploaded parts received."
        }), 400

    clean_parts = []

    try:

        for part in parts:

            if not isinstance(
                part,
                dict
            ):
                raise ValueError(
                    "Invalid part."
                )

            part_number = int(
                part.get(
                    "PartNumber"
                )
            )

            etag = str(
                part.get(
                    "ETag",
                    ""
                )
            ).strip()

            if part_number < 1:
                raise ValueError(
                    "Invalid part number."
                )

            if not etag:
                raise ValueError(
                    "Missing ETag."
                )

            clean_parts.append({
                "PartNumber":
                    part_number,
                "ETag":
                    etag
            })

        clean_parts.sort(
            key=lambda item:
                item["PartNumber"]
        )

        seen = set()

        for part in clean_parts:

            number = part[
                "PartNumber"
            ]

            if number in seen:

                raise ValueError(
                    "Duplicate part number."
                )

            seen.add(number)

    except Exception as exc:

        return jsonify({
            "ok": False,
            "error":
                f"Invalid multipart parts: {exc}"
        }), 400

    client = get_r2_client()

    try:

        print(
            "================================================"
        )

        print(
            "MULTIPART COMPLETE START"
        )

        print(
            "Key:",
            key
        )

        print(
            "Upload ID:",
            upload_id
        )

        print(
            "Parts:",
            len(clean_parts)
        )

        print(
            "================================================"
        )

        # ====================================================
        # THIS IS THE IMPORTANT STEP
        # ====================================================

        response = (
            client.complete_multipart_upload(
                Bucket=R2_BUCKET,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={
                    "Parts": clean_parts
                }
            )
        )

        print(
            "R2 COMPLETE RESPONSE:",
            response
        )

        # ====================================================
        # VERIFY FINAL OBJECT
        # ====================================================

        head = client.head_object(
            Bucket=R2_BUCKET,
            Key=key
        )

        object_size = int(
            head.get(
                "ContentLength",
                0
            )
        )

        if object_size <= 0:

            raise RuntimeError(
                "R2 object completed but size is zero."
            )

        content_type = (
            head.get(
                "ContentType",
                ""
            )
            or get_video_mime(key)
        )

        public_url = r2_object_url(
            key
        )

        print(
            "================================================"
        )

        print(
            "MULTIPART COMPLETE + VERIFIED"
        )

        print(
            "Key:",
            key
        )

        print(
            "Size:",
            object_size
        )

        print(
            "Content-Type:",
            content_type
        )

        print(
            "Public URL:",
            public_url
        )

        print(
            "================================================"
        )

        return jsonify({
            "ok": True,
            "completed": True,
            "verified": True,
            "key": key,
            "size": object_size,
            "etag": head.get(
                "ETag",
                ""
            ),
            "content_type":
                content_type,
            "public_url":
                public_url
        })

    except Exception as exc:

        print(
            "================================================"
        )

        print(
            "MULTIPART COMPLETE ERROR:",
            repr(exc)
        )

        print(
            "================================================"
        )

        return jsonify({
            "ok": False,
            "completed": False,
            "verified": False,
            "error": str(exc)
        }), 500


# ============================================================
# MULTIPART ABORT
# ============================================================

@app.route(
    "/api/r2/multipart/abort",
    methods=["POST"]
)
@admin_required
def r2_multipart_abort():

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    key = clean_r2_key(
        data.get(
            "key",
            ""
        )
    )

    upload_id = str(
        data.get(
            "upload_id",
            ""
        )
    ).strip()

    if not validate_r2_key(
        key,
        "videos/"
    ):

        return jsonify({
            "ok": False,
            "error":
                "Invalid R2 video key."
        }), 400

    if not upload_id:

        return jsonify({
            "ok": False,
            "error":
                "Upload ID missing."
        }), 400

    try:

        client = get_r2_client()

        client.abort_multipart_upload(
            Bucket=R2_BUCKET,
            Key=key,
            UploadId=upload_id
        )

        print(
            "Multipart ABORTED:",
            key
        )

        return jsonify({
            "ok": True,
            "aborted": True
        })

    except Exception as exc:

        print(
            "Multipart abort error:",
            repr(exc)
        )

        return jsonify({
            "ok": False,
            "error": str(exc)
        }), 500


# ============================================================
# R2 DELETE API
# ============================================================

@app.route(
    "/api/r2/delete",
    methods=["POST"]
)
@admin_required
def api_r2_delete():

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    key = clean_r2_key(
        data.get(
            "key",
            ""
        )
    )

    if not key:

        return jsonify({
            "ok": False,
            "error":
                "Key missing."
        }), 400

    if not (
        key.startswith(
            "videos/"
        )
        or
        key.startswith(
            "posters/"
        )
    ):

        return jsonify({
            "ok": False,
            "error":
                "Invalid R2 key."
        }), 400

    try:

        client = get_r2_client()

        client.delete_object(
            Bucket=R2_BUCKET,
            Key=key
        )

        return jsonify({
            "ok": True,
            "deleted": True,
            "key": key
        })

    except Exception as exc:

        print(
            "R2 delete API error:",
            repr(exc)
        )

        return jsonify({
            "ok": False,
            "error": str(exc)
        }), 500


# ============================================================
# SAVE MOVIE
# ============================================================

@app.route(
    "/api/movie/save",
    methods=["POST"]
)
@admin_required
def api_movie_save():

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    title = str(
        data.get(
            "title",
            ""
        )
    ).strip()

    category = str(
        data.get(
            "category",
            "Other"
        )
    ).strip()

    description = str(
        data.get(
            "description",
            ""
        )
    ).strip()

    video_key = clean_r2_key(
        data.get(
            "video_key",
            ""
        )
    )

    poster_key = clean_r2_key(
        data.get(
            "poster_key",
            ""
        )
    )

    if not title:

        return jsonify({
            "ok": False,
            "error":
                "Movie title is required."
        }), 400

    if not validate_r2_key(
        video_key,
        "videos/"
    ):

        return jsonify({
            "ok": False,
            "error":
                "Invalid video key."
        }), 400

    if poster_key:

        if not validate_r2_key(
            poster_key,
            "posters/"
        ):

            return jsonify({
                "ok": False,
                "error":
                    "Invalid poster key."
            }), 400

    # ========================================================
    # VIDEO MUST EXIST BEFORE DB SAVE
    # ========================================================

    if not r2_object_exists(
        video_key
    ):

        return jsonify({
            "ok": False,
            "error":
                "Video is not completed in R2 yet."
        }), 400

    if poster_key:

        if not r2_object_exists(
            poster_key
        ):

            return jsonify({
                "ok": False,
                "error":
                    "Poster does not exist in R2."
            }), 400

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
            (
                %s,
                %s,
                %s,
                %s,
                %s,
                0
            )
            RETURNING id
        """, (
            title,
            category,
            description,
            poster_key,
            video_key
        ))

        movie_id = cur.fetchone()[
            "id"
        ]

        db.commit()

        print(
            "MOVIE SAVED:",
            movie_id,
            video_key
        )

        return jsonify({
            "ok": True,
            "saved": True,
            "movie_id":
                movie_id,
            "video_key":
                video_key,
            "poster_key":
                poster_key,
            "public_url":
                r2_object_url(
                    video_key
                ),
            "poster_url":
                (
                    r2_object_url(
                        poster_key
                    )
                    if poster_key
                    else ""
                )
        })

    except Exception as exc:

        db.rollback()

        print(
            "Movie save error:",
            repr(exc)
        )

        return jsonify({
            "ok": False,
            "error":
                str(exc)
        }), 500

    finally:
        db.close()


# ============================================================
# ADS.TXT
# ============================================================

@app.route("/ads.txt")
def ads_txt():

    return Response(
        "google.com, pub-8697157365303435, DIRECT, f08c47fec0942fa0\n",
        mimetype="text/plain"
    )


# ============================================================
# ERROR HANDLERS
# ============================================================

@app.errorhandler(413)
def file_too_large(error):

    return (
        "File too large. Maximum allowed size is 4 GB.",
        413
    )


@app.errorhandler(404)
def page_not_found(error):

    return (
        "Page not found.",
        404
    )


@app.errorhandler(500)
def internal_error(error):

    print(
        "500 error:",
        repr(error)
    )

    return (
        "Internal Server Error",
        500
    )


# ============================================================
# STARTUP
# ============================================================

try:

    init_db()

except Exception as startup_error:

    print(
        "Database initialization error:",
        repr(startup_error)
    )


# ============================================================
# LOCAL RUN
# ============================================================

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
