"""Read-only SQL enforcement.

The `run_safe_query` tool is the only one that takes user-supplied SQL, so
it has to be locked down hard. Two layers:

1. SQL parse-and-classify (`is_read_only_sql`) — rejects anything that
   isn't a single SELECT/EXPLAIN/SHOW/WITH-RECURSIVE statement before it
   ever reaches Postgres.
2. Server-side: the connection sets `default_transaction_read_only = on`
   and `statement_timeout` per session in the server module, so even if
   the parser is fooled by an exotic injection, the database refuses to
   write.

Both layers are deliberately redundant. The parser catches obvious
mistakes (multiple statements, DML keywords, dollar-quoted code blocks);
the server-side flag catches everything the parser misses.
"""

from __future__ import annotations

import sqlparse
from sqlparse.tokens import DDL, DML, Keyword, Name, Punctuation, String

# Statement-leading tokens we consider read-only. SELECT and friends.
# `ANALYZE` is *not* in this set even though it returns no rows — it
# updates planner statistics, which is a write to pg_statistic.
_READ_ONLY_KEYWORDS: frozenset[str] = frozenset({"SELECT", "EXPLAIN", "WITH", "SHOW", "VALUES"})

# Anything that would mutate state. Comprehensive on purpose — easier to
# extend the read-only set later than to add a new mutator we missed.
_MUTATING_KEYWORDS: frozenset[str] = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "TRUNCATE",
        "CREATE",
        "ALTER",
        "DROP",
        "GRANT",
        "REVOKE",
        "COMMENT",
        "VACUUM",
        "REINDEX",
        "REFRESH",
        "CLUSTER",
        "COPY",  # COPY can write; refuse always
        "CALL",
        "DO",
        "PREPARE",
        "EXECUTE",
        "SET",
        "RESET",
        "BEGIN",
        "COMMIT",
        "ROLLBACK",
        "SAVEPOINT",
        "LOCK",
        "LISTEN",
        "NOTIFY",
        "UNLISTEN",
    }
)

# Pg-builtin / contrib functions that mutate state or have side effects even
# when called from inside a SELECT. The classifier's leading-keyword check
# waves SELECT through, so without this denylist a caller could smuggle
# `SELECT pg_terminate_backend(123)` through the parser. The DB-side
# `default_transaction_read_only` flag catches *most* of these — but not all
# (advisory locks don't count as writes; `dblink_exec` writes on a remote
# host the local read-only flag doesn't govern), and even when it does,
# rejecting at the parser yields a clearer error message that names the
# offending function.
#
# All entries are lowercase; the matcher compares case-insensitively.
_SIDE_EFFECTING_FUNCTIONS: frozenset[str] = frozenset(
    {
        # Backend / cluster control
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_promote",
        # Advisory locks (don't write user data, but acquire locks that
        # block other sessions — incompatible with the read-only contract)
        "pg_advisory_lock",
        "pg_advisory_lock_shared",
        "pg_advisory_xact_lock",
        "pg_advisory_xact_lock_shared",
        "pg_try_advisory_lock",
        "pg_try_advisory_lock_shared",
        "pg_try_advisory_xact_lock",
        "pg_try_advisory_xact_lock_shared",
        "pg_advisory_unlock",
        "pg_advisory_unlock_all",
        "pg_advisory_unlock_shared",
        # Session/local config mutation
        "set_config",
        # Replication-slot management
        "pg_create_logical_replication_slot",
        "pg_drop_replication_slot",
        "pg_replication_slot_advance",
        "pg_create_physical_replication_slot",
        # Snapshot import/export (visible side effects across sessions)
        "pg_export_snapshot",
        "pg_import_snapshot",
        # Logical-decoding message emitter
        "pg_logical_emit_message",
        # WAL / backup control
        "pg_switch_wal",
        "pg_walfile_name",
        "pg_start_backup",
        "pg_stop_backup",
        "pg_backup_start",
        "pg_backup_stop",
        # dblink contrib module — anything that takes SQL or controls a
        # remote connection bypasses the local read-only flag. The remote
        # database has no idea about our local `default_transaction_read_only`
        # setting, so a `SELECT dblink_send_query('conn', 'INSERT ...')`
        # would otherwise sail through the leading-keyword check and the
        # remote DB would happily execute the INSERT. Cover the full
        # contrib surface — synchronous (`dblink_exec`/`dblink`),
        # asynchronous (`dblink_send_query`/`dblink_get_result`/
        # `dblink_cancel_query`), connection lifecycle (`dblink_open`/
        # `dblink_close`/`dblink_disconnect`), result fetching
        # (`dblink_fetch`), and the inspection helpers
        # (`dblink_get_pkey`/`dblink_get_connections`) which are less
        # dangerous on their own but enable hostile-DB-side-channel work
        # against any reachable foreign server.
        "dblink_exec",
        "dblink",
        "dblink_send_query",
        "dblink_get_result",
        "dblink_cancel_query",
        "dblink_open",
        "dblink_close",
        "dblink_fetch",
        "dblink_disconnect",
        "dblink_get_pkey",
        "dblink_get_connections",
        # Large object mutators
        "lo_create",
        "lo_unlink",
        "lo_import",
        "lo_export",
        # Sequence mutators
        "nextval",
        "setval",
    }
)


