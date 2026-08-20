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

app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024


# =========================================================
# PATHS
# =========================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

UPLOAD_ROOT = os.path.join(
    BASE_DIR,
    "uploads"
)

POSTER_ROOT = os.path.join(
    UPLOAD_ROOT,
    "posters"
)

VIDEO_ROOT = os.path.join(
    UPLOAD_ROOT,
    "videos"
)


os.makedirs(POSTER_ROOT, exist_ok=True)
os.makedirs(VIDEO_ROOT, exist_ok=True)


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

    conn = db()

    try:

        cur = conn.cursor()

        # -------------------------------------------------
        # MOVIES
        # -------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS movies (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                category TEXT DEFAULT 'Other',
                description TEXT DEFAULT '',
                poster TEXT DEFAULT '',
                video TEXT DEFAULT '',
                poster_url TEXT DEFAULT '',
                video_url TEXT DEFAULT '',
                views INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # -------------------------------------------------
        # Existing database compatibility
        # -------------------------------------------------

        cur.execute("""
            ALTER TABLE movies
            ADD COLUMN IF NOT EXISTS poster_url TEXT DEFAULT ''
        """)

        cur.execute("""
            ALTER TABLE movies
            ADD COLUMN IF NOT EXISTS video_url TEXT DEFAULT ''
        """)

        # -------------------------------------------------
        # SETTINGS
        # -------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT DEFAULT ''
            )
        """)

        # -------------------------------------------------
        # DEFAULT ADS
        # -------------------------------------------------

        for key in (
            "ad_top",
            "ad_player",
            "ad_bottom",
        ):

            cur.execute("""
                INSERT INTO settings (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO NOTHING
            """, (
                key,
                "",
            ))

        conn.commit()

    finally:
        conn.close()


# =========================================================
# INITIALIZE DATABASE
# =========================================================

try:

    init_db()

    print("Database initialized successfully.")

except Exception as e:

    print(
        "Database initialization error:",
        e
    )


# =========================================================
# ADMIN DECORATOR
# =========================================================

def admin_required(func):

    @wraps(func)
    def wrapper(*args, **kwargs):

        if not session.get("admin"):

            return redirect(
                url_for("login")
            )

        return func(
            *args,
            **kwargs
        )

    return wrapper


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


def allowed_file(
    filename,
    extensions
):

    if not filename:
        return False

    if "." not in filename:
        return False

    extension = filename.rsplit(
        ".",
        1
    )[1].lower()

    return extension in extensions


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

    conn = db()

    try:

        cur = conn.cursor()

        # -------------------------------------------------
        # SEARCH + CATEGORY
        # -------------------------------------------------

        if q and category:

            cur.execute("""
                SELECT *
                FROM movies
                WHERE
                    (
                        title ILIKE %s
                        OR description ILIKE %s
                    )
                    AND category = %s
                ORDER BY created_at DESC
            """, (
                f"%{q}%",
                f"%{q}%",
                category,
            ))

        # -------------------------------------------------
        # SEARCH ONLY
        # -------------------------------------------------

        elif q:

            cur.execute("""
                SELECT *
                FROM movies
                WHERE
                    title ILIKE %s
                    OR description ILIKE %s
                ORDER BY created_at DESC
            """, (
                f"%{q}%",
                f"%{q}%",
            ))

        # -------------------------------------------------
        # CATEGORY ONLY
        # -------------------------------------------------

        elif category:

            cur.execute("""
                SELECT *
                FROM movies
                WHERE category = %s
                ORDER BY created_at DESC
            """, (
                category,
            ))

        # -------------------------------------------------
        # ALL
        # -------------------------------------------------

        else:

            cur.execute("""
                SELECT *
                FROM movies
                ORDER BY created_at DESC
            """)

        movies = cur.fetchall()

        # -------------------------------------------------
        # CATEGORIES
        # -------------------------------------------------

        cur.execute("""
            SELECT DISTINCT category
            FROM movies
            WHERE category IS NOT NULL
            AND category <> ''
            ORDER BY category
        """)

        categories = [
            row["category"]
            for row in cur.fetchall()
        ]

    finally:

        conn.close()

    return render_template(
        "index.html",
        movies=movies,
        categories=categories,
        q=q,
        category=category,
    )


# =========================================================
# MOVIE PAGE
# =========================================================

@app.route("/movie/<int:movie_id>")
def movie(movie_id):

    conn = db()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT *
            FROM movies
            WHERE id = %s
        """, (
            movie_id,
        ))

        m = cur.fetchone()

        if not m:

            abort(404)

        # -------------------------------------------------
        # VIEW COUNT
        # -------------------------------------------------

        cur.execute("""
            UPDATE movies
            SET views = COALESCE(views, 0) + 1
            WHERE id = %s
        """, (
            movie_id,
        ))

        conn.commit()

        m["views"] = (
            m.get("views") or 0
        ) + 1

        # -------------------------------------------------
        # ADS
        # -------------------------------------------------

        cur.execute("""
            SELECT key, value
            FROM settings
            WHERE key IN (
                'ad_top',
                'ad_player',
                'ad_bottom'
            )
        """)

        ads = {}

        for row in cur.fetchall():

            ads[row["key"]] = (
                row["value"] or ""
            )

    finally:

        conn.close()

    return render_template(
        "movie.html",
        m=m,
        ads=ads,
    )


