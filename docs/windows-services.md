# Windows services — `snmp_exporter` with NSSM (phase G · step G.4)

> **مبنا:** اعداد و عبارت‌های این صفحه از خروجیِ همان session ای است که
> `windows/install-snmp-exporter.ps1` نوشته شد (گام‌های G.4.1–G.4.3 روی `phase-c`).
> هیچ عددی از پیش فرض نشده و **هیچ عددِ زمانی (مدت/تخمین) در این سند نیست**.
> اسکریپت در sandbox توسعه **اجرا نشده است** — ویندوز نیست و دانلود باینری ممنوع بود؛
> آنچه سنجیده شد ساختار/متن است (§۶).

---

## 1. Why NSSM (and not `sc.exe` or Task Scheduler)

| نیازِ این فاز | `sc.exe create` | Task Scheduler | NSSM |
|---|---|---|---|
| اجرا در پس‌زمینه، بدونِ لاگینِ کاربر | ✔ | ✔ | ✔ |
| چرخشِ خودکارِ لاگ (ویندوز ۹۱۱۶ زیاد حرف می‌زند) | ✘ | ✘ | ✔ `AppRotateFiles` / `AppRotateBytes` |
| نگه‌داشتنِ stdout/stderr پروسه در فایل | ✘ (نیاز به wrapper) | محدود | ✔ `AppStdout` / `AppStderr` |
| شروعِ خودکار بعد از ری‌استارت + تنظیمِ دقیق | محدود | ✔ (با شرطِ لاگین) | ✔ `Start SERVICE_AUTO_START` |
| environment برای v3 (`SNMP_V3_*`) بدونِ عبورِ رمز از argv | ✘ | دستی | ✔ کلیدِ registry `AppEnvironmentExtra` |
| idempotent‌بودنِ «نصب/به‌روزرسانی» با یک اسکریپت | دستی | دستی | ✔ `nssm set`های جدا، بدونِ reinstall |

ویندوزِ همین مخزن برای *اپلیکیشن* از Task Scheduler استفاده می‌کند
(`PrintGuardMonitor`، `tools/install-startup-task.bat`)؛ exporter یک سرویسِ مستقلِ
معمولی است و NSSM استانداردِ بی‌دردسرِ همان کار است. باینریِ NSSM commit نمی‌شود؛
اسکریپت در صورت نبود، آن را از منبعِ pin‌شده می‌گیرد (§۲).

---

## 2. Prerequisites

- **PowerShell 5.1 یا بالاتر** و **اجرای Elevated**: اسکریپت اولِ کار با
  `[Security.Principal.WindowsPrincipal]::IsInRole(Administrator)` چک می‌کند؛ اگر
  ادمین نباشید `exit 2` با پیامِ خودِ اسکریپت می‌دهد (از `#Requires -RunAsAdministrator`
  عمداً استفاده نشده تا پیام، راهنما داشته باشد).
- **ویندوز ۶۴بیتی** — داراییِ منتشرشده `windows-amd64` است؛ روی ۳۲بیتی `exit 2` می‌گیرد.
- **دسترسی ساختِ مسیرها**: `C:\PrintGuard\snmp-exporter`، `C:\PrintGuard\data`،
  `C:\PrintGuard\logs`.
- **`monitoring/snmp.yml` در مخزن** — اسکریپت آن را از کنارِ خودش (`$PSScriptRoot\..`)
  برمی‌دارد. نبودش = `exit 2` با یادآوریِ `python tools\build_snmp_yml.py`.
- شبکه برای دو منبعِ pin‌شده (فقط این دو؛ هر منبعِ دیگری در فایل خطای تست است):
  `github.com` (snmp_exporter) و `nssm.cc` (NSSM). اگر سرور آفلاین است، هر دو فایل را
  دستی در `C:\PrintGuard\snmp-exporter` بگذارید: `snmp_exporter.exe` و `nssm.exe` —
  اسکریپت در این حالت چیزی دانلود نمی‌کند.
- **اختیاری:** `-ExpectedSha256 <hash>` برای تطبیقِ checksumِ zipِ دانلودی. اگر
  ندهید، hash فقط چاپ و در لاگ ثبت می‌شود تا با صفحهٔ انتشار مقایسه‌اش کنید.

نسخه‌های pin‌شده (تغییر فقط با کامیت):

| کامپوننت | نسخه |
|---|---|
| `snmp_exporter` | `0.26.0` |
| `nssm` | `2.24` |

