# Phase G decisions — from the OID matrix to comparing the two sources

**مخزن:** شاخه‌ی `phase-c` · **تاریخ:** ۲۰۲۶-۱۰-۰۶ · **دامنه:** G.1 … G.6 + TD-016
**کامیت‌ها:** ۲۴ کامیت در `77d2a73..HEAD` (از `b426a53` تا `0124e0a`؛ رفعِ TD-016 = `37e78f2`)

**قاعدهٔ نگارش این سند (همان دستورِ فاز E):** برای هر تصمیم سه چیز نوشته شود — **دلیل**،
**جایگزینی که رد شد و چرا**، و **اثر**. هیچ عددی «به‌نظر می‌رسد» نیست: هر رقم یا از اجرای
ابزار/سوئیت روی همین repo است یا صریحاً «انجام‌نشده» خورده است. آنچه در sandbox سنجیده
نشد (باینریِ exporter، ویندوز، پرینتر واقعی) در این سند **ادعا نشده** و در
`docs/snmp-exporter-setup.md` §۵ و `monitoring/snmp-manifest.json` به‌عنوان `not_attempted`
ثبت است.

**وضعیت baseline پس از G:** سوئیت **۷۴۱ passed / ۰ failed / ۰ skipped** (۱۰۹ subtest) در هر
دو نشستِ `TZ=Asia/Tehran` و `TZ=UTC` · چهار گیت rc=۰: `build_snmp_yml --check` ·
`build_oid_matrix --check` · `check_oid_coverage` · `record_golden --check --only all`
(۱۰ fixtureِ طلایی در `tests/fixtures/golden/`، همه تفاضل‌صفر) ·
`pyflakes core web` **۳۵** (بدونِ تغییرِ baseline) ·
`logs.db` بی‌تغییر (`e29d40630c45fc1d392c9a11d60b8de2`) · `USE_SNMP_EXPORTER` **پیش‌فرض ۰** ·
`git remote -v` **خالی** (هیچ push انجام نشده) · تست‌هایِ فاز G: **۱۴۴** (۷+۵+۱۵+۱۳+۱۳+۱۶+
۲۲+۹+۲۰+۲۴، همه بدونِ skip).

---

## Summary table

| # | موضوع | تصمیم | کامیت | اثر بر production |
|---|---|---|---|---|
| ۱ | ماتریس OID | **مرجع = کد**، نه پلان؛ عدد **۱۳۶** (نه ۱۵۸) | `b426a53` + ۳ رفعِ پارس | گیتِ `--check`؛ سندِ تولیدیِ `docs/oid-matrix.md` |
| ۲ | `generator.yml` | specِ پروژه با **۶ ماژول / ۹۸ get / ۷ walk / ۳۱ ignore_values / ۱۰ probe-only در کامنت** | `00cd786` `337d290` `f288731` `d3e7b2a` `890d3d1` | هیچ کدِ اجرایی مصرفش نمی‌کند؛ گیتِ پوشش |
| ۳ | `snmp.yml` | **دستی از spec، با converterِ repo** (۸۵۴ خط / ۱۳۶ متریک / صفر کلیدِ زنده) | `6122e76` `83182aa` `d81d6d7` `ab979df` | load با باینری **موکول**؛ strict-loader با تستِ source-verified قفل شد |
| ۴ | نصبِ ویندوز | NSSM + localhost پیش‌فرض، **تنها یک سوئیچِ** `-AllowSubnetAccess` | `6c61158` `7690233` `85670ba` | فایلِ اجراییِ آماده؛ **اجرا نشده** |
| ۵ | ExporterClient + poller | مسیرِ exporter **پشتِ پرچم، پیش‌فرض ۰**؛ آینهٔ ساختارِ collector | `7dd6ec0` `0e53182` `9abc989` `be0ba78` `699228c` | poller +۲۸ خط / −۰؛ رفتارِ امروز دست‌نخورده |
| ۶ | ابزارِ مقایسه | **ابزار در repo، عدد روی سرورِ مالک** | `0124e0a` | صفر تغییرِ runtime |
| ۷ | TD-016 | رفعِ `core/collectors/hp.py` در کامیتِ **جدا** (مرزِ کلمه) | `37e78f2` | رفتارِ واقعیِ HP تک‌رنگ اصلاح شد؛ golden دست‌نخورده |

