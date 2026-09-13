import os
import re
import secrets
import mimetypes
from functools import wraps
from urllib.parse import quote

import boto3
import psycopg2

from botocore.client import Config
from botocore.config import Config as BotocoreConfig

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
    "tomesh-movies-change-this-secret"
)

# Flask request upload limit.
# Direct R2 upload does NOT send the video through Flask,
# but keep this limit for safety.
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024


# =========================================================
# BASIC CONFIG
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


# ---------------------------------------------------------
# IMPORTANT UPLOAD SETTINGS
# ---------------------------------------------------------
#
# 5 MB parts:
# - safer on slow connections
# - a failed part does not waste a huge amount of data
# - works well with automatic retry
#
# 4 parallel:
# - faster on normal connections
# - still reasonable for browser/R2
#
# The browser receives these values from the API.
# ---------------------------------------------------------

MAX_FILE_SIZE = 4 * 1024 * 1024 * 1024

MAX_POSTER_SIZE = 25 * 1024 * 1024

PART_SIZE = 5 * 1024 * 1024

PARALLEL_PARTS = 4

PRESIGNED_EXPIRES = 3600

MAX_MULTIPART_PARTS = 10000


# =========================================================
# ENVIRONMENT HELPERS
# =========================================================

def clean_r2_value(name, default=""):
    """
    Remove accidental spaces/newlines from R2 environment
    variables.

    This is especially important for:
      R2_ACCOUNT_ID
      R2_ACCESS_KEY_ID
      R2_SECRET_ACCESS_KEY
    """

    value = os.environ.get(name, default)

    if value is None:
        return ""

    value = str(value)

    return re.sub(r"\s+", "", value)


# =========================================================
# R2 CONFIG
# =========================================================

R2_ACCOUNT_ID = clean_r2_value(
    "R2_ACCOUNT_ID"
)

R2_ACCESS_KEY_ID = clean_r2_value(
    "R2_ACCESS_KEY_ID"
)

R2_SECRET_ACCESS_KEY = clean_r2_value(
    "R2_SECRET_ACCESS_KEY"
)

R2_BUCKET = os.environ.get(
    "R2_BUCKET",
    "tomesh-movies"
).strip()

R2_ENDPOINT = os.environ.get(
    "R2_ENDPOINT",
    ""
).strip().rstrip("/")

R2_PUBLIC_URL = os.environ.get(
    "R2_PUBLIC_URL",
    ""
).strip().rstrip("/")


# =========================================================
# DATABASE
# =========================================================

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    ""
).strip()


def get_db():

    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL environment variable is missing."
        )

    return psycopg2.connect(
        DATABASE_URL
    )


# =========================================================
# R2 CLIENT
# =========================================================

def get_r2_client():

    if not R2_ACCOUNT_ID:
        raise RuntimeError(
            "R2_ACCOUNT_ID is missing."
        )

    if not R2_ACCESS_KEY_ID:
        raise RuntimeError(
            "R2_ACCESS_KEY_ID is missing."
        )

    if not R2_SECRET_ACCESS_KEY:
        raise RuntimeError(
            "R2_SECRET_ACCESS_KEY is missing."
        )

    if not R2_BUCKET:
        raise RuntimeError(
            "R2_BUCKET is missing."
        )

    if not R2_ENDPOINT:
        raise RuntimeError(
            "R2_ENDPOINT is missing."
        )

    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=BotocoreConfig(
            signature_version="s3v4",
            max_pool_connections=20,
            connect_timeout=30,
            read_timeout=120,
            retries={
                "max_attempts": 5,
                "mode": "adaptive",
            },
            s3={
                "addressing_style": "path"
            },
        ),
    )


# =========================================================
# R2 HELPERS
# =========================================================

def r2_public_url(key):

    key = str(key).lstrip("/")

    if not R2_PUBLIC_URL:
        return ""

    return (
        f"{R2_PUBLIC_URL}/"
        f"{quote(key, safe='/')}"
    )


