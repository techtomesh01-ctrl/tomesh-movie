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
    Response,
    jsonify,
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

# Direct browser -> R2 upload के लिए
# बड़ी files Flask/Render server से होकर नहीं जातीं।
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024


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
    "mkv",
    "webm",
    "mov",
}


# =========================================================
# FILE HELPERS
# =========================================================

def clean_filename(filename):
    if not filename:
        return ""

    filename = str(filename).strip()

    filename = filename.replace("\\", "/")
    filename = filename.rsplit("/", 1)[-1]

    filename = filename.split("?", 1)[0]
    filename = filename.split("#", 1)[0]

    return secure_filename(filename)


def get_extension(filename):
    if not filename:
        return ""

    filename = str(filename).strip()

    filename = filename.replace("\\", "/")
    filename = filename.rsplit("/", 1)[-1]

    filename = filename.split("?", 1)[0]
    filename = filename.split("#", 1)[0]

    if "." not in filename:
        return ""

    return filename.rsplit(".", 1)[1].lower()


def ext_ok(filename, allowed):
    extension = get_extension(filename)

    return (
        bool(extension)
        and extension in allowed
    )


def poster_mime_ok(content_type):
    if not content_type:
        return False

    content_type = str(
        content_type
    ).split(";", 1)[0].strip().lower()

    return content_type in {
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/webp",
    }


def video_mime_ok(content_type):
    if not content_type:
        return False

    content_type = str(
        content_type
    ).split(";", 1)[0].strip().lower()

    return content_type in {
        "video/mp4",
        "video/x-matroska",
        "video/webm",
        "video/quicktime",
        "application/octet-stream",
    }


def get_content_type(
    filename,
    default="application/octet-stream"
):
    extension = get_extension(filename)

    content_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",

        ".mp4": "video/mp4",
        ".mkv": "video/x-matroska",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
    }

    if extension:
        mime = content_types.get(
            "." + extension
        )

        if mime:
            return mime

    return default


# =========================================================
# ADMIN REQUIRED
# =========================================================

def admin_required(function):

    @wraps(function)
    def wrapper(*args, **kwargs):

        if not session.get("admin"):

            if request.path.startswith("/api/"):

                return jsonify({
                    "ok": False,
                    "error": (
                        "Admin session expired. "
                        "Please login again."
                    )
                }), 401

            return redirect(
                url_for("login")
            )

        return function(
            *args,
            **kwargs
        )

    return wrapper


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

    def execute(
        self,
        query,
        params=None
    ):
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
            signature_version="s3v4",
            s3={
                "addressing_style": "path"
            }
        )
    )


# =========================================================
# R2 PUBLIC URL
# =========================================================

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


# =========================================================
# R2 PRESIGNED GET URL
# =========================================================

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
            "Presigned GET URL failed: %s",
            e
        )

        return ""


# =========================================================
# R2 DELETE
# =========================================================

def r2_delete(object_key):

    if not object_key:
        return

    client = get_r2_client()

    client.delete_object(
        Bucket=R2_BUCKET,
        Key=object_key
    )


# =========================================================
# DIRECT R2 MULTIPART SETTINGS
# =========================================================

PART_SIZE = 10 * 1024 * 1024

PARALLEL_PARTS = 3

PRESIGNED_EXPIRES = 3600

MAX_MULTIPART_PARTS = 10000


# =========================================================
# R2 KEY VALIDATION
# =========================================================

def valid_r2_key(
    object_key,
    allowed_prefixes
):

    if not object_key:
        return False

    object_key = str(
        object_key
    ).strip()

    if len(object_key) > 1024:
        return False

    for prefix in allowed_prefixes:

        if object_key.startswith(prefix):
            return True

    return False


# =========================================================
# CREATE MULTIPART UPLOAD
# =========================================================

