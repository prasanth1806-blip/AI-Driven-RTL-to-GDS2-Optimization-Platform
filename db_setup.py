"""Create the SQLite schema the flow records runs into.

Idempotent: safe to run against an existing veriopt.db. scripts/optimize.py
creates the designs table itself if it is missing, so the flow works without
this script having been run -- but running it up front means the schema exists
before the first analysis rather than being created as a side effect.
"""
import sqlite3
import sys

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "veriopt.db"

SCHEMA = [
    # One row per analysis run. `suggestions` holds the ranked headlines from
    # scripts/optimize.py, so the history shows how the advice changed as the
    # design was worked on.
    """
    CREATE TABLE IF NOT EXISTS designs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT,
        file_path   TEXT,
        status      TEXT,
        suggestions TEXT,
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS designs_name_idx ON designs (name)",
    "CREATE INDEX IF NOT EXISTS designs_created_idx ON designs (created_at)",
]


def main():
    connection = sqlite3.connect(DB_PATH)
    for statement in SCHEMA:
        connection.execute(statement)
    connection.commit()

    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    rows = connection.execute("SELECT COUNT(*) FROM designs").fetchone()[0]
    connection.close()

    print(f"{DB_PATH}: tables {', '.join(tables)}; designs holds {rows} row(s).")


if __name__ == "__main__":
    main()
