# 🖨️ Multi‑Brand Printer Monitor

سامانه‌ی پایش و مانیتورینگ پرینترهای **Toshiba، HP، Canon، Brother** و سنسورهای **ECS100G** با استفاده از **SNMP + HTTP**.

این پروژه برای نمایش وضعیت آنلاین/آفلاین، شمارنده‌های چاپ، تونر، سینی‌ها، رویدادها، گزارش‌گیری و مدیریت دسترسی کاربران طراحی شده است.

---

## ✨ قابلیت‌ها

### 🖨️ پشتیبانی از چند برند
- **Toshiba e‑STUDIO**
- **HP LaserJet / JetDirect**
- **Canon MF / LBP**
- **Brother MFC / NC series**
- **ECS100G** برای دما/رطوبت

### 📊 مانیتورینگ دستگاه
- تشخیص **آنلاین / آفلاین**
- نمایش **مدل، سریال، firmware، uptime**
- شمارنده‌های چاپ:
  - کل
  - رنگی
  - سیاه‌وسفید
  - کپی
  - پرینت
  - فکس
  - اسکن
- نمایش **سینی‌های کاغذ** و سطح آن‌ها
- نمایش **تونر / کارتریج / درام** در قالب کارت‌های یکپارچه
- نمایش **هشدارهای فعال**

### 🎨 تونر و مواد مصرفی
- خواندن سطح تونر از **SNMP** و در برخی برندها **HTTP scraping**
- پشتیبانی از **manual toner reset**
- محاسبه **pages since last reset**
- یادگیری خودکار **yield_per_page** با Yield Engine جدید
- محاسبه **per-cartridge / per-color** برای تونرهای مشکی و رنگی
- پشتیبانی از پرینترهای کم‌مصرف با روش **anchor-based learning**
- سطح اعتماد محاسبات: `low` / `medium` / `high`
- استفاده از **کاتالوگ محلی کارتریج‌ها** (`cartridge_yield_catalog.json`) به‌جای fallback خام ۲۰۰۰ صفحه
- استفاده از ظرفیت اعلام‌شده توسط خود پرینتر (`device_capacity`) قبل از fallback پیش‌فرض
- اشتراک yield معتبر بین کارتریج‌های **هم‌مدل و هم‌نام**
- نگهداری history برای **toner snapshots** و نمونه‌های یادگیری
- نمایش **Dot Count / Mega Dots** در مدل‌هایی که واقعاً داده ارائه می‌کنند

### 📜 رویدادها و لاگ‌ها
- ثبت خودکار رویدادهای:
  - `PRINT`
  - `STATUS`
  - `ALERT`
  - `REFILL`
  - `SERVICE`
  - `SENSOR_CHANGE` برای تغییر معنی‌دار سنسورها: دما حداقل ۱°C و رطوبت حداقل ۵٪
- ثبت `paper_size` برای مدل‌های پشتیبانی‌شده (به‌خصوص Toshiba)
- ثبت `poll_timestamp` در رویدادهای `PRINT`
- جلوگیری از ثبت اشتباه در اولین poll یا هنگام reset شمارنده

### 📈 گزارش و خروجی
- نمودار مصرف روزانه
- خروجی **Excel**
- خروجی **CSV**
- خروجی **JSON**
- شیت اختصاصی **Reset History** در Excel

### 👤 کاربران و امنیت
- احراز هویت با **username/password**
- پشتیبانی از **Google OAuth**
- نقش‌ها:
  - `admin`
  - `manager`
  - `viewer`
- محدودسازی بر اساس:
  - **دفاتر مجاز** (`allowed_offices`)
  - **ماژول‌های مجاز** (`allowed_modules`)
- **CSRF protection**
- **rate limiting**
- **security audit log**

### 🧩 رابط کاربری
- تم **دارک / لایت**
- چیدمان responsive
- Drag & Drop برای مرتب‌سازی کارت‌ها
- Tooltip برای کانترهای مبهم
- Toast notification
- آکاردئون برای شمارنده‌های تکمیلی

