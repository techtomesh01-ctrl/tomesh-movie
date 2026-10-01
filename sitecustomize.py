"""CINEMA WORLD safe extension loader.

Python imports sitecustomize automatically when the project root is on sys.path.
This keeps optional feature code isolated from the core Flask app so a feature
mistake cannot prevent the main application from booting.
"""
import builtins
import sys

_original_import = builtins.__import__
_loaded = False


def _cinema_import(name, globals=None, locals=None, fromlist=(), level=0):
    global _loaded
    module = _original_import(name, globals, locals, fromlist, level)
    if name == "app" and not _loaded:
        _loaded = True
        try:
            _original_import("cinema_features", globals, locals, (), 0)
        except Exception as exc:
            # Never break the core application because an optional feature fails.
            print("CINEMA WORLD optional features disabled:", repr(exc))
    return module


builtins.__import__ = _cinema_import
