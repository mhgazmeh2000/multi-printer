# =============================================================================
# @file:         tests/test_alembic_ini_encoding.py
# @brief:        Tests that alembic.ini is readable on Windows and non-UTF-8 systems.
# @details:      Responsibilities: Check encoding and compatibility of the alembic.ini file.
#                Main API: AlembicIniEncodingTests
#                Side effects: database access, file I/O
# @dependencies: internal: alembic.config
# @notes:        Can be run directly as a script.
# =============================================================================

"""
Alembic.ini encoding and compatibility test with Windows and non-UTF-8 systems.

Checks that the alembic.ini file contains ASCII-only characters
Even in Windows environments with default encodings such as cp1252 with UnicodeDecodeError error
do not face
"""

import configparser
import unittest
from pathlib import Path

from alembic.config import Config

ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = ROOT / "alembic.ini"


class AlembicIniEncodingTests(unittest.TestCase):
    """Guaranteed pure Ski encoding for alembic.ini for full Windows compatibility."""

    def test_file_exists(self):
        """Verifies file exists."""
        self.assertTrue(ALEMBIC_INI.is_file(), "فایل alembic.ini یافت نشد.")

    def test_alembic_ini_is_pure_ascii(self):
        """The file should be decoded with the ASCII codec without any errors (no non-ASCII bytes)."""
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
        """Dedicated test for simulating file reading on English/European Windows (cp1252)."""
        content_bytes = ALEMBIC_INI.read_bytes()
        try:
            text = content_bytes.decode("cp1252")
        except UnicodeDecodeError as exc:
            self.fail(f"خطا در خواندن فایل با کدک cp1252: {exc}")
        self.assertIn("script_location", text)

    def test_configparser_reads_alembic_ini_with_cp1252(self):
        """File processing test by configparser with encoding=cp1252."""
        parser = configparser.ConfigParser()
        with open(ALEMBIC_INI, "r", encoding="cp1252") as f:
            parser.read_file(f)
        self.assertTrue(parser.has_section("alembic"))
        self.assertIn("script_location", parser["alembic"])

    def test_alembic_config_object_loads_cleanly(self):
        """The Config object of the Alembic library itself loads the file without any problems."""
        cfg = Config(str(ALEMBIC_INI))
        script_loc = cfg.get_main_option("script_location")
        self.assertTrue(script_loc and "alembic" in script_loc)


if __name__ == "__main__":
    unittest.main()