---

## 🧱 معماری کلی پروژه

- **Flask** برای backend و API
- **SQLite** برای ذخیره‌ی لاگ‌ها، کاربران، شمارنده‌ها و snapshotها
- **SNMP v1 / v2c** با پیاده‌سازی داخلی
- Collectorهای اختصاصی برای برندها
- Frontend ساده با HTML/CSS/JS

---

## 📁 ساختار پروژه

```text
Multi-Printer/
├── run.py
├── requirements.txt
├── create_admin.py
├── printers.example.json
├── config/
│   ├── __init__.py
│   └── settings.py
├── core/
│   ├── database.py
│   ├── poller.py
│   ├── store.py
│   ├── enhanced_collector.py
│   ├── device_classifier.py
│   ├── security_audit.py
│   ├── collectors/
│   │   ├── base.py
│   │   ├── base_enhanced.py
│   │   ├── toshiba.py
│   │   ├── hp.py
│   │   ├── canon.py
│   │   ├── brother.py
│   │   └── sensor.py
│   ├── snmp/
│   │   ├── protocol.py
│   │   └── oid_map.py
│   └── oid/
│       ├── scanner.py
│       ├── catalog.py
│       └── validator.py
├── web/
│   ├── __init__.py
│   ├── auth.py
│   ├── security.py
│   ├── routes/
│   │   ├── dashboard.py
│   │   ├── printers.py
│   │   ├── logs.py
│   │   ├── export_bp.py
│   │   ├── discover.py
│   │   ├── scan.py
│   │   ├── stats.py
│   │   ├── system.py
│   │   ├── security.py
│   │   ├── users.py
│   │   └── validation.py
│   ├── templates/
│   │   ├── base.html
│   │   ├── dashboard.html
│   │   ├── login.html
│   │   ├── register.html
│   │   ├── forgot_password.html
│   │   ├── reset_password.html
│   │   ├── pending_verification.html
│   │   ├── users.html
│   │   └── security.html
│   └── static/
│       ├── css/
│       │   └── style.css
│       ├── js/
│       │   ├── dashboard.js
│       │   ├── legacy-mode.js
│       │   ├── Sortable.min.js
│       │   └── chart.umd.min.js
│       └── favicon.ico
└── README.md
```

### 📝 توضیح فایل‌ها و پوشه‌ها

#### ریشه پروژه
- **`run.py`**: نقطه ورود اصلی برنامه؛ Flask را بالا می‌آورد، دیتابیس را init می‌کند، threadهای polling و scan را اجرا می‌کند.
- **`requirements.txt`**: فهرست وابستگی‌های پایتون پروژه.
- **`create_admin.py`**: اسکریپت کمکی برای ساخت یا ارتقای کاربر ادمین از طریق خط فرمان.
- **`printers.example.json`**: نمونه فرمت فایل پرینترها برای راه‌اندازی اولیه یا مستندسازی.
- **`README.md`**: مستندات اصلی پروژه، نصب، اجرا، APIها و ساختار کلی.

#### پوشه `config/`
- **`config/`**: محل تنظیمات سراسری پروژه.
- **`config/__init__.py`**: فایل package برای import کردن تنظیمات.
- **`config/settings.py`**: تنظیمات اصلی مثل پورت، فایل‌ها، subnet دفاتر، secret key، CORS و version assetها.

