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

# Prohance = attendance / time only. Keep this narrow on purpose.
PROHANCE_PATTERN = re.compile(
    r"\b("
    r"prohance|"
    r"logged\s*hours?|hours?\s*logged|hours?\s*worked|worked\s*hours?|"
    r"total\s*hours?|average\s*hours?|avg\s*hours?|"
    r"aafs|break(?:s)?|lunch\s*break|short\s*break|personal\s*time|"
    r"login|logout|first\s*login|last\s*logout|session\s*date|"
    r"attendance|shift(?:s)?|late\s*login|early\s*logout|swipe|"
    r"workforce|employee\s*hours|time\s*tracked|time\s*tracking|"
    r"time\s*at\s*(?:work|desk)|on\s*desk|away\s*from\s*system|"
    r"how\s+long\s+(?:did|have)\b|were\s+they\s+late|clock\s*in|clock\s*out"
    r")\b",
    re.IGNORECASE,
)


def get_datavista_database_name():
    """Optional override; connection string itself is unchanged."""
    return (os.environ.get("DataVistaDatabase") or "DataVista").strip() or "DataVista"


def detect_question_domain(question):
    """
    Route questions to Prohance (attendance/time) or DataVista (recruiting).

    Prohance is ONLY for logged hours, breaks, AAFS, login/logout, attendance.
    Submits, pay rates, start/placement dates, clients, candidates → DataVista.
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

    # Recruiting keywords win over attendance when both appear
    # (e.g. "breaks between submits" is still a recruiting question).
    if has_datavista:
        return "datavista"
    if has_prohance:
        return "prohance"

    # Default: DataVista. Prohance only when attendance/time keywords are present.
    return "datavista"


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
    "what", "whats", "who", "whose", "whom", "which", "when", "where", "why", "how",
    "many", "much", "avg", "average", "mean", "total", "sum", "count", "number",
    "logged", "log", "logs", "logging", "hours", "hour",
    "break", "breaks", "lunch", "personal", "time", "times",
    "login", "logins", "logout", "late", "early", "shift", "shifts", "location",
    "locations", "session", "sessions", "attendance", "activity", "activities",
    "today", "yesterday", "tomorrow", "this", "that", "these", "those", "last", "next",
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
    "something", "anything", "nothing", "everything", "metric", "metrics",
    "amount", "number", "count", "counts", "stats", "statistics", "report",
    "summary", "breakdown", "trend", "trends", "compare", "comparison",
}


def is_multi_part(question):
    return bool(question and MULTI_PART_PATTERN.search(question))


def is_recruiter_question(question):
    """True when the named person is the recruiter/user, not the candidate."""
    text = question or ""
    return bool(
        re.search(
            r"\b("
            r"recruit(?:s|ed|ing)?|recruiter|"
            r"who\s+(?:all\s+)?(?:did|has|have)\b.*\b(?:recruit|submit|hire|interview)|"
            r"candidates?\s+(?:for|of|under)|"
            r"who\s+(?:all\s+)?(?:they|he|she)\s+recruit|"
            r"(?:for|by)\s+this\s+user|"
            r"worked\s+on|their\s+candidates|"
            # "how many submits/hires/clients did Jordan make/get"
            r"how\s+many\s+"
            r"(?:submits?|submittals?|submissions?|hires?|interviews?|rejects?|"
            r"clients?|candidates?|placements?|offers?)"
            r"\b.*\b(?:did|has|have|made|got|get)\b|"
            # "submits Jordan made" / "submittals by Priya" / "hires for this user"
            r"(?:submits?|submittals?|submissions?|hires?|interviews?|rejects?|"
            r"placements?)\s+(?:did|for|by|from)\b|"
            r"(?:did|made|got)\s+.+\s+"
            r"(?:submit|submittal|hire|interview|place|recruit)"
            r")\b",
            text,
            re.IGNORECASE,
        )
    )


def datavista_table_guidance(question):
    text = (question or "").lower()

    if is_recruiter_question(question) or re.search(
        r"\b(how\s+many\s+clients?|clients?\s+did|performance|placed|placements?\s+did)\b",
        text,
    ):
        return (
            "DATABASE: DataVista (recruiting/performance). "
            "This is a recruiter/user performance question. Treat the named person as the "
            "user/recruiter. Filter PRIMARYRECRUITERNAME / USERFIRSTNAME / USERLASTNAME / userid. "
            "Return candidate/client results with light info "
            "(candidate name, company, job title, pay rate, location, stage date). "
            "If stage is unnamed, UNION ALL all four CR_* tables with a Stage column. "
            "For client counts use COUNT(DISTINCT COMPANYNAME)."
        )

    if re.search(r"\b(placement\s*date|offer\s*date|hire\s*date|received\s+an\s+offer)\b", text):
        return (
            "DATABASE: DataVista. TABLE: CR_HireMaster only. "
            "Use PLACEMENTDATE (date they received the offer / were placed). "
            "Do not confuse with STARTDATE."
        )

    if re.search(
        r"\b(start\s*date|started\s+work|first\s+day|came\s+in\s+for\s+work|start\s+work)\b",
        text,
    ):
        return (
            "DATABASE: DataVista. TABLE: CR_HireMaster only. "
            "Use STARTDATE (date they started work). "
            "Do not confuse with PLACEMENTDATE (offer/placement date)."
        )

    if re.search(r"\b(interview|interviewed|interviews)\b", text):
        return (
            "DATABASE: DataVista. TABLE: CR_InterviewMaster only. "
            "Use INTERVIEWDATE for interview timing."
        )

    if re.search(r"\b(submittal|submitted|submit|submission)s?\b", text):
        return (
            "DATABASE: DataVista. TABLE: CR_SubmittalMaster only. "
            "Use SUBMITTALDATE for when they were submitted."
        )

    if re.search(r"\b(reject|rejected|rejection|rejects)\b", text):
        return (
            "DATABASE: DataVista. TABLE: CR_RejectMaster only. "
            "Use INTERNALREJECTDATE / EXTERNALREJECTDATE and REJECTREASON."
        )

    if re.search(r"\b(hire[ds]?|hiring|offer(?:ed|s)?|bill\s*rate)\b", text):
        return (
            "DATABASE: DataVista. TABLE: CR_HireMaster only. "
            "PLACEMENTDATE = offer/placement date; STARTDATE = work start date."
        )

    if re.search(
        r"\b(pay\s*rate|agreed\s*pay|company(?:\s*name)?|job\s*title|location)\b",
        text,
    ):
        return (
            "DATABASE: DataVista. Stage unclear — do NOT default to CR_HireMaster. "
            "UNION ALL CR_SubmittalMaster, CR_InterviewMaster, CR_HireMaster, CR_RejectMaster "
            "with a Stage column. Return rows where requested fields are present."
        )

    return (
        "DATABASE: DataVista. If pipeline stage is unclear, UNION ALL all four CR_* tables "
        "with a Stage column. Remember: Prohance is only for attendance/logged hours/breaks."
    )


def enhance_for_sql(question):
    notes = []
    text = question or ""

    if is_multi_part(question):
        notes.append(
            "IMPORTANT: This message asks MULTIPLE things at once. "
            "Return exactly ONE SELECT statement (no semicolons) that answers EVERY part. "
            "Combine results using multiple columns, aggregates, CASE/SUM, and subqueries in the same query."
        )

    if re.search(r"\b(average|avg|mean|total|sum|overall)\b", text, re.IGNORECASE):
        notes.append(
            "IMPORTANT: The user asked for an aggregate. Use AVG/SUM in SQL over matching rows. "
            "Do NOT return only the first raw row. "
            "Duration fields may be VARCHAR 'HH:MM:SS' — convert to seconds before AVG/SUM. "
            "Return the aggregate as INTEGER seconds (alias total_seconds or avg_seconds). "
            "NEVER CONVERT/DATEADD back to TIME/HH:MM:SS for totals — SQL TIME wraps at 24 hours "
            "and month totals would be wrong. The app formats seconds into days/weeks/hours."
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
            "return total_seconds only (no TIME convert)."
        )

    if detect_question_domain(question) == "datavista":
        notes.append(datavista_table_guidance(question))

    if not notes:
        return question

    return text + "\n\n" + "\n".join(notes)


def enhance_for_answer(question):
    base = (
        "\n\nIMPORTANT: Answer in 1-2 short natural-language sentences only. "
        "Example style: \"Akshay Soni averaged 32.5 logged hours this month.\" "
        "Lead with the person, the metric, and the time period."
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

    if domain == "datavista":
        if is_recruiter_question(question):
            if confirmed_employee_id:
                lines.append(
                    f"The user confirmed recruiter/user id exactly: {confirmed_employee_id}. "
                    "Filter with userid = that value OR match PRIMARYRECRUITERNAME / USER names for that person. "
                    "Return the candidates they worked on with light details."
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
                        "Return candidate names plus job title, company, pay rate, location, stage/date."
                    )
                else:
                    lines.append(
                        f"The user confirmed recruiter/user: {confirmed_username}. "
                        "Match PRIMARYRECRUITERNAME / USERFIRSTNAME / USERLASTNAME to that person "
                        "and return their candidates with light details."
                    )
        elif confirmed_employee_id:
            lines.append(
                f"The user confirmed they mean CANDIDATEID exactly: {confirmed_employee_id}. "
                "Use WHERE CANDIDATEID = that exact value."
            )
        elif confirmed_username:
            parts = str(confirmed_username).split()
            if len(parts) >= 2:
                first = parts[0]
                last = parts[-1]
                lines.append(
                    f"The user confirmed candidate {confirmed_username}. "
                    f"Use WHERE CANDIDATEFIRSTNAME = '{first}' "
                    f"AND CANDIDATELASTNAME = '{last}' (exact match)."
                )
            else:
                lines.append(
                    f"The user confirmed candidate name: {confirmed_username}. "
                    "Match CANDIDATEFIRSTNAME / CANDIDATELASTNAME to that person."
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

    table_aliases = {
        "cr_submitmaster": "CR_SubmittalMaster",
        "cr_submittals": "CR_SubmittalMaster",
        "cr_submissionmaster": "CR_SubmittalMaster",
        "submittalmaster": "CR_SubmittalMaster",
        "submittals": "CR_SubmittalMaster",
        "submissions": "CR_SubmittalMaster",
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
            rf"(?<![\w.\]])\[?{wrong}\]?\b",
            right,
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
    sql = convert_limit_to_top(sql)
    sql = rewrite_duration_time_converts(sql)
    sql = ensure_single_readonly_sql(sql)
    return repair_select_query(sql)

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


def parse_hhmmss_to_seconds(value):
    """Parse 'HH:MM:SS' / 'H:MM:SS' into seconds. Returns None if not a time string."""
    if value is None:
        return None
    text = str(value).strip()
    match = re.fullmatch(r"(\d{1,4}):([0-5]?\d):([0-5]?\d)", text)
    if not match:
        return None
    hours, minutes, seconds = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return hours * 3600 + minutes * 60 + seconds


def _is_seconds_column(name):
    key = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    return key.endswith("seconds") or key.endswith("secs") or key in {
        "totalseconds",
        "avgseconds",
        "loggedseconds",
        "breakseconds",
        "durationseconds",
        "sumseconds",
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
            "duration",
            "totaltime",
            "breaktime",
        )
    ) or key in {
        "loggedhours",
        "logged_hours",
        "totalhours",
        "avghours",
        "hours",
    }


def enrich_duration_results(results):
    """
    Add human-readable duration fields so totals > 24h don't look like clock times.
    Prefers *_seconds columns; also formats HH:MM:SS duration aggregates.
    """
    if not results:
        return results

    enriched = []
    for row in results:
        new_row = dict(row)
        for key, value in list(row.items()):
            label_key = None
            seconds = None

            if _is_seconds_column(key) and value is not None:
                try:
                    seconds = float(value)
                except (TypeError, ValueError):
                    seconds = None
                if seconds is not None:
                    base = re.sub(r"_?seconds?$", "", key, flags=re.IGNORECASE)
                    label_key = f"{base}_duration" if base and base != key else "duration"

            elif value is not None and _is_duration_label_column(key):
                seconds = parse_hhmmss_to_seconds(value)
                if seconds is not None:
                    # Keep original clock string only when under 24h; always add readable label.
                    label_key = f"{key}_duration" if not key.lower().endswith("duration") else key

            if seconds is None or label_key is None:
                continue

            readable = format_duration_seconds(seconds)
            if not readable:
                continue
            new_row[label_key] = readable
            # For second totals, also expose a friendly primary label when missing.
            if _is_seconds_column(key) and "duration" not in {
                str(k).lower() for k in new_row.keys()
            }:
                new_row["duration"] = readable
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
        # "Akshay Soni, how many hours..." / "Akshay Soni: show logged hours"
        rf"^\s*{_NAME_SPAN}\s*[,:\-]\s*"
        rf"(?:how|what|when|who|where|show|list|give|tell|did|does|can|could|"
        rf"please|find|get)\b",
        # "Akshay Soni how many hours did he log"
        rf"^\s*{_NAME_SPAN}\s+"
        rf"(?:how|what|when|who|where)\b",
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
        rf"this|last|next|,|\?|$))",
        rf"\b(?:about|regarding)\s+{_NAME_SPAN}\b",
        # "Akshay's logged hours" / "Jordan's submittal"
        rf"\b{_NAME_SPAN}(?:'s)?\s+"
        rf"(?:interview|submittal|submission|hire|pay\s*rate|logged|log|"
        rf"break|hours|submits?)\b",
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
    Avoid treating words like "monthly" / "made" / "log" as people.
    """
    text = question or ""
    if not text.strip():
        return False

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
        if h in name_l:
            total += 0
            continue
        distances = [levenshtein_distance(h, token) for token in tokens] or [99]
        best = min(distances)
        # Allow 1-2 character typos on a token of similar length.
        if best <= 2 and any(abs(len(h) - len(token)) <= 2 for token in tokens):
            total += best
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

    if 2 <= len(best_matches) <= max_confirm and best_score <= 2:
        return None, None, best_matches[:max_confirm], "confirm"

    if len(best_matches) > max_confirm:
        return None, None, [], "ambiguous"

    # Fall back to the single closest name if it's within 2 edits.
    if best_score <= 2:
        match = scored[0][1]
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


