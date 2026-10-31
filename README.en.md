# 🖨️ Printer Monitoring (PrintGuard)

> 🇮🇷 **Persian version:** [README.md](README.md)  
> 📘 **Full technical reference:** [docs/technical-reference.en.md](docs/technical-reference.en.md)  
> 🗺️ **Architecture and data-flow diagram:** [docs/architecture.html](docs/architecture.html)  
> 📋 **Database migration runbook:** [docs/cutover-runbook.md](docs/cutover-runbook.md)

An enterprise monitoring and fleet-management system for multi-brand printers (**Toshiba, HP, Canon, Brother**) and **ECS100G** environmental sensors. Data is collected over **SNMP (v1/v2c)** and, where needed, **HTTP web scraping**, and stored in **SQLite** or **PostgreSQL 16**.

This file has two parts:

- **👤 End-user part:** for people who view the dashboard and run reports.
- **🛠️ Developer part:** for people who install, configure, extend, and operate the system.

Every main section has both parts. Detailed technical documentation is in the `docs/` folder.

---

## 📑 Table of Contents

1. Features
2. Architecture Overview
3. Data Flow
4. Project Structure
5. Prerequisites
6. Installation & Setup
7. Environment Configuration
8. API Reference
9. Toner & Consumables
10. Log & Counter Integrity
11. Tools & CLI Utilities
12. Testing & Quality Assurance
13. Enterprise Security
14. Excel Export & Reports
15. Operational Notes
16. License

---

## ✨ Features

### 👤 For end users
- **Online / offline status:** a printer is marked offline only after **two consecutive failed polling cycles**, so a brief network glitch does not cause a false alarm.
- **Device details:** model, serial number, firmware version, and uptime.
- **Print counters:** total, colour, black-and-white, copy, print, fax, and scan, with an A3/A4 paper-size breakdown.
- **Hardware status:** paper trays and their levels, toner, cartridges, drums, and active alerts for each device.
- **Cartridge forecasting:** the system estimates the remaining capacity of each cartridge.
- **Grouping:** create, rename, and delete printer groups.
- **Events:** print activity, alerts, cartridge changes, and significant temperature or humidity changes from sensors.
- **Reports:** daily charts, plus **Excel** and **CSV** exports.
- **Users and access:** three roles (`admin`, `manager`, `viewer`) and access limited to specific office subnets.

### 🛠️ For developers
- Collection and processing logic lives in `core/`; the web layer (Flask, templates, and dashboard JS) lives in `web/`.
- Cartridge estimation is in `core/yield_engine.py`; the database layer is in `core/db/`.
- Roles (`admin` / `manager` / `viewer`) and subnet-scoped access control are implemented in `web/` (see Enterprise Security).
- To add or change brand behaviour, review the relevant modules in `core/` and `core/collectors/`.

---

## 🧱 Architecture Overview

### 👤 For end users
The system has three parts:

1. **Monitoring service:** reads the printers' status **every 60 seconds**.
2. **Database:** keeps the history of status, counters, and events.
3. **Web dashboard:** shows the information in your browser.

### 🛠️ For developers
- **`run.py`:** the entry point. It starts the polling loop in a thread and the Flask server.
- **`core/poller.py`:** `poll_all()` calls `collect(ip)` for each printer.
- **`core/collectors/`:** an optional `exporter_adapter` path (enabled with `USE_SNMP_EXPORTER=1`), the main collector in `core/enhanced_collector.py`, and a web-scraping fallback when data is incomplete.
- **`core/snmp/protocol.py`:** sends SNMP GET requests over UDP/161.
- **`core/collectors/base.py`:** processing: detects genuine counter changes, reconciles the A3/A4 split, and computes toner and Yield Engine values.
- **`core/store.py` and `core/db/`:** an in-memory live store for the API, and persistent storage in SQLite or PostgreSQL.

---

## 🔄 Data Flow

### 👤 For end users
1. The system checks each printer on schedule.
2. The collected data is processed and saved.
3. The dashboard periodically fetches fresh data and displays it.
4. Times are stored in **UTC** and shown in **Tehran time** (`Asia/Tehran`) on the dashboard and in reports.