---

## G.1 — OID matrix from the code, not from the plan

**تصمیم:** مرجعِ شمارشِ OID، خروجیِ `tools/build_oid_matrix.py` است که خودِ کد را
source-scan می‌کند (نقشهٔ فایل→ماژول: `MODULE_BY_FILE` در `tools/build_oid_matrix.py:72`)؛ عددِ مبنای فاز **۱۳۶**
است، و هر رقمِ دیگری باید به یکی از سه پیمانهٔ سند برگردد، وگرنه «ردیف‌هایِ گستردهٔ
walk × index» یا تکرارها شمرده شده‌اند (`docs/oid-matrix.md` §۳).

**دلیل:** پلانِ فاز G به «ماتریس ۱۵۸ OID» ارجاع می‌داد در حالی که آن فایل در مخزن نبود و
عدد ۱۵۸ هیچ‌جای repo پیدا نمی‌شد (همین در سرِ `tools/build_oid_matrix.py:8-9` ثبت شده).
سنجیده‌شدهٔ امروز: ۱۳۶ OID یکتا · `core/oid/catalog.py::OID_CATALOG` = ۷۹ ورودی که ۷۶ تای آن
یکتا است (۳ OID تکراری برای fallback) · ۲۴۷ occurrence (یک OID می‌تواند در چند collector/فهرست
بآید) · نوع خواندن در ماتریس `get` = ۱۰۴ و `walk` = ۳۲ · ۱۰ OID یتیمِ کمکی/وب/ابزار.

**جایگزینی که رد شد:** (الف) ماتریسِ دستی — کهنه‌شدنِ بی‌صدا، همان چیزی که یک فازِ مستندسازی
نباید تولید کند؛ (ب) بالا بردنِ ۱۵۸ به «منبعِ جدید» با افزودنِ ردیف — یعنی OID جعلی. سه باگِ
پارسِ خودِ ابزار هم در سه کامیتِ رفع بسته شد (برچسبِ OID ترکیبی، کلیدِ dict برای مقدارِ dict،
شمرده‌شدنِ رشته‌هایِ غیر digit-dotted) + یک باگِ چهارم («نامِ ظرفِ enclosing» در طبقه‌بندی
walk، `890d3d1`)؛ این‌ها نشان داد خروجیِ ابزار هم بی‌تست نیست: ۵ تستِ `tests/test_oid_matrix.py`
از همان روز اول قطعی‌بودنِ بازتولید و در‌تاریخ‌بودنِ سند را می‌سنجد.

**اثر:** گیتِ `--check` با کدِ خروجِ ۱ برای سندِ کهنه، و همان گیت در TD-016 **دقیقاً کار کرد**
(۱۵ سطر از شیفتِ +۱۱ خطیِ `hp.py` قرمز شد و `--write` اصلاحش کرد).

---

## G.2 — `monitoring/generator.yml` with 6 modules