# =========================================================
# POSTER ROUTE
# =========================================================

@app.route("/poster/<path:name>")
def poster(name):

    if not name:

        abort(404)

    # -----------------------------------------------------
    # External URL
    # -----------------------------------------------------

    if (
        name.startswith("http://")
        or name.startswith("https://")
    ):

        return redirect(name)

    # -----------------------------------------------------
    # Local file
    # -----------------------------------------------------

    filename = secure_filename(name)

    if not filename:

        abort(404)

    file_path = os.path.join(
        POSTER_ROOT,
        filename
    )

    if not os.path.isfile(file_path):

        abort(404)

    return send_from_directory(
        POSTER_ROOT,
        filename
    )


# =========================================================
# VIDEO ROUTE
# =========================================================

@app.route("/video/<path:name>")
def video(name):

    if not name:

        abort(404)

    # -----------------------------------------------------
    # External URL
    # -----------------------------------------------------

    if (
        name.startswith("http://")
        or name.startswith("https://")
    ):

        return redirect(name)

    # -----------------------------------------------------
    # Local file
    # -----------------------------------------------------

    filename = secure_filename(name)

    if not filename:

        abort(404)

    file_path = os.path.join(
        VIDEO_ROOT,
        filename
    )

    if not os.path.isfile(file_path):

        abort(404)

    return send_from_directory(
        VIDEO_ROOT,
        filename
    )


# =========================================================
# LOGIN
# =========================================================

@app.route(
    "/login",
    methods=["GET", "POST"]
)
def login():

    if session.get("admin"):

        return redirect(
            url_for("admin")
        )

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
            username == ADMIN_USER
            and password == ADMIN_PASSWORD
        ):

            session["admin"] = True

            flash(
                "Login successful."
            )

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

    flash(
        "Logout successful."
    )

    return redirect(
        url_for("home")
    )


# =========================================================
# ADMIN PANEL
# =========================================================

