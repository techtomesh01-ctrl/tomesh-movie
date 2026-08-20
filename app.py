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
        cursor_factory=RealDictCursor
    )


# =========================================================
# DATABASE INITIALIZATION
# =========================================================

def init_db():

    con = db()

    try:

        cur = con.cursor()

        # -------------------------------------------------
        # Movies
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
        # Settings
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
        # Default ads
        # -------------------------------------------------

        defaults = {
            "ad_top": "",
            "ad_player": "",
            "ad_bottom": "",
        }

        for key, value in defaults.items():

            cur.execute(
                """
                INSERT INTO settings(key, value)
                VALUES (%s, %s)
                ON CONFLICT(key) DO NOTHING
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
# SAFE DATABASE INIT
# =========================================================

try:

    init_db()

except Exception as e:

    print(
        "Database initialization error:",
        e
    )


# =========================================================
# SETTINGS
# =========================================================

def get_settings():

    con = db()

    try:

        cur = con.cursor()

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
# TEMPLATE DATA
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
# ADMIN CHECK
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

        cur = con.cursor()

        # -------------------------------------------------
        # Search
        # -------------------------------------------------

        if q:

            cur.execute(
                """
                SELECT *
                FROM movies
                WHERE title ILIKE %s
                   OR category ILIKE %s
                ORDER BY id DESC
                """,
                (
                    f"%{q}%",
                    f"%{q}%"
                )
            )

        # -------------------------------------------------
        # Category
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
        # All
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
        # Categories
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

        rows = cur.fetchall()

        categories = [
            row["category"]
            for row in rows
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

        cur = con.cursor()

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

        # -------------------------------------------------
        # Increase views
        # -------------------------------------------------

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

        # Refresh movie data
        movie_data["views"] = (
            movie_data["views"] + 1
        )

        return render_template(
            "movie.html",
            m=movie_data
        )

    finally:

        con.close()


# =========================================================
# POSTER ROUTE
# =========================================================

@app.route("/poster/<path:name>")
def poster(name):

    if not name:

        abort(404)

    # Only return a safe relative filename
    safe_name = os.path.basename(name)

    # Render can use external URL directly if you store one.
    if safe_name.startswith("http"):

        abort(404)

    # Local upload support.
    # NOTE: Render Free filesystem is temporary.
    return Response(
        "Poster file storage is not configured.",
        status=404
    )


# =========================================================
# VIDEO ROUTE
# =========================================================

@app.route("/video/<path:name>")
def video(name):

    if not name:

        abort(404)

    # -----------------------------------------------------
    # IMPORTANT
    # -----------------------------------------------------
    # For Render Free, local uploaded videos are NOT
    # permanent.
    #
    # This route is kept so old templates do not crash.
    #
    # If video contains a full external URL, movie.html
    # should use that URL directly.
    # -----------------------------------------------------

    return Response(
        "Video file is not available from permanent storage.",
        status=404
    )


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
            secrets.compare_digest(
                username,
                ADMIN_USER
            )
            and
            secrets.compare_digest(
                password,
                ADMIN_PASSWORD
            )
        ):

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

        cur = con.cursor()

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

    # -----------------------------------------------------
    # NEW SYSTEM
    # -----------------------------------------------------
    # Permanent video URL is preferred on Render.
    # -----------------------------------------------------

    video_url = request.form.get(
        "video_url",
        ""
    ).strip()

    poster_url = request.form.get(
        "poster_url",
        ""
    ).strip()

    # -----------------------------------------------------
    # Optional old file fields
    # -----------------------------------------------------

    poster_file = request.files.get(
        "poster"
    )

    video_file = request.files.get(
        "video"
    )

    # -----------------------------------------------------
    # Required
    # -----------------------------------------------------

    if not title:

        flash(
            "Movie title जरूरी है."
        )

        return redirect(
            url_for("admin")
        )

    # -----------------------------------------------------
    # Prefer URL
    # -----------------------------------------------------

    if not video_url:

        if not video_file or not video_file.filename:

            flash(
                "Video URL या video file देना जरूरी है."
            )

            return redirect(
                url_for("admin")
            )

    # -----------------------------------------------------
    # Poster URL
    # -----------------------------------------------------

    poster = poster_url

    # -----------------------------------------------------
    # If poster file was uploaded
    # -----------------------------------------------------

    if (
        not poster
        and poster_file
        and poster_file.filename
    ):

        # We cannot promise permanent storage on Render.
        flash(
            "Render पर permanent poster के लिए Poster URL इस्तेमाल करें."
        )

        return redirect(
            url_for("admin")
        )

    # -----------------------------------------------------
    # If video URL supplied
    # -----------------------------------------------------

    video = video_url

    # -----------------------------------------------------
    # Old file upload fallback
    # -----------------------------------------------------

    if not video:

        flash(
            "Render पर permanent movie के लिए Video URL इस्तेमाल करें."
        )

        return redirect(
            url_for("admin")
        )

    # -----------------------------------------------------
    # Save database
    # -----------------------------------------------------

    con = db()

    try:

        cur = con.cursor()

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
                poster,
                video
            )
        )

        row = cur.fetchone()

        con.commit()

        movie_id = row["id"]

        flash(
            f"Movie publish हो गई. ID: {movie_id}"
        )

    except Exception as e:

        con.rollback()

        app.logger.exception(
            "Movie insert failed: %s",
            e
        )

        flash(
            "Movie database में save नहीं हो पाई."
        )

    finally:

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

    con = db()

    try:

        cur = con.cursor()

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

    except Exception as e:

        con.rollback()

        app.logger.exception(
            "Delete failed: %s",
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

        cur = con.cursor()

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
                    value
                )
            )

        con.commit()

        flash(
            "Ads settings saved."
        )

    except Exception as e:

        con.rollback()

        app.logger.exception(
            "Ads save failed: %s",
            e
        )

        flash(
            "Ads settings save नहीं हुई."
        )

    finally:

        con.close()

    return redirect(
        url_for("admin")
    )


# =========================================================
# HEALTH CHECK
# =========================================================

@app.route("/health")
def health():

    try:

        con = db()

        cur = con.cursor()

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
# ERROR HANDLERS
# =========================================================

@app.errorhandler(404)
def not_found(error):

    return render_template(
        "base.html"
    ), 404


@app.errorhandler(413)
def too_large(error):

    flash(
        "File बहुत बड़ी है. Render पर Video URL इस्तेमाल करें."
    )

    return redirect(
        url_for("admin")
    )


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
