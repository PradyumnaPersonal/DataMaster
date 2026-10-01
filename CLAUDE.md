# CLAUDE.md — guidance for Claude working in this repo

## What this is
DataMaster: user asks a question in plain English → Claude writes SQL → we run it read-only →
Claude explains the result in plain English. The UI always shows the answer AND the SQL used.

## Layout
- `src/datamaster/db.py` — SQLAlchemy connection, schema description, `validate_select` safety gate
- `src/datamaster/llm.py` — prompts + `ClaudeLLM` (Anthropic SDK) + tag parsing
- `src/datamaster/engine.py` — `DataMaster.ask()`: generate → run → self-repair on error → explain
- `src/datamaster/app.py` — Streamlit chat UI
- `src/datamaster/cli.py` — terminal interface (`datamaster`)
- `scripts/make_sample_db.py` — builds `data/sample.db`
- `tests/` — pytest, uses a `FakeLLM`; tests must never call the real API

## Rules
- Never weaken `validate_select` — only SELECT/WITH may ever reach the database.
- Never read or print `.env`.
- Every behaviour change gets a test with `FakeLLM`. Run `pytest` before finishing.
- Keep it simple: no new frameworks without asking.

## Commands
- `pytest` · `python scripts/make_sample_db.py` · `streamlit run src/datamaster/app.py` · `datamaster --schema`
