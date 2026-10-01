"""Orchestration: question -> SQL -> run (with self-repair) -> plain-English answer."""

from dataclasses import dataclass, field

import pandas as pd

from .config import Settings, load_settings
from .db import Database, UnsafeQueryError
from .llm import ANSWER_SYSTEM, FIX_PROMPT, LLM, SQL_SYSTEM, ClaudeLLM, extract_tag, strip_tags


@dataclass
class Answer:
    question: str
    answer: str
    sql: str = ""
    data: pd.DataFrame = field(default_factory=pd.DataFrame)
    truncated: bool = False
    attempts: int = 0
    error: str = ""


class DataMaster:
    def __init__(self, db: Database, llm: LLM, max_retries: int = 2):
        self.db = db
        self.llm = llm
        self.max_retries = max_retries

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "DataMaster":
        s = settings or load_settings()
        return cls(Database(s.database_url, s.max_rows), ClaudeLLM(s.model), s.max_retries)

    def ask(self, question: str, history: list[tuple] | None = None) -> Answer:
        """history: previous (question, sql[, answer]) turns so follow-ups like 'and by month?'
        or 'why?' work. sql may be empty for turns answered without a query."""
        system = SQL_SYSTEM.format(dialect=self.db.dialect, schema=self.db.describe_schema())
        messages: list[dict] = []
        for q, s, *rest in (history or [])[-5:]:
            said = f"\n<said>{rest[0]}</said>" if rest and rest[0] else ""
            messages += [{"role": "user", "content": q},
                         {"role": "assistant", "content": f"<sql>{s}</sql>{said}"}]
        messages.append({"role": "user", "content": question})

        sql, error = "", ""
        for attempt in range(1, self.max_retries + 2):
            reply = self.llm.chat(system, messages)
            sql = extract_tag(reply, "sql")
            if not sql:
                note = (extract_tag(reply, "answer") or strip_tags(reply)
                        or "I couldn't answer that from this database.")
                return Answer(question, note, attempts=attempt)
            try:
                df = self.db.run(sql, limit=self.db.max_rows + 1)
            except UnsafeQueryError as e:
                return Answer(question, f"Refused to run the generated query: {e}", sql, attempts=attempt, error=str(e))
            except Exception as e:  # SQL error -> let the model fix it
                error = str(e).splitlines()[0][:500]
                messages += [{"role": "assistant", "content": reply},
                             {"role": "user", "content": FIX_PROMPT.format(error=error)}]
                continue
            truncated = len(df) > self.db.max_rows
            df = df.head(self.db.max_rows)
            return Answer(question, self._explain(question, sql, df, truncated), sql, df, truncated, attempt)

        return Answer(question, f"Sorry, I couldn't build a working query. Last error: {error}",
                      sql, attempts=self.max_retries + 1, error=error)

    def _explain(self, question: str, sql: str, df: pd.DataFrame, truncated: bool) -> str:
        preview = df.head(50).to_csv(index=False) if not df.empty else "(no rows)"
        msg = (f"Question: {question}\n\nSQL used:\n{sql}\n\n"
               f"Result ({len(df)} rows{', truncated' if truncated else ''}, first 50 shown as CSV):\n{preview}")
        return self.llm.chat(ANSWER_SYSTEM, [{"role": "user", "content": msg}]).strip()
