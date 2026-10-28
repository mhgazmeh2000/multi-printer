#!/usr/bin/env python3
# =============================================================================
# @file:         run.py
# @brief:        Entry point of the monitoring application (starts the web server and the poller).
# @details:      Responsibilities: Initialise the app, start background loops and handle shutdown
#                signals.
#                Main API: signal_handler(), cleanup_loop(), clear_terminal_loop(), main()
#                Side effects: network I/O, starts threads
# @dependencies: internal: config, core, core.database, core.oid.scanner, core.poller, web
# @notes:        Stop with Ctrl+C; the signal handler stops the background loops. Reads
#                environment variables: FLASK_DEBUG, FLASK_PORT, LOG_LEVEL. Can be run directly as
#                a script.
# =============================================================================

"""
Entry point of the Multi Printer Monitoring program
Run: python run.py (from inside the pm2/ folder)
"""

import logging
import os
import platform
import signal
import socket
import sys
import threading
import time

# Ensure that the root of the project is in sys.path
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config import settings as _settings

# Allow overriding Flask port via environment variable `FLASK_PORT`.
# Keep default in config/settings.py but let start scripts set FLASK_PORT in env.
try:
    DEFAULT_FLASK_PORT = int(_settings.FLASK_PORT)
except Exception:
    DEFAULT_FLASK_PORT = 5053

FLASK_PORT = int(os.getenv("FLASK_PORT", DEFAULT_FLASK_PORT))
from core.database import init_db
from core import store
from core.poller import poll_all, polling_loop
from core.oid.scanner import startup_scan_all, weekly_scan_loop
from web import create_app

# Initial logging settings for the entry point
log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_name, logging.INFO)
logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("PrinterMonitor")

# ─── graceful shutdown event ───
stop_event = threading.Event()
shutdown_in_progress = False

def signal_handler(sig, frame):
    """Handle SIGINT and SIGTERM by starting an orderly shutdown."""

    global shutdown_in_progress
    if shutdown_in_progress:
        return  # If shutdown is running, do not call again
    shutdown_in_progress = True
    log.info("\n🛑 دریافت سیگنال توقف – در حال بستن نرم برنامه...")
    stop_event.set()
    # Clean exit: SystemExit causes Flask to unwind and daemon threads to close.
    # Previous os._exit(0) bypassed cleanup/flush.
    raise SystemExit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ─── Automatic cleaning function of old logs (every 24 hours) ───
def cleanup_loop():
    """
    A loop that runs once every 24 hours.
    According to user request, PRINT logs are never deleted (lifetime).
    This loop exists only to preserve the previous structure and does not perform cleanup operations.
    """
    while not stop_event.is_set():
        try:
            # Requirement: PRINT logs to be lifetime → no purge
            # Other types can be added here if needed to be cleared in the future
            pass
        except Exception as e:
            log.exception("Cleanup loop error: %s", e)
        # Wait 24 hours or until you receive a stop signal
        for _ in range(86400):
            if stop_event.is_set():
                break
            time.sleep(1)

# ─── automatic terminal cleaning function (every 24 hours) ───
def clear_terminal_loop():
    """
    It clears the terminal screen every 24 hours.
    It only clears the console output and does not affect the log files.
    """
    # Operating system detection to select the appropriate command
    if platform.system() == "Windows":
        clear_cmd = "cls"
    else:
        clear_cmd = "clear"

    while not stop_event.is_set():
        try:
            # Wait 24 hours
            for _ in range(86400):
                if stop_event.is_set():
                    break
                time.sleep(1)
            if not stop_event.is_set():
                os.system(clear_cmd)
                log.info("🧹 صفحه ترمینال پاک شد (پاکسازی خودکار هر 24 ساعت)")
        except Exception as e:
            log.exception("Terminal clear loop error: %s", e)

def main():
    # DB setup
    """Start the web server and the background loops of the application."""

    try:
        init_db()
    except Exception as exc:
        # Temporarily locking SQLite on the share should not prevent the web interface from coming up.
        # Subsequent database operations will log their appropriate error with timeout/retry.
        log.exception("Database initialization deferred because the database is unavailable: %s", exc)

    # Flask app
    app = create_app()

    # Display the initial state
    def _get_local_ip():
        """Return the local IP address used for outbound traffic, or localhost on failure."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            try:
                return socket.gethostbyname(socket.gethostname())
            except Exception:
                return "127.0.0.1"

    host_ip = _get_local_ip()
    log.info("""
╔══════════════════════════════════════════════════════╗
║    Multi Printer Monitoring  |  پایش لحظه‌ای        ║
╠══════════════════════════════════════════════════════╣""")
    with store.printers_lock:
        for p in store.PRINTERS:
            log.info(f"  🖨  {p['name']:<14} {p['ip']}  (community: {p['community']})")
    log.info(f"╠══════════════════════════════════════════════════════╣\n  Local   → http://localhost:{FLASK_PORT}/\n  Network → http://{host_ip}:{FLASK_PORT}/\n╚══════════════════════════════════════════════════════╝")

    # Startup OID Scan (background)
    with store.printers_lock:
        printers_copy = list(store.PRINTERS)

    threading.Thread(
        target=lambda: startup_scan_all(printers_copy, force=True),
        daemon=True, name="startup-scan",
    ).start()

    # Polling (infinite loops with stop_event support)
    threading.Thread(target=poll_all,     daemon=True, name="poll-init").start()
    threading.Thread(target=polling_loop, daemon=True, name="poll-loop").start()

    # Weekly scan
    threading.Thread(
        target=weekly_scan_loop,
        args=(lambda: list(store.PRINTERS),),
        daemon=True, name="weekly-scan",
    ).start()

    # Automatic cleaning of old PRINT logs (every 24 hours) - without deleting PRINTs
    threading.Thread(target=cleanup_loop, daemon=True, name="cleanup-loop").start()

    # Automatic cleaning of the terminal every 24 hours
    threading.Thread(target=clear_terminal_loop, daemon=True, name="clear-terminal").start()

    # Flask server
    debug_mode = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=debug_mode, use_reloader=False)


if __name__ == "__main__":
    main()