#!/usr/bin/env python3
"""
Live web dashboard for the MySQL health recorder.

Serves:
    /                -> dashboard HTML (Chart.js, auto-refreshes every 5s)
    /api/data        -> JSON: latest live.json sample + recent CSV history
    /api/processlist -> JSON: current long-running queries + processlist counts

Run:  python3 dashboard.py   (binds to 127.0.0.1:5000 by default)
"""

import csv
import json
import os

from flask import Flask, jsonify, render_template

import config

app = Flask(__name__)

NUMERIC_COLUMNS = {
    "cpu_pct", "load1", "load5", "load15",
    "ram_used_mb", "ram_free_mb", "swap_used_mb",
    "disk_read_mbps", "disk_write_mbps",
    "threads_connected", "threads_running", "max_used_connections", "max_connections",
    "questions_per_sec", "queries_per_sec", "slow_queries_total", "slow_queries_delta",
    "bytes_received_kbs", "bytes_sent_kbs",
    "buffer_pool_used_mb", "buffer_pool_free_mb", "buffer_pool_dirty_pages",
    "created_tmp_tables", "created_tmp_disk_tables", "tmp_disk_table_pct",
    "row_lock_waits", "row_lock_time_ms",
    "proc_sleeping", "proc_running", "proc_locked",
}


def read_live():
    path = os.path.join(config.DATA_DIR, "live.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def csv_path_for(live):
    """Pick the CSV matching the monitor's current file (by live timestamp)."""
    if live and live.get("timestamp"):
        day = live["timestamp"][:10]
        p = os.path.join(config.DATA_DIR, f"metrics-{day}.csv")
        if os.path.exists(p):
            return p
    today = __import__("datetime").date.today().isoformat()
    p = os.path.join(config.DATA_DIR, f"metrics-{today}.csv")
    return p if os.path.exists(p) else None


def read_history(path, limit):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            out = {}
            for k, v in r.items():
                if k is None:          # extra columns beyond the CSV header →
                    continue           # guard against header/schema mismatch
                out[k] = float(v) if k in NUMERIC_COLUMNS and v not in ("", None) else v
            rows.append(out)
    return rows[-limit:]


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/data")
def api_data():
    live = read_live()
    path = csv_path_for(live)
    history = read_history(path, config.DASHBOARD_HISTORY_ROWS) if path else []
    return jsonify({
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "live": live,
        "history": history,
        "metric_file": os.path.basename(path) if path else None,
    })


@app.route("/api/processlist")
def api_processlist():
    live = read_live()
    return jsonify({
        "long_queries": (live or {}).get("long_queries", []),
        "counts": (live or {}).get("processlist_counts", {}),
        "alarm": (live or {}).get("alarm", False),
        "threshold": (live or {}).get("threshold", config.THREADS_RUNNING_THRESHOLD),
    })


if __name__ == "__main__":
    import datetime
    host = config.DASHBOARD_HOST
    port = config.DASHBOARD_PORT
    print(f"Dashboard: http://{host}:{port}  (data dir: {config.DATA_DIR})")
    app.run(host=host, port=port, debug=False, threaded=True)
