"""SQL safety filter tests — these run anywhere, no DB needed."""

from __future__ import annotations

import pytest

from devops_mcp_bundle.postgres.safety import (
    classify_sql,
    classify_statement,
    is_read_only_sql,
)


def test_classify_statement_is_an_alias_of_classify_sql() -> None:
    # README documents the MCP tool name (`classify_statement`); the
    # underlying function is `classify_sql`. The alias keeps both
    # spellings importable so direct-import callers don't trip.
    assert classify_statement is classify_sql


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
            ("WITH a AS (SELECT 1), b AS (SELECT 2) SELECT * FROM a CROSS JOIN b"),
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
            ("WITH deleted AS (DELETE FROM users WHERE id = 1 RETURNING *) SELECT * FROM deleted"),
            # CTE that hides an UPDATE.
            ("WITH updated AS (UPDATE users SET name = 'x' RETURNING *) SELECT * FROM updated"),
            # CTE chain where the *second* CTE writes.
            ("WITH a AS (SELECT 1), b AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM b"),
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

    def test_select_into_rejected(self) -> None:
        # `SELECT * INTO newtab FROM users` is the Postgres CREATE-TABLE-AS
        # shorthand. The leading keyword is SELECT (which would otherwise
        # be allowed); the classifier has to look at the body to spot it.
        c = classify_sql("SELECT * INTO newtab FROM users")
        assert c.is_read_only is False
        assert "INTO" in c.reason

    def test_cte_with_inner_select_into_rejected(self) -> None:
        # CTE-prefixed variant: the leading keyword is WITH, but the
        # outer query still creates a new table.
        c = classify_sql("WITH x AS (SELECT 1) SELECT * INTO out FROM x")
        assert c.is_read_only is False
        assert "INTO" in c.reason

    def test_select_with_inner_into_keyword_in_string_allowed(self) -> None:
        # The substring "into" inside a string literal is tagged as
        # Literal.String by sqlparse, not Keyword — so the body-scan
        # for INTO must not fire here.
        assert is_read_only_sql("SELECT 'foo into bar'::text")

    def test_select_for_share_rejected(self) -> None:
        # `FOR SHARE` row-locks the SELECT'd rows. Not a write to user
        # data, but it acquires locks — incompatible with the bundle's
        # read-only contract.
        c = classify_sql("SELECT * FROM t FOR SHARE")
        assert c.is_read_only is False
        assert "FOR SHARE" in c.reason

    def test_select_for_update_rejected(self) -> None:
        c = classify_sql("SELECT * FROM t FOR UPDATE")
        assert c.is_read_only is False
        assert "FOR UPDATE" in c.reason

    def test_select_for_no_key_update_rejected(self) -> None:
        c = classify_sql("SELECT * FROM t FOR NO KEY UPDATE")
        assert c.is_read_only is False
        assert "FOR NO KEY UPDATE" in c.reason

    def test_select_for_key_share_rejected(self) -> None:
        c = classify_sql("SELECT * FROM t FOR KEY SHARE")
        assert c.is_read_only is False
        assert "FOR KEY SHARE" in c.reason

    def test_select_with_for_in_string_literal_allowed(self) -> None:
        # `'for update'` inside a string literal must not trigger the
        # lock-clause scan — string contents are tagged Literal.String,
        # not Keyword.
        assert is_read_only_sql("SELECT 'for update' AS x")