**تصمیم:** specِ شش‌ماژوله (`printer_std/hp/canon/brother/toshiba` + `sensor_ecs100g`) با
۱۳۶ ردیف، **۹۸ get** و **۷ walk-table**، ۳۱ `ignore_values`، و ۱۰ ردیفِ probe-only که
عمداً **در کامنت**‌اند؛ typeها: ۷۶ اعلام‌شده + ۶۰ استنتاجی (از کد). `check_oid_coverage.py`
گیتِ شمارش است (exit 0/1) و خروجی‌اش را همین session چاپ کرده: ۱۳۶/۱۳۶ پوشیده، یتیم‌ها ۱۰/۱۰
در کامنت، OID زندهٔ بیرونِ ماتریس ۰، `ignore_values` ۳۱/۳۱؛ به‌تفکیکِ ماژول —
brother ۹/۹ (get ۴/walk ۱) · canon ۱۶/۱۶ · hp ۱۴/۱۴ (get ۱۰/walk ۲) · std ۲۴/۲۴ (get ۱۱/walk ۲) ·
toshiba ۶۳/۶۳ (get ۴۷/walk ۲) · sensor ۱۰/۱۰.

**دلیل:** exporter ماژول‌محور است، پس spec هم ماژول‌محور؛ `ignore_values` در spec می‌ماند تا
هم آینهٔ G.5 (`IGNORED_METRICS`) و هم تستِ `tests/test_snmp_yml.py` آن را «ببینند» و افزودنِ
sentinelِ تازه به spec بدونِ به‌روزرسانیِ adapter تست را بشکند (این در §۹.۴ سندِ setup ثبت شده).

**جایگزینی که رد شد:** (الف) یک ماژولِ بزرگ — انتخابِ module بی‌معنی می‌شد و provenance
کد↔متریک گم می‌شد؛ (ب) حذفِ ردیف‌هایِ probe-only از فایل — گیتِ یتیم‌ها معنایش را از دست
می‌داد (۱۰/۱۰ «در کامنت» دقیقاً به‌همین خاطر است)؛ (ج) نگه‌داشتنِ نام‌هایِ کوتاه — در `snmp.yml`
برچسبی مثلِ `cap` یا `brother` هیچ چیزی نمی‌گوید، پس `337d290` نام‌ها را با ظرفِ
خواندن‌شدن qualification کرد (`candidate_subtrees_brother`) در حالی‌که نام‌هایِ گویا املایِ کد
را نگه داشتند (`toner_remain_1`)؛ شمارش و گروه‌بندی عوض نشد (تأیید: همان ۱۳۶/۶).

**اثر:** ۷ تستِ `tests/test_generator_yml.py` + ۵ تستِ ماتریس؛ `monitoring/generator.yml`
ورودیِ `tools/build_snmp_yml.py` است (G.3) و نام‌هایِ v3‌اش طبقِ قراردادِ فاز با
`f"public_{snmp_version}"` می‌خواند (`monitoring/generator.yml:35`).

---

## G.3 — `snmp.yml` by hand, but reproducible

**تصمیم:** `monitoring/snmp.yml` (۸۵۴ خط، ۱۳۶ متریک، صفر کلیدِ `ignore_values` زنده) **دستی
نوشته نشد که دستی بماند**: `tools/build_snmp_yml.py` آن از `generator.yml` می‌سازد و
`--check` گیتِ کهنه‌بودن است؛ provenance در `monitoring/snmp-manifest.json` (از `generated_by`
تا `compare_tool_test`)؛ pin: `snmp_exporter` **0.26.0**.

**دلیل:** `generator` رسمی به MIB vendor (Canon/Brother/Toshiba) نیاز دارد که در مخزن نیست —
جملهٔ دقیق در manifest («generator_unavailable_sentence») و در §۴ سندِ setup. چون loaderِ
v0.26.0 `UnmarshalStrict` است، هر کلیدِ ناشناس روی سرور = load error، پس مجموعهٔ کلیدهایِ
مجاز از **خواندنِ سورسِ** `config/config.go@v0.26.0` بیرون آمد و `tests/test_snmp_yml.py`
(۱۵ تست) همان را می‌سنجد — نه «امیدواریم load شود».