#### پوشه `core/`
- **`core/`**: منطق اصلی backend، polling، SNMP، database و collectorها.
- **`core/database.py`**: همه عملیات SQLite؛ شامل لاگ‌ها، کاربران، printer counters، toner history و reset history.
- **`core/poller.py`**: منطق poll کردن دستگاه‌ها، اجرای cycleها و fallback بین collectorها.
- **`core/store.py`**: حافظه سراسری برنامه (`printer_data`, `PRINTERS`, `poll_stats`) و کلاس `PrevStore` برای نگهداری snapshot قبلی.
- **`core/enhanced_collector.py`**: collector پیشرفته و مسیر اصلی جمع‌آوری داده برای همه برندها؛ شامل supplies، toner، tray، counters و OID profile.
- **`core/device_classifier.py`**: تشخیص نوع دستگاه یا کمک به طبقه‌بندی آن‌ها.
- **`core/security_audit.py`**: ثبت و بازیابی رویدادهای امنیتی مثل login failure، logout و فعالیت‌های مشکوک.

#### پوشه `core/collectors/`
- **`core/collectors/`**: collectorهای اختصاصی هر برند و توابع مشترک آن‌ها.
- **`core/collectors/base.py`**: توابع پایه مثل تبدیل امن داده، ثبت رویداد `PRINT/ALERT`، yield learning و محاسبات تونر.
- **`core/collectors/base_enhanced.py`**: bridge ساده برای import کردن `collect_enhanced` از مسیر پایدار.
- **`core/collectors/toshiba.py`**: collector اختصاصی Toshiba با OIDهای vendor-specific، paper size، twin، toner scrape و trayها.
- **`core/collectors/hp.py`**: collector اختصاصی HP و fallback برای counter/toner.
- **`core/collectors/canon.py`**: collector اختصاصی Canon با پشتیبانی از مدل‌های MF و LBP.
- **`core/collectors/brother.py`**: collector اختصاصی Brother و fallback برای toner/drum.
- **`core/collectors/sensor.py`**: collector سنسورهای ECS100G برای دما و رطوبت.

#### پوشه `core/snmp/`
- **`core/snmp/`**: لایه SNMP سفارشی پروژه.
- **`core/snmp/protocol.py`**: پیاده‌سازی low-level SNMP v1/v2c، parse پاسخ، cache نسخه SNMP و fallback بین نسخه‌ها.
- **`core/snmp/oid_map.py`**: OIDهای اختصاصی به‌خصوص برای Toshiba و بعضی نگاشت‌های مربوط به کاغذ/تونر.

#### پوشه `core/oid/`
- **`core/oid/`**: ابزارهای scan و اعتبارسنجی OIDها.
- **`core/oid/scanner.py`**: اسکن OIDهای دستگاه‌ها و ساخت profile برای هر IP.
- **`core/oid/catalog.py`**: کاتالوگ و metadata مربوط به OIDهای شناخته‌شده.
- **`core/oid/validator.py`**: اعتبارسنجی مقادیر OIDها و بررسی سلامت داده‌ها.

#### پوشه `web/`
- **`web/`**: لایه Flask و رابط وب پروژه.
- **`web/__init__.py`**: ساخت app Flask، ثبت blueprintها، middlewareهای دسترسی و CORS.
- **`web/auth.py`**: احراز هویت، نقش‌ها، login/register، reset password و helperهای دسترسی کاربران.
- **`web/security.py`**: CSRF، rate limiting و security headers.

#### پوشه `web/routes/`
- **`web/routes/`**: endpointها و routeهای Flask.
- **`web/routes/dashboard.py`**: route صفحه اصلی داشبورد.
- **`web/routes/printers.py`**: APIهای مربوط به پرینترها؛ افزودن، حذف، rename، toner reset و debug.
- **`web/routes/logs.py`**: APIهای لاگ‌ها، دریافت رویدادها، پاک‌سازی و ثبت رویدادهای دستی.
- **`web/routes/export_bp.py`**: خروجی Excel/CSV و ساخت شیت‌های گزارش.
- **`web/routes/discover.py`**: کشف شبکه‌ای دستگاه‌ها با SNMP.
- **`web/routes/scan.py`**: routeهای اسکن OID و profileها.
- **`web/routes/stats.py`**: API آمار روزانه و داده‌های نمودارها.
- **`web/routes/system.py`**: وضعیت سیستم و اجرای Pull دستی.
- **`web/routes/security.py`**: صفحه امنیت و APIهای security events/stats.
- **`web/routes/users.py`**: مدیریت کاربران، نقش‌ها، تأیید حساب و access control.
- **`web/routes/validation.py`**: بررسی سازگاری counterها و اعتبارسنجی profileهای OID.

