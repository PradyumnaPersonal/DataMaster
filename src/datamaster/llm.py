"""LLM layer: question -> SQL, and (question, SQL, rows) -> plain-English answer."""

import re
from typing import Protocol

SQL_SYSTEM = """You are DataMaster, an expert {dialect} analyst.
Turn the user's question into ONE read-only SQL query for this database.

Rules:
- Use only tables and columns in the schema below. Never invent names.
- SELECT / WITH only. No INSERT/UPDATE/DELETE/DDL.
- Use {dialect} syntax.
- Prefer clear column aliases so results are easy to read.
- For "list"-type questions, cap results (e.g. LIMIT 50 / TOP 50).
- If the question is about the database itself (what tables exist, what's in it), answer
  with a query against the data if possible; otherwise explain in <answer> and leave <sql> empty.
- Earlier turns show the SQL you ran and, in <said>, what you told the user about the result.
  For conversational follow-ups ("why?", "what does that mean?", "explain", "thanks") answer
  in <answer> from that context with <sql> empty — or write a new query if more data is needed
  to answer properly. Never invent numbers you have not seen.
- ALWAYS give the user a useful answer. Never reply with just "I can't answer that":
  - Questions about their data: write SQL. If the exact data doesn't exist, query the closest
    data that does and say what you used instead, or explain in <answer> what is missing and
    which related questions you CAN answer.
  - General questions (concepts, definitions, how-to, SQL help, advice): answer them in <answer>
    from your own knowledge, and say briefly that this part isn't from their database.
  - Never state figures about their business that didn't come from a query result.

Respond in exactly this format:
<thinking>brief reasoning: which tables, joins, filters</thinking>
<sql>the query</sql>
<answer>only used when <sql> is empty</answer>

SCHEMA:
{schema}"""

FIX_PROMPT = """That query failed with this error:
{error}

Return a corrected query in the same format."""

ANSWER_SYSTEM = """You are DataMaster. Explain query results to a non-technical business user.
- Answer the question directly in the first sentence, using the actual numbers.
- Plain English, no SQL jargon. Keep it short (2-5 sentences) unless the data needs more.
- If the result was truncated, say the answer is based on the first rows shown.
- If the result is empty, say so, suggest why, and suggest a question that would return data.
- Never invent numbers that are not in the result."""


class LLM(Protocol):
    def chat(self, system: str, messages: list[dict]) -> str: ...


class ClaudeLLM:
    def __init__(self, model: str, max_tokens: int = 2000):
        import anthropic  # imported lazily so tests don't need it configured

        self.client = anthropic.Anthropic()
        self.model = model
        self.max_tokens = max_tokens

    def chat(self, system: str, messages: list[dict]) -> str:
        resp = self.client.messages.create(
            model=self.model, max_tokens=self.max_tokens, system=system, messages=messages
        )
        return "".join(b.text for b in resp.content if b.type == "text")


def extract_tag(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    if tag == "sql":  # tolerate a fenced code block instead of tags
        m = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""


def strip_tags(text: str) -> str:
    """Fallback when the model ignored the format: drop <thinking> and any tags, keep the prose."""
    text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL | re.IGNORECASE)
    return re.sub(r"</?\w+>", "", text).strip()
