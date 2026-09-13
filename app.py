import os
import secrets
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
    jsonify,
    Response,
)

from werkzeug.utils import secure_filename


# ============================================================
# APP
# ============================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "change-this-secret-key"
)

# Browser -> R2 direct upload is used.
# This limit mainly protects normal Flask requests.
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024


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

MAX_FILE_SIZE = 4 * 1024 * 1024 * 1024

# R2 multipart settings
PART_SIZE = 10 * 1024 * 1024
PARALLEL_PARTS = 3
PRESIGNED_EXPIRES = 3600
MAX_MULTIPART_PARTS = 10000


# ============================================================
# BASIC HELPERS
# ============================================================

def get_extension(filename):
    return os.path.splitext(filename or "")[1].lower().lstrip(".")


def clean_filename(filename):
    filename = secure_filename(filename or "")
    return filename or "file"


def is_allowed_file(filename, kind):
    ext = get_extension(filename)

    if kind == "video":
        return ext in ALLOWED_VIDEOS

    if kind == "poster":
        return ext in ALLOWED_POSTERS

    return False


def is_valid_r2_key(key):
    if not isinstance(key, str):
        return False

    if ".." in key:
        return False

    if key.startswith("videos/"):
        return True

    if key.startswith("posters/"):
        return True

    return False


# ============================================================
# ADMIN LOGIN
# ============================================================

def admin_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):

        if not session.get("admin_logged_in"):

            if request.path.startswith("/api/"):
                return jsonify({
                    "ok": False,
                    "error": "Admin login required"
                }), 401

            return redirect(
                url_for(
                    "login",
                    next=request.path
                )
            )

        return function(*args, **kwargs)

    return wrapper


# ============================================================
# DATABASE
# ============================================================

class Database:

    def connect(self):

        database_url = os.environ.get("DATABASE_URL")

        if not database_url:
            raise RuntimeError(
                "DATABASE_URL environment variable is missing"
            )

        return psycopg2.connect(
            database_url,
            sslmode="require"
        )

    def fetchall(self, sql, params=()):

        with self.connect() as connection:

            with connection.cursor(
                cursor_factory=RealDictCursor
            ) as cursor:

                cursor.execute(sql, params)

                return cursor.fetchall()

    def fetchone(self, sql, params=()):

        with self.connect() as connection:

            with connection.cursor(
                cursor_factory=RealDictCursor
            ) as cursor:

                cursor.execute(sql, params)

                return cursor.fetchone()

    def execute(self, sql, params=()):

        with self.connect() as connection:

            with connection.cursor() as cursor:

                cursor.execute(sql, params)


db = Database()


# ============================================================
# DATABASE INIT
# ============================================================

