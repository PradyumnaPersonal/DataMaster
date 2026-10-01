"""Terminal interface: `datamaster "question"` or `datamaster` for an interactive session."""

import argparse
import sys

from .engine import DataMaster


def _print(ans) -> None:
    print(f"\n{ans.answer}\n")
    if ans.sql:
        print("SQL used:\n" + ans.sql + "\n")
    if not ans.data.empty:
        print(ans.data.head(20).to_string(index=False))
        if len(ans.data) > 20:
            print(f"... ({len(ans.data)} rows{', truncated' if ans.truncated else ''})")
    print()


def main() -> None:
    p = argparse.ArgumentParser(prog="datamaster", description="Ask your database in plain English.")
    p.add_argument("question", nargs="*", help="question to ask (omit for interactive mode)")
    p.add_argument("--schema", action="store_true", help="print the schema DataMaster sees and exit")
    args = p.parse_args()

    dm = DataMaster.from_settings()
    if args.schema:
        print(dm.db.describe_schema())
        return
    if args.question:
        _print(dm.ask(" ".join(args.question)))
        return

    history: list[tuple[str, str, str]] = []
    print("DataMaster — ask anything about your data. Ctrl+C or 'exit' to quit.")
    while True:
        try:
            q = input("> ").strip()
        except (KeyboardInterrupt, EOFError):
            sys.exit(0)
        if q.lower() in {"exit", "quit"}:
            break
        if q:
            ans = dm.ask(q, history)
            _print(ans)
            if not ans.error:
                history.append((q, ans.sql, ans.answer))


if __name__ == "__main__":
    main()
