"""Accès aux données de la version cloud (Supabase PostgreSQL)."""

from __future__ import annotations

from typing import Any

import pandas as pd
import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor, execute_values

from database_cloud import connect


COLUMNS = [
    "crm_id", "date_creation", "nom", "prenom", "code_postal", "ville",
    "statut", "date_statut", "source", "intervenant", "telephone",
    "nombre_nrp", "dernier_appel", "agence", "type_lead",
]


def table_exists(table_name: str) -> bool:
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = %s)",
            (table_name,),
        )
        return bool(cursor.fetchone()[0])


def read_table(table_name: str) -> pd.DataFrame:
    """Lit une table publique; un nom absent renvoie un DataFrame vide."""
    if not table_exists(table_name):
        return pd.DataFrame()
    with connect() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table_name)))
        return pd.DataFrame([dict(row) for row in cursor.fetchall()])


def load_leads() -> pd.DataFrame:
    if not table_exists("leads"):
        return pd.DataFrame()
    with connect() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute('SELECT * FROM "leads" ORDER BY date_creation DESC NULLS LAST, id DESC')
        return pd.DataFrame([dict(row) for row in cursor.fetchall()])


def _normalise(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def _fallback(row: dict[str, Any], number: int) -> str:
    parts = [row.get(key) for key in ("telephone", "nom", "prenom", "date_creation", "code_postal")]
    signature = "|".join(str(value or "").strip() for value in parts)
    return f"fallback:{signature}" if signature.replace("|", "").strip() else f"fallback:row:{number}"


def upsert(frame: pd.DataFrame) -> None:
    """Met à jour/insère les leads sans imposer de contrainte UNIQUE."""
    if frame is None or frame.empty:
        return
    clean = frame.copy()
    for column in COLUMNS:
        if column not in clean.columns:
            clean[column] = None

    update_columns = [column for column in COLUMNS if column != "crm_id"]
    update_statement = sql.SQL("UPDATE leads SET {}, synced_at = CURRENT_TIMESTAMP WHERE crm_id = %s").format(
        sql.SQL(", ").join(
            sql.SQL("{} = %s").format(sql.Identifier(column)) for column in update_columns
        )
    )
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT COALESCE(MAX(id), 0) FROM leads")
        next_id = int(cursor.fetchone()[0]) + 1
        insert_columns = ["id", *COLUMNS]
        insert_statement = sql.SQL("INSERT INTO leads ({}, synced_at) VALUES %s").format(
            sql.SQL(", ").join(map(sql.Identifier, insert_columns))
        )
        inserts = []
        for number, row in enumerate(clean[COLUMNS].to_dict("records"), start=1):
            crm_id = _normalise(row.get("crm_id"))
            row["crm_id"] = str(crm_id).strip() if crm_id and str(crm_id).strip() else _fallback(row, number)
            values = [_normalise(row[column]) for column in update_columns] + [row["crm_id"]]
            cursor.execute(update_statement, values)
            if cursor.rowcount == 0:
                inserts.append((next_id, *(_normalise(row[column]) for column in COLUMNS)))
                next_id += 1
        if inserts:
            execute_values(cursor, insert_statement.as_string(connection), inserts, page_size=500)
