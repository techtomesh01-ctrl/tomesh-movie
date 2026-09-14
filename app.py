import os
import secrets
import mimetypes
import re
from functools import wraps
from urllib.parse import quote

import boto3
import psycopg2
from psycopg2.extras import RealDictCursor
from botocore.client import Config
from botocore.exceptions import ClientError

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

# ============================================================

# APP

# ============================================================

app = Flask(**name**)

app.secret_key = (
os.environ.get("SECRET_KEY", "").strip()
or secrets.token_hex(32)
)

# Render request limit

app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024

# ============================================================

# ADMIN

# ============================================================

ADMIN_USER = (
os.environ.get("ADMIN_USER", "admin").strip()
or "admin"
)

ADMIN_PASSWORD = (
os.environ.get("ADMIN_PASSWORD", "change-me-now").strip()
or "change-me-now"
)

# ============================================================

# DATABASE

# ============================================================

DATABASE_URL = (
os.environ.get("DATABASE_URL", "").strip()
)

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

MAX_VIDEO_SIZE = 4 * 1024 * 1024 * 1024
MAX_POSTER_SIZE = 25 * 1024 * 1024

# ============================================================

# R2 SETTINGS

# ============================================================

R2_ACCOUNT_ID = (
os.environ.get("R2_ACCOUNT_ID", "").strip()
)

R2_ACCESS_KEY_ID = (
os.environ.get("R2_ACCESS_KEY_ID", "").strip()
)

R2_SECRET_ACCESS_KEY = (
os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
)

R2_BUCKET = (
os.environ.get("R2_BUCKET", "tomesh-movies").strip()
or "tomesh-movies"
)

R2_ENDPOINT = (
os.environ.get("R2_ENDPOINT", "").strip()
)

R2_PUBLIC_URL = (
os.environ.get("R2_PUBLIC_URL", "").strip()
)

# Multipart settings

PART_SIZE = 10 * 1024 * 1024
PARALLEL_PARTS = 3
PRESIGNED_EXPIRES = 3600
MAX_MULTIPART_PARTS = 10000

VIDEO_PREFIX = "videos/"
POSTER_PREFIX = "posters/"

# ============================================================

# CLEAN ENV VALUES

# ============================================================

def clean_env_value(value):
if value is None:
return ""

```
value = str(value)

# Remove accidental line breaks
value = value.replace("\r", "")
value = value.replace("\n", "")

# Remove accidental surrounding quotes
value = value.strip().strip('"').strip("'")

return value.strip()
```

R2_ACCOUNT_ID = clean_env_value(R2_ACCOUNT_ID)
R2_ACCESS_KEY_ID = clean_env_value(R2_ACCESS_KEY_ID)
R2_SECRET_ACCESS_KEY = clean_env_value(R2_SECRET_ACCESS_KEY)
R2_BUCKET = clean_env_value(R2_BUCKET)
R2_ENDPOINT = clean_env_value(R2_ENDPOINT)
R2_PUBLIC_URL = clean_env_value(R2_PUBLIC_URL)
DATABASE_URL = clean_env_value(DATABASE_URL)

def clean_endpoint(endpoint):
endpoint = clean_env_value(endpoint)

```
if not endpoint:
    return ""

endpoint = endpoint.rstrip("/")

# User sometimes pastes:
# https://ACCOUNT.r2.cloudflarestorage.com/bucket
bucket_suffix = "/" + R2_BUCKET

if endpoint.lower().endswith(
    bucket_suffix.lower()
):
    endpoint = endpoint[
        :-len(bucket_suffix)
    ].rstrip("/")

return endpoint
```

R2_ENDPOINT = clean_endpoint(R2_ENDPOINT)

# ============================================================

# JSON HELPERS

# ============================================================

def json_ok(**kwargs):
data = {
"ok": True
}
data.update(kwargs)
return jsonify(data)

def json_error(message, status=400, **kwargs):
data = {
"ok": False,
"error": str(message),
}
data.update(kwargs)
return jsonify(data), status

# ============================================================

# FILE HELPERS

# ============================================================

def get_extension(name):
name = str(name or "").lower().strip()

```
if "." not in name:
    return ""

return name.rsplit(".", 1)[1]
```

def allowed_video(filename):
return (
get_extension(filename)
in ALLOWED_VIDEOS
)

def allowed_poster(filename):
return (
get_extension(filename)
in ALLOWED_POSTERS
)

def safe_filename(filename):
filename = secure_filename(
str(filename or "")
)

```
if not filename:
    filename = "file"

return filename
```

def content_type_for_key(key):
key = str(key or "").lower()

```
ext = get_extension(key)

mapping = {
    "mp4": "video/mp4",
    "mkv": "video/x-matroska",
    "webm": "video/webm",
    "mov": "video/quicktime",

    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}

if ext in mapping:
    return mapping[ext]

guessed, _ = mimetypes.guess_type(key)

return (
    guessed
    or "application/octet-stream"
)
```

# ============================================================

# R2 KEY VALIDATION

# ============================================================

def validate_r2_key(key):
if not key:
return False

```
key = str(key)

if len(key) > 1024:
    return False

if "\r" in key or "\n" in key:
    return False

if key.startswith(VIDEO_PREFIX):
    return True

if key.startswith(POSTER_PREFIX):
    return True

return False
```

def validate_video_key(key):
if not key:
return False

```
return str(key).startswith(
    VIDEO_PREFIX
)
```

def validate_poster_key(key):
if not key:
return False

```
return str(key).startswith(
    POSTER_PREFIX
)
```

# ============================================================

# DATABASE

# ============================================================