**جایگزینی که رد شد:** (الف) `snmp.yml` دست‌نویسِ محض — driftِ قطعی بینِ spec و config، و
«تستِ واقعی» هم که موکول است، هیچ گایتی نمی‌ماند؛ (ب) صبر تا دسترسیِ باینری — گیت‌هایِ ساختاری
(۱:۱ با spec، ۱۳۶/۱۳۶، ۷۶+۶۰ type، ۳۱ ignore) بی‌صدا نامعتبر می‌شدند؛ (ج) دست‌بردنِ
نام‌هایِ v3 در `snmp.yml` — رد شد و نگاشتِ runtime فقط در `tools/build_snmp_yml.py` + کامنتِ
بالای `auths` نشست (`ab979df`)، تا spec بخواند بماند.

**اثر:** ۱۳ + ۱۵ تست؛ شش جهشِ §۲ سندِ setup روی همین فایل رد می‌شوند؛ `load_test_sandbox:
not_attempted` و `load_test_planned: end of project on owner server` — یعنی «load شد» هیچ‌جا
ادعا نشده.

---

## G.4 — `windows/install-snmp-exporter.ps1`

**تصمیم:** نصبِ سرویس با NSSM، pin‌های `0.26.0` و `2.24` (خط ۴۶–۴۷)، **پیش‌فرضِ localhost**
(`127.0.0.1:9116` + `-RemoteAddress 127.0.0.1,::1`)، یک سوئیچِ opt-in
(`-AllowSubnetAccess` ⇒ `0.0.0.0:9116` + `LocalSubnet`)، رمز فقط از env با
`AppEnvironmentExtra` (MultiString؛ در لاگ **فقط نامِ** متغیرها نوشته می‌شود)، چرخشِ لاگ
(`AppRotateBytes 10485760`)، exit codes ۰/۱/۲ و چهار مسیرِ idempotentِ skip.

**دلیل:** exporter هیچ احرازِ هویتی ندارد؛ بازکردنِ ۹۱۱۶ روی LAN تصمیمِ امنیتی است، پس باید
**یک** سوئیچ باشد نه دو تنظیمِ جدا که بشود فراموش‌شان کرد (گپِ listen/firewall). NSSM نسبت به
`sc.exe`/Task Scheduler در restart و چرخشِ stdout قابل‌اتکاتر است (مقایسهٔ کامل در
`docs/windows-services.md` §۱).

**جایگزینی که رد شد:** Task Scheduler (بدونِ AppExit-restart واقعی)؛ `-ListenAddress` آزاد
(می‌شد listen را با فایروال ناسازگار انتخاب کرد)؛ رمز در argv/فایل (لاگِ سرویس و
`Get-Process` لو می‌دهند)؛ `#Requires -RunAsAdministrator` (روی PS 5.1 پیامِ گیج‌کننده
می‌دهد — جای آن بررسیِ دستیِ elevation با `exit 2`). بازبینیِ ثابت **هشت** باگِ واقعیِ PS 5.1
را در نسخهٔ اولِ خودم گرفت: regexِ بدونِ نقل‌قولِ تکی · `-RemoteAddress` رشته‌ای به‌جای
`String[]` · صدازدنِ `nssm` پیش از install · backtickهایِ سرگردان · `Set-ItemProperty -Type`
(نامعتبر) · ترتیبِ پیدا‌کردنِ `win64` به‌جای `-Recurse -Filter` · و اپراتورهای
`??` / `&&` / `-Parallel` که در 5.1 وجود ندارند. پس از رفع، ۸ جهش روی فایل رفت (§اثر، پایینِ همین
بخش).

**اثر:** ۳۶۹ خط / ۱۹٬۸۷۳ بایت، UTF-8 با BOM و LF (BOM برای Parserِ PS 5.1 لازم است؛
`write_file` نمی‌تواندش، پس بایت‌به‌بایت نوشته شد) · ۱۳ تستِ ساختار/متن · ۸ جهش، هرکدام
۲–۴ تست قرمز · **اجرا نشده** (ویندوز/دانلود در sandbox نیست؛ «تستِ واقعیِ ویندوز در فاز L
می‌ماند» — `docs/windows-services.md` §۶).