class Classification:
    """Result of :func:`classify_sql`.

    Plain data carrier; not a Pydantic model so it stays cheap on the hot
    path (every `run_safe_query` call constructs one). Callers that need
    to render this through MCP should reach for
    :class:`devops_mcp_bundle.postgres.models.StatementClass`.
    """

    __slots__ = ("is_read_only", "leading_keyword", "reason")

    def __init__(
        self,
        is_read_only: bool,
        leading_keyword: str | None,
        reason: str,
    ) -> None:
        self.is_read_only = is_read_only
        self.leading_keyword = leading_keyword
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Classification(is_read_only={self.is_read_only!r}, "
            f"leading_keyword={self.leading_keyword!r}, reason={self.reason!r})"
        )


def is_read_only_sql(sql: str) -> bool:
    """Return True iff `sql` parses as a single read-only statement.

    Conservative on purpose: any parse oddity is treated as not-read-only.
    Thin wrapper around :func:`classify_sql` — kept as the primary API
    for the hot path; callers that want a *reason* string should use the
    classifier instead.
    """
    return classify_sql(sql).is_read_only


def classify_sql(sql: str) -> Classification:
    """Classify `sql` as read-only or not, with a human-readable reason.

    Returns a :class:`Classification` so server-side error messages can
    explain *why* a statement was refused without the caller having to
    re-parse the SQL. The decision is the same the production hot path
    makes — this is the single source of truth.
    """
    if not sql or not sql.strip():
        return Classification(False, None, "blank statement")

    statements = list(sqlparse.parse(sql))
    if len(statements) != 1:
        return Classification(False, None, f"expected exactly one statement, got {len(statements)}")

    stmt = statements[0]
    leading: str | None = None
    for token in stmt.tokens:
        if token.is_whitespace:
            continue
        if token.ttype in (DML, DDL, Keyword):
            leading = token.normalized.upper()
            break
        if token.value.startswith("--") or token.value.startswith("/*"):
            continue
        first_word = token.value.strip().split()[:1]
        if first_word:
            leading = first_word[0].upper()
            break

    if leading is None:
        return Classification(False, None, "could not identify leading keyword")
    if leading in _MUTATING_KEYWORDS:
        return Classification(False, leading, f"{leading} mutates state")
    if leading not in _READ_ONLY_KEYWORDS:
        return Classification(False, leading, f"unknown leading keyword {leading!r}")

    # SELECT ... FOR {UPDATE|NO KEY UPDATE|SHARE|KEY SHARE} acquires
    # row-level locks. They don't *write* user data, but they do hold
    # locks — both layers of "read-only" (parser + DB-side
    # `default_transaction_read_only`) refuse them, and they have
    # well-known foot-gun behaviour in production (blocking other
    # writers until the read transaction commits). Reject explicitly
    # so the error message names the lock clause.
    flat = list(stmt.flatten())  # type: ignore[no-untyped-call]
    for i, token in enumerate(flat):
        if token.ttype is not Keyword or token.normalized.upper() != "FOR":
            continue
        # Look at the next non-whitespace tokens to decide what FOR
        # clause this is. PG locking suffixes: SHARE, UPDATE, KEY
        # SHARE, NO KEY UPDATE.
        suffix: list[str] = []
        for follow in flat[i + 1 :]:
            if follow.is_whitespace:
                continue
            if follow.ttype not in (Keyword, DML):
                break
            suffix.append(follow.normalized.upper())
            if len(suffix) >= 3:
                break
        if not suffix:
            continue
        if suffix[0] in ("SHARE", "UPDATE") or (
            suffix[0] in ("KEY", "NO") and any(w in ("SHARE", "UPDATE") for w in suffix)
        ):
            lock = " ".join(suffix[: 3 if suffix[0] == "NO" else 2])
            return Classification(
                False,
                leading,
                f"SELECT ... FOR {lock} acquires row locks; not allowed in read-only mode",
            )

    # Belt-and-suspenders: scan every flattened token for a mutating
    # keyword inside a CTE / subquery / EXPLAIN body.
    for token in flat:
        kw = token.normalized.upper()
        if token.ttype in (DML, DDL) and kw in _MUTATING_KEYWORDS:
            return Classification(False, leading, f"{leading} body contains {kw}")
        if token.ttype is Keyword and kw == "ANALYZE":
            return Classification(False, leading, "ANALYZE updates planner statistics (a write)")

    # SELECT ... INTO creates a new table (the Postgres equivalent of
    # CREATE TABLE AS). The leading-keyword check sees SELECT and waves
    # it through; only by walking the flattened token stream can we spot
    # the trailing INTO modifier. The DB-side
    # `default_transaction_read_only` flag would also catch this, but
    # defense-in-depth is the whole point of the classifier.
    #
    # `INTO` only appears as a Token.Keyword when it's a real clause —
    # inside a string literal (`SELECT 'foo into bar'`) it's tagged
    # Literal.String, so this scan is precise.
    if leading in ("SELECT", "WITH"):
        for token in flat:
            if token.ttype is Keyword and token.normalized.upper() == "INTO":
                return Classification(
                    False,
                    leading,
                    "SELECT ... INTO creates a new table; not allowed in read-only mode",
                )

    # Scan for calls to side-effecting builtins (pg_terminate_backend,
    # pg_advisory_lock, set_config, dblink, nextval, …). The leading-
    # keyword check accepts SELECT and WITH; a caller could otherwise
    # smuggle a mutating function through the parser as
    # `SELECT pg_terminate_backend(123)`. We look for an IDENTIFIER token
    # in the denylist immediately followed by `(` (skipping whitespace),
    # which is the only shape a function call takes in flattened SQL.
    bad_fn = _find_side_effecting_call(flat)
    if bad_fn is not None:
        return Classification(
            False,
            leading,
            f"call to side-effecting function {bad_fn}() is not allowed in read-only mode",
        )

    return Classification(True, leading, f"{leading} is read-only")


