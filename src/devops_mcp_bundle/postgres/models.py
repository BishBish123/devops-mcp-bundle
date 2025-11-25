"""Pydantic models that FastMCP introspects to publish JSON Schema."""

from __future__ import annotations

from pydantic import BaseModel, Field


class DatabaseInfo(BaseModel):
    """One row from `pg_database` filtered to non-template DBs."""

    name: str
    owner: str
    encoding: str
    size_bytes: int = Field(description="On-disk size from pg_database_size().")


class ColumnInfo(BaseModel):
    name: str
    data_type: str
    is_nullable: bool
    default: str | None = None


class IndexInfo(BaseModel):
    name: str
    definition: str = Field(description="Full CREATE INDEX statement.")
    is_unique: bool
    is_primary: bool


class TableInfo(BaseModel):
    schema_: str = Field(alias="schema")
    name: str
    row_estimate: int = Field(description="From pg_class.reltuples — fast but stale.")
    size_bytes: int

    model_config = {"populate_by_name": True}


class TableSchema(BaseModel):
    schema_: str = Field(alias="schema")
    name: str
    columns: list[ColumnInfo]
    indexes: list[IndexInfo]

    model_config = {"populate_by_name": True}

class SlowQuery(BaseModel):
    """One row from pg_stat_statements above the configured threshold."""

    query: str = Field(description="Normalized SQL — parameters replaced with placeholders.")
    calls: int
    total_exec_time_ms: float
    mean_exec_time_ms: float
    rows: int
    shared_blks_hit: int
    shared_blks_read: int


class VacuumStatus(BaseModel):
    schema_: str = Field(alias="schema")
    name: str
    last_vacuum: str | None
    last_autovacuum: str | None
    last_analyze: str | None
    last_autoanalyze: str | None
    n_dead_tup: int
    n_live_tup: int
    autovacuum_vacuum_scale_factor: float | None

    model_config = {"populate_by_name": True}


class QueryResult(BaseModel):
    """Result envelope for `run_safe_query`."""

    columns: list[str]
    rows: list[list[object]]
    row_count: int
    elapsed_ms: float

class ActivitySnapshot(BaseModel):
    """One row from `pg_stat_activity`, narrowed to the columns triage uses.

    The full view has ~30 columns; we expose the subset that's actually
    actionable when answering "what's the database doing right now?".
    """

    pid: int
    datname: str | None
    usename: str | None
    application_name: str | None
    state: str | None = Field(description="active | idle | idle in transaction | …")
    wait_event_type: str | None
    wait_event: str | None
    backend_start: str | None
    xact_start: str | None
    query_start: str | None
    query: str | None
    runtime_ms: float = Field(description="now() - query_start, in milliseconds.")
class StatementClass(BaseModel):
    """Classification result for a SQL statement.

    Returned by `classify_sql` so the agent can render *why* a statement
    is or isn't allowed without the caller having to dig through parser
    internals.
    """

    is_read_only: bool
    leading_keyword: str | None
    reason: str = Field(description="Human-readable explanation for the classification.")
