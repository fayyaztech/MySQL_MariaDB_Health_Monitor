"""
Single place for all monitor configuration.

Every value is read from the environment — edit the .env file (created by setup.sh)
to change anything without touching code.  Values that aren't set in the
environment fall back to the sensible defaults shown here.
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── auto-load .env ───────────────────────────────────────────────────────────
# Parse .env from the project root (if it exists) into os.environ before
# resolving any setting, so every script picks up the file automatically.
_env_path = os.path.join(BASE_DIR, ".env")
if os.path.isfile(_env_path):
    with open(_env_path) as _fh:
        for _line in _fh:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _key, _, _val = _line.partition("=")
            _key = _key.strip()
            _val = _val.strip().strip("\"'")
            os.environ.setdefault(_key, _val)

# ── helpers ──────────────────────────────────────────────────────────────────

def _env(key, default):
    return os.environ.get(key, default)

def _env_int(key, default):
    v = os.environ.get(key, "")
    return int(v) if v else default

def _env_float(key, default):
    v = os.environ.get(key, "")
    return float(v) if v else default

# ── MariaDB / MySQL connection ───────────────────────────────────────────────

DB_HOST             = _env("DB_HOST", "127.0.0.1")
DB_PORT             = _env_int("DB_PORT", 3306)
DB_USER             = _env("DB_USER", "root")
DB_PASSWORD          = _env("DB_PASSWORD", "password")
DB_NAME             = _env("DB_NAME", "shiksha-core-module")

DB_CONNECT_TIMEOUT  = _env_int("DB_CONNECT_TIMEOUT", 5)
DB_READ_TIMEOUT     = _env_int("DB_READ_TIMEOUT", 10)

# ── sampling ─────────────────────────────────────────────────────────────────

SAMPLE_INTERVAL     = _env_float("SAMPLE_INTERVAL", 10)

# ── alarm / snapshot thresholds ──────────────────────────────────────────────

THREADS_RUNNING_THRESHOLD = _env_int("THREADS_RUNNING_THRESHOLD", 10)
SLOW_QUERY_SECONDS        = _env_int("SLOW_QUERY_SECONDS", 5)

# ── storage ──────────────────────────────────────────────────────────────────

DATA_DIR   = os.path.join(BASE_DIR, _env("DATA_DIR", "data"))
GRAPHS_DIR = os.path.join(BASE_DIR, _env("GRAPHS_DIR", "graphs"))
RETENTION_DAYS = _env_int("RETENTION_DAYS", 30)

# ── dashboard ────────────────────────────────────────────────────────────────

DASHBOARD_HOST        = _env("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT        = _env_int("DASHBOARD_PORT", 5000)
DASHBOARD_HISTORY_ROWS = _env_int("DASHBOARD_HISTORY_ROWS", 500)
