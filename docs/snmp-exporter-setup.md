# SNMP Exporter — setup guide (phase G)

> **مبنا:** این سند در طول فاز G رشد کرده است: §۰ تا §۳ از session گام G.2 (کامیت
> `91acf16`)، §۴ و §۵ از G.3 (`d81d6d7`، `ab979df`)، §۶ تا §۸ از G.4 (`85670ba`)، §۹ از G.5
> (`699228c`، با اصلاحِ TD-016 در `37e78f2`) و §۱۰ از G.6 (`0124e0a`). همهٔ عددهای هر بخش از
> خروجیِ **همان** session اند؛ هیچ عددی از پیش «فرض» نشده است و هیچ عددِ زمانی (مدت/تخمین)
> در این سند نمی‌آید. **تصمیم‌ها، دلیل‌ها و «جایگزین‌هایِ ردشده» در
> `docs/snmp-exporter-decision.md`‌اند (گام G.7).**

---

## 0. What exists and what does not yet

| فایل/قابلیت | وضعیت | توضیح |
|---|---|---|
| `monitoring/generator.yml` | ✔ هست | spec شش‌ماژوله، ۱۳۶ ردیف، مبنای گام بعد |
| `tools/build_oid_matrix.py` + `docs/oid-matrix.md` | ✔ هست | ماتریس OID **از کد** تولید می‌شود |
| `tools/check_oid_coverage.py` | ✔ هست | گیت شمارش (exit 0/1) |
| `tools/build_snmp_yml.py` | ✔ هست | spec → `snmp.yml`؛ deterministic + `--check` (گیتِ کهنه‌بودن) |
| `tests/test_generator_yml.py`, `tests/test_oid_matrix.py` | ✔ هست | ۷ + ۵ تست |
| `PyYAML` در `requirements-dev.txt` | ✔ هست | گیت‌ها با `yaml.safe_load` کار می‌کنند |
| `monitoring/snmp.yml` | ✔ هست | configِ runtime، ۸۵۴ خط، ۱۳۶ متریک — از `generator.yml` با `tools/build_snmp_yml.py` (§۴) |
| `monitoring/snmp-manifest.json` | ✔ هست | provenance: چه ساخته شده و چه چیزی **اجرا نشده** |
| `tests/test_snmp_yml.py` | ✔ هست | ۱۵ تست؛ جانشینِ «load با باینری» (کلیدهای strict، 1:1 با spec) |
| `tests/test_build_snmp_yml.py` | ✔ هست | ۱۳ تست؛ باثبات‌بودنِ بازتولید، نگاشتِ v3، دروازهٔ INFER |
| `tests/test_install_ps1.py` | ✔ هست | ۱۳ تست؛ ساختار/متنِ اسکریپتِ نصب (parser اگر PowerShell بود) |
| load شدنِ `snmp.yml` با باینری رسمی | ✘ نشده | موکول به سرور مالک در پایان پروژه (§۵) — `load_test_sandbox: not_attempted` |
| `windows/install-snmp-exporter.ps1` | ✔ هست | اسکریپتِ نصبِ سرویس با NSSM — **اجرا نشده**؛ راهنمایش `docs/windows-services.md` |
| `core/exporter_client.py` / شاخهٔ hybrid در poller | ✘ نیست | گام بعد |
| متغیر `USE_SNMP_EXPORTER` | ✔ هست | `ENV_FLAG` در `core/exporter_client.py`؛ **پیش‌فرض ۰**؛ مصرف در `core/poller.py` (§۹). `SNMP_EXPORTER_URL` هم همان فایل |
| fixtureهای پاسخِ exporter | ✔ ۴ فایل، **دست‌نویس** | `tests/fixtures/exporter_response_{sample,std,canon,canon_std}.txt` — capture نیست (§۹.۱) |
| `tests/test_exporter_*.py` + `tests/test_poller_exporter_flag.py` + `tests/test_hybrid_full_path.py` | ✔ ۹۱ تست | ۱۶ + ۳۸ + ۱۶ + ۹ + ۱۲، بدونِ skip (§۹.۲) |
| `tools/compare_snmp_sources.py` + ۳ fixtureِ `compare_snmp_pair_*.json` | ✔ هست | ابزارِ مقایسهٔ دو منبع، ۲۴ تست روی دادهٔ مصنوعی؛ عددِ واقعیِ PDU/اختلاف در repo نیست (§۱۰) |

**نتیجهٔ عملی:** در حال حاضر **هیچ ترافیک SNMP تازه‌ای تولید نمی‌شود**. `snmp.yml` یک
پیکربندیِ آماده است که هیچ سرویسی آن را مصرف نمی‌کند؛ اسکریپتِ نصب نوشته شده ولی **روی
هیچ ماشین اجرا نشده**، و فعال‌سازی روی دستگاه تا گام‌های بعد و تأیید مالک انجام نمی‌شود.

---

## 1. How `monitoring/generator.yml` works

این فایل **spec پروژه** است، نه ورودیِ باینریِ `snmp_exporter/generator` — چون MIBهای
vendor در مخزن نیست و `snmp.yml` دستی نوشته می‌شود. ساختارش:

```yaml
version: 1
auths:
  public_v1: {community: public, version: 1}     # نام‌ها = f"public_{snmp_version}" (قراردادِ فاز G؛ monitoring/generator.yml:35)
  public_v2: {community: public, version: 2}
  secure_v3:                                      # placeholder؛ مقدار از env
    version: 3
    username: "${SNMP_V3_USERNAME}"
    security_level: authPriv
    auth_protocol: SHA
    auth_passphrase: "${SNMP_V3_AUTH_PASSPHRASE}"
    priv_protocol: AES128
    priv_passphrase: "${SNMP_V3_PRIV_PASSPHRASE}"
modules:
  printer_std:
    walk:                                        # جدول اندیس‌دار: یک PDU، N سطر
      - name: supplies_table
        subtree: 1.3.6.1.2.1.43.11.1.1           # باید = LCPِ ردیف‌های خودش باشد (گیت می‌سنجد)
        lookups:                                  # index → description، از ردیف‌های ماتریس
          - {row_index: 1, label_oid: 1.3.6.1.2.1.43.11.1.1.6.1.1}
        metrics:
          - oid: 1.3.6.1.2.1.43.11.1.1.9.1.1
            name: toner_remain_1
            type: gauge
            ignore_values: ["-2", "-3"]
            source: core/oid/catalog.py:30         # = «منبع» همان ردیف در ماتریس
    get:                                         # ثابت‌ها (بدون اندیس)
      - {oid: 1.3.6.1.2.1.1.1.0, name: sys_descr, type: DisplayString, source: core/oid/catalog.py:8}
```

وضعیت فعلی فایل (از `tools/check_oid_coverage.py`، خروجی ۰):

| ماژول | ردیف | `get` | ردیفِ پوشیده‌شده با `walk` | جدول walk | `ignore_values` | `lookups` |
|---|---|---|---|---|---|---|
| `printer_std` | 24 | 11 | 13 | 2 | 8 | 4 |
| `printer_hp` | 14 | 10 | 4 | 2 | 3 | 0 |
| `printer_canon` | 16 | 16 | 0 | 0 | 2 | 0 |
| `printer_brother` | 9 | 4 | 5 | 1 | 2 | 0 |
| `printer_toshiba` | 63 | 47 | 16 | 2 | 16 | 0 |
| `sensor_ecs100g` | 10 | 10 | 0 | 0 | 0 | 0 |
| **جمع** | **136** | **98** | **38** | **7** | **31** | **4** |

سه قاعدهٔ مهم:

1. **هیچ OIDی اختراع نمی‌شود.** هر ردیف یک `source:` به `file:line` کد دارد و گیت،
   `source:` را با همان ردیفِ ماتریس مقایسه می‌کند. ماتریس هم فقط کد را می‌خواند
   (`core/oid/catalog.py`، `core/snmp/oid_map.py`، `core/collectors/*.py`،
   `core/enhanced_collector.py`).
2. **`subtree` باید طولانی‌ترین پیشوند مشترکِ ردیف‌های خودش باشد.** پس نوشتن یک
   درختِ «سلیقه‌ای» ممکن نیست؛ یا ردیف‌ها زیرش‌اند یا گیت قرمز می‌کند. تنها OIDهای
   مجازِ بیرونِ ماتریس، همین `subtree`هایند.