def r2_presigned_url(key):

    client = get_r2_client()

    return client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": R2_BUCKET,
            "Key": key,
        },
        ExpiresIn=PRESIGNED_EXPIRES,
    )


def r2_delete(key):

    if not key:
        return

    client = get_r2_client()

    try:

        client.delete_object(
            Bucket=R2_BUCKET,
            Key=key,
        )

    except Exception as e:

        print(
            "R2 delete warning:",
            e
        )


def r2_head(key):

    client = get_r2_client()

    return client.head_object(
        Bucket=R2_BUCKET,
        Key=key,
    )


# =========================================================
# FILE HELPERS
# =========================================================

def get_extension(filename):

    filename = filename or ""

    if "." not in filename:
        return ""

    return (
        filename
        .rsplit(".", 1)[1]
        .lower()
        .strip()
    )


def allowed_video(filename):

    return (
        get_extension(filename)
        in ALLOWED_VIDEOS
    )


def allowed_poster(filename):

    return (
        get_extension(filename)
        in ALLOWED_POSTERS
    )


def safe_original_name(filename):

    filename = secure_filename(
        filename or ""
    )

    if not filename:
        return "file"

    return filename


def make_object_key(
    prefix,
    filename
):

    filename = safe_original_name(
        filename
    )

    extension = get_extension(
        filename
    )

    base = filename

    if extension:

        base = filename.rsplit(
            ".",
            1
        )[0]

    base = re.sub(
        r"[^A-Za-z0-9._-]+",
        "-",
        base
    ).strip("-")

    if not base:
        base = "movie"

    token = secrets.token_hex(12)

    if extension:

        return (
            f"{prefix}/"
            f"{base}-{token}."
            f"{extension}"
        )

    return (
        f"{prefix}/"
        f"{base}-{token}"
    )


def guess_content_type(
    filename,
    fallback
):

    content_type, _ = mimetypes.guess_type(
        filename
    )

    if content_type:
        return content_type

    return fallback


# =========================================================
# R2 KEY SECURITY
# =========================================================

def valid_r2_key(key):

    if not key:
        return False

    if not isinstance(
        key,
        str
    ):
        return False

    if key.startswith("/"):
        return False

    if ".." in key:
        return False

    return (
        key.startswith("videos/")
        or key.startswith("posters/")
    )


# =========================================================
# ADMIN AUTH
# =========================================================

def admin_required(view):

    @wraps(view)
    def wrapped(*args, **kwargs):

        if not session.get(
            "admin_logged_in"
        ):

            if request.path.startswith(
                "/api/"
            ):

                return jsonify({
                    "ok": False,
                    "error":
                        "Admin login required."
                }), 401

            return redirect(
                url_for("login")
            )

        return view(
            *args,
            **kwargs
        )

    return wrapped


# =========================================================
# DATABASE INIT
# =========================================================

