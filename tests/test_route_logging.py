# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_route_logging.py
# @brief:        Tests for logging on error paths and the 'log is undefined' guard (TD-004).
# @details:      Responsibilities: Verify route error logging.
#                Main API: SecurityStatsErrorPathTests, LogBindingGuardTests
#                Side effects: none at import time
# @dependencies: internal: core.database, models, web, web.routes.security
# @notes:        Can be run directly as a script.
# =============================================================================

r"""
Tests E.6.7 (TD-004) — Logging on error path, and "`log` undefined" guard.

Why is this file?
----------------
`web/routes/security.py:146` called `log.exception(...)` in `except Exception`,
While the module never creates ``log''. Result in practice: any internal error
`/api/security/stats` instead of `500 {"error": …}` a `NameError` on `NameError`
was giving — that is, the scheduled JSON response was not received, and **no log record** was written
Couldn't (exactly what the handler was designed to record).

The second part of the file is a structural lock: each `web/` and `core/` module that `log.<level>()`
calls must have defined ``log'' in the same scope. pyflakes understands this
And it is also found in manual review, but in CI this lock is a *test*, not a warning - so
The next refactor cannot silently return the same state.
"""
import ast
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


class SecurityStatsErrorPathTests(unittest.TestCase):
    """Error path `/api/security/stats`: 500 with JSON and a log record — no NameError."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(lambda: os.chdir(self.old_cwd))
        env = mock.patch.dict(os.environ, {"DB_BACKEND": "sqlite", "PG_DSN": ""})
        env.start()
        self.addCleanup(env.stop)

        from core.database import init_db
        from web import create_app
        from models import User
        init_db()
        self.user = User.create(username="sec_stats_admin", email="s@test.io",
                                password="secret123", role="admin", is_verified=True)
        self.app = create_app()
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.id)
            sess["_fresh"] = True

    def test_handler_error_returns_json_500_and_logs(self):
        """Verifies handler error returns json 500 and logs."""
        boom = RuntimeError("security_events unavailable")
        with mock.patch("web.routes.security._db_conn", side_effect=boom):
            with self.assertLogs("PrinterMonitor", level="ERROR") as captured:
                resp = self.client.get("/api/security/stats")
        self.assertEqual(resp.status_code, 500, resp.get_data(as_text=True))
        payload = resp.get_json()
        self.assertIsNotNone(payload, "پاسخ باید JSON باشد، نه HTML خطای Flask")
        self.assertIn("خطا", str(payload.get("error", "")))
        self.assertTrue(any("security stats API" in line for line in captured.output),
                        captured.output)
        self.assertTrue(any("RuntimeError: security_events unavailable" in line
                            for line in captured.output),
                        "exc_info باید در رکورد لاگ باشد، نه فقط پیام")

    def test_name_error_is_not_possible_in_the_handler(self):
        """Direct witness TD-004: `log` is bound at module level and has `exception`."""
        import web.routes.security as security
        self.assertIsInstance(security.log, logging.Logger)
        self.assertTrue(callable(security.log.exception))
        src = (ROOT / "web" / "routes" / "security.py").read_text(encoding="utf-8")
        self.assertIn("log = logging.getLogger(", src)


class LogBindingGuardTests(unittest.TestCase):
    """
    No module should call `log.<level>()` without a binding seen.

    binding accepted: module-level assignment (`log =...`), assignment within the same function, or
    import with the name `log`. "Inside internal function" mode is also counted because it is Python's scope
    sees the same; The rule of this test is deliberately kept simple to keep it readable.
    """

    LEVELS = {"info", "warning", "error", "exception", "debug", "critical", "log"}
    DIRS = ("web", "core")

    def _module_offenders(self, path: Path):
        """Helper: module offenders."""
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        module_bound = set()
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name) and t.id == "log":
                        module_bound.add("log")
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if (alias.asname or alias.name).split(".")[0] == "log":
                        module_bound.add("log")
        offenders = []
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            uses = [
                node for node in ast.walk(fn)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "log"
                and node.func.attr in self.LEVELS
            ]
            if not uses:
                continue
            local = any(isinstance(n, ast.Name) and n.id == "log"
                        and isinstance(n.ctx, ast.Store) for n in ast.walk(fn))
            if not local and "log" not in module_bound:
                first = uses[0]
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{first.lineno}")
        # External use of any function (module level) must also have binding
        top_uses = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "log"
        ]
        inside_fn_lines = {n.lineno for fn in ast.walk(tree)
                           if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                           for n in ast.walk(fn) if hasattr(n, "lineno")}
        for node in top_uses:
            if node.lineno not in inside_fn_lines and "log" not in module_bound:
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno} (module level)")
        return offenders

    def test_no_unbound_log_calls(self):
        """Verifies no unbound log calls."""
        offenders = []
        for d in self.DIRS:
            for path in sorted((ROOT / d).rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                offenders.extend(self._module_offenders(path))
        self.assertEqual(offenders, [],
                         "صدا زدن log بدون binding یعنی NameError در مسیر خطا — "
                         "TD-004. فایل‌های متخلف: " + ", ".join(offenders))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