3. **نام متریک از برچسبِ کد می‌آید.** اگر برچسب یک کلمهٔ مبهم باشد (نام برند یا
   کوتاه‌تر از ۶ حرف مثل `cap`)، نام ظرف پیشوند می‌شود:
   `brother` داخل `CANDIDATE_SUBTREES` ⇒ `candidate_subtrees_brother`.

### `ignore_values: ["-2", "-3"]` کجاها می‌نشیند

فقط روی متریک‌های **عددیِ سطح/درصد**؛ قانون در `needs_ignore()` (همان گیت) نوشته شده:
دستهٔ کاتالوگ `supply`، یا نامِ شامل `level|remain|cap|max|pct|toner|drum|black`،
یا منبعِ `core/enhanced_collector.py` (که همه‌اش از حلقه‌ای می‌خواند که فقط
`0 <= v <= 100` را می‌پذیرد: `enhanced_collector.py:229-235` و `244-250`).
متریک‌های `DisplayString` هرگز sentinel نمی‌گیرند (نامِ کارتریج، `-3` نیست).

منطقِ دامنه که این عدد را تأیید می‌کند — و **دست‌نخورده می‌ماند**:
`hp.py:258,260` (`lvl == -2` / `lvl == -3`)، `brother.py:182-183`،
و توضیح `brother.py:13` دربارهٔ خانوادگه‌ای که `level=-3` برمی‌گرداند.

---

## 2. Gates: how to tell that it is still correct

```bat
python tools\build_oid_matrix.py --check      :: سند ماتریس با کد می‌خواند؟ (۰/۱)
python tools\check_oid_coverage.py            :: ۱۳۶/۱۳۶ پوشش، صفر OID سرگردان، auths
python tools\check_oid_coverage.py --json     :: همان برای CI
python -m pytest tests\test_generator_yml.py tests\test_oid_matrix.py -q
python -m pytest tests\test_snmp_yml.py tests\test_build_snmp_yml.py -q
python tools\build_snmp_yml.py --check        :: snmp.yml با spec می‌خواند؟ (۰/۱)
```

`check_oid_coverage.py` عمداً «منفی‌سنجی» شده است: حذف یک ردیف، عوض‌کردن یک `subtree`،
زنده‌کردن یک یتیم، گذاشتن `ignore_values` روی متریک رشته‌ای، و نوشتن username واقعیِ v3
— هر پنج مورد با پیام مشخص رد می‌شوند.

`tests/test_snmp_yml.py` هم روی `monitoring/snmp.yml` همین کار را کرده (شش جهش، هرکدام
با شکستِ مشخص): افزودن متریک با OID بیرونِ ماتریس ⇒ ۷ تست رد؛ حذف `type` از یک متریک ⇒
۱ رد؛ تبدیل کامنت `ignore_values` به کلیدِ زنده ⇒ ۲ رد؛ حذف یک `subtree` از `walk` ⇒
۳ رد؛ تکراری‌کردن یک نام ⇒ ۲ رد؛ `type: unsigned32` (که runtime نمی‌شناسد) ⇒ ۱ رد.
پس سبزبودنشان تصادفی نیست.

---

## 3. Adding a new OID (mandatory order)

1. **اول کد:** `core/oid/catalog.py::OID_CATALOG` (یا collector مربوطه). تا کد نخوانده،
   جایی در spec ندارد.
2. `python tools\build_oid_matrix.py --write` و دیدن diff سند — ماتریس همیشه برابر کد است.
3. افزودن ردیف در `monitoring/generator.yml`:
   - ثابت ⇒ زیر `get:` با `oid`/`name`/`type`/`source:`؛
   - عضو یک جدول اندیس‌دار ⇒ به `metrics:` همان ورودیِ `walk:` اضافه شود (`subtree`
     ممکن است با LCPِ تازه عوض شود؛ گیت می‌گوید)؛
   - اگر متریک عددیِ سطح/درصد است، `ignore_values: ["-2", "-3"]`.
4. `python tools\check_oid_coverage.py` باید ۰ شود؛ اگر شکست، پیامش می‌گوید کدام قاعده.
5. بازتولید `monitoring/snmp.yml` (§۴.۶) و سپس `python -m pytest tests\test_snmp_yml.py -q`.
   ابزارِ خودکارِ بازتولید وجود ندارد؛ **گیتِ ضدِ drift، همان تست است** (۱:۱ با spec).

> یادتان باشد: عدد ۱۵۸ در پلان فاز G **مرجع نیست**. مرجع، شمارشِ کد است (۱۳۶ OID یکتا)؛
> توضیح کامل و پیمانه‌های جایگزین در §۱ `docs/oid-matrix.md`.

---

## 4. `monitoring/snmp.yml` — mapping the spec to the runtime config (step G.3)

### ۴.۱ چرا دستی نوشته شد

generator رسمی به MIB vendor (Canon/Brother/Toshiba) نیاز دارد که در دسترس نیست.
snmp.yml دستی از `generator.yml` تولید شد.

ثبتِ صریحِ آنچه **انجام نشد** (تصمیم مالک، عیناً در پلان G.3): در sandbox نه باینریِ
`generator` دانلود شد، نه `snmp_exporter`، و هیچ تستِ e2e/UDP (stub یا snmpsim) اجرا نشد.
پس جمله‌ای که برای `generator.yml` نوشته شده بود («با باینریِ رسمی در sandbox لینوکسی
اعتبارسنجی شد») برای این فایل **صادق نیست** و اینجا نوشته نمی‌شود؛ وضعیت در
`monitoring/snmp-manifest.json` → `load_test_sandbox: "not_attempted"` است و تستِ واقعی
به §۵ (سرور مالک، پایان پروژه) موکول شده.

### ۴.۲ schema؛ و چرا «کلیدِ اضافه» یعنی خطای load

از `config/config.go` در تگ `v0.26.0`:

| سطح | کلیدهای مجاز |
|---|---|
| root | `auths` · `modules` · `version` |
| `auths.<name>` | `community` · `security_level` · `username` · `password` · `auth_protocol` · `priv_protocol` · `priv_password` · `context_name` · `version` |
| `modules.<name>` | `walk` · `get` · `metrics` · `filters` + (inline) `max_repetitions` · `retries` · `timeout` · `use_unconnected_udp_socket` · `allow_nonincreasing_oids` |
| `metrics[]` | `name` · `oid` · `type` · `help` · `indexes` · `lookups` · `regex_extracts` · `enum_values` · `offset` · `scale` |

`LoadFile()` از `yaml.UnmarshalStrict` استفاده می‌کند ⇒ هر کلیدی که در جدول بالا نیست
**خطای load** است، نه هشدار. دلیلِ اینکه `ignore_values` و `source` در فایل کلید نیستند
همین است (§۴.۳) و تستِ `test_only_keys_the_official_loader_accepts` همین را می‌سنجد —
جانشینِ «اجرای باینری» در sandbox.

### ۴.۳ نگاشتِ کلیدها (spec → config)

| در `generator.yml` | در `snmp.yml` | چرا |
|---|---|---|
| `version: 1` | `version: 1` | هر دو schema این کلید را دارند |
| `auths.public_v1` / `public_v2` | عیناً | |
| `secure_v3.auth_passphrase` | `password` | نامِ کلیدِ runtime این است؛ نامِ متغیرِ محیطی (`SNMP_V3_AUTH_PASSPHRASE`) عوض **نشده** |
| `secure_v3.priv_passphrase` | `priv_password` | همان دلیل |
| `secure_v3.priv_protocol: AES128` | `priv_protocol: AES` | `ConfigureSNMP()` فقط `DES/AES/AES192/AES192C/AES256/AES256C` را match می‌کند؛ `AES128` در switch نیست و بی‌سروصدا بی‌اثر می‌ماند |
| `walk: [{name, subtree, metrics}]` | `walk: [<subtree>]` + همان ردیف‌ها در `metrics:` | runtime درخت را GETNEXT می‌کند و فقط OIDهای اعلام‌شده را نگه می‌دارد |
| `get: [{oid, …}]` | `get: [<oid>]` + `metrics:` | |
| `metrics[].type` (۷۶ ردیف) | عیناً | |
| ردیفِ بی‌`type` (۶۰) | `type` استنتاجی + کامنتِ شاهد | §۴.۴ |
| `source: <file>:<line>` | متنِ `help:` | runtime `source` ندارد؛ traceability داخل `help` زنده می‌ماند (تست، عضویتش را می‌سنجد) |
| `ignore_values: ["-2", "-3"]` | **کامنتِ** `# ignore_values: ["-2", "-3"]` | runtime هیچ کلیدی برای «دور ریختن مقدار» ندارد؛ فیلترِ sentinel در `core/collectors/exporter_adapter.py` (گام G.5) می‌نشیند — همان‌جا که امروز `si()` و گارد `0..100` در کدِ collector هست |
| `lookups` / `row_index` / `label_oid` | حذف | `lookups` بدون `indexes` معنا ندارد و سطرهای ما برگ‌های با اندیسِ صریح‌اند (§۴.۵)؛ آن دو `label_oid` خودشان متریک‌اند (`toner_name_1`, `tray1_name`, …) پس چیزی گم نمی‌شود |
| ۱۰ کامنتِ probe-only | در `snmp.yml` نمی‌آیند | سرِ جای خودشان در spec می‌مانند (§۶) |