#### پوشه `web/templates/`
- **`web/templates/`**: قالب‌های HTML سمت سرور.
- **`web/templates/base.html`**: قالب پایه، assetها، CSRF meta، و scriptهای سراسری.
- **`web/templates/dashboard.html`**: صفحه اصلی داشبورد و containerهای اصلی UI.
- **`web/templates/login.html`**: فرم ورود کاربر.
- **`web/templates/register.html`**: فرم ثبت‌نام.
- **`web/templates/forgot_password.html`**: درخواست لینک بازنشانی رمز.
- **`web/templates/reset_password.html`**: فرم تعیین رمز جدید.
- **`web/templates/pending_verification.html`**: صفحه انتظار برای تأیید ادمین.
- **`web/templates/users.html`**: صفحه مدیریت کاربران و مودال‌های مربوطه.
- **`web/templates/security.html`**: صفحه داشبورد امنیت و گزارش رویدادهای امنیتی.

#### پوشه `web/static/`
- **`web/static/`**: فایل‌های استاتیک frontend.
- **`web/static/css/`**: استایل‌های پروژه.
- **`web/static/css/style.css`**: فایل اصلی استایل‌ها برای داشبورد، مودال‌ها، جدول‌ها و تم‌ها.
- **`web/static/js/`**: اسکریپت‌های frontend.
- **`web/static/js/dashboard.js`**: منطق اصلی frontend؛ fetch داده، render کارت‌ها، نمودارها، مودال‌ها، رویدادها و اکشن‌ها.
- **`web/static/js/legacy-mode.js`**: اسکریپت مربوط به حالت نمایش قدیمی یا featureهای legacy.
- **`web/static/js/Sortable.min.js`**: کتابخانه Drag & Drop برای مرتب‌سازی کارت‌ها.
- **`web/static/js/chart.umd.min.js`**: کتابخانه Chart.js برای نمودارها.
- **`web/static/favicon.ico`**: آیکون مرورگر / تب سایت.

---

## 🔧 پیش‌نیازها

- Python **3.10+**
- دسترسی شبکه به پرینترها
- باز بودن پورت **161/UDP** برای SNMP
- برای برخی مدل‌ها، فعال بودن SNMP **v1** یا **v2c**
- برای خروجی Excel:
  - `openpyxl`

---

## 🚀 نصب و اجرا

### 1) کلون پروژه
```bash
git clone https://github.com/mh-shahryari/Multi-Printer.git
cd Multi-Printer
```

### 2) ساخت محیط مجازی
```bash
python -m venv .venv
```

### 3) فعال‌سازی محیط مجازی
**Linux / macOS**
```bash
source .venv/bin/activate
```

**Windows**
```bat
.venv\Scripts\activate
```

### 4) نصب وابستگی‌ها
```bash
pip install -r requirements.txt
```

### 5) اجرای برنامه
```bash
python run.py
```

به‌صورت پیش‌فرض:
- آدرس محلی: `http://localhost:5053`

---

## ⚙️ تنظیمات مهم محیطی

### امنیت و اجرا
```bash
export ENVIRONMENT=development
export SECRET_KEY=change-me
export FLASK_PORT=5053
```

### پرینترهای پیش‌فرض (اختیاری)
به‌جای hardcode شدن IPها، می‌توانید لیست پیش‌فرض را از ENV بدهید:

```bash
export DEFAULT_PRINTERS_JSON='[
  {"ip":"192.168.1.10","name":"Printer #1","community":"public"}
]'
```

