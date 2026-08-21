import os
import secrets
from functools import wraps

import boto3
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

app.config["MAX_CONTENT_LENGTH"] = (
    2 * 1024 * 1024 * 1024
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

DATABASE_URL = os.environ.get("DATABASE_URL")


class DB:
    """
    psycopg2 connection wrapper.

    इससे पुराने code में con.execute(...)
    भी काम करेगा।
    """

    def __init__(self, connection):
        self.connection = connection
        self.cursor = connection.cursor(
            cursor_factory=RealDictCursor
        )

    def execute(self, *args, **kwargs):
        return self.cursor.execute(
            *args,
            **kwargs
        )

    def commit(self):
        return self.connection.commit()

    def rollback(self):
        return self.connection.rollback()

    def close(self):
        try:
            self.cursor.close()
        except Exception:
            pass

        try:
            self.connection.close()
        except Exception:
            pass


def db():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL environment variable is missing."
        )

    connection = psycopg2.connect(
        DATABASE_URL
    )

    return DB(connection)


# =========================================================
# CLOUDFLARE R2
# =========================================================

R2_ENDPOINT = os.environ.get(
    "R2_ENDPOINT",
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

R2_BUCKET_NAME = os.environ.get(
    "R2_BUCKET_NAME",
    ""
).strip()

R2_PUBLIC_URL = os.environ.get(
    "R2_PUBLIC_URL",
    ""
).strip().rstrip("/")


def r2_enabled():
    return all([
        R2_ENDPOINT,
        R2_ACCESS_KEY_ID,
        R2_SECRET_ACCESS_KEY,
        R2_BUCKET_NAME,
    ])


def get_r2():

    if not r2_enabled():
        raise RuntimeError(
            "R2 environment variables are missing."
        )

    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


def r2_upload(file_storage, folder):

    if not file_storage:
        return ""

    if not file_storage.filename:
        return ""

    filename = secure_filename(
        file_storage.filename
    )

    if not filename:
        raise RuntimeError(
            "Invalid filename."
        )

    base, ext = os.path.splitext(
        filename
    )

    key = (
        folder.rstrip("/")
        + "/"
        + base
        + "_"
        + secrets.token_hex(8)
        + ext.lower()
    )

    client = get_r2()

    file_storage.stream.seek(0)

    content_type = (
        file_storage.mimetype
        or "application/octet-stream"
    )

    client.upload_fileobj(
        file_storage.stream,
        R2_BUCKET_NAME,
        key,
        ExtraArgs={
            "ContentType": content_type
        }
    )

    return key


def r2_delete(key):

    if not key:
        return

    if not r2_enabled():
        return

    try:

        client = get_r2()

        client.delete_object(
            Bucket=R2_BUCKET_NAME,
            Key=key
        )

    except Exception as e:

        app.logger.exception(
            "R2 delete failed: %s",
            e
        )


def r2_url(key):

    if not key:
        return ""

    if not R2_PUBLIC_URL:
        return ""

    return (
        R2_PUBLIC_URL.rstrip("/")
        + "/"
        + key.lstrip("/")
    )


# =========================================================
# LOCAL UPLOAD DIRECTORIES
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
}


