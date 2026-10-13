# 🖨️ Multi‑Brand Printer Monitor (PrintGuard)

> 🇮🇷 **نسخه فارسی:** [README.md](README.md)  
> 🗺️ **Interactive architecture & data-flow diagram:** [docs/architecture.html](docs/architecture.html) (exports to draw.io XML)  
> 📋 **PostgreSQL Cutover Runbook:** [docs/cutover-runbook.md](docs/cutover-runbook.md)

An enterprise-grade monitoring, analytics, and management platform for multi-brand printers (**Toshiba, HP, Canon, Brother**) and **ECS100G** environmental sensors using **SNMP (v1/v2c) + HTTP Web Scraping** and a high-performance **Prometheus SNMP Exporter** hybrid pipeline.

PrintGuard tracks real-time online/offline states, granular print counters (A3/A4 split, color/mono), toner and drum levels, device alerts, machine-learned cartridge capacity (Yield Engine), audit logs, Excel/CSV exports, custom printer groups, and role-based access security.

---

## 📑 Table of Contents

1. [Key Features](#-key-features)
2. [Architecture & System Layers](#-architecture--system-layers)
3. [Data Flow & Monitoring Pipelines](#-data-flow--monitoring-pipelines)
4. [Project Structure](#-project-structure)
5. [Prerequisites & Installation](#-prerequisites--installation)
6. [Environment Configuration](#-environment-configuration)
7. [API Reference](#-api-reference)
8. [Toner & Yield Engine](#-toner--yield-engine)
9. [Log & Counter Integrity](#-log--counter-integrity)
10. [Command-Line Tools (CLI)](#-command-line-tools-cli)
11. [Testing & Quality Assurance](#-testing--quality-assurance)
12. [Security](#-security)

---

## ✨ Key Features

### 🖨️ Multi-Brand Enterprise Support
- **Toshiba e‑STUDIO:** Vendor-specific counters, A3/A4 attribution, paper tray status, toner level extraction via SNMP or TopAccess web scraping.
- **HP LaserJet / FutureSmart / JetDirect:** Enterprise models (e.g. M527, E52645, M577), NPCL percentage handling, sensor alerts, and color supplies tracking.
- **Canon i‑SENSYS / imageRUNNER / LBP:** MF and single-function devices (e.g. LBP233dw), discrete status code mapping (`0/5/7`), and Remote UI scraping.
- **Brother MFC / NC Series:** Counters, drum life, and cartridge status tracking.
- **ECS100G:** Real-time temperature and humidity monitoring for server rooms and print facilities.

### 💾 Dual-Backend Storage Architecture
- Native support for both **SQLite** (local development and isolated unit testing) and **PostgreSQL 16+** (high-concurrency production).
- Unified database connection adapter layer (`core/db/`) with session-level timezone enforcement locked strictly to **UTC**.
- Production-ready schema migration chain with **Alembic** (`0001` through `0005`), supporting `JSONB` log payloads and native PostgreSQL `IDENTITY` auto-incrementing sequences.

### 🚀 Prometheus & Resilience Pipeline
- Standard metrics exposition on `/metrics` with dedicated bearer token security (`METRICS_TOKEN`).
- Optional high-throughput hybrid collection pipeline (`USE_SNMP_EXPORTER=1`) based on Prometheus `snmp_exporter`.
- Built-in resilience engineering with **Exponential Backoff** and **Circuit Breaker** patterns in `core/exporter_client.py` to protect against network flooding on offline devices.
- Zero-copy HTTP scraping fallback when SNMP metrics are missing or incomplete.

### 📊 Accurate Tracking & Diagnostics
- Multi-cycle offline detection requiring **at least 2 consecutive failed polling cycles** to eliminate transient network flapping.
- Granular counters: Total / Color / Monochrome / Copy / Print / Fax / Scan.
- **Deterministic paper-size attribution (A3/A4 split)** verified through snapshot-matching cycles.
- Active alert codes, paper tray capacities, and toner levels.

### 🧩 Smart Cartridge Yield Engine
- Unique cartridge replacement detection (`CARTRIDGE_CHANGED`) via Chip ID / Serial Number discovery.
- Adaptive learning of `yield_per_page` based on historical printing patterns.
- Comprehensive local catalog (`cartridge_yield_catalog.json`) with prioritized capacity resolution:
  `auto_learn → shared_profile → device_capacity → catalog → default`.

### 👥 Access Control & Reporting
- Robust authentication with **bcrypt**, **Google OAuth**, and 3-tier role authorization (`admin` / `manager` / `viewer`).
- Granular permissions based on **office subnets** and **allowed modules**.
- Multi-sheet **Excel** export (Printer Status, Job Logs, Reset History) and **CSV** reports.

---

## 🧱 Architecture & System Layers

| Layer | Component / Tech | Details |
|---|---|---|
| **User Interface** | HTML5 / CSS3 / Vanilla JS + Chart.js + Sortable.js | Responsive, Persian RTL, Dark/Light theme support |
| **Web & API** | Flask + Blueprints (`web/`) | Default port: **5050** (`FLASK_PORT`) |
| **Core Monitoring** | Poller + Collectors + Yield Engine (`core/`) | Multi-threaded polling loops, state tracking |
| **Protocols** | SNMP v1/v2c (`core/snmp/`) + HTTP Web Scraping | Native UDP socket transport + web panel scraping |
| **Metrics Pipeline**| Prometheus `/metrics` + `snmp_exporter` | High-throughput collector with Circuit Breaker |
| **Database** | **SQLite** (`logs.db`) or **PostgreSQL 16+** | Abstracted through `core/db/` adapter with **Alembic** migrations |

🗺️ Explore the **interactive data-flow and architecture diagram** in **[docs/architecture.html](docs/architecture.html)**.

---

## 🔄 Data Flow & Monitoring Pipelines

### Polling Pipeline (Every 60s — `POLL_INTERVAL`)

```text
run.py (Main Application Entry)
   │
   ├─▶ core/poller.py : polling_loop()
   │      │
   │      ├─ [Default Mode: USE_SNMP_EXPORTER=0]
   │      │     └─▶ core/enhanced_collector.py (Direct SNMP over UDP/161)
   │      │           └─▶ Fallback to brand collectors (HP, Toshiba, Canon, Brother)
   │      │
   │      └─ [Exporter Mode: USE_SNMP_EXPORTER=1]
   │            └─▶ core/collectors/exporter_adapter.py
   │                  ├─▶ core/exporter_client.py ──▶ snmp_exporter (HTTP)
   │                  │     (Guarded by Circuit Breaker & Exponential Backoff)
   │                  └─▶ If SNMP Incomplete ──▶ HTTP Scraping Fallback
   │
   ├─▶ core/collectors/base.py (Event processing, A3/A4 split, counter validation)
   │
   ├─▶ core/yield_engine.py (Yield calculations, learning updates, Chip ID detection)
   │
   ├─▶ core/store.py (In-memory live cache of device states for dashboard)
   │
   └─▶ core/db/ (Transactional persistence of logs, stats, and security events)
```

### Background Worker Threads (`run.py`)
| Thread | Responsibility |
|---|---|
| `poll-init` / `poll-loop` | Immediate startup poll followed by periodic 60-second polling cycle |
| `startup-scan` / `weekly-scan` | Discovers new OIDs, checks firmware changes, updates device profiles |
| `cleanup-loop` | Enforces historical log retention policies |
| `clear-terminal` | Daily console refresh for server operator legibility |

---

## 📁 Project Structure

```text
multi-printer/
├── run.py                          # Application entry point: Flask + background threads
├── models.py                       # User models (Flask-Login) & authentication logic
├── create_admin.py                 # CLI utility to create or promote an admin account
├── alembic.ini                     # Alembic database migration config (ASCII-only)
├── alembic/                        # Migration version scripts (0001 to 0005)
│   └── versions/
│       ├── 0001_initial_schema.py          # Baseline cross-engine schema
│       ├── 0002_boolean_columns.py         # Boolean column transformations
│       ├── 0003_identity_columns.py        # BIGSERIAL to IDENTITY migrations
│       ├── 0004_jsonb_columns.py           # logs.details JSONB conversion
│       └── 0005_add_legacy_log_columns.py   # Production logs schema alignment
├── config/
│   ├── settings.py                 # Core configuration: ports, subnets, thresholds
│   └── cartridge_id_map.json       # OID-to-color and chip serial mapping table
├── core/
│   ├── db/                         # Database abstraction adapter (SQLite & PostgreSQL)
│   │   ├── connection.py           # Unified connections with strict UTC enforcement
│   │   └── exceptions.py           # Normalized database exception classes
│   ├── poller.py                   # Polling scheduler and offline resilience logic
│   ├── store.py                    # In-memory printer states and PrevStore
│   ├── enhanced_collector.py       # Primary multi-brand SNMP collector
│   ├── exporter_client.py          # snmp_exporter HTTP client with Circuit Breaker
│   ├── yield_engine.py             # Capacity learning and cartridge catalog engine
│   ├── collectors/                 # Dedicated brand modules (HP, Canon, Toshiba, Brother)
│   │   └── exporter_adapter.py     # Prometheus adapter with zero-copy scraping fallback
│   ├── snmp/                       # Low-level native SNMP v1/v2c packet handler
│   └── oid/                        # Automatic network OID scanner and validator
├── web/
│   ├── __init__.py                 # Flask factory, blueprint registration, CSRF setup
│   ├── auth.py                     # Authentication routes, session guards, Google OAuth
│   ├── security.py                 # CSRF protection, rate limiting, and security headers
│   ├── routes/                     # Blueprint routes (dashboard, printers, logs, metrics)
│   ├── templates/                  # Jinja2 templates (dashboard, users, security, login)
│   └── static/                     # Frontend styles, client JS, Chart.js, Sortable.js
├── tools/                          # Command-line tools (CLI)
│   ├── migrate_sqlite_to_pg.py     # Full SQLite-to-PostgreSQL cutover migration tool
│   ├── diagnose_data_quality.py    # Independent SQLite data quality diagnosis tool
│   ├── migrate_timestamps_to_utc.py# Timestamp UTC-Z converter with audit manifests
│   ├── check_cutover_readiness.py  # 10-point preflight cutover readiness verifier
│   ├── generate_cutover_fixture.py # Synthetic 30MB SQLite fixture generator for cutover
│   ├── build_snmp_yml.py           # Compiles generator.yml into monitoring snmp.yml
│   └── diagnose_printer.py         # Diagnostic utility to dump raw SNMP and web data
└── docs/                           # Official technical docs and architecture reports
    ├── cutover-runbook.md          # Step-by-step PostgreSQL cutover runbook
    ├── hybrid-path-report.md       # Full-path Prometheus hybrid evaluation report
    ├── phase-h-decisions.md        # Architectural decision records for exporter pipeline
    └── tech-debt.md                # Living technical debt catalog (TD-001 to TD-023)
```

---

## 🔧 Prerequisites & Installation

### Requirements
- **Python:** **3.10+** (tested on Python 3.10 through 3.13)
- **Database (choose one):**
  - **SQLite:** Built-in, zero setup required.
  - **PostgreSQL 16+:** Recommended for production deployments.
- **Network Access:** Port **161/UDP** open towards target printers.

### Step-by-Step Installation

```bash
# 1. Clone repository
git clone https://github.com/mhgazmeh2000/multi-printer.git
cd multi-printer

# 2. Set up virtual environment
python3 -m venv .venv
source .venv/bin/activate       # On Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt   # Optional: for testing and CLI tools

# 4. Apply database migrations (Required for PostgreSQL)
python -m alembic upgrade head

# 5. Create administrator user
python create_admin.py

# 6. Start the server
python run.py
```

The application is now accessible at **`http://localhost:5050`**.

### 🪟 Windows Quick Start (`start.bat`)
On Windows servers, `start.bat` automates environment verification, dependencies, port checks, migrations, and server startup:
* **Test Machine:**
  To test on PostgreSQL, create a `.env` file in the project root:
  ```env
  DB_BACKEND=postgresql
  PG_DSN=postgresql://postgres:123456@127.0.0.1:5432/printguard_test
  ```
  `start.bat` dynamically discovers the running PostgreSQL Windows service, applies Alembic migrations, runs `tools/auto_migrate.py` with preflight safety checks, and starts Flask.
* **Production Machine:**
  In production, `.env` **must NOT** have `DB_BACKEND=postgresql` prior to scheduled cutover (keep `DB_BACKEND=sqlite` or omit `.env`). `start.bat` defaults to SQLite (`logs.db`).
* **Pre-TRUNCATE Safety Verification in `auto_migrate.py`:**
  If the target PostgreSQL database already contains users (`users > 0`), migration is skipped (Idempotent). If users count is 0, all tables are inspected: if real data exists in any non-seed table, migration aborts immediately with an error, preventing any TRUNCATE. Only if the DB is clean or contains only seed rows, seed tables are truncated and full verified migration runs.

---

## ⚙️ Environment Configuration

Set these in your `.env` file or environment variables:

### Database & Storage
```bash
# Backend engine: sqlite (default) or postgresql
export DB_BACKEND=postgresql

# PostgreSQL connection DSN (when DB_BACKEND=postgresql)
export PG_DSN="postgresql://printguard:strong-password@127.0.0.1:5432/printguard"

# SQLite path (when DB_BACKEND=sqlite)
export DB_PATH="logs.db"
```

### Web Server & Security
```bash
export ENVIRONMENT=production
export SECRET_KEY="generate-a-strong-secret-key"
export FLASK_PORT=5050
export CORS_ALLOWED_ORIGINS="https://monitor.example.com"
```

### Prometheus & Exporter Pipeline
```bash
# Enable Prometheus SNMP Exporter pipeline (default: 0)
export USE_SNMP_EXPORTER=0

# Address of running snmp_exporter service
export SNMP_EXPORTER_URL="http://127.0.0.1:9116"

# Bearer token to protect /metrics endpoint (recommended for production)
export METRICS_TOKEN="your-secure-metrics-token"
```

### Subnets & Office Groups
```bash
export OFFICE_SUBNET_IMAMAT=172.16.25
export OFFICE_SUBNET_SOROUSH=172.16.24
export OFFICE_SUBNET_FALESTIN=172.16.0
export OFFICE_SUBNET_ELAHIYE=172.16.32
```

---

## 🔌 API Reference

### Prometheus Metrics
- **`GET /metrics`:** Prometheus exposition format endpoint (protected by `METRICS_TOKEN` via `Authorization: Bearer <token>`).

### System Status & Polling
- **`GET /api/status`:** Server health status, poller state, and error counters.
- **`POST /api/poll/now`:** Triggers an immediate polling cycle for all devices.

### Device Management
- **`GET /api/printers`:** List of all monitored devices with online status and counters.
- **`GET /api/printer/<ip>`:** Detailed state of a specific device.
- **`POST /api/printers/add` / `remove` / `update`:** Printer CRUD operations.
- **`POST /api/printer/<ip>/toner_reset`:** Manual supply reset record.
- **`POST /api/printers/discover`:** Subnet scan for printer discovery.

### Logs & Events
- **`GET /api/logs/all?limit=100`:** Paginated event logs.
- **`GET /api/printer/<ip>/log`:** Filtered logs for a single device.
- **`POST /api/events/manual`:** Logs manual maintenance or paper refilling.

### Reporting
- **`GET /api/export/excel`:** Comprehensive multi-sheet workbook (Status, Logs, Resets).
- **`GET /api/export/logs?format=csv`:** Raw CSV event export.

### Database Migration & Import
- **`POST /api/import/analyze`:** Analyzes an uploaded SQLite backup (works cross-backend).
- **`POST /api/import/confirm`:** Direct database write on SQLite (retired on PostgreSQL for data safety; use `tools/migrate_sqlite_to_pg.py` instead).

---

## 📊 Toner & Yield Engine

### Local Cartridge Catalog
`cartridge_yield_catalog.json` covers 30+ cartridge models to provide realistic initial capacities before adaptive learning finishes.
```bash
# Configure market capacity mode: local | oem | compatible | refill
export CARTRIDGE_YIELD_MODE=local
export CARTRIDGE_YIELD_FACTOR=0.90
```

### Chip ID Cartridge Change Detection
`cartridge_discovery.py` scans standard Printer-MIB and vendor subtrees on the first successful poll. A 3-cycle stability gate verifies serial consistency and persists the identity to `config/cartridge_id_map.json`. When a supply is physically replaced, the Chip ID changes, triggering a `CARTRIDGE_CHANGED` event.

---

## 🧮 Log & Counter Integrity

To prevent counter corruption from device reboots or rollovers, `core/collectors/base.py` enforces:
* **First Poll Baseline:** Sets the baseline without creating fabricated `PRINT` events.
* **Sharp Drop with Reboot:** Logged as `COUNTER_RESET` without false page counts.
* **Extreme Jump:** Blocked as `PRINT_OVERFLOW` if page counts exceed physical engine limits.
* **Minor Drift after Reboot:** Flagged as `COUNTER_ANOMALY`.

---

## 🛠️ Command-Line Tools (CLI)

The `tools/` directory includes specialized CLI utilities:

### 1. SQLite to PostgreSQL Migration (`migrate_sqlite_to_pg.py`)
```bash
# Dry run verification
python tools/migrate_sqlite_to_pg.py --source logs.db --dry-run

# Full cutover with deep verification
python tools/migrate_sqlite_to_pg.py --source logs.db --target "$PG_DSN" --force --verify
```
* **Features:** Intelligent `COPY` / `BATCH` selection, PostgreSQL `IDENTITY` sequence synchronization, preflight data quality validation, and read-only enforcement on the SQLite source.

### 2. Data Quality Diagnosis Tool (`diagnose_data_quality.py`)
Scans SQLite databases before cutover to detect corrupted types, shifted data from recovery, or missing cartridge rows:
```bash
python tools/diagnose_data_quality.py --db logs.db --output logs.db.diagnosis.json
```

### 3. Timestamp UTC-Z Standardizer (`migrate_timestamps_to_utc.py`)
Converts legacy timestamps to ISO-8601 UTC with trailing `Z` and outputs timestamped audit manifests:
```bash
python tools/migrate_timestamps_to_utc.py --db logs.db --apply
```

### 4. Cutover Readiness Checker (`check_cutover_readiness.py`)
Runs 10 automated preflight checks (Python, psycopg, port, PostgreSQL service, Alembic head, disk space):
```bash
python tools/check_cutover_readiness.py
```

### 5. Printer SNMP/Web Dump (`diagnose_printer.py`)
Dumps raw OIDs and HTML pages from an unknown device to facilitate driver development:
```bash
python tools/diagnose_printer.py <PRINTER_IP>
```

---

## 🧪 Testing & Quality Assurance

PrintGuard features a comprehensive dual-backend test suite:

```bash
# Run tests against SQLite (default)
PYTHONPATH=. pytest -q
# Result: 774 passed, 74 skipped

# Run tests against real PostgreSQL
PG_DSN="host=127.0.0.1 port=5432 dbname=printguard user=printguard password=***" PYTHONPATH=. pytest -q
# Result: 848 passed, 0 skipped

# Run golden scenario regression harness
PYTHONPATH=. pytest tests/test_golden.py tests/test_hybrid_full_path.py
# Result: 13/13 passed (diff-zero)
```

---

## 🛡️ Security

- **Encrypted Passwords:** Passwords hashed with **bcrypt**; state-changing endpoints protected by CSRF tokens.
- **Rate Limiting:** Prevents brute-force attacks on login and sensitive endpoints.
- **Audit Logging:** Security events (logins, privilege escalations, locks) tracked in `/security`.
- **Environment Isolation:** Sensitive credentials read exclusively from environment variables; passwords masked in logs.

---

## 📄 License

Internal enterprise printer monitoring and management software. All rights reserved.