### ۴.۴ `type`: ۷۶ اعلام‌شده در کد، ۶۰ استنتاجِ قابل‌ردیابی

`collector.go` در `pduToSamples()` عدد را فقط برای این نام‌ها می‌فهمد: `counter` · `gauge` ·
`Float` · `Double` · `DateAndTime` · `EnumAsInfo` · `EnumAsStateSet` · `Bits`. هر رشتهٔ دیگر
(از جمله `counter32` / `unsigned32` / `timeTicks`) به مسیرِ «رشته» می‌رود: **مقدار `1`
می‌شود** و متنِ پاسخ به یک لیبل هم‌نامِ متریک منتقل می‌شود. پس `type: counter32` یک تلهٔ
بی‌سروصدا است؛ چراغِ سبزِ load گول‌زننده است.

برای ۷۶ ردیفی که spec type داشت، همان مقدار عیناً نشست. ۶۰ ردیفِ دیگر در کد type
نداشتند (بیشترشان فقط در `core/snmp/oid_map.py::OIDS` تعریف شده‌اند). برایشان
استنتاجِ **شاهد‌دار** به‌کار رفت و روی هر متریک این کامنت نشست:
`# type inferred, not declared in code — <شاهد>`.

| خانواده | type | شمار | شاهد (از کد، در همین session) |
|---|---|---|---|
| شمارنده‌های انباشته: `print_*`/`copy`/`scan*`/`fax`/sizeهای توشیبا (`a3_*…b4_*`, `oids_a*_*`, `scan_net_*`, `print_list`), `alt_*` و `bro_*`/`canon_*`, `bw_std`, `val_val` | `counter` | ۲۵ | `base.py:325` (پارامترها `int` و با `_safe_delta` کم می‌شوند — `toshiba.py:419-425`)؛ `canon.py:216-219` و `canon.py:267` با `si()`؛ `canon.py:203` (bw pages)؛ `cartridge_id.py:305-310` `Toner Replace Count`، «هر بار تعویض تونر +۱» |
| سطح/درصد/ظرفیت/وضعیت/اندازهٔ سینی/uptime/usage | `gauge` | ۲۷ | `enhanced_collector.py:229-235,244-250` (`int(val)` + گارد `0..100`)؛ `brother.py:169-172`؛ `base.py:375,396` (`prev_uptime`)؛ و هم‌خانواده‌های type‌دارِ همان module (`tsh_t1_level`, `tsh_cyan_st`, `tsh_t1_size`) |
| متنِ هویت و **ریشهٔ** جدول (`standard`, `candidate_subtrees_*`) | `DisplayString` | ۸ | `…43.11.1.1.6` و `…9.4.2.1.1.6` ستون‌اند، نه برگ؛ اگر دستگاه همان OIDِ خالی را ندهد هیچ sample نمی‌دهد — این عمداً همین‌طور است |

(۲۵ + ۲۷ + ۸ = ۶۰.) توزیعِ نهایی در فایل: `gauge` ۵۳ · `counter` ۵۱ · `DisplayString` ۳۲ = ۱۳۶.
این ۶۰ type روی دستگاه واقعی **تأیید نشده‌اند**؛ §۵ بند ۷ جایی است که تأیید یا اصلاح
می‌شوند (و همان‌جا در manifest ثبت می‌شود).

### ۴.۵ چرا `indexes:` ندارد (و چرا این «کوتاه آمدن» نیست)

ماتریس، برگ‌های با اندیسِ صریح می‌دهد: `…43.11.1.1.9.1.1` و `…9.1.2` دو ردیف‌اند، نه
«یک ستون با دو سطر». نوشتنِ `indexes: [{labelname: …}]` یعنی ادعای «عددِ آخر، اندیسِ
سطرِ Nام است» — بازخوانیِ ساختارِ MIB که هیچ‌جا در کد نیامده و در `docs/oid-matrix.md`
 هم قاعده است که ساختارِ جدول اختراع نشود. پس هر برگ یک متریکِ صافِ مستقل است، و `walk:`
روی درختِ پدر می‌نشیند تا یک GETNEXT-sweep چند برگ را یک‌جا بیاورد. `buildMetricTree()`
و حلقهٔ «Look for metrics that match each pdu» در `collector.go:393-413` همین را می‌پذیرد:
هر PDU در درختِ متریک‌ها سرازیر می‌شود، نخستین گرهٔ متریک‌دار برنده است و ردیف‌های
اعلام‌نشدهٔ همان جدول دور ریخته می‌شوند — نه خطا، نه متریکِ سرگردان.

### ۴.۶ بازتولید، و گیتِ ضدِ drift

`monitoring/snmp.yml` خروجیِ **`tools/build_snmp_yml.py`** است (از G.4.1 در مخزن؛ پیش‌تر
اسکریپتِ یک‌بار‌مصرف بیرونِ repo بود). ابزار هیچ OIDی نمی‌سازد: فقط ردیف‌های
`generator.yml` را برمی‌دارد، همان سه نگاشتِ §۴.۳ را روی `auths` اعمال می‌کند و جدول
`INFER` را «عیناً روی ردیف‌های بی‌type» الزام می‌کند — نه کم (پیام «کم است»)
و نه زیاد (پیام «اضافه است»)؛ یعنی استنتاج هم دروازهٔ ماشینی دارد، نه سلیقه.

```bat
python tools\build_snmp_yml.py            :: بازتولیدِ monitoring/snmp.yml
python tools\build_snmp_yml.py --check     :: rc=1 اگر فایل با spec نمی‌خواند (+ diff)
python tools\build_snmp_yml.py --report    :: شمارش‌ها: rows/modules/metrics/get/walk/ignore/typeها
```

خروجی **deterministic** است (بدون timestamp، بدون ترتیبِ dict-وابسته): دو اجرای پیاپی
بایت‌به‌بایت یکی است و بازتولید روی فایلِ commit‌شده هیچ diffی نمی‌سازد — همین، شرطِ
idempotent بودنِ `--check` به‌عنوان گیتِ CI است
(`tests/test_build_snmp_yml.py::test_regeneration_is_deterministic_and_idempotent`).

و لایه‌های دیگرِ ضد-drift (همه در `tests/test_snmp_yml.py` / `tests/test_build_snmp_yml.py`):

- `test_metrics_are_one_to_one_with_generator_yml_rows` — نام↔OID در هر module، و `source` داخل `help`؛
- `test_all_136_matrix_oids_are_declared_and_none_is_invented` — برابریِ مجموعهٔ OID با ماتریسِ *کد*؛
- `test_ignore_values_are_comment_markers_on_the_same_thirty_one_metrics` و
  `test_sixty_types_are_marked_as_inferred_and_match_the_untyped_spec_rows`؛
- `test_seven_walk_tables_and_no_overlap_with_get` و `test_every_metric_is_reachable_by_exactly_one_of_walk_or_get`؛
- `test_tool_writes_only_spec_oids` و `test_check_mode_fails_when_spec_gained_a_row` —
  اگر ردیفی به spec اضافه شود، فایلِ commit‌شده «کهنه» شمرده می‌شود (این تست با یک
  specِ موقتِ ۱۳۷ ردیفی در tmp اثباتش می‌کند، بدونِ دست‌زدن به فایل‌های مخزن).

پیام‌ها نامِ متریک را می‌گویند؛ یعنی «یادآوریِ همگام‌سازی» خودکار است، نه دستی —
همان الگوی `check_oid_coverage.py` که در §۲ تعریف شده، این‌جا هم روی خودِ config نشسته است.

### ۴.۷ شمارش‌های `monitoring/snmp.yml` (خروجیِ همین session)