### subnet دفاتر
```bash
export OFFICE_SUBNET_IMAMAT=172.16.25
export OFFICE_SUBNET_SOROUSH=172.16.24
export OFFICE_SUBNET_FALESTIN=172.16.0
export OFFICE_SUBNET_ELAHIYE=172.16.32
```

### نسخه assetها برای cache busting
```bash
export ASSET_VERSION=20260609-2
```

### CORS (در صورت نیاز)
```bash
export CORS_ALLOWED_ORIGINS="https://app.example.com"
```

---

## 👤 ساخت ادمین اولیه

اگر بخواهید ادمین دستی بسازید:

```bash
python create_admin.py
```

نکته:
- اگر هیچ کاربری وجود نداشته باشد، **اولین کاربر ثبت‌نام‌شده** به‌صورت خودکار `admin` و `verified` می‌شود.

---

## 📦 فایل‌های runtime / generated

این فایل‌ها در زمان اجرا ساخته می‌شوند و نباید به‌عنوان source اصلی در git مدیریت شوند:

- `logs.db`
- `printers.json`
- `oid_profiles.json`
- `oid_validation_errors.txt`
- `toner_report.txt`
- `missing_yield_printers.txt` (legacy)
- `yield_status_report.txt`

برای نمونه لیست پرینترها از فایل زیر استفاده کنید:
- `printers.example.json`

---

## 🔌 APIهای مهم

### وضعیت سیستم
- `GET /api/status`
- `POST /api/poll/now`

### پرینترها
- `GET /api/printers`
- `GET /api/printer/<ip>`
- `POST /api/printers/add`
- `POST /api/printers/bulk-add`
- `POST /api/printers/remove`
- `POST /api/printer/<ip>/rename`
- `POST /api/printer/<ip>/toner_reset`

### لاگ‌ها
- `GET /api/logs/all`
- `GET /api/printer/<ip>/log`
- `POST /api/logs/clear`
- `POST /api/events/manual`

### Import دیتابیس
- `POST /api/import/analyze`
- `POST /api/import/confirm`

Import دیتابیس علاوه بر لاگ‌ها، می‌تواند `printer_counters`، `toner_history`، `sensor_readings` و جدول‌های Yield Engine را هم وارد کند. UI امکان انتخاب بخش‌ها، فیلتر IP/تاریخ، مدیریت رکوردهای تکراری و گرفتن backup قبل از import را دارد.

### خروجی‌ها
- `GET /api/export/excel`
- `GET /api/export/logs?format=csv`
- `GET /api/export/logs?format=excel`

### اسکن و اعتبارسنجی
- `POST /api/scan/oids`
- `GET /api/scan/oids/<ip>`
- `POST /api/scan/all`
- `GET /api/validate/counters`
- `GET /api/validate/oids/<ip>`

### Yield Engine
- `GET /api/yield/status`
- `POST /api/yield/report`

### کاتالوگ ظرفیت کارتریج
فایل `cartridge_yield_catalog.json` برای ظرفیت اسمی کارتریج‌ها استفاده می‌شود تا اگر هنوز یادگیری خودکار یا ظرفیت اعلامی دستگاه وجود ندارد، مقدار بهتری از fallback خام `2000` نمایش داده شود.

اولویت کلی:
1. `auto_learn`
2. `shared_profile`
3. `device_capacity`
4. `catalog`
5. `default`

برای کاتالوگ اختصاصی می‌توان مسیر را با ENV زیر تغییر داد:
```bash
export CARTRIDGE_YIELD_CATALOG=/path/to/cartridge_yield_catalog.json
```

برای شرایط بازار ایران/کارتریج شارژی یا طرح، کاتالوگ با مقادیر محافظه‌کارانه `local` تنظیم شده است. حالت پیش‌فرض Yield Engine هم `local` است؛ یعنی بدون تنظیم ENV هم به‌جای مقدار OEM از مقدار مناسب‌تر برای ترکیب «طرح و شارژی» استفاده می‌شود.