---

## 3. Running

```powershell
cd C:\PrintGuard\multi-printer
powershell -ExecutionPolicy Bypass -File windows\install-snmp-exporter.ps1
```

حالت‌های شبکه — **یک سوئیچ، دو متغیر**، تا bind address و فایروال هرگز از هم جدا نیفتند:

| حالت | `$ListenAddress` | `$FirewallRemote` |
|---|---|---|
| پیش‌فرض (امن) | `127.0.0.1:9116` | `127.0.0.1,::1` |
| `-AllowSubnetAccess` | `0.0.0.0:9116` | `LocalSubnet` |

```powershell
# فقط اگر Prometheus روی ماشین دیگری است:
powershell -ExecutionPolicy Bypass -File windows\install-snmp-exporter.ps1 -AllowSubnetAccess
```

**دلیلِ پیش‌فرضِ localhost:** مصرف‌کنندهٔ این exporter در فاز G،
`core/exporter_client.py` است که **روی همان ماشین** اجرا می‌شود؛ و `snmp_exporter`
هیچ احرازِ هویتی ندارد. پس `-AllowSubnetAccess` یعنی «هرکس در subnet می‌تواند
وضعیتِ پرینترهای ما را بخواند و exporter را به‌عنوان بازتاب‌دهندهٔ SNMP به دستگاه‌ها
بدانگیزاند». اسکریپت هم در همین حالت یک `WARN` چاپ می‌کند. اگر به دسترسِ LAN نیاز
شد، راهِ درست‌ترِ میانی: Prometheus روی همین ماشین + reverse-proxy با auth بیرون از
این فاز.

**خروجیِ اسکریپت:** هر خط هم روی کنسول و هم در
`C:\PrintGuard\logs\install-snmp-exporter.log` (با timestamp).

| `exit` | معنی |
|---|---|
| ۰ | نصب/به‌روزرسانی موفق، سرویس Running، پاسخ ۲۰۰ از `http://localhost:9116/` |
| ۱ | خطای قابل‌توجه: دانلود/extract رد شد، سرویس Running نشد، یا پروبِ HTTP جواب نداد |
| ۲ | نیازمندِ اقدامِ دستی: ادمین نیستید، ویندوز ۳۲بیتی، `monitoring/snmp.yml` نیست، یا در archive فایلِ هدف پیدا نشد |

**idempotent بودن (چیزی که دوباره‌اجرا را بی‌خطر می‌کند):** چهار مسیرِ «از قبل درست است
⇒ رد می‌شود» در اسکریپت هست — باینری (مقایسهٔ `--version` با پین)، `nssm.exe` (و اگر
در PATH باشد کپی‌اش می‌کند)، کپی‌کردنِ `snmp.yml` (مقایسهٔ sha256)، و قاعدهٔ فایروال
(مقایسهٔ `RemoteAddress` و `LocalPort`). اگر سرویس هست، `install` دوباره صدا زده
نمی‌شود: اول `stop`، بعد `nssm set`ها، بعد `start`.

تنظیماتِ سرویس که اسکریپت می‌نویسد (معادلِ دستی، برای عیب‌یابی):

```
nssm install printguard-snmp-exporter C:\PrintGuard\snmp-exporter\snmp_exporter.exe
nssm set printguard-snmp-exporter AppParameters "--config.file=C:\PrintGuard\snmp-exporter\snmp.yml --web.listen-address=127.0.0.1:9116 --config.expand-env"
nssm set printguard-snmp-exporter AppDirectory C:\PrintGuard\snmp-exporter
nssm set printguard-snmp-exporter AppStdout C:\PrintGuard\logs\snmp-exporter.log
nssm set printguard-snmp-exporter AppStderr C:\PrintGuard\logs\snmp-exporter.err.log
nssm set printguard-snmp-exporter AppRotateFiles 1
nssm set printguard-snmp-exporter AppRotateBytes 10485760
nssm set printguard-snmp-exporter Start SERVICE_AUTO_START
nssm start printguard-snmp-exporter
```

