"""
تست بررسی انکودینگ و سازگاری alembic.ini با ویندوز و سیستم‌های غیر UTF-8.

بررسی می‌کند که فایل alembic.ini صرفاً شامل کاراکترهای اسکی (ASCII-only) باشد
تا در محیط‌های ویندوزی با انکودینگ‌های پیش‌فرض مانند cp1252 با خطای UnicodeDecodeError
مواجه نشود.
"""

import configparser
from pathlib import Path
import unittest

from alembic.config import Config

ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = ROOT / "alembic.ini"


class AlembicIniEncodingTests(unittest.TestCase):
    """تضمین انکودینگ اسکی خالص برای alembic.ini جهت سازگاری کامل با ویندوز."""

    def test_file_exists(self):
        self.assertTrue(ALEMBIC_INI.is_file(), "فایل alembic.ini یافت نشد.")

    def test_alembic_ini_is_pure_ascii(self):
        """فایل باید بدون هیچ خطایی با کدک ASCII دیکود شود (بدون بایت‌های غیر اسکی)."""
        content_bytes = ALEMBIC_INI.read_bytes()
        try:
            decoded = content_bytes.decode("ascii")
        except UnicodeDecodeError as exc:
            self.fail(
                f"فایل alembic.ini شامل بایت‌های غیر اسکی است: {exc}. "
                "تمام کامنت‌ها و متون این فایل باید به انگلیسی و در محدوده ASCII باشند."
            )
        self.assertIn("[alembic]", decoded)

    def test_alembic_ini_decodes_with_cp1252(self):
        """تست اختصاصی برای شبیه‌سازی خواندن فایل در ویندوزهای انگلیسی/اروپایی (cp1252)."""
        content_bytes = ALEMBIC_INI.read_bytes()
        try:
            text = content_bytes.decode("cp1252")
        except UnicodeDecodeError as exc:
            self.fail(f"خطا در خواندن فایل با کدک cp1252: {exc}")
        self.assertIn("script_location", text)

    def test_configparser_reads_alembic_ini_with_cp1252(self):
        """تست پردازش فایل توسط configparser با encoding=cp1252."""
        parser = configparser.ConfigParser()
        with open(ALEMBIC_INI, "r", encoding="cp1252") as f:
            parser.read_file(f)
        self.assertTrue(parser.has_section("alembic"))
        self.assertIn("script_location", parser["alembic"])

    def test_alembic_config_object_loads_cleanly(self):
        """آبجکت Config خود کتابخانه Alembic فایل را بدون اشکال لود کند."""
        cfg = Config(str(ALEMBIC_INI))
        script_loc = cfg.get_main_option("script_location")
        self.assertTrue(script_loc and "alembic" in script_loc)


if __name__ == "__main__":
    unittest.main()
