import json
import os
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_file

try:
    import pyodbc
except Exception as exc:
    pyodbc = None
    PYODBC_IMPORT_ERROR = exc
else:
    PYODBC_IMPORT_ERROR = None

DATABASE_ERROR_TYPES = (pyodbc.Error,) if pyodbc is not None else ()


BASE_DIR = Path(__file__).resolve().parent
MEMORY_ROWS = 500

app = Flask(__name__)


def load_local_settings():
    path = BASE_DIR / "local.settings.json"
    if not path.exists():
        return

    with path.open("r", encoding="utf-8") as handle:
        settings = json.load(handle)

    for name, value in settings.get("Values", {}).items():
        if value is None:
            continue
        os.environ.setdefault(name, str(value))


load_local_settings()


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


def load_text_file(file_name):
    path = BASE_DIR / file_name
    return path.read_text(encoding="utf-8") if path.exists() else ""

 
def parse_connection_string(connection_string):
    values = {}
    for part in connection_string.split(";"):
        if not part.strip() or "=" not in part:
            continue
        key, value = part.split("=", 1)
        values[key.strip().lower()] = value.strip()
    return values


def get_connection_string():
    connection_string = os.environ.get("SqlConnectionString")
    if not connection_string or not connection_string.strip():
        return None

    values = parse_connection_string(connection_string)
    host_override = os.environ.get("SqlServerHost", "").strip()

    if host_override:
        values["server"] = host_override

    if "driver" not in values:
        values["driver"] = "ODBC Driver 18 for SQL Server"
    if "trustservercertificate" not in values and "encrypt" not in values:
        values["trustservercertificate"] = "yes"

    ordered_keys = [
        "driver",
        "server",
        "database",
        "initial catalog",
        "user id",
        "uid",
        "password",
        "pwd",
        "trusted_connection",
        "integrated security",
        "encrypt",
        "trustservercertificate",
    ]
    parts = []
    seen = set()
    for key in ordered_keys:
        if key in values and values[key]:
            parts.append(f"{key}={values[key]}")
            seen.add(key)
    for key, value in values.items():
        if key not in seen and value:
            parts.append(f"{key}={value}")
    return ";".join(parts)


def get_config_error():
    host = os.environ.get("SqlServerHost", "").strip()
    if not host:
        return None

    placeholders = {"YOUR_WINDOWS_IP", "YOUR_SERVER"}
    if host.upper() in placeholders:
        return f"SqlServerHost is still '{host}'. Replace it with your SQL Server host or leave it blank to use SqlConnectionString."

    return None


def open_sql_server_connection():
    if pyodbc is None:
        raise RuntimeError(
            "pyodbc could not load. Install pyodbc and Microsoft ODBC Driver 18 for SQL Server, "
            f"then restart the app. Details: {PYODBC_IMPORT_ERROR}"
        )

    connection_string = get_connection_string()
    if connection_string is None:
        raise RuntimeError("SqlConnectionString is not configured.")
    return pyodbc.connect(connection_string, timeout=30, autocommit=True)


def rows_as_dicts(cursor):
    columns = [column[0] for column in cursor.description or []]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


