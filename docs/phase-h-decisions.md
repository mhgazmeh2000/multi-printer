# تصمیم‌های فاز H — «تکمیل Metrics Collector (باقی‌مانده بعد از G.5)»

**مبنا:** شاخهٔ `phase-c`، ادامهٔ `8047526` (پایان G.7)؛ گام H.1 در دو کامیت بسته شد
(`7bd2959` + `fix(h)`ِ بعدش که دو انحرافِ نخست را طبقِ متنِ برنامه اصلاح کرد — §«بازبینی» پایین).
**خواندن همراه:** `docs/snmp-exporter-decision.md` (تصمیم‌های فاز G و دلیلی که G.5 همه‌چیز را جمع
کرد)، `docs/snmp-exporter-setup.md` §۹ (قراردادِ آینه)، `docs/tech-debt.md` (TD-017/TD-018).
**قاعدهٔ شماره‌ها:** هر عدد این‌جا از *همین* session و از مخزن خوانده شده (`wc -l`، خروجیِ
`pytest`، `grep`، `ast`)؛ هیچ عددی از برنامه کپی نشده و جایی که برنامه تخمین زده و ما اندازه
نگرفته‌ایم، همان‌طور نوشته شده.

---

## چرا فاز H بعد از G.5 کوچک شد

فاز G با یک تصمیم تمام شد: `core/collectors/exporter_adapter.py` خروجیِ `snmp_exporter` را به
*همان شکلِ* `collect_hp`/`collect_canon` برمی‌گرداند (۱۲ کلید، صفر کلیدِ اضافه) و
`USE_SNMP_EXPORTER` پیش‌فرض ۰ است. سه چیز از H افتاد:

1. **«collector جدید لازم نیست»** — G.5 آن را به‌صورتِ آینه داد؛ در H فقط *شکاف‌هایِ* آینه می‌ماند.
2. **sunsetِ `core/snmp/protocol.py` از H بیرون رفت** — دامنه‌اش ۲۱ فایلِ import‌کننده است
   (۱۳ production / ۶ تست / ۲ ابزار) و به فازِ خودش موکول شد.
3. **سنجهٔ میدانی در sandbox ممکن نیست** — اجرای واقعیِ exporter، scrape روی پرینتر واقعی و
   `snmp_scrape_packets_sent` در manifest `not_attempted`‌اند؛ هر چه در H سنجیده می‌شود
   تست‌محور روی fixtureها است و تعهدِ سرور به §۱۰ سندِ setup موکول است.

نتیجه: H = چهار گام، هر گام یک کامیت — H.1 وب‌اسکرپ‌ها، H.2 backoff/circuit-breaker،
H.3 مسیرِ کامل در برابرِ golden، H.4 اسناد/بدهی.

---

## H.1 — وب‌اسکرپِ تونر در مسیرِ exporter

### دامنه (تصمیمِ مالک: گزینهٔ ۱)

| برند | مسیرِ exporter | HTTP fallback |
|---|---|---|
| HP | ✅ | ✅ |
| Canon | ✅ | ✅ |
| Toshiba | ❌ → SNMPِ legacy | ❌ |
| Brother | ❌ → SNMPِ legacy | ❌ |
| ECS100G | ❌ → SNMPِ legacy | — |

سطرِ اولِ «مسیرِ exporter» از یک چیز می‌آید، نه از سلیقه:
`SUPPORTED_BRANDS = ("hp", "canon")` در adapter؛ برایِ بقیه `collect_via_exporter` *پیش ازِ هر
کوئری* `None` می‌دهد و poller مسیرِ مستقیم را می‌نویسد. سه گاردِ پارامتریز شده
(`test_toshiba_does_not_enter_exporter_path`، `test_brother_does_not_enter_exporter_path`،
`test_sensor_does_not_enter_exporter_path`) همین را
می‌بندند: `query_brand` صدا زده **نمی‌شود**، وب‌اسکرپ صدا زده **نمی‌شود**، و دقیقاً یک
`log.warning` (به‌ازای `(brand, ip)`، نه هر چرخهٔ poll) نوشته می‌شود.

### تصمیم

1. **دو قاعدهٔ خالص، کپیِ وفادار در adapter:** `_toner_level_missing` (منبع:
   `core/enhanced_collector.py:132-142`) و `_merge_scraped_toners` (منبع: همان‌جا `:145-177`)،
   هر دو با docstringِ «قاعدهٔ `core/enhanced_collector.py:<line>`» و **صفر importِ تازه** از آن
   ماژول. تنها دو انحرافِ مجاز و شمرده: حذفِ bookkeepingِ `raw_level` و `Dict`→`dict`.
