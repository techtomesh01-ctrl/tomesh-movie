import os
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

app.secret_key = os.environ.get(
    "SECRET_KEY",
    secrets.token_hex(32)
)

# 4 GB server request limit
# Direct R2 upload is still used by the Admin Panel.
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024


# ============================================================
# ADMIN
# ============================================================

ADMIN_USER = os.environ.get(
    "ADMIN_USER",
    "admin"
)

ADMIN_PASSWORD = os.environ.get(
    "ADMIN_PASSWORD",
    "change-me-now"
)


# ============================================================
# DATABASE
# ============================================================

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    ""
)


def get_db():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not configured"
        )

    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor,
        sslmode="require",
    )


def init_db():
    conn = get_db()

    try:
        cur = conn.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS movies (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                category TEXT DEFAULT 'Other',
                description TEXT DEFAULT '',
                poster TEXT DEFAULT '',
                video TEXT NOT NULL,
                views INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ads (
                id SERIAL PRIMARY KEY,
                slot TEXT UNIQUE NOT NULL,
                code TEXT DEFAULT ''
            )
            """
        )

        for slot in (
            "top",
            "player",
            "bottom"
        ):
            cur.execute(
                """
                INSERT INTO ads (slot, code)
                VALUES (%s, '')
                ON CONFLICT (slot) DO NOTHING
                """,
                (slot,),
            )

        conn.commit()

    finally:
        conn.close()


# ============================================================
# ENVIRONMENT CLEANER
# ============================================================

def clean_env(name, default=""):
    value = os.environ.get(
        name,
        default
    )

    if value is None:
        return ""

    return (
        str(value)
        .replace("\r", "")
        .replace("\n", "")
        .strip()
    )


# ============================================================
# R2 CONFIG
# ============================================================

R2_ACCOUNT_ID = clean_env(
    "R2_ACCOUNT_ID"
)

R2_ACCESS_KEY_ID = clean_env(
    "R2_ACCESS_KEY_ID"
)

R2_SECRET_ACCESS_KEY = clean_env(
    "R2_SECRET_ACCESS_KEY"
)

R2_BUCKET = clean_env(
    "R2_BUCKET",
    "tomesh-movies"
)

R2_ENDPOINT = clean_env(
    "R2_ENDPOINT",
    f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
).rstrip("/")

R2_PUBLIC_URL = clean_env(
    "R2_PUBLIC_URL"
).rstrip("/")


# ============================================================
# FILE TYPES
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


# ============================================================
# MULTIPART SETTINGS
# ============================================================

PART_SIZE = 10 * 1024 * 1024

MAX_PARTS = 10000

PARALLEL_PARTS = 3

PRESIGNED_EXPIRES = 3600


# ============================================================
# R2 CLIENT
# ============================================================

r2 = None

if (
    R2_ACCOUNT_ID
    and R2_ACCESS_KEY_ID
    and R2_SECRET_ACCESS_KEY
):
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


# ============================================================
# HELPERS
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

        return func(
            *args,
            **kwargs
        )

    return wrapper


def extension(filename):

    filename = filename or ""

    if "." not in filename:
        return ""

    return filename.rsplit(
        ".",
        1
    )[1].lower()


def r2_required():

    if r2 is None:
        raise RuntimeError(
            "Cloudflare R2 environment variables are missing"
        )


def validate_r2_key(key):

    if not key or not isinstance(
        key,
        str
    ):
        abort(
            400,
            "Invalid R2 key"
        )

    key = (
        key
        .replace("\r", "")
        .replace("\n", "")
        .strip()
    )

    if ".." in key:
        abort(
            400,
            "Invalid R2 key"
        )

    if not (
        key.startswith("videos/")
        or key.startswith("posters/")
    ):
        abort(
            400,
            "Invalid R2 key"
        )

    return key


def r2_object_url(key):

    if not key:
        return ""

    if R2_PUBLIC_URL:

        return (
            f"{R2_PUBLIC_URL}/"
            f"{quote(key, safe='/')}"
        )

    return ""


def object_exists(key):

    if not key:
        return False

    r2_required()

    try:

        r2.head_object(
            Bucket=R2_BUCKET,
            Key=key,
        )

        return True

    except Exception as e:

        print(
            "R2 object check failed:",
            str(e)
        )

        return False


def delete_object(key):

    if not key:
        return

    if r2 is None:
        return

    try:

        r2.delete_object(
            Bucket=R2_BUCKET,
            Key=key,
        )

    except Exception as e:

        print(
            "R2 delete failed:",
            str(e)
        )


# ============================================================
# VIDEO MIME TYPE
# ============================================================

def video_mime_type(key):

    """
    Browser ko video ka correct MIME type dene ke liye.
    """

    key = (key or "").lower()

    if key.endswith(".mp4"):
        return "video/mp4"

    if key.endswith(".webm"):
        return "video/webm"

    if key.endswith(".mov"):
        return "video/quicktime"

    if key.endswith(".mkv"):
        return "video/x-matroska"

    mime = mimetypes.guess_type(key)[0]

    if mime:
        return mime

    return "application/octet-stream"


# ============================================================
# MOVIE VIDEO URL
# ============================================================

def movie_video_url(movie):

    video = movie.get(
        "video",
        ""
    )

    if not video:
        return ""

    if (
        video.startswith("http://")
        or video.startswith("https://")
    ):
        return video

    return r2_object_url(
        video
    )


# ============================================================
# MOVIE POSTER URL
# ============================================================

def movie_poster_url(movie):

    poster = movie.get(
        "poster",
        ""
    )

    if not poster:
        return ""

    if (
        poster.startswith("http://")
        or poster.startswith("https://")
    ):
        return poster

    return r2_object_url(
        poster
    )


# ============================================================
# GET ADS
# ============================================================

def get_ads():

    conn = get_db()

    try:

        cur = conn.cursor()

        cur.execute(
            """
            SELECT slot, code
            FROM ads
            ORDER BY id
            """
        )

        rows = cur.fetchall()

        return {
            row["slot"]:
            row["code"] or ""
            for row in rows
        }

    finally:
        conn.close()


# ============================================================
# HOME
# ============================================================

@app.route("/")
def index():

    conn = get_db()

    try:

        cur = conn.cursor()

        cur.execute(
            """
            SELECT *
            FROM movies
            ORDER BY created_at DESC
            """
        )

        movies = cur.fetchall()

        for movie in movies:

            movie["poster_url"] = (
                movie_poster_url(movie)
            )

        ads = get_ads()

        return render_template(
            "index.html",
            movies=movies,
            ads=ads,
        )

    finally:
        conn.close()


# ============================================================
# MOVIE PAGE
# ============================================================

@app.route(
    "/movie/<int:movie_id>"
)
def movie_page(movie_id):

    conn = get_db()

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
            abort(404)

        cur.execute(
            """
            UPDATE movies
            SET views = COALESCE(views, 0) + 1
            WHERE id = %s
            """,
            (movie_id,),
        )

        conn.commit()

        movie["views"] = (
            movie.get("views") or 0
        ) + 1

        # ====================================================
        # IMPORTANT PLAYBACK FIX
        # ====================================================

        # Browser ab direct public R2 URL ko source nahi lega.
        # Pehle /video/<id> route hit karega.
        # Woh route R2 presigned GET URL par redirect karega.

        movie["video_url"] = url_for(
            "video_redirect",
            movie_id=movie_id
        )

        movie["video_mime"] = (
            video_mime_type(
                movie.get("video", "")
            )
        )

        movie["poster_url"] = (
            movie_poster_url(movie)
        )

        cur.execute(
            """
            SELECT *
            FROM movies
            WHERE id != %s
            ORDER BY created_at DESC
            LIMIT 12
            """,
            (movie_id,),
        )

        more_movies = cur.fetchall()

        for item in more_movies:

            item["poster_url"] = (
                movie_poster_url(item)
            )

        ads = get_ads()

        return render_template(
            "movie.html",
            movie=movie,

            # Both names are supplied.
            # This prevents template variable mismatch.
            more_movies=more_movies,
            related_movies=more_movies,

            ads=ads,
        )

    finally:
        conn.close()


# ============================================================
# VIDEO PLAYBACK ROUTE
# ============================================================

@app.route(
    "/video/<int:movie_id>"
)
def video_redirect(movie_id):

    """
    IMPORTANT:

    Render video ko proxy nahi karega.

    Browser:
        /video/5
             ↓
        Flask creates R2 presigned GET URL
             ↓
        302 redirect
             ↓
        Cloudflare R2
             ↓
        Browser video player
    """

    try:

        r2_required()

        conn = get_db()

        try:

            cur = conn.cursor()

            cur.execute(
                """
                SELECT id, video
                FROM movies
                WHERE id = %s
                """,
                (movie_id,),
            )

            movie = cur.fetchone()

        finally:

            conn.close()

        if not movie:

            abort(
                404,
                "Movie not found"
            )

        video_key = (
            movie.get("video") or ""
        ).strip()

        if not video_key:

            abort(
                404,
                "Video not found"
            )

        # Old records may contain a full URL.
        if (
            video_key.startswith("http://")
            or video_key.startswith("https://")
        ):

            return redirect(
                video_key,
                code=302
            )

        # Only allow our video folder.
        if not video_key.startswith(
            "videos/"
        ):

            abort(
                404,
                "Invalid video key"
            )

        # Check that object really exists.
        try:

            head = r2.head_object(
                Bucket=R2_BUCKET,
                Key=video_key,
            )

        except Exception as e:

            print(
                "VIDEO R2 HEAD ERROR:",
                str(e)
            )

            abort(
                404,
                "Video file not found in R2"
            )

        object_size = int(
            head.get(
                "ContentLength",
                0
            )
        )

        if object_size <= 0:

            abort(
                404,
                "Video file is empty"
            )

        mime = video_mime_type(
            video_key
        )

        # ====================================================
        # PRESIGNED GET URL
        # ====================================================

        signed_url = (
            r2.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": R2_BUCKET,
                    "Key": video_key,
                    "ResponseContentType": mime,
                },
                ExpiresIn=PRESIGNED_EXPIRES,
            )
        )

        print(
            "VIDEO PLAYBACK:",
            video_key,
            "|",
            mime,
            "|",
            object_size,
            "bytes"
        )

        return redirect(
            signed_url,
            code=302
        )

    except Exception as e:

        print(
            "VIDEO PLAYBACK ERROR:",
            str(e)
        )

        abort(
            404,
            "Unable to load video"
        )


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
        )

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
            "Invalid username or password",
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
# ADMIN DASHBOARD
# ============================================================

@app.route("/admin")
@admin_required
def admin():

    conn = get_db()

    try:

        cur = conn.cursor()

        cur.execute(
            """
            SELECT *
            FROM movies
            ORDER BY created_at DESC
            """
        )

        movies = cur.fetchall()

        cur.execute(
            """
            SELECT
                COUNT(*) AS total_movies,
                COALESCE(SUM(views), 0)
                AS total_views
            FROM movies
            """
        )

        stats = cur.fetchone()

        for movie in movies:

            movie["poster_url"] = (
                movie_poster_url(movie)
            )

        ads = get_ads()

        return render_template(
            "admin.html",
            movies=movies,
            stats=stats,
            ads=ads,
        )

    finally:
        conn.close()


# ============================================================
# OLD SERVER UPLOAD
# ============================================================

@app.route(
    "/admin/add",
    methods=["GET", "POST"]
)
@admin_required
def admin_add():

    if request.method == "GET":

        return render_template(
            "admin_add.html"
        )

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

    video_file = request.files.get(
        "video"
    )

    poster_file = request.files.get(
        "poster"
    )

    if not title:

        flash(
            "Movie title is required",
            "error"
        )

        return redirect(
            url_for("admin")
        )

    if (
        not video_file
        or not video_file.filename
    ):

        flash(
            "Video is required",
            "error"
        )

        return redirect(
            url_for("admin")
        )

    video_ext = extension(
        video_file.filename
    )

    if video_ext not in ALLOWED_VIDEOS:

        flash(
            "Video only MP4, MKV, WebM or MOV is allowed",
            "error"
        )

        return redirect(
            url_for("admin")
        )

    r2_required()

    video_key = (
        "videos/"
        f"{secrets.token_hex(16)}_"
        f"{secure_filename(video_file.filename)}"
    )

    poster_key = ""

    try:

        video_mime = (
            video_file.mimetype
            or mimetypes.guess_type(
                video_file.filename
            )[0]
            or video_mime_type(
                video_key
            )
        )

        r2.upload_fileobj(
            video_file,
            R2_BUCKET,
            video_key,
            ExtraArgs={
                "ContentType": video_mime
            },
        )

        if (
            poster_file
            and poster_file.filename
        ):

            poster_ext = extension(
                poster_file.filename
            )

            if poster_ext in ALLOWED_POSTERS:

                poster_key = (
                    "posters/"
                    f"{secrets.token_hex(16)}_"
                    f"{secure_filename(poster_file.filename)}"
                )

                poster_mime = (
                    poster_file.mimetype
                    or mimetypes.guess_type(
                        poster_file.filename
                    )[0]
                    or "application/octet-stream"
                )

                r2.upload_fileobj(
                    poster_file,
                    R2_BUCKET,
                    poster_key,
                    ExtraArgs={
                        "ContentType": poster_mime
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
                VALUES
                    (%s, %s, %s, %s, %s)
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

        finally:

            conn.close()

        flash(
            "Movie added successfully",
            "success"
        )

    except Exception as e:

        delete_object(
            video_key
        )

        delete_object(
            poster_key
        )

        flash(
            f"Upload failed: {str(e)}",
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
    methods=["POST", "GET"]
)
@admin_required
def admin_delete(movie_id):

    conn = get_db()

    try:

        cur = conn.cursor()

        cur.execute(
            """
            SELECT video, poster
            FROM movies
            WHERE id = %s
            """,
            (movie_id,),
        )

        movie = cur.fetchone()

        if not movie:

            abort(404)

        cur.execute(
            """
            DELETE FROM movies
            WHERE id = %s
            """,
            (movie_id,),
        )

        conn.commit()

    finally:

        conn.close()

    delete_object(
        movie.get("video")
    )

    delete_object(
        movie.get("poster")
    )

    flash(
        "Movie deleted",
        "success"
    )

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

    conn = get_db()

    try:

        cur = conn.cursor()

        for slot in (
            "top",
            "player",
            "bottom"
        ):

            value = request.form.get(
                slot,
                ""
            )

            cur.execute(
                """
                INSERT INTO ads (slot, code)
                VALUES (%s, %s)
                ON CONFLICT (slot)
                DO UPDATE
                SET code = EXCLUDED.code
                """,
                (
                    slot,
                    value
                ),
            )

        conn.commit()

    finally:

        conn.close()

    flash(
        "Ads settings saved",
        "success"
    )

    return redirect(
        url_for("admin")
    )


# ============================================================
# R2 HEALTH
# ============================================================

@app.route("/r2-health")
def r2_health():

    try:

        r2_required()

        r2.head_bucket(
            Bucket=R2_BUCKET
        )

        return jsonify({
            "ok": True,
            "message": "R2 OK",
            "bucket": R2_BUCKET,
        })

    except Exception as e:

        return jsonify({
            "ok": False,
            "error": str(e),
        }), 500


# ============================================================
# APP HEALTH
# ============================================================

@app.route("/health")
def health():

    try:

        conn = get_db()

        try:

            cur = conn.cursor()

            cur.execute(
                "SELECT 1"
            )

            cur.fetchone()

        finally:

            conn.close()

        return jsonify({
            "ok": True,
            "database": "OK",
        })

    except Exception as e:

        return jsonify({
            "ok": False,
            "error": str(e),
        }), 500


# ============================================================
# R2 MULTIPART CREATE
# ============================================================

@app.route(
    "/api/r2/multipart/create",
    methods=["POST"]
)
@admin_required
def multipart_create():

    try:

        r2_required()

        data = (
            request.get_json(
                silent=True
            ) or {}
        )

        filename = str(
            data.get(
                "filename",
                ""
            )
        ).strip()

        content_type = str(
            data.get(
                "content_type"
            )
            or "application/octet-stream"
        ).strip()

        size = int(
            data.get(
                "size",
                0
            )
        )

        if not filename:

            return jsonify({
                "ok": False,
                "error": "Filename missing",
            }), 400

        if size <= 0:

            return jsonify({
                "ok": False,
                "error": "Invalid file size",
            }), 400

        # Browser direct upload maximum.
        max_size = 4 * 1024 * 1024 * 1024

        if size > max_size:

            return jsonify({
                "ok": False,
                "error": "Video is larger than 4 GB limit",
            }), 400

        ext = extension(
            filename
        )

        if ext not in ALLOWED_VIDEOS:

            return jsonify({
                "ok": False,
                "error": (
                    "Video only MP4, MKV, WebM "
                    "or MOV is allowed"
                ),
            }), 400

        clean_name = secure_filename(
            filename
        )

        if not clean_name:

            clean_name = "video"

        key = (
            "videos/"
            f"{secrets.token_hex(16)}_"
            f"{clean_name}"
        )

        parts = (
            size + PART_SIZE - 1
        ) // PART_SIZE

        if parts > MAX_PARTS:

            return jsonify({
                "ok": False,
                "error": "Too many multipart parts",
            }), 400

        # If browser sends a generic MIME type,
        # use the extension-based MIME instead.
        if (
            not content_type
            or content_type
            == "application/octet-stream"
        ):

            content_type = (
                video_mime_type(
                    filename
                )
            )

        result = (
            r2.create_multipart_upload(
                Bucket=R2_BUCKET,
                Key=key,
                ContentType=content_type,
            )
        )

        upload_id = result[
            "UploadId"
        ]

        return jsonify({
            "ok": True,
            "key": key,
            "upload_id": upload_id,
            "part_size": PART_SIZE,
            "parts": parts,
            "content_type": content_type,
        })

    except Exception as e:

        print(
            "MULTIPART CREATE ERROR:",
            str(e)
        )

        return jsonify({
            "ok": False,
            "error": str(e),
        }), 500


# ============================================================
# R2 MULTIPART PRESIGNED URLS
# ============================================================

@app.route(
    "/api/r2/multipart/urls",
    methods=["POST"]
)
@admin_required
def multipart_urls():

    try:

        r2_required()

        data = (
            request.get_json(
                silent=True
            ) or {}
        )

        key = data.get(
            "key"
        )

        upload_id = data.get(
            "upload_id"
        )

        parts = data.get(
            "parts"
        )

        if not key or not upload_id:

            return jsonify({
                "ok": False,
                "error": "Missing key or upload_id",
            }), 400

        if not isinstance(
            parts,
            int
        ):

            try:

                parts = int(
                    parts
                )

            except Exception:

                return jsonify({
                    "ok": False,
                    "error": "Invalid parts",
                }), 400

        validate_r2_key(
            key
        )

        if not key.startswith(
            "videos/"
        ):

            return jsonify({
                "ok": False,
                "error": (
                    "Only video multipart is allowed"
                ),
            }), 400

        if (
            parts < 1
            or parts > MAX_PARTS
        ):

            return jsonify({
                "ok": False,
                "error": "Invalid part count",
            }), 400

        urls = []

        for number in range(
            1,
            parts + 1
        ):

            url = (
                r2.generate_presigned_url(
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

            urls.append({
                "part_number": number,
                "url": url,
            })

        return jsonify({
            "ok": True,
            "urls": urls,
        })

    except Exception as e:

        print(
            "MULTIPART URL ERROR:",
            str(e)
        )

        return jsonify({
            "ok": False,
            "error": str(e),
        }), 500


# ============================================================
# R2 MULTIPART COMPLETE
# ============================================================

@app.route(
    "/api/r2/multipart/complete",
    methods=["POST"]
)
@admin_required
def multipart_complete():

    try:

        r2_required()

        data = (
            request.get_json(
                silent=True
            ) or {}
        )

        key = data.get(
            "key"
        )

        upload_id = data.get(
            "upload_id"
        )

        parts = data.get(
            "parts"
        )

        if not key or not upload_id:

            return jsonify({
                "ok": False,
                "error": "Missing key or upload_id",
            }), 400

        if (
            not isinstance(
                parts,
                list
            )
            or not parts
        ):

            return jsonify({
                "ok": False,
                "error": "Parts missing",
            }), 400

        validate_r2_key(
            key
        )

        if not key.startswith(
            "videos/"
        ):

            return jsonify({
                "ok": False,
                "error": "Invalid video key",
            }), 400

        final_parts = []

        for item in parts:

            try:

                number = int(
                    item[
                        "PartNumber"
                    ]
                )

                etag = str(
                    item[
                        "ETag"
                    ]
                ).strip()

            except Exception:

                return jsonify({
                    "ok": False,
                    "error": "Invalid part data",
                }), 400

            if (
                number < 1
                or number > MAX_PARTS
            ):

                return jsonify({
                    "ok": False,
                    "error": "Invalid part number",
                }), 400

            if not etag:

                return jsonify({
                    "ok": False,
                    "error": (
                        f"Missing ETag for part {number}"
                    ),
                }), 400

            if not (
                etag.startswith('"')
                and etag.endswith('"')
            ):

                etag = (
                    f'"{etag}"'
                )

            final_parts.append({
                "PartNumber": number,
                "ETag": etag,
            })

        final_parts.sort(
            key=lambda x:
            x["PartNumber"]
        )

        r2.complete_multipart_upload(
            Bucket=R2_BUCKET,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": final_parts
            },
        )

        head = r2.head_object(
            Bucket=R2_BUCKET,
            Key=key,
        )

        object_size = int(
            head.get(
                "ContentLength",
                0
            )
        )

        if object_size <= 0:

            return jsonify({
                "ok": False,
                "error": (
                    "Completed object is empty"
                ),
            }), 500

        return jsonify({
            "ok": True,
            "key": key,
            "url": r2_object_url(key),
            "size": object_size,
            "content_type": (
                head.get(
                    "ContentType"
                )
                or video_mime_type(key)
            ),
        })

    except Exception as e:

        print(
            "MULTIPART COMPLETE ERROR:",
            str(e)
        )

        return jsonify({
            "ok": False,
            "error": str(e),
        }), 500


# ============================================================
# R2 MULTIPART ABORT
# ============================================================

@app.route(
    "/api/r2/multipart/abort",
    methods=["POST"]
)
@admin_required
def multipart_abort():

    try:

        r2_required()

        data = (
            request.get_json(
                silent=True
            ) or {}
        )

        key = data.get(
            "key"
        )

        upload_id = data.get(
            "upload_id"
        )

        if not key or not upload_id:

            return jsonify({
                "ok": False,
                "error": "Missing key or upload_id",
            }), 400

        validate_r2_key(
            key
        )

        if not key.startswith(
            "videos/"
        ):

            return jsonify({
                "ok": False,
                "error": "Invalid video key",
            }), 400

        r2.abort_multipart_upload(
            Bucket=R2_BUCKET,
            Key=key,
            UploadId=upload_id,
        )

        return jsonify({
            "ok": True,
        })

    except Exception as e:

        return jsonify({
            "ok": False,
            "error": str(e),
        }), 500


# ============================================================
# R2 DELETE API
# ============================================================

@app.route(
    "/api/r2/delete",
    methods=["POST"]
)
@admin_required
def r2_delete_api():

    try:

        r2_required()

        data = (
            request.get_json(
                silent=True
            ) or {}
        )

        key = data.get(
            "key"
        )

        validate_r2_key(
            key
        )

        r2.delete_object(
            Bucket=R2_BUCKET,
            Key=key,
        )

        return jsonify({
            "ok": True,
        })

    except Exception as e:

        return jsonify({
            "ok": False,
            "error": str(e),
        }), 500


# ============================================================
# SAVE MOVIE AFTER DIRECT R2 UPLOAD
# ============================================================

@app.route(
    "/api/movie/save",
    methods=["POST"]
)
@admin_required
def save_movie():

    try:

        data = (
            request.get_json(
                silent=True
            ) or {}
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

        if not title:

            return jsonify({
                "ok": False,
                "error": (
                    "Movie title is required"
                ),
            }), 400

        if not video_key:

            return jsonify({
                "ok": False,
                "error": (
                    "Video key is required"
                ),
            }), 400

        video_key = (
            video_key
            .replace("\r", "")
            .replace("\n", "")
            .strip()
        )

        poster_key = (
            poster_key
            .replace("\r", "")
            .replace("\n", "")
            .strip()
        )

        if not video_key.startswith(
            "videos/"
        ):

            return jsonify({
                "ok": False,
                "error": "Invalid video key",
            }), 400

        if not object_exists(
            video_key
        ):

            return jsonify({
                "ok": False,
                "error": (
                    "Video is not completed in R2"
                ),
            }), 400

        if poster_key:

            if not poster_key.startswith(
                "posters/"
            ):

                return jsonify({
                    "ok": False,
                    "error": "Invalid poster key",
                }), 400

            if not object_exists(
                poster_key
            ):

                return jsonify({
                    "ok": False,
                    "error": (
                        "Poster is not completed in R2"
                    ),
                }), 400

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
                VALUES
                    (%s, %s, %s, %s, %s)
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

            movie = cur.fetchone()

            conn.commit()

        finally:

            conn.close()

        return jsonify({
            "ok": True,
            "movie_id": movie["id"],
            "video_key": video_key,
            "poster_key": poster_key,
            "video_url": r2_object_url(
                video_key
            ),
            "poster_url": r2_object_url(
                poster_key
            ),
            "video_mime": video_mime_type(
                video_key
            ),
        })

    except Exception as e:

        print(
            "SAVE MOVIE ERROR:",
            str(e)
        )

        return jsonify({
            "ok": False,
            "error": str(e),
        }), 500


# ============================================================
# ADS.TXT
# ============================================================

@app.route("/ads.txt")
def ads_txt():

    return Response(
        (
            "google.com, "
            "pub-8697157365303435, "
            "DIRECT, "
            "f08c47fec0942fa0\n"
        ),
        mimetype="text/plain",
    )


# ============================================================
# ERROR HANDLERS
# ============================================================

@app.errorhandler(404)
def not_found(error):

    try:

        return (
            render_template(
                "404.html"
            ),
            404
        )

    except Exception:

        return (
            "404 Not Found",
            404
        )


@app.errorhandler(413)
def file_too_large(error):

    return jsonify({
        "ok": False,
        "error": (
            "File is larger than "
            "4 GB limit"
        ),
    }), 413


@app.errorhandler(500)
def server_error(error):

    try:

        return (
            render_template(
                "500.html"
            ),
            500
        )

    except Exception:

        return (
            "500 Internal Server Error",
            500
        )


# ============================================================
# STARTUP
# ============================================================

try:

    init_db()

    print(
        "PostgreSQL initialized successfully."
    )

except Exception as e:

    print(
        "Database initialization warning:",
        str(e)
    )


# ============================================================
# LOCAL RUN
# ============================================================

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
