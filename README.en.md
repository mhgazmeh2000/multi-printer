# 🖨️ Multi‑Brand Printer Monitor (PrintGuard)

> 🇮🇷 **نسخه فارسی:** [README.md](README.md)  
> 🗺️ **Interactive Architecture & Data-Flow Diagram:** [docs/architecture.html](docs/architecture.html) (exports to draw.io XML)  
> 📋 **Database Cutover Runbook:** [docs/cutover-runbook.md](docs/cutover-runbook.md)

A comprehensive enterprise monitoring, analytics, and fleet management system for multi-brand printers (**Toshiba, HP, Canon, Brother**) and **ECS100G** environmental sensors, powered by **SNMP (v1/v2c) + HTTP Web Scraping**, a high-throughput **Prometheus SNMP Exporter** pipeline, and a resilient **Dual-Backend Database (SQLite + PostgreSQL 16+)** architecture (tested and verified on versions 16.x, 17.5, and 18.1).

Built to track online/offline status, segregated page counters (A3/A4, color/mono, print/copy/scan), toner and drum levels, paper trays, active alerts, intelligent cartridge yield prediction (Yield Engine), granular event logs, analytical exports, printer grouping, and enterprise role-based access control (RBAC).

---

## 📑 Table of Contents

1. [Features](#-features)
2. [Architecture Overview & Layers](#-architecture-overview)
3. [Data Flow & Pipelines](#-data-flow)
4. [Project Structure & File Guide](#-project-structure)
5. [Prerequisites](#-prerequisites)
6. [Installation & Setup](#-installation--setup)
7. [Windows Automation Suite (Setup / Migrate / Start)](#-windows-automation-suite)
8. [PostgreSQL Windows Service & Enterprise Backup](#-postgresql-windows-service--enterprise-backup)
9. [Environment Configuration](#-environment-configuration)
10. [API Reference](#-api-reference)
11. [Toner & Yield Engine](#-toner--consumables)
12. [Log & Counter Integrity](#-log--counter-integrity)
13. [Tools & CLI Utilities](#-tools)
14. [Testing & Quality Assurance](#-testing--quality-assurance)
15. [Roadmap & Phase Status](#-roadmap--phase-status)
16. [Enterprise Security](#-security)
17. [Excel Export & Reports](#-excel-export)
18. [Operational Notes](#-operational-notes)
19. [License](#-license)

---

## ✨ Features

### 🖨️ Multi-Brand Enterprise Support
- **Toshiba e‑STUDIO:** Vendor-specific multi-function counters, A3/A4 paper size differentiation, paper tray monitoring, toner tracking via SNMP or TopAccess web panel scraping.
- **HP LaserJet / FutureSmart / JetDirect:** Enterprise models (e.g. M527, E52645, M577), NPCL percentage handling, hardware alert sensors, and color consumables.
- **Canon i‑SENSYS / imageRUNNER / LBP:** Multi-function and single-function devices (e.g. LBP233dw), discrete toner status code interpretation (`0/5/7`), and Remote UI web scraping.
- **Brother MFC / NC Series:** Real-time counters, drum life, and cartridge status.
- **ECS100G:** Live temperature and humidity monitoring for server rooms and print facilities.

### 💾 Dual-Backend Database Architecture & Production Status
- Native support for both **SQLite** (local development and isolated environments) and **PostgreSQL 16+** (high-concurrency production deployments; tested and validated across PostgreSQL **16.x**, **17.5**, and **18.1**).
- **Completed Production Cutover (Phase F):** Live enterprise deployment running stably under real production load, actively monitoring **27 network devices** (printers and environmental sensors).
- **Cartridge State Completeness:** 100% data integrity with **51/51 rows** migrated and verified across all 26 printers and active color channels.
- Unified database abstraction and adapter layer (`core/db/`) handling parameterization, connection pooling, and cross-backend UPSERT operations.
- Strict adherence to the **UTC Timezone Contract** at the database layer (`docs/timezone-contract.md`) with standardized UTC-Z formatting, accompanied by transparent localization to `Asia/Tehran` for UI and reporting.
- Production-grade schema migrations using **Alembic** (`0001` through `0005`), supporting `BOOLEAN` types, modern `IDENTITY` columns, and high-performance `JSONB` log storage.

### 🚀 Prometheus & SNMP Exporter Pipeline
- Standard metric exposition endpoint at `/metrics` secured with a dedicated Bearer token (`METRICS_TOKEN`).
- Optional high-speed hybrid pipeline via Prometheus `snmp_exporter` (`USE_SNMP_EXPORTER=1`) for low-overhead, concurrent SNMP collection.
- Resilient client (`core/exporter_client.py`) equipped with **Exponential Backoff** and **Circuit Breaker** patterns to protect server resources when devices are powered off or network paths are degraded.
- Automatic, zero-copy HTTP web scraping fallback (`_merge_scraped`) ensuring comprehensive telemetry without code duplication.

### 📊 Device Monitoring
- **Online/Offline detection** backed by a **minimum of 2 consecutive failed cycles** to prevent transient network blips from triggering false offline alerts.
- Device model, serial number, firmware revision, and system uptime.
- Segregated counters: Total / Color / Monochrome / Copy / Print / Fax / Scan.
- **Paper-size split (A3/A4)** using a verified snapshot-matching cycle.
- Paper tray capacities, fill levels, and status warnings.
- Unified toner, drum, and consumable status cards.
- Active device alerts and hardware fault indicators.

### 🎨 Toner & Consumables
- Toner level acquisition via **SNMP** and brand-specific **HTTP scraping** (Toshiba TopAccess / Canon Remote UI).
- RFC 3805 supply unit compliance: percentage (`unit=19`), Canon discrete status codes (`0/5/7`), and sentinel values (`-2/-3`).
- Support for **manual toner reset** tracking with *pages since last reset*.
- Intelligent **Yield Engine** (`core/yield_engine.py`):
  - Adaptive learning of `yield_per_page` based on real historical print volume.
  - **Anchor-based learning** algorithm tailored for low-volume devices.
  - Granular confidence levels: `low` / `medium` / `high`.
  - Profile sharing across same-model and same-part-number cartridges.
  - Snapshot and learning-sample audit trail.
- **Local cartridge catalog** (`cartridge_yield_catalog.json` covering 30+ models) replacing generic 2000-page fallbacks.
- Capacity resolution priority: `auto_learn` → `shared_profile` → `device_capacity` → `catalog` → `default`.
- Dot Count / Mega Dots reporting where supported by hardware.

### 📜 Events & Logs
- Automatic generation of `PRINT`, `STATUS`, `ALERT`, `REFILL`, and `SERVICE` events.
- `CARTRIDGE_CHANGED` — **Definitive cartridge-change detection via unique supply identity (Chip ID / serial)**:
  - **Automatic OID Discovery:** On the first successful poll, `cartridge_discovery.py` walks the standard Printer-MIB supply table (`prtMarkerSuppliesDescription`) and vendor subtrees (HP, Canon, Brother, Toshiba) to extract unique serial signatures.
  - **Stability Gate:** Candidate OIDs are validated only if their value remains identical across 3 consecutive probes (filtering out dynamic values like toner percentages).
  - Confirmed OIDs are saved to `config/cartridge_id_map.json` under `learned`; manual overrides always take precedence and are never overwritten.
  - Identity change triggers an immediate `CARTRIDGE_CHANGED` event recording previous/new IDs and consumable color; secondary signals include supply print count reset and toner jump detection.
  - The active Chip ID is displayed across dashboard cards with a 🧩 indicator.
- `SENSOR_CHANGE` logging for significant environmental shifts (temperature ≥ 1°C, humidity ≥ 5%).
- Detailed `paper_size` (`Large (A3/B4)` / `Small (A4/A5)` / `Mixed`) and `paper_split` recorded in log metadata.
- Precise `poll_timestamp` stored on all `PRINT` events.
- **Protection Against Corrupt Logs:** Suppression of initial poll, detection of `COUNTER_RESET`, `COUNTER_DROP`, `PRINT_OVERFLOW`, and `COUNTER_ANOMALY` (details in [Log Integrity](#-log--counter-integrity)).

### 👥 Printer Grouping
- Create, rename, and delete **custom printer groups** with custom icons and colors (`groups.json`).
- Groups persist independently of printer assignments; stable internal IDs separate from display names.
- Automatic office-based default groups determined by network **subnet**.

### 📈 Analytics & Reporting
- Daily consumption and volume charts (Chart.js).
- Multi-sheet **Excel exports** (Printer Status, Job Log, and Reset History).
- Clean **CSV** and **JSON** exports.
- **Database Import** with schema preflight, selective table import, IP/date filtering, duplicate handling, and automated pre-import backup.

### 👤 Users & Security
- Secure authentication via **username/password** (bcrypt hashing) and **Google OAuth**.
- Role-based access control (RBAC): `admin`, `manager`, `viewer`.
- Granular scoping restricting users to **allowed office subnets** and **allowed modules**.
- Comprehensive **CSRF protection**, **rate limiting**, reCAPTCHA, and a detailed **security audit log**.
- Dedicated user management and security administration dashboards.

### 🧩 User Interface
- Modern **Dark / Light** themes, fully responsive layout, Persian RTL and English LTR support.
- Drag & Drop card reordering (Sortable.js).
- Interactive tooltips, toast notifications, and collapsible accordions for secondary hardware counters.

---

## 🧱 Architecture Overview

| Layer | Technology / Module | Description |
|---|---|---|
| **Frontend (UI)** | HTML5 / CSS3 / Vanilla JS + Chart.js + Sortable.js (`web/templates`, `web/static`) | Lightweight, responsive interface with Dark/Light theme toggle |
| **Web & API Server** | Flask + Modular Blueprints (`web/`) | Listens on port **5050** (`FLASK_PORT`) with RESTful JSON endpoints |
| **Core Engine** | Poller + Collectors + Yield Engine (`core/`) | Background polling threads, snapshot reconciliation, and consumable yield learning |
| **Device Protocol** | In-house SNMP v1/v2c over **UDP/161** (`core/snmp/`) + HTTP Web Scraping | Low-level UDP communication, OID catalog mapping, and web scraper fallbacks |
| **Metrics & Monitoring** | Prometheus Exposition (`/metrics`) + `snmp_exporter` | High-throughput metrics endpoint with Backoff & Circuit Breaker client |
| **Storage (Dual Backend)**| **SQLite** (`logs.db`) or **PostgreSQL 16+** (16.x, 17.5, 18.1) via `core/db/` | Unified adapter, Alembic migrations (`0001`–`0005`), strict UTC enforcement, and 100% cartridge state completeness |

🗺️ For an **interactive architectural diagram** illustrating component interactions and data pipelines (with draw.io XML export), open **[docs/architecture.html](docs/architecture.html)** in a browser.

---

## 🔄 Data Flow

### Polling Pipeline (every 60 s — `POLL_INTERVAL`)

```text
run.py ──▶ polling_loop (thread)
   │
   ├─▶ core/poller.py : poll_all()
   │      │  printer inventory loaded from core/store.py (printers.json)
   │      ▼
   │   collect(ip) ──▶ Path 1: core/collectors/exporter_adapter.py (if USE_SNMP_EXPORTER=1)
   │      │                 └─▶ core/exporter_client.py ──▶ Prometheus snmp_exporter
   │      │
   │      ▼            Primary/Direct Path: core/enhanced_collector.py
   │                   (Generic MIB-2 + Host Resources + Printer MIB + Vendor OIDs)
   │                      if incomplete/failed ─▶ fallback to vendor collector:
   │                          toshiba.py / hp.py / canon.py / brother.py
   │                          (+ HTTP scraping fallback via _merge_scraped)
   │      ▼
   │   core/snmp/protocol.py ──▶ SNMP GET over UDP/161
   │      │                    (v2c first, fallback to v1; per-IP version cache)
   │      ▼
   │   core/collectors/base.py ──▶ Processing pipeline:
   │      │   • _counters_event: verify genuine print vs reset/anomaly
   │      │   • attribute_paper_size: reconcile A3/A4 paper split
   │      │   • toner computation + Yield Engine (yield_engine.py + catalog)
   │      ▼
   ├─▶ core/store.py  ──▷ live in-memory store (printer_data) for web APIs
   └─▶ core/db/       ──▷ persist to database (SQLite or PostgreSQL) with UTC normalization
            │
            ▼
   User Browser ──▶ dashboard.js periodic GET /api/printers
            └──── Web routes read from store/db and return JSON
```

### Background Threads (`run.py`)
| Thread | Responsibility |
|---|---|
| `poll-init` / `poll-loop` | Immediate initial poll on startup, followed by continuous 60-second polling cycles |
| `weekly-scan` | Weekly automated network OID scan to update `oid_profiles.json` |
| `cleanup-loop` | Periodic maintenance and pruning of aged print logs based on retention rules |
| Flask (Main Thread) | Serves web traffic and API endpoints on `0.0.0.0:5050` |

### Auxiliary Data Paths
- **ECS100G Sensors** → `core/collectors/sensor.py` → `sensor_readings` table + `SENSOR_CHANGE` events.
- **OID Discovery** → `core/oid/scanner.py` → builds `oid_profiles.json` + `core/device_classifier.py`.
- **User Actions** (add printer, manual toner reset, group/user updates) → `web/routes/` controllers → written to `store` and `db`.
- **Reporting** → `web/routes/export_bp.py` → generates Excel and CSV files from database tables.
- **Prometheus Scrapes** → `GET /metrics` with `Authorization: Bearer <TOKEN>` → computes aggregate metrics from database and memory.

---

## 📁 Project Structure

```text
Multi-Printer/
├── run.py                      # Application entry point: Flask app + background threads
├── models.py                   # User authentication model (Flask-Login) with bcrypt
├── utils.py                    # Utilities: email dispatch, password reset tokens, reCAPTCHA
├── create_admin.py             # CLI utility to bootstrap or promote the initial admin
├── requirements.txt            # Python dependencies (pinned versions)
├── printers.example.json       # Template printer configuration file
├── cartridge_yield_catalog.json# Reference catalog of cartridge nominal yields (30+ models)
├── groups.json                 # Custom printer groups structure
├── start.bat / stop-project.bat# Windows automation scripts for startup and shutdown
├── alembic.ini                 # Alembic configuration (ASCII-clean for Windows compatibility)
│
├── alembic/                    # Database schema migrations
│   ├── env.py                  # Alembic environment runner with dynamic DSN resolution
│   └── versions/               # Sequential schema migration scripts
│       ├── 0001_initial_schema.py          # Baseline database schema (11 core tables)
│       ├── 0002_boolean_columns.py         # Conversion of flag columns to native BOOLEAN
│       ├── 0003_identity_columns.py        # Conversion of primary keys to IDENTITY columns
│       ├── 0004_jsonb_columns.py           # Conversion of logs.details column to JSONB
│       └── 0005_add_legacy_log_columns.py  # Addition of legacy log columns for backward compatibility
│
├── config/                     # Application configuration
│   ├── __init__.py
│   ├── settings.py             # Environment variables, file paths, subnets, and keys
│   └── cartridge_id_map.json   # Manual and auto-discovered cartridge serial OID mappings
│
├── core/                       # Core engine logic
│   ├── database.py             # High-level bridge for events, counters, users, and logs
│   ├── poller.py               # Polling loop management, collector selection, resilience
│   ├── store.py                # In-memory real-time state store (printer_data)
│   ├── enhanced_collector.py   # Unified hybrid collector for all printer brands
│   ├── yield_engine.py         # Intelligent cartridge yield learning and prediction engine
│   ├── groups.py               # Custom printer groups management
│   ├── device_classifier.py    # Hardware model and brand detection via scanned OIDs
│   ├── security_audit.py       # Security event logging and retrieval
│   ├── time_utils.py           # Uniform UTC/local time conversions and formatters
│   ├── exporter_client.py      # Resilient snmp_exporter client with Backoff & Circuit Breaker
│   │
│   ├── db/                     # Dual-Backend Database Abstraction Layer
│   │   ├── __init__.py         # Public database interfaces and connection factory
│   │   ├── connection.py       # PostgreSQL connection pool and SQLite connection manager
│   │   ├── exceptions.py       # Normalized cross-backend database exceptions
│   │   ├── placeholders.py     # Parameter placeholder adapter (?, %s)
│   │   ├── schema.py           # Table schema definitions and column constants
│   │   ├── scripts.py          # DDL bootstrap runner
│   │   └── upsert.py           # Cross-backend UPSERT query builders
│   │
│   ├── collectors/             # Brand-specific device collectors
│   │   ├── base.py             # Base logic, counter verification, and A3/A4 attribution
│   │   ├── base_enhanced.py    # Bridge to the enhanced hybrid collector
│   │   ├── toshiba.py          # Toshiba e-STUDIO collector (A3/A4 and TopAccess scraping)
│   │   ├── hp.py               # HP LaserJet collector (NPCL percent units and alerts)
│   │   ├── canon.py            # Canon MF/LBP collector (discrete status and Remote UI)
│   │   ├── brother.py          # Brother MFC/NC collector (toner and drum)
│   │   ├── sensor.py           # ECS100G environmental sensor collector
│   │   ├── cartridge_id.py     # Cartridge serial/chip ID extraction logic
│   │   ├── cartridge_discovery.py # Automatic discovery of cartridge serial OIDs
│   │   └── exporter_adapter.py # Prometheus adapter with web scraping fallback
│   │
│   ├── snmp/                   # SNMP protocol implementation
│   │   ├── protocol.py         # Lightweight SNMP v1/v2c UDP socket client with version cache
│   │   └── oid_map.py          # OID catalog mappings for counters, trays, and consumables
│   │
│   └── oid/                    # OID scanner and validator
│       ├── scanner.py          # Network OID scanner and profile generator
│       ├── catalog.py          # Catalog of known vendor and standard OIDs
│       └── validator.py        # OID value validation and error logger
│
├── web/                        # Web and API layer
│   ├── __init__.py             # Flask application factory, blueprints, and middlewares
│   ├── auth.py                 # RBAC decorators, authentication, sessions, and OAuth
│   ├── security.py             # CSRF protection, rate limiting, and security headers
│   ├── routes/                 # Modular route controllers
│   │   ├── dashboard.py        # Primary web monitoring dashboard
│   │   ├── printers.py         # Printer inventory CRUD, manual toner reset, debug routes
│   │   ├── logs.py             # Event log inspection and manual event insertion
│   │   ├── metrics.py          # Prometheus exposition endpoint with Bearer auth
│   │   ├── export_bp.py        # Multi-sheet Excel and CSV reporting endpoints
│   │   ├── discover.py         # Network printer subnet discovery
│   │   ├── scan.py             # OID scanning and profile management
│   │   ├── stats.py            # Aggregate volume statistics and chart data
│   │   ├── system.py           # System health metrics and manual poll triggers
│   │   ├── security.py         # Security audit dashboard and incident feeds
│   │   ├── users.py            # User management, roles, and office permissions
│   │   ├── groups.py           # Custom printer groups API
│   │   ├── import_db.py        # SQLite database import with dry-run analysis
│   │   ├── validation.py       # Counter validation and device profile inspection
│   │   └── yield_status.py     # Yield Engine status and fleet reporting
│   │
│   ├── templates/              # Jinja2 HTML templates
│   └── static/                 # Static web assets (CSS stylesheets and JavaScript modules)
│
├── monitoring/                 # Prometheus configuration and manifests
│   ├── snmp.yml                # Compiled OID extraction rules for snmp_exporter
│   ├── generator.yml          # Module definitions for compiling snmp.yml
│   └── snmp-manifest.json      # Complete manifest of PrintGuard-supported OIDs
│
├── tools/                      # CLI Diagnostic and Migration Tooling
│   ├── auto_migrate.py         # Automated preflight check and idempotent migration runner
│   ├── migrate_sqlite_to_pg.py # High-speed SQLite-to-PostgreSQL migration with sequence synchronization
│   ├── migrate_timestamps_to_utc.py # Timestamp migration to standardized UTC-Z project contract
│   ├── check_cutover_readiness.py   # Comprehensive pre-cutover checklist and verification tool
│   ├── diagnose_data_quality.py# Database health and column data quality inspection tool
│   ├── export_from_pg.py       # Rollback utility: reverse data export from PostgreSQL to SQLite
│   ├── build_snmp_yml.py       # Compiles Prometheus snmp.yml from project OIDs
│   ├── compare_snmp_sources.py # Verifies parity between direct SNMP and snmp_exporter
│   ├── check_oid_coverage.py   # Analyzes OID coverage against device profiles
│   ├── diagnose_printer.py     # Comprehensive SNMP and web dump tool for new printer models
│   ├── init_pg_database.py     # PostgreSQL role and database initialization helper
│   ├── test_pg_connection.py   # Verifies PostgreSQL credentials, permissions, and DDL rights
│   └── verify_server_timezone.py# Server timezone verification against the UTC contract
│
├── tests/                      # Automated test suite (>850 tests)
│   ├── conftest.py             # Pytest root configuration with dynamic schema isolation
│   ├── golden_harness.py       # Deterministic scenario replay harness for golden test profiles
│   ├── test_golden.py          # Zero-diff assertion suite across recorded fleet scenarios
│   └── fixtures/               # Test fixtures and reference data
│       ├── golden/             # Golden test scenarios from production fleet captures
│       └── migration_sample/   # Sample datasets for database migration validation
│
└── docs/                       # Architecture specifications and runbooks
    ├── architecture.html       # Interactive visual architecture and data flow diagram
    ├── cutover-runbook.md      # Step-by-step production cutover operational guide
    ├── timezone-contract.md    # Strict UTC database timezone contract
    └── tech-debt.md            # Technical debt registry and architectural decisions
```

### Windows Automation Suite (Root Scripts)
- **`setup.bat`:** 7-step self-healing Windows provisioning script (checks admin rights, Python, venv, PG tools, dirs).
- **`migrate.bat`:** Fail-safe production migration script with automated backups to `E:\pgbackup` and UTC-Z conversion.
- **`start.bat`:** Production runner with dynamic port selection, conflict PID clearance, and PostgreSQL auto-discovery.

### Runtime Files (Excluded from Git)
`logs.db` (when running SQLite), `printers.json`, `groups.json`, `oid_profiles.json`, `oid_validation_errors.txt`, `toner_report.txt`, `yield_status_report.txt`, `.env`.

---

## 🔧 Prerequisites

- **Python 3.8+** (recommended and tested on **Python 3.10+**).
- Network access to printer IP addresses with **UDP/161** open (SNMP v1 or v2c).
- HTTP access to printer web panels on ports **80/TCP** or **443/TCP** (for brands utilizing web scraping).
- Database: Local **SQLite** (default) or **PostgreSQL 16+** (tested and verified on **16.x**, **17.5**, and **18.1**).
- Packages installed via `requirements.txt` (includes `Flask`, `openpyxl`, `psycopg[binary]`, `alembic`, `bcrypt`, `authlib`, and `tzdata`).

---

## 🚀 Installation & Setup

### 1. Clone Repository & Install Dependencies
```bash
git clone https://github.com/mhgazmeh2000/multi-printer.git
cd multi-printer

# Create and activate a virtual environment
python3 -m venv .venv
# Linux / macOS:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

# Install required packages
pip install -r requirements.txt
```

### 2. Configure Printers
Copy the example configuration and define your fleet:
```bash
# Linux / macOS:
cp printers.example.json printers.json
# Windows:
copy printers.example.json printers.json
```

### 3. Launch the Application
```bash
python run.py
```
By default, the web interface is accessible at **`http://localhost:5050`**.

If no users exist in the database, the **first registered user** is automatically granted the `admin` role with `verified` status; alternatively, bootstrap an admin user directly via the CLI:
```bash
python create_admin.py
```

### 🪟 Windows Automation Suite

To ensure smooth operation, zero-touch deployments, and foolproof operations on Windows Server environments, PrintGuard includes three dedicated batch scripts:

#### 1. Self-Healing Setup Script (`setup.bat` — 7 Steps)
The `setup.bat` script inspects and prepares the entire host environment in seven sequential phases:
1. **Administrator Privileges Check:** Verifies elevated permissions to configure services and directory paths.
2. **Python Environment Discovery:** Identifies a valid Python installation (Python 3.10+ recommended) and executable path.
3. **Virtual Environment Self-Healing (`.venv`):** Automatically initializes or repairs a corrupted virtual environment if directories were moved.
4. **Dependency Installation & Updates:** Silently installs packages from `requirements.txt` and ensures `pip` is current.
5. **PostgreSQL Client & Service Discovery:** Detects client binaries (`psql`, `pg_dump`, `pg_restore`) and checks for active PostgreSQL Windows services.
6. **Database Schema Readiness:** Inspects and applies official Alembic schema migrations (`alembic upgrade head`).
7. **Directory & Configuration Provisioning:** Prepares enterprise backup folders (such as `E:\pgbackup`), log directories, and runtime files.

#### 2. Fail-Safe Database Cutover & Migration (`migrate.bat`)
The `migrate.bat` script executes a rock-solid, verifiable transition from SQLite to PostgreSQL:
- **Preflight Sanity Inspection:** Executes `tools/check_cutover_readiness.py` to ensure all connectivity and permission prerequisites are met.
- **Automated Dual-Sided Backups to `E:\pgbackup`:** Prior to any schema or data changes, creates a point-in-time snapshot of `logs.db` and an enterprise custom-format dump (`pg_dump -Fc`) of PostgreSQL, verifying SHA256 hashes.
- **Timestamp Standardization to UTC-Z:** Executes `tools/migrate_timestamps_to_utc.py` to systematically convert all legacy timestamps to strict ISO-8601 UTC with the trailing `Z` designator.
- **Alembic Schema Upgrade:** Applies official schema migrations through revision `0005`.
- **High-Throughput Data Migration & Verification:** Runs `tools/migrate_sqlite_to_pg.py --verify` with type safety enforcement, GENERATED column handling, sequence synchronization, and complete row-count verification.

#### 3. Daily Production Runner (`start.bat`)
The `start.bat` script launches and monitors the application with resilient failure-recovery:
- **UNC & Parentheses Path Resilience:** Uses `pushd` to map script directories to temporary drive letters, eliminating batch syntax issues with parentheses or network paths.
- **Multi-Tier Python Discovery:** Scans via priority: local `.venv` -> `py` launcher -> system `PATH` -> standard AppData directories for Python 3.10 through 3.14.
- **Cache Eviction:** Automatically purges `__pycache__` directories and stale `*.pyc` files.
- **Interactive Port Prompt & Conflict Clearance:** Prompts for target port (defaults to `5050`, configurable to `5053` or other ports in production), scans active listeners via `netstat -aon`, and forcefully terminates conflicting processes via `taskkill /F /PID`.
- **Dynamic PostgreSQL Service Recovery:** Auto-detects running PostgreSQL services across all versions (`postgresql-x64-16`, `postgresql-x64-17`, `postgresql-x64-18`); provides clear service start instructions (`net start <service>`) if stopped.
- **Safety Preflight (`auto_migrate.py`):** Protects live data by refusing destructive operations if the destination database already contains operational data.
- **Automated Browser Launch:** Automatically opens the PrintGuard monitoring dashboard upon successful startup.

---

## 🐘 PostgreSQL Windows Service & Enterprise Backup

### 1. Registering & Managing PostgreSQL as a Windows Service
In enterprise Windows Server deployments, PostgreSQL can be registered as a managed service with automatic recovery:

```cmd
:: Register PostgreSQL service using pg_ctl (Run as Administrator)
"C:\Program Files\PostgreSQL\17\bin\pg_ctl.exe" register -N "postgresql-x64-17" -D "C:\Program Files\PostgreSQL\17\data" -w

:: Configure Automatic Startup on system boot
sc.exe config "postgresql-x64-17" start= auto

:: Configure Auto-Recovery on service failure
sc.exe failure "postgresql-x64-17" reset= 86400 actions= restart/60000/restart/60000/restart/60000

:: Control and query service status
net start postgresql-x64-17
sc query postgresql-x64-17
net stop postgresql-x64-17
```

### 2. Enterprise Backup & Disaster Recovery (`E:\pgbackup`)
Scheduled backups are taken using PostgreSQL's compressed custom format (`pg_dump -Fc`), targeted to the dedicated backup storage path (`E:\pgbackup`):

#### A. Generating an Enterprise Backup (`pg_dump -Fc`):
```powershell
# PowerShell with administrative access
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$backupFile = "E:\pgbackup\printguard_$timestamp.dump"
$env:PGPASSWORD = "your_secure_db_password"

pg_dump -Fc -h localhost -p 5432 -U printguard -f $backupFile printguard
Write-Host "Backup successfully created at: $backupFile"
```

#### B. Verifying Backup Archive Integrity (`pg_restore -l`):
```powershell
# Validate the Table of Contents (TOC) to ensure the archive is complete and uncorrupted
pg_restore -l $backupFile | Measure-Object -Line
# Confirms 740+ tables, sequences, and indexes are intact
```

#### C. Disaster Recovery & Restoration:
```powershell
$env:PGPASSWORD = "your_secure_db_password"
# Restore database schema and data, dropping and recreating existing objects cleanly
pg_restore --clean --if-exists -h localhost -p 5432 -U printguard -d printguard "E:\pgbackup\printguard_live_cutover.dump"
```

---

## ⚙️ Environment Configuration

System configuration is managed via `.env` files or system environment variables:

### Security & Database
```bash
export ENVIRONMENT=production                         # development | production
export SECRET_KEY=your-strong-random-secret-key-here   # 32-byte secure random string
export FLASK_PORT=5050                                 # Web server port

# Database backend selection (default: sqlite)
export DB_BACKEND=sqlite                              # sqlite | postgresql
export PG_DSN="postgresql://user:pass@127.0.0.1:5432/printguard"
```

### Prometheus & Exporter Configuration
```bash
export METRICS_TOKEN="your-secure-prometheus-bearer-token" # Token for /metrics endpoint
export USE_SNMP_EXPORTER=0                                # Enable snmp_exporter pipeline (1 or 0)
export SNMP_EXPORTER_URL="http://127.0.0.1:9116"          # Prometheus snmp_exporter address
```

### Default Printers (Optional)
```bash
export DEFAULT_PRINTERS_JSON='[
  {"ip":"192.168.1.10","name":"Printer-HQ","community":"public"}
]'
```

### Office Subnets & Auto-Grouping
```bash
export OFFICE_SUBNET_IMAMAT=172.16.25
export OFFICE_SUBNET_SOROUSH=172.16.24
export OFFICE_SUBNET_FALESTIN=172.16.0
export OFFICE_SUBNET_ELAHIYE=172.16.32
```

### Consumables & Yield Engine
```bash
export CARTRIDGE_YIELD_CATALOG="cartridge_yield_catalog.json"
export CARTRIDGE_YIELD_MODE=local                      # local | oem | compatible | refill
export CARTRIDGE_YIELD_FACTOR=0.90                     # Conservative coverage factor
```

### Email, OAuth, and CORS
```bash
export CORS_ALLOWED_ORIGINS="https://monitor.example.com"
export ASSET_VERSION=20261013-1                        # Static asset cache-busting key
# SMTP settings, Google OAuth, and reCAPTCHA can be configured via Admin UI or config/settings.py
```

---

## 🔌 API Reference

### System Health & Monitoring
- `GET /api/status` — System health, active printer counts, thread liveness, and database backend info
- `GET /metrics` — Prometheus metrics exposition (requires `Authorization: Bearer <METRICS_TOKEN>` header)
- `POST /api/poll/now` — Triggers an immediate polling cycle across all configured printers

### Printer Inventory
- `GET /api/printers` — Full list of printers with online status, counters, toner levels, and alerts
- `GET /api/printer/<ip>` — Detailed telemetry and history for a specific printer
- `POST /api/printers/add` · `POST /api/printers/bulk-add` · `POST /api/printers/remove` — Inventory CRUD
- `POST /api/printer/<ip>/update` — Update printer metadata (display name, group, IP, etc.)
- `POST /api/printer/<ip>/toner_reset` — Record a manual consumable reset event after replacement
- `POST /api/printers/discover` · `POST /api/discover/auto-add` — Subnet scanner and auto-discovery

### Logs & Events
- `GET /api/logs/all` — Paginated and filterable system event log feed
- `GET /api/printer/<ip>/log` — Filtered event log stream for a specific printer
- `POST /api/logs/clear` — Prune aged log entries based on timeframe or criteria
- `POST /api/events/manual` — Manually insert an operational event record

### Printer Groups
- `GET /api/groups` · `POST /api/groups` — Inspect and create custom printer groups
- `POST /api/groups/<gid>/rename` · `DELETE /api/groups/<gid>` — Rename or delete groups

### Database Import
- `POST /api/import/analyze` — Preflight analysis of a backup SQLite database prior to ingestion
- `POST /api/import/confirm` — Commit backup import with date filtering and automated snapshot creation

### Export & Reporting
- `GET /api/export/excel` — Download comprehensive multi-sheet Excel report
- `GET /api/export/logs?format=csv` / `?format=excel` — Export event logs in CSV or Excel formats

### Scanning & Validation
- `POST /api/scan/oids` · `GET /api/scan/oids/<ip>` — Deep-scan all device OIDs
- `POST /api/scan/all` · `GET /api/scan/profiles` — Manage scanned device profile catalogs
- `GET /api/validate/counters` · `GET /api/validate/oids/<ip>` — Validate hardware counter integrity

### Yield Engine
- `GET /api/yield/status` — Inspection of cartridge learning state, samples, and confidence tiers
- `POST /api/yield/report` — Generate full fleet consumable yield analysis report

### Statistics
- `GET /api/stats/daily` — Aggregate daily volume trends and printable breakdown
- `GET /api/stats/sensor/daily` — Historical temperature and humidity telemetry

### User & Security Management
- `GET /users` · `GET /api/users` · `POST /api/users` — User administration
- `POST /api/users/<id>/role` · `.../verify` · `.../access` · `DELETE /api/users/<id>` — Permissions and subnet scoping
- `GET /security` · `GET /api/security/events` · `GET /api/security/stats` — Security incident feed and audit logs

---

## 📊 Toner & Consumables

### Cartridge Capacity Catalog
`cartridge_yield_catalog.json` provides realistic nominal capacity figures for over 30 cartridge models before historical learning completes, avoiding inaccurate generic defaults (such as 2,000 pages).

Capacity resolution hierarchy:  
**`auto_learn → shared_profile → device_capacity → catalog → default`**

```bash
# Configure consumable yield mode (default: local for refill/compatible market)
export CARTRIDGE_YIELD_MODE=local     # local | oem | compatible | refill
export CARTRIDGE_YIELD_FACTOR=0.90    # Safety factor accounting for page coverage
```

### Brand-Specific Hardware Behaviors
- **Canon i‑SENSYS (e.g. LBP233dw):** Returns discrete status codes (`0=Empty / 5=Low / 7=OK`) rather than percentages. The system translates these into representative display values (`0% / 15% / 70%`) tagged with the `canon_status_code` source to preserve yield learning. Remote UI web scraping is used when detailed levels are available.
- **HP FutureSmart (e.g. M527 and E52645):** Returns negative `MaxCapacity` sentinels (`-3` or `-2`) while `Remaining` reflects the actual **percentage** (`unit=19`). The system recognizes this pattern and provides native definitions for 87A/87X/87Y and 89A/89X/89Y cartridges.
- **Brother:** Certain models report only binary presence/absence of toner; in this state, the UI renders an em-dash (`—`) rather than inventing false percentage estimates.
- **RFC 3805 Sentinel `-3`:** Indicates that a consumable container exists but remaining capacity cannot be quantified; properly recorded as `unknown` rather than `not_supported`.
- **Toshiba e-STUDIO:** Scrapes TopAccess when SNMP supplies are incomplete and maintains the `pages_since_last_reset` metric to monitor real cartridge longevity.

---

## 🧮 Log & Counter Integrity

To ensure that print accounting remains accurate and immune to corrupted data, the `_counters_event` processing pipeline enforces strict validation rules:

| Condition | System Behavior & Event Logged |
|---|---|
| **First Poll of Device** | Baseline snapshot established; **no synthetic PRINT event is emitted**. |
| **Hardware Counter Reset** (sudden drop to 0 with zero uptime) | Emits `COUNTER_RESET`; suppresses bogus negative/positive print delta. |
| **Minor Counter Drift / Soft Reset** | Emits `COUNTER_DROP` and re-anchors the baseline. |
| **Unrealistic Print Surge** (exceeds physical engine speed per interval) | Rejected as `PRINT_OVERFLOW` to prevent corrupted surges. |
| **Counter Anomaly after Reboot** | Emits `COUNTER_ANOMALY` documenting lost pages during power cycle. |
| **Normal Production Print** | Emits official `PRINT` event with accurate A3/A4 split and `poll_timestamp`. |

**Paper Size Attribution:** Computed via differential analysis between total engine counters and paper-size specific counters across snapshot cycles. If A3 or A4 hardware counters update with latency relative to the master counter, `a3_lagged` or `a4_lagged` flags are recorded to preserve audit integrity.

---

## 🛠️ Tools & CLI Utilities

The `tools/` directory contains standalone utilities for administration, diagnostics, and migration:

### 1. Deep Printer Diagnostics (`tools/diagnose_printer.py`)
```bash
python tools/diagnose_printer.py 192.168.1.50
```
Dumps raw SNMP trees (system, counters, trays, supplies) and fetches web administrative panels, saving the output to `diagnose_<IP>.md` for rapid profiling of new printer hardware.

### 2. Migration & Data Quality Tooling (`tools/`)
- **`tools/auto_migrate.py`:** Automated startup migration runner with built-in safety guards that prevents data overwrite on non-empty databases.
- **`tools/migrate_sqlite_to_pg.py`:** High-speed SQLite-to-PostgreSQL migration utility with type coercion, GENERATED column handling, sequence synchronization, and post-migration `--verify`.
- **`tools/migrate_timestamps_to_utc.py`:** Converts and normalizes all historical database timestamps to standardized UTC with trailing `Z` designator per the project contract, featuring `--dry-run` and automated backups.
- **`tools/check_cutover_readiness.py`:** Automated pre-cutover checklist verification runner (exit code 0 for fully ready, 1 for blocking errors, 2 for warnings).
- **`tools/diagnose_data_quality.py`:** Scans database records for type anomalies, column shifts, and data corruption, outputting a comprehensive JSON diagnostic report.
- **`tools/export_from_pg.py`:** Rollback utility exporting PostgreSQL data back to a clean SQLite database.
- **`tools/init_pg_database.py`:** Initializes PostgreSQL role, database, UTF8 encoding, and `C` collation settings.
- **`tools/test_pg_connection.py`:** Validates PostgreSQL connectivity, DDL/DML capabilities, and user permission boundaries.
- **`tools/verify_server_timezone.py`:** Asserts server host and database timezone compliance with the UTC contract.
- **`tools/build_snmp_yml.py`:** Generates `monitoring/snmp.yml` for Prometheus `snmp_exporter`.
- **`tools/compare_snmp_sources.py`:** Compares live SNMP walk output against Prometheus Exporter telemetry to ensure complete parity.
- **`tools/check_oid_coverage.py`:** Analyzes OID coverage against device profiles.
- **`tools/probe_cartridge_id.py`:** Field probe for cartridge unique identifiers and chip serial numbers.
- **`tools/audit_pages.py`:** Auditing utility for reconciling printed page counts across time windows.

### 3. In-Browser Diagnostic Endpoints
- `GET /api/debug/printer/<ip>` — Raw extracted state for a specific printer
- `GET /api/debug/brother-toner/<ip>` — Specialized telemetry for Brother consumables
- `GET /api/debug/toshiba-snmp/<ip>` — Complete OID map dump for Toshiba devices

---

## 🧪 Testing & Quality Assurance

The codebase includes an extensive automated test suite of **over 850 tests** covering both SQLite and PostgreSQL backends:

### Running Tests on SQLite
```bash
python -m pytest tests/ -v
```

### Running Tests on PostgreSQL
```bash
PG_DSN="postgresql://postgres:pass@127.0.0.1:5432/printguard_test" python -m pytest tests/ -v
```

### Golden Scenario Verification
`tests/test_golden.py` replays production hardware snapshots (`tests/fixtures/golden/*.json`) through the processing pipeline, guaranteeing **Zero-Diff** stability across counter reconciliation, paper splitting, and cartridge replacement detection.

---

## 🗺️ Roadmap & Phase Status

| Phase | Milestone | Status | Description |
|---|---|---|---|
| **Phases A–D** | Multi-brand monitoring, enhanced collector, Yield Engine, chip UID detection | ✅ Completed | Stable operation in production across multi-brand printer fleet |
| **Phase E** | Dual Backend architecture (SQLite + PostgreSQL) and UTC Timezone Contract | ✅ Completed | Database adapter layer, Alembic migrations 0001 through 0005 |
| **Phase F** | Production Cutover & Data Migration to PostgreSQL | ✅ Completed | Enterprise cutover completed, 27 network devices monitored, 51/51 cartridge states verified, automated backups at `E:\pgbackup` |
| **Phase G** | Prometheus snmp_exporter pipeline and resilient client | ✅ Completed | Hybrid exporter adapter, Circuit Breaker, Exponential Backoff, Windows service installer |
| **Phase H** | Collector refinement and scraper decoupling | ✅ Completed | Scraper isolation, delegated merging, test standardization |
| **Phase I** | Time-Series Retention, Rollups, Toshiba Yield Engine & TD-006 | ✅ Completed | Alembic 0006 migration, data retention & rollup engines, background scheduler, 6 Toshiba color yield improvements, Asia/Tehran quiet hours fix, and Dual-Backend test promotion |

---

## 🛡️ Enterprise Security

- **Authentication:** Passwords hashed with **bcrypt**; enterprise Single Sign-On via **Google OAuth**.
- **Role-Based Access Control (RBAC):** Three distinct tiers (`admin` for full governance, `manager` for fleet operation and reporting, `viewer` for read-only visibility).
- **Subnet-Scoped Permissions:** Ability to bind users to specific office subnets, restricting their operational view strictly to their local fleet.
- **CSRF Defense:** Enforced on all mutating HTTP methods (POST, PUT, DELETE) via frontend `apiFetch`.
- **Rate Limiting:** Protects sensitive routes (login, registration, diagnostic endpoints) against brute-force attacks.
- **Audit Logging:** Comprehensive tracking of logins, logouts, permission elevations, and failed authentication attempts in the `security_events` table and `/security` panel.
- **Credential Safety:** Enforces strict validation of `SECRET_KEY` in production environments.

---

## 📈 Excel Export & Reports

The export module produces structured multi-sheet workbooks:
1. **Printer Status:** Real-time online state, IP, model, serial, total meters, consumable health, and active alerts.
2. **Job Log:** Historical log of print jobs with timestamps, user context (when available), volume, and A3/A4 breakdown.
3. **Reset History:** Detailed log of consumable reset actions, including reset timestamp, target percentage, engine meter reading, pages printed since reset, and estimated `yield_per_page`.

---

## ⚠️ Operational Notes

- **Cache Busting:** Web assets are versioned using `ASSET_VERSION` to ensure seamless client updates without requiring manual browser cache flushes.
- **Reverse Proxy Deployment:** In production, deploy behind **Nginx** or **IIS** terminating HTTPS (`docs/deployment_reverse_proxy.md`).
- **Diagnostic Protection:** Access to `/api/debug/*` routes is strictly restricted to authenticated administrators in production.
- **Runtime Isolation:** Database files, logs, and sensitive credentials are excluded from Git tracking via `.gitignore`.

---

## 📄 License

All rights reserved by the Multi-Printer Development Team. Permitted for organizational deployment, development, and operation under internal enterprise guidelines.