def get_db(dict_rows=False):
if not DATABASE_URL:
raise RuntimeError(
"DATABASE_URL is missing."
)

```
kwargs = {
    "dsn": DATABASE_URL,
    "connect_timeout": 15,
}

if dict_rows:
    kwargs["cursor_factory"] = RealDictCursor

return psycopg2.connect(**kwargs)
```

def init_db():
conn = get_db()

```
try:
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS movies (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            category TEXT,
            description TEXT,
            poster TEXT,
            video TEXT,
            views INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )

    conn.commit()

finally:
    conn.close()
```

# ============================================================

# SETTINGS

# ============================================================

def get_setting(key, default=""):
conn = None

```
try:
    conn = get_db(dict_rows=True)

    cur = conn.cursor()

    cur.execute(
        """
        SELECT value
        FROM settings
        WHERE key = %s
        """,
        (key,)
    )

    row = cur.fetchone()

    if row and row.get("value") is not None:
        return row["value"]

    return default

finally:
    if conn:
        conn.close()
```

def set_setting(key, value):
conn = get_db()

```
try:
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO settings(key, value)
        VALUES(%s, %s)
        ON CONFLICT(key)
        DO UPDATE SET value = EXCLUDED.value
        """,
        (key, value)
    )

    conn.commit()

finally:
    conn.close()
```

def get_ads():
return {
"top": get_setting(
"ad_top",
""
),
"player": get_setting(
"ad_player",
""
),
"bottom": get_setting(
"ad_bottom",
""
),
}

# ============================================================

# R2 CLIENT

# ============================================================

_r2_client = None

def get_r2_client():
global _r2_client

```
if _r2_client is not None:
    return _r2_client

if not R2_ENDPOINT:
    raise RuntimeError(
        "R2_ENDPOINT is missing."
    )

if not R2_ACCESS_KEY_ID:
    raise RuntimeError(
        "R2_ACCESS_KEY_ID is missing."
    )

if not R2_SECRET_ACCESS_KEY:
    raise RuntimeError(
        "R2_SECRET_ACCESS_KEY is missing."
    )

_r2_client = boto3.client(
    "s3",

    endpoint_url=R2_ENDPOINT,

    aws_access_key_id=
        R2_ACCESS_KEY_ID,

    aws_secret_access_key=
        R2_SECRET_ACCESS_KEY,

    region_name="auto",

    config=Config(
        signature_version="s3v4",
        s3={
            "addressing_style":
                "path"
        },
        retries={
            "max_attempts": 4,
            "mode": "standard",
        },
    ),
)

return _r2_client
```

# ============================================================

# R2 URL HELPERS

# ============================================================

def r2_public_url(key):
if not key:
return None

```
key = str(key).lstrip("/")

if not R2_PUBLIC_URL:
    return None

base = R2_PUBLIC_URL.rstrip("/")

return (
    base
    + "/"
    + quote(
        key,
        safe="/"
    )
)
```

def media_url(key):
if not key:
return None

```
# If public URL exists, use it for posters.
public = r2_public_url(key)

if public:
    return public

return r2_presigned_url(
    key,
    expires=PRESIGNED_EXPIRES
)
```

def r2_presigned_url(
key,
expires=PRESIGNED_EXPIRES
):
if not validate_r2_key(key):
raise ValueError(
"Invalid R2 key."
)

```
client = get_r2_client()

response = client.generate_presigned_url(
    "get_object",
    Params={
        "Bucket": R2_BUCKET,
        "Key": key,
    },
    ExpiresIn=int(expires),
)

return response
```

# ============================================================

# R2 OBJECT HELPERS

# ============================================================

def r2_head(key):
if not validate_r2_key(key):
raise ValueError(
"Invalid R2 key."
)

```
client = get_r2_client()

return client.head_object(
    Bucket=R2_BUCKET,
    Key=key,
)
```

def r2_delete(key):
if not validate_r2_key(key):
return False

```
client = get_r2_client()

client.delete_object(
    Bucket=R2_BUCKET,
    Key=key,
)

return True
```

# ============================================================

# ADMIN AUTH

# ============================================================

def admin_required(view_func):

```
@wraps(view_func)
def wrapped(*args, **kwargs):

    if not session.get("admin_logged_in"):
        return redirect(
            url_for(
                "login",
                next=request.path
            )
        )

    return view_func(
        *args,
        **kwargs
    )

return wrapped
```

# ============================================================

# HOME

# ============================================================

@app.route("/")
def home():

```
conn = None

try:

    conn = get_db(
        dict_rows=True
    )

    cur = conn.cursor()

    cur.execute(
        """
        SELECT *
        FROM movies
        ORDER BY id DESC
        """
    )

    movies = cur.fetchall()

    for movie in movies:

        movie["poster_url"] = (
            media_url(
                movie.get("poster")
            )
            if movie.get("poster")
            else None
        )

        movie["views"] = int(
            movie.get("views") or 0
        )

    return render_template(
        "index.html",
        movies=movies,
        ads=get_ads()
    )

finally:

    if conn:
        conn.close()
```

# Keep compatibility with templates

# using url_for('index')

app.add_url_rule(
"/",
endpoint="index",
view_func=home
)

# ============================================================

# MOVIE PAGE

# ============================================================

@app.route("/movie/[int:movie_id](int:movie_id)")
def movie_page(movie_id):

