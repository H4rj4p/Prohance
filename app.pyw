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
# Keep response dict key order (month/date leftmost). Default True sorts
# alphabetically so avg_logged_hours would appear before month.
app.config["JSON_SORT_KEYS"] = False
try:
    app.json.sort_keys = False
except Exception:
    pass


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


_cached_connected_db_name = None
_cached_connected_db_checked = False


def get_connected_database_name():
    """Return configured SQL database name (cached). Prefer live DB_NAME(), else connection string."""
    global _cached_connected_db_name, _cached_connected_db_checked
    if _cached_connected_db_checked:
        return _cached_connected_db_name
    _cached_connected_db_checked = True

    connection_string = get_connection_string()
    if connection_string is None:
        _cached_connected_db_name = None
        return None

    try:
        with open_sql_server_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT DB_NAME() AS DatabaseName")
                row = cursor.fetchone()
                if row and row.DatabaseName:
                    _cached_connected_db_name = str(row.DatabaseName)
                    return _cached_connected_db_name
    except Exception:
        pass

    values = parse_connection_string(connection_string)
    for key in ("database", "initial catalog"):
        if values.get(key):
            _cached_connected_db_name = values[key]
            return _cached_connected_db_name

    _cached_connected_db_name = None
    return None


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
        duration_note = (
            "\n-- IMPORTANT: logged_hours and aafs*/break duration columns are often "
            "stored as VARCHAR 'HH:MM:SS' (example '00:53:45'). "
            "Convert to seconds with DATEDIFF(SECOND, 0, TRY_CAST(... AS TIME)) "
            "before AVG, SUM, or addition. Never COALESCE(column, 0) on those varchar times. "
            "For totals/averages return INTEGER seconds (total_seconds / avg_seconds). "
            "Do not CONVERT aggregates back to TIME — TIME wraps at 24 hours.\n"
        )

        live_schema = self._load_schema_from_database()
        if live_schema and "CREATE TABLE" in live_schema.upper():
            return live_schema + duration_note

        file_schema = self._load_schema_file()
        live_table = self.get_primary_table_name()

        if file_schema and live_table:
            return file_schema.replace("EmployeeAttendance", live_table) + duration_note

        if file_schema:
            return file_schema + duration_note

        return (live_schema or "-- No schema available.") + duration_note

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

DATAVISTA_TABLES = (
    "CR_HireMaster",
    "CR_InterviewMaster",
    "CR_RejectMaster",
    "CR_SubmittalMaster",
)

DATAVISTA_PATTERN = re.compile(
    r"\b("
    r"datavista|"
    r"hire[ds]?|hiring|offer(?:ed|s)?|interview(?:s|ed)?|"
    r"reject(?:ion|ed|s)?|"
    # submit / submits / submitted / submittal(s) / submission(s)
    r"submit(?:s|ted|tal|tals)?|submission(?:s)?|"
    r"candidate(?:s)?|client(?:s)?|recruiter(?:s)?|recruit(?:s|ed|ing)?|"
    r"placement(?:s)?|placement\s*date|hire\s*date|offer\s*date|start\s*date|"
    r"bill\s*rate|pay\s*rate|agreed\s*pay(?:\s*rate)?|agreed\s*bill(?:\s*rate)?|"
    r"job\s*title|company(?:\s*name)?|pipeline|recruiting|"
    r"quickbooks|job\s*id|job\s*reference|division|"
    r"cr_hire|cr_interview|cr_reject|cr_submittal"
    r")\b",
    re.IGNORECASE,
)

# Prohance = attendance / time only.
# Include bare "hour(s)" / "log" / "logged" so questions like
# "how many hour did he log for July" do not fall through to DataVista.
PROHANCE_PATTERN = re.compile(
    r"\b("
    r"prohance|"
    r"hours?|logged|logging|"
    r"logged\s*hours?|hours?\s*logged|hours?\s*worked|worked\s*hours?|"
    r"total\s*hours?|average\s*hours?|avg\s*hours?|"
    r"(?:did|does|have|has)\s+(?:he|she|they)\s+log|log\s+for|"
    r"\blog\b|"
    r"aafs|break(?:s)?|lunch\s*break|short\s*break|personal\s*time|"
    r"login|logout|first\s*login|last\s*logout|session\s*date|"
    r"attendance|shift(?:s)?|late\s*login|early\s*logout|swipe|"
    r"present(?:\s*days?)?|half[\s-]?days?|absent(?:\s*days?)?|"
    r"workforce|employee\s*hours|time\s*tracked|time\s*tracking|"
    r"time\s*at\s*(?:work|desk)|on\s*desk|away\s*from\s*system|"
    r"how\s+long\s+(?:did|have)\b|were\s+they\s+late|clock\s*in|clock\s*out"
    r")\b",
    re.IGNORECASE,
)


def get_datavista_database_name():
    """Optional override; connection string itself is unchanged."""
    return (os.environ.get("DataVistaDatabase") or "DataVista").strip() or "DataVista"


def detect_question_domain(question, history=None, last_result=None):
    """
    Route questions to Prohance (attendance/time) or DataVista (recruiting).

    Prohance is ONLY for logged hours, breaks, AAFS, login/logout, attendance.
    Submits, pay rates, start/placement dates, clients, candidates → DataVista.
    Continuation follow-ups ("excluding Fridays", "and avg", "include logged hours")
    inherit the prior domain when this turn alone would mis-route.
    When unsure, prefer DataVista (not Prohance).
    """
    text = question or ""
    has_datavista = bool(DATAVISTA_PATTERN.search(text))
    has_prohance = bool(PROHANCE_PATTERN.search(text))

    # Explicit database names always win.
    if re.search(r"\bdatavista\b", text, re.IGNORECASE):
        return "datavista"
    if re.search(r"\bprohance\b", text, re.IGNORECASE):
        return "prohance"

    # If both match, prefer the stronger signal.
    # Time/hours/log words beat weak overlap; clear recruiting words win otherwise.
    if has_datavista and has_prohance:
        # "hours" + "start date" etc.: recruiting date/pay/submit wins.
        if re.search(
            r"\b("
            r"submit|submits|submittal|submission|interview|hire|hired|reject|"
            r"placement|pay\s*rate|bill\s*rate|agreed\s*pay|candidate|client|"
            r"recruiter|start\s*date|offer|pipeline"
            r")\b",
            text,
            re.IGNORECASE,
        ):
            return "datavista"
        return "prohance"

    # "include logged hours" after interviews/hires/starts must stay on DataVista
    # so mixed month-by-month SQL (stages + hours) is used — not Prohance-only.
    if is_continuation_followup(question):
        inherited = infer_domain_from_context(history, last_result)
        if inherited == "datavista" and has_prohance and not has_datavista:
            return "datavista"
        if inherited and not has_prohance and not has_datavista:
            return inherited

    if has_prohance:
        return "prohance"
    if has_datavista:
        return "datavista"

    # Default: DataVista for non-attendance questions.
    return "datavista"


def infer_domain_from_context(history=None, last_result=None):
    """Infer Prohance vs DataVista from prior SQL / question when the follow-up is bare."""
    chunks = []
    if isinstance(last_result, dict):
        chunks.append(str(last_result.get("query") or ""))
        chunks.append(str(last_result.get("question") or ""))
        chunks.append(str(last_result.get("answer") or ""))
    for item in reversed(history or []):
        chunks.append(str(item.get("content") or ""))
        if len(chunks) >= 10:
            break
    blob = "\n".join(chunks)
    if not blob.strip():
        return None

    has_attendance = bool(
        re.search(
            r"\b(sessionDate|logged_hours|userName|EmployeeAttendance|"
            r"avg_logged|total_seconds|avg_seconds)\b",
            blob,
            re.IGNORECASE,
        )
    )
    has_recruiting = bool(
        re.search(
            r"\bCR_(?:Submittal|Interview|Hire|Reject)Master\b|"
            r"\b(SUBMITTALDATE|INTERVIEWDATE|PLACEMENTDATE|PRIMARYRECRUITERNAME)\b",
            blob,
            re.IGNORECASE,
        )
    )
    if has_attendance and not has_recruiting:
        return "prohance"
    if has_recruiting and not has_attendance:
        return "datavista"
    if has_attendance:
        # Mixed performance → exclusions on hours still need Prohance for hour filters;
        # day-exclusion follow-ups after hours questions are Prohance.
        if re.search(
            r"\b(logged|hours?|sessionDate|avg_logged|total_seconds)\b",
            blob,
            re.IGNORECASE,
        ):
            return "prohance"
        return "datavista"

    # Fall back to keyword domain of the most recent user question.
    for item in reversed(history or []):
        if str(item.get("role") or "").lower() != "user":
            continue
        prior = str(item.get("content") or "")
        if DATAVISTA_PATTERN.search(prior) and not PROHANCE_PATTERN.search(prior):
            return "datavista"
        if PROHANCE_PATTERN.search(prior):
            return "prohance"
        break
    return None


def get_datavista_schema_text():
    db_name = get_datavista_database_name()
    content = load_text_file("schema_datavista.sql").strip()
    if not content:
        return ""
    return content.replace("DataVista", db_name)


def qualify_datavista_sql(sql, db_name=None):
    db_name = db_name or get_datavista_database_name()
    if not sql:
        return sql

    for table in DATAVISTA_TABLES:
        pattern = (
            rf"(?<![\w.\]])"
            rf"(?:\[?{re.escape(db_name)}\]?\s*\.\s*)?"
            rf"(?:\[?dbo\]?\s*\.\s*)?"
            rf"\[?{table}\]?"
        )
        sql = re.sub(
            pattern,
            f"[{db_name}].[dbo].[{table}]",
            sql,
            flags=re.IGNORECASE,
        )
    return sql


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

NAME_STOPWORDS = {
    "a", "an", "the", "and", "or", "for", "of", "on", "in", "at", "to", "from", "by",
    "with", "as", "if", "so", "than", "then", "into", "over", "under", "about",
    "is", "are", "was", "were", "be", "been", "being", "am",
    "what", "whats", "what's", "whatis", "who", "whos", "who's", "whose", "whom",
    "which", "when", "where", "wheres", "where's", "why", "how", "hows", "how's",
    "thats", "that's", "theres", "there's", "heres", "here's",
    "many", "much", "avg", "average", "mean", "total", "sum", "count", "number",
    "logged", "log", "logs", "logging", "hours", "hour",
    "break", "breaks", "lunch", "personal", "time", "times",
    "login", "logins", "logout", "late", "early", "shift", "shifts", "location",
    "locations", "session", "sessions", "attendance", "activity", "activities",
    "today", "yesterday", "tomorrow", "this", "that", "these", "those", "last", "next",
    "now", "currently", "again", "instead", "maybe", "perhaps", "actually", "basically",
    "simply", "okay", "ok", "alright", "sure", "yes", "yeah", "yep", "nope", "no",
    "fine", "cool", "thanks", "thank", "hello", "hi", "hey", "please",
    "week", "weeks", "month", "months", "year", "years", "day", "days", "daily",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "he", "she", "him", "her", "his", "hers", "they", "them", "their", "i", "me", "my",
    "we", "our", "you", "your", "someone", "somebody", "anyone", "anybody",
    "does", "do", "did", "have", "has", "had",
    "get", "got", "getting", "gets", "give", "gave", "given", "giving",
    "make", "made", "makes", "making", "take", "took", "taken", "taking",
    "put", "puts", "putting", "let", "lets", "keeping", "keep", "kept",
    "show", "list", "tell", "please", "can", "could", "would", "should",
    "will", "just", "also", "only", "were", "been", "being",
    "them", "those", "these", "into", "onto",
    "all", "any", "some", "each", "every", "both", "between", "during", "before",
    "after", "since", "until", "per", "vs", "versus", "compare", "compared",
    "employee", "employees", "person", "people", "user", "users", "name", "named",
    "called", "aafs", "meeting", "meetings", "training", "call", "calls",
    "record", "records", "data", "info", "information", "report", "summary",
    "long", "short", "most", "least", "top", "bottom", "highest", "lowest",
    "first", "second", "third", "one", "two", "three", "four", "five",
    "hire", "hired", "hiring", "hires", "interview", "interviews", "interviewed",
    "reject", "rejected", "rejection", "rejects", "submittal", "submittals",
    "submitted", "submit", "submits", "submitting", "submission", "submissions",
    "candidate", "candidates", "recruiter", "recruiters",
    "company", "companies", "placement", "placements", "bill", "pay", "rate",
    "rates", "job", "jobs", "title", "division", "reason", "reasons",
    "internal", "external", "datavista", "prohance", "pipeline", "recruiting",
    "recruit", "recruited", "recruits", "worked", "work", "everyone", "slight",
    "date", "dates", "dated", "detail", "details", "field", "fields", "value",
    "values", "status", "email", "phone", "address", "when", "whenever",
    "whatever", "wherever", "which", "while", "find", "look", "looking",
    "search", "searched", "check", "checking", "want", "wanted", "need",
    "needed", "know", "known", "see", "saw", "ask", "asking", "told", "say",
    "said", "from", "into", "onto", "upon", "via", "using", "based", "according",
    "there", "here", "been", "being", "still", "already", "also", "even",
    "really", "very", "much", "more", "less", "same", "other", "another",
    "something", "anything", "nothing", "everything", "full", "exact",
    "agreed", "primary", "quickbooks", "flag", "flags", "master", "table",
    "tables", "column", "columns", "row", "rows", "query", "sql", "database",
    "db", "dtae", "dta", "teh", "wat", "wut",
    "monthly", "weekly", "yearly", "quarterly", "annually", "annual", "daily",
    "currently", "recently", "previous", "previously", "overall", "totaled",
    "metric", "metrics", "amount", "counts", "stats", "statistics",
    "breakdown", "trend", "trends", "comparison",
    "performance", "including", "include", "includes", "exclude", "excluding",
    "excluded", "weekend", "weekends", "weekday", "weekdays",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun",
    "offer", "offers", "offered", "start", "starts", "started", "starting",
    "give", "gimme", "show", "me", "get", "got",
    "by", "per", "each", "every", "across", "within", "without",
    "more", "than", "less", "over", "under", "above", "below",
    "nine", "ten", "eleven", "twelve",
}


def is_multi_part(question):
    return bool(question and MULTI_PART_PATTERN.search(question))


def is_recruiter_question(question):
    """
    Named people in DataVista are always the user/recruiter (never the candidate).
    Kept as a helper so prompt/UI wording stays consistent.
    """
    return True


def is_count_question(question):
    text = question or ""
    return bool(
        re.search(
            r"\b(how\s+many|count|number\s+of|total\s+#?|totals?)\b",
            text,
            re.IGNORECASE,
        )
    )


def datavista_stage(question):
    """Return submittal|interview|hire|reject|None from the question."""
    text = (question or "").lower()
    if re.search(r"\b(submittal|submittals|submitted|submit|submits|submission|submissions)\b", text):
        return "submittal"
    if re.search(r"\b(interview|interviews|interviewed)\b", text):
        return "interview"
    if re.search(r"\b(reject|rejects|rejected|rejection)\b", text):
        return "reject"
    if re.search(
        r"\b(hire|hires|hired|hiring|offer|offers|offered|placement|placements|"
        r"placement\s*date|offer\s*date|hire\s*date|start\s*date|bill\s*rate)\b",
        text,
    ):
        return "hire"
    return None


def datavista_table_guidance(question):
    text = (question or "").lower()
    stages = requested_datavista_stages(question)
    stage = stages[0] if len(stages) == 1 else datavista_stage(question)
    count_mode = is_count_question(question)

    recruiter_filter = (
        "Treat the named person as the USER/RECRUITER only (never as a candidate). "
        "Filter PRIMARYRECRUITERNAME / USERFIRSTNAME / USERLASTNAME / userid. "
    )

    if len(stages) >= 2:
        order_note = ", ".join(stages)
        return (
            "DATABASE: DataVista. MULTI-STAGE COUNTS. "
            + recruiter_filter
            + f"Return ONE SELECT with scalar COUNT(*) subqueries as columns in this order: {order_note}. "
            "Tables: submittals→CR_SubmittalMaster/SUBMITTALDATE, "
            "interviews→CR_InterviewMaster/INTERVIEWDATE, "
            "hires/offers→CR_HireMaster/PLACEMENTDATE, "
            "starts→CR_HireMaster/STARTDATE, "
            "rejects→CR_RejectMaster. "
            "Use TRY_CONVERT(date, ...) for date filters. "
            "Do NOT UNION tables. Do NOT invent JOINs across stages. "
            "Alias columns exactly: " + order_note + "."
        )

    if stage == "submittal":
        if count_mode:
            return (
                "DATABASE: DataVista. TABLE: CR_SubmittalMaster ONLY. "
                + recruiter_filter
                + "The user asked HOW MANY submittals. "
                "Return exactly: SELECT COUNT(*) AS submittal_count FROM "
                "DataVista.dbo.CR_SubmittalMaster with recruiter + SUBMITTALDATE filters. "
                "Do NOT UNION interview/hire/reject tables. "
                "Do NOT return detail rows — only the count."
            )
        return (
            "DATABASE: DataVista. TABLE: CR_SubmittalMaster ONLY. "
            + recruiter_filter
            + "Use SUBMITTALDATE. Do NOT UNION other CR_* tables."
        )

    if stage == "interview":
        if count_mode:
            return (
                "DATABASE: DataVista. TABLE: CR_InterviewMaster ONLY. "
                + recruiter_filter
                + "Return SELECT COUNT(*) AS interview_count only. No UNION. No detail rows."
            )
        return (
            "DATABASE: DataVista. TABLE: CR_InterviewMaster ONLY. "
            + recruiter_filter
            + "Use INTERVIEWDATE. Do NOT UNION other CR_* tables."
        )

    if stage == "reject":
        if count_mode:
            return (
                "DATABASE: DataVista. TABLE: CR_RejectMaster ONLY. "
                + recruiter_filter
                + "Return SELECT COUNT(*) AS reject_count only. No UNION. No detail rows."
            )
        return (
            "DATABASE: DataVista. TABLE: CR_RejectMaster ONLY. "
            + recruiter_filter
            + "Use INTERNALREJECTDATE / EXTERNALREJECTDATE. Do NOT UNION other CR_* tables."
        )

    if stage == "hire" or re.search(
        r"\b(placement\s*date|offer\s*date|hire\s*date|start\s*date|bill\s*rate)\b",
        text,
    ):
        if count_mode:
            return (
                "DATABASE: DataVista. TABLE: CR_HireMaster ONLY. "
                + recruiter_filter
                + "Return SELECT COUNT(*) AS hire_count only. No UNION. No detail rows. "
                "PLACEMENTDATE = offer/placement; STARTDATE = work start."
            )
        return (
            "DATABASE: DataVista. TABLE: CR_HireMaster ONLY. "
            + recruiter_filter
            + "PLACEMENTDATE = offer/placement date; STARTDATE = work start date. "
            "Do NOT UNION other CR_* tables."
        )

    if re.search(
        r"\b(how\s+many\s+clients?|clients?\s+did|performance|placed|placements?\s+did|"
        r"who\s+did|recruit)\b",
        text,
    ):
        return (
            "DATABASE: DataVista (recruiter/user performance). "
            + recruiter_filter
            + "If stage is unnamed, UNION ALL four CR_* tables with a Stage column. "
            "For client counts use COUNT(DISTINCT COMPANYNAME). "
            "For how-many without stage, COUNT rows (or DISTINCT candidates) across the UNION."
        )

    if re.search(
        r"\b(pay\s*rate|agreed\s*pay|company(?:\s*name)?|job\s*title)\b",
        text,
    ):
        return (
            "DATABASE: DataVista. Named person is the USER/RECRUITER. "
            "Stage unclear — UNION ALL four CR_* tables with Stage. "
            "Return rows for that recruiter where the requested fields are present."
        )

    return (
        "DATABASE: DataVista. Named person is always the USER/RECRUITER (not a candidate). "
        "If pipeline stage is unclear, UNION ALL all four CR_* tables with a Stage column. "
        "If the question says submittal/interview/hire/reject, use THAT table only."
    )


