import os
import secrets
from functools import wraps
from urllib.parse import quote

import boto3
from botocore.client import Config
import psycopg2
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
# FLASK APP
# =========================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    secrets.token_hex(32)
)

# Browser -> R2 direct upload होने के कारण
# Render को 4 GB body receive नहीं करनी पड़ेगी.
# फिर भी normal routes के लिए 4 GB limit रखी है.
app.config["MAX_CONTENT_LENGTH"] = (
    4 * 1024 * 1024 * 1024
)


# =========================================================
# ADMIN
# =========================================================

ADMIN_USER = os.environ.get(
    "ADMIN_USER",
    "admin"
)

ADMIN_PASSWORD = os.environ.get(
    "ADMIN_PASSWORD",
    "change-me-now"
)


# =========================================================
# DATABASE
# =========================================================

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    ""
).strip()


class DB:

    def __init__(self, url):

        if not url:
            raise RuntimeError(
                "DATABASE_URL environment variable is missing."
            )

        if not (
            url.startswith("postgresql://")
            or url.startswith("postgres://")
        ):
            raise RuntimeError(
                "DATABASE_URL गलत है."
            )

        self.con = psycopg2.connect(
            url,
            cursor_factory=RealDictCursor,
            connect_timeout=10
        )

    def execute(self, query, params=None):

        cursor = self.con.cursor()

        if params is None:
            cursor.execute(query)
        else:
            cursor.execute(
                query,
                params
            )

        return cursor

    def commit(self):
        self.con.commit()

    def rollback(self):
        self.con.rollback()

    def close(self):
        self.con.close()


def db():
    return DB(DATABASE_URL)


# =========================================================
# CLOUDFLARE R2
# =========================================================

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
    ""
).strip()

R2_ENDPOINT = os.environ.get(
    "R2_ENDPOINT",
    ""
).strip()

R2_PUBLIC_URL = os.environ.get(
    "R2_PUBLIC_URL",
    ""
).strip()


def get_r2_client():

    if not R2_ACCESS_KEY_ID:
        raise RuntimeError(
            "R2_ACCESS_KEY_ID missing."
        )

    if not R2_SECRET_ACCESS_KEY:
        raise RuntimeError(
            "R2_SECRET_ACCESS_KEY missing."
        )

    if not R2_BUCKET:
        raise RuntimeError(
            "R2_BUCKET missing."
        )

    if R2_ENDPOINT:
        endpoint_url = R2_ENDPOINT
    else:

        if not R2_ACCOUNT_ID:
            raise RuntimeError(
                "R2_ACCOUNT_ID missing."
            )

        endpoint_url = (
            "https://"
            + R2_ACCOUNT_ID
            + ".r2.cloudflarestorage.com"
        )

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=Config(
            signature_version="s3v4"
        )
    )


# =========================================================
# R2 PUBLIC URL
# =========================================================

def r2_public_url(object_key):

    if not object_key:
        return ""

    if not R2_PUBLIC_URL:
        return ""

    return (
        R2_PUBLIC_URL.rstrip("/")
        + "/"
        + quote(
            object_key,
            safe="/"
        )
    )


def r2_presigned_url(object_key):

    if not object_key:
        return ""

    try:

        client = get_r2_client()

        return client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": R2_BUCKET,
                "Key": object_key
            },
            ExpiresIn=3600
        )

    except Exception as e:

        app.logger.exception(
            "Presigned GET URL failed: %s",
            e
        )

        return ""


def r2_delete(object_key):

    if not object_key:
        return

    client = get_r2_client()

    client.delete_object(
        Bucket=R2_BUCKET,
        Key=object_key
    )


# =========================================================
# DIRECT R2 MULTIPART UPLOAD
# =========================================================

# 20 MB chunks
# R2/S3 multipart में last part को छोड़कर parts >= 5 MB होने चाहिए.
PART_SIZE = 20 * 1024 * 1024

# एक साथ इतने chunks upload होंगे
PARALLEL_PARTS = 8