### 🛠️ For developers
- **Main path:** `run.py` → `polling_loop` (thread) → `core/poller.py:poll_all()` → `collect(ip)` → processing in `core/collectors/base.py` → storage in `core/store.py` and `core/db/`.
- **Exporter path:** with `USE_SNMP_EXPORTER=1`, requests go through `core/exporter_client.py`, which uses **exponential backoff** and a **circuit breaker**. If the data is incomplete, the system falls back to web scraping.
- **Time contract:** the database layer works only in UTC. Conversion to `Asia/Tehran` happens only in the display and report layers (`docs/timezone-contract.md`).

---

## 📁 Project Structure

### 👤 For end users
You do not need to know the folder layout for day-to-day use. Note that `printers.json` holds your printer list and, in SQLite mode, `logs.db` holds the data. Neither file is committed to Git.

### 🛠️ For developers
```text
run.py                  entry point
core/                   collection, processing, and storage
  collectors/           collectors (base, exporter_adapter, ...)
  snmp/                 SNMP protocol layer
  db/                   database layer (SQLite and PostgreSQL)
  yield_engine.py       cartridge capacity estimation
web/                    Flask: routes, templates, and dashboard JS
config/                 settings (settings.py) and example JSON files
alembic/versions/       database schema migrations
tools/                  command-line tools
tests/                  tests (fixtures in tests/fixtures)
monitoring/             snmp_exporter configuration (generator.yml, snmp.yml)
grafana/                Grafana dashboards and provisioning
windows/                Windows scripts (e.g. snmp_exporter installer)
docs/                   technical documentation and runbooks
```

---

## 🔧 Prerequisites

### 👤 For end users
- A modern browser (Chrome, Edge, or Firefox).
- Network access from the monitoring host to the printers: **UDP 161** for SNMP, and **TCP 80/443** for printer web pages where needed.
- A user account on the system. Installation and initial setup are normally handled by the technical team.

### 🛠️ For developers
- **Python** 3.8 or newer (tested on 3.10 and later).
- **UDP 161** for SNMP v1/v2c and **TCP 80/443** for web scraping.
- **SQLite** (default) or **PostgreSQL 16** (for high-traffic deployments).
- Runtime dependencies are in `requirements.txt`; development and test tools are in `requirements-dev.txt`.

---

## 🚀 Installation & Setup

### 👤 For end users
**On Windows:**

1. Open the project folder.
2. For the **first run**, double-click `setup.bat`. It prepares the Python environment, the dependencies, and the initial settings.
3. For everyday use, run `start.bat`. The browser opens automatically.
4. If an error appears, send the error text to the technical team.

**Address:** `http://localhost:5050` (default port). If the technical team has changed the port, use that port instead.

**Sign-in:** the first account created becomes the **admin**. Later accounts are created by an admin.

### 🛠️ For developers
```bash
git clone https://github.com/mhgazmeh2000/multi-printer.git
cd multi-printer
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp printers.example.json printers.json
python run.py
```

- **Address:** `http://localhost:5050` (the port is read from `FLASK_PORT`). Note: if settings fail to load, `run.py` falls back to port **5053**.
- **Create an admin from the CLI:** `python create_admin.py`. If no users exist, the first web sign-up also becomes an admin.
- **Windows:** `setup.bat` (first run) and `start.bat` (everyday use). If `.env` sets `DB_BACKEND=postgresql`, `start.bat` applies the Alembic migrations and calls `tools/auto_migrate.py`. That tool stops if PostgreSQL already contains users or print records.
- **Long-running service:** `run-watchdog.bat` restarts the application after a crash; `stop-project.bat` stops it.

---

## ⚙️ Environment Configuration

### 👤 For end users
- Common settings such as email (SMTP), Google sign-in, and reCAPTCHA are configured **from the admin UI**. You do not need to edit files.
- If the technical team changes the address or port, use the new values.

### 🛠️ For developers