2. **قاعدهٔ trigger یکنواخت:** وب فقط وقتی SNMP برای هیچ تونرِ واقعی عدد ندارد
   (`_toner_level_missing`) — برایِ HP *و* Canon، همان‌طور که برنامه نوشت «فقط وقتی SNMP داده
   ندارد یا ناقص است».
3. **وب فقط *بعد از* پاسخِ exporter**، و فقط در `collect_via_exporter` — `exporter_to_collector_shape`
   خالص/آفلاین می‌ماند.
4. **اسکریپرها import می‌شوند** (`_scrape_hp_toners`، `_scrape_canon_toners`) با **صفر کپی** —
   تصمیمِ نهاییِ مالک (پاسخِ H.1، پایین). merge در این فایل فقط یک تابع است: `_merge_scraped`
   که **هیچ چیزی را بازنویسی نمی‌کند و فقط deleg می‌کند** (canon → `_prefer_scraped_canon_toners`،
   بقیه → `_merge_scraped_toners`)؛ خودِ دو قاعده کپی‌هایِ وفادارِ بندِ ۱ هستند.
5. **`_used`/سریالِ EWS و retryِ «total=0» آینه نمی‌شوند** → TD-017؛ mirrorِ سه برندِ دیگر
   → TD-018؛ گره‌ی importِ `core.collectors.*` → TD-019.

| برند | کی وب می‌زنیم؟ | timeout | شکلِ بازنویسی | `source` |
|---|---|---|---|---|
| `hp` | `_toner_level_missing(toners)` | ۴٫۰ = پیش‌فرضِ `_scrape_hp_toners:12` | پرکردنِ فیلدهایِ خالی + نردبانِ `0/10/25` | `hp_web` |
| `canon` | همان قاعده (گاردِ یکنواخت) — **نه** «همیشه» خودِ collector | ۳٫۰ = `timeout=3.0`ِ صریحِ `collect_canon:325` | جاگذاریِ رکوردِ کاملِ HTTP | `http_scrape` |

### دلیل

- **چرا کپی و نه import برایِ قاعده‌ها:** `enhanced_collector` یعنی `core.snmp.protocol` +
  `core.store` + `core.database` در مسیرِ collector؛ گیتِ ASTِ G.5
  (`test_adapter_has_no_side_effects`) دقیقاً برایِ همین است که آینه «کوئری + تبدیلِ متن» بماند،
  نه مصرف‌کنندهٔ DB. ضمناً قاعدهٔ «`collectors/` دست‌نخورده» را نگه می‌دارد: نمی‌شد دو تابع را
  به `core/collectors/base.py` منتقل کرد.
- **چرا قفلِ *متنی* برایِ کپی‌ها:** کپی، واگراییِ بی‌صدا می‌سازد؛ پس همان الگوی TD-016
  (`test_rule_matches_the_web_helper`) با `inspect.getsource` + normalize جاری شد
  (`test_merge_functions_are_faithful_copies`). قاعده‌هایِ نرمال‌سازی *شمرده‌اند* (بلوکِ
  `raw_level`، کلیدِ `"raw_level": pct`، `Dict`→`dict`) و همین شمرده‌بودن، خودِ ادعاست. در کنارش
  قفلِ *رفتاری* هم هست (`test_merge_and_missing_rules_match_enhanced_collector`) تا «متنِ دوقلو»
  تنها شاهد نباشد.
- **چرا trigger یکنواخت:** متنِ برنامه. اثرش این است که روی canonِ سالم (SNMP عدد دارد) آینه وب
  نمی‌زند، برخلافِ `collect_canon` که همیشه می‌زند؛ پس این دو مسیر روی canon *می‌توانند* دو جواب
  بدهند و این واگرایی پین شده است (`test_canon_divergence_from_the_collector_is_pinned` — با
  شاهدِ خوانده‌شده از سورسِ collector، نه با ادعایِ ما).
- **چرا فقط بعد از exporter:** وعدهٔ G.5 «داده، نه دادهٔ ساختگی» بود؛ وب لایهٔ *تکمیلی* است و
  اگر قبل از exporter می‌رفت، نبودِ متریک در `snmp.yml` با یک منبعِ دیگر پر می‌شد و مقایسهٔ G.6
  معنایش را از دست می‌داد.

### جایگزین‌هایی که رد شدند

1. **گزینهٔ ۲ دامنه (Toshiba هم در H.1)** — رد. حجمِ اندازه‌گیری‌شده: ماژولِ `printer_toshiba` در
   `monitoring/snmp.yml` = **۳۶۹ خط با ۶۳ ردیفِ `oid:`** (کل فایل ۸۵۴ خط و ۱۳۶ OID — یعنی
   Toshiba به‌تنهایی ~۴۶٪ ردیف‌ها) و `core/collectors/toshiba.py` = ۵۲۸ خط با
   `_scrape_toshiba_toners` ۸۵ خط. این یک G.5 دیگر است، نه یک ضمیمه ⇒ TD-018.