class SchemaProvider:
    def __init__(self):
        self._cached_primary_table = None

    def get_primary_table_name(self):
        if self._cached_primary_table:
            return self._cached_primary_table

        if get_connection_string() is None:
            return None

        # Prefer the workforce/attendance table described in instructions.txt.
        sql = """
            SELECT TOP (1) TABLE_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_CATALOG = DB_NAME()
              AND LOWER(COLUMN_NAME) IN (
                    'employeeid', 'username', 'sessiondate',
                    'logged_hours', 'firstname', 'lastname'
              )
            GROUP BY TABLE_NAME
            HAVING
                SUM(CASE WHEN LOWER(COLUMN_NAME) IN ('employeeid', 'username') THEN 1 ELSE 0 END) > 0
                OR SUM(CASE WHEN LOWER(COLUMN_NAME) = 'sessiondate' THEN 1 ELSE 0 END) > 0
            ORDER BY
                SUM(CASE WHEN LOWER(COLUMN_NAME) = 'sessiondate' THEN 1 ELSE 0 END) DESC,
                SUM(CASE WHEN LOWER(COLUMN_NAME) = 'employeeid' THEN 1 ELSE 0 END) DESC,
                TABLE_NAME
        """

        try:
            with open_sql_server_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql)
                    row = cursor.fetchone()
                    if row:
                        self._cached_primary_table = row.TABLE_NAME
                        return self._cached_primary_table
        except Exception:
            pass

        tables = self.list_tables()
        self._cached_primary_table = tables[0] if tables else None
        return self._cached_primary_table

    def list_tables(self):
        if get_connection_string() is None:
            return []

        sql = """
            SELECT TABLE_NAME
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_CATALOG = DB_NAME()
              AND TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_NAME
        """

        tables = []
        with open_sql_server_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
                for row in rows_as_dicts(cursor):
                    tables.append(row["TABLE_NAME"])
        return tables

    def get_schema_text(self):
        live_schema = self._load_schema_from_database()
        if live_schema and "CREATE TABLE" in live_schema.upper():
            return live_schema

        file_schema = self._load_schema_file()
        live_table = self.get_primary_table_name()

        if file_schema and live_table:
            return file_schema.replace("EmployeeAttendance", live_table)

        if file_schema:
            return file_schema

        return live_schema or "-- No schema available."

    @staticmethod
    def _load_schema_file():
        content = load_text_file("schema.sql").strip()
        return content if "CREATE TABLE" in content.upper() else ""

    @staticmethod
    def _load_schema_from_database():
        if get_connection_string() is None:
            return "-- No schema file and SqlConnectionString is not configured."

        sql = """
            SELECT
                c.TABLE_NAME,
                c.COLUMN_NAME,
                c.DATA_TYPE,
                c.IS_NULLABLE
            FROM INFORMATION_SCHEMA.COLUMNS c
            INNER JOIN INFORMATION_SCHEMA.TABLES t
                ON c.TABLE_SCHEMA = t.TABLE_SCHEMA
               AND c.TABLE_NAME = t.TABLE_NAME
            WHERE t.TABLE_TYPE = 'BASE TABLE'
              AND c.TABLE_CATALOG = DB_NAME()
            ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION
        """

        lines = ["-- Auto-generated from INFORMATION_SCHEMA"]
        current_table = None
        with open_sql_server_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
                for row in rows_as_dicts(cursor):
                    table = row.get("TABLE_NAME", "")
                    column = row.get("COLUMN_NAME", "")
                    data_type = row.get("DATA_TYPE", "")
                    nullable = row.get("IS_NULLABLE", "")

                    if table != current_table:
                        if current_table is not None:
                            lines.append(");")
                        lines.append(f"CREATE TABLE [{table}] (")
                        current_table = table

                    lines.append(f"  [{column}] {data_type} NULL={nullable},")

        if current_table is not None:
            lines.append(");")

        return "\n".join(lines)


schema_provider = SchemaProvider()


MULTI_PART_PATTERN = re.compile(
    r"\b(and|also|plus|as\s+well\s+as)\b"
    r"|\?[^?]*\?"
    r"|,\s*(how|what|who|where|when|why|show|give|tell|list|count|average|total)",
    re.IGNORECASE,
)

CHART_PATTERN = re.compile(
    r"\b(graph|graphs|chart|charts|plot|plots|visual|visuali[sz]e|diagram|"
    r"pie\s*chart|bar\s*chart|line\s*chart|show\s+me\s+a\s+(graph|chart|plot))\b",
    re.IGNORECASE,
)

COMPARISON_PATTERN = re.compile(
    r"\b(or|vs|versus|compare|between|compared\s+to)\b",
    re.IGNORECASE,
)
LISTING_PATTERN = re.compile(
    r"\b(top|bottom|first|last|highest|lowest|most|least|all|list|show|give|"
    r"rank|ranking|sort|order|employees?|people|rows|hours|breaks?|meetings?|"
    r"training|locations?|shifts?)\b",
    re.IGNORECASE,
)

ID_COLUMNS = {"employeeid", "id", "rownumber"}
CATEGORY_COLUMNS = {
    "username",
    "location",
    "shiftname",
    "sessiondate",
    "late_login",
    "latelogincomment",
}
VALID_CHART_TYPES = {"bar", "line", "pie"}


def is_multi_part(question):
    return bool(question and MULTI_PART_PATTERN.search(question))


def enhance_for_sql(question):
    if not is_multi_part(question):
        return question

    return (
        question
        + "\n\nIMPORTANT: This message asks MULTIPLE things at once. "
        + "Return exactly ONE SELECT statement (no semicolons) that answers EVERY part. "
        + "Combine results using multiple columns, aggregates, CASE/SUM, and subqueries in the same query."
    )


def enhance_for_answer(question):
    if not is_multi_part(question):
        return question

    return (
        question
        + "\n\nIMPORTANT: Answer every part in 1-2 short lines total. Be terse."
    )


def parse_chat_request(data):
    if not isinstance(data, dict):
        data = {}

    message = str(data.get("message") or "")
    confirmed_username = data.get("confirmed_username") or data.get("confirmed_surname")
    confirmed_employee_id = (
        data.get("confirmed_employee_id") or data.get("confirmed_customer_id")
    )

    history = []
    raw_history = data.get("history")
    if isinstance(raw_history, list):
        for item in raw_history:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            content = str(item.get("content") or "").strip()
            if role and content:
                history.append({"role": role, "content": content})

    return (
        message,
        history,
        str(confirmed_username) if confirmed_username else None,
        str(confirmed_employee_id) if confirmed_employee_id else None,
    )


