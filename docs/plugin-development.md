# راهنمای جامع توسعه پلاگین‌های PrintGuard (Plugin Development Guide)

این راهنما مرجع کامل معماری و پیاده‌سازی افزونه‌ها (Plugins) در سامانه پایش و مدیریت ناوگان چاپ **PrintGuard** (فاز J.5 و J.12) می‌باشد. با استفاده از این فریم‌ورک، توسعه‌دهندگان می‌توانند قابلیت‌های جدید، منابع داده، ویجت‌های داشبورد، منوهای ناوبری و مسیرهای اختصاصی وب را بدون تغییر در هسته نرم‌افزار توسعه دهند.

---

## ۱. معماری و اصول ایزولاسیون پلاگین‌ها

فریم‌ورک پلاگین‌های PrintGuard بر پایه اصول زیر طراحی شده است:
1. **عدم وابستگی مخرب (Non-invasive Extension):** هسته برنامه (`core/collectors/`, `core/snmp/`, `core/db/`) تغییر نمی‌کند.
2. **پیکربندی اعلامی (Declarative Manifest):** متادیتا، دسترسی‌ها و ویجت‌های هر پلاگین در `manifest.json` تعریف می‌شوند.
3. **بارگذاری و ایزولاسیون پویا:** پلاگین‌ها در مسیر `plugins/<plugin_id>` قرار گرفته و در زمان اجرا توسط `core.plugins.loader.PluginManager` کشف، بررسی و فعال می‌شوند.
4. **کنترل دسترسی مبتنی بر نقش (RBAC):** امکان فعال‌سازی سازمانی و کنترل دسترسی بر اساس نقش‌های `admin`, `manager`, `viewer`.
5. **جداسازی خطا (Failure Containment):** در صورت بروز خطا در زمان بارگذاری یک پلاگین، کل سیستم متوقف نشده و وضعیت خطا لاگ می‌شود.

---

## ۲. ساختار پوشه پلاگین

هر پلاگین باید در پوشه‌ای مستقل درون شاخه `plugins/` با ساختار استاندارد زیر قرار گیرد:

```
plugins/
└── attendance/                   # شناسه یکتای پلاگین (plugin_id)
    ├── manifest.json             # مانیفست و تعاریف متادیتا، ویجت‌ها و منوها
    ├── __init__.py               # نقطه ورود ماژول پایتون
    ├── plugin.py                 # کلاس اصلی مشتق‌شده از BasePlugin
    ├── routes.py                 # (اختیاری) Blueprint اختصاصی Flask
    ├── static/                   # فایل‌های استاتیک اختصاصی (JS, CSS, Images)
    │   ├── js/
    │   └── css/
    └── templates/                # قالب‌های Jinja2 اختصاصی
        └── attendance/
            └── index.html
```

---

## ۳. مشخصات و نمونه کامل `manifest.json`

فایل `manifest.json` در ریشه پوشه پلاگین قرار می‌گیرد و شناسنامه آن محسوب می‌شود:

```json
{
  "id": "attendance",
  "name": "حضور و غیاب پرسنل",
  "version": "1.0.0",
  "description": "پلاگین ثبت تردد و پایش مصرف کاغذ به تفکیک پرسنل و شیفت‌های کاری",
  "author": "PrintGuard Core Team",
  "min_app_version": "2.5.0",
  "enabled": true,
  "entrypoint": "plugin:AttendancePlugin",
  "permissions": [
    "db:read",
    "dashboard:widget",
    "menu:navigation"
  ],
  "roles_allowed": ["admin", "manager"],
  "menus": [
    {
      "id": "attendance-menu",
      "title": "تردد و کارکرد پرسنل",
      "url": "/plugins/attendance",
      "icon": "👥",
      "order": 40
    }
  ],
  "widgets": [
    {
      "id": "attendance_summary",
      "name": "خلاصه تردد و چاپ پرسنل",
      "description": "نمایش آمار پرسنل حاضر و حجم پرینت امروز",
      "category": "سازمانی",
      "default_width": 6,
      "default_height": 4,
      "schema": {
        "type": "object",
        "properties": {
          "shift": { "type": "string", "default": "all" },
          "refresh_interval": { "type": "integer", "default": 60 }
        }
      }
    }
  ]
}
```

### فیلدهای الزامی:
- `id`: شناسه منحصر‌به‌فرد بدون فاصله و کاراکترهای خاص (فقط حروف کوچک، اعداد و underline).
- `name`: نام نمایشی پلاگین به زبان فارسی یا انگلیسی.
- `version`: نسخه به فرمت SemVer (مانند `1.0.0`).
- `entrypoint`: آدرس کلاس در فرمت `module:ClassName` (مانند `plugin:AttendancePlugin`).

---

## ۴. چرخه حیات و هوک‌ها (Lifecycle Hooks)

کلاس پلاگین باید از کلاس پایه `BasePlugin` در `core.plugins.base` ارث‌بری نماید:

