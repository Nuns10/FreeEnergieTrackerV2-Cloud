#!/usr/bin/env python3
"""Restaure Supabase dans un SQLite temporaire avant la collecte."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import psycopg2
from psycopg2 import sql

TARGET = Path(__file__).resolve().parent / "data" / "leads.sqlite"


def main() -> None:
    # Crée d'abord les schémas SQLite avec leurs clés et contraintes.
    from database import migrate_database
    from crm_sync import ensure_call_events_table
    from calendar_sync import ensure_tables

    migrate_database()
    ensure_call_events_table()
    ensure_tables()

    url = os.environ["DATABASE_URL"]
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    local = sqlite3.connect(TARGET)
    remote = psycopg2.connect(url, connect_timeout=20)
    try:
        with remote.cursor() as cursor:
            cursor.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name"
            )
            tables = [row[0] for row in cursor.fetchall()]
            for table in tables:
                cursor.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
                    (table,),
                )
                columns = [row[0] for row in cursor.fetchall()]
                if not columns:
                    continue
                quoted_cols = ", ".join(f'"{c.replace(chr(34), chr(34)*2)}"' for c in columns)
                quoted_table = table.replace('"', '""')
                local.execute(f'CREATE TABLE IF NOT EXISTS "{quoted_table}" ({quoted_cols})')
                local.execute(f'DELETE FROM "{quoted_table}"')
                cursor.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table)))
                rows = cursor.fetchall()
                if rows:
                    placeholders = ",".join("?" for _ in columns)
                    local.executemany(
                        f'INSERT INTO "{quoted_table}" VALUES ({placeholders})', rows
                    )
                local.commit()
                print(f"Restauration {table}: {len(rows)} ligne(s)")
    finally:
        remote.close()
        local.close()


if __name__ == "__main__":
    main()
