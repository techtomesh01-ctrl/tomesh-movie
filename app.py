import os
import re
import math
import mimetypes
import secrets
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


# =========================================================
# TOMESH MOVIES - COMPLETE APP.PY
# PostgreSQL + Cloudflare R2 + Direct Multipart Upload
# =========================================================

app = Flask(__name__)

app.secret_key = os.environ.get("SECRET_KEY", "tomesh-movies-secret-key")

# ---------------------------------------------------------
# BASIC SETTINGS
# ---------------------------------------------------------

app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024  # 4 GB

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me-now")

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "").strip()
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
R2_BUCKET = os.environ.get("R2_BUCKET", "tomesh-movies").strip()
R2_ENDPOINT = os.environ.get("R2_ENDPOINT", "").strip().rstrip("/")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "").strip().rstrip("/")


# ---------------------------------------------------------
# ALLOWED FILE TYPES
# ---------------------------------------------------------

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
# R2 MULTIPART SETTINGS
# ---------------------------------------------------------

PART_SIZE = 10 * 1024 * 1024       # 10 MB
MAX_PARTS = 10000
PRESIGNED_EXPIRES = 3600


# =========================================================
# HELPERS
# =========================================================

def allowed_file(filename, allowed_extensions):
    if not filename:
        return False

    filename = filename.lower().strip()

    if "." not in filename:
        return False

    extension = filename.rsplit(".", 1)[1]

    return extension in allowed_extensions


def get_extension(filename):
    if not filename or "." not in filename:
        return ""

    return filename.rsplit(".", 1)[1].lower()


def clean_text(value):
    if value is None:
        return ""

    return str(value).strip()


def login_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("login"))

        return function(*args, **kwargs)

    return wrapper


# =========================================================
# DATABASE
# =========================================================

def get_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")

    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor
    )


def init_db():
    """
    Creates required tables if they do not exist.
    """

    db = get_db()

    try:
        cur = db.cursor()

        cur.execute(
            """
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
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ads (
                id SERIAL PRIMARY KEY,
                ad_top TEXT DEFAULT '',
                ad_player TEXT DEFAULT '',
                ad_bottom TEXT DEFAULT ''
            )
            """
        )

        cur.execute(
            """
            SELECT id
            FROM ads
            ORDER BY id
            LIMIT 1
            """
        )

        ad = cur.fetchone()

        if not ad:
            cur.execute(
                """
                INSERT INTO ads (ad_top, ad_player, ad_bottom)
                VALUES ('', '', '')
                """
            )

        db.commit()

    finally:
        db.close()


def get_ads():
    try:
        db = get_db()

        try:
            cur = db.cursor()

            cur.execute(
                """
                SELECT *
                FROM ads
                ORDER BY id
                LIMIT 1
                """
            )

            row = cur.fetchone()

            if row:
                return row

            return {
                "ad_top": "",
                "ad_player": "",
                "ad_bottom": "",
            }

        finally:
            db.close()

    except Exception:
        return {
            "ad_top": "",
            "ad_player": "",
            "ad_bottom": "",
        }


# =========================================================
# R2
# =========================================================

def get_r2_client():
    if not R2_ENDPOINT:
        raise RuntimeError("R2_ENDPOINT is not configured")

    if not R2_ACCESS_KEY_ID:
        raise RuntimeError("R2_ACCESS_KEY_ID is not configured")

    if not R2_SECRET_ACCESS_KEY:
        raise RuntimeError("R2_SECRET_ACCESS_KEY is not configured")

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
        ),
    )


def r2_object_url(key):
    if not key:
        return ""

    key = str(key).lstrip("/")

    if R2_PUBLIC_URL:
        return f"{R2_PUBLIC_URL}/{quote(key, safe='/')}"

    return ""


def r2_presigned_url(key, expires=PRESIGNED_EXPIRES):
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


def delete_r2_object(key):
    if not key:
        return

    try:
        client = get_r2_client()

        client.delete_object(
            Bucket=R2_BUCKET,
            Key=key,
        )

    except Exception as exc:
        print("R2 delete error:", exc)


def r2_key_is_allowed(key):
    if not key:
        return False

    key = str(key).lstrip("/")

    if len(key) > 1024:
        return False

    return (
        key.startswith("videos/")
        or
        key.startswith("posters/")
    )


# =========================================================
# MIME TYPES
# =========================================================

