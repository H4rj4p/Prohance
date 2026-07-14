"""OpenAI helpers for natural-language → SQL and answer drafting."""

from __future__ import annotations

import json
import re
from typing import Any

from openai import OpenAI

from app.config import Settings


def _client(settings: Settings) -> OpenAI:
    if not settings.openai_api_key:
        raise RuntimeError(
            "OpenAIApiKey is missing. Add it to local.settings.json or set the "
            "OpenAIApiKey environment variable."
        )
    return OpenAI(api_key=settings.openai_api_key)


def generate_sql(settings: Settings, question: str, schema: str) -> str:
    client = _client(settings)
    system = (
        "You are a SQL Server expert for the Prohance database. "
        "Given a user question and schema, write one read-only T-SQL query. "
        "Rules: SELECT or WITH only; no INSERT/UPDATE/DELETE/DDL; "
        "prefer TOP 50; use schema-qualified names; return ONLY the SQL text."
    )
    user = f"Schema:\n{schema}\n\nQuestion:\n{question}"
    response = client.chat.completions.create(
        model=settings.openai_model,
        temperature=0,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    content = (response.choices[0].message.content or "").strip()
    return _extract_sql(content)


def explain_results(
    settings: Settings,
    question: str,
    sql: str,
    result: dict[str, Any],
) -> str:
    client = _client(settings)
    preview = {
        "columns": result.get("columns"),
        "rows": result.get("rows", [])[:20],
        "row_count": result.get("row_count"),
        "truncated": result.get("truncated"),
    }
    system = (
        "You answer questions about Prohance data using SQL query results. "
        "Be concise and factual. If the result set is empty, say so clearly."
    )
    user = (
        f"Question: {question}\n\n"
        f"SQL used:\n{sql}\n\n"
        f"Result JSON:\n{json.dumps(preview, default=str)}"
    )
    response = client.chat.completions.create(
        model=settings.openai_model,
        temperature=0.2,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return (response.choices[0].message.content or "").strip()


def _extract_sql(text: str) -> str:
    fenced = re.search(r"```(?:sql)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fenced:
        return fenced.group(1).strip()
    return text.strip()