2. **گزینهٔ ۳ دامنه (Toshiba + Brother + ECS100G)** — رد. `printer_brother` = ۹ ردیفِ OID/۵۸ خطِ
   ماژول + `_scrape_brother_toners` (۹۳ خط)؛ `sensor_ecs100g` = ۱۰ ردیف/۵۴ خط و شکلِ خروجی‌اش
   `toners` ندارد که بشود `raw_level` را روی آن سوار کرد. همان‌جا هم TD-018.
   *(عددِ «~۱۵۰/~۸۰ خطِ adapterِ تازه» در برنامه تخمین بود؛ مقدارِ اندازه‌گیری‌شده‌ای برایش ثبت
   نشده و عمداً به‌عنوانِ شاهد ننوشتم.)*
3. **importِ `core.enhanced_collector` برایِ استفادهٔ مستقیمِ `_merge_scraped_toners`** — رد (بالا).
4. **گیتِ «canon هم مثلِ collector همیشه اسکریپ»** — رد؛ نسخهٔ اولِ همین گام آن را پیاده کرده بود
   و با متنِ برنامه نمی‌خواند. اگر مالک ترجیح داد برگردد، یک‌خطی است (گاردِ `_toner_level_missing`
   از بالایِ `apply_toner_http_fallback` به داخلِ شاخهٔ `hp` منتقل شود) و تستِ واگرایی باید برعکس
   شود.
5. **fallback داخلِ `exporter_to_collector_shape`** — رد؛ `tools/compare_snmp_sources.py` همان تابع
   را آفلاین مصرف می‌کند.
6. **بی‌صدا برگرداندنِ برندِ آینه‌نشده** — رد؛ یک `log.warning` با ارجاعِ TD-018 می‌آید (نه هر poll).

### اثر (سنجیده در همین session)

- `core/collectors/exporter_adapter.py`: **۵۵۶ خط / ۳۰٬۳۸۰ بایت** (پیش از H.1: ۳۷۸ / ۱۹٬۰۹۱).
  نام‌هایِ تازه: `CANON_HTTP_TIMEOUT`، `HP_HTTP_TIMEOUT`، `_WARNED_UNSUPPORTED`،
  `apply_toner_http_fallback`، `_toner_level_missing`، `_merge_scraped_toners`،
  `_prefer_scraped_canon_toners`، `_CANON_TONER_MAP`، `_merge_scraped`.
- `tests/test_exporter_adapter.py`: **۳۸ تست** (از ۲۲؛ +۱۶)، فایلِ ۷۱۲ خط / ۴۲٬۸۳۱ بایت؛ سه
  فایلِ exporter مجموعاً **۶۳ تست، صفر skip**. fixtureِ autouse (`no_web_by_default`) هر دو اسکریپر را بی‌اثر
  می‌کند و «اصلِ bind‌شده» را برایِ پینِ هویت نگه می‌دارد.
- گیتِ AST به‌روز شد: `core.collectors.canon` به allowlist اضافه شد + دو assertِ تازه
  («هر دو اسکریپر باید واقعاً صدا زده شوند»، «`fetch_first_web_page`/`Request` در این فایل نباشد»).
- پروبِ جهش: **۱۹ جهش، ۱۹ گرفته شد، صفر بی‌اثر** (دورِ سوم، بعد از dispatcher). چهار پروب،
  چون سه‌تایشان چیزی را می‌گیرند که با «فقط رفتار» قابل دیدن نیست:
  * «فقط شکلِ بیانِ کپی عوض شود» (`for k in toners.keys()` → `for k in toners`) → **تنها** تستِ
    هم‌متنی می‌شکند (هیچ تستِ رفتاری نمی‌تواند بفهمد)؛
  * «dispatcher حذف شود و قاعده‌ها مستقیم صدا زده شوند» — رفتار *یکی* است و فقط assertِ متنیِ
    «تک‌درِ merge» می‌گیرد؛ یعنی بندِ ۱-۳ (deleg) قفل شده، نه فقط نوشته؛
  * «`_used` گاردِ عددی» هم همین‌طور (رفتاراً دو گارد یکی را پوشش می‌دهند)؛
  * «`"raw_level": pct` به دیکشنریِ بازگشتی برگردد» → تستِ هم‌متنی عمداً **نمی‌شکند** (نرمال‌سازی
    حذفش می‌کند) و دو تستِ رفتاری می‌شکنند — یعنی تلورانسِ متنی آسیبِ واقعی را نمی‌پوشاند.