@app.route(
    "/api/r2/multipart/create",
    methods=["POST"]
)
@admin_required
def create_multipart():

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

    if not filename:
        return jsonify({
            "ok": False,
            "error": "Filename missing."
        }), 400

    safe_name = secure_filename(
        filename
    )

    if not safe_name:
        return jsonify({
            "ok": False,
            "error": "Invalid filename."
        }), 400

    if not ext_ok(
        safe_name,
        ALLOWED_VIDEOS
    ):
        return jsonify({
            "ok": False,
            "error": (
                "Video केवल MP4, WebM या MOV होनी चाहिए."
            )
        }), 400

    base, extension = os.path.splitext(
        safe_name
    )

    extension = extension.lower()

    object_key = (
        "videos/"
        + base
        + "_"
        + secrets.token_hex(8)
        + extension
    )

    try:

        client = get_r2_client()

        result = client.create_multipart_upload(
            Bucket=R2_BUCKET,
            Key=object_key,
            ContentType=content_type
        )

        upload_id = result["UploadId"]

        return jsonify({
            "ok": True,
            "upload_id": upload_id,
            "key": object_key,
            "part_size": PART_SIZE,
            "parallel": PARALLEL_PARTS
        })

    except Exception as e:

        app.logger.exception(
            "Create multipart failed: %s",
            e
        )

        return jsonify({
            "ok": False,
            "error": "R2 multipart upload शुरू नहीं हो पाया."
        }), 500


# =========================================================
# GET PRESIGNED PART URLS
# =========================================================

@app.route(
    "/api/r2/multipart/urls",
    methods=["POST"]
)
@admin_required
def multipart_urls():

    data = request.get_json(
        silent=True
    ) or {}

    upload_id = str(
        data.get("upload_id", "")
    ).strip()

    object_key = str(
        data.get("key", "")
    ).strip()

    parts = data.get(
        "parts",
        []
    )

    if not upload_id or not object_key:
        return jsonify({
            "ok": False,
            "error": "Upload information missing."
        }), 400

    if not isinstance(parts, list):
        return jsonify({
            "ok": False,
            "error": "Parts invalid."
        }), 400

    if len(parts) > 10000:
        return jsonify({
            "ok": False,
            "error": "Too many parts."
        }), 400

    try:

        client = get_r2_client()

        urls = []

        for part_number in parts:

            part_number = int(
                part_number
            )

            if part_number < 1 or part_number > 10000:
                raise ValueError(
                    "Invalid part number."
                )

            url = client.generate_presigned_url(
                "upload_part",
                Params={
                    "Bucket": R2_BUCKET,
                    "Key": object_key,
                    "UploadId": upload_id,
                    "PartNumber": part_number
                },
                ExpiresIn=3600
            )

            urls.append({
                "part_number": part_number,
                "url": url
            })

        return jsonify({
            "ok": True,
            "urls": urls
        })

    except Exception as e:

        app.logger.exception(
            "Generate multipart URLs failed: %s",
            e
        )

        return jsonify({
            "ok": False,
            "error": "Upload URLs generate नहीं हुए."
        }), 500


# =========================================================
# COMPLETE MULTIPART
# =========================================================

@app.route(
    "/api/r2/multipart/complete",
    methods=["POST"]
)
@admin_required
def complete_multipart():

    data = request.get_json(
        silent=True
    ) or {}

    upload_id = str(
        data.get("upload_id", "")
    ).strip()

    object_key = str(
        data.get("key", "")
    ).strip()

    parts = data.get(
        "parts",
        []
    )

    if not upload_id or not object_key:
        return jsonify({
            "ok": False,
            "error": "Upload information missing."
        }), 400

    if not parts:
        return jsonify({
            "ok": False,
            "error": "No uploaded parts."
        }), 400

    try:

        clean_parts = []

        for part in parts:

            part_number = int(
                part["PartNumber"]
            )

            etag = str(
                part["ETag"]
            ).strip()

            if etag.startswith('"') and etag.endswith('"'):
                etag = etag[1:-1]

            clean_parts.append({
                "PartNumber": part_number,
                "ETag": etag
            })

        clean_parts.sort(
            key=lambda x: x["PartNumber"]
        )

        client = get_r2_client()

        result = client.complete_multipart_upload(
            Bucket=R2_BUCKET,
            Key=object_key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": clean_parts
            }
        )

        return jsonify({
            "ok": True,
            "key": object_key,
            "url": r2_public_url(
                object_key
            ),
            "result": {
                "location": result.get(
                    "Location",
                    ""
                )
            }
        })

    except Exception as e:

        app.logger.exception(
            "Complete multipart failed: %s",
            e
        )

        return jsonify({
            "ok": False,
            "error": (
                "R2 multipart upload complete नहीं हुआ."
            )
        }), 500


# =========================================================
# ABORT MULTIPART
# =========================================================

