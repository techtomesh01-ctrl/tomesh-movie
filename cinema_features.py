from datetime import datetime
from flask import request, session, redirect, url_for, render_template, flash, jsonify

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
    return redirect(url_for('movie', movie_id=movie_id))


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
    return redirect(url_for('movie', movie_id=movie_id))


@app.route('/request-movie', methods=['GET','POST'])
@customer_login_required
def cw_request_movie():
    _ensure_tables()
    cid = _customer_id()
    if request.method == 'POST':
        title = ' '.join((request.form.get('title') or '').split())[:180]
        normalized = title.casefold()
        if len(title) < 2:
            flash('Movie name thoda clearly likho.', 'error')
            return redirect(url_for('cw_request_movie'))
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
        flash('Movie demand admin ko bhej di gayi. ❤️', 'success')
        return redirect(url_for('cw_request_movie'))

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