```
conn = None

try:

    conn = get_db(
        dict_rows=True
    )

    cur = conn.cursor()

    cur.execute(
        """
        SELECT *
        FROM movies
        WHERE id = %s
        """,
        (movie_id,)
    )

    movie = cur.fetchone()

    if not movie:
        abort(404)

    # Increment views
    cur.execute(
        """
        UPDATE movies
        SET views =
            COALESCE(views, 0) + 1
        WHERE id = %s
        """,
        (movie_id,)
    )

    conn.commit()

    video_key = movie.get(
        "video"
    )

    poster_key = movie.get(
        "poster"
    )


    # ====================================================
    # IMPORTANT PLAYBACK FIX
    #
    # Do NOT send browser directly to R2 video.
    # Browser now uses our Range-enabled stream endpoint.
    # ====================================================

    if video_key and validate_video_key(
        video_key
    ):

        movie["video_url"] = url_for(
            "stream_movie",
            movie_id=movie_id
        )

        movie["video_mime"] = (
            content_type_for_key(
                video_key
            )
        )

    else:

        movie["video_url"] = None

        movie["video_mime"] = (
            "video/mp4"
        )


    # Poster can still use R2 public/presigned URL
    if poster_key:

        try:

            movie["poster_url"] = (
                media_url(
                    poster_key
                )
            )

        except Exception as e:

            print(
                "Poster URL error:",
                repr(e)
            )

            movie["poster_url"] = None

    else:

        movie["poster_url"] = None


    movie["views"] = int(
        movie.get("views") or 0
    )


    return render_template(
        "movie.html",
        movie=movie,
        ads=get_ads()
    )

finally:

    if conn:
        conn.close()
```

# ============================================================

# VIDEO STREAM

# ============================================================

#

# THIS IS THE MAIN PLAYBACK FIX.

#

# Browser requests:

#

# GET /stream/5

#

# Browser can send:

#

# Range: bytes=0-

# Range: bytes=1000000-

#

# R2 object is fetched with the same Range.

#

# Response:

#

# 206 Partial Content

#

# This allows:

# - video loading

# - seeking

# - forward/backward

# - browser buffering

# ============================================================

@app.route(
"/stream/[int:movie_id](int:movie_id)",
methods=["GET"]
)
def stream_movie(movie_id):

```
conn = None
r2_response = None
body = None

try:

    # ----------------------------------------------------
    # GET MOVIE FROM DATABASE
    # ----------------------------------------------------

    conn = get_db(
        dict_rows=True
    )

    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            id,
            title,
            video
        FROM movies
        WHERE id = %s
        """,
        (movie_id,)
    )

    movie = cur.fetchone()

    if not movie:
        return Response(
            "Movie not found.",
            status=404,
            mimetype="text/plain"
        )

    video_key = movie.get(
        "video"
    )

    if not validate_video_key(
        video_key
    ):
        return Response(
            "Video file not found.",
            status=404,
            mimetype="text/plain"
        )


    # ----------------------------------------------------
    # R2 CLIENT
    # ----------------------------------------------------

    client = get_r2_client()


    # ----------------------------------------------------
    # HEAD OBJECT
    # ----------------------------------------------------

    try:

        head = client.head_object(
            Bucket=R2_BUCKET,
            Key=video_key,
        )

    except ClientError as e:

        print(
            "R2 HEAD ERROR:",
            repr(e)
        )

        return Response(
            "Video object not found in R2.",
            status=404,
            mimetype="text/plain"
        )


    total_size = int(
        head.get("ContentLength", 0)
        or 0
    )

    if total_size <= 0:

        return Response(
            "Video object is empty.",
            status=404,
            mimetype="text/plain"
        )


    content_type = (
        head.get("ContentType")
        or content_type_for_key(
            video_key
        )
    )


    # ----------------------------------------------------
    # RANGE HEADER
    # ----------------------------------------------------

    range_header = request.headers.get(
        "Range"
    )


    # ====================================================
    # NO RANGE
    # ====================================================

    if not range_header:

        try:

            r2_response = client.get_object(
                Bucket=R2_BUCKET,
                Key=video_key,
            )

            body = r2_response["Body"]

        except ClientError as e:

            print(
                "R2 GET ERROR:",
                repr(e)
            )

            return Response(
                "Unable to read video from R2.",
                status=502,
                mimetype="text/plain"
            )


        def generate_full():

            try:

                while True:

                    chunk = body.read(
                        1024 * 1024
                    )

                    if not chunk:
                        break

                    yield chunk

            finally:

                try:
                    body.close()
                except Exception:
                    pass


        response = Response(
            generate_full(),
            status=200,
            mimetype=content_type,
        )

        response.headers[
            "Content-Length"
        ] = str(total_size)

        response.headers[
            "Accept-Ranges"
        ] = "bytes"

        response.headers[
            "Cache-Control"
        ] = "public, max-age=3600"

        response.headers[
            "Content-Disposition"
        ] = "inline"

        return response


    # ====================================================
    # PARSE RANGE
    # ====================================================

    match = re.match(
        r"^bytes=(\d*)-(\d*)$",
        range_header.strip()
    )

    if not match:

        return Response(
            "Invalid Range.",
            status=416,
            headers={
                "Content-Range":
                    "bytes */"
                    + str(total_size)
            }
        )


    start_text = match.group(1)
    end_text = match.group(2)


    # ----------------------------------------------------
    # bytes=-500000
    # Last 500000 bytes
    # ----------------------------------------------------

    if not start_text:

        suffix_length = int(
            end_text or 0
        )

        if suffix_length <= 0:

            return Response(
                "Invalid Range.",
                status=416,
                headers={
                    "Content-Range":
                        "bytes */"
                        + str(total_size)
                }
            )

        if suffix_length > total_size:
            suffix_length = total_size

        start = (
            total_size
            - suffix_length
        )

        end = total_size - 1


    else:

        # ------------------------------------------------
        # bytes=START-
        # ------------------------------------------------

        start = int(
            start_text
        )

        if start >= total_size:

            return Response(
                "Range Not Satisfiable.",
                status=416,
                headers={
                    "Content-Range":
                        "bytes */"
                        + str(total_size)
                }
            )


        # ------------------------------------------------
        # bytes=START-END
        # ------------------------------------------------

        if end_text:

            end = int(
                end_text
            )

        else:

            end = total_size - 1


        if end >= total_size:
            end = total_size - 1


        if start > end:

            return Response(
                "Range Not Satisfiable.",
                status=416,
                headers={
                    "Content-Range":
                        "bytes */"
                        + str(total_size)
                }
            )


    content_length = (
        end - start + 1
    )


    # ----------------------------------------------------
    # R2 RANGE GET
    # ----------------------------------------------------

    try:

        r2_response = client.get_object(
            Bucket=R2_BUCKET,
            Key=video_key,
            Range=(
                "bytes="
                + str(start)
                + "-"
                + str(end)
            ),
        )

        body = r2_response["Body"]

    except ClientError as e:

        print(
            "R2 RANGE GET ERROR:",
            repr(e)
        )

        return Response(
            "Unable to stream video from R2.",
            status=502,
            mimetype="text/plain"
        )


    # ----------------------------------------------------
    # STREAM GENERATOR
    # ----------------------------------------------------

    def generate_range():

        try:

            remaining = content_length

            while remaining > 0:

                chunk = body.read(
                    min(
                        1024 * 1024,
                        remaining
                    )
                )

                if not chunk:
                    break

                remaining -= len(chunk)

                yield chunk

        finally:

            try:
                body.close()
            except Exception:
                pass


    response = Response(
        generate_range(),
        status=206,
        mimetype=content_type,
    )

    response.headers[
        "Content-Length"
    ] = str(content_length)

    response.headers[
        "Content-Range"
    ] = (
        "bytes "
        + str(start)
        + "-"
        + str(end)
        + "/"
        + str(total_size)
    )

    response.headers[
        "Accept-Ranges"
    ] = "bytes"

    response.headers[
        "Cache-Control"
    ] = "public, max-age=3600"

    response.headers[
        "Content-Disposition"
    ] = "inline"

    return response


except ValueError as e:

    print(
        "STREAM RANGE VALUE ERROR:",
        repr(e)
    )

    return Response(
        "Invalid video range.",
        status=416,
        mimetype="text/plain"
    )


except Exception as e:

    print(
        "STREAM ERROR:",
        repr(e)
    )

    return Response(
        "Video streaming error.",
        status=500,
        mimetype="text/plain"
    )


finally:

    if conn:
        conn.close()
```