# =========================================================
# FILE EXTENSION CHECK
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
                INSERT INTO settings (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key)
                DO NOTHING
                """,
                (
                    key,
                    value,
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
                    f"%{q}%",
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

        # R2 URL add करें
        for movie_data in movies:

            movie_data["poster_url"] = r2_url(
                movie_data.get("poster", "")
            )

            movie_data["video_url"] = r2_url(
                movie_data.get("video", "")
            )

        return render_template(
            "index.html",
            movies=movies,
            categories=categories,
            q=q,
            category=category,
        )

    finally:

        con.close()


# =========================================================
# MOVIE PAGE
# =========================================================

@app.route(
    "/movie/<int:movie_id>"
)
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

        movie_data["poster_url"] = r2_url(
            movie_data.get("poster", "")
        )

        movie_data["video_url"] = r2_url(
            movie_data.get("video", "")
        )

        return render_template(
            "movie.html",
            m=movie_data
        )

    finally:

        con.close()


# =========================================================
# LEGACY LOCAL POSTER ROUTE
# =========================================================

@app.route(
    "/poster/<path:name>"
)
def poster(name):

    return redirect(
        r2_url(name)
        or url_for(
            "local_poster",
            name=name
        )
    )


@app.route(
    "/local-poster/<path:name>"
)
def local_poster(name):

    return send_local_file(
        POSTER_DIR,
        name
    )


# =========================================================
# LOCAL FILE HELPER
# =========================================================

def send_local_file(directory, name):

    from flask import send_from_directory

    return send_from_directory(
        directory,
        name
    )


# =========================================================
# LEGACY LOCAL VIDEO ROUTE
# =========================================================

@app.route(
    "/video/<path:name>"
)
def video(name):

    r2 = r2_url(name)

    if r2:
        return redirect(r2)

    return send_local_video(name)


@app.route(
    "/local-video/<path:name>"
)
def send_local_video(name):

    from flask import send_from_directory

    return send_from_directory(
        VIDEO_DIR,
        name,
        conditional=True
    )


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
            "Health check failed: %s",
            e
        )

        return (
            "Database error",
            500
        )

    finally:

        if con:
            con.close()


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

        for movie_data in movies:

            movie_data["poster_url"] = r2_url(
                movie_data.get("poster", "")
            )

            movie_data["video_url"] = r2_url(
                movie_data.get("video", "")
            )

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

    if not ext_ok(
        video_file.filename,
        ALLOWED_VIDEOS
    ):

        flash(
            "Video केवल MP4, WebM या MOV होनी चाहिए."
        )

        return redirect(
            url_for("admin")
        )

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

    uploaded_video = ""
    uploaded_poster = ""

    try:

        # =================================================
        # R2 UPLOAD
        # =================================================

        if not r2_enabled():

            flash(
                "R2 configuration missing है."
            )

            return redirect(
                url_for("admin")
            )

        uploaded_video = r2_upload(
            video_file,
            "videos"
        )

        if (
            poster_file
            and poster_file.filename
        ):

            uploaded_poster = r2_upload(
                poster_file,
                "posters"
            )

        # =================================================
        # DATABASE
        # =================================================

        con = db()

        try:

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
                    uploaded_poster,
                    uploaded_video,
                )
            ).fetchone()

            con.commit()

            movie_id = row["id"]

        finally:

            con.close()

        flash(
            f"Movie publish हो गई. ID: {movie_id}"
        )

    except Exception as e:

        app.logger.exception(
            "Movie upload failed: %s",
            e
        )

        if uploaded_video:
            r2_delete(
                uploaded_video
            )

        if uploaded_poster:
            r2_delete(
                uploaded_poster
            )

        flash(
            "Movie upload नहीं हो पाई."
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
def delete_movie(movie_id):

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
            r2_delete(
                video_name
            )

        if poster_name:
            r2_delete(
                poster_name
            )

        flash(
            "Movie delete हो गई."
        )

    except Exception as e:

        con.rollback()

        app.logger.exception(
            "Delete movie failed: %s",
            e
        )

        flash(
            "Movie delete नहीं हो पाई."
        )

    finally:

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

    con = db()

    try:

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
                VALUES (
                    %s,
                    %s
                )
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

        con.rollback()

        app.logger.exception(
            "Save ads failed: %s",
            e
        )

        flash(
            "Ads save नहीं हो पाईं."
        )

    finally:

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
        "File बहुत बड़ी है. Maximum upload size 2 GB है."
    )

    return redirect(
        url_for("admin")
    )


# =========================================================
# STARTUP
# =========================================================

try:

    init_db()

    app.logger.info(
        "Database initialization successful."
    )

except Exception as e:

    app.logger.exception(
        "Database initialization failed: %s",
        e
    )


# =========================================================
# START
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
