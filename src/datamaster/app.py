"""Streamlit chat UI. Run with: streamlit run src/datamaster/app.py"""

import time

import streamlit as st

from datamaster.config import load_settings
from datamaster.db import DB_TYPES, URL_TEMPLATES, Database, build_url, connect_error_hint
from datamaster.engine import DataMaster
from datamaster.llm import ClaudeLLM

st.set_page_config(page_title="DataMaster", page_icon="🗄️", layout="wide")


@st.cache_resource
def get_settings():
    return load_settings(require_db=False)


@st.cache_resource
def get_llm() -> ClaudeLLM:
    return ClaudeLLM(get_settings().model)


try:
    settings, llm = get_settings(), get_llm()
except Exception as e:
    st.error(f"Setup problem: {e}")
    st.stop()

st.session_state.setdefault("chat", [])


def connect(url, db_type: str | None = None, schemas: list[str] | None = None) -> bool:
    """Open a per-session connection. Credentials live only in this session's memory."""
    with st.status("Connecting…", expanded=True) as status:
        try:
            t0 = time.perf_counter()
            db = Database(url, max_rows=settings.max_rows, schemas=schemas)
            db.test()
            st.write(f"Connected in {time.perf_counter() - t0:.1f}s. Reading schema…")
            db.describe_schema()  # fail now, not on the first question
        except Exception as e:
            status.update(label="Couldn't connect", state="error")
            st.error(connect_error_hint(e, db_type))
            return False
        status.update(label=f"Ready: {db.table_count} tables in {time.perf_counter() - t0:.1f}s",
                      state="complete")
    st.session_state.dm = DataMaster(db, llm, settings.max_retries)
    st.session_state.chat = []
    return True


# Auto-connect to DATABASE_URL from .env once, if set.
if "dm" not in st.session_state and settings.database_url and not st.session_state.get("tried_env"):
    st.session_state.tried_env = True
    connect(settings.database_url)


def connect_page() -> None:
    st.title("Connect to a database")
    st.caption("Use a **read-only** database user. Credentials are kept in memory for this "
               "browser session only. They are never saved to disk or sent to the AI model.")
    if dm := st.session_state.get("dm"):
        st.success(f"Connected: `{dm.db.display_name}`")

    db_type = st.selectbox("Database type", list(DB_TYPES))
    kind = DB_TYPES[db_type][3]
    use_url = kind == "url" or st.toggle("Paste a connection URL instead")
    with st.form("conn_form"):
        host = user = password = raw = database = ""
        port = None
        if use_url:
            if kind == "url":
                st.caption(f"Fill in this template:  \n`{URL_TEMPLATES[db_type]}`")
            raw = st.text_input("SQLAlchemy URL", type="password",
                                placeholder=URL_TEMPLATES.get(db_type, "dialect+driver://user:pass@host:port/db"))
        elif kind == "file":
            database = st.text_input("Database file path", placeholder="data/sample.db")
        else:
            c1, c2 = st.columns([3, 1])
            host = c1.text_input("Host", placeholder="myserver.example.com")
            port = c2.number_input("Port", value=DB_TYPES[db_type][1], step=1)
            database = st.text_input("Service name" if db_type == "Oracle" else "Database name")
            c3, c4 = st.columns(2)
            user = c3.text_input("Username")
            password = c4.text_input("Password", type="password")
        schemas_txt = "" if kind == "file" else st.text_input(
            "Schemas to include (optional, comma-separated)", placeholder="e.g. sales, finance",
            help="Leave empty for all schemas. On large warehouses, pick the few you need: "
                 "connecting is faster and answers are better.")
        submitted = st.form_submit_button("Connect", type="primary")

    if submitted:
        if use_url:
            if not raw.strip():
                st.warning("Please paste a connection URL.")
                return
            url = raw.strip()
        elif not database or (kind == "server" and not host):
            st.warning("Please fill in at least host and database." if kind == "server"
                       else "Please fill in the connection details.")
            return
        else:
            url = build_url(db_type, host, int(port) if port else None, database, user, password)
        schemas = [s.strip() for s in schemas_txt.split(",") if s.strip()]
        if connect(url, db_type, schemas):
            st.switch_page(chat)


def render(ans) -> None:
    st.markdown(ans.answer)
    if ans.sql:
        with st.expander("SQL used", expanded=False):
            st.code(ans.sql, language="sql")
            if ans.attempts > 1:
                st.caption(f"Fixed automatically after {ans.attempts - 1} failed attempt(s).")
    if not ans.data.empty:
        with st.expander(f"Data ({len(ans.data)} rows{', truncated' if ans.truncated else ''})"):
            st.dataframe(ans.data, use_container_width=True)
            st.download_button("Download CSV", ans.data.to_csv(index=False), "datamaster.csv",
                               key=f"dl-{id(ans)}")


def chat_page() -> None:
    dm: DataMaster | None = st.session_state.get("dm")
    if dm is None:
        st.info("Connect to a database first.")
        if st.button("Go to Connect", type="primary"):
            st.switch_page(connect_pg)
        return

    with st.sidebar:
        st.caption(f"Connected: **{dm.db.dialect}**")
        if st.button("New conversation"):
            st.session_state.chat = []
            st.rerun()
        with st.expander("Schema DataMaster sees"):
            st.code(dm.db.describe_schema(), language="text")
        st.markdown("**Try:**\n- How many customers do we have?\n- Top 5 products by revenue\n"
                    "- Monthly revenue this year\n- Which tables have a foreign key to customers?")

    for ans in st.session_state.chat:
        with st.chat_message("user"):
            st.markdown(ans.question)
        with st.chat_message("assistant"):
            render(ans)

    if q := st.chat_input("Ask anything about your data…"):
        with st.chat_message("user"):
            st.markdown(q)
        history = [(a.question, a.sql, a.answer) for a in st.session_state.chat if not a.error]
        with st.chat_message("assistant"):
            with st.spinner("Thinking…"):
                ans = dm.ask(q, history)
            render(ans)
        st.session_state.chat.append(ans)


connect_pg = st.Page(connect_page, title="Connect", icon="🔌", url_path="connect")
chat = st.Page(chat_page, title="Chat", icon="💬", url_path="chat",
               default="dm" in st.session_state)
with st.sidebar:
    st.title("DataMaster")
st.navigation([chat, connect_pg] if "dm" in st.session_state else [connect_pg, chat]).run()