| سنجه | عدد |
|---|---|
| اندازهٔ فایل | ۸۵۴ خط (۶۸۸ خطِ بدون‌کامنت) · ۴۶۰۲۸ بایت |
| modules | ۶ — std 24 · hp 14 · canon 16 · brother 9 · toshiba 63 · sensor 10 متریک |
| متریک / نام یکتا / OID یکتا | ۱۳۶ / ۱۳۶ / ۱۳۶ |
| ردیف‌های `get` | ۹۸ — std 11 · hp 10 · canon 16 · brother 4 · toshiba 47 · sensor 10 |
| درخت‌های `walk` | ۷ — std 2 · hp 2 · toshiba 2 · brother 1 |
| `# ignore_values` کامنت / کلیدِ زنده | ۳۱ / **۰** |
| type اعلام‌شده / استنتاجی | ۷۶ / ۶۰ |
| تست | `tests/test_snmp_yml.py` → ۱۵ passed (و ۶ جهشِ §۲ رد می‌شوند) · `tests/test_build_snmp_yml.py` → ۱۳ passed |
| `build_snmp_yml.py --check` روی فایلِ commit‌شده | rc ۰ (دو اجرای پیاپی: بایت‌یکسان) |
| manifest | `monitoring/snmp-manifest.json` — ۸ کلیدِ پلان + extras، همه از همین خروجی |

---

## 5. Binary test on the owner's server (end of project)

هیچ‌یک از بند‌های زیر در sandbox انجام **نشده** است — عملاً و طبقِ تصمیمِ مالک. ترتیبِ
اجرا روی سرور مالک، پس از بسته‌شدنِ فاز G:

1. دانلود `snmp_exporter` نسخهٔ pin‌شدهٔ **0.26.0** برای Windows (`snmp_exporter.exe`) و
   بررسیِ checksum با انتشارِ رسمی. باینری هرگز commit نمی‌شود.
   `windows/install-snmp-exporter.ps1` همین دو منبعِ pin‌شده را می‌گیرد و
   `-ExpectedSha256` اختیاری دارد؛ راهنمای اجرا، حالت‌های شبکه و چک‌لیست:
   `docs/windows-services.md`.
2. کنار هم گذاشتن `snmp_exporter.exe` و **همین** `monitoring/snmp.yml` (بدون تغییر).
3. `\path\to\snmp_exporter.exe --config.file=snmp.yml --web.listen-address=127.0.0.1:9116 --config.expand-env`
   — `--config.expand-env` برای `${SNMP_V3_*}`ها لازم است (§۸). اگر load خطا داد، پیامِ
   strict را عیناً در گزارش فاز بنویسید: یعنی کلیدی در فایل هست که `config/config.go`
   نمی‌شناسد؛ مرجعِ نهایی باینری است، نه تست‌های ما.
4. اجرای واقعی روی یک پرینترِ واقعی و پرسیدنِ exporter:
   `curl http://localhost:9116/snmp?target=<ip>&module=printer_hp&auth=public_v2`
5. چیزی که باید دیده شود: متریک‌های همان module (برای `printer_hp`: `hp_model`,
   `hp_serial`, `hp_firmware`, `general_oids`, `hp_print_color`, `hp_npcl_pct_oid`, …)،
  +self-metricsِ `snmp_scrape_duration_seconds`, `snmp_scrape_walk_duration_seconds`,
   `snmp_scrape_packets_sent`, `snmp_scrape_packets_retried`, `snmp_scrape_pdus_returned`
   (همین نام‌ها از `collector.go` نسخهٔ 0.26.0 برداشته شده‌اند)، و **نبودِ** `snmp_error`.
   برای متریک‌های متنی انتظارِ `1` با لیبلِ هم‌نامِ متریک است، نه مقدارِ رشته‌ای (§۴.۴).
6. تطبیق با collector خودِ PrintGuard روی همان دستگاه — `tools/compare_snmp_sources.py`
   (گام G.6). `snmp_scrape_packets_sent` تنها شاهدِ قابل‌استنادِ «کاهش بسته» است؛ تا آن
   زمان هیچ عددی دربارهٔ کاهش بسته در این سند ادعا نشده است.
7. تأییدِ ۶۰ typeِ استنتاجی (§۴.۴): اگر دستگاه متریکی را عددی داد که ما `DisplayString`
   گذاشته بودیم (یا برعکس)، `type` در spec و در `snmp.yml` با یک کامیتِ جدا اصلاح و در
   `snmp-manifest.json` ثبت می‌شود.
8. اگر همه‌چیز خواند، `monitoring/snmp-manifest.json` → `load_test_sandbox` از
   `not_attempted` به `passed on owner server` عوض می‌شود (با نسخهٔ باینری و تاریخِ اجرا).

**استقلال از OS:** `monitoring/snmp.yml` متنِ UTF-8ِ خالص است — LF، بدون BOM، بدون
مسیرِ مطلق، بدون هیچ ارجاعِ OS-وابسته؛ همان بایت‌هایی که در sandbox لینوکسی ساخته شده
روی Windowsِ سرور مالک load می‌شود و فرقِ دو OS در این فایل فقط «اجراکننده» است.
ثبیتِ این جمله در تست: `test_file_is_os_independent_plain_text` (و برای چشمِ انسان:
`sha256sum monitoring/snmp.yml` روی هر دو OS باید یکی باشد).

---

## 6. Where the probe-only OIDs are and why

ده OID در کد هست که **مسیر polling** نمی‌خواندشان؛ ماتریس آن‌ها را در §۵ «یتیم‌ها»
می‌آورد و در `generator.yml` فقط **کامنت**‌اند (تفکیک: ۸ برادر، ۱ توشیبا، ۱ استاندارد):

| OID | محل | چرا زنده نیست |
|---|---|---|
| ۷ کاندیدای `brInfo/brToner` | `tools/diagnose_printer.py:110-116` | فهرستِ ۹ تاییِ کاوش است و ۷ تازش یتیم‌اند؛ کامنتِ خودِ کد: «هرکدام پاسخ بدهد نگاشت دقیقش را در کالکتور می‌گذاریم» — هنوز نگاشته نشده |
| `…2435.2.3.9.1.1.3.1.1.1.1.2.0` | `web/routes/discover.py:45` | کاشِ شناساییِ دستگاه در مسیر اکتشاف، نه چرخهٔ polling |
| `…1129.2.3.50.1.3.21.6.1` | `tools/diagnose_printer.py:285` | پایهٔ walk برای کاوش؛ مصرف‌کنندهٔ ثابت ندارد |
| `1.3.6.1.2.1.43.11.1.1` | `tools/diagnose_printer.py:27` | **پوشش داده شده**: همین `subtree` در `printer_std` walk می‌شود، فقط ردیفِ مستقل نیست |

چرا به spec راه نیافتند؟ چون هر ردیفِ generator باید مصرف‌کنندهٔ polling داشته باشد
(قاعدهٔ ۲ §۱). اگر روزی به `OID_CATALOG` برسند، ماتریس خودکار آن‌ها را می‌گیرد و
`check_oid_coverage.py` تا زمان افزودنشان به `generator.yml` قرمز می‌ماند — یعنی
یادآوری‌اش خودکار است، نه دستی.

برای برادر این یعنی: **۹ ردیف زنده** در spec و **۸ کاندیدای کامنت** — همان چیزی که
اختلاف شمارش‌ها را توضیح می‌دهد: ۹ در ماتریسِ کد در برابر ۱۷ با کاوش‌ها — و عددِ مرجع `generator.yml` همان **۹** است.

---

## 7. What was measured and what was not run

**اندازه‌گیری‌شده در گام G.2:** شمارش‌های §۱ (از گیت و از `build_oid_matrix.py`)،
اعتبار YAML با `yaml.safe_load`، یکتابی نام‌ها، انطباق `source:` با ماتریس،
سازندگیِ ردیف‌های f-string (`head` در JSON ماتریس)، و پنج سنجهٔ منفی §۲.

**اندازه‌گیری‌شده در گام G.3:** جدولِ §۴.۷ (همه از پارسِ خودِ `monitoring/snmp.yml` و
`yaml.safe_load`)، برابریِ مجموعهٔ OID فایل با ماتریسِ کد (۱۳۶/۱۳۶، بدونِ کم و زیاد)،
۱:۱بودنِ نام↔OID↔source با `generator.yml`، ۳۱ کامنتِ `ignore_values` و صفر کلیدِ زنده،
۷۶+۶۰بودنِ typeها، و شش جهشِ §۲ روی همین فایل.

