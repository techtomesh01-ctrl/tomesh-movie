import os
import secrets
import subprocess
import tempfile
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

# Maximum upload size = 4 GB
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


def r2_upload(
    file_storage,
    object_key,
    content_type=None
):

    if not file_storage:
        raise RuntimeError(
            "Upload file missing."
        )

    client = get_r2_client()

    extra_args = {}

    if content_type:
        extra_args["ContentType"] = content_type

    file_storage.seek(0)

    client.upload_fileobj(
        file_storage,
        R2_BUCKET,
        object_key,
        ExtraArgs=extra_args
    )

    return object_key


def r2_upload_path(
    file_path,
    object_key,
    content_type=None
):

    if not os.path.isfile(file_path):
        raise RuntimeError(
            "Converted file नहीं मिली."
        )

    client = get_r2_client()

    extra_args = {}

    if content_type:
        extra_args["ContentType"] = content_type

    with open(
        file_path,
        "rb"
    ) as file_object:

        client.upload_fileobj(
            file_object,
            R2_BUCKET,
            object_key,
            ExtraArgs=extra_args
        )

    return object_key


def r2_delete(object_key):

    if not object_key:
        return

    client = get_r2_client()

    client.delete_object(
        Bucket=R2_BUCKET,
        Key=object_key
    )


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


def r2_exists(object_key):

    if not object_key:
        return False

    try:

        client = get_r2_client()

        client.head_object(
            Bucket=R2_BUCKET,
            Key=object_key
        )

        return True

    except Exception:
        return False


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
            "Presigned URL creation failed: %s",
            e
        )

        return ""


# =========================================================
# UPLOAD DIRECTORIES
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
    "mkv",
}


# =========================================================
# FILE CHECK
# =========================================================

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


def get_content_type(filename, default):

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
        ".mkv": "video/x-matroska",
    }

    return content_types.get(
        extension,
        default
    )


# =========================================================
# FFMPEG CHECK
# =========================================================

