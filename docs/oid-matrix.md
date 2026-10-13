# ماتریس OID — تولیدشده از کد

> ⚙️ خروجی `tools/build_oid_matrix.py` است؛ **دستی ویرایش نکنید**.  
> بازتولید: `python tools\build_oid_matrix.py --write` · گیت تازگی: `python tools\build_oid_matrix.py --check` (کد خروج ۱ ⇒ سند کهنه است).

## ۱. سرجمع

- **136** OID یکتا در ماتریس — مرجع `monitoring/generator.yml`.
- `core/oid/catalog.py::OID_CATALOG`: **79** ورودی، که 76 تای آن‌ها OID یکتا می‌دهد (3 OID تکراری برای fallback)
- نوشتارِ متن OID در فایل‌های مرجع (occurrence): 247 — یک OID می‌تواند در چند collector/فهرست آمده باشد
- نوع خواندن: `get` = 104 · `walk` = 32
- OID یتیم (فقط در کدِ کمکی/وب/ابزار): 10
- با احتساب probeها: 146
- پیمانهٔ خام (هر رشتهٔ OID-شکل، حتی دُم/پیشوند/probe): 163 — 27 تای آن در ماتریس **نیست** و مبنای `generator.yml` نمی‌شود
- پایهٔ ساخت که از ماتریس بیرون رفت (`OID_BASE`/`BASE`): 1 — `1.3.6.1.4.1.1129.2.3.50`
- رشته‌هایی که OID شمرده **نشده‌اند**: 1 جدول در §۶ (کوتاه‌تر از ۷ جزء، یا پیشوند برند‌یابی/regex)

### چرا «یک عدد» نیست

| پیمانه | عدد | چیست |
|---|---|---|
| OID یکتا | 136 | مجموعهٔ ریاضی OIDهایی که کد واقعاً می‌خواند |
| ردیف کاتالوگ | 76 | هر ورودیِ `OID_CATALOG` یک سطر (OID تکراری هم دارد) |
| occurrence | 247 | هر بار که رشتهٔ OID در کدِ مرجع نوشته شده |

> هر عدد دیگری (مثل **۱۵۸** در پلان فاز G) باید به یکی از این سه پیمانه برگردد، وگرنه یا ردیف‌های گستردهٔ `walk × index` شمرده شده یا تکرارها. `generator.yml` با پیمانهٔ **اول** ساخته و سنجیده می‌شود.

## ۲. توزیع به تفکیک ماژول

| ماژول | OID یکتا | `get` | `walk` | + probe |
|---|---|---|---|---|
| `printer_toshiba` | 63 | 46 | 17 | 64 |
| `printer_brother` | 9 | 7 | 2 | 17 |
| `printer_hp` | 14 | 12 | 2 | 14 |
| `printer_canon` | 16 | 16 | 0 | 16 |
| `sensor_ecs100g` | 10 | 10 | 0 | 10 |
| `printer_std` | 24 | 13 | 11 | 25 |
| **جمع** | **136** | **104** | **32** | **146** |

ستون «+ probe» شامل `web/routes/*` و `tools/*` (کاوش‌گرها و تشخیص‌دهنده‌ها) هم هست؛ مبنای `generator.yml` ستون دوم است، چون مسیر polling همین کد است.

## ۳. ردیف‌ها

### `printer_toshiba` · 63 OID

