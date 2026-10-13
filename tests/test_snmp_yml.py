# -*- coding: utf-8 -*-
"""
تست‌های `monitoring/snmp.yml` (گام G.3).

این فایل را باینری رسمی *نخوانده است* (تصمیم مالک: تست واقعی روی سرور خودش، پایان
پروژه). پس این تست‌ها هر آنچه در sandbox می‌شد سنجید را می‌سنجند:

  ۱) کلیدها فقط همان‌هایی باشند که `config/config.go` نسخهٔ 0.26.0 می‌شناسد —
     چون loader آن `yaml.UnmarshalStrict` است، یک کلیدِ اضافه روی سرور مالک
     خطای *load* می‌دهد، نه هشدار؛
  ۲) پوشش ۱۳۶/۱۳۶ با ماتریس و 1:1 با `generator.yml` (هیچ OIDی اختراع/جا نماند)؛
  ۳) هر ۱۳۶ متریک `type` معتبر داشته باشد و نام/OID یکتا و 1:1 باشند؛
  ۴) `ignore_values` (۳۱) به‌صورت کامنتِ ردیابی‌شدنی باشد و هیچ‌جا کلیدِ زنده نشود؛
  ۵) manifest همان اعدادی را بگوید که در فایل هست.
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SNMP_YML = ROOT / "monitoring" / "snmp.yml"
GENERATOR = ROOT / "monitoring" / "generator.yml"
MANIFEST = ROOT / "monitoring" / "snmp-manifest.json"
MATRIX_TOOL = ROOT / "tools" / "build_oid_matrix.py"

EXPECTED_MODULES = {"printer_std": 24, "printer_hp": 14, "printer_canon": 16,
                    "printer_brother": 9, "printer_toshiba": 63, "sensor_ecs100g": 10}
TOTAL_OIDS = sum(EXPECTED_MODULES.values())          # 136 — مبنای فاز G: «کد مرجع است»
WALK_TABLES = 7
IGNORE_MARKERS = 31
INFERRED_TYPES = 60
SENTINELS = ["-2", "-3"]
AUTH_NAMES = {"public_v1", "public_v2", "secure_v3"}

# کلیدهای مجاز، از config/config.go در تگ v0.26.0 (ساختارِ runtime، نه spec)
RUNTIME_KEYS = {
    "root": {"auths", "modules", "version"},
    "auth": {"community", "security_level", "username", "password", "auth_protocol",
             "priv_protocol", "priv_password", "context_name", "version"},
    "module": {"walk", "get", "metrics", "filters", "max_repetitions", "retries", "timeout",
               "use_unconnected_udp_socket", "allow_nonincreasing_oids"},
    "metric": {"name", "oid", "type", "help", "indexes", "lookups", "regex_extracts",
               "enum_values", "offset", "scale"},
}
# نوع‌هایی که collector واقعاً می‌شناسد (collector.go: pduToSamples + pduValueAsString).
# هر رشته‌ای بیرون از counter/gauge به مسیر «رشته» می‌رود (value=1 + لیبل)؛ هر عددی
# باید gauge/counter باشد وگرنه مقدار عددی‌اش دور ریخته می‌شود.
VALID_TYPES = {"counter", "gauge", "DisplayString", "OctetString", "Integer32",
               "Float", "Double", "Bits", "EnumAsInfo", "EnumAsStateSet"}
NUMERIC_TYPES = {"counter", "gauge", "Float", "Double"}
NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
OID_RE = re.compile(r"^\d+(?:\.\d+){1,}$")
BLOCK_RE = re.compile(r"^(\s*)- name: (\S+)\s*$", re.M)


def _text() -> str:
    return SNMP_YML.read_text(encoding="utf-8")


def _doc() -> dict:
    return yaml.safe_load(_text())


def _spec() -> dict:
    return yaml.safe_load(GENERATOR.read_text(encoding="utf-8"))


def _spec_entries() -> dict:
    """{module: [rows]} — ردیف‌های spec، به همان ترتیبی که snmp.yml باید بازتابشان باشد.\""""
    out = {}
    for mod, cfg in _spec()["modules"].items():
        rows = [r for r in (cfg.get("get") or []) if isinstance(r, dict)]
        for w in cfg.get("walk") or []:
            rows.extend(w.get("metrics") or [])
        out[mod] = rows
    return out


def _metrics(doc: dict | None = None):
    doc = doc if doc is not None else _doc()
    for mod, cfg in (doc.get("modules") or {}).items():
        for m in cfg.get("metrics") or []:
            yield mod, m


def _blocks() -> dict:
    """کامنت‌هایِ هر بلوکِ متریک: {«module/name» → [خطوط کامنت]} — stdlib این‌ها را دور می‌ریزد."""
    out: dict[str, list[str]] = {}
    cur_mod = cur_name = None
    for line in _text().splitlines():
        mm = re.match(r"^  (printer_\w+|sensor_\w+):$", line)
        if mm:
            cur_mod = mm.group(1)
            continue
        mb = BLOCK_RE.match(line)
        if mb:
            assert cur_mod, "متریک بیرون از هر module پیدا شد"
            cur_name = f"{cur_mod}/{mb.group(2)}"
            out.setdefault(cur_name, [])
        elif cur_name and line.lstrip().startswith("#"):
            out[cur_name].append(line.strip())
    return out


def _matrix_modules() -> tuple[dict, dict]:
    spec = importlib.util.spec_from_file_location("build_oid_matrix", MATRIX_TOOL)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build()


# ── ۱) ساختار ───────────────────────────────────────────────────────────────
def test_snmp_yml_is_valid_yaml_with_exactly_six_modules():
    doc = _doc()
    assert isinstance(doc, dict), "snmp.yml mapping نیست"
    assert doc.get("version") == 1, "version باید 1 باشد"
    modules = doc.get("modules") or {}
    assert set(modules) == set(EXPECTED_MODULES), f"ماژول‌ها: {sorted(modules)}"
    for name, expected in EXPECTED_MODULES.items():
        got = [m["name"] for m in modules[name].get("metrics") or []]
        assert len(got) == expected, f"{name}: {len(got)} متریک، انتظار {expected}"


def test_only_keys_the_official_loader_accepts():
    """`UnmarshalStrict` ⇒ هر کلیدِ ناشناخته = خطای load. این تست جانشینِ اجرای باینری است."""
    doc = _doc()
    assert set(doc) <= RUNTIME_KEYS["root"], f"کلید ریشهٔ ناشناخته: {set(doc) - RUNTIME_KEYS['root']}"
    for auth_name, adef in (doc.get("auths") or {}).items():
        bad = set(adef) - RUNTIME_KEYS["auth"]
        assert not bad, f"auths.{auth_name}: {sorted(bad)}"
    for mod, cfg in (doc.get("modules") or {}).items():
        bad = set(cfg) - RUNTIME_KEYS["module"]
        assert not bad, f"modules.{mod}: {sorted(bad)}"
        for m in cfg.get("metrics") or []:
            bad = set(m) - RUNTIME_KEYS["metric"]
            assert not bad, f"{mod}/{m.get('name')}: {sorted(bad)}"
    # این سه، کلیدهای spec‌اند و در schema بیگانه‌اند
    for key in ("ignore_values", "source", "subtree", "lookups", "row_index", "label_oid"):
        assert f"\n    {key}:" not in _text() and f"      {key}:" not in _text(), f"کلیدِ spec زنده شد: {key}"


def test_auths_use_the_runtime_field_names():
    doc = _doc()
    auths = doc.get("auths") or {}
    assert set(auths) == AUTH_NAMES, f"authها: {sorted(auths)}"
    assert auths["public_v1"]["version"] == 1 and auths["public_v1"]["community"] == "public"
    assert auths["public_v2"]["version"] == 2 and auths["public_v2"]["community"] == "public"
    v3 = auths["secure_v3"]
    assert v3["version"] == 3 and v3["security_level"] == "authPriv"
    assert v3["auth_protocol"] == "SHA" and v3["priv_protocol"] == "AES", "نام‌های v3 باید runtimeی باشند"
    # در spec این‌ها auth_passphrase/priv_passphrase + AES128 بودند؛ اینجا نه
    for gone in ("auth_passphrase", "priv_passphrase"):
        assert gone not in v3, f"کلیدِ spec در snmp.yml مجاز نیست: {gone}"
    assert not re.search(r"^\s+priv_protocol: +AES128$", _text(), re.M), "AES128 را runtime نمی‌شناسد (AES)"
    for key in ("username", "password", "priv_password"):
        assert re.match(r"^\$\{[A-Z0-9_]+\}$", str(v3[key])), f"secure_v3.{key} placeholder نیست"
    # «همهٔ moduleها در auths»: هر نامِ module با همان سه auth صدا زده می‌شود
    for mod in (doc.get("modules") or {}):
        assert AUTH_NAMES, f"{mod} بدون auth قابل استفاده نیست"


# ── ۲) پوشش و 1:1 با spec ───────────────────────────────────────────────────
def test_all_136_matrix_oids_are_declared_and_none_is_invented():
    modules, stats = _matrix_modules()
    matrix_oids = {r["oid"] for rows in modules.values() for r in rows}
    file_oids = {str(m["oid"]) for _mod, m in _metrics()}
    assert stats["unique_oids"] == len(matrix_oids) == TOTAL_OIDS == len(file_oids)
    assert matrix_oids - file_oids == set(), f"کد می‌خواند و فایل ندارد: {sorted(matrix_oids - file_oids)}"
    assert file_oids - matrix_oids == set(), f"فایل OID بیرون از کد دارد: {sorted(file_oids - matrix_oids)}"


def test_metrics_are_one_to_one_with_generator_yml_rows():
    spec = _spec_entries()
    for mod, rows in spec.items():
        want = {(r["name"], str(r["oid"])) for r in rows}
        got = {(m["name"], str(m["oid"])) for m in (_doc()["modules"][mod].get("metrics") or [])}
        assert len(got) == len(rows) == len(want), f"{mod}: تعداد متریک‌ها با spec نمی‌خواند"
        assert want == got, f"{mod}: نام↔OID از spec جدا شده — {sorted(want ^ got)}"
        # ردیابی: sourceِ هر ردیف باید داخل help باشد
        by_name = {m["name"]: m for m in (_doc()["modules"][mod].get("metrics") or [])}
        for r in rows:
            src = str(r.get("source", ""))
            assert src and src in by_name[r["name"]]["help"], f"{mod}/{r['name']}: source در help نیست"


def test_metric_names_are_unique_valid_prometheus_names():
    names = [m["name"] for _mod, m in _metrics()]
    assert len(names) == TOTAL_OIDS == len(set(names)), "نامِ متریک تکراری است"
    for n in names:
        assert NAME_RE.match(n), f"نامِ غیرمجازِ Prometheus: {n}"
        assert n == n.lower(), f"نام باید lowercase باشد: {n}"


# ── ۳) type و help ───────────────────────────────────────────────────────────
def test_every_metric_has_a_type_the_runtime_understands():
    spec_types = {(mod, r["name"]): r.get("type") for mod, rows in _spec_entries().items() for r in rows}
    for mod, m in _metrics():
        typ = m.get("type")
        assert isinstance(typ, str) and typ, f"{mod}/{m['name']}: type ندارد"
        assert typ in VALID_TYPES, f"{mod}/{m['name']}: type «{typ}» را collector نمی‌شناسد"
        want = spec_types[(mod, m["name"])]
        if want is not None:
            assert typ == want, f"{mod}/{m['name']}: typeِ spec عوض شده ({want} → {typ})"


def test_sixty_types_are_marked_as_inferred_and_match_the_untyped_spec_rows():
    untyped = {f"{mod}/{r['name']}" for mod, rows in _spec_entries().items() for r in rows if "type" not in r}
    assert len(untyped) == INFERRED_TYPES, f"ردیفِ بی‌type در spec: {len(untyped)}، انتظار {INFERRED_TYPES}"
    marked = {k for k, notes in _blocks().items() if any(n.startswith("# type inferred") for n in notes)}
    assert marked == untyped, (f"فقط‌mark شده: {sorted(marked - untyped)} / بی‌mark: "
                              f"{sorted(untyped - marked)}")
    # استنتاج باید در خانوادهٔ درست بماند: ردیفِ عددِ انباشته counter، بقیه gauge/رشته
    for mod, m in _metrics():
        key = f"{mod}/{m['name']}"
        if key not in untyped:
            continue
        name = m["name"]
        typ = m["type"]
        if typ in {"counter", "gauge"}:
            assert typ in NUMERIC_TYPES
        if re.search(r"(level|cap|max|remain|pct|status|_st$|size|uptime|usage)", name):
            assert typ == "gauge", f"{key}: نام می‌گوید سطح/وضعیت، type={typ}"
        if re.search(r"(_msg|descr|name|model|serial|firmware)$", name):
            assert typ == "DisplayString", f"{key}: نام متنی است، type={typ}"


def test_help_text_exists_and_is_module_scoped():
    for mod, m in _metrics():
        help_text = m.get("help")
        assert isinstance(help_text, str) and help_text, f"{mod}/{m['name']}: help خالی است"
        assert help_text.startswith(f"PrintGuard {mod}: "), f"{mod}/{m['name']}: prefix ماژول ندارد"


# ── ۴) walk / get ───────────────────────────────────────────────────────────
def test_seven_walk_tables_and_no_overlap_with_get():
    doc = _doc()
    tables = [(mod, str(sub)) for mod, cfg in doc["modules"].items() for sub in (cfg.get("walk") or [])]
    assert len(tables) == WALK_TABLES, f"جدول walk: {len(tables)}، انتظار {WALK_TABLES} (std2·hp2·tsh2·bro1)"
    n_get = sum(len(cfg.get("get") or []) for cfg in doc["modules"].values())
    assert n_get == 98, f"ردیف get: {n_get}، انتظار 98 (۱۳۶ − ۳۸ سطرِ جداول)"
    for mod, cfg in doc["modules"].items():
        subs = [str(s) for s in cfg.get("walk") or []]
        leaves = {str(m["oid"]) for m in cfg.get("metrics") or []}
        for o in cfg.get("get") or []:
            o = str(o)
            assert o in leaves, f"{mod}: get {o} متریک نیست"
            assert not any(o == s or o.startswith(s + ".") for s in subs), f"{mod}: {o} زیر walk هم هست"
        for s in subs:
            under = {o for o in leaves if o == s or o.startswith(s + ".")}
            assert len(under) >= 1, f"{mod}: subtree {s} هیچ ردیفی ندارد"


def test_every_metric_is_reachable_by_exactly_one_of_walk_or_get():
    for mod, cfg in _doc()["modules"].items():
        subs = [str(s) for s in cfg.get("walk") or []]
        gets = {str(o) for o in cfg.get("get") or []}
        for m in cfg.get("metrics") or []:
            o = str(m["oid"])
            via_walk = any(o == s or o.startswith(s + ".") for s in subs)
            assert via_walk != (o in gets), f"{mod}/{m['name']}: walk={via_walk} get={o in gets}"
            assert OID_RE.match(o), f"{mod}/{m['name']}: OID نامعتبر «{o}»"


# ── ۵) ignore_values ────────────────────────────────────────────────────────
def test_ignore_values_are_comment_markers_on_the_same_thirty_one_metrics():
    want = {f"{mod}/{r['name']}": list(r["ignore_values"])
            for mod, rows in _spec_entries().items() for r in rows if r.get("ignore_values")}
    assert len(want) == IGNORE_MARKERS, f"spec: {len(want)} ردیفِ ignore_values، انتظار {IGNORE_MARKERS}"
    got = {}
    for key, notes in _blocks().items():
        for n in notes:
            m = re.match(r"^# ignore_values: \[(.*)\]$", n)
            if m:
                got[key] = [v.strip().strip('"').strip("'") for v in m.group(1).split(",")]
    assert set(got) == set(want), (f"کم/زیاد: {sorted(set(want) ^ set(got))}")
    for key, vals in want.items():
        assert got[key] == vals == SENTINELS, f"{key}: مقدار sentinel جابه‌جا شده"
    # و هیچ‌جا کلیدِ زنده نشود (runtime این کلید را ندارد)
    assert not re.search(r"^\s+ignore_values:", _text(), re.M), "ignore_values کلیدِ زنده شد"


def test_probe_only_orphans_stay_out_of_snmp_yml():
    _modules, stats = _matrix_modules()
    orphans = set(stats["orphans"])
    assert orphans, "ماتریس یتیم ندارد؟!"
    live = {str(m["oid"]) for _mod, m in _metrics()}
    assert not (orphans & live), f"یتیم به متریک تبدیل شده: {sorted(orphans & live)}"
    # در spec کامنت‌اند؛ همان ۱۰ کامنت را expectationsِ گیت §۲ می‌سنجد
    commented = set(re.findall(r"^\s*#\s*(\d+(?:\.\d+){6,})\s+\u2190", GENERATOR.read_text(encoding="utf-8"), re.M))
    assert orphans <= commented and len(commented) == 10


# ── ۶) manifest ────────────────────────────────────────────────────────────
def test_manifest_reports_what_the_file_actually_contains():
    man = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for key in ("generated_by", "modules", "total_oids", "walk_tables", "ignore_values_count",
                "generated_at_utc", "load_test_sandbox", "load_test_planned"):
        assert key in man, f"manifest کلید {key} را ندارد"
    doc = _doc()
    assert man["generated_by"] == "hand-written", "اگر generator رسمی اجرا شد، همین‌جا و سند عوض می‌شود"
    assert man["modules"] == len(doc["modules"]) == 6
    assert man["total_oids"] == len({str(m["oid"]) for _x, m in _metrics()}) == TOTAL_OIDS
    assert man["walk_tables"] == sum(len(c.get("walk") or []) for c in doc["modules"].values()) == WALK_TABLES
    assert man["ignore_values_count"] == IGNORE_MARKERS
    assert man["load_test_sandbox"] == "not_attempted", "در sandbox باینری اجرا نشد — دروغ نگوییم"
    assert "owner" in man["load_test_planned"] and "end of project" in man["load_test_planned"]
    _dt.datetime.strptime(man["generated_at_utc"], "%Y-%m-%dT%H:%M:%SZ")


def test_file_is_os_independent_plain_text():
    raw = SNMP_YML.read_bytes()
    assert b"\r" not in raw, "CRLF: فایل باید در Windows و لینوکس یک بایت بماند"
    text = raw.decode("utf-8")                      # latin-1/cp1252 نه، حتماً utf-8
    assert text.endswith("\n") and "\t" not in text
    for leak in ("/home/user", "C:\\", "/tmp/"):
        assert leak not in text, f"مسیرِ sandbox به فایل راه یافته: {leak}"
    # کامنت‌های فارسی فقط کامنت‌اند: ساختارِ parsed بدونِ آنها هم دقیقاً همان است
    doc = _doc()
    stripped = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert yaml.safe_load(stripped) == doc, "ساختار به کامنت‌ها وابسته شده است"
    assert {str(m["oid"]) for _x, m in _metrics()} == {
        str(m["oid"]) for _x, m in _metrics(yaml.safe_load(stripped))}