---

## G.5 — `ExporterClient` + the hybrid branch in the poller

**تصمیم:** خوانندهٔ `core/exporter_client.py` (۲۸۰ خط: پارسرِ متنِ exposition،
`query_brand` برای دو module، `is_available()` با timeout ۲ ثانیه، localhost پیش‌فرض) +
آینهٔ `core/collectors/exporter_adapter.py` (۳۷۸ خط: متریک → **شکلِ همیشگیِ** collector) +
شاخه‌ای در `core/poller.py` با شرطِ short-circuit
`if exporter_enabled() and _exporter_client().is_available():` که `return` می‌کند و در غیر این
صورت مسیرِ legacy را نمی‌شکند. **`USE_SNMP_EXPORTER` پیش‌فرض ۰.**

**دلیل:** روشن‌کردنِ پیش‌فرضِ یک منبعِ دادهٔ تازه بدونِ سنجشِ هم‌خوانی یعنی تغییرِ بی‌صدای
snapshotِ مصرفیِ UI و گزارش‌ها؛ و آینه به‌جایِ بازنویسی انتخاب شد تا اگر روزی exporter
روشن شد، معنایِ فیلدها را همان کدِ امروز بدهد — نه ترجمهٔ تازه. همین تصمیمِ آینه بود که
TD-016 را بیرون داد (یعنی ابزارِ بی‌طرف کار کرد).

**جایگزینی که رد شد:** (الف) حذفِ collectors و جانشین‌کردنشان — collectors طبقِ دستورِ مالک
دست‌نخورده می‌مانند (`git diff --stat 77d2a73..HEAD -- core web` = ۴ فایل؛ `snmp/protocol.py`
و `yield_engine.py` صفر)؛ (ب) ساختارِ دادهٔ تازه برای exporter — مصرف‌کننده‌ها (`web/`,
reportها) می‌شکستند؛ (ج) fallbackِ `try/except` روی هر متریک — snapshotِ نصفه‌تمام می‌ساخت، پس
یا کاملِ آینه یا هیچ؛ (د) تشخیصِ خطا از متنِ `# ERROR:` — سورسِ v0.26.0 (`expfmt/text_create.go`)
چنان مارکرِ متنی‌ای تولید نمی‌کند، پس خطا فقط از وضعیتِ HTTP و متریک‌هایِ غایب خوانده می‌شود.

**اثر:** ۴۷ تستِ بدونِ skip (۱۶+۲۲+۹) · ۱۷ جهشِ G.5 + سه جهشِ پسِ رفعِ TD-016 · آزمونِ
واژگونیِ پرچم (پیش‌فرضِ `1` ⇒ `3 failed / 694 passed`، بقیه سبز) · کاستیِ ساختاری **سنجیده
با AST**: آینه ۱۲ کلید از ۱۸ِ `collect_enhanced` تولید می‌کند؛ ششِ غایب = ۴×`cartridge_*` +
`nickname`/`device_type` که دو تای آخر را poller می‌چسباند، پس کاستیِ *عددی* چهار کلید است ·
سه تستِ «پیش‌فرض خاموش» که آزمونِ واژگونی را قرمز می‌کنند:
`tests/test_exporter_client.py::test_flag_is_off_unless_exactly_one` (هفت مقدارِ env:
`None/0/"" → False`، `1/«1 » → True`، `true/2 → False`) و دو تستِ flag-off در
`tests/test_poller_exporter_flag.py` ·
هیچ سنجهٔ واقعی روی دستگاه انجام نشد و §۹.۶ سندِ setup همین را فریاد می‌زند.

---

## G.6 — `tools/compare_snmp_sources.py` (a tool, not a number)