def format_for_prompt(question, history, confirmed_username=None, confirmed_employee_id=None):
    lines = []
    if history:
        lines.append("Conversation so far:")
        for item in history[-8:]:
            speaker = "User" if item["role"].lower() == "user" else "Assistant"
            lines.append(f"{speaker}: {item['content']}")

    lines.append(f"Current question: {question}" if lines else question)

    if confirmed_employee_id:
        lines.append(
            f"The user confirmed they mean employeeid exactly: {confirmed_employee_id}. "
            "Use WHERE employeeid = that exact value."
        )
    elif confirmed_username:
        lines.append(
            f"The user confirmed they mean employee with userName exactly: {confirmed_username}. "
            "Use WHERE userName = that exact value (not LIKE)."
        )

    return "\n".join(lines)


def get_openai_completion(system_prompt, user_prompt):
    api_key = os.environ.get("OpenAIApiKey")
    if not api_key or not api_key.strip():
        raise RuntimeError("OpenAIApiKey is not set in local.settings.json.")

    model = os.environ.get("OpenAIModel") or "gpt-4o-mini"
    response = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        },
        timeout=90,
    )

    body = response.text
    if not response.ok:
        try:
            message = response.json().get("error", {}).get("message") or body
        except ValueError:
            message = body
        raise RuntimeError(f"OpenAI request failed: {message}")

    payload = response.json()
    return payload["choices"][0]["message"].get("content") or ""


def extract_sql_query(raw):
    raw = (
        raw.replace("```json", "")
        .replace("```JSON", "")
        .replace("```sql", "")
        .replace("```SQL", "")
        .replace("```", "")
        .strip()
    )

    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            for key in ("query", "Query", "sql", "SQL"):
                value = payload.get(key)
                if value is not None:
                    return str(value)
    except ValueError:
        pass

    # Prefer a full CTE (WITH ... SELECT) over the first inner SELECT.
    with_match = re.search(r"(?:^|;)\s*(WITH\b[\s\S]+)", raw, re.IGNORECASE)
    select_match = re.search(r"\bSELECT\b[\s\S]+", raw, re.IGNORECASE)

    if with_match and select_match and with_match.start() <= select_match.start():
        return with_match.group(1).strip().rstrip(";")
    if select_match:
        return select_match.group(0).strip().rstrip(";")
    if with_match:
        return with_match.group(1).strip().rstrip(";")

    return raw


def normalize_readonly_sql(sql):
    """Strip leading noise that SQL Server models often add before a SELECT/WITH."""
    if not sql:
        return sql

    sql = sql.strip().lstrip(";").strip()
    # Drop leading USE / SET lines (e.g. SET NOCOUNT ON) before the real query.
    while True:
        match = re.match(
            r"^(?:USE\b[^;\n]*|SET\b[^;\n]*);?\s*",
            sql,
            flags=re.IGNORECASE,
        )
        if not match:
            break
        sql = sql[match.end():].lstrip(";").strip()
    return sql


def clean_sql(sql, actual_table_name="EmployeeAttendance"):
    if not sql or not sql.strip():
        return "NA"

    sql = (
        sql.replace("```json", "")
        .replace("```JSON", "")
        .replace("```sql", "")
        .replace("```SQL", "")
        .replace("```", "")
        .strip()
        .rstrip(";")
    )
    sql = normalize_readonly_sql(sql)
    sql = fix_username_prefix_match(fix_workforce_schema(sql, actual_table_name))
    sql = convert_limit_to_top(sql)
    return ensure_single_readonly_sql(sql)

def fix_username_prefix_match(sql):
    sql = re.sub(
        r"(\buserName\b)\s+LIKE\s+(['\"])%([^'\"%]+)%\2",
        r"\1 LIKE \2\3%\2",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"(\buserName\b)\s+LIKE\s+(['\"])%([^'\"%]+)\2",
        r"\1 LIKE \2\3%\2",
        sql,
        flags=re.IGNORECASE,
    )
    return sql