**انجام‌نشده (و در این سند ادعا نشده):** اتصال به دستگاه واقعی؛ دانلود یا اجرای باینریِ
`generator` / `snmp_exporter`؛ هرگونه تستِ e2e یا agent قلابی (UDP stub / snmpsim)؛
نصب روی ویندوز (NSSM/فایروال)؛ تأییدِ ۶۰ typeِ استنتاجی؛ و هر عددی دربارهٔ «کاهش
بسته‌ها». آن‌ها §۵ (سرور مالک، پایان پروژه) و `tools/compare_snmp_sources.py` (گام G.6)
هستند. تست واقعی ویندوز در فاز L می‌ماند.

## 8. v3 environment variables

`SNMP_V3_USERNAME` · `SNMP_V3_AUTH_PASSPHRASE` · `SNMP_V3_PRIV_PASSPHRASE`

فقط placeholder‌های `${…}` در git می‌نشینند (گیت، هر مقدار غیر از الگوی `${VAR}` را در
`secure_v3` رد می‌کند). تنظیم‌شان روی سرویس ویندوز، گام نصب است؛ تا وقتی تنظیم نشده،
ماژول `secure_v3` عملاً استفاده‌ناپذیر است و این خواستهٔ فاز است: **v3-first
در گام بعد انتخاب می‌شود، نه اینجا.**

**نگاشت به `snmp.yml` (گام G.3):** نامِ متغیرها عوض نشده‌اند؛ فقط نامِ کلیدهایی که
exporter می‌خواند runtimeی شده‌اند — `password:` جای `auth_passphrase:`،
`priv_password:` جای `priv_passphrase:`، و مقدارِ `AES` جای `AES128` (§۴.۳). برای همین
اجرا باید با `--config.expand-env` باشد، وگرنه `${…}`ها رشته‌های خام می‌مانند. جدولِ
کلیدهای `secure_v3` در دو فایل عمداً فرق دارد و تست‌ها هر دو شکل را می‌سنجند
(`tests/test_generator_yml.py` برای spec، `tests/test_snmp_yml.py::test_auths_use_the_runtime_field_names`
برای config).

**تصمیم (الف)، گام G.4.1:** `secure_v3` در spec **rename نمی‌شود** — همان نگاشتِ بالا در
کامنتِ بالای `auths:` در `monitoring/generator.yml` نوشته شده و کدِ نگاشت
(`AUTH_KEY_MAP`/`AUTH_VALUE_MAP`) در `tools/build_snmp_yml.py` است. دلیل: اگر روزی
`generator` رسمی با MIBهای vendor در دسترس بیاید، spec باید ورودیِ همان باینری بماند؛
و دلیلِ دوم: نگاشتِ نام‌ها یک‌جا و ماشینی است (تستِ
`test_auth_mapping_translates_exactly_the_documented_three_names` آن را با خودِ جدول
می‌سنجد، نه با بازنویسیِ دو فایل)، پس «دو نسخهٔ هم‌آهنگِ دستی» تولید نمی‌شود.

---

## 9. Step G.5 — exporter reader, adapter and hybrid flag

### ۹.۱ تصمیمِ تست (سؤال → پاسخ مالک)

باینری در sandbox نیست و دانلود/اجرای SNMP واقعی در این فاز ممنوع بود، پس مسیرِ داده
نمی‌تواند «واقعی» باشد. تصمیم: **گزینهٔ ۱ + ۳ با هم** —

- **fixture متنیِ دست‌نویس از روی spec** (چهار فایل در `tests/fixtures/`) برای لایهٔ
  پارس/normalize/sentinel؛
- **mocked HTTP transport** (تزریقِ `open_fn`) برای لایهٔ کوئری/timeout/خطاها.

**جایگزین‌های ردشده و دلیلشان:**
- *capture واقعی از سرور مالک*: دادهٔ درست‌تری می‌داد ولی G.5 را به زمانِ دسترسیِ مالک
  گره می‌زد؛ قرار شد capture در G.6 (مقایسه) بیاید، نه اینکه تستِ unit را بلوکه کند.
- *fake transport بدونِ fixture*: رفتارِ پارسر را بی‌آزمون می‌گذاشت — همان‌جا که بیشترین
  باگِ ممکن است (escaping، خط‌های کامنت، مقدارهای غیرعددی).
- *صفر تغییرِ production (فقط سند)*: با خواستهٔ مالک (کلید + poller با پیش‌فرض ۰)
  نمی‌خواند.

**صریح:** هیچ‌کدام از این تست‌ها «load با باینری» یا «parity با SNMPِ مستقیم» را اثبات
نمی‌کنند. آن دو در §۵ (سرور مالک) و گام G.6 (`tools/compare_snmp_sources.py`) سنجیده
می‌شوند؛ تا آن‌جا وضعیتِ هر دو در manifest `not_attempted` است.

### ۹.۲ فایل‌ها

| فایل | خط / بایت | نقش |
|---|---|---|
| `core/exporter_client.py` | ۴۳۷ / ۲۴٬۰۵۶ | پارسِ متنِ exposition، `ExporterClient.query/query_brand/is_available`، `exporter_enabled()`؛ از گام H.2 backoff/circuit-breaker هم همین‌جاست (`DeviceState`, `delay_for_failures`, `device_states()`, `reset_backoff()`) |
| `core/collectors/exporter_adapter.py` | ۵۶۶ / ۳۱٬۳۵۱ | exporter → شکلِ `collect_hp`/`collect_canon`؛ `SUPPORTED_BRANDS = ("hp", "canon")`؛ از گام H.1 وب‌اسکرپِ تونرِ همان دو برند را هم آینه می‌کند (`apply_toner_http_fallback`) |
| `core/poller.py` | +۲۸ خط (بدونِ حذفِ هیچ سطری) | شاخهٔ `if exporter_enabled() and _exporter_client().is_available():` |
| `tests/fixtures/exporter_response_{sample,std,canon,canon_std}.txt` | ۶۲/۷۱/۴۰/۵۳ خط | پاسخِ ماژول‌محورِ دست‌نویس (نه capture) |
| `tests/test_exporter_client.py` · `tests/test_exporter_adapter.py` · `tests/test_exporter_backoff.py` · `tests/test_poller_exporter_flag.py` · `tests/test_hybrid_full_path.py` | ۲۱۹/۷۱۲/۳۳۳/۲۴۹/۵۳۴ خط | ۱۶ + ۳۸ + ۱۶ + ۹ + ۱۲ = ۹۱ تست، بدونِ skip (۱۲ تای آخر از H.3؛ گزارشِ جامع: `docs/hybrid-path-report.md`) |

تغییرِ بقیهٔ درخت: **صفر**. تنها فایلِ موجودی که ویرایش شد `core/poller.py` است و
`collectors/`، `snmp/protocol.py`، `yield_engine.py`، `cartridge_*.py` و `config/`
دست‌نخورده‌اند (تأیید: `git diff --stat` فقط همین یک فایل را در `core/` نشان می‌دهد).

### ۹.۳ قرارداد

کوئری، دقیقاً همان چیزی که `/snmp` مستنداتِ exporter می‌خواهد:

```
GET http://localhost:9116/snmp?target=172.16.0.5&module=printer_hp&auth=public_v2
```

- `parse_prometheus_text()` → `{name: [(labels, value), …]}`؛ خط‌های `#` و خالی رد
  می‌شوند، `NaN`/`±Inf` «دادهٔ نامعتبر» است (`metric_value` مقدارِ default می‌دهد)،
  سریِ تکراری حفظ می‌شود و برای متریک‌های scalarِ spec **آخرین** سری خوانده می‌شود.
  خطای پارس `ExporterError` می‌دهد، نه skipِ بی‌صدا.
- متریک‌های متنی: مقدارِ `1` + لیبلِ **هم‌نامِ متریک** (رفتارِ `collector/collector.go`
  نسخهٔ v0.26.0؛ اگر لیبلِ متنیِ دیگری هم باشد، لیبلِ هم‌نام برنده است — تست دارد).
- `snmp_scrape_*` لیبلِ ثابتِ `module="<module>"` دارد؛ exporter خودش هیچ `instance`
  اضافه نمی‌کند (جست‌وجوی `instance` در `collector.go` و `main.go` = ۰ نتیجه). پارسر
  لیبل‌های اضافه (مثل `instance` که Prometheus می‌گذارد) را نادیده می‌گیرد.
- `is_available()`: `GET /` با timeout ۲ ثانیه، هر خطایی → `False` و هیچ استثنایی بالا
  نمی‌آید. `query()` هر خطای شبکه/HTTP/پارس را به `ExporterError` تبدیل می‌کند.