def normalize_user_question(question):
    """
    Expand shorthand chatbot phrasing into a clearer question.
    Examples:
      "disha how many submittals july 26" -> "... July 2026"
      "submits july 2026" -> "how many submittals in July 2026"
      "what's disha submittals" -> "how many submittals did disha have"
    """
    text = (question or "").strip()
    if not text:
        return text

    # Contractions / fillers that must never become names.
    text = re.sub(r"\bwhat['’]?s\b", "what is", text, flags=re.IGNORECASE)
    text = re.sub(r"\bwho['’]?s\b", "who is", text, flags=re.IGNORECASE)
    text = re.sub(r"\bwhere['’]?s\b", "where is", text, flags=re.IGNORECASE)
    text = re.sub(r"\bhow['’]?s\b", "how is", text, flags=re.IGNORECASE)
    text = re.sub(r"\bthat['’]?s\b", "that is", text, flags=re.IGNORECASE)
    text = re.sub(r"\bthere['’]?s\b", "there is", text, flags=re.IGNORECASE)

    # Common glued / typo forms from chat.
    text = re.sub(
        r"\bmonthly(submits?|submittals?|submissions?|hires?|interviews?|"
        r"starts?|rejects?|offers?)\b",
        r"monthly \1",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\blogged\s*ours\b", "logged hours", text, flags=re.IGNORECASE)
    text = re.sub(r"\blog+ed\s*hours?\b", "logged hours", text, flags=re.IGNORECASE)

    stage_words = (
        r"submits?|submittals?|submissions?|hires?|interviews?|rejects?|"
        r"clients?|placements?|offers?"
    )

    # "what is disha submittals" / "what is disha's submittal count"
    text = re.sub(
        rf"\bwhat\s+is\s+([A-Za-z][A-Za-z'.-]*)(?:'s)?\s+({stage_words})\b",
        r"how many \2 did \1 have",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"\bwhat\s+is\s+([A-Za-z][A-Za-z'.-]*)(?:'s)?\s+({stage_words})\s+count\b",
        r"how many \2 did \1 have",
        text,
        flags=re.IGNORECASE,
    )

    # july 26 / july '26 -> July 2026
    months = (
        "january|february|march|april|may|june|july|august|september|october|"
        "november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
    )

    def _expand_month_year(match):
        month = match.group(1)
        yy = int(match.group(2))
        year = 2000 + yy if yy < 100 else yy
        return f"{month} {year}"

    text = re.sub(
        rf"\b({months})\s+'?(\d{{2}})\b",
        _expand_month_year,
        text,
        flags=re.IGNORECASE,
    )

    already_count = bool(
        re.search(r"\b(how\s+many|count|number\s+of)\b", text, re.IGNORECASE)
    )

    # "submits/submittals July 2026" without how-many -> count question
    if (
        not already_count
        and re.search(rf"\b({stage_words})\b", text, re.I)
        and re.search(rf"\b({months}|\d{{4}})\b", text, re.I)
        and not re.search(r"\b(show|list|give|tell)\b", text, re.I)
    ):
        text = re.sub(r"\b(submits|submit)\b", "submittals", text, flags=re.I, count=1)
        text = "how many " + text
        already_count = True

    # "<name> submittals july 2026" (no how many) -> how many
    if (
        not already_count
        and re.search(rf"\b({stage_words})\b", text, re.I)
        and not re.search(r"\b(show|list|give|tell|monthly|month\s*by\s*month)\b", text, re.I)
        and extract_name_hints(text)
    ):
        text = "how many " + text
        already_count = True

    # Normalize submit -> submittals for count questions
    if already_count or re.search(r"\bhow\s+many\b", text, re.I):
        text = re.sub(r"\bhow\s+many\s+submits\b", "how many submittals", text, flags=re.I)
        text = re.sub(r"\bhow\s+many\s+submit\b", "how many submittals", text, flags=re.I)
        text = re.sub(r"\bhow\s+many\s+what\s+is\b", "how many", text, flags=re.I)

    return re.sub(r"\s+", " ", text).strip()


def is_mixed_performance_question(question):
    text = question or ""
    has_stages = bool(
        re.search(
            r"\b(submits?|submittals?|interviews?|offers?|hires?|starts?|rejects?)\b",
            text,
            re.I,
        )
    )
    has_hours = bool(re.search(r"\b(logged\s*hours?|avg|average)\b", text, re.I))
    has_perf = bool(re.search(r"\bperformance\b", text, re.I))
    return (has_perf and has_stages) or (has_stages and has_hours)


DATAVISTA_STAGE_METRICS = (
    "submittals",
    "interviews",
    "hires",
    "offers",
    "starts",
    "rejects",
)

_STAGE_TABLE_DATE = {
    "submittals": ("CR_SubmittalMaster", "SUBMITTALDATE"),
    "interviews": ("CR_InterviewMaster", "INTERVIEWDATE"),
    "hires": ("CR_HireMaster", "PLACEMENTDATE"),
    "offers": ("CR_HireMaster", "PLACEMENTDATE"),
    "starts": ("CR_HireMaster", "STARTDATE"),
    "rejects": ("CR_RejectMaster", "INTERNALREJECTDATE"),
}


def requested_datavista_stages(question):
    """Stage metrics in ask order (submittals, interviews, hires, ...)."""
    ordered = extract_requested_metric_order(question)
    stages = [m for m in ordered if m in DATAVISTA_STAGE_METRICS]
    if stages:
        return stages
    # Fallback when extract missed plurals / shorthand.
    text = question or ""
    found = []
    specs = [
        ("submittals", r"\bsubmitt?als?\b|\bsubmits?\b"),
        ("interviews", r"\binterviews?\b"),
        ("hires", r"\bhires?\b|\bhired\b"),
        ("offers", r"\boffers?\b"),
        ("starts", r"\bstarts?\b"),
        ("rejects", r"\brejects?\b|\brejected\b"),
    ]
    matches = []
    for alias, pat in specs:
        for match in re.finditer(pat, text, re.I):
            matches.append((match.start(), alias))
    matches.sort(key=lambda item: item[0])
    for _, alias in matches:
        if alias not in found:
            found.append(alias)
    return found


def is_multi_stage_datavista_question(question):
    return len(requested_datavista_stages(question)) >= 2


def _datavista_recruiter_filter(name, confirmed_employee_id=None):
    safe_name = sql_literal(name)
    parts = [p for p in re.split(r"\s+", name) if p]
    first = sql_literal(parts[0]) if parts else safe_name
    last = sql_literal(parts[-1]) if len(parts) > 1 else ""
    recruiter_filter = (
        "("
        f"PRIMARYRECRUITERNAME = '{safe_name}' "
        f"OR PRIMARYRECRUITERNAME LIKE '{safe_name} %' "
        f"OR PRIMARYRECRUITERNAME LIKE '{first}%' "
        f"OR USERFIRSTNAME = '{first}' "
    )
    if last:
        recruiter_filter += (
            f"OR (USERFIRSTNAME = '{first}' AND USERLASTNAME = '{last}') "
            f"OR (USERFIRSTNAME + ' ' + USERLASTNAME) = '{safe_name}' "
        )
    if confirmed_employee_id:
        recruiter_filter += f"OR userid = '{sql_literal(confirmed_employee_id)}' "
    recruiter_filter += ")"
    return recruiter_filter, first, safe_name


def _recruiting_period_bounds(question):
    """Month/year when named; otherwise current calendar year for open-ended lists."""
    text = question or ""
    ranged = extract_month_range_bounds(text)
    if ranged:
        return ranged
    month_named = bool(
        re.search(rf"\b({_CALENDAR_MONTHS})\b", text, re.IGNORECASE)
    )
    year = extract_year_from_question(text)
    if year is not None and not month_named:
        return f"{year}-01-01", f"{year + 1}-01-01"
    if re.search(r"\bthis\s+year\b", text, re.IGNORECASE) and not month_named:
        y = date.today().year
        return f"{y}-01-01", f"{y + 1}-01-01"
    if month_named or re.search(r"\bthis\s+month|last\s+month\b", text, re.IGNORECASE):
        return extract_period_bounds(question)
    y = date.today().year
    return f"{y}-01-01", f"{y + 1}-01-01"


def build_datavista_stage_counts_sql(
    question,
    confirmed_username=None,
    confirmed_employee_id=None,
    history=None,
    last_result=None,
):
    """
    Deterministic COUNT columns for multi-stage recruiting asks
    (e.g. hires + interviews + submits), in ask order.
    Month-by-month when requested — avoids LLM 42000 syntax errors.
    Follow-ups like "monthly for 26" / "month by month" inherit prior stages.
    """
    stages = requested_datavista_stages(question)
    prior_q = prior_question_text(history, last_result)
    if len(stages) < 1 and prior_q and (
        is_continuation_followup(question)
        or effective_wants_month_breakdown(question, history, last_result)
    ):
        stages = requested_datavista_stages(prior_q)

    month_mode = effective_wants_month_breakdown(
        question, history=history, last_result=last_result
    )
    # Month-by-month: one stage is enough. Totals still need 2+ stages.
    if month_mode:
        if len(stages) < 1:
            return None
    elif len(stages) < 2:
        return None

    name = (confirmed_username or "").strip()
    if not name:
        hints = extract_name_hints(question)
        if not hints and prior_q:
            hints = extract_name_hints(prior_q)
        if hints:
            name = " ".join(hints)
    if not name:
        name = _person_from_history_questions(history) or _extract_username_from_sql(
            _prior_sql_from_context(history, last_result)
        )
    if not name:
        return None

    start_iso, end_iso = _recruiting_period_bounds(question)
    # Follow-ups / month-by-month: inherit or force the right period.
    if month_mode:
        start_iso, end_iso = resolve_period_bounds(
            question, history=history, last_result=last_result
        )

    datavista_db = get_datavista_database_name()
    recruiter_filter, _, _ = _datavista_recruiter_filter(name, confirmed_employee_id)

    if month_mode:
        # One row per month; metric columns in ask order after month.
        union_parts = []
        for stage in stages:
            table, date_col = _STAGE_TABLE_DATE[stage]
            if stage == "rejects":
                date_expr = (
                    "COALESCE(TRY_CONVERT(date, INTERNALREJECTDATE), "
                    "TRY_CONVERT(date, EXTERNALREJECTDATE))"
                )
            else:
                date_expr = f"TRY_CONVERT(date, {date_col})"
            count_cols = []
            for s in stages:
                count_cols.append("COUNT(*)" if s == stage else "0")
            select_counts = ", ".join(
                f"{expr} AS {s}" for s, expr in zip(stages, count_cols)
            )
            union_parts.append(
                f"SELECT DATENAME(month, {date_expr}) AS month, "
                f"MONTH({date_expr}) AS _month_num, "
                f"YEAR({date_expr}) AS _year_num, "
                f"{select_counts} "
                f"FROM [{datavista_db}].[dbo].[{table}] "
                f"WHERE {date_expr} >= '{start_iso}' AND {date_expr} < '{end_iso}' "
                f"AND {recruiter_filter} "
                f"GROUP BY DATENAME(month, {date_expr}), MONTH({date_expr}), YEAR({date_expr})"
            )
        sum_cols = ", ".join(f"SUM({s}) AS {s}" for s in stages)
        return (
            "SELECT month, "
            + sum_cols
            + " FROM ("
            + " UNION ALL ".join(union_parts)
            + ") AS stage_months "
            "GROUP BY month, _month_num, _year_num "
            "ORDER BY _year_num, _month_num"
        )

    select_parts = []
    for stage in stages:
        table, date_col = _STAGE_TABLE_DATE[stage]
        if stage == "rejects":
            date_pred = (
                f"("
                f"(TRY_CONVERT(date, INTERNALREJECTDATE) >= '{start_iso}' "
                f"AND TRY_CONVERT(date, INTERNALREJECTDATE) < '{end_iso}') "
                f"OR (TRY_CONVERT(date, EXTERNALREJECTDATE) >= '{start_iso}' "
                f"AND TRY_CONVERT(date, EXTERNALREJECTDATE) < '{end_iso}')"
                f")"
            )
        else:
            date_pred = (
                f"TRY_CONVERT(date, {date_col}) >= '{start_iso}' "
                f"AND TRY_CONVERT(date, {date_col}) < '{end_iso}'"
            )
        select_parts.append(
            f"(SELECT COUNT(*) FROM [{datavista_db}].[dbo].[{table}] "
            f"WHERE {date_pred} AND {recruiter_filter}) AS {stage}"
        )

    return "SELECT " + ", ".join(select_parts)


def build_month_breakdown_with_hours_followup_sql(
    question,
    history=None,
    last_result=None,
    confirmed_username=None,
    confirmed_employee_id=None,
    primary_table=None,
):
    """
    Month-by-month recruiting stages + logged hours.
    Handles first asks ("monthly submits and logged hours") and follow-ups
    ("also show logged hours" after a month-by-month stage ask).
    """
    text = question or ""
    asks_hours = bool(
        re.search(
            r"\b(logged\s*hours?|logged\s*ours|avg(?:erage)?\s+(?:logged\s*)?hours?|"
            r"total\s+(?:logged\s*)?hours?|hours?\s+logged|\bhours?\b)\b",
            text,
            re.IGNORECASE,
        )
    )
    if not asks_hours:
        return None
    if not effective_wants_month_breakdown(text, history, last_result):
        return None

    prior_q = prior_question_text(history, last_result)
    stages = requested_datavista_stages(text)
    if len(stages) < 1 and prior_q:
        stages = requested_datavista_stages(prior_q)
    # If no recruiting stages in the thread, plain hours month-breakdown handles it.
    if len(stages) < 1:
        return None

    name = (confirmed_username or "").strip()
    if not name:
        hints = extract_name_hints(text) or extract_name_hints(prior_q)
        if hints:
            name = " ".join(hints)
    if not name:
        name = _person_from_history_questions(history) or _extract_username_from_sql(
            _prior_sql_from_context(history, last_result)
        )
    if not name:
        return None

    start_iso, end_iso = resolve_period_bounds(
        text, history=history, last_result=last_result
    )
    datavista_db = get_datavista_database_name()
    recruiter_filter, _, _ = _datavista_recruiter_filter(name, confirmed_employee_id)

    table_name = primary_table or (
        schema_provider.get_primary_table_name() if "schema_provider" in globals() else None
    ) or "EmployeeAttendance"
    db_name = get_connected_database_name()
    attendance_from = f"[{db_name}].[dbo].[{table_name}]" if db_name else f"[{table_name}]"
    safe_person = sql_literal(name)
    first = sql_literal(name.split()[0]) if name.split() else safe_person
    seconds_expr = (
        "COALESCE(DATEDIFF(SECOND, 0, "
        "TRY_CAST(NULLIF(LTRIM(RTRIM(logged_hours)), '') AS TIME)), 0)"
    )
    use_avg = bool(
        re.search(r"\bavg(?:erage)?\b", text, re.I)
        or re.search(r"\bavg(?:erage)?\b", prior_q or "", re.I)
    )
    hours_metric = (
        f"CAST(ROUND(AVG({seconds_expr}), 0) AS int)"
        if use_avg
        else f"CAST(SUM({seconds_expr}) AS int)"
    )
    hours_alias = "avg_seconds" if use_avg else "total_seconds"

    union_parts = []
    for stage in stages:
        table, date_col = _STAGE_TABLE_DATE[stage]
        if stage == "rejects":
            date_expr = (
                "COALESCE(TRY_CONVERT(date, INTERNALREJECTDATE), "
                "TRY_CONVERT(date, EXTERNALREJECTDATE))"
            )
        else:
            date_expr = f"TRY_CONVERT(date, {date_col})"
        count_cols = ["COUNT(*)" if s == stage else "0" for s in stages]
        select_counts = ", ".join(f"{expr} AS {s}" for s, expr in zip(stages, count_cols))
        union_parts.append(
            f"SELECT DATENAME(month, {date_expr}) AS month, "
            f"MONTH({date_expr}) AS _month_num, YEAR({date_expr}) AS _year_num, "
            f"{select_counts} "
            f"FROM [{datavista_db}].[dbo].[{table}] "
            f"WHERE {date_expr} >= '{start_iso}' AND {date_expr} < '{end_iso}' "
            f"AND {recruiter_filter} "
            f"GROUP BY DATENAME(month, {date_expr}), MONTH({date_expr}), YEAR({date_expr})"
        )

    # Aggregate stages first, then join hours. Use a single alias (AS s) —
    # "AS stage_rows s" is invalid T-SQL and caused SQL error 42000.
    sum_cols = ", ".join(f"SUM({stage}) AS {stage}" for stage in stages)
    return (
        "SELECT s.month, "
        + ", ".join(f"s.{stage}" for stage in stages)
        + f", h.{hours_alias} AS {hours_alias} "
        "FROM ("
        f"SELECT month, _month_num, _year_num, {sum_cols} "
        "FROM ("
        + " UNION ALL ".join(union_parts)
        + ") AS stage_rows "
        "GROUP BY month, _month_num, _year_num"
        ") AS s "
        "LEFT JOIN ("
        f"SELECT DATENAME(month, sessionDate) AS month, "
        f"MONTH(sessionDate) AS _month_num, YEAR(sessionDate) AS _year_num, "
        f"{hours_metric} AS {hours_alias} "
        f"FROM {attendance_from} "
        f"WHERE (userName = '{safe_person}' OR userName LIKE '{safe_person}%' "
        f"OR userName LIKE '{first}%') "
        f"AND sessionDate >= '{start_iso}' AND sessionDate < '{end_iso}' "
        "GROUP BY DATENAME(month, sessionDate), MONTH(sessionDate), YEAR(sessionDate)"
        ") AS h ON s._month_num = h._month_num AND s._year_num = h._year_num "
        "ORDER BY s._year_num, s._month_num"
    )


_MONTH_NAME_TO_NUM = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}


def _month_alt_pattern():
    return "|".join(sorted(_MONTH_NAME_TO_NUM.keys(), key=len, reverse=True))


def _add_months(year, month, delta):
    """Return (year, month) after adding delta months (month is 1-12)."""
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def extract_month_range_bounds(question):
    """
    Inclusive month ranges:
      "from January to February", "January till March", "Jan through March 2026"
      "between January and March"
    Returns (start_iso, end_iso) with end exclusive (day after last included month),
    or None if no range is present.
    Does NOT match "month to month".
    """
    text = question or ""
    months = _month_alt_pattern()
    year_default = date.today().year

    patterns = [
        # from January [2026] to/till/until/through February [2026]
        rf"(?:from\s+)?({months})(?:\s+(\d{{4}}))?\s+"
        rf"(?:to|till|until|through)\s+({months})(?:\s+(\d{{4}}))?",
        # between January [2026] and March [2026]
        rf"\bbetween\s+({months})(?:\s+(\d{{4}}))?\s+and\s+({months})(?:\s+(\d{{4}}))?",
    ]
    match = None
    for pat in patterns:
        match = re.search(pat, text, re.IGNORECASE)
        if match:
            break
    if not match:
        return None

    start_name, start_year_s, end_name, end_year_s = match.groups()
    start_month = _MONTH_NAME_TO_NUM[start_name.lower()]
    end_month = _MONTH_NAME_TO_NUM[end_name.lower()]
    start_year = int(start_year_s) if start_year_s else None
    end_year = int(end_year_s) if end_year_s else None

    # Bare year elsewhere in the question (e.g. "January to March for 2026")
    loose_year = extract_year_from_question(text)
    if start_year is None and end_year is None:
        start_year = loose_year or year_default
        end_year = start_year
    elif start_year is None:
        start_year = end_year
    elif end_year is None:
        end_year = start_year

    # Cross-year shorthand: Nov to Feb with one year → end in next year
    if end_year < start_year or (
        end_year == start_year and end_month < start_month and not end_year_s and not start_year_s
    ):
        end_year = start_year + 1

    start = date(start_year, start_month, 1)
    end_y, end_m = _add_months(end_year, end_month, 1)
    end = date(end_y, end_m, 1)
    return start.isoformat(), end.isoformat()


def extract_month_year_bounds(question):
    """Return (start_iso, end_iso) for a named month, or this month if none named."""
    ranged = extract_month_range_bounds(question)
    if ranged:
        return ranged

    text = question or ""
    today = date.today()
    months = _month_alt_pattern()
    year = today.year
    month = today.month

    match = re.search(rf"\b({months})\s+(\d{{4}})\b", text, re.IGNORECASE)
    if match:
        month = _MONTH_NAME_TO_NUM[match.group(1).lower()]
        year = int(match.group(2))
    else:
        match = re.search(rf"\b({months})\b", text, re.IGNORECASE)
        if match:
            month = _MONTH_NAME_TO_NUM[match.group(1).lower()]
            year = extract_year_from_question(text) or today.year
        elif re.search(r"\bthis\s+month\b", text, re.IGNORECASE):
            month = today.month
            year = today.year

    start = date(year, month, 1)
    end_y, end_m = _add_months(year, month, 1)
    end = date(end_y, end_m, 1)
    return start.isoformat(), end.isoformat()


def build_mixed_performance_sql(
    question,
    confirmed_username=None,
    confirmed_employee_id=None,
    primary_table=None,
):
    """
    Deterministic SQL for recruiter performance + avg logged hours.
    Avoids LLM inventing invalid DATEDIFF / object names (42000 / 42S02).
    Single-row totals only — monthly / month-by-month mixed asks are handled
    by build_month_breakdown_with_hours_followup_sql.
    """
    if not is_mixed_performance_question(question):
        return None
    # Do not steal month-by-month asks (e.g. monthly submits + logged hours).
    if wants_month_breakdown(question):
        return None

    name = (confirmed_username or "").strip()
    if not name:
        hints = extract_name_hints(question)
        if not hints:
            return None
        name = " ".join(hints)

    safe_name = sql_literal(name)
    parts = [p for p in re.split(r"\s+", name) if p]
    first = sql_literal(parts[0]) if parts else safe_name
    last = sql_literal(parts[-1]) if len(parts) > 1 else ""

    start_iso, end_iso = extract_month_year_bounds(question)
    datavista_db = get_datavista_database_name()
    prohance_db = get_connected_database_name()
    table_name = primary_table or (
        schema_provider.get_primary_table_name() if "schema_provider" in globals() else None
    ) or "EmployeeAttendance"

    if not prohance_db:
        attendance_from = f"[dbo].[{table_name}]"
    else:
        attendance_from = f"[{prohance_db}].[dbo].[{table_name}]"

    recruiter_filter = (
        "("
        f"PRIMARYRECRUITERNAME = '{safe_name}' "
        f"OR PRIMARYRECRUITERNAME LIKE '{safe_name} %' "
        f"OR PRIMARYRECRUITERNAME LIKE '{first}%' "
        f"OR USERFIRSTNAME = '{first}' "
    )
    if last:
        recruiter_filter += (
            f"OR (USERFIRSTNAME = '{first}' AND USERLASTNAME = '{last}') "
            f"OR (USERFIRSTNAME + ' ' + USERLASTNAME) = '{safe_name}' "
        )
    if confirmed_employee_id:
        recruiter_filter += f"OR userid = '{sql_literal(confirmed_employee_id)}' "
    recruiter_filter += ")"

    hours_person = (
        f"(userName = '{safe_name}' OR userName LIKE '{safe_name}%' OR userName LIKE '{first}%')"
    )

    def _count_subquery(table, date_col, alias):
        return (
            f"(SELECT COUNT(*) FROM [{datavista_db}].[dbo].[{table}] "
            f"WHERE TRY_CONVERT(date, {date_col}) >= '{start_iso}' "
            f"AND TRY_CONVERT(date, {date_col}) < '{end_iso}' "
            f"AND {recruiter_filter}) AS {alias}"
        )

    avg_hours = (
        "(SELECT CAST(ROUND(AVG(COALESCE("
        "DATEDIFF(SECOND, 0, TRY_CAST(NULLIF(LTRIM(RTRIM(logged_hours)), '') AS TIME)), "
        "0)), 0) AS int) "
        f"FROM {attendance_from} "
        f"WHERE {hours_person} "
        f"AND sessionDate >= '{start_iso}' "
        f"AND sessionDate < '{end_iso}') AS avg_logged_seconds"
    )

    return (
        "SELECT "
        + ", ".join(
            [
                _count_subquery("CR_SubmittalMaster", "SUBMITTALDATE", "submittals"),
                _count_subquery("CR_InterviewMaster", "INTERVIEWDATE", "interviews"),
                _count_subquery("CR_HireMaster", "PLACEMENTDATE", "offers"),
                _count_subquery("CR_HireMaster", "STARTDATE", "starts"),
                avg_hours,
            ]
        )
    )


