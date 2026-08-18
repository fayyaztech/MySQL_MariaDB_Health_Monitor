"""PM2 process collector.

Samples every PM2-managed app on the local host (via `pm2 jlist`), persists a
PM2ProcessMetrics row per app, and records lifecycle events (crash / restart /
status change / pool exhaustion) as PM2Event rows plus alerts.

Requires the `pm2` binary and (for connection counting) privileges to read
other processes' sockets — all best-effort with graceful degradation.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import select

from app.config import get_settings
from app.models import PM2Event, PM2ProcessMetrics
from app.pm2_utils import count_mysql_connections, parse_jlist

logger = logging.getLogger(__name__)


def _last_state(session, name: str) -> PM2ProcessMetrics | None:
    return session.scalar(
        select(PM2ProcessMetrics)
        .where(PM2ProcessMetrics.name == name)
        .order_by(PM2ProcessMetrics.timestamp.desc())
        .limit(1)
    )


def _record_event(session, prev: PM2ProcessMetrics | None, proc: dict,
                  pool_size: int) -> None:
    """Record lifecycle events on transitions between polls."""
    name = proc["name"]
    status = proc["status"]
    if prev is None:
        return

    if prev.status == "online" and status != "online":
        session.add(PM2Event(
            process_name=name, event_type=status,
            detail=f"status changed {prev.status} -> {status} (pid {proc['pid'] or '—'})",
        ))
    elif prev.status != "online" and status == "online":
        session.add(PM2Event(
            process_name=name, event_type="online",
            detail=f"status changed {prev.status} -> online (pid {proc['pid'] or '—'})",
        ))

    if proc["restarts"] > prev.restarts:
        session.add(PM2Event(
            process_name=name, event_type="restart",
            detail=f"restarts {prev.restarts} -> {proc['restarts']} (unstable: {proc['unstable_restarts']})",
        ))

    conns = proc["mysql_connections"]
    if conns >= pool_size and prev.mysql_connections < pool_size:
        session.add(PM2Event(
            process_name=name, event_type="pool_warn",
            detail=f"{conns}/{pool_size} MySQL connections in use (pool exhausted)",
        ))


def collect(session, conn=None) -> int:
    """Sample PM2 processes and persist metrics + lifecycle events.

    conn is accepted for interface uniformity; it is unused here.
    """
    settings = get_settings()
    procs = parse_jlist()
    include = {a for a in settings.pm2_apps} if settings.pm2_apps else None

    rows_written = 0
    now_ms = int(time.time() * 1000)
    for proc in procs:
        if include is not None and proc["name"] not in include:
            continue
        pid = proc["pid"]
        proc["mysql_connections"] = count_mysql_connections(pid, settings.pm2_mysql_port)
        uptime = proc["uptime_ms"]
        proc["uptime_ms"] = max(0, now_ms - uptime) if uptime else 0

        prev = _last_state(session, proc["name"])
        _record_event(session, prev, proc, settings.pm2_pool_size)

        row = PM2ProcessMetrics(
            name=proc["name"],
            pm_id=proc["pm_id"],
            pid=pid,
            status=proc["status"],
            cpu=proc["cpu"],
            memory_rss=proc["memory_rss"],
            memory_heap=proc["memory_heap"],
            loop_delay=proc["loop_delay"],
            uptime_ms=proc["uptime_ms"],
            restarts=proc["restarts"],
            unstable_restarts=proc["unstable_restarts"],
            mysql_connections=proc["mysql_connections"],
        )
        session.add(row)
        rows_written += 1

    from app.alerts.alerting import fire_pm2_alerts
    rows_written += fire_pm2_alerts(session)

    session.flush()
    return rows_written