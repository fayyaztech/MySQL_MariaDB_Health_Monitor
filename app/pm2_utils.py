"""Helpers for reading PM2 state and log files (local host only).

The collectors and the /api/pm2/* endpoints share this module. All functions
are best-effort: anything that requires a live `pm2` daemon or extra
privileges degrades to a safe default instead of raising.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

import psutil


def run_pm2_jlist() -> list[dict]:
    """Run `pm2 jlist` and return the raw JSON (best-effort)."""
    try:
        out = subprocess.run(
            ["pm2", "jlist"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        data = json.loads(out.stdout or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _float_value(raw: Any) -> float:
    """Parse a PM2 axm_monitor value string like '2.5ms' / '128.0 MB'."""
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    match = re.search(r"([-+]?\d*\.?\d+)", str(raw))
    return float(match.group(1)) if match else 0.0


def parse_jlist(raw: list[dict] | None = None) -> list[dict]:
    """Normalize `pm2 jlist` output into per-process metric dicts."""
    raw = raw if raw is not None else run_pm2_jlist()
    out: list[dict] = []
    for proc in raw:
        name = proc.get("name") or proc.get("pm_id") and str(proc.get("pm_id")) or "unknown"
        env = proc.get("pm2_env") or {}
        monit = proc.get("monit") or {}
        axm = proc.get("axm_monitor") or {}
        loop_delay = 0.0
        if isinstance(axm.get("Loop delay"), dict):
            loop_delay = _float_value(axm["Loop delay"].get("value"))
        heap = 0
        if isinstance(axm.get("Used Heap Size"), dict):
            heap = int(_float_value(axm["Used Heap Size"].get("value")) * 1024 * 1024)
        out.append({
            "name": name,
            "pm_id": proc.get("pm_id"),
            "pid": proc.get("pid"),
            "status": proc.get("status") or env.get("status") or "unknown",
            "cpu": float(monit.get("cpu") or 0.0),
            "memory_rss": int(monit.get("memory") or 0),
            "memory_heap": heap,
            "loop_delay": loop_delay,
            "uptime_ms": int((proc.get("pm2_env") or {}).get("pm_uptime") or 0),
            "restarts": int(env.get("restart_time") or 0),
            "unstable_restarts": int(proc.get("unstable_restarts") or 0),
            "out_log": env.get("pm_out_log_path"),
            "err_log": env.get("pm_err_log_path"),
        })
    return out


def count_mysql_connections(pid: int | None, port: int = 3306) -> int:
    """Count live TCP sockets from this pid to the MySQL port.

    Uses psutil (needs privileges to see other users' sockets); falls back to
    parsing `ss -tanp` output. Returns 0 on any error.
    """
    if not pid:
        return 0
    try:
        return sum(
            1 for c in psutil.net_connections(kind="inet")
            if c.pid == pid and c.raddr and c.raddr.port == port
        )
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["ss", "-tanp"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
        return sum(
            1 for line in out.splitlines()
            if f"pid={pid}" in line and f":{port}" in line
        )
    except Exception:
        return 0


def expand(path: str | None) -> str | None:
    if not path:
        return None
    return os.path.expanduser(path)


def tail_lines(path: str | None, lines: int = 200) -> list[str]:
    """Return the last `lines` lines of a log file (UTF-8 tolerant)."""
    if not path:
        return []
    path = os.path.expanduser(path)
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            pos = size
            chunk = 8192
            data = b""
            newline_count = 0
            while pos > 0 and newline_count < lines:
                start = max(0, pos - chunk)
                f.seek(start)
                data = f.read(pos - start) + data
                pos = start
                newline_count = data.count(b"\n")
            text = data.decode("utf-8", errors="replace")
            return text.splitlines()[-lines:]
    except (OSError, ValueError):
        return []