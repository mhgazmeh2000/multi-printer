# -*- coding: utf-8 -*-
"""
تست‌های زمان رویداد کارتریج (فاز D — کار D.6؛ گارد مقاوم — فاز E.0، کار E.0.14).

زمینه: `core/cartridge_change_engine.py` دو مبنای زمانی متفاوت تولید می‌کرد:
  • خط ۲۷۲ (مسیرِ زنده، مشترکِ هر دو مسیر شواهد): ساعت محلی + پسوند جعلی `Z`
    ⇒ برای سرور با `TZ=Asia/Tehran` کج‌فهمی ۳:۳۰ ساعت (اثبات‌شده: انحراف ۲۱۰ دقیقه).
  • خط ۳۰۶ (داخل بلوک غیرقابل‌دسترس ۲۹۵–۳۲۸): UTC واقعی بود ولی `datetime.utcnow()`
    از پایتون ۳.۱۲ deprecated است.

تست‌ها:
  ۱) مسیر شواهد chip_id («برنددار»): timestamp باید UTC واقعی باشد.
  ۲) مسیر شواهد شمارنده/سطح/عمر («غیربرند»): همان ادعاها.
  ۳) گارد ایستای AST روی خودِ ماژول: هیچ `utcnow()`، هیچ `now().astimezone()`، و هر
     فراخوانی `datetime.now(...)` باید **یک منطقه‌ی زمانی UTC معتبر** بگیرد.
     از فاز E.3 سخت‌گیرتر هم شده: ماژول هیچ فراخوانی مستقیم `now` ندارد و **هر دو**
     نقطهٔ تولید timestamp از `core.time_utils.utc_now_iso()` (فرمت استاندارد
     `…T…ffffffZ`) می‌آیند — گارد `audit_shared_clock_usage`.

**مستقل از TZ (گام E.0.3-pre):** تست‌های ۱ و ۲ هیچ skip ندارند و در هر TZ اجرا
می‌شوند. ادعای اصلی آن‌ها این است: رشته‌ی تولیدشده را با قالب `...Z` تجزیه کن،
نتیجه باید با UTCِ همین لحظه کمتر از ۱۰ ثانیه اختلاف داشته باشد. این ادعا باگِ
قدیمی (ساعت محلی + پسوند جعلی `Z`) را در هر TZ با offset غیرصفر می‌گیرد، چون در
آن حالت اختلاف با UTC برابر کلِ offset (مثلاً ۲۱۰ دقیقه) می‌شود. تنها حالتی که
«محلی» و «UTC» عملاً یک چیزند `TZ=UTC` است (offset صفر ⇒ تفکیک‌ناپذیر به‌ذات)، و
همان حالت را گارد ایستای تست ۳ پوشش می‌دهد.

**گارد مقاوم به فرم‌ها (E.0.14):** گارد دیگر روی *شکل نوشتاری* قفل نمی‌کند؛ فرم‌های
گوناگونِ «UTC صریح» را می‌پذیرد و فقط فرم‌های **نادرست** را می‌گیرد:

    پذیرفته می‌شود:
      datetime.now(timezone.utc)        datetime.now(tz=timezone.utc)
      datetime.now(UTC)                 datetime.now(tz=UTC)
      datetime.now(ZoneInfo("UTC"))     datetime.now(tz=ZoneInfo("UTC"))
      datetime.now(datetime.timezone.utc)   # import datetime به‌جای from … import
      و همان‌ها با نام مستعار import (مثل `import datetime as dt` یا `timezone as tz2`)
    رد می‌شود:
      datetime.now()                    # naive ⇒ ۳:۳۰ خطا
      datetime.now(timezone(timedelta(hours=3, minutes=30)))   # تهران، نه UTC
      datetime.datetime.utcnow()        # deprecated + naive
      datetime.now()  در ادامه .astimezone()  ⇒ برچسب محلی با Z
      datetime.now().replace(tzinfo=timezone.utc)  # «برچسب زدن» به مقدار محلی

  همچنین فراخوانی‌هایی که **ربطی به datetime ندارند** (مثل `store.now()` یا تابعی به
  نام `now` که خودِ ماژول تعریف کرده) نادیده گرفته می‌شوند تا گارد مثبت کاذب ندهد.

اجرا:
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
# فاز E.3: فرمت قطعی پروژه — همیشه UTC با پسوند Z و **۶ رقم میکروثانیه**
# (طول ثابت ۲۷). پیش‌تر این فایل روی «ثانیه‌دقت بدون میکروثانیه» قفل بود و همین
# باعث می‌شد تست با نویسندهٔ جدید (utc_now_iso) قرمز بماند.
TS_FORMAT = ISO_Z_FORMAT

#: نام‌های رشته‌ای که به‌عنوان منطقه‌ی زمانی UTC پذیرفته می‌شوند.
UTC_ZONE_NAMES = frozenset({"UTC", "utc", "Etc/UTC", "Etc/GMT", "GMT", "Z"})


# ─────────────────────────────────────────────────────────────
# گارد AST (تابع خالص و آزمون‌پذیر)
# ─────────────────────────────────────────────────────────────

def _import_aliases(tree):
    """نام‌های به‌کاررفته در ماژول را برای datetime/timezone/UTC/ZoneInfo استخراج می‌کند.

    خروجی: dict با کلیدهای modules / classes / timezones / utc_names / zoneinfo_names.
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
    classes.add("datetime")            # نام متعارف کلاس، حتی اگر import در ماژول نباشد
    timezones.add("timezone")
    utc_names.add("UTC")
    zoneinfo_names.add("ZoneInfo")
    return {"modules": modules, "classes": classes, "timezones": timezones,
            "utc_names": utc_names, "zoneinfo_names": zoneinfo_names}