def enhance_for_sql(question, history=None, domain=None):
    notes = []
    text = question or ""
    active_domain = domain or detect_question_domain(question, history=history)

    if is_multi_part(question):
        notes.append(
            "IMPORTANT: This message asks MULTIPLE things at once. "
            "Return exactly ONE SELECT statement (no semicolons) that answers EVERY part. "
            "Combine results using multiple columns, aggregates, CASE/SUM, and subqueries in the same query. "
            "Do NOT answer only the first metric — every requested metric must appear as its own column."
        )

    requested_metrics = extract_requested_metric_order(question)
    if len(requested_metrics) >= 2:
        notes.append(
            "REQUIRED METRICS (include ALL as separate output columns in one SELECT): "
            + ", ".join(requested_metrics)
            + ". Never omit any of these. If this is month-by-month, each month row must "
            "contain every metric column."
        )

    if re.search(r"\b(average|avg|mean|total|sum|overall)\b", text, re.IGNORECASE):
        notes.append(
            "IMPORTANT: The user asked for an aggregate. Use AVG/SUM in SQL over matching rows. "
            "Do NOT return only the first raw row. "
            "Duration fields may be VARCHAR 'HH:MM:SS' — convert to seconds before AVG/SUM. "
            "Return the aggregate as INTEGER seconds (alias total_seconds or avg_seconds). "
            "NEVER CONVERT/DATEADD back to TIME/HH:MM:SS for totals — SQL TIME wraps at 24 hours "
            "and month totals would be wrong. The app formats seconds as hours:minutes:seconds."
        )

    if re.search(
        r"\b(break|breaks|aafs|logged\s*hours?|lunch|personal\s*time)\b",
        text,
        re.IGNORECASE,
    ):
        notes.append(
            "IMPORTANT: Break/AAFS/logged_hours values look like '00:53:45'. "
            "Never COALESCE(column, 0) or AVG(column) directly on those varchar times. "
            "For total logged hours / total breaks in a week or month, SUM the seconds and "
            "return total_seconds / break_seconds only (no TIME convert). "
            "Alias break totals as break_seconds or total_break_seconds — NEVER alias as duration."
        )

    if active_domain == "datavista":
        notes.append(datavista_table_guidance(question))

    if re.search(r"\bthis\s+month\b", text, re.IGNORECASE):
        notes.append(
            'IMPORTANT: "this month" means the current calendar month from today\'s date '
            "(see date bounds in the system prompt). Do not use a different month/year."
        )
    if re.search(r"\bthis\s+year\b", text, re.IGNORECASE):
        notes.append(
            'IMPORTANT: "this year" means the current calendar year from today\'s date '
            "(Jan 1 through Dec 31 of that year). Do not use a different year."
        )

    month_note = month_breakdown_guidance(
        question, history=history, last_result=None
    )
    if month_note:
        notes.append(month_note)

    attendance_note = attendance_status_guidance(question)
    if attendance_note:
        notes.append(attendance_note)

    exclude_note = exclusion_sql_guidance(question, history=history)
    if exclude_note:
        notes.append(exclude_note)

    if is_continuation_followup(question):
        notes.append(
            "FOLLOW-UP: Keep all filters from the previous question "
            "(person, date range, month-by-month grouping, thresholds, day exclusions). "
            "If the previous answer was month-by-month, this follow-up MUST also return "
            "one row per month for that same period (full year or January–March range) — "
            "do NOT collapse to this month only. "
            "Only change what the user newly asked for (e.g. ADD logged hours as a column). "
            "If weekends were already excluded and the user now excludes Fridays, "
            "keep Saturday/Sunday out AND also exclude Friday. "
            "CRITICAL: Write fresh SQL and recalculate. "
            "Do not copy the previous answer's hours/average unchanged."
        )

    # Mixed recruiting + attendance performance questions.
    if (
        re.search(r"\bperformance\b", text, re.IGNORECASE)
        or (
            re.search(r"\b(submits?|submittals?|interviews?|offers?|starts?|hires?)\b", text, re.I)
            and re.search(r"\b(logged\s*hours?|avg|average)\b", text, re.I)
        )
    ):
        prohance_db = get_connected_database_name() or "YOUR_PROHANCE_DB"
        prohance_table = schema_provider.get_primary_table_name() or "EmployeeAttendance"
        datavista_db = get_datavista_database_name()
        if wants_month_breakdown(text):
            notes.append(
                "MONTHLY MIXED METRICS (REQUIRED): Return ONE SELECT with "
                "ONE ROW PER MONTH for the period (full year unless a single "
                "month/range was named). Columns: month, then each requested "
                "stage count, then logged hours seconds. "
                "Do NOT return a single total row. "
                "Do NOT use only scalar subqueries without DATENAME(month, ...) "
                "GROUP BY. "
                f"Use [{datavista_db}].[dbo].[CR_*] and "
                f"[{prohance_db}].[dbo].[{prohance_table}]. "
                "Hours alias: total_seconds or avg_seconds (integer seconds)."
            )
        else:
            notes.append(
                "PERFORMANCE / MIXED METRICS: Named person is the recruiter/user. "
                "Return ONE SELECT with scalar subqueries / columns only. "
                f"Use EXACT three-part names (do not invent databases/tables): "
                f"[{datavista_db}].[dbo].[CR_SubmittalMaster], "
                f"[{datavista_db}].[dbo].[CR_InterviewMaster], "
                f"[{datavista_db}].[dbo].[CR_HireMaster], "
                f"[{prohance_db}].[dbo].[{prohance_table}]. "
                "Never invent a database named Prohance/Workforce/Attendance — "
                f"attendance lives in [{prohance_db}].[dbo].[{prohance_table}]. "
                "Alias columns exactly: submittals, interviews, offers, starts, avg_logged_seconds. "
                "For avg hours use EXACTLY: "
                "CAST(ROUND(AVG(COALESCE(DATEDIFF(SECOND, 0, "
                "TRY_CAST(NULLIF(LTRIM(RTRIM(logged_hours)), '') AS TIME)), 0)), 0) AS int). "
                "Never write 'DATEDIFF seconds of logged_hours' — that is invalid SQL. "
                "Filter DataVista on PRIMARYRECRUITERNAME / USERFIRSTNAME / USERLASTNAME."
            )

    if not notes:
        return question

    return text + "\n\n" + "\n".join(notes)


