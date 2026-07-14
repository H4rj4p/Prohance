"""SQL Server helpers via pyodbc."""

from __future__ import annotations

import re
from typing import Any

import pyodbc

from app.config import Settings

# Allow only a single SELECT / WITH query; block mutations and multi-statements.
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|MERGE|EXEC|EXECUTE|"
    r"GRANT|REVOKE|DENY|BACKUP|RESTORE|SHUTDOWN|XP_|SP_OA)\b",
    re.IGNORECASE,
)
_LEADING = re.compile(r"^\s*(WITH|SELECT)\b", re.IGNORECASE)


def get_connection(settings: Settings) -> pyodbc.Connection:
    return pyodbc.connect(settings.sql_connection_string, timeout=10)


def test_connection(settings: Settings) -> dict[str, Any]:
    try:
        with get_connection(settings) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT @@VERSION AS version, DB_NAME() AS database_name")
            row = cursor.fetchone()
            return {
                "ok": True,
                "database": row.database_name if row else None,
                "version": (row.version.splitlines()[0] if row and row.version else None),
            }
    except Exception as exc:  # noqa: BLE001 — surface driver/network errors to UI
        return {"ok": False, "error": str(exc)}


def fetch_schema_summary(settings: Settings, limit_tables: int = 40) -> str:
    """Return a compact schema description for the LLM prompt."""
    # Compatible with SQL Server 2012+ (avoids STRING_AGG).
    sql = """
    SELECT TOP (?)
        s.name AS schema_name,
        t.name AS table_name,
        STUFF((
            SELECT ', ' + c.name + ' (' + ty.name + ')'
            FROM sys.columns c
            INNER JOIN sys.types ty ON ty.user_type_id = c.user_type_id
            WHERE c.object_id = t.object_id
            ORDER BY c.column_id
            FOR XML PATH(''), TYPE
        ).value('.', 'nvarchar(max)'), 1, 2, '') AS columns
    FROM sys.tables t
    INNER JOIN sys.schemas s ON s.schema_id = t.schema_id
    WHERE t.is_ms_shipped = 0
    ORDER BY s.name, t.name
    """
    with get_connection(settings) as conn:
        cursor = conn.cursor()
        cursor.execute(sql, limit_tables)
        lines = []
        for schema_name, table_name, columns in cursor.fetchall():
            lines.append(f"{schema_name}.{table_name}: {columns}")
        return "\n".join(lines) if lines else "(no user tables found)"


def validate_readonly_sql(sql: str) -> str:
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        raise ValueError("Empty SQL.")
    if ";" in cleaned:
        raise ValueError("Multiple SQL statements are not allowed.")
    if not _LEADING.search(cleaned):
        raise ValueError("Only SELECT / WITH queries are allowed.")
    if _FORBIDDEN.search(cleaned):
        raise ValueError("Query contains a forbidden keyword.")
    return cleaned


def run_select(settings: Settings, sql: str, max_rows: int = 50) -> dict[str, Any]:
    safe_sql = validate_readonly_sql(sql)
    with get_connection(settings) as conn:
        cursor = conn.cursor()
        cursor.execute(safe_sql)
        if cursor.description is None:
            return {"columns": [], "rows": [], "row_count": 0, "sql": safe_sql}
        columns = [col[0] for col in cursor.description]
        rows = []
        for i, row in enumerate(cursor.fetchall()):
            if i >= max_rows:
                break
            rows.append([_serialize(v) for v in row])
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "sql": safe_sql,
            "truncated": len(rows) >= max_rows,
        }


def _serialize(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, (int, float, str, bool)):
        return value
    return str(value)