def init_db():

    conn = get_db()

    try:

        cur = conn.cursor()

        cur.execute(
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

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT DEFAULT ''
            )
            """
        )

        conn.commit()

        cur.close()

    finally:

        conn.close()


# =========================================================
# HOME
# =========================================================

@app.route("/")
def home():

    conn = get_db()

    try:

        cur = conn.cursor()

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

    return render_template(
        "index.html",
        movies=movies
    )


# =========================================================
# MOVIE PAGE
# =========================================================

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
            (movie_id,)
        )

        movie = cur.fetchone()

        if not movie:
            abort(404)

        cur.execute(
            """
            UPDATE movies
            SET views = views + 1
            WHERE id = %s
            """,
            (movie_id,)
        )

        conn.commit()

        cur.close()

    finally:

        conn.close()

    return render_template(
        "movie.html",
        movie=movie
    )


# =========================================================
# POSTER
# =========================================================

@app.route(
    "/poster/<path:name>"
)
def poster(name):

    if not valid_r2_key(name):
        abort(404)

    try:

        url = r2_presigned_url(
            name
        )

        return redirect(url)

    except Exception:

        abort(404)


# =========================================================
# VIDEO
# =========================================================

@app.route(
    "/video/<path:name>"
)
def video(name):

    if not valid_r2_key(name):
        abort(404)

    try:

        url = r2_presigned_url(
            name
        )

        return redirect(url)

    except Exception:

        abort(404)


# =========================================================
# ADS.TXT
# =========================================================

@app.route("/ads.txt")
def ads_txt():

    publisher_id = (
        "pub-8697157365303435"
    )

    text = (
        "google.com, "
        + publisher_id
        + ", DIRECT, "
        + "f08c47fec0942fa0"
    )

    return Response(
        text + "\n",
        mimetype="text/plain"
    )


# =========================================================
# HEALTH
# =========================================================

@app.route("/health")
def health():

    try:

        conn = get_db()

        conn.close()

        return jsonify({
            "ok": True,
            "database": "OK"
        })

    except Exception as e:

        return jsonify({
            "ok": False,
            "database": "ERROR",
            "error": str(e)
        }), 500


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
            "bucket": R2_BUCKET
        })

    except Exception as e:

        return jsonify({
            "ok": False,
            "message": "R2 ERROR",
            "error": str(e)
        }), 500


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
# ADMIN
# =========================================================

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
            ORDER BY id DESC
            """
        )

        movies = cur.fetchall()

        cur.execute(
            """
            SELECT COALESCE(
                SUM(views),
                0
            )
            FROM movies
            """
        )

        total_views = (
            cur.fetchone()[0]
        )

        cur.execute(
            """
            SELECT key, value
            FROM settings
            """
        )

        settings_rows = (
            cur.fetchall()
        )

        settings = {
            row[0]: row[1]
            for row in settings_rows
        }

        cur.close()

    finally:

        conn.close()

    return render_template(
        "admin.html",
        movies=movies,
        total_movies=len(movies),
        total_views=total_views,
        settings=settings
    )


# =========================================================
# R2 MULTIPART CREATE
# =========================================================