# ============================================================

# LOGIN

# ============================================================

@app.route(
"/login",
methods=["GET", "POST"]
)
def login():

```
if request.method == "POST":

    username = (
        request.form.get(
            "username",
            ""
        ).strip()
    )

    password = (
        request.form.get(
            "password",
            ""
        )
    )

    if (
        username == ADMIN_USER
        and password == ADMIN_PASSWORD
    ):

        session[
            "admin_logged_in"
        ] = True

        next_url = (
            request.args.get(
                "next"
            )
            or url_for("admin")
        )

        return redirect(
            next_url
        )


    flash(
        "Invalid username or password.",
        "error"
    )


return render_template(
    "login.html"
)
```

# ============================================================

# LOGOUT

# ============================================================

@app.route("/logout")
def logout():

```
session.clear()

return redirect(
    url_for("login")
)
```

# ============================================================

# ADMIN DASHBOARD

# ============================================================

@app.route("/admin")
@admin_required
def admin():

```
conn = None

try:

    conn = get_db(
        dict_rows=True
    )

    cur = conn.cursor()


    # Total movies
    cur.execute(
        """
        SELECT COUNT(*) AS total
        FROM movies
        """
    )

    total_row = cur.fetchone()

    total_movies = int(
        total_row["total"]
        if total_row
        else 0
    )


    # Total views
    cur.execute(
        """
        SELECT
            COALESCE(
                SUM(views),
                0
            ) AS total_views
        FROM movies
        """
    )

    views_row = cur.fetchone()

    total_views = int(
        views_row["total_views"]
        if views_row
        else 0
    )


    # Movies
    cur.execute(
        """
        SELECT *
        FROM movies
        ORDER BY id DESC
        """
    )

    movies = cur.fetchall()


    for movie in movies:

        if movie.get("poster"):

            try:

                movie["poster_url"] = (
                    media_url(
                        movie["poster"]
                    )
                )

            except Exception as e:

                print(
                    "Admin poster URL error:",
                    repr(e)
                )

                movie["poster_url"] = None

        else:

            movie["poster_url"] = None


        movie["views"] = int(
            movie.get("views") or 0
        )


    return render_template(
        "admin.html",
        movies=movies,
        total_movies=total_movies,
        total_views=total_views,
        ads=get_ads()
    )


finally:

    if conn:
        conn.close()
```

# ============================================================

# ADMIN ADD PAGE

# ============================================================

@app.route(
"/admin/add",
methods=["GET", "POST"]
)
@admin_required
def admin_add():