@app.route(
    "/api/r2/multipart/create",
    methods=["POST"]
)
@admin_required
def create_multipart():

    data = request.get_json(
        silent=True
    ) or {}

    original_filename = str(
        data.get(
            "filename",
            ""
        )
    ).strip()

    kind = str(
        data.get(
            "kind",
            "video"
        )
    ).strip().lower()

    browser_content_type = str(
        data.get(
            "content_type",
            ""
        )
    ).strip().lower()

    safe_name = clean_filename(
        original_filename
    )

    if kind not in (
        "video",
        "poster"
    ):

        return jsonify({
            "ok": False,
            "error": "Invalid upload type."
        }), 400

    if not safe_name:

        return jsonify({
            "ok": False,
            "error": "Invalid filename."
        }), 400


    # =====================================================
    # VIDEO
    # =====================================================

    if kind == "video":

        extension_valid = ext_ok(
            safe_name,
            ALLOWED_VIDEOS
        )

        # IMPORTANT:
        # Video को extension से allow करेंगे।
        # Browser MIME blank/unknown होने पर भी
        # valid MP4/MKV/WebM/MOV reject नहीं होगा.
        if not extension_valid:

            mime_valid = video_mime_ok(
                browser_content_type
            )

            if not mime_valid:

                return jsonify({
                    "ok": False,
                    "error": (
                        "Video केवल MP4, MKV, "
                        "WebM या MOV होनी चाहिए. "
                        "Filename: "
                        + safe_name
                        + " | Type: "
                        + (
                            browser_content_type
                            or "unknown"
                        )
                    )
                }), 400

        prefix = "videos/"


    # =====================================================
    # POSTER
    # =====================================================

    else:

        extension_valid = ext_ok(
            safe_name,
            ALLOWED_POSTERS
        )

        mime_valid = poster_mime_ok(
            browser_content_type
        )

        if not extension_valid and not mime_valid:

            return jsonify({
                "ok": False,
                "error": (
                    "Poster केवल JPG, JPEG, "
                    "PNG या WEBP होना चाहिए. "
                    "Filename: "
                    + safe_name
                    + " | Type: "
                    + (
                        browser_content_type
                        or "unknown"
                    )
                )
            }), 400

        prefix = "posters/"

        if not extension_valid:

            mime_extension = {
                "image/jpeg": ".jpg",
                "image/jpg": ".jpg",
                "image/png": ".png",
                "image/webp": ".webp",
            }.get(
                browser_content_type,
                ""
            )

            if not mime_extension:

                return jsonify({
                    "ok": False,
                    "error": (
                        "Poster format पहचान नहीं पाया."
                    )
                }), 400

            base_name = os.path.splitext(
                safe_name
            )[0]

            safe_name = (
                base_name
                + mime_extension
            )


    # =====================================================
    # CONTENT TYPE
    # =====================================================

    content_type = get_content_type(
        safe_name,
        browser_content_type
        or "application/octet-stream"
    )


    # =====================================================
    # OBJECT KEY
    # =====================================================

    base, extension = os.path.splitext(
        safe_name
    )

    extension = extension.lower()

    # Filename में unsafe/problematic characters
    # होने की स्थिति में clean base रखें.
    base = secure_filename(base)

    if not base:
        base = "upload"

    object_key = (
        prefix
        + base
        + "_"
        + secrets.token_hex(12)
        + extension
    )


    # =====================================================
    # R2 CREATE
    # =====================================================

    try:

        client = get_r2_client()

        result = client.create_multipart_upload(
            Bucket=R2_BUCKET,
            Key=object_key,
            ContentType=content_type
        )

        upload_id = result["UploadId"]

        app.logger.info(
            "R2 multipart created | kind=%s | filename=%s | key=%s | type=%s | upload_id=%s",
            kind,
            safe_name,
            object_key,
            content_type,
            upload_id
        )

        return jsonify({
            "ok": True,
            "upload_id": upload_id,
            "key": object_key,
            "part_size": PART_SIZE,
            "parallel": PARALLEL_PARTS,
            "expires": PRESIGNED_EXPIRES
        })

    except Exception as e:

        app.logger.exception(
            "Create multipart failed: %s",
            e
        )

        return jsonify({
            "ok": False,
            "error": (
                "R2 multipart upload शुरू नहीं हो पाया: "
                + str(e)
            )
        }), 500