def _normalize_identifier(value: str) -> str:
    """Return `value` with surrounding double quotes stripped, lowercased.

    Postgres quoted identifiers (`"pg_terminate_backend"`) preserve case
    and are syntactically distinct from bareword identifiers, but for
    the purposes of the side-effect denylist we treat them as
    case-insensitive: the underlying builtin is the same function
    regardless of how it was spelled at the call site, and an attacker
    could otherwise smuggle a denylisted call past the matcher just by
    quoting it (`"pg_advisory_lock"(1)` vs `pg_advisory_lock(1)`).
    """
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1].replace('""', '"')
    return value.lower()


def _find_side_effecting_call(flat: list[sqlparse.sql.Token]) -> str | None:
    """Return the name of the first denylisted function called in `flat`.

    `flat` is the output of `Statement.flatten()` — a flat sequence of
    leaf tokens. A function call lexes as either `Name "fn"` (bareword)
    or `String.Symbol "\"fn\""` (quoted identifier), then optional
    whitespace then `Punctuation "("`. Both forms are normalized to a
    lowercase, unquoted name before being matched against the denylist
    so quoting cannot bypass the check.
    """
    for i, token in enumerate(flat):
        # Bareword function names lex as Token.Name; quoted identifiers
        # (`"pg_terminate_backend"`) lex as Token.Literal.String.Symbol.
        # We accept both — the normalizer strips quotes and lowercases,
        # so either form falls into the same denylist comparison.
        if token.ttype is not Name and token.ttype is not String.Symbol:
            continue
        name = _normalize_identifier(str(token.value))
        if name not in _SIDE_EFFECTING_FUNCTIONS:
            continue
        # Look ahead for `(`, skipping whitespace. If the next non-space
        # token isn't `(`, this is a column / alias reference, not a
        # call — wave it through.
        for follow in flat[i + 1 :]:
            if follow.is_whitespace:
                continue
            if follow.ttype is Punctuation and follow.value == "(":
                return name
            break
    return None