**تصمیم (مالک):** «ابزار ساخته شود، سنجش روی سرورِ مالک». پس repo این‌ها را دارد: ابزار
(۳۹۱ خط)، سه fixtureِ مصنوعی (`tests/fixtures/compare_snmp_pair_{ok,diff,canon}.json`)،
۲۴ تست؛ و **هیچ** عددی دربارهٔ PDU، کاهشِ ترافیک، یا «درصدِ هم‌خوانیِ دو منبع» ثبت نشده.

**دلیل:** در این sandbox نه exporter اجرا می‌شود نه پرینتر؛ عددی که می‌نوشتیم هیچ‌گاه
اندازه‌گرفته‌شده نبود. واحدِ مقایسه هم «مسیرِ ساختار» است نه «نامِ متریک»، و ترجمه از
**همان آینهٔ G.5** عبور می‌کند — مقایسه‌ای که خودش ترجمهٔ تازه داشته باشد، باگِ تازه هم دارد.
`missing ≠ diff` (وگرنه همان کاستیِ چهار کلیدی، هر اجرای سالمی را قرمز می‌کرد)؛ فیلدهایِ
ناپایدار (`last_poll`/`poll_ms`) خارج‌اند ولی **خروج‌بودنشان در گزارش چاپ می‌شود**؛ نبودِ
شمارشگر ⇒ رشتهٔ `N/A`، هیچ پیش‌فرضی اختراع نمی‌شود.

**جایگزینی که رد شد:** (الف) درجِ «۱۳۲ → ۶۳ بسته» در سند به‌عنوانِ عددِ سنجیده‌شده — تنها
ردِ ۱۳۲ در repo کامنتِ `monitoring/generator.yml:14` است که آن را *پیش‌بینیِ پلان* نوشته
(و خطِ ۱۲ همان‌جا نامِ این ابزار را از G.2 پیش‌بینی کرده بود)؛ پس repo هیچ عددِ
**اندازه‌گرفته‌شده‌ای** دربارهٔ PDU ندارد؛
(ب) پارسرِ دومِ موازی در ابزار — تستِ منبع‌محور جلوی همین را می‌گیرد (`assertNotIn
"def parse_prometheus_text"`, و نبودِ `urlopen`/`requests`)؛ (ج) exit 1 برای کلیدهایِ غایب.

**اثر:** قرارداد و چهار گامِ اجرای سرور در `docs/snmp-exporter-setup.md` §۱۰؛
`compare_tool_test = {method: "synthetic fixtures", real_capture: "not_attempted — end of
project on owner server"}` در manifest؛ **۱۲ جهش**: ۱۱ تا همان اول گرفته شدند (۱–۱۳ تست) و
«`rel <= tolerance` → `<`» زنده ماند، چون هیچ سنجه‌ای مرزِ *دقیق* را نداشت ⇒
`test_verdict_is_ok_exactly_at_the_tolerance_not_below_only` (۱۹۰۰ در برابرِ ۲۰۰۰ ⇒ rel دقیقاً
۵٫۰۰) اضافه شد و از آن پس ۱۲/۱۲.

---

## TD-016 — the HP toner-key bug that the mirror exposed

**تصمیم:** رفع در **کامیتِ جدا** (`37e78f2`) بیرونِ ترتیبِ گام‌ها، به تشخیصِ مالک، با
تستِ مستقل؛ آینه `_hp_toner_key` را import می‌کند، پس رفع **خودکار** به مسیرِ exporter هم
منتقل می‌شود (و همین، تستِ آینه را از «pinِ باگ» به «pinِ رفع» برد).

**دلیل (اعدادِ سنجیده‌شده قبلِ رفع):** تطبیقِ زیررشته‌ای با کلیدهایِ تک‌حرفی `k/c/m/y`، پنج
نامِ مصرفیِ رایج را رنگی می‌کرد — `Imaging Drum → magenta` · `CF470A Imaging Drum → cyan` ·
`Fuser Kit → black` · `Maintenance Kit → black` · `Document Feeder Kit → black`
(پنج، نه سه؛ در برنامه «سه» نوشته شده بود). اثر روی HP تک‌رنگ: `has_color_toner=True` کاذب ⇒
`full_color=None` و `black_white=None` به‌جای `black_white=total` — یعنی **گم‌شدنِ تفکیکِ
سیاه‌وسفید**، نه فقط یک برچسبِ زشت.

