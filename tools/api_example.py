# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tools/api_example.py
# @brief:        Example script showing how to call the PrintGuard HTTP API.
# @details:      Responsibilities: Log in and call a few endpoints as a usage example.
#                Main API: login(), main()
#                Side effects: network I/O
# @dependencies: external: requests
# @notes:        Can be run directly as a script.
# =============================================================================

"""tools/api_example.py — example script usage of Print Guard API.

Log in with a real session + CSRF (the same path as the browser) and then call
GET endpoints. "csrf_token_invalid" error response means compliance cookie/token dance
Not done — this script automates exactly that.

Examples:
    python tools/api_example.py admin mypass /api/status
    python tools/api_example.py admin mypass /api/printers /api/printer/172.16.25.54
    python tools/api_example.py admin mypass /api/status --base http://localhost:5050

Tips:
- The name of the user field in the form is "identifier" (username or email).
- In the development mode, the reCAPTCHA challenge is omitted; If on the server
  Real reCAPTCHA is enabled, script login is not possible (use browser).
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
    """Extract the CSRF token from an HTML page."""
    for rx in _CSRF_RES:
        m = rx.search(html)
        if m:
            return m.group(1)
    return None


def login(session, base, identifier, password):
    """Actual login: receive form → extract csrf_token → POST login."""
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
    # Unsuccessful login also returns 302, but to /login?next=... — successful means leaving
    # Go to dashboard/next page, not back to login.
    if r.status_code in (301, 302, 303) and "/login" not in loc:
        return
    raise RuntimeError(
        f"ورود ناموفق (HTTP {r.status_code}" + (f" → {loc}" if loc else "") +
        ") — نام کاربری/رمز را بررسی کنید؛ یا اگر reCAPTCHA فعال است از مرورگر استفاده کنید."
    )


def main():
    """Command-line example: log in and call the given API paths."""

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
