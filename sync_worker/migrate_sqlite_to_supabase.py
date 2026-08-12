#!/usr/bin/env python3
"""Copie toutes les tables d'un SQLite vers Supabase PostgreSQL.

Le script ne modifie jamais le fichier SQLite source. Par sécurité, il refuse
d'écraser une table Supabase non vide sans l'option --replace.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values


TYPE_MAP = {
    "INT": "BIGINT",
    "CHAR": "TEXT",
    "CLOB": "TEXT",
    "TEXT": "TEXT",
    "BLOB": "BYTEA",
    "REAL": "DOUBLE PRECISION",
    "FLOA": "DOUBLE PRECISION",
    "DOUB": "DOUBLE PRECISION",
    "NUM": "NUMERIC",
    "DEC": "NUMERIC",
    "BOOL": "BOOLEAN",
    "DATE": "TEXT",
    "TIME": "TEXT",
}


def pg_type(sqlite_type: str | None) -> str:
    value = (sqlite_type or "TEXT").upper()
    for marker, target in TYPE_MAP.items():
        if marker in value:
            return target
    return "TEXT"


def database_url() -> str:
    value = os.getenv("DATABASE_URL", "").strip()
    if value:
        return value
    try:
        import tomllib

        secret_file = Path(".streamlit/secrets.toml")
        if secret_file.exists():
            return str(tomllib.loads(secret_file.read_text())["DATABASE_URL"])
    except (KeyError, OSError, ValueError):
        pass
    raise SystemExit("DATABASE_URL introuvable. Consultez le README.")


def table_names(source: sqlite3.Connection) -> list[str]:
    rows = source.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def migrate_table(
    source: sqlite3.Connection, target: Any, table: str, replace: bool
) -> int:
    columns = source.execute(f'PRAGMA table_info("{table.replace(chr(34), chr(34) * 2)}")').fetchall()
    if not columns:
        return 0

    definitions = []
    primary_keys = []
    for _cid, name, declared_type, not_null, default, primary_key in columns:
        definition = sql.SQL("{} {}").format(sql.Identifier(name), sql.SQL(pg_type(declared_type)))
        if not_null:
            definition += sql.SQL(" NOT NULL")
        if default is not None:
            definition += sql.SQL(" DEFAULT ") + sql.SQL(str(default))
        definitions.append(definition)
        if primary_key:
            primary_keys.append((primary_key, name))
    if primary_keys:
        ordered = [name for _, name in sorted(primary_keys)]
        definitions.append(
            sql.SQL("PRIMARY KEY ({})").format(
                sql.SQL(", ").join(map(sql.Identifier, ordered))
            )
        )

    with target.cursor() as cursor:
        cursor.execute(
            sql.SQL("CREATE TABLE IF NOT EXISTS {} ({})").format(
                sql.Identifier(table), sql.SQL(", ").join(definitions)
            )
        )
        # Les tables Supabase peuvent déjà exister avec un ancien schéma.
        # Ajoute sans risque les nouvelles colonnes produites par le collecteur
        # (par exemple color_hex et appointment_status) avant l'insertion.
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s",
            (table,),
        )
        target_columns = {row[0] for row in cursor.fetchall()}
        for _cid, name, declared_type, _not_null, _default, _primary_key in columns:
            if name not in target_columns:
                cursor.execute(
                    sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} {}").format(
                        sql.Identifier(table),
                        sql.Identifier(name),
                        sql.SQL(pg_type(declared_type)),
                    )
                )
        cursor.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table)))
        existing = cursor.fetchone()[0]
        if existing and not replace:
            raise RuntimeError(
                f"La table {table!r} contient déjà {existing} ligne(s). "
                "Relancez avec --replace uniquement si vous voulez les remplacer."
            )
        if replace:
            cursor.execute(sql.SQL("TRUNCATE TABLE {}").format(sql.Identifier(table)))

        names = [column[1] for column in columns]
        quoted = table.replace('"', '""')
        rows = source.execute(f'SELECT * FROM "{quoted}"').fetchall()
        if rows:
            statement = sql.SQL("INSERT INTO {} ({}) VALUES %s").format(
                sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, names))
            )
            execute_values(cursor, statement.as_string(target), rows, page_size=500)
        return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Migration SQLite vers Supabase")
    parser.add_argument("sqlite", type=Path, help="Chemin du fichier SQLite local")
    parser.add_argument(
        "--replace", action="store_true", help="Vide les tables Supabase avant la copie"
    )
    args = parser.parse_args()
    if not args.sqlite.is_file():
        raise SystemExit(f"Fichier introuvable : {args.sqlite}")

    source = sqlite3.connect(f"file:{args.sqlite.resolve()}?mode=ro", uri=True)
    target = psycopg2.connect(database_url(), connect_timeout=15)
    try:
        for table in table_names(source):
            count = migrate_table(source, target, table, args.replace)
            target.commit()
            print(f"OK  {table}: {count} ligne(s)")
        print("Migration terminée avec succès.")
    except Exception:
        target.rollback()
        raise
    finally:
        source.close()
        target.close()


if __name__ == "__main__":
    main()