`--config.expand-env` شرطِ لازمِ `secure_v3` است: بدونِ آن، `${SNMP_V3_*}`ها در
`snmp.yml` رشته‌های خام می‌مانند. رمزها هرگز از خطِ فرمان نمی‌گذرند (در فهرستِ
فرآیندها دیده می‌شوند): اگر `SNMP_V3_USERNAME` / `SNMP_V3_AUTH_PASSPHRASE` /
`SNMP_V3_PRIV_PASSPHRASE` در محیطِ پروسهٔ ادمین باشند، اسکریپت آن‌ها را به‌صورت
`AppEnvironmentExtra` (MultiString) در کلیدِ registryِ سرویس می‌نویسد و در لاگ **فقط
نامِ** متغیرها را چاپ می‌کند. اگر نباشند، یک `WARN` می‌بینید و `secure_v3` تا
تنظیم‌شدن استفاده‌ناپذیر می‌ماند (سیاستِ فاز: v3-first در انتخابِ دستگاه، نه اینجا).

---

## 4. Post-installation checklist

```powershell
# ۱) سرویس Running و autostart است؟
Get-Service printguard-snmp-exporter | Format-List Name,Status,StartType

# ۲) پورت باز است؟ (روی ویندوز curl.exe صدا کنید، نه aliasِ Invoke-WebRequest)
curl.exe -s -o NUL -w "%{http_code}" http://localhost:9116/   # انتظار: 200

# ۳) قاعدهٔ فایروال و دامنه‌اش
Get-NetFirewallRule -DisplayName "PrintGuard SNMP Exporter" |
    Get-NetFirewallAddressFilter | Select-Object RemoteAddress
Get-NetFirewallRule -DisplayName "PrintGuard SNMP Exporter" |
    Get-NetFirewallPortFilter | Select-Object LocalPort,Protocol

# ۴) یک scrape واقعی روی پرینتر (سنجهٔ اصلیِ G.3/§۵ سندِ setup)
curl.exe -s "http://localhost:9116/snmp?target=<ip>&module=printer_hp&auth=public_v2"

# ۵) لاگ و چرخشش
Get-Content C:\PrintGuard\logs\snmp-exporter.log -Tail 20
nssm get printguard-snmp-exporter AppRotateBytes

# ۶) configِ سرویس همان فایلِ مخزن است؟ (sha256ِ دو فایل باید یکی باشد)
(Get-FileHash C:\PrintGuard\snmp-exporter\snmp.yml).Hash
(Get-FileHash .\monitoring\snmp.yml).Hash
```

چه چیزی در بند ۴ باید دیده شود: متریک‌های همان module (برای `printer_hp`:
`hp_model`, `hp_serial`, `hp_firmware`, `general_oids`, `hp_print_color`,
`hp_npcl_pct_oid`, …)، self-metricsِ `snmp_scrape_duration_seconds`,
`snmp_scrape_walk_duration_seconds`, `snmp_scrape_packets_sent`,
`snmp_scrape_packets_retried`, `snmp_scrape_pdus_returned`، و **نبودِ** `snmp_error`.
متریک‌های متنی مقدارِ `1` با لیبلِ هم‌نامِ متریک می‌دهند (رفتارِ collector، نه باگ).
`snmp_scrape_packets_sent` همان عددی است که در G.6 با `tools/compare_snmp_sources.py`
سنجیده می‌شود؛ تا آن‌جا هیچ ادعایی دربارهٔ «کاهش بسته» در این مخزن نیست.

---

## 5. Removal / update / rollback

```powershell
# توقفِ موقت (poller دست‌نخورده می‌ماند؛ دلیلش پایینِ همین بند)
nssm stop printguard-snmp-exporter

# حذفِ کاملِ سرویس (فایل‌ها می‌مانند)
nssm remove printguard-snmp-exporter confirm
Remove-NetFirewallRule -DisplayName "PrintGuard SNMP Exporter"

# به‌روزرسانیِ نسخه: فقط پین را عوض کنید و دوباره اجرا کنید
#   windows\install-snmp-exporter.ps1 ⇒ $SNMP_EXPORTER_VERSION = "0.27.0"
git checkout phase-c -- windows/install-snmp-exporter.ps1   # اگر خواستید برگردید
powershell -ExecutionPolicy Bypass -File windows\install-snmp-exporter.ps1
```

اسکریپت در مسیرِ به‌روزرسانی، سرویس را `stop` می‌کند، باینری را جایگزین، تنظیمات را
بازنویسی و دوباره `start` می‌کند؛ اگر فایلِ `.exe` به‌دلیلِ قفل‌بودن بازنویسی نشود،
`exit 1` با لاگِ کامل می‌گیرید (نه نیمه‌کاره‌شدنِ نصب).

