# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_cartridge_change_timezone.py
# @brief:        Tests that cartridge-change event timestamps use the shared UTC clock.
# @details:      Responsibilities: Check timestamp correctness for branded and generic branches.
#                Main API: _TimestampAssertions, BrandedBranchTimestampTests,
#                GenericBranchTimestampTests, ModuleTimeSourceGuardTests,
#                GuardAcceptsValidFormsTests, GuardRejectsInvalidFormsTests
#                Side effects: file I/O
# @dependencies: internal: core.cartridge_change_engine, core.time_utils
# @notes:        Can be run directly as a script.
# =============================================================================

"""
Cartridge Event Time Tests (Phase D — Task D.6; Resilient Guard — Phase E.0, Task E.0.14).

Context: `core/cartridge_change_engine.py` was producing two different time bases:
  • Line 272 (live path, common to both evidence paths): local time + fake suffix `Z'
    ⇒ For server with `TZ=Asia/Tehran', you have a 3:30 hour deviation (proven: 210 minutes deviation).
  • Line 306 (inside unreachable block 295–328): UTC was real, but `datetime.utcnow()`
    It is deprecated from Python 3.12.

Tests:
  1) evidence path chip_id ("branded"): timestamp must be real UTC.
  2) counter/level/lifetime (“off-brand”) evidence pathway: same claims.
  3) AST static guard on the module itself: no `utcnow()`, no `now().astimezone()`, and any
     Calling `datetime.now(...)` must take **a valid UTC timezone**.
     It is more strict than phase E.3: the module does not have any direct call ``now'' and **both**
     Timestamp generation point from `core.time_utils.utc_now_iso()` (standard format
     `…T…ffffffZ`) are coming — `audit_shared_clock_usage` guard.

**Independent of TZ (step E.0.3-pre):** Tests 1 and 2 have no skip and are executed in each TZ
they become Their main claim is: Parse the generated string with `...Z' format,
The result should be less than 10 seconds different from the UTC of this moment. This claim is a bug
It takes the old (local time + bogus suffix `Z`) in each TZ with non-zero offset, as in
The difference with UTC is equal to the total offset (for example, 210 minutes). The only mode that
"Local" and "UTC" are practically the same thing `TZ=UTC' (zero offset ⇒ intrinsically inseparable), and
The same situation is covered by the static guard test 3.

**Forms Resistant Guard (E.0.14):** Guard no longer locks on *written form*; forms
Accepts the ``explicit UTC'' variant and only takes **incorrect** forms:

    It is accepted:
      datetime.now(timezone.utc)        datetime.now(tz=timezone.utc)
      datetime.now(UTC)                 datetime.now(tz=UTC)
      datetime.now(ZoneInfo("UTC"))     datetime.now(tz=ZoneInfo("UTC"))
      datetime.now(datetime.timezone.utc) # import datetime instead of from... import
      And the same ones with the nickname import (like `import datetime as dt` or `timezone as tz2`)
    Rejected:
      datetime.now() # naive ⇒ 3:30 error
      datetime.now(timezone(timedelta(hours=3, minutes=30))) # Tehran, not UTC
      datetime.datetime.utcnow()        # deprecated + naive
      datetime.now() followed by.astimezone() ⇒ Local label with Z
      datetime.now().replace(tzinfo=timezone.utc) # "tag" to local value

  Also, calls that **have nothing to do with datetime** (such as `store.now()' or a function like
  ``now'' name defined by the module itself) are ignored to avoid false positive guard.

Execution:
    python -m pytest tests/test_cartridge_change_timezone.py -q          # TZ=UTC
    TZ=Asia/Tehran python -m pytest tests/test_cartridge_change_timezone.py -q
"""
import ast
import os
import unittest
from datetime import datetime, timezone

from core.cartridge_change_engine import evaluate_cartridge_change
from core.time_utils import ISO_Z_FORMAT, ISO_Z_LENGTH, parse_utc, to_utc_z

MODULE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           os.pardir, "core", "cartridge_change_engine.py")
# Phase E.3: Definitive project format — always UTC with suffix Z and **6 microsecond digits**
# (fixed length 27). Previously, this file was locked on "accuracy seconds without microseconds" and that's it
# It was causing the test with the new author (utc_now_iso) to stay red.
TS_FORMAT = ISO_Z_FORMAT