@app.route(
    "/api/r2/multipart/abort",
    methods=["POST"]
)
@admin_required
def abort_multipart():

    data = request.get_json(
        silent=True
    ) or {}

    upload_id = str(
        data.get("upload_id", "")
    ).strip()

    object_key = str(
        data.get("key", "")
    ).strip()

    if not upload_id or not object_key:

        return jsonify({
            "ok": True
        })

    try:

        client = get_r2_client()

        client.abort_multipart_upload(
            Bucket=R2_BUCKET,
            Key=object_key,
            UploadId=upload_id
        )

    except Exception as e:

        app.logger.exception(
            "Abort multipart failed: %s",
            e
        )

    return jsonify({
        "ok": True
    })


# =========================================================
# ALLOWED FILE TYPES
# =========================================================

ALLOWED_POSTERS = {
    "jpg",
    "jpeg",
    "png",
    "webp",
}

ALLOWED_VIDEOS = {
    "mp4",
    "webm",
    "mov",
}


def ext_ok(filename, allowed):

    if not filename:
        return False

    if "." not in filename:
        return False

    extension = filename.rsplit(
        ".",
        1
    )[1].lower()

    return extension in allowed


def get_content_type(
    filename,
    default
):

    extension = os.path.splitext(
        filename
    )[1].lower()

    content_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
    }

    return content_types.get(
        extension,
        default
    )


# =========================================================
# LOCAL DIRECTORIES
# =========================================================

BASE = os.path.dirname(
    os.path.abspath(__file__)
)

UPLOAD_ROOT = os.path.join(
    BASE,
    "uploads"
)

POSTER_DIR = os.path.join(
    UPLOAD_ROOT,
    "posters"
)

VIDEO_DIR = os.path.join(
    UPLOAD_ROOT,
    "videos"
)

os.makedirs(
    POSTER_DIR,
    exist_ok=True
)

os.makedirs(
    VIDEO_DIR,
    exist_ok=True
)


# =========================================================
# ADMIN REQUIRED
# =========================================================

def admin_required(function):

    @wraps(function)
    def wrapper(*args, **kwargs):

        if not session.get("admin"):

            return redirect(
                url_for("login")
            )

        return function(
            *args,
            **kwargs
        )

    return wrapper


# =========================================================
# DATABASE INITIALIZATION
# =========================================================