```
# --------------------------------------------------------
# This old server-upload route is kept for compatibility.
#
# Main admin.html uses direct R2 multipart upload.
# --------------------------------------------------------

if request.method == "POST":

    title = (
        request.form.get(
            "title",
            ""
        ).strip()
    )

    category = (
        request.form.get(
            "category",
            ""
        ).strip()
    )

    description = (
        request.form.get(
            "description",
            ""
        ).strip()
    )

    video_file = request.files.get(
        "video"
    )

    poster_file = request.files.get(
        "poster"
    )


    if not title:
        flash(
            "Movie title is required.",
            "error"
        )

        return redirect(
            url_for("admin_add")
        )


    if not video_file:

        flash(
            "Video is required.",
            "error"
        )

        return redirect(
            url_for("admin_add")
        )


    if not allowed_video(
        video_file.filename
    ):

        flash(
            "Video केवल MP4, MKV, WebM या MOV होनी चाहिए.",
            "error"
        )

        return redirect(
            url_for("admin_add")
        )


    video_key = None
    poster_key = None


    try:

        client = get_r2_client()


        # ------------------------------------------------
        # VIDEO
        # ------------------------------------------------

        video_name = safe_filename(
            video_file.filename
        )

        video_ext = get_extension(
            video_name
        )

        video_key = (
            VIDEO_PREFIX
            + secrets.token_hex(16)
            + "."
            + video_ext
        )


        video_file.stream.seek(0)

        client.upload_fileobj(
            video_file,
            R2_BUCKET,
            video_key,
            ExtraArgs={
                "ContentType":
                    content_type_for_key(
                        video_key
                    )
            },
        )


        # ------------------------------------------------
        # POSTER
        # ------------------------------------------------

        if poster_file and poster_file.filename:

            if not allowed_poster(
                poster_file.filename
            ):

                raise ValueError(
                    "Poster केवल JPG, JPEG, PNG या WEBP होनी चाहिए."
                )


            poster_name = safe_filename(
                poster_file.filename
            )

            poster_ext = get_extension(
                poster_name
            )

            poster_key = (
                POSTER_PREFIX
                + secrets.token_hex(16)
                + "."
                + poster_ext
            )


            poster_file.stream.seek(0)

            client.upload_fileobj(
                poster_file,
                R2_BUCKET,
                poster_key,
                ExtraArgs={
                    "ContentType":
                        content_type_for_key(
                            poster_key
                        )
                },
            )


        # ------------------------------------------------
        # DATABASE
        # ------------------------------------------------

        conn = get_db()

        try:

            cur = conn.cursor()

            cur.execute(
                """
                INSERT INTO movies(
                    title,
                    category,
                    description,
                    poster,
                    video
                )
                VALUES(
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )
                """,
                (
                    title,
                    category,
                    description,
                    poster_key,
                    video_key,
                )
            )

            conn.commit()

        finally:

            conn.close()


        flash(
            "Movie successfully added.",
            "success"
        )

        return redirect(
            url_for("admin")
        )


    except Exception as e:

        print(
            "ADMIN ADD ERROR:",
            repr(e)
        )


        if video_key:

            try:
                r2_delete(
                    video_key
                )
            except Exception:
                pass


        if poster_key:

            try:
                r2_delete(
                    poster_key
                )
            except Exception:
                pass


        flash(
            "Upload failed: "
            + str(e),
            "error"
        )


return render_template(
    "admin_add.html"
)
```

# ============================================================

# DELETE MOVIE

# ============================================================

@app.route(
"/admin/delete/[int:movie_id](int:movie_id)",
methods=["GET", "POST"]
)
@admin_required
def admin_delete_movie(movie_id):

```
conn = None

try:

    conn = get_db(
        dict_rows=True
    )

    cur = conn.cursor()

    cur.execute(
        """
        SELECT *
        FROM movies
        WHERE id = %s
        """,
        (movie_id,)
    )

    movie = cur.fetchone()

    if not movie:

        flash(
            "Movie not found.",
            "error"
        )

        return redirect(
            url_for("admin")
        )


    video_key = movie.get(
        "video"
    )

    poster_key = movie.get(
        "poster"
    )


    # Delete video
    if video_key:

        try:

            r2_delete(
                video_key
            )

        except Exception as e:

            print(
                "Video delete error:",
                repr(e)
            )


    # Delete poster
    if poster_key:

        try:

            r2_delete(
                poster_key
            )

        except Exception as e:

            print(
                "Poster delete error:",
                repr(e)
            )


    # Delete DB record
    cur.execute(
        """
        DELETE FROM movies
        WHERE id = %s
        """,
        (movie_id,)
    )

    conn.commit()


    flash(
        "Movie deleted successfully.",
        "success"
    )

    return redirect(
        url_for("admin")
    )


finally:

    if conn:
        conn.close()
```

# ============================================================

# R2 MULTIPART CREATE

# ============================================================

@app.route(
"/api/r2/multipart/create",
methods=["POST"]
)
@admin_required
def api_r2_multipart_create():

```
try:

    data = request.get_json(
        silent=True
    ) or {}

    key = str(
        data.get("key")
        or ""
    ).strip()

    content_type = str(
        data.get("content_type")
        or "application/octet-stream"
    ).strip()


    if not validate_r2_key(key):

        return json_error(
            "Invalid R2 key.",
            400
        )


    if not (
        validate_video_key(key)
        or validate_poster_key(key)
    ):

        return json_error(
            "Invalid media key.",
            400
        )


    client = get_r2_client()


    result = (
        client.create_multipart_upload(
            Bucket=R2_BUCKET,
            Key=key,
            ContentType=content_type,
        )
    )


    upload_id = result.get(
        "UploadId"
    )

    if not upload_id:

        return json_error(
            "R2 UploadId नहीं मिला.",
            502
        )


    return json_ok(
        upload_id=upload_id,
        key=key,
        part_size=PART_SIZE,
        parallel=PARALLEL_PARTS,
        expires=PRESIGNED_EXPIRES,
    )


except Exception as e:

    print(
        "R2 MULTIPART CREATE ERROR:",
        repr(e)
    )

    return json_error(
        "R2 multipart create failed: "
        + str(e),
        500
    )
```

