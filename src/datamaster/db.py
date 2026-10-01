"""Database access: schema introspection and safe, read-only query execution."""

import re

import pandas as pd
from sqlalchemy import URL, create_engine, inspect, literal_column, make_url, select, table, text
from sqlalchemy.engine import Engine

# Label -> (SQLAlchemy driver, default port, pip extra, form kind)
#   kind "server": host/port/database/user/password · "file": a local file path
#   "url": warehouses with their own auth; the user pastes a URL (see URL_TEMPLATES)
DB_TYPES = {
    "PostgreSQL": ("postgresql+psycopg2", 5432, "postgres", "server"),
    "MySQL / MariaDB": ("mysql+pymysql", 3306, "mysql", "server"),
    "SQL Server / Azure SQL": ("mssql+pymssql", 1433, "mssql", "server"),
    "Oracle": ("oracle+oracledb", 1521, "oracle", "server"),
    "Amazon Redshift": ("redshift+redshift_connector", 5439, "redshift", "server"),
    "Snowflake": ("snowflake", None, "snowflake", "url"),
    "Google BigQuery": ("bigquery", None, "bigquery", "url"),
    "Databricks": ("databricks", None, "databricks", "url"),
    "DuckDB (file)": ("duckdb", None, "duckdb", "file"),
    "SQLite (file)": ("sqlite", None, None, "file"),
}

URL_TEMPLATES = {
    "Snowflake": "snowflake://USER:PASSWORD@ACCOUNT/DATABASE/SCHEMA?warehouse=WH&role=ROLE",
    "Google BigQuery": "bigquery://PROJECT_ID/DATASET?credentials_path=C:/path/service-account.json",
    "Databricks": "databricks://token:ACCESS_TOKEN@HOST?http_path=/sql/1.0/warehouses/ID&catalog=CATALOG&schema=SCHEMA",
}


def build_url(db_type: str, host: str = "", port: int | None = None, database: str = "",
              user: str = "", password: str = "") -> URL:
    """Build a connection URL from form fields. URL.create escapes special characters in passwords.
    For Oracle, `database` is the service name."""
    driver, default_port, _, kind = DB_TYPES[db_type]
    if kind == "file":
        return URL.create(driver, database=database)
    if kind != "server":
        raise ValueError(f"{db_type} connects with a URL, not host/port fields.")
    query, db = ({"service_name": database}, None) if driver.startswith("oracle") else ({}, database or None)
    return URL.create(driver, username=user or None, password=password or None, host=host,
                      port=port or default_port, database=db, query=query)


def connect_error_hint(error: Exception, db_type: str | None = None) -> str:
    """Short, credential-free message for the UI."""
    if isinstance(error, ModuleNotFoundError) and db_type and DB_TYPES[db_type][2]:
        return f"The driver for {db_type} isn't installed. Run: pip install -e \".[{DB_TYPES[db_type][2]}]\" and restart the app."
    return str(error).splitlines()[0][:300]

# Statements we refuse to run. Defence in depth: ALSO connect with a read-only DB user.
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|"
    r"exec|execute|call|attach|detach|pragma|vacuum|replace|copy)\b",
    re.IGNORECASE,
)


class UnsafeQueryError(ValueError):
    pass


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)
    return re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)


def validate_select(sql: str) -> str:
    """Allow a single SELECT / WITH statement only. Returns the cleaned SQL."""
    cleaned = _strip_comments(sql).strip().rstrip(";").strip()
    if not cleaned:
        raise UnsafeQueryError("Empty query.")
    if ";" in cleaned:
        raise UnsafeQueryError("Only one statement is allowed.")
    if not re.match(r"^(select|with)\b", cleaned, re.IGNORECASE):
        raise UnsafeQueryError("Only SELECT queries are allowed.")
    # Ignore string literals when scanning for forbidden keywords.
    no_strings = re.sub(r"'(?:[^']|'')*'", "''", cleaned)
    bad = _FORBIDDEN.search(no_strings)
    if bad:
        raise UnsafeQueryError(f"Forbidden keyword in query: {bad.group(0).upper()}")
    return cleaned


CONNECT_TIMEOUT = 10   # seconds to wait for the server before giving up
QUERY_TIMEOUT = 60     # seconds a single query may run (where the driver supports it)
MAX_TABLES = 300       # beyond this the schema prompt gets too big; ask the user to pick schemas
MAX_SAMPLED_TABLES = 40  # sample rows cost one round-trip per table; skip them on big databases


def _timeouts(driver: str) -> dict:
    """Driver-specific connect/query timeouts, so an unreachable host fails fast."""
    if driver.startswith("postgresql"):
        # (no statement_timeout startup option: PgBouncer and some managed services reject it)
        return {"connect_timeout": CONNECT_TIMEOUT}
    if driver.startswith("redshift"):
        return {"timeout": CONNECT_TIMEOUT}
    if driver.startswith("mysql"):
        return {"connect_timeout": CONNECT_TIMEOUT, "read_timeout": QUERY_TIMEOUT}
    if driver == "mssql+pymssql":
        return {"login_timeout": CONNECT_TIMEOUT, "timeout": QUERY_TIMEOUT}
    if driver.startswith("oracle"):
        return {"tcp_connect_timeout": CONNECT_TIMEOUT}
    return {}


def _or_empty(fn, **kw) -> dict:
    """Keys are nice-to-have context; don't fail the connection if the server hides them."""
    try:
        return fn(**kw)
    except Exception:
        return {}


