"""HTTP routes for the Prohance chat UI."""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, render_template, request

from app import db, openai_client

bp = Blueprint("main", __name__)


@bp.get("/")
def index():
    return render_template("index.html")


@bp.get("/api/health")
def health():
    settings = current_app.config["SETTINGS"]
    db_status = db.test_connection(settings)
    return jsonify(
        {
            "ok": True,
            "openai_configured": bool(settings.openai_api_key),
            "model": settings.openai_model,
            "database": db_status,
        }
    )


@bp.post("/api/chat")
def chat():
    settings = current_app.config["SETTINGS"]
    payload = request.get_json(silent=True) or {}
    question = (payload.get("message") or "").strip()
    if not question:
        return jsonify({"error": "Message is required."}), 400

    try:
        schema = db.fetch_schema_summary(settings)
        sql = openai_client.generate_sql(settings, question, schema)
        result = db.run_select(settings, sql)
        answer = openai_client.explain_results(settings, question, sql, result)
        return jsonify(
            {
                "answer": answer,
                "sql": result["sql"],
                "columns": result["columns"],
                "rows": result["rows"],
                "row_count": result["row_count"],
                "truncated": result.get("truncated", False),
            }
        )
    except Exception as exc:  # noqa: BLE001 — return error to client for debugging
        return jsonify({"error": str(exc)}), 500