# ============================================================

# R2 MULTIPART PRESIGNED URLS

# ============================================================

@app.route(
"/api/r2/multipart/urls",
methods=["POST"]
)
@admin_required
def api_r2_multipart_urls():

```
try:

    data = request.get_json(
        silent=True
    ) or {}


    key = str(
        data.get("key")
        or ""
    ).strip()

    upload_id = str(
        data.get("upload_id")
        or ""
    ).strip()


    parts = (
        data.get("part_numbers")
        or data.get("parts")
        or []
    )


    if not validate_r2_key(key):

        return json_error(
            "Invalid R2 key.",
            400
        )


    if not upload_id:

        return json_error(
            "upload_id missing.",
            400
        )


    if not isinstance(
        parts,
        list
    ):

        return json_error(
            "part_numbers must be an array.",
            400
        )


    clean_parts = []


    for part in parts:

        try:

            number = int(part)

        except Exception:

            continue


        if (
            number >= 1
            and number <= MAX_MULTIPART_PARTS
        ):

            clean_parts.append(
                number
            )


    clean_parts = sorted(
        set(clean_parts)
    )


    if not clean_parts:

        return json_error(
            "No valid part numbers.",
            400
        )


    if len(clean_parts) > MAX_MULTIPART_PARTS:

        return json_error(
            "Too many parts.",
            400
        )


    client = get_r2_client()


    urls = {}
    url_map = {}


    for part_number in clean_parts:

        url = (
            client.generate_presigned_url(
                "upload_part",
                Params={
                    "Bucket":
                        R2_BUCKET,

                    "Key":
                        key,

                    "UploadId":
                        upload_id,

                    "PartNumber":
                        part_number,
                },

                ExpiresIn=
                    PRESIGNED_EXPIRES,
            )
        )


        urls[str(part_number)] = url

        url_map[str(part_number)] = url


    return json_ok(
        urls=urls,
        url_map=url_map,
        part_urls=urls,
        part_size=PART_SIZE,
        parallel=PARALLEL_PARTS,
        expires=PRESIGNED_EXPIRES,
        total_parts=len(clean_parts),
    )


except Exception as e:

    print(
        "R2 MULTIPART URL ERROR:",
        repr(e)
    )

    return json_error(
        "R2 presigned URL generation failed: "
        + str(e),
        500
    )
```

# ============================================================

# R2 MULTIPART COMPLETE

# ============================================================

@app.route(
"/api/r2/multipart/complete",
methods=["POST"]
)
@admin_required
def api_r2_multipart_complete():

```
try:

    data = request.get_json(
        silent=True
    ) or {}


    key = str(
        data.get("key")
        or ""
    ).strip()

    upload_id = str(
        data.get("upload_id")
        or ""
    ).strip()

    parts = (
        data.get("parts")
        or []
    )


    if not validate_r2_key(key):

        return json_error(
            "Invalid R2 key.",
            400
        )


    if not upload_id:

        return json_error(
            "upload_id missing.",
            400
        )


    if not isinstance(
        parts,
        list
    ):

        return json_error(
            "parts must be an array.",
            400
        )


    clean_parts = []


    for part in parts:

        if not isinstance(
            part,
            dict
        ):
            continue


        number = (
            part.get("PartNumber")
            or part.get("part_number")
            or part.get("part")
        )

        etag = (
            part.get("ETag")
            or part.get("etag")
        )


        try:

            number = int(
                number
            )

        except Exception:

            continue


        if (
            number < 1
            or number > MAX_MULTIPART_PARTS
        ):
            continue


        if not etag:
            continue


        etag = str(
            etag
        ).strip()


        clean_parts.append(
            {
                "PartNumber":
                    number,

                "ETag":
                    etag,
            }
        )


    clean_parts.sort(
        key=lambda x:
            x["PartNumber"]
    )


    if not clean_parts:

        return json_error(
            "No valid completed parts.",
            400
        )


    # Check duplicate part numbers
    numbers = [
        x["PartNumber"]
        for x in clean_parts
    ]

    if len(numbers) != len(set(numbers)):

        return json_error(
            "Duplicate part numbers.",
            400
        )


    client = get_r2_client()


    result = (
        client.complete_multipart_upload(
            Bucket=R2_BUCKET,

            Key=key,

            UploadId=upload_id,

            MultipartUpload={
                "Parts":
                    clean_parts
            },
        )
    )


    # Verify object
    head = client.head_object(
        Bucket=R2_BUCKET,
        Key=key,
    )


    size = int(
        head.get(
            "ContentLength",
            0
        )
        or 0
    )


    if size <= 0:

        return json_error(
            "R2 object completed but size is zero.",
            502
        )


    if size > MAX_VIDEO_SIZE:

        # Remove oversized object
        try:
            r2_delete(key)
        except Exception:
            pass

        return json_error(
            "Video exceeds 4 GB.",
            400
        )


    return json_ok(
        key=key,
        size=size,
        etag=head.get("ETag"),
        public_url=r2_public_url(key),
        location=result.get(
            "Location"
        ),
    )


except Exception as e:

    print(
        "R2 MULTIPART COMPLETE ERROR:",
        repr(e)
    )

    return json_error(
        "R2 multipart complete failed: "
        + str(e),
        500
    )
```

# ============================================================

# R2 MULTIPART ABORT

# ============================================================

