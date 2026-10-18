"""Fast, offline smoke tests for the CINEMA WORLD Flask application.

These tests intentionally do not connect to production services or mutate data.
"""
import re
import unittest
from unittest import mock
from pathlib import Path

import app as cinema_app


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"


class ApplicationSmokeTests(unittest.TestCase):
    def test_core_feature_endpoints_are_registered(self):
        required = {
            "home",
            "request_movie",
            "cw_request_movie",
            "movie_page",
            "admin",
            "admin_creator_studio",
            "admin_review_creator_submission",
            "creator_upload",
        }
        missing = required.difference(cinema_app.app.view_functions)
        self.assertFalse(missing, f"Missing required Flask endpoints: {sorted(missing)}")

    def test_creator_publish_requires_rights_quality_and_evidence(self):
        client = cinema_app.app.test_client()
        with client.session_transaction() as session:
            session["admin_logged_in"] = True
            session["admin_user"] = "test-reviewer"

        # Invalid approval must be rejected before opening the database or publishing.
        with mock.patch.object(cinema_app, "get_db", side_effect=AssertionError("database should not be touched")):
            response = client.post(
                "/admin/creator-submission/123/approve",
                data={"rights_verified": "yes", "quality_checked": "yes", "rights_evidence": "short"},
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as session:
            flashes = session.get("_flashes", [])
        self.assertTrue(any("Publish blocked" in message for _, message in flashes))

    def test_invalid_user_admin_action_is_rejected_without_database_write(self):
        client = cinema_app.app.test_client()
        with client.session_transaction() as session:
            session["admin_logged_in"] = True
            session["admin_user"] = "test-reviewer"

        with mock.patch.object(cinema_app, "get_db", side_effect=AssertionError("database should not be touched")):
            response = client.post(
                "/admin/user-restriction/123",
                data={"action": "delete_everything"},
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 400)

    def test_security_center_includes_admin_action_audit(self):
        source = (TEMPLATES / "admin.html").read_text(encoding="utf-8")
        self.assertIn("Admin Action Audit", source)
        self.assertIn("admin_action_audit", source)
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS admin_action_audit", app_source)
        self.assertIn("USER_UNBLOCKED", app_source)
        self.assertIn("PREMIUM_REVOKED", app_source)

    def test_creator_review_page_has_audit_history_and_rights_notice(self):
        source = (TEMPLATES / "admin_creator_studio.html").read_text(encoding="utf-8")
        self.assertIn("Decision audit history", source)
        self.assertIn("Rights evidence/reference", source)
        self.assertIn("not independent proof", source)

    def test_templates_compile(self):
        self.assertTrue(TEMPLATES.is_dir(), "templates directory is missing")
        template_names = sorted(
            path.relative_to(TEMPLATES).as_posix()
            for path in TEMPLATES.rglob("*.html")
        )
        self.assertTrue(template_names, "No HTML templates were found")
        for name in template_names:
            with self.subTest(template=name):
                cinema_app.app.jinja_env.get_template(name)

    def test_literal_template_url_for_endpoints_exist(self):
        missing = []
        pattern = re.compile(r"url_for\(\s*['\"]([^'\"]+)['\"]")
        for path in TEMPLATES.rglob("*.html"):
            source = path.read_text(encoding="utf-8")
            for endpoint in pattern.findall(source):
                if endpoint != "static" and endpoint not in cinema_app.app.view_functions:
                    missing.append(f"{path.relative_to(ROOT)} -> {endpoint}")
        self.assertFalse(
            missing,
            "Templates refer to unregistered Flask endpoints:\n" + "\n".join(sorted(set(missing))),
        )


if __name__ == "__main__":
    unittest.main()