| Variable | Purpose |
|---|---|
| `ENVIRONMENT` | Runtime environment (`production` or `development`) |
| `SECRET_KEY` | Session-signing key. Set a unique value in any real deployment; the default value triggers a warning. |
| `FLASK_PORT` | Web port (default 5050) |
| `DB_BACKEND` | `sqlite` or `postgresql` |
| `PG_DSN` | PostgreSQL connection string |
| `METRICS_TOKEN` | Bearer token for the `/metrics` endpoint |
| `USE_SNMP_EXPORTER` and `SNMP_EXPORTER_URL` | Enable the snmp_exporter path and set its URL |
| `DEFAULT_PRINTERS_JSON` | Path to the default printer list |
| `OFFICE_SUBNET_*` | Office subnet definitions |
| `CARTRIDGE_YIELD_CATALOG` | Path to the cartridge capacity catalogue |
| `CARTRIDGE_YIELD_MODE` | Cartridge type: `local`, `oem`, `compatible`, or `refill` |
| `CARTRIDGE_YIELD_FACTOR` | Capacity adjustment factor |
| `CORS_ALLOWED_ORIGINS` | Allowed origins for CORS |
| `ASSET_VERSION` | Version of static assets (for browser cache busting) |

- SMTP, Google OAuth, and reCAPTCHA are configured in the admin UI or in `config/settings.py`.
- `.env.example` is not included in this repository. Never commit secret values to Git.

---

## 🔌 API Reference

### 👤 For end users
You normally do not need the API; the dashboard uses it. If you need it for automated reporting or to connect another system, coordinate with the technical team.

### 🛠️ For developers
All API routes require authentication. Details are in the technical reference.

- **Status:** `GET /api/status`, `GET /api/printers`, `GET /api/printer/<ip>`
- **Printer management:** `POST /api/printers/add`, `POST /api/printers/bulk-add`, `POST /api/printers/remove`, `POST /api/printers/discover`
- **Groups:** `GET` and `POST /api/groups`, `POST /api/groups/<gid>/rename`, `DELETE /api/groups/<gid>`
- **Statistics and reports:** `GET /api/stats/daily`, `GET /api/stats/sensor/daily`, `GET /api/yield/report`
- **Logs and export:** `GET /api/logs/all`, `GET /api/export/excel`, `GET /api/export/logs`
- **Users:** `GET` and `POST /api/users`, `POST /api/users/<id>/role`, `DELETE /api/users/<id>`
- **Security:** `GET /api/security/events`, `GET /api/security/stats`
- **Technical diagnostics:** `GET /api/debug/printer/<ip>`, `GET /api/debug/brother-toner/<ip>`, `GET /api/debug/toshiba-snmp/<ip>`
- **Metrics:** `GET /metrics` with a Bearer token (`METRICS_TOKEN`)

---

## 📊 Toner & Consumables

### 👤 For end users
- Toner and drum levels are read from the printer. For some brands, the value is taken from the device's web page.
- The system estimates the remaining toner from **actual usage**. The more data it collects, the more accurate the estimate becomes.
- After a cartridge change, a `CARTRIDGE_CHANGED` event is recorded.
- If you replaced toner by hand, you can record a **toner reset** so that page counting starts from that moment.
- The cartridge chip ID is shown on dashboard cards with a 🧩 indicator.

### 🛠️ For developers
- **`core/yield_engine.py`:** learns `yield_per_page` from real consumption history, and uses an **anchor-based** method for low-usage printers.
- **Settings:** `CARTRIDGE_YIELD_CATALOG`, `CARTRIDGE_YIELD_MODE`, and `CARTRIDGE_YIELD_FACTOR` (see Environment Configuration).
- **Manual reset:** `POST /api/printer/<ip>/toner_reset`.
- **Environmental sensors:** a `SENSOR_CHANGE` event is written when temperature changes by at least 1 °C or humidity by at least 5%.

---

## 🧮 Log & Counter Integrity

### 👤 For end users
- The system distinguishes a **genuine print** from a **counter reset or anomaly**, so reports are not distorted by incorrect numbers.
- If a number looks wrong, first check the time range and the events for that printer. If the problem continues, report it to the technical team with the exact time.

### 🛠️ For developers
- **Genuine-change detection:** implemented in the processing pipeline in `core/collectors/base.py`.
- **A3/A4 split:** handled by `attribute_paper_size`, followed by a reconciliation pass.
- **Data-quality check:** `python tools/diagnose_data_quality.py --db logs.db` (exits with code 1 when issues are found).
- **Live validation:** `GET /api/validate/counters` and `GET /api/validate/oids/<ip>`.

---

## 🛠️ Tools & CLI Utilities

### 👤 For end users
Windows batch files:

- `setup.bat`: first-time setup.
- `start.bat`: starts the application.
- `migrate.bat`: backs up `logs.db` and migrates the data to PostgreSQL. **This moves data; run it only on the technical team's instruction.**
- `stop-project.bat`: stops the application.
- `run-watchdog.bat`: restarts the application automatically after a crash.
- `cartridge_test.bat`: a guided test of cartridge-change detection.

