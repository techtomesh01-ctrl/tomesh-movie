"""Public SEO endpoints and crawlable metadata pages for CINEMA WORLD."""
from xml.sax.saxutils import escape

from flask import Response, abort, render_template, url_for

from app import app, get_db, media_url

SITE_ORIGIN = "https://tomesh-movie.onrender.com"
PUBLIC_PATHS = (
    "/", "/dramas", "/terms", "/privacy-policy", "/cookie-policy",
    "/refund-policy", "/about", "/contact", "/content-rights",
    "/copyright", "/disclaimer",
)


def _robots_txt():
    body = """User-agent: *
Allow: /
Disallow: /admin
Disallow: /api/
Disallow: /login
Disallow: /logout
Disallow: /account
Disallow: /profile
Disallow: /user-details
Disallow: /member
Disallow: /membership
Disallow: /payment-history
Disallow: /payments
Disallow: /payment/
Disallow: /earnings
Disallow: /creator/
Disallow: /notifications/
Disallow: /help-support
Disallow: /movie/
Disallow: /share/movie/
Sitemap: https://tomesh-movie.onrender.com/sitemap.xml
"""
    response = Response(body, mimetype="text/plain; charset=utf-8")
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


def _sitemap_xml():
    """Return canonical public URLs and only live movie landing pages."""
    urls = [SITE_ORIGIN + path for path in PUBLIC_PATHS]
    conn = None
    try:
        conn = get_db(dict_rows=True)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id
            FROM movies
            WHERE UPPER(COALESCE(status, '')) = 'LIVE'
              AND UPPER(COALESCE(content_type, 'MOVIE')) IN ('MOVIE', 'DRAMA')
            ORDER BY id ASC
            """
        )
        urls.extend(
            SITE_ORIGIN + url_for("public_movie_page", movie_id=int(row["id"]))
            for row in cur.fetchall()
        )
        cur.close()
    except Exception as exc:
        print("SITEMAP MOVIE QUERY ERROR:", repr(exc))
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn is not None:
            conn.close()

    entries = "".join("<url><loc>%s</loc></url>" % escape(url) for url in dict.fromkeys(urls))
    xml = '<?xml version="1.0" encoding="UTF-8"?>' + (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + entries + "</urlset>"
    )
    response = Response(xml, mimetype="application/xml; charset=utf-8")
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


# Reuse the original URL rules without duplicate registrations.
app.view_functions["public_robots_txt"] = _robots_txt
app.view_functions["public_sitemap_xml"] = _sitemap_xml
