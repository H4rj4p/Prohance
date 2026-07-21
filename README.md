# Prohance / IRI AI

Flask chatbot that answers natural-language questions about IRI workforce data in SQL Server (attendance, logins, breaks, AAFS, shifts).

## Setup

1. Install dependencies: `pip install -r requirements.txt`
2. Configure `local.settings.json` with `SqlConnectionString`, optional `SqlServerHost`, and `OpenAIApiKey`
3. Run: `python app.pyw`
4. Open `http://localhost:7179/api/Chat`

The SQL connection layer is unchanged. Schema is loaded from the live database when available, with `schema.sql` / `instructions.txt` / `sample_queries.txt` guiding SQL generation.