- `collect_via_exporter()` هیچ استثنایی بیرون نمی‌دهد: خطا یا پاسخِ ناکافی → `None` ⇒
  poller همان چرخهٔ امروز را می‌رود (یک `log.warning`).
- برند‌هایی که آینه‌سازی نشده‌اند (`toshiba`، `brother`، `sensor`) **بدونِ هیچ کوئری‌ای**
  None می‌شوند؛ این رفتار با اسپایی که اگر `query_brand` صدا زده شود throw می‌کند، تست
  شده است.

### ۹.۴ آنچه adapter *عمداً* بازتولید نمی‌کند

سنجیده با AST روی `core/enhanced_collector.py::collect_enhanced` (مسیرِ موفق، ۱۸ کلید)
در برابرِ خروجیِ adapter (۱۲ کلید، همان ۱۲ کلیدِ `collect_hp`):

```
cartridge_id_source, cartridge_identity_type, cartridge_ids, cartridge_signal_quality,
device_type, nickname            # ۶ کلید تولید نمی‌شود؛ ۰ کلید اضافه
```

`device_type` و `nickname` را poller بعد از بازگشت می‌چسباند (همان کاری که برای مسیرِ
legacy می‌کند)، پس عمقِ تفاوت به **۴ کلیدِ `cartridge_*`** است که از
`_counters_event`/YieldEngine/`cartridge_change_engine` می‌آیند؛ آن‌ها در
collectorها زندگی می‌کنند و طبقِ دستورِ مالک `collectors/` دست‌نخورده است.
**نتیجه:** پرچمِ روشن snapshot می‌سازد، نه چرخهٔ کاملِ poll؛ به همین دلیل
`USE_SNMP_EXPORTER` پیش‌فرض ۰ است و تا سنجشِ G.6 روی سرورِ شما (§۱۰) روشن‌کردنش توصیه
نمی‌شود.

**به‌روز‌شده در گام H.1:** دو مورد از آن سه چیز حالا آینه *شده‌اند* — `_scrape_hp_toners` و
`_scrape_canon_toners` با import از collectorهایِ خودشان در `apply_toner_http_fallback` صدا
زده می‌شوند — با **صفر کپیِ اسکرپر** (import از همان collectorها؛ تصمیمِ مالک، TD-019) و
merge از تک‌درِ `_merge_scraped` که فقط deleg می‌کند. با یک triggerِ **یکنواخت** برایِ هر دو برند: فقط وقتی SNMP عددی برای هیچ
تونرِ واقعی ندارد (`_toner_level_missing`). تفاوتِ دو برند در *شکلِ بازنویسی* است، نه در
زمانِ اسکریپ: HP فیلدهایِ خالی را با نردبانِ `0/10/25` پر می‌کند (`source="hp_web"`) و canon
رکوردِ کاملِ HTTP را جاگذاری می‌کند (`remaining=None`/`max=100`/`source="http_scrape"`).
«فقط وقتی ناقص است» روی canon با collector خودش فرق می‌کند (`canon.py:325` همیشه اسکریپ
می‌زند) و همان واگرایی تستِ صریح دارد: `test_canon_divergence_from_the_collector_is_pinned`.
کوئریِ وب فقط در `collect_via_exporter` رخ می‌دهد و `exporter_to_collector_shape` خالص/آفلاین
مانده، پس `tools/compare_snmp_sources.py` (§۱۰.۳) مثلِ قبل شبکه نمی‌زند. جزئیات و جایگزین‌هایِ
ردشده: `docs/phase-h-decisions.md` §H.1. تنها چیزی که از سه مورد ماند، شرطِ «total=0 مشکوک ⇒
دوباره SNMP بزن» است (دومین خواندن؛ state می‌خواهد و به گام H.2 موکول شد) و `_used`/
`hp_web_used` (آشکارسازیِ کارتریج شارژی از `SupplyState=Used`) که کلیدهایش در قراردادِ
۱۲تاییِ بالا نیست — TD-017. یک واگرایی هم ماند که **تستِ صریح** دارد
تا بی‌صدا نشود: `prtInputCurrent = -2` در مسیرِ HP یعنی `no_sensor`؛ اینجا sentinelِ
متریکِ `tray*_level` (که در `snmp.yml` کامنتِ `ignore_values` دارد) دور ریخته می‌شود و
defaultِ `-9` نتیجه را `empty` می‌کند. رفعش یک‌خطی است — حذفِ `tray1_level`/`tray2_level`
از `IGNORED_METRICS` — ولی انجام نشده تا «تغییرِ بی‌صدا» ممکن نباشد
(`test_documented_divergence_hp_no_sensor_becomes_empty`).

**یافته‌ای که همین آینه‌سازی بیرون داد (TD-016):** `_hp_toner_key()` در
`core/collectors/hp.py` با زیررشته تطبیق می‌داد، پس «m» در «iMaging» و «c» در «CF470A»
رنگِ کاذب می‌ساخت؛ اعدادِ سنجیده‌شده قبل از رفع: `Imaging Drum → magenta`،
`CF470A Imaging Drum → cyan`، `Fuser Kit / Maintenance Kit / Document Feeder Kit → black`.
اثرش روی پرینتر تک‌رنگ: `has_color_toner=True` کاذب ⇒ `full_color=None` و
`black_white=None` (به‌جای `black_white=total`). در همان فایل، مسیرِ وب (`_key()` داخل
`_scrape_hp_toners`) مرزِ کلمه داشت و کامنتش همین مثالِ «Document Feeder Kit» را می‌زد —
فقط مسیرِ SNMP اصلاح نشده بود. رفع در کامیتِ `fix(hp)` انجام شد (بیرونِ G،
به‌تشخیصِ مالک) با `tests/test_hp_toner_key.py` (۲۰ مورد: ۱۷ حالتِ parametrized +
۳ تست)؛ چون adapter همان تابع را import می‌کند، رفع به‌صورت خودکار در مسیرِ
exporter هم نشست و تستِ آینه
(`test_hp_drum_slot_uses_the_generic_key`) حالا `toner_2` را انتظار دارد. شرحِ کامل و
جدولِ سنجه در `docs/tech-debt.md` ردیف TD-016؛ شیفتِ +۱۱ خطیِ شماره‌های `hp.py` در
`docs/oid-matrix.md` هم با بازتولیدِ ابزار اصلاح شد (۱۵ سطر، فقط ستونِ «منبع»).

`ignore_values`: دقیقاً **۳۱** متریکِ دارایِ کامنت در `monitoring/snmp.yml`
(۸ + ۳ + ۲ + ۲ + ۱۶)؛ `IGNORED_METRICS` با همان فایل مقایسه می‌شود، پس افزودن
`ignore_values` به spec بدونِ به‌روزرسانیِ adapter تست را می‌شکند.

### ۹.۵ گیت‌ها و ضدپوچی

**پذیرشِ G.5 (به‌جای دروازهٔ «git diff خالی»ِ فازهای قبل):**

- پیش‌فرضِ خاموش: `697 passed / 0 skipped / 109 subtests` در `TZ=Asia/Tehran` (و همین در
  `TZ=UTC`) — یعنی ۶۵۰ تستِ قبلی + ۴۷ تستِ جدید، بدونِ یک skip اضافه. بعد از `fix(hp)`
  (TD-016) همان دو سوئیت `717 passed / 0 skipped / 109 subtests` شدند (۱۸ تستِ تازهٔ
  `tests/test_hp_toner_key.py` + ۲ تغییرِ انتظارات در تستِ آینه).
- با `USE_SNMP_EXPORTER=0` صریح، golden تفاضل‌صفر است (در سوئیت، `tests/test_golden.py`).
- **آزمونِ واژگونیِ خودِ پرچم:** با پیش‌فرضِ `1` در کد، سوئیت `3 failed / 694 passed`
  می‌شود — همان سه تستی که «پیش‌فرض باید ۰ باشد» را می‌بندند — و بقیهٔ سوئیت سبز
  می‌ماند، چون مسیرِ exporter بی‌پاسخ می‌ماند و fail-safe برمی‌گردد. پس (الف) پیش‌فرض
  محافظت می‌شود و (ب) روشن‌بودنِ پرچم، سوئیت را نمی‌شکند.
- دروازهٔ ساختاریِ poller: `test_branch_is_one_short_circuit_condition_inside_collect_inner`
  با AST می‌گوید *یکی* و فقط *یکی* `if` با شرطِ `exporter_enabled() and …is_available()`
  داخلِ `_collect_inner` هست، `and` ترتیبِ short-circuit دارد، و بدنهٔ شاخه `try/pass`
  ندارد.
