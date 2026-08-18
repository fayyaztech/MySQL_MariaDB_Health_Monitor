"""APScheduler job registrations.

Collectors run in-process inside the API app's lifespan (background scheduler).
Each job opens its own DB session and MySQL connection so failures are isolated.
"""
import logging
from contextlib import contextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from app.config import get_settings
from app.db import session_scope

logger = logging.getLogger(__name__)

# Registered collectors keyed by name -> callable(session, conn) -> int rows written.
# Populated as collector modules are wired in.
COLLECTORS: dict = {}


def _collect(name: str) -> None:
    """Run a single collector with its own session + fresh MySQL connection."""
    if name not in COLLECTORS:
        logger.warning("No collector registered for %r", name)
        return
    try:
        with session_scope() as session:
            from app.mysql_connection import connect
            conn = connect()
            try:
                rows = COLLECTORS[name](session, conn)
                logger.info("collector=%s rows=%s", name, rows)
            finally:
                try:
                    conn.close()
                except Exception:  # pragma: no cover - best effort
                    pass
    except Exception:
        logger.exception("collector=%s failed", name)


def register(name: str, fn) -> None:
    """Register a collector: fn(session, conn) -> rows written."""
    COLLECTORS[name] = fn


def build_scheduler() -> BackgroundScheduler:
    settings = get_settings()
    scheduler = BackgroundScheduler(timezone="UTC")

    if "system" in COLLECTORS:
        scheduler.add_job(
            _collect,
            args=["system"],
            trigger=IntervalTrigger(seconds=settings.system_interval),
            id="collect-system",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
    if "mysql" in COLLECTORS:
        scheduler.add_job(
            _collect,
            args=["mysql"],
            trigger=IntervalTrigger(seconds=settings.mysql_interval),
            id="collect-mysql",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
    if "processlist" in COLLECTORS:
        scheduler.add_job(
            _collect,
            args=["processlist"],
            trigger=IntervalTrigger(seconds=settings.mysql_interval),
            id="collect-processlist",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
    if "slow_queries" in COLLECTORS:
        scheduler.add_job(
            _collect,
            args=["slow_queries"],
            trigger=IntervalTrigger(seconds=settings.mysql_interval),
            id="collect-slow",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
    if "innodb" in COLLECTORS:
        scheduler.add_job(
            _collect,
            args=["innodb"],
            trigger=IntervalTrigger(seconds=settings.mysql_interval),
            id="collect-innodb",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
    if settings.pm2_enabled and "pm2" in COLLECTORS:
        scheduler.add_job(
            _collect,
            args=["pm2"],
            trigger=IntervalTrigger(seconds=settings.pm2_interval),
            id="collect-pm2",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
    if "analyze" in COLLECTORS:
        scheduler.add_job(
            _collect,
            args=["analyze"],
            trigger=IntervalTrigger(seconds=settings.analyze_interval),
            id="analyze",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
    if "report" in COLLECTORS:
        scheduler.add_job(
            _collect,
            args=["report"],
            trigger=CronTrigger(hour=settings.report_hour, minute=5),
            id="daily-report",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
    return scheduler
