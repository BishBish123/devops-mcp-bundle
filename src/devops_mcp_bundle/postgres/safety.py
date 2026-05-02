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
from sqlparse.tokens import DDL, DML, Keyword

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

    return Classification(True, leading, f"{leading} is read-only")