@app.route("/admin")
@admin_required
def admin():

    conn = db()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT *
            FROM movies
            ORDER BY created_at DESC
        """)

        movies = cur.fetchall()

        cur.execute("""
            SELECT key, value
            FROM settings
            WHERE key IN (
                'ad_top',
                'ad_player',
                'ad_bottom'
            )
        """)

        ads = {}

        for row in cur.fetchall():

            ads[row["key"]] = (
                row["value"] or ""
            )

    finally:

        conn.close()

    return render_template(
        "admin.html",
        movies=movies,
        ads=ads,
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
        "Other"
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

    # -----------------------------------------------------
    # TITLE
    # -----------------------------------------------------

    if not title:

        flash(
            "Movie title जरूरी है."
        )

        return redirect(
            url_for("admin")
        )

    # -----------------------------------------------------
    # POSTER
    # -----------------------------------------------------

    poster = ""

    if poster_url:

        poster = poster_url

    elif (
        poster_file
        and poster_file.filename
    ):

        if not allowed_file(
            poster_file.filename,
            ALLOWED_POSTERS
        ):

            flash(
                "Poster केवल JPG, JPEG, PNG या WEBP होना चाहिए."
            )

            return redirect(
                url_for("admin")
            )

        filename = secure_filename(
            poster_file.filename
        )

        poster_file.save(
            os.path.join(
                POSTER_ROOT,
                filename
            )
        )

        poster = filename

    # -----------------------------------------------------
    # VIDEO
    # -----------------------------------------------------

    video = ""

    if video_url:

        video = video_url

    elif (
        video_file
        and video_file.filename
    ):

        if not allowed_file(
            video_file.filename,
            ALLOWED_VIDEOS
        ):

            flash(
                "Video केवल MP4, WebM या MOV होना चाहिए."
            )

            return redirect(
                url_for("admin")
            )

        filename = secure_filename(
            video_file.filename
        )

        video_file.save(
            os.path.join(
                VIDEO_ROOT,
                filename
            )
        )

        video = filename

    # -----------------------------------------------------
    # VIDEO REQUIRED
    # -----------------------------------------------------

    if not video:

        flash(
            "Video URL या Video file देना जरूरी है."
        )

        return redirect(
            url_for("admin")
        )

    # -----------------------------------------------------
    # DATABASE
    # -----------------------------------------------------

    conn = db()

    try:

        cur = conn.cursor()

        cur.execute("""
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
        """, (
            title,
            category or "Other",
            description,
            poster,
            video,
            poster_url,
            video_url,
        ))

        movie_id = cur.fetchone()["id"]

        conn.commit()

    finally:

        conn.close()

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
    methods=["GET", "POST"]
)
@admin_required
def delete_movie(movie_id):

    conn = db()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT poster, video
            FROM movies
            WHERE id = %s
        """, (
            movie_id,
        ))

        movie_data = cur.fetchone()

        if not movie_data:

            flash(
                "Movie नहीं मिली."
            )

            return redirect(
                url_for("admin")
            )

        cur.execute("""
            DELETE FROM movies
            WHERE id = %s
        """, (
            movie_id,
        ))

        conn.commit()

    finally:

        conn.close()

    # -----------------------------------------------------
    # DELETE LOCAL POSTER
    # -----------------------------------------------------

    poster_name = (
        movie_data.get("poster")
        or ""
    )

    if poster_name and not (
        poster_name.startswith("http://")
        or poster_name.startswith("https://")
    ):

        filename = secure_filename(
            poster_name
        )

        path = os.path.join(
            POSTER_ROOT,
            filename
        )

        try:

            if os.path.isfile(path):

                os.remove(path)

        except Exception as e:

            print(
                "Poster delete error:",
                e
            )

    # -----------------------------------------------------
    # DELETE LOCAL VIDEO
    # -----------------------------------------------------

    video_name = (
        movie_data.get("video")
        or ""
    )

    if video_name and not (
        video_name.startswith("http://")
        or video_name.startswith("https://")
    ):

        filename = secure_filename(
            video_name
        )

        path = os.path.join(
            VIDEO_ROOT,
            filename
        )

        try:

            if os.path.isfile(path):

                os.remove(path)

        except Exception as e:

            print(
                "Video delete error:",
                e
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

    conn = db()

    try:

        cur = conn.cursor()

        ads = {
            "ad_top": ad_top,
            "ad_player": ad_player,
            "ad_bottom": ad_bottom,
        }

        for key, value in ads.items():

            cur.execute("""
                INSERT INTO settings
                (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key)
                DO UPDATE SET
                    value = EXCLUDED.value
            """, (
                key,
                value,
            ))

        conn.commit()

    finally:

        conn.close()

    flash(
        "Ads settings save हो गई."
    )

    return redirect(
        url_for("admin")
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
# HEALTH CHECK
# =========================================================

@app.route("/health")
def health():

    try:

        conn = db()

        cur = conn.cursor()

        cur.execute(
            "SELECT 1"
        )

        cur.fetchone()

        conn.close()

        return "OK - Database Connected", 200

    except Exception as e:

        print(
            "Health check error:",
            e
        )

        return (
            "Database Error: "
            + str(e),
            500
        )


# =========================================================
# 404
# =========================================================

@app.errorhandler(404)
def not_found(error):

    return (
        "Page not found",
        404
    )


# =========================================================
# 413
# =========================================================

@app.errorhandler(413)
def too_large(error):

    flash(
        "File बहुत बड़ी है. Maximum size 2GB है."
    )

    return redirect(
        url_for("admin")
    )


# =========================================================
# 500
# =========================================================

@app.errorhandler(500)
def internal_error(error):

    print(
        "INTERNAL SERVER ERROR:",
        error
    )

    return (
        "Internal Server Error",
        500
    )


# =========================================================
# START LOCAL SERVER
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
