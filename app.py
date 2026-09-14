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
# ADMIN / DATABASE
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
# R2 SETTINGS
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

    if len(value) >= 2:

        if (
            value.startswith('"')
            and value.endswith('"')
        ) or (
            value.startswith("'")
            and value.endswith("'")
        ):

            value = value[1:-1].strip()

    value = value.replace("\r", "")
    value = value.replace("\n", "")
    value = value.strip()

    return value


def clean_endpoint(value):

    value = clean_env_value(value)

    if not value:
        return ""

    value = value.rstrip("/")

    bucket_name = clean_env_value(
        os.environ.get(
            "R2_BUCKET",
            "tomesh-movies"
        )
    )

    suffix = "/" + bucket_name

    if value.lower().endswith(
        suffix.lower()
    ):

        value = value[
            :-len(suffix)
        ]

    return value.rstrip("/")


# ============================================================
# R2 ENV
# ============================================================

R2_ACCOUNT_ID = clean_env_value(
    os.environ.get(
        "R2_ACCOUNT_ID",
        ""
    )
)

R2_ACCESS_KEY_ID = clean_env_value(
    os.environ.get(
        "R2_ACCESS_KEY_ID",
        ""
    )
)

R2_SECRET_ACCESS_KEY = clean_env_value(
    os.environ.get(
        "R2_SECRET_ACCESS_KEY",
        ""
    )
)

R2_BUCKET = (
    clean_env_value(
        os.environ.get(
            "R2_BUCKET",
            "tomesh-movies"
        )
    )
    or "tomesh-movies"
)

R2_ENDPOINT = clean_endpoint(
    os.environ.get(
        "R2_ENDPOINT",
        ""
    )
)

R2_PUBLIC_URL = clean_env_value(
    os.environ.get(
        "R2_PUBLIC_URL",
        ""
    )
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


def json_error(
    message,
    status=400,
    **kwargs
):

    data = {
        "ok": False,
        "error": str(message)
    }

    data.update(kwargs)

    return jsonify(data), status


# ============================================================
# FILE HELPERS
# ============================================================

def get_extension(filename):

    if not filename:
        return ""

    filename = secure_filename(
        str(filename)
    )

    if "." not in filename:
        return ""

    return filename.rsplit(
        ".",
        1
    )[1].lower()


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

    original = secure_filename(
        filename or "file"
    )

    ext = get_extension(
        original
    )

    if ext:

        return (
            secrets.token_hex(16)
            + "."
            + ext
        )

    return secrets.token_hex(16)


# ============================================================
# MIME TYPES
# ============================================================

def content_type_for_key(key):

    key = str(
        key or ""
    ).strip()

    ext = get_extension(key)

    if ext == "mp4":
        return "video/mp4"

    if ext == "mkv":
        return "video/x-matroska"

    if ext == "webm":
        return "video/webm"

    if ext == "mov":
        return "video/quicktime"

    if ext in {
        "jpg",
        "jpeg"
    }:
        return "image/jpeg"

    if ext == "png":
        return "image/png"

    if ext == "webp":
        return "image/webp"

    guessed, _ = mimetypes.guess_type(
        key
    )

    if guessed:
        return guessed

    return "application/octet-stream"


# ============================================================
# R2 KEY VALIDATION
# ============================================================

def validate_r2_key(
    key,
    allowed_prefixes=None
):

    if not key:
        raise ValueError(
            "R2 object key is required."
        )

    key = str(
        key
    ).strip()

    if not key:
        raise ValueError(
            "R2 object key is required."
        )

    if key.startswith("/"):
        raise ValueError(
            "Invalid R2 object key."
        )

    if ".." in key:
        raise ValueError(
            "Invalid R2 object key."
        )

    if allowed_prefixes:

        if not any(
            key.startswith(prefix)
            for prefix in allowed_prefixes
        ):

            raise ValueError(
                "Invalid R2 object key prefix."
            )

    return key


# ============================================================
# DATABASE
# ============================================================

def get_db(dict_rows=False):

    if not DATABASE_URL:

        raise RuntimeError(
            "DATABASE_URL environment variable is missing."
        )

    if dict_rows:

        return psycopg2.connect(
            DATABASE_URL,
            sslmode="require",
            cursor_factory=RealDictCursor
        )

    return psycopg2.connect(
        DATABASE_URL,
        sslmode="require"
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

def get_setting(
    key,
    default=""
):

    conn = get_db(
        dict_rows=True
    )

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

        if row and row.get("value") is not None:
            return row["value"]

        return default

    finally:

        conn.close()


def set_setting(
    key,
    value
):

    conn = get_db()

    try:

        cur = conn.cursor()

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
        )
    }


