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

app.secret_key = (
    os.environ.get("SECRET_KEY", "").strip()
    or secrets.token_hex(32)
)

app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024


# ============================================================
# ADMIN
# ============================================================

ADMIN_USER = (
    os.environ.get("ADMIN_USER", "admin").strip()
    or "admin"
)

ADMIN_PASSWORD = (
    os.environ.get("ADMIN_PASSWORD", "change-me-now").strip()
    or "change-me-now"
)


# ============================================================
# DATABASE
# ============================================================

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

MAX_VIDEO_SIZE = (
    4 * 1024 * 1024 * 1024
)

MAX_POSTER_SIZE = (
    25 * 1024 * 1024
)


# ============================================================
# R2 SETTINGS
# ============================================================

R2_ACCOUNT_ID = (
    os.environ.get("R2_ACCOUNT_ID", "").strip()
)

R2_ACCESS_KEY_ID = (
    os.environ.get("R2_ACCESS_KEY_ID", "").strip()
)

R2_SECRET_ACCESS_KEY = (
    os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
)

R2_BUCKET = (
    os.environ.get("R2_BUCKET", "tomesh-movies").strip()
)

R2_ENDPOINT = (
    os.environ.get("R2_ENDPOINT", "").strip()
)

R2_PUBLIC_URL = (
    os.environ.get("R2_PUBLIC_URL", "").strip()
)


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
    value = clean_env_value(value)

    value = value.rstrip("/")

    bucket_suffix = "/" + R2_BUCKET

    if value.endswith(bucket_suffix):
        value = value[
            :-len(bucket_suffix)
        ]

    return value.rstrip("/")


R2_ACCOUNT_ID = clean_env_value(
    R2_ACCOUNT_ID
)

R2_ACCESS_KEY_ID = clean_env_value(
    R2_ACCESS_KEY_ID
)

R2_SECRET_ACCESS_KEY = clean_env_value(
    R2_SECRET_ACCESS_KEY
)

R2_BUCKET = clean_env_value(
    R2_BUCKET
)

R2_ENDPOINT = clean_endpoint(
    R2_ENDPOINT
)

R2_PUBLIC_URL = clean_env_value(
    R2_PUBLIC_URL
).rstrip("/")


# ============================================================
# JSON HELPERS
# ============================================================

def json_ok(**kwargs):
    data = {
        "ok": True
    }

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
        ".",
        1
    )[1].lower().strip()


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


def safe_filename(filename):
    filename = secure_filename(
        str(filename or "")
    )

    if not filename:
        filename = "file"

    return filename


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
        or mimetypes.guess_type(
            str(key or "")
        )[0]
        or "application/octet-stream"
    )


# ============================================================
# R2 KEY VALIDATION
# ============================================================

def validate_r2_key(key):
    key = str(key or "").strip()

    if not key:
        raise ValueError(
            "R2 object key missing."
        )

    if "\r" in key or "\n" in key:
        raise ValueError(
            "Invalid R2 object key."
        )

    if key.startswith(VIDEO_PREFIX):
        return key

    if key.startswith(POSTER_PREFIX):
        return key

    raise ValueError(
        "Invalid R2 object prefix."
    )


# ============================================================
# DATABASE
# ============================================================

def get_db(dict_rows=False):
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not configured."
        )

    conn = psycopg2.connect(
        DATABASE_URL,
        cursor_factory=(
            RealDictCursor
            if dict_rows
            else None
        )
    )

    return conn


def init_db():
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
            (key,)
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
            (
                key,
                value,
            )
        )

        conn.commit()

        cur.close()

    finally:
        conn.close()


def get_ads():
    return {
        "top": get_setting(
            "ad_top",
            ""
        ),
        "player": get_setting(
            "ad_player",
            ""
        ),
        "bottom": get_setting(
            "ad_bottom",
            ""
        ),
    }


# ============================================================
# R2 CLIENT
# ============================================================