def ffmpeg_available():

    try:

        result = subprocess.run(
            [
                "ffmpeg",
                "-version"
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10
        )

        return result.returncode == 0

    except Exception:

        return False


# =========================================================
# MKV -> MP4 CONVERSION
# =========================================================

def convert_mkv_to_mp4(
    input_file,
    output_path
):

    if not ffmpeg_available():

        raise RuntimeError(
            "FFmpeg Render server पर installed नहीं है."
        )

    input_file.seek(0)

    temp_input = None

    try:

        with tempfile.NamedTemporaryFile(
            suffix=".mkv",
            delete=False
        ) as temp:

            temp_input = temp.name

            while True:

                chunk = input_file.read(
                    8 * 1024 * 1024
                )

                if not chunk:
                    break

                temp.write(chunk)

        command = [
            "ffmpeg",
            "-y",
            "-i",
            temp_input,
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            output_path
        ]

        app.logger.info(
            "Starting MKV -> MP4 conversion."
        )

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        if result.returncode != 0:

            app.logger.error(
                "FFmpeg error: %s",
                result.stderr[-5000:]
            )

            raise RuntimeError(
                "MKV को MP4 में convert नहीं किया जा सका."
            )

        if not os.path.isfile(output_path):

            raise RuntimeError(
                "FFmpeg ने MP4 file create नहीं की."
            )

        return output_path

    finally:

        if temp_input:

            try:
                os.remove(temp_input)
            except Exception:
                pass


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

        default_settings = {
            "ad_top": "",
            "ad_player": "",
            "ad_bottom": "",
        }

        for key, value in default_settings.items():

            con.execute(
                """
                INSERT INTO settings (
                    key,
                    value
                )
                VALUES (%s, %s)
                ON CONFLICT (key)
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
# GLOBAL TEMPLATE DATA
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

        # IMPORTANT:
        # movie.html में movie.title इस्तेमाल हो रहा है.
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
# HEALTH CHECK
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
            "Health check failed: %s",
            e
        )

        return "Database error", 500

    finally:

        if con:
            con.close()


# =========================================================
# R2 HEALTH CHECK
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
            "R2 health check failed: %s",
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
# ADD MOVIE
# =========================================================

@app.route(
    "/admin/add",
    methods=["POST"]
)
@admin_required
def add_movie():

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

    poster_file = request.files.get(
        "poster"
    )

    video_file = request.files.get(
        "video"
    )


    # =====================================================
    # REQUIRED FIELDS
    # =====================================================

    if not title:

        flash(
            "Movie title जरूरी है."
        )

        return redirect(
            url_for("admin")
        )

    if (
        not video_file
        or not video_file.filename
    ):

        flash(
            "Movie video जरूरी है."
        )

        return redirect(
            url_for("admin")
        )


    # =====================================================
    # VIDEO EXTENSION
    # =====================================================

    if not ext_ok(
        video_file.filename,
        ALLOWED_VIDEOS
    ):

        flash(
            "Video केवल MP4, WebM, MOV या MKV होनी चाहिए."
        )

        return redirect(
            url_for("admin")
        )


    # =====================================================
    # POSTER EXTENSION
    # =====================================================

    if (
        poster_file
        and poster_file.filename
        and not ext_ok(
            poster_file.filename,
            ALLOWED_POSTERS
        )
    ):

        flash(
            "Poster केवल JPG, JPEG, PNG या WEBP होना चाहिए."
        )

        return redirect(
            url_for("admin")
        )


    # =====================================================
    # SAFE VIDEO NAME
    # =====================================================

    original_video_name = secure_filename(
        video_file.filename
    )

    if not original_video_name:

        flash(
            "Video filename invalid है."
        )

        return redirect(
            url_for("admin")
        )

    video_base, video_ext = os.path.splitext(
        original_video_name
    )

    video_ext = video_ext.lower()

    random_name = secrets.token_hex(8)


    # =====================================================
    # MKV -> MP4
    # =====================================================

    converted_file = None

    if video_ext == ".mkv":

        try:

            video_name = (
                video_base
                + "_"
                + random_name
                + ".mp4"
            )

            video_key = (
                "videos/"
                + video_name
            )

            converted_file = tempfile.NamedTemporaryFile(
                suffix=".mp4",
                delete=False
            )

            converted_path = converted_file.name

            converted_file.close()

            flash(
                "MKV मिल गई. MP4 में conversion शुरू हो रही है..."
            )

            convert_mkv_to_mp4(
                video_file,
                converted_path
            )

            r2_upload_path(
                converted_path,
                video_key,
                "video/mp4"
            )

        except Exception as e:

            app.logger.exception(
                "MKV conversion/upload failed: %s",
                e
            )

            if converted_file:

                try:
                    os.remove(
                        converted_file.name
                    )
                except Exception:
                    pass

            flash(
                "MKV को MP4 में convert/upload नहीं किया जा सका. Render Logs देखें."
            )

            return redirect(
                url_for("admin")
            )

        finally:

            if converted_file:

                try:
                    os.remove(
                        converted_file.name
                    )
                except Exception:
                    pass

    else:

        # =================================================
        # NORMAL VIDEO
        # =================================================

        safe_video = (
            video_base
            + "_"
            + random_name
            + video_ext
        )

        video_key = (
            "videos/"
            + safe_video
        )

        try:

            r2_upload(
                video_file,
                video_key,
                get_content_type(
                    video_file.filename,
                    "application/octet-stream"
                )
            )

        except Exception as e:

            app.logger.exception(
                "R2 video upload failed: %s",
                e
            )

            flash(
                "R2 video upload failed. Render Logs देखें."
            )

            return redirect(
                url_for("admin")
            )


    # =====================================================
    # POSTER
    # =====================================================

    poster_key = ""

    if (
        poster_file
        and poster_file.filename
    ):

        safe_poster = secure_filename(
            poster_file.filename
        )

        if not safe_poster:

            try:
                r2_delete(video_key)
            except Exception:
                pass

            flash(
                "Poster filename invalid है."
            )

            return redirect(
                url_for("admin")
            )

        poster_base, poster_ext = os.path.splitext(
            safe_poster
        )

        poster_name = (
            poster_base
            + "_"
            + secrets.token_hex(8)
            + poster_ext.lower()
        )

        poster_key = (
            "posters/"
            + poster_name
        )

        try:

            r2_upload(
                poster_file,
                poster_key,
                get_content_type(
                    poster_file.filename,
                    "application/octet-stream"
                )
            )

        except Exception as e:

            app.logger.exception(
                "R2 poster upload failed: %s",
                e
            )

            try:
                r2_delete(video_key)
            except Exception:
                pass

            flash(
                "Poster upload failed."
            )

            return redirect(
                url_for("admin")
            )


    # =====================================================
    # DATABASE INSERT
    # =====================================================

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
            VALUES (%s, %s, %s, %s, %s, 0)
            RETURNING id
            """,
            (
                title,
                category,
                description,
                poster_key,
                video_key,
            )
        ).fetchone()

        con.commit()

        movie_id = row["id"]

        flash(
            f"Movie publish हो गई. ID: {movie_id}"
        )

    except Exception as e:

        if con:
            con.rollback()

        app.logger.exception(
            "Movie database insert failed: %s",
            e
        )

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
            "Movie database में save नहीं हो पाई."
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
        "Internal Server Error. "
        "Render Logs देखें.",
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
# LOCAL RUN
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
