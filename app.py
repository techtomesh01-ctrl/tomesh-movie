```python
import os
import secrets
from functools import wraps

import boto3
from botocore.exceptions import BotoCoreError, ClientError

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


def db():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL environment variable is missing."
        )

    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor
    )


# =========================================================
# CLOUDFLARE R2
# =========================================================

R2_ACCOUNT_ID = os.environ.get(
    "R2_ACCOUNT_ID",
    ""
)

R2_BUCKET = os.environ.get(
    "R2_BUCKET",
    "tomesh-movies"
)

R2_ENDPOINT = os.environ.get(
    "R2_ENDPOINT",
    ""
)

R2_ACCESS_KEY_ID = os.environ.get(
    "R2_ACCESS_KEY_ID",
    ""
)

R2_SECRET_ACCESS_KEY = os.environ.get(
    "R2_SECRET_ACCESS_KEY",
    ""
)

R2_PUBLIC_URL = os.environ.get(
    "R2_PUBLIC_URL",
    ""
).rstrip("/")


def r2_client():

    if not R2_ENDPOINT:
        raise RuntimeError(
            "R2_ENDPOINT environment variable is missing."
        )

    if not R2_ACCESS_KEY_ID:
        raise RuntimeError(
            "R2_ACCESS_KEY_ID environment variable is missing."
        )

    if not R2_SECRET_ACCESS_KEY:
        raise RuntimeError(
            "R2_SECRET_ACCESS_KEY environment variable is missing."
        )

    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


def r2_url(key):

    if not R2_PUBLIC_URL:
        raise RuntimeError(
            "R2_PUBLIC_URL environment variable is missing."
        )

    return f"{R2_PUBLIC_URL}/{key}"


def upload_to_r2(file_object, key, content_type):

    client = r2_client()

    file_object.stream.seek(0)

    client.upload_fileobj(
        file_object.stream,
        R2_BUCKET,
        key,
        ExtraArgs={
            "ContentType": content_type
        }
    )

    return r2_url(key)


def delete_from_r2(key):

    if not key:
        return

    try:

        client = r2_client()

        client.delete_object(
            Bucket=R2_BUCKET,
            Key=key
        )

    except Exception as e:

        app.logger.warning(
            "R2 delete failed: %s",
            e
        )


# =========================================================
# FILE TYPES
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

        # -------------------------------------------------
        # MOVIES TABLE
        # -------------------------------------------------

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

        # -------------------------------------------------
        # R2 URL COLUMNS
        # -------------------------------------------------

        con.execute(
            """
            ALTER TABLE movies
            ADD COLUMN IF NOT EXISTS poster_url TEXT DEFAULT ''
            """
        )

        con.execute(
            """
            ALTER TABLE movies
            ADD COLUMN IF NOT EXISTS video_url TEXT DEFAULT ''
            """
        )

        # -------------------------------------------------
        # SETTINGS
        # -------------------------------------------------

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT DEFAULT ''
            )
            """
        )

        # -------------------------------------------------
        # DEFAULT ADS
        # -------------------------------------------------

        for key in (
            "ad_top",
            "ad_player",
            "ad_bottom"
        ):

            con.execute(
                """
                INSERT INTO settings(key, value)
                VALUES (%s, %s)
                ON CONFLICT(key)
                DO NOTHING
                """,
                (
                    key,
                    ""
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

        return render_template(
            "movie.html",
            m=movie_data
        )

    finally:

        con.close()


# =========================================================
# OLD LOCAL FILE ROUTES
# =========================================================

# पुराने links टूटने से बचाने के लिए रखे गए हैं.
# नई movies R2 से चलेंगी.

@app.route("/poster/<path:name>")
def poster(name):
    abort(404)


@app.route("/video/<path:name>")
def video(name):
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

    try:

        con = db()

        con.execute(
            "SELECT 1"
        ).fetchone()

        con.close()

        return "OK", 200

    except Exception as e:

        app.logger.exception(
            "Health check failed: %s",
            e
        )

        return "Database error", 500


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

    # -----------------------------------------------------
    # REQUIRED
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # VIDEO TYPE
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # POSTER TYPE
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # SAFE FILE NAMES
    # -----------------------------------------------------

    safe_video = secure_filename(
        video_file.filename
    )

    if not safe_video:

        flash(
            "Video filename invalid है."
        )

        return redirect(
            url_for("admin")
        )

    video_base, video_ext = os.path.splitext(
        safe_video
    )

    video_key = (
        "videos/"
        + video_base
        + "_"
        + secrets.token_hex(8)
        + video_ext.lower()
    )

    poster_key = ""

    # -----------------------------------------------------
    # POSTER KEY
    # -----------------------------------------------------

    if (
        poster_file
        and poster_file.filename
    ):

        safe_poster = secure_filename(
            poster_file.filename
        )

        if not safe_poster:

            flash(
                "Poster filename invalid है."
            )

            return redirect(
                url_for("admin")
            )

        poster_base, poster_ext = os.path.splitext(
            safe_poster
        )

        poster_key = (
            "posters/"
            + poster_base
            + "_"
            + secrets.token_hex(8)
            + poster_ext.lower()
        )

    # -----------------------------------------------------
    # CONTENT TYPES
    # -----------------------------------------------------

    video_content_type = (
        video_file.mimetype
        or "video/mp4"
    )

    poster_content_type = (
        poster_file.mimetype
        if poster_file
        else None
    )

    uploaded_video = False
    uploaded_poster = False

    try:

        # -------------------------------------------------
        # UPLOAD VIDEO TO R2
        # -------------------------------------------------

        video_url = upload_to_r2(
            video_file,
            video_key,
            video_content_type
        )

        uploaded_video = True

        # -------------------------------------------------
        # UPLOAD POSTER TO R2
        # -------------------------------------------------

        poster_url = ""

        if poster_file and poster_key:

            poster_url = upload_to_r2(
                poster_file,
                poster_key,
                poster_content_type
            )

            uploaded_poster = True

        # -------------------------------------------------
        # DATABASE
        # -------------------------------------------------

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
                    poster_url,
                    video_url,
                    views
                )
                VALUES
                (
                    %s,
                    %s,
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
                    video_key,
                    poster_url,
                    video_url,
                )
            ).fetchone()

            con.commit()

            movie_id = row["id"]

        finally:

            con.close()

        flash(
            f"Movie publish हो गई. ID: {movie_id}"
        )

    except (
        BotoCoreError,
        ClientError,
        Exception
    ) as e:

        app.logger.exception(
            "Movie upload failed: %s",
            e
        )

        # -------------------------------------------------
        # ROLLBACK R2 FILES IF DATABASE FAILED
        # -------------------------------------------------

        if uploaded_video:

            delete_from_r2(
                video_key
            )

        if uploaded_poster:

            delete_from_r2(
                poster_key
            )

        flash(
            "Movie upload नहीं हो पाई. R2 या Database error."
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

        video_key = movie_data.get(
            "video"
        )

        poster_key = movie_data.get(
            "poster"
        )

        # -------------------------------------------------
        # DATABASE DELETE
        # -------------------------------------------------

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

    except Exception as e:

        con.rollback()

        app.logger.exception(
            "Delete movie database failed: %s",
            e
        )

        flash(
            "Movie delete नहीं हो पाई."
        )

        return redirect(
            url_for("admin")
        )

    finally:

        con.close()

    # -----------------------------------------------------
    # R2 DELETE
    # -----------------------------------------------------

    if video_key:

        delete_from_r2(
            video_key
        )

    if poster_key:

        delete_from_r2(
            poster_key
        )

    flash(
        "Movie delete हो गई."
    )

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
                INSERT INTO settings(key, value)
                VALUES (%s, %s)
                ON CONFLICT(key)
                DO UPDATE SET value = EXCLUDED.value
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
```
