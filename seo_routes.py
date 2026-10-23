"""Public SEO routes for CINEMA WORLD.

Public title pages expose only catalogue metadata; playback remains protected
by the existing /movie/<id> login/access checks.
"""
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from flask import Response, abort, render_template, url_for

from app import app, get_db, media_url

SITE_ORIGIN = "https://tomesh-movie.onrender.com"

PUBLIC_STATIC_PATHS = (
    "/",
    "/dramas",
    "/terms",
    "/privacy-policy",
    "/cookie-policy",
    "/refund-policy",
    "/about",
    "/contact",
    "/content-rights",
    "/copyright",
    "/disclaimer",
)


@app.route("/title/<int:movie_id>", methods=["GET"])
def public_movie_title(movie_id):
    """Public, crawlable movie information without exposing the video URL."""
    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, title, category, description, poster, content_type
            FROM movies
            WHERE id = %s
              AND COALESCE(video, '') <> ''
            """,
            (movie_id,),
        )
        movie = cur.fetchone()
        cur.close()
    finally:
        conn.close()

    if not movie:
        abort(404)

    movie = dict(movie)
    try:
        movie["poster_url"] = media_url(movie.get("poster")) if movie.get("poster") else None
    except Exception:
        movie["poster_url"] = None

    return render_template("public_movie_title.html", movie=movie)


def _robots_txt():
    body = """User-agent: *
Allow: /
Disallow: /admin
Disallow: /api/
Disallow: /login
Disallow: /logout
Disallow: /movie/
Disallow: /share/movie/
Disallow: /account
Disallow: /profile
Disallow: /user-details
Disallow: /member
Disallow: /membership
Disallow: /payment-history
Disallow: /payments
Disallow: /earnings
Disallow: /creator/
Disallow: /notifications/
Disallow: /help-support
Sitemap: https://tomesh-movie.onrender.com/sitemap.xml
"""
    return Response(body, mimetype="text/plain; charset=utf-8")


def _sitemap_xml():
    """Generate the sitemap from real, available public pages and movie rows."""
    urls = list(PUBLIC_STATIC_PATHS)
    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id
            FROM movies
            WHERE COALESCE(video, '') <> ''
            ORDER BY id ASC
            """
        )
        urls.extend("/title/%s" % int(row["id"]) for row in cur.fetchall())
        cur.close()
    finally:
        conn.close()

    unique_urls = dict.fromkeys(urls)
    entries = "".join(
        "<url><loc>%s</loc></url>" % escape(SITE_ORIGIN + path)
        for path in unique_urls
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + entries
        + "</urlset>"
    )
    return Response(xml, mimetype="application/xml; charset=utf-8")


# Replace the earlier inline static handlers without creating duplicate rules.
app.view_functions["public_robots_txt"] = _robots_txt
app.view_functions["public_sitemap_xml"] = _sitemap_xml
