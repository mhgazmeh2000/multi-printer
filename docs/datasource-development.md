# راهنمای توسعه منابع داده (Data Source Development Guide)

این راهنما مستندات توسعه و افزودن درایورهای جدید برای منابع داده سازمانی (Data Sources) در سامانه **PrintGuard** (فاز J.6/J.7 و J.12) می‌باشد.

---

## ۱. معماری فریم‌ورک منابع داده

فریم‌ورک Data Sources امکان اتصال پنل‌های داشبورد به منابع داده داخلی و خارجی را با امنیت بالا و ایزولاسیون کامل فراهم می‌کند:

```
core/datasources/
├── __init__.py           # رجیستری و توابع اصلی
├── base.py               # اینترفیس پایه BaseDataSource و استثناهای امنیتی
├── crypto.py             # ماسک‌کردن کلیدها و رمزهای عبور
├── manager.py            # مدیریت رجیستری و ذخیره‌سازی در دیتابیس
└── drivers/              # درایورهای پیاده‌سازی‌شده
    ├── postgresql_ds.py  # درایور PostgreSQL
    ├── rest_ds.py        # درایور REST API
    └── github_ds.py      # درایور GitHub Metrics
```

---

## ۲. قرارداد کلاس پایه `BaseDataSource`

هر درایور منبع داده باید از کلاس `BaseDataSource` در فایل `core.datasources.base` ارث‌بری کرده و متدهای زیر را پیاده‌سازی نماید:

```python
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

class BaseDataSource(ABC):
    def __init__(self, uid: str, name: str, config: Dict[str, Any]):
        self.uid = uid
        self.name = name
        self.config = config

    @classmethod
    @abstractmethod
    def driver_type(cls) -> str:
        """شناسه یکتای درایور مانند postgresql یا rest."""
        pass

    @classmethod
    @abstractmethod
    def get_config_schema(cls) -> Dict[str, Any]:
        """JSON Schema اعتبارسنجی فیلدهای اتصال و فرم تنظیمات."""
        pass

    @abstractmethod
    def validate_config(self) -> None:
        """اعتبارسنجی وجود پارامترهای الزامی (Host, Port, Token و...)."""
        pass

    @abstractmethod
    def test_connection(self) -> Dict[str, Any]:
        """
        بررسی برقراری ارتباط با منبع داده.
        خروجی: {'success': bool, 'message': str, 'latency_ms': float}
        """
        pass

    @abstractmethod
    def query(self, query_spec: Dict[str, Any]) -> Dict[str, Any]:
        """
        اجرای کوئری و برگرداندن خروجی جدولی استاندارد.
        خروجی استاندارد:
        {
            'columns': [{'name': 'col1', 'type': 'string'}, ...],
            'rows': [{'col1': 'val1'}, ...],
            'total': int,
            'execution_ms': float
        }
        """
        pass

    @abstractmethod
    def get_schema(self) -> Dict[str, Any]:
        """دریافت ساختار متادیتا و ستون‌های جداول یا متدهای منبع داده."""
        pass
```

---

## ۳. امنیت، حفاظت از کلمات عبور و ماسک‌کردن (Credential Masking)

برای جلوگیری از افشای تصادفی کلمات عبور در فرانت‌اند یا لاگ‌های سیستم، ماژول `core.datasources.crypto` فیلدهای حساس را ماسک می‌کند:

```python
from core.datasources.crypto import mask_config, merge_masked_config

# هنگام بازگرداندن اطلاعات به کاربر در API:
safe_config = mask_config(raw_config)
# نتیجه: {'host': 'pg.local', 'password': '••••••••', ...}

# هنگام ذخیره ویرایش، اگر کاربر مقدار ماسک‌شده را ارسال کرده باشد،
# رمز عبور قبلی حفظ شده و رونویسی با '••••••••' نمی‌شود:
updated_config = merge_masked_config(new_payload, current_config)
```

---

## ۴. گارد امنیتی کوئری‌های پایگاه داده (Strict Read-Only Enforcement)