def get_r2_client():

    if not R2_ACCOUNT_ID:
        raise RuntimeError(
            "R2_ACCOUNT_ID missing."
        )

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

    if not R2_ENDPOINT:
        raise RuntimeError(
            "R2_ENDPOINT missing."
        )

    return boto3.client(
        "s3",

        endpoint_url=R2_ENDPOINT,

        aws_access_key_id=
            R2_ACCESS_KEY_ID,

        aws_secret_access_key=
            R2_SECRET_ACCESS_KEY,

        region_name="auto",

        config=Config(
            signature_version="s3v4",
            s3={
                "addressing_style": "path"
            },
            retries={
                "max_attempts": 5,
                "mode": "standard"
            },
        ),
    )


# ============================================================
# R2 URL HELPERS
# ============================================================

def r2_public_url(key):

    if not key:
        return None

    key = str(key).lstrip("/")

    if not R2_PUBLIC_URL:
        return None

    return (
        R2_PUBLIC_URL.rstrip("/")
        + "/"
        + quote(
            key,
            safe="/"
        )
    )


def r2_presigned_url(
    key,
    expires=PRESIGNED_EXPIRES
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
        return r2_presigned_url(
            key
        )
    except Exception:
        public = r2_public_url(key)

        if public:
            return public

        raise


# ============================================================
# R2 HEAD
# ============================================================

def r2_head(key):

    key = validate_r2_key(key)

    client = get_r2_client()

    return client.head_object(
        Bucket=R2_BUCKET,
        Key=key
    )


# ============================================================
# R2 DELETE
# ============================================================

def r2_delete(key):

    if not key:
        return False

    key = validate_r2_key(key)

    client = get_r2_client()

    client.delete_object(
        Bucket=R2_BUCKET,
        Key=key
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
# HOME
# ============================================================

@app.route("/")
def home():

    conn = get_db(
        dict_rows=True
    )

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

    for movie in movies:

        poster_key = movie.get(
            "poster"
        )

        try:
            movie["poster_url"] = (
                media_url(
                    poster_key
                )
                if poster_key
                else None
            )
        except Exception:
            movie["poster_url"] = None

    return render_template(
        "index.html",
        movies=movies,
        ads=get_ads()
    )


# ============================================================
# IMPORTANT:
# SUPPORT BOTH home AND index
# ============================================================

try:
    app.add_url_rule(
        "/",
        endpoint="index",
        view_func=home
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
            (movie_id,)
        )

        movie = cur.fetchone()

        if not movie:
            cur.close()
            abort(404)

        cur.execute(
            """
            UPDATE movies
            SET views = COALESCE(views, 0) + 1
            WHERE id = %s
            """,
            (movie_id,)
        )

        conn.commit()

        cur.close()

    finally:
        conn.close()

    video_key = movie.get(
        "video"
    )

    poster_key = movie.get(
        "poster"
    )


    # ========================================================
    # IMPORTANT PLAYBACK FIX
    #
    # Do NOT give browser direct R2 URL.
    # Browser will use our /stream/<id> endpoint.
    # ========================================================

    if video_key:

        movie["video_url"] = url_for(
            "stream_movie",
            movie_id=movie_id
        )

        movie["video_mime"] = (
            content_type_for_key(
                video_key
            )
        )

    else:

        movie["video_url"] = None

        movie["video_mime"] = (
            "video/mp4"
        )


    # ========================================================
    # POSTER
    # ========================================================

    if poster_key:

        try:
            movie["poster_url"] = (
                media_url(
                    poster_key
                )
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
        ads=get_ads()
    )


# ============================================================
# VIDEO STREAM
# ============================================================
#
# THIS IS THE MAIN PLAYBACK FIX.
#
# Browser sends:
#
# Range: bytes=0-
#
# We request same range from Cloudflare R2
# and return HTTP 206.
#
# This supports:
# - Chrome video
# - buffering
# - seeking
# - large files
#
# ============================================================

@app.route(
    "/stream/<int:movie_id>",
    methods=["GET"]
)
def stream_movie(movie_id):

    # --------------------------------------------------------
    # GET MOVIE
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
            (movie_id,)
        )

        movie = cur.fetchone()

        cur.close()

    finally:
        conn.close()


    if not movie:
        return Response(
            "Movie not found.",
            status=404
        )


    video_key = movie.get(
        "video"
    )


    if not video_key:
        return Response(
            "Video not found.",
            status=404
        )


    try:
        video_key = validate_r2_key(
            video_key
        )
    except Exception:
        return Response(
            "Invalid video object.",
            status=400
        )


    # --------------------------------------------------------
    # R2 CLIENT
    # --------------------------------------------------------

    try:
        client = get_r2_client()
    except Exception as exc:

        print(
            "R2 client error:",
            repr(exc)
        )

        return Response(
            "R2 configuration error.",
            status=500
        )


    # --------------------------------------------------------
    # HEAD OBJECT
    # --------------------------------------------------------

    try:

        head = client.head_object(
            Bucket=R2_BUCKET,
            Key=video_key
        )

    except Exception as exc:

        print(
            "R2 HEAD ERROR:",
            repr(exc)
        )

        return Response(
            "Video object not found in R2.",
            status=404
        )


    # --------------------------------------------------------
    # SIZE
    # --------------------------------------------------------

    try:
        total_size = int(
            head.get("ContentLength", 0)
        )
    except Exception:
        total_size = 0


    if total_size <= 0:
        return Response(
            "Video file is empty.",
            status=404
        )


    # --------------------------------------------------------
    # CONTENT TYPE
    # --------------------------------------------------------

    content_type = (
        head.get("ContentType")
        or content_type_for_key(
            video_key
        )
    )


    # --------------------------------------------------------
    # RANGE
    # --------------------------------------------------------

    range_header = request.headers.get(
        "Range"
    )


    start = 0
    end = total_size - 1


    if range_header:

        try:

            if not range_header.startswith(
                "bytes="
            ):
                raise ValueError(
                    "Invalid range"
                )

            range_value = (
                range_header
                .replace(
                    "bytes=",
                    "",
                    1
                )
                .split(",")[0]
                .strip()
            )


            if "-" not in range_value:
                raise ValueError(
                    "Invalid range"
                )


            start_text, end_text = (
                range_value.split(
                    "-",
                    1
                )
            )


            # ------------------------------------------------
            # bytes=-500000
            # ------------------------------------------------

            if not start_text:

                suffix_length = int(
                    end_text
                )

                if suffix_length <= 0:
                    raise ValueError(
                        "Invalid suffix"
                    )

                if suffix_length > total_size:
                    suffix_length = total_size

                start = (
                    total_size
                    - suffix_length
                )

                end = (
                    total_size - 1
                )


            else:

                start = int(
                    start_text
                )

                if end_text:
                    end = int(
                        end_text
                    )
                else:
                    end = (
                        total_size - 1
                    )


            # ------------------------------------------------
            # VALIDATE RANGE
            # ------------------------------------------------

            if start < 0:
                raise ValueError(
                    "Invalid start"
                )

            if start >= total_size:
                raise ValueError(
                    "Range outside file"
                )

            if end >= total_size:
                end = (
                    total_size - 1
                )

            if end < start:
                raise ValueError(
                    "Invalid end"
                )

        except Exception:

            return Response(
                "Range Not Satisfiable",
                status=416,
                headers={
                    "Content-Range":
                        "bytes */"
                        + str(total_size)
                }
            )


    content_length = (
        end - start + 1
    )


    # --------------------------------------------------------
    # R2 GET
    # --------------------------------------------------------

    try:

        if range_header:

            obj = client.get_object(
                Bucket=R2_BUCKET,
                Key=video_key,
                Range=(
                    "bytes="
                    + str(start)
                    + "-"
                    + str(end)
                )
            )

        else:

            obj = client.get_object(
                Bucket=R2_BUCKET,
                Key=video_key
            )

    except Exception as exc:

        print(
            "R2 GET VIDEO ERROR:",
            repr(exc)
        )

        return Response(
            "Unable to load video from R2.",
            status=502
        )


    body = obj["Body"]


    # --------------------------------------------------------
    # STREAM GENERATOR
    # --------------------------------------------------------

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


    # --------------------------------------------------------
    # RESPONSE HEADERS
    # --------------------------------------------------------

    headers = {

        "Content-Type":
            content_type,

        "Content-Length":
            str(content_length),

        "Accept-Ranges":
            "bytes",

        "Cache-Control":
            "public, max-age=3600",

        "Content-Disposition":
            "inline",

        "X-Content-Type-Options":
            "nosniff",
    }


    # --------------------------------------------------------
    # PARTIAL RESPONSE
    # --------------------------------------------------------

    if range_header:

        headers[
            "Content-Range"
        ] = (
            "bytes "
            + str(start)
            + "-"
            + str(end)
            + "/"
            + str(total_size)
        )


        return Response(
            generate(),
            status=206,
            headers=headers,
            direct_passthrough=True
        )


    # --------------------------------------------------------
    # FULL RESPONSE
    # --------------------------------------------------------

    return Response(
        generate(),
        status=200,
        headers=headers,
        direct_passthrough=True
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

        username = (
            request.form.get(
                "username",
                ""
            ).strip()
        )

        password = (
            request.form.get(
                "password",
                ""
            )
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
# ADMIN DASHBOARD
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
                COALESCE(
                    SUM(views),
                    0
                ) AS total_views
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

        poster_key = movie.get(
            "poster"
        )

        try:

            movie["poster_url"] = (
                media_url(
                    poster_key
                )
                if poster_key
                else None
            )

        except Exception:

            movie["poster_url"] = None


    return render_template(
        "admin.html",
        movies=movies,
        total_movies=(
            stats["total_movies"]
            if stats
            else 0
        ),
        total_views=(
            stats["total_views"]
            if stats
            else 0
        ),
        ads=get_ads()
    )


# ============================================================
# LEGACY ADMIN ADD
# ============================================================

@app.route(
    "/admin/add",
    methods=["GET", "POST"]
)
@admin_required
def admin_add():

    if request.method == "GET":

        return redirect(
            url_for("admin")
        )


    title = (
        request.form.get(
            "title",
            ""
        ).strip()
    )

    category = (
        request.form.get(
            "category",
            ""
        ).strip()
    )

    description = (
        request.form.get(
            "description",
            ""
        ).strip()
    )

    video = request.files.get(
        "video"
    )

    poster = request.files.get(
        "poster"
    )


    if not title:
        flash(
            "Movie title required.",
            "error"
        )

        return redirect(
            url_for("admin")
        )


    if not video or not video.filename:
        flash(
            "Video required.",
            "error"
        )

        return redirect(
            url_for("admin")
        )


    if not allowed_video(
        video.filename
    ):
        flash(
            "Invalid video format.",
            "error"
        )

        return redirect(
            url_for("admin")
        )


    if (
        video.content_length
        and
        video.content_length
        > MAX_VIDEO_SIZE
    ):
        flash(
            "Video maximum 4 GB.",
            "error"
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
                "error"
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

        video_content_type = (
            content_type_for_key(
                video.filename
            )
        )


        client.upload_fileobj(
            video,
            R2_BUCKET,
            video_key,
            ExtraArgs={
                "ContentType":
                    video_content_type
            }
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


            poster_content_type = (
                content_type_for_key(
                    poster.filename
                )
            )


            client.upload_fileobj(
                poster,
                R2_BUCKET,
                poster_key,
                ExtraArgs={
                    "ContentType":
                        poster_content_type
                }
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
                (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )
                """,
                (
                    title,
                    category,
                    description,
                    poster_key,
                    video_key
                )
            )

            conn.commit()

            cur.close()

        finally:
            conn.close()


        flash(
            "Movie uploaded successfully.",
            "success"
        )


    except Exception as exc:

        print(
            "ADMIN ADD ERROR:",
            repr(exc)
        )


        try:
            r2_delete(
                video_key
            )
        except Exception:
            pass


        if poster_key:

            try:
                r2_delete(
                    poster_key
                )
            except Exception:
                pass


        flash(
            "Upload failed: "
            + str(exc),
            "error"
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

    movie = None

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

        if movie:

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


    if movie:

        video_key = movie.get(
            "video"
        )

        poster_key = movie.get(
            "poster"
        )


        if video_key:

            try:
                r2_delete(
                    video_key
                )
            except Exception as exc:
                print(
                    "VIDEO DELETE ERROR:",
                    repr(exc)
                )


        if poster_key:

            try:
                r2_delete(
                    poster_key
                )
            except Exception as exc:
                print(
                    "POSTER DELETE ERROR:",
                    repr(exc)
                )


        flash(
            "Movie deleted successfully.",
            "success"
        )

    else:

        flash(
            "Movie not found.",
            "error"
        )


    return redirect(
        url_for("admin")
    )


# ============================================================
# R2 MULTIPART CREATE
# ============================================================

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


        key = validate_r2_key(
            data.get("key")
        )


        content_type = (
            data.get(
                "content_type"
            )
            or content_type_for_key(
                key
            )
        )


        client = get_r2_client()


        result = (
            client.create_multipart_upload(
                Bucket=R2_BUCKET,
                Key=key,
                ContentType=content_type
            )
        )


        return json_ok(
            upload_id=result[
                "UploadId"
            ],
            key=key,
            part_size=PART_SIZE,
            parallel=PARALLEL_PARTS,
            expires=PRESIGNED_EXPIRES
        )


    except Exception as exc:

        print(
            "R2 multipart create error:",
            repr(exc)
        )

        return json_error(
            "R2 multipart create failed: "
            + str(exc),
            500
        )


# ============================================================
# R2 MULTIPART URLS
# ============================================================

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


        key = validate_r2_key(
            data.get("key")
        )


        upload_id = str(
            data.get(
                "upload_id",
                ""
            )
        ).strip()


        if not upload_id:
            return json_error(
                "upload_id missing."
            )


        raw_parts = (
            data.get(
                "part_numbers"
            )
            or []
        )


        part_numbers = []


        for value in raw_parts:

            number = int(value)

            if number < 1:
                raise ValueError(
                    "Invalid part number."
                )

            if number > MAX_MULTIPART_PARTS:
                raise ValueError(
                    "Part number too large."
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


        if len(part_numbers) > MAX_MULTIPART_PARTS:

            return json_error(
                "Too many parts.",
                400
            )


        client = get_r2_client()


        urls = {}
        url_map = {}


        for part_number in part_numbers:

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
                            part_number
                    },

                    ExpiresIn=
                        PRESIGNED_EXPIRES
                )
            )


            urls[str(part_number)] = url

            url_map[str(part_number)] = url


        return json_ok(
            urls=urls,
            url_map=url_map,
            part_urls=urls,
            part_size=PART_SIZE,
            parallel=PARALLEL_PARTS,
            expires=PRESIGNED_EXPIRES,
            total_parts=len(
                part_numbers
            )
        )


    except Exception as exc:

        print(
            "R2 multipart URLs error:",
            repr(exc)
        )

        return json_error(
            "R2 multipart URLs failed: "
            + str(exc),
            500
        )


# ============================================================
# R2 MULTIPART COMPLETE
# ============================================================

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


        key = validate_r2_key(
            data.get("key")
        )


        upload_id = str(
            data.get(
                "upload_id",
                ""
            )
        ).strip()


        if not upload_id:

            return json_error(
                "upload_id missing."
            )


        raw_parts = (
            data.get(
                "parts"
            )
            or []
        )


        parts = []


        for item in raw_parts:

            if not isinstance(
                item,
                dict
            ):
                continue


            part_number = (
                item.get(
                    "PartNumber"
                )
                or item.get(
                    "part_number"
                )
                or item.get(
                    "part"
                )
            )


            etag = (
                item.get(
                    "ETag"
                )
                or item.get(
                    "etag"
                )
            )


            if not part_number:
                continue

            if not etag:
                continue


            parts.append({
                "PartNumber":
                    int(part_number),

                "ETag":
                    str(etag)
            })


        parts.sort(
            key=lambda x:
                x["PartNumber"]
        )


        if not parts:

            return json_error(
                "No multipart parts supplied."
            )


        client = get_r2_client()


        result = (
            client.complete_multipart_upload(
                Bucket=R2_BUCKET,

                Key=key,

                UploadId=upload_id,

                MultipartUpload={
                    "Parts": parts
                }
            )
        )


        # ----------------------------------------------------
        # VERIFY OBJECT
        # ----------------------------------------------------

        head = client.head_object(
            Bucket=R2_BUCKET,
            Key=key
        )


        size = int(
            head.get(
                "ContentLength",
                0
            )
        )


        if size <= 0:

            return json_error(
                "R2 object completed but size is 0.",
                500
            )


        return json_ok(
            key=key,
            size=size,
            etag=(
                result.get(
                    "ETag"
                )
            ),
            public_url=r2_public_url(
                key
            )
        )


    except Exception as exc:

        print(
            "R2 multipart complete error:",
            repr(exc)
        )

        return json_error(
            "R2 multipart complete failed: "
            + str(exc),
            500
        )


# ============================================================
# R2 MULTIPART ABORT
# ============================================================

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


        key = validate_r2_key(
            data.get("key")
        )


        upload_id = str(
            data.get(
                "upload_id",
                ""
            )
        ).strip()


        if not upload_id:

            return json_error(
                "upload_id missing."
            )


        client = get_r2_client()


        client.abort_multipart_upload(
            Bucket=R2_BUCKET,
            Key=key,
            UploadId=upload_id
        )


        return json_ok(
            message="Multipart upload aborted."
        )


    except Exception as exc:

        print(
            "R2 multipart abort error:",
            repr(exc)
        )

        return json_error(
            "R2 multipart abort failed: "
            + str(exc),
            500
        )


# ============================================================
# R2 DELETE API
# ============================================================

@app.route(
    "/api/r2/delete",
    methods=["POST"]
)
@admin_required
def api_r2_delete():

    try:

        data = request.get_json(
            silent=True
        ) or {}


        key = validate_r2_key(
            data.get("key")
        )


        r2_delete(
            key
        )


        return json_ok(
            key=key
        )


    except Exception as exc:

        print(
            "R2 delete error:",
            repr(exc)
        )

        return json_error(
            "R2 delete failed: "
            + str(exc),
            500
        )


# ============================================================
# SAVE MOVIE
# ============================================================

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
            data.get(
                "title",
                ""
            )
        ).strip()


        category = str(
            data.get(
                "category",
                ""
            )
        ).strip()


        description = str(
            data.get(
                "description",
                ""
            )
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


        if not video_key:

            return json_error(
                "Video key missing."
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


        # ----------------------------------------------------
        # VERIFY VIDEO IN R2
        # ----------------------------------------------------

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

            return json_error(
                "Video R2 object is empty.",
                400
            )


        if video_size > MAX_VIDEO_SIZE:

            return json_error(
                "Video exceeds 4 GB.",
                400
            )


        # ----------------------------------------------------
        # VERIFY POSTER
        # ----------------------------------------------------

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

                return json_error(
                    "Poster R2 object is empty.",
                    400
                )


            if poster_size > MAX_POSTER_SIZE:

                return json_error(
                    "Poster exceeds 25 MB.",
                    400
                )


        # ----------------------------------------------------
        # SAVE DB
        # ----------------------------------------------------

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
                (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
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
                movie_id=movie_id
            )
        )


    except Exception as exc:

        print(
            "MOVIE SAVE ERROR:",
            repr(exc)
        )

        return json_error(
            "Movie save failed: "
            + str(exc),
            500
        )


# ============================================================
# ADS ADMIN
# ============================================================

@app.route(
    "/admin/ads",
    methods=["GET", "POST"]
)
@admin_required
def admin_ads():

    if request.method == "POST":

        top = request.form.get(
            "ad_top",
            ""
        )

        player = request.form.get(
            "ad_player",
            ""
        )

        bottom = request.form.get(
            "ad_bottom",
            ""
        )


        set_setting(
            "ad_top",
            top
        )

        set_setting(
            "ad_player",
            player
        )

        set_setting(
            "ad_bottom",
            bottom
        )


        flash(
            "Ads settings saved.",
            "success"
        )


        return redirect(
            url_for("admin_ads")
        )


    ads = get_ads()


    return render_template(
        "ads.html",
        ads=ads
    )


# ============================================================
# ADS.TXT
# ============================================================

@app.route("/ads.txt")
def ads_txt():

    content = (
        "google.com, "
        "pub-8697157365303435, "
        "DIRECT, "
        "f08c47fec0942fa0\n"
    )


    return Response(
        content,
        mimetype="text/plain"
    )


# ============================================================
# R2 HEALTH
# ============================================================

@app.route("/r2-health")
def r2_health():

    try:

        client = get_r2_client()


        client.list_objects_v2(
            Bucket=R2_BUCKET,
            MaxKeys=1
        )


        return json_ok(
            message="R2 OK",
            bucket=R2_BUCKET
        )


    except Exception as exc:

        print(
            "R2 HEALTH ERROR:",
            repr(exc)
        )

        return json_error(
            "R2 ERROR: "
            + str(exc),
            500
        )


# ============================================================
# DATABASE HEALTH
# ============================================================

@app.route("/db-health")
def db_health():

    try:

        conn = get_db()

        try:

            cur = conn.cursor()

            cur.execute(
                "SELECT 1"
            )

            result = cur.fetchone()

            cur.close()

        finally:
            conn.close()


        return json_ok(
            message="Database OK"
        )


    except Exception as exc:

        print(
            "DB HEALTH ERROR:",
            repr(exc)
        )

        return json_error(
            "Database ERROR: "
            + str(exc),
            500
        )


# ============================================================
# GENERAL HEALTH
# ============================================================

@app.route("/health")
def health():

    return jsonify({
        "ok": True,
        "status": "ok",
        "app": "Tomesh Movies"
    })


# ============================================================
# LEGACY POSTER ROUTE
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
            key = (
                POSTER_PREFIX
                + name
            )


        url = r2_presigned_url(
            key
        )


        return redirect(
            url
        )


    except Exception as exc:

        print(
            "LEGACY POSTER ERROR:",
            repr(exc)
        )

        return Response(
            "Poster not found.",
            status=404
        )


# ============================================================
# LEGACY VIDEO ROUTE
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
            key = (
                VIDEO_PREFIX
                + name
            )


        url = r2_presigned_url(
            key
        )


        return redirect(
            url
        )


    except Exception as exc:

        print(
            "LEGACY VIDEO ERROR:",
            repr(exc)
        )

        return Response(
            "Video not found.",
            status=404
        )


# ============================================================
# ERROR 413
# ============================================================

@app.errorhandler(413)
def request_entity_too_large(error):

    return Response(
        "File too large. Maximum 4 GB.",
        status=413
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
            font-family:Arial,sans-serif;
            display:flex;
            align-items:center;
            justify-content:center;
            min-height:100vh;
            text-align:center;
        ">

            <div>

                <h1 style="
                    font-size:60px;
                    margin:0;
                ">
                    404
                </h1>

                <p>
                    Page not found.
                </p>

                <a href="/"
                   style="
                       color:#ffc400;
                       text-decoration:none;
                       font-weight:bold;
                   ">
                    Go Home
                </a>

            </div>

        </body>
        </html>
        """,
        status=404,
        mimetype="text/html"
    )


# ============================================================
# ERROR 500
# ============================================================

@app.errorhandler(500)
def internal_error(error):

    print(
        "500 ERROR:",
        repr(error)
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
            font-family:Arial,sans-serif;
            display:flex;
            align-items:center;
            justify-content:center;
            min-height:100vh;
            text-align:center;
        ">

            <div>

                <h1 style="
                    font-size:55px;
                    margin:0;
                    color:#ff315b;
                ">
                    500
                </h1>

                <p>
                    Server error.
                </p>

                <a href="/"
                   style="
                       color:#ffc400;
                       text-decoration:none;
                       font-weight:bold;
                   ">
                    Go Home
                </a>

            </div>

        </body>
        </html>
        """,
        status=500,
        mimetype="text/html"
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
            "WARNING: DATABASE_URL is missing."
        )

except Exception as exc:

    print(
        "Database initialization error:",
        repr(exc)
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
        debug=False
    )
