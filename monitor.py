#!/usr/bin/env python3
"""
MySQL / MariaDB health recorder.

Samples MySQL + Linux metrics every SAMPLE_INTERVAL seconds and writes them to
daily-rotated CSV files in data/. When Threads_running exceeds a threshold it
also snapshots SHOW FULL PROCESSLIST, SHOW ENGINE INNODB STATUS and the current
long-running queries into log files.

Usage:
    python3 monitor.py                  # run as a service (Ctrl-C to stop)
    python3 monitor.py --once           # sample once, print the row, exit
    python3 monitor.py --interval 15    # override the sample interval
"""

import argparse
import csv
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime

import pymysql

import config

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

COLUMNS = [
    "timestamp", "cpu_pct", "load1", "load5", "load15",
    "ram_used_mb", "ram_free_mb", "swap_used_mb",
    "disk_read_mbps", "disk_write_mbps",
    "threads_connected", "threads_running", "max_used_connections", "max_connections",
    "questions_per_sec", "queries_per_sec", "slow_queries_total", "slow_queries_delta",
    "bytes_received_kbs", "bytes_sent_kbs",
    "buffer_pool_used_mb", "buffer_pool_free_mb", "buffer_pool_dirty_pages",
    "created_tmp_tables", "created_tmp_disk_tables", "tmp_disk_table_pct",
    "row_lock_waits", "row_lock_time_ms",
    "proc_sleeping", "proc_running", "proc_locked",
]

LIVE_PATH = "live.json"


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print(f"[{now_str()}] {msg}", flush=True)