- دروازهٔ بی‌اثری: AST of adapter — هیچ `store`/`add_event`/`_counters_event`/`urlopen`
  در کدش نیست و importهایش زیرمجموعهٔ `{logging, re, time, core.collectors.base,
  core.collectors.hp, core.collectors.canon, core.exporter_client, core.time_utils}` است
  (از H.1 `core.collectors.canon` برای `_scrape_canon_toners` اضافه شد؛ همان گیتِ AST
  `tests/test_exporter_adapter.py::test_adapter_has_no_side_effects` هر importِ تازه را می‌گیرد).

**جهش‌ها (۱۷ مورد روی سه فایل + ۳ موردِ مجددِ بعد از `fix(hp)`، سپس بازگشتِ
بایت‌به‌بایت با `cmp`):** ترتیبِ شرطِ poller
(۳ قرمز)، حذفِ `is_available()` (۳)، خاموش‌کردنِ کل شاخه (۳)، حذفِ `ignore_values` (۳)،
آستانهٔ ۲۵٪→۶۰٪ تونر (۱)، بی‌کارانداختنِ `_hp_toner_key` در آینه (۲)، واحدِ uptime
(۲)، بی‌اعتبارکردنِ رنگِ MF (۱)، کلیدِ `paper_sizes` (۲)، None‌نکردنِ برندِ
پشتیبانی‌نشده (۱)، اولین‌سری به‌جای آخرین (۱)، `is_available` همیشه True (۱)، نامِ
پارامترِ URL (۴)، پیش‌فرضِ پرچم = ۱ (۳)، فیلترِ non-finite (۱)، ترجیحِ لیبلِ هم‌نام (۱).
یک جهشِ اولیهِ «پارسر خطا را می‌بلعد» رفتار را عوض نمی‌کرد (همان `ExporterError` از
جای دیگر بلند می‌شد)؛ نسخهٔ درستش (`try/except: continue` در حلقه) اجرا شد و ۱ تست را
قرمز کرد — پس آن مورد در شمارشِ بالا با نسخهٔ درست ثبت شده است.

### ۹.۶ انجام‌نشده‌ها

اجرای واقعیِ exporter، تماسِ شبکه‌ای با ۹۱۱۶، scrape روی پرینتر واقعی، مقایسهٔ
«exporter در برابرِ SNMPِ مستقیم» روی دستگاه، و سنجشِ `snmp_scrape_packets_sent` — هیچ‌کدام
در این sandbox انجام **نشد** (ابزارش ساخته شده و منتظرِ سرورِ شماست: §۱۰). fixtureها ساختگی‌اند (synthetic، از روی spec — خودِ فایل‌ها این را
فریاد می‌زنند) و در سرتاسرِ فایل‌ها همین‌طور برچسب خورده‌اند؛ `load_test_sandbox` در
`monitoring/snmp-manifest.json` هم همچنان `not_attempted` است و یادداشتِ G.5 به `notes`
اضافه شده تا وضعیتِ «دادهٔ ساختگی، سنجشِ واقعی موکول» از فایلِ provenance هم پیدا باشد.

---

### ۹.۷ گام H.2 — backoff و مدارِ باز (چه انتظاری روی سرور داشته باشید)

شمارشِ «چند بار پیاپی نشد؟» در خودِ کلاینت است، به‌ازای `(ip, module)` — `core/poller.py`
چیزی نمی‌داند و چیزی ذخیره نمی‌کند. نردبان (ثانیه، از لحظهٔ *آخرین شکست*):

| شکستِ پیاپی | تأخیر پیش ازِ تلاشِ بعدی |
|---|---|
| ۱–۲ | ۰ — بی‌درنگ (خوابِ دستگاه و شلوغیِ لحظه‌ای زیاد می‌افتد) |
| ۳–۵ | ۳۰۰ (۵ دقیقه) |
| ۶–۱۰ | ۱۸۰۰ (۳۰ دقیقه) |
| >۱۰ | ۲۱۶۰۰ (۶ ساعت) = مدار باز |

سه چیز که موقعِ اجرا باید بدانید:

- **در زمانِ backoff هیچ درخواستی نمی‌رود** و شمارنده هم جلو نمی‌رود؛ پس یک پرینترِ
  خاموشِ ۳ روزه، سه روزِ هشدار و ۴۳۲۰ «شکست» تولید نمی‌کند. مسیرِ legacy همان چرخه
  می‌نویسد (داده از دست نمی‌رود؛ فقط از منبعِ SNMPِ مستقیم).
- **نیمه‌خواندن نداریم:** اگر *یکی* از ماژول‌های آن برند در backoff باشد،
  `query_brand` کلِ برند را با `ExporterCircuitOpen` رد می‌کند — شکلِ collector با
  `printer_hp` و بدونِ `printer_std` کامل نمی‌شود و snapshotِ نصفه از نبودِ snapshot بدتر است.
- **لاگ:** خطایِ معمولیِ exporter مثلِ قبل `warning` است؛ *backoff* در `debug` (تا سیلِ
  هشدار نشود) و دقیقاً **یک** `warning` در لحظهٔ باز شدنِ مدار:
  `exporter: مدارِ <ip>/<module> باز شد — تا 6.0 ساعت هیچ کوئری‌ای نمی‌رود …`.

عیب‌یابیِ سریع روی سرور:

```python
from core.exporter_client import client_from_env
c = client_from_env()
c.device_states()          # فقط دستگاه‌هایِ در backoff: {"10.0.20.9|printer_std": {…}}
c.reset_backoff("10.0.20.9")   # «دستگاه را تازه دیدم/روشن کردم» → تلاشِ فوری
c.reset_backoff()              # همه
```

`next_retry_at` در **حافظهٔ زنده** است و با restart سرویس می‌پرد — عمدی: بیدار‌شدنِ سرویس
نباید یک پرینترِ مرده را ۶ ساعت بلوکه کند. بدهیِ «in-memory بودنِ این state» (و اینکه
چرا در DB ذخیره نشده) در گام H.4 به `docs/tech-debt.md` اضافه می‌شود. عددِ نردبان در
`tests/test_exporter_backoff.py::test_ladder_matches_the_plan` قفل شده؛ تغییرش یعنی
تغییرِ سندِ شما.

## 10. Step G.6 — `tools/compare_snmp_sources.py` (comparing the two sources)

### ۱۰.۱ چرا «ابزار» و نه «عدد»

تصمیمِ مالک: **ابزار ساخته شود، سنجش روی سرورِ مالک**. در این sandbox نه
`snmp_exporter` اجرا می‌شود نه پرینتر، پس ثبتِ جمله‌ای مثلِ «legacy ۱۳۲ بسته می‌زد، exporter
۶۳ بسته» یعنی نوشتنِ عددی که هیچ‌کدام اندازه نگرفته‌ایم. در نتیجه در repo این‌ها هست:
خودِ ابزار، سه fixtureِ مصنوعی، و ۲۴ تست؛ و این‌ها **نیست**: هر عددی دربارهٔ تعدادِ PDU،
کاهشِ ترافیک، یا «میانگینِ اختلافِ دو منبع». **هیچ عددی در repo ثبت نمی‌شود — شما روی
سرورِ خودتان می‌سنجید** و گزارشی که ابزار می‌نویسد (`--md`) تنها جای معتبرِ آن اعداد است.

### ۱۰.۲ قراردادِ ابزار (چه چیزی «اختلاف» است)

- **ورودیِ exporter**: همان متنِ exposition که `/snmp?target=…` برمی‌گرداند، در یک یا چند
  فایل (`--exporter-capture` قابلِ تکرار است تا `printer_hp` و `printer_std` با هم
  بخوانده شوند). پارسِ آن با `core/exporter_client.parse_prometheus_text` است — پارسرِ
  تست‌شدهٔ G.5؛ ابزار پارسرِ دومِ موازی نمی‌سازد (تستِ منبع‌محور دارد).
- **ورودیِ legacy**: JSONِ همان ساختاری که `collect_hp`/`collect_canon` برمی‌گردانند
  (یا `collect_enhanced`)؛ `brand` از کلیدِ `brand` یا از `--brand`.
