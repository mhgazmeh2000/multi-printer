"""
Regression tests for the create_admin.py crash:

    ValueError: invalid literal for int() with base 10: 'role'
        models.py User.get(user_id)  ←  u.get("role")

علت: فایل create_admin.py روی نمونه‌های «User» دسترسی دیکشنری‌واری
(‎u.get("role")‎ و ‎u['username']‎) انجام می‌داد، درحالی‌که «get» روی کلاس
User یک classmethod سازنده است (User.get(id) برای Flask-Login). صدای
u.get("role") روی نمونه، همان classmethod را با user_id='role' اجرا می‌کرد.
رفع: استفاده‌ی صفت‌وار (u.role / u.username) + نگهبانِ پیام‌واضح در User.get.
"""

import builtins
import os
import tempfile
import unittest
from unittest.mock import patch


class CreateAdminTests(unittest.TestCase):
    def setUp(self):
        # همان الگوی suiteهای دیگر: دیتابیس موقت با chdir
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        from core.database import init_db
        init_db()

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    # --------------------------------------------------------------
    def _run_main(self, answers):
        import create_admin
        with patch.object(builtins, "input", side_effect=list(answers)):
            create_admin.main()

    def test_create_first_admin_end_to_end(self):
        """ساخت اولین ادمین با ورودی‌های استاندارد — قبلاً روی لیست خالی
        کرش نمی‌کرد، ولی خط مسیر بعدی (لیست ادمین‌ها) پنهان می‌ماند."""
        self._run_main(["testadmin", "admin@example.com", "secret123", "secret123"])
        from models import User
        u = User.find_by_identifier("testadmin")
        self.assertIsNotNone(u)
        self.assertEqual(u.role, "admin")
        self.assertTrue(u.is_verified)
        self.assertTrue(u.verify_password("secret123"))

    def test_existing_admin_listing_does_not_crash(self):
        """وقتی ادمین وجود دارد، چاپ لیست (خط قدیمی u['username']) نباید کرش کند
        و انتخاب «n» باید تمیز لغو کند."""
        from models import User
        User.create(username="root", email="root@example.com", password="secret123",
                    role="admin", is_verified=True)
        self._run_main(["n"])  # Create another admin? → n
        remaining = User.all()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].username, "root")

    def test_promote_existing_viewer_to_admin(self):
        """جریان ارتقای کاربر موجود — خطوط قدیمی user.get('role') و user['id']
        دقیقاً همان خطاهای گزارش‌شده بودند."""
        from models import User
        User.create(username="ali", email="ali@example.com", password="secret123",
                    role="viewer", is_verified=False)
        # جریان: username=ali → پیدایش کرد → promote? y
        self._run_main(["ali", "y"])
        u = User.find_by_identifier("ali")
        self.assertEqual(u.role, "admin")
        self.assertTrue(u.is_verified)

    def test_dict_style_get_on_instance_raises_clear_typeerror(self):
        """تله‌ی گزارش‌شده: صدای u.get('role') روی نمونه — حالا TypeError
        با پیام راهنما به‌جای ValueError نامفهوم int('role')."""
        from models import User
        u = User.from_dict({"id": 5, "username": "x", "role": "viewer"})
        with self.assertRaises(TypeError) as ctx:
            u.get("role")
        self.assertIn("شناسه‌ی عددی", str(ctx.exception))

    def test_classmethod_get_still_works_for_flask_login(self):
        """مسیر اصلی Flask-Login (load_user → User.get(id)) سالم بماند."""
        from models import User
        created = User.create(username="bob", email="bob@example.com",
                              password="secret123", role="manager", is_verified=True)
        fetched = User.get(created.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.username, "bob")
        self.assertEqual(fetched.role, "manager")

    def test_create_admin_has_no_dict_style_access_left(self):
        """نگهبان رگرسیون استاتیک: دیگر هیچ دسترسی دیکشنری‌واری به نمونه‌ی
        User در create_admin.py نباشد."""
        import create_admin, inspect
        src = inspect.getsource(create_admin)
        self.assertNotIn('.get("role")', src)
        self.assertNotIn("u['username']", src)
        self.assertNotIn('u["username"]', src)
        self.assertNotIn('user["id"]', src)


if __name__ == "__main__":
    unittest.main()