def fix_workforce_schema(sql, actual_table_name):
    table_name = actual_table_name or "EmployeeAttendance"

    sql = re.sub(
        r"(`?|\[?)(?:bank_data\.)?(?:dbo\.)?(?:customers|employeeattendance|employee_sessions|employees)(`?|\]?)",
        lambda match: f"{match.group(1)}{table_name}{match.group(2)}",
        sql,
        flags=re.IGNORECASE,
    )

    columns = {
        "employeeid": "employeeid",
        "customerid": "employeeid",
        "username": "userName",
        "surname": "userName",
        "location": "location",
        "shiftname": "shiftName",
        "sessiondate": "sessionDate",
        "firstlogin": "firstLogin",
        "lastlogin": "lastLogin",
        "logged_hours": "logged_hours",
        "loggedhours": "logged_hours",
        "firstswipein": "firstSwipeIn",
        "lastswipeout": "lastSwipeOut",
        "late_login": "late_login",
        "latelogin": "late_login",
        "latelogincomment": "lateLoginComment",
        "earlylogoutcomment": "earlyLogoutComment",
        "aafsconferencecall": "aafsConferenceCall",
        "aafstraining": "aafsTraining",
        "aafsmeeting": "aafsMeeting",
        "aafsworkreview": "aafsWorkReview",
        "aafsunknowntafs": "aafsUnknownTafs",
        "aafsitdesksupport": "aafsItDeskSupport",
        "aafsteammeeting": "aafsTeamMeeting",
        "aafsofficefunactivity": "aafsOfficefunactivity",
        "aafsdocumentmgmt": "aafsDocumentMgmt",
        "aafssalescall": "aafsSalesCall",
        "aafsnamesonboard": "aafsNamesOnBoard",
        "aafsondesksupport": "aafsOnDeskSupport",
        "aafbaqhseq": "aafBaqHseq",
        "aafsinterview": "aafsInterview",
        "aafsbreak": "aafsBreak",
        "aafslunchbreak": "aafsLunchBreak",
        "aafsshortbreak": "aafsShortBreak",
        "aafspersonaltime": "aafsPersonalTime",
    }

    for pattern, replacement in columns.items():
        sql = re.sub(rf"\b{pattern}\b", replacement, sql, flags=re.IGNORECASE)

    return sql


def convert_limit_to_top(sql):
    match = re.search(r"\s+LIMIT\s+(\d+)\s*$", sql, re.IGNORECASE)
    if not match:
        return sql

    limit = match.group(1)
    without_limit = sql[: match.start()].rstrip()
    if re.match(r"^\s*SELECT\s+TOP\s*\(", without_limit, re.IGNORECASE):
        return without_limit
    if re.match(r"^\s*SELECT\s+DISTINCT\b", without_limit, re.IGNORECASE):
        return re.sub(
            r"^\s*SELECT\s+DISTINCT\b",
            f"SELECT DISTINCT TOP ({limit})",
            without_limit,
            count=1,
            flags=re.IGNORECASE,
        )
    return re.sub(
        r"^\s*SELECT\b",
        f"SELECT TOP ({limit})",
        without_limit,
        count=1,
        flags=re.IGNORECASE,
    )


def strip_comments(sql):
    sql = re.sub(r"--[^\r\n]*", "", sql)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return sql.strip()


def strip_string_literals(sql):
    sql = re.sub(r"N?'(?:''|[^'])*'", "''", sql)
    sql = re.sub(r'"(?:""|[^"])*"', '""', sql)
    return sql


def split_sql_statements(sql):
    """Split on semicolons that are outside string literals."""
    parts = []
    buffer = []
    in_single_quote = False
    index = 0
    while index < len(sql):
        char = sql[index]
        if in_single_quote:
            buffer.append(char)
            if char == "'":
                if index + 1 < len(sql) and sql[index + 1] == "'":
                    buffer.append(sql[index + 1])
                    index += 2
                    continue
                in_single_quote = False
            index += 1
            continue

        if char == "'":
            in_single_quote = True
            buffer.append(char)
            index += 1
            continue

        if char == ";":
            piece = "".join(buffer).strip()
            if piece:
                parts.append(piece)
            buffer = []
            index += 1
            continue

        buffer.append(char)
        index += 1

    piece = "".join(buffer).strip()
    if piece:
        parts.append(piece)
    return parts


def ensure_single_readonly_sql(sql):
    """
    Models often emit WITH ...; SELECT ..., SET + SELECT, or a second SELECT.
    Keep one read-only statement instead of blocking the whole request.
    """
    if not sql or not sql.strip():
        return sql

    stripped = normalize_readonly_sql(strip_comments(sql.strip().rstrip(";")))
    parts = split_sql_statements(stripped)
    if not parts:
        return stripped

    merged = []
    index = 0
    while index < len(parts):
        part = parts[index]
        next_part = parts[index + 1] if index + 1 < len(parts) else None
        if (
            part.upper().startswith("WITH")
            and next_part
            and re.match(r"^\s*SELECT\b", next_part, re.IGNORECASE)
        ):
            merged.append(f"{part} {next_part}")
            index += 2
            continue
        merged.append(part)
        index += 1

    for part in merged:
        upper = part.lstrip().upper()
        if upper.startswith("SELECT") or upper.startswith("WITH") or upper.startswith("("):
            return part.strip()

    return merged[0].strip()