@app.route(
"/api/r2/multipart/abort",
methods=["POST"]
)
@admin_required
def api_r2_multipart_abort():

```
try:

    data = request.get_json(
        silent=True
    ) or {}


    key = str(
        data.get("key")
        or ""
    ).strip()

    upload_id = str(
        data.get("upload_id")
        or ""
    ).strip()


    if not validate_r2_key(key):

        return json_error(
            "Invalid R2 key.",
            400
        )


    if not upload_id:

        return json_error(
            "upload_id missing.",
            400
        )


    client = get_r2_client()


    client.abort_multipart_upload(
        Bucket=R2_BUCKET,
        Key=key,
        UploadId=upload_id,
    )


    return json_ok(
        message="Multipart upload aborted."
    )


except Exception as e:

    print(
        "R2 MULTIPART ABORT ERROR:",
        repr(e)
    )

    return json_error(
        "R2 multipart abort failed: "
        + str(e),
        500
    )
```

# ============================================================

# R2 DELETE API

# ============================================================

@app.route(
"/api/r2/delete",
methods=["POST"]
)
@admin_required
def api_r2_delete():

```
try:

    data = request.get_json(
        silent=True
    ) or {}


    key = str(
        data.get("key")
        or ""
    ).strip()


    if not validate_r2_key(key):

        return json_error(
            "Invalid R2 key.",
            400
        )


    r2_delete(key)


    return json_ok(
        key=key,
        message="R2 object deleted."
    )


except Exception as e:

    print(
        "R2 DELETE ERROR:",
        repr(e)
    )

    return json_error(
        "R2 delete failed: "
        + str(e),
        500
    )
```

# ============================================================

# SAVE MOVIE TO DATABASE

# ============================================================

@app.route(
"/api/movie/save",
methods=["POST"]
)
@admin_required
def api_movie_save():

```
conn = None

try:

    data = request.get_json(
        silent=True
    ) or {}


    title = str(
        data.get("title")
        or ""
    ).strip()

    category = str(
        data.get("category")
        or ""
    ).strip()

    description = str(
        data.get("description")
        or ""
    ).strip()

    video_key = str(
        data.get("video")
        or data.get("video_key")
        or ""
    ).strip()

    poster_key = str(
        data.get("poster")
        or data.get("poster_key")
        or ""
    ).strip()


    # ----------------------------------------------------
    # TITLE
    # ----------------------------------------------------

    if not title:

        return json_error(
            "Movie title is required.",
            400
        )


    # ----------------------------------------------------
    # VIDEO KEY
    # ----------------------------------------------------

    if not validate_video_key(
        video_key
    ):

        return json_error(
            "Invalid video key.",
            400
        )


    # ----------------------------------------------------
    # POSTER KEY
    # ----------------------------------------------------

    if poster_key:

        if not validate_poster_key(
            poster_key
        ):

            return json_error(
                "Invalid poster key.",
                400
            )


    # ----------------------------------------------------
    # VERIFY VIDEO EXISTS
    # ----------------------------------------------------

    try:

        video_head = r2_head(
            video_key
        )

    except Exception as e:

        print(
            "VIDEO VERIFY ERROR:",
            repr(e)
        )

        return json_error(
            "Video R2 में नहीं मिली.",
            400
        )


    video_size = int(
        video_head.get(
            "ContentLength",
            0
        )
        or 0
    )


    if video_size <= 0:

        return json_error(
            "Video R2 object empty है.",
            400
        )


    if video_size > MAX_VIDEO_SIZE:

        return json_error(
            "Video maximum 4 GB हो सकती है.",
            400
        )


    # ----------------------------------------------------
    # VERIFY POSTER
    # ----------------------------------------------------

    if poster_key:

        try:

            poster_head = r2_head(
                poster_key
            )

            poster_size = int(
                poster_head.get(
                    "ContentLength",
                    0
                )
                or 0
            )


            if poster_size <= 0:

                return json_error(
                    "Poster R2 object empty है.",
                    400
                )


            if poster_size > MAX_POSTER_SIZE:

                return json_error(
                    "Poster maximum 25 MB हो सकता है.",
                    400
                )


        except Exception as e:

            print(
                "POSTER VERIFY ERROR:",
                repr(e)
            )

            return json_error(
                "Poster R2 में नहीं मिली.",
                400
            )


    # ----------------------------------------------------
    # SAVE DATABASE
    # ----------------------------------------------------

    conn = get_db()

    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO movies(
            title,
            category,
            description,
            poster,
            video
        )
        VALUES(
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
            poster_key or None,
            video_key,
        )
    )


    row = cur.fetchone()

    movie_id = (
        row[0]
        if row
        else None
    )


    conn.commit()


    if not movie_id:

        return json_error(
            "Movie save failed: ID missing.",
            500
        )


    return json_ok(
        movie_id=movie_id,
        video_url=url_for(
            "stream_movie",
            movie_id=movie_id
        ),
        poster_url=(
            media_url(
                poster_key
            )
            if poster_key
            else None
        ),
    )


except Exception as e:

    if conn:

        try:
            conn.rollback()
        except Exception:
            pass


    print(
        "MOVIE SAVE ERROR:",
        repr(e)
    )


    return json_error(
        "Movie database save failed: "
        + str(e),
        500
    )


finally:

    if conn:
        conn.close()
```

# ============================================================

# ADS ADMIN

# ============================================================

@app.route(
"/admin/ads",
methods=["GET", "POST"]
)
@admin_required
def admin_ads():