# =========================================================
# GET PRESIGNED PART URLS
# =========================================================

@app.route(
    "/api/r2/multipart/urls",
    methods=["POST"]
)
@admin_required
def multipart_urls():

    data = request.get_json(
        silent=True
    ) or {}

    upload_id = str(
        data.get(
            "upload_id",
            ""
        )
    ).strip()

    object_key = str(
        data.get(
            "key",
            ""
        )
    ).strip()

    parts = data.get(
        "parts",
        []
    )

    if not upload_id:

        return jsonify({
            "ok": False,
            "error": "Upload ID missing."
        }), 400

    if not valid_r2_key(
        object_key,
        (
            "videos/",
            "posters/"
        )
    ):

        return jsonify({
            "ok": False,
            "error": "Invalid R2 object key."
        }), 400

    if not isinstance(
        parts,
        list
    ):

        return jsonify({
            "ok": False,
            "error": "Parts invalid."
        }), 400

    if not parts:

        return jsonify({
            "ok": False,
            "error": "Parts missing."
        }), 400

    if len(parts) > MAX_MULTIPART_PARTS:

        return jsonify({
            "ok": False,
            "error": "Too many parts."
        }), 400

    try:

        client = get_r2_client()

        urls = []

        seen = set()

        for raw_part_number in parts:

            try:

                part_number = int(
                    raw_part_number
                )

            except Exception:

                raise ValueError(
                    "Invalid part number."
                )

            if (
                part_number < 1
                or part_number > MAX_MULTIPART_PARTS
            ):

                raise ValueError(
                    "Invalid part number."
                )

            if part_number in seen:

                raise ValueError(
                    "Duplicate part number."
                )

            seen.add(
                part_number
            )

            url = client.generate_presigned_url(
                "upload_part",
                Params={
                    "Bucket": R2_BUCKET,
                    "Key": object_key,
                    "UploadId": upload_id,
                    "PartNumber": part_number
                },
                ExpiresIn=PRESIGNED_EXPIRES
            )

            urls.append({
                "part_number": part_number,
                "url": url
            })

        return jsonify({
            "ok": True,
            "urls": urls
        })

    except Exception as e:

        app.logger.exception(
            "Generate multipart URLs failed: %s",
            e
        )

        return jsonify({
            "ok": False,
            "error": (
                "Upload URLs generate नहीं हुए: "
                + str(e)
            )
        }), 500


# =========================================================
# COMPLETE MULTIPART UPLOAD
# =========================================================

