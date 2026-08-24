# -*- coding: utf-8 -*-
"""tools/api_example.py — نمونه‌ی استفاده‌ی اسکریپتی از API پرینت‌گارد.

ورود با session واقعی + CSRF (همان مسیری که مرورگر می‌رود) و سپس فراخوانی
endpointهای GET. پاسخ خطای «csrf_token_invalid» یعنی رقص کوکی/توکن رعایت
نشده — این اسکریپت دقیقاً همان را خودکار می‌کند.

مثال‌ها:
    python tools/api_example.py admin mypass /api/status
    python tools/api_example.py admin mypass /api/printers /api/printer/172.16.25.54
    python tools/api_example.py admin mypass /api/status --base http://localhost:5050

نکته‌ها:
- نام فیلد کاربر در فرم «identifier» است (نام کاربری یا ایمیل).
- در حالت development چالش reCAPTCHA کنار گذاشته می‌شود؛ اگر روی سرور
  reCAPTCHA واقعی فعال باشد، ورود اسکریپتی ممکن نیست (از مرورگر استفاده کنید).
"""
import argparse
import json
import re
import sys

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("❌ پکیج requests نصب نیست:  python -m pip install requests")

_CSRF_RES = (
    re.compile(r'name="csrf_token"[^>]*value="([^"]+)"'),
    re.compile(r'value="([^"]+)"[^>]*name="csrf_token"'),
    re.compile(r'<meta[^>]*name="csrf-token"[^>]*content="([^"]+)"'),
)


def _extract_csrf(html):
    for rx in _CSRF_RES:
        m = rx.search(html)
        if m:
            return m.group(1)
    return None


def login(session, base, identifier, password):
    """ورود واقعی: دریافت فرم → استخراج csrf_token → POST لاگین."""
    r = session.get(base + "/login", timeout=15)
    r.raise_for_status()
    token = _extract_csrf(r.text)
    if not token:
        raise RuntimeError("csrf_token در صفحه‌ی لاگین پیدا نشد (ساختار صفحه عوض شده؟)")
    r = session.post(base + "/login", data={
        "identifier": identifier,
        "password": password,
        "csrf_token": token,
        "g-recaptcha-response": "",
    }, timeout=15, allow_redirects=False)
    loc = r.headers.get("Location") or ""
    # ورود ناموفق هم 302 برمی‌گرداند ولی به /login?next=... — موفق یعنی رفتن
    # به داشبورد/صفحه‌ی next، نه برگشت به لاگین.
    if r.status_code in (301, 302, 303) and "/login" not in loc:
        return
    raise RuntimeError(
        f"ورود ناموفق (HTTP {r.status_code}" + (f" → {loc}" if loc else "") +
        ") — نام کاربری/رمز را بررسی کنید؛ یا اگر reCAPTCHA فعال است از مرورگر استفاده کنید."
    )


def main():
    ap = argparse.ArgumentParser(description="نمونه‌ی فراخوانی API پرینت‌گارد با ورود + CSRF")
    ap.add_argument("identifier", help="نام کاربری یا ایمیل")
    ap.add_argument("password", help="رمز عبور")
    ap.add_argument("endpoints", nargs="+",
                    help="مسیرهای GET مثل: /api/status /api/printers /api/printer/172.16.25.54")
    ap.add_argument("--base", default="http://localhost:5050",
                    help="آدرس پایه (پیش‌فرض: http://localhost:5050)")
    ap.add_argument("--full", action="store_true", help="چاپ کامل پاسخ JSON (پیش‌فرض ۴۰۰۰ کاراکتر اول)")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    s = requests.Session()
    try:
        login(s, base, args.identifier, args.password)
    except requests.ConnectionError:
        sys.exit(f"❌ اتصال برقرار نشد: {base} — آیا برنامه اجراست و پورت درست است؟")
    except RuntimeError as e:
        sys.exit(f"❌ {e}")
    print(f"✅ ورود موفق: {args.identifier} @ {base}")

    rc = 0
    for ep in args.endpoints:
        url = base + (ep if ep.startswith("/") else "/" + ep)
        r = s.get(url, timeout=20)
        print(f"\n=== GET {ep} → {r.status_code} ===")
        if r.status_code >= 400:
            rc = 1
        try:
            body = json.dumps(r.json(), ensure_ascii=False, indent=2)
        except ValueError:
            body = r.text
        print(body if args.full else body[:4000])
    sys.exit(rc)


if __name__ == "__main__":
    main()