def _is_utc_timezone_expr(node, names):
    """آیا این عبارت، یک منطقه‌ی زمانی UTC است؟ توضیح رشته‌ای یا None."""
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

    # UTC (با هر نام مستعار)
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
    """آیا این عبارت «کلاس/شیء datetime» است که `.now()` رویش معنادار است؟"""
    if isinstance(node, ast.Name):
        return node.id in names["classes"] and node.id not in local_defs
    if isinstance(node, ast.Attribute) and node.attr == "datetime":
        return isinstance(node.value, ast.Name) and node.value.id in names["modules"]
    return False


def audit_now_calls(source):
    """گارد ایستا روی یک ماژول: هر `now`/`utcnow`/`astimezone` مشکوک را پیدا می‌کند.

    خروجی: dict با کلیدهای
      accepted : [(lineno, توضیح فرم)]  — فراخوانی‌های now با UTC صریح
      problems : [پیام]                — فرم‌های نادرست
      skipped  : [(lineno, نام)]       — فراخوانی‌هایی که ربطی به datetime ندارند
    """
    tree = ast.parse(source)
    names = _import_aliases(tree)
    local_defs = {n.name for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    accepted, problems, skipped = [], [], []

    def _checkable_now(call):
        """(قابل‌بررسی؟, توضیح) برای یک Call که func.attr/id آن «now» است."""
        f = call.func
        if isinstance(f, ast.Attribute) and f.attr == "now":
            if _is_datetime_like(f.value, names, local_defs):
                return True
            if isinstance(f.value, ast.Name) and f.value.id not in local_defs:
                # مثل self.now()/store.now() ⇒ ربطی به datetime ندارد
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
    """گارد ایستا (E.3): «الآن» این ماژول باید فقط از هِلپر مرکزی زمان بیاید.

    خروجی:
      imports    : bool — آیا `utc_now_iso` از `core.time_utils` import شده است؟
      call_lines : [int] — خطوط فراخوانی `utc_now_iso(...)`

    چرا لازم شد؟ تا فاز E.2 این گارد روی خودِ `datetime.now(timezone.utc)` در دو
    خط تولید timestamp قفل بود؛ از E.3 هر دو خط به `utc_now_iso()` رسیدند. گارد
    جدید همان تضمین را حفظ می‌کند (هر دو نقطهٔ تولید timestamp از یک منبع واحد و
    استاندارد) و در عین حال شکل نوشتاری را قفل نمی‌کند.
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
    """مسیر شواهد chip_id (کارتریج برنددار) — رویداد CONFIRMED می‌دهد."""
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
    """مسیر شواهد شمارنده/سطح/عمر (کارتریج غیربرند) — رویداد POSSIBLE می‌دهد."""
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
    """ادعاهای مشترک دو مسیر شواهد."""

    def assert_event_timestamp_is_utc(self, result, now_utc=None):
        event = result.get("event") or {}
        ts = event.get("timestamp") or result.get("timestamp")
        self.assertTrue(ts, "رویداد باید timestamp داشته باشد")
        self.assertTrue(ts.endswith("Z"), f"timestamp باید با Z تمام شود: {ts!r}")
        # فاز E.3: فرمت استاندارد پروژه — ۶ رقم میکروثانیه و طول ثابت ۲۷
        # (هم‌چیدمانی واژه‌به‌واژه‌ای با ترتیب زمانی ⇒ مقایسه‌ی رشته‌ای امن).
        self.assertEqual(ISO_Z_LENGTH, len(ts),
                         f"طول timestamp باید {ISO_Z_LENGTH} باشد ({ts!r})")
        try:
            parsed = datetime.strptime(ts, TS_FORMAT).replace(tzinfo=timezone.utc)
        except ValueError as exc:
            self.fail(f"timestamp با فرمت استاندارد پروژه {ISO_Z_FORMAT} نمی‌خواند: "
                      f"{ts!r} ({exc})")
        # رفت‌وبرگشت با تنها سریال‌ساز پروژه باید بی‌تغییر بماند (قرارداد E.3).
        self.assertEqual(ts, to_utc_z(parse_utc(ts)),
                         f"timestamp با هِلپر مرکزی زمان سازگار نیست: {ts!r}")
        ref = now_utc or datetime.now(timezone.utc)
        delta = abs((ref - parsed).total_seconds())
        self.assertLess(delta, 10.0,
                        f"timestamp باید UTCِ همین لحظه باشد (اختلاف {delta:.0f}s، "
                        f"مقدار={ts!r}) — انحراف بزرگ یعنی ساعت محلی با پسوند جعلی Z")


class BrandedBranchTimestampTests(_TimestampAssertions):
    """تست ۱ — مسیر شواهد chip_id."""

    def test_branded_branch_writes_real_utc(self):
        prev, curr = _branded_scenario()
        result = evaluate_cartridge_change(prev, curr, reboot_detected=False, poll_gap_ok=True)
        self.assertTrue(result["should_emit"])
        self.assertIn("chip_id", result["detection_method"])
        self.assert_event_timestamp_is_utc(result)


class GenericBranchTimestampTests(_TimestampAssertions):
    """تست ۲ — مسیر شواهد شمارنده/سطح/عمر (بدون chip_id)."""

    def test_generic_branch_writes_real_utc(self):
        prev, curr = _generic_scenario()
        result = evaluate_cartridge_change(prev, curr, reboot_detected=False, poll_gap_ok=True)
        self.assertTrue(result["should_emit"])
        self.assertNotIn("chip_id", result["detection_method"])
        self.assert_event_timestamp_is_utc(result)


class ModuleTimeSourceGuardTests(unittest.TestCase):
    """
    تست ۳ — گارد ایستا (AST) روی `core/cartridge_change_engine.py`.

    سه بررسی:
      ۱) هیچ `utcnow()` نباشد (deprecated + naive)؛
      ۲) هیچ `now(...).astimezone(...)` یا `now().replace(tzinfo=…)` نباشد؛
      ۳) هر `datetime.now(...)` یک منطقه‌ی زمانی **صریح UTC** بگیرد — با پذیرش همه‌ی
         فرم‌های معتبر (positional/keyword، نام مستعار import، `ZoneInfo("UTC")`).
      ۴) هر دو خطِ تولید timestamp (۲۷۲ زنده، ۳۰۶ داخل کد مرده) در همان مجموعه باشند
         تا حتی کد مرده هم قفل بماند.
    """

    @classmethod
    def setUpClass(cls):
        with open(MODULE_PATH, encoding="utf-8") as fh:
            cls.source = fh.read()
        cls.report = audit_now_calls(cls.source)
        cls.clock = audit_shared_clock_usage(cls.source)

    def test_no_deprecated_utcnow_and_no_relabelling(self):
        bad = [p for p in self.report["problems"]
               if "utcnow()" in p or "astimezone" in p or "replace(tzinfo" in p]
        self.assertEqual([], bad, "فرم‌های زمانی نادرست در ماژول: " + " | ".join(bad))

    def test_every_now_call_has_explicit_utc(self):
        """هیچ `datetime.now(...)` بی‌قید و بندی در ماژول نمانده باشد.

        از E.3 این ماژول هیچ فراخوانی مستقیم `now` ندارد: «الآن» از
        `core.time_utils.utc_now_iso()` می‌آید (تست بعدی). اگر روزی کسی مستقیم
        `datetime.now(...)` اضافه کند، این تست او را متوقف می‌کند تا تصمیم آگاهانه
        باشد: یا از هِلپر مرکزی استفاده کند، یا این گارد را با دلیل به‌روز کند.
        """
        self.assertEqual([], self.report["problems"],
                         "مشکل در فراخوانی‌های now: " + " | ".join(self.report["problems"]))
        self.assertEqual([], self.report["accepted"],
                         "این ماژول باید «الآن» را فقط از utc_now_iso بگیرد؛ "
                         f"فراخوانی‌های مستقیم now: {self.report['accepted']}")

    def test_both_timestamp_lines_are_locked(self):
        """هر دو نقطهٔ تولید timestamp (زنده و کد مرده) از منبع واحد پروفایل‌شده."""
        self.assertTrue(self.clock["imports"],
                        "`utc_now_iso` باید از core.time_utils import شده باشد")
        self.assertGreaterEqual(
            len(self.clock["call_lines"]), 2,
            "هر دو خط تولید timestamp باید از utc_now_iso استفاده کنند؛ "
            f"خطوط فعلی: {self.clock['call_lines']}")

    def test_no_local_now_left_in_source(self):
        self.assertNotIn("datetime.now()", self.source.replace(" ", ""),
                         "`datetime.now()` بدون آرگومان در ماژول مانده است")


class GuardAcceptsValidFormsTests(unittest.TestCase):
    """گارد فرم‌های معتبر UTC را می‌پذیرد (بدون مثبت/منفی کاذب) — E.0.14."""

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
        for label, src in self.VALID_SNIPPETS.items():
            with self.subTest(form=label):
                rep = audit_now_calls(src)
                self.assertEqual([], rep["problems"], f"فرم معتبر «{label}» رد شد: {rep['problems']}")
                self.assertEqual(1, len(rep["accepted"]), f"فرم «{label}» پذیرفته نشد")

    def test_unrelated_now_calls_are_skipped(self):
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
    """گارد فرم‌های نادرست را می‌گیرد — E.0.14."""

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
                              None),  # این یکی باید **پذیرفته** شود (UTC صریح است)
    }

    def test_invalid_forms_are_reported(self):
        for label, (src, needle) in self.INVALID_SNIPPETS.items():
            if needle is None:
                continue
            with self.subTest(form=label):
                rep = audit_now_calls(src)
                self.assertTrue(rep["problems"], f"فرم نادرست «{label}» گرفته نشد")
                self.assertTrue(any(needle in p for p in rep["problems"]),
                                f"پیام گارد برای «{label}» گویا نیست: {rep['problems']}")

    def test_utc_plus_timedelta_is_still_accepted(self):
        src = self.INVALID_SNIPPETS["calculated offset"][0]
        rep = audit_now_calls(src)
        self.assertEqual([], rep["problems"],
                         "گارد فرمی را رد کرد که خودش UTC صریح می‌گیرد: " + str(rep["problems"]))

    def test_guard_would_have_caught_the_historical_d6_bug(self):
        """بازتولید باگ واقعیِ قبل از D.6 (خطوط ۲۷۲ و ۳۰۶ همان ماژول)."""
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
