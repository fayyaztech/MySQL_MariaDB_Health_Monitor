"""
Single place for all monitor configuration. Edit this file to change anything
(hardware, credentials, sample rate, thresholds, paths). Every value can also be
overridden from the command line where noted.
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------
# MariaDB / MySQL connection
# --------------------------------------------------------------------------
DB_HOST = "127.0.0.1"
DB_PORT = 3306
DB_USER = "root"
DB_PASSWORD = "password"
DB_NAME = "shiksha-core-module"   # default database used for the connection

# Connection timeout (seconds). The monitor retries forever if the DB is down,
# so a short connect timeout lets it keep sampling Linux metrics meanwhile.
DB_CONNECT_TIMEOUT = 5
DB_READ_TIMEOUT = 10

# --------------------------------------------------------------------------
# Sampling
# --------------------------------------------------------------------------
# Seconds between samples. Change this to 15 / 30 / whatever you need.
SAMPLE_INTERVAL = 10

# --------------------------------------------------------------------------
# Alarm / snapshot thresholds
# --------------------------------------------------------------------------
# When Threads_running exceeds this, a snapshot is appended to
# processlist.log + innodb_status.log + slow_query_snapshot.log.
THREADS_RUNNING_THRESHOLD = 10

# A query running longer than this (seconds) is considered "long-running"
# and recorded into slow_query_snapshot.log on a snapshot.
SLOW_QUERY_SECONDS = 5

# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
DATA_DIR = os.path.join(BASE_DIR, "data")
GRAPHS_DIR = os.path.join(BASE_DIR, "graphs")

# Keep daily CSV files / graphs this many days, then auto-purge.
RETENTION_DAYS = 30

# How many recent rows the dashboard serves from the current CSV (history).
DASHBOARD_HISTORY_ROWS = 500
