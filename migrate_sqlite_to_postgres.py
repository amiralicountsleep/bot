import json
import os
import sqlite3
import sys
import zipfile
from pathlib import Path

import psycopg

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise SystemExit("DATABASE_URL is required.")

if len(sys.argv) != 2:
    raise SystemExit("Usage: python migrate_sqlite_to_postgres.py database.db | backup.zip")

source = Path(sys.argv[1])
if not source.exists():
    raise SystemExit(f"File not found: {source}")

def read_sqlite(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
        return {
            t: [dict(r) for r in conn.execute(f'SELECT * FROM "{t}"').fetchall()]
            for t in tables
        }
    finally:
        conn.close()

def load_source(path):
    if path.suffix.lower() == ".db":
        return read_sqlite(path)
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            if "postgres_backup.json" in names:
                return json.loads(zf.read("postgres_backup.json").decode("utf-8"))
            if "bot_database.db" not in names:
                raise SystemExit("ZIP does not contain bot_database.db.")
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "bot_database.db"
                p.write_bytes(zf.read("bot_database.db"))
                data = read_sqlite(p)
                if "wallet.db" in names:
                    w = Path(td) / "wallet.db"
                    w.write_bytes(zf.read("wallet.db"))
                    data.update({f"wallet__{k}": v for k, v in read_sqlite(w).items()})
                return data
    raise SystemExit("Use a .db or .zip backup.")

payload = load_source(source)
conn = psycopg.connect(DATABASE_URL)
try:
    # The new bot must have started once so its schema exists.
    for table, rows in payload.items():
        # Legacy wallet tables are mapped to the new main DB names.
        if table.startswith("wallet__"):
            table = table[len("wallet__"):]
        if not rows:
            continue
        exists = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s",
            (table,),
        ).fetchone()
        if not exists:
            print(f"Skipping missing table: {table}")
            continue
        cols = list(rows[0].keys())
        col_sql = ", ".join(f'"{c}"' for c in cols)
        placeholders = ", ".join(["%s"] * len(cols))
        for row in rows:
            vals = [row.get(c) for c in cols]
            conn.execute(
                f'INSERT INTO "{table}" ({col_sql}) VALUES ({placeholders}) ON CONFLICT DO NOTHING',
                vals,
            )
        print(f"Imported {len(rows)} rows into {table}")

    identity_rows = conn.execute("""
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema='public' AND is_identity='YES'
    """).fetchall()
    for ident in identity_rows:
        table = ident[0]
        column = ident[1]
        max_id = conn.execute(
            f'SELECT COALESCE(MAX("{column}"), 0) FROM "{table}"'
        ).fetchone()[0]
        seq = conn.execute(
            "SELECT pg_get_serial_sequence(%s, %s)",
            (f"public.{table}", column),
        ).fetchone()[0]
        if seq:
            if int(max_id or 0) > 0:
                conn.execute("SELECT setval(%s, %s, true)", (seq, int(max_id)))
            else:
                conn.execute("SELECT setval(%s, 1, false)", (seq,))
    conn.commit()
finally:
    conn.close()
print("Migration completed.")