| OID | خواندن | برچسب/کلید | دسته | نوع | منبع |
|---|---|---|---|---|---|
| `1.3.6.1.4.1.1129.2.3.50.1.2.3.1.3.1.1` | get | tsh_model | identity | str | `core/oid/catalog.py:42` (+2) |
| `1.3.6.1.4.1.1129.2.3.50.1.2.3.1.4.1.1` | get | tsh_firmware | identity | str | `core/oid/catalog.py:44` (+1) |
| `1.3.6.1.4.1.1129.2.3.50.1.2.3.1.11.1.1` | get | uptime | toshiba_map | — | `core/snmp/oid_map.py:11` |
| `1.3.6.1.4.1.1129.2.3.50.1.2.4.1.8.1.1` | get | tsh_serial | identity | str | `core/oid/catalog.py:43` (+2) |
| `1.3.6.1.4.1.1129.2.3.50.1.3.5.1.1.3.1.1` | walk | tray1_status | toshiba_map | — | `core/snmp/oid_map.py:57` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.5.1.1.3.1.2` | walk | tray2_status | toshiba_map | — | `core/snmp/oid_map.py:58` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.5.1.1.3.1.3` | walk | tray3_status | toshiba_map | — | `core/snmp/oid_map.py:59` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.5.1.1.3.1.6` | walk | tray6_status | toshiba_map | — | `core/snmp/oid_map.py:60` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.5.1.1.4.1.1` | walk | tsh_t1_size | tray | int | `core/oid/catalog.py:58` (+1) |
| `1.3.6.1.4.1.1129.2.3.50.1.3.5.1.1.4.1.2` | walk | tsh_t2_size | tray | int | `core/oid/catalog.py:59` (+1) |
| `1.3.6.1.4.1.1129.2.3.50.1.3.5.1.1.4.1.3` | walk | tray3_size | toshiba_map | — | `core/snmp/oid_map.py:55` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.5.1.1.4.1.6` | walk | tray6_size | toshiba_map | — | `core/snmp/oid_map.py:56` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.5.1.1.5.1.1` | walk | tsh_t1_level | tray | int | `core/oid/catalog.py:56` (+1) |
| `1.3.6.1.4.1.1129.2.3.50.1.3.5.1.1.5.1.2` | walk | tsh_t2_level | tray | int | `core/oid/catalog.py:57` (+1) |
| `1.3.6.1.4.1.1129.2.3.50.1.3.5.1.1.5.1.3` | walk | tray3_level | toshiba_map | — | `core/snmp/oid_map.py:51` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.5.1.1.5.1.6` | walk | tray6_level | toshiba_map | — | `core/snmp/oid_map.py:52` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.15.2.1.7.1.1` | get | alert1_code | toshiba_map | — | `core/snmp/oid_map.py:72` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.15.2.1.7.1.2` | get | alert2_code | toshiba_map | — | `core/snmp/oid_map.py:73` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.15.2.1.7.1.3` | get | alert3_code | toshiba_map | — | `core/snmp/oid_map.py:74` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.15.2.1.8.1.1` | get | alert1_msg | toshiba_map | — | `core/snmp/oid_map.py:69` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.15.2.1.8.1.2` | get | alert2_msg | toshiba_map | — | `core/snmp/oid_map.py:70` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.15.2.1.8.1.3` | get | alert3_msg | toshiba_map | — | `core/snmp/oid_map.py:71` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.2.1.1` | get | tsh_color | counter | int | `core/oid/catalog.py:46` (+2) |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.2.1.2` | get | tsh_twin | counter | int | `core/oid/catalog.py:52` (+1) |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.2.1.3` | get | tsh_bw | counter | int | `core/oid/catalog.py:47` (+1) |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.2.1.4` | get | tsh_total | counter | int | `core/oid/catalog.py:45` (+2) |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.3.1.1` | get | tsh_print_fc | counter | int | `core/oid/catalog.py:50` (+1) |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.3.1.3` | get | tsh_print_bw | counter | int | `core/oid/catalog.py:51` (+1) |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.4.1.1` | get | tsh_copy_fc | counter | int | `core/oid/catalog.py:48` (+1) |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.4.1.3` | get | tsh_copy_bw | counter | int | `core/oid/catalog.py:49` (+1) |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.7.1.4` | get | tsh_fax | counter | int | `core/oid/catalog.py:53` (+1) |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.8.1.1` | get | scan_net_fc | toshiba_map | — | `core/snmp/oid_map.py:47` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.8.1.3` | get | scan_net_bw | toshiba_map | — | `core/snmp/oid_map.py:48` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.9.1.1` | get | tsh_scan_fc | counter | int | `core/oid/catalog.py:54` (+1) |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.9.1.3` | get | tsh_scan_bw | counter | int | `core/oid/catalog.py:55` (+1) |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.11.1.4` | walk | print_list | toshiba_map | — | `core/snmp/oid_map.py:21` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.207.1.1` | get | a3_fc | toshiba_map | — | `core/snmp/oid_map.py:34` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.207.1.3` | get | a3_bw | toshiba_map | — | `core/snmp/oid_map.py:35` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.207.1.4` | get | a3_total | toshiba_map | — | `core/snmp/oid_map.py:33` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.208.1.1` | get | a4_fc | toshiba_map | — | `core/snmp/oid_map.py:31` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.208.1.3` | get | a4_bw | toshiba_map | — | `core/snmp/oid_map.py:32` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.208.1.4` | get | a4_total | toshiba_map | — | `core/snmp/oid_map.py:30` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.209.1.1` | get | a4r_fc | toshiba_map | — | `core/snmp/oid_map.py:37` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.209.1.3` | get | a4r_bw | toshiba_map | — | `core/snmp/oid_map.py:38` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.209.1.4` | get | a4r_total | toshiba_map | — | `core/snmp/oid_map.py:36` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.210.1.1` | get | a5_fc | toshiba_map | — | `core/snmp/oid_map.py:40` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.210.1.3` | get | a5_bw | toshiba_map | — | `core/snmp/oid_map.py:41` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.210.1.4` | get | a5_total | toshiba_map | — | `core/snmp/oid_map.py:39` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.227.1.1` | get | b4_fc | toshiba_map | — | `core/snmp/oid_map.py:43` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.227.1.3` | get | b4_bw | toshiba_map | — | `core/snmp/oid_map.py:44` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.227.1.4` | get | b4_total | toshiba_map | — | `core/snmp/oid_map.py:42` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.8.1.2.1.1` | get | toner_cyan_usage | toshiba_map | — | `core/snmp/oid_map.py:61` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.8.1.2.1.2` | get | toner_magenta_usage | toshiba_map | — | `core/snmp/oid_map.py:62` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.8.1.2.1.3` | get | toner_yellow_usage | toshiba_map | — | `core/snmp/oid_map.py:63` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.8.1.2.1.4` | get | toner_black_usage | toshiba_map | — | `core/snmp/oid_map.py:64` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.8.1.3.1.1` | walk | tsh_cyan_st | supply | int | `core/oid/catalog.py:60` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.8.1.3.1.2` | walk | tsh_mag_st | supply | int | `core/oid/catalog.py:61` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.8.1.3.1.3` | walk | tsh_yel_st | supply | int | `core/oid/catalog.py:62` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.8.1.3.1.4` | walk | tsh_blk_st | supply | int | `core/oid/catalog.py:63` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.8.1.5.1.1` | get | toner_magenta_status | toshiba_map | — | `core/snmp/oid_map.py:65` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.8.1.5.1.2` | get | toner_cyan_status | toshiba_map | — | `core/snmp/oid_map.py:66` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.8.1.5.1.3` | get | toner_yellow_status | toshiba_map | — | `core/snmp/oid_map.py:67` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.8.1.5.1.4` | get | toner_black_status | toshiba_map | — | `core/snmp/oid_map.py:68` |