def init_db():

    db.execute(
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

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT ''
        )
        """
    )


# ============================================================
# CLOUDFLARE R2
# ============================================================

def get_r2_client():

    endpoint = os.environ.get(
        "R2_ENDPOINT",
        ""
    ).rstrip("/")

    if not endpoint:
        raise RuntimeError(
            "R2_ENDPOINT environment variable is missing"
        )

    access_key = os.environ.get(
        "R2_ACCESS_KEY_ID"
    )

    secret_key = os.environ.get(
        "R2_SECRET_ACCESS_KEY"
    )

    if not access_key:
        raise RuntimeError(
            "R2_ACCESS_KEY_ID environment variable is missing"
        )

    if not secret_key:
        raise RuntimeError(
            "R2_SECRET_ACCESS_KEY environment variable is missing"
        )

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            s3={
                "addressing_style": "path"
            }
        ),
    )


def get_r2_bucket():

    return os.environ.get(
        "R2_BUCKET",
        "tomesh-movies"
    )


def get_public_url(key):

    public_base = os.environ.get(
        "R2_PUBLIC_URL",
        ""
    ).rstrip("/")

    if public_base:

        return (
            public_base
            + "/"
            + quote(key, safe="/")
        )

    return get_r2_client().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": get_r2_bucket(),
            "Key": key,
        },
        ExpiresIn=3600,
    )


def delete_r2_object(key):

    if not is_valid_r2_key(key):
        return

    client = get_r2_client()

    client.delete_object(
        Bucket=get_r2_bucket(),
        Key=key,
    )


# ============================================================
# GLOBAL SETTINGS
# ============================================================

@app.context_processor
def inject_global_settings():

    try:

        rows = db.fetchall(
            """
            SELECT key, value
            FROM settings
            """
        )

        settings = {
            row["key"]: row["value"]
            for row in rows
        }

    except Exception:

        settings = {}

    return {
        "site_settings": settings
    }


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    movies = db.fetchall(
        """
        SELECT *
        FROM movies
        ORDER BY id DESC
        """
    )

    return render_template(
        "index.html",
        movies=movies
    )


# ============================================================
# MOVIE PAGE
# ============================================================

@app.route("/movie/<int:movie_id>")
def movie(movie_id):

    movie_data = db.fetchone(
        """
        SELECT *
        FROM movies
        WHERE id=%s
        """,
        (movie_id,)
    )

    if not movie_data:
        abort(404)

    db.execute(
        """
        UPDATE movies
        SET views = views + 1
        WHERE id=%s
        """,
        (movie_id,)
    )

    movie_data["views"] = (
        movie_data.get("views", 0) + 1
    )

    return render_template(
        "movie.html",
        movie=movie_data
    )


# ============================================================
# POSTER ROUTE
# ============================================================

@app.route("/poster/<path:name>")
def poster(name):

    key = "posters/" + name

    if not is_valid_r2_key(key):
        abort(404)

    try:

        client = get_r2_client()

        result = client.get_object(
            Bucket=get_r2_bucket(),
            Key=key
        )

        content_type = result.get(
            "ContentType",
            "image/jpeg"
        )

        return Response(
            result["Body"].read(),
            mimetype=content_type
        )

    except Exception:

        abort(404)


# ============================================================
# VIDEO ROUTE
# ============================================================

@app.route("/video/<path:name>")
def video(name):

    key = "videos/" + name

    if not is_valid_r2_key(key):
        abort(404)

    return redirect(
        get_public_url(key)
    )


# ============================================================
# ADS.TXT
# ============================================================

@app.route("/ads.txt")
def ads_txt():

    publisher = os.environ.get(
        "ADSENSE_PUBLISHER_ID",
        "pub-8697157365303435"
    )

    content = (
        "google.com, "
        + publisher
        + ", DIRECT, f08c47fec0942fa0\n"
    )

    return Response(
        content,
        mimetype="text/plain"
    )


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    try:

        db.fetchone("SELECT 1")

        return "OK", 200

    except Exception as error:

        return (
            "DB ERROR: "
            + str(error),
            500
        )


# ============================================================
# R2 HEALTH
# ============================================================

@app.route("/r2-health")
def r2_health():

    try:

        client = get_r2_client()

        client.head_bucket(
            Bucket=get_r2_bucket()
        )

        return "R2 OK", 200

    except Exception as error:

        return (
            "R2 ERROR: "
            + str(error),
            500
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
        )

        password = request.form.get(
            "password",
            ""
        )

        admin_user = os.environ.get(
            "ADMIN_USER",
            "admin"
        )

        admin_password = os.environ.get(
            "ADMIN_PASSWORD",
            "change-me-now"
        )

        if (
            username == admin_user
            and password == admin_password
        ):

            session["admin_logged_in"] = True

            next_url = request.args.get(
                "next"
            )

            return redirect(
                next_url or url_for("admin")
            )

        flash(
            "Invalid username or password",
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

    movies = db.fetchall(
        """
        SELECT *
        FROM movies
        ORDER BY id DESC
        """
    )

    settings_rows = db.fetchall(
        """
        SELECT key, value
        FROM settings
        """
    )

    settings = {
        row["key"]: row["value"]
        for row in settings_rows
    }

    return render_template(
        "admin.html",
        movies=movies,
        settings=settings
    )


# ============================================================
# R2 MULTIPART CREATE
# ============================================================

@app.post("/api/r2/multipart/create")
@admin_required
def create_multipart():

    data = request.get_json(
        silent=True
    ) or {}

    filename = clean_filename(
        data.get("filename")
    )

    kind = data.get("kind")

    try:
        size = int(
            data.get("size") or 0
        )
    except Exception:
        size = 0

    content_type = (
        data.get("content_type")
        or "application/octet-stream"
    )

    # --------------------------------------------------------
    # Validate kind
    # --------------------------------------------------------

    if kind not in {
        "video",
        "poster"
    }:

        return jsonify({
            "ok": False,
            "error": "Invalid upload type"
        }), 400

    # --------------------------------------------------------
    # Validate size
    # --------------------------------------------------------

    if size <= 0:

        return jsonify({
            "ok": False,
            "error": "File is empty"
        }), 400

    if size > MAX_FILE_SIZE:

        return jsonify({
            "ok": False,
            "error": "Maximum file size is 4 GB"
        }), 400

    # --------------------------------------------------------
    # Validate extension
    # --------------------------------------------------------

    if not is_allowed_file(
        filename,
        kind
    ):

        if kind == "video":

            error = (
                "Video केवल MP4, MKV, "
                "WebM या MOV होनी चाहिए."
            )

        else:

            error = (
                "Poster केवल JPG, JPEG, "
                "PNG या WEBP होना चाहिए."
            )

        return jsonify({
            "ok": False,
            "error": error
        }), 400

    # --------------------------------------------------------
    # Object key
    # --------------------------------------------------------

    prefix = (
        "videos"
        if kind == "video"
        else "posters"
    )

    token = secrets.token_hex(12)

    key = (
        prefix
        + "/"
        + token
        + "-"
        + filename
    )

    # --------------------------------------------------------
    # Create R2 multipart
    # --------------------------------------------------------

    try:

        client = get_r2_client()

        result = client.create_multipart_upload(
            Bucket=get_r2_bucket(),
            Key=key,
            ContentType=content_type,
        )

        upload_id = result["UploadId"]

        return jsonify({
            "ok": True,
            "upload_id": upload_id,
            "key": key,
            "part_size": PART_SIZE,
            "parallel": PARALLEL_PARTS,
            "expires": PRESIGNED_EXPIRES,
        })

    except Exception as error:

        return jsonify({
            "ok": False,
            "error": (
                "R2 multipart create failed: "
                + str(error)
            )
        }), 500


# ============================================================
# R2 MULTIPART PRESIGNED URLS
# ============================================================

@app.post("/api/r2/multipart/urls")
@admin_required
def multipart_urls():

    data = request.get_json(
        silent=True
    ) or {}

    upload_id = data.get(
        "upload_id"
    )

    key = data.get(
        "key"
    )

    parts = data.get(
        "parts"
    ) or []

    if not upload_id:

        return jsonify({
            "ok": False,
            "error": "Upload ID missing"
        }), 400

    if not is_valid_r2_key(key):

        return jsonify({
            "ok": False,
            "error": "Invalid R2 key"
        }), 400

    # --------------------------------------------------------
    # Convert part numbers safely
    # --------------------------------------------------------

    try:

        part_numbers = sorted(
            {
                int(number)
                for number in parts
            }
        )

    except Exception:

        return jsonify({
            "ok": False,
            "error": "Invalid part numbers"
        }), 400

    if not part_numbers:

        return jsonify({
            "ok": False,
            "error": "No parts requested"
        }), 400

    if len(part_numbers) > MAX_MULTIPART_PARTS:

        return jsonify({
            "ok": False,
            "error": "Too many parts"
        }), 400

    if (
        part_numbers[0] < 1
        or part_numbers[-1] > MAX_MULTIPART_PARTS
    ):

        return jsonify({
            "ok": False,
            "error": "Invalid part range"
        }), 400

    # --------------------------------------------------------
    # Generate presigned URLs
    # --------------------------------------------------------

    try:

        client = get_r2_client()

        urls = []

        for part_number in part_numbers:

            signed_url = client.generate_presigned_url(
                "upload_part",
                Params={
                    "Bucket": get_r2_bucket(),
                    "Key": key,
                    "UploadId": upload_id,
                    "PartNumber": part_number,
                },
                ExpiresIn=PRESIGNED_EXPIRES,
            )

            urls.append({
                "part_number": part_number,
                "url": signed_url,
            })

        return jsonify({
            "ok": True,
            "urls": urls,
        })

    except Exception as error:

        return jsonify({
            "ok": False,
            "error": (
                "R2 presigned URL generation failed: "
                + str(error)
            )
        }), 500


# ============================================================
# R2 MULTIPART COMPLETE
# ============================================================

@app.post("/api/r2/multipart/complete")
@admin_required
def complete_multipart():

    data = request.get_json(
        silent=True
    ) or {}

    upload_id = data.get(
        "upload_id"
    )

    key = data.get(
        "key"
    )

    try:
        expected_size = int(
            data.get("expected_size") or 0
        )
    except Exception:
        expected_size = 0

    if not upload_id:

        return jsonify({
            "ok": False,
            "error": "Upload ID missing"
        }), 400

    if not is_valid_r2_key(key):

        return jsonify({
            "ok": False,
            "error": "Invalid R2 key"
        }), 400

    if expected_size <= 0:

        return jsonify({
            "ok": False,
            "error": "Expected file size missing"
        }), 400

    client = get_r2_client()

    try:

        # ----------------------------------------------------
        # Read all uploaded parts from R2
        # ----------------------------------------------------

        uploaded_parts = []

        marker = None

        while True:

            params = {
                "Bucket": get_r2_bucket(),
                "Key": key,
                "UploadId": upload_id,
                "MaxParts": 1000,
            }

            if marker is not None:

                params[
                    "PartNumberMarker"
                ] = marker

            page = client.list_parts(
                **params
            )

            page_parts = page.get(
                "Parts",
                []
            )

            uploaded_parts.extend(
                page_parts
            )

            if not page.get(
                "IsTruncated"
            ):
                break

            marker = page.get(
                "NextPartNumberMarker"
            )

            if not marker:
                break

        # ----------------------------------------------------
        # No parts
        # ----------------------------------------------------

        if not uploaded_parts:

            raise RuntimeError(
                "R2 में कोई uploaded part नहीं मिला."
            )

        # ----------------------------------------------------
        # Sort
        # ----------------------------------------------------

        uploaded_parts.sort(
            key=lambda item: item["PartNumber"]
        )

        # ----------------------------------------------------
        # Verify sequential parts
        # ----------------------------------------------------

        actual_numbers = [
            int(item["PartNumber"])
            for item in uploaded_parts
        ]

        expected_numbers = list(
            range(
                1,
                len(uploaded_parts) + 1
            )
        )

        if actual_numbers != expected_numbers:

            raise RuntimeError(
                "Multipart upload incomplete है. "
                "कुछ parts missing हैं."
            )

        # ----------------------------------------------------
        # Verify total size
        # ----------------------------------------------------

        total_size = sum(
            int(item.get("Size", 0))
            for item in uploaded_parts
        )

        if total_size != expected_size:

            raise RuntimeError(
                "Uploaded size mismatch: "
                + str(total_size)
                + " / "
                + str(expected_size)
            )

        # ----------------------------------------------------
        # Build complete list using REAL R2 ETags
        # ----------------------------------------------------

        complete_parts = []

        for item in uploaded_parts:

            complete_parts.append({
                "PartNumber": int(
                    item["PartNumber"]
                ),
                "ETag": item["ETag"],
            })

        # ----------------------------------------------------
        # Complete multipart
        # ----------------------------------------------------

        client.complete_multipart_upload(
            Bucket=get_r2_bucket(),
            Key=key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": complete_parts
            },
        )

        # ----------------------------------------------------
        # Final verification
        # ----------------------------------------------------

        head = client.head_object(
            Bucket=get_r2_bucket(),
            Key=key
        )

        final_size = int(
            head.get(
                "ContentLength",
                0
            )
        )

        if final_size != expected_size:

            raise RuntimeError(
                "Final R2 size mismatch: "
                + str(final_size)
                + " / "
                + str(expected_size)
            )

        return jsonify({
            "ok": True,
            "key": key,
            "url": get_public_url(key),
            "size": final_size,
            "parts": len(complete_parts),
        })

    except Exception as error:

        return jsonify({
            "ok": False,
            "error": str(error)
        }), 500


# ============================================================
# R2 MULTIPART ABORT
# ============================================================

@app.post("/api/r2/multipart/abort")
@admin_required
def abort_multipart():

    data = request.get_json(
        silent=True
    ) or {}

    upload_id = data.get(
        "upload_id"
    )

    key = data.get(
        "key"
    )

    if not upload_id:

        return jsonify({
            "ok": False,
            "error": "Upload ID missing"
        }), 400

    if not is_valid_r2_key(key):

        return jsonify({
            "ok": False,
            "error": "Invalid R2 key"
        }), 400

    try:

        client = get_r2_client()

        client.abort_multipart_upload(
            Bucket=get_r2_bucket(),
            Key=key,
            UploadId=upload_id,
        )

        return jsonify({
            "ok": True
        })

    except Exception as error:

        return jsonify({
            "ok": False,
            "error": str(error)
        }), 500


# ============================================================
# R2 OBJECT DELETE
# ============================================================

@app.post("/api/r2/object/delete")
@admin_required
def delete_r2_object_api():

    data = request.get_json(
        silent=True
    ) or {}

    key = data.get(
        "key"
    )

    if not is_valid_r2_key(key):

        return jsonify({
            "ok": False,
            "error": "Invalid R2 key"
        }), 400

    try:

        delete_r2_object(key)

        return jsonify({
            "ok": True
        })

    except Exception as error:

        return jsonify({
            "ok": False,
            "error": str(error)
        }), 500


# ============================================================
# SAVE MOVIE
# ============================================================

@app.post("/api/movie/save")
@admin_required
def save_movie():

    data = request.get_json(
        silent=True
    ) or {}

    title = (
        data.get("title")
        or ""
    ).strip()

    category = (
        data.get("category")
        or ""
    ).strip()

    description = (
        data.get("description")
        or ""
    ).strip()

    video_key = data.get(
        "video"
    )

    poster_key = (
        data.get("poster")
        or ""
    )

    # --------------------------------------------------------
    # Video validation
    # --------------------------------------------------------

    if not title:

        return jsonify({
            "ok": False,
            "error": "Movie title डालें."
        }), 400

    if not video_key:

        return jsonify({
            "ok": False,
            "error": "Uploaded video missing."
        }), 400

    if (
        not is_valid_r2_key(video_key)
        or not video_key.startswith("videos/")
    ):

        return jsonify({
            "ok": False,
            "error": "Invalid video R2 key."
        }), 400

    # --------------------------------------------------------
    # Poster validation
    # --------------------------------------------------------

    if poster_key:

        if (
            not is_valid_r2_key(poster_key)
            or not poster_key.startswith("posters/")
        ):

            return jsonify({
                "ok": False,
                "error": "Invalid poster R2 key."
            }), 400

    # --------------------------------------------------------
    # Verify R2 objects
    # --------------------------------------------------------

    try:

        client = get_r2_client()

        video_head = client.head_object(
            Bucket=get_r2_bucket(),
            Key=video_key
        )

        video_size = int(
            video_head.get(
                "ContentLength",
                0
            )
        )

        if video_size <= 0:

            raise RuntimeError(
                "Video R2 object empty है."
            )

        if poster_key:

            client.head_object(
                Bucket=get_r2_bucket(),
                Key=poster_key
            )

    except Exception as error:

        return jsonify({
            "ok": False,
            "error": (
                "R2 object verification failed: "
                + str(error)
            )
        }), 400

    # --------------------------------------------------------
    # Save PostgreSQL record
    # --------------------------------------------------------

    try:

        row = db.fetchone(
            """
            INSERT INTO movies (
                title,
                category,
                description,
                poster,
                video
            )
            VALUES (
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
                video_key,
            )
        )

        return jsonify({
            "ok": True,
            "id": row["id"]
        })

    except Exception as error:

        return jsonify({
            "ok": False,
            "error": (
                "Database save failed: "
                + str(error)
            )
        }), 500


