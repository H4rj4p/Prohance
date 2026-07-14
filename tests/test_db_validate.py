import pytest

from app.db import validate_readonly_sql


def test_accepts_select():
    assert validate_readonly_sql("SELECT 1") == "SELECT 1"


def test_accepts_with():
    sql = "WITH x AS (SELECT 1 AS n) SELECT * FROM x"
    assert validate_readonly_sql(sql) == sql


def test_rejects_insert():
    with pytest.raises(ValueError):
        validate_readonly_sql("INSERT INTO t VALUES (1)")


def test_rejects_multi_statement():
    with pytest.raises(ValueError):
        validate_readonly_sql("SELECT 1; DROP TABLE t")