### `printer_brother` · 9 OID

| OID | خواندن | برچسب/کلید | دسته | نوع | منبع |
|---|---|---|---|---|---|
| `1.3.6.1.4.1.2435.2.3.9.1.1.1.1.3.0` | get | bro_firmware_alt | identity | str | `core/oid/catalog.py:89` (+1) |
| `1.3.6.1.4.1.2435.2.3.9.4.2.1.1.6` | walk | brother | code | — | `core/collectors/cartridge_discovery.py:69` |
| `1.3.6.1.4.1.2435.2.3.9.4.2.1.5` | walk | brother | code | — | `core/collectors/cartridge_discovery.py:70` |
| `1.3.6.1.4.1.2435.2.3.9.4.2.1.5.1.1.6.0` | get | val | code | — | `core/collectors/cartridge_id.py:305` |
| `1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.1.1` | get | brother_black | code | — | `core/collectors/brother.py:169` (+1) |
| `1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.1.2` | get | BROTHER_DRUM_OID | code | — | `core/enhanced_collector.py:66` |
| `1.3.6.1.4.1.2435.2.4.3.99.3.1.6.1.2.1` | get | bro_model | identity | str | `core/oid/catalog.py:86` (+2) |
| `1.3.6.1.4.1.2435.2.4.3.99.3.1.6.1.2.3` | get | bro_serial | identity | str | `core/oid/catalog.py:87` (+2) |
| `1.3.6.1.4.1.2435.2.4.3.1240.6.5.0` | get | bro_firmware | identity | str | `core/oid/catalog.py:88` (+1) |

### `printer_hp` · 14 OID