def validate_select_query(sql):
    if not sql or not sql.strip():
        return False, "Query is empty."

    stripped = ensure_single_readonly_sql(sql)
    if not stripped:
        return False, "Query is empty."

    keyword_scan = strip_string_literals(stripped)
    for keyword in (
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "TRUNCATE",
        "ALTER",
        "CREATE",
        "EXEC",
        "EXECUTE",
        "MERGE",
        "GRANT",
        "REVOKE",
    ):
        if re.search(rf"\b{keyword}\b", keyword_scan, re.IGNORECASE):
            return False, f"Blocked keyword detected: {keyword}."

    # Allow "SELECT ... FROM ... INTO" only when it is SELECT INTO (write).
    if re.search(r"\bSELECT\b[\s\S]*?\bINTO\b\s+[\#\[]?\w+", keyword_scan, re.IGNORECASE):
        return False, "SELECT INTO is not allowed."

    upper = stripped.upper()
    if upper.startswith("SELECT") or upper.startswith("("):
        return True, ""

    if upper.startswith("WITH") and re.search(r"\bSELECT\b", stripped, re.IGNORECASE):
        return True, ""

    return False, "Only read-only SELECT queries are allowed."

def make_json_value(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return int.from_bytes(value, byteorder="big") if len(value) <= 8 else value.hex()
    return value


def execute_sql(sql_query):
    results = []
    with open_sql_server_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql_query)
            for row in rows_as_dicts(cursor):
                results.append({key: make_json_value(value) for key, value in row.items()})
    return results


def user_requested_specific_row_count(question):
    text = question or ""
    if re.search(r"\b(top|bottom|first|last)\s+\d+\b", text, re.IGNORECASE):
        return True
    if re.search(r"\b\d+\s+(rows|records|results|employees|people)\b", text, re.IGNORECASE):
        return True
    if re.search(r"\blimit\s+\d+\b", text, re.IGNORECASE):
        return True
    return False


def remove_broad_query_limit(sql_query, question):
    if user_requested_specific_row_count(question):
        return sql_query

    if not LISTING_PATTERN.search(question or ""):
        return sql_query

    pattern = re.compile(r"\bLIMIT\s+(\d+)\s*$", re.IGNORECASE)
    match = pattern.search(sql_query)
    if match:
        return pattern.sub("", sql_query).rstrip()

    top_pattern = re.compile(r"^\s*SELECT\s+(DISTINCT\s+)?TOP\s*\(\s*\d+\s*\)\s+", re.IGNORECASE)
    return top_pattern.sub(lambda match: f"SELECT {match.group(1) or ''}", sql_query, count=1)


def get_candidates(results):
    if not results:
        return []

    keys = [key for row in results for key in row.keys()]
    username_key = next(
        (key for key in keys if key.lower() in {"username", "surname", "name"}),
        None,
    )
    if not username_key:
        return []

    employee_id_key = next(
        (key for key in keys if key.lower() in {"employeeid", "customerid", "id"}),
        None,
    )
    seen = set()
    candidates = []

    for row in results:
        username = row.get(username_key)
        if not username:
            continue

        employee_id = row.get(employee_id_key) if employee_id_key else None
        dedupe_key = str(employee_id) if employee_id is not None else str(username).lower()
        if dedupe_key in seen:
            continue

        seen.add(dedupe_key)
        candidates.append({"employee_id": employee_id, "username": str(username)})

    return sorted(candidates, key=lambda c: (c["username"].lower(), str(c["employee_id"])))


def should_confirm(candidates, question, confirmed_employee_id):
    if confirmed_employee_id:
        return False
    if len(candidates) <= 1:
        return False
    if COMPARISON_PATTERN.search(question or ""):
        return False
    if LISTING_PATTERN.search(question or ""):
        return False

    username_counts = {}
    for candidate in candidates:
        username = str(candidate.get("username") or "").strip().lower()
        if username:
            username_counts[username] = username_counts.get(username, 0) + 1

    duplicate_names = [
        username
        for username, count in username_counts.items()
        if count > 1
    ]
    question_text = question or ""
    return any(
        re.search(rf"\b{re.escape(username)}\b", question_text, re.IGNORECASE)
        for username in duplicate_names
    )


def wants_chart(question):
    return bool(question and CHART_PATTERN.search(question))


def requested_chart_type(question):
    text = (question or "").lower()
    if re.search(r"\bpie(?:\s+chart)?\b", text):
        return "pie"
    if re.search(r"\bline(?:\s+chart|\s+graph)?\b", text):
        return "line"
    if re.search(r"\bbar(?:\s+chart|\s+graph)?\b", text):
        return "bar"
    return None