### 🛠️ For developers

| Command | Purpose |
|---|---|
| `python tools/diagnose_printer.py <IP>` | Diagnostic report for one printer, written to `diagnose_<IP>.md` |
| `python tools/auto_migrate.py` | Automatic migration to PostgreSQL (empty target only) |
| `python tools/migrate_sqlite_to_pg.py --verify` | Copy SQLite data to PostgreSQL and verify it |
| `python tools/diagnose_data_quality.py --db logs.db` | Database health check |
| `python tools/build_oid_matrix.py --write` | Regenerate `docs/oid-matrix.md` |
| `python tools/compare_snmp_sources.py` | Compare SNMP data sources |
| `python tools/build_snmp_yml.py` | Build `snmp.yml` for snmp_exporter |
| `python tools/verify_server_timezone.py` | Check the server's timezone setting |
| `python create_admin.py` | Create an admin account |

---

## 🧪 Testing & Quality Assurance

### 👤 For end users
The project has an automated test suite so that obvious failures are caught before they reach you. If behaviour looks unusual after an update, report it.

### 🛠️ For developers
```bash
python -m pytest tests/ -v                          # SQLite
PG_DSN=postgresql://... python -m pytest tests/ -v  # PostgreSQL
python -m pytest tests/ -m "not slow" -q            # skip slow tests
```

- **Markers:** `pytest_only`, `sqlite_only`, `pg_live`, and `slow` (defined in `pytest.ini`).
- **Golden tests:** inputs are in `tests/fixtures/golden/*.json`.
- **Warning:** some tests rewrite `docs/oid-matrix.md`. Check `git status` after running the suite.

---

## 🛡️ Enterprise Security

### 👤 For end users
- Each user sees only the printers in the subnets and areas that an admin has assigned to them.
- **Roles:**
  - **admin:** full control of the system.
  - **manager:** manages the printer fleet and reports.
  - **viewer:** read-only access.
- Security events, including sign-ins and access changes, are recorded on the `/security` page.
- Do not share your password.

### 🛠️ For developers
- Passwords are hashed with **bcrypt**.
- **CSRF** protection and **rate limiting** are enabled.
- **Google OAuth** sign-in and **reCAPTCHA** are configurable from the admin UI.
- **RBAC:** roles `admin`, `manager`, and `viewer`, with subnet scoping (`/api/users/<id>/access`).
- The `/metrics` endpoint is protected by `METRICS_TOKEN` (Bearer).
- In any real deployment, change `SECRET_KEY` from its default value.

---

## 📈 Excel Export & Reports

### 👤 For end users
- **Daily report:** charts of printing and usage for each day, on the dashboard.
- **Excel export:** a multi-sheet workbook for analysis in Microsoft Excel.
- **CSV export:** for logs and raw data.
- Dates and times in all reports are shown in **Tehran time**.

### 🛠️ For developers
- `GET /api/export/excel` for the multi-sheet Excel export.
- `GET /api/export/logs` for log export.
- `GET /api/stats/daily` and `GET /api/stats/sensor/daily` for chart data.
- The A3/A4 split is computed in the data layer before reports are built.

---

## ⚠️ Operational Notes

### 👤 For end users
- If a printer shows as offline, first check that it is powered on and connected to the network. If the problem continues, inform the technical team.
- Move the database or change servers only by following the **runbook** (`docs/cutover-runbook.md`).
- Do not delete or edit historical data.

### 🛠️ For developers
- **Production:** set `ENVIRONMENT=production`, use a unique `SECRET_KEY`, and run behind a reverse proxy (`docs/deployment_reverse_proxy.md`). The built-in Flask development server is not suitable for production.
- **PostgreSQL:** `docs/postgresql-setup.md`.
- **snmp_exporter:** `docs/snmp-exporter-setup.md`.
- **Windows services:** `docs/windows-services.md`.
- **Backups:** back up `logs.db` before any migration. `migrate.bat` does this for you.
- **Full reference:** `docs/technical-reference.en.md`.

---

## 📄 License

All rights reserved by the Multi-Printer Development Team. Permitted for organizational deployment, development, and operation under internal enterprise guidelines.