| OID | خواندن | برچسب/کلید | دسته | نوع | منبع |
|---|---|---|---|---|---|
| `1.3.6.1.4.1.11.2.3.9.1.1.3.1.1.1.1.2.0` | get | hp_model | identity | str | `core/oid/catalog.py:76` (+2) |
| `1.3.6.1.4.1.11.2.3.9.1.1.3.1.1.1.1.3.0` | get | hp_serial | identity | str | `core/oid/catalog.py:77` (+2) |
| `1.3.6.1.4.1.11.2.3.9.1.1.3.1.1.1.1.7.0` | get | hp_firmware | identity | str | `core/oid/catalog.py:78` (+1) |
| `1.3.6.1.4.1.11.2.3.9.1.1.7.0` | get | general_oids | code | — | `core/enhanced_collector.py:231` |
| `1.3.6.1.4.1.11.2.3.9.4.2.1.1.6` | walk | hp | code | — | `core/collectors/cartridge_discovery.py:65` |
| `1.3.6.1.4.1.11.2.3.9.4.2.1.4` | walk | hp | code | — | `core/collectors/cartridge_discovery.py:66` |
| `1.3.6.1.4.1.11.2.3.9.4.2.1.4.1.2.1.5.5.1.1` | get | general_oids | code | — | `core/enhanced_collector.py:229` |
| `1.3.6.1.4.1.11.2.3.9.4.2.1.4.1.2.4.1.2.1.5.5.1.1` | get | _HP_NPCL_PCT_OID | code | — | `core/enhanced_collector.py:39` (+1) |
| `1.3.6.1.4.1.11.2.3.9.6.1.1.3.1` | get | hp_scan_color | counter | int | `core/oid/catalog.py:82` (+1) |
| `1.3.6.1.4.1.11.2.3.9.6.1.1.5.1` | get | hp_print_color | counter | int | `core/oid/catalog.py:81` (+1) |
| `1.3.6.1.4.1.11.2.3.9.6.1.1.6.1` | get | hp_copy_color | counter | int | `core/oid/catalog.py:79` (+1) |
| `1.3.6.1.4.1.11.2.3.9.6.1.1.9.1` | get | hp_copy_mono | counter | int | `core/oid/catalog.py:80` (+1) |
| `1.3.6.1.4.1.11.2.3.9.6.1.1.10.1` | get | hp_scan_mono | counter | int | `core/oid/catalog.py:83` (+1) |
| `1.3.6.1.4.1.11.2.3.9.6.1.1.11.1` | get | hp_fax | counter | int | `core/oid/catalog.py:84` (+1) |

### `printer_canon` · 16 OID

| OID | خواندن | برچسب/کلید | دسته | نوع | منبع |
|---|---|---|---|---|---|
| `1.3.6.1.4.1.1602.1.1.1.1.0` | get | canon_model | identity | str | `core/oid/catalog.py:65` (+2) |
| `1.3.6.1.4.1.1602.1.1.1.4.0` | get | canon_firmware | identity | str | `core/oid/catalog.py:67` (+1) |
| `1.3.6.1.4.1.1602.1.2.1.1.1.1.1` | get | CANON_ALTERNATE_OIDS | code | — | `core/enhanced_collector.py:60` |
| `1.3.6.1.4.1.1602.1.2.1.1.1.2.1` | get | CANON_ALTERNATE_OIDS | code | — | `core/enhanced_collector.py:61` |
| `1.3.6.1.4.1.1602.1.2.1.4.0` | get | canon_serial | identity | str | `core/oid/catalog.py:66` (+3) |
| `1.3.6.1.4.1.1602.1.11.1.1.1.3.1` | get | canon_lbp_total | counter | int | `core/oid/catalog.py:73` (+1) |
| `1.3.6.1.4.1.1602.1.11.1.1.1.3.3` | get | alt_lbp_total | code | — | `core/collectors/canon.py:267` |
| `1.3.6.1.4.1.1602.1.11.1.1.1.3.4` | get | canon_lbp_color | counter | int | `core/oid/catalog.py:74` (+1) |
| `1.3.6.1.4.1.1602.1.11.2.1.1.3.1` | get | canon_total | counter | int | `core/oid/catalog.py:68` (+2) |
| `1.3.6.1.4.1.1602.1.11.2.1.1.3.6` | get | canon_copy | counter | int | `core/oid/catalog.py:70` (+1) |
| `1.3.6.1.4.1.1602.1.11.2.1.1.3.9` | get | canon_print | counter | int | `core/oid/catalog.py:71` (+1) |
| `1.3.6.1.4.1.1602.1.11.2.1.1.3.12` | get | canon_scan | counter | int | `core/oid/catalog.py:72` (+1) |
| `1.3.6.1.4.1.1602.1.11.2.2.1.3.1` | get | alt_total | code | — | `core/collectors/canon.py:216` |
| `1.3.6.1.4.1.1602.1.11.2.2.1.3.6` | get | alt_copy | code | — | `core/collectors/canon.py:217` |
| `1.3.6.1.4.1.1602.1.11.2.2.1.3.9` | get | alt_print | code | — | `core/collectors/canon.py:218` |
| `1.3.6.1.4.1.1602.1.11.2.2.1.3.12` | get | alt_scan | code | — | `core/collectors/canon.py:219` |