@app.route(
    "/api/r2/multipart/complete",
    methods=["POST"]
)
@admin_required
def complete_multipart():

    data = request.get_json(
        silent=True
    ) or {}

    upload_id = str(
        data.get(
            "upload_id",
            ""
        )
    ).strip()

    object_key = str(
        data.get(
            "key",
            ""
        )
    ).strip()

    parts = data.get(
        "parts",
        []
    )

    # Browser से भेजी गई original file size
    expected_size_raw = data.get(
        "expected_size"
    )

    try:

        expected_size = int(
            expected_size_raw
        )

    except Exception:

        expected_size = 0

    if not upload_id:

        return jsonify({
            "ok": False,
            "error": "Upload ID missing."
        }), 400

    if not valid_r2_key(
        object_key,
        (
            "videos/",
            "posters/"
        )
    ):

        return jsonify({
            "ok": False,
            "error": "Invalid R2 object key."
        }), 400

    if not isinstance(
        parts,
        list
    ):

        return jsonify({
            "ok": False,
            "error": "Parts invalid."
        }), 400

    if not parts:

        return jsonify({
            "ok": False,
            "error": "No uploaded parts."
        }), 400

    if len(parts) > MAX_MULTIPART_PARTS:

        return jsonify({
            "ok": False,
            "error": "Too many parts."
        }), 400

    if expected_size <= 0:

        return jsonify({
            "ok": False,
            "error": "Original file size missing."
        }), 400

    try:

        clean_parts = []

        seen = set()

        for part in parts:

            if not isinstance(
                part,
                dict
            ):

                raise ValueError(
                    "Invalid part data."
                )

            raw_part_number = part.get(
                "PartNumber",
                part.get(
                    "part_number"
                )
            )

            raw_etag = part.get(
                "ETag",
                part.get(
                    "etag",
                    ""
                )
            )

            try:

                part_number = int(
                    raw_part_number
                )

            except Exception:

                raise ValueError(
                    "Invalid part number."
                )

            if (
                part_number < 1
                or part_number > MAX_MULTIPART_PARTS
            ):

                raise ValueError(
                    "Invalid part number."
                )

            if part_number in seen:

                raise ValueError(
                    "Duplicate part number."
                )

            seen.add(
                part_number
            )

            etag = str(
                raw_etag
            ).strip()

            if not etag:

                raise ValueError(
                    "ETag missing for part "
                    + str(part_number)
                )

            if (
                etag.startswith('"')
                and etag.endswith('"')
            ):

                etag = etag[1:-1]

            etag = etag.strip()

            if not etag:

                raise ValueError(
                    "Invalid ETag for part "
                    + str(part_number)
                )

            clean_parts.append({
                "PartNumber": part_number,
                "ETag": etag
            })

        clean_parts.sort(
            key=lambda x:
                x["PartNumber"]
        )

        # Parts 1 से शुरू होकर continuous होने चाहिए.
        expected_part_numbers = list(
            range(
                1,
                len(clean_parts) + 1
            )
        )

        actual_part_numbers = [
            item["PartNumber"]
            for item in clean_parts
        ]

        if actual_part_numbers != expected_part_numbers:

            raise ValueError(
                "Multipart parts incomplete हैं. "
                "Expected: "
                + str(expected_part_numbers[-1])
                + " parts, received part numbers: "
                + str(actual_part_numbers)
            )

        client = get_r2_client()

        # =================================================
        # COMPLETE ON R2
        # =================================================

        result = client.complete_multipart_upload(
            Bucket=R2_BUCKET,
            Key=object_key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": clean_parts
            }
        )

        # =================================================
        # IMPORTANT:
        # R2 में final object verify करें.
        # =================================================

        head = client.head_object(
            Bucket=R2_BUCKET,
            Key=object_key
        )

        actual_size = int(
            head.get(
                "ContentLength",
                0
            )
            or 0
        )

        if actual_size <= 0:

            raise RuntimeError(
                "R2 complete के बाद object खाली है."
            )

        if actual_size != expected_size:

            app.logger.error(
                "R2 size mismatch | key=%s | expected=%s | actual=%s",
                object_key,
                expected_size,
                actual_size
            )

            return jsonify({
                "ok": False,
                "error": (
                    "R2 upload size match नहीं हुई. "
                    "Expected "
                    + str(expected_size)
                    + " bytes, लेकिन R2 में "
                    + str(actual_size)
                    + " bytes मिले."
                ),
                "expected_size": expected_size,
                "actual_size": actual_size
            }), 400

        app.logger.info(
            "R2 multipart complete VERIFIED | key=%s | size=%s | parts=%s",
            object_key,
            actual_size,
            len(clean_parts)
        )

        return jsonify({
            "ok": True,
            "key": object_key,
            "url": r2_public_url(
                object_key
            ),
            "message": "R2 upload complete and verified.",
            "size": actual_size,
            "parts": len(clean_parts),
            "result": {
                "location": result.get(
                    "Location",
                    ""
                )
            }
        })

    except Exception as e:

        app.logger.exception(
            "Complete multipart failed: %s",
            e
        )

        return jsonify({
            "ok": False,
            "error": (
                "R2 multipart upload complete नहीं हुआ: "
                + str(e)
            )
        }), 500


# =========================================================
# ABORT MULTIPART
# =========================================================

