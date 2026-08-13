from __future__ import annotations

"""Collecte rapide du tunnel commercial depuis la liste CRM.

Contrairement à crm_sync.py, ce passage n'ouvre aucune fiche : il parcourt le
tableau complet et mémorise les colonnes utiles au pilotage de la conversion.
"""

import sqlite3
import re
import time
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
    option_is_selected,
    paginator_text,
    set_100_rows,
    status_filter_select,
    wait_for_rows,
)


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "leads.sqlite"
EXPECTED_TOTAL_LEADS = 25_184
MINIMUM_ACCEPTABLE_LEADS = 25_000


def wait_for_pagination_total(page, timeout_seconds: int = 20) -> tuple[str, int]:
    """Attend le vrai total : le CRM affiche brièvement « 0 de 0 » au chargement."""
    deadline = time.monotonic() + timeout_seconds
    last_text = ""
    while time.monotonic() < deadline:
        last_text = paginator_text(page)
        match = re.search(r"de\s+([\d\s]+)$", last_text)
        total = int(match.group(1).replace(" ", "")) if match else 0
        if total > 0:
            return last_text, total
        page.wait_for_timeout(500)
    return last_text, 0


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


def clear_status_filter(page) -> None:
    """Retire réellement le filtre statut, y compris les statuts vides."""
    select = status_filter_select(page)
    if select.count() == 0:
        raise RuntimeError("Filtre Statut introuvable dans le CRM.")

    # Le cas normal est déjà le bon : le profil cloud ouvre parfois la liste
    # sans filtre. Ne surtout pas cliquer dans ce cas, car le clic global
    # créerait lui-même une sélection partielle.
    pagination, initial_total = wait_for_pagination_total(page)
    print(f"Total avant manipulation du filtre : {pagination}")
    if initial_total >= MINIMUM_ACCEPTABLE_LEADS:
        print(f"Aucun filtre à retirer : {initial_total}/{EXPECTED_TOTAL_LEADS} leads visibles.")
        return
    # Le journal nous donne les valeurs réellement mémorisées. On désactive
    # uniquement chaque option cochée, sans utiliser la case globale qui a un
    # comportement ambigu dans ce composant Angular.
    select.first.click(force=True)
    page.wait_for_timeout(700)
    options = page.locator(
        ".cdk-overlay-pane mat-option[role='option'], "
        ".cdk-overlay-pane [role='option']"
    )
    options.first.wait_for(state="attached", timeout=15_000)
    removed = []
    for index in range(options.count()):
        option = options.nth(index)
        label = normalize(option.text_content())
        if label and option_is_selected(option):
            option.click(force=True)
            removed.append(label)
            page.wait_for_timeout(250)
    page.keyboard.press("Escape")
    page.wait_for_timeout(4000)
    wait_for_rows(page)
    pagination, last_total = wait_for_pagination_total(page)
    print("Statuts décochés : " + (" | ".join(removed) or "aucun"))
    print(f"Total après retrait précis : {pagination}")
    if last_total >= MINIMUM_ACCEPTABLE_LEADS:
        print(f"Filtre statut supprimé : {last_total}/{EXPECTED_TOTAL_LEADS} leads visibles.")
        return
    raise RuntimeError(
        f"Tunnel incomplet : {last_total} leads visibles au lieu des "
        f"{EXPECTED_TOTAL_LEADS} attendus."
    )


def print_filter_diagnostic(page) -> None:
    """Journal non sensible des filtres actifs pour fiabiliser le robot cloud."""
    selects = page.locator("thead mat-select")
    visible_selects = []
    for index in range(selects.count()):
        item = selects.nth(index)
        visible_selects.append(
            f"{item.get_attribute('placeholder') or item.get_attribute('aria-label')}:"
            f"{clean(item.inner_text())}"
        )
    inputs = page.locator("thead input:not([type='checkbox']):not([type='radio'])")
    filled_inputs = []
    for index in range(inputs.count()):
        item = inputs.nth(index)
        value = clean(item.input_value())
        if value:
            filled_inputs.append(f"{item.get_attribute('placeholder')}:{value}")
    print("Filtres select visibles : " + " | ".join(visible_selects))
    print("Filtres texte remplis : " + (" | ".join(filled_inputs) or "aucun"))


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
    first_link = page.locator("tbody tr.mat-row td.mat-column-name a[href^='/lead/']").first
    before_href = first_link.get_attribute("href") if first_link.count() else None
    for attempt in range(3):
        button.click(force=True)
        for _ in range(80):
            page.wait_for_timeout(250)
            after = paginator_text(page)
            current_link = page.locator("tbody tr.mat-row td.mat-column-name a[href^='/lead/']").first
            after_href = current_link.get_attribute("href") if current_link.count() else None
            if (after and after != before) or (before_href and after_href and after_href != before_href):
                wait_for_rows(page)
                return True
        print(f"Pagination sans réponse, nouvelle tentative {attempt + 2}/3.")
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
        print_filter_diagnostic(page)
        clear_status_filter(page)
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
        saved_total = connection.execute("SELECT COUNT(*) FROM lead_funnel").fetchone()[0]
        connection.commit()
    if saved_total < MINIMUM_ACCEPTABLE_LEADS:
        raise RuntimeError(
            f"Synchronisation refusée : {saved_total} leads enregistrés au lieu des "
            f"{EXPECTED_TOTAL_LEADS} attendus."
        )
    print(f"Tunnel commercial synchronisé : {saved_total}/{EXPECTED_TOTAL_LEADS} leads.")


if __name__ == "__main__":
    main()
