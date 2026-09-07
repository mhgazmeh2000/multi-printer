# استقرار پشت nginx — دامنه printer.arman-group.ir

> **تفکیک مسئولیت:** دو مشکل مستقل وجود دارد. ① گواهی TLS فقط روی **سرور/nginx** درست می‌شود
> (هیچ تغییری در کد اپ آن را حل نمی‌کند). ② `ProxyFix` تغییر **کد اپ** است و فقط رفتار
> Flask پشت proxy را اصلاح می‌کند. هر دو لازم‌اند.

---

## ۱) گواهی TLS — علت اصلی `ERR_CERT_AUTHORITY_INVALID` (سمت سرور)

`printer.arman-group.ir` به دو IP رند-رابین می‌رود:

| IP | گواهی فعلی | وضعیت |
|----|-----------|-------|
| `2.180.16.22` | `CN=portal.arman-group.ir` (Let's Encrypt) | ❌ SAN فقط portal |
| `94.183.132.237` | همان گواهی | ❌ SAN فقط portal |

گواهی، `printer.arman-group.ir` را پوشش نمی‌دهد ⇒ مرورگر XHRها را با
`ERR_CERT_AUTHORITY_INVALID` می‌بندد. چون DNS بین دو IP می‌چرخد، خطا «بعد از مدتی»
ظاهر می‌شود. راه‌حل — **روی هر دو سرور**:

```bash
# صدور گواهی برای hostname پرینتر (nginx plugin خودش server block را ویرایش می‌کند)
certbot --nginx -d printer.arman-group.ir

# یا اگر می‌خواهید گواهی مشترک portal + printer باشد:
certbot --nginx -d portal.arman-group.ir -d printer.arman-group.ir --expand
```

بعد در server block مربوط به printer:

```nginx
server {
    listen 443 ssl http2;
    server_name printer.arman-group.ir;

    ssl_certificate     /etc/letsencrypt/live/printer.arman-group.ir/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/printer.arman-group.ir/privkey.pem;
    include             /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam         /etc/letsencrypt/ssl-dhparams.pem;

    location / {
        proxy_pass         http://127.0.0.1:5050;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;   # ← برای ProxyFix حیاتی است
        proxy_read_timeout 120s;
    }
}
```

⚠️ **روی هر دو IP/سرور انجام شود** — اگر یکی از دو خط (WAN) گواهی معتبر نداشته باشد،
باز هم به‌صورت متناوب خطا می‌دهید. گواهی‌های Let's Encrypt هر ۶۰ روز تمدید می‌شوند
(`certbot renew` در cron/systemd timer).

**بررسی:**

```bash
echo | openssl s_client -connect 2.180.16.22:443 -servername printer.arman-group.ir 2>/dev/null   | openssl x509 -noout -subject -ext subjectAltName
echo | openssl s_client -connect 94.183.132.237:443 -servername printer.arman-group.ir 2>/dev/null   | openssl x509 -noout -subject -ext subjectAltName
# هر دو باید CN/SAN = printer.arman-group.ir را نشان دهند
```

---

## ۲) ProxyFix در اپ — `TRUST_PROXY=1` (سمت کد — اعمال شد)

nginx می‌آید TLS را terminate می‌کند و HTTP ساده به Flask می‌دهد؛ بدون ProxyFix،
Flask فکر می‌کند همه‌چیز `http` است. با این تغییر، فقط وقتی متغیر محیطی
`TRUST_PROXY=1` باشد، هدرهای `X-Forwarded-For/Proto/Host/Port` (فقط ۱ hop — خود nginx)
معتبر شمرده می‌شوند:

```bash
# در واحد systemd / سرویس استقرار production:
Environment=TRUST_PROXY=1
```

- بدون این flag رفتار توسعه/دسترسی مستقیم عیناً قبل است (هدر جعل‌شده اثری ندارد).
- با flag: `request.is_secure` پشت HTTPS درست می‌شود ⇒ `WTF_CSRF_SSL_STRICT`،
  redirectهای `next=https://…` و OAuth callbackها دیگر بین http/https سرگردانی نمی‌کنند.
- مقدار `x_for=1` یعنی فقط آخرین hop اعتماد می‌شود؛ XFF زنجیره‌ایِ جعلی عبور نمی‌کند.

تست‌های مرتبط: `tests/test_proxy_fix.py` (۴ تست ProxyFix + ۳ تست CSRF/poll-now).

---

## ۳) رفتار جدید فرانت‌اند (کد — اعمال شد)

`dashboard.js` دیگر با یک fetch ناموفق نمی‌میرد:

- شکست fetch ⇒ retry با backoff نمایی (۵s → ۱۰s → ۲۰s → ۴۰s → سقف ۶۰s) تا همیشه.
- بنر کوچک «اتصال به سرور قطع شده — تلاش مجدد خودکار…» در گوشه صفحه (بدون اسپم toast).
- رویداد `visibilitychange` (بازگشت به تب) و `online` (برگشت شبکه) ⇒ رفرش فوری.
- گارد fetch همزمان + لغو تایمر معلق — هیچ fetch موازی‌ای رخ نمی‌دهد.
- موفقیت ⇒ ریست backoff، پنهان‌شدن بنر، ادامه‌ی چرخه‌ی عادی ۴۰ ثانیه‌ای.

نسخه‌ی asset برای باطل‌کردن کش مرورگر: `ASSET_VERSION=20260907-resilience-proxy`
(اگر env `ASSET_VERSION` تنظیم کرده‌اید، آن را هم به‌روز کنید).

---

## ۴) چک‌لیست نهایی استقرار

```text
[ ] certbot برای printer.arman-group.ir روی 2.180.16.22 اجرا شد
[ ] certbot برای printer.arman-group.ir روی 94.183.132.237 اجرا شد
[ ] nginx: X-Forwarded-Proto $scheme در server block پرینتر هست
[ ] سرویس اپ با TRUST_PROXY=1 ری‌استارت شد
[ ] curl -I https://printer.arman-group.ir/ بدون خطای گواهی جواب می‌دهد
[ ] بعد از قطع/وصل موقت شبکه، داشبورد خودش به Live برمی‌گردد (بدون رفرش دستی)
```
