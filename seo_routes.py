"""Public SEO endpoints for CINEMA WORLD.

The existing app registers the endpoint URLs; this module replaces the
robots/sitemap view functions so we don't add duplicate URL rules. The
catalogue pages expose metadata only; /movie/<id> playback stays protected.
"""
from xml.sax.saxutils import escape

from flask import Response, url_for

from app import app, get_db

SITE_ORIGIN = "https://tomesh-movie.onrender.com"


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
Disallow: /member/
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
    """Generate a sitemap using real public routes and live movie records."""
    static_endpoints = (
        "home",
        "about",
        "contact",
        "privacy_policy",
        "terms",
        "disclaimer",
        "copyright_policy",
        "cookie_policy",
        "refund_policy",
        "content_rights",
        "dramas_page",
        "cw_request_movie",
    )
    urls = []
    for endpoint in static_endpoints:
        try:
            urls.append(SITE_ORIGIN + url_for(endpoint))
        except Exception as exc:
            # An optional route can be absent in a future app version; do not
            # make the entire sitemap unavailable because of that one route.
            print("SITEMAP OPTIONAL ROUTE SKIPPED:", endpoint, repr(exc))

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

    unique_urls = dict.fromkeys(urls)
    entries = "".join(
        "<url><loc>%s</loc></url>" % escape(url)
        for url in unique_urls
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + entries
        + "</urlset>"
    )
    response = Response(xml, mimetype="application/xml; charset=utf-8")
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


# The app's original URL rules already exist. Replacing these view functions
# keeps one route per endpoint and ensures the dynamic sitemap is used.
app.view_functions["public_robots_txt"] = _robots_txt
app.view_functions["public_sitemap_xml"] = _sitemap_xml
