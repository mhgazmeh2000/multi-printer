# Grafana Dashboard — Printer Cartridge Monitoring

Pre-built Grafana dashboard for monitoring printer fleet cartridge status, change events, and chip ID quality across HP, Canon, Brother, and Toshiba printers.

## Quick Start (Docker)

```bash
# 1. Start the printer monitor server
cd multi-printer
python run.py   # Runs on port 5050

# 2. Start Grafana with pre-loaded config
docker run -d \
  --name grafana \
  -p 3000:3000 \
  -v $(pwd)/grafana/provisioning/datasources:/etc/grafana/provisioning/datasources \
  -v $(pwd)/grafana/provisioning/dashboards:/etc/grafana/provisioning/dashboards \
  -v $(pwd)/grafana/dashboards:/var/lib/grafana/dashboards \
  grafana/grafana:latest

# 3. Open Grafana
#    URL:      http://localhost:3000
#    Username: admin
#    Password: admin
```

## Manual Setup (Grafana installed)

1. **Add Prometheus datasource:**
   - Go to **Configuration → Data Sources → Add data source**
   - Select **Prometheus**
   - URL: `http://<printer-server>:5050`
   - Click **Save & Test**

2. **Import dashboard:**
   - Go to **Dashboards → Import**
   - Upload `dashboards/cartridge-monitoring.json`
   - Select the Prometheus datasource
   - Click **Import**

## Dashboard Panels

| Row | Panel | Description |
|-----|-------|-------------|
| 🟢 Fleet Overview | Printers Online | Online vs offline count |
| | Total Cartridge Changes | Lifetime change event count |
| | Genuine Chip IDs | Printers with unique chip identifiers |
| | Static / Fallback Chips | Printers using model-based detection |
| | Fleet Printers | Total monitored printers |
| | Chip ID Quality | Quality breakdown (genuine/static/generation) |
| 📊 Toner Levels | Toner Levels Table | Current level per printer/color with gauge cells |
| | Toner Level History | Time series of toner depletion |
| 🔄 Changes | Changes by Printer | Bar chart of changes per printer |
| | Changes by Detection | Chip ID vs supply_name vs toner_jump vs pages_reset |
| | Changes Detail Table | Full breakdown with detection/color |
| | Changes by Type | Donut chart of detection methods |
| 🧩 Chip Quality | Quality Distribution | Donut chart (genuine vs static vs generation) |
| | IDs per Printer | Bar gauge showing color slots with chip IDs |
| | Quality by Brand | Stacked bars by printer name and quality |
| 📈 Page Volume | Pages by Printer | Bar chart of total printed pages |
| | Page Volume Table | Sortable table with page counts |
| ℹ️ Fleet Status | Fleet Status Table | Online/offline status for all printers |

## Available Metrics (Prometheus)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `cartridge_changes_total` | counter | `printer_ip`, `printer_name`, `detection`, `color` | Total cartridge change events |
| `printer_toner_level` | gauge | `printer_ip`, `printer_name`, `color` | Current toner level % |
| `printer_total_pages` | gauge | `printer_ip`, `printer_name` | Total pages printed |
| `printer_online` | gauge | `printer_ip`, `printer_name` | 1=online, 0=offline |
| `printer_cartridge_chip_id_known` | gauge | `printer_ip`, `printer_name`, `color`, `quality` | Chip ID known (1=yes) |
| `scrape_timestamp_seconds` | gauge | — | Last scrape time |

## Sample PromQL Queries

```promql
# Printers that had a cartridge change today
sum by (printer_name) (cartridge_changes_total{detection="chip_id"})

# Low toner alerts (< 15%)
printer_toner_level < 15

# Printers with genuine chip IDs
count by (printer_name) (printer_cartridge_chip_id_known{quality="genuine"})

# Offline printers
printer_online == 0

# Total changes by detection method
sum by (detection) (cartridge_changes_total)
```

## Alert Rules

```yaml
# Prometheus alertmanager rules
groups:
  - name: printer-cartridge
    rules:
      - alert: LowToner
        expr: printer_toner_level < 15
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Low toner on {{ $labels.printer_name }} ({{ $labels.color }})"
          description: "Toner level is {{ $value }}%"

      - alert: CartridgeChanged
        expr: changes(cartridge_changes_total[1h]) > 0
        for: 0m
        labels:
          severity: info
        annotations:
          summary: "Cartridge change on {{ $labels.printer_name }}"
          description: "Detection: {{ $labels.detection }}, Color: {{ $labels.color }}"

      - alert: PrinterOffline
        expr: printer_online == 0
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "Printer {{ $labels.printer_name }} is offline"
```