def enhance_for_answer(question):
    base = (
        "\n\nIMPORTANT: Answer in 1-2 short natural-language sentences only. "
        "Example style: \"Akshay Soni logged 45:30:00 in July.\" "
        "When a time field is present, use the exact hours:minutes:seconds value "
        "from the data (e.g. 08:15:00 or 160:05:12), not days/weeks wording. "
        "Lead with the person, the metric, and the time period."
    )
    if month_breakdown_guidance(question):
        base += (
            " If the data is month-by-month, use month NAMES (January, February, ...) "
            "and cover every month present in the data, not only the first few."
        )
    if attendance_status_guidance(question):
        base += (
            " Attendance: present = days with >= 7 logged hours, "
            "halfday = 5–7 hours, absent = under 5 hours. "
            "State the count(s) clearly."
        )
    requested_metrics = extract_requested_metric_order(question)
    if len(requested_metrics) >= 2:
        base += (
            " The user asked for multiple metrics ("
            + ", ".join(requested_metrics)
            + "). Mention ALL of them in your 1-2 sentences — do not cover only the first."
        )
    if exclusion_sql_guidance(question) or (
        is_continuation_followup(question)
        and re.search(
            r"\b(exclud|without|except|weekend|weekday|friday|saturday|sunday)\b",
            question or "",
            re.IGNORECASE,
        )
    ):
        base += (
            " This is a filter follow-up that recalculated the metric. "
            "State the new average/total and which days were excluded "
            "(for example: excluding Saturdays, Sundays, and Fridays). "
            "Do NOT repeat an earlier number if the data shows a different value. "
            "Do NOT say you couldn't find Fridays/weekends as a field — those are day filters."
        )
    if not is_multi_part(question):
        return (question or "") + base

    return (
        (question or "")
        + base
        + " Cover every part of the question inside those 1-2 sentences."
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


def format_for_prompt(
    question,
    history,
    confirmed_username=None,
    confirmed_employee_id=None,
    domain="prohance",
):
    lines = []
    if history:
        lines.append("Conversation so far:")
        for item in history[-8:]:
            speaker = "User" if item["role"].lower() == "user" else "Assistant"
            lines.append(f"{speaker}: {item['content']}")

    lines.append(f"Current question: {question}" if lines else question)
    lines.append(f"Active database domain: {domain}")
    if extract_name_hints(question) and not is_comparison_question(question):
        lines.append(
            "IMPORTANT: The current question names a person. "
            "Answer only about that person. Do not reuse a different person "
            "from earlier conversation turns unless this is an explicit comparison."
        )

    if domain == "datavista":
        # Named people are always the recruiter/user — never candidates.
        if confirmed_employee_id:
            lines.append(
                f"The user confirmed recruiter/user id exactly: {confirmed_employee_id}. "
                "Filter with userid = that value OR match PRIMARYRECRUITERNAME / USER names "
                "for that person. Never filter candidate name columns."
            )
        elif confirmed_username:
            parts = str(confirmed_username).split()
            if len(parts) >= 2:
                first = parts[0]
                last = parts[-1]
                lines.append(
                    f"The user confirmed recruiter/user: {confirmed_username}. "
                    f"Filter WHERE PRIMARYRECRUITERNAME = '{confirmed_username}' "
                    f"OR (USERFIRSTNAME = '{first}' AND USERLASTNAME = '{last}') "
                    f"OR (USERFIRSTNAME + ' ' + USERLASTNAME) = '{confirmed_username}'. "
                    "Never filter CANDIDATEFIRSTNAME/CANDIDATELASTNAME for this person."
                )
            else:
                lines.append(
                    f"The user confirmed recruiter/user: {confirmed_username}. "
                    "Match PRIMARYRECRUITERNAME / USERFIRSTNAME / USERLASTNAME to that person. "
                    "Never treat them as a candidate."
                )
        if is_count_question(question) and datavista_stage(question):
            lines.append(
                "COUNT question for a specific stage: return one COUNT(*) value only "
                "from that stage's table. Do not UNION other stages or return detail rows."
            )
    else:
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
                    return repair_select_query(str(value))
    except ValueError:
        pass

    # Prefer a full CTE (WITH ... SELECT) over the first inner SELECT.
    with_match = re.search(r"(?:^|;)\s*(WITH\b[\s\S]+)", raw, re.IGNORECASE)
    select_match = re.search(r"\bSELECT\b[\s\S]+", raw, re.IGNORECASE)
    # Models sometimes omit SELECT on "how many" answers: COUNT(*) FROM ...
    aggregate_match = re.search(
        r"\b((?:COUNT|AVG|SUM|MIN|MAX)\s*\([\s\S]+)",
        raw,
        re.IGNORECASE,
    )

    if with_match and select_match and with_match.start() <= select_match.start():
        return repair_select_query(with_match.group(1).strip().rstrip(";"))
    if select_match:
        return repair_select_query(select_match.group(0).strip().rstrip(";"))
    if with_match:
        return repair_select_query(with_match.group(1).strip().rstrip(";"))
    if aggregate_match:
        return repair_select_query(
            f"SELECT {aggregate_match.group(1).strip().rstrip(';')}"
        )

    return repair_select_query(raw)


def normalize_readonly_sql(sql):
    """Strip leading noise that SQL Server models often add before a SELECT/WITH."""
    if not sql:
        return sql

    sql = sql.strip().lstrip(";").strip()
    sql = sql.lstrip("\ufeff\u200b\u200c\u200d").strip()
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


def _strip_sql_wrapper_noise(sql):
    """Remove trailing BEGIN/END wrappers and leftover JSON/markdown crumbs."""
    if not sql:
        return sql
    sql = sql.strip().rstrip(";").strip()
    sql = re.sub(r"^\s*BEGIN\s+", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\s+END\s*$", "", sql, flags=re.IGNORECASE)
    # Leftover from {"query":"..."} only — never strip a lone trailing quote,
    # which would break LIKE '%Name%' and cause SQL error 42000.
    sql = re.sub(r'["\']\}\s*$', "", sql).strip()
    sql = re.sub(r"\}\s*$", "", sql).strip()
    return sql.rstrip(";").strip()


def repair_select_query(sql):
    """Fix common model mistakes so valid read-only queries are not blocked."""
    if not sql or not sql.strip():
        return sql

    sql = normalize_readonly_sql(strip_comments(sql))
    sql = _strip_sql_wrapper_noise(sql)
    if not sql:
        return sql

    # Already a normal SELECT / CTE / subquery.
    upper = sql.upper()
    if upper.startswith("SELECT") or upper.startswith("WITH") or upper.startswith("("):
        return _strip_sql_wrapper_noise(sql)

    # Inline DECLARE @x = expr first so SELECT does not keep undeclared @vars.
    if re.search(r"\bDECLARE\b", sql, re.IGNORECASE):
        sql = expand_declare_variables(sql)
        sql = _strip_sql_wrapper_noise(sql)
        upper = sql.upper()
        if upper.startswith("SELECT") or upper.startswith("WITH") or upper.startswith("("):
            return sql

    # Drop leading DECLARE @x = ... before a SELECT (keep the SELECT only).
    declare_select = re.search(
        r"\bDECLARE\b[\s\S]*?\b(SELECT\b[\s\S]+)",
        sql,
        re.IGNORECASE,
    )
    if declare_select:
        return _strip_sql_wrapper_noise(declare_select.group(1))

    # "how many ..." answers sometimes come back as: COUNT(*) FROM table WHERE ...
    if re.match(r"^(?:COUNT|AVG|SUM|MIN|MAX)\s*\(", sql, re.IGNORECASE):
        return f"SELECT {_strip_sql_wrapper_noise(sql)}"

    # Or prose before the real query — pull SELECT/WITH/aggregate out.
    select_match = re.search(r"\bSELECT\b[\s\S]+", sql, re.IGNORECASE)
    with_match = re.search(r"\bWITH\b[\s\S]+", sql, re.IGNORECASE)
    aggregate_match = re.search(
        r"\b((?:COUNT|AVG|SUM|MIN|MAX)\s*\([\s\S]+)",
        sql,
        re.IGNORECASE,
    )
    if with_match and (not select_match or with_match.start() <= select_match.start()):
        return _strip_sql_wrapper_noise(with_match.group(0))
    if select_match:
        return _strip_sql_wrapper_noise(select_match.group(0))
    if aggregate_match:
        return f"SELECT {_strip_sql_wrapper_noise(aggregate_match.group(1))}"

    return sql


def _split_sql_comma_args(text):
    """Split on commas that are outside parentheses and string literals."""
    parts = []
    buf = []
    depth = 0
    in_quote = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_quote:
            buf.append(ch)
            if ch == "'" and i + 1 < len(text) and text[i + 1] == "'":
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == "'":
                in_quote = False
            i += 1
            continue
        if ch == "'":
            in_quote = True
            buf.append(ch)
            i += 1
            continue
        if ch == "(":
            depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
            i += 1
            continue
        if ch == "," and depth == 0:
            piece = "".join(buf).strip()
            if piece:
                parts.append(piece)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    piece = "".join(buf).strip()
    if piece:
        parts.append(piece)
    return parts


def expand_declare_variables(sql):
    """
    Inline DECLARE @var = expr so we do not leave undeclared @variables
    after stripping DECLARE (a common 42000 failure).
    """
    if not sql or not re.search(r"\bDECLARE\b", sql, re.IGNORECASE):
        return sql

    assignments = {}

    def collect_declare(match):
        body = match.group(1).strip().rstrip(";")
        for part in _split_sql_comma_args(body):
            assign = re.match(
                r"(@\w+)\s+(?:AS\s+)?[A-Za-z][A-Za-z0-9_\(\)\s]*?\s*=\s*(.+)$",
                part,
                re.IGNORECASE | re.DOTALL,
            )
            if assign:
                assignments[assign.group(1)] = assign.group(2).strip().rstrip(";")
        return " "

    sql = re.sub(
        r"\bDECLARE\b\s+((?:(?!\bSELECT\b|\bWITH\b|\bSET\b).)+)",
        collect_declare,
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )

    for var, expr in assignments.items():
        sql = re.sub(rf"(?<!\w){re.escape(var)}\b", f"({expr})", sql)

    return sql.strip()


def fix_datavista_schema(sql):
    """Rewrite common wrong table/column names the model invents for DataVista."""
    if not sql:
        return sql

    # Only rewrite invented TABLE names in table position (FROM/JOIN/APPLY).
    # Never rewrite aliases like "AS submittals" — that produced 42000 syntax errors.
    table_aliases = {
        "cr_submitmaster": "CR_SubmittalMaster",
        "cr_submittals": "CR_SubmittalMaster",
        "cr_submissionmaster": "CR_SubmittalMaster",
        "submittalmaster": "CR_SubmittalMaster",
        "submissionmaster": "CR_SubmittalMaster",
        "cr_hire": "CR_HireMaster",
        "cr_hires": "CR_HireMaster",
        "hiremaster": "CR_HireMaster",
        "cr_interview": "CR_InterviewMaster",
        "interviewmaster": "CR_InterviewMaster",
        "cr_reject": "CR_RejectMaster",
        "rejectmaster": "CR_RejectMaster",
    }
    for wrong, right in table_aliases.items():
        sql = re.sub(
            rf"(\b(?:FROM|JOIN|APPLY)\s+)"
            rf"(?:(?:\[[^\]]+\]|[A-Za-z0-9_]+)\s*\.\s*)?"
            rf"(?:\[?dbo\]?\s*\.\s*)?"
            rf"\[?{re.escape(wrong)}\]?\b",
            rf"\1{right}",
            sql,
            flags=re.IGNORECASE,
        )

    columns = {
        "submitdate": "SUBMITTALDATE",
        "submit_date": "SUBMITTALDATE",
        "submissiondate": "SUBMITTALDATE",
        "submission_date": "SUBMITTALDATE",
        "submittal_date": "SUBMITTALDATE",
        "submitteddate": "SUBMITTALDATE",
        "submitted_date": "SUBMITTALDATE",
        "recruitername": "PRIMARYRECRUITERNAME",
        "recruiter_name": "PRIMARYRECRUITERNAME",
        "primaryrecruiter": "PRIMARYRECRUITERNAME",
        "primary_recruiter": "PRIMARYRECRUITERNAME",
        "primary_recruiter_name": "PRIMARYRECRUITERNAME",
        "interview_date": "INTERVIEWDATE",
        "hiredate": "PLACEMENTDATE",
        "hire_date": "PLACEMENTDATE",
        "offerdate": "PLACEMENTDATE",
        "offer_date": "PLACEMENTDATE",
        "placement_date": "PLACEMENTDATE",
        "start_date": "STARTDATE",
        "payrate": "AGREEDPAYRATE",
        "pay_rate": "AGREEDPAYRATE",
        "agreed_pay_rate": "AGREEDPAYRATE",
        "billrate": "AGREEDBILLRATE",
        "bill_rate": "AGREEDBILLRATE",
        "company_name": "COMPANYNAME",
        "job_title": "JOBTITLE",
        "reject_reason": "REJECTREASON",
    }
    for wrong, right in columns.items():
        sql = re.sub(rf"\b{wrong}\b", right, sql, flags=re.IGNORECASE)

    # Dates are NVARCHAR — YEAR/MONTH/DAY need TRY_CONVERT first.
    date_cols = (
        "SUBMITTALDATE",
        "INTERVIEWDATE",
        "PLACEMENTDATE",
        "STARTDATE",
        "INTERNALREJECTDATE",
        "EXTERNALREJECTDATE",
    )
    for col in date_cols:
        sql = re.sub(
            rf"\b(YEAR|MONTH|DAY)\s*\(\s*\[?{col}\]?\s*\)",
            rf"\1(TRY_CONVERT(date, {col}))",
            sql,
            flags=re.IGNORECASE,
        )
        # Bare date comparisons: SUBMITTALDATE >= '2026-07-01'
        # Skip when already wrapped in TRY_CONVERT(date, ...).
        sql = re.sub(
            rf"(?<!TRY_CONVERT\(date, )(?<![\w.])\[?{col}\]?\s*(=|<>|!=|>=|<=|>|<)\s*",
            lambda m, c=col: f"TRY_CONVERT(date, {c}) {m.group(1)} ",
            sql,
            flags=re.IGNORECASE,
        )
        sql = re.sub(
            rf"(?<!TRY_CONVERT\(date, )(?<![\w.])\[?{col}\]?\s+BETWEEN\b",
            f"TRY_CONVERT(date, {col}) BETWEEN",
            sql,
            flags=re.IGNORECASE,
        )

    sql = re.sub(
        r"TRY_CONVERT\s*\(\s*date\s*,\s*TRY_CONVERT\s*\(\s*date\s*,\s*([A-Za-z0-9_]+)\s*\)\s*\)",
        r"TRY_CONVERT(date, \1)",
        sql,
        flags=re.IGNORECASE,
    )
    return sql


def fix_prohance_object_names(sql, db_name=None, table_name=None):
    """
    Prevent 42S02 from invented names like [Prohance].[dbo].[EmployeeAttendance].
    Rewrite to the connected database + live attendance table.
    """
    if not sql:
        return sql

    db_name = db_name or get_connected_database_name()
    table_name = table_name or (
        schema_provider.get_primary_table_name() if "schema_provider" in globals() else None
    ) or "EmployeeAttendance"
    if not db_name:
        return sql

    target = f"[{db_name}].[dbo].[{table_name}]"
    attendance_tables = {"employeeattendance", table_name.lower()}

    def _rewrite_three_part(match):
        found_db = (match.group(1) or "").strip("[]")
        found_table = (match.group(2) or "").strip("[]")
        if found_table.lower() not in attendance_tables:
            return match.group(0)
        # Already correct.
        if found_db.lower() == db_name.lower() and found_table.lower() == table_name.lower():
            return match.group(0)
        # Any other database label (Prohance, DataVista, etc.) → live attendance object.
        return target

    # Three-part attendance refs with a wrong DB/table → connected object.
    # Prefer fully-bracketed matches first so a leading "[" is never left behind.
    sql = re.sub(
        rf"\[([A-Za-z0-9_]+)\]\s*\.\s*\[dbo\]\s*\.\s*\[([A-Za-z0-9_]+)\]",
        _rewrite_three_part,
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        rf"\b([A-Za-z0-9_]+)\s*\.\s*dbo\s*\.\s*([A-Za-z0-9_]+)\b",
        _rewrite_three_part,
        sql,
        flags=re.IGNORECASE,
    )

    # Bare / single-bracket attendance table → three-part when query touches DataVista.
    # Skip names already qualified as something.[dbo].[Table].
    if re.search(r"\bCR_(?:Submittal|Interview|Hire|Reject)Master\b", sql, re.IGNORECASE):
        for att_name in sorted({table_name, "EmployeeAttendance"}, key=len, reverse=True):
            sql = re.sub(
                rf"(?<!\.\[dbo\]\.)(?<!\.dbo\.)\[{re.escape(att_name)}\]",
                target,
                sql,
                flags=re.IGNORECASE,
            )
            sql = re.sub(
                rf"(?<![\w.\[])\b{re.escape(att_name)}\b(?![\w.\]])",
                target,
                sql,
                flags=re.IGNORECASE,
            )

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
    sql = expand_declare_variables(sql)
    sql = repair_select_query(sql)
    sql = normalize_readonly_sql(sql)
    sql = fix_username_prefix_match(fix_workforce_schema(sql, actual_table_name))
    sql = fix_datavista_schema(sql)
    sql = qualify_datavista_sql(sql)
    sql = fix_prohance_object_names(sql, table_name=actual_table_name)
    sql = convert_limit_to_top(sql)
    sql = rewrite_duration_time_converts(sql)
    sql = ensure_single_readonly_sql(sql)
    return repair_select_query(sql)

def fix_username_prefix_match(sql):
    """
    Prefer prefix matches for userName.
    Keep multi-token patterns like '%Akshay%Soni%' intact (only drop the leading %).
    """
    def repl_multi(match):
        quote = match.group(2)
        body = match.group(3)
        # '%Akshay%Soni%' -> 'Akshay%Soni%'
        return f"{match.group(1)} LIKE {quote}{body}{quote}"

    sql = re.sub(
        r"(\buserName\b)\s+LIKE\s+(['\"])%((?:[^'\"%]+%)+[^'\"%]*)\2",
        repl_multi,
        sql,
        flags=re.IGNORECASE,
    )
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


def _extract_balanced_call(sql, start_index):
    """Given index at the '(' of a function call, return (inner, end_index_exclusive)."""
    if start_index >= len(sql) or sql[start_index] != "(":
        return None, start_index
    depth = 0
    in_quote = False
    i = start_index
    while i < len(sql):
        ch = sql[i]
        if in_quote:
            if ch == "'" and i + 1 < len(sql) and sql[i + 1] == "'":
                i += 2
                continue
            if ch == "'":
                in_quote = False
            i += 1
            continue
        if ch == "'":
            in_quote = True
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return sql[start_index + 1 : i], i + 1
        i += 1
    return None, start_index


def rewrite_duration_time_converts(sql):
    """
    Replace CONVERT(varchar(...), DATEADD(SECOND, <expr>, 0), 108) with <expr>
    so aggregates are returned as seconds instead of TIME (which wraps at 24h).
    """
    if not sql or not re.search(r"\bDATEADD\s*\(\s*SECOND\b", sql, re.IGNORECASE):
        return sql

    pattern = re.compile(r"\bCONVERT\s*\(", re.IGNORECASE)
    pieces = []
    cursor = 0
    for match in pattern.finditer(sql):
        convert_open = match.end() - 1  # index of '('
        convert_args, convert_end = _extract_balanced_call(sql, convert_open)
        if convert_args is None:
            continue

        # Expect: varchar(...), DATEADD(SECOND, <expr>, 0), 108
        dateadd_match = re.search(r"\bDATEADD\s*\(", convert_args, re.IGNORECASE)
        if not dateadd_match:
            continue
        # varchar style first arg and style 108 somewhere
        if not re.search(r"\bvarchar\b", convert_args, re.IGNORECASE):
            continue
        if not re.search(r",\s*108\s*$", convert_args.strip(), re.IGNORECASE):
            continue

        dateadd_open = match.start() + dateadd_match.end() - 1
        # dateadd_open is absolute? match.start() is CONVERT start; dateadd_match is in convert_args
        dateadd_open = (convert_open + 1) + dateadd_match.end() - 1
        dateadd_args, dateadd_end = _extract_balanced_call(sql, dateadd_open)
        if dateadd_args is None:
            continue
        if not re.match(r"^\s*SECOND\s*,", dateadd_args, re.IGNORECASE):
            continue

        # SECOND, <expr>, 0
        inner = re.sub(r"^\s*SECOND\s*,\s*", "", dateadd_args, count=1, flags=re.IGNORECASE)
        inner = re.sub(r",\s*0\s*$", "", inner, count=1).strip()
        if not inner:
            continue

        # Prefer casting rounded expressions to int seconds.
        replacement = inner
        alias_hint = ""
        # Preserve AS alias after the CONVERT(...) if present
        alias_match = re.match(r"\s+AS\s+([A-Za-z_][\w]*)", sql[convert_end:], re.IGNORECASE)
        if alias_match:
            alias_name = alias_match.group(1)
            convert_end = convert_end + alias_match.end()
            if not re.search(r"second", alias_name, re.IGNORECASE):
                alias_hint = f" AS {alias_name}_seconds"
            else:
                alias_hint = f" AS {alias_name}"
        else:
            alias_hint = " AS total_seconds"

        pieces.append(sql[cursor:match.start()])
        pieces.append(f"{replacement}{alias_hint}")
        cursor = convert_end

    pieces.append(sql[cursor:])
    return "".join(pieces)


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

    stripped = repair_select_query(sql)
    stripped = normalize_readonly_sql(strip_comments(stripped.strip().rstrip(";")))
    parts = split_sql_statements(stripped)
    if not parts:
        return stripped

    merged = []
    index = 0
    while index < len(parts):
        part = repair_select_query(parts[index])
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
        candidate = repair_select_query(part).strip()
        upper = candidate.lstrip().upper()
        if upper.startswith("SELECT") or upper.startswith("WITH") or upper.startswith("("):
            return candidate

    return repair_select_query(merged[0]).strip()


def validate_select_query(sql):
    if not sql or not sql.strip():
        return False, "Query is empty."

    stripped = ensure_single_readonly_sql(sql)
    stripped = repair_select_query(stripped)
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
    # Avoid false positives on words like SUBMITTAL containing "into" as letters.
    if re.search(r"\bSELECT\b[\s\S]*?\bINTO\b\s+[\#\[]?\w+", keyword_scan, re.IGNORECASE):
        return False, "SELECT INTO is not allowed."

    upper = stripped.lstrip().upper()
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


def format_duration_seconds(seconds):
    """
    Human duration that does not wrap at 24 hours.
    Examples: "3 hours 15 minutes", "1 day 3 hours", "2 weeks 23 hours and 10 minutes"
    Kept for rare spoken fallbacks; table display uses format_hhmmss_seconds.
    """
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return None
    if total < 0:
        total = 0

    weeks, rem = divmod(total, 7 * 24 * 3600)
    days, rem = divmod(rem, 24 * 3600)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    parts = []
    if weeks:
        parts.append(f"{weeks} week{'s' if weeks != 1 else ''}")
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if not parts:
        if secs:
            parts.append(f"{secs} second{'s' if secs != 1 else ''}")
        else:
            return "0 minutes"

    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return f"{', '.join(parts[:-1])}, and {parts[-1]}"


def format_hhmmss_seconds(seconds):
    """
    Clock-style duration that does not wrap at 24 hours.
    Examples: "08:15:00", "45:30:00", "160:05:12"
    """
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return None
    if total < 0:
        total = 0
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def parse_hhmmss_to_seconds(value):
    """Parse 'HH:MM:SS' / 'H:MM:SS' into seconds. Returns None if not a time string."""
    if value is None:
        return None
    text = str(value).strip()
    # Allow unbounded hours (month totals like 160:05:12).
    match = re.fullmatch(r"(\d+):([0-5]?\d):([0-5]?\d)", text)
    if not match:
        return None
    hours, minutes, seconds = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return hours * 3600 + minutes * 60 + seconds


def coerce_to_seconds(value):
    """
    Convert a cell value to seconds.
    Accepts HH:MM:SS strings or numeric seconds (e.g. 66704 from AVG/SUM).
    """
    if value is None or value == "":
        return None
    parsed = parse_hhmmss_to_seconds(value)
    if parsed is not None:
        return parsed
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    text = str(value).strip().replace(",", "")
    if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        try:
            return float(text)
        except (TypeError, ValueError):
            return None
    return None


def is_time_metric_bucket(bucket):
    return bucket in {"logged_hours", "avg_logged_hours", "break", "avg_break"}


# Attendance day status thresholds (logged_hours that day).
ABSENT_MAX_SECONDS = 5 * 3600  # < 5 hours → absent
PRESENT_MIN_SECONDS = 7 * 3600  # >= 7 hours → present
# halfday: ABSENT_MAX_SECONDS <= seconds < PRESENT_MIN_SECONDS


def attendance_day_status(seconds):
    """Return 'absent', 'halfday', or 'present' from logged seconds that day."""
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        return "absent"
    if total < ABSENT_MAX_SECONDS:
        return "absent"
    if total < PRESENT_MIN_SECONDS:
        return "halfday"
    return "present"


def _is_seconds_column(name):
    key = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    return key.endswith("seconds") or key.endswith("secs") or key in {
        "totalseconds",
        "avgseconds",
        "loggedseconds",
        "breakseconds",
        "durationseconds",
        "sumseconds",
        "averageseconds",
        "avgloggedseconds",
        "avgtotalseconds",
    }


def _is_duration_label_column(name):
    key = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    return any(
        token in key
        for token in (
            "loggedhour",
            "totalhour",
            "totalbreak",
            "avgbreak",
            "avglogged",
            "averagelogged",
            "averagehour",
            "avghour",
            "duration",
            "totaltime",
            "breaktime",
        )
    ) or key in {
        "loggedhours",
        "logged_hours",
        "totalhours",
        "avghours",
        "averagehours",
        "hours",
    }


def _norm_col(name):
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def extract_requested_metric_order(question):
    """
    Metric aliases in the order the user asked, e.g.
    "logged hours, avg logged hours and break" →
    ["logged_hours", "avg_logged_hours", "break"].
    """
    text = question or ""
    if not text.strip():
        return []

    specs = [
        ("avg_logged_hours", re.compile(r"avg(?:erage)?\s+logged\s*hours?", re.I)),
        ("avg_logged_hours", re.compile(r"avg(?:erage)?\s+hours?", re.I)),
        ("avg_break", re.compile(r"avg(?:erage)?\s+breaks?", re.I)),
        ("logged_hours", re.compile(r"logged\s*hours?", re.I)),
        ("logged_hours", re.compile(r"total\s+(?:logged\s*)?hours?", re.I)),
        ("break", re.compile(r"\bbreaks?\b", re.I)),
        ("present_days", re.compile(r"\bpresent(?:\s*days?)?\b", re.I)),
        ("halfday_days", re.compile(r"\bhalf[\s-]?days?\b", re.I)),
        ("absent_days", re.compile(r"\babsent(?:\s*days?)?\b", re.I)),
        ("aafs", re.compile(r"\baafs\b", re.I)),
        ("submittals", re.compile(r"\bsubmitt?als?\b|\bsubmits?\b", re.I)),
        ("interviews", re.compile(r"\binterviews?\b", re.I)),
        ("hires", re.compile(r"\bhires?\b|\bhired\b", re.I)),
        ("offers", re.compile(r"\boffers?\b", re.I)),
        ("starts", re.compile(r"\bstarts?\b", re.I)),
        ("rejects", re.compile(r"\brejects?\b|\brejected\b", re.I)),
    ]

    matches = []
    for alias, cre in specs:
        for match in cre.finditer(text):
            matches.append((match.start(), match.end(), alias))
    matches.sort(key=lambda item: (item[0], -(item[1] - item[0])))

    used = [False] * (len(text) + 1)
    ordered = []
    for start, end, alias in matches:
        if any(used[idx] for idx in range(start, end)):
            continue
        for idx in range(start, end):
            used[idx] = True
        if alias not in ordered:
            ordered.append(alias)
    return ordered


def _metric_bucket_for_column(col_name):
    """Map a result column to a requested-metric alias, when possible."""
    key = _norm_col(col_name)
    if key in {"month", "monthname", "mon"}:
        return "month"
    if key in {"sessiondate", "date", "workdate", "day"}:
        return "date"
    if "present" in key:
        return "present_days"
    if "halfday" in key or ("half" in key and "day" in key):
        return "halfday_days"
    if "absent" in key:
        return "absent_days"
    if "avg" in key and "break" in key:
        return "avg_break"
    if (
        ("avg" in key or "average" in key)
        and (
            "hour" in key
            or "logged" in key
            or key in {"avgseconds", "averageseconds"}
        )
    ) or key in {"avghours", "averagehours", "averageloggedhours"}:
        return "avg_logged_hours"
    if "break" in key or key in {"totalbreak", "breaktime", "breakseconds"}:
        return "break"
    if key == "aafs" or key.startswith("aafs"):
        return "aafs"
    if key in {
        "loggedhours",
        "totalseconds",
        "loggedseconds",
        "totalloggedseconds",
        "totalhours",
        "hours",
        "duration",
        "loggedduration",
    } or (
        "logged" in key
        and "avg" not in key
        and "break" not in key
    ) or (
        key.endswith("seconds")
        and "avg" not in key
        and "break" not in key
        and "month" not in key
        and "present" not in key
        and "absent" not in key
        and "half" not in key
    ):
        return "logged_hours"
    if "submittal" in key or key in {"submits", "submit"}:
        return "submittals"
    if "interview" in key:
        return "interviews"
    if key in {"hires", "hire", "hirecount"} or (
        "hire" in key and "interview" not in key
    ):
        return "hires"
    if "offer" in key or "placement" in key:
        return "offers"
    if key in {"rejects", "reject", "rejectcount"} or "reject" in key:
        return "rejects"
    if key in {"starts", "start", "startdate"} or (
        "start" in key and "login" not in key
    ):
        return "starts"
    return None


def _friendly_display_name(col_name, bucket=None, question=None):
    """
    Column label matching what the user asked — never a bare 'duration'.
    Break questions → total_break / avg_break; hours → logged_hours / avg_logged_hours.
    """
    q = question or ""
    wants_avg = bool(re.search(r"\bavg(?:erage)?\b", q, re.IGNORECASE))
    key = _norm_col(col_name)

    if bucket == "month" or key in {"month", "monthname"}:
        return "month"
    if bucket == "date" or key in {"sessiondate", "date"}:
        return "sessionDate" if key == "sessiondate" else col_name
    if bucket == "present_days" or "present" in key:
        return "present_days"
    if bucket == "halfday_days" or "halfday" in key or ("half" in key and "day" in key):
        return "halfday_days"
    if bucket == "absent_days" or "absent" in key:
        return "absent_days"
    if bucket == "avg_logged_hours" or (
        bucket == "logged_hours" and wants_avg and re.search(r"\bavg(?:erage)?\s+(?:logged\s*)?hours?\b", q, re.I)
    ):
        return "avg_logged_hours"
    if bucket == "avg_break" or (bucket == "break" and wants_avg):
        return "avg_break"
    if bucket == "break" or "break" in key:
        return "total_break" if not wants_avg else "avg_break"
    if bucket == "logged_hours":
        metrics = extract_requested_metric_order(q)
        # Ambiguous total_seconds / duration → name from the question.
        if (
            "break" in metrics or "avg_break" in metrics
        ) and "logged_hours" not in metrics and "avg_logged_hours" not in metrics:
            return "avg_break" if wants_avg or "avg_break" in metrics else "total_break"
        if "avg_logged_hours" in metrics and "logged_hours" not in metrics:
            return "avg_logged_hours"
        return "logged_hours"
    if key == "duration" or key.endswith("duration"):
        metrics = extract_requested_metric_order(q)
        if "break" in metrics or "avg_break" in metrics:
            return "avg_break" if wants_avg else "total_break"
        if "avg_logged_hours" in metrics:
            return "avg_logged_hours"
        return "logged_hours"
    if _is_seconds_column(col_name):
        base = re.sub(r"_?seconds?$", "", col_name, flags=re.IGNORECASE).strip("_")
        base_bucket = _metric_bucket_for_column(base) or _metric_bucket_for_column(col_name)
        if base_bucket:
            return _friendly_display_name(base or col_name, base_bucket, question)
        if "break" in key:
            return "avg_break" if wants_avg else "total_break"
        if "avg" in key:
            return "avg_logged_hours"
        return "logged_hours"
    return col_name


def order_result_columns(results, question=None):
    """
    Column 1 = month or day/date when present.
    Then metrics in the exact order asked in the question.
    Then identity / leftover columns.
    """
    if not results:
        return results

    keys = list(results[0].keys())
    metric_order = extract_requested_metric_order(question)
    month_keys = [
        k
        for k in keys
        if _metric_bucket_for_column(k) == "month"
        or _norm_col(k) in {"month", "monthname", "mon"}
    ]
    date_keys = [
        k
        for k in keys
        if _metric_bucket_for_column(k) == "date"
        or _norm_col(k) in {"sessiondate", "date", "workdate", "day"}
    ]

    preferred = []
    seen = set()

    def _add(key):
        if key in keys and key not in seen:
            preferred.append(key)
            seen.add(key)

    # 1) Month or day/date always first.
    for key in month_keys:
        _add(key)
    for key in date_keys:
        _add(key)

    # 2) Metrics in the order the user asked (hires → interviews → submittals, etc.).
    for metric in metric_order:
        for key in keys:
            if _metric_bucket_for_column(key) == metric:
                _add(key)

    # 3) Common leftovers (only if not already placed by ask-order).
    for name in (
        "userName",
        "username",
        "employeeid",
        "logged_hours",
        "avg_logged_hours",
        "total_break",
        "avg_break",
        "present_days",
        "halfday_days",
        "absent_days",
        "submittals",
        "interviews",
        "hires",
        "offers",
        "starts",
        "rejects",
        "session_count",
    ):
        for key in keys:
            if key.lower() == name.lower():
                _add(key)

    for key in keys:
        if _norm_col(key) == "duration":
            continue
        _add(key)

    lead = month_keys[0] if month_keys else (date_keys[0] if date_keys else None)
    if lead and preferred and preferred[0] != lead:
        preferred = [lead] + [k for k in preferred if k != lead]

    ordered_rows = []
    for row in results:
        ordered_rows.append({key: row.get(key) for key in preferred if key in row})
    return ordered_rows


def result_column_order(results, question=None):
    """Ordered column names for the UI (month/date first, then ask order)."""
    if not results:
        return []
    ordered = order_result_columns(results, question)
    return list(ordered[0].keys()) if ordered else []


def enrich_duration_results(results, question=None):
    """
    Format time metrics as hours:minutes:seconds and drop redundant clones.

    Never invent a bare `duration` column — use logged_hours / total_break /
    avg_logged_hours / etc. matching the question.
    Month/date stay leftmost; metrics follow ask order.
    Numeric AVG/SUM seconds (e.g. 66704) are converted to HH:MM:SS even when
    the column is not named *_seconds.
    """
    if not results:
        return results

    redundant_norms = {
        "loggedduration",
        "loggedhoursduration",
        "loggedhourduration",
        "totalduration",
        "avgduration",
        "averageduration",
        "hourduration",
        "hoursduration",
        "durationhours",
        "loggedhoursseconds",
        "duration",
    }

    is_monthy = bool(month_breakdown_guidance(question)) or any(
        _metric_bucket_for_column(key) == "month" or _norm_col(key) == "month"
        for key in results[0].keys()
    )
    seconds_cols = [key for key in results[0].keys() if _is_seconds_column(key)]
    multi_metric = (
        is_monthy
        or len(seconds_cols) > 1
        or len(extract_requested_metric_order(question)) > 1
    )

    def _format_time_cell(key, value, question):
        bucket = _metric_bucket_for_column(key)
        display_name = _friendly_display_name(key, bucket, question)
        seconds = None
        if _is_seconds_column(key) or is_time_metric_bucket(bucket) or _is_duration_label_column(key):
            seconds = coerce_to_seconds(value)
        if seconds is None:
            return None, None, None
        readable = format_hhmmss_seconds(seconds)
        if not readable:
            return None, None, None
        return display_name, readable, bucket or _norm_col(key)

    enriched = []
    for row in results:
        cleaned = {}
        filled_buckets = set()

        # Prefer seconds / numeric time metrics first.
        for key, value in row.items():
            if value is None:
                continue
            if not (
                _is_seconds_column(key)
                or is_time_metric_bucket(_metric_bucket_for_column(key))
                or _is_duration_label_column(key)
            ):
                continue
            display_name, readable, bucket = _format_time_cell(key, value, question)
            if not readable:
                continue
            cleaned[display_name] = readable
            if bucket:
                filled_buckets.add(bucket)

        for key, value in row.items():
            key_n = _norm_col(key)
            if _is_seconds_column(key) or is_time_metric_bucket(
                _metric_bucket_for_column(key)
            ) or _is_duration_label_column(key):
                # Already handled (or unparsable time metric — skip raw seconds dump).
                if key_n not in redundant_norms and not key_n.endswith("duration"):
                    if _format_time_cell(key, value, question)[1]:
                        continue
                else:
                    continue
            if key_n in redundant_norms or key_n.endswith("duration"):
                display_name, readable, bucket = _format_time_cell(key, value, question)
                if readable and display_name not in cleaned:
                    cleaned[display_name] = readable
                    if bucket:
                        filled_buckets.add(bucket)
                continue
            bucket = _metric_bucket_for_column(key)
            if bucket and bucket in filled_buckets and bucket not in {"month", "date"}:
                continue
            if bucket == "month" or key_n in {"month", "monthname", "mon"}:
                cleaned["month"] = value
                filled_buckets.add("month")
                continue
            display_name = _friendly_display_name(key, bucket, question)
            cleaned[display_name] = value
            if bucket:
                filled_buckets.add(bucket)

        if not multi_metric and not any(
            is_time_metric_bucket(_metric_bucket_for_column(k)) for k in cleaned
        ):
            for key, value in row.items():
                display_name, readable, _bucket = _format_time_cell(key, value, question)
                if readable:
                    cleaned[display_name] = readable
                    break

        enriched.append(cleaned)

    enriched = enrich_month_name_results(enriched)
    return order_result_columns(enriched, question)


def build_duration_answer_context(results, question=None):
    """
    Force answers to include HH:MM:SS totals from seconds / time fields.
    Works on raw seconds rows or already-enriched HH:MM:SS rows.
    """
    if not results:
        return ""

    first = results[0]
    time_keys = [
        key
        for key in first.keys()
        if is_time_metric_bucket(_metric_bucket_for_column(key))
        or _is_seconds_column(key)
        or _is_duration_label_column(key)
    ]

    def _fmt(key, value):
        seconds = coerce_to_seconds(value)
        if seconds is None:
            # Already formatted HH:MM:SS string
            if parse_hhmmss_to_seconds(value) is not None or (
                isinstance(value, str) and ":" in value
            ):
                return str(value)
            return None
        return format_hhmmss_seconds(seconds)

    # Month-by-month: list each month's value so the model does not invent numbers.
    has_month = any(
        _metric_bucket_for_column(k) == "month" or _norm_col(k) == "month"
        for k in first.keys()
    )
    if has_month and len(results) > 1 and time_keys:
        parts = []
        for row in results:
            month = row.get("month") or row.get("Month") or row.get("month_name")
            for key in time_keys:
                display = _fmt(key, row.get(key))
                if display is None:
                    continue
                label = _friendly_display_name(
                    key, _metric_bucket_for_column(key), question
                )
                if month:
                    parts.append(f"{month} {label}={display}")
                else:
                    parts.append(f"{label}={display}")
                break
        if parts:
            return (
                "REQUIRED: Your answer MUST use these exact hours:minutes:seconds "
                "values (do not convert or invent other numbers): "
                + "; ".join(parts)
                + "."
            )

    # Single-row aggregate with seconds or duration.
    if len(results) == 1:
        parts = []
        for key, value in first.items():
            if value is None or value == "":
                continue
            key_l = str(key).lower()
            if key_l in {"month", "username", "surname", "name", "employeeid", "sessiondate"}:
                continue
            if not (
                _is_seconds_column(key)
                or is_time_metric_bucket(_metric_bucket_for_column(key))
                or _is_duration_label_column(key)
                or "duration" in key_l
            ):
                continue
            display = _fmt(key, value)
            if not display:
                continue
            label = _friendly_display_name(
                key, _metric_bucket_for_column(key), question
            )
            parts.append(f"{label}={display}")
        if parts:
            return (
                "REQUIRED: Your answer MUST include these logged-time value(s) verbatim "
                "in hours:minutes:seconds: "
                + "; ".join(parts)
                + "."
            )
        return ""

    # Multi-row without month labels: sum logged-hours seconds into one overall total.
    logged_keys = [
        key
        for key in first.keys()
        if _metric_bucket_for_column(key) in {"logged_hours", "avg_logged_hours"}
        or _is_seconds_column(key)
    ]
    if not logged_keys:
        return (
            "REQUIRED: Mention the key time totals from the data using "
            "hours:minutes:seconds (not raw seconds)."
            if time_keys
            else ""
        )

    total_seconds = 0
    parsed_any = False
    # Only sum plain logged_hours totals — averaging averages is misleading.
    sum_keys = [
        key
        for key in logged_keys
        if _metric_bucket_for_column(key) == "logged_hours" or _is_seconds_column(key)
    ]
    if not sum_keys:
        # Avg-only multi-row: quote each formatted value instead of summing.
        parts = []
        for row in results[:12]:
            for key in logged_keys:
                display = _fmt(key, row.get(key))
                if display:
                    label = _friendly_display_name(
                        key, _metric_bucket_for_column(key), question
                    )
                    parts.append(f"{label}={display}")
                    break
        if parts:
            return (
                "REQUIRED: Your answer MUST use these exact hours:minutes:seconds "
                "values: " + "; ".join(parts) + "."
            )
        return ""

    for row in results:
        for key in sum_keys:
            value = row.get(key)
            if value is None or value == "":
                continue
            seconds = coerce_to_seconds(value)
            if seconds is not None:
                total_seconds += seconds
                parsed_any = True
                break
    if parsed_any:
        total_label = format_hhmmss_seconds(total_seconds)
        if total_label:
            return (
                "REQUIRED: Your answer MUST include the overall total logged time "
                f"across all months/rows as: {total_label}."
            )
    return (
        "REQUIRED: Mention the logged hours totals from the data "
        "(use hours:minutes:seconds, not raw seconds)."
    )


_MONTH_NAMES = (
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def enrich_month_name_results(results):
    """
    Show one month-name column only; drop redundant month_num / month_number.
    Always place `month` as the first key so the UI keeps it leftmost.
    """
    if not results:
        return results

    display_keys = {"month", "monthname", "mon"}
    sort_only_keys = {"monthnum", "monthnumber", "monthno", "monthofyear"}

    def _as_month_name(value):
        if isinstance(value, str) and value.strip().lower() in {
            name.lower() for name in _MONTH_NAMES if name
        }:
            return value.strip().title()
        try:
            month_num = int(float(value))
        except (TypeError, ValueError):
            return None
        if 1 <= month_num <= 12:
            return _MONTH_NAMES[month_num]
        return None

    enriched = []
    for row in results:
        rest = {}
        pending_name = None
        sort_only_name = None
        for key, value in row.items():
            key_l = re.sub(r"[^a-z0-9]+", "", (key or "").lower())

            if key_l in sort_only_keys:
                name = _as_month_name(value)
                if name:
                    sort_only_name = name
                continue

            if key_l in display_keys:
                name = _as_month_name(value)
                if name:
                    pending_name = name
                    continue
                pending_name = str(value).strip() if value is not None else pending_name
                continue

            if key_l == "monthname" or key == "month_name":
                name = _as_month_name(value) or (str(value).strip() if value else None)
                if name:
                    pending_name = name
                continue

            rest[key] = value

        month_label = pending_name or sort_only_name
        # Month always first in the row dict.
        new_row = {}
        if month_label:
            new_row["month"] = month_label
        for key, value in rest.items():
            if key in {"month", "month_name", "monthName"}:
                continue
            new_row[key] = value

        enriched.append(new_row)
    return enriched


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

    keys = list({key for row in results for key in row.keys()})
    lower_map = {key.lower(): key for key in keys}

    username_key = next(
        (lower_map[name] for name in ("username", "surname", "name") if name in lower_map),
        None,
    )
    first_key = next(
        (
            lower_map[name]
            for name in ("candidatefirstname", "userfirstname", "firstname")
            if name in lower_map
        ),
        None,
    )
    last_key = next(
        (
            lower_map[name]
            for name in ("candidatelastname", "userlastname", "lastname")
            if name in lower_map
        ),
        None,
    )
    employee_id_key = next(
        (
            lower_map[name]
            for name in ("employeeid", "candidateid", "customerid", "id")
            if name in lower_map
        ),
        None,
    )

    if not username_key and not (first_key or last_key):
        return []

    seen = set()
    candidates = []

    for row in results:
        if username_key:
            username = row.get(username_key)
        else:
            first = str(row.get(first_key) or "").strip() if first_key else ""
            last = str(row.get(last_key) or "").strip() if last_key else ""
            username = f"{first} {last}".strip()

        if not username:
            continue

        employee_id = row.get(employee_id_key) if employee_id_key else None
        dedupe_key = str(employee_id) if employee_id is not None else str(username).lower()
        if dedupe_key in seen:
            continue

        seen.add(dedupe_key)
        candidates.append({"employee_id": employee_id, "username": str(username)})

    return sorted(candidates, key=lambda c: (c["username"].lower(), str(c["employee_id"])))


def sql_literal(value):
    return str(value).replace("'", "''")


# Verbs / question words that end a person-name span.
_NAME_TAIL_VERBS = (
    r"make|made|makes|making|get|got|gets|getting|give|gave|given|"
    r"log|logs|logged|logging|work|works|worked|working|"
    r"have|has|had|submit|submits|submitted|hire|hired|recruit|recruited|"
    r"show|list|tell|do|does|did|is|are|was|were|can|could|would|should|"
    r"spend|spent|take|took|use|used"
)
_NAME_MONTHS = (
    r"january|february|march|april|may|june|july|august|september|october|"
    r"november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec|"
    r"today|yesterday|tomorrow|this|last|next|week|month|year"
)
_NAME_TOKEN = r"[A-Za-z][A-Za-z'.-]*"
_NAME_SPAN = rf"({_NAME_TOKEN}(?:\s+{_NAME_TOKEN}){{0,2}})"


def extract_name_hints(question):
    """
    Pull likely first/last name tokens from a question.

    Priority:
    1) Leading name: "Akshay Soni, how many hours..."
    2) Role marker: "candidate/employee/recruiter Akshay Soni"
    3) "did <Name> log/make..." / "how many ... did <Name> log..."
    4) "for <Name>" / "for candidate <Name>"
    Never treat verbs like log/made or months like July as names.
    """
    text = question or ""
    text = re.sub(r"\b20\d{2}\b", " ", text)
    text = re.sub(r"\b\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?\b", " ", text)
    text = re.sub(r"[`\"“”]", " ", text)
    # what's / who's → remove so they never become names
    text = re.sub(r"\b(what|who|where|how|that|there|here)['’]s\b", r"\1", text, flags=re.I)
    # "Samantha's performance" → keep "Samantha", drop possessive
    text = re.sub(r"['’]s\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"['’]", "", text)

    def clean_name_parts(raw):
        parts = []
        seen = set()
        for token in re.findall(_NAME_TOKEN, raw or ""):
            cleaned = token.strip(".'-")
            key = cleaned.lower()
            if len(cleaned) < 2 or key in NAME_STOPWORDS or key in seen:
                continue
            if key.endswith("ly") and key not in {
                "lily", "kelly", "holly", "emily", "hailey", "bailey",
            }:
                continue
            seen.add(key)
            parts.append(cleaned)
            if len(parts) >= 3:
                break
        return parts

    patterned = [
        # "Give me Disha Samantha's July performance..."
        rf"^\s*(?:give\s+me|show\s+me|get\s+me|tell\s+me)\s+{_NAME_SPAN}\b",
        # "Akshay Soni, how many hours..." / "Akshay Soni: show logged hours"
        rf"^\s*{_NAME_SPAN}\s*[,:\-]\s*"
        rf"(?:how|what|when|who|where|show|list|give|tell|did|does|can|could|"
        rf"please|find|get)\b",
        # "Akshay Soni how many hours did he log"
        rf"^\s*{_NAME_SPAN}\s+"
        rf"(?:how|what|when|who|where)\b",
        # "Disha how many submittals" / "Disha submittals july 2026"
        rf"^\s*{_NAME_SPAN}\s+"
        rf"(?:how\s+many\s+)?"
        rf"(?:submits?|submittals?|submissions?|hires?|interviews?|rejects?|"
        rf"clients?|placements?|offers?|hours?|breaks?|performance)\b",
        # "Disha Samantha July month performance" / "Disha's July performance"
        rf"\b{_NAME_SPAN}\s+"
        rf"(?:{_NAME_MONTHS}|performance|month)\b",
        # role markers — name is the next word(s)
        rf"\b(?:candidate|employee|recruiter|user)(?:'s)?\s+{_NAME_SPAN}\b",
        # "for candidate Akshay Soni" / "for employee John"
        rf"\bfor\s+(?:candidate|employee|recruiter|user)\s+{_NAME_SPAN}\b",
        # "how many hours/submits did Akshay Soni log/make"
        rf"\bhow\s+many\s+{_NAME_TOKEN}\s+did\s+{_NAME_SPAN}\s+"
        rf"(?:{_NAME_TAIL_VERBS})\b",
        # "did Akshay Soni log" / "has Priya made"
        rf"\b(?:did|has|have)\s+{_NAME_SPAN}\s+(?:{_NAME_TAIL_VERBS})\b",
        # "submits Jordan made" / "hours Akshay logged"
        rf"\b(?:submits?|submittals?|submissions?|hires?|interviews?|rejects?|"
        rf"clients?|placements?|offers?|hours?)\s+{_NAME_SPAN}\s+"
        rf"(?:{_NAME_TAIL_VERBS})\b",
        # "for Akshay Soni" but not "for July" / "for this month"
        rf"\bfor\s+(?!{_NAME_MONTHS}\b){_NAME_SPAN}"
        rf"(?=\s+(?:how|what|when|who|did|does|do|has|have|had|was|is|are|"
        rf"{_NAME_TAIL_VERBS}|the|his|her|their|a|an|on|in|at|to|of|"
        rf"this|last|next|performance|,|\?|$))",
        rf"\b(?:about|regarding)\s+{_NAME_SPAN}\b",
        # "Akshay's logged hours" / "Jordan's submittal" / "Disha's performance"
        rf"\b{_NAME_SPAN}\s+"
        rf"(?:interview|submittal|submission|hire|pay\s*rate|logged|log|"
        rf"break|hours|submits?|performance|july|june|month)\b",
    ]

    for pattern in patterned:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        parts = clean_name_parts(match.group(1))
        if parts:
            return parts

    # Fallback: first 1–2 consecutive non-stopword tokens (usually the leading name).
    tokens = re.findall(_NAME_TOKEN, text)
    hints = []
    seen = set()
    for token in tokens:
        cleaned = token.strip(".'-")
        key = cleaned.lower()
        if (
            len(cleaned) < 2
            or key in NAME_STOPWORDS
            or key in seen
            or (
                key.endswith("ly")
                and key not in {"lily", "kelly", "holly", "emily", "hailey", "bailey"}
            )
        ):
            if hints:
                break
            continue
        seen.add(key)
        hints.append(cleaned)
        if len(hints) >= 2:
            break

    return hints


def looks_like_person_question(question):
    """
    Only run person lookup when the question actually seems to name someone.
    Avoid treating words like "monthly" / "made" / "log" / "now" as people.
    """
    text = question or ""
    if not text.strip():
        return False

    # Discourse openers are never a person cue by themselves.
    if re.match(
        r"^\s*(now|okay|ok|alright|sure|please|then|also|and|plus)\b",
        text,
        re.IGNORECASE,
    ):
        # Still allow "Now Akshay's hours..." if a real name follows.
        remainder = re.sub(
            r"^\s*(now|okay|ok|alright|sure|please|then|also|and|plus)\b[\s,:-]*",
            "",
            text,
            flags=re.IGNORECASE,
        )
        if not extract_name_hints(remainder):
            return False
        text = remainder

    hints = extract_name_hints(question)
    if not hints:
        return False

    # Any clear name cue — leading name, role marker, or 2-token name.
    if re.search(
        r"^\s*[A-Za-z][A-Za-z'.-]*(?:\s+[A-Za-z][A-Za-z'.-]*){0,2}\s*[,:\-]",
        text,
    ):
        return True

    if re.search(
        r"\b(?:candidate|employee|recruiter|user)(?:'s)?\s+[A-Za-z]",
        text,
        re.IGNORECASE,
    ):
        return True

    if is_recruiter_question(question):
        # Only if hints survived stopwords and look like a real name cue.
        if len(hints) >= 2:
            return True
        if len(hints) == 1 and hints[0][0].isupper():
            # Avoid "Show"/"Now" style leftovers — require a person-like pattern.
            if re.search(
                rf"\b(?:did|for|about|regarding|candidate|employee|recruiter)\s+"
                rf"{re.escape(hints[0])}\b",
                question or "",
                re.IGNORECASE,
            ) or re.search(
                rf"^\s*{re.escape(hints[0])}\b",
                text,
            ):
                return True
        if len(hints) >= 1 and re.search(
            rf"\b(?:give\s+me|show\s+me|get\s+me|tell\s+me)\s+{re.escape(hints[0])}\b",
            question or "",
            re.IGNORECASE,
        ):
            return True

    if re.search(
        r"\bfor\s+(?:candidate\s+|employee\s+|recruiter\s+|user\s+)?[A-Za-z]",
        text,
        re.IGNORECASE,
    ) and not re.search(
        rf"\bfor\s+(?:{_NAME_MONTHS})\b",
        text,
        re.IGNORECASE,
    ):
        return True

    if len(hints) >= 2:
        return True

    if len(hints) == 1:
        hint = hints[0]
        # Single token: accept capitalized names, or lowercase if used with did/for.
        if re.search(rf"\b{re.escape(hint)}\b", text) and hint[0].isupper():
            return True
        if re.search(
            rf"\b(?:did|for|candidate|employee|recruiter)\s+{re.escape(hint)}\b",
            text,
            re.IGNORECASE,
        ):
            return True

    return False


def levenshtein_distance(left, right):
    a = (left or "").lower()
    b = (right or "").lower()
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i]
        for j, cb in enumerate(b, start=1):
            ins = curr[j - 1] + 1
            delete = prev[j] + 1
            sub = prev[j - 1] + (ca != cb)
            curr.append(min(ins, delete, sub))
        prev = curr
    return prev[-1]


def score_name_match(hints, full_name):
    """Lower is better. None means not close enough (more than ~2 letter mistakes)."""
    name = " ".join(str(full_name or "").split())
    if not name or not hints:
        return None

    name_l = name.lower()
    tokens = [t for t in re.split(r"[^a-z0-9]+", name_l) if t]
    total = 0

    for hint in hints:
        h = hint.lower()
        # Exact full name or exact token (correct spelling) — best score.
        if name_l == h or h in tokens:
            total += 0
            continue
        if name_l.startswith(h + " ") or name_l.endswith(" " + h):
            total += 0
            continue
        if h in name_l:
            total += 1
            continue
        distances = [levenshtein_distance(h, token) for token in tokens] or [99]
        best = min(distances)
        # Allow 1-2 character typos on a token of similar length.
        if best <= 2 and any(abs(len(h) - len(token)) <= 2 for token in tokens):
            total += best + 1
            continue
        return None

    return total


def finalize_name_matches(matches, hints, max_confirm=3):
    """
    Decide whether to auto-resolve, ask Did you mean (max 3), or report not found.
    Returns (username, id, confirm_matches, status)
    status: resolved | confirm | not_found | ambiguous
    """
    if not matches:
        return None, None, [], "not_found"

    scored = []
    for match in matches:
        score = score_name_match(hints, match.get("username"))
        if score is None:
            continue
        scored.append((score, match))

    if not scored:
        return None, None, [], "not_found"

    scored.sort(key=lambda item: (item[0], str(item[1].get("username") or "").lower()))
    best_score = scored[0][0]
    best_matches = [match for score, match in scored if score == best_score]

    # Deduplicate same display name (same person, different userid copies).
    unique_names = {}
    for match in best_matches:
        key = str(match.get("username") or "").strip().lower()
        unique_names.setdefault(key, match)
    best_unique = list(unique_names.values())

    # Single unique display name with exact/near-exact score → resolve.
    if len(best_unique) == 1 and best_score <= 1:
        match = best_unique[0]
        employee_id = match.get("employee_id")
        return (
            match.get("username"),
            str(employee_id) if employee_id is not None else None,
            [],
            "resolved",
        )

    # Unique clear winner (exact/near-exact, or clearly better than the rest).
    if len(best_matches) == 1:
        if len(scored) == 1 or scored[1][0] > best_score:
            match = best_matches[0]
            employee_id = match.get("employee_id")
            return (
                match.get("username"),
                str(employee_id) if employee_id is not None else None,
                [],
                "resolved",
            )

    if 2 <= len(best_unique) <= max_confirm and best_score <= 2:
        return None, None, best_unique[:max_confirm], "confirm"

    if len(best_unique) > max_confirm:
        return None, None, [], "ambiguous"

    # Fall back to the single closest name if it's within 2 edits.
    if best_score <= 2 and len(best_unique) == 1:
        match = best_unique[0]
        employee_id = match.get("employee_id")
        return (
            match.get("username"),
            str(employee_id) if employee_id is not None else None,
            [],
            "resolved",
        )

    return None, None, [], "not_found"


def person_not_found_message(question):
    hint_text = " ".join(extract_name_hints(question)) or "that person"
    return (
        f'I couldn\'t find anything about "{hint_text}". '
        "Could you double-check the spelling, or try the full first and last name?"
    )


def person_ambiguous_message(question):
    hint_text = " ".join(extract_name_hints(question)) or "that name"
    return (
        f'I found several people that look similar to "{hint_text}". '
        "Please use the full first and last name so I can narrow it down."
    )


def find_matching_employees(table_name, name_hints, limit=20):
    """Match employees by first name, last name, or both against userName."""
    if not table_name or not name_hints:
        return []

    where_parts = [
        f"userName LIKE '%{sql_literal(hint)}%'"
        for hint in name_hints
    ]
    where_sql = " AND ".join(where_parts)
    if len(name_hints) >= 2:
        ordered = "%" + "%".join(sql_literal(hint) for hint in name_hints) + "%"
        where_sql = f"(({where_sql}) OR userName LIKE '{ordered}')"

    sql = f"""
        SELECT DISTINCT TOP ({int(limit)})
            userName AS username,
            employeeid AS employee_id
        FROM [{table_name}]
        WHERE {where_sql}
        ORDER BY userName, employeeid
    """

    try:
        rows = execute_sql(sql)
    except Exception:
        rows = []

    if not rows:
        # Fuzzy fallback: names that sound like / start like the hint.
        fuzzy_parts = []
        for hint in name_hints:
            safe = sql_literal(hint)
            prefix = sql_literal(hint[: max(2, min(3, len(hint)))])
            fuzzy_parts.append(
                "("
                f"userName LIKE '%{prefix}%' "
                f"OR DIFFERENCE(userName, '{safe}') >= 3 "
                f"OR SOUNDEX(userName) = SOUNDEX('{safe}')"
                ")"
            )
        fuzzy_sql = f"""
            SELECT DISTINCT TOP (80)
                userName AS username,
                employeeid AS employee_id
            FROM [{table_name}]
            WHERE {" AND ".join(fuzzy_parts)}
            ORDER BY userName, employeeid
        """
        try:
            rows = execute_sql(fuzzy_sql)
        except Exception:
            return []

    seen = set()
    matches = []
    for row in rows:
        username = str(row.get("username") or "").strip()
        if not username:
            continue
        employee_id = row.get("employee_id")
        dedupe_key = f"{username.lower()}::{employee_id}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        matches.append({"username": username, "employee_id": employee_id})
    return matches


def resolve_employee_from_question(question, table_name, confirmed_username, confirmed_employee_id):
    """
    Resolve a Prohance employee by first/last name.
    Returns (username, id, confirm_matches, status).
    """
    if confirmed_employee_id or confirmed_username:
        return confirmed_username, confirmed_employee_id, [], "resolved"

    hints = extract_name_hints(question)
    if not hints:
        return None, None, [], "none"

    matches = find_matching_employees(table_name, hints)
    if not matches and len(hints) > 1:
        matches = find_matching_employees(table_name, [hints[0]])

    return finalize_name_matches(matches, hints)


def _candidate_name_where(name_hints):
    clauses = []
    for hint in name_hints:
        safe = sql_literal(hint)
        clauses.append(
            "("
            f"CANDIDATEFIRSTNAME LIKE '%{safe}%' "
            f"OR CANDIDATELASTNAME LIKE '%{safe}%' "
            f"OR (CANDIDATEFIRSTNAME + ' ' + CANDIDATELASTNAME) LIKE '%{safe}%'"
            ")"
        )
    return " AND ".join(clauses)


def find_matching_candidates(name_hints, limit=20):
    """Match recruiting candidates across DataVista CR_* tables."""
    if not name_hints:
        return []

    db_name = get_datavista_database_name()
    where_sql = _candidate_name_where(name_hints)
    selects = []
    for table in DATAVISTA_TABLES:
        selects.append(
            f"""
            SELECT DISTINCT
                LTRIM(RTRIM(COALESCE(CANDIDATEFIRSTNAME, ''))) + ' ' +
                LTRIM(RTRIM(COALESCE(CANDIDATELASTNAME, ''))) AS username,
                CANDIDATEID AS employee_id
            FROM [{db_name}].[dbo].[{table}]
            WHERE {where_sql}
            """
        )

    sql = f"""
        SELECT DISTINCT TOP ({int(limit)}) username, employee_id
        FROM (
            {" UNION ALL ".join(selects)}
        ) AS people
        WHERE LTRIM(RTRIM(username)) <> ''
        ORDER BY username, employee_id
    """

    try:
        rows = execute_sql(sql)
    except Exception:
        rows = []

    if not rows:
        fuzzy_selects = []
        for table in DATAVISTA_TABLES:
            fuzzy_parts = []
            for hint in name_hints:
                safe = sql_literal(hint)
                prefix = sql_literal(hint[: max(2, min(3, len(hint)))])
                fuzzy_parts.append(
                    "("
                    f"CANDIDATEFIRSTNAME LIKE '%{prefix}%' "
                    f"OR CANDIDATELASTNAME LIKE '%{prefix}%' "
                    f"OR DIFFERENCE(CAST(CANDIDATEFIRSTNAME AS NVARCHAR(400)), '{safe}') >= 3 "
                    f"OR DIFFERENCE(CAST(CANDIDATELASTNAME AS NVARCHAR(400)), '{safe}') >= 3 "
                    f"OR SOUNDEX(CAST(CANDIDATEFIRSTNAME AS NVARCHAR(400))) = SOUNDEX('{safe}') "
                    f"OR SOUNDEX(CAST(CANDIDATELASTNAME AS NVARCHAR(400))) = SOUNDEX('{safe}')"
                    ")"
                )
            fuzzy_selects.append(
                f"""
                SELECT DISTINCT
                    LTRIM(RTRIM(COALESCE(CANDIDATEFIRSTNAME, ''))) + ' ' +
                    LTRIM(RTRIM(COALESCE(CANDIDATELASTNAME, ''))) AS username,
                    CANDIDATEID AS employee_id
                FROM [{db_name}].[dbo].[{table}]
                WHERE {" AND ".join(fuzzy_parts)}
                """
            )
        fuzzy_sql = f"""
            SELECT DISTINCT TOP (80) username, employee_id
            FROM (
                {" UNION ALL ".join(fuzzy_selects)}
            ) AS people
            WHERE LTRIM(RTRIM(username)) <> ''
            ORDER BY username, employee_id
        """
        try:
            rows = execute_sql(fuzzy_sql)
        except Exception:
            return []

    seen = set()
    matches = []
    for row in rows:
        username = str(row.get("username") or "").strip()
        if not username:
            continue
        employee_id = row.get("employee_id")
        dedupe_key = f"{username.lower()}::{employee_id}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        matches.append({"username": username, "employee_id": employee_id})
    return matches


def _recruiter_name_where(name_hints, mode="contains"):
    """
    mode:
      exact — first/last/full equality and 'First %' prefix on full name
      contains — LIKE %hint% (broader)
    """
    clauses = []
    for hint in name_hints:
        safe = sql_literal(hint)
        if mode == "exact":
            clauses.append(
                "("
                f"LTRIM(RTRIM(PRIMARYRECRUITERNAME)) = '{safe}' "
                f"OR LTRIM(RTRIM(USERFIRSTNAME)) = '{safe}' "
                f"OR LTRIM(RTRIM(USERLASTNAME)) = '{safe}' "
                f"OR LTRIM(RTRIM(USERFIRSTNAME)) + ' ' + LTRIM(RTRIM(USERLASTNAME)) = '{safe}' "
                f"OR PRIMARYRECRUITERNAME LIKE '{safe} %' "
                f"OR USERFIRSTNAME LIKE '{safe}' "
                f"OR (USERFIRSTNAME + ' ' + USERLASTNAME) LIKE '{safe} %'"
                ")"
            )
        else:
            clauses.append(
                "("
                f"PRIMARYRECRUITERNAME LIKE '%{safe}%' "
                f"OR USERFIRSTNAME LIKE '%{safe}%' "
                f"OR USERLASTNAME LIKE '%{safe}%' "
                f"OR (USERFIRSTNAME + ' ' + USERLASTNAME) LIKE '%{safe}%'"
                ")"
            )
    return " AND ".join(clauses)


def _recruiter_selects(db_name, where_sql):
    selects = []
    for table in DATAVISTA_TABLES:
        selects.append(
            f"""
            SELECT DISTINCT
                COALESCE(
                    NULLIF(LTRIM(RTRIM(PRIMARYRECRUITERNAME)), ''),
                    LTRIM(RTRIM(COALESCE(USERFIRSTNAME, ''))) + ' ' +
                    LTRIM(RTRIM(COALESCE(USERLASTNAME, '')))
                ) AS username,
                userid AS employee_id
            FROM [{db_name}].[dbo].[{table}]
            WHERE {where_sql}
            """
        )
    return selects


def find_matching_recruiters(name_hints, limit=20):
    """Match DataVista users/recruiters across CR_* tables. Prefer exact names."""
    if not name_hints:
        return []

    db_name = get_datavista_database_name()

    def run_where(where_sql, top=None):
        top_n = int(top or limit)
        sql = f"""
            SELECT DISTINCT TOP ({top_n}) username, employee_id
            FROM (
                {" UNION ALL ".join(_recruiter_selects(db_name, where_sql))}
            ) AS people
            WHERE LTRIM(RTRIM(username)) <> ''
            ORDER BY username, employee_id
        """
        try:
            return execute_sql(sql)
        except Exception:
            return []

    # 1) Exact / prefix match first (correct spelling should hit here).
    rows = run_where(_recruiter_name_where(name_hints, mode="exact"))

    # 2) Contains match.
    if not rows:
        rows = run_where(_recruiter_name_where(name_hints, mode="contains"))

    # 3) If full name failed, try first token only (exact then contains).
    if not rows and len(name_hints) > 1:
        rows = run_where(_recruiter_name_where([name_hints[0]], mode="exact"))
        if not rows:
            rows = run_where(_recruiter_name_where([name_hints[0]], mode="contains"))

    # 4) Fuzzy fallback last.
    if not rows:
        fuzzy_parts = []
        for hint in name_hints:
            safe = sql_literal(hint)
            prefix = sql_literal(hint[: max(2, min(3, len(hint)))])
            fuzzy_parts.append(
                "("
                f"PRIMARYRECRUITERNAME LIKE '{prefix}%' "
                f"OR USERFIRSTNAME LIKE '{prefix}%' "
                f"OR USERLASTNAME LIKE '{prefix}%' "
                f"OR DIFFERENCE(CAST(PRIMARYRECRUITERNAME AS NVARCHAR(400)), '{safe}') >= 4 "
                f"OR DIFFERENCE(CAST(USERFIRSTNAME AS NVARCHAR(400)), '{safe}') >= 4 "
                f"OR DIFFERENCE(CAST(USERLASTNAME AS NVARCHAR(400)), '{safe}') >= 4"
                ")"
            )
        rows = run_where(" AND ".join(fuzzy_parts), top=80)

    seen = set()
    matches = []
    for row in rows:
        username = str(row.get("username") or "").strip()
        if not username:
            continue
        employee_id = row.get("employee_id")
        dedupe_key = f"{username.lower()}::{employee_id}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        matches.append({"username": username, "employee_id": employee_id})
    return matches


def resolve_recruiter_from_question(question, confirmed_username, confirmed_employee_id):
    if confirmed_employee_id or confirmed_username:
        return confirmed_username, confirmed_employee_id, [], "resolved"

    hints = extract_name_hints(question)
    if not hints:
        return None, None, [], "none"

    matches = find_matching_recruiters(hints)
    if not matches and len(hints) > 1:
        matches = find_matching_recruiters([hints[0]])

    return finalize_name_matches(matches, hints)


def resolve_person_from_question(
    question,
    primary_table,
    confirmed_username,
    confirmed_employee_id,
    domain=None,
    history=None,
    last_result=None,
):
    """
    Resolve a named person in Prohance (employees) or DataVista (recruiters/users).
    DataVista never resolves candidate names — only recruiter/user names.
    Returns (username, id, matches, domain, status).
    """
    if domain is None:
        domain = detect_question_domain(question, history=history, last_result=last_result)

    if confirmed_username or confirmed_employee_id:
        if should_reuse_prior_person(question, confirmed_username, confirmed_employee_id):
            return confirmed_username, confirmed_employee_id, [], domain, "resolved"
        # Current question names a different person — ignore prior confirmation.
        confirmed_username = None
        confirmed_employee_id = None

    if not looks_like_person_question(question):
        return None, None, [], domain, "none"

    if domain == "datavista":
        username, person_id, matches, status = resolve_recruiter_from_question(
            question, confirmed_username, confirmed_employee_id
        )
        return username, person_id, matches, "datavista", status

    # Attendance/time questions: resolve against Prohance employees.
    if domain == "prohance":
        username, person_id, matches, status = resolve_employee_from_question(
            question, primary_table, confirmed_username, confirmed_employee_id
        )
        return username, person_id, matches, "prohance", status

    return (
        None,
        None,
        [],
        "datavista",
        "not_found" if extract_name_hints(question) else "none",
    )


def should_confirm(candidates, question, confirmed_employee_id, confirmed_username=None):
    # Prefer the pre-query resolver. Avoid large Did-you-mean lists after SQL runs.
    if confirmed_employee_id or confirmed_username:
        return False
    if not looks_like_person_question(question):
        return False
    if COMPARISON_PATTERN.search(question or ""):
        return False
    if len(candidates) < 2 or len(candidates) > 3:
        return False
    return True


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


def current_date_context(domain="prohance"):
    today = date.today()
    month_start = today.replace(day=1)
    year_start = date(today.year, 1, 1)
    if today.month == 12:
        next_month = date(today.year + 1, 1, 1)
    else:
        next_month = date(today.year, today.month + 1, 1)
    next_year = date(today.year + 1, 1, 1)

    if domain == "datavista":
        date_cols = (
            "the stage date column with TRY_CONVERT(date, ...): "
            "SUBMITTALDATE, INTERVIEWDATE, PLACEMENTDATE/STARTDATE, "
            "or INTERNALREJECTDATE/EXTERNALREJECTDATE"
        )
    else:
        date_cols = "sessionDate"

    return (
        f"Today's date is {today.isoformat()} ({today.strftime('%A')}). "
        f"Current calendar month is {today.strftime('%B %Y')}. "
        f"Current calendar year is {today.year}. "
        f"\"This month\" means date >= '{month_start.isoformat()}' "
        f"AND date < '{next_month.isoformat()}'. "
        f"\"This year\" means date >= '{year_start.isoformat()}' "
        f"AND date < '{next_year.isoformat()}'. "
        f"Apply those bounds to {date_cols}. "
        "Relative dates (today, yesterday, this week, this month, this year) "
        "MUST use today's date above — never invent a different year or month."
    )


def is_comparison_question(question):
    text = question or ""
    if COMPARISON_PATTERN.search(text):
        return True
    return bool(
        re.search(
            r"\b(compare|versus|vs\.?|both|side\s*by\s*side|difference\s+between)\b",
            text,
            re.IGNORECASE,
        )
    )


def has_pronoun_person_followup(question):
    """True when the question refers to a prior person without naming them."""
    return bool(
        re.search(
            r"\b(he|she|they|them|his|her|their|him|"
            r"this\s+user|that\s+user|same\s+(?:person|user|recruiter|employee)|"
            r"the\s+same\s+(?:person|user|one))\b",
            question or "",
            re.IGNORECASE,
        )
    )


def is_continuation_followup(question):
    """
    Follow-ups that should keep prior filters/SQL/person context:
    "and avg logged hours", "excluding weekends", "now show me...",
    "what about the other two".
    """
    text = (question or "").strip()
    if not text:
        return False
    if is_comparison_question(text) or has_pronoun_person_followup(text):
        return True
    if re.match(
        r"^\s*(and|also|plus|now|then|okay|ok|alright|sure|please|"
        r"include|including|"
        r"what\s+about|how\s+about|excluding|exclude|without|except)\b",
        text,
        re.IGNORECASE,
    ):
        return True
    # "include/add/with logged hours" mid-sentence follow-ups.
    if re.match(
        r"^\s*(add|with)\b",
        text,
        re.IGNORECASE,
    ) and re.search(
        r"\b(logged\s*hours?|hours?|avg|average|break|breaks)\b",
        text,
        re.IGNORECASE,
    ):
        return True
    if re.search(
        r"\b(what\s+about|how\s+about|the\s+other|as\s+well|too|also|"
        r"include|including)\b",
        text,
        re.IGNORECASE,
    ):
        return True
    # Day / weekend exclusion follow-ups phrased mid-sentence.
    if re.search(
        r"\b(exclud(?:e|ing|ed)|without|except|omit|remove|skip)\b",
        text,
        re.IGNORECASE,
    ) and re.search(
        r"\b(weekend|weekends|weekday|weekdays|"
        r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
        r"mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)s?\b",
        text,
        re.IGNORECASE,
    ):
        return True
    # Metric-only / period-only follow-up with no new person name.
    # "monthly for 26", "month by month", "for 2026" keep prior stages/person.
    if not extract_name_hints(text) and re.search(
        r"\b(avg|average|total|sum|excluding|exclude|weekend|weekday|"
        r"logged\s*hours?|month\s+by\s+month|month\s*to\s*month|"
        r"by\s+month|each\s+month|per\s+month|monthly|months?|"
        r"years?|yearly|\bmom\b|break|information|same|those|that|"
        r"for\s+(?:20\d{2}|'?\d{2})|in\s+(?:20\d{2}|'?\d{2}))\b",
        text,
        re.IGNORECASE,
    ):
        return True
    return False


_DAY_NAME_MAP = {
    "monday": "Monday",
    "tuesday": "Tuesday",
    "wednesday": "Wednesday",
    "thursday": "Thursday",
    "friday": "Friday",
    "saturday": "Saturday",
    "sunday": "Sunday",
    "mon": "Monday",
    "tue": "Tuesday",
    "tues": "Tuesday",
    "wed": "Wednesday",
    "thu": "Thursday",
    "thur": "Thursday",
    "thurs": "Thursday",
    "fri": "Friday",
    "sat": "Saturday",
    "sun": "Sunday",
}


def _extract_exclusion_flags(text):
    """Return (exclude_weekends, exclude_weekdays, [day labels]) from one utterance."""
    lowered = (text or "").lower()
    if not re.search(r"\b(exclud(?:e|ing|ed)|without|except|omit|remove|skip)\b", lowered):
        # Bare "weekends" in an excluding-style follow-up still counts when paired upstream.
        if not re.search(r"\b(weekend|weekends|weekday|weekdays)\b", lowered):
            return False, False, []

    exclude_weekends = bool(re.search(r"\bweekends?\b", lowered))
    exclude_weekdays = bool(re.search(r"\bweekdays?\b", lowered))
    excluded_days = []
    for key, label in _DAY_NAME_MAP.items():
        if re.search(rf"\b{key}s?\b", lowered):
            if label not in excluded_days:
                excluded_days.append(label)
    return exclude_weekends, exclude_weekdays, excluded_days


def collect_excluded_weekdays(question, history=None):
    """
    Stacked weekday names to exclude from this turn + prior user follow-ups.
    Returns (excluded_day_labels, weekends_only_mode).
    weekends_only_mode means "exclude weekdays" (keep Sat/Sun only).
    """
    texts = []
    current = question or ""
    if current.strip():
        texts.append(current)

    if history and (
        is_continuation_followup(current)
        or re.search(
            r"\b(exclud|without|except|omit|remove|skip|avg|average|total)\b",
            current,
            re.IGNORECASE,
        )
    ):
        for item in history:
            if str(item.get("role") or "").lower() != "user":
                continue
            prior = str(item.get("content") or "")
            if re.search(
                r"\b(exclud|without|except|omit|remove|skip|weekend|weekday|"
                r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
                prior,
                re.IGNORECASE,
            ):
                texts.append(prior)

    exclude_weekends = False
    exclude_weekdays = False
    excluded_days = []
    for text in texts:
        wends, wdays, days = _extract_exclusion_flags(text)
        exclude_weekends = exclude_weekends or wends
        exclude_weekdays = exclude_weekdays or wdays
        for day in days:
            if day not in excluded_days:
                excluded_days.append(day)

    if exclude_weekends:
        for day in ("Saturday", "Sunday"):
            if day not in excluded_days:
                excluded_days.append(day)

    weekends_only = bool(exclude_weekdays and not excluded_days)
    return excluded_days, weekends_only


def exclusion_sql_guidance(question, history=None):
    """Translate excluding weekends/weekdays/Monday into SQL DATENAME filters."""
    excluded_days, weekends_only = collect_excluded_weekdays(question, history)
    current_has_exclusion = bool(
        re.search(
            r"\b(exclud(?:e|ing|ed)|without|except|omit|remove|skip)\b",
            question or "",
            re.IGNORECASE,
        )
    )
    if not excluded_days and not weekends_only:
        return None
    if not current_has_exclusion and not is_continuation_followup(question):
        return None

    notes = [
        "EXCLUSION FILTER: Use DATENAME(WEEKDAY, sessionDate) for day-of-week filters "
        "(do not use DATEPART weekday numbers — they depend on DATEFIRST and are wrong). "
        "You MUST recalculate the aggregate with this filter in SQL — "
        "never reuse or repeat the previous numeric answer."
    ]
    if weekends_only:
        notes.append(
            "Exclude weekdays: AND DATENAME(WEEKDAY, sessionDate) IN ('Saturday', 'Sunday')."
        )
    if excluded_days:
        listed = ", ".join(f"'{d}'" for d in excluded_days)
        notes.append(
            f"Exclude these weekdays (combined from this and prior follow-ups): "
            f"AND DATENAME(WEEKDAY, sessionDate) NOT IN ({listed})."
        )
    return " ".join(notes)


def _prior_sql_from_context(history=None, last_result=None):
    if isinstance(last_result, dict):
        query = str(last_result.get("query") or "").strip()
        if query and query.upper() != "NA":
            return query
    for item in reversed(history or []):
        content = str(item.get("content") or "")
        match = re.search(r"SQL:\s*(SELECT[\s\S]+)$", content, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        if re.search(r"\bsessionDate\b", content, re.IGNORECASE):
            match = re.search(r"(SELECT[\s\S]+)", content, re.IGNORECASE)
            if match:
                return match.group(1).strip()
    return ""


def _extract_username_from_sql(sql):
    if not sql:
        return None
    match = re.search(r"\buserName\b\s+LIKE\s+'([^']+)'", sql, re.IGNORECASE)
    if match:
        return match.group(1).rstrip("%").strip() or None
    match = re.search(r"\buserName\b\s*=\s*'([^']+)'", sql, re.IGNORECASE)
    if match:
        return match.group(1).strip() or None
    return None


def _extract_date_bounds_from_sql(sql):
    if not sql:
        return None
    match = re.search(
        r"\bsessionDate\b\s*>=\s*'(\d{4}-\d{2}-\d{2})'[\s\S]*?"
        r"\bsessionDate\b\s*<\s*'(\d{4}-\d{2}-\d{2})'",
        sql,
        re.IGNORECASE,
    )
    if match:
        return match.group(1), match.group(2)
    match = re.search(
        r"\bsessionDate\b\s+BETWEEN\s+'(\d{4}-\d{2}-\d{2})'\s+AND\s+'(\d{4}-\d{2}-\d{2})'",
        sql,
        re.IGNORECASE,
    )
    if match:
        return match.group(1), match.group(2)
    return None


def _person_from_history_questions(history):
    for item in history or []:
        if str(item.get("role") or "").lower() != "user":
            continue
        hints = extract_name_hints(item.get("content") or "")
        if hints:
            return " ".join(hints)
    return None


def _date_bounds_from_history(history, question):
    for item in history or []:
        if str(item.get("role") or "").lower() != "user":
            continue
        content = str(item.get("content") or "")
        if re.search(
            rf"\b({_NAME_MONTHS}|this\s+month|this\s+year)\b",
            content,
            re.IGNORECASE,
        ):
            return extract_month_year_bounds(content)
    return extract_month_year_bounds(question)


def wants_average_hours_metric(question, history=None, prior_sql=""):
    text = question or ""
    if re.search(r"\b(avg|average)\b", text, re.IGNORECASE):
        return True
    if re.search(r"\bavg_seconds\b", prior_sql or "", re.IGNORECASE):
        return True
    for item in reversed(history or []):
        content = str(item.get("content") or "")
        role = str(item.get("role") or "").lower()
        if role == "user" and re.search(r"\b(avg|average)\b", content, re.IGNORECASE):
            return True
        if role == "assistant" and re.search(
            r"\bavg_seconds\b|\baveraged\b|\baverage\b", content, re.IGNORECASE
        ):
            return True
    return False


def is_hours_followup_question(question, history=None, last_result=None):
    """Avg / exclusion follow-ups on a prior logged-hours thread."""
    if not is_continuation_followup(question):
        return False
    if is_mixed_performance_question(question):
        return False

    text = question or ""
    asks_hours_metric = bool(
        re.search(
            r"\b(avg|average|total|sum|logged\s*hours?|hours?|"
            r"exclud|without|except|omit|remove|skip|weekend|weekday|"
            r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            text,
            re.IGNORECASE,
        )
    )
    if not asks_hours_metric:
        return False

    blob_parts = [text]
    if isinstance(last_result, dict):
        blob_parts.append(str(last_result.get("query") or ""))
        blob_parts.append(str(last_result.get("question") or ""))
    for item in history or []:
        blob_parts.append(str(item.get("content") or ""))
    blob = "\n".join(blob_parts)
    return bool(
        re.search(
            r"\b(logged_hours|sessionDate|avg_seconds|total_seconds|"
            r"logged\s*hours?|EmployeeAttendance)\b",
            blob,
            re.IGNORECASE,
        )
    )


def build_prohance_hours_followup_sql(
    question,
    history=None,
    last_result=None,
    confirmed_username=None,
    primary_table=None,
):
    """
    Deterministic AVG/SUM logged_hours SQL for follow-ups, including day exclusions.
    Forces a fresh recalculation so "excluding Friday" cannot reuse the prior number.
    """
    # Month-by-month follow-ups must keep one row per month — handled elsewhere.
    if effective_wants_month_breakdown(question, history, last_result):
        return None
    if not is_hours_followup_question(question, history, last_result):
        return None

    prior_sql = _prior_sql_from_context(history, last_result)
    person = (confirmed_username or "").strip()
    if not person:
        person = _person_from_history_questions(history) or _extract_username_from_sql(
            prior_sql
        )
    if not person:
        return None

    bounds = _extract_date_bounds_from_sql(prior_sql)
    if not bounds:
        bounds = _date_bounds_from_history(history, question)
    start_iso, end_iso = bounds

    excluded_days, weekends_only = collect_excluded_weekdays(question, history)
    use_avg = wants_average_hours_metric(question, history, prior_sql)

    table_name = primary_table or (
        schema_provider.get_primary_table_name() if "schema_provider" in globals() else None
    ) or "EmployeeAttendance"
    db_name = get_connected_database_name()
    from_table = f"[{db_name}].[dbo].[{table_name}]" if db_name else f"[{table_name}]"

    safe_person = sql_literal(person)
    first = sql_literal(person.split()[0]) if person.split() else safe_person
    seconds_expr = (
        "COALESCE(DATEDIFF(SECOND, 0, "
        "TRY_CAST(NULLIF(LTRIM(RTRIM(logged_hours)), '') AS TIME)), 0)"
    )
    if use_avg:
        select_expr = f"CAST(ROUND(AVG({seconds_expr}), 0) AS int) AS avg_seconds"
    else:
        select_expr = f"CAST(SUM({seconds_expr}) AS int) AS total_seconds"

    where_parts = [
        f"(userName = '{safe_person}' OR userName LIKE '{safe_person}%' "
        f"OR userName LIKE '{first}%')",
        f"sessionDate >= '{start_iso}'",
        f"sessionDate < '{end_iso}'",
    ]
    if weekends_only:
        where_parts.append("DATENAME(WEEKDAY, sessionDate) IN ('Saturday', 'Sunday')")
    elif excluded_days:
        listed = ", ".join(f"'{d}'" for d in excluded_days)
        where_parts.append(f"DATENAME(WEEKDAY, sessionDate) NOT IN ({listed})")

    return f"SELECT {select_expr} FROM {from_table} WHERE " + " AND ".join(where_parts)


def build_month_breakdown_hours_sql(
    question,
    confirmed_username=None,
    confirmed_employee_id=None,
    primary_table=None,
    history=None,
    last_result=None,
):
    """
    Deterministic month-by-month AVG/SUM logged_hours.
    Also handles follow-ups like "also show logged hours" after a prior
    month-by-month question — keeps the same months/year, not just this month.
    """
    text = question or ""
    if not effective_wants_month_breakdown(text, history, last_result):
        return None

    asks_hours = bool(
        re.search(
            r"\b(logged\s*hours?|avg(?:erage)?\s+(?:logged\s*)?hours?|"
            r"total\s+(?:logged\s*)?hours?|hours?\s+logged|\bhours?\b)\b",
            text,
            re.IGNORECASE,
        )
    )
    # Follow-up that only adds hours onto a prior month-by-month thread.
    if not asks_hours and not (
        is_continuation_followup(text)
        and re.search(r"\b(logged|hours?|avg|average)\b", text, re.I)
    ):
        return None
    if not asks_hours:
        return None

    person = (confirmed_username or "").strip()
    if not person:
        hints = extract_name_hints(question)
        if hints:
            person = " ".join(hints)
    if not person:
        person = _person_from_history_questions(history)
    if not person and isinstance(last_result, dict):
        person = _extract_username_from_sql(str(last_result.get("query") or ""))
    if not person:
        return None

    start_iso, end_iso = resolve_period_bounds(
        text, history=history, last_result=last_result
    )

    table_name = primary_table or (
        schema_provider.get_primary_table_name() if "schema_provider" in globals() else None
    ) or "EmployeeAttendance"
    db_name = get_connected_database_name()
    from_table = f"[{db_name}].[dbo].[{table_name}]" if db_name else f"[{table_name}]"

    safe_person = sql_literal(person)
    first = sql_literal(person.split()[0]) if person.split() else safe_person
    seconds_expr = (
        "COALESCE(DATEDIFF(SECOND, 0, "
        "TRY_CAST(NULLIF(LTRIM(RTRIM(logged_hours)), '') AS TIME)), 0)"
    )

    # Prefer avg when current or prior ask said average.
    prior_q = prior_question_text(history, last_result)
    use_avg = bool(
        re.search(r"\bavg(?:erage)?\b", text, re.IGNORECASE)
        or (
            is_continuation_followup(text)
            and re.search(r"\bavg(?:erage)?\b", prior_q or "", re.IGNORECASE)
            and not re.search(r"\b(total|sum)\b", text, re.IGNORECASE)
        )
    )
    if use_avg:
        metric_expr = f"CAST(ROUND(AVG({seconds_expr}), 0) AS int) AS avg_seconds"
    else:
        metric_expr = f"CAST(SUM({seconds_expr}) AS int) AS total_seconds"

    return (
        "SELECT DATENAME(month, sessionDate) AS month, "
        f"{metric_expr} "
        f"FROM {from_table} "
        f"WHERE (userName = '{safe_person}' OR userName LIKE '{safe_person}%' "
        f"OR userName LIKE '{first}%') "
        f"AND sessionDate >= '{start_iso}' AND sessionDate < '{end_iso}' "
        "GROUP BY DATENAME(month, sessionDate), MONTH(sessionDate), YEAR(sessionDate) "
        "ORDER BY YEAR(sessionDate), MONTH(sessionDate)"
    )


_CALENDAR_MONTHS = (
    r"january|february|march|april|may|june|july|august|september|october|"
    r"november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)


def extract_year_from_question(question, default=None):
    """
    Parse a year from the question.
    Accepts 2026, '26, for 26, in 26, year 26, monthly 26 → 2026.
    """
    text = question or ""
    match = re.search(r"\b(20\d{2})\b", text)
    if match:
        return int(match.group(1))
    match = re.search(
        r"\b(?:for|in|of|year|during|monthly|month\s*by\s*month|"
        r"by\s+month|per\s+month|each\s+month)\s+'?(\d{2})\b",
        text,
        re.IGNORECASE,
    )
    if match:
        return 2000 + int(match.group(1))
    # "26 monthly" / "26 month by month"
    match = re.search(
        r"\b'?(\d{2})\s+(?:monthly|month\s*by\s*month|by\s+month|per\s+month)\b",
        text,
        re.IGNORECASE,
    )
    if match:
        return 2000 + int(match.group(1))
    # Bare two-digit year when the ask is clearly monthly / month-by-month.
    if re.search(
        r"\b(monthly|month\s*by\s*month|by\s+month|per\s+month|each\s+month|\bmom\b)\b",
        text,
        re.IGNORECASE,
    ):
        match = re.search(r"(?<!\d)(\d{2})(?!\d)", text)
        if match:
            yy = int(match.group(1))
            # Avoid day-of-month noise (01-31) unless phrased as a year ask.
            if yy >= 20 or re.search(r"\b(?:for|in|of|year)\b", text, re.I):
                return 2000 + yy
    match = re.search(r"'(\d{2})\b", text)
    if match:
        return 2000 + int(match.group(1))
    return default


def extract_period_bounds(question):
    """
    Return (start_iso, end_iso) for:
      - inclusive month ranges (January to March)
      - year / month-by-month asks → full calendar year
      - a named month, or this month
    """
    text = question or ""
    today = date.today()

    ranged = extract_month_range_bounds(text)
    if ranged:
        return ranged

    month_named = bool(
        re.search(rf"\b({_CALENDAR_MONTHS})\b", text, re.IGNORECASE)
    )
    year = extract_year_from_question(text)
    mentions_year_word = bool(
        re.search(r"\b(?:this\s+)?years?\b|\byearly\b", text, re.IGNORECASE)
    )
    mentions_month_breakdown = bool(
        re.search(
            r"\b("
            r"month\s*by\s*month|month\s*to\s*month|month\s+over\s+month|"
            r"by\s+month|each\s+month|monthly(?:\s+breakdown)?|per\s+month|"
            r"\bmom\b|\bmonths?\b"
            r")\b",
            text,
            re.IGNORECASE,
        )
    )

    # Year (2026 / for 26 / this year / "year") or bare month-breakdown wording
    # without a single named month → full calendar year, one row per month.
    if (year is not None or mentions_year_word or mentions_month_breakdown) and not month_named:
        y = year or today.year
        if re.search(r"\bthis\s+year\b", text, re.IGNORECASE):
            y = today.year
        return f"{y}-01-01", f"{y + 1}-01-01"

    if not month_named:
        if re.search(r"\bthis\s+month\b", text, re.IGNORECASE):
            return extract_month_year_bounds(question)
        return extract_month_year_bounds(question)

    return extract_month_year_bounds(question)


def is_single_named_month_ask(question):
    """
    True when the user named one calendar month (e.g. July) and did not ask
    for a multi-month range, a year, or an explicit month-by-month breakdown.
    Those asks should return that month only — not every month of the year.
    """
    text = question or ""
    if not text.strip():
        return False
    if extract_month_range_bounds(text):
        return False
    if extract_year_from_question(text) is not None and not re.search(
        rf"\b({_CALENDAR_MONTHS})\b", text, re.IGNORECASE
    ):
        return False
    if not re.search(rf"\b({_CALENDAR_MONTHS})\b", text, re.IGNORECASE):
        return False
    # Explicit multi-month wording wins over a named month.
    if re.search(
        r"\b("
        r"month\s*by\s*month|month\s*to\s*month|month\s+over\s+month|"
        r"by\s+month|each\s+month|per\s+month|monthly|\bmom\b|"
        r"this\s+year|last\s+year|years?|yearly"
        r")\b",
        text,
        re.IGNORECASE,
    ):
        return False
    return True


def wants_month_breakdown(question):
    """
    True when results should be one row per calendar month.
    Any mention of month, year, a calendar year, or a named month triggers this.
    A single named month (July) still uses month labeling, but the date filter
    is that month only — not the full year.
    """
    text = question or ""
    if not text.strip():
        return False

    if extract_month_range_bounds(text):
        return True

    if extract_year_from_question(text) is not None:
        return True

    if re.search(
        r"\b("
        r"months?|monthly|years?|yearly|"
        r"this\s+month|this\s+year|last\s+month|last\s+year|"
        r"month\s*by\s*month|month\s*to\s*month|month\s+over\s+month|"
        r"by\s+month|each\s+month|per\s+month|\bmom\b"
        r")\b",
        text,
        re.IGNORECASE,
    ):
        return True

    if re.search(rf"\b({_CALENDAR_MONTHS})\b", text, re.IGNORECASE):
        return True

    return False


def prior_question_text(history=None, last_result=None):
    """Latest prior user question from history / last_result."""
    if isinstance(last_result, dict):
        prior_q = str(last_result.get("question") or "").strip()
        if prior_q:
            return prior_q
    for item in reversed(history or []):
        if str(item.get("role") or "").lower() != "user":
            continue
        content = str(item.get("content") or "").strip()
        if content:
            return content
    return ""


def prior_wants_month_breakdown(history=None, last_result=None):
    prior_q = prior_question_text(history, last_result)
    if prior_q and wants_month_breakdown(prior_q):
        return True
    prior_sql = ""
    if isinstance(last_result, dict):
        prior_sql = str(last_result.get("query") or "")
    if not prior_sql:
        prior_sql = _prior_sql_from_context(history, last_result)
    return bool(
        re.search(
            r"\bDATENAME\s*\(\s*month\b|\bGROUP\s+BY\s+DATENAME\s*\(\s*month",
            prior_sql or "",
            re.IGNORECASE,
        )
    )


def effective_wants_month_breakdown(question, history=None, last_result=None):
    """Current ask or a follow-up that should keep month-by-month from prior turn."""
    if wants_month_breakdown(question):
        return True
    if is_continuation_followup(question) and prior_wants_month_breakdown(
        history, last_result
    ):
        return True
    return False


def resolve_period_bounds(question, history=None, last_result=None):
    """
    Period for the current ask, inheriting prior month-by-month / SQL bounds
    on follow-ups like "also show logged hours".
    "monthly" / "month by month" on this turn → full calendar year (not this month).
    A single named month (July) on this turn → that month only.
    """
    text = question or ""
    # Explicit range on this turn wins.
    if extract_month_range_bounds(text):
        return extract_period_bounds(text)
    # "July" / "logged hours for July" → July only (never expand to the full year).
    if is_single_named_month_ask(text):
        return extract_period_bounds(text)
    # Bare year / year+month phrasing on this turn.
    if extract_year_from_question(text) is not None:
        return extract_period_bounds(text)
    if re.search(
        rf"\b({_CALENDAR_MONTHS}|this\s+year|this\s+month|last\s+month)\b",
        text,
        re.IGNORECASE,
    ) and not is_continuation_followup(text):
        return extract_period_bounds(text)

    # This turn asks for monthly / month-by-month without a single named month
    # → full year. Do NOT inherit a prior default of "this month" only.
    month_named = bool(
        re.search(rf"\b({_CALENDAR_MONTHS})\b", text, re.IGNORECASE)
    )
    if (
        wants_month_breakdown(text)
        and not month_named
        and not re.search(r"\bthis\s+month\b", text, re.IGNORECASE)
    ):
        return extract_period_bounds(text)

    if is_continuation_followup(text) or effective_wants_month_breakdown(
        text, history, last_result
    ):
        prior_sql = _prior_sql_from_context(history, last_result)
        bounds = _extract_date_bounds_from_sql(prior_sql)
        if bounds:
            return bounds
        prior_q = prior_question_text(history, last_result)
        if prior_q and wants_month_breakdown(prior_q):
            return extract_period_bounds(prior_q)
        if prior_q and not wants_month_breakdown(text):
            return extract_period_bounds(prior_q)

    if effective_wants_month_breakdown(text, history, last_result):
        y = date.today().year
        return f"{y}-01-01", f"{y + 1}-01-01"

    return extract_period_bounds(text)


def month_breakdown_guidance(question, history=None, last_result=None):
    if not effective_wants_month_breakdown(question, history, last_result):
        return None
    # Use current + prior metrics so follow-ups keep column intent.
    prior_q = prior_question_text(history, last_result)
    metric_source = f"{prior_q} {question}" if prior_q and is_continuation_followup(question) else question
    metric_order = extract_requested_metric_order(metric_source)
    if metric_order:
        order_note = (
            " Column order MUST be: month, then "
            + ", ".join(metric_order)
            + " (same order the user asked)."
        )
    else:
        order_note = (
            " Column order MUST start with month, then each metric in the "
            "same order the user asked."
        )
    start_iso, end_iso = resolve_period_bounds(
        question, history=history, last_result=last_result
    )
    if is_single_named_month_ask(question):
        range_note = (
            f" SINGLE MONTH ONLY: Date filter MUST be >= '{start_iso}' AND < '{end_iso}'. "
            "Return only that named month (e.g. July) — do NOT return other months "
            "or expand to the full year."
        )
    elif extract_month_range_bounds(question) or (
        prior_q and extract_month_range_bounds(prior_q)
    ):
        range_note = (
            f" Date filter MUST be sessionDate/stage date >= '{start_iso}' "
            f"AND < '{end_iso}' (inclusive month range). "
            "One row per month inside that range only."
        )
    else:
        range_note = (
            f" Date filter MUST be >= '{start_iso}' AND < '{end_iso}'. "
            "For month-by-month / year asks with no smaller range, "
            "cover that full period with one row per month."
        )
    return (
        "MONTH BREAKDOWN: Return one row per calendar month in the date filter. "
        "Select ONLY DATENAME(month, <date>) AS month (display name like January). "
        "GROUP BY DATENAME(month, <date>), MONTH(<date>), YEAR(<date>) "
        "ORDER BY YEAR(<date>), MONTH(<date>). "
        "Do NOT select MONTH(<date>) / month_num as an output column — "
        "users only want the month name, not a number column."
        + order_note
        + range_note
        + " For hours/breaks return INTEGER seconds columns "
        "(total_seconds / avg_seconds / break_seconds) — the app formats as "
        "hours:minutes:seconds (HH:MM:SS, hours may exceed 24)."
    )


def attendance_status_guidance(question):
    text = question or ""
    if not re.search(
        r"\b(present|half[\s-]?day|absent|attendance)\b",
        text,
        re.IGNORECASE,
    ):
        return None
    return (
        "ATTENDANCE DAY STATUS from that day's logged_hours seconds: "
        f"absent if seconds < {ABSENT_MAX_SECONDS} (< 5 hours); "
        f"halfday if seconds >= {ABSENT_MAX_SECONDS} AND seconds < {PRESENT_MIN_SECONDS} "
        "(5–7 hours); "
        f"present if seconds >= {PRESENT_MIN_SECONDS} (>= 7 hours). "
        "For 'how many present days' COUNT days WHERE logged seconds >= 25200. "
        "For halfday/absent use the matching thresholds. "
        "Alias counts present_days / halfday_days / absent_days as asked. "
        "Do NOT invent a duration column."
    )


def is_attendance_day_count_question(question):
    text = question or ""
    return bool(
        re.search(
            r"\b("
            r"(?:how\s+many\s+)?(?:present|half[\s-]?day|absent)\s*(?:days?|sessions?)?|"
            r"attendance\s+(?:count|days?|summary|status|breakdown)"
            r")\b",
            text,
            re.IGNORECASE,
        )
    )


def build_attendance_day_count_sql(
    question,
    history=None,
    last_result=None,
    confirmed_username=None,
    primary_table=None,
):
    """
    Deterministic COUNT of present / halfday / absent days from logged_hours.
    present >= 7h, halfday 5–7h, absent < 5h.
    """
    if not is_attendance_day_count_question(question):
        return None

    person = (confirmed_username or "").strip()
    if not person:
        hints = extract_name_hints(question)
        if hints:
            person = " ".join(hints)
    if not person:
        person = _person_from_history_questions(history) or _extract_username_from_sql(
            _prior_sql_from_context(history, last_result)
        )
    if not person:
        return None

    prior_sql = _prior_sql_from_context(history, last_result)
    bounds = _extract_date_bounds_from_sql(prior_sql)
    if not bounds:
        bounds = extract_period_bounds(question)
    start_iso, end_iso = bounds

    table_name = primary_table or (
        schema_provider.get_primary_table_name() if "schema_provider" in globals() else None
    ) or "EmployeeAttendance"
    db_name = get_connected_database_name()
    from_table = f"[{db_name}].[dbo].[{table_name}]" if db_name else f"[{table_name}]"

    safe_person = sql_literal(person)
    first = sql_literal(person.split()[0]) if person.split() else safe_person
    seconds_expr = (
        "COALESCE(DATEDIFF(SECOND, 0, "
        "TRY_CAST(NULLIF(LTRIM(RTRIM(logged_hours)), '') AS TIME)), 0)"
    )

    requested = extract_requested_metric_order(question)
    want_present = "present_days" in requested or not requested
    want_half = "halfday_days" in requested
    want_absent = "absent_days" in requested
    # If they only said "attendance" with no status word, return all three.
    if not any(m in requested for m in ("present_days", "halfday_days", "absent_days")):
        if re.search(r"\battendance\b", question or "", re.I):
            want_present = want_half = want_absent = True
        elif re.search(r"\bpresent\b", question or "", re.I):
            want_present = True
            want_half = want_absent = False
        elif re.search(r"\bhalf[\s-]?day\b", question or "", re.I):
            want_half = True
            want_present = want_absent = False
        elif re.search(r"\babsent\b", question or "", re.I):
            want_absent = True
            want_present = want_half = False

    select_parts = []
    if want_present:
        select_parts.append(
            f"SUM(CASE WHEN {seconds_expr} >= {PRESENT_MIN_SECONDS} THEN 1 ELSE 0 END) "
            "AS present_days"
        )
    if want_half:
        select_parts.append(
            f"SUM(CASE WHEN {seconds_expr} >= {ABSENT_MAX_SECONDS} "
            f"AND {seconds_expr} < {PRESENT_MIN_SECONDS} THEN 1 ELSE 0 END) "
            "AS halfday_days"
        )
    if want_absent:
        select_parts.append(
            f"SUM(CASE WHEN {seconds_expr} < {ABSENT_MAX_SECONDS} THEN 1 ELSE 0 END) "
            "AS absent_days"
        )
    if not select_parts:
        select_parts.append(
            f"SUM(CASE WHEN {seconds_expr} >= {PRESENT_MIN_SECONDS} THEN 1 ELSE 0 END) "
            "AS present_days"
        )

    where_parts = [
        f"(userName = '{safe_person}' OR userName LIKE '{safe_person}%' "
        f"OR userName LIKE '{first}%')",
        f"sessionDate >= '{start_iso}'",
        f"sessionDate < '{end_iso}'",
    ]
    return f"SELECT {', '.join(select_parts)} FROM {from_table} WHERE " + " AND ".join(
        where_parts
    )


def names_refer_to_same_person(hints, full_name):
    if not hints or not full_name:
        return False
    name_l = str(full_name).lower()
    tokens = [t for t in re.split(r"[^a-z0-9]+", name_l) if t]
    for hint in hints:
        h = hint.lower()
        if h == name_l or h in tokens or name_l.startswith(h + " "):
            continue
        if h in name_l:
            continue
        return False
    return True


def missing_requested_metrics(question, results):
    """Return metric aliases asked for but missing from the result columns."""
    requested = extract_requested_metric_order(question)
    if len(requested) < 2:
        return []
    if not results:
        return list(requested)
    present = set()
    for key in results[0].keys():
        bucket = _metric_bucket_for_column(key)
        if bucket:
            present.add(bucket)
        if _norm_col(key) in {"duration", "loggedhours", "totalbreak"}:
            if "break" in _norm_col(key):
                present.add("break")
            else:
                present.add("logged_hours")
    return [metric for metric in requested if metric not in present]


def infer_active_person(history=None, last_result=None, confirmed_username=None):
    """Prefer explicit confirmation, else prior person from history/SQL."""
    if confirmed_username:
        return confirmed_username
    if last_result and isinstance(last_result, dict):
        from_sql = _extract_username_from_sql(str(last_result.get("query") or ""))
        if from_sql:
            return from_sql
        mem = last_result.get("memory") if isinstance(last_result.get("memory"), dict) else {}
        people = mem.get("people") if isinstance(mem.get("people"), list) else []
        if people:
            return str(people[0])
    return _person_from_history_questions(history)


def should_reuse_prior_person(question, confirmed_username=None, confirmed_employee_id=None):
    """
    Keep the active person across turns until the user names someone else.
    """
    if not (confirmed_username or confirmed_employee_id):
        return False
    if is_comparison_question(question):
        return True
    hints = extract_name_hints(question)
    if hints:
        if confirmed_username and names_refer_to_same_person(hints, confirmed_username):
            return True
        # A different person was named — switch.
        return False
    # No new person named → keep the current one.
    return True


def sanitize_history_for_question(question, history):
    """
    Avoid carrying the previous person's SQL filters into a new-person question.
    Keep full history for comparisons, pronoun follow-ups, and metric continuations.
    """
    if not history:
        return []
    if (
        is_comparison_question(question)
        or has_pronoun_person_followup(question)
        or is_continuation_followup(question)
    ):
        return history

    hints = extract_name_hints(question)
    if not hints:
        # No new name — keep history for vague follow-ups like "what about this month?"
        return history

    cleaned = []
    for item in history:
        role = str(item.get("role") or "")
        content = str(item.get("content") or "")
        if role.lower() == "user":
            cleaned.append({"role": role, "content": content})
            continue
        # Drop prior SQL / row dumps so the model does not reuse old person filters.
        clipped = content.split("SQL:")[0].strip()
        clipped = re.split(r"\nColumns:", clipped, maxsplit=1)[0].strip()
        if clipped:
            cleaned.append({"role": role, "content": clipped})
    return cleaned


def should_attach_last_result(question, last_result):
    if not last_result:
        return False
    if is_chart_followup(question):
        return True
    if is_comparison_question(question):
        return True
    if has_pronoun_person_followup(question):
        return True
    if is_continuation_followup(question):
        return True
    # New named person → do not inject previous result context.
    if extract_name_hints(question):
        return False
    return True


def generate_sql(
    question,
    history,
    primary_table,
    confirmed_username,
    confirmed_employee_id,
    domain="prohance",
):
    date_hint = current_date_context(domain=domain)
    enhanced_question = enhance_for_sql(question, history=history, domain=domain)
    prompt_question = format_for_prompt(
        enhanced_question,
        sanitize_history_for_question(question, history),
        confirmed_username,
        confirmed_employee_id,
        domain=domain,
    )
    db_name = get_datavista_database_name()

    if domain == "datavista":
        instructions_text = load_text_file("instructions_datavista.txt")
        schema_text = get_datavista_schema_text()
        samples_text = load_text_file("sample_queries_datavista.txt")
        table_rule = datavista_table_guidance(question)
        extra = (
            f"\nDATABASE ROUTING: DataVista = recruiting/performance "
            f"(submittals/interviews/hires/rejects/clients). "
            f"Prohance = attendance (logged hours/breaks/AAFS/login).\n"
            f"Use three-part names with database [{db_name}], e.g. "
            f"[{db_name}].[dbo].[CR_HireMaster].\n"
            f"{table_rule}\n"
            "In CR_HireMaster: PLACEMENTDATE = offer/placement date; "
            "STARTDATE = date they started work. Never swap them.\n"
            "Never default to CR_HireMaster when the question did not say "
            "hire/offer/placement/start date.\n"
            "Named people are always the recruiter/user — never filter candidate name columns.\n"
            "If the question asks how many submittals/interviews/hires/rejects, "
            "return SELECT COUNT(*) from that ONE table only (no UNION, no detail rows)."
        )
    else:
        instructions_text = load_text_file("instructions.txt")
        schema_text = schema_provider.get_schema_text()
        samples_text = load_text_file("sample_queries.txt")
        table_hint = (
            f"\n\nPrimary Prohance table to query when unsure: [{primary_table}]"
            if primary_table
            else ""
        )
        extra = (
            f"{table_hint}\n"
            "DATABASE ROUTING: Prohance = attendance/time tracking "
            "(logged hours, breaks, AAFS, login/logout, late login, swipe, shift). "
            "DataVista = recruiting/performance "
            "(submittals, interviews, hires, rejects, clients, placement/start dates).\n"
            "Duration columns like logged_hours and aafs* breaks are often VARCHAR 'HH:MM:SS'. "
            "Never COALESCE them with 0 or AVG them directly. Convert to seconds with "
            "DATEDIFF(SECOND, 0, TRY_CAST(... AS TIME)) before AVG/SUM/addition. "
            "For SUM/total of durations (month/week totals), return total_seconds as an integer. "
            "Do NOT CONVERT seconds back to TIME/varchar HH:MM:SS — TIME wraps at 24 hours. "
            "The app formats seconds as hours:minutes:seconds (hours may exceed 24). "
            "If the user asks for an average, the SQL MUST include AVG(...) and GROUP BY when needed, "
            "and should return avg_seconds (integer), not a TIME string. "
            "For day-by-day logged hours lists, SELECT logged_hours once "
            "(optionally logged_seconds for ORDER BY only). "
            "Never SELECT a column aliased duration / logged_duration — "
            "use logged_hours, total_break / break_seconds, etc. matching the question. "
            "Attendance day status from logged_hours: "
            "absent < 5h, halfday 5–7h, present >= 7h."
        )

    raw = get_openai_completion(
        system_prompt=(
            f"{instructions_text}\n\nDatabase Schema:\n{schema_text}\n\n"
            f"Example Queries:\n{samples_text}\n\n"
            f"{date_hint}\n{extra}\n\n"
            "Return ONLY one read-only SQL Server query. "
            "It must be a single SELECT or WITH ... SELECT statement. "
            "For 'how many' questions always write SELECT COUNT(...) FROM ..., "
            "never bare COUNT(...) without SELECT. "
            "Unless the user is comparing people, answer ONLY about the person "
            "named in the current question — do not reuse a person from earlier turns. "
            "\"This month\" / \"this year\" must use the exact date bounds provided above. "
            "Do not use DECLARE, BEGIN/END, USE, SET, or markdown. "
            "No explanation, no INSERT/UPDATE/DELETE."
        ),
        user_prompt=prompt_question,
    )
    return extract_sql_query(raw)


def generate_answer(
    question,
    history,
    results,
    confirmed_username,
    confirmed_employee_id,
    domain="prohance",
    duration_hint="",
):
    enhanced_question = enhance_for_answer(question)
    prompt_question = format_for_prompt(
        enhanced_question,
        history,
        confirmed_username,
        confirmed_employee_id,
        domain=domain,
    )

    if not results:
        return (
            "I couldn't find any matching records for that question.",
            "table",
        )

    preview_rows = results[:5]
    extra = ""
    if len(results) > 5:
        extra = f"\nTotal rows returned: {len(results)}. Only the first 5 rows are shown above."
    if duration_hint:
        extra = f"{extra}\n{duration_hint}".strip()

    person_hint = ""
    if confirmed_username:
        label = "candidate" if domain == "datavista" else "employee"
        person_hint = f"\nConfirmed {label} full name: {confirmed_username}."

    answer_examples = (
        "\"Akshay Soni was hired at Acme for Software Engineer on 2026-07-12.\""
        if domain == "datavista"
        else (
            "\"Akshay Soni logged 45:30:00 in July.\" "
            "or \"Akshay Soni averaged 08:12:00 per day this month.\""
        )
    )

    try:
        raw_answer = get_openai_completion(
            system_prompt=(
                "You are IRI AI, a data analyst for workforce (Prohance) and recruiting (DataVista). "
                "Reply with ONLY 1-2 short natural-language sentences. "
                f"Lead with the direct answer in plain English, like: {answer_examples} "
                "Include the person's full name when available, the key number/date, and the period asked about. "
                "When a time field is present (logged_hours, total_break, avg_logged_hours, etc.), "
                "use that exact hours:minutes:seconds value (e.g. 08:15:00 or 160:05:12). "
                "Do not convert to days/weeks wording. "
                "If the prompt includes a REQUIRED logged-time total, you MUST include that "
                "exact hours:minutes:seconds value in your answer. "
                "Do not use markdown, bullets, headings, or tables. "
                "Do not list rows or repeat every column — the UI already shows the data table underneath. "
                "If the data is empty, say no matching records were found. "
                "Never invent values that are not in the data."
            ),
            user_prompt=(
                f"{prompt_question}{person_hint}\n\n"
                f"Data: {json.dumps(preview_rows, default=str)}{extra}"
            ),
        )
        answer = " ".join(str(raw_answer or "").split()).strip()
        if not answer:
            answer = build_compact_result_answer(question, results)
    except Exception:
        answer = build_compact_result_answer(question, results)

    # If the model still omitted a required overall total, append it.
    if duration_hint and results:
        match = re.search(
            r"overall total logged time across all months/rows as:\s*([^.]+)\.",
            duration_hint,
            flags=re.IGNORECASE,
        )
        overall = match.group(1).strip() if match else None
        if not overall:
            match = re.search(
                r"verbatim:\s*([^=;]+=[^;]+)",
                duration_hint,
                flags=re.IGNORECASE,
            )
            if match:
                # Prefer a logged_hours=... piece when present.
                pieces = [
                    p.strip()
                    for p in re.findall(
                        r"([A-Za-z0-9_]+=[^;]+)",
                        duration_hint[match.start() :],
                    )
                ]
                chosen = next(
                    (p for p in pieces if p.lower().startswith("logged_hours=")),
                    pieces[0] if pieces else None,
                )
                if chosen and "=" in chosen:
                    overall = chosen.split("=", 1)[1].strip()
        if overall and not re.search(
            r"\b\d+\s+(week|day|hour|minute)s?\b",
            answer or "",
            re.IGNORECASE,
        ):
            answer = (
                f"{answer} Total logged time: {overall}."
                if answer
                else f"Total logged time: {overall}."
            ).strip()

    return answer, recommend_chart(results, "table", question)


def is_large_result_question(question, results):
    if len(results) >= 8:
        return True
    return bool(results and LISTING_PATTERN.search(question or ""))


def build_compact_result_answer(question, results):
    row_count = len(results)
    first_row = results[0] if results else {}
    username = None
    for key, value in first_row.items():
        if key.lower() in {"username", "surname", "name"} and value:
            username = str(value)
            break

    # Prefer a simple spoken fallback using the first row's main metric.
    metric_parts = []
    for key, value in first_row.items():
        if key.lower() in {"username", "surname", "name", "employeeid", "id"}:
            continue
        if value is None or value == "":
            continue
        metric_parts.append(f"{key.replace('_', ' ')} {value}")
        if len(metric_parts) >= 2:
            break

    who = username or "The matching employee"
    if metric_parts and row_count == 1:
        return f"{who} has {', '.join(metric_parts)}."
    if username and row_count > 1:
        return f"I found {row_count} matching records for {username}."
    if row_count == 0:
        return "I couldn't find any matching records for that question."
    return f"I found {row_count} matching records."


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

                datavista_db = get_datavista_database_name()
                datavista_ok = False
                datavista_error = None
                datavista_tables = []
                try:
                    cursor.execute(
                        f"""
                        SELECT name
                        FROM [{datavista_db}].sys.tables
                        WHERE name IN (
                            'CR_HireMaster', 'CR_InterviewMaster',
                            'CR_RejectMaster', 'CR_SubmittalMaster'
                        )
                        ORDER BY name
                        """
                    )
                    datavista_tables = [
                        item.get("name") for item in rows_as_dicts(cursor) if item.get("name")
                    ]
                    datavista_ok = len(datavista_tables) > 0
                    if not datavista_ok:
                        datavista_error = (
                            f"Connected to SQL Server, but no CR_* tables were found in [{datavista_db}]. "
                            "Check the database name or permissions."
                        )
                except Exception as datavista_exc:
                    datavista_error = str(datavista_exc)

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
                "datavistaDatabase": get_datavista_database_name(),
                "datavistaOk": datavista_ok,
                "datavistaTables": datavista_tables,
                "datavistaError": datavista_error,
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
    question = normalize_user_question(question)

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

    sql_query = ""
    domain = "prohance"
    try:
        if not should_attach_last_result(question, last_result):
            last_result = None
        history = add_result_context_to_history(history, last_result)

        # Stick with the current person until a different person is named.
        if not confirmed_username:
            confirmed_username = infer_active_person(
                history=history,
                last_result=last_result,
                confirmed_username=None,
            )

        if not should_reuse_prior_person(
            question, confirmed_username, confirmed_employee_id
        ):
            confirmed_username = None
            confirmed_employee_id = None
        elif confirmed_username and not extract_name_hints(question):
            # Keep sticky person for follow-ups with no new name.
            pass

        primary_table = schema_provider.get_primary_table_name()
        domain = detect_question_domain(question, history=history, last_result=last_result)

        # Prohance still needs a local table; DataVista uses three-part names on the same server.
        if domain == "prohance" and not primary_table:
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

        (
            confirmed_username,
            confirmed_employee_id,
            name_matches,
            domain,
            name_status,
        ) = resolve_person_from_question(
            question,
            primary_table,
            confirmed_username,
            confirmed_employee_id,
            domain=domain,
            history=history,
            last_result=last_result,
        )

        if name_status == "not_found":
            return jsonify(
                {
                    "query": "",
                    "answer": person_not_found_message(question),
                    "data": [],
                    "chart_type": "table",
                    "domain": domain,
                }
            )

        if name_status == "ambiguous":
            return jsonify(
                {
                    "query": "",
                    "answer": person_ambiguous_message(question),
                    "data": [],
                    "chart_type": "table",
                    "domain": domain,
                }
            )

        if name_matches and name_status == "confirm":
            hint_text = " ".join(extract_name_hints(question)) or "that name"
            person_word = (
                "recruiters"
                if domain == "datavista" and is_recruiter_question(question)
                else "candidates"
                if domain == "datavista"
                else "people"
            )
            return jsonify(
                {
                    "query": "",
                    "answer": (
                        f'I found {len(name_matches)} close matches for "{hint_text}". '
                        "Did you mean one of these?"
                    ),
                    "needs_confirmation": True,
                    "candidates": name_matches[:3],
                    "data": [],
                    "chart_type": "table",
                    "domain": domain,
                }
            )

        # Month-by-month stages + hours first so mixed single-row totals do not win.
        sql_query = build_month_breakdown_with_hours_followup_sql(
            question,
            history=history,
            last_result=last_result,
            confirmed_username=confirmed_username,
            confirmed_employee_id=confirmed_employee_id,
            primary_table=primary_table,
        )
        if not sql_query:
            sql_query = build_mixed_performance_sql(
                question,
                confirmed_username=confirmed_username,
                confirmed_employee_id=confirmed_employee_id,
                primary_table=primary_table,
            )
        if not sql_query:
            sql_query = build_datavista_stage_counts_sql(
                question,
                confirmed_username=confirmed_username,
                confirmed_employee_id=confirmed_employee_id,
                history=history,
                last_result=last_result,
            )
        if not sql_query:
            sql_query = build_month_breakdown_hours_sql(
                question,
                confirmed_username=confirmed_username,
                confirmed_employee_id=confirmed_employee_id,
                primary_table=primary_table,
                history=history,
                last_result=last_result,
            )
        if not sql_query:
            sql_query = build_attendance_day_count_sql(
                question,
                history=history,
                last_result=last_result,
                confirmed_username=confirmed_username,
                primary_table=primary_table,
            )
        if not sql_query:
            sql_query = build_prohance_hours_followup_sql(
                question,
                history=history,
                last_result=last_result,
                confirmed_username=confirmed_username,
                primary_table=primary_table,
            )
        if not sql_query:
            sql_query = generate_sql(
                question,
                history,
                primary_table,
                confirmed_username,
                confirmed_employee_id,
                domain=domain,
            )
        # If LLM omitted day exclusions on an hours follow-up, force deterministic SQL.
        if (
            domain == "prohance"
            and is_hours_followup_question(question, history, last_result)
            and collect_excluded_weekdays(question, history)[0]
            and not re.search(r"\bDATENAME\s*\(\s*WEEKDAY", sql_query or "", re.I)
        ):
            rebuilt = build_prohance_hours_followup_sql(
                question,
                history=history,
                last_result=last_result,
                confirmed_username=confirmed_username
                or _extract_username_from_sql(sql_query)
                or _person_from_history_questions(history),
                primary_table=primary_table,
            )
            if rebuilt:
                sql_query = rebuilt

        # Hard guard: monthly / month-by-month / year asks must not return a
        # single total row (mixed performance / LLM often emit one row with
        # submittals, interviews, offers, starts, avg_logged_seconds).
        if effective_wants_month_breakdown(
            question, history=history, last_result=last_result
        ) and not re.search(
            r"\bDATENAME\s*\(\s*month\b", sql_query or "", re.IGNORECASE
        ):
            person_for_month = (
                confirmed_username
                or _extract_username_from_sql(sql_query)
                or _person_from_history_questions(history)
                or " ".join(extract_name_hints(question) or [])
            ).strip()
            rebuilt = build_month_breakdown_with_hours_followup_sql(
                question,
                history=history,
                last_result=last_result,
                confirmed_username=person_for_month or None,
                confirmed_employee_id=confirmed_employee_id,
                primary_table=primary_table,
            )
            if not rebuilt:
                rebuilt = build_datavista_stage_counts_sql(
                    question,
                    confirmed_username=person_for_month or None,
                    confirmed_employee_id=confirmed_employee_id,
                    history=history,
                    last_result=last_result,
                )
            if not rebuilt:
                rebuilt = build_month_breakdown_hours_sql(
                    question,
                    confirmed_username=person_for_month or None,
                    confirmed_employee_id=confirmed_employee_id,
                    primary_table=primary_table,
                    history=history,
                    last_result=last_result,
                )
            if rebuilt:
                sql_query = rebuilt

        sql_query = clean_sql(sql_query, primary_table or "EmployeeAttendance")
        sql_query = remove_broad_query_limit(sql_query, question)
        sql_query = ensure_single_readonly_sql(sql_query)
        if domain == "datavista" or is_mixed_performance_question(question):
            sql_query = qualify_datavista_sql(sql_query)
            sql_query = fix_prohance_object_names(
                sql_query, table_name=primary_table or "EmployeeAttendance"
            )

        if sql_query.upper() == "NA":
            return jsonify(
                {
                    "query": "NA",
                    "answer": (
                        "I couldn't map that question to your Prohance or DataVista tables. "
                        "Try asking about attendance/breaks (Prohance) or "
                        "hires/interviews/rejects/submittals (DataVista)."
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

        raw_results = execute_sql(sql_query)
        # If the user asked for several metrics but SQL only returned one, regenerate once.
        missing = missing_requested_metrics(question, raw_results)
        if missing and domain == "prohance":
            retry_question = (
                f"{question}\n\n"
                f"CRITICAL RETRY: Previous SQL missed these metrics: {', '.join(missing)}. "
                f"Return ONE SELECT that includes ALL of: "
                f"{', '.join(extract_requested_metric_order(question))}."
            )
            retry_sql = generate_sql(
                retry_question,
                history,
                primary_table,
                confirmed_username,
                confirmed_employee_id,
                domain=domain,
            )
            retry_sql = clean_sql(retry_sql, primary_table or "EmployeeAttendance")
            retry_sql = remove_broad_query_limit(retry_sql, question)
            retry_sql = ensure_single_readonly_sql(retry_sql)
            is_retry_valid, _ = validate_select_query(retry_sql)
            if is_retry_valid and retry_sql.upper() != "NA":
                try:
                    retry_results = execute_sql(retry_sql)
                    if not missing_requested_metrics(question, retry_results):
                        sql_query = retry_sql
                        raw_results = retry_results
                except Exception:
                    pass

        duration_hint = build_duration_answer_context(raw_results, question)
        # Follow-ups like "also show logged hours" should keep prior metric order.
        order_question = question
        if is_continuation_followup(question):
            prior_q = prior_question_text(history, last_result)
            if prior_q:
                order_question = f"{prior_q}\n{question}"
        results = enrich_duration_results(raw_results, order_question)
        # Prefer the enriched HH:MM:SS values for the spoken answer (avoids raw 66704).
        enriched_hint = build_duration_answer_context(results, order_question)
        if enriched_hint:
            duration_hint = enriched_hint
        candidates = get_candidates(results)
        if should_confirm(candidates, question, confirmed_employee_id, confirmed_username):
            hint_text = " ".join(extract_name_hints(question)) or "that name"
            shortlist = candidates[:3]
            return jsonify(
                {
                    "query": sql_query,
                    "answer": (
                        f'I found {len(shortlist)} close matches for "{hint_text}". '
                        "Did you mean one of these?"
                    ),
                    "needs_confirmation": True,
                    "candidates": shortlist,
                    "data": [],
                    "chart_type": "table",
                    "domain": domain,
                    "confirmed_username": confirmed_username,
                    "confirmed_employee_id": confirmed_employee_id,
                }
            )

        if not confirmed_username and is_hours_followup_question(
            question, history, last_result
        ):
            confirmed_username = (
                _person_from_history_questions(history)
                or _extract_username_from_sql(sql_query)
            )
        if not confirmed_username:
            confirmed_username = infer_active_person(
                history=history,
                last_result={"query": sql_query},
                confirmed_username=None,
            )

        answer, chart_type = generate_answer(
            question,
            history,
            results,
            confirmed_username,
            confirmed_employee_id,
            domain=domain,
            duration_hint=duration_hint,
        )
        chart_type = recommend_chart(results, chart_type, question)
        columns = result_column_order(results, order_question)

        return jsonify(
            {
                "query": sql_query,
                "answer": answer,
                "data": results,
                "column_order": columns,
                "chart_type": chart_type,
                "total_rows": len(results),
                "domain": domain,
                "confirmed_username": confirmed_username,
                "confirmed_employee_id": confirmed_employee_id,
            }
        )
    except DATABASE_ERROR_TYPES as exc:
        detail = str(exc)
        invalid_object = bool(
            re.search(r"42S02|invalid object name", detail, re.IGNORECASE)
        )
        syntax_error = bool(
            re.search(r"42000|incorrect syntax|syntax error", detail, re.IGNORECASE)
        )
        if domain == "datavista" or is_mixed_performance_question(question):
            hint = (
                "This looked like a DataVista (recruiting) question. "
                "Use SUBMITTALDATE (not SubmitDate), PRIMARYRECRUITERNAME, "
                "and TRY_CONVERT(date, ...) for month filters. "
                "Confirm the SQL login can read the DataVista database."
            )
            if invalid_object:
                hint += (
                    " For mixed performance (submits + logged hours), do not invent a "
                    "database named Prohance — use the connected attendance database/"
                    "table three-part name from the schema notes."
                )
            if syntax_error:
                hint += (
                    " Check aliases like AS submittals were not rewritten into table names, "
                    "and avg hours uses DATEDIFF(SECOND, 0, TRY_CAST(logged_hours AS TIME))."
                )
        else:
            hint = (
                "This looked like a Prohance (attendance/hours) question. "
                "Use sessionDate for month filters, userName for the person, "
                "and SUM of DATEDIFF(SECOND, ...) on logged_hours as total_seconds "
                "(do not convert totals back to TIME — it wraps at 24 hours)."
            )
            if invalid_object:
                hint += (
                    " Do not invent database/table names; use the connected database "
                    "and the live attendance table from schema."
                )
            if syntax_error:
                hint += " Open the SQL below — a syntax error (42000) usually means a broken rewrite."
        return jsonify(
            {
                "query": sql_query,
                "answer": (
                    "I couldn't run the database query. The table or column name may be wrong "
                    "for your connected SQL Server database.\n\n"
                    f"{hint}\n\n"
                    "Open SQL query details below to see the exact statement that failed."
                ),
                "error": detail,
                "data": [],
                "chart_type": "table",
                "domain": domain,
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
