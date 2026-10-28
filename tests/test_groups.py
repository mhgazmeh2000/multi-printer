# =============================================================================
# @file:         tests/test_groups.py
# @brief:        Tests for printer groups storage and API.
# @details:      Responsibilities: Check group CRUD and the group routes.
#                Main API: GroupStoreTests, GroupApiTests
#                Side effects: none at import time
# @dependencies: internal: core, core.database, core.groups, models, web
# @notes:        Can be run directly as a script.
# =============================================================================

"""Tests for printer group storage and the group API (CRUD operations and routes)."""

import os
import tempfile
import unittest

from core import store
from core.groups import load_groups, create_group, rename_group, delete_group
from web import create_app
from core.database import init_db


class GroupStoreTests(unittest.TestCase):
    """Tests for group store."""
    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)

    def tearDown(self):
        """Release the resources that each test in this class created."""
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def test_create_rename_delete_flow(self):
        # The create_group signature accepts icon/color for backward compatibility.
        # But the current data model is intentionally name-only (no icon/color).
        """Verifies create rename delete flow."""
        g = create_group("مالی", icon="🏦", color="green")
        self.assertTrue(g["id"].startswith("g_"))
        self.assertEqual(g["name"], "مالی")
        self.assertNotIn("icon", g)
        self.assertNotIn("color", g)
        # repetitive
        with self.assertRaises(ValueError):
            create_group("مالی")
        # The name is too long
        with self.assertRaises(ValueError):
            create_group("x" * 100)
        # rename
        self.assertTrue(rename_group(g["id"], "مالی واحد A"))
        self.assertEqual(load_groups()[0]["name"], "مالی واحد A")
        # delete
        self.assertTrue(delete_group(g["id"]))
        self.assertFalse(delete_group(g["id"]))
        self.assertEqual(load_groups(), [])

    def test_duplicate_id_gets_unique_slug(self):
        """Verifies duplicate id gets unique slug."""
        g1 = create_group("Server Room")
        g2 = create_group("Server Room 2")
        # Different name but similar slug should not match
        self.assertNotEqual(g1["id"], g2["id"])


class GroupApiTests(unittest.TestCase):
    """Tests for group api."""
    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        init_db()
        self.app = create_app()
        self.app.config["WTF_CSRF_ENABLED"] = False  # Only for API testing
        self.client = self.app.test_client()

    def tearDown(self):
        """Release the resources that each test in this class created."""
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def test_groups_match_module_permissions(self):
        # Without login: GET is also 401
        """Verifies groups match module permissions."""
        r = self.client.get("/api/groups")
        self.assertEqual(r.status_code, 401)
        # Make real admin user so load_user works, then session with real id
        from models import User
        u = User.create(username="admin1", email="a@test.io", password="secret123", role="admin", is_verified=True)
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(u.id)
            sess["_fresh"] = True
        r = self.client.post("/api/groups", json={"name": "اتاق سرور", "icon": "🖨️", "color": "yellow"})
        self.assertEqual(r.status_code, 201)
        gid = r.get_json()["group"]["id"]
        r = self.client.get("/api/groups")
        self.assertEqual(len(r.get_json()["groups"]), 1)
        r = self.client.post(f"/api/groups/{gid}/rename", json={"name": "سرور 2"})
        self.assertEqual(r.status_code, 200)
        r = self.client.delete(f"/api/groups/{gid}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(load_groups(), [])

    def test_delete_group_clears_printers(self):
        """Verifies delete group clears printers."""
        from models import User
        u = User.create(username="admin2", email="b@test.io", password="secret123", role="admin", is_verified=True)
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(u.id)
            sess["_fresh"] = True
        r = self.client.post("/api/groups", json={"name": "بخش فروش"})
        gid = r.get_json()["group"]["id"]
        with store.printers_lock:
            store.PRINTERS[:] = [{"ip": "10.1.1.1", "name": "P1", "community": "public", "group": gid}]
        r = self.client.delete(f"/api/groups/{gid}")
        self.assertEqual(r.get_json()["printers_ungrouped"], 1)
        self.assertEqual(store.PRINTERS[0]["group"], "")
        with store.printers_lock:
            store.PRINTERS[:] = []


if __name__ == "__main__":
    unittest.main()