def _recruiter_name_where(name_hints):
    clauses = []
    for hint in name_hints:
        safe = sql_literal(hint)
        clauses.append(
            "("
            f"PRIMARYRECRUITERNAME LIKE '%{safe}%' "
            f"OR USERFIRSTNAME LIKE '%{safe}%' "
            f"OR USERLASTNAME LIKE '%{safe}%' "
            f"OR (USERFIRSTNAME + ' ' + USERLASTNAME) LIKE '%{safe}%'"
            ")"
        )
    return " AND ".join(clauses)


def find_matching_recruiters(name_hints, limit=20):
    """Match DataVista users/recruiters across CR_* tables."""
    if not name_hints:
        return []

    db_name = get_datavista_database_name()
    where_sql = _recruiter_name_where(name_hints)
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
                    f"PRIMARYRECRUITERNAME LIKE '%{prefix}%' "
                    f"OR USERFIRSTNAME LIKE '%{prefix}%' "
                    f"OR USERLASTNAME LIKE '%{prefix}%' "
                    f"OR DIFFERENCE(CAST(PRIMARYRECRUITERNAME AS NVARCHAR(400)), '{safe}') >= 3 "
                    f"OR DIFFERENCE(CAST(USERFIRSTNAME AS NVARCHAR(400)), '{safe}') >= 3 "
                    f"OR DIFFERENCE(CAST(USERLASTNAME AS NVARCHAR(400)), '{safe}') >= 3"
                    ")"
                )
            fuzzy_selects.append(
                f"""
                SELECT DISTINCT
                    COALESCE(
                        NULLIF(LTRIM(RTRIM(PRIMARYRECRUITERNAME)), ''),
                        LTRIM(RTRIM(COALESCE(USERFIRSTNAME, ''))) + ' ' +
                        LTRIM(RTRIM(COALESCE(USERLASTNAME, '')))
                    ) AS username,
                    userid AS employee_id
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


def resolve_candidate_from_question(question, confirmed_username, confirmed_employee_id):
    if confirmed_employee_id or confirmed_username:
        return confirmed_username, confirmed_employee_id, [], "resolved"

    hints = extract_name_hints(question)
    if not hints:
        return None, None, [], "none"

    matches = find_matching_candidates(hints)
    if not matches and len(hints) > 1:
        matches = find_matching_candidates([hints[0]])

    return finalize_name_matches(matches, hints)


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


def resolve_person_from_question(question, primary_table, confirmed_username, confirmed_employee_id):
    """
    Resolve a named person in Prohance (employees) or DataVista (candidates/recruiters).
    Returns (username, id, matches, domain, status).
    """
    domain = detect_question_domain(question)

    if confirmed_username or confirmed_employee_id:
        return confirmed_username, confirmed_employee_id, [], domain, "resolved"

    if not looks_like_person_question(question):
        return None, None, [], domain, "none"

    if domain == "datavista":
        if is_recruiter_question(question):
            username, person_id, matches, status = resolve_recruiter_from_question(
                question, confirmed_username, confirmed_employee_id
            )
            return username, person_id, matches, "datavista", status

        username, person_id, matches, status = resolve_candidate_from_question(
            question, confirmed_username, confirmed_employee_id
        )
        return username, person_id, matches, "datavista", status

    # Attendance/time questions: resolve against Prohance employees.
    if domain == "prohance":
        username, person_id, matches, status = resolve_employee_from_question(
            question, primary_table, confirmed_username, confirmed_employee_id
        )
        return username, person_id, matches, "prohance", status

    # DataVista (default for non-attendance): recruiters/candidates already handled above.
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


def current_date_context():
    today = date.today()
    month_start = today.replace(day=1)
    if today.month == 12:
        next_month = date(today.year + 1, 1, 1)
    else:
        next_month = date(today.year, today.month + 1, 1)

    return (
        f"Today's date is {today.isoformat()} ({today.strftime('%A')}). "
        f"Current month is {today.strftime('%B %Y')}. "
        f"\"This month\" means sessionDate >= '{month_start.isoformat()}' "
        f"AND sessionDate < '{next_month.isoformat()}'. "
        "Relative dates like today/yesterday/this week/this month MUST use this date, "
        "not an assumed year."
    )


def generate_sql(
    question,
    history,
    primary_table,
    confirmed_username,
    confirmed_employee_id,
    domain="prohance",
):
    date_hint = current_date_context()
    enhanced_question = enhance_for_sql(question)
    prompt_question = format_for_prompt(
        enhanced_question,
        history,
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
            "hire/offer/placement/start date."
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
            "If the user asks for an average, the SQL MUST include AVG(...) and GROUP BY when needed, "
            "and should return avg_seconds (integer), not a TIME string."
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

    person_hint = ""
    if confirmed_username:
        label = "candidate" if domain == "datavista" else "employee"
        person_hint = f"\nConfirmed {label} full name: {confirmed_username}."

    answer_examples = (
        "\"Akshay Soni was hired at Acme for Software Engineer on 2026-07-12.\""
        if domain == "datavista"
        else (
            "\"Akshay Soni logged 1 week 2 days and 3 hours in July.\" "
            "or \"Akshay Soni averaged 8 hours and 12 minutes per day this month.\""
        )
    )

    try:
        raw_answer = get_openai_completion(
            system_prompt=(
                "You are IRI AI, a data analyst for workforce (Prohance) and recruiting (DataVista). "
                "Reply with ONLY 1-2 short natural-language sentences. "
                f"Lead with the direct answer in plain English, like: {answer_examples} "
                "Include the person's full name when available, the key number/date, and the period asked about. "
                "When a *_duration or duration field is present (for example "
                "'1 week 2 days and 3 hours'), USE THAT exact wording for time totals — "
                "do not convert seconds yourself and do not quote HH:MM:SS clock times for "
                "totals that can exceed 24 hours. "
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
    try:
        history = add_result_context_to_history(history, last_result)
        primary_table = schema_provider.get_primary_table_name()
        domain = detect_question_domain(question)

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

        sql_query = generate_sql(
            question,
            history,
            primary_table,
            confirmed_username,
            confirmed_employee_id,
            domain=domain,
        )
        sql_query = clean_sql(sql_query, primary_table or "EmployeeAttendance")
        sql_query = remove_broad_query_limit(sql_query, question)
        sql_query = ensure_single_readonly_sql(sql_query)
        if domain == "datavista":
            sql_query = qualify_datavista_sql(sql_query)

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

        results = enrich_duration_results(execute_sql(sql_query))
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
                }
            )

        answer, chart_type = generate_answer(
            question,
            history,
            results,
            confirmed_username,
            confirmed_employee_id,
            domain=domain,
        )
        chart_type = recommend_chart(results, chart_type, question)

        return jsonify(
            {
                "query": sql_query,
                "answer": answer,
                "data": results,
                "chart_type": chart_type,
                "total_rows": len(results),
                "domain": domain,
            }
        )
    except DATABASE_ERROR_TYPES as exc:
        detail = str(exc)
        hint = (
            "Common DataVista fixes: use SUBMITTALDATE (not SubmitDate), "
            "PRIMARYRECRUITERNAME, and TRY_CONVERT(date, ...) for month filters. "
            "Also confirm the SQL login can read the DataVista database."
        )
        return jsonify(
            {
                "query": sql_query,
                "answer": (
                    "I couldn't run the database query. The table or column name may be wrong "
                    "for your connected SQL Server database.\n\n"
                    f"{hint}"
                ),
                "error": detail,
                "data": [],
                "chart_type": "table",
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
