# -*- coding: utf-8 -*-
r"""
تست‌های E.6.7 (TD-004) — لاگ‌گذاری در مسیر خطا، و نگهبان «`log` تعریف‌نشده».

چرا این فایل هست
----------------
`web/routes/security.py:135` در `except Exception` صدا می‌زد `log.exception(...)`،
در حالی که ماژول هرگز `log را` نمی‌ساخت. نتیجه در عمل: هر خطای داخلی
`/api/security/stats` به‌جای `500 {"error": …}` یک `NameError` روی `NameError`
می‌داد — یعنی هم پاسخ JSON برنامه‌ریزی‌شده نمی‌رسید، هم **هیچ رکورد لاگی** نوشته
نمی‌شد (دقیقاً همان چیزی که handler برای ثبت آن ساخته شده بود).

بخش دوم فایل، یک قفل ساختاری است: هر ماژول `web/` و `core/` که `log.<level>()`
را صدا می‌زند باید `log` را در همان scope تعریف کرده باشد. pyflakes این را می‌فهمد
و در بازبینی دستی هم پیدا می‌شود، ولی در CI این قفل، *تست* است نه هشدار — پس
refactor بعدی نمی‌تواند بی‌صدا همان حالت را برگرداند.
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
    """مسیر خطای `/api/security/stats`: ۵۰۰ با JSON و یک رکورد لاگ — بدون NameError."""

    def setUp(self):
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
        """شاهد مستقیم TD-004: `log` در سطح ماژول bound است و `exception` را دارد."""
        import web.routes.security as security
        self.assertIsInstance(security.log, logging.Logger)
        self.assertTrue(callable(security.log.exception))
        src = (ROOT / "web" / "routes" / "security.py").read_text(encoding="utf-8")
        self.assertIn("log = logging.getLogger(", src)


class LogBindingGuardTests(unittest.TestCase):
    """
    هیچ ماژولی `log.<level>()` را بدون binding دیده‌شده صدا نزند.

    binding پذیرفته: انتساب سطح‌ماژول (`log = …`)، انتساب داخل همان تابع، یا
    import با نام `log`. حالت «داخل تابع داخلی» هم counted است چون scope پایتون
    همان را می‌بیند؛ قاعدهٔ این تست عمداً ساده نگه داشته شده تا خوانا بماند.
    """

    LEVELS = {"info", "warning", "error", "exception", "debug", "critical", "log"}
    DIRS = ("web", "core")

    def _module_offenders(self, path: Path):
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
        # استفادهٔ بیرون از هیچ تابعی (سطح ماژول) هم باید binding داشته باشد
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
