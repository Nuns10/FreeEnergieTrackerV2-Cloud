"""Connexion PostgreSQL/Supabase séparée de la version SQLite locale.

Ce fichier n'impose aucun changement à l'application locale. Importez-le uniquement
dans la copie destinée au cloud. La connexion est lue depuis Streamlit Secrets ou
depuis la variable d'environnement DATABASE_URL.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Sequence

import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values


def _secret(name: str, default: Any = None) -> Any:
    try:
        import streamlit as st

        return st.secrets.get(name, default)
    except Exception:
        return os.getenv(name, default)


def database_url() -> str:
    url = _secret("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL absent. Ajoutez-le dans .streamlit/secrets.toml "
            "ou dans les Secrets de Streamlit Community Cloud."
        )
    return str(url)


@contextmanager
def connect() -> Iterator[Any]:
    """Ouvre une transaction et ferme toujours la connexion proprement."""
    connection = psycopg2.connect(database_url(), connect_timeout=15)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def execute(sql: str, params: Sequence[Any] | None = None) -> int:
    """Exécute INSERT/UPDATE/DELETE et renvoie le nombre de lignes touchées."""
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.rowcount


def fetch_all(sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
    """Renvoie le résultat sous forme de liste de dictionnaires."""
    with connect() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]


def fetch_one(sql: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
    rows = fetch_all(sql, params)
    return rows[0] if rows else None


def read_dataframe(sql: str, params: Sequence[Any] | None = None) -> pd.DataFrame:
    """Renvoie directement un DataFrame pandas."""
    return pd.DataFrame(fetch_all(sql, params))


def insert_many(table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> int:
    """Insère plusieurs lignes. Les noms de table/colonnes doivent venir du code."""
    safe_table = '"' + table.replace('"', '""') + '"'
    safe_columns = ", ".join('"' + col.replace('"', '""') + '"' for col in columns)
    values = list(rows)
    if not values:
        return 0
    sql = f"INSERT INTO {safe_table} ({safe_columns}) VALUES %s"
    with connect() as connection, connection.cursor() as cursor:
        execute_values(cursor, sql, values, page_size=500)
    return len(values)

