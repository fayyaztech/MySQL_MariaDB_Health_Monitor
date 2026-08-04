# MySQL / MariaDB Health Monitor

Samples MySQL **and** Linux metrics every few seconds, writes them to daily-rotated
CSV files, and serves a live web dashboard. When `Threads_running` spikes, it
automatically captures `SHOW FULL PROCESSLIST`, `SHOW ENGINE INNODB STATUS`, and
long-running queries so you can see exactly what happened during the spike.

Everything is CSV-first, so you can graph it with Excel, Grafana, matplotlib, or
the included dashboard.

## Features

| Category          | Metrics (CSV columns)                                             |
| ----------------- | ----------------------------------------------------------------- |
| System            | `cpu_pct`, `load1/5/15`, `ram_used_mb`, `ram_free_mb`, `swap_used_mb`, `disk_read_mbps`, `disk_write_mbps` |
| MySQL Connections | `threads_connected`, `threads_running`, `max_used_connections`, `max_connections` |
| Queries           | `questions_per_sec`, `queries_per_sec`, `slow_queries_total`, `slow_queries_delta` |
| InnoDB            | `buffer_pool_used_mb`, `buffer_pool_free_mb`, `buffer_pool_dirty_pages`, `row_lock_waits`, `row_lock_time_ms` |
| Temp Tables       | `created_tmp_tables`, `created_tmp_disk_tables`, `tmp_disk_table_pct` |
| Network           | `bytes_received_kbs`, `bytes_sent_kbs`                             |
| Processlist       | `proc_sleeping`, `proc_running`, `proc_locked`                     |
| Top Queries       | `long_queries` (in `live.json` / dashboard, not CSV)               |
| Snapshots         | `processlist.log`, `innodb_status.log`, `slow_query_snapshot.log`  |

> Every row also carries `timestamp` (`YYYY-MM-DD HH:MM:SS`). `live.json` in
> `data/` always holds the most recent sample and is what the dashboard feeds on.

## Project layout

```text
server_monitor/
├── monitor.py             # the sampler (writes CSV + live.json + snapshots)
├── dashboard.py           # Flask live web dashboard (127.0.0.1:5000)
├── generate_graphs.py     # offline PNG charts + interactive report.html
├── config.py              # all settings in one place
├── requirements.txt
├── install_service.sh     # install monitor.py as a systemd service
├── mysql-monitor.service  # systemd unit file
├── templates/
│   └── dashboard.html     # dashboard page (Chart.js)
├── data/                  # created at runtime
│   ├── metrics-YYYY-MM-DD.csv
│   ├── live.json
│   ├── processlist.log
│   ├── innodb_status.log
│   └── slow_query_snapshot.log
└── graphs/                # created by generate_graphs.py
    ├── *.png              # per-metric-group charts
    └── report.html        # self-contained interactive report
```

## Requirements

- Python 3.9+
- A running MySQL / MariaDB server
- Read access to `performance_schema` / the DB is optional; the monitor uses the
  regular `SHOW GLOBAL STATUS` / `SHOW ENGINE INNODB STATUS` commands
  (requires `PROCESS` privilege for full processlist/innodb status).

## Setup

```bash
cd server_monitor

# 1. Create the virtualenv and install dependencies
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Point the monitor at your database
#    Edit config.py — at minimum DB_USER, DB_PASSWORD, DB_NAME (and DB_HOST/DB_PORT if not localhost)
```

If the DB is down the monitor keeps running and retries — it still samples Linux
metrics, it just records `0`/empty values for the MySQL columns until the DB is
reachable again.

## Run

### 1. Sample the monitor (foreground)

```bash
.venv/bin/python3 monitor.py              # run continuously (Ctrl-C to stop)
.venv/bin/python3 monitor.py --once       # sample once, print the row, exit
.venv/bin/python3 monitor.py --interval 15   # override sampling interval (seconds)
```

### 2. View the live dashboard

In a second terminal:

```bash
.venv/bin/python3 dashboard.py
# Dashboard: http://127.0.0.1:5000
```

Open http://127.0.0.1:5000 — it auto-refreshes every 5 seconds and shows the
latest sample plus the last 500 CSV rows.

### 3. Generate graphs / offline report

```bash
.venv/bin/python3 generate_graphs.py            # today's CSV
.venv/bin/python3 generate_graphs.py --days 7   # last 7 days
```

This writes PNG charts per metric group to `graphs/` plus a self-contained
interactive `graphs/report.html` that opens in any browser without a server.

## Run as a service (systemd)

Installs `monitor.py` as a service that starts on boot and restarts on failure.
It logs to the systemd journal.

```bash
./install_service.sh              # system-wide → /etc/systemd/system (needs sudo)
./install_service.sh --user       # user scope → ~/.config/systemd/user (no sudo)
```

Manage it with:

```bash
systemctl status mysql-monitor          # or: systemctl --user status mysql-monitor
journalctl -u mysql-monitor -f          # follow the logs
```

To stop/disable later:

```bash
systemctl --user stop mysql-monitor && systemctl --user disable mysql-monitor
# or without --user if installed system-wide
```

## Configuration (config.py)

| Setting                    | Default   | Meaning                                             |
| -------------------------- | --------- | --------------------------------------------------- |
| `DB_HOST` / `DB_PORT`      | 127.0.0.1 / 3306 | MySQL connection             |
| `DB_USER` / `DB_PASSWORD`  | root / password | credentials (edit these!)    |
| `DB_NAME`                  | shiksha-core-module | database to connect to       |
| `SAMPLE_INTERVAL`          | 10        | seconds between samples                              |
| `THREADS_RUNNING_THRESHOLD`| 10        | when exceeded, snapshot processlist/innodb/slow logs |
| `SLOW_QUERY_SECONDS`       | 5         | a query running this long is "long-running"          |
| `RETENTION_DAYS`           | 30        | auto-purge CSVs/graphs older than this               |
| `DASHBOARD_HISTORY_ROWS`   | 500       | rows the dashboard serves from the current CSV       |

## What each output file is for

- **`data/metrics-YYYY-MM-DD.csv`** — one row per sample, new file each day. This is
  your time-series data for Excel/Grafana.
- **`data/live.json`** — the latest sample (plus `long_queries` and
  `processlist_counts`). The dashboard reads this.
- **`data/processlist.log`** — `SHOW FULL PROCESSLIST` snapshots taken when
  `Threads_running` crossed the threshold.
- **`data/innodb_status.log`** — `SHOW ENGINE INNODB STATUS` snapshots (same trigger).
- **`data/slow_query_snapshot.log`** — queries running longer than `SLOW_QUERY_SECONDS`.

With this you can immediately visualize: CPU vs. concurrent users,
`Threads_running` vs. response time, slow queries over time, buffer-pool usage,
temp-table spillover to disk, lock waits, and connection spikes.