def _int(v):
    """Safely coerce a SHOW STATUS string (or None) to int."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------
# Linux system metrics via /proc (stdlib only, no psutil needed)
# --------------------------------------------------------------------------

class SystemMonitor:
    """Reads CPU/load/RAM/swap/disk I/O from /proc and computes rates."""

    def __init__(self):
        self._stat = None
        self._disk = None
        self._t = None

    def sample(self):
        stat = _read_proc_stat()
        disk = _read_diskstats()
        t = time.monotonic()
        mem = _read_meminfo()
        load = _read_loadavg()

        elapsed = (t - self._t) if self._t is not None else 0.0

        # CPU% from jiffy deltas (time-independent).
        if self._stat is not None:
            total_d = stat["total"] - self._stat["total"]
            idle_d = stat["idle"] - self._stat["idle"]
            cpu_pct = 100.0 * (total_d - idle_d) / total_d if total_d > 0 else 0.0
        else:
            cpu_pct = 0.0

        # Disk I/O rate (needs wall-clock elapsed).
        if self._disk is not None and elapsed > 0:
            read_bps = (disk["read"] - self._disk["read"]) / elapsed
            write_bps = (disk["write"] - self._disk["write"]) / elapsed
        else:
            read_bps = write_bps = 0.0

        self._stat, self._disk, self._t = stat, disk, t

        total_kb = mem["MemTotal"]
        avail_kb = mem.get("MemAvailable") or (mem.get("MemFree", 0)
                                               + mem.get("Buffers", 0)
                                               + mem.get("Cached", 0))
        swap_total = mem.get("SwapTotal", 0)
        swap_free = mem.get("SwapFree", 0)

        return {
            "cpu_pct": round(cpu_pct, 1),
            "load1": round(load[0], 2),
            "load5": round(load[1], 2),
            "load15": round(load[2], 2),
            "ram_used_mb": round((total_kb - avail_kb) / 1024, 1),
            "ram_free_mb": round(avail_kb / 1024, 1),
            "swap_used_mb": round((swap_total - swap_free) / 1024, 1),
            "disk_read_mbps": round(read_bps / 1e6, 3),
            "disk_write_mbps": round(write_bps / 1e6, 3),
        }


def _read_proc_stat():
    with open("/proc/stat") as f:
        parts = f.readline().split()
    vals = [int(x) for x in parts[1:]]
    user, nice, system, idle, iowait = vals[0], vals[1], vals[2], vals[3], vals[4]
    irq = vals[5]
    softirq = vals[6]
    steal = vals[7] if len(vals) > 7 else 0
    user += vals[8] if len(vals) > 8 else 0       # guest
    nice += vals[9] if len(vals) > 9 else 0       # guest_nice
    total = user + nice + system + idle + iowait + irq + softirq + steal
    return {"total": total, "idle": idle + iowait}


def _read_loadavg():
    with open("/proc/loadavg") as f:
        parts = f.read().split()
    return float(parts[0]), float(parts[1]), float(parts[2])


def _read_meminfo():
    d = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, _, rest = line.partition(":")
            if key:
                d[key] = int(rest.split()[0])  # kB
    return d


def _read_diskstats():
    totals = {"read": 0, "write": 0}
    with open("/proc/diskstats") as f:
        for line in f:
            p = line.split()
            if len(p) < 10 or p[2].startswith(("loop", "ram", "zram")):
                continue
            try:
                read_sectors = int(p[5])
                write_sectors = int(p[9])
            except ValueError:
                continue
            totals["read"] += read_sectors * 512
            totals["write"] += write_sectors * 512
    return totals


# --------------------------------------------------------------------------
# MySQL / MariaDB access
# --------------------------------------------------------------------------

def connect():
    return pymysql.connect(
        host=config.DB_HOST, port=config.DB_PORT,
        user=config.DB_USER, password=config.DB_PASSWORD,
        database=config.DB_NAME,
        connect_timeout=config.DB_CONNECT_TIMEOUT,
        read_timeout=config.DB_READ_TIMEOUT,
        charset="utf8mb4", autocommit=True,
    )


def fetch_status(conn):
    with conn.cursor() as cur:
        cur.execute("SHOW GLOBAL STATUS")
        return {row[0]: row[1] for row in cur.fetchall()}


def fetch_variables(conn):
    with conn.cursor() as cur:
        cur.execute("SHOW GLOBAL VARIABLES")
        return {row[0]: row[1] for row in cur.fetchall()}


def fetch_processlist(conn):
    with conn.cursor() as cur:
        cur.execute("SHOW FULL PROCESSLIST")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def fetch_innodb_status(conn):
    with conn.cursor() as cur:
        cur.execute("SHOW ENGINE INNODB STATUS")
        row = cur.fetchone()
        if not row:
            return ""
        return row[2] if len(row) > 2 else ""


def rate(prev, cur, key, elapsed):
    """Per-second delta of a counter, guarding against counter resets."""
    if prev is None or elapsed <= 0 or key not in prev or key not in cur:
        return 0.0
    d = _int(cur[key]) - _int(prev[key])
    return (d / elapsed) if d > 0 else 0.0


def classify_processlist(plist, slow_sec):
    sleeping = running = locked = 0
    long_queries = []
    for p in plist:
        cmd = p.get("Command") or ""
        state = p.get("State") or ""
        t = _int(p.get("Time"))
        if cmd == "Sleep":
            sleeping += 1
        elif cmd != "Daemon":
            running += 1
        if "lock" in state.lower():
            locked += 1
        if cmd not in ("Sleep", "Daemon") and t >= slow_sec:
            long_queries.append({
                "id": p.get("Id"), "user": p.get("User"),
                "db": p.get("db") or "", "time": t, "state": state,
                "info": (p.get("Info") or "")[:300],
            })
    long_queries.sort(key=lambda q: q["time"], reverse=True)
    return sleeping, running, locked, long_queries[:20]


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------

def collect(conn, sysmon, prev_status, prev_time, variables):
    """Gather one sample. Returns a dict with the CSV row + extras."""
    status = fetch_status(conn)
    sysdata = sysmon.sample()
    now_mono = time.monotonic()
    elapsed = (now_mono - prev_time) if prev_time is not None else 0.0
    plist = fetch_processlist(conn)
    sleeping, running, locked, long_queries = classify_processlist(
        plist, config.SLOW_QUERY_SECONDS)

    page_size = _int(variables.get("Innodb_page_size")) or 16384
    data_pages = _int(status.get("Innodb_buffer_pool_pages_data"))
    free_pages = _int(status.get("Innodb_buffer_pool_pages_free"))
    dirty_pages = _int(status.get("Innodb_buffer_pool_pages_dirty"))

    tmp_total = _int(status.get("Created_tmp_tables"))
    tmp_disk = _int(status.get("Created_tmp_disk_tables"))

    threads_running = _int(status.get("Threads_running"))

    row = {
        "timestamp": now_str(),
        "cpu_pct": sysdata["cpu_pct"],
        "load1": sysdata["load1"], "load5": sysdata["load5"], "load15": sysdata["load15"],
        "ram_used_mb": sysdata["ram_used_mb"], "ram_free_mb": sysdata["ram_free_mb"],
        "swap_used_mb": sysdata["swap_used_mb"],
        "disk_read_mbps": sysdata["disk_read_mbps"], "disk_write_mbps": sysdata["disk_write_mbps"],
        "threads_connected": _int(status.get("Threads_connected")),
        "threads_running": threads_running,
        "max_used_connections": _int(status.get("Max_used_connections")),
        "max_connections": _int(variables.get("max_connections")),
        "questions_per_sec": round(rate(prev_status, status, "Questions", elapsed), 1),
        "queries_per_sec": round(rate(prev_status, status, "Queries", elapsed), 1),
        "slow_queries_total": _int(status.get("Slow_queries")),
        "slow_queries_delta": _int(rate(prev_status, status, "Slow_queries", elapsed) * elapsed),
        "bytes_received_kbs": round(rate(prev_status, status, "Bytes_received", elapsed) / 1024, 1),
        "bytes_sent_kbs": round(rate(prev_status, status, "Bytes_sent", elapsed) / 1024, 1),
        "buffer_pool_used_mb": round(data_pages * page_size / (1024 * 1024), 1),
        "buffer_pool_free_mb": round(free_pages * page_size / (1024 * 1024), 1),
        "buffer_pool_dirty_pages": dirty_pages,
        "created_tmp_tables": tmp_total,
        "created_tmp_disk_tables": tmp_disk,
        "tmp_disk_table_pct": round(100.0 * tmp_disk / tmp_total, 1) if tmp_total else 0.0,
        "row_lock_waits": _int(status.get("Innodb_row_lock_waits")),
        "row_lock_time_ms": _int(status.get("Innodb_row_lock_time")),
        "proc_sleeping": sleeping, "proc_running": running, "proc_locked": locked,
    }

    return {
        "row": row,
        "status": status,
        "mono_time": now_mono,
        "threads_running": threads_running,
        "alarm": threads_running > config.THREADS_RUNNING_THRESHOLD,
        "processlist": plist,
        "long_queries": long_queries,
    }


# --------------------------------------------------------------------------
# Output: CSV (daily rotation), live.json, snapshots
# --------------------------------------------------------------------------

class CsvLogger:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.date = None
        self.file = None
        self.writer = None

    def _ensure(self, d):
        if self.file is not None and self.date == d:
            return
        self.close()
        self.date = d
        path = os.path.join(self.data_dir, f"metrics-{d}.csv")
        is_new = not os.path.exists(path)
        self.file = open(path, "a", newline="")
        self.writer = csv.writer(self.file)
        if is_new:
            self.writer.writerow(COLUMNS)
        log(f"writing to {os.path.basename(path)}")

    def write(self, row):
        d = row["timestamp"][:10]
        self._ensure(d)
        self.writer.writerow([row.get(c, "") for c in COLUMNS])
        self.file.flush()

    def close(self):
        if self.file is not None:
            self.file.close()
            self.file = None


def write_live(payload):
    path = os.path.join(config.DATA_DIR, LIVE_PATH)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def snapshot(conn, sample):
    ts = now_str()
    header = (
        "\n" + "=" * 78 + f"\n{ts}  Threads_running={sample['threads_running']}"
        f" (threshold {config.THREADS_RUNNING_THRESHOLD})\n" + "=" * 78 + "\n"
    )
    with open(os.path.join(config.DATA_DIR, "processlist.log"), "a") as f:
        f.write(header)
        for p in sample["processlist"]:
            f.write(
                f"Id={p.get('Id')} User={p.get('User')} Host={p.get('Host')} "
                f"db={p.get('db') or ''} Cmd={p.get('Command')} Time={p.get('Time')} "
                f"State={p.get('State') or ''}\n    Info: {(p.get('Info') or '')[:500]}\n"
            )
    try:
        innodb = fetch_innodb_status(conn)
        if innodb:
            with open(os.path.join(config.DATA_DIR, "innodb_status.log"), "a") as f:
                f.write(header + innodb + "\n")
    except Exception as e:
        log(f"innodb status snapshot failed: {e}")
    if sample["long_queries"]:
        with open(os.path.join(config.DATA_DIR, "slow_query_snapshot.log"), "a") as f:
            f.write(header)
            for q in sample["long_queries"]:
                f.write(f"Id={q['id']} User={q['user']} Time={q['time']}s "
                        f"State={q['state']}\n    {q['info']}\n")
    log(f"snapshot written (threads_running={sample['threads_running']})")


def cleanup_old_files():
    cutoff = time.time() - config.RETENTION_DAYS * 86400
    for f in os.listdir(config.DATA_DIR):
        if f.startswith("metrics-") and f.endswith(".csv"):
            p = os.path.join(config.DATA_DIR, f)
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
                log(f"purged {f}")
    for f in os.listdir(config.GRAPHS_DIR) if os.path.isdir(config.GRAPHS_DIR) else []:
        if f.endswith(".png"):
            p = os.path.join(config.GRAPHS_DIR, f)
            if os.path.getmtime(p) < cutoff:
                os.remove(p)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def run(interval, once):
    sysmon = SystemMonitor()
    csvlog = CsvLogger(config.DATA_DIR)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    conn = None
    variables = {}
    prev_status = None
    prev_time = None
    last_cleanup_day = ""

    while not stop.is_set():
        tick_start = time.monotonic()

        if conn is None:
            try:
                conn = connect()
                variables = fetch_variables(conn)
                log(f"connected to {config.DB_HOST}:{config.DB_PORT}")
            except Exception as e:
                log(f"DB connect failed: {e} — retrying")
                if once:
                    csvlog.close()
                    sys.exit(1)
                time.sleep(min(interval, 5))
                continue

        try:
            sample = collect(conn, sysmon, prev_status, prev_time, variables)
        except Exception as e:
            log(f"DB error: {e} — reconnecting")
            try:
                conn.close()
            except Exception:
                pass
            conn = None
            if once:
                csvlog.close()
                sys.exit(1)
            time.sleep(interval)
            continue

        prev_status = sample["status"]
        prev_time = sample["mono_time"]

        csvlog.write(sample["row"])

        payload = {
            "timestamp": sample["row"]["timestamp"],
            "alarm": sample["alarm"],
            "threshold": config.THREADS_RUNNING_THRESHOLD,
            "interval": interval,
            "current": sample["row"],
            "processlist_counts": {
                "sleeping": sample["row"]["proc_sleeping"],
                "running": sample["row"]["proc_running"],
                "locked": sample["row"]["proc_locked"],
            },
            "long_queries": sample["long_queries"],
        }
        write_live(payload)

        if sample["alarm"]:
            snapshot(conn, sample)

        today = sample["row"]["timestamp"][:10]
        if today != last_cleanup_day:
            last_cleanup_day = today
            cleanup_old_files()

        if once:
            print(json.dumps(sample["row"], indent=2, default=str))
            csvlog.close()
            return 0

        elapsed = time.monotonic() - tick_start
        time.sleep(max(0.0, interval - elapsed))

    csvlog.close()
    log("stopped")
    return 0


def main():
    ap = argparse.ArgumentParser(description="MySQL/MariaDB health recorder")
    ap.add_argument("--once", action="store_true",
                    help="sample once, print the row, then exit")
    ap.add_argument("--interval", type=float, default=config.SAMPLE_INTERVAL,
                    help="sample interval in seconds (default: config)")
    args = ap.parse_args()

    if args.interval <= 0:
        sys.stderr.write("--interval must be > 0\n")
        return 2
    if not os.path.isdir(config.DATA_DIR):
        os.makedirs(config.DATA_DIR, exist_ok=True)

    sys.exit(run(args.interval, args.once))


if __name__ == "__main__":
    main()
