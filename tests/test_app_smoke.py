"""Fast, offline smoke tests for the CINEMA WORLD Flask application.

These tests intentionally do not connect to production services or mutate data.
"""
import re
import unittest
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
        }
        missing = required.difference(cinema_app.app.view_functions)
        self.assertFalse(missing, f"Missing required Flask endpoints: {sorted(missing)}")

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