# ============================================================
# ADS SETTINGS
# ============================================================

@app.post("/admin/ads")
@admin_required
def save_ads():

    fields = [
        "top_ad",
        "player_ad",
        "bottom_ad",
    ]

    for key in fields:

        value = request.form.get(
            key,
            ""
        )

        db.execute(
            """
            INSERT INTO settings(key,value)
            VALUES(%s,%s)
            ON CONFLICT(key)
            DO UPDATE SET value=EXCLUDED.value
            """,
            (
                key,
                value,
            )
        )

    flash(
        "Ads settings saved",
        "success"
    )

    return redirect(
        url_for("admin")
    )


# ============================================================
# DELETE MOVIE
# ============================================================

@app.post("/admin/delete/<int:movie_id>")
@admin_required
def delete_movie(movie_id):

    movie_data = db.fetchone(
        """
        SELECT video, poster
        FROM movies
        WHERE id=%s
        """,
        (movie_id,)
    )

    if not movie_data:

        flash(
            "Movie not found",
            "error"
        )

        return redirect(
            url_for("admin")
        )

    # --------------------------------------------------------
    # Delete video
    # --------------------------------------------------------

    video_key = movie_data.get(
        "video"
    )

    if video_key:

        try:
            delete_r2_object(
                video_key
            )
        except Exception as error:
            print(
                "Video delete warning:",
                error
            )

    # --------------------------------------------------------
    # Delete poster
    # --------------------------------------------------------

    poster_key = movie_data.get(
        "poster"
    )

    if poster_key:

        try:
            delete_r2_object(
                poster_key
            )
        except Exception as error:
            print(
                "Poster delete warning:",
                error
            )

    # --------------------------------------------------------
    # Delete DB record
    # --------------------------------------------------------

    db.execute(
        """
        DELETE FROM movies
        WHERE id=%s
        """,
        (movie_id,)
    )

    flash(
        "Movie deleted",
        "success"
    )

    return redirect(
        url_for("admin")
    )


# ============================================================
# STARTUP DATABASE
# ============================================================

try:

    init_db()

except Exception as startup_error:

    print(
        "Database init warning:",
        startup_error
    )


# ============================================================
# LOCAL RUN
# ============================================================

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
