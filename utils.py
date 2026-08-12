
from __future__ import annotations

import re
import unicodedata

import pandas as pd


ALIASES = {
    "cree_le": "date_creation",
    "date_creation": "date_creation",
    "nom": "nom",
    "prenom": "prenom",
    "code_postal": "code_postal",
    "ville": "ville",
    "statut": "statut",
    "status": "statut",
    "status_le": "date_statut",
    "date_statut": "date_statut",
    "source": "source",
    "intervenant": "intervenant",
    "telephone": "telephone",
    "portable": "telephone",
    "nombre_nrp": "nombre_nrp",
    "nombre_de_nrp": "nombre_nrp",
    "nrp": "nombre_nrp",
    "dernier_appel": "dernier_appel",
    "crm_id": "crm_id",
    # IMPORTANT :
    # La colonne SQLite "id" reste "id".
    # On ne la renomme plus en "crm_id", sinon doublon.
    "id": "id",
    "agence": "agence",
    "type_lead": "type_lead",
    "synced_at": "synced_at",
}


def slug(value: object) -> str:
    value = unicodedata.normalize("NFKD", str(value))
    value = "".join(
        character
        for character in value
        if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()


def make_columns_unique(df: pd.DataFrame) -> pd.DataFrame:
    """
    Garantit des noms de colonnes uniques.

    Si deux colonnes portent encore le même nom après normalisation,
    la première est conservée sous son nom d'origine et les suivantes
    reçoivent un suffixe.
    """
    counts: dict[str, int] = {}
    unique_columns: list[str] = []

    for column in df.columns:
        name = str(column)
        count = counts.get(name, 0)

        if count == 0:
            unique_columns.append(name)
        else:
            unique_columns.append(f"{name}_{count + 1}")

        counts[name] = count + 1

    result = df.copy()
    result.columns = unique_columns
    return result


def clean_dataframe(df: pd.DataFrame | None) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    renamed = {
        column: ALIASES.get(slug(column), slug(column))
        for column in df.columns
    }

    cleaned = df.rename(columns=renamed).copy()
    cleaned = make_columns_unique(cleaned)

    if "statut" in cleaned.columns:
        cleaned["statut"] = (
            cleaned["statut"]
            .fillna("")
            .astype(str)
            .str.upper()
            .str.replace("À", "A", regex=False)
            .str.replace(r"\s+", " ", regex=True)
            .str.strip()
        )

    for column in (
        "date_creation",
        "date_statut",
        "dernier_appel",
        "synced_at",
    ):
        if column in cleaned.columns:
            cleaned[column] = pd.to_datetime(
                cleaned[column],
                errors="coerce",
                dayfirst=True,
            )

    if "nombre_nrp" in cleaned.columns:
        cleaned["nombre_nrp"] = (
            pd.to_numeric(
                cleaned["nombre_nrp"],
                errors="coerce",
            )
            .fillna(0)
            .astype(int)
        )

    return cleaned


def read_uploaded_file(uploaded_file) -> pd.DataFrame:
    extension = uploaded_file.name.rsplit(".", 1)[-1].lower()

    if extension == "csv":
        try:
            dataframe = pd.read_csv(
                uploaded_file,
                sep=None,
                engine="python",
            )
        except UnicodeDecodeError:
            uploaded_file.seek(0)
            dataframe = pd.read_csv(
                uploaded_file,
                sep=None,
                engine="python",
                encoding="latin-1",
            )
    elif extension in {"xlsx", "xls"}:
        dataframe = pd.read_excel(uploaded_file)
    else:
        raise ValueError("Format non pris en charge.")

    return clean_dataframe(dataframe)
