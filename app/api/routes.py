"""FastAPI routes: JSON API + dashboard pages.

Main dashboard (/) is a single page with all graphs, updated live over a
WebSocket. The /logs page lists alerts with a date-range filter and a Live
toggle. JSON endpoints under /api/* accept API-key auth.
Dashboard HTML pages are protected by a session cookie (2-hour TTL).
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import bcrypt
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, get_session_factory
from app.diagnose import registry
from app.models import (
    Alert,
    MySqlServer,
    PM2Event,
    PM2ProcessMetrics,
    Recommendation,
    Report,
    SystemMetrics,
)

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# --- Auth helpers -------------------------------------------------------------

def _password_enabled() -> bool:
    return bool(get_settings().dashboard_password_hash)


def _session_ok(request: Request) -> bool:
    return request.session.get("authenticated") is True


def _require_session(request: Request):
    """Redirect to /login if no valid session (only when password is set)."""
    if _password_enabled() and not _session_ok(request):
        return RedirectResponse("/login", status_code=302)
    return None


def require_key(request: Request):
    settings = get_settings()
    token = request.headers.get("X-API-Key", "")
    if settings.api_token != "changeme-token" and token != settings.api_token:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return None


# --- snapshot helpers (shared by initial render + WebSocket) -----------------

def _server(db: Session) -> dict | None:
    row = db.scalar(select(MySqlServer).limit(1))
    if row is None:
        return None
    return {
        "hostname": row.hostname,
        "port": row.port,
        "version": row.version,
        "uptime_seconds": row.uptime_seconds,
        "threads_connected": row.threads_connected,
        "max_connections": row.max_connections,
        "database_size_bytes": row.database_size_bytes,
        "innodb_buffer_pool_size": row.innodb_buffer_pool_size,
    }


def _iso(dt) -> str | None:
    return dt.isoformat() if dt is not None else None


def _latest_system(db: Session) -> dict | None:
    row = db.scalar(select(SystemMetrics).order_by(SystemMetrics.timestamp.desc()).limit(1))
    if row is None:
        return None
    return {
        "timestamp": _iso(row.timestamp),
        "cpu": row.cpu,
        "cpu_per_core": list(row.cpu_per_core) if row.cpu_per_core else [],
        "load_avg": row.load_avg,
        "load_avg_5": row.load_avg_5,
        "load_avg_15": row.load_avg_15,
        "mem_total": row.mem_total,
        "mem_used": row.mem_used,
        "mem_avail": row.mem_avail,
        "mem_pct": round(row.mem_used / row.mem_total * 100, 1) if row.mem_total else 0.0,
        "swap_pct": round(row.swap_used / row.swap_total * 100, 1) if row.swap_total else 0.0,
        "disk_used": row.disk_used,
        "disk_total": row.disk_total,
        "disk_pct": round(row.disk_used / row.disk_total * 100, 1) if row.disk_total else 0.0,
        "net_in": row.net_in,
        "net_out": row.net_out,
    }


def _system_row(r) -> dict:
    return {
        "timestamp": _iso(r.timestamp),
        "cpu": r.cpu,
        "cpu_per_core": list(r.cpu_per_core) if r.cpu_per_core else [],
        "load_avg": r.load_avg,
        "load_avg_5": r.load_avg_5,
        "load_avg_15": r.load_avg_15,
        "mem_used": r.mem_used,
        "mem_total": r.mem_total,
        "mem_pct": round(r.mem_used / r.mem_total * 100, 1) if r.mem_total else 0.0,
        "swap_pct": round(r.swap_used / r.swap_total * 100, 1) if r.swap_total else 0.0,
        "disk_used": r.disk_used,
        "disk_total": r.disk_total,
        "disk_pct": round(r.disk_used / r.disk_total * 100, 1) if r.disk_total else 0.0,
        "net_in": r.net_in,
        "net_out": r.net_out,
    }


def _recent_system(db: Session, limit: int = 120) -> list[dict]:
    rows = db.execute(
        select(SystemMetrics).order_by(SystemMetrics.timestamp.desc()).limit(limit)
    ).scalars().all()
    return [_system_row(r) for r in reversed(rows)]


def _query_rankings(db: Session, limit: int = 8) -> list[dict]:
    from app.analyzers.query_analyzer import most_expensive
    rankings = most_expensive(db, limit)
    for r in rankings:
        r["last_seen"] = _iso(r.get("last_seen"))
    return rankings


def _active_alerts(db: Session) -> int:
    return db.scalar(select(func.count(Alert.id)).where(Alert.active == True)) or 0  # noqa: E712


def _server_brief(db: Session) -> dict:
    """Light per-tick server fields so connection/database tiles stay live."""
    row = db.scalar(select(MySqlServer).limit(1))
    if row is None:
        return {"threads_connected": None, "max_connections": None,
                "database_size_bytes": None}
    return {
        "threads_connected": row.threads_connected,
        "max_connections": row.max_connections,
        "database_size_bytes": row.database_size_bytes,
    }


def _snapshot(db: Session) -> dict:
    return {
        "server": _server(db),
        "latest": _latest_system(db),
        "series": _recent_system(db, 120),
        "expensive": _query_rankings(db),
        "alerts_active": _active_alerts(db),
    }


# --- WebSocket live updates --------------------------------------------------

@router.websocket("/ws")
async def ws_live(websocket: WebSocket):
    # Protect WS with session cookie when password auth is enabled
    if _password_enabled():
        session = websocket.session  # Starlette populates this via SessionMiddleware
        if not session.get("authenticated"):
            await websocket.close(code=4403)
            return
    await websocket.accept()
    factory = get_session_factory()

    # One-time full snapshot so the client renders history immediately.
    db = factory()
    try:
        snapshot = _snapshot(db)
    finally:
        db.close()
    await websocket.send_json({"type": "init", "snapshot": snapshot})

    # Incremental single-point updates every 1s (light payload, ~1s latency).
    try:
        while True:
            db = factory()
            try:
                latest = _latest_system(db)
                server_brief = _server_brief(db)
                alerts_active = _active_alerts(db)
            finally:
                db.close()
            await websocket.send_json(
                {"type": "update", "latest": latest, "server": server_brief,
                 "alerts_active": alerts_active}
            )
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await websocket.close()
        except Exception:
            pass


# --- JSON API ----------------------------------------------------------------

@router.get("/api/overview")
def api_overview(_: None = Depends(require_key), db: Session = Depends(get_db)):
    return _snapshot(db)


@router.get("/api/queries")
def api_queries(limit: int = Query(20), _: None = Depends(require_key),
                db: Session = Depends(get_db)):
    from app.analyzers.query_analyzer import most_expensive, worst_latency
    return {"most_expensive": most_expensive(db, limit),
            "worst_latency": worst_latency(db, limit)}


@router.get("/api/server-health")
def api_health(_: None = Depends(require_key), db: Session = Depends(get_db)):
    return {"system_metrics": _recent_system(db)}


@router.get("/api/system")
def api_system(from_: str = Query(None, alias="from"),
               to_: str = Query(None, alias="to"),
               limit: int = Query(2000, le=10000),
               _: None = Depends(require_key), db: Session = Depends(get_db)):
    """System metric history (incl. load averages) over a date range."""
    stmt = select(SystemMetrics)
    if from_ := _parse_dt(from_):
        stmt = stmt.where(SystemMetrics.timestamp >= from_)
    if to := _parse_dt(to_):
        stmt = stmt.where(SystemMetrics.timestamp <= to)
    rows = db.execute(
        stmt.order_by(SystemMetrics.timestamp.desc()).limit(limit)
    ).scalars().all()
    return [_system_row(r) for r in reversed(rows)]


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@router.get("/api/alerts")
def api_alerts(from_: str = Query(None, alias="from"),
               to_: str = Query(None, alias="to"),
               _: None = Depends(require_key), db: Session = Depends(get_db)):
    stmt = select(Alert).order_by(Alert.created_at.desc()).limit(500)
    if from_ := _parse_dt(from_):
        stmt = stmt.where(Alert.created_at >= from_)
    if to := _parse_dt(to_):
        stmt = stmt.where(Alert.created_at <= to)
    rows = db.scalars(stmt).all()
    return [
        {"id": a.id, "type": a.type, "severity": a.severity, "message": a.message,
         "value": a.value, "threshold": a.threshold, "active": a.active,
         "created_at": a.created_at}
        for a in rows
    ]


@router.get("/api/alerts/summary")
def api_alerts_summary(from_: str = Query(None, alias="from"),
                       to_: str = Query(None, alias="to"),
                       _: None = Depends(require_key), db: Session = Depends(get_db)):
    stmt = (select(func.date(Alert.created_at).label("day"), Alert.severity,
                   func.count(Alert.id).label("n"))
            .group_by("day", Alert.severity).order_by("day"))
    if from_ := _parse_dt(from_):
        stmt = stmt.where(Alert.created_at >= from_)
    if to := _parse_dt(to_):
        stmt = stmt.where(Alert.created_at <= to)
    return [{"day": r.day, "severity": r.severity, "count": r.n} for r in db.execute(stmt).all()]


@router.get("/api/recommendations")
def api_recommendations(_: None = Depends(require_key), db: Session = Depends(get_db)):
    rows = db.scalars(select(Recommendation).order_by(Recommendation.created_at.desc()).limit(100)).all()
    return [{"type": r.type, "title": r.title, "detail": r.detail,
             "sql": r.sql, "severity": r.severity, "created_at": r.created_at} for r in rows]


@router.get("/api/reports")
def api_reports(_: None = Depends(require_key), db: Session = Depends(get_db)):
    rows = db.scalars(select(Report).order_by(Report.created_at.desc()).limit(50)).all()
    return [{"filename": r.filename, "health_score": r.health_score,
             "created_at": r.created_at} for r in rows]


# --- PM2 endpoints ----------------------------------------------------------

def _pm2_live() -> list[dict]:
    """Current PM2 process state from the local pm2 daemon (best-effort)."""
    from app.config import get_settings
    from app.pm2_utils import count_mysql_connections, parse_jlist

    settings = get_settings()
    include = {a for a in settings.pm2_apps} if settings.pm2_apps else None
    out = []
    for p in parse_jlist():
        if include is not None and p["name"] not in include:
            continue
        p["mysql_connections"] = count_mysql_connections(p["pid"], settings.pm2_mysql_port)
        out.append(p)
    return out


@router.get("/api/pm2/processes")
def api_pm2_processes(_: None = Depends(require_key)):
    from app.config import get_settings
    settings = get_settings()
    return {"processes": _pm2_live(), "pool_size": settings.pm2_pool_size,
            "enabled": settings.pm2_enabled}


@router.get("/api/pm2/history")
def api_pm2_history(name: str = Query(..., description="PM2 app name"),
                    limit: int = Query(200, le=2000),
                    _: None = Depends(require_key), db: Session = Depends(get_db)):
    rows = db.execute(
        select(PM2ProcessMetrics).where(PM2ProcessMetrics.name == name)
        .order_by(PM2ProcessMetrics.timestamp.desc()).limit(limit)
    ).scalars().all()
    return [{
        "timestamp": _iso(r.timestamp), "status": r.status, "cpu": r.cpu,
        "memory_rss": r.memory_rss, "memory_heap": r.memory_heap,
        "loop_delay": r.loop_delay, "uptime_ms": r.uptime_ms,
        "restarts": r.restarts, "mysql_connections": r.mysql_connections,
    } for r in reversed(rows)]


@router.get("/api/pm2/logs")
def api_pm2_logs(name: str = Query(...), stream: str = Query("out", pattern="^(out|error)$"),
                 lines: int = Query(200, ge=1, le=5000),
                 _: None = Depends(require_key)):
    from app.pm2_utils import tail_lines
    settings = get_settings()

    path = None
    for p in _pm2_live():
        if p["name"] == name:
            path = p["error_log" if stream == "error" else "out_log"]
            break
    if path is None:
        path = f"{settings.pm2_log_dir}/{name}-{stream}-0.log"

    entries = tail_lines(path, lines)
    return {"process": name, "stream": stream, "path": path,
            "lines": entries, "count": len(entries)}


@router.get("/api/pm2/events")
def api_pm2_events(from_: str = Query(None, alias="from"),
                   to_: str = Query(None, alias="to"),
                   _: None = Depends(require_key), db: Session = Depends(get_db)):
    stmt = select(PM2Event).order_by(PM2Event.timestamp.desc()).limit(500)
    if from_ := _parse_dt(from_):
        stmt = stmt.where(PM2Event.timestamp >= from_)
    if to := _parse_dt(to_):
        stmt = stmt.where(PM2Event.timestamp <= to)
    rows = db.scalars(stmt).all()
    return [{"id": e.id, "process_name": e.process_name, "event_type": e.event_type,
             "detail": e.detail, "timestamp": e.timestamp} for e in rows]


@router.post("/api/pm2/diagnose")
def api_pm2_diagnose(app: str = Query(...),
                     _: None = Depends(require_key)):
    settings = get_settings()
    if not settings.pm2_diagnose_enabled:
        raise HTTPException(status_code=403, detail="PM2 diagnostics disabled")
    from app.diagnostics import run_diagnostic
    # Pass the monitor's DB credentials so the MySQL sections run non-interactively.
    db_env = {
        "host": settings.mysql_host,
        "user": settings.mysql_user,
        "password": settings.mysql_password,
        "database": settings.mysql_database,
    }
    job = registry.start(app, run_diagnostic, args=(app,), kwargs={"db_env": db_env})
    return {"job_id": job.id, "app": app, "started": True}


@router.get("/api/pm2/diagnose/{job_id}")
def api_pm2_diagnose_status(job_id: str, _: None = Depends(require_key)):
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown diagnostic job")
    return {
        "job_id": job.id, "done": job.done, "returncode": job.returncode,
        "output": job.read(),
    }


# --- Dashboard pages ---------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if _session_ok(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, password: str = Form(...)):
    settings = get_settings()
    pw_hash = settings.dashboard_password_hash
    if pw_hash and bcrypt.checkpw(password.encode(), pw_hash.encode()):
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": "Invalid password"}, status_code=401)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    redir = _require_session(request)
    if redir is not None:
        return redir
    return templates.TemplateResponse(request, "main.html", {"snapshot": _snapshot(db)})


@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request):
    redir = _require_session(request)
    if redir is not None:
        return redir
    return templates.TemplateResponse(request, "logs.html", {})


@router.get("/pm2", response_class=HTMLResponse)
def pm2_page(request: Request):
    redir = _require_session(request)
    if redir is not None:
        return redir
    return templates.TemplateResponse(request, "pm2.html", {})
