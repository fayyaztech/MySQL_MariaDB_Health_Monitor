#!/usr/bin/env python3
"""
Offline graph generation from the collected CSV data.

Produces, into graphs/:
  * *.png  — matplotlib charts of each metric group
  * report.html — a self-contained interactive report (Chart.js, data inlined)
                  that opens in any browser without a server.

Usage:
    python3 generate_graphs.py            # use today's CSV
    python3 generate_graphs.py --days 7   # include the last 7 days
"""

import argparse
import csv
import datetime
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import dates as mdates

import config

NUMERIC = {
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

# Metric groups: filename, title, y-label, series (key, label, color)
SPECS = [
    ("cpu_load",     "CPU & load",      "% / load", [
        ("cpu_pct", "CPU %", "#e74c3c"), ("load1", "Load 1m", "#f39c12"),
        ("load5", "Load 5m", "#f1c40f"), ("load15", "Load 15m", "#7f8c8d")]),
    ("ram",          "Memory",          "MB", [
        ("ram_used_mb", "RAM used", "#3498db"), ("ram_free_mb", "RAM free", "#2ecc71"),
        ("swap_used_mb", "Swap used", "#9b59b6")]),
    ("connections",  "Connections",     "count", [
        ("threads_connected", "Connected", "#3498db"), ("threads_running", "Running", "#e74c3c"),
        ("max_used_connections", "Max used", "#95a5a6")]),
    ("queries",      "Queries",         "per second", [
        ("queries_per_sec", "Queries/s", "#3498db"), ("questions_per_sec", "Questions/s", "#e67e22")]),
    ("slow_queries", "Slow queries",    "count", [
        ("slow_queries_delta", "Slow delta", "#e74c3c"), ("slow_queries_total", "Slow total", "#95a5a6")]),
    ("buffer_pool",  "InnoDB buffer pool", "MB", [
        ("buffer_pool_used_mb", "Used", "#3498db"), ("buffer_pool_free_mb", "Free", "#2ecc71")]),
    ("tmp_tables",   "Temporary tables", "count", [
        ("created_tmp_disk_tables", "Disk tmp tables", "#e74c3c"),
        ("created_tmp_tables", "All tmp tables", "#95a5a6")]),
    ("locks",        "Row locks",       "count", [
        ("row_lock_waits", "Lock waits", "#e74c3c"), ("row_lock_time_ms", "Lock time (ms)", "#f39c12")]),
    ("network",      "Network I/O",     "KB/s", [
        ("bytes_received_kbs", "Received", "#2ecc71"), ("bytes_sent_kbs", "Sent", "#3498db")]),
    ("disk_io",      "Disk I/O",        "MB/s", [
        ("disk_read_mbps", "Read", "#3498db"), ("disk_write_mbps", "Write", "#e74c3c")]),
]


def load_metrics(days):
    rows = []
    today = datetime.date.today()
    for i in range(days):
        d = today - datetime.timedelta(days=i)
        path = os.path.join(config.DATA_DIR, f"metrics-{d.isoformat()}.csv")
        if not os.path.exists(path):
            continue
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                out = {k: (float(v) if k in NUMERIC and v not in ("", None) else v)
                       for k, v in r.items()}
                rows.append(out)
    rows.sort(key=lambda r: r.get("timestamp", ""))
    return rows


def generate_png(rows):
    os.makedirs(config.GRAPHS_DIR, exist_ok=True)
    times = [datetime.datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S")
             for r in rows if r.get("timestamp")]
    for name, title, ylabel, series in SPECS:
        fig, ax = plt.subplots(figsize=(11, 4.2))
        for key, label, color in series:
            data = [r.get(key) if r.get(key) is not None else float("nan") for r in rows]
            ax.plot(times, data, label=label, color=color, linewidth=1.4)
        ax.set_title(title, fontsize=11)
        ax.set_ylabel(ylabel, fontsize=9)
        ax.grid(True, alpha=.25)
        ax.legend(fontsize=8, ncol=len(series))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
        fig.autofmt_xdate()
        fig.tight_layout()
        fig.savefig(os.path.join(config.GRAPHS_DIR, f"{name}.png"), dpi=110)
        plt.close(fig)
        print(f"  wrote {name}.png")


def generate_report_html(rows, days):
    os.makedirs(config.GRAPHS_DIR, exist_ok=True)
    data = []
    for r in rows:
        data.append({k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()})
    series_js = json.dumps([
        {"id": name, "title": title, "y": ylabel,
         "series": [{"key": k, "label": lb, "color": c} for k, lb, c in series]}
        for name, title, ylabel, series in SPECS
    ])
    html = REPORT_TEMPLATE.replace("__SERIES__", series_js).replace("__DATA__", json.dumps(data))
    path = os.path.join(config.GRAPHS_DIR, "report.html")
    with open(path, "w") as f:
        f.write(html)
    print(f"  wrote report.html ({len(data)} samples, {days} day(s))")


REPORT_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MySQL Health Report</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  body{margin:0;background:#0f1420;color:#dde3ee;font-family:"Segoe UI",system-ui,sans-serif}
  .wrap{max-width:1400px;margin:0 auto;padding:20px}
  h1{font-size:20px} .sub{color:#8b96ab;font-size:12px;margin-bottom:18px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(460px,1fr));gap:16px}
  .card{background:#1a2130;border:1px solid #2c3a50;border-radius:12px;padding:14px 16px}
  .card h2{font-size:13px;color:#8b96ab;margin:0 0 10px;font-weight:600}
  .chart{position:relative;height:240px}
</style>
</head>
<body><div class="wrap">
<h1>📊 MySQL Health Report</h1>
<div class="sub">Generated __GENERATED__ · __COUNT__ samples · __DAYS__ day(s)</div>
<div class="grid" id="grid"></div>
</div>
<script>
const SPECS = __SERIES__;
const ROWS = __DATA__;
const grid = document.getElementById('grid');
const labels = ROWS.map(r => (r.timestamp||'').slice(11));
for (const spec of SPECS) {
  const card = document.createElement('div'); card.className = 'card';
  card.innerHTML = `<h2>${spec.title}</h2><div class="chart"><canvas id="c_${spec.id}"></canvas></div>`;
  grid.appendChild(card);
  new Chart(document.getElementById('c_'+spec.id), {
    type:'line',
    data:{ labels, datasets: spec.series.map(s => ({
      label:s.label, data:ROWS.map(r=>r[s.key]??null),
      borderColor:s.color, backgroundColor:s.color, borderWidth:1.6,
      pointRadius:0, tension:.25, fill:false })),
    },
    options:{ responsive:true, maintainAspectRatio:false,
      plugins:{ legend:{ labels:{color:'#8b96ab',boxWidth:10,font:{size:10}} } },
      scales:{ x:{ticks:{color:'#5d6a80',maxTicksLimit:10},grid:{color:'#1d2636'}},
               y:{ticks:{color:'#5d6a80'},grid:{color:'#1d2636'},title:{display:true,text:spec.y,color:'#5d6a80',font:{size:10}}} } }
  });
}
</script>
</body></html>
"""


def main():
    ap = argparse.ArgumentParser(description="Generate static graphs from monitor CSV")
    ap.add_argument("--days", type=int, default=1, help="days of history to include (default 1)")
    args = ap.parse_args()

    rows = load_metrics(args.days)
    if not rows:
        print(f"No CSV data found in {config.DATA_DIR} for the last {args.days} day(s).")
        print("Start the monitor first:  .venv/bin/python3 monitor.py")
        return 1

    print(f"Loaded {len(rows)} samples spanning "
          f"{rows[0].get('timestamp')} → {rows[-1].get('timestamp')}")

    generate_png(rows)
    generate_report_html(rows, args.days)

    print(f"\nGraphs written to {config.GRAPHS_DIR}/")
    print("  PNGs:      open any *.png")
    print("  report:    open graphs/report.html in a browser (interactive)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
