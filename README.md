# 🖨️ Multi‑Brand Printer Monitor (PrintGuard)

> 🇬🇧 **English version:** [README.en.md](README.en.md)  
> 🗺️ **Interactive architecture and data-flow diagram:** [docs/architecture.html](docs/architecture.html) (with XML export for draw.io)  
> 📋 **Operational runbook for the database migration (Cutover Runbook):** [docs/cutover-runbook.md](docs/cutover-runbook.md)

A comprehensive system for monitoring, analysing and managing multi-brand enterprise printers (**Toshiba, HP, Canon, Brother**) and **ECS100G** environmental sensors. It supports **SNMP (v1/v2c) + HTTP web scraping**, a high-speed **Prometheus SNMP Exporter** pipeline, and a dual database architecture (**SQLite + PostgreSQL 16**).

The system shows online/offline status, per-type print counters (A3/A4, colour/black-and-white, copy/print/scan), toner and drum status, paper trays, events and active alerts, intelligent cartridge capacity forecasting (Yield Engine), detailed logs, analytical outputs, advanced grouping and enterprise role-based security.

---

## 📑 Table of contents

1. [Features](#-features)
2. [Overall architecture and layers](#-overall-project-architecture)
3. [Data flow and pipelines](#-data-flow)
4. [Project structure and file descriptions](#-project-structure)
5. [Prerequisites and installation](#-prerequisites)
6. [Setup and running](#-installation-and-run)
7. [Important environment settings](#-important-environment-settings)
8. [Important APIs](#-important-apis)
9. [Notes on toner and the Yield Engine](#-notes-on-toner-and-consumables)
10. [Log and counter accuracy](#-log-and-counter-accuracy)
11. [Tools and helper scripts (CLI)](#-tools-and-cli-scripts)
12. [Tests and quality assurance](#-tests-and-quality-assurance)
13. [Enterprise security](#-enterprise-security)
14. [Excel export and reports](#-excel-export-and-reports)
15. [Operational notes](#-operational-notes-and-deployment)
16. [License](#-license)

---

## ✨ Features

### 🖨️ Support for multiple enterprise brands
- **Toshiba e‑STUDIO:** per-function counters for multifunction models, A3/A4 paper-size breakdown, paper-tray status, toner reading from SNMP or extraction from the TopAccess web panel.
- **HP LaserJet / FutureSmart / JetDirect:** support for enterprise models (such as M527, E52645, M577), handling of NPCL percentages, sensor alerts and colour breakdown.
- **Canon i‑SENSYS / imageRUNNER / LBP:** support for MF and single-function models (such as LBP233dw), interpretation of discrete toner codes (`0/5/7`) and extraction from the Remote UI web panel.
- **Brother MFC / NC Series:** monitoring of counters, drum and cartridge status.
- **ECS100G:** online monitoring of temperature and humidity in the server room and print hall.

### 💾 Dual database architecture (Dual Backend)
- Native, integrated support for **SQLite** (local development and isolated servers) and **PostgreSQL 16** (production and high-traffic enterprise environments) with no hard dependency on a single database engine.
- A shared data adapter layer (`core/db/`) that keeps queries, parameters and cross-backend UPSERTs consistent.
- Strict adherence to the **UTC time-zone contract** at the database level (`docs/timezone-contract.md`), with conversion to Tehran local time (`Asia/Tehran`) in the display and report layers.
- Formal schema migrations with **Alembic** (`0001` to `0005`), including logical `BOOLEAN` columns, modern `IDENTITY` fields and storage of log details in the high-performance `JSONB` data type.

### 🚀 Prometheus pipeline and resilience (Prometheus & Resilience)
- Standard exposition of metrics on the `/metrics` path, protected by a dedicated authentication token (`METRICS_TOKEN`).
- Optional high-speed hybrid pipeline (`USE_SNMP_EXPORTER=1`) based on `snmp_exporter` for concurrent, low-load collection of SNMP data across the printer fleet.
- A fault-tolerant client (`core/exporter_client.py`) implementing **exponential backoff** and a **circuit breaker**, so that unresponsive printers or an unstable network cannot exhaust server resources or block threads.
- Automatic, intelligent fallback to web scraping (HTTP fallback) when raw SNMP data is incomplete, without code duplication or loss of monitoring coverage.

### 📊 Device monitoring
- **Online / offline** detection with a **minimum of 2 consecutive failed cycles**, so that a momentary network outage does not produce a false offline status.
- Shows the **model, serial number, firmware and uptime**.
- Print counters: total / colour / black-and-white / copy / print / fax / scan.
- **Paper-size breakdown (A3/A4)** with a reconciliation cycle for snapshots (verified by precise simulation).
- Shows the **paper trays** and their levels.
- Shows **toner / cartridge / drum** in unified cards.
- Shows the device's **active alerts**.

### 🎨 Toner and consumables
- Reads toner levels over **SNMP** and, for some brands, by **HTTP scraping** (Toshiba TopAccess / Canon Remote UI).
- Support for RFC 3805 units: percentage (`unit=19`), Canon discrete codes (`0/5/7`), and the `-2/-3` sentinels.
- Support for **manual toner reset** and calculation of **pages since last reset**.
- Intelligent **Yield Engine** (`core/yield_engine.py`):
  - Automatic learning of `yield_per_page` from real consumption history.
  - **Anchor-based learning** method for low-usage printers.
  - Confidence levels `low` / `medium` / `high`.
  - Yield sharing between cartridges of the same model or name.
  - History of snapshots and learning samples.
- **Local cartridge catalogue** (`cartridge_yield_catalog.json` — more than 30 models), used instead of a raw fallback of 2,000 pages.
- Capacity source priority: `auto_learn` → `shared_profile` → `device_capacity` → `catalog` → `default`.
- Shows **dot count / mega dots** on models that actually report this data.

### 📜 Events and logs
- Automatic recording of `PRINT` / `STATUS` / `ALERT` / `REFILL` / `SERVICE` events.
- `CARTRIDGE_CHANGED` — **definitive detection of a cartridge change based on its unique identifier (Chip ID / chip serial)**:
  - **Automatic OID discovery:** on the first successful poll of each printer, the `cartridge_discovery.py` module walks the standard Printer-MIB branch (`prtMarkerSuppliesDescription`, for colour mapping) and the vendor branches of all four brands (HP / Canon / Brother / Toshiba), and extracts serial-like values.
  - **Stability gate:** a candidate is confirmed only when its value stays the same over 3 consecutive probes (dynamic values such as toner level are discarded).
  - Confirmed OIDs are saved automatically in `config/cartridge_id_map.json` (the `learned` section); manual entries you make take priority and are not overwritten.
  - A change of identifier ⇒ an immediate `CARTRIDGE_CHANGED` event with the previous/new identifier and the cartridge colour. Complementary signals: a reset of the "pages printed with this cartridge" counter and (as a fallback) a jump in toner level.
  - The current identifier of each cartridge is shown on the dashboard (summary card and each consumable card) with a 🧩 label.
- `SENSOR_CHANGE` for significant sensor changes (temperature ≥ 1 °C, humidity ≥ 5%).
- Records `paper_size` (`Large (A3/B4)` / `Small (A4/A5)` / `Mixed`) and `paper_split` in the log details.
- Records `poll_timestamp` on `PRINT` events.
- **Protection against false logs:** ignoring the first poll, and detection of `COUNTER_RESET`, `COUNTER_DROP`, `PRINT_OVERFLOW` and `COUNTER_ANOMALY` (details in the section [Log and counter accuracy](#-log-and-counter-accuracy)).

### 👥 Printer grouping
- Create, rename and delete **custom groups** with an icon and colour (`groups.json`).
- A group remains even with no printers in it; the `id` is stable and separate from the display name.
- Default groups based on **office subnets**.

### 📈 Reporting and export
- Daily consumption chart (Chart.js).
- Structured **Excel** export (sheets: Printer Status / Job Log / Reset History).
- **CSV** and **JSON** export.
- **Database import** with analysis, selection of sections, IP/date filtering, duplicate handling and automatic backup.

### 👤 Users and security
- Authentication with **username/password** (bcrypt hashing) and **Google OAuth**.
- Three roles and permissions: `admin` / `manager` / `viewer`.
- Access restriction by **permitted offices/subnets** and **permitted modules**.
- **CSRF protection**, **rate limiting**, reCAPTCHA, and a **security audit log**.
- A user management page and a dedicated security dashboard.

### 🧩 User interface
- **Dark / light** theme, responsive layout, and right-to-left (RTL) Persian support.
- Drag & drop to reorder cards (Sortable.js).
- Tooltips, toast notifications and an accordion for supplementary counters.

---

## 🧱 Overall project architecture

| Layer | Technology / module | Description |
|---|---|---|
| **User interface (Frontend)** | HTML5 / CSS3 / Vanilla JS + Chart.js + Sortable.js (`web/templates`, `web/static`) | Modern, responsive design, mobile-friendly, with light and dark themes |
| **Web server and API** | Flask + Blueprints (`web/`) | Default port **5050** (`FLASK_PORT`) with modular routes |
| **Core engine** | Poller + Collectors + Yield Engine (`core/`) | Background threads, counter reconciliation algorithms and toner-yield learning |
| **Device protocol** | SNMP v1/v2c implemented in-house over **UDP/161** (`core/snmp/`) + HTTP scraping | Direct communication with network devices and extraction of the OID catalogue |
| **Export and monitoring** | Prometheus exposition (`/metrics`) + `snmp_exporter` | High-speed hybrid layer with a backoff / circuit-breaker client |
| **Database (Dual Backend)** | **SQLite** (`logs.db`) or **PostgreSQL 16** via `core/db/` | Formal schema migrations with **Alembic** (`0001` to `0005`) and time locked to UTC |

🗺️ To see the **full interactive flowchart** of how files and data flow relate (with XML export for draw.io), open **[docs/architecture.html](docs/architecture.html)** in your browser.

---

## 🔄 Data flow

### Polling cycle (every 60 seconds — `POLL_INTERVAL`)

```text
run.py ──▶ polling_loop (thread)
   │
   ├─▶ core/poller.py : poll_all()
   │      │  list of printers from core/store.py (printers.json)
   │      ▼
   │   collect(ip) ──▶ first path: core/collectors/exporter_adapter.py (if USE_SNMP_EXPORTER=1)
   │      │                 └─▶ core/exporter_client.py ──▶ Prometheus snmp_exporter
   │      │
   │      ▼            main/direct path: core/enhanced_collector.py
   │                   (Generic MIB-2 + Host Resources + Printer MIB + Vendor OIDs)
   │                      if incomplete/failed ─▶ fallback to the brand collector:
   │                          toshiba.py / hp.py / canon.py / brother.py
   │                          (+ HTTP scraping of toner with _merge_scraped if needed)
   │      ▼
   │   core/snmp/protocol.py ──▶ SNMP GET over UDP/161
   │      │                    (v2c first, fallback to v1; version cache per IP)
   │      ▼
   │   core/collectors/base.py ──▶ processing:
   │      │   • _counters_event: detection of real print vs reset/anomaly
   │      │   • attribute_paper_size: A3/A4 breakdown with reconciliation cycle
   │      │   • toner calculations + Yield Engine (yield_engine.py + catalogue)
   │      ▼
   ├─▶ core/store.py  ──▷ live memory (printer_data) for the APIs
   └─▶ core/db/       ──▷ storage in the database (SQLite or PostgreSQL) with UTC conversion
            │
            ▼
   user's browser ──▶ dashboard.js GETs /api/printers every few seconds
            └──── Web routes read from store/db and return JSON
```

### Background threads (`run.py`)
| Thread | Task and behaviour |
|---|---|
| `poll-init` / `poll-loop` | First immediate poll after boot, then the regular monitoring loop every 60 seconds |
| `weekly-scan` | Weekly OID scan across the network and automatic update of `oid_profiles.json` |
| `cleanup-loop` | Periodic cleanup of old logs and database maintenance (pruning according to the retention policy) |
| Flask (main thread) | Serves the web application and accepts HTTP requests on `0.0.0.0:5050` |

### Other data-flow paths
- **ECS100G sensor** → `core/collectors/sensor.py` → `sensor_readings` table + `SENSOR_CHANGE` event.
- **OID scan** → `core/oid/scanner.py` → builds `oid_profiles.json` + `core/device_classifier.py`.
- **User actions** (add printer, toner reset, manage groups and users) → controllers in `web/routes/` → applied in `store` and the `db` layer.
- **Reporting** → `web/routes/export_bp.py` → generates Excel and CSV files from database tables.
- **Prometheus** → `GET /metrics` with the header `Authorization: Bearer <TOKEN>` → extracts aggregated metrics from the database and live memory.

---

## 📁 Project structure

```text
Multi-Printer/
├── run.py                      # entry point: Flask web server + polling/scan/cleanup threads
├── models.py                   # User authentication model (Flask-Login) with bcrypt validation
├── utils.py                    # helper functions: sending email, password-reset token, reCAPTCHA validation
├── create_admin.py             # CLI tool to create or promote the initial admin user
├── requirements.txt            # full list of Python dependencies (pinned versions)
├── printers.example.json       # standard sample of the printer configuration file
├── cartridge_yield_catalog.json# standard reference catalogue of cartridge capacities (30+ models with local coefficients)
├── groups.json                 # initial structure of custom printer groups
├── start.bat / stop-project.bat# start-up and shut-down automation scripts on Windows
├── alembic.ini                 # Alembic migration configuration file (compatible with Windows)
│
├── alembic/                    # database migrations
│   ├── env.py                  # migration runner script with dynamic DSN extraction
│   └── versions/               # table structure changes
│       ├── 0001_initial_schema.py          # creates the base database schema (11 main tables)
│       ├── 0002_boolean_columns.py         # converts flag columns to standard BOOLEAN
│       ├── 0003_identity_columns.py        # converts key identifiers to GENERATED BY DEFAULT AS IDENTITY
│       ├── 0004_jsonb_columns.py           # converts the details column of the logs table to JSONB
│       └── 0005_add_legacy_log_columns.py  # adds 5 legacy columns for full compatibility
│
├── config/                     # system configuration
│   ├── __init__.py
│   ├── settings.py             # environment variables, file paths, subnets, encryption keys and ...
│   └── cartridge_id_map.json   # manual and automatic mapping of cartridge serial-number OIDs
│
├── core/                       # core processing logic
│   ├── database.py             # bridge for events, counters, users and log recording
│   ├── poller.py               # polling thread management, collector selection, fault-tolerance mechanism
│   ├── store.py                # real-time in-memory cache (printer_data) and last-state storage
│   ├── enhanced_collector.py   # standard combined collector for all brands
│   ├── yield_engine.py         # intelligent cartridge-yield calculation and learning engine
│   ├── groups.py               # management of custom printer groups
│   ├── device_classifier.py    # detects device brand and model from scanned OIDs
│   ├── security_audit.py       # management and recording of system security events
│   ├── time_utils.py           # uniform functions for local/UTC time conversion and format consistency
│   ├── exporter_client.py      # snmp_exporter client with exponential backoff and circuit breaker
│   │
│   ├── db/                     # database abstraction layer (Dual Backend)
│   │   ├── __init__.py         # entry point and general connection functions
│   │   ├── connection.py       # PostgreSQL connection-pool management and safe SQLite connection
│   │   ├── exceptions.py       # unified database exceptions
│   │   ├── placeholders.py     # converts parameter placeholders (?, %s) between SQLite and PG
│   │   ├── schema.py           # table structure and column name definitions
│   │   ├── scripts.py          # runs database DDL scripts
│   │   └── upsert.py           # generates UPSERT statements compatible with both engines
│   │
│   ├── collectors/             # vendor-specific collectors
│   │   ├── base.py             # base calculations, counter validation and A3/A4 breakdown
│   │   ├── base_enhanced.py    # connects the base collectors to the combined engine
│   │   ├── toshiba.py          # dedicated collector for Toshiba e-STUDIO (A3/A4 and TopAccess)
│   │   ├── hp.py               # dedicated collector for HP LaserJet (NPCL percentages and error codes)
│   │   ├── canon.py            # dedicated collector for Canon MF/LBP (discrete codes and Remote UI)
│   │   ├── brother.py          # dedicated collector for Brother MFC/NC (toner and drum)
│   │   ├── sensor.py           # collector for the ECS100G environmental sensor (temperature and humidity)
│   │   ├── cartridge_id.py     # extracts the cartridge chip serial
│   │   ├── cartridge_discovery.py # automatic discovery of cartridge serial-number OIDs
│   │   └── exporter_adapter.py # Prometheus output consumer adapter with fallback to web scraping
│   │
│   ├── snmp/                   # SNMP protocol communication layer
│   │   ├── protocol.py         # lightweight SNMP v1/v2c implementation over a UDP/161 socket, with version cache
│   │   └── oid_map.py          # OID identifier mapping for counters and devices
│   │
│   └── oid/                    # OID catalogue and scanner
│       ├── scanner.py          # scans device OIDs and creates the initial profile
│       ├── catalog.py          # database of standard and vendor-specific OIDs
│       └── validator.py        # validates OID values and records errors
│
├── web/                        # web and display layer
│   ├── __init__.py             # builds the Flask application, registers Blueprints and middleware
│   ├── auth.py                 # access control, user sessions, Google sign-in and decorators
│   ├── security.py             # protective layers: CSRF, rate limiter and security headers
│   ├── routes/                 # modular system controllers
│   │   ├── dashboard.py        # main monitoring page
│   │   ├── printers.py         # printer management, manual toner reset and debugging
│   │   ├── logs.py             # display and filtering of system events, and manual event recording
│   │   ├── metrics.py          # Prometheus metric exposition with a security token
│   │   ├── export_bp.py        # generates structured Excel and CSV reports
│   │   ├── discover.py         # automatic discovery of new devices on the network subnet
│   │   ├── scan.py             # network scan management and OID profiles
│   │   ├── stats.py            # charts and print statistics indicators
│   │   ├── system.py           # server health status and manual monitoring run
│   │   ├── security.py         # security panel and suspicious-event report
│   │   ├── users.py            # user management, roles and permitted offices
│   │   ├── groups.py           # custom group management
│   │   ├── import_db.py        # analyses and imports a database backup
│   │   ├── validation.py       # counter validation and device profile validation
│   │   └── yield_status.py     # Yield Engine reports panel
│   │
│   ├── templates/              # Jinja2 UI templates
│   └── static/                 # static assets (CSS and JS files)
│
├── monitoring/                 # Prometheus monitoring configurations
│   ├── snmp.yml                # compiled OID extraction rules for snmp_exporter
│   ├── generator.yml          # brand module definitions for building snmp.yml
│   └── snmp-manifest.json      # manifest of valid Printguard OIDs
│
├── tools/                      # command-line toolbox (CLI)
│   ├── auto_migrate.py         # automatic, intelligent migration with data-safety checks
│   ├── migrate_sqlite_to_pg.py # complete data migration tool from SQLite to PostgreSQL
│   ├── diagnose_data_quality.py# data-quality validation tool and anomaly detection
│   ├── build_snmp_yml.py       # generates the Prometheus configuration from the project's OIDs
│   ├── compare_snmp_sources.py # compares direct SNMP output with snmp_exporter
│   ├── check_oid_coverage.py   # measures OID coverage
│   ├── diagnose_printer.py     # deep troubleshooting tool for a printer and OID extraction
│   ├── init_pg_database.py     # initial setup of the PostgreSQL user and database
│   └── verify_server_timezone.py# checks that the server time zone matches the system contract
│
├── tests/                      # automated tests and quality assurance
│   ├── conftest.py             # Pytest root configuration with test-schema isolation
│   ├── golden_harness.py       # simulator for running golden scenarios with pre-recorded responses
│   ├── test_golden.py          # Diff-Zero validation test for golden scenarios
│   └── fixtures/               # reference test data
│       ├── golden/             # real recorded data from the printer fleet
│       └── migration_sample/   # migration schema test samples
│
└── docs/                       # architecture documentation, contracts and guides
    ├── architecture.html       # interactive visual diagram of data flow
    ├── cutover-runbook.md      # step-by-step operational runbook for the cutover
    ├── timezone-contract.md    # strict UTC time contract for the database
    └── tech-debt.md            # record of technical debt and engineering decisions
```

### Runtime files (not stored in Git)
`logs.db` (in SQLite mode), `printers.json`, `groups.json`, `oid_profiles.json`, `oid_validation_errors.txt`, `toner_report.txt`, `yield_status_report.txt`, `.env`.

---

## 🔧 Prerequisites

- **Python 3.8+** (recommended and tested version: **3.10+**)
- Local network access to the printers' IP addresses, and port **161/UDP** open (SNMP v1 or v2c).
- Access to the printers' web panels on port **80/TCP** or **443/TCP** (for web scraping on compatible brands).
- Database: a local **SQLite** file (default) or **PostgreSQL 16** (for production traffic).
- Required libraries are installed from `requirements.txt` (including `Flask`, `openpyxl`, `psycopg[binary]`, `alembic`, `bcrypt`, `authlib` and `tzdata`).

---

## 🚀 Installation and run

### 1. Clone the repository and install dependencies
```bash
git clone https://github.com/mhgazmeh2000/multi-printer.git
cd multi-printer

# create and activate the virtual environment
python3 -m venv .venv
# on Linux / macOS:
source .venv/bin/activate
# on Windows:
.venv\Scripts\activate

# install packages
pip install -r requirements.txt
```

### 2. Configure the printers file
Copy the sample file and define the printers on your network:
```bash
# on Linux:
cp printers.example.json printers.json
# on Windows:
copy printers.example.json printers.json
```

### 3. Run from the command line
```bash
python run.py
```
By default, the system is available at **`http://localhost:5050`**.

If no user has been registered in the system, the **first sign-up** through the web automatically gets the `admin` role with `verified` status; alternatively, you can create the admin user directly from the CLI:
```bash
python create_admin.py
```

### 🪟 Quick start on a Windows server (`start.bat`)
On Windows servers, the bundled `start.bat` file carries out all the steps of checking prerequisites, starting up and health-checking automatically:
* **PostgreSQL test mode:** if a `.env` file with test database settings (`DB_BACKEND=postgresql`) is present, the script automatically finds the PostgreSQL service, applies the Alembic migrations, and transfers the data through `tools/auto_migrate.py` with full safety checks.
* **Production mode:** in the production environment there is no test `.env` file, and `start.bat` starts safely with the default database.
* **Safety check before TRUNCATE:** the `auto_migrate.py` tool has a safety guard; if a user exists in the database or the printer data is not empty, the migration is stopped to prevent accidental overwriting.

---

## ⚙️ Important environment settings

System settings are controlled through the `.env` file or system environment variables:

### Security and database
```bash
export ENVIRONMENT=production                         # development | production
export SECRET_KEY=your-strong-random-secret-key-here   # 32-byte random key
export FLASK_PORT=5050                                 # web server port

# database settings (default: sqlite)
export DB_BACKEND=sqlite                              # sqlite | postgresql
export PG_DSN="postgresql://user:pass@127.0.0.1:5432/printguard"
```

### Prometheus monitoring and Exporter
```bash
export METRICS_TOKEN="your-secure-prometheus-bearer-token" # token for the /metrics path
export USE_SNMP_EXPORTER=0                                # enable the snmp_exporter pipeline (1 or 0)
export SNMP_EXPORTER_URL="http://127.0.0.1:9116"          # address of the snmp_exporter service
```

### Default printers (optional)
```bash
export DEFAULT_PRINTERS_JSON='[
  {"ip":"192.168.1.10","name":"Printer-HQ","community":"public"}
]'
```

### Office subnets and automatic grouping
```bash
export OFFICE_SUBNET_IMAMAT=172.16.25
export OFFICE_SUBNET_SOROUSH=172.16.24
export OFFICE_SUBNET_FALESTIN=172.16.0
export OFFICE_SUBNET_ELAHIYE=172.16.32
```

### Cartridge yield settings (Yield Engine)
```bash
export CARTRIDGE_YIELD_CATALOG="cartridge_yield_catalog.json"
export CARTRIDGE_YIELD_MODE=local                      # local | oem | compatible | refill
export CARTRIDGE_YIELD_FACTOR=0.90                     # conservative factor for the Iranian market
```

### Email, OAuth and CORS
```bash
export CORS_ALLOWED_ORIGINS="https://monitor.example.com"
export ASSET_VERSION=20261013-1                        # static-file cache control
# SMTP, Google OAuth and reCAPTCHA settings are in the admin settings panel or config/settings.py
```

---

## 🔌 Important APIs

### System status and monitoring
- `GET /api/status` — system health, number of printers, status of threads and the active database
- `GET /metrics` — standard Prometheus metric exposition (requires the header `Authorization: Bearer <METRICS_TOKEN>`)
- `POST /api/poll/now` — immediately trigger one polling cycle for all printers

### Printers
- `GET /api/printers` — full list of all printers with latest online status, counters and toner
- `GET /api/printer/<ip>` — detailed information and history for one specific printer
- `POST /api/printers/add` · `POST /api/printers/bulk-add` · `POST /api/printers/remove` — manage printers
- `POST /api/printer/<ip>/update` — edit printer details (name, group, IP, etc.)
- `POST /api/printer/<ip>/toner_reset` — record a manual cartridge reset event after a replacement or refill
- `POST /api/printers/discover` · `POST /api/discover/auto-add` — search for and automatically add printers on the network

### Events and logs
- `GET /api/logs/all` — list system logs with paging and filtering
- `GET /api/printer/<ip>/log` — logs for one specific printer
- `POST /api/logs/clear` — clear old logs by time or filter
- `POST /api/events/manual` — manually record an event in the system

### Groups
- `GET /api/groups` · `POST /api/groups` — view and define printer groups
- `POST /api/groups/<gid>/rename` · `DELETE /api/groups/<gid>` — edit and delete a group

### Database import
- `POST /api/import/analyze` — analyse an SQLite backup file before importing data
- `POST /api/import/confirm` — finally apply the backup data, with a date filter, and create a safe backup copy

### Exports and reporting
- `GET /api/export/excel` — download the comprehensive multi-sheet Excel export
- `GET /api/export/logs?format=csv` / `?format=excel` — export logs in various formats

### Scanning and validation
- `POST /api/scan/oids` · `GET /api/scan/oids/<ip>` — scan and read back all OIDs of a printer
- `POST /api/scan/all` · `GET /api/scan/profiles` — manage the scanned device profiles
- `GET /api/validate/counters` · `GET /api/validate/oids/<ip>` — validate the accuracy of counters

### Cartridge yield engine (Yield Engine)
- `GET /api/yield/status` — learning status and confidence level of cartridge yield
- `POST /api/yield/report` — generate a comprehensive yield report for the fleet's cartridges

### Statistics and charts
- `GET /api/stats/daily` — daily and cumulative print consumption statistics
- `GET /api/stats/sensor/daily` — history of temperature and humidity changes from environmental sensors

### User management and security
- `GET /users` · `GET /api/users` · `POST /api/users` — manage system users
- `POST /api/users/<id>/role` · `.../verify` · `.../access` · `DELETE /api/users/<id>` — control roles and office access
- `GET /security` · `GET /api/security/events` · `GET /api/security/stats` — security events panel

---

## 📊 Notes on toner and consumables

### Cartridge capacity catalogue
When the device does not report capacity information, or learning has not yet started, the file `cartridge_yield_catalog.json` provides a realistic value based on the exact cartridge model, instead of a fixed raw value (such as 2,000 pages).

Capacity selection priority:  
**`auto_learn → shared_profile → device_capacity → catalog → default`**

```bash
# set the yield mode (default: local, suitable for the Iranian market and multiple refills)
export CARTRIDGE_YIELD_MODE=local     # local | oem | compatible | refill
export CARTRIDGE_YIELD_FACTOR=0.90    # confidence factor for real print coverage
```

### Behaviour specific to brands and features
- **Canon i‑SENSYS (such as LBP233dw):** these devices return discrete status codes (`0=empty / 5=low / 7=normal`) instead of a percentage. The system maps these values to equivalent display percentages (`0% / 15% / 70%`) with a stated source, so that learning is not corrupted. If necessary, the Remote UI web page is scraped.
- **HP FutureSmart (such as M527 and E52645):** the `MaxCapacity` value is returned as a negative value (`-3` or `-2`), and the `Remaining` field is itself a **percentage** (`unit=19`). The system automatically detects this structure and supports cartridges of the 87A/87X/87Y and 89A/89X/89Y series.
- **Brother:** on some models only "toner present or absent" is reported; in this case the dashboard shows a dash (`—`) to avoid generating assumed and incorrect percentages.
- **RFC 3805 sentinel `-3`:** indicates that the toner reservoir is present but the exact level is not known; this state is correctly recorded as `unknown` rather than `not_supported`.
- **Toshiba e-STUDIO:** in addition to the SNMP protocol, if needed, the TopAccess web panel is scraped for accurate toner and counter information, and the `pages_since_last_reset` indicator is calculated to assess cartridge life.

---

## 🧮 Log and counter accuracy

To ensure that print statistics are honest and free of inflated or incorrect data, the processing function `_counters_event` applies strict rules:

| Monitoring scenario | System behaviour and recorded event |
|---|---|
| **First poll** | Only the base snapshot is recorded; **no print log is produced**. |
| **Hardware counter reset** (sudden drop to zero with zero uptime) | Records a `COUNTER_RESET` event without recording false print statistics. |
| **Small counter drop / soft adjustment** | Records a `COUNTER_DROP` event and resets the baseline value. |
| **Unrealistic counter jump** (more than the physical print capacity in one minute) | Records a `PRINT_OVERFLOW` event and the suspicious print log is rejected. |
| **Counter decrease after a restart** | Records a `COUNTER_ANOMALY` event with full details of the occurrence. |
| **Normal, standard printing** | Records the official `PRINT` event with a precise A3/A4 breakdown and the `poll_timestamp`. |

**Precise paper-size breakdown:** this is done by a differential comparison of the total counters against the per-paper-size counters in the snapshots. If the A3 or A4 counters are updated later than the device's total counter, the flags `a3_lagged` or `a4_lagged` are recorded so that later calculations are not skewed.

---

## 🛠️ Tools and CLI scripts

Standalone tools for management, troubleshooting and migration are provided in the `tools/` folder:

### 1. Deep printer diagnostics tool (`tools/diagnose_printer.py`)
```bash
python tools/diagnose_printer.py 192.168.1.50
```
Saves a raw dump of all system OIDs, counters, trays, consumables status and the printer's web pages to a file named `diagnose_<IP>.md`.

### 2. Data migration and quality check (`tools/`)
- **`tools/auto_migrate.py`:** automatic checking of the database state at start-up, and risk-free data migration when the target database is empty.
- **`tools/migrate_sqlite_to_pg.py`:** very fast transfer of data from SQLite to PostgreSQL, with data-type validation, handling of computed (GENERATED) columns, sequence reset (Sequence Sync) and a final check with `--verify`.
- **`tools/diagnose_data_quality.py`:** checks and validates the health of database data, detects column drift and produces a detailed JSON report.
- **`tools/compare_snmp_sources.py`:** compares live SNMP output with Prometheus Exporter output to confirm a full match.
- **`tools/build_snmp_yml.py`:** automatically generates the `monitoring/snmp.yml` configuration for `snmp_exporter`.
- **`tools/verify_server_timezone.py`:** tests whether the server time zone matches the UTC contract.

### 3. Dedicated browser debug paths
- `GET /api/debug/printer/<ip>` — raw output extracted from the printer
- `GET /api/debug/brother-toner/<ip>` — specialised debug of Brother cartridge sensors
- `GET /api/debug/toshiba-snmp/<ip>` — debug of the Toshiba printer's OID table

---

## 🧪 Tests and quality assurance

The project has more than **850 automated tests** in the `tests/` folder, supporting both databases:

### Running tests on the local database (SQLite)
```bash
python -m pytest tests/ -v
```

### Running tests on the PostgreSQL database
```bash
# setting the PG_DSN variable makes the test fixtures create an isolated test database automatically
PG_DSN="postgresql://postgres:pass@127.0.0.1:5432/printguard_test" python -m pytest tests/ -v
```

### Golden scenarios test
The `tests/test_golden.py` tests compare system behaviour with real recorded scenarios from the printer fleet (`tests/fixtures/golden/*.json`), and guarantee that the calculations do not change under the **Diff-Zero** criterion.

---

## 🛡️ Enterprise security

- **Authentication:** passwords are encrypted with the secure **bcrypt** algorithm; integrated enterprise sign-in is supported with **Google OAuth**.
- **Role-based access control (RBAC):** three access levels (`admin` full access, `manager` manages printers and exports, `viewer` view only).
- **Office/subnet access separation:** permitted offices can be assigned to users to limit their view to the printers of their own branch only.
- **CSRF protection:** mandatory on all state-changing methods (POST/PUT/DELETE) in the front end, through `apiFetch`.
- **Rate limiting:** limits request rates to prevent brute-force attacks on sensitive routes.
- **Security log:** sign-ins, sign-outs, failed attempts and privilege escalations are recorded in the `security_events` table and the `/security` panel.
- **Key management:** a secure `SECRET_KEY` must be set in the production environment through the environment.

---

## 📈 Excel export and reports

The Excel export module produces structured files with separate sheets:
1. **Printer Status:** latest online status, IP address, model, serial number, total counters, consumables and active alerts.
2. **Job Log:** records of individual print jobs performed, with date and time, user (where available), print volume and the A3/A4 share.
3. **Reset History:** exact records of cartridge resets, including reset date, the percentage set, the device's live usage, pages printed since the last reset and an estimate of `yield_per_page`.

---

## ⚠️ Operational notes and deployment

- **Cache busting system:** static files in the front end are controlled by the `ASSET_VERSION` variable, so that after an update the browser cache does not need to be cleared.
- **Deployment behind a reverse proxy:** in production, deploy the project behind **Nginx** or **IIS** with HTTPS enabled (`docs/deployment_reverse_proxy.md`).
- **Debug paths:** in the production environment, access to the debug routes is limited to administrators.
- **Local files:** local database files and configuration keys are not stored in the Git repository and are protected in `.gitignore`.

---

## 📄 License

All rights to this system belong to the Multi-Printer development team. Use, development and deployment are permitted under the organisation's policies.