class Database:
    def __init__(self, url: str | URL, max_rows: int = 200, schemas: list[str] | None = None):
        url = make_url(url)
        self.engine: Engine = create_engine(url, pool_pre_ping=True, connect_args=_timeouts(url.drivername))
        self.max_rows = max_rows
        self.only_schemas = [s for s in (schemas or []) if s]
        self.table_count = 0
        self._schema_cache: str | None = None

    @property
    def dialect(self) -> str:
        return self.engine.dialect.name

    @property
    def display_name(self) -> str:
        """Connection target without the password, safe to show in the UI."""
        return self.engine.url.render_as_string(hide_password=True)

    def test(self) -> None:
        """Raise if we can't connect."""
        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    def describe_schema(self, sample_values: int = 3) -> str:
        """Compact text description of tables, columns, keys and a few sample values."""
        if self._schema_cache is not None:
            return self._schema_cache
        insp = inspect(self.engine)
        # One bulk query per schema for columns/PKs/FKs instead of ~3 round-trips per table.
        found: list[tuple[str | None, str, list, set, list]] = []
        hidden = 0
        for schema in self._schemas(insp):
            names = insp.get_table_names(schema=schema)
            if not names:
                continue
            if len(found) + len(names) > MAX_TABLES:
                names = names[: MAX_TABLES - len(found)]
            try:
                cols = insp.get_multi_columns(schema=schema, filter_names=names)
            except Exception:
                # Locked-down servers may deny the system catalogs SQLAlchemy reads
                # (e.g. Postgres pg_collation); information_schema is readable by any user.
                cols = self._columns_from_information_schema(schema, schema or insp.default_schema_name)
            pks = _or_empty(insp.get_multi_pk_constraint, schema=schema, filter_names=names)
            fks = _or_empty(insp.get_multi_foreign_keys, schema=schema, filter_names=names)
            for name in names:
                key = (schema, name)
                if not cols.get(key):  # no visible columns = this user can't read it
                    hidden += 1
                    continue
                pk = set((pks.get(key) or {}).get("constrained_columns") or [])
                found.append((schema, name, cols[key], pk, fks.get(key, [])))
            if len(found) >= MAX_TABLES:
                break

        if not found and hidden:
            where = ", ".join(self.only_schemas) or "any schema"
            raise PermissionError(
                f"Connected, but this user can't read any tables in {where} ({hidden} tables found, "
                "0 readable). Ask the database admin for USAGE/SELECT rights, or try another schema.")
        self.table_count = len(found)
        sample = sample_values and len(found) <= MAX_SAMPLED_TABLES
        lines: list[str] = []
        if len(found) >= MAX_TABLES:
            lines += [f"(Schema truncated to the first {MAX_TABLES} tables.)", ""]
        for schema, name, columns, pk, fks in found:
            lines.append(f"TABLE {schema + '.' if schema else ''}{name}")
            for col in columns:
                tag = " PK" if col["name"] in pk else ""
                lines.append(f"  - {col['name']} {col['type']}{tag}")
            for fk in fks:
                ref = f"{fk.get('referred_schema') + '.' if fk.get('referred_schema') else ''}{fk['referred_table']}"
                lines.append(f"  FK ({', '.join(fk['constrained_columns'])}) -> {ref}({', '.join(fk['referred_columns'])})")
            if sample:
                lines.extend(self._samples(schema, name, sample_values))
            lines.append("")
        self._schema_cache = "\n".join(lines).strip()
        return self._schema_cache

    def _columns_from_information_schema(self, schema: str | None, resolved: str | None) -> dict:
        """{(schema, table): [{"name", "type"}]} — same shape as Inspector.get_multi_columns.
        `resolved` is the real schema name to query (the default schema when `schema` is None)."""
        sql = text("SELECT table_name, column_name, data_type FROM information_schema.columns "
                   "WHERE table_schema = :s ORDER BY table_name, ordinal_position")
        if self.dialect == "duckdb" and resolved and "." in resolved:
            resolved = resolved.split(".")[-1]  # duckdb lists schemas as catalog.schema
        out: dict = {}
        with self.engine.connect() as conn:
            for tname, cname, dtype in conn.execute(sql, {"s": resolved}):
                out.setdefault((schema, tname), []).append({"name": cname, "type": dtype})
        return out

    def _schemas(self, insp) -> list[str | None]:
        if self.dialect == "sqlite":
            return [None]
        if self.only_schemas:
            return self.only_schemas
        skip = {"information_schema", "pg_catalog", "sys", "INFORMATION_SCHEMA", "guest"}
        names = [s for s in insp.get_schema_names() if s not in skip and not s.startswith(("pg_", "db_"))]
        return names or [None]

    def _samples(self, schema: str | None, name: str, n: int) -> list[str]:
        # Built with SQLAlchemy (always a SELECT, identifiers quoted, LIMIT/TOP per dialect),
        # so the server returns only n rows instead of the whole table.
        stmt = select(literal_column("*")).select_from(table(name, schema=schema)).limit(n)
        try:
            with self.engine.connect() as conn:
                result = conn.execute(stmt)
                df = pd.DataFrame(result.fetchall(), columns=list(result.keys()))
        except Exception:
            return []
        if df.empty:
            return ["  (empty table)"]
        return ["  sample rows:"] + [f"    {row}" for row in df.astype(str).to_dict("records")]

    def run(self, sql: str, limit: int | None = None) -> pd.DataFrame:
        """Validate then execute a SELECT, returning at most `limit` rows."""
        cleaned = validate_select(sql)
        limit = limit or self.max_rows
        # stream_results = server-side cursor where supported, so fetching `limit` rows
        # doesn't first download the whole result set (psycopg2 does that by default).
        with self.engine.connect().execution_options(stream_results=True) as conn:
            result = conn.execute(text(cleaned))
            rows = result.fetchmany(limit)
            return pd.DataFrame(rows, columns=list(result.keys()))
