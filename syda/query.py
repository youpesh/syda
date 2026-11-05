"""Read-only SQL over a generated dataset's CSVs ("chat with your data").

Used by the MCP server and the platform chat agent; has no MCP dependency.
"""

import os
import sqlite3
from typing import Any, Dict, Optional

import pandas as pd


# Statement types a read-only query may touch. Everything else (INSERT, ATTACH —
# which can create files on disk — PRAGMA, CREATE, ...) is denied by SQLite itself.
_READ_ONLY_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),  # WITH RECURSIVE
}


def _read_only_authorizer(action, *_args):
    return sqlite3.SQLITE_OK if action in _READ_ONLY_ACTIONS else sqlite3.SQLITE_DENY


def query_dataset(
    output_dir: str,
    sql: Optional[str] = None,
    max_rows: int = 200,
) -> Dict[str, Any]:
    """Answer questions about a generated dataset by running read-only SQL over its CSVs.

    Every <table>.csv in output_dir becomes a SQLite table named <table>. Use this
    after generate_from_schema(output_dir=...) to answer questions ("what % of
    claims were denied?") or verify quality (orphaned FKs, out-of-range values,
    duplicate keys).

    Args:
        output_dir: Directory containing the generated CSVs.
        sql:        A single SELECT / WITH query (SQLite dialect). Omit to list
                    tables, columns, and row counts first.
        max_rows:   Cap on returned rows (default 200).

    Returns:
        Without sql: ok, tables {name: {columns, row_count}}.
        With sql:    ok, columns, rows (list of dicts), row_count, truncated.
    """
    try:
        csvs = sorted(f for f in os.listdir(output_dir) if f.lower().endswith(".csv"))
        if not csvs:
            raise FileNotFoundError(f"No CSV files found in {output_dir}")

        # ponytail: reloads CSVs on every call; cache per output_dir if datasets get large.
        conn = sqlite3.connect(":memory:")
        try:
            tables = {}
            for f in csvs:
                name = os.path.splitext(f)[0]
                df = pd.read_csv(os.path.join(output_dir, f))
                df.to_sql(name, conn, index=False)
                tables[name] = {"columns": list(df.columns), "row_count": len(df)}

            if not sql:
                return {"ok": True, "tables": tables}

            conn.set_authorizer(_read_only_authorizer)
            cur = conn.execute(sql)
            columns = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(max_rows + 1)
        finally:
            conn.close()

        truncated = len(rows) > max_rows
        rows = [dict(zip(columns, r)) for r in rows[:max_rows]]
        return {
            "ok": True,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": type(exc).__name__,
            "message": str(exc),
            "suggestion": (
                "Only single read-only SELECT/WITH queries are allowed (SQLite dialect). "
                "Call query_dataset without sql to see table and column names."
            ),
        }
