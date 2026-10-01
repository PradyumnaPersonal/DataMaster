import sqlite3

import pytest
from sqlalchemy import event

import datamaster.db as dbmod
from datamaster.db import Database, UnsafeQueryError, build_url, connect_error_hint, validate_select
from datamaster.engine import DataMaster


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, country TEXT);
        INSERT INTO customers VALUES (1,'Anna','NL'),(2,'Ravi','IN'),(3,'Jan','NL');
    """)
    con.commit()
    con.close()
    return Database(f"sqlite:///{path}", max_rows=2)


class FakeLLM:
    """Returns scripted replies in order and records what it was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, system, messages):
        self.calls.append((system, messages))
        return self.replies.pop(0)


# --- safety ---------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT * FROM customers",
    "with x as (select 1) select * from x;",
    "SELECT name FROM customers WHERE name = 'drop table'",
    "-- comment\nSELECT 1",
])
def test_allows_selects(sql):
    validate_select(sql)


@pytest.mark.parametrize("sql", [
    "DELETE FROM customers",
    "SELECT 1; DROP TABLE customers",
    "UPDATE customers SET name='x'",
    "WITH d AS (DELETE FROM customers RETURNING *) SELECT * FROM d",
    "PRAGMA table_info(customers)",
    "",
])
def test_blocks_writes(sql):
    with pytest.raises(UnsafeQueryError):
        validate_select(sql)


# --- db -------------------------------------------------------------------

def test_schema_lists_tables_and_columns(db):
    s = db.describe_schema()
    assert "TABLE customers" in s and "country" in s and "PK" in s


def test_schema_samples_use_limit_not_full_scan(db):
    sent = []
    event.listen(db.engine, "before_cursor_execute", lambda c, cur, stmt, *a: sent.append(stmt))
    assert "sample rows" in db.describe_schema()
    samples = [s for s in sent if s.upper().startswith("SELECT *")]
    assert samples and all("LIMIT" in s.upper() for s in samples)


def test_schema_skips_samples_on_large_databases(tmp_path, monkeypatch):
    path = tmp_path / "big.db"
    con = sqlite3.connect(path)
    con.executescript("".join(f"CREATE TABLE t{i} (id INTEGER PRIMARY KEY);" for i in range(5)))
    con.close()
    monkeypatch.setattr(dbmod, "MAX_SAMPLED_TABLES", 3)
    d = Database(f"sqlite:///{path}")
    s = d.describe_schema()
    assert d.table_count == 5 and "TABLE t4" in s and "sample rows" not in s


def test_schema_filter_only_includes_chosen_schemas(tmp_path):
    pytest.importorskip("duckdb_engine")
    d = Database(f"duckdb:///{tmp_path / 's.duckdb'}")
    with d.engine.begin() as conn:
        for s in ("sales", "hr"):
            conn.exec_driver_sql(f"CREATE SCHEMA {s}; CREATE TABLE {s}.t_{s} (id INTEGER)")
    url = d.engine.url
    d.engine.dispose()
    s = Database(url, schemas=["sales"]).describe_schema()
    assert "TABLE sales.t_sales" in s and "t_hr" not in s


def test_falls_back_to_information_schema_when_catalog_is_denied(tmp_path, monkeypatch):
    pytest.importorskip("duckdb_engine")
    from sqlalchemy.engine.reflection import Inspector

    def denied(*a, **kw):
        raise PermissionError("permission denied for table pg_collation")

    d = Database(f"duckdb:///{tmp_path / 'locked.duckdb'}")
    with d.engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE orders (id INTEGER, amount DOUBLE)")
    monkeypatch.setattr(Inspector, "get_multi_columns", denied)
    s = d.describe_schema()
    assert "orders" in s and "amount" in s


def test_clear_error_when_no_table_is_readable(tmp_path, monkeypatch):
    pytest.importorskip("duckdb_engine")
    d = Database(f"duckdb:///{tmp_path / 'noperm.duckdb'}")
    with d.engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE secret (id INTEGER)")
    monkeypatch.setattr(Database, "_columns_from_information_schema", lambda self, s, r: {})
    from sqlalchemy.engine.reflection import Inspector
    monkeypatch.setattr(Inspector, "get_multi_columns", lambda *a, **k: {})
    with pytest.raises(PermissionError, match="0 readable"):
        d.describe_schema()