- `docs/snmp-exporter-setup.md`: §۹.۲ (جدولِ خط/بایت و تعدادِ تست) و §۹.۴ (ادعایِ «اسکریپرها آینه
  نشدند») با H.1 هم‌خوان شد.

### محدودیت‌هایِ شناخته‌شدهٔ H.1

| بند | وضعیت | شاهد |
|---|---|---|
| `_used` / `hp_web_used` / `note="used_cartridge"` (کارتریج شارژی از `SupplyState=Used`، `enhanced_collector.py:1263-1279`) | آینه نشد | TD-017؛ `_merge_scraped_toners` کلیدِ «_» را رد می‌کند |
| شرطِ «total=0 مشکوک ⇒ دوباره SNMP بزن» | آینه نشد | TD-017؛ state می‌خواهد → H.2 |
| mirrorِ `toshiba`/`brother`/`sensor` | خارجِ دامنه | TD-018 (شرط انجام: تستِ ۴۸ ساعتهٔ HP/Canon روی سرورِ مالک) |
| «درامِ ۹۰٪ + تونرِ بی‌عدد» در HP | fallback **نمی‌کند**، چون قاعده فقط `drum`/`opc` را معاف می‌کند و درامِ HP روی کلیدِ `toner_2` می‌نشیند | `test_hp_drum_number_blocks_fallback_like_the_rule` (قفلِ آگاهانه) |
| canon روی دستگاهِ سالم | آینه وب نمی‌زند، collector می‌زند | `test_canon_divergence_from_the_collector_is_pinned` |

### باگِ همین گام (ثبت شد، چون هیچ گیتی نگرفت)

نخستین نسخه `apply_toner_http_fallback` را با `brand`ِ *خامِ* فراخوان مقایسه می‌کرد؛ poller برند
را از ستونِ DB می‌دهد (`"HP-05"`/`"CANON"`) و `"CANON" != "canon"` یعنی وب‌اسکرپِ canon بی‌صدا
خاموش. رفع: مقایسه با `shape["brand"]` + `test_fallback_keys_off_the_normalized_brand`
(پروبِ M10 همان را می‌شکند).

### معیارهایِ پذیرش و شاهدِ هرکدام

| معیار | شاهد (سنجیده، نه ادعا) |
|---|---|
| صفر کپی از اسکرپرها | `test_adapter_uses_the_real_scrapers_not_copies` (هویتِ تابع‌ها) + گیتِ AST؛ `exporter_adapter.py` هیچ `urlopen`/`requests`/HTML-پارسِ تازه‌ای ندارد |
| گیت AST با `core.collectors.canon` در allowlist | `test_adapter_has_no_side_effects`؛ `banned` دست‌نخورده (۷ نام) و دو assertِ تازهٔ «هر دو اسکریپر صدا زده شوند» |
| merge فقط deleg | `test_merge_scraped_delegates_per_brand` + assertِ متنیِ «`apply_toner_http_fallback` نباید قاعده‌ها را مستقیم صدا بزند» |
| ۶ تست H.1 (از جمله هویت) | شش نامِ خواسته‌شده همگی در `tests/test_exporter_adapter.py`‌اند؛ مجموعِ فایل ۳۸ تست |
| ۷۴۱ + تست‌های H.1 سبز | `757 passed / 0 failed / 109 subtests` در `TZ=Asia/Tehran` و `TZ=UTC` (پایهٔ ۷۴۱ + ۱۶ در این گام) و گیت‌هایِ چهارگانه rc=0 |
| golden ۱۰/۱۰ diff-zero | `tools/record_golden.py --check --only all` → «همه تفاضل-صفر» |
| `USE_SNMP_EXPORTER` پیش‌فرض ۰ | `exporter_enabled()` روی `False`؛ `core/exporter_client.py` در این گام دست‌نخورده |
| collectors دست‌نخورده | `git diff` فقط `core/collectors/exporter_adapter.py` را در آن پوشه نشان می‌دهد (فایلِ خودِ گام)؛ `hp/canon/toshiba/brother/base.py`، `enhanced_collector.py`، `snmp/protocol.py`، `yield_engine.py`، `config/` صفر تغییر |
| TD-018 + TD-019 ثبت | ردیف + بخشِ هر دو در `docs/tech-debt.md`، با حجمِ اندازه‌گیری‌شده |
| صفر push | `git remote -v` خالی |

### بازبینی (چرا H.1 سه کامیت شد)

کامیتِ نخست (`7bd2959`) دو جا از متنِ برنامه فاصله گرفت و به‌جای اجرا، «توجیه» کرد:
(الف) trigger canon را از `collect_canon` گرفت (همیشه) به‌جای قاعدهٔ یکنواختِ برنامه؛
(ب) قفلِ *متنیِ* خواسته‌شده را قفلِ *رفتاری* کرد. این دو در کامیتِ دوم اصلاح شدند
(گاردِ یکنواخت + `test_merge_functions_are_faithful_copies`)، و عددِ پروب‌ها از ۱۶/۱۷ به **۱۷/۱۷**
رسید.