### `sensor_ecs100g` · 10 OID

| OID | خواندن | برچسب/کلید | دسته | نوع | منبع |
|---|---|---|---|---|---|
| `1.3.6.1.4.1.47206.1.0` | get | sensor_model | sensor | str | `core/oid/catalog.py:13` (+3) |
| `1.3.6.1.4.1.47206.2.0` | get | sensor_serial | sensor | str | `core/oid/catalog.py:14` (+1) |
| `1.3.6.1.4.1.47206.110.1.1.0` | get | sensor_temp1_st | sensor | int | `core/oid/catalog.py:16` |
| `1.3.6.1.4.1.47206.110.1.2.0` | get | sensor_temp1 | sensor | int | `core/oid/catalog.py:15` (+2) |
| `1.3.6.1.4.1.47206.110.2.1.0` | get | sensor_temp2_st | sensor | int | `core/oid/catalog.py:18` |
| `1.3.6.1.4.1.47206.110.2.2.0` | get | sensor_temp2 | sensor | int | `core/oid/catalog.py:17` |
| `1.3.6.1.4.1.47206.111.1.1.0` | get | sensor_hum1_st | sensor | int | `core/oid/catalog.py:20` |
| `1.3.6.1.4.1.47206.111.1.2.0` | get | sensor_hum1 | sensor | int | `core/oid/catalog.py:19` (+2) |
| `1.3.6.1.4.1.47206.111.2.1.0` | get | sensor_hum2_st | sensor | int | `core/oid/catalog.py:22` |
| `1.3.6.1.4.1.47206.111.2.2.0` | get | sensor_hum2 | sensor | int | `core/oid/catalog.py:21` |

### `printer_std` · 24 OID

| OID | خواندن | برچسب/کلید | دسته | نوع | منبع |
|---|---|---|---|---|---|
| `1.3.6.1.2.1.1.1.0` | get | sys_descr | sys | str | `core/oid/catalog.py:8` (+5) |
| `1.3.6.1.2.1.1.2.0` | get | sys_object_id | sys | str | `core/oid/catalog.py:11` (+2) |
| `1.3.6.1.2.1.1.3.0` | get | sys_uptime | sys | int | `core/oid/catalog.py:9` (+7) |
| `1.3.6.1.2.1.1.5.0` | get | sys_hostname | sys | str | `core/oid/catalog.py:10` (+3) |
| `1.3.6.1.2.1.43.5.1.1.16.1` | get | model_mib | identity | str | `core/oid/catalog.py:24` (+4) |
| `1.3.6.1.2.1.43.5.1.1.17.1` | get | serial_mib | identity | str | `core/oid/catalog.py:25` (+5) |
| `1.3.6.1.2.1.43.6.1.1.3.1.1` | get | cover_status | status | int | `core/oid/catalog.py:40` (+3) |
| `1.3.6.1.2.1.43.8.2.1.9.1.1` | walk | tray1_cap | tray | int | `core/oid/catalog.py:35` |
| `1.3.6.1.2.1.43.8.2.1.9.1.2` | walk | tray2_cap | tray | int | `core/oid/catalog.py:38` |
| `1.3.6.1.2.1.43.8.2.1.10.1.1` | walk | tray1_level | tray | int | `core/oid/catalog.py:36` |
| `1.3.6.1.2.1.43.8.2.1.10.1.2` | walk | tray2_level | tray | int | `core/oid/catalog.py:39` |
| `1.3.6.1.2.1.43.8.2.1.13.1.1` | walk | tray1_name | identity | str | `core/oid/catalog.py:34` |
| `1.3.6.1.2.1.43.8.2.1.13.1.2` | walk | tray2_name | identity | str | `core/oid/catalog.py:37` |
| `1.3.6.1.2.1.43.10.2.1.4.1.1` | get | bro_total | counter | int | `core/oid/catalog.py:90` (+9) |
| `1.3.6.1.2.1.43.10.2.1.4.1.2` | get | bro_color | counter | int | `core/oid/catalog.py:91` (+4) |
| `1.3.6.1.2.1.43.10.2.1.4.1.3` | get | bw_std | code | — | `core/collectors/canon.py:203` (+1) |
| `1.3.6.1.2.1.43.10.2.1.5.1.1` | get | bro_bw | counter | int | `core/oid/catalog.py:92` |
| `1.3.6.1.2.1.43.11.1.1.6` | walk | standard | code | — | `core/collectors/cartridge_discovery.py:62` |
| `1.3.6.1.2.1.43.11.1.1.6.1.1` | get | toner_name_1 | identity | str | `core/oid/catalog.py:28` (+2) |
| `1.3.6.1.2.1.43.11.1.1.6.1.2` | get | toner_name_2 | identity | str | `core/oid/catalog.py:31` |
| `1.3.6.1.2.1.43.11.1.1.8.1.1` | walk | toner_max_1 | supply | int | `core/oid/catalog.py:29` |
| `1.3.6.1.2.1.43.11.1.1.8.1.2` | walk | toner_max_2 | supply | int | `core/oid/catalog.py:32` |
| `1.3.6.1.2.1.43.11.1.1.9.1.1` | walk | toner_remain_1 | supply | int | `core/oid/catalog.py:30` |
| `1.3.6.1.2.1.43.11.1.1.9.1.2` | walk | toner_remain_2 | supply | int | `core/oid/catalog.py:33` |

