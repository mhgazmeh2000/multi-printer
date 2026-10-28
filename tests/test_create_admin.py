# =============================================================================
# @file:         tests/test_create_admin.py
# @brief:        Regression tests for the create_admin.py crash.
# @details:      Responsibilities: Verify the admin-creation script runs without errors.
#                Main API: CreateAdminTests
#                Side effects: none at import time
# @dependencies: internal: core.database, create_admin, models
# @notes:        Can be run directly as a script.
# =============================================================================

"""
Regression tests for the create_admin.py crash:

    ValueError: invalid literal for int() with base 10: 'role'
        models.py User.get(user_id)  ←  u.get("role")

Reason: create_admin.py file on "User" instances of access dictionary
(u.get("role") and u['username']) did, while "get" on the class
User is a constructor classmethod (User.get(id) for Flask-Login). voice
u.get("role") on the instance would execute the same classmethod with user_id='role'.
Fix: use of attribute (u.role / u.username) + clear message guard in User.get.
"""

import builtins
import os
import tempfile
import unittest
from unittest.mock import patch


class CreateAdminTests(unittest.TestCase):
    """Tests for create admin."""
    def setUp(self):
        # Same pattern as other suites: temporary database with chdir
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        from core.database import init_db
        init_db()

    def tearDown(self):
        """Release the resources that each test in this class created."""
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    # --------------------------------------------------------------
    def _run_main(self, answers):
        """Helper: run main."""
        import create_admin
        with patch.object(builtins, "input", side_effect=list(answers)):
            create_admin.main()

    def test_create_first_admin_end_to_end(self):
        """Creating the first admin with standard entries — already on the empty list
        It didn't crash, but the next path line (list of admins) remains hidden."""
        self._run_main(["testadmin", "admin@example.com", "secret123", "secret123"])
        from models import User
        u = User.find_by_identifier("testadmin")
        self.assertIsNotNone(u)
        self.assertEqual(u.role, "admin")
        self.assertTrue(u.is_verified)
        self.assertTrue(u.verify_password("secret123"))

    def test_existing_admin_listing_does_not_crash(self):
        """Printing the list (old line u['username']) should not crash when there is admin
        And selecting "n" should cancel cleanly."""
        from models import User
        User.create(username="root", email="root@example.com", password="secret123",
                    role="admin", is_verified=True)
        self._run_main(["n"])  # Create another admin? → n
        remaining = User.all()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].username, "root")

    def test_promote_existing_viewer_to_admin(self):
        """Existing user upgrade flow — legacy lines user.get('role') and user['id']
        Exactly the same errors were reported."""
        from models import User
        User.create(username="ali", email="ali@example.com", password="secret123",
                    role="viewer", is_verified=False)
        # flow: username=ali → created → promote? y
        self._run_main(["ali", "y"])
        u = User.find_by_identifier("ali")
        self.assertEqual(u.role, "admin")
        self.assertTrue(u.is_verified)

    def test_dict_style_get_on_instance_raises_clear_typeerror(self):
        """Reported trap: u.get('role') on instance — now TypeError
        with a help message instead of the unintelligible ValueError int('role')."""
        from models import User
        u = User.from_dict({"id": 5, "username": "x", "role": "viewer"})
        with self.assertRaises(TypeError) as ctx:
            u.get("role")
        self.assertIn("شناسه‌ی عددی", str(ctx.exception))

    def test_classmethod_get_still_works_for_flask_login(self):
        """Keep the original Flask-Login path (load_user → User.get(id)) intact."""
        from models import User
        created = User.create(username="bob", email="bob@example.com",
                              password="secret123", role="manager", is_verified=True)
        fetched = User.get(created.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.username, "bob")
        self.assertEqual(fetched.role, "manager")

    def test_create_admin_has_no_dict_style_access_left(self):
        """Static regression guard: no more dictionary access to the instance
        User is not in create_admin.py."""
        import create_admin, inspect
        src = inspect.getsource(create_admin)
        self.assertNotIn('.get("role")', src)
        self.assertNotIn("u['username']", src)
        self.assertNotIn('u["username"]', src)
        self.assertNotIn('user["id"]', src)


if __name__ == "__main__":
    unittest.main()