کامیتِ سوم (`feat(h)`) بندِ ۲ برنامه را با پاسخِ مالک بست: importِ هر دو اسکرپر،
dispatcherِ `_merge_scraped`، سه گاردِ نام‌دار، TD-018/TD-019.

### تصمیمِ نهاییِ مالک (گام H.1): import + dispatcher، نه کپی

برنامه نوشت «کپیِ وفادار در adapter» برایِ هر دو اسکریپر. برایِ HP شدنی است
(`_scrape_hp_toners` = ۱۲۴ خط، `_key`/`_mark_used` تودرتو، transportش `base.fetch_first_web_page`
که از قبل import است)؛ برایِ Canon نه: `_scrape_canon_toners` (۱۰۱ خط، `canon.py:18-118`) داخلِ
بدنه `import requests as _req` (`canon.py:30`، `_req.get` در `:33`) و
`import urllib.request as _ur` + `_ur.urlopen` (`:48` و `:52`) دارد، و همان گیتِ AST
این دو اسم را در `exporter_adapter.py` **ممنوع** کرده (`banned = {..., "urlopen", "requests", ...}`).
یعنی «کپیِ وفادارِ canon» و «گیتِ AST G.5 سالم» با هم برقرار نمی‌شوند. سه راه:
**الف)** وضعیتِ فعلی — import هر دو اسکریپر، allowlistِ گیت یک ماژولِ collectorِ دیگر جا باز کرده
(اثری جز یک سطرِ allowlist ندارد؛ DB/رویداد همچنان ممنوع‌اند)؛
**ب)** HP کپی، canon import (دو سبک در یک فایل — به‌نظرم بدترین گزینه)؛
**ج)** کپیِ هر دو + حذفِ `urlopen`/`requests` از `banned` یا reroute کردن canon به
`fetch_first_web_page` (دیگر «وفادار» نیست و معنای timeout/هدرهای canon عوض می‌شود).
**پاسخِ مالک = گزینهٔ «الف»**، با سه قید که اجرا شدند: (۱) صفر کپی از اسکرپرها،
(۲) allowlistِ گیت فقط `core.collectors.canon` را جا باز می‌کند و `banned`
(urlopen/requests/store/add_event/DB) **دست‌نخورده** می‌ماند، (۳) merge در adapter یک تابعِ
*فقط-deleg* به نامِ `_merge_scraped` است. اثرِ جانبیِ این انتخاب — که importِ هر چیزی از
`core.collectors.*` کل `core.snmp.protocol`/`core.store`/`core.db.*` را بار می‌کند — در
**TD-019** ثبت شد (عدد: ۲۲ ماژولِ `core.*` روی importِ adapter، که ۲۰ تایشان حتی با
`core.collectors.base` تنها هم بار می‌شدند ⇒ importِ تازهٔ H.1 صفر ماژول اضافه کرده است).

---

## H.2 — backoff و circuit-breaker در `core/exporter_client.py`

**تصمیم:** state در *کلاینت*، به‌ازای `(ip, module)`؛ نردبان ۱–۲ فوری / ۳–۵ ۳۰۰s / ۶–۱۰ ۱۸۰۰s /
>۱۰ ۲۱٬۶۰۰s؛ موفقیت = حذفِ رکورد؛ یک ماژولِ در backoff = ردِ *کلِ برند*؛ `ExporterCircuitOpen`
زیرکلاسِ `ExporterError` است و آینه آن را در `debug` می‌نویسد.

**دلیل:**
- poller هر ۶۰ ثانیه می‌چرخد و `poller.py` برایِ هر پرینتر یک thread دارد
  (`core/poller.py:384`)؛ بدونِ backoff، یک پرینترِ خاموش سه درخواستِ بی‌پاسخِ ۵ ثانیه‌ای در
  هر چرخه می‌گیرد (`printer_hp` + `printer_std`، و timeoutِ ۵s) — یعنی ~۲۵ دقیقه از عمرِ
  thread در هر روزِ آن دستگاه، صرفِ صبر کردن.
- کلیدِ `(ip, module)`، نه `ip`: `snmp_exporter` هر ماژول را جدا scrape می‌کند و یک ماژولِ
  خراب (`printer_hp` در نسخه‌ای از `snmp.yml`) نباید `printer_std` را هم بلوکه کند.
