# Prohance / IRI AI

Flask chatbot for IRI data in SQL Server:

- **Prohance** — workforce attendance (logins, breaks, AAFS, shifts)
- **DataVista** — recruiting pipeline (hires, interviews, rejects, submittals) on the same server

## Setup

1. Install dependencies: `pip install -r requirements.txt`
2. Configure `local.settings.json` with `SqlConnectionString` (Prohance), optional `SqlServerHost`, and `OpenAIApiKey`
3. Optional: `DataVistaDatabase` (defaults to `DataVista`) if the database name differs
4. Run: `python app.pyw`
5. Open `http://localhost:7179/api/Chat`

The SQL connection layer is unchanged. DataVista is queried with three-part names (`DataVista.dbo.CR_HireMaster`, etc.) over the same connection, so the SQL login needs read access to both databases.