```python
from core.plugins.base import BasePlugin, PluginContext

class AttendancePlugin(BasePlugin):
    def on_load(self, context: PluginContext) -> bool:
        """هنگام بارگذاری پلاگین در شروع به کار سامانه اجرا می‌شود."""
        self.log.info("Attendance plugin loaded successfully.")
        return True

    def on_enable(self) -> bool:
        """هنگام فعال‌سازی توسط مدیر سیستم اجرا می‌شود."""
        self.log.info("Attendance plugin enabled.")
        return True

    def on_disable(self) -> bool:
        """هنگام غیرفعال‌سازی پلاگین اجرا می‌شود."""
        self.log.info("Attendance plugin disabled.")
        return True

    def on_unload(self) -> bool:
        """هنگام خروج از حافظه اجرا می‌شود."""
        return True
```

---

## ۵. رابط‌های برنامه‌نویسی قابل استفاده (Plugin APIs)

شیء `PluginContext` در متد `on_load` به پلاگین تزریق می‌شود و دسترسی‌های امن زیر را فراهم می‌کند:

### ۵.۱. Data API (دسترسی به پایگاه داده)
```python
# خواندن داده با ارتباط امن
with context.get_db() as conn:
    rows = conn.execute("SELECT ip, name, total_pages FROM printer_counters").fetchall()
```

### ۵.۲. Web Routes & Blueprint API
```python
from flask import Blueprint, render_template, jsonify

bp_attendance = Blueprint(
    "attendance",
    __name__,
    template_folder="templates",
    static_folder="static",
    url_prefix="/plugins/attendance",
)

@bp_attendance.route("/")
def index():
    return render_template("attendance/index.html")

# ثبت در متد on_load:
context.register_blueprint(bp_attendance)
```

### ۵.۳. Widget API (یکپارچه‌سازی با Dashboard Builder)
پلاگین‌ها می‌توانند ویجت‌های اختصاصی را در `panel_registry` ثبت کنند تا کاربر در محیط Dashboard Builder بتواند آن‌ها را به داشبورد بکشد و رها کند:

```python
def render_attendance_widget(container_id: str, data: dict, config: dict):
    # این متد در فرانت‌اند توسط جاوااسکریپت رندر می‌شود
    pass

context.register_widget(
    widget_id="attendance_summary",
    name="خلاصه تردد و چاپ پرسنل",
    renderer_name="attendanceSummaryRenderer",
    schema={"properties": {"shift": {"type": "string"}}},
)
```

### ۵.۴. Event Bus API
امکان انتشار و اشتراک رویدادها:
```python
# انتشار رویداد
context.publish_event("attendance.user_clock_in", {"user_id": 102, "time": "08:00"})

# اشتراک روی رویدادهای هسته
context.subscribe_event("print.completed", self.on_print_event)
```

---

## ۶. مثال کامل و عملی: پلاگین Attendance

کد پیاده‌سازی‌شده در مسیر `plugins/attendance/` نمونه مرجع کامل است:
- `manifest.json`: تعریف متادیتا و ویجت `attendance_stats`
- `plugin.py`: پیاده‌سازی هوک‌ها و ثبت روت
- `routes.py`: کنترلر وب و API خروجی JSON
- `templates/attendance/index.html`: صفحه مدیریت با استایل‌های منطبق بر PrintGuard

---

## ۷. بسته‌بندی، نصب و استقرار (Deployment)

1. پوشه پلاگین را به صورت یک فایل ZIP بدون پوشه اضافی در ریشه فشرده کنید:
   ```bash
   zip -r attendance.zip plugins/attendance/
   ```
2. فایل فشرده را در سرور در مسیر `/home/user/multi-printer/plugins/` اکسترکت نمایید.
3. برنامه را با اجرای دستور `systemctl restart printguard` یا اجرای `start.bat` مجدداً راه‌اندازی کنید.
4. وارد بخش **مدیریت پلاگین‌ها** (`/api/plugins`) شده و وضعیت آن را فعال (`enabled: true`) فرمایید.

---

## ۸. چک‌لیست امنیت و بهترین شیوه‌ها (Security Best Practices)

- **کوئری‌های فقط‌خواندنی:** پلاگین‌ها نباید جداول حیاتی مانند `users`، `printer_counters` یا `logs` را بدون هماهنگی ویرایش کنند.
- **اعتبارسنجی CSRF:** در تمام درخواست‌های `POST/PUT/DELETE` باید توکن `X-CSRFToken` ارسال شود.
- **ماسک‌کردن داده‌های حساس:** از لاگ‌کردن رمزهای عبور، توکن‌های دسترسی یا PII کاربران اکیداً خودداری نمایید.
- **آفلاین بودن کامل:** پلاگین‌ها نباید به منابع خارجی اینترنتی و CDN وابسته باشند؛ کلیه کتابخانه‌های JS/CSS باید در پوشه `static/` پلاگین قرار داده شوند.