- چرا `ExporterCircuitOpen` زیرکلاس است: هیچ مصرف‌کنندهٔ موجودی نباید تغییر کند —
  `collect_via_exporter` همان `except ExporterError` را دارد و `return None` می‌دهد؛ نوعِ تازه
  فقط برایِ *تنِ صدایِ* لاگ و برایِ عددِ `retry_in` لازم بود.
- چرا «نیمه‌خواندن» نه: شکلِ `collect_hp` از دو ماژول می‌آید؛ با یکی، `trays` و `counters`
  خالی می‌ماند و آن snapshotِ ناقص در DB می‌نشیند، در حالی که مسیرِ legacy همان چرخه می‌تواند
  کامل بنویسد.

**جایگزین‌هایی که رد شدند:**
1. state در `poller.py` (دیکشنریِ module-level) — رد: کلاینتِ poller کش‌شده است ولی عمرش
   تضمینی نیست، و تست‌هایِ H.3 باید همان منطق را دوباره بسازند.
2. `time.sleep()` پیش ازِ تلاشِ بعدی — رد: threadِ poll را می‌خواباند (۶ ساعت!).
3. ذخیرهٔ state در DB (ستونِ `next_retry_at` روی `printers`) — ردِ *موقتی*: مهاجرتِ schema در
   این فاز مجاز نیست و «مقدارِ زندهٔ حافظه» هم عمداً با restart پاک شود تا دستگاهِ
   برگشته بی‌درنگ دیده شود؛ بدهیاش در H.4 ثبت می‌شود.
4. `warning` برایِ هر چرخهٔ ردشده — رد (سیلِ لاگ؛ همان دلیلِ `_WARNED_UNSUPPORTED` در H.1).
5. خطایِ پارس را «شکستِ دستگاه» نشمردن — رد: پاسخِ خراب هم دقیقاً همان هزینهٔ «هر ۶۰ ثانیه»
   را دارد؛ `test_parse_error_counts_as_a_failure` همین را می‌بندد.

**اثر (سنجیده در همین session):** `core/exporter_client.py` **۴۳۷ خط / ۲۴٬۰۵۶ بایت**
(از ۲۸۰ / ۱۴٬۲۳۳؛ +۱۵۷ خط، که ~۴۰ تای آن داکسِ تصمیم/جدول است)؛ `core/collectors/exporter_adapter.py`
۵۵۶ → **۵۶۶** (importِ `ExporterCircuitOpen` + شاخهٔ debug + بندِ ۴ داکس)؛
`tests/test_exporter_backoff.py` تازه: **۱۶ تست / ۳۳۳ خط**؛ کلِ سوئیت **۷۷۳** (از ۷۵۷) سبز در
`TZ=Asia/Tehran` و `TZ=UTC`، صفر skip. APIِ تازه برایِ عیب‌یابی: `retry_in()`،
`consecutive_failures()`، `device_states()`، `reset_backoff(ip=None)`.
حافظه: فقط دستگاه‌هایِ شکست‌خورده رکورد دارند — `test_healthy_devices_leave_no_state_behind`
۵۰۰ خواندنِ سالم می‌زند و assert می‌کند `device_states()` خالی می‌ماند.
پروبِ جهش: **۱۱ جهش، ۱۰ گرفته‌شده**؛ تنها جهشِ «lock حذف شود» بی‌اثر ماند — همان چیزی که
`test_concurrent_failures_lose_nothing` در docstringش صادقانه می‌گوید (تستِ ترэдی
«آسیب‌ندیدن» را می‌سنجد، نه «ضرورتِ lock»؛ اثباتِ ریس در CPython با این حجم ممکن نیست).

**محدودیت‌ها:** state با restart می‌پرد (عمدی)؛ `is_available()` در backoff نیست (خطای
exporter، نه خطایِ دستگاه)؛ circuit-breaker عمداً *نیمه‌باز* است — بعد از cooldown دقیقاً یک
تلاشِ آزمونی در همان چرخه، نه چندتا. 

---

## H.3 — مسیرِ کاملِ `USE_SNMP_EXPORTER=1` در برابرِ golden

### تصمیم (گزینهٔ ۱ مالک: پیمانِ برگ‌های قابل‌استخراج + جدول واگرایی)

مسیر کامل پرچم `USE_SNMP_EXPORTER=1` روی تمام ۵ سناریوی fixtureهای طلایی (`tests/fixtures/golden/enhanced_*.json`)
با شرایط کاملاً ایزوله (hermetic)، زمان منجمد (`frozen_time`)، دیتابیس موقت (`isolated_db`) و نگاشتِ Prometheus
برگرفته از مشخصات رسمی (`monitoring/snmp.yml`) آزموده و قفل شد:

