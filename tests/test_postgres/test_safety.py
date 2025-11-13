"""SQL safety filter tests — these run anywhere, no DB needed."""

from __future__ import annotations

import pytest

from devops_mcp_bundle.postgres.safety import is_read_only_sql


class TestAccept:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1",
            "select id from users",
            "EXPLAIN SELECT * FROM users",
            "WITH cte AS (SELECT id FROM users) SELECT * FROM cte",
            "SHOW server_version",
            "VALUES (1, 2), (3, 4)",
            "  SELECT 1   ",  # leading whitespace
            "/* comment */ SELECT 1",
        ],
    )
    def test_read_only_accepted(self, sql: str) -> None:
        assert is_read_only_sql(sql)


class TestReject:
    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO t VALUES (1)",
            "UPDATE t SET x = 1",
            "DELETE FROM t",
            "DROP TABLE t",
            "CREATE TABLE x (id int)",
            "TRUNCATE t",
            "ALTER TABLE t ADD COLUMN y int",
            "GRANT SELECT ON t TO bob",
            "REVOKE ALL ON t FROM bob",
            "VACUUM t",
            "ANALYZE t",  # mutates pg_statistic
            "REINDEX TABLE t",
            "REFRESH MATERIALIZED VIEW v",
            "COPY t FROM '/etc/passwd'",
            "CALL my_proc()",
            "DO $$ BEGIN ... END $$",
            "BEGIN",
            "COMMIT",
            "SET lock_timeout = 0",
            "RESET ALL",
            "LISTEN c",
        ],
    )
    def test_mutating_rejected(self, sql: str) -> None:
        assert not is_read_only_sql(sql)

    def test_multi_statement_rejected(self) -> None:
        # Even if both halves are read-only, multiple statements get refused
        # — defends against `SELECT 1; DROP TABLE x` injection.
        assert not is_read_only_sql("SELECT 1; SELECT 2")

    def test_dml_inside_cte_rejected(self) -> None:
        # `WITH inserted AS (INSERT ... RETURNING ...) SELECT ...` is the
        # classic Postgres write-via-WITH pattern; refuse it.
        assert not is_read_only_sql(
            "WITH inserted AS (INSERT INTO t (x) VALUES (1) RETURNING *) SELECT * FROM inserted"
        )

    def test_explain_analyze_rejected(self) -> None:
        # EXPLAIN ANALYZE actually executes the inner statement.
        assert not is_read_only_sql("EXPLAIN ANALYZE SELECT * FROM users")

    @pytest.mark.parametrize("sql", ["", "   ", "\n\t  "])
    def test_blank_rejected(self, sql: str) -> None:
        assert not is_read_only_sql(sql)

    def test_unknown_leading_keyword_rejected(self) -> None:
        # Refuse anything we don't explicitly recognize as read-only.
        assert not is_read_only_sql("FOO bar")


class TestExoticReadOnly:
    """Read-only shapes the classifier handles correctly even though they
    look fishy."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1 -- with a trailing line comment",
            "SELECT 1 /* with a trailing block comment */",
            "EXPLAIN (FORMAT JSON, COSTS OFF) SELECT * FROM users",
            "EXPLAIN (BUFFERS, FORMAT TEXT) SELECT 1",
            # A WITH that has multiple CTEs — all reads.
            (
                "WITH a AS (SELECT 1), b AS (SELECT 2) "
                "SELECT * FROM a CROSS JOIN b"
            ),
            # SHOW with a parameter name — read-only.
            "SHOW search_path",
            # VALUES with a CTE on top.
            "WITH t(x, y) AS (VALUES (1, 2), (3, 4)) SELECT x + y FROM t",
        ],
    )
    def test_accepted(self, sql: str) -> None:
        assert is_read_only_sql(sql)


class TestExoticRejected:
    """Adversarial inputs: ensure the classifier doesn't get fooled."""

    @pytest.mark.parametrize(
        "sql",
        [
            # CTE that hides a DELETE.
            (
                "WITH deleted AS (DELETE FROM users WHERE id = 1 RETURNING *) "
                "SELECT * FROM deleted"
            ),
            # CTE that hides an UPDATE.
            (
                "WITH updated AS (UPDATE users SET name = 'x' RETURNING *) "
                "SELECT * FROM updated"
            ),
            # CTE chain where the *second* CTE writes.
            (
                "WITH a AS (SELECT 1), b AS (INSERT INTO t VALUES (1) RETURNING *) "
                "SELECT * FROM b"
            ),
            # Trailing-DML-after-SELECT (semicolon split).
            "SELECT 1; INSERT INTO t VALUES (1)",
            # EXPLAIN with ANALYZE buried in the options list.
            "EXPLAIN (ANALYZE, BUFFERS) SELECT 1",
            # EXPLAIN with ANALYZE without parens.
            "EXPLAIN ANALYZE VERBOSE SELECT 1",
            # COPY TO program — runs arbitrary shell on the server.
            "COPY (SELECT 1) TO PROGRAM 'rm -rf /'",
            # `EXECUTE` of a prepared statement — could be a write.
            "EXECUTE my_write_proc()",
        ],
    )
    def test_rejected(self, sql: str) -> None:
        assert not is_read_only_sql(sql), f"should refuse: {sql!r}"
