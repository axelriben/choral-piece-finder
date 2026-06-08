"""
SQLite connection helper for data/index.db.

Usage:
    from db import get_connection

    with get_connection() as conn:
        rows = conn.execute("SELECT ...").fetchall()
"""

import sqlite3
from pathlib import Path

_DB_PATH = Path(__file__).parent.parent / "data" / "index.db"

# Module-level connection — sqlite3 supports concurrent reads in the same process.
# Initialised lazily on first call to get_connection().
_conn: sqlite3.Connection | None = None


def get_connection() -> sqlite3.Connection:
    """Return a configured sqlite3 connection to data/index.db.

    The connection is cached at module level.  Rows are returned as
    sqlite3.Row objects (dict-like, accessible by column name).
    Foreign-key enforcement and WAL mode are enabled.
    """
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA foreign_keys = ON")
        _conn.execute("PRAGMA journal_mode = WAL")
    return _conn