1. **پیمان ۳۰ برگِ قابل‌استخراج (Extractable Leaves):** هر ۳۰ برگِ داده‌های خام SNMP برای HP color
   در هر دو حالت پر (`enhanced_01`) و خالی (`enhanced_02`) با تفاضلِ صفر (Zero Diff) با خروجی golden تطبیق دارد.
2. **پیمان عدم ورود (Non-entry):** برای برندهای پشتیبانی‌نشده (Brother، Toshiba، ECS100G)، به exporter
   هیچ درخواستی نمی‌رود (`fake.calls == []`)، آداپتور مقدار `None` می‌دهد، و دقیقاً یک `log.warning` با ارجاع
   به TD-018 ثبت می‌شود. در سطح ۲، پولر به شکل تمیز و بدون خطا به مسیر مستقیم legacy برمی‌گردد.
3. **شکاف fixtureهای طلایی (TD-020):** دو کلید `counters.full_color` و `counters.black_white` در خروجی exporter
   برابر `None` هستند؛ این به دلیل نبود OIDهای اختصاصی اچ‌پی (`1.3.6.1.4.1.11.2.3.9.6.1.1.*`) در فیکسچرهای
   دست‌نویس اولیه است و در **TD-020** ثبت شد تا بعد از capture واقعی روی سرور رفع شود.
4. **عدم انتقال غنای legacy به آداپتور:** ۱۲۶ برگ غنای legacy (شامل ۴ کلید `cartridge_*`، مشخصات یادگیری
   `yield_engine`، فیلدهای ظرفیت و هشدارهای ترکیبی تونر خالی) عمداً در آداپتور غایب هستند. خط قرمز «کپی ممنوع»
   و «collectors دست‌نخورده» کاملاً رعایت شد.
5. **رفتار سطح ۲ پولر:** در آزمونِ `poller.collect()` با پرچم فعال، جاروی سنگین ۱۳۲ فراخوانی OIDهای کلکتور
   کاملاً bypass می‌شود (توالی فراخوانی transport فقط شامل حداکثر ۳ فراخوانی health probe است)؛ فراداده‌های
   `nickname` و `device_type` توسط پولر به دیکشنری افزوده شده و در `store.printer_data` می‌نشینند؛ و
   مطابق طراحی G.5، این مسیر هیچ سطری در جداول شمارنده دیتابیس نمی‌نویسد (صفر ردیف در DB).

### جایگزین‌هایی که رد شدند

1. **گزینهٔ ۲ (گسترش آداپتور برای شبیه‌سازی غنای کاتالوگ و تخمین):** رد؛ نقض صریح قانون «کپی ممنوع» و
   بارگذاری منطق تاریخچه و پایگاه داده در آداپتور بود.
2. **گزینهٔ ۳ (تنها بستن wire-up و واگذاری تطابق داده به سرور):** رد؛ بدون داشتن پیمانِ برگ‌های قابل‌استخراج،
   رگرسیون‌های ناخواسته در لایه آداپتور قابل کشف در محیط CI نبودند.
3. **ادعای خطای دیتابیس برای ثبت نشدن شمارنده‌ها:** رد؛ ثبت نشدن رویداد شمارنده در مسیر exporter رفتارِ
   طراحی‌شدهٔ G.5 است (`collectors` دست‌نخورده می‌ماند) و یک باگ نیست.

### اثر (سنجیده در همین session)

- `tests/test_hybrid_full_path.py`: فایل جدید با **۱۲ تست / ۵۳۴ خط** (شامل نگاشت خودکار از `snmp.yml`،
  آزمون‌های سطح ۱، آزمون عدم وجود برگ طبقه‌بندی‌نشده، آزمون‌های غیرپشتیبانی و آزمون‌های سطح ۲ پولر).
- کل سوئیت: **۷۸۵ تست** (پایهٔ ۷۷۳ + ۱۲ تست تازه)؛ سبز در هر دو منطقهٔ زمانی `TZ=Asia/Tehran` و `TZ=UTC`.
- گیت‌های چهارگانه: همگی rc=0؛ فیکسچرهای طلایی ۱۰ از ۱۰ تفاضل-صفر (`record_golden.py`).
- گزارش کامل واگرایی: `docs/hybrid-path-report.md` (شامل جدول ۵ دسته‌ای، لیست کامل ۳۰ برگ منطبق،
  توضیح شکاف OIDهای HP و لیست ۱۲۶ برگ غنای legacy).
- بدهی فنی: ثبت **TD-020** در `docs/tech-debt.md`.
- فایل‌های دست‌نخورده: `core/collectors/exporter_adapter.py`، `core/enhanced_collector.py`،
  `core/snmp/protocol.py`، `yield_engine.py`، پوشه‌های `config/` و `monitoring/` صفر بایت تغییر.

---

## H.4 — بستن مستندات، تحلیل بدهی‌های فنی و بررسی نهایی فاز H

