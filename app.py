import os
import sqlite3
import secrets
from functools import wraps

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    send_from_directory,
    abort,
)

from werkzeug.utils import secure_filename


# =========================================================
# PATHS
# =========================================================

BASE = os.path.dirname(os.path.abspath(__file__))

DB = os.path.join(BASE, "movies.db")

UPLOAD_ROOT = os.path.join(BASE, "uploads")
POSTER_DIR = os.path.join(UPLOAD_ROOT, "posters")
VIDEO_DIR = os.path.join(UPLOAD_ROOT, "videos")


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
# CREATE UPLOAD DIRECTORIES
# =========================================================

os.makedirs(
    POSTER_DIR,
    exist_ok=True
)

os.makedirs(
    VIDEO_DIR,
    exist_ok=True
)


# =========================================================
# DATABASE CONNECTION
# =========================================================

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


# =========================================================
# DATABASE INITIALIZATION
# =========================================================

def init_db():

    con = db()

    # Movies table
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS movies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    # Settings table
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT ''
        )
        """
    )

    # Default ad settings
    default_settings = {
        "ad_top": "<div class='ad-slot'>YOUR TOP AD CODE</div>",
        "ad_player": "<div class='ad-slot'>YOUR PLAYER AD CODE</div>",
        "ad_bottom": "<div class='ad-slot'>YOUR BOTTOM AD CODE</div>",
    }

    for key, value in default_settings.items():

        con.execute(
            """
            INSERT OR IGNORE INTO settings(key, value)
            VALUES (?, ?)
            """,
            (key, value),
        )

    con.commit()
    con.close()


# =========================================================
# INITIALIZE DATABASE
# =========================================================

init_db()


# =========================================================
# FILE EXTENSION CHECK
# =========================================================

def ext_ok(name, allowed):

    return (
        "." in name
        and name.rsplit(".", 1)[1].lower() in allowed
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
# SETTINGS
# =========================================================

def settings():

    con = db()

    rows = con.execute(
        "SELECT key, value FROM settings"
    ).fetchall()

    con.close()

    return {
        row["key"]: row["value"]
        for row in rows
    }


# =========================================================
# GLOBAL TEMPLATE DATA
# =========================================================

@app.context_processor
def inject():

    return {
        "ads": settings()
    }


# =========================================================
# GOOGLE ADSENSE ADS.TXT
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
            "Content-Type": "text/plain; charset=utf-8"
        }
    )


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

    if q:

        movies = con.execute(
            """
            SELECT *
            FROM movies
            WHERE title LIKE ?
            ORDER BY id DESC
            """,
            (f"%{q}%",),
        ).fetchall()

    elif category:

        movies = con.execute(
            """
            SELECT *
            FROM movies
            WHERE category = ?
            ORDER BY id DESC
            """,
            (category,),
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
        WHERE category <> ''
        ORDER BY category
        """
    ).fetchall()

    con.close()

    return render_template(
        "index.html",
        movies=movies,
        categories=[
            c["category"]
            for c in cats
        ],
        q=q,
        category=category,
    )


# =========================================================
# MOVIE PAGE
# =========================================================

@app.route("/movie/<int:movie_id>")
def movie(movie_id):

    con = db()

    movie_data = con.execute(
        """
        SELECT *
        FROM movies
        WHERE id = ?
        """,
        (movie_id,),
    ).fetchone()

    if not movie_data:

        con.close()

        abort(404)

    # Increase views
    con.execute(
        """
        UPDATE movies
        SET views = views + 1
        WHERE id = ?
        """,
        (movie_id,),
    )

    con.commit()
    con.close()

    return render_template(
        "movie.html",
        m=movie_data,
    )


# =========================================================
# POSTER FILE
# =========================================================

@app.route("/poster/<path:name>")
def poster(name):

    return send_from_directory(
        POSTER_DIR,
        name
    )


# =========================================================
# VIDEO FILE
# =========================================================

@app.route("/video/<path:name>")
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
# ADMIN DASHBOARD
# =========================================================

@app.route("/admin")
@admin_required
def admin():

    con = db()

    movies = con.execute(
        """
        SELECT *
        FROM movies
        ORDER BY id DESC
        """
    ).fetchall()

    con.close()

    return render_template(
        "admin.html",
        movies=movies
    )


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
    # Required fields
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
    # Poster validation
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
            "Poster format गलत है."
        )

        return redirect(
            url_for("admin")
        )

    # -----------------------------------------------------
    # Video validation
    # -----------------------------------------------------

    if not ext_ok(
        video_file.filename,
        ALLOWED_VIDEOS
    ):

        flash(
            "Video केवल MP4/WebM/MOV रखें."
        )

        return redirect(
            url_for("admin")
        )

    # -----------------------------------------------------
    # Secure video filename
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

    # -----------------------------------------------------
    # Save video
    # -----------------------------------------------------

    video_path = os.path.join(
        VIDEO_DIR,
        safe_video
    )

    video_file.save(
        video_path
    )

    # -----------------------------------------------------
    # Save poster
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

            poster_path = os.path.join(
                POSTER_DIR,
                poster_name
            )

            poster_file.save(
                poster_path
            )

    # -----------------------------------------------------
    # Save movie in database
    # -----------------------------------------------------

    con = db()

    con.execute(
        """
        INSERT INTO movies
        (
            title,
            category,
            description,
            poster,
            video
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            title,
            category,
            description,
            poster_name,
            safe_video,
        ),
    )

    con.commit()
    con.close()

    flash(
        "Movie publish हो गई."
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

    movie_data = con.execute(
        """
        SELECT *
        FROM movies
        WHERE id = ?
        """,
        (movie_id,),
    ).fetchone()

    if movie_data:

        files_to_delete = [
            (
                VIDEO_DIR,
                movie_data["video"]
            ),
            (
                POSTER_DIR,
                movie_data["poster"]
            ),
        ]

        for directory, name in files_to_delete:

            if name:

                file_path = os.path.join(
                    directory,
                    name
                )

                if os.path.isfile(
                    file_path
                ):

                    os.remove(
                        file_path
                    )

        con.execute(
            """
            DELETE FROM movies
            WHERE id = ?
            """,
            (movie_id,),
        )

        con.commit()

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

    for key in (
        "ad_top",
        "ad_player",
        "ad_bottom"
    ):

        con.execute(
            """
            INSERT INTO settings(key, value)
            VALUES (?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value
            """,
            (
                key,
                request.form.get(
                    key,
                    ""
                ),
            ),
        )

    con.commit()
    con.close()

    flash(
        "Ads settings saved."
    )

    return redirect(
        url_for("admin")
    )


# =========================================================
# LOCAL DEVELOPMENT / RENDER
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
        debug=True,
    )