**جایگزینی که رد شد:** «آینه‌سازیِ باگ به‌عنوانِ رفتارِ مرجع» (وضعیتِ اولِ G.5 — همان چیزی که
باگ را زنده نگه می‌داشت و فقط فریاد می‌زد)؛ «رفع فقط در adapter» (collectors خراب می‌ماندند و
دو مسیرِ HP دو قاعده داشتند — تستِ سومِ همین فایل دقیقاً تک‌قاعده‌بودن را می‌بندد)؛
«شل‌کردنِ تست‌هایِ pin‌شده» (به‌جایش انتظاراتشان به `toner_2` تغییر کرد تا سنجه بماند).

**اثر:** `tests/test_hp_toner_key.py` = ۲۰ مورد (۱۷ حالتِ parametrized + ۳ تست، شاملِ مثبت‌ها:
`K → black`، `Toner C → cyan`) · بازتولیدِ `docs/oid-matrix.md` در همان کامیت (۱۵ سطر، +۱۱؛
درسِ ثبت‌شده در TD-016: هر ویرایشِ خطی در `core/collectors/*.py` مستلزمِ بازتولیدِ ماتریس است)
· **fixtureهای طلایی دست‌نخورده** (۱۰ فایل در `tests/fixtures/golden/`، `record_golden
--check --only all` تفاضل‌صفر؛ سناریوی mono تازه‌ای اضافه نشد که re-record لازم شود) ·
`docs/tech-debt.md`: ردیف TD-016 + بخشِ کامل، وضعیت
«✅ بسته — رفع شد».

---

## What is deferred to the owner's server (and not claimed anywhere)

| موکول | کجا سنجیده می‌شود | وضعیتِ فعلی |
|---|---|---|
| load شدنِ `snmp.yml` با باینریِ واقعی | §۵ سندِ setup (پایانِ پروژه روی سرور) | `load_test_sandbox: not_attempted` |
| اجرای `install-snmp-exporter.ps1` روی ویندوز | §۵ + `docs/windows-services.md` §۶ | نوشته/تست‌شده، **اجرا نشده** |
| scrape روی پرینتر واقعی و تأییدِ متریک‌ها | `tools/compare_snmp_sources.py` (§۱۰) | صفرِ تماسِ شبکه‌ای در sandbox |
| عددِ PDU و «کاهشِ ترافیک» | گزارشِ `/tmp/cmp.md` روی سرور؛ `snmp_scrape_packets_sent` | **در repo ثبت نمی‌شود** (تصمیم) |
| تأییدِ ۶۰ typeِ استنتاجی | §۵ (مقایسهٔ `snmp.yml` با پاسخِ واقعی) | ۷۶ اعلامی / ۶۰ استنتاجی، تأیید نشده |
| اجرای سوئیت در حالتِ PG | `tests/conftest.py` با `PG_DSN` | در فاز G تکرار نشد؛ عددِ موثق در `docs/phase-e-decisions.md` (۴۸۴/۷۱ skip) — و کدِ فاز G backend-agnostic است: `grep -c sqlite3` روی پنج فایلِ تازه = ۰، و diffِ poller صفر کلمهٔ SQL/نوشتن دارد |

## What is deferred but **not scheduled**