### دستاوردها و وضعیت گام‌های فاز H
با اتمام گام‌های چهارگانهٔ فاز H، اهداف تکمیل Metrics Collector و تعمیق آزمون‌های مسیر پیوندی محقق شد:
- **H.1 (وب‌اسکرپِ تونر در آداپتور):** مکانیزم fallback به HTTP scrapers با ایمپورتِ بدون کپی برای HP و Canon، گارد یکنواخت trigger، و پین متنی/رفتاری کپی‌های وفادار (`7bd2959`, `b0bf78c`, `a4fae7e`).
- **H.2 (مکانیسم Backoff و Circuit Breaker):** مدیریت خطای دستگاه‌های خاموش در `core/exporter_client.py` با نردبان زمانی ۱–۲ فوری، ۳–۵ ۳۰۰s، ۶–۱۰ ۱۸۰۰s، و بیش از ۱۰ بار شکست ۶ ساعت مدار باز (`afbbe1a`).
- **H.3 (مسیرِ کامل در برابر Fixtureهای طلایی):** آزمون‌های hermetic سطح ۱ و سطح ۲ در `tests/test_hybrid_full_path.py` (۱۲ تست)، اثبات تفاضل صفر ۳۰ برگ قابل‌استخراج، صفر برگ طبقه‌بندی‌نشده، اثبات bypass شدن جاروی OID در سطح پولر، صفر ردیف در دیتابیس، و تدوین گزارش جامع `docs/hybrid-path-report.md` (`4b395eb`, `f21cde4`).
- **H.4 (این گام — مستندات نهایی و ثبت بدهی‌ها):** بررسی و ثبت کامل بدهی‌های فنی TD-018، TD-019، TD-020 و TD-021.

### بررسی بدهی‌های فنی فاز H
1. **TD-018 (عدم آینه‌سازی برندهای Toshiba/Brother/ECS100G):** با گارد صریح non-entry و ثبت هشدار کنترل شده و هدایت خودکار به legacy؛ رفع به پس از آزمون ۴۸ ساعته روی سرور موکول شد.
2. **TD-019 (گره وابستگی Transitive در کلکتورها):** وابستگی ناخواستهٔ ایمپورت `core.collectors.*` به `core.db`؛ رفع به فاز L (جداسازی پنل‌های وب‌اسکرپ از SNMP) موکول شد.
3. **TD-020 (شکاف Fixture برای OIDهای شمارنده اچ‌پی):** فیکسچرهای طلایی فاقد OIDهای شاخهٔ Enterprise اچ‌پی (`11.2.3.9.6.1.1.*`) هستند؛ رفع پس از ضبط ترافیک زنده روی سرور مالک.
4. **TD-021 (تعداد اسلات‌های تونر HP Color در snmp.yml):** کشف در گام H.3؛ ماژول `printer_std` فقط ۲ اسلات تونر دارد و `printer_hp` اسلات تونر ندارد؛ رفع: گسترش `generator.yml` به ۴ اسلات قبل از روشن‌کردن `USE_SNMP_EXPORTER=1` در پروداکشن.
5. **وضعیت TD-012 (تست‌های SQLite-only):** بازبینی شد؛ ۵۲ تست پایه‌ای همچنان روی SQLite ایزوله هستند و تست‌های جدید H.3 نیز با ایزولاسیون hermetic SQLite کار می‌کنند؛ وضعیت این بدهی بدون تغییر باز می‌ماند.
6. **وضعیت State مکانیسم Backoff در حافظه:** ذخیرهٔ `_states` در حافظهٔ زندهٔ کلاینت (نه دیتابیس) باقی می‌ماند تا هیچ تغییری در اسکیمای دیتابیس داده نشود؛ پاک‌شدن وضعیت با ریستارت سرویس طبق تصمیم H.2 رفتار پذیرفته‌شده است.

### سنجه‌های نهایی فاز H
- **مجموعه تست‌ها:** ۷۸۵ تست سبز در هر دو منطقه زمانی `TZ=Asia/Tehran` و `TZ=UTC` بدون هیچ شکست و بدون skip ناخواسته (۷۴۱ پایه + ۱۶ در H.1 + ۱۶ در H.2 + ۱۲ در H.3).
- **فیکسچرهای طلایی:** ۱۰ از ۱۰ فیکسچر تفاضل صفر در `record_golden.py`.
- **گیت‌های چهارگانه:** `build_snmp_yml.py`، `build_oid_matrix.py`، `check_oid_coverage.py` و `record_golden.py` همگی با کد خروج صفر.
- **تغییرات کد Production:** در گام H.4 دقیقاً صفر تغییر (صرفاً تکمیل اسناد و بدهی‌های فنی).
