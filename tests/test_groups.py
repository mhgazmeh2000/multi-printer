import os
import tempfile
import unittest

from core import store
from core.groups import load_groups, create_group, rename_group, delete_group
from web import create_app
from core.database import init_db


class GroupStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def test_create_rename_delete_flow(self):
        # امضای create_group برای سازگاریِ عقب‌رو icon/color می‌پذیرد،
        # ولی مدل داده‌ی فعلی عمداً name-only است (بدون icon/color).
        g = create_group("مالی", icon="🏦", color="green")
        self.assertTrue(g["id"].startswith("g_"))
        self.assertEqual(g["name"], "مالی")
        self.assertNotIn("icon", g)
        self.assertNotIn("color", g)
        # تکراری
        with self.assertRaises(ValueError):
            create_group("مالی")
        # نام خیلی بلند
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
        g1 = create_group("Server Room")
        g2 = create_group("Server Room 2")
        # نام متفاوت ولی slug مشابه نباید برخورد کند
        self.assertNotEqual(g1["id"], g2["id"])


class GroupApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        init_db()
        self.app = create_app()
        self.app.config["WTF_CSRF_ENABLED"] = False  # فقط برای تست API
        self.client = self.app.test_client()

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def test_groups_match_module_permissions(self):
        # بدون لاگین: GET هم 401 است
        r = self.client.get("/api/groups")
        self.assertEqual(r.status_code, 401)
        # ساخت کاربر ادمین واقعی تا load_user کار کند، سپس session با id واقعی
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
