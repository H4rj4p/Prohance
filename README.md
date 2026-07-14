# Prohance Chat

Python Flask website that chats with your **Prohance** SQL Server database using OpenAI. Ask a question in plain English; the app generates a read-only SQL query, runs it, and explains the result.

## Requirements (Windows)

On the computer that will run this app:

1. **Python 3.10+**
2. **[ODBC Driver 18 for SQL Server](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server)**
3. Network access to the SQL Server machine (`VMWinSQLS` on port `1433`)
4. Windows account permissions to the `Prohance` database (`Trusted_Connection=yes`)

On the SQL Server computer:

- SQL Server must allow remote TCP connections on port 1433
- Windows Firewall must allow inbound 1433 from your PC
- Hostname `VMWinSQLS` must resolve (DNS or `C:\Windows\System32\drivers\etc\hosts`)

## Setup

```bat
cd path\to\Prohance
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy local.settings.json.example local.settings.json
```

Edit `local.settings.json` and set your real `OpenAIApiKey`. Keep that file local — it is gitignored.

Example:

```json
{
  "IsEncrypted": false,
  "Values": {
    "SqlServerHost": "VMWinSQLS",
    "SqlConnectionString": "Driver={ODBC Driver 18 for SQL Server};Server=VMWinSQLS,1433;Database=Prohance;Trusted_Connection=yes;TrustServerCertificate=yes;Encrypt=no;",
    "OpenAIApiKey": "sk-...",
    "OpenAIModel": "gpt-4o-mini"
  }
}
```

If Windows Authentication is awkward across machines, switch to SQL auth:

```text
Driver={ODBC Driver 18 for SQL Server};Server=VMWinSQLS,1433;Database=Prohance;UID=YourUser;PWD=YourPassword;TrustServerCertificate=yes;Encrypt=no;
```

## Run

```bat
python chat.pyw
```

This starts Flask at `http://127.0.0.1:5000/` and opens your browser.  
`.pyw` hides the console on Windows. To see startup errors, run:

```bat
python -c "exec(open('chat.pyw', encoding='utf-8').read())"
```

## How it works

1. Browser UI posts your question to `/api/chat`
2. App loads a short schema summary from SQL Server
3. OpenAI writes a `SELECT` / `WITH` query
4. App validates the SQL (blocks inserts/updates/DDL) and executes it
5. OpenAI turns the rows into a short answer; SQL + table preview are shown too

## API

- `GET /api/health` — Flask + database + OpenAI config status
- `POST /api/chat` — `{ "message": "..." }`

## Security notes

- Do not commit `local.settings.json` or API keys
- Only read-only queries are allowed by the app
- Prefer a least-privilege SQL login if you stop using Windows auth