def is_chart_followup(question):
    if not wants_chart(question) and requested_chart_type(question) is None:
        return False

    tokens = re.findall(r"[a-z0-9]+", (question or "").lower())
    filler = {
        "a",
        "an",
        "and",
        "as",
        "bar",
        "can",
        "chart",
        "charts",
        "diagram",
        "employee",
        "employees",
        "graph",
        "graphs",
        "it",
        "line",
        "make",
        "me",
        "now",
        "of",
        "ok",
        "okay",
        "people",
        "person",
        "pie",
        "please",
        "plot",
        "for",
        "result",
        "results",
        "show",
        "that",
        "the",
        "them",
        "these",
        "this",
        "those",
        "to",
        "turn",
        "visual",
        "visualize",
        "visualise",
        "you",
    }
    meaningful_tokens = [token for token in tokens if token not in filler]
    return len(meaningful_tokens) == 0


def parse_last_result(payload):
    last_result = payload.get("last_result") if isinstance(payload, dict) else None
    if not isinstance(last_result, dict):
        return None

    rows = last_result.get("data")
    if not isinstance(rows, list):
        rows = []

    clean_rows = []
    for row in rows[:MEMORY_ROWS]:
        if isinstance(row, dict):
            clean_rows.append(row)

    return {
        "question": str(last_result.get("question") or ""),
        "answer": str(last_result.get("answer") or ""),
        "query": str(last_result.get("query") or ""),
        "chart_type": str(last_result.get("chart_type") or "table"),
        "data": clean_rows,
        "memory": last_result.get("memory") if isinstance(last_result.get("memory"), dict) else {},
    }


def summarize_last_result(last_result):
    if not last_result or not last_result.get("data"):
        return ""

    rows = last_result["data"]
    columns = list(rows[0].keys()) if rows and isinstance(rows[0], dict) else []
    preview = rows[:5]
    parts = [
        "Previous result context:",
        f"Previous question: {last_result.get('question', '')}",
        f"Previous SQL query: {last_result.get('query', '')}",
        f"Previous columns: {', '.join(columns)}",
        f"Previous rows shown: {len(rows)}",
        f"First rows: {json.dumps(preview, default=str)}",
    ]

    memory = last_result.get("memory") if isinstance(last_result.get("memory"), dict) else {}
    people = memory.get("people") if isinstance(memory.get("people"), list) else []
    numeric_columns = memory.get("numeric_columns") if isinstance(memory.get("numeric_columns"), list) else []

    if people:
        parts.append(
            "Previous people/employees: "
            + json.dumps(people[:20], default=str)
        )
    if memory.get("label_column"):
        parts.append(f"Previous chart label/x-axis column: {memory['label_column']}")
    if memory.get("value_column"):
        parts.append(f"Previous chart value/y-axis column: {memory['value_column']}")
    if numeric_columns:
        parts.append(f"Previous numeric metrics: {', '.join(str(col) for col in numeric_columns)}")
    if last_result.get("chart_type"):
        parts.append(f"Previous chart type: {last_result['chart_type']}")
    if people:
        parts.append(
            "If the user says they, them, their, those employees, or those people, "
            "treat that as referring to the previous people/employees listed above."
        )

    return "\n".join(part for part in parts if part.strip())


def add_result_context_to_history(history, last_result):
    summary = summarize_last_result(last_result)
    if not summary:
        return history
    return [*history, {"role": "assistant", "content": summary}]


def is_numeric(value):
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float, Decimal)):
        return True
    try:
        float(str(value))
        return True
    except ValueError:
        return False


def get_numeric_columns(row):
    return [
        key
        for key, value in row.items()
        if key.lower() not in ID_COLUMNS and is_numeric(value)
    ]


def find_label_column(row):
    for key in row.keys():
        if key.lower() in CATEGORY_COLUMNS:
            return key

    for key, value in row.items():
        if key.lower() not in ID_COLUMNS and isinstance(value, str):
            return key

    return None


def recommend_chart(data, llm_chart_type, question):
    explicit_chart_type = requested_chart_type(question)
    if not data or (not wants_chart(question) and explicit_chart_type is None):
        return "table"

    first = data[0]
    numeric_cols = get_numeric_columns(first)
    label_col = find_label_column(first)
    preferred_chart_type = explicit_chart_type
    if preferred_chart_type is None and llm_chart_type in VALID_CHART_TYPES:
        preferred_chart_type = llm_chart_type

    if preferred_chart_type is not None:
        return preferred_chart_type

    if len(data) == 1:
        if len(numeric_cols) >= 2 and label_col is None:
            parts_cols = [col.lower() for col in numeric_cols]
            wants_parts = any(
                "count" in col or "male" in col or "female" in col or "total" in col
                for col in parts_cols
            )
            return "pie" if wants_parts and len(numeric_cols) <= 6 else "bar"
        return "table"

    if label_col and numeric_cols:
        if len(data) <= 6 and label_col.lower() in CATEGORY_COLUMNS:
            return "pie"
        if len(data) <= 25:
            return "bar"

    if len(data) >= 2 and numeric_cols:
        return "bar"

    return "table"


