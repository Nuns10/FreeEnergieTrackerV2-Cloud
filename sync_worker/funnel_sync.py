from __future__ import annotations

"""Collecte rapide du tunnel commercial depuis la liste CRM.

Contrairement à crm_sync.py, ce passage n'ouvre aucune fiche : il parcourt le
tableau complet et mémorise les colonnes utiles au pilotage de la conversion.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from cloud_browser import launch_context
from crm_sync import (
    LIST_URL,
    PROFILE_DIR,
    cell_text,
    clean,
    normalize,
    open_list,
    paginator_text,
    set_100_rows,
    status_filter_select,
    wait_for_rows,
)


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "leads.sqlite"


def ensure_table() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS lead_funnel (
                crm_id TEXT PRIMARY KEY,
                date_creation TEXT,
                nom TEXT,
                code_postal TEXT,
                ville TEXT,
                statut TEXT,
                date_statut TEXT,
                source TEXT,
                intervenant TEXT,
                telephone TEXT,
                synced_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS lead_status_history (
                event_key TEXT PRIMARY KEY,
                crm_id TEXT NOT NULL,
                statut TEXT,
                date_statut TEXT,
                source TEXT,
                intervenant TEXT,
                observed_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS ix_funnel_creation ON lead_funnel(date_creation)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS ix_funnel_source ON lead_funnel(source)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS ix_funnel_intervenant ON lead_funnel(intervenant)"
        )
        connection.commit()


def select_all_statuses(page) -> None:
    """Sélectionne tous les statuts afin d'afficher tout le tunnel.

    Dans ce CRM, retirer toutes les coches réactive le filtre mémorisé. La
    méthode fiable consiste donc à cocher explicitement chaque statut.
    """
    select = status_filter_select(page)
    if select.count() == 0:
        raise RuntimeError("Filtre Statut introuvable dans le CRM.")

    select.first.click(force=True)
    page.wait_for_timeout(500)
    # Le menu utilise une liste virtualisée : seules quelques options sont
    # présentes dans le DOM. Cliquer chacune d'elles donnait donc un faux
    # « tous les statuts » et omettait notamment SIGNÉ / DÉBALLÉ PAS SIGNÉ.
    # Le premier contrôle de recherche fournit la vraie case « tout cocher ».
    toggle_all = page.locator(
        ".cdk-overlay-pane .mat-select-search-toggle-all-checkbox, "
        ".cdk-overlay-pane ngx-mat-select-search mat-checkbox, "
        ".cdk-overlay-pane mat-checkbox[aria-label*='Select all'], "
        ".cdk-overlay-pane mat-checkbox[aria-label*='Tout']"
    ).first
    if toggle_all.count() == 0:
        page.keyboard.press("Escape")
        raise RuntimeError("Case 'tous les statuts' introuvable dans le filtre CRM.")

    classes = toggle_all.get_attribute("class") or ""
    aria_checked = toggle_all.get_attribute("aria-checked")
    checked = aria_checked == "true" or "mat-checkbox-checked" in classes
    if not checked:
        toggle_all.click(force=True)
        page.wait_for_timeout(1200)
    page.keyboard.press("Escape")
    page.wait_for_timeout(3500)
    wait_for_rows(page)
    pagination = paginator_text(page)
    print(f"Tous les statuts sont sélectionnés. Pagination : {pagination}")
    match = __import__("re").search(r"de\s+([\d\s]+)$", pagination)
    total = int(match.group(1).replace(" ", "")) if match else 0
    if total and total < 16_000:
        raise RuntimeError(
            f"Tous les statuts ne sont pas actifs ({total} leads seulement)."
        )


def row_status_any(row) -> str | None:
    cells = row.locator(
        "td.mat-column-status, td[class*='mat-column-status'], "
        "[role='gridcell'][class*='status']"
    )
    for index in range(cells.count()):
        value = clean(cells.nth(index).inner_text())
        if value:
            return value
    return None


def extract_rows(page, synced_at: str) -> list[tuple]:
    rows = page.locator("tbody tr.mat-row")
    result: list[tuple] = []
    for index in range(rows.count()):
        row = rows.nth(index)
        link = row.locator("td.mat-column-name a[href^='/lead/']").first
        if link.count() == 0:
            continue
        href = link.get_attribute("href") or ""
        crm_id = href.rstrip("/").split("/")[-1]
        if not crm_id or crm_id == "lead":
            continue
        result.append(
            (
                crm_id,
                cell_text(row, "createdAt"),
                cell_text(row, "name"),
                cell_text(row, "zip"),
                cell_text(row, "city"),
                row_status_any(row),
                cell_text(row, "statusAt"),
                cell_text(row, "source"),
                cell_text(row, "attributed_to"),
                cell_text(row, "phone"),
                synced_at,
            )
        )
    return result


def save(rows: list[tuple]) -> None:
    with sqlite3.connect(DB_PATH) as connection:
        connection.executemany(
            """
            INSERT INTO lead_funnel (
                crm_id, date_creation, nom, code_postal, ville, statut,
                date_statut, source, intervenant, telephone, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(crm_id) DO UPDATE SET
                date_creation=excluded.date_creation, nom=excluded.nom,
                code_postal=excluded.code_postal, ville=excluded.ville,
                statut=excluded.statut, date_statut=excluded.date_statut,
                source=excluded.source, intervenant=excluded.intervenant,
                telephone=excluded.telephone, synced_at=excluded.synced_at
            """,
            rows,
        )
        history_rows = []
        for row in rows:
            crm_id, _created, _name, _zip, _city, status, status_at, source, owner, _phone, observed = row
            event_key = "|".join((str(crm_id), normalize(status), clean(status_at) or observed))
            history_rows.append((event_key, crm_id, status, status_at, source, owner, observed))
        connection.executemany(
            """
            INSERT OR IGNORE INTO lead_status_history (
                event_key, crm_id, statut, date_statut, source, intervenant, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            history_rows,
        )
        connection.commit()


def next_page(page) -> bool:
    button = page.locator("button.mat-paginator-navigation-next").first
    if button.count() == 0 or button.get_attribute("disabled") is not None:
        return False
    if "mat-button-disabled" in (button.get_attribute("class") or ""):
        return False
    before = paginator_text(page)
    button.click(force=True)
    for _ in range(40):
        page.wait_for_timeout(250)
        after = paginator_text(page)
        if after and after != before:
            wait_for_rows(page)
            return True
    raise RuntimeError("La page suivante du tunnel CRM n'a pas chargé.")


def main() -> None:
    ensure_table()
    synced_at = datetime.now().isoformat(sep=" ", timespec="seconds")
    total = 0
    with sync_playwright() as playwright:
        context = launch_context(playwright, PROFILE_DIR, {"width": 1490, "height": 995})
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(LIST_URL, wait_until="domcontentloaded", timeout=90_000)
        open_list(page)
        set_100_rows(page)
        select_all_statuses(page)
        page_number = 1
        while True:
            rows = extract_rows(page, synced_at)
            if not rows:
                raise RuntimeError(f"Aucun lead lisible à la page {page_number}.")
            save(rows)
            total += len(rows)
            print(f"Tunnel page {page_number}: {len(rows)} leads, total {total}.")
            if not next_page(page):
                break
            page_number += 1
        context.close()

    # Une suppression n'est faite qu'après un parcours complet réussi.
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("DELETE FROM lead_funnel WHERE synced_at <> ?", (synced_at,))
        connection.commit()
    print(f"Tunnel commercial synchronisé : {total} leads.")


if __name__ == "__main__":
    main()