#: string names that are accepted as UTC time zone.
UTC_ZONE_NAMES = frozenset({"UTC", "utc", "Etc/UTC", "Etc/GMT", "GMT", "Z"})


# ─────────────────────────────────────────────────────────────
# AST guard (pure and testable function)
# ─────────────────────────────────────────────────────────────

def _import_aliases(tree):
    """Extracts the names used in the module for datetime/timezone/UTC/ZoneInfo.

    Output: dict with keys modules / classes / timezones / utc_names / zoneinfo_names.
    """
    modules, classes, timezones, utc_names, zoneinfo_names = set(), set(), set(), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                if alias.name.split(".")[0] == "datetime":
                    modules.add(bound)
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            for alias in node.names:
                bound = alias.asname or alias.name
                if mod == "datetime" and alias.name == "datetime":
                    classes.add(bound)
                elif mod == "datetime" and alias.name == "timezone":
                    timezones.add(bound)
                elif mod == "datetime" and alias.name in ("UTC", "utc"):
                    utc_names.add(bound)
                elif mod in ("zoneinfo", "backports.zoneinfo") and alias.name == "ZoneInfo":
                    zoneinfo_names.add(bound)
    classes.add("datetime")            # Conventional class name, even if import is not in the module
    timezones.add("timezone")
    utc_names.add("UTC")
    zoneinfo_names.add("ZoneInfo")
    return {"modules": modules, "classes": classes, "timezones": timezones,
            "utc_names": utc_names, "zoneinfo_names": zoneinfo_names}


def _is_utc_timezone_expr(node, names):
    """Is this expression a UTC time zone? Description String or None."""
    # ZoneInfo("UTC") / ZoneInfo(key="UTC")
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Name) and f.id in names["zoneinfo_names"]:
            args = list(node.args)
            for kw in node.keywords:
                if kw.arg == "key":
                    args.append(kw.value)
            if len(args) == 1 and isinstance(args[0], ast.Constant) \
                    and args[0].value in UTC_ZONE_NAMES:
                return f'ZoneInfo({args[0].value!r})'
        return None

    # UTC (any alias)
    if isinstance(node, ast.Name):
        return "UTC" if node.id in names["utc_names"] else None

    # timezone.utc / datetime.timezone.utc / <module-alias>.timezone.utc
    if isinstance(node, ast.Attribute) and node.attr == "utc":
        base = node.value
        if isinstance(base, ast.Name) and base.id in names["timezones"]:
            return f"{base.id}.utc"
        if isinstance(base, ast.Attribute) and base.attr == "timezone" \
                and isinstance(base.value, ast.Name) and base.value.id in names["modules"]:
            return f"{base.value.id}.timezone.utc"
    return None


def _is_datetime_like(node, names, local_defs):
    """Is it a ``datetime class/object'' expression that `.now()' makes sense?"""
    if isinstance(node, ast.Name):
        return node.id in names["classes"] and node.id not in local_defs
    if isinstance(node, ast.Attribute) and node.attr == "datetime":
        return isinstance(node.value, ast.Name) and node.value.id in names["modules"]
    return False


