from datetime import datetime
import time
from flask import request, session, redirect, url_for, render_template, flash, jsonify, Response

# This module is imported at the very end of app.py, after Flask and DB helpers exist.
from app import app, get_db, customer_login_required, admin_required


def _ensure_tables():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cw_movie_likes (
                id SERIAL PRIMARY KEY,
                movie_id INTEGER NOT NULL,
                customer_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(movie_id, customer_id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cw_movie_reports (
                id SERIAL PRIMARY KEY,
                movie_id INTEGER NOT NULL,
                customer_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                details TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'OPEN',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                resolved_at TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cw_movie_requests (
                id SERIAL PRIMARY KEY,
                customer_id TEXT,
                requested_title TEXT NOT NULL,
                normalized_title TEXT NOT NULL,
                votes INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'REQUESTED',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(customer_id, normalized_title)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cw_support_tickets (
                id SERIAL PRIMARY KEY,
                customer_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                admin_reply TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cw_notifications (
                id SERIAL PRIMARY KEY,
                customer_id TEXT NOT NULL,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'INFO',
                is_read BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cw_reports_status ON cw_movie_reports(status, created_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cw_requests_votes ON cw_movie_requests(votes DESC, updated_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cw_tickets_status ON cw_support_tickets(status, updated_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cw_notifications_user ON cw_notifications(customer_id, is_read, created_at DESC)")
        conn.commit()
    finally:
        conn.close()


def _customer_id():
    return str(session.get('customer_id') or '')


@app.route('/movie/<int:movie_id>/cw-like', methods=['POST'])
@customer_login_required
def cw_like(movie_id):
    _ensure_tables()
    cid = _customer_id()
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT 1 FROM movies WHERE id=%s', (movie_id,))
        if not cur.fetchone():
            flash('Movie not found.', 'error')
            return redirect(url_for('member_home'))
        cur.execute('SELECT 1 FROM cw_movie_likes WHERE movie_id=%s AND customer_id=%s', (movie_id, cid))
        if cur.fetchone():
            cur.execute('DELETE FROM cw_movie_likes WHERE movie_id=%s AND customer_id=%s', (movie_id, cid))
        else:
            cur.execute('INSERT INTO cw_movie_likes(movie_id,customer_id) VALUES(%s,%s) ON CONFLICT DO NOTHING', (movie_id, cid))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for('movie_page', movie_id=movie_id))


@app.route('/movie/<int:movie_id>/cw-report', methods=['POST'])
@customer_login_required
def cw_report(movie_id):
    _ensure_tables()
    reason = (request.form.get('reason') or 'Other').strip()[:80]
    details = (request.form.get('details') or '').strip()[:1000]
    allowed = {'Video not playing','Poor video quality','Audio problem','Wrong movie','Subtitle problem','Other'}
    if reason not in allowed:
        reason = 'Other'
    cid = _customer_id()
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT id FROM movies WHERE id=%s', (movie_id,))
        if not cur.fetchone():
            flash('Movie not found.', 'error')
            return redirect(url_for('member_home'))
        cur.execute('''INSERT INTO cw_movie_reports(movie_id,customer_id,reason,details) VALUES(%s,%s,%s,%s)''', (movie_id,cid,reason,details))
        conn.commit()
    finally:
        conn.close()
    flash('Report submitted to CINEMA WORLD support.', 'success')
    return redirect(url_for('movie_page', movie_id=movie_id))


@app.route('/request-movie', methods=['GET','POST'])
def cw_request_movie():
    _ensure_tables()
    cid = _customer_id() or None
    from_homepage = request.form.get('source') == 'homepage'
    from_movie_page = request.form.get('source') == 'moviepage'
    try:
        return_movie_id = int(request.form.get('movie_id') or 0)
    except (TypeError, ValueError):
        return_movie_id = 0
    def request_return_target():
        if from_homepage:
            return url_for('home')
        if from_movie_page and return_movie_id > 0:
            return url_for('movie_page', movie_id=return_movie_id)
        return url_for('cw_request_movie')
    if request.method == 'POST':
        # Honeypot and a small per-session cooldown help reduce automated spam.
        if (request.form.get('website') or '').strip():
            return redirect(url_for('home'))
        now = time.time()
        last_submit = float(session.get('movie_request_last_submit') or 0)
        if now - last_submit < 10:
            flash('Thoda ruk kar doosri movie request bhejein.', 'error')
            return redirect(request_return_target())
        title = ' '.join((request.form.get('title') or '').split())[:180]
        normalized = title.casefold()
        if len(title) < 2:
            flash('Movie name thoda clearly likho.', 'error')
            return redirect(request_return_target())
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute('''
                INSERT INTO cw_movie_requests(customer_id,requested_title,normalized_title)
                VALUES(%s,%s,%s)
                ON CONFLICT(customer_id,normalized_title) DO UPDATE SET updated_at=NOW()
            ''', (cid,title,normalized))
            conn.commit()
        finally:
            conn.close()
        session['movie_request_last_submit'] = now
        flash('Movie request admin ko bhej di gayi. ❤️', 'success')
        return redirect(request_return_target())

    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute('''SELECT requested_title, SUM(votes) AS votes, MAX(updated_at) AS updated_at
                       FROM cw_movie_requests GROUP BY normalized_title, requested_title
                       ORDER BY votes DESC, updated_at DESC LIMIT 100''')
        requests_list = cur.fetchall()
    finally:
        conn.close()
    return render_template('cinema_requests.html', requests_list=requests_list)


@app.route('/cinema-notifications')
@customer_login_required
def cw_notifications():
    _ensure_tables()
    cid = _customer_id()
    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute('SELECT * FROM cw_notifications WHERE customer_id=%s ORDER BY id DESC LIMIT 100', (cid,))
        rows = cur.fetchall()
        cur.execute('UPDATE cw_notifications SET is_read=TRUE WHERE customer_id=%s', (cid,))
        conn.commit()
    finally:
        conn.close()
    return render_template('cinema_notifications.html', notifications=rows)


@app.route('/cinema-support', methods=['GET','POST'])
@customer_login_required
def cw_support():
    _ensure_tables()
    cid = _customer_id()
    if request.method == 'POST':
        subject = ' '.join((request.form.get('subject') or '').split())[:160]
        message = (request.form.get('message') or '').strip()[:3000]
        if len(subject) < 2 or len(message) < 5:
            flash('Subject aur message complete likho.', 'error')
            return redirect(url_for('cw_support'))
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute('INSERT INTO cw_support_tickets(customer_id,subject,message) VALUES(%s,%s,%s)', (cid,subject,message))
            conn.commit()
        finally:
            conn.close()
        flash('Support ticket create ho gaya.', 'success')
        return redirect(url_for('cw_support'))
    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute('SELECT * FROM cw_support_tickets WHERE customer_id=%s ORDER BY id DESC LIMIT 50', (cid,))
        tickets = cur.fetchall()
    finally:
        conn.close()
    return render_template('cinema_support.html', tickets=tickets)


@app.route('/cinema-stats')
@customer_login_required
def cw_stats():
    _ensure_tables()
    cid = _customer_id()
    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        queries = {
            'payments': "SELECT COUNT(*) AS n FROM payment_orders WHERE customer_id=%s AND status='PAID'",
            'my_list': "SELECT COUNT(*) AS n FROM customer_movie_list WHERE customer_id=%s",
            'ratings': "SELECT COUNT(*) AS n FROM movie_ratings WHERE customer_id=%s",
            'likes': "SELECT COUNT(*) AS n FROM cw_movie_likes WHERE customer_id=%s",
            'requests': "SELECT COUNT(*) AS n FROM cw_movie_requests WHERE customer_id=%s",
            'tickets': "SELECT COUNT(*) AS n FROM cw_support_tickets WHERE customer_id=%s",
        }
        stats={}
        for key,q in queries.items():
            try:
                cur.execute(q,(cid,)); stats[key]=int((cur.fetchone() or {}).get('n') or 0)
            except Exception:
                conn.rollback(); stats[key]=0
        cur.execute('SELECT COUNT(*) AS n FROM movie_watch_history WHERE customer_id=%s', (cid,))
        stats['watch_events']=int((cur.fetchone() or {}).get('n') or 0)
    finally:
        conn.close()
    return render_template('cinema_stats.html', stats=stats)


@app.route('/admin/feature-center')
@admin_required
def cw_admin_feature_center():
    _ensure_tables()
    conn=get_db(dict_rows=True)
    try:
        cur=conn.cursor()
        cur.execute('''SELECT r.*, m.title FROM cw_movie_reports r LEFT JOIN movies m ON m.id=r.movie_id ORDER BY CASE WHEN r.status='OPEN' THEN 0 ELSE 1 END, r.id DESC LIMIT 300''')
        reports=cur.fetchall()
        cur.execute('''SELECT requested_title, SUM(votes) AS votes, MAX(updated_at) AS updated_at FROM cw_movie_requests GROUP BY normalized_title, requested_title ORDER BY votes DESC, updated_at DESC LIMIT 100''')
        requests_list=cur.fetchall()
        cur.execute('SELECT * FROM cw_support_tickets ORDER BY CASE WHEN status=\'OPEN\' THEN 0 ELSE 1 END, id DESC LIMIT 200')
        tickets=cur.fetchall()
    finally:
        conn.close()
    return render_template('cinema_feature_center.html', reports=reports, requests_list=requests_list, tickets=tickets)


@app.route('/admin/feature-center/report/<int:report_id>/close', methods=['POST'])
@admin_required
def cw_close_report(report_id):
    _ensure_tables()
    conn=get_db()
    try:
        cur=conn.cursor(); cur.execute("UPDATE cw_movie_reports SET status='RESOLVED', resolved_at=NOW() WHERE id=%s", (report_id,)); conn.commit()
    finally: conn.close()
    return redirect(url_for('cw_admin_feature_center'))


@app.route('/admin/feature-center/ticket/<int:ticket_id>/reply', methods=['POST'])
@admin_required
def cw_reply_ticket(ticket_id):
    _ensure_tables()
    reply=(request.form.get('reply') or '').strip()[:3000]
    conn=get_db()
    try:
        cur=conn.cursor()
        cur.execute('UPDATE cw_support_tickets SET admin_reply=%s,status=\'REPLIED\',updated_at=NOW() WHERE id=%s RETURNING customer_id', (reply,ticket_id))
        row=cur.fetchone()
        if row and reply:
            cur.execute('INSERT INTO cw_notifications(customer_id,title,message,kind) VALUES(%s,%s,%s,%s)', (row[0],'Support reply received',reply,'SUPPORT'))
        conn.commit()
    finally: conn.close()
    return redirect(url_for('cw_admin_feature_center'))


@app.route('/admin/feature-center/notify', methods=['POST'])
@admin_required
def cw_admin_notify():
    _ensure_tables()
    customer_id=(request.form.get('customer_id') or '').strip()
    title=' '.join((request.form.get('title') or '').split())[:160]
    message=(request.form.get('message') or '').strip()[:1000]
    if customer_id and title and message:
        conn=get_db()
        try:
            cur=conn.cursor(); cur.execute('INSERT INTO cw_notifications(customer_id,title,message,kind) VALUES(%s,%s,%s,%s)', (customer_id,title,message,'ADMIN')); conn.commit()
        finally: conn.close()
    return redirect(url_for('cw_admin_feature_center'))


# ============================================================
# ADSENSE / PUBLIC INFORMATION PAGES
# These routes are deliberately isolated in the optional feature module so
# the existing R2, Cashfree, OTP, login, movie and admin logic is untouched.
# ============================================================

@app.route('/about')
def about():
    return render_template('about.html')


@app.route('/contact')
def contact():
    return render_template('contact.html')


@app.route('/privacy-policy')
def privacy_policy():
    return render_template('privacy_policy.html')


@app.route('/terms')
def terms():
    return render_template('terms.html')


@app.route('/disclaimer')
def disclaimer():
    return render_template('disclaimer.html')


@app.route('/copyright')
def copyright_policy():
    return render_template('copyright.html')


@app.route('/cookie-policy')
def cookie_policy():
    return render_template('cookie_policy.html')


@app.route('/refund-policy')
def refund_policy():
    return render_template('refund_policy.html')


@app.route('/content-rights')
def content_rights():
    return render_template('content_rights.html')


# Use a fixed HTTPS origin rather than request-derived URLs. This avoids
# advertising http:// canonical/sitemap links behind Render's TLS proxy.
SEO_SITE_URL = "https://tomesh-movie.onrender.com"


@app.route('/title/<int:movie_id>')
def public_movie_page(movie_id):
    """Public, crawlable title details; the existing /movie/<id> player stays private."""
    conn = get_db(dict_rows=True)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT *
            FROM movies
            WHERE id = %s
              AND UPPER(COALESCE(status, '')) = 'LIVE'
              AND UPPER(COALESCE(content_type, 'MOVIE')) IN ('MOVIE', 'DRAMA')
            LIMIT 1
            """,
            (movie_id,),
        )
        row = cur.fetchone()
        cur.close()
    finally:
        conn.close()

    if not row:
        from flask import abort
        abort(404)

    movie = dict(row)
    try:
        movie["poster_url"] = media_url(movie.get("poster")) if movie.get("poster") else None
    except Exception:
        movie["poster_url"] = None

    return render_template("public_movie.html", movie=movie)


@app.route('/robots.txt')
def robots_txt():
    # Public landing pages and assets remain crawlable. Account, admin,
    # API, payment, and the existing login-gated playback route stay private.
    body = "\n".join([
        "User-agent: *",
        "Allow: /",
        "Disallow: /admin",
        "Disallow: /login",
        "Disallow: /logout",
        "Disallow: /api/",
        "Disallow: /member/",
        "Disallow: /creator/",
        "Disallow: /cinema-support",
        "Disallow: /cinema-notifications",
        "Disallow: /cinema-stats",
        "Disallow: /payment/",
        "Disallow: /movie/",
        "Disallow: /share/movie/",
        "Sitemap: " + SEO_SITE_URL + "/sitemap.xml",
        "",
    ])
    response = Response(body, mimetype='text/plain')
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@app.route('/sitemap.xml')
def sitemap_xml():
    # Only include public pages and published title landing pages. Do not
    # expose private /movie/<id> player URLs, pending uploads, or admin paths.
    urls = [
        SEO_SITE_URL + url_for('home'),
        SEO_SITE_URL + url_for('about'),
        SEO_SITE_URL + url_for('contact'),
        SEO_SITE_URL + url_for('privacy_policy'),
        SEO_SITE_URL + url_for('terms'),
        SEO_SITE_URL + url_for('disclaimer'),
        SEO_SITE_URL + url_for('copyright_policy'),
        SEO_SITE_URL + url_for('cookie_policy'),
        SEO_SITE_URL + url_for('refund_policy'),
        SEO_SITE_URL + url_for('content_rights'),
        SEO_SITE_URL + url_for('dramas_page'),
        SEO_SITE_URL + url_for('cw_request_movie'),
    ]

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
            ORDER BY id DESC
            """
        )
        for row in cur.fetchall():
            urls.append(SEO_SITE_URL + url_for('public_movie_page', movie_id=int(row["id"])))
        cur.close()
    except Exception as exc:
        # Keep the sitemap available during a temporary database interruption.
        print("SITEMAP MOVIE QUERY ERROR:", repr(exc))
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn is not None:
            conn.close()

    # Stable order and no duplicates make the sitemap deterministic.
    urls = list(dict.fromkeys(urls))
    from xml.sax.saxutils import escape as xml_escape
    xml = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for url in urls:
        xml.append("  <url><loc>" + xml_escape(url) + "</loc></url>")
    xml.append("</urlset>")
    response = Response("\n".join(xml), mimetype='application/xml')
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


# Google Search Console HTML-file verification endpoint.
@app.route('/googleaa2c7aea3fca410e.html')
def google_search_console_verification():
    return Response('google-site-verification: googleaa2c7aea3fca410e.html\n', mimetype='text/html')