## ۴. تکرارها (یک OID، چند جا)

| OID | ماژول | تکرارها |
|---|---|---|
| `1.3.6.1.4.1.1129.2.3.50.1.2.3.1.3.1.1` | `printer_toshiba` | `core/snmp/oid_map.py:8` · `core/enhanced_collector.py:1079` |
| `1.3.6.1.4.1.1129.2.3.50.1.2.3.1.4.1.1` | `printer_toshiba` | `core/snmp/oid_map.py:10` |
| `1.3.6.1.4.1.1129.2.3.50.1.2.4.1.8.1.1` | `printer_toshiba` | `core/snmp/oid_map.py:9` · `core/enhanced_collector.py:1093` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.5.1.1.4.1.1` | `printer_toshiba` | `core/snmp/oid_map.py:53` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.5.1.1.4.1.2` | `printer_toshiba` | `core/snmp/oid_map.py:54` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.5.1.1.5.1.1` | `printer_toshiba` | `core/snmp/oid_map.py:49` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.5.1.1.5.1.2` | `printer_toshiba` | `core/snmp/oid_map.py:50` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.2.1.1` | `printer_toshiba` | `core/snmp/oid_map.py:13` · `core/enhanced_collector.py:990` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.2.1.2` | `printer_toshiba` | `core/snmp/oid_map.py:19` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.2.1.3` | `printer_toshiba` | `core/snmp/oid_map.py:14` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.2.1.4` | `printer_toshiba` | `core/snmp/oid_map.py:12` · `core/enhanced_collector.py:958` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.3.1.1` | `printer_toshiba` | `core/snmp/oid_map.py:15` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.3.1.3` | `printer_toshiba` | `core/snmp/oid_map.py:16` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.4.1.1` | `printer_toshiba` | `core/snmp/oid_map.py:17` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.4.1.3` | `printer_toshiba` | `core/snmp/oid_map.py:18` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.7.1.4` | `printer_toshiba` | `core/snmp/oid_map.py:20` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.9.1.1` | `printer_toshiba` | `core/snmp/oid_map.py:45` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.9.1.3` | `printer_toshiba` | `core/snmp/oid_map.py:46` |
| `1.3.6.1.4.1.2435.2.3.9.1.1.1.1.3.0` | `printer_brother` | `core/collectors/brother.py:139` |
| `1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.1.1` | `printer_brother` | `core/enhanced_collector.py:65` |
| `1.3.6.1.4.1.2435.2.4.3.99.3.1.6.1.2.1` | `printer_brother` | `core/collectors/brother.py:131` · `core/enhanced_collector.py:1082` |
| `1.3.6.1.4.1.2435.2.4.3.99.3.1.6.1.2.3` | `printer_brother` | `core/collectors/brother.py:135` · `core/enhanced_collector.py:1096` |
| `1.3.6.1.4.1.2435.2.4.3.1240.6.5.0` | `printer_brother` | `core/collectors/brother.py:138` |
| `1.3.6.1.4.1.11.2.3.9.1.1.3.1.1.1.1.2.0` | `printer_hp` | `core/collectors/hp.py:177` · `core/enhanced_collector.py:1080` |
| `1.3.6.1.4.1.11.2.3.9.1.1.3.1.1.1.1.3.0` | `printer_hp` | `core/collectors/hp.py:184` · `core/enhanced_collector.py:1094` |
| `1.3.6.1.4.1.11.2.3.9.1.1.3.1.1.1.1.7.0` | `printer_hp` | `core/collectors/hp.py:187` |
| `1.3.6.1.4.1.11.2.3.9.4.2.1.4.1.2.4.1.2.1.5.5.1.1` | `printer_hp` | `core/enhanced_collector.py:230` |
| `1.3.6.1.4.1.11.2.3.9.6.1.1.3.1` | `printer_hp` | `core/collectors/hp.py:215` |
| `1.3.6.1.4.1.11.2.3.9.6.1.1.5.1` | `printer_hp` | `core/collectors/hp.py:211` |
| `1.3.6.1.4.1.11.2.3.9.6.1.1.6.1` | `printer_hp` | `core/collectors/hp.py:213` |
| `1.3.6.1.4.1.11.2.3.9.6.1.1.9.1` | `printer_hp` | `core/collectors/hp.py:212` |
| `1.3.6.1.4.1.11.2.3.9.6.1.1.10.1` | `printer_hp` | `core/collectors/hp.py:214` |
| `1.3.6.1.4.1.11.2.3.9.6.1.1.11.1` | `printer_hp` | `core/collectors/hp.py:216` |
| `1.3.6.1.4.1.1602.1.1.1.1.0` | `printer_canon` | `core/collectors/canon.py:177` · `core/enhanced_collector.py:1081` |
| `1.3.6.1.4.1.1602.1.1.1.4.0` | `printer_canon` | `core/collectors/canon.py:185` |
| `1.3.6.1.4.1.1602.1.2.1.4.0` | `printer_canon` | `core/collectors/canon.py:182` · `core/enhanced_collector.py:1095` · `core/enhanced_collector.py:674` |
| `1.3.6.1.4.1.1602.1.11.1.1.1.3.1` | `printer_canon` | `core/collectors/canon.py:213` |
| `1.3.6.1.4.1.1602.1.11.1.1.1.3.4` | `printer_canon` | `core/collectors/canon.py:214` |
| `1.3.6.1.4.1.1602.1.11.2.1.1.3.1` | `printer_canon` | `core/collectors/canon.py:208` · `core/enhanced_collector.py:599` |
| `1.3.6.1.4.1.1602.1.11.2.1.1.3.6` | `printer_canon` | `core/collectors/canon.py:209` |
| `1.3.6.1.4.1.1602.1.11.2.1.1.3.9` | `printer_canon` | `core/collectors/canon.py:210` |
| `1.3.6.1.4.1.1602.1.11.2.1.1.3.12` | `printer_canon` | `core/collectors/canon.py:211` |
| `1.3.6.1.4.1.47206.1.0` | `sensor_ecs100g` | `core/collectors/base.py:1528` · `core/collectors/sensor.py:26` · `core/enhanced_collector.py:904` |
| `1.3.6.1.4.1.47206.2.0` | `sensor_ecs100g` | `core/collectors/sensor.py:27` |
| `1.3.6.1.4.1.47206.110.1.2.0` | `sensor_ecs100g` | `core/collectors/base.py:1529` · `core/enhanced_collector.py:905` |
| `1.3.6.1.4.1.47206.111.1.2.0` | `sensor_ecs100g` | `core/collectors/base.py:1530` · `core/enhanced_collector.py:906` |
| `1.3.6.1.2.1.1.1.0` | `printer_std` | `core/collectors/base.py:1521` · `core/collectors/canon.py:181` · `core/collectors/hp.py:181` · `core/enhanced_collector.py:894` … |
| `1.3.6.1.2.1.1.2.0` | `printer_std` | `core/collectors/base.py:1520` · `core/enhanced_collector.py:922` |
| `1.3.6.1.2.1.1.3.0` | `printer_std` | `core/collectors/brother.py:128` · `core/collectors/canon.py:139` · `core/collectors/hp.py:173` · `core/collectors/sensor.py:28` … |
| `1.3.6.1.2.1.1.5.0` | `printer_std` | `core/collectors/brother.py:133` · `core/enhanced_collector.py:1083` · `core/enhanced_collector.py:569` |
| `1.3.6.1.2.1.43.5.1.1.16.1` | `printer_std` | `core/collectors/brother.py:132` · `core/collectors/canon.py:179` · `core/collectors/hp.py:179` · `core/enhanced_collector.py:1078` |
| `1.3.6.1.2.1.43.5.1.1.17.1` | `printer_std` | `core/collectors/brother.py:136` · `core/collectors/canon.py:184` · `core/collectors/hp.py:186` · `core/enhanced_collector.py:1092` … |
| `1.3.6.1.2.1.43.6.1.1.3.1.1` | `printer_std` | `core/collectors/brother.py:230` · `core/collectors/canon.py:393` · `core/collectors/hp.py:281` |
| `1.3.6.1.2.1.43.10.2.1.4.1.1` | `printer_std` | `core/collectors/brother.py:141` · `core/collectors/brother.py:148` · `core/collectors/canon.py:201` · `core/collectors/canon.py:285` … |
| `1.3.6.1.2.1.43.10.2.1.4.1.2` | `printer_std` | `core/collectors/brother.py:163` · `core/collectors/canon.py:202` · `core/enhanced_collector.py:597` · `core/enhanced_collector.py:989` |
| `1.3.6.1.2.1.43.10.2.1.4.1.3` | `printer_std` | `core/enhanced_collector.py:598` |
| `1.3.6.1.2.1.43.11.1.1.6.1.1` | `printer_std` | `core/collectors/cartridge_id.py:321` · `core/collectors/cartridge_id.py:334` |

## ۵. یتیم‌ها

این OIDها در **کد غیرمرجع** (routeها، ابزارها، کاوشگرها) خوانده می‌شوند نه در کاتالوگ/collector — فهرست برای این است که معلوم شود کدام‌ها باید به کاتالوگ اضافه شوند:

| OID | ماژول | محل |
|---|---|---|
| `1.3.6.1.2.1.43.11.1.1` | `printer_std` | `tools/diagnose_printer.py:27` |
| `1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1` | `printer_toshiba` | `tools/diagnose_printer.py:285` |
| `1.3.6.1.4.1.2435.2.3.9.1.1.3.1.1.1.1.2.0` | `printer_brother` | `web/routes/discover.py:45` |
| `1.3.6.1.4.1.2435.2.3.9.4.2.1.5.4.5.10.0` | `printer_brother` | `tools/diagnose_printer.py:112` |
| `1.3.6.1.4.1.2435.2.3.9.4.2.1.5.4.5.11.0` | `printer_brother` | `tools/diagnose_printer.py:113` |
| `1.3.6.1.4.1.2435.2.3.9.4.2.1.5.4.5.12.0` | `printer_brother` | `tools/diagnose_printer.py:114` |
| `1.3.6.1.4.1.2435.2.3.9.4.2.1.5.4.5.13.0` | `printer_brother` | `tools/diagnose_printer.py:115` |
| `1.3.6.1.4.1.2435.2.3.9.4.2.1.5.4.5.14.0` | `printer_brother` | `tools/diagnose_printer.py:116` |
| `1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.8.0` | `printer_brother` | `tools/diagnose_printer.py:110` |
| `1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.10.0` | `printer_brother` | `tools/diagnose_printer.py:111` |

## ۶. پیشوندهایی که OID شمرده نشد

| پیشوند | نمونهٔ محل | چرا شمرده نشد |
|---|---|---|
| `172.16.25` | `web/routes/discover.py:76` | یا پیشوند برندیابی است یا «دُم» یک f-string/تابع ⇒ OID کاملِ یک برگ نیست |

از کاتالوگ، **22** OID فقط در `OID_CATALOG` آمده و در collectorها مستقیم نوشته نشده — اینها هم در ماتریس‌اند (کاتالوگ بخشی از قرارداد است).

## ۷. چطور مصرف شود

- `monitoring/generator.yml`: ماژول‌ها = §۲؛ هر ردیف §۳ باید در آن باشد. سنجهٔ پوشش: `tools/check_oid_coverage.py` (G.2) — اگر OIDی در ماتریس بود و در generator نبود، یا برعکس، CI قرمز است.
- نام متریک در generator از ستون «برچسب/کلید» می‌آید؛ اگر برچسب یک کلمهٔ مبهم باشد (نام برند یا کوتاه‌تر از ۶ حرف)، نام ظرف پیشوند می‌شود (`brother` داخل `CANDIDATE_SUBTREES` ⇒ `candidate_subtrees_brother`).
- یتیم‌های §۵ در generator فقط **کامنت**‌اند (probe-only) و گیت مراقب است که متریک زنده نشوند؛ پوششِ یک یتیم با walk زیر درختش مجاز است (std: `43.11.1.1`).
- OID تازه: اول کد (کاتالوگ/collector)، بعد `--write`. سند هیچ‌وقت جلوتر از کد نمی‌رود.
<!-- sha256:b3a1872a39ad · tools/build_oid_matrix.py -->