def init_db():

    con = db()

    try:

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS movies (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                category TEXT DEFAULT '',
                description TEXT DEFAULT '',
                poster TEXT DEFAULT '',
                video TEXT NOT NULL,
                views INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT DEFAULT ''
            )
            """
        )

        defaults = {
            "ad_top": "",
            "ad_player": "",
            "ad_bottom": "",
        }

        for key, value in defaults.items():

            con.execute(
                """
                INSERT INTO settings(
                    key,
                    value
                )
                VALUES (%s, %s)
                ON CONFLICT(key)
                DO NOTHING
                """,
                (
                    key,
                    value
                )
            )

        con.commit()

    finally:

        con.close()


# =========================================================
# SETTINGS
# =========================================================

def get_settings():

    con = db()

    try:

        rows = con.execute(
            """
            SELECT key, value
            FROM settings
            ORDER BY key
            """
        ).fetchall()

        return {
            row["key"]: row["value"]
            for row in rows
        }

    finally:

        con.close()


# =========================================================
# TEMPLATE GLOBALS
# =========================================================

@app.context_processor
def inject_global_data():

    try:

        ads = get_settings()

    except Exception:

        ads = {
            "ad_top": "",
            "ad_player": "",
            "ad_bottom": "",
        }

    return {
        "ads": ads
    }


# =========================================================
# HOME
# =========================================================

@app.route("/")
def home():

    q = request.args.get(
        "q",
        ""
    ).strip()

    category = request.args.get(
        "category",
        ""
    ).strip()

    con = db()

    try:

        if q:

            movies = con.execute(
                """
                SELECT *
                FROM movies
                WHERE
                    title ILIKE %s
                    OR category ILIKE %s
                ORDER BY id DESC
                """,
                (
                    f"%{q}%",
                    f"%{q}%"
                )
            ).fetchall()

        elif category:

            movies = con.execute(
                """
                SELECT *
                FROM movies
                WHERE category = %s
                ORDER BY id DESC
                """,
                (
                    category,
                )
            ).fetchall()

        else:

            movies = con.execute(
                """
                SELECT *
                FROM movies
                ORDER BY id DESC
                """
            ).fetchall()

        cats = con.execute(
            """
            SELECT DISTINCT category
            FROM movies
            WHERE
                category IS NOT NULL
                AND category <> ''
            ORDER BY category
            """
        ).fetchall()

        categories = [
            row["category"]
            for row in cats
        ]

        return render_template(
            "index.html",
            movies=movies,
            categories=categories,
            q=q,
            category=category
        )

    finally:

        con.close()


# =========================================================
# MOVIE PAGE
# =========================================================

@app.route("/movie/<int:movie_id>")
def movie(movie_id):

    con = db()

    try:

        movie_data = con.execute(
            """
            SELECT *
            FROM movies
            WHERE id = %s
            """,
            (
                movie_id,
            )
        ).fetchone()

        if not movie_data:
            abort(404)

        con.execute(
            """
            UPDATE movies
            SET views = COALESCE(views, 0) + 1
            WHERE id = %s
            """,
            (
                movie_id,
            )
        )

        con.commit()

        movie_data = dict(
            movie_data
        )

        poster_key = movie_data.get(
            "poster",
            ""
        )

        video_key = movie_data.get(
            "video",
            ""
        )

        poster_url = r2_public_url(
            poster_key
        )

        video_url = r2_public_url(
            video_key
        )

        if poster_key and not poster_url:

            poster_url = r2_presigned_url(
                poster_key
            )

        if video_key and not video_url:

            video_url = r2_presigned_url(
                video_key
            )

        movie_data["poster_url"] = poster_url
        movie_data["video_url"] = video_url

        return render_template(
            "movie.html",
            movie=movie_data
        )

    finally:

        con.close()


# =========================================================
# POSTER ROUTE
# =========================================================

@app.route("/poster/<path:name>")
def poster(name):

    public_url = r2_public_url(
        name
    )

    if public_url:

        return redirect(
            public_url
        )

    signed_url = r2_presigned_url(
        name
    )

    if signed_url:

        return redirect(
            signed_url
        )

    local_path = os.path.join(
        POSTER_DIR,
        os.path.basename(name)
    )

    if os.path.isfile(local_path):

        from flask import send_from_directory

        return send_from_directory(
            POSTER_DIR,
            os.path.basename(name)
        )

    abort(404)


# =========================================================
# VIDEO ROUTE
# =========================================================

@app.route("/video/<path:name>")
def video(name):

    public_url = r2_public_url(
        name
    )

    if public_url:

        return redirect(
            public_url
        )

    signed_url = r2_presigned_url(
        name
    )

    if signed_url:

        return redirect(
            signed_url
        )

    local_path = os.path.join(
        VIDEO_DIR,
        os.path.basename(name)
    )

    if os.path.isfile(local_path):

        from flask import send_file

        return send_file(
            local_path,
            conditional=True
        )

    abort(404)


# =========================================================
# ADS.TXT
# =========================================================

@app.route("/ads.txt")
def ads_txt():

    content = (
        "google.com, "
        "pub-8697157365303435, "
        "DIRECT, "
        "f08c47fec0942fa0"
    )

    return Response(
        content + "\n",
        mimetype="text/plain"
    )


# =========================================================
# HEALTH
# =========================================================

@app.route("/health")
def health():

    con = None

    try:

        con = db()

        con.execute(
            "SELECT 1"
        ).fetchone()

        return "OK", 200

    except Exception as e:

        app.logger.exception(
            "Health failed: %s",
            e
        )

        return "Database error", 500

    finally:

        if con:
            con.close()


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

        return "R2 OK", 200

    except Exception as e:

        app.logger.exception(
            "R2 health failed: %s",
            e
        )

        return "R2 error", 500


# =========================================================
# LOGIN
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

        username_ok = secrets.compare_digest(
            username,
            ADMIN_USER
        )

        password_ok = secrets.compare_digest(
            password,
            ADMIN_PASSWORD
        )

        if username_ok and password_ok:

            session["admin"] = True

            return redirect(
                url_for("admin")
            )

        flash(
            "गलत username या password."
        )

    return render_template(
        "login.html"
    )


# =========================================================
# LOGOUT
# =========================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("home")
    )


# =========================================================
# ADMIN
# =========================================================

@app.route("/admin")
@admin_required
def admin():

    con = db()

    try:

        movies = con.execute(
            """
            SELECT *
            FROM movies
            ORDER BY id DESC
            """
        ).fetchall()

        ads = get_settings()

        return render_template(
            "admin.html",
            movies=movies,
            ads=ads
        )

    finally:

        con.close()


# =========================================================
# SAVE MOVIE AFTER DIRECT R2 UPLOAD
# =========================================================

@app.route(
    "/api/movie/save",
    methods=["POST"]
)
@admin_required
def save_movie():

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

    video_key = str(
        data.get("video_key", "")
    ).strip()

    poster_key = str(
        data.get("poster_key", "")
    ).strip()

    if not title:

        return jsonify({
            "ok": False,
            "error": "Movie title जरूरी है."
        }), 400

    if not video_key:

        return jsonify({
            "ok": False,
            "error": "Video upload missing."
        }), 400

    con = None

    try:

        con = db()

        row = con.execute(
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
            VALUES (
                %s,
                %s,
                %s,
                %s,
                %s,
                0
            )
            RETURNING id
            """,
            (
                title,
                category,
                description,
                poster_key,
                video_key
            )
        ).fetchone()

        con.commit()

        return jsonify({
            "ok": True,
            "id": row["id"],
            "message": "Movie publish हो गई."
        })

    except Exception as e:

        if con:
            con.rollback()

        app.logger.exception(
            "Save movie failed: %s",
            e
        )

        return jsonify({
            "ok": False,
            "error": "Movie database में save नहीं हुई."
        }), 500

    finally:

        if con:
            con.close()