**بازگشتِ امن:** تا وقتی `USE_SNMP_EXPORTER` صفر است (پیش‌فرض فاز G)، Poller هرگز
از exporter نمی‌خواند؛ پس نصب/حذفِ این سرویس روی داده‌های پرینترها اثر ندارد. حذفِ
سرویس = برگشتِ کامل. کلید و مسیر مصرف در گام G.5 اضافه می‌شود، نه اینجا.

---

## 6. What was verified and what was **not run**

**سنجیده‌شده** (بدونِ ویندوز، بدونِ pwsh): `tests/test_install_ps1.py` — ۱۳ تست.
لایهٔ اول، parser-check است **اگر** PowerShell روی PATH باشد
(`[System.Management.Automation.Language.Parser]::ParseFile`)؛ در این sandbox
`command -v pwsh powershell` هیچ خروجی‌ای ندارد، پس همان تست لایهٔ دوم را اجرا می‌کند
(تعادلِ آکولاد، زوج‌بودنِ کوتیشن، وجودِ BOM). لایهٔ متنی همیشه اجرا می‌شود: پین‌ها،
URLها و میزبان‌ها، ترتیبِ شاخهٔ سوئیچ (پیش‌فرض = localhost)، array‌بودنِ
`-RemoteAddress`، خطوطِ `nssm set`، چرخشِ لاگ، چهار مسیرِ idempotency، قراردادِ
exit codeها، شکلِ قاعدهٔ فایروال، نبودِ رمز در فایل و در argv، و ساختارهای ممنوعهٔ
PS7. `windows/install-snmp-exporter.ps1` = ۳۶۹ خط / ۱۹٬۸۷۳ بایت، UTF-8 با BOM، LF.
هشت جهش روی فایل (پیش‌فرضِ subnet، چرخشِ لاگِ خاموش، `RemoteAddress` رشته‌ای، حذفِ
`nssm start`, حذفِ BOM، پورتِ ۹۱۱۷، pin‌نشدنِ نسخه، نوشتنِ رمز از argv) هرکدام
سبز→قرمز شدند و بعد فایل بایت‌به‌بایت بازگردانده شد.

**انجام‌نشده (و ادعا نشده):** اجرای واقعیِ اسکریپت؛ نصبِ واقعی روی ویندوز؛ دانلودِ
`snmp_exporter`/NSSM در sandbox؛ شروعِ سرویس؛ تستِ قاعدهٔ فایروال؛ تستِ `-AllowSubnetAccess`
روی LAN؛ و هر سنجشِ کارکردیِ exporter. این‌ها در سرور مالک و طبقِ §۵ سند
`docs/snmp-exporter-setup.md` انجام می‌شود. تستِ واقعی ویندوز در فاز L می‌ماند.

---

## 7. Quick troubleshooting

| نشانه | معنی / کار |
|---|---|
| `exit 2` و «ادمین می‌خواهد» | در PowerShellِ Elevated اجرا کنید (نه فقط `-ExecutionPolicy Bypass`) |
| `exit 2` و «نبودِ … monitoring\snmp.yml» | `python tools\build_snmp_yml.py` و بعد `python tools\check_oid_coverage.py` (باید ۰ شود) |
| سرویس Running است اما `curl` وصل نمی‌شود | احتمالِ حالتِ localhost روی ماشینِ دیگر: §۳؛ روی همان ماشین تست کنید |
| `snmp_exporter` لاگِ «load error» دارد | کلیدی در `snmp.yml` هست که `config/config.go` نمی‌شناسد (`UnmarshalStrict`)؛ خروجیِ §۴.۲ سندِ setup و `tests/test_snmp_yml.py` را ببینید |
| `${SNMP_V3_USERNAME}` خام در پاسخ | `--config.expand-env` نرسیده یا متغیرها در محیطِ پروسهٔ نصب نبودند (§۳) |
| مقدارِ `1` با لیبلِ متنی | رفتارِ درستِ متریک‌های `DisplayString` است (§۴.۴ سندِ setup) |
| `-2`/`-3` در خروجی | sentinel‌های دستگاه؛ فیلترِ آنها در `exporter_adapter` (گام G.5) است، نه در `snmp.yml` — `ignore_values` در این فایل **کامنت** است |
| لاگِ حجیم | `AppRotateBytes 10485760` + `AppRotateFiles 1`؛ تغییرشان با `nssm set` و `nssm restart printguard-snmp-exporter` |