def build_chart_followup_response(question, last_result):
    if not last_result or not last_result.get("data"):
        return None

    rows = last_result["data"]
    chart_type = requested_chart_type(question) or recommend_chart(rows, last_result.get("chart_type"), question)
    if chart_type == "table":
        chart_type = recommend_chart(rows, "bar", "show me a chart")

    answer = (
        f"Here is a {chart_type} chart for the previous results."
        if chart_type != "table"
        else "I can show the previous results, but they do not have enough chartable values for a graph."
    )

    return {
        "query": last_result.get("query") or "",
        "answer": answer,
        "data": rows,
        "chart_type": chart_type,
        "chart_followup": True,
    }


def generate_sql(question, history, primary_table, confirmed_username, confirmed_employee_id):
    schema_text = schema_provider.get_schema_text()
    instructions_text = load_text_file("instructions.txt")
    samples_text = load_text_file("sample_queries.txt")
    enhanced_question = enhance_for_sql(question)
    prompt_question = format_for_prompt(
        enhanced_question,
        history,
        confirmed_username,
        confirmed_employee_id,
    )

    table_hint = (
        f"\n\nPrimary table to query when unsure: [{primary_table}]"
        if primary_table
        else ""
    )

    raw = get_openai_completion(
        system_prompt=(
            f"{instructions_text}\n\nDatabase Schema:\n{schema_text}\n\n"
            f"Example Queries:\n{samples_text}{table_hint}\n\n"
            "Return ONLY one read-only SQL Server query. "
            "It must be a single SELECT or WITH ... SELECT statement. "
            "No markdown, no explanation, no USE/SET/INSERT/UPDATE/DELETE."
        ),
        user_prompt=prompt_question,
    )
    return extract_sql_query(raw)


def generate_answer(question, history, results, confirmed_username, confirmed_employee_id):
    enhanced_question = enhance_for_answer(question)
    if is_large_result_question(question, results):
        return build_compact_result_answer(question, results), recommend_chart(results, "table", question)

    prompt_question = format_for_prompt(
        enhanced_question,
        history,
        confirmed_username,
        confirmed_employee_id,
    )
    raw_answer = get_openai_completion(
        system_prompt=(
            "You are a helpful workforce data analyst for IRI. Summarize query results as a clear, "
            "concise answer. Include notable insights. Your response will be shown "
            "in a web chat interface. Do not list rows one by one, do not use markdown "
            "tables, and do not repeat every field from the data. The UI already shows "
            "the rows in a table/chart, so keep the answer to a few sentences."
        ),
        user_prompt=f"{prompt_question}\n\nData: {json.dumps(results, default=str)}",
    )
    return raw_answer, recommend_chart(results, "table", question)


def is_large_result_question(question, results):
    if len(results) >= 8:
        return True
    return bool(results and LISTING_PATTERN.search(question or ""))


def build_compact_result_answer(question, results):
    row_count = len(results)
    first_row = results[0] if results else {}
    has_employee_fields = any(
        key.lower() in {"employeeid", "username"}
        for key in first_row.keys()
    )
    noun = "employee" if has_employee_fields else "row"
    noun = noun if row_count == 1 else f"{noun}s"

    if wants_chart(question) or requested_chart_type(question):
        return f"I found {row_count} matching {noun}."

    return f"I found {row_count} matching {noun}."


@app.route("/api/Chat", methods=["GET"])
def chat():
    path = BASE_DIR / "chat.html"
    if not path.exists():
        return "<h1>chat.html not found</h1>", 404
    return send_file(path, mimetype="text/html")


@app.route("/api/GetDatabaseSchema", methods=["GET"])
def get_database_schema():
    try:
        return jsonify(
            {
                "success": True,
                "schema": schema_provider.get_schema_text(),
                "hint": "Copy useful parts into schema.sql and sample_queries.txt for better answers.",
            }
        )
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)})


