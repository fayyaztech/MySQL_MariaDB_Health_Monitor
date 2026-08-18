import io
import time

from app import diagnostics
from app.diagnose import DiagnoseJob
from app.diagnostics import (
    Reporter,
    dotenv_keys,
    resolve_db_credentials,
    resolve_pid,
    resolve_port,
    run_diagnostic,
)


class FakeConn:
    def __init__(self, pid, status, port):
        self.pid, self.status = pid, status
        self.laddr = type("A", (), {"port": port})() if port else None


def _monkey_connect(monkeypatch, hosts):
    monkeypatch.setattr(diagnostics.psutil, "net_connections",
                        lambda kind="inet": hosts)


def test_reporter_streams(tmp_path):
    buf = io.StringIO()
    rep = Reporter(buf)
    rep.section("HELLO")
    rep.write("line one")
    assert "line one" in buf.getvalue()
    assert "HELLO" in rep.text()
    assert rep.lines[0] == ""


def test_dotenv_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nDB_USER=\"appuser\"\nDB_PASS='secret'\nOTHER=1\n")
    got = dotenv_keys(str(env), {"DB_USER", "DB_PASS"})
    assert got == {"DB_USER": "appuser", "DB_PASS": "secret"}


def test_resolve_db_credentials_uses_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "dotenv_keys", lambda path, keys: {})
    creds = resolve_db_credentials(str(tmp_path), {
        "host": "db.local", "user": "monitor", "password": "pw", "database": "db",
    })
    assert creds == {"host": "db.local", "user": "monitor",
                     "password": "pw", "database": "db"}


def test_resolve_db_credentials_falls_back_to_app_env(tmp_path, monkeypatch):
    app_env = tmp_path / ".env"
    app_env.write_text("DB_HOST=1.2.3.4\nDB_USER=app\nDB_PASSWORD=pw\nDB_NAME=erp\n")
    monkeypatch.setattr(diagnostics, "dotenv_keys",
                        lambda path, keys: dotenv_keys(path, keys) if "DB_" in str(keys) else {})
    creds = resolve_db_credentials(str(tmp_path))
    assert creds["user"] == "app"
    assert creds["password"] == "pw"
    assert creds["host"] == "1.2.3.4"
    assert creds["database"] == "erp"


def test_resolve_pid_none_when_app_missing(monkeypatch):
    monkeypatch.setattr(diagnostics, "run_cmd", lambda *a, **k: (0, "", ""))
    assert resolve_pid("does-not-exist") is None


def test_resolve_port_autodetect_and_fallback(monkeypatch):
    _monkey_connect(monkeypatch, [FakeConn(42, "LISTEN", 9999)])
    assert resolve_port(42) == 9999
    assert resolve_port(42, port_arg=4321) == 4321


def test_resolve_port_fallback_on_error(monkeypatch):
    def boom(kind="inet"):
        raise OSError("no permission")
    monkeypatch.setattr(diagnostics.psutil, "net_connections", boom)
    assert resolve_port(1234) == 3021


def test_run_diagnostic_no_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "resolve_pid", lambda app: None)
    report = run_diagnostic("shiksha-erp-api", port=3021, app_root=str(tmp_path))
    assert "PM2 PROCESS DIAGNOSTIC" in report
    assert "Could not determine PM2 PID" in report


def test_diagnose_job_streams_and_marks_done():
    def fake_fn(app, *, out):
        out.write("hello\n")
        out.write("world\n")

    job = DiagnoseJob("shiksha-erp-api", fake_fn, args=("shiksha-erp-api",))
    job.start()
    deadline = time.time() + 5
    while not job.done and time.time() < deadline:
        time.sleep(0.02)
    assert job.done
    assert job.returncode == 0
    assert "hello" in job.read()
    assert "world" in job.read()