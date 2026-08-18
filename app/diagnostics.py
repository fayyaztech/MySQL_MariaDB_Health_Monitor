"""On-demand diagnostics for a PM2-managed Node app.

Python port of the former ``diagnose-shiksha-api.sh``. Every section is an
independent, reusable function; ``run_diagnostic()`` wires them into a single
report that streams incrementally to an optional file-like ``out`` so the
diagnose job can be polled while it runs.

Dependencies are deliberately kept to what the app already ships (psutil,
mysql-connector) plus a few thin ``subprocess`` helpers for curl / pm2 / df.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from collections import Counter
from pathlib import Path

import psutil

from app.pm2_utils import count_mysql_connections, parse_jlist, tail_lines

_MYSQL_STATUS_VARS = (
    "Threads_connected", "Threads_running", "Threads_created", "Threads_cached",
    "Connections", "Aborted_clients", "Aborted_connects", "Slow_queries",
    "Queries", "Questions", "Created_tmp_tables", "Created_tmp_disk_tables",
    "Table_locks_waited", "Innodb_row_lock_current_waits", "Innodb_row_lock_time",
    "Innodb_row_lock_time_max", "Innodb_row_lock_waits",
)


class Reporter:
    """Accumulates report lines and streams them to an optional file-like."""

    def __init__(self, out=None):
        self._out = out
        self.lines: list[str] = []

    def write(self, text: str = ""):
        self.lines.append(text)
        if self._out is not None:
            try:
                self._out.write(text + "\n")
                self._out.flush()
            except (OSError, ValueError):
                pass

    def section(self, title: str):
        self.write("")
        self.write("=" * 60)
        self.write(f" {title}")
        self.write("=" * 60)

    def text(self) -> str:
        return "\n".join(self.lines)


# --- generic command helpers -------------------------------------------------

def run_cmd(*args, timeout=10, env=None) -> tuple[int, str, str]:
    """Run a command; returns (returncode, stdout, stderr). Never raises."""
    try:
        proc = subprocess.run(
            [str(a) for a in args], capture_output=True, text=True,
            timeout=timeout, errors="replace", env=env,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except Exception as exc:
        return -1, "", str(exc)


def run_sh(script: str, timeout=10, env=None) -> tuple[int, str, str]:
    """Run a shell pipeline string; returns (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            script, shell=True, capture_output=True, text=True,
            timeout=timeout, errors="replace", env=env,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except Exception as exc:
        return -1, "", str(exc)


def _read_proc_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# --- resolution helpers ------------------------------------------------------

def resolve_pid(app: str) -> int | None:
    """Return the PM2 pid for an app, or None if it isn't running."""
    _, out, _ = run_cmd("pm2", "pid", app)
    pid = out.strip().splitlines()[-1].strip() if out.strip() else ""
    return int(pid) if pid.isdigit() and int(pid) > 0 else None


def resolve_port(pid: int, port_arg: int | None = None) -> int:
    """Auto-detect the process's first listening port; explicit arg wins."""
    if port_arg:
        return port_arg
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.pid == pid and conn.status == "LISTEN" and conn.laddr:
                return conn.laddr.port
    except Exception:
        pass
    return 3021


def resolve_app_root(app: str, app_root: str | None = None) -> str:
    """Resolve the app directory by name; explicit arg wins."""
    if app_root:
        return app_root
    base = Path("/www/wwwroot/sserp.mangrule.in/apps")
    if (base / app).is_dir():
        return str(base / app)
    return str(base / "api")


def dotenv_keys(path, keys: set[str]) -> dict[str, str]:
    """Return values for ``keys`` found in a simple KEY=VALUE .env file."""
    result: dict[str, str] = {}
    for line in _read_proc_file(path).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in keys:
            result.setdefault(key, value.strip().strip('"').strip("'"))
    return result


def resolve_db_credentials(app_root: str,
                           env_overrides: dict | None = None) -> dict[str, str]:
    """Resolve MySQL credentials: env overrides -> monitor .env -> app .env."""
    env = env_overrides or {}
    monitor_env = dotenv_keys(
        Path(__file__).resolve().parent.parent / ".env",
        {"MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE"},
    )
    app_env = dotenv_keys(
        Path(app_root) / ".env",
        {"DB_HOST", "DB_USER", "DB_NAME", "DB_PASSWORD", "DB_PASS", "DB_PWD"},
    )

    host = env.get("host") or env.get("MYSQL_HOST") or monitor_env.get("MYSQL_HOST") or app_env.get("DB_HOST")
    user = env.get("user") or env.get("MYSQL_USER") or monitor_env.get("MYSQL_USER") or app_env.get("DB_USER")
    password = (
        env.get("password") or env.get("MYSQL_PASSWORD")
        or monitor_env.get("MYSQL_PASSWORD")
        or app_env.get("DB_PASSWORD") or app_env.get("DB_PASS") or app_env.get("DB_PWD")
    )
    database = (
        env.get("database") or env.get("MYSQL_DATABASE")
        or monitor_env.get("MYSQL_DATABASE") or app_env.get("DB_NAME")
    )
    return {"host": host or "", "user": user or "", "password": password or "",
            "database": database or ""}


def _mysql_query(sql: str, creds: dict) -> list[dict]:
    """Run a query against the resolved credentials; [] on any error."""
    import mysql.connector
    try:
        conn = mysql.connector.connect(
            host=creds["host"] or "localhost",
            port=3306,
            user=creds["user"] or "root",
            password=creds["password"],
            database=creds["database"] or None,
            connection_timeout=5,
        )
    except Exception:
        return []
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(sql)
            return cur.fetchall()
        finally:
            cur.close()
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _proc(pid: int) -> psutil.Process | None:
    try:
        return psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None


# --- PM2 sections ------------------------------------------------------------

def section_pm2(rep: Reporter, app: str, pid: int):
    rep.section("PM2 STATUS")
    for cmd in (("pm2", "status", app), ("pm2", "describe", app)):
        _, out, err = run_cmd(*cmd)
        rep.write((out.strip() or err.strip() or "(no output)"))

    rep.section("PM2 ENVIRONMENT - FILTERED")
    _, id_out, _ = run_cmd("pm2", "id", app)
    pm_id = id_out.strip().strip("[] ")
    if pm_id.isdigit():
        _, out, _ = run_cmd("pm2", "env", pm_id)
        for line in out.splitlines():
            if re.match(r"^(NODE_|PM2_|PWD|HOME|USER|PORT|NODE_ENV)=", line):
                rep.write(line)
    else:
        rep.write("(could not read pm2 env)")

    for proc in parse_jlist():
        if proc["name"] == app:
            rep.section("PM2 PROCESS SNAPSHOT")
            rep.write(f"name={proc['name']} pid={proc['pid']} status={proc['status']} "
                      f"cpu={proc['cpu']:.1f}% restarts={proc['restarts']} "
                      f"unstable_restarts={proc['unstable_restarts']}")
            break


# --- process sections --------------------------------------------------------

def _children_lines(p: psutil.Process, prefix: str = "") -> list[str]:
    lines: list[str] = []
    try:
        children = p.children()
    except Exception:
        return lines
    for i, child in enumerate(children):
        last = i == len(children) - 1
        branch = "└── " if last else "├── "
        try:
            info = f"{child.pid} ({child.name()})"
        except Exception:
            info = f"{child.pid}"
        lines.append(f"{prefix}{branch}{info}")
        lines.extend(_children_lines(child, prefix + ("    " if last else "│   ")))
    return lines


def section_process(rep: Reporter, pid: int):
    rep.section("PROCESS INFORMATION")
    p = _proc(pid)
    if p is None:
        rep.write("process not found")
        return
    try:
        rep.write(f"pid={p.pid} ppid={p.ppid()} name={p.name()} "
                  f"user={p.username()} status={p.status()}")
    except Exception:
        pass
    try:
        rep.write(f"cmdline: {' '.join(p.cmdline() or [])}")
    except Exception:
        pass
    rep.write("Process tree:")
    for line in _children_lines(p):
        rep.write(line)
    rep.write("")
    rep.write("Process state (/proc status):")
    for line in _read_proc_file(f"/proc/{pid}/status").splitlines()[:24]:
        rep.write(line)


def section_cpu_mem(rep: Reporter, pid: int):
    rep.section("CPU / MEMORY")
    p = _proc(pid)
    if p is not None:
        try:
            mem = p.memory_info()
            rep.write(f"cpu_percent={p.cpu_percent(interval=None):.1f} "
                      f"rss={mem.rss / 1048576:.1f}MB vsz={mem.vms / 1048576:.1f}MB "
                      f"threads={p.num_threads()}")
        except Exception:
            pass
    _, out, _ = run_cmd("ps", "-o", "pid,ppid,user,%cpu,%mem,rss,vsz,stat,etime,lstart,cmd", "-p", str(pid))
    rep.write(out.strip())
    rep.write("")
    _, out, _ = run_cmd("uptime")
    rep.write("System uptime/load:")
    rep.write(out.strip())
    _, out, _ = run_cmd("free", "-h")
    rep.write(out.strip())


def section_fds(rep: Reporter, pid: int):
    rep.section("OPEN FILE DESCRIPTORS")
    p = _proc(pid)
    if p is None:
        rep.write("process not found")
        return
    try:
        rep.write(f"FD count: {p.num_fds()}")
    except Exception:
        pass
    limits = _read_proc_file(f"/proc/{pid}/limits")
    for line in limits.splitlines():
        if re.search(r"open files|Max processes|Max locked memory|Max address space", line):
            rep.write(line)
    rep.write("FD targets:")
    try:
        for f in p.open_files()[:80]:
            rep.write(f"  {f.path}")
    except Exception:
        pass


def section_tcp(rep: Reporter, pid: int):
    rep.section("TCP CONNECTION SUMMARY")
    try:
        conns = [c for c in psutil.net_connections(kind="inet") if c.pid == pid]
    except Exception:
        conns = []
    for status, n in Counter(c.status for c in conns).most_common():
        rep.write(f"{status}: {n}")
    rep.write("TCP details:")
    for c in conns[:120]:
        rep.write(f"  {c.status} local={c.laddr or ''} remote={c.raddr or ''}")
    rep.section("LISTEN SOCKET")
    listen = [c for c in conns if c.status == "LISTEN" and c.laddr]
    for c in listen:
        rep.write(f"  LISTEN {c.laddr}")


def section_api_test(rep: Reporter, port: int):
    rep.section(f"LOCAL API RESPONSE TEST (port {port})")
    rep.write(f"Testing http://127.0.0.1:{port}/")
    for i in range(1, 11):
        start = time.perf_counter()
        _, out, _ = run_cmd(
            "curl", "-s", "-o", os.devnull, "-w", "%{http_code}",
            "--connect-timeout", "2", "--max-time", "5",
            f"http://127.0.0.1:{port}/",
        )
        ms = int((time.perf_counter() - start) * 1000)
        rep.write(f"{time.strftime('%H:%M:%S')} request={i} http={out.strip() or '—'} time={ms}ms")
        time.sleep(1)


# --- MySQL sections ----------------------------------------------------------

def section_mysql(rep: Reporter, pid: int, creds: dict):
    rep.section("MYSQL PROCESSLIST")
    rep.write(f"MySQL connections from pid {pid}: "
              f"{count_mysql_connections(pid, 3306)}")
    rep.write(f"DB Host: {creds['host'] or 'localhost'}  DB User: {creds['user'] or ''}  "
              f"DB Name: {creds['database'] or ''}")
    rows = _mysql_query("SHOW FULL PROCESSLIST", creds)
    if rows:
        for r in rows:
            info = str(r.get("Info") or "")[:200]
            rep.write(f"{r.get('Id')} {r.get('User')} {r.get('Host')} "
                      f"{r.get('db') or ''} {r.get('Command')} {r.get('Time')} "
                      f"{r.get('State') or ''} {info}")
    else:
        rep.write("(processlist unavailable — MySQL connection failed)")

    rep.section("MYSQL GLOBAL STATUS")
    names = ", ".join(f"'{v}'" for v in _MYSQL_STATUS_VARS)
    rows = _mysql_query(f"SHOW GLOBAL STATUS WHERE Variable_name IN ({names})", creds)
    if rows:
        for r in rows:
            rep.write(f"{r.get('Variable_name')}: {r.get('Value')}")
    else:
        rep.write("(global status unavailable — MySQL connection failed)")


# --- system sections ---------------------------------------------------------

def section_disk(rep: Reporter, app_root: str):
    rep.section("DISK")
    _, out, _ = run_cmd("df", "-h")
    rep.write(out.strip())
    rep.write("")
    _, out, _ = run_cmd("du", "-sh", app_root)
    rep.write(f"App root ({app_root}): {out.strip()}")

    rep.section("DISK I/O")
    _, out, err = run_cmd("iostat", "-xz", "1", "3", timeout=15)
    rep.write((out.strip() or err.strip()) or "iostat not installed")


def section_system(rep: Reporter):
    rep.section("SYSTEM MEMORY / SWAP")
    for line in _read_proc_file("/proc/meminfo").splitlines():
        if re.match(r"^(MemTotal|MemFree|MemAvailable|Buffers|Cached|SwapTotal|SwapFree|Dirty|Writeback):", line):
            rep.write(line)

    rep.section("KERNEL / OOM ERRORS")
    pattern = r"oom|out of memory|killed process|memory cgroup|blocked for more than|hung task|I/O error|ext4|xfs"
    rep.write("dmesg:")
    _, out, _ = run_sh(f"dmesg -T 2>/dev/null | grep -Ei '{pattern}' | tail -100")
    rep.write(out.strip() or "(no matching dmesg lines)")
    rep.write("")
    rep.write("journal:")
    _, out, _ = run_sh(f"journalctl -k --since '1 hour ago' 2>/dev/null | grep -Ei '{pattern}' | tail -100")
    rep.write(out.strip() or "(no matching journal lines)")

    rep.section("SYSTEM LOAD")
    try:
        rep.write("  ".join(f"{v:.2f}" for v in psutil.getloadavg()))
    except Exception:
        rep.write(_read_proc_file("/proc/loadavg").strip())


# --- node sections -----------------------------------------------------------

def section_node(rep: Reporter, pid: int):
    rep.section("NODE COMMAND LINE")
    rep.write(_read_proc_file(f"/proc/{pid}/cmdline").replace("\0", " ") or "(unavailable)")

    rep.section("NODE MEMORY")
    for line in _read_proc_file(f"/proc/{pid}/status").splitlines():
        if re.match(r"^(VmPeak|VmSize|VmRSS|VmHWM|VmData|VmStk|VmExe|VmLib|VmSwap):", line):
            rep.write(line)

    rep.section("NODE THREADS")
    for line in _read_proc_file(f"/proc/{pid}/status").splitlines():
        if line.startswith("Threads:"):
            rep.write(line)

    rep.section("PROCESS IO")
    rep.write(_read_proc_file(f"/proc/{pid}/io").strip() or "(unavailable)")


def section_logs(rep: Reporter, app: str):
    rep.section("APPLICATION ERROR LOG")
    err_log = str(Path.home() / ".pm2" / "logs" / f"{app}-error-0.log")
    lines = tail_lines(err_log, 300)
    if lines:
        rep.write(f"{err_log} — last {len(lines)} lines:")
        for line in lines:
            rep.write(line)
    else:
        rep.write(f"Error log not found: {err_log}")

    rep.section("APPLICATION OUTPUT LOG")
    out_log = str(Path.home() / ".pm2" / "logs" / f"{app}-out-0.log")
    lines = tail_lines(out_log, 300)
    if lines:
        rep.write(f"{out_log} — last {len(lines)} lines:")
        for line in lines:
            rep.write(line)
    else:
        rep.write(f"Output log not found: {out_log}")

    rep.section("RECENT PM2 LOG TIMESTAMPS")
    for log in (err_log, out_log):
        try:
            size = Path(log).stat().st_size
            rep.write(f"{size / 1024:.1f} KB  {log}")
        except OSError:
            pass


# --- orchestrator ------------------------------------------------------------

def run_diagnostic(app: str, out=None, *, port: int | None = None,
                   app_root: str | None = None,
                   db_env: dict | None = None) -> str:
    """Run the full diagnostic for a PM2 app; returns the complete report."""
    rep = Reporter(out)
    rep.write("=" * 60)
    rep.write(" PM2 PROCESS DIAGNOSTIC")
    rep.write("=" * 60)
    rep.write(f"Time     : {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    rep.write(f"Hostname : {os.uname().nodename}")
    rep.write(f"App      : {app}")
    rep.write("=" * 60)

    pid = resolve_pid(app)
    rep.write(f"PID      : {pid or '—'}")
    if pid is None:
        rep.write("ERROR: Could not determine PM2 PID (is the app online?)")
        rep.write("=" * 60)
        return rep.text()

    port = resolve_port(pid, port)
    app_root = resolve_app_root(app, app_root)
    creds = resolve_db_credentials(app_root, db_env)

    section_pm2(rep, app, pid)
    section_process(rep, pid)
    section_cpu_mem(rep, pid)
    section_fds(rep, pid)
    section_tcp(rep, pid)
    section_api_test(rep, port)
    section_mysql(rep, pid, creds)
    section_disk(rep, app_root)
    section_system(rep)
    section_node(rep, pid)
    section_logs(rep, app)

    rep.write("")
    rep.write("=" * 60)
    rep.write(" END — diagnostic completed. This run does NOT restart the app.")
    rep.write("=" * 60)
    return rep.text()