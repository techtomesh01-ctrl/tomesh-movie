import os
import secrets
from functools import wraps

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
    send_from_directory,
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

# Maximum upload size: 2 GB
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024


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
        cursor_factory=RealDictCursor,
        sslmode="require"
    )


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
}


# =========================================================
# DATABASE INITIALIZATION
# =========================================================

def init_db():

    con = db()

    try:

        with con.cursor() as cur:

            # -------------------------------------------------
            # MOVIES
            # -------------------------------------------------

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

            # -------------------------------------------------
            # SETTINGS
            # -------------------------------------------------

            cur.execute(
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

            default_settings = {
                "ad_top": "",
                "ad_player": "",
                "ad_bottom": "",
            }

            for key, value in default_settings.items():

                cur.execute(
                    """
                    INSERT INTO settings(key, value)
                    VALUES (%s, %s)
                    ON CONFLICT(key) DO NOTHING
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
# INITIALIZE DATABASE
# =========================================================

try:
    init_db()
except Exception as e:
    print("Database initialization error:", e)


# =========================================================
# FILE EXTENSION CHECK
# =========================================================

def ext_ok(name, allowed):

    if not name or "." not in name:
        return False

    return (
        name.rsplit(
            ".",
            1
        )[1].lower()
        in allowed
    )


# =========================================================
# ADMIN REQUIRED
# =========================================================

def admin_required(f):

    @wraps(f)
    def wrapper(*args, **kwargs):

        if not session.get("admin"):

            return redirect(
                url_for("login")
            )

        return f(
            *args,
            **kwargs
        )

    return wrapper


# =========================================================
# SETTINGS
# =========================================================

def get_settings():

    con = db()

    try:

        with con.cursor() as cur:

            cur.execute(
                """
                SELECT key, value
                FROM settings
                """
            )

            rows = cur.fetchall()

            return {
                row["key"]: row["value"]
                for row in rows
            }

    finally:

        con.close()


# =========================================================
# TEMPLATE GLOBAL DATA
# =========================================================

@app.context_processor
def inject_global_data():

    try:

        return {
            "ads": get_settings()
        }

    except Exception:

        return {
            "ads": {}
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

        with con.cursor() as cur:

            # -------------------------------------------------
            # SEARCH
            # -------------------------------------------------

            if q:

                cur.execute(
                    """
                    SELECT *
                    FROM movies
                    WHERE title ILIKE %s
                    ORDER BY id DESC
                    """,
                    (
                        f"%{q}%",
                    )
                )

            # -------------------------------------------------
            # CATEGORY
            # -------------------------------------------------

            elif category:

                cur.execute(
                    """
                    SELECT *
                    FROM movies
                    WHERE category = %s
                    ORDER BY id DESC
                    """,
                    (
                        category,
                    )
                )

            # -------------------------------------------------
            # ALL MOVIES
            # -------------------------------------------------

            else:

                cur.execute(
                    """
                    SELECT *
                    FROM movies
                    ORDER BY id DESC
                    """
                )

            movies = cur.fetchall()

            # -------------------------------------------------
            # CATEGORIES
            # -------------------------------------------------

            cur.execute(
                """
                SELECT DISTINCT category
                FROM movies
                WHERE category IS NOT NULL
                  AND category <> ''
                ORDER BY category
                """
            )

            cats = cur.fetchall()

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

@app.route(
    "/movie/<int:movie_id>"
)
def movie(movie_id):

    con = db()

    try:

        with con.cursor() as cur:

            cur.execute(
                """
                SELECT *
                FROM movies
                WHERE id = %s
                """,
                (
                    movie_id,
                )
            )

            movie_data = cur.fetchone()

            if not movie_data:

                abort(404)

            # Increase views
            cur.execute(
                """
                UPDATE movies
                SET views = views + 1
                WHERE id = %s
                """,
                (
                    movie_id,
                )
            )

        con.commit()

        return render_template(
            "movie.html",
            m=movie_data,
        )

    finally:

        con.close()


# =========================================================
# POSTER ROUTE
# =========================================================
# यह route index.html और movie.html दोनों के लिए जरूरी है.
# =========================================================

@app.route(
    "/poster/<path:name>"
)
def poster(name):

    return send_from_directory(
        POSTER_DIR,
        name
    )


# =========================================================
# VIDEO ROUTE
# =========================================================
# यह route movie.html के video player के लिए जरूरी है.
# =========================================================

@app.route(
    "/video/<path:name>"
)
def video(name):

    return send_from_directory(
        VIDEO_DIR,
        name,
        conditional=True
    )


# =========================================================
# LOGIN
# =========================================================

@app.route(
    "/login",
    methods=[
        "GET",
        "POST"
    ]
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

        username_ok = secrets.compare_digest(
            username,
            ADMIN_USER
        )

        password_ok = secrets.compare_digest(
            password,
            ADMIN_PASSWORD
        )

        if username_ok and password_ok:

            session.clear()

            session["admin"] = True

            return redirect(
                url_for("admin")
            )

        flash(
            "गलत username या password"
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
# ADMIN DASHBOARD
# =========================================================

@app.route("/admin")
@admin_required
def admin():

    con = db()

    try:

        with con.cursor() as cur:

            cur.execute(
                """
                SELECT *
                FROM movies
                ORDER BY id DESC
                """
            )

            movies = cur.fetchall()

        return render_template(
            "admin.html",
            movies=movies
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

    if (
        not title
        or not video_file
        or not video_file.filename
    ):

        flash(
            "Title और video जरूरी हैं."
        )

        return redirect(
            url_for("admin")
        )

    # -----------------------------------------------------
    # POSTER VALIDATION
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
    # VIDEO VALIDATION
    # -----------------------------------------------------

    if not ext_ok(
        video_file.filename,
        ALLOWED_VIDEOS
    ):

        flash(
            "Video केवल MP4, WebM या MOV होना चाहिए."
        )

        return redirect(
            url_for("admin")
        )

    # -----------------------------------------------------
    # VIDEO NAME
    # -----------------------------------------------------

    safe_video = secure_filename(
        video_file.filename
    )

    if not safe_video:

        flash(
            "Video filename गलत है."
        )

        return redirect(
            url_for("admin")
        )

    video_base = os.path.splitext(
        safe_video
    )[0]

    video_ext = os.path.splitext(
        safe_video
    )[1]

    safe_video = (
        video_base
        + "_"
        + secrets.token_hex(6)
        + video_ext
    )

    video_path = os.path.join(
        VIDEO_DIR,
        safe_video
    )

    # -----------------------------------------------------
    # SAVE VIDEO
    # -----------------------------------------------------

    try:

        video_file.save(
            video_path
        )

    except Exception as e:

        app.logger.exception(
            "Video upload failed: %s",
            e
        )

        flash(
            "Video upload नहीं हो पाई."
        )

        return redirect(
            url_for("admin")
        )

    # -----------------------------------------------------
    # POSTER
    # -----------------------------------------------------

    poster_name = ""

    if (
        poster_file
        and poster_file.filename
    ):

        poster_name = secure_filename(
            poster_file.filename
        )

        if poster_name:

            poster_base = os.path.splitext(
                poster_name
            )[0]

            poster_ext = os.path.splitext(
                poster_name
            )[1]

            poster_name = (
                poster_base
                + "_"
                + secrets.token_hex(6)
                + poster_ext
            )

            poster_path = os.path.join(
                POSTER_DIR,
                poster_name
            )

            try:

                poster_file.save(
                    poster_path
                )

            except Exception as e:

                app.logger.exception(
                    "Poster upload failed: %s",
                    e
                )

                if os.path.isfile(
                    video_path
                ):

                    os.remove(
                        video_path
                    )

                flash(
                    "Poster upload नहीं हो पाया."
                )

                return redirect(
                    url_for("admin")
                )

    # -----------------------------------------------------
    # DATABASE INSERT
    # -----------------------------------------------------

    con = db()

    try:

        with con.cursor() as cur:

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
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    title,
                    category,
                    description,
                    poster_name,
                    safe_video,
                )
            )

            movie_id = cur.fetchone()["id"]

        con.commit()

    except Exception as e:

        con.rollback()

        app.logger.exception(
            "Database insert failed: %s",
            e
        )

        if os.path.isfile(
            video_path
        ):

            os.remove(
                video_path
            )

        if poster_name:

            poster_path = os.path.join(
                POSTER_DIR,
                poster_name
            )

            if os.path.isfile(
                poster_path
            ):

                os.remove(
                    poster_path
                )

        flash(
            "Movie database में save नहीं हो पाई."
        )

        return redirect(
            url_for("admin")
        )

    finally:

        con.close()

    flash(
        f"Movie publish हो गई. ID: {movie_id}"
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

        with con.cursor() as cur:

            cur.execute(
                """
                SELECT *
                FROM movies
                WHERE id = %s
                """,
                (
                    movie_id,
                )
            )

            movie_data = cur.fetchone()

            if movie_data:

                # -------------------------------------------------
                # DELETE VIDEO
                # -------------------------------------------------

                video_name = movie_data["video"]

                if video_name:

                    video_path = os.path.join(
                        VIDEO_DIR,
                        video_name
                    )

                    if os.path.isfile(
                        video_path
                    ):

                        try:
                            os.remove(
                                video_path
                            )
                        except Exception as e:
                            app.logger.warning(
                                "Video delete failed: %s",
                                e
                            )

                # -------------------------------------------------
                # DELETE POSTER
                # -------------------------------------------------

                poster_name = movie_data["poster"]

                if poster_name:

                    poster_path = os.path.join(
                        POSTER_DIR,
                        poster_name
                    )

                    if os.path.isfile(
                        poster_path
                    ):

                        try:
                            os.remove(
                                poster_path
                            )
                        except Exception as e:
                            app.logger.warning(
                                "Poster delete failed: %s",
                                e
                            )

                # -------------------------------------------------
                # DELETE DATABASE RECORD
                # -------------------------------------------------

                cur.execute(
                    """
                    DELETE FROM movies
                    WHERE id = %s
                    """,
                    (
                        movie_id,
                    )
                )

                con.commit()

                flash(
                    "Movie delete हो गई."
                )

            else:

                flash(
                    "Movie नहीं मिली."
                )

    except Exception:

        con.rollback()

        raise

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

        with con.cursor() as cur:

            for key in (
                "ad_top",
                "ad_player",
                "ad_bottom"
            ):

                value = request.form.get(
                    key,
                    ""
                )

                cur.execute(
                    """
                    INSERT INTO settings(key, value)
                    VALUES (%s, %s)
                    ON CONFLICT(key)
                    DO UPDATE SET value = EXCLUDED.value
                    """,
                    (
                        key,
                        value,
                    )
                )

        con.commit()

        flash(
            "Ads settings saved."
        )

    except Exception:

        con.rollback()

        raise

    finally:

        con.close()

    return redirect(
        url_for("admin")
    )


# =========================================================
# ADS.TXT
# =========================================================

@app.route("/ads.txt")
def ads_txt():

    publisher_id = os.environ.get(
        "ADSENSE_PUBLISHER_ID",
        "pub-8697157365303435"
    )

    content = (
        "google.com, "
        + publisher_id
        + ", DIRECT, f08c47fec0942fa0\n"
    )

    return (
        content,
        200,
        {
            "Content-Type": "text/plain; charset=utf-8"
        }
    )


# =========================================================
# HEALTH CHECK
# =========================================================

@app.route("/health")
def health():

    try:

        con = db()

        with con.cursor() as cur:

            cur.execute(
                "SELECT 1"
            )

            cur.fetchone()

        con.close()

        return "OK", 200

    except Exception as e:

        app.logger.exception(
            "Health check failed: %s",
            e
        )

        return "Database error", 500


# =========================================================
# 404
# =========================================================

@app.errorhandler(404)
def not_found(error):

    return (
        render_template(
            "base.html"
        ),
        404
    )


# =========================================================
# 413
# =========================================================

@app.errorhandler(413)
def too_large(error):

    flash(
        "File बहुत बड़ी है. Maximum size 2 GB है."
    )

    return redirect(
        url_for("admin")
    )


# =========================================================
# RUN LOCAL
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