VIDEO_MIME_TYPES = {
    "mp4": "video/mp4",
    "m4v": "video/mp4",
    "webm": "video/webm",
    "mov": "video/quicktime",
    "mkv": "video/x-matroska",
}


def get_video_mime(filename_or_key):
    extension = get_extension(filename_or_key)

    if extension in VIDEO_MIME_TYPES:
        return VIDEO_MIME_TYPES[extension]

    guessed, _ = mimetypes.guess_type(filename_or_key or "")

    return guessed or "video/mp4"


# =========================================================
# HOME
# =========================================================

@app.route("/")
def index():

    db = get_db()

    try:
        cur = db.cursor()

        cur.execute(
            """
            SELECT *
            FROM movies
            ORDER BY created_at DESC, id DESC
            """
        )

        movies = cur.fetchall()

        for movie in movies:

            if movie.get("poster"):
                movie["poster_url"] = r2_object_url(movie["poster"])

            else:
                movie["poster_url"] = ""

        ads = get_ads()

        return render_template(
            "index.html",
            movies=movies,
            ads=ads,
        )

    finally:
        db.close()


# =========================================================
# MOVIE PAGE
# =========================================================

@app.route("/movie/<int:movie_id>")
def movie(movie_id):

    db = get_db()

    try:
        cur = db.cursor()

        cur.execute(
            """
            SELECT *
            FROM movies
            WHERE id = %s
            """,
            (movie_id,)
        )

        movie_data = cur.fetchone()

        if not movie_data:
            abort(404)

        cur.execute(
            """
            UPDATE movies
            SET views = COALESCE(views, 0) + 1
            WHERE id = %s
            """,
            (movie_id,)
        )

        db.commit()

    finally:
        db.close()

    movie_data["video_url"] = ""

    if movie_data.get("video"):

        try:
            movie_data["video_url"] = r2_presigned_url(
                movie_data["video"],
                expires=3600
            )

        except Exception as exc:
            print("Presigned video URL error:", exc)

            movie_data["video_url"] = r2_object_url(
                movie_data["video"]
            )

    movie_data["video_mime"] = get_video_mime(
        movie_data.get("video", "")
    )

    if movie_data.get("poster"):
        movie_data["poster_url"] = r2_object_url(
            movie_data["poster"]
        )
    else:
        movie_data["poster_url"] = ""

    return render_template(
        "movie.html",
        movie=movie_data,
        ads=get_ads(),
    )


# =========================================================
# HEALTH
# =========================================================

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "tomesh-movie",
    })


@app.route("/r2-health")
def r2_health():

    try:

        client = get_r2_client()

        client.head_bucket(
            Bucket=R2_BUCKET
        )

        return jsonify({
            "status": "ok",
            "message": "R2 OK",
            "bucket": R2_BUCKET,
        })

    except Exception as exc:

        return jsonify({
            "status": "error",
            "message": str(exc),
            "bucket": R2_BUCKET,
        }), 500


