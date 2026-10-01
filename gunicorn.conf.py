# ============================================================
# CINEMA WORLD - ACCESS FLOW COMPATIBILITY LAYER
#
# Keeps the existing app/payment code intact while changing the
# customer access policy to:
#   1) Login -> Member Home (no activation payment)
#   2) First login gets one 24-hour free watch trial
#   3) After trial expiry, each movie needs its own ₹1 / 24h watch
#   4) Premium/download/share behaviour remains unchanged
# ============================================================

import re
from datetime import datetime, timedelta


def _safe_redirect_to_member(response, app_mod):
    """Convert old activation checkout redirects to Member Home."""
    try:
        location = response.headers.get("Location", "")
        if "membership_checkout" in location or "/membership" in location:
            from flask import redirect, url_for
            return redirect(url_for("member_home"))
    except Exception:
        pass
    return response


def post_worker_init(worker):
    import app as app_mod
    from flask import redirect, url_for, session

    flask_app = app_mod.app

    # ------------------------------------------------------------
    # One-time free-trial storage
    # ------------------------------------------------------------
    conn = app_mod.get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS customer_free_trials (
                customer_id TEXT PRIMARY KEY,
                started_at TIMESTAMP NOT NULL DEFAULT NOW(),
                expires_at TIMESTAMP NOT NULL
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_customer_free_trials_expiry
            ON customer_free_trials(expires_at)
        """)
        conn.commit()
        cur.close()
    finally:
        conn.close()

    # ------------------------------------------------------------
    # Start the free trial once, on the first authenticated request.
    # A persistent table prevents a new 24-hour trial on every login.
    # ------------------------------------------------------------
    def start_free_trial_if_needed():
        if not session.get("customer_logged_in"):
            return
        customer_id = str(session.get("customer_id") or "").strip()
        if not customer_id:
            return

        conn = None
        try:
            conn = app_mod.get_db(dict_rows=True)
            cur = conn.cursor()
            cur.execute(
                "SELECT customer_id FROM customer_free_trials WHERE customer_id=%s LIMIT 1",
                (customer_id,),
            )
            if cur.fetchone():
                cur.close()
                return

            # Customers who already paid should not receive a retroactive
            # free-trial grant.
            cur.execute(
                """
                SELECT 1
                FROM payment_orders
                WHERE customer_id=%s AND status='PAID'
                LIMIT 1
                """,
                (customer_id,),
            )
            already_paid = bool(cur.fetchone())
            if already_paid:
                cur.close()
                return

            now = datetime.now()
            expires = now + timedelta(hours=24)
            cur.execute(
                """
                INSERT INTO customer_free_trials(customer_id,started_at,expires_at)
                VALUES(%s,%s,%s)
                ON CONFLICT(customer_id) DO NOTHING
                """,
                (customer_id, now, expires),
            )
            cur.execute(
                """
                INSERT INTO customer_access
                (customer_id,movie_id,watch_until,download_until,premium_until,share_until)
                VALUES(%s,NULL,%s,NULL,NULL,NULL)
                ON CONFLICT DO NOTHING
                """,
                (customer_id, expires),
            )
            conn.commit()
            cur.close()
        except Exception as exc:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            print("FREE TRIAL INIT ERROR:", repr(exc))
        finally:
            if conn:
                conn.close()

    flask_app.before_request(start_free_trial_if_needed)

    # ------------------------------------------------------------
    # After the 24-hour free period, WATCH is per movie.
    # Other payment types continue using the original implementation.
    # ------------------------------------------------------------
    original_grant_access = app_mod.grant_access

    def grant_access_per_movie(customer_id, movie_id, payment_type):
        if str(payment_type or "").lower() != "watch" or not movie_id:
            return original_grant_access(customer_id, movie_id, payment_type)

        conn = app_mod.get_db()
        try:
            cur = conn.cursor()
            now = datetime.now()
            new_until = now + timedelta(hours=24)
            cur.execute(
                """
                SELECT id, watch_until
                FROM customer_access
                WHERE customer_id=%s AND movie_id=%s
                FOR UPDATE
                """,
                (customer_id, movie_id),
            )
            row = cur.fetchone()
            if row:
                old_until = row[1]
                if old_until and old_until > new_until:
                    new_until = old_until
                cur.execute(
                    "UPDATE customer_access SET watch_until=%s WHERE id=%s",
                    (new_until, row[0]),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO customer_access
                    (customer_id,movie_id,watch_until,download_until,premium_until,share_until)
                    VALUES(%s,%s,%s,NULL,NULL,NULL)
                    """,
                    (customer_id, movie_id, new_until),
                )
            conn.commit()
            cur.close()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    app_mod.grant_access = grant_access_per_movie

    # ------------------------------------------------------------
    # Login flow: never force the old activation checkout.
    # ------------------------------------------------------------
    for endpoint_name in ("customer_password_login", "user_details"):
        original = flask_app.view_functions.get(endpoint_name)
        if not original:
            continue

        def make_login_wrapper(fn):
            def wrapped(*args, **kwargs):
                response = fn(*args, **kwargs)
                return _safe_redirect_to_member(response, app_mod)
            wrapped.__name__ = getattr(fn, "__name__", "wrapped") + "_free_access_flow"
            return wrapped

        flask_app.view_functions[endpoint_name] = make_login_wrapper(original)

    # OTP verification currently sends a new user to /user-details.
    # Send them straight to Member Home instead; the trial is initialized
    # by before_request on the redirected request.
    original_otp = flask_app.view_functions.get("verify_mobile_otp_route")
    if original_otp:
        def verify_otp_and_open_member(*args, **kwargs):
            response = original_otp(*args, **kwargs)
            try:
                location = response.headers.get("Location", "")
                if "/user-details" in location:
                    return redirect(url_for("member_home"))
            except Exception:
                pass
            return _safe_redirect_to_member(response, app_mod)
        verify_otp_and_open_member.__name__ = "verify_mobile_otp_route_free_access_flow"
        flask_app.view_functions["verify_mobile_otp_route"] = verify_otp_and_open_member

    # ------------------------------------------------------------
    # Update customer-facing wording without changing the existing
    # movie template/payment UI structure.
    # ------------------------------------------------------------
    original_render_template = app_mod.render_template

    def render_template_with_watch_wording(template_name, *args, **kwargs):
        rendered = original_render_template(template_name, *args, **kwargs)
        if template_name == "movie.html":
            rendered = rendered.replace(
                "24 hours movie watch access",
                "24 hours watch access for this movie",
            )
            rendered = rendered.replace(
                "One ₹1 Watch payment unlocks the whole account for 24 hours.",
                "₹1 Watch payment unlocks this movie for 24 hours.",
            )
        return rendered

    app_mod.render_template = render_template_with_watch_wording

    print("CINEMA WORLD access flow enabled: 24h first-login trial + per-movie watch payment")