```
if request.method == "POST":

    top = (
        request.form.get(
            "ad_top",
            ""
        )
    )

    player = (
        request.form.get(
            "ad_player",
            ""
        )
    )

    bottom = (
        request.form.get(
            "ad_bottom",
            ""
        )
    )


    set_setting(
        "ad_top",
        top
    )

    set_setting(
        "ad_player",
        player
    )

    set_setting(
        "ad_bottom",
        bottom
    )


    flash(
        "Ads settings saved.",
        "success"
    )


    return redirect(
        url_for(
            "admin_ads"
        )
    )


ads = get_ads()


return render_template(
    "admin_ads.html",
    ads=ads
)
```

# ============================================================

# ADS.TXT

# ============================================================

@app.route("/ads.txt")
def ads_txt():

```
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
```

# ============================================================

# R2 HEALTH

# ============================================================

@app.route("/r2-health")
def r2_health():

```
try:

    client = get_r2_client()

    client.head_bucket(
        Bucket=R2_BUCKET
    )


    return jsonify({
        "ok": True,
        "message": "R2 OK",
        "bucket": R2_BUCKET,
    })


except Exception as e:

    print(
        "R2 HEALTH ERROR:",
        repr(e)
    )

    return jsonify({
        "ok": False,
        "message": "R2 ERROR",
        "error": str(e),
    }), 500
```

# ============================================================

# DATABASE HEALTH

# ============================================================

@app.route("/db-health")
def db_health():

```
conn = None

try:

    conn = get_db()

    cur = conn.cursor()

    cur.execute(
        "SELECT 1"
    )

    cur.fetchone()


    return jsonify({
        "ok": True,
        "message": "Database OK",
    })


except Exception as e:

    print(
        "DB HEALTH ERROR:",
        repr(e)
    )

    return jsonify({
        "ok": False,
        "message": "Database ERROR",
        "error": str(e),
    }), 500


finally:

    if conn:
        conn.close()
```

# ============================================================

# APP HEALTH

# ============================================================

@app.route("/health")
def health():

```
return jsonify({
    "ok": True,
    "status": "ok",
    "app": "Tomesh Movies",
})
```

# ============================================================

# LEGACY POSTER ROUTE

# ============================================================

@app.route(
"/poster/[path:name](path:name)"
)
def poster_legacy(name):

```
key = name

if not key.startswith(
    POSTER_PREFIX
):
    key = (
        POSTER_PREFIX
        + name
    )


if not validate_poster_key(
    key
):

    abort(404)


try:

    return redirect(
        media_url(key)
    )

except Exception:

    abort(404)
```

# ============================================================

# LEGACY VIDEO ROUTE

# ============================================================

@app.route(
"/video/[path:name](path:name)"
)
def video_legacy(name):

```
key = name

if not key.startswith(
    VIDEO_PREFIX
):
    key = (
        VIDEO_PREFIX
        + name
    )


if not validate_video_key(
    key
):

    abort(404)


# Old direct video URLs are redirected
# to a presigned R2 URL for compatibility.
try:

    return redirect(
        r2_presigned_url(
            key,
            expires=PRESIGNED_EXPIRES
        )
    )

except Exception:

    abort(404)
```

# ============================================================

# 413

# ============================================================

@app.errorhandler(413)
def too_large(error):

```
return Response(
    "File too large. Maximum 4 GB.",
    status=413,
    mimetype="text/plain"
)
```

# ============================================================

# 404

# ============================================================

@app.errorhandler(404)
def page_not_found(error):

```
return Response(
    """
    <!doctype html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>404 | Tomesh Movies</title>
        <style>
            body{
                margin:0;
                min-height:100vh;
                display:flex;
                align-items:center;
                justify-content:center;
                background:#080808;
                color:#fff;
                font-family:Arial,sans-serif;
                text-align:center;
            }
            h1{
                font-size:60px;
                margin:0 0 10px;
            }
            p{
                color:#999;
            }
            a{
                color:#ffc400;
                text-decoration:none;
            }
        </style>
    </head>
    <body>
        <div>
            <h1>404</h1>
            <p>Page not found.</p>
            <a href="/">← Back to Tomesh Movies</a>
        </div>
    </body>
    </html>
    """,
    status=404,
    mimetype="text/html"
)
```

# ============================================================

# 500

# ============================================================

@app.errorhandler(500)
def internal_error(error):

```
print(
    "500 ERROR:",
    repr(error)
)

return Response(
    """
    <!doctype html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>500 | Tomesh Movies</title>
        <style>
            body{
                margin:0;
                min-height:100vh;
                display:flex;
                align-items:center;
                justify-content:center;
                background:#080808;
                color:#fff;
                font-family:Arial,sans-serif;
                text-align:center;
            }
            h1{
                color:#ff4d6d;
                font-size:50px;
                margin:0 0 10px;
            }
            p{
                color:#aaa;
            }
            a{
                color:#ffc400;
                text-decoration:none;
            }
        </style>
    </head>
    <body>
        <div>
            <h1>500</h1>
            <p>Something went wrong.</p>
            <a href="/">← Back to Tomesh Movies</a>
        </div>
    </body>
    </html>
    """,
    status=500,
    mimetype="text/html"
)
```

# ============================================================

# STARTUP

# ============================================================

try:

```
if DATABASE_URL:

    init_db()

    print(
        "Database initialized successfully."
    )

else:

    print(
        "WARNING: DATABASE_URL is missing."
    )
```

except Exception as e:

```
print(
    "Database initialization error:",
    repr(e)
)
```

# ============================================================

# LOCAL RUN

# ============================================================

if **name** == "**main**":

```
port = int(
    os.environ.get(
        "PORT",
        "5000"
    )
)

app.run(
    host="0.0.0.0",
    port=port,
    debug=False
)
```