@app.route(
    "/api/r2/multipart/abort",
    methods=["POST"]
)
@admin_required
def abort_multipart():

    data = request.get_json(
        silent=True
    ) or {}

    upload_id = str(
        data.get(
            "upload_id",
            ""
        )
    ).strip()

    object_key = str(
        data.get(
            "key",
            ""
        )
    ).strip()

    if not upload_id or not object_key:

        return jsonify({
            "ok": True
        })

    if not valid_r2_key(
        object_key,
        (
            "videos/",
            "posters/"
        )
    ):

        return jsonify({
            "ok": False,
            "error": "Invalid R2 object key."
        }), 400

    try:

        client = get_r2_client()

        client.abort_multipart_upload(
            Bucket=R2_BUCKET,
            Key=object_key,
            UploadId=upload_id
        )

        app.logger.info(
            "R2 multipart aborted | key=%s | upload_id=%s",
            object_key,
            upload_id
        )

    except Exception as e:

        app.logger.exception(
            "Abort multipart failed: %s",
            e
        )

    return jsonify({
        "ok": True
    })


# =========================================================
# LOCAL DIRECTORIES
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

        defaults = {
            "ad_top": "",
            "ad_player": "",
            "ad_bottom": "",
        }

        for key, value in defaults.items():

            con.execute(
                """
                INSERT INTO settings(
                    key,
                    value
                )
                VALUES (%s, %s)
                ON CONFLICT(key)
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
# TEMPLATE GLOBALS
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

        return render_template(
            "movie.html",
            movie=movie_data
        )

    finally:

        con.close()


# =========================================================
# POSTER ROUTE
# =========================================================

@app.route(
    "/poster/<path:name>"
)
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

    if os.path.isfile(
        local_path
    ):

        from flask import send_from_directory

        return send_from_directory(
            POSTER_DIR,
            os.path.basename(name)
        )

    abort(404)


# =========================================================
# VIDEO ROUTE
# =========================================================

@app.route(
    "/video/<path:name>"
)
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

    if os.path.isfile(
        local_path
    ):

        from flask import send_file

        return send_file(
            local_path,
            conditional=True
        )

    abort(404)


# =========================================================
# ADS.TXT
# =========================================================

@app.route(
    "/ads.txt"
)
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

@app.route(
    "/health"
)
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
            "Health failed: %s",
            e
        )

        return "Database error", 500

    finally:

        if con:
            con.close()


# =========================================================
# R2 HEALTH
# =========================================================

@app.route(
    "/r2-health"
)
def r2_health():

    try:

        client = get_r2_client()

        client.head_bucket(
            Bucket=R2_BUCKET
        )

        return "R2 OK", 200

    except Exception as e:

        app.logger.exception(
            "R2 health failed: %s",
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

@app.route(
    "/logout"
)
def logout():

    session.clear()

    return redirect(
        url_for("home")
    )


# =========================================================
# ADMIN
# =========================================================

@app.route(
    "/admin"
)
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
# SAVE MOVIE AFTER R2 UPLOAD
# =========================================================

@app.route(
    "/api/movie/save",
    methods=["POST"]
)
@admin_required
def save_movie():

    data = request.get_json(
        silent=True
    ) or {}

    title = str(
        data.get(
            "title",
            ""
        )
    ).strip()

    category = str(
        data.get(
            "category",
            ""
        )
    ).strip()

    description = str(
        data.get(
            "description",
            ""
        )
    ).strip()

    video_key = str(
        data.get(
            "video_key",
            ""
        )
    ).strip()

    poster_key = str(
        data.get(
            "poster_key",
            ""
        )
    ).strip()

    # Optional frontend size verification
    expected_video_size_raw = data.get(
        "video_size"
    )

    try:

        expected_video_size = int(
            expected_video_size_raw
        )

    except Exception:

        expected_video_size = 0

    if not title:

        return jsonify({
            "ok": False,
            "error": "Movie title जरूरी है."
        }), 400

    if not video_key:

        return jsonify({
            "ok": False,
            "error": "Video upload missing."
        }), 400

    if not valid_r2_key(
        video_key,
        (
            "videos/",
        )
    ):

        return jsonify({
            "ok": False,
            "error": "Invalid video R2 key."
        }), 400

    video_extension = get_extension(
        video_key
    )

    if video_extension not in ALLOWED_VIDEOS:

        return jsonify({
            "ok": False,
            "error": (
                "Video R2 key में valid extension नहीं है. "
                "Allowed: MP4, MKV, WebM, MOV."
            )
        }), 400

    if (
        poster_key
        and
        not valid_r2_key(
            poster_key,
            (
                "posters/",
            )
        )
    ):

        return jsonify({
            "ok": False,
            "error": "Invalid poster R2 key."
        }), 400

    if poster_key:

        poster_extension = get_extension(
            poster_key
        )

        if poster_extension not in ALLOWED_POSTERS:

            return jsonify({
                "ok": False,
                "error": (
                    "Poster R2 key में valid extension नहीं है. "
                    "Allowed: JPG, JPEG, PNG, WEBP."
                )
            }), 400


    # =====================================================
    # VERIFY R2 OBJECTS
    # =====================================================

    try:

        client = get_r2_client()

        video_head = client.head_object(
            Bucket=R2_BUCKET,
            Key=video_key
        )

        actual_video_size = int(
            video_head.get(
                "ContentLength",
                0
            )
            or 0
        )

        if actual_video_size <= 0:

            return jsonify({
                "ok": False,
                "error": (
                    "R2 video object खाली है."
                )
            }), 400

        if (
            expected_video_size > 0
            and
            actual_video_size != expected_video_size
        ):

            return jsonify({
                "ok": False,
                "error": (
                    "Video size verify नहीं हुई. "
                    "Expected "
                    + str(expected_video_size)
                    + " bytes, R2 में "
                    + str(actual_video_size)
                    + " bytes मिले."
                ),
                "expected_size": expected_video_size,
                "actual_size": actual_video_size
            }), 400

        if poster_key:

            poster_head = client.head_object(
                Bucket=R2_BUCKET,
                Key=poster_key
            )

            poster_size = int(
                poster_head.get(
                    "ContentLength",
                    0
                )
                or 0
            )

            if poster_size <= 0:

                return jsonify({
                    "ok": False,
                    "error": (
                        "R2 poster object खाली है."
                    )
                }), 400

    except Exception as e:

        app.logger.exception(
            "R2 object verification failed: %s",
            e
        )

        return jsonify({
            "ok": False,
            "error": (
                "R2 में uploaded file verify नहीं हुई: "
                + str(e)
            )
        }), 400


    # =====================================================
    # DATABASE SAVE
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
                poster_key,
                video_key
            )
        ).fetchone()

        con.commit()

        app.logger.info(
            "Movie published | id=%s | title=%s | video=%s | poster=%s | size=%s",
            row["id"],
            title,
            video_key,
            poster_key,
            actual_video_size
        )

        return jsonify({
            "ok": True,
            "id": row["id"],
            "message": "Movie publish हो गई.",
            "video_size": actual_video_size
        })

    except Exception as e:

        if con:
            con.rollback()

        app.logger.exception(
            "Save movie failed: %s",
            e
        )

        return jsonify({
            "ok": False,
            "error": (
                "Movie database में save नहीं हुई: "
                + str(e)
            )
        }), 500

    finally:

        if con:
            con.close()


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
# 404
# =========================================================

@app.errorhandler(404)
def not_found(error):

    return (
        "Page नहीं मिली.",
        404
    )


# =========================================================
# 413
# =========================================================

@app.errorhandler(413)
def too_large(error):

    if request.path.startswith("/api/"):

        return jsonify({
            "ok": False,
            "error": (
                "Request बहुत बड़ी है. "
                "Maximum upload size 4 GB है."
            )
        }), 413

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

    if request.path.startswith("/api/"):

        return jsonify({
            "ok": False,
            "error": (
                "Server error. "
                "Render Logs देखें."
            ),
            "path": request.path
        }), 500

    return (
        "Internal Server Error. Render Logs देखें.",
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
