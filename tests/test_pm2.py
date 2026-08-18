import time

from app.collectors import pm2_processes
from app.config import Settings
from app.models import Alert, PM2Event, PM2ProcessMetrics
from app.pm2_utils import _float_value, parse_jlist, tail_lines
from sqlalchemy import select


def _settings(**kw):
    return Settings(_env_file=None, **kw)


def _raw_proc(name, pid, status="online", restarts=0, cpu=1.5, mem=100 * 2 ** 20,
              loop="1.2ms", heap="50.0 MB", unstable=0):
    return {
        "name": name, "pm_id": 0, "pid": pid, "status": status,
        "restart_time": restarts, "unstable_restarts": unstable,
        "monit": {"cpu": cpu, "memory": mem},
        "pm2_env": {
            "status": status, "restart_time": restarts,
            "pm_uptime": int(time.time() * 1000 - 60000),
            "pm_out_log_path": f"/tmp/{name}-out-0.log",
            "pm_err_log_path": f"/tmp/{name}-error-0.log",
        },
        "axm_monitor": {"Loop delay": {"value": loop}, "Used Heap Size": {"value": heap}},
    }


def _proc(name, pid, status="online", restarts=0, cpu=1.5, conns=2):
    return {
        "name": name, "pm_id": 0, "pid": pid, "status": status,
        "cpu": cpu, "memory_rss": 100 * 2 ** 20, "memory_heap": 50 * 2 ** 20,
        "loop_delay": 1.2, "uptime_ms": 60000, "restarts": restarts,
        "unstable_restarts": 0, "mysql_connections": conns,
        "out_log": f"/tmp/{name}-out-0.log", "err_log": f"/tmp/{name}-error-0.log",
    }


def test_float_value_parses_units():
    assert _float_value("2.5ms") == 2.5
    assert _float_value("128.0 MB") == 128.0
    assert _float_value(None) == 0.0
    assert _float_value(7) == 7.0


def test_parse_jlist_normalizes(session):
    raw = [_raw_proc("shiksha-erp-api", 1234, loop="3.5ms", heap="64.0 MB")]
    procs = parse_jlist(raw)
    assert len(procs) == 1
    p = procs[0]
    assert p["name"] == "shiksha-erp-api"
    assert p["status"] == "online"
    assert p["loop_delay"] == 3.5
    assert p["memory_heap"] == 64 * 1024 * 1024
    assert p["restarts"] == 0
    assert p["out_log"].endswith("out-0.log")


def test_collector_writes_metrics_and_events(session, monkeypatch):
    conns = {100: 3, 200: 0}
    state = {"api": _proc("api", 100, conns=3), "worker": _proc("worker", 200, conns=0)}

    def fake_jlist():
        return list(state.values())

    monkeypatch.setattr(pm2_processes, "parse_jlist", fake_jlist)
    monkeypatch.setattr(pm2_processes, "count_mysql_connections",
                        lambda pid, port=3306: conns.get(pid, 0))
    monkeypatch.setattr(pm2_processes, "get_settings",
                        lambda: _settings(pm2_apps=[], pm2_pool_size=5))

    # First run: baseline, no events.
    assert pm2_processes.collect(session) == 2
    assert len(session.scalars(select(PM2ProcessMetrics)).all()) == 2
    assert len(session.scalars(select(PM2Event)).all()) == 0

    # Second run: api restarted twice and its pool filled (5/5) -> events + alerts.
    conns[100] = 5
    state["api"] = _proc("api", 100, restarts=2, conns=5)
    pm2_processes.collect(session)
    events = {e.event_type for e in session.scalars(select(PM2Event)).all()}
    assert "restart" in events
    assert "pool_warn" in events

    types = {a.type for a in session.scalars(select(Alert)).all()}
    assert "pm2_restart:api" in types
    assert "pm2_pool_warn:api" in types

    # Third run: api crashed (status errored) -> crash event + critical alert.
    state["api"] = _proc("api", None, status="errored", restarts=3, conns=0)
    pm2_processes.collect(session)
    latest = session.scalar(
        select(PM2ProcessMetrics).where(PM2ProcessMetrics.name == "api")
        .order_by(PM2ProcessMetrics.timestamp.desc()).limit(1)
    )
    assert latest.status == "errored"
    crash_types = {a.type for a in session.scalars(select(Alert)).all()}
    assert "pm2_errored:api" in crash_types


def test_collector_empty_jlist(session, monkeypatch):
    monkeypatch.setattr(pm2_processes, "parse_jlist", lambda: [])
    monkeypatch.setattr(pm2_processes, "count_mysql_connections", lambda pid, port=3306: 0)
    monkeypatch.setattr(pm2_processes, "get_settings", lambda: _settings(pm2_apps=[]))
    assert pm2_processes.collect(session) == 0


def test_tail_lines(tmp_path):
    path = tmp_path / "app.log"
    path.write_text("\n".join(f"line {i}" for i in range(300)))
    lines = tail_lines(str(path), 200)
    assert len(lines) == 200
    assert lines[0] == "line 100"
    assert lines[-1] == "line 299"


def test_tail_lines_missing_file():
    assert tail_lines("/does/not/exist.log", 50) == []