سه موردِ زیر در برنامه‌ی فاز G قرار داشتند و عمداً انجام نشدند. تذکرِ دقت: در اسنادِ مخزن،
«فاز L» تنها برای **سرویسِ ویندوزی** و **اجرای واقعیِ اسکریپت‌ها** نام برده شده
(`docs/windows-services.md` §۶، `docs/cutover-runbook.md:136,228`) و برای
**بازنشستگیِ `import_db`** به‌عنوان «گزینه‌ی روی میز» (`docs/tech-debt.md` TD-008). هیچ فایلِ
موجودی حذفِ `protocol.py` یا حذفِ مسیرِ legacy را به فازی **گره نزده است** — تنها چیزی که
ثبت شده این است که در G دست‌نخورده ماندند (`docs/snmp-exporter-setup.md` §۹.۲). پس این‌ها را
«موکول به L» **ننویسید**، مگر وقتی تصمیمِ فازِ بعد گرفته شود:

- **حذفِ `core/snmp/protocol.py`** — اندازهٔ کار **سنجیده شد**: ۲۱ فایل `core.snmp` را import
  می‌کنند (۱۳ production: ۱۱ در `core/` از جمله `poller.py`، `enhanced_collector.py` و هر
  شش collector + ۲ در `web/routes/{discover,printers}.py`؛ ۶ در `tests/` از جمله
  `fake_snmp.py` و `golden_harness.py`؛ ۲ در `tools/`) و خودِ فایل ۵۶۷ خط است (+ ۸۳ خط
  `core/snmp/oid_map.py`). این «یک کامیتِ پاک‌سازی» نیست؛ پیش‌نیازش هم‌خوانیِ سنجیده‌شدهٔ §۱۰
  روی چند دستگاه + تأییدِ مالک است. تا آن موقع ۱۰ ردیفِ یتیمِ ماتریس (۱۰/۱۰ در کامنت)
  سندِ زنده‌ماندنِ همین فایل‌اند.
- **حذفِ مسیرِ SNMPِ دست‌ساز با `USE_SNMP_EXPORTER=0`** — یعنی روزی که exporter **پیش‌فرض**
  شود؛ همان روز باید پرچم هم حذف شود و سه تستِ «پیش‌فرض خاموش» (ردیفِ G.5) بازنویسی شوند —
  امروز همان‌ها آزمونِ واژگونی را قرمز می‌کنند.
- **NSSM برای `printguard-flask`** (الگوی G.4، با همان `AppRotateBytes` و env برای رمز).

## Rollback plan (per-step revert)

| گام | چه چیزی برمی‌گردد | ریسک |
|---|---|---|
| G.6 | `0124e0a` (ابزار + تست + §۱۰ + کلیدِ manifest) | صفر — به runtime وصل نیست |
| G.5 | `7dd6ec0 0e53182 9abc989 be0ba78 699228c` (۳ فایل + ۲۸ خط poller) | صفر با پرچمِ خاموش؛ `USE_SNMP_EXPORTER=0` امروز پیش‌فرض است |
| G.4 | `6c61158 7690233 85670ba` (اسکریپت + تست + راهنما) | صفر — اجرا نشده |
| G.3 | `6122e76 83182aa d81d6d7 ab979df` | صفر — هیچ سرویسی `snmp.yml` را مصرف نمی‌کند |
| G.2 | `00cd786 337d290 f288731 d3e7b2a 890d3d1` | صفر (گیت‌ها می‌روند، کد نمی‌رود) |
| G.1 | `b426a53 870b6da 243e2e1 2988f65` + `docs/oid-matrix.md` | ⚠ سندِ مرجعِ شمارش از دست می‌رود؛ ارجاع‌های §۹.۲/§۱۰ باید هم بروند |
| TD-016 | `37e78f2` | ❌ **برنگردانید** — باگِ رفتاری است، نه سلیقه‌ای |

**تنها راهِ «خاموش‌کردنِ کاملِ فاز G» بدونِ revert:** چیزی لازم نیست — پرچم پیش‌فرض ۰ است و
`is_available()` بی‌جواب می‌ماند؛ یعنی رفتارِ productionِ امروز همان رفتارِ قبلِ فاز G است.
