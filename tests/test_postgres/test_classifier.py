"""Tests for the `classify_sql` API surface.

`is_read_only_sql` is exhaustively tested in `test_safety.py`. The
classifier is the same logic, but it returns a *reason* — these tests
pin the reason strings the server uses in error messages so the agent
can render "why was my query refused?" without re-parsing.
"""

from __future__ import annotations

import pytest

from devops_mcp_bundle.postgres.safety import Classification, classify_sql


class TestAcceptedShape:
    def test_select_is_read_only(self) -> None:
        c = classify_sql("SELECT 1")
        assert c.is_read_only is True
        assert c.leading_keyword == "SELECT"
        assert "read-only" in c.reason

    def test_explain_without_analyze_is_read_only(self) -> None:
        c = classify_sql("EXPLAIN SELECT * FROM users")
        assert c.is_read_only is True
        assert c.leading_keyword == "EXPLAIN"

    def test_explain_format_json_is_read_only(self) -> None:
        # `EXPLAIN (FORMAT JSON)` — the parser sees the parenthesised
        # options list before the inner SELECT; still read-only.
        c = classify_sql("EXPLAIN (FORMAT JSON) SELECT 1")
        assert c.is_read_only is True

    def test_with_recursive_select_is_read_only(self) -> None:
        c = classify_sql(
            "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM t WHERE n < 5) "
            "SELECT * FROM t"
        )
        assert c.is_read_only is True
        assert c.leading_keyword == "WITH"


class TestRejectedReasons:
    def test_blank(self) -> None:
        c = classify_sql("   ")
        assert c.is_read_only is False
        assert "blank" in c.reason
        assert c.leading_keyword is None

    def test_multi_statement(self) -> None:
        c = classify_sql("SELECT 1; SELECT 2")
        assert c.is_read_only is False
        assert "exactly one statement" in c.reason

    def test_explain_analyze_reason(self) -> None:
        c = classify_sql("EXPLAIN ANALYZE SELECT 1")
        assert c.is_read_only is False
        assert c.leading_keyword == "EXPLAIN"
        assert "ANALYZE" in c.reason

    def test_cte_with_insert_reason(self) -> None:
        c = classify_sql(
            "WITH inserted AS (INSERT INTO t (x) VALUES (1) RETURNING *) SELECT * FROM inserted"
        )
        assert c.is_read_only is False
        assert c.leading_keyword == "WITH"
        assert "INSERT" in c.reason

    def test_unknown_leading_keyword_reason(self) -> None:
        c = classify_sql("FOO bar")
        assert c.is_read_only is False
        assert "unknown leading keyword" in c.reason

    @pytest.mark.parametrize(
        ("sql", "kw"),
        [
            ("VACUUM t", "VACUUM"),
            ("REINDEX TABLE t", "REINDEX"),
            ("REFRESH MATERIALIZED VIEW v", "REFRESH"),
            ("COPY t FROM '/etc/passwd'", "COPY"),
            ("CALL p()", "CALL"),
            ("SET work_mem = '64MB'", "SET"),
            ("GRANT SELECT ON t TO bob", "GRANT"),
            ("LOCK TABLE t", "LOCK"),
            ("PREPARE foo AS SELECT 1", "PREPARE"),
            ("SAVEPOINT a", "SAVEPOINT"),
        ],
    )
    def test_mutating_kw_reason_includes_keyword(self, sql: str, kw: str) -> None:
        c = classify_sql(sql)
        assert c.is_read_only is False
        # Either the leading keyword is the mutator, or the reason names it.
        assert c.leading_keyword == kw or kw in c.reason


class TestClassificationObject:
    def test_repr_renders(self) -> None:
        c = Classification(True, "SELECT", "ok")
        text = repr(c)
        assert "SELECT" in text
        assert "is_read_only=True" in text

    def test_slots_prevent_extra_attrs(self) -> None:
        c = classify_sql("SELECT 1")
        with pytest.raises(AttributeError):
            c.something_else = 1  # type: ignore[attr-defined]