# =========================================================
# LOGIN
# =========================================================

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        username = clean_text(
            request.form.get("username")
        )

        password = request.form.get(
            "password",
            ""
        )

        if (
            username == ADMIN_USER
            and
            password == ADMIN_PASSWORD
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
@login_required
def admin():

    db = get_db()

    try:

        cur = db.cursor()

        cur.execute(
            """
            SELECT *
            FROM movies
            ORDER BY created_at DESC, id DESC
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

        return render_template(
            "admin.html",
            movies=movies,
            stats=stats,
            ads=get_ads(),
        )

    finally:
        db.close()


# =========================================================
# ADD MOVIE - NORMAL FORM COMPATIBILITY
# =========================================================

@app.route("/admin/add", methods=["GET", "POST"])
@login_required
def admin_add():

    if request.method == "GET":

        return render_template(
            "admin.html",
            movies=[],
            stats={
                "total_movies": 0,
                "total_views": 0,
            },
            ads=get_ads(),
        )

    title = clean_text(
        request.form.get("title")
    )

    category = clean_text(
        request.form.get("category")
    )

    description = clean_text(
        request.form.get("description")
    )

    poster_file = request.files.get("poster")
    video_file = request.files.get("video")

    if not title:
        flash(
            "Movie title required.",
            "error"
        )

        return redirect(
            url_for("admin")
        )

    if not poster_file or not poster_file.filename:
        flash(
            "Poster required.",
            "error"
        )

        return redirect(
            url_for("admin")
        )

    if not video_file or not video_file.filename:
        flash(
            "Video required.",
            "error"
        )

        return redirect(
            url_for("admin")
        )

    if not allowed_file(
        poster_file.filename,
        ALLOWED_POSTERS
    ):
        flash(
            "Poster must be JPG, JPEG, PNG or WEBP.",
            "error"
        )

        return redirect(
            url_for("admin")
        )

    if not allowed_file(
        video_file.filename,
        ALLOWED_VIDEOS
    ):
        flash(
            "Video must be MP4, MKV, WebM or MOV.",
            "error"
        )

        return redirect(
            url_for("admin")
        )

    poster_ext = get_extension(
        poster_file.filename
    )

    video_ext = get_extension(
        video_file.filename
    )

    poster_key = (
        "posters/"
        +
        secrets.token_hex(16)
        +
        "."
        +
        poster_ext
    )

    video_key = (
        "videos/"
        +
        secrets.token_hex(16)
        +
        "."
        +
        video_ext
    )

    client = get_r2_client()

    try:

        poster_file.stream.seek(0)

        client.upload_fileobj(
            poster_file.stream,
            R2_BUCKET,
            poster_key,
            ExtraArgs={
                "ContentType": (
                    mimetypes.guess_type(
                        poster_file.filename
                    )[0]
                    or
                    "application/octet-stream"
                )
            }
        )

        video_file.stream.seek(0)

        client.upload_fileobj(
            video_file.stream,
            R2_BUCKET,
            video_key,
            ExtraArgs={
                "ContentType": get_video_mime(
                    video_file.filename
                )
            }
        )

        db = get_db()

        try:

            cur = db.cursor()

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
                """,
                (
                    title,
                    category,
                    description,
                    poster_key,
                    video_key,
                )
            )

            db.commit()

        finally:
            db.close()

        flash(
            "Movie added successfully.",
            "success"
        )

    except Exception as exc:

        print("Movie upload error:", exc)

        delete_r2_object(poster_key)
        delete_r2_object(video_key)

        flash(
            "Movie upload failed: " + str(exc),
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
    methods=["GET", "POST"]
)
@login_required
def admin_delete(movie_id):

    db = get_db()

    movie_data = None

    try:

        cur = db.cursor()

        cur.execute(
            """
            SELECT *
            FROM movies
            WHERE id = %s
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

        db.commit()

    finally:
        db.close()

    delete_r2_object(
        movie_data.get("video")
    )

    delete_r2_object(
        movie_data.get("poster")
    )

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
@login_required
def admin_ads():

    ad_top = request.form.get(
        "ad_top",
        ""
    )

    ad_player = request.form.get(
        "ad_player",
        ""
    )

    ad_bottom = request.form.get(
        "ad_bottom",
        ""
    )

    db = get_db()

    try:

        cur = db.cursor()

        cur.execute(
            """
            SELECT id
            FROM ads
            ORDER BY id
            LIMIT 1
            """
        )

        row = cur.fetchone()

        if row:

            cur.execute(
                """
                UPDATE ads
                SET
                    ad_top = %s,
                    ad_player = %s,
                    ad_bottom = %s
                WHERE id = %s
                """,
                (
                    ad_top,
                    ad_player,
                    ad_bottom,
                    row["id"],
                )
            )

        else:

            cur.execute(
                """
                INSERT INTO ads
                (
                    ad_top,
                    ad_player,
                    ad_bottom
                )
                VALUES
                (
                    %s,
                    %s,
                    %s
                )
                """,
                (
                    ad_top,
                    ad_player,
                    ad_bottom,
                )
            )

        db.commit()

    finally:
        db.close()

    flash(
        "Ads settings saved.",
        "success"
    )

    return redirect(
        url_for("admin")
    )


# =========================================================
# R2 MULTIPART CREATE
# =========================================================

@app.route(
    "/api/r2/multipart/create",
    methods=["POST"]
)
@login_required
def r2_multipart_create():

    try:

        data = request.get_json(
            silent=True
        ) or {}

        filename = clean_text(
            data.get("filename")
        )

        file_type = clean_text(
            data.get("file_type")
        ).lower()

        if not filename:
            return jsonify({
                "ok": False,
                "error": "Filename missing."
            }), 400

        if file_type == "poster":

            if not allowed_file(
                filename,
                ALLOWED_POSTERS
            ):
                return jsonify({
                    "ok": False,
                    "error": (
                        "Poster must be "
                        "JPG, JPEG, PNG or WEBP."
                    )
                }), 400

            prefix = "posters/"

        else:

            if not allowed_file(
                filename,
                ALLOWED_VIDEOS
            ):
                return jsonify({
                    "ok": False,
                    "error": (
                        "Video must be "
                        "MP4, MKV, WebM or MOV."
                    )
                }), 400

            prefix = "videos/"

        extension = get_extension(
            filename
        )

        safe_name = secure_filename(
            filename
        )

        base_name = os.path.splitext(
            safe_name
        )[0]

        base_name = re.sub(
            r"[^a-zA-Z0-9_-]+",
            "-",
            base_name
        ).strip("-")

        if not base_name:
            base_name = "file"

        key = (
            prefix
            +
            base_name
            +
            "-"
            +
            secrets.token_hex(12)
            +
            "."
            +
            extension
        )

        client = get_r2_client()

        content_type = (
            clean_text(
                data.get("content_type")
            )
            or
            get_video_mime(filename)
        )

        response = client.create_multipart_upload(
            Bucket=R2_BUCKET,
            Key=key,
            ContentType=content_type,
        )

        upload_id = response["UploadId"]

        return jsonify({
            "ok": True,
            "key": key,
            "upload_id": upload_id,
            "part_size": PART_SIZE,
            "max_parts": MAX_PARTS,
            "content_type": content_type,
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
@login_required
def r2_multipart_urls():

    try:

        data = request.get_json(
            silent=True
        ) or {}

        key = clean_text(
            data.get("key")
        ).lstrip("/")

        upload_id = clean_text(
            data.get("upload_id")
        )

        part_numbers = data.get(
            "part_numbers"
        )

        if not r2_key_is_allowed(key):
            return jsonify({
                "ok": False,
                "error": "Invalid R2 key."
            }), 400

        if not upload_id:
            return jsonify({
                "ok": False,
                "error": "Upload ID missing."
            }), 400

        if not isinstance(
            part_numbers,
            list
        ):
            return jsonify({
                "ok": False,
                "error": "part_numbers must be an array."
            }), 400

        if len(part_numbers) > MAX_PARTS:
            return jsonify({
                "ok": False,
                "error": "Too many parts."
            }), 400

        client = get_r2_client()

        urls = []

        for number in part_numbers:

            try:
                number = int(number)
            except Exception:
                return jsonify({
                    "ok": False,
                    "error": "Invalid part number."
                }), 400

            if number < 1 or number > MAX_PARTS:
                return jsonify({
                    "ok": False,
                    "error": "Invalid part number."
                }), 400

            url = client.generate_presigned_url(
                "upload_part",
                Params={
                    "Bucket": R2_BUCKET,
                    "Key": key,
                    "UploadId": upload_id,
                    "PartNumber": number,
                },
                ExpiresIn=PRESIGNED_EXPIRES,
            )

            urls.append({
                "part_number": number,
                "url": url,
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
@login_required
def r2_multipart_complete():

    try:

        data = request.get_json(
            silent=True
        ) or {}

        key = clean_text(
            data.get("key")
        ).lstrip("/")

        upload_id = clean_text(
            data.get("upload_id")
        )

        parts = data.get(
            "parts"
        )

        if not r2_key_is_allowed(key):
            return jsonify({
                "ok": False,
                "error": "Invalid R2 key."
            }), 400

        if not upload_id:
            return jsonify({
                "ok": False,
                "error": "Upload ID missing."
            }), 400

        if not isinstance(
            parts,
            list
        ) or not parts:
            return jsonify({
                "ok": False,
                "error": "Parts missing."
            }), 400

        clean_parts = []

        for part in parts:

            if not isinstance(
                part,
                dict
            ):
                continue

            part_number = part.get(
                "PartNumber"
            )

            etag = part.get(
                "ETag"
            )

            if part_number is None:
                part_number = part.get(
                    "part_number"
                )

            if not etag:
                etag = part.get(
                    "etag"
                )

            if part_number is None or not etag:
                continue

            clean_parts.append({
                "PartNumber": int(
                    part_number
                ),
                "ETag": str(
                    etag
                ),
            })

        if not clean_parts:
            return jsonify({
                "ok": False,
                "error": "No valid parts."
            }), 400

        clean_parts.sort(
            key=lambda item: item["PartNumber"]
        )

        client = get_r2_client()

        response = client.complete_multipart_upload(
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
            "location": response.get(
                "Location",
                ""
            ),
            "etag": response.get(
                "ETag",
                ""
            ),
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
@login_required
def r2_multipart_abort():

    try:

        data = request.get_json(
            silent=True
        ) or {}

        key = clean_text(
            data.get("key")
        ).lstrip("/")

        upload_id = clean_text(
            data.get("upload_id")
        )

        if not r2_key_is_allowed(key):
            return jsonify({
                "ok": False,
                "error": "Invalid R2 key."
            }), 400

        if not upload_id:
            return jsonify({
                "ok": False,
                "error": "Upload ID missing."
            }), 400

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
            "R2 multipart abort failed:",
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
@login_required
def api_movie_save():

    try:

        data = request.get_json(
            silent=True
        ) or {}

        title = clean_text(
            data.get("title")
        )

        category = clean_text(
            data.get("category")
        )

        description = clean_text(
            data.get("description")
        )

        poster = clean_text(
            data.get("poster")
        ).lstrip("/")

        video = clean_text(
            data.get("video")
        ).lstrip("/")

        if not title:
            return jsonify({
                "ok": False,
                "error": "Movie title required."
            }), 400

        if not video:
            return jsonify({
                "ok": False,
                "error": "Video key missing."
            }), 400

        if not video.startswith(
            "videos/"
        ):
            return jsonify({
                "ok": False,
                "error": "Invalid video key."
            }), 400

        if poster and not poster.startswith(
            "posters/"
        ):
            return jsonify({
                "ok": False,
                "error": "Invalid poster key."
            }), 400

        db = get_db()

        try:

            cur = db.cursor()

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
                    poster,
                    video,
                )
            )

            movie_row = cur.fetchone()

            db.commit()

        finally:
            db.close()

        return jsonify({
            "ok": True,
            "message": "Movie saved successfully.",
            "movie_id": movie_row["id"],
        })

    except Exception as exc:

        print(
            "Movie save failed:",
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

    return Response(
        "google.com, pub-8697157365303435, DIRECT, f08c47fec0942fa0\n",
        mimetype="text/plain"
    )


# =========================================================
# ERROR HANDLERS
# =========================================================

@app.errorhandler(413)
def file_too_large(error):

    return jsonify({
        "ok": False,
        "error": "File is larger than 4 GB."
    }), 413


@app.errorhandler(404)
def not_found(error):
    if request.path.startswith("/api/"):
        return jsonify({
            "ok": False,
            "error": "Not found."
        }), 404

    return """
    <!doctype html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>404 | Tomesh Movies</title>
        <style>
            body{margin:0;background:#09090d;color:#fff;font-family:Arial,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center}
            .box{max-width:600px;padding:40px 24px}
            h1{font-size:72px;margin:0 0 10px}
            p{color:#aaa;line-height:1.6}
            a{display:inline-block;margin-top:18px;padding:12px 22px;border-radius:10px;background:#8e24aa;color:#fff;text-decoration:none;font-weight:700}
        </style>
    </head>
    <body><div class="box"><h1>404</h1><h2>Page Not Found</h2><p>The page you requested could not be found.</p><a href="/">Back to Home</a></div></body>
    </html>
    """, 404


@app.errorhandler(500)
def internal_server_error(error):
    # Never render 500.html here: a missing template would create another 500.
    if request.path.startswith("/api/"):
        return jsonify({
            "ok": False,
            "error": "Internal server error."
        }), 500

    return """
    <!doctype html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>500 | Tomesh Movies</title>
        <style>
            body{margin:0;background:#09090d;color:#fff;font-family:Arial,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center}
            .box{max-width:600px;padding:40px 24px}
            h1{font-size:72px;margin:0 0 10px;color:#ff3b3b}
            p{color:#aaa;line-height:1.6}
            a{display:inline-block;margin-top:18px;padding:12px 22px;border-radius:10px;background:linear-gradient(90deg,#ff1744,#8e24aa);color:#fff;text-decoration:none;font-weight:700}
        </style>
    </head>
    <body><div class="box"><h1>500</h1><h2>Something Went Wrong</h2><p>Tomesh Movies encountered a temporary server error. Please try again.</p><a href="/">Back to Home</a></div></body>
    </html>
    """, 500


# =========================================================
# STARTUP
# =========================================================

try:
    init_db()
    print("PostgreSQL initialized successfully.")

except Exception as exc:
    print(
        "Database initialization warning:",
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
