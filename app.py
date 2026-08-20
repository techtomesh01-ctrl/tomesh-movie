```python
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
    cur = con.cursor()

    try:

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS movies (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                category TEXT DEFAULT '',
                description TEXT DEFAULT '',
                poster TEXT DEFAULT '',
                video TEXT DEFAULT '',
                poster_url TEXT DEFAULT '',
                video_url TEXT DEFAULT '',
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
                ON CONFLICT(key)
                DO NOTHING
                """,
                (key, value)
            )

        con.commit()

    except Exception:
        con.rollback()
        raise

    finally:
        cur.close()
        con.close()


# =========================================================
# INITIALIZE DATABASE ON RENDER / GUNICORN
# =========================================================

try:
    init_db()
except Exception as e:
    print("DATABASE INITIALIZATION ERROR:", e)


# =========================================================
# SETTINGS
# =========================================================

def get_settings():

    con = db()
    cur = con.cursor()

    try:

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
        cur.close()
        con.close()


# =========================================================
# GLOBAL TEMPLATE DATA
# =========================================================

@app.context_processor
def inject():

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
# ADS.TXT
# =========================================================

@app.route("/ads.txt")
def ads_txt():

    content = (
        "google.com, pub-8697157365303435, DIRECT, "
        "f08c47fec0942fa0\n"
    )

    return (
        content,
        200,
        {
            "Content-Type":
            "text/plain; charset=utf-8"
        }
    )


# =========================================================
# ADMIN LOGIN CHECK
# =========================================================

def admin_required(f):

    @wraps(f)
    def wrapper(*args, **kwargs):

        if not session.get("admin"):
            return redirect(
                url_for("login")
            )

        return f(*args, **kwargs)

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
    cur = con.cursor()

    try:

        if q:

            cur.execute(
                """
                SELECT *
                FROM movies
                WHERE title ILIKE %s
                ORDER BY id DESC
                """,
                (f"%{q}%",)
            )

        elif category:

            cur.execute(
                """
                SELECT *
                FROM movies
                WHERE category = %s
                ORDER BY id DESC
                """,
                (category,)
            )

        else:

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
            SELECT DISTINCT category
            FROM movies
            WHERE category <> ''
            ORDER BY category
            """
        )

        cats = cur.fetchall()

        categories = [
            c["category"]
            for c in cats
        ]

        return render_template(
            "index.html",
            movies=movies,
            categories=categories,
            q=q,
            category=category
        )

    finally:
        cur.close()
        con.close()


# =========================================================
# MOVIE PAGE
# =========================================================

@app.route("/movie/<int:movie_id>")
def movie(movie_id):

    con = db()
    cur = con.cursor()

    try:

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
            SET views = views + 1
            WHERE id = %s
            """,
            (movie_id,)
        )

        con.commit()

        return render_template(
            "movie.html",
            m=movie_data
        )

    finally:
        cur.close()
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
        )

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
# ADMIN
# =========================================================

@app.route("/admin")
@admin_required
def admin():

    con = db()
    cur = con.cursor()

    try:

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
        cur.close()
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

    poster_url = request.form.get(
        "poster_url",
        ""
    ).strip()

    video_url = request.form.get(
        "video_url",
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
        not video_url
        and (
            not video_file
            or not video_file.filename
        )
    ):

        flash(
            "Movie video file देना जरूरी है."
        )

        return redirect(
            url_for("admin")
        )

    poster_name = ""

    video_name = ""

    if (
        poster_file
        and poster_file.filename
    ):

        poster_name = secure_filename(
            poster_file.filename
        )

    if (
        video_file
        and video_file.filename
    ):

        video_name = secure_filename(
            video_file.filename
        )

    con = db()
    cur = con.cursor()

    try:

        cur.execute(
            """
            INSERT INTO movies
            (
                title,
                category,
                description,
                poster,
                video,
                poster_url,
                video_url
            )
            VALUES
            (
                %s,
                %s,
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
                poster_name,
                video_name,
                poster_url,
                video_url
            )
        )

        movie_id = cur.fetchone()["id"]

        con.commit()

        flash(
            f"Movie publish हो गई. ID: {movie_id}"
        )

    except Exception as e:

        con.rollback()

        print(
            "ADD MOVIE ERROR:",
            repr(e)
        )

        flash(
            "Movie publish नहीं हुई. Logs में error देखें."
        )

    finally:

        cur.close()
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
    cur = con.cursor()

    try:

        cur.execute(
            """
            DELETE FROM movies
            WHERE id = %s
            """,
            (movie_id,)
        )

        con.commit()

        flash(
            "Movie delete हो गई."
        )

    finally:

        cur.close()
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
    cur = con.cursor()

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

            cur.execute(
                """
                INSERT INTO settings(key, value)
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
            "Ads settings saved."
        )

    finally:

        cur.close()
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

        init_db()

        con = db()
        cur = con.cursor()

        cur.execute(
            "SELECT 1"
        )

        cur.fetchone()

        cur.close()
        con.close()

        return {
            "status": "ok",
            "database": "connected"
        }

    except Exception as e:

        return {
            "status": "error",
            "database": "not connected",
            "message": str(e)
        }, 500


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                5000
            )
        ),
        debug=False
    )
```