در صورت نیاز می‌توان mode را تغییر داد:
```bash
# Windows CMD:
set CARTRIDGE_YIELD_MODE=local       # ترکیبی/ایران - پیش‌فرض پیشنهادی
set CARTRIDGE_YIELD_MODE=compatible  # کارتریج طرح
set CARTRIDGE_YIELD_MODE=refill      # کارتریج شارژی
set CARTRIDGE_YIELD_MODE=oem         # اورجینال

# ضریب اضافه اختیاری، مثلاً ۱۰٪ محافظه‌کارانه‌تر:
set CARTRIDGE_YIELD_FACTOR=0.90

# Linux/macOS:
export CARTRIDGE_YIELD_MODE=local
export CARTRIDGE_YIELD_FACTOR=0.90
```

همچنین داخل هر entry کاتالوگ می‌توان علاوه بر مقدار اسمی، مقادیر محلی گذاشت:
```json
{
  "yield_per_page_oem": 9000,
  "yield_per_page_compatible": 6500,
  "yield_per_page_refill": 5500,
  "yield_per_page_local": 6000
}
```

نکته: مقدار کاتالوگ فقط fallback است و بعد از جمع شدن داده واقعی، `auto_learn` جایگزین آن می‌شود.

### کاربران و امنیت
- `GET /users`
- `GET /api/users`
- `POST /api/users`
- `POST /api/users/<id>/role`
- `POST /api/users/<id>/verify`
- `POST /api/users/<id>/access`
- `DELETE /api/users/<id>`
- `GET /security`
- `GET /api/security/events`
- `GET /api/security/stats`

---

## 📊 نکات مربوط به تونر و مواد مصرفی

### Brother
در برخی مدل‌های Brother:
- دستگاه وجود تونر را گزارش می‌کند
- اما **سطح باقی‌مانده تونر** را از طریق SNMP ارائه نمی‌دهد

در این حالت:
- UI مقدار `—` نشان می‌دهد
- progress bar خالی باقی می‌ماند
- مقدار ساختگی تولید نمی‌شود

### Toshiba
برای Toshiba:
- کاغذ A3/A4 و بعضی شمارنده‌های vendor-specific پشتیبانی می‌شود
- `paper_size` در رویدادهای PRINT ثبت می‌شود
- `pages_since_last_reset` پشتیبانی می‌شود

---

## 📈 خروجی Excel

فایل Excel شامل شیت‌های زیر است:
- `Printer Status`
- `Job Log`
- `Reset History`

### Reset History
این شیت شامل:
- زمان تنظیم مجدد کارتریج
- IP و نام دستگاه
- رنگ کارتریج
- درصد تنظیم‌شده
- total pages در لحظه reset
- صفحات چاپ‌شده بعد از reset
- مقدار `yield_per_page`

---

## 🧪 مسیرهای debug / توسعه

برخی endpointها برای دیباگ داخلی هستند و بهتر است فقط در محیط توسعه استفاده شوند:

- `GET /api/debug/printer/<ip>`
- `GET /api/debug/brother-toner/<ip>`

این endpoint دوم برای بررسی raw OIDهای Brother اضافه شده و در آینده می‌تواند حذف شود.

---

## ⚠️ نکات مهم اجرایی

- برای درخواست‌های `POST/PUT/DELETE` از CSRF استفاده می‌شود.
- در frontend باید درخواست‌های state-changing با `apiFetch(...)` ارسال شوند.
- assetها با `ASSET_VERSION` versioned می‌شوند تا مشکل cache مرورگر کمتر شود.

---

## 🛠️ توسعه و نگهداری

اگر قصد توسعه دارید، پیشنهاد می‌شود این بخش‌ها را جداگانه بررسی کنید:
- Collectorهای برندها
- منطق `PrevStore`
- لاگ‌ها و reset history
- محدودسازی دسترسی کاربران
- export و reporting

---

## 📄 مجوز / License

در صورت نیاز، این بخش را با مجوز مدنظر پروژه تکمیل کنید.