@app.route(
    "/api/r2/multipart/create",
    methods=["POST"]
)
@admin_required
def r2_multipart_create():

    try:

        data = request.get_json(
            silent=True
        ) or {}

        filename = (
            data.get("filename")
            or ""
        ).strip()

        try:

            size = int(
                data.get("size")
                or 0
            )

        except Exception:

            return jsonify({
                "ok": False,
                "error":
                    "Invalid file size."
            }), 400

        kind = (
            data.get("kind")
            or ""
        ).strip().lower()

        content_type = (
            data.get("content_type")
            or ""
        ).strip()

        # -------------------------------------------------
        # BASIC VALIDATION
        # -------------------------------------------------

        if not filename:

            return jsonify({
                "ok": False,
                "error":
                    "Filename is required."
            }), 400

        if size <= 0:

            return jsonify({
                "ok": False,
                "error":
                    "Invalid file size."
            }), 400

        if size > MAX_FILE_SIZE:

            return jsonify({
                "ok": False,
                "error":
                    "File is larger than 4 GB."
            }), 400

        extension = get_extension(
            filename
        )

        # -------------------------------------------------
        # VIDEO
        # -------------------------------------------------

        if kind == "video":

            if (
                extension
                not in ALLOWED_VIDEOS
            ):

                return jsonify({
                    "ok": False,
                    "error": (
                        "Video केवल MP4, MKV, "
                        "WebM या MOV होनी चाहिए."
                    )
                }), 400

            prefix = "videos/"

            if not content_type:

                content_type = (
                    guess_content_type(
                        filename,
                        "application/octet-stream"
                    )
                )

        # -------------------------------------------------
        # POSTER
        # -------------------------------------------------

        elif kind == "poster":

            if (
                extension
                not in ALLOWED_POSTERS
            ):

                return jsonify({
                    "ok": False,
                    "error": (
                        "Poster JPG, JPEG, PNG "
                        "या WEBP होना चाहिए."
                    )
                }), 400

            if size > MAX_POSTER_SIZE:

                return jsonify({
                    "ok": False,
                    "error":
                        "Poster maximum 25 MB हो सकता है."
                }), 400

            prefix = "posters/"

            if not content_type:

                content_type = (
                    guess_content_type(
                        filename,
                        "application/octet-stream"
                    )
                )

        else:

            return jsonify({
                "ok": False,
                "error":
                    "Invalid upload type."
            }), 400

        # -------------------------------------------------
        # CREATE OBJECT KEY
        # -------------------------------------------------

        key = make_object_key(
            prefix,
            filename
        )

        client = get_r2_client()

        # -------------------------------------------------
        # CREATE MULTIPART UPLOAD
        # -------------------------------------------------

        result = (
            client.create_multipart_upload(
                Bucket=R2_BUCKET,
                Key=key,
                ContentType=content_type,
            )
        )

        upload_id = result.get(
            "UploadId"
        )

        if not upload_id:

            raise RuntimeError(
                "R2 did not return UploadId."
            )

        return jsonify({
            "ok": True,
            "upload_id": upload_id,
            "key": key,

            # IMPORTANT:
            # Frontend should use these values.
            "part_size": PART_SIZE,
            "parallel": PARALLEL_PARTS,

            "expires": PRESIGNED_EXPIRES,

            "max_file_size":
                MAX_FILE_SIZE,

            "max_parts":
                MAX_MULTIPART_PARTS
        })

    except Exception as e:

        print(
            "R2 multipart create error:",
            repr(e)
        )

        return jsonify({
            "ok": False,
            "error": (
                "R2 multipart create failed: "
                + str(e)
            )
        }), 500


# =========================================================
# R2 MULTIPART PRESIGNED PART URLS
# =========================================================