# ============================================================
# R2 CLIENT
# ============================================================

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
            signature_version="s3v4",
            s3={
                "addressing_style": "path"
            },
            retries={
                "max_attempts": 4,
                "mode": "standard"
            }
        )
    )


# ============================================================
# R2 URL HELPERS
# ============================================================

def r2_public_url(key):

    if not key:
        return None

    key = str(
        key
    ).strip()

    if (
        key.startswith("http://")
        or key.startswith("https://")
    ):

        return key

    if not R2_PUBLIC_URL:

        raise RuntimeError(
            "R2_PUBLIC_URL is missing."
        )

    key = key.lstrip("/")

    return (
        R2_PUBLIC_URL
        + "/"
        + quote(
            key,
            safe="/"
        )
    )


def media_url(value):

    if not value:
        return None

    value = str(
        value
    ).strip()

    if (
        value.startswith("http://")
        or value.startswith("https://")
    ):

        return value

    return r2_public_url(value)


def r2_presigned_url(
    key,
    expires=PRESIGNED_EXPIRES
):

    if not key:
        return None

    key = str(
        key
    ).strip()

    if (
        key.startswith("http://")
        or key.startswith("https://")
    ):

        return key

    client = get_r2_client()

    return client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": R2_BUCKET,
            "Key": key
        },
        ExpiresIn=expires
    )


# ============================================================
# R2 OBJECT FUNCTIONS
# ============================================================

def r2_head(key):

    client = get_r2_client()

    return client.head_object(
        Bucket=R2_BUCKET,
        Key=key
    )


def r2_delete(key):

    if not key:
        return

    client = get_r2_client()

    client.delete_object(
        Bucket=R2_BUCKET,
        Key=key
    )


# ============================================================
# AUTH
# ============================================================

def admin_required(view):

    @wraps(view)
    def wrapped(*args, **kwargs):

        if not session.get(
            "admin_logged_in"
        ):

            return redirect(
                url_for("login")
            )

        return view(
            *args,
            **kwargs
        )

    return wrapped


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

        try:

            movie["poster_url"] = media_url(
                movie.get("poster")
            )

        except Exception as e:

            print(
                "HOME POSTER URL ERROR:",
                repr(e)
            )

            movie["poster_url"] = None

    return render_template(
        "index.html",
        movies=movies,
        ads=get_ads()
    )


# ============================================================
# HOME ALIAS
# ============================================================