# =========================================================
# SAVE ADS
# =========================================================

@app.route(
    "/admin/ads",
    methods=["POST"]
)
@admin_required
def save_ads():

    con = None

    try:

        con = db()

        for key in (
            "ad_top",
            "ad_player",
            "ad_bottom"
        ):

            value = request.form.get(
                key,
                ""
            )

            con.execute(
                """
                INSERT INTO settings(
                    key,
                    value
                )
                VALUES (%s, %s)
                ON CONFLICT(key)
                DO UPDATE SET
                    value = EXCLUDED.value
                """,
                (
                    key,
                    value
                )
            )

        con.commit()

        flash(
            "Ads settings save हो गईं."
        )

    except Exception as e:

        if con:
            con.rollback()

        app.logger.exception(
            "Save ads failed: %s",
            e
        )

        flash(
            "Ads save नहीं हो पाईं."
        )

    finally:

        if con:
            con.close()

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
def delete_movie(movie_id):

    con = None

    try:

        con = db()

        movie_data = con.execute(
            """
            SELECT *
            FROM movies
            WHERE id = %s
            """,
            (
                movie_id,
            )
        ).fetchone()

        if not movie_data:

            flash(
                "Movie नहीं मिली."
            )

            return redirect(
                url_for("admin")
            )

        video_name = movie_data.get(
            "video"
        )

        poster_name = movie_data.get(
            "poster"
        )

        con.execute(
            """
            DELETE FROM movies
            WHERE id = %s
            """,
            (
                movie_id,
            )
        )

        con.commit()

        if video_name:

            try:
                r2_delete(
                    video_name
                )
            except Exception as e:

                app.logger.exception(
                    "R2 video delete failed: %s",
                    e
                )

        if poster_name:

            try:
                r2_delete(
                    poster_name
                )
            except Exception as e:

                app.logger.exception(
                    "R2 poster delete failed: %s",
                    e
                )

        flash(
            "Movie delete हो गई."
        )

    except Exception as e:

        if con:
            con.rollback()

        app.logger.exception(
            "Delete movie failed: %s",
            e
        )

        flash(
            "Movie delete नहीं हो पाई."
        )

    finally:

        if con:
            con.close()

    return redirect(
        url_for("admin")
    )


# =========================================================
# 404
# =========================================================

@app.errorhandler(404)
def not_found(error):

    return render_template(
        "base.html"
    ), 404


# =========================================================
# 413
# =========================================================

@app.errorhandler(413)
def too_large(error):

    flash(
        "File बहुत बड़ी है. Maximum upload size 4 GB है."
    )

    return redirect(
        url_for("admin")
    )


# =========================================================
# 500
# =========================================================

@app.errorhandler(500)
def internal_error(error):

    app.logger.exception(
        "Internal server error: %s",
        error
    )

    return (
        "Internal Server Error. Render Logs देखें.",
        500
    )


# =========================================================
# STARTUP
# =========================================================

try:

    init_db()

except Exception as e:

    app.logger.exception(
        "Database initialization failed: %s",
        e
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            5000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