درایورهای مبتنی بر پایگاه داده (مانند PostgreSQL یا MySQL) موظف به رعایت گارد امنیتی ضد تغییرات مخرب هستند:
- **اجازه به دستورات بی‌خطر:** فقط کوئری‌هایی که با `SELECT` یا `WITH` شروع می‌شوند مجاز هستند.
- **انسداد واژگان خطرناک:** هرگونه کلمه کلیدی شامل `INSERT`, `UPDATE`, `DELETE`, `DROP`, `TRUNCATE`, `ALTER`, `GRANT`, `REVOKE`, `EXEC`, `CREATE` شناسایی و رد می‌شود.
- **جلوگیری از اجرای چنددستوری (Multi-statement injection):** وجود کاراکتر `;` در میانه کوئری ممنوع است.
- **تزریق خودکار محدودیت سقف ردیف:** در صورت نبود `LIMIT` در کوئری، سقف حداکثر ۱۰۰۰ ردیف به صورت خودکار به کوئری افزوده می‌شود.

نمونه اجرای گارد امنیتی در درایور:
```python
def _guard_read_only_sql(self, sql: str) -> str:
    cleaned = sql.strip().rstrip(";")
    if ";" in cleaned:
        raise DataSourceSecurityError("اجرای هم‌زمان چند کوئری مجاز نمی‌باشد.")
    if not re.match(r"^(SELECT|WITH)\b", cleaned, re.IGNORECASE):
        raise DataSourceSecurityError("فقط کوئری‌های فقط‌خواندنی (SELECT) مجاز هستند.")
    for kw in ["DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "TRUNCATE"]:
        if re.search(r"\b" + kw + r"\b", cleaned, re.IGNORECASE):
            raise DataSourceSecurityError(f"دستور خطرناک {kw} مجاز نیست.")
    if not re.search(r"\bLIMIT\b", cleaned, re.IGNORECASE):
        cleaned += " LIMIT 1000"
    return cleaned
```

---

## ۵. راهنمای گام‌به‌گام پیاده‌سازی یک درایور سفارشی

برای مثال، ساخت درایور سامانه اختصاصی لاگ‌های الستیک‌سرچ یا وب‌سرویس داخلی:

### گام ۱: ایجاد فایل درایور
فایل `core/datasources/drivers/my_driver.py` را ایجاد کنید:

```python
from core.datasources.base import BaseDataSource, DataSourceError
from typing import Dict, Any

class CustomApiDataSource(BaseDataSource):
    @classmethod
    def driver_type(cls) -> str:
        return "custom_api"

    @classmethod
    def get_config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "required": ["api_url"],
            "properties": {
                "api_url": {"type": "string", "title": "آدرس سرور"},
                "api_key": {"type": "string", "title": "کلید دسترسی (API Key)"}
            }
        }

    def validate_config(self) -> None:
        if not self.config.get("api_url"):
            raise DataSourceError("آدرس سرور API الزامی است.")

    def test_connection(self) -> Dict[str, Any]:
        # ارسال پینگ یا درخواست تست به سرور مقصد
        return {"success": True, "message": "ارتباط با موفقیت برقرار شد.", "latency_ms": 15.4}

    def query(self, query_spec: Dict[str, Any]) -> Dict[str, Any]:
        # استخراج داده‌ها و تبدیل به ساختار استاندارد
        return {
            "columns": [{"name": "metric", "type": "string"}, {"name": "val", "type": "number"}],
            "rows": [{"metric": "cpu_load", "val": 42}],
            "total": 1,
            "execution_ms": 12.0
        }

    def get_schema(self) -> Dict[str, Any]:
        return {"metrics": ["cpu_load", "memory_usage"]}
```

### گام ۲: ثبت درایور در رجیستری
در `core/datasources/manager.py` درایور را اضافه کنید:
```python
from core.datasources.drivers.my_driver import CustomApiDataSource

data_source_registry.register(CustomApiDataSource)
```

---

## ۶. تست درایور با تست‌های خودکار

تست‌های درایور باید در شاخه `tests/test_datasources.py` اضافه شده و موارد زیر را پوشش دهند:
1. اعتبارسنجی پارامترهای ورودی (`validate_config`)
2. تست اتصال (`test_connection`)
3. اجرای کوئری و اطمینان از خروجی استاندارد (`columns`, `rows`, `execution_ms`)
4. آزمایش رفتار در شرایط خطای شبکه یا احراز هویت
5. ماسک‌کردن کلیدهای محرمانه