- **واحدِ مقایسه «مسیر» است، نه نامِ متریک**: اول capture با *همان آینهٔ G.5*
  (`exporter_to_collector_shape`) به ساختارِ collector درمی‌آید، بعد مسیرهایِ نقطه‌چین
  (`counters.total`, `toners.black.level`, `trays.0.pct`, `alerts.0.code`) سطر‌به‌سطر
  مقایسه می‌شوند. چرا: چون قرار است دقیقاً همان چیزی سنجیده شود که
  `USE_SNMP_EXPORTER=1` عوض می‌کند — نه یک ترجمهٔ دستیِ تازه که خودش باگ داشته باشد.
- **عدد**: `rel = |a − b| / max(|a|, |b|) × 100`، آستانه با `--tolerance` (پیش‌فرض **۵**).
  مرزِ *دقیق* داخلِ `ok` است (`<=`) — یک تستِ مرزی با ۱۹۰۰/۲۰۰۰ که `rel` را دقیقاً ۵.۰۰
  می‌کند همین را می‌سنجد. **رشته و bool تلورانس ندارند**: نابرابریِ `device.model` یک
  اختلافِ کامل است.
- **`missing` ≠ اختلاف**: مسیری که یک طرفش `None`/غایب است، در گزارش می‌آید
  (`only_in_exporter` / `only_in_snapshot`) ولی exit را تکان نمی‌دهد — وگرنه کاستیِ ساختاریِ
  شناخته‌شدهٔ آینه (۴ کلیدِ `cartridge_*` + `nickname`/`device_type`، §۹.۴) هر اجرای سالمی را
  قرمز می‌کرد.
- **ناپایدارها outside**: `last_poll` و `poll_ms` حالتِ دستگاه نیستند؛ پیش‌فرض مقایسه
  نمی‌شوند، در `ignored_paths` و در markdown فهرست می‌شوند (پنهان نیستند) و با
  `--include-volatile` سنجیده می‌شوند.
- **PDU**: از `snmp_scrape_packets_sent` و `snmp_scrape_pdus_returned` (جمعِ روی همهٔ
  moduleهایِ capture، با `per_series` برای بازبینی) و از `SNMP_PDU_COUNT` در JSONِ snapshot.
  هر کدام نبود **`N/A`** نوشته می‌شود؛ ابزار چیزی اختراع نمی‌کند.
- **exit**: `0` بدونِ اختلافِ معنادار · `1` اختلافِ معنادار هست · `2` ورودیِ ناقص/ناسازگار
  (فایل نیست · JSON خراب است · ساختار شبیهِ collector نیست · brand پشتیبانی‌نشده یا غایب ·
  capture بی‌متریک · صفرِ «مسیرِ مشترکِ *معنادار*» — کلیدهایِ `brand/ip/name/online` برای
  این گیت شمرده نمی‌شوند، تا snapshotِ بی‌ربط از آن رد نشود).

### ۱۰.۳ چهار گام روی سرورِ خودتان

**۱) capture از exporter** (همان machine که ۹۱۱۶ باز است؛ هر دو module را بگیرید چون
`printer_hp` تنر/سینی/`sys_*` ندارد):

```bash
curl -s 'http://127.0.0.1:9116/snmp?target=172.16.0.5&module=printer_hp&auth=public_v2' \
    > /tmp/exp_hp.txt
curl -s 'http://127.0.0.1:9116/snmp?target=172.16.0.5&module=printer_std&auth=public_v2' \
    > /tmp/exp_std.txt
```

`snmp_scrape_packets_sent` در پاسخِ پیش‌فرضِ exporter هست و `snmp.yml` آن را رد نمی‌کند؛
اگر در capture شما نبود، ستونِ PDU `N/A` می‌ماند (و شما همان‌جا می‌فهمید که عددی در کار نیست).

**۲) snapshot از مسیرِ legacy، همان لحظه** (تا دو منبع هم‌زمان خوانده شوند؛ snapshotِ
کهنه «اختلافِ منبع» جا می‌زند):

```bash
cd /opt/printguard && python - <<'PY'
import json, time
from core.collectors.hp import collect_hp          # canon: collect_canon(ip, name, community, start)
snap = collect_hp("172.16.0.5", "Studio", "public", time.time())
open("/tmp/snap_hp.json", "w", encoding="utf-8").write(
    json.dumps(snap, ensure_ascii=False, indent=1, default=str))
PY
```

اگر snapshot را از `logs.db` (یا از `collect_enhanced`) برمی‌دارید، `brand` را یا با کلیدِ
`brand` در JSON بگذارید یا با `--brand`.

**۳) مقایسه:**

```bash
python tools/compare_snmp_sources.py \
    --exporter-capture /tmp/exp_hp.txt --exporter-capture /tmp/exp_std.txt \
    --snmp-snapshot /tmp/snap_hp.json --tolerance 5 \
    --json /tmp/cmp.json --md /tmp/cmp.md ; echo "exit=$?"
```

**۴) تفسیرِ گزارش** (`/tmp/cmp.md` را نگه دارید، نه در repo):

| چه می‌بینید | یعنی چه |
|---|---|
| `exit=0`، `diff 0` | برای این دستگاه، مسیرِ exporter همان اعدادِ legacy را می‌دهد؛ روشن‌کردنِ `USE_SNMP_EXPORTER=1` ریسکِ عددی ندارد |
| `diff` روی `counters.*` | اول snapshot را *تازه* بگیرید؛ اگر ماند، اختلافِ واقعیِ دو منبع است (یا `ignore_values`/walk که با spec نمی‌خواند) |
| `diff` روی `toners.*.status` / `trays.*.level` | احتمالاً sentinelها؛ §۹.۴ دو واگراییِ شناخته‌شده را نام می‌برد (یکیِ `no_sensor→empty` تستِ صریح دارد) |
| `missing` هایِ `only_in_snapshot` | کاستیِ ساختاریِ آینه، نه خرابیِ دستگاه؛ با `collect_enhanced` پر می‌شوند نه با adapter |
| `PDU = N/A` | آن متریک در ورودی نبوده — عددی برای مقایسه ندارید؛ `snmp_scrape_packets_sent` را به capture اضافه کنید |

برای چند دستگاه یک حلقه کافی است (`for` روی `ip`ها)؛ `exit` را در CI نگه دارید (۰ =
هم‌خوان). سنجشِ «کاهشِ بسته‌ها» را با همان `snmp_scrape_packets_sent` در برابرِ
شمارشگرِ legacy که §۵ گفته انجام دهید — این‌جا ابزار فقط دو سمتِ *داده* را به هم می‌رساند.

### ۱۰.۴ در repo چه سنجیده شده و چه چیزی نه

- **سنجیده**: `tests/test_compare_snmp_sources.py` — ۲۴ تست، سه fixtureِ مصنوعی
  (`compare_snmp_pair_{ok,diff,canon}.json`؛ اعدادشان از `exporter_response_*.txt`ِ G.5.1 است،
  هیچ‌کدام از دستگاه واقعی نیست و هر سه در `_comment`شان مصنوعی‌بودن را اعلام می‌کنند): کدِ خروجِ ۰/۱/۲،
  سطرهایِ موردِ انتظار، رفتارِ آستانه (زیرِ ۵٪ `ok`، مرزِ ۵٪ `ok`، ۵.۰۰۱۹٪ `diff`)،
  رشته‌ها، `missing`، فیلدهایِ ناپایدار، `per_series`ِ PDU، و قطعی‌بودنِ گزارش‌ها
  (`md` بایت‌به‌بایت برابر، `rows` مرتب، round-tripِ JSON). یک تست هم CLI را با
  `subprocess` می‌زند تا `sys.exit(main())` هم پوشش داده شود.
- **ضدپوچی**: ۱۲ جهش روی ابزار — ۱۱ تا همان اول قرمز شدند (۱ تا ۱۳ تست). سطرِ
  «`rel <= tolerance` → `<`» **زنده ماند** (چون هیچ سنجه‌ای مرزِ دقیق را نداشت)؛
  پاسخِ درست نه شل‌کردنِ تست که افزودنِ `test_verdict_is_ok_exactly_at_the_tolerance_not_below_only`
  بود (۱۹۰۰ در برابرِ ۲۰۰۰ ⇒ `rel` دقیقاً ۵.۰۰)، و از آن پس ۱۲/۱۲ گرفته می‌شوند. بازگشتِ
  فایل‌ها با `cmp` بایت‌به‌بایت بود.
- **انجام‌نشده (عمداً)**: اجرای exporter، scrapeِ واقعی، عددِ PDUِ واقعی، و «چند درصد
  ترافیک کم شد». `compare_tool_test` در `monitoring/snmp-manifest.json` همین را ثبت می‌کند
  (`method: synthetic fixtures`، `real_capture: not_attempted — end of project on owner server`).