@app.route(
    "/api/r2/multipart/urls",
    methods=["POST"]
)
@admin_required
def r2_multipart_urls():

    try:

        data = request.get_json(
            silent=True
        ) or {}

        upload_id = (
            data.get("upload_id")
            or ""
        ).strip()

        key = (
            data.get("key")
            or ""
        ).strip()

        parts = data.get(
            "parts"
        )

        if not upload_id:

            return jsonify({
                "ok": False,
                "error":
                    "Upload ID is required."
            }), 400

        if not valid_r2_key(key):

            return jsonify({
                "ok": False,
                "error":
                    "Invalid R2 object key."
            }), 400

        if not isinstance(
            parts,
            list
        ):

            return jsonify({
                "ok": False,
                "error":
                    "Parts must be an array."
            }), 400

        if not parts:

            return jsonify({
                "ok": False,
                "error":
                    "No parts requested."
            }), 400

        if len(parts) > MAX_MULTIPART_PARTS:

            return jsonify({
                "ok": False,
                "error":
                    "Too many parts."
            }), 400

        client = get_r2_client()

        urls = []

        for raw_part_number in parts:

            try:

                part_number = int(
                    raw_part_number
                )

            except Exception:

                return jsonify({
                    "ok": False,
                    "error":
                        "Invalid part number."
                }), 400

            if (
                part_number < 1
                or
                part_number >
                MAX_MULTIPART_PARTS
            ):

                return jsonify({
                    "ok": False,
                    "error":
                        "Invalid part number."
                }), 400

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
            "urls": urls,
            "expires":
                PRESIGNED_EXPIRES
        })

    except Exception as e:

        print(
            "R2 presigned URL error:",
            repr(e)
        )

        return jsonify({
            "ok": False,
            "error": (
                "R2 presigned URL creation failed: "
                + str(e)
            )
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

    try:

        data = request.get_json(
            silent=True
        ) or {}

        upload_id = (
            data.get("upload_id")
            or ""
        ).strip()

        key = (
            data.get("key")
            or ""
        ).strip()

        try:

            expected_size = int(
                data.get(
                    "expected_size"
                )
                or 0
            )

        except Exception:

            return jsonify({
                "ok": False,
                "error":
                    "Invalid expected file size."
            }), 400

        if not upload_id:

            return jsonify({
                "ok": False,
                "error":
                    "Upload ID is required."
            }), 400

        if not valid_r2_key(key):

            return jsonify({
                "ok": False,
                "error":
                    "Invalid R2 object key."
            }), 400

        if expected_size <= 0:

            return jsonify({
                "ok": False,
                "error":
                    "Invalid expected file size."
            }), 400

        if expected_size > MAX_FILE_SIZE:

            return jsonify({
                "ok": False,
                "error":
                    "File is larger than 4 GB."
            }), 400

        client = get_r2_client()

        # -------------------------------------------------
        # GET ALL UPLOADED PARTS
        # -------------------------------------------------

        all_parts = []

        part_marker = None

        while True:

            params = {
                "Bucket":
                    R2_BUCKET,

                "Key":
                    key,

                "UploadId":
                    upload_id,

                "MaxParts":
                    1000,
            }

            if part_marker is not None:

                params[
                    "PartNumberMarker"
                ] = part_marker

            response = client.list_parts(
                **params
            )

            for part in response.get(
                "Parts",
                []
            ):

                all_parts.append(
                    part
                )

            if not response.get(
                "IsTruncated",
                False
            ):

                break

            part_marker = (
                response.get(
                    "NextPartNumberMarker"
                )
            )

            if part_marker is None:
                break

        # -------------------------------------------------
        # CHECK PARTS
        # -------------------------------------------------

        if not all_parts:

            return jsonify({
                "ok": False,
                "error":
                    "R2 has no uploaded parts."
            }), 400

        all_parts.sort(
            key=lambda x:
                int(x["PartNumber"])
        )

        expected_part_count = (
            (
                expected_size
                + PART_SIZE
                - 1
            )
            // PART_SIZE
        )

        if (
            len(all_parts)
            != expected_part_count
        ):

            return jsonify({
                "ok": False,
                "error": (
                    "R2 parts mismatch. "
                    f"Expected "
                    f"{expected_part_count}, "
                    f"found "
                    f"{len(all_parts)}."
                )
            }), 400

        # -------------------------------------------------
        # BUILD COMPLETE LIST
        # -------------------------------------------------

        total_size = 0

        complete_parts = []

        for index, part in enumerate(
            all_parts,
            start=1
        ):

            part_number = int(
                part["PartNumber"]
            )

            if part_number != index:

                return jsonify({
                    "ok": False,
                    "error":
                        "R2 parts are not sequential."
                }), 400

            size = int(
                part.get(
                    "Size",
                    0
                )
            )

            total_size += size

            etag = part.get(
                "ETag"
            )

            if not etag:

                return jsonify({
                    "ok": False,
                    "error": (
                        "Missing ETag for part "
                        f"{part_number}."
                    )
                }), 400

            complete_parts.append({
                "PartNumber":
                    part_number,
                "ETag":
                    etag
            })

        # -------------------------------------------------
        # SIZE CHECK
        # -------------------------------------------------

        if (
            total_size
            != expected_size
        ):

            return jsonify({
                "ok": False,
                "error": (
                    "R2 size mismatch. "
                    f"Expected "
                    f"{expected_size} bytes, "
                    f"found "
                    f"{total_size} bytes."
                )
            }), 400

        # -------------------------------------------------
        # COMPLETE MULTIPART
        # -------------------------------------------------

        result = (
            client.complete_multipart_upload(
                Bucket=R2_BUCKET,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={
                    "Parts":
                        complete_parts
                }
            )
        )

        # -------------------------------------------------
        # FINAL OBJECT CHECK
        # -------------------------------------------------

        head = client.head_object(
            Bucket=R2_BUCKET,
            Key=key
        )

        final_size = int(
            head.get(
                "ContentLength",
                0
            )
        )

        if (
            final_size
            != expected_size
        ):

            return jsonify({
                "ok": False,
                "error":
                    "Final R2 object size mismatch."
            }), 500

        return jsonify({
            "ok": True,
            "key": key,
            "url":
                r2_public_url(key),
            "size":
                final_size,
            "parts":
                len(complete_parts),
            "etag":
                result.get(
                    "ETag",
                    ""
                )
        })

    except Exception as e:

        print(
            "R2 multipart complete error:",
            repr(e)
        )

        return jsonify({
            "ok": False,
            "error": (
                "R2 multipart complete failed: "
                + str(e)
            )
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

    try:

        data = request.get_json(
            silent=True
        ) or {}

        upload_id = (
            data.get("upload_id")
            or ""
        ).strip()

        key = (
            data.get("key")
            or ""
        ).strip()

        if not upload_id:

            return jsonify({
                "ok": False,
                "error":
                    "Upload ID is required."
            }), 400

        if not valid_r2_key(key):

            return jsonify({
                "ok": False,
                "error":
                    "Invalid R2 object key."
            }), 400

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
            "R2 multipart abort error:",
            repr(e)
        )

        return jsonify({
            "ok": False,
            "error": (
                "R2 multipart abort failed: "
                + str(e)
            )
        }), 500


# =========================================================
# DELETE R2 OBJECT
# =========================================================

@app.route(
    "/api/r2/object/delete",
    methods=["POST"]
)
@admin_required
def r2_object_delete():

    try:

        data = request.get_json(
            silent=True
        ) or {}

        key = (
            data.get("key")
            or ""
        ).strip()

        if not valid_r2_key(key):

            return jsonify({
                "ok": False,
                "error":
                    "Invalid R2 object key."
            }), 400

        r2_delete(key)

        return jsonify({
            "ok": True
        })

    except Exception as e:

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


# =========================================================
# SAVE MOVIE
# =========================================================

@app.route(
    "/api/movie/save",
    methods=["POST"]
)
@admin_required
def save_movie():

    video_key = ""

    poster_key = ""

    try:

        data = request.get_json(
            silent=True
        ) or {}

        title = (
            data.get("title")
            or ""
        ).strip()

        category = (
            data.get("category")
            or ""
        ).strip()

        description = (
            data.get("description")
            or ""
        ).strip()

        video_key = (
            data.get("video")
            or ""
        ).strip()

        poster_key = (
            data.get("poster")
            or ""
        ).strip()

        if not title:

            return jsonify({
                "ok": False,
                "error":
                    "Movie title is required."
            }), 400

        if not valid_r2_key(
            video_key
        ):

            return jsonify({
                "ok": False,
                "error":
                    "Invalid video R2 key."
            }), 400

        if not video_key.startswith(
            "videos/"
        ):

            return jsonify({
                "ok": False,
                "error":
                    "Invalid video object."
            }), 400

        if poster_key:

            if not valid_r2_key(
                poster_key
            ):

                return jsonify({
                    "ok": False,
                    "error":
                        "Invalid poster R2 key."
                }), 400

            if not poster_key.startswith(
                "posters/"
            ):

                return jsonify({
                    "ok": False,
                    "error":
                        "Invalid poster object."
                }), 400

        # -------------------------------------------------
        # VERIFY VIDEO
        # -------------------------------------------------

        video_head = r2_head(
            video_key
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
                "error":
                    "Video object is empty."
            }), 400

        if video_size > MAX_FILE_SIZE:

            return jsonify({
                "ok": False,
                "error":
                    "Video is larger than 4 GB."
            }), 400

        # -------------------------------------------------
        # VERIFY POSTER
        # -------------------------------------------------

        if poster_key:

            poster_head = r2_head(
                poster_key
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
                    "error":
                        "Poster object is empty."
                }), 400

            if poster_size > MAX_POSTER_SIZE:

                return jsonify({
                    "ok": False,
                    "error":
                        "Poster is larger than 25 MB."
                }), 400

        # -------------------------------------------------
        # SAVE DATABASE
        # -------------------------------------------------

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
                (
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
            )

            movie_id = (
                cur.fetchone()[0]
            )

            conn.commit()

            cur.close()

        finally:

            conn.close()

        return jsonify({
            "ok": True,
            "id": movie_id,
            "message":
                "Movie published successfully."
        })

    except Exception as e:

        print(
            "Movie save error:",
            repr(e)
        )

        return jsonify({
            "ok": False,
            "error": (
                "Movie save failed: "
                + str(e)
            )
        }), 500


# =========================================================
# DELETE MOVIE
# =========================================================

@app.route(
    "/admin/delete/<int:movie_id>",
    methods=["POST", "GET"]
)
@admin_required
def delete_movie(movie_id):

    video_key = ""

    poster_key = ""

    conn = get_db()

    try:

        cur = conn.cursor()

        cur.execute(
            """
            SELECT video, poster
            FROM movies
            WHERE id = %s
            """,
            (movie_id,)
        )

        movie = cur.fetchone()

        if not movie:

            flash(
                "Movie not found.",
                "error"
            )

            return redirect(
                url_for("admin")
            )

        video_key = movie[0]

        poster_key = movie[1]

        cur.execute(
            """
            DELETE FROM movies
            WHERE id = %s
            """,
            (movie_id,)
        )

        conn.commit()

        cur.close()

    finally:

        conn.close()

    if video_key:
        r2_delete(video_key)

    if poster_key:
        r2_delete(poster_key)

    flash(
        "Movie deleted successfully.",
        "success"
    )

    return redirect(
        url_for("admin")
    )


# =========================================================
# ADS SETTINGS
# =========================================================

@app.route(
    "/admin/ads",
    methods=["POST"]
)
@admin_required
def admin_ads():

    top_ad = request.form.get(
        "top_ad",
        ""
    )

    player_ad = request.form.get(
        "player_ad",
        ""
    )

    bottom_ad = request.form.get(
        "bottom_ad",
        ""
    )

    conn = get_db()

    try:

        cur = conn.cursor()

        values = {
            "top_ad": top_ad,
            "player_ad": player_ad,
            "bottom_ad": bottom_ad
        }

        for key, value in values.items():

            cur.execute(
                """
                INSERT INTO settings
                (
                    key,
                    value
                )
                VALUES
                (
                    %s,
                    %s
                )
                ON CONFLICT (key)
                DO UPDATE SET
                    value = EXCLUDED.value
                """,
                (
                    key,
                    value
                )
            )

        conn.commit()

        cur.close()

    finally:

        conn.close()

    flash(
        "Ads settings saved.",
        "success"
    )

    return redirect(
        url_for("admin")
    )


# =========================================================
# ERROR HANDLERS
# =========================================================

@app.errorhandler(413)
def too_large(error):

    if request.path.startswith(
        "/api/"
    ):

        return jsonify({
            "ok": False,
            "error":
                "File is too large."
        }), 413

    return (
        "File is too large.",
        413
    )


@app.errorhandler(500)
def server_error(error):

    if request.path.startswith(
        "/api/"
    ):

        return jsonify({
            "ok": False,
            "error":
                "Internal server error."
        }), 500

    return (
        "Internal Server Error",
        500
    )


# =========================================================
# STARTUP
# =========================================================

try:

    init_db()

except Exception as startup_error:

    print(
        "Database initialization warning:",
        startup_error
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
        debug=False
    )