@app.route("/api/TestSqlConnection", methods=["GET", "POST"])
def test_sql_connection():
    if get_connection_string() is None:
        return jsonify(
            {
                "success": False,
                "message": "SqlConnectionString is not set in local.settings.json (Values section).",
            }
        )

    config_error = get_config_error()
    if config_error:
        return jsonify(
            {
                "success": False,
                "message": "Database config needs to be updated.",
                "error": config_error,
            }
        )

    try:
        with open_sql_server_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT @@VERSION AS ServerVersion, DB_NAME() AS DatabaseName")
                rows = rows_as_dicts(cursor)
                row = rows[0] if rows else {}

        values = parse_connection_string(os.environ.get("SqlConnectionString", ""))
        return jsonify(
            {
                "success": True,
                "message": "Connected to SQL Server successfully.",
                "server": os.environ.get("SqlServerHost", "").strip()
                or values.get("server", "")
                or values.get("data source", ""),
                "database": row.get("DatabaseName", ""),
                "serverVersion": row.get("ServerVersion", ""),
            }
        )
    except Exception as exc:
        return jsonify(
            {
                "success": False,
                "message": "Failed to connect to SQL Server.",
                "error": str(exc),
                "hint": "Connection refused usually means: wrong SqlServerHost/server, SQL Server not allowing TCP connections, missing ODBC Driver 18, or a firewall blocking port 1433.",
            }
        )


@app.route("/api/AskQuestion", methods=["POST"])
def ask_question():
    payload = request.get_json(silent=True) or {}
    question, history, confirmed_username, confirmed_employee_id = parse_chat_request(payload)
    last_result = parse_last_result(payload)

    if not question.strip():
        return jsonify({"error": 'Send JSON like { "message": "your question", "history": [] }.'})

    if is_chart_followup(question):
        chart_response = build_chart_followup_response(question, last_result)
        if chart_response is not None:
            return jsonify(chart_response)

    if get_connection_string() is None:
        return jsonify({"error": "SqlConnectionString is not configured."})

    if not os.environ.get("OpenAIApiKey"):
        return jsonify(
            {
                "answer": "Chat is not set up yet. Add your OpenAI API key to local.settings.json when you're ready."
            }
        )

    try:
        history = add_result_context_to_history(history, last_result)
        primary_table = schema_provider.get_primary_table_name()
        if not primary_table:
            tables = schema_provider.list_tables()
            return jsonify(
                {
                    "answer": (
                        "I can't find any tables in the connected SQL Server database. "
                        "Confirm the connection points at the right database, then try again."
                    ),
                    "error": "No tables found in the connected database."
                    if not tables
                    else f"Tables found: {', '.join(tables)}",
                }
            )

        sql_query = generate_sql(
            question,
            history,
            primary_table,
            confirmed_username,
            confirmed_employee_id,
        )
        sql_query = clean_sql(sql_query, primary_table)
        sql_query = remove_broad_query_limit(sql_query, question)
        sql_query = ensure_single_readonly_sql(sql_query)

        if sql_query.upper() == "NA":
            return jsonify(
                {
                    "query": "NA",
                    "answer": (
                        "I couldn't map that question to your workforce tables. Open "
                        "/api/GetDatabaseSchema to see table and column names, then ask using those names - "
                        'for example: "Who logged in late yesterday?" or '
                        '"Show total break time for employees in Toronto this week."'
                    ),
                    "data": [],
                    "chart_type": "table",
                }
            )

        is_valid, validation_error = validate_select_query(sql_query)
        if not is_valid:
            return jsonify(
                {
                    "query": sql_query,
                    "answer": f"Query blocked for safety: {validation_error}",
                    "data": [],
                    "chart_type": "table",
                }
            )

        results = execute_sql(sql_query)
        candidates = get_candidates(results)
        if should_confirm(candidates, question, confirmed_employee_id):
            return jsonify(
                {
                    "query": sql_query,
                    "answer": f"I found {len(candidates)} matching employees. Which one did you mean?",
                    "needs_confirmation": True,
                    "candidates": candidates,
                    "data": results,
                    "chart_type": "table",
                }
            )

        answer, chart_type = generate_answer(
            question,
            history,
            results,
            confirmed_username,
            confirmed_employee_id,
        )
        chart_type = recommend_chart(results, chart_type, question)

        return jsonify(
            {
                "query": sql_query,
                "answer": answer,
                "data": results,
                "chart_type": chart_type,
                "total_rows": len(results),
            }
        )
    except DATABASE_ERROR_TYPES as exc:
        return jsonify(
            {
                "answer": (
                    "I couldn't run the database query. The table or column name may be wrong "
                    "for your connected SQL Server database."
                ),
                "error": str(exc),
            }
        )
    except Exception as exc:
        message = str(exc)
        return jsonify(
            {
                "error": message,
                "answer": "ChatGPT request failed. Check your API key, billing, and restart the app after updating local.settings.json."
                if "OpenAI" in message
                else f"Something went wrong: {message}",
            }
        )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7179"))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"Starting IRI AI on http://localhost:{port}/api/Chat")
    app.run(host=host, port=port, debug=False)