def test_run_caps_rows(db):
    assert len(db.run("SELECT * FROM customers")) == 2


def test_build_url_escapes_password_and_hides_it():
    url = build_url("PostgreSQL", "db.local", None, "sales", "reader", "p@ss:w/rd")
    assert url.port == 5432 and url.password == "p@ss:w/rd"  # special chars survive intact
    assert "p@ss" not in url.render_as_string(hide_password=True)


def test_build_url_sqlite_connects(db, tmp_path):
    d = Database(build_url("SQLite (file)", database=str(tmp_path / "t.db")))
    d.test()
    assert "customers" in d.describe_schema()


def test_build_url_oracle_uses_service_name():
    url = build_url("Oracle", "ora.local", None, "ORCLPDB1", "reader", "x")
    assert url.drivername == "oracle+oracledb" and url.port == 1521
    assert url.query["service_name"] == "ORCLPDB1" and url.database is None


def test_build_url_rejects_url_only_warehouses():
    with pytest.raises(ValueError):
        build_url("Snowflake", "acct")


def test_missing_driver_hint_names_the_extra():
    assert "[mysql]" in connect_error_hint(ModuleNotFoundError("pymysql"), "MySQL / MariaDB")


# --- engine ---------------------------------------------------------------

def test_happy_path(db):
    llm = FakeLLM("<sql>SELECT country, COUNT(*) AS n FROM customers GROUP BY country</sql>",
                  "Most customers are in NL (2).")
    ans = DataMaster(db, llm).ask("Where are customers?")
    assert ans.answer == "Most customers are in NL (2)."
    assert "GROUP BY" in ans.sql and len(ans.data) == 2 and ans.attempts == 1
    assert "NL" in llm.calls[1][1][0]["content"]  # results were passed to the explainer


def test_self_repairs_bad_sql(db):
    llm = FakeLLM("<sql>SELECT nme FROM customers</sql>",
                  "<sql>SELECT name FROM customers</sql>",
                  "There are customers.")
    ans = DataMaster(db, llm).ask("names?")
    assert ans.attempts == 2 and ans.sql == "SELECT name FROM customers"
    assert "nme" in llm.calls[1][1][-1]["content"]  # error was fed back


def test_gives_up_after_retries(db):
    llm = FakeLLM(*["<sql>SELECT bad FROM nowhere</sql>"] * 3)
    ans = DataMaster(db, llm, max_retries=2).ask("?")
    assert ans.error and ans.attempts == 3


def test_refuses_unsafe_generated_sql(db):
    ans = DataMaster(db, FakeLLM("<sql>DELETE FROM customers</sql>")).ask("clean up")
    assert "Refused" in ans.answer
    assert len(db.run("SELECT * FROM customers", limit=10)) == 3


def test_unanswerable_question(db):
    ans = DataMaster(db, FakeLLM("<sql></sql><answer>No weather data here.</answer>")).ask("weather?")
    assert ans.answer == "No weather data here." and not ans.sql


def test_history_is_sent_for_follow_ups(db):
    llm = FakeLLM("<sql>SELECT 1 AS x</sql>", "ok")
    DataMaster(db, llm).ask("and by month?", history=[("revenue?", "SELECT 2")])
    msgs = llm.calls[0][1]
    assert msgs[0]["content"] == "revenue?" and "SELECT 2" in msgs[1]["content"]


def test_previous_answers_are_sent_for_conversational_follow_ups(db):
    llm = FakeLLM("<sql></sql><answer>Because the laptop costs the most.</answer>")
    ans = DataMaster(db, llm).ask("why?", history=[("top product?", "SELECT 2", "Laptop leads.")])
    assert "Laptop leads." in llm.calls[0][1][1]["content"]  # model sees what it told the user
    assert ans.answer == "Because the laptop costs the most." and not ans.sql
    assert len(llm.calls) == 1  # no query run, no explainer call


def test_untagged_reply_is_shown_instead_of_generic_refusal(db):
    ans = DataMaster(db, FakeLLM("<thinking>chit-chat</thinking>You're welcome!")).ask("thanks")
    assert ans.answer == "You're welcome!"
