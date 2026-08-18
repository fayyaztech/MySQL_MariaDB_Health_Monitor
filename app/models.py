"""SQLAlchemy ORM models for the monitoring history database."""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class MySqlServer(Base):
    __tablename__ = "mysql_servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=3306)
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    uptime_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    threads_connected: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_connections: Mapped[int | None] = mapped_column(Integer, nullable=True)
    database_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    innodb_buffer_pool_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (UniqueConstraint("hostname", "port", name="uq_server_host_port"),)


class QueryStats(Base):
    """Per-digest statement statistics captured from performance_schema.

    Values are deltas between successive snapshots (per scheduler run).
    """
    __tablename__ = "query_stats"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("mysql_servers.id"))
    digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    query_text: Mapped[str] = mapped_column(Text)  # DIGEST_TEXT
    schema_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    calls: Mapped[int] = mapped_column(Integer, default=0)
    total_ms: Mapped[float] = mapped_column(Float, default=0.0)
    avg_ms: Mapped[float] = mapped_column(Float, default=0.0)
    max_ms: Mapped[float] = mapped_column(Float, default=0.0)
    rows_examined: Mapped[int] = mapped_column(Integer, default=0)
    rows_sent: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SystemMetrics(Base):
    __tablename__ = "system_metrics"

    id: Mapped[int] = mapped_column(primary_key=True)
    cpu: Mapped[float] = mapped_column(Float, default=0.0)
    cpu_per_core: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    load_avg: Mapped[float] = mapped_column(Float, default=0.0)
    load_avg_5: Mapped[float] = mapped_column(Float, default=0.0)
    load_avg_15: Mapped[float] = mapped_column(Float, default=0.0)
    cpu_freq: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_total: Mapped[int] = mapped_column(Integer, default=0)
    mem_used: Mapped[int] = mapped_column(Integer, default=0)
    mem_avail: Mapped[int] = mapped_column(Integer, default=0)
    swap_used: Mapped[int] = mapped_column(Integer, default=0)
    swap_total: Mapped[int] = mapped_column(Integer, default=0)
    disk_used: Mapped[int] = mapped_column(Integer, default=0)
    disk_total: Mapped[int] = mapped_column(Integer, default=0)
    disk_read: Mapped[int] = mapped_column(Integer, default=0)
    disk_write: Mapped[int] = mapped_column(Integer, default=0)
    net_in: Mapped[int] = mapped_column(Integer, default=0)
    net_out: Mapped[int] = mapped_column(Integer, default=0)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ProcessMetrics(Base):
    __tablename__ = "process_metrics"

    id: Mapped[int] = mapped_column(primary_key=True)
    process_name: Mapped[str] = mapped_column(String(64))
    cpu_percent: Mapped[float] = mapped_column(Float, default=0.0)
    memory_rss: Mapped[int] = mapped_column(Integer, default=0)
    threads: Mapped[int] = mapped_column(Integer, default=0)
    open_files: Mapped[int] = mapped_column(Integer, default=0)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class InnoDBMetrics(Base):
    __tablename__ = "innodb_metrics"

    id: Mapped[int] = mapped_column(primary_key=True)
    buffer_hit_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    deadlocks: Mapped[int] = mapped_column(Integer, default=0)
    dirty_pages: Mapped[int] = mapped_column(Integer, default=0)
    pending_io: Mapped[int] = mapped_column(Integer, default=0)
    history_list_len: Mapped[int] = mapped_column(Integer, default=0)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(64))  # e.g. cpu_high, slow_query
    severity: Mapped[str] = mapped_column(String(16), default="warning")  # info|warning|critical
    message: Mapped[str] = mapped_column(Text)
    value: Mapped[float] = mapped_column(Float, default=0.0)
    threshold: Mapped[float] = mapped_column(Float, default=0.0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Recommendation(Base):
    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(64))  # index, memory, config
    title: Mapped[str] = mapped_column(String(255))
    detail: Mapped[str] = mapped_column(Text)
    sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[str] = mapped_column(String(16), default="info")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    health_score: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PM2ProcessMetrics(Base):
    """Per-poll snapshot of a PM2-managed process (jlist + OS introspection)."""
    __tablename__ = "pm2_process_metrics"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    pm_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="unknown")
    cpu: Mapped[float] = mapped_column(Float, default=0.0)
    memory_rss: Mapped[int] = mapped_column(Integer, default=0)
    memory_heap: Mapped[int] = mapped_column(Integer, default=0)
    loop_delay: Mapped[float] = mapped_column(Float, default=0.0)
    uptime_ms: Mapped[int] = mapped_column(Integer, default=0)
    restarts: Mapped[int] = mapped_column(Integer, default=0)
    unstable_restarts: Mapped[int] = mapped_column(Integer, default=0)
    mysql_connections: Mapped[int] = mapped_column(Integer, default=0)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class PM2Event(Base):
    """PM2 lifecycle events: crash / restart / status change / pool warning."""
    __tablename__ = "pm2_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    process_name: Mapped[str] = mapped_column(String(128), index=True)
    event_type: Mapped[str] = mapped_column(String(32))  # crash|restart|stopped|errored|online|pool_warn
    detail: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
