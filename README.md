# DataMaster

Ask your database anything — technical or not — and get a plain-English answer **plus the SQL used** to get it.

```
You:  Which country brought in the most revenue last quarter?
DataMaster: The Netherlands, with €48,210 from 61 completed orders — about 27% of the total.
      ▸ SQL used   ▸ Data (6 rows)  ⤓ CSV
```

## How it works

1. **Schema** — reads your tables, columns, keys and 3 sample rows per table.
2. **SQL** — Claude turns the question into one read-only query.
3. **Safety gate** — only a single `SELECT`/`WITH` is ever executed (writes, DDL, multiple statements are refused).
4. **Self-repair** — if the query errors, the error goes back to Claude to fix (up to 2 retries).
5. **Answer** — Claude explains the result rows in plain English. Follow-ups ("and by month?") keep context.

## Setup (VS Code, Windows)

```powershell
cd datamaster
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev,all-dbs]"    # or pick: postgres, mysql, mssql, oracle, redshift, snowflake, bigquery, databricks, duckdb
copy .env.example .env             # then put your ANTHROPIC_API_KEY in .env
python scripts\make_sample_db.py   # sample sales DB to try first
pytest                             # no API calls
streamlit run src\datamaster\app.py
```

Terminal mode: `datamaster "top 5 products by revenue"`, or just `datamaster` for a chat.

## Your own database

In the web app, open the **Connect** page and enter host, database, user and password (or paste a
SQLAlchemy URL). Credentials stay in memory for that browser session only. They are never written
to disk or sent to the model. Alternatively, set `DATABASE_URL` in `.env`: the app auto-connects
to it, and the terminal mode requires it.
**Use a read-only database user.** The safety gate is a second line of defence, not the first.

## Building on it with the Claude extension

`CLAUDE.md` tells Claude the layout and rules; `.claude/settings.json` lets it run tests/the app and blocks it from reading `.env`.
Good next prompts:
- "Add a chart to the answer when the result is a time series or a category breakdown."
- "Add a business glossary file (e.g. 'revenue = completed orders only') that's included in the SQL prompt."
- "Cache schema description to disk and add a refresh button."
- "Log every question, SQL, and latency to a `query_log` table in a separate SQLite file."
