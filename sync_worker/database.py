
from __future__ import annotations

from pathlib import Path
import shutil
import sqlite3
from datetime import datetime

import pandas as pd


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "leads.sqlite"
DATA_DIR.mkdir(exist_ok=True)

COLUMNS = [
    "crm_id",
    "date_creation",
    "nom",
    "prenom",
    "code_postal",
    "ville",
    "statut",
    "date_statut",
    "source",
    "intervenant",
    "telephone",
    "nombre_nrp",
    "dernier_appel",
    "agence",
    "type_lead",
]

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crm_id TEXT,
    date_creation TEXT,
    nom TEXT,
    prenom TEXT,
    code_postal TEXT,
    ville TEXT,
    statut TEXT,
    date_statut TEXT,
    source TEXT,
    intervenant TEXT,
    telephone TEXT,
    nombre_nrp INTEGER DEFAULT 0,
    dernier_appel TEXT,
    agence TEXT,
    type_lead TEXT,
    synced_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def _connect_raw() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone() is not None


def _get_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    return [
        row[1]
        for row in conn.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    ]


def _backup_once() -> None:
    if not DB_PATH.exists():
        return

    existing = list(DATA_DIR.glob("leads_backup_compatible_*.sqlite"))
    if existing:
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = DATA_DIR / f"leads_backup_compatible_{stamp}.sqlite"
    shutil.copy2(DB_PATH, backup)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    existing = set(_get_columns(conn, "leads"))

    definitions = {
        "crm_id": "TEXT",
        "date_creation": "TEXT",
        "nom": "TEXT",
        "prenom": "TEXT",
        "code_postal": "TEXT",
        "ville": "TEXT",
        "statut": "TEXT",
        "date_statut": "TEXT",
        "source": "TEXT",
        "intervenant": "TEXT",
        "telephone": "TEXT",
        "nombre_nrp": "INTEGER DEFAULT 0",
        "dernier_appel": "TEXT",
        "agence": "TEXT",
        "type_lead": "TEXT",
        "synced_at": "TEXT",
    }

    for name, definition in definitions.items():
        if name not in existing:
            conn.execute(
                f"ALTER TABLE leads ADD COLUMN {name} {definition}"
            )

    conn.execute(
        """
        UPDATE leads
        SET synced_at = CURRENT_TIMESTAMP
        WHERE synced_at IS NULL
        """
    )


def migrate_database() -> None:
    _backup_once()

    with _connect_raw() as conn:
        conn.execute(CREATE_TABLE_SQL)
        _add_missing_columns(conn)

        # Les anciennes bases peuvent contenir des crm_id vides.
        # On les transforme en NULL pour éviter les faux doublons.
        conn.execute(
            """
            UPDATE leads
            SET crm_id = NULL
            WHERE crm_id IS NOT NULL
              AND TRIM(crm_id) = ''
            """
        )

        # Conserve la ligne la plus récente en cas de doublon existant.
        conn.execute(
            """
            DELETE FROM leads
            WHERE crm_id IS NOT NULL
              AND id NOT IN (
                  SELECT MAX(id)
                  FROM leads
                  WHERE crm_id IS NOT NULL
                  GROUP BY crm_id
              )
            """
        )

        # Index classiques uniquement. L'upsert ci-dessous ne dépend
        # volontairement d'aucune contrainte UNIQUE.
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_leads_crm_id
            ON leads(crm_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_leads_date_creation
            ON leads(date_creation)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_leads_intervenant
            ON leads(intervenant)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_leads_source
            ON leads(source)
            """
        )
        conn.commit()


def connect() -> sqlite3.Connection:
    migrate_database()
    return _connect_raw()


def load_leads() -> pd.DataFrame:
    with connect() as conn:
        return pd.read_sql_query(
            """
            SELECT *
            FROM leads
            ORDER BY date_creation DESC, id DESC
            """,
            conn,
        )


def _normalise_value(value):
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


def _generate_fallback_crm_id(row: dict, row_number: int) -> str:
    parts = [
        str(row.get("telephone") or "").strip(),
        str(row.get("nom") or "").strip(),
        str(row.get("prenom") or "").strip(),
        str(row.get("date_creation") or "").strip(),
        str(row.get("code_postal") or "").strip(),
    ]

    signature = "|".join(parts)

    if signature.replace("|", "").strip():
        return f"fallback:{signature}"

    return f"fallback:row:{row_number}"


def upsert(df: pd.DataFrame) -> None:
    """
    Met à jour puis insère les leads.

    Cette méthode n'utilise volontairement PAS `ON CONFLICT`, afin de
    fonctionner avec les anciennes bases dont crm_id n'est ni PRIMARY KEY
    ni UNIQUE.
    """
    if df is None or df.empty:
        return

    clean = df.copy()

    for column in COLUMNS:
        if column not in clean.columns:
            clean[column] = None

    records = clean[COLUMNS].to_dict("records")

    update_columns = [
        column
        for column in COLUMNS
        if column != "crm_id"
    ]

    update_assignments = ", ".join(
        f"{column} = ?"
        for column in update_columns
    )

    update_query = f"""
        UPDATE leads
        SET {update_assignments},
            synced_at = CURRENT_TIMESTAMP
        WHERE crm_id = ?
    """

    insert_placeholders = ",".join("?" for _ in COLUMNS)

    insert_query = f"""
        INSERT INTO leads ({",".join(COLUMNS)}, synced_at)
        VALUES ({insert_placeholders}, CURRENT_TIMESTAMP)
    """

    with connect() as conn:
        for row_number, row in enumerate(records, start=1):
            crm_id = _normalise_value(row.get("crm_id"))

            if crm_id is None or not str(crm_id).strip():
                crm_id = _generate_fallback_crm_id(
                    row,
                    row_number,
                )

            row["crm_id"] = str(crm_id).strip()

            update_values = [
                _normalise_value(row[column])
                for column in update_columns
            ]
            update_values.append(row["crm_id"])

            cursor = conn.execute(
                update_query,
                update_values,
            )

            if cursor.rowcount == 0:
                insert_values = [
                    _normalise_value(row[column])
                    for column in COLUMNS
                ]
                conn.execute(
                    insert_query,
                    insert_values,
                )

        conn.commit()


migrate_database()