class TestSideEffectingFunctionDenylist:
    """The classifier must refuse SELECTs that smuggle side-effecting
    function calls past the leading-keyword check.

    `default_transaction_read_only` catches most of these at the DB
    layer, but not advisory locks, not `dblink_exec` (writes happen on
    a remote host the local read-only flag can't govern), and the error
    message Postgres returns is generic. Layer 1 names the offending
    function up-front."""

    def test_classifier_rejects_pg_terminate_backend(self) -> None:
        c = classify_sql("SELECT pg_terminate_backend(123)")
        assert c.is_read_only is False
        assert "pg_terminate_backend" in c.reason

    def test_classifier_rejects_pg_advisory_lock(self) -> None:
        c = classify_sql("SELECT pg_advisory_lock(1)")
        assert c.is_read_only is False
        assert "pg_advisory_lock" in c.reason

    def test_classifier_rejects_set_config_mutating_call(self) -> None:
        c = classify_sql("SELECT set_config('work_mem', '64MB', false)")
        assert c.is_read_only is False
        assert "set_config" in c.reason

    def test_classifier_rejects_dblink_exec(self) -> None:
        c = classify_sql("SELECT dblink_exec('host=remote', 'INSERT INTO t VALUES (1)')")
        assert c.is_read_only is False
        assert "dblink_exec" in c.reason

    def test_classifier_rejects_dblink_send_query(self) -> None:
        # The async dblink path was the original bypass: the SELECT shape
        # passes the leading-keyword check, and the remote DB executes
        # whatever SQL is in the second argument regardless of our local
        # `default_transaction_read_only` flag. Cover it explicitly.
        c = classify_sql(
            "SELECT dblink_send_query('conn', 'INSERT INTO t VALUES (1)')"
        )
        assert c.is_read_only is False
        assert "dblink_send_query" in c.reason

    def test_classifier_rejects_dblink_get_result(self) -> None:
        c = classify_sql("SELECT dblink_get_result('conn')")
        assert c.is_read_only is False
        assert "dblink_get_result" in c.reason

    def test_classifier_rejects_dblink_cancel_query(self) -> None:
        c = classify_sql("SELECT dblink_cancel_query('conn')")
        assert c.is_read_only is False
        assert "dblink_cancel_query" in c.reason

    def test_classifier_rejects_dblink_open(self) -> None:
        c = classify_sql("SELECT dblink_open('cur', 'SELECT 1')")
        assert c.is_read_only is False
        assert "dblink_open" in c.reason

    def test_classifier_rejects_dblink_close(self) -> None:
        c = classify_sql("SELECT dblink_close('cur')")
        assert c.is_read_only is False
        assert "dblink_close" in c.reason

    def test_classifier_rejects_dblink_fetch(self) -> None:
        c = classify_sql("SELECT * FROM dblink_fetch('cur', 5) AS t(id int)")
        assert c.is_read_only is False
        assert "dblink_fetch" in c.reason

    def test_classifier_rejects_dblink_disconnect(self) -> None:
        c = classify_sql("SELECT dblink_disconnect('conn')")
        assert c.is_read_only is False
        assert "dblink_disconnect" in c.reason

    def test_classifier_rejects_dblink_get_pkey(self) -> None:
        c = classify_sql("SELECT * FROM dblink_get_pkey('public.t')")
        assert c.is_read_only is False
        assert "dblink_get_pkey" in c.reason

    def test_classifier_rejects_dblink_get_connections(self) -> None:
        c = classify_sql("SELECT dblink_get_connections()")
        assert c.is_read_only is False
        assert "dblink_get_connections" in c.reason

    def test_classifier_rejects_nextval(self) -> None:
        c = classify_sql("SELECT nextval('my_seq')")
        assert c.is_read_only is False
        assert "nextval" in c.reason

    def test_classifier_allows_pg_stat_activity(self) -> None:
        # `pg_stat_activity` is a read-only system view, not a function;
        # the denylist matcher must not be triggered by table
        # references that happen to share a `pg_` prefix.
        assert is_read_only_sql("SELECT pid FROM pg_stat_activity")

    def test_classifier_rejects_function_inside_cte(self) -> None:
        # The CTE body parses with the same flat token stream; the
        # denylist scan walks the entire flattened statement so it
        # catches the call regardless of nesting.
        c = classify_sql("WITH x AS (SELECT pg_terminate_backend(123)) SELECT * FROM x")
        assert c.is_read_only is False
        assert "pg_terminate_backend" in c.reason

    def test_classifier_rejects_quoted_pg_advisory_lock(self) -> None:
        # PG quoted identifiers preserve case at the parser level but the
        # safety matcher treats them as case-insensitive — quoting must
        # not be a bypass for the denylist.
        c = classify_sql('SELECT "pg_advisory_lock"(1)')
        assert c.is_read_only is False
        assert "pg_advisory_lock" in c.reason

    def test_classifier_rejects_quoted_pg_cancel_backend(self) -> None:
        c = classify_sql('SELECT "pg_cancel_backend"(123)')
        assert c.is_read_only is False
        assert "pg_cancel_backend" in c.reason

    def test_classifier_rejects_mixed_case_quoted_call(self) -> None:
        # Mixed-case quoted identifiers must still match — the denylist
        # is keyed on lowercase canonical names.
        c = classify_sql('SELECT "PG_TERMINATE_BACKEND"(123)')
        assert c.is_read_only is False
        assert "pg_terminate_backend" in c.reason

    def test_classifier_allows_quoted_legitimate_function(self) -> None:
        # A user-defined function whose quoted name is not on the
        # denylist must still parse as read-only. This guards against
        # an over-eager normalizer that would reject all quoted calls.
        assert is_read_only_sql('SELECT "my_helper"()')

    @pytest.mark.parametrize(
        "fn",
        [
            "pg_read_file",
            "pg_read_binary_file",
            "pg_ls_dir",
            "pg_stat_file",
            "pg_read_server_files",
            "pg_logfile_rotate",
            "lo_import",
            "lo_export",
        ],
    )
    def test_classifier_rejects_server_file_builtins(self, fn: str) -> None:
        # A role with `pg_read_server_files` (or superuser) can call
        # these from a SELECT and read arbitrary files on the Postgres
        # host. The local `default_transaction_read_only` flag classes
        # them as reads, so the parser is the only line of defence.
        c = classify_sql(f"SELECT {fn}('arg')")
        assert c.is_read_only is False, f"{fn} should be rejected"
        assert fn in c.reason

    @pytest.mark.parametrize(
        "fn",
        [
            "PG_READ_FILE",
            "Pg_Ls_Dir",
            "PG_STAT_FILE",
        ],
    )
    def test_classifier_rejects_server_file_builtins_case_insensitive(
        self, fn: str
    ) -> None:
        # Postgres identifiers fold to lowercase; the denylist matcher
        # must as well so `SELECT PG_READ_FILE(...)` doesn't bypass.
        c = classify_sql(f"SELECT {fn}('arg')")
        assert c.is_read_only is False
        assert fn.lower() in c.reason