def audit_now_calls(source):
    """Static guard on a module: Finds any suspicious `now`/`utcnow`/`astimezone`.

    Output: dict with keys
      accepted: [(lineno, form description)] — now calls with explicit UTC
      problems: [message] — Incorrect forms
      skipped: [(lineno, name)] — calls that have nothing to do with datetime
    """
    tree = ast.parse(source)
    names = _import_aliases(tree)
    local_defs = {n.name for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    accepted, problems, skipped = [], [], []

    def _checkable_now(call):
        """(checkable?, explanation) for a Call whose func.attr/id is "now"."""
        f = call.func
        if isinstance(f, ast.Attribute) and f.attr == "now":
            if _is_datetime_like(f.value, names, local_defs):
                return True
            if isinstance(f.value, ast.Name) and f.value.id not in local_defs:
                # Like self.now()/store.now() ⇒ It has nothing to do with datetime
                if f.value.id in ("self", "cls") or f.value.id[:1].islower():
                    return False
        return isinstance(f, ast.Name) and f.id == "now" and "now" not in local_defs

    now_nodes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            fname = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if fname == "now":
                if _checkable_now(node):
                    now_nodes.append(node)
                else:
                    skipped.append((node.lineno, "now"))
            elif fname == "utcnow":
                base_ok = (isinstance(f, ast.Attribute) and _is_datetime_like(f.value, names, local_defs)) \
                    or (isinstance(f, ast.Name) and "utcnow" not in local_defs)
                if base_ok:
                    problems.append(
                        f"خط {node.lineno}: `utcnow()` (naive + در ۳.۱۲ deprecated) — "
                        f"از `datetime.now(timezone.utc)` استفاده کنید")
            elif fname == "astimezone" and isinstance(f, ast.Attribute):
                inner = f.value
                if isinstance(inner, ast.Call):
                    ifn = inner.func
                    iname = ifn.attr if isinstance(ifn, ast.Attribute) else getattr(ifn, "id", "")
                    if iname == "now":
                        problems.append(
                            f"خط {node.lineno}: `now(...).astimezone(...)` — زمان را محلی "
                            f"برچسب می‌زند؛ مستقیم `datetime.now(timezone.utc)` بنویسید")
            elif fname == "replace" and isinstance(f, ast.Attribute):
                inner = f.value
                if isinstance(inner, ast.Call):
                    ifn = inner.func
                    iname = ifn.attr if isinstance(ifn, ast.Attribute) else getattr(ifn, "id", "")
                    tz_kw = [kw for kw in node.keywords if kw.arg == "tzinfo"]
                    if iname == "now" and tz_kw:
                        problems.append(
                            f"خط {node.lineno}: `now().replace(tzinfo=…)` — برچسب زدن به مقدار "
                            f"محلی (نه تبدیل) — مستقیم UTC بگیرید")

    for node in now_nodes:
        args = list(node.args)
        kw_tz = [kw for kw in node.keywords if kw.arg == "tz"]
        extra = [kw for kw in node.keywords if kw.arg not in ("tz", None)]
        if len(args) > 1 or extra:
            problems.append(f"خط {node.lineno}: فراخوانی `now` با آرگومان غیرمنتظره")
            continue
        if not args and not kw_tz:
            problems.append(
                f"خط {node.lineno}: `datetime.now()` بدون آرگومان ⇒ naive؛ برای سرور "
                f"Asia/Tehran ۳:۳۰ خطا. `datetime.now(timezone.utc)` بنویسید")
            continue
        expr = args[0] if args else kw_tz[0].value
        kind = _is_utc_timezone_expr(expr, names)
        if kind is None:
            problems.append(
                f"خط {node.lineno}: آرگومان `now` صریحاً UTC نیست "
                f"(`{ast.unparse(expr)}`) — فقط فرم‌های صریح UTC پذیرفته می‌شوند")
            continue
        accepted.append((node.lineno, f"now({'tz=' if kw_tz else ''}{kind})"))

    return {"accepted": accepted, "problems": problems, "skipped": skipped,
            "now_lines": [ln for ln, _ in accepted]}


def audit_shared_clock_usage(source):
    """Static Guard (E.3): "Now" this module should only come from the central time helper.

    Output:
      imports: bool — Is `utc_now_iso` imported from `core.time_utils`?
      call_lines: [int] — Call lines of `utc_now_iso(...)`

    Why was it necessary? Until phase E.2 of this guard on `datetime.now(timezone.utc)` itself in two
    The timestamp production line was locked; From E.3 both lines reached `utc_now_iso()`. guard
    New maintains the same guarantee (both timestamp generation points from a single source and
    standard) and at the same time does not lock the written form.
    """
    tree = ast.parse(source)
    imports = []
    call_lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "core.time_utils":
            imports += [a.name for a in node.names]
        elif isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
            if name == "utc_now_iso":
                call_lines.append(node.lineno)
    return {"imports": "utc_now_iso" in imports, "call_lines": sorted(call_lines)}


def _branded_scenario():
    """The chip_id evidence path (branded cartridge) — gives the CONFIRMED event."""
    prev = {
        "printer_ip": "10.0.0.10",
        "brand": "hp",
        "model": "LaserJet 400",
        "color": "black",
        "chip_id": "HP-AAA-111",
        "serial": "SER-100",
        "supply_description": "HP 85A",
        "supply_counter": 12000,
        "page_counter": 15000,
        "remaining_level": 22,
        "supply_life": 68,
        "generation": 3,
    }
    return prev, {**prev, "chip_id": "HP-BBB-222", "serial": "SER-200"}

def _generic_scenario():
    """counter/level/life evidence path (unbranded cartridge) — gives POSSIBLE event."""
    prev = {
        "printer_ip": "10.0.0.11",
        "brand": "generic",
        "color": "black",
        "cartridge_model": "T-2000",
        "supply_counter": 9000,
        "remaining_level": 60,
        "supply_life": 55,
    }
    return prev, {**prev, "supply_counter": 100, "remaining_level": 100, "supply_life": 99}

class _TimestampAssertions(unittest.TestCase):
    """Common claims of two lines of evidence."""

    def assert_event_timestamp_is_utc(self, result, now_utc=None):
        """Helper: assert event timestamp is utc."""
        event = result.get("event") or {}
        ts = event.get("timestamp") or result.get("timestamp")
        self.assertTrue(ts, "رویداد باید timestamp داشته باشد")
        self.assertTrue(ts.endswith("Z"), f"timestamp باید با Z تمام شود: {ts!r}")
        # Phase E.3: Project standard format — 6 microsecond digits and fixed length 27
        # (Word-to-word arrangement with chronological order ⇒ safe string comparison).
        self.assertEqual(ISO_Z_LENGTH, len(ts),
                         f"طول timestamp باید {ISO_Z_LENGTH} باشد ({ts!r})")
        try:
            parsed = datetime.strptime(ts, TS_FORMAT).replace(tzinfo=timezone.utc)
        except ValueError as exc:
            self.fail(f"timestamp با فرمت استاندارد پروژه {ISO_Z_FORMAT} نمی‌خواند: "
                      f"{ts!r} ({exc})")
        # Roundtrip with the project's only serializer should remain unchanged (contract E.3).
        self.assertEqual(ts, to_utc_z(parse_utc(ts)),
                         f"timestamp با هِلپر مرکزی زمان سازگار نیست: {ts!r}")
        ref = now_utc or datetime.now(timezone.utc)
        delta = abs((ref - parsed).total_seconds())
        self.assertLess(delta, 10.0,
                        f"timestamp باید UTCِ همین لحظه باشد (اختلاف {delta:.0f}s، "
                        f"مقدار={ts!r}) — انحراف بزرگ یعنی ساعت محلی با پسوند جعلی Z")


class BrandedBranchTimestampTests(_TimestampAssertions):
    """Test 1 — path of chip_id evidence."""

    def test_branded_branch_writes_real_utc(self):
        """Verifies branded branch writes real utc."""
        prev, curr = _branded_scenario()
        result = evaluate_cartridge_change(prev, curr, reboot_detected=False, poll_gap_ok=True)
        self.assertTrue(result["should_emit"])
        self.assertIn("chip_id", result["detection_method"])
        self.assert_event_timestamp_is_utc(result)


class GenericBranchTimestampTests(_TimestampAssertions):
    """Test 2 — counter/level/life evidence path (no chip_id)."""

    def test_generic_branch_writes_real_utc(self):
        """Verifies generic branch writes real utc."""
        prev, curr = _generic_scenario()
        result = evaluate_cartridge_change(prev, curr, reboot_detected=False, poll_gap_ok=True)
        self.assertTrue(result["should_emit"])
        self.assertNotIn("chip_id", result["detection_method"])
        self.assert_event_timestamp_is_utc(result)


class ModuleTimeSourceGuardTests(unittest.TestCase):
    """
    Test 3 — Static guard (AST) on `core/cartridge_change_engine.py`.

    Three reviews:
      1) no `utcnow()' (deprecated + naive);
      2) no `now(...).astimezone(...)` or `now().replace(tzinfo=...)`;
      3) Each `datetime.now(...)` takes an **explicit UTC** time zone — accepting all
         Valid forms (positional/keyword, import alias, `ZoneInfo("UTC")`).
      4) Both timestamp production lines (272 live, 306 inside dead code) should be in the same collection
         so that even the dead code remains locked.
    """

    @classmethod
    def setUpClass(cls):
        """Prepare the fixtures shared by all tests in this class."""
        with open(MODULE_PATH, encoding="utf-8") as fh:
            cls.source = fh.read()
        cls.report = audit_now_calls(cls.source)
        cls.clock = audit_shared_clock_usage(cls.source)

    def test_no_deprecated_utcnow_and_no_relabelling(self):
        """Verifies no deprecated utcnow and no relabelling."""
        bad = [p for p in self.report["problems"]
               if "utcnow()" in p or "astimezone" in p or "replace(tzinfo" in p]
        self.assertEqual([], bad, "فرم‌های زمانی نادرست در ماژول: " + " | ".join(bad))

    def test_every_now_call_has_explicit_utc(self):
        """No ``datetime.now(...)'' is left in the module.

        As of E.3 this module has no direct `now' calls: ``now'' from
        Here comes `core.time_utils.utc_now_iso()` (next test). If one day someone straight
        add `datetime.now(...)`, this test stops him from making an informed decision
        be: either use the central helper, or update this guard with reason.
        """
        self.assertEqual([], self.report["problems"],
                         "مشکل در فراخوانی‌های now: " + " | ".join(self.report["problems"]))
        self.assertEqual([], self.report["accepted"],
                         "این ماژول باید «الآن» را فقط از utc_now_iso بگیرد؛ "
                         f"فراخوانی‌های مستقیم now: {self.report['accepted']}")

    def test_both_timestamp_lines_are_locked(self):
        """The timestamp generation point is read from the single source (utc_now_iso).

        Originally this asserted two call sites, one of which was an unreachable duplicate
        block after `return result`. That block was removed as dead code (CLEANUP), so the
        threshold now requires the one live call site.
        """
        self.assertTrue(self.clock["imports"],
                        "`utc_now_iso` باید از core.time_utils import شده باشد")
        self.assertGreaterEqual(
            len(self.clock["call_lines"]), 1,
            "خط تولید timestamp باید از utc_now_iso استفاده کند؛ "
            f"خطوط فعلی: {self.clock['call_lines']}")

    def test_no_local_now_left_in_source(self):
        """Verifies no local now left in source."""
        self.assertNotIn("datetime.now()", self.source.replace(" ", ""),
                         "`datetime.now()` بدون آرگومان در ماژول مانده است")


class GuardAcceptsValidFormsTests(unittest.TestCase):
    """Guard accepts valid UTC forms (no false positives/negatives) — E.0.14."""

    VALID_SNIPPETS = {
        "positional timezone.utc": "from datetime import datetime, timezone\nt = datetime.now(timezone.utc)",
        "keyword tz=timezone.utc": "from datetime import datetime, timezone\nt = datetime.now(tz=timezone.utc)",
        "from-import UTC": "from datetime import datetime, UTC\nt = datetime.now(UTC)",
        "keyword tz=UTC": "from datetime import datetime, UTC\nt = datetime.now(tz=UTC)",
        "ZoneInfo(\"UTC\")": "from datetime import datetime\nfrom zoneinfo import ZoneInfo\nt = datetime.now(ZoneInfo(\"UTC\"))",
        "keyword tz=ZoneInfo": "from datetime import datetime\nfrom zoneinfo import ZoneInfo\nt = datetime.now(tz=ZoneInfo(\"Etc/UTC\"))",
        "module form datetime.timezone.utc": "import datetime\nt = datetime.datetime.now(datetime.timezone.utc)",
        "alias import: dt + tz": "import datetime as dt\nt = dt.datetime.now(dt.timezone.utc)",
        "alias: timezone as tz2 (keyword)": "from datetime import datetime, timezone as tz2\nt = datetime.now(tz=tz2.utc)",
        "alias: UTC as UTCZ": "from datetime import datetime, UTC as UTCZ\nt = datetime.now(UTCZ)",
    }

    def test_valid_forms_are_accepted_without_problems(self):
        """Verifies valid forms are accepted without problems."""
        for label, src in self.VALID_SNIPPETS.items():
            with self.subTest(form=label):
                rep = audit_now_calls(src)
                self.assertEqual([], rep["problems"], f"فرم معتبر «{label}» رد شد: {rep['problems']}")
                self.assertEqual(1, len(rep["accepted"]), f"فرم «{label}» پذیرفته نشد")

    def test_unrelated_now_calls_are_skipped(self):
        """Verifies unrelated now calls are skipped."""
        src = ("class Store:\n"
               "    def now(self):\n"
               "        return 1\n"
               "def run(store, self_):\n"
               "    return store.now()\n")
        rep = audit_now_calls(src)
        self.assertEqual([], rep["problems"], "گارد روی now() غیر-datetime مثبت کاذب داد")
        self.assertEqual([], rep["accepted"])
        self.assertTrue(rep["skipped"], "now غیر-datetime باید skipped شود")


class GuardRejectsInvalidFormsTests(unittest.TestCase):
    """Guard takes incorrect forms — E.0.14."""

    INVALID_SNIPPETS = {
        "naive now()": ("from datetime import datetime\nt = datetime.now()", "بدون آرگومان"),
        "deprecated utcnow()": ("import datetime\nt = datetime.datetime.utcnow()", "utcnow()"),
        "local tz + astimezone": ("from datetime import datetime, timezone\n"
                                  "t = datetime.now(timezone.utc).astimezone()", "astimezone"),
        "relabel with replace(tzinfo=)": ("from datetime import datetime, timezone\n"
                                          "t = datetime.now().replace(tzinfo=timezone.utc)",
                                          "replace(tzinfo"),
        "Tehran timezone": ("from datetime import datetime, timezone, timedelta\n"
                            "t = datetime.now(timezone(timedelta(hours=3, minutes=30)))",
                            "UTC نیست"),
        "calculated offset": ("from datetime import datetime, timezone, timedelta\n"
                              "t = datetime.now(timezone.utc) + timedelta(hours=3)\n"
                              "u = datetime.now(timezone.utc)",
                              None),  # This one should be **accepted** (UTC is explicit)
    }

    def test_invalid_forms_are_reported(self):
        """Verifies invalid forms are reported."""
        for label, (src, needle) in self.INVALID_SNIPPETS.items():
            if needle is None:
                continue
            with self.subTest(form=label):
                rep = audit_now_calls(src)
                self.assertTrue(rep["problems"], f"فرم نادرست «{label}» گرفته نشد")
                self.assertTrue(any(needle in p for p in rep["problems"]),
                                f"پیام گارد برای «{label}» گویا نیست: {rep['problems']}")

    def test_utc_plus_timedelta_is_still_accepted(self):
        """Verifies utc plus timedelta is still accepted."""
        src = self.INVALID_SNIPPETS["calculated offset"][0]
        rep = audit_now_calls(src)
        self.assertEqual([], rep["problems"],
                         "گارد فرمی را رد کرد که خودش UTC صریح می‌گیرد: " + str(rep["problems"]))

    def test_guard_would_have_caught_the_historical_d6_bug(self):
        """Reproducing the actual bug before D.6 (lines 272 and 306 of the same module)."""
        buggy = (
            "from datetime import datetime\n"
            "def live_branch():\n"
            "    timestamp = datetime.now().astimezone().strftime('%Y-%m-%dT%H:%M:%SZ')\n"
            "    return timestamp\n"
            "def dead_branch():\n"
            "    timestamp = datetime.utcnow().isoformat(timespec='seconds') + 'Z'\n"
            "    return timestamp\n"
        )
        rep = audit_now_calls(buggy)
        joined = " | ".join(rep["problems"])
        self.assertIn("astimezone", joined, "گارد باید `now().astimezone()` را بگیرد")
        self.assertIn("utcnow()", joined, "گارد باید `utcnow()` را بگیرد")
        self.assertIn("بدون آرگومان", joined, "گارد باید `now()` بدون آرگومان را بگیرد")
        self.assertEqual([], rep["accepted"], "هیچ کدام از فرم‌های باگ‌دار نباید پذیرفته شود")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