app.add_url_rule(
    "/",
    endpoint="index",
    view_func=home
)


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
            SET views =
                COALESCE(views, 0) + 1
            WHERE id = %s
            """,
            (movie_id,)
        )

        conn.commit()

        cur.close()

    finally:

        conn.close()

    video_key = movie.get("video")
    poster_key = movie.get("poster")

    # IMPORTANT:
    # Browser will use our own streaming endpoint.
    # That endpoint supports HTTP Range requests.
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
        movie["video_mime"] = "video/mp4"

    # Poster remains direct R2 URL.
    if poster_key:

        try:

            movie["poster_url"] = media_url(
                poster_key
            )

        except Exception as e:

            print(
                "POSTER URL ERROR:",
                repr(e)
            )

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
# VIDEO STREAMING WITH HTTP RANGE SUPPORT
# ============================================================

@app.route(
    "/stream/<int:movie_id>"
)
def stream_movie(movie_id):

    conn = get_db(
        dict_rows=True
    )

    try:

        cur = conn.cursor()

        cur.execute(
            """
            SELECT
                id,
                video,
                title
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

        abort(404)

    video_key = str(
        movie.get("video") or ""
    ).strip()

    if not video_key:

        abort(404)

    try:

        video_key = validate_r2_key(
            video_key,
            [
                VIDEO_PREFIX
            ]
        )

    except Exception:

        abort(404)

    try:

        client = get_r2_client()

        # Get object metadata first.
        head = client.head_object(
            Bucket=R2_BUCKET,
            Key=video_key
        )

        total_size = int(
            head.get(
                "ContentLength",
                0
            ) or 0
        )

        if total_size <= 0:

            abort(404)

        content_type = (
            head.get("ContentType")
            or content_type_for_key(
                video_key
            )
        )

        range_header = request.headers.get(
            "Range"
        )

        # ----------------------------------------------------
        # NO RANGE
        # ----------------------------------------------------

        if not range_header:

            r2_object = client.get_object(
                Bucket=R2_BUCKET,
                Key=video_key
            )

            body = r2_object["Body"]

            def generate_full():

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

            response = Response(
                generate_full(),
                status=200,
                mimetype=content_type
            )

            response.headers["Accept-Ranges"] = "bytes"
            response.headers["Content-Length"] = str(
                total_size
            )
            response.headers["Cache-Control"] = (
                "public, max-age=3600"
            )

            return response

        # ----------------------------------------------------
        # RANGE REQUEST
        # ----------------------------------------------------

        if not range_header.startswith("bytes="):

            return Response(
                "Invalid Range",
                status=416
            )

        range_value = range_header[
            6:
        ].split(",", 1)[0].strip()

        if "-" not in range_value:

            return Response(
                "Invalid Range",
                status=416
            )

        start_text, end_text = (
            range_value.split(
                "-",
                1
            )
        )

        # bytes=-500000
        if start_text == "":

            try:

                suffix_length = int(
                    end_text
                )

            except Exception:

                return Response(
                    "Invalid Range",
                    status=416
                )

            if suffix_length <= 0:

                return Response(
                    "Invalid Range",
                    status=416
                )

            if suffix_length > total_size:

                suffix_length = total_size

            start = (
                total_size
                - suffix_length
            )

            end = total_size - 1

        else:

            try:

                start = int(
                    start_text
                )

            except Exception:

                return Response(
                    "Invalid Range",
                    status=416
                )

            if start < 0:

                return Response(
                    "Invalid Range",
                    status=416
                )

            if start >= total_size:

                response = Response(
                    status=416
                )

                response.headers[
                    "Content-Range"
                ] = (
                    "bytes */"
                    + str(total_size)
                )

                return response

            if end_text == "":

                end = total_size - 1

            else:

                try:

                    end = int(
                        end_text
                    )

                except Exception:

                    return Response(
                        "Invalid Range",
                        status=416
                    )

                if end < start:

                    return Response(
                        "Invalid Range",
                        status=416
                    )

                if end >= total_size:

                    end = total_size - 1

        content_length = (
            end
            - start
            + 1
        )

        r2_object = client.get_object(
            Bucket=R2_BUCKET,
            Key=video_key,
            Range=(
                "bytes="
                + str(start)
                + "-"
                + str(end)
            )
        )

        body = r2_object["Body"]

        def generate_range():

            remaining = content_length

            try:

                while remaining > 0:

                    chunk_size = min(
                        1024 * 1024,
                        remaining
                    )

                    chunk = body.read(
                        chunk_size
                    )

                    if not chunk:
                        break

                    remaining -= len(
                        chunk
                    )

                    yield chunk

            finally:

                try:
                    body.close()
                except Exception:
                    pass

        response = Response(
            generate_range(),
            status=206,
            mimetype=content_type
        )

        response.headers["Accept-Ranges"] = "bytes"

        response.headers["Content-Range"] = (
            "bytes "
            + str(start)
            + "-"
            + str(end)
            + "/"
            + str(total_size)
        )

        response.headers["Content-Length"] = str(
            content_length
        )

        response.headers["Cache-Control"] = (
            "public, max-age=3600"
        )

        return response

    except Exception as e:

        print(
            "VIDEO STREAM ERROR:",
            repr(e)
        )

        return Response(
            "Video streaming failed: "
            + str(e),
            status=500,
            mimetype="text/plain"
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
        ).strip()

        password = request.form.get(
            "password",
            ""
        ).strip()

        if (
            username == ADMIN_USER
            and password == ADMIN_PASSWORD
        ):

            session.clear()

            session["admin_logged_in"] = True
            session["admin_user"] = username

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

        try:

            movie["poster_url"] = media_url(
                movie.get("poster")
            )

        except Exception:

            movie["poster_url"] = None

    total_movies = len(movies)

    total_views = sum(
        int(
            movie.get("views") or 0
        )
        for movie in movies
    )

    return render_template(
        "admin.html",
        movies=movies,
        total_movies=total_movies,
        total_views=total_views,
        ads=get_ads()
    )


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
        ""
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
            url_for("admin_add")
        )

    if not video or not video.filename:

        flash(
            "Video is required.",
            "error"
        )

        return redirect(
            url_for("admin_add")
        )

    if not allowed_video(
        video.filename
    ):

        flash(
            "Video केवल MP4, MKV, WebM या MOV होनी चाहिए.",
            "error"
        )

        return redirect(
            url_for("admin_add")
        )

    video_key = None
    poster_key = None

    try:

        client = get_r2_client()

        video_key = (
            VIDEO_PREFIX
            + safe_filename(
                video.filename
            )
        )

        video.seek(0)

        client.upload_fileobj(
            video,
            R2_BUCKET,
            video_key,
            ExtraArgs={
                "ContentType":
                    content_type_for_key(
                        video_key
                    )
            }
        )

        if poster and poster.filename:

            if not allowed_poster(
                poster.filename
            ):

                raise ValueError(
                    "Poster केवल JPG, JPEG, PNG या WEBP होनी चाहिए."
                )

            poster_key = (
                POSTER_PREFIX
                + safe_filename(
                    poster.filename
                )
            )

            poster.seek(0)

            client.upload_fileobj(
                poster,
                R2_BUCKET,
                poster_key,
                ExtraArgs={
                    "ContentType":
                        content_type_for_key(
                            poster_key
                        )
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
            "Movie successfully added.",
            "success"
        )

        return redirect(
            url_for("admin")
        )

    except Exception as e:

        print(
            "ADMIN ADD ERROR:",
            repr(e)
        )

        try:

            if video_key:
                r2_delete(video_key)

        except Exception:
            pass

        try:

            if poster_key:
                r2_delete(poster_key)

        except Exception:
            pass

        flash(
            "Movie upload failed: "
            + str(e),
            "error"
        )

        return redirect(
            url_for("admin_add")
        )


# ============================================================
# R2 MULTIPART CREATE
# ============================================================

@app.route(
    "/api/r2/multipart/create",
    methods=["POST"]
)
@admin_required
def api_r2_multipart_create():

    try:

        data = request.get_json(
            silent=True
        ) or {}

        key = str(
            data.get("key") or ""
        ).strip()

        content_type = str(
            data.get("content_type")
            or "application/octet-stream"
        ).strip()

        key = validate_r2_key(
            key,
            [
                VIDEO_PREFIX,
                POSTER_PREFIX
            ]
        )

        client = get_r2_client()

        response = client.create_multipart_upload(
            Bucket=R2_BUCKET,
            Key=key,
            ContentType=content_type
        )

        upload_id = response.get(
            "UploadId"
        )

        if not upload_id:

            return json_error(
                "R2 did not return UploadId.",
                500
            )

        return json_ok(
            key=key,
            upload_id=upload_id,
            part_size=PART_SIZE,
            parallel=PARALLEL_PARTS,
            expires=PRESIGNED_EXPIRES
        )

    except Exception as e:

        print(
            "R2 multipart create error:",
            repr(e)
        )

        return json_error(
            "R2 multipart create failed: "
            + str(e),
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
def api_r2_multipart_urls():

    try:

        data = request.get_json(
            silent=True
        ) or {}

        key = str(
            data.get("key") or ""
        ).strip()

        upload_id = str(
            data.get("upload_id")
            or ""
        ).strip()

        part_numbers = (
            data.get("part_numbers")
            or data.get("parts")
            or []
        )

        key = validate_r2_key(
            key,
            [
                VIDEO_PREFIX,
                POSTER_PREFIX
            ]
        )

        if not upload_id:

            return json_error(
                "Upload ID is required."
            )

        if not isinstance(
            part_numbers,
            list
        ):

            return json_error(
                "part_numbers must be an array."
            )

        if not part_numbers:

            return json_error(
                "No part numbers supplied."
            )

        if len(part_numbers) > MAX_MULTIPART_PARTS:

            return json_error(
                "Too many multipart parts."
            )

        clean_parts = []

        for part in part_numbers:

            try:

                number = int(part)

            except Exception:

                continue

            if number < 1:
                continue

            if number > MAX_MULTIPART_PARTS:
                continue

            clean_parts.append(
                number
            )

        clean_parts = sorted(
            set(clean_parts)
        )

        if not clean_parts:

            return json_error(
                "Invalid multipart part numbers."
            )

        client = get_r2_client()

        urls = []
        url_map = {}

        for part_number in clean_parts:

            presigned_url = client.generate_presigned_url(
                "upload_part",
                Params={
                    "Bucket": R2_BUCKET,
                    "Key": key,
                    "UploadId": upload_id,
                    "PartNumber": part_number
                },
                ExpiresIn=PRESIGNED_EXPIRES,
                HttpMethod="PUT"
            )

            urls.append(
                {
                    "part_number": part_number,
                    "url": presigned_url
                }
            )

            url_map[
                str(part_number)
            ] = presigned_url

        return json_ok(
            key=key,
            upload_id=upload_id,
            urls=urls,
            url_map=url_map,
            part_urls=urls,
            part_size=PART_SIZE,
            parallel=PARALLEL_PARTS,
            expires=PRESIGNED_EXPIRES,
            total_parts=len(clean_parts)
        )

    except Exception as e:

        print(
            "R2 multipart URLs error:",
            repr(e)
        )

        return json_error(
            "R2 presigned URLs failed: "
            + str(e),
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
def api_r2_multipart_complete():

    try:

        data = request.get_json(
            silent=True
        ) or {}

        key = str(
            data.get("key") or ""
        ).strip()

        upload_id = str(
            data.get("upload_id")
            or ""
        ).strip()

        parts = data.get(
            "parts"
        ) or []

        key = validate_r2_key(
            key,
            [
                VIDEO_PREFIX,
                POSTER_PREFIX
            ]
        )

        if not upload_id:

            return json_error(
                "Upload ID is required."
            )

        if not isinstance(
            parts,
            list
        ):

            return json_error(
                "parts must be an array."
            )

        if not parts:

            return json_error(
                "No completed parts supplied."
            )

        completed_parts = []

        for item in parts:

            if not isinstance(
                item,
                dict
            ):
                continue

            part_number = (
                item.get("PartNumber")
                or item.get("part_number")
                or item.get("part")
            )

            etag = (
                item.get("ETag")
                or item.get("etag")
            )

            if part_number is None:
                continue

            if not etag:
                continue

            try:

                part_number = int(
                    part_number
                )

            except Exception:

                continue

            if part_number < 1:
                continue

            if part_number > MAX_MULTIPART_PARTS:
                continue

            completed_parts.append(
                {
                    "PartNumber": part_number,
                    "ETag": str(etag)
                }
            )

        unique_parts = {}

        for item in completed_parts:

            unique_parts[
                item["PartNumber"]
            ] = item["ETag"]

        completed_parts = [
            {
                "PartNumber": number,
                "ETag": etag
            }
            for number, etag
            in unique_parts.items()
        ]

        completed_parts.sort(
            key=lambda x: x["PartNumber"]
        )

        if not completed_parts:

            return json_error(
                "No valid completed parts."
            )

        client = get_r2_client()

        response = client.complete_multipart_upload(
            Bucket=R2_BUCKET,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": completed_parts
            }
        )

        location = response.get(
            "Location"
        )

        try:

            head = client.head_object(
                Bucket=R2_BUCKET,
                Key=key
            )

            object_size = int(
                head.get(
                    "ContentLength",
                    0
                ) or 0
            )

        except Exception:

            object_size = 0

        if object_size <= 0:

            return json_error(
                "R2 upload completed but object could not be verified.",
                500
            )

        return json_ok(
            key=key,
            upload_id=upload_id,
            location=location,
            size=object_size,
            url=r2_public_url(key)
        )

    except Exception as e:

        print(
            "R2 multipart complete error:",
            repr(e)
        )

        return json_error(
            "R2 multipart complete failed: "
            + str(e),
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
def api_r2_multipart_abort():

    try:

        data = request.get_json(
            silent=True
        ) or {}

        key = str(
            data.get("key") or ""
        ).strip()

        upload_id = str(
            data.get("upload_id")
            or ""
        ).strip()

        key = validate_r2_key(
            key,
            [
                VIDEO_PREFIX,
                POSTER_PREFIX
            ]
        )

        if not upload_id:

            return json_error(
                "Upload ID is required."
            )

        client = get_r2_client()

        client.abort_multipart_upload(
            Bucket=R2_BUCKET,
            Key=key,
            UploadId=upload_id
        )

        return json_ok(
            key=key,
            upload_id=upload_id
        )

    except Exception as e:

        print(
            "R2 multipart abort error:",
            repr(e)
        )

        return json_error(
            "R2 multipart abort failed: "
            + str(e),
            500
        )


# ============================================================
# DELETE R2 OBJECT
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

        key = str(
            data.get("key") or ""
        ).strip()

        key = validate_r2_key(
            key,
            [
                VIDEO_PREFIX,
                POSTER_PREFIX
            ]
        )

        r2_delete(key)

        return json_ok(
            key=key
        )

    except Exception as e:

        print(
            "R2 delete error:",
            repr(e)
        )

        return json_error(
            "R2 delete failed: "
            + str(e),
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
            data.get("title") or ""
        ).strip()

        category = str(
            data.get("category") or ""
        ).strip()

        description = str(
            data.get("description") or ""
        ).strip()

        poster = str(
            data.get("poster") or ""
        ).strip()

        video = str(
            data.get("video") or ""
        ).strip()

        if not title:

            return json_error(
                "Movie title is required."
            )

        if not video:

            return json_error(
                "Video key is required."
            )

        video = validate_r2_key(
            video,
            [
                VIDEO_PREFIX
            ]
        )

        if poster:

            poster = validate_r2_key(
                poster,
                [
                    POSTER_PREFIX
                ]
            )

        video_head = r2_head(
            video
        )

        video_size = int(
            video_head.get(
                "ContentLength",
                0
            ) or 0
        )

        if video_size <= 0:

            return json_error(
                "Uploaded video object is empty."
            )

        if video_size > MAX_VIDEO_SIZE:

            return json_error(
                "Video size is larger than 4 GB."
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
                RETURNING id
                """,
                (
                    title,
                    category,
                    description,
                    poster or None,
                    video
                )
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
            movie_id=movie_id,
            title=title,
            video=video,
            poster=poster or None,
            video_url=url_for(
                "stream_movie",
                movie_id=movie_id
            )
        )

    except Exception as e:

        print(
            "MOVIE SAVE ERROR:",
            repr(e)
        )

        return json_error(
            "Movie save failed: "
            + str(e),
            500
        )


# ============================================================
# DELETE MOVIE
# ============================================================

@app.route(
    "/admin/delete/<int:movie_id>",
    methods=["POST", "GET"]
)
@admin_required
def admin_delete_movie(
    movie_id
):

    conn = get_db(
        dict_rows=True
    )

    video_key = None
    poster_key = None

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

            flash(
                "Movie not found.",
                "error"
            )

            return redirect(
                url_for("admin")
            )

        video_key = movie.get(
            "video"
        )

        poster_key = movie.get(
            "poster"
        )

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

        try:

            r2_delete(
                video_key
            )

        except Exception as e:

            print(
                "VIDEO R2 DELETE ERROR:",
                repr(e)
            )

    if poster_key:

        try:

            r2_delete(
                poster_key
            )

        except Exception as e:

            print(
                "POSTER R2 DELETE ERROR:",
                repr(e)
            )

    flash(
        "Movie deleted successfully.",
        "success"
    )

    return redirect(
        url_for("admin")
    )


# ============================================================
# ADS SETTINGS
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

    return render_template(
        "ads.html",
        ads=get_ads()
    )


# ============================================================
# ADS.TXT
# ============================================================

@app.route("/ads.txt")
def ads_txt():

    publisher_id = (
        "pub-8697157365303435"
    )

    content = (
        "google.com, "
        + publisher_id
        + ", DIRECT, "
        + "f08c47fec0942fa0"
        + "\n"
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

        client.head_bucket(
            Bucket=R2_BUCKET
        )

        return Response(
            "R2 OK",
            mimetype="text/plain"
        )

    except Exception as e:

        print(
            "R2 HEALTH ERROR:",
            repr(e)
        )

        return Response(
            "R2 ERROR: "
            + str(e),
            status=500,
            mimetype="text/plain"
        )


# ============================================================
# DATABASE HEALTH
# ============================================================

@app.route("/db-health")
def db_health():

    try:

        conn = get_db()

        cur = conn.cursor()

        cur.execute(
            "SELECT 1"
        )

        cur.fetchone()

        cur.close()
        conn.close()

        return Response(
            "DATABASE OK",
            mimetype="text/plain"
        )

    except Exception as e:

        print(
            "DB HEALTH ERROR:",
            repr(e)
        )

        return Response(
            "DATABASE ERROR: "
            + str(e),
            status=500,
            mimetype="text/plain"
        )


# ============================================================
# GENERAL HEALTH
# ============================================================

@app.route("/health")
def health():

    return json_ok(
        status="ok",
        app="Tomesh Movies"
    )


# ============================================================
# LEGACY POSTER
# ============================================================

@app.route(
    "/poster/<path:name>"
)
def poster(name):

    try:

        return redirect(
            r2_presigned_url(
                name,
                expires=3600
            )
        )

    except Exception as e:

        print(
            "POSTER ROUTE ERROR:",
            repr(e)
        )

        abort(404)


# ============================================================
# LEGACY VIDEO
# ============================================================

@app.route(
    "/video/<path:name>"
)
def video(name):

    try:

        return redirect(
            r2_presigned_url(
                name,
                expires=3600
            )
        )

    except Exception as e:

        print(
            "VIDEO ROUTE ERROR:",
            repr(e)
        )

        abort(404)


# ============================================================
# 413
# ============================================================

@app.errorhandler(413)
def request_too_large(error):

    return (
        "File बहुत बड़ी है. Maximum video size 4 GB है.",
        413
    )


# ============================================================
# 404
# ============================================================

@app.errorhandler(404)
def page_not_found(error):

    return (
        """
        <!doctype html>
        <html lang="hi">

        <head>

            <meta charset="utf-8">

            <meta
                name="viewport"
                content="width=device-width,initial-scale=1"
            >

            <title>
                Tomesh Movies - Page Not Found
            </title>

            <style>

                body{
                    margin:0;
                    min-height:100vh;
                    display:flex;
                    align-items:center;
                    justify-content:center;
                    background:#080808;
                    color:#fff;
                    font-family:Arial,sans-serif;
                    text-align:center;
                }

                .box{
                    width:min(90%,600px);
                    padding:40px 25px;
                    border:1px solid #333;
                    border-radius:20px;
                    background:#111;
                }

                h1{
                    font-size:32px;
                    margin:0 0 15px;
                }

                p{
                    color:#aaa;
                    line-height:1.6;
                }

                a{
                    display:inline-block;
                    margin-top:15px;
                    padding:12px 22px;
                    border-radius:10px;
                    background:#d4af37;
                    color:#000;
                    text-decoration:none;
                    font-weight:700;
                }

            </style>

        </head>

        <body>

            <div class="box">

                <h1>🎬 Page Not Found</h1>

                <p>
                    यह page Tomesh Movies पर नहीं मिला।
                </p>

                <a href="/">
                    ← Home पर जाएँ
                </a>

            </div>

        </body>

        </html>
        """,
        404
    )


# ============================================================
# 500
# ============================================================

@app.errorhandler(500)
def internal_server_error(error):

    print(
        "INTERNAL SERVER ERROR:",
        repr(error)
    )

    return (
        """
        <!doctype html>
        <html lang="hi">

        <head>

            <meta charset="utf-8">

            <meta
                name="viewport"
                content="width=device-width,initial-scale=1"
            >

            <title>
                Tomesh Movies - Server Error
            </title>

            <style>

                body{
                    margin:0;
                    min-height:100vh;
                    display:flex;
                    align-items:center;
                    justify-content:center;
                    background:#080808;
                    color:#fff;
                    font-family:Arial,sans-serif;
                    text-align:center;
                }

                .box{
                    width:min(90%,600px);
                    padding:40px 25px;
                    border:1px solid #333;
                    border-radius:20px;
                    background:#111;
                    box-shadow:
                        0 20px 60px rgba(0,0,0,.5);
                }

                h1{
                    margin:0 0 15px;
                    font-size:32px;
                }

                p{
                    color:#bbb;
                    line-height:1.6;
                }

                a{
                    display:inline-block;
                    margin-top:15px;
                    padding:12px 22px;
                    border-radius:10px;
                    background:#d4af37;
                    color:#000;
                    text-decoration:none;
                    font-weight:700;
                }

            </style>

        </head>

        <body>

            <div class="box">

                <h1>
                    🎬 Something went wrong
                </h1>

                <p>
                    Tomesh Movies में temporary server error आया है.
                    कृपया थोड़ी देर बाद फिर कोशिश करें।
                </p>

                <a href="/">
                    ← Home पर जाएँ
                </a>

            </div>

        </body>

        </html>
        """,
        500
    )


# ============================================================
# STARTUP
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

except Exception as e:

    print(
        "Database initialization error:",
        repr(e)
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
