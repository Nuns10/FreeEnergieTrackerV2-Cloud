
from __future__ import annotations

import argparse
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import pandas as pd
from playwright.sync_api import Page, sync_playwright

from database import upsert
from cloud_browser import launch_context


BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "browser-profile"
CRM_URL = "https://crm.freeenergie.fr"
LIST_URL = f"{CRM_URL}/lead"

TARGET_STATUSES = {
    "PROSPECT A ATTRIBUER",
    "A RELANCER",
}


def clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize(value: str | None) -> str:
    value = clean(value).replace("_", " ")
    value = unicodedata.normalize("NFD", value)
    value = "".join(char for char in value if unicodedata.category(char) != "Mn")
    return clean(value).upper()


def first_visible(locator):
    for index in range(locator.count()):
        item = locator.nth(index)
        try:
            if item.is_visible():
                return item
        except Exception:
            continue
    return None


def wait_for_rows(page: Page, timeout_ms: int = 30_000) -> None:
    page.locator("tbody tr.mat-row").first.wait_for(
        state="attached",
        timeout=timeout_ms,
    )


def open_list(page: Page) -> None:
    page.goto(LIST_URL, wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(1500)
    wait_for_rows(page)
    print("Page Prospection/Client ouverte.")


def paginator_text(page: Page) -> str:
    locator = page.locator(".mat-paginator-range-label")
    return clean(locator.first.inner_text()) if locator.count() else ""


def set_100_rows(page: Page) -> None:
    select = page.locator('mat-select[aria-label="Résultat par page :"]')
    if select.count() == 0:
        return

    try:
        if clean(select.first.inner_text()) == "100":
            return

        select.first.click(force=True)
        page.wait_for_timeout(300)

        option = first_visible(page.locator("mat-option").filter(has_text="100"))
        if option:
            option.click(force=True)
            page.wait_for_timeout(1200)
            print("Affichage réglé sur 100 résultats par page.")
    except Exception as exc:
        print(f"Réglage 100 lignes ignoré : {exc}")


def status_filter_select(page: Page):
    return page.locator(
        "th.mat-column-status-search mat-select[placeholder='Status']"
    )


def option_is_selected(option) -> bool:
    classes = option.get_attribute("class") or ""
    return (
        option.get_attribute("aria-selected") == "true"
        or "mat-selected" in classes
        or "mat-option-selected" in classes
    )


def apply_target_statuses(page: Page) -> None:
    """
    Sélectionne exactement PROSPECT À ATTRIBUER et À RELANCER.
    Toute ancienne sélection est désactivée.
    """
    status_select = status_filter_select(page)
    if status_select.count() == 0:
        raise RuntimeError(
            "Le filtre Statut du tableau CRM est introuvable."
        )

    # La session CRM mémorise généralement ce filtre. Dans ce cas, ne pas
    # rouvrir puis manipuler le panneau Angular : en mode cloud, ses options
    # peuvent être visibles à l'écran tout en étant temporairement absentes
    # de la collection Playwright.
    current_selected = {
        normalize(item)
        for item in clean(status_select.first.inner_text()).split(",")
        if clean(item)
    }
    if current_selected == TARGET_STATUSES:
        print("Filtre CRM déjà actif : PROSPECT À ATTRIBUER + À RELANCER.")
        verify_filtered_results(page)
        return

    before = paginator_text(page)
    status_select.first.click(force=True)
    page.wait_for_timeout(500)

    panel = first_visible(
        page.locator(
            ".cdk-overlay-pane .mat-select-panel, "
            ".cdk-overlay-pane [role='listbox']"
        )
    )
    if panel is None:
        raise RuntimeError(
            "La liste des statuts ne s’est pas ouverte."
        )

    # Utiliser une recherche globale dans l'overlay. Le contexte GitHub
    # Actions expose bien le panneau mais peut retourner zéro descendant
    # lorsque le locator est resserré sur le listbox Angular.
    options = page.locator(
        ".cdk-overlay-pane mat-option[role='option'], "
        ".cdk-overlay-pane [role='option']"
    )
    options.first.wait_for(state="attached", timeout=15_000)
    # Angular attache parfois les options avant d'injecter leur libellé,
    # surtout sur les runners GitHub sans écran. Attendre les textes évite
    # de conclure à tort que les statuts ont disparu du CRM.
    for _ in range(30):
        option_labels = [
            normalize(options.nth(i).text_content())
            for i in range(options.count())
        ]
        if any(option_labels):
            break
        page.wait_for_timeout(250)
    found_targets: set[str] = set()
    visible_labels: list[str] = []

    for index in range(options.count()):
        option = options.nth(index)
        try:
            # text_content() reste fiable dans Chromium sans écran, alors que
            # inner_text() peut échouer pendant l'animation de l'overlay.
            label = normalize(option.text_content())
        except Exception:
            continue

        if not label:
            continue

        visible_labels.append(label)

        wanted = label in TARGET_STATUSES
        selected = option_is_selected(option)

        if wanted:
            found_targets.add(label)

        if wanted != selected:
            option.click(force=True)
            page.wait_for_timeout(150)

    missing = TARGET_STATUSES - found_targets
    if missing:
        print("Options de statut visibles : " + " | ".join(visible_labels))
        if os.getenv("GITHUB_ACTIONS"):
            artifact_dir = BASE_DIR / "diagnostic_cloud"
            artifact_dir.mkdir(exist_ok=True)
            page.screenshot(path=str(artifact_dir / "statuts.png"), full_page=True)
            (artifact_dir / "page.html").write_text(page.content(), encoding="utf-8")
        page.keyboard.press("Escape")
        raise RuntimeError(
            "Statuts absents du filtre CRM : "
            + ", ".join(sorted(missing))
        )

    page.keyboard.press("Escape")
    page.wait_for_timeout(1800)
    wait_for_rows(page)

    after = paginator_text(page)
    print(
        "Filtre appliqué : PROSPECT À ATTRIBUER + À RELANCER. "
        f"Pagination : {after or 'non lisible'}"
    )

    verify_filtered_results(page)

    if after == before and "16448" in after.replace(" ", ""):
        raise RuntimeError(
            "Le filtre n’a pas réduit les 16 448 leads. "
            "Synchronisation annulée pour éviter de parcourir toute la base."
        )


def row_status(row) -> str:
    cell = row.locator("td.mat-column-status")
    if cell.count() == 0:
        return ""
    return normalize(cell.first.inner_text())


def verify_filtered_results(page: Page) -> None:
    rows = page.locator("tbody tr.mat-row")
    observed: set[str] = set()

    for index in range(min(rows.count(), 20)):
        status = row_status(rows.nth(index))
        if status:
            observed.add(status)

    invalid = observed - TARGET_STATUSES
    if invalid:
        raise RuntimeError(
            "Le filtre Statut n’est pas actif. Statuts inattendus visibles : "
            + ", ".join(sorted(invalid))
        )

    if not observed:
        raise RuntimeError(
            "Impossible de vérifier les statuts après filtrage."
        )


def cell_text(row, css_column: str) -> str | None:
    locator = row.locator(f"td.mat-column-{css_column}")
    if locator.count() == 0:
        return None
    value = clean(locator.first.inner_text())
    return value or None


def extract_page_records(page: Page) -> list[dict[str, Any]]:
    rows = page.locator("tbody tr.mat-row")
    records: list[dict[str, Any]] = []

    for index in range(rows.count()):
        row = rows.nth(index)
        status = row_status(row)

        # Double protection : aucune fiche hors des deux statuts.
        if status not in TARGET_STATUSES:
            continue

        name_link = row.locator(
            "td.mat-column-name a[href^='/lead/']"
        ).first

        if name_link.count() == 0:
            continue

        href = name_link.get_attribute("href")
        if not href or href.rstrip("/") == "/lead":
            continue

        crm_id = href.rstrip("/").split("/")[-1]

        records.append(
            {
                "crm_id": crm_id,
                "detail_url": urljoin(CRM_URL, href),
                "date_creation": cell_text(row, "createdAt"),
                "nom": cell_text(row, "name"),
                "code_postal": cell_text(row, "zip"),
                "ville": cell_text(row, "city"),
                "statut": status,
                "date_statut": cell_text(row, "statusAt"),
                "source": cell_text(row, "source"),
                "intervenant": cell_text(row, "attributed_to"),
                "telephone": cell_text(row, "phone"),
            }
        )

    return records


def next_page(page: Page) -> bool:
    button = page.locator("button.mat-paginator-navigation-next").first

    if button.count() == 0:
        return False

    if (
        button.get_attribute("disabled") is not None
        or "mat-button-disabled" in (button.get_attribute("class") or "")
    ):
        return False

    before = paginator_text(page)
    button.click(force=True)

    for _ in range(40):
        page.wait_for_timeout(250)
        after = paginator_text(page)
        if after and after != before:
            wait_for_rows(page)
            verify_filtered_results(page)
            return True

    raise RuntimeError(
        "La page suivante du CRM n’a pas chargé."
    )


def ensure_call_events_table() -> None:
    import sqlite3

    db_path = BASE_DIR / "data" / "leads.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS call_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                crm_id TEXT NOT NULL,
                prospect_name TEXT,
                commercial TEXT,
                event_type TEXT NOT NULL,
                event_datetime TEXT NOT NULL,
                comment TEXT,
                is_lunch_slot INTEGER NOT NULL DEFAULT 0,
                is_evening_slot INTEGER NOT NULL DEFAULT 0,
                synced_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_call_events_crm_id
            ON call_events(crm_id)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_call_events_commercial_datetime
            ON call_events(commercial, event_datetime)
            """
        )
        connection.commit()


def parse_crm_datetime(value: str | None) -> datetime | None:
    value = clean(value)
    if not value:
        return None

    value = re.sub(r"\s+à\s+", " ", value, flags=re.I)
    value = re.sub(r"\s+", " ", value)

    for fmt in (
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%y %H:%M:%S",
        "%d/%m/%y %H:%M",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M",
        "%d-%m-%y %H:%M:%S",
        "%d-%m-%y %H:%M",
    ):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue

    return None


def extract_call_events(
    page: Page,
    record: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Extrait chaque carte NRP avec l'auteur et l'horodatage.

    Dans le CRM, « NRP » et « 23/07/26 à 17:23 » peuvent être
    placés dans des éléments frères. On remonte donc dans leurs
    conteneurs communs au lieu de lire uniquement l'élément NRP.
    """
    raw_events = page.evaluate(
        """() => {
            const normalize = value =>
                (value || '').replace(/\\s+/g, ' ').trim();

            const datePattern =
                /\\b\\d{2}\\/\\d{2}\\/\\d{2,4}\\s+à\\s+\\d{1,2}:\\d{2}(?::\\d{2})?\\b/i;

            const isVisible = element => {
                const rect = element.getBoundingClientRect();
                const style = getComputedStyle(element);
                return rect.width > 0 &&
                       rect.height > 0 &&
                       style.display !== 'none' &&
                       style.visibility !== 'hidden';
            };

            const nodes = Array.from(
                document.querySelectorAll('div, span, p, mat-card')
            ).filter(isVisible);

            const nrpNodes = nodes.filter(element =>
                normalize(element.innerText || element.textContent)
                    .toUpperCase() === 'NRP'
            );

            const results = [];

            for (const nrpNode of nrpNodes) {
                let container = nrpNode;
                let chosen = null;

                for (let depth = 0; depth < 8 && container; depth++) {
                    const text = normalize(container.innerText);
                    const rect = container.getBoundingClientRect();

                    if (
                        datePattern.test(text) &&
                        text.length < 800 &&
                        rect.height < 500
                    ) {
                        chosen = container;
                        break;
                    }
                    container = container.parentElement;
                }

                if (!chosen) {
                    // Cherche un conteneur voisin, fréquent dans les cartes Material.
                    let parent = nrpNode.parentElement;
                    for (let depth = 0; depth < 5 && parent; depth++) {
                        const siblingText = normalize(
                            parent.parentElement
                                ? parent.parentElement.innerText
                                : ''
                        );
                        if (
                            datePattern.test(siblingText) &&
                            siblingText.length < 1000
                        ) {
                            chosen = parent.parentElement;
                            break;
                        }
                        parent = parent.parentElement;
                    }
                }

                const text = normalize(
                    chosen ? chosen.innerText : nrpNode.innerText
                );
                const dateMatch = text.match(datePattern);

                // L'auteur est généralement le texte court avant NRP.
                const pieces = text
                    .split(/\\n|\\r/)
                    .map(normalize)
                    .filter(Boolean);

                results.push({
                    fullText: text,
                    dateText: dateMatch ? dateMatch[0] : '',
                    pieces
                });
            }

            return results;
        }"""
    )

    body_text = clean(page.locator("body").inner_text(timeout=10000))

    # Secours : récupère les blocs textuels entourant NRP dans le body.
    if not raw_events:
        pattern = re.compile(
            r"(?P<author>[A-Za-zÀ-ÿ' -]{2,80})\s+"
            r"NRP\s+"
            r"(?P<date>\d{2}/\d{2}/\d{2,4}\s+à\s+"
            r"\d{1,2}:\d{2}(?::\d{2})?)",
            flags=re.I,
        )
        raw_events = [
            {
                "fullText": match.group(0),
                "dateText": match.group("date"),
                "pieces": [
                    clean(match.group("author")),
                    "NRP",
                    clean(match.group("date")),
                ],
            }
            for match in pattern.finditer(body_text)
        ]

    events: list[dict[str, Any]] = []
    seen: set[str] = set()

    for raw in raw_events:
        event_dt = parse_crm_datetime(raw.get("dateText"))
        if event_dt is None:
            continue

        full_text = clean(raw.get("fullText"))
        pieces = [
            clean(piece)
            for piece in raw.get("pieces", [])
            if clean(piece)
        ]

        commercial = None
        for piece in pieces:
            normalized_piece = normalize(piece)
            if normalized_piece in {"NRP"}:
                continue
            if parse_crm_datetime(piece):
                continue
            if (
                2 <= len(piece) <= 80
                and not re.search(r"\d{5,}", piece)
            ):
                commercial = piece
                break

        if commercial is None:
            commercial = record.get("intervenant")

        event_key = "|".join(
            [
                str(record["crm_id"]),
                event_dt.isoformat(sep=" "),
                normalize(commercial),
                "NRP",
            ]
        )

        if event_key in seen:
            continue
        seen.add(event_key)

        hour_minutes = event_dt.hour * 60 + event_dt.minute

        events.append(
            {
                "event_key": event_key,
                "crm_id": str(record["crm_id"]),
                "prospect_name": record.get("nom"),
                "commercial": commercial,
                "event_type": "NRP",
                "event_datetime": event_dt.isoformat(sep=" "),
                "comment": full_text,
                "is_lunch_slot": int(12 * 60 <= hour_minutes < 14 * 60),
                "is_evening_slot": int(hour_minutes >= 18 * 60 + 30),
            }
        )

    # Le compteur CRM reste la référence pour le total.
    explicit_count = re.search(
        r"Nombre\s+de\s+NRP\s*:\s*(\d+)",
        body_text,
        flags=re.I,
    )

    if explicit_count and int(explicit_count.group(1)) > len(events):
        print(
            f"Attention {record.get('nom') or record['crm_id']} : "
            f"{explicit_count.group(1)} NRP annoncés, "
            f"mais {len(events)} horodatage(s) extrait(s)."
        )

    return events


def save_call_events(events: list[dict[str, Any]]) -> None:
    if not events:
        return

    import sqlite3

    db_path = BASE_DIR / "data" / "leads.sqlite"
    now = datetime.now().isoformat(sep=" ", timespec="seconds")

    with sqlite3.connect(db_path) as connection:
        for event in events:
            connection.execute(
                """
                INSERT INTO call_events (
                    event_key,
                    crm_id,
                    prospect_name,
                    commercial,
                    event_type,
                    event_datetime,
                    comment,
                    is_lunch_slot,
                    is_evening_slot,
                    synced_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_key) DO UPDATE SET
                    prospect_name = excluded.prospect_name,
                    commercial = excluded.commercial,
                    comment = excluded.comment,
                    is_lunch_slot = excluded.is_lunch_slot,
                    is_evening_slot = excluded.is_evening_slot,
                    synced_at = excluded.synced_at
                """,
                (
                    event["event_key"],
                    event["crm_id"],
                    event.get("prospect_name"),
                    event.get("commercial"),
                    event["event_type"],
                    event["event_datetime"],
                    event.get("comment"),
                    event["is_lunch_slot"],
                    event["is_evening_slot"],
                    now,
                ),
            )
        connection.commit()


def history_text(page: Page) -> str:
    return clean(page.locator("body").inner_text(timeout=10000))


def parse_last_call(text: str) -> str | None:
    timestamps = re.findall(
        r"\b(\d{2}/\d{2}/\d{2,4})\s+(?:à\s*)?"
        r"(\d{1,2}:\d{2}(?::\d{2})?)\b",
        text,
        flags=re.I,
    )

    parsed: list[datetime] = []
    for date_part, time_part in timestamps:
        value = f"{date_part} {time_part}"
        parsed_value = parse_crm_datetime(value)
        if parsed_value:
            parsed.append(parsed_value)

    return max(parsed).isoformat(sep=" ") if parsed else None


def parse_nrp(text: str) -> int:
    explicit = re.search(
        r"Nombre\s+de\s+NRP\s*:\s*(\d+)",
        text,
        flags=re.I,
    )
    if explicit:
        return int(explicit.group(1))

    return len(
        re.findall(
            r"\bNRP\b",
            text,
            flags=re.I,
        )
    )

def enrich_record(
    detail_page: Page,
    record: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    print(f"Ouverture : {record.get('nom') or record['crm_id']}")

    detail_page.goto(
        record["detail_url"],
        wait_until="domcontentloaded",
        timeout=90_000,
    )
    detail_page.wait_for_timeout(1200)

    if "/lead/" not in detail_page.url:
        raise RuntimeError(
            f"La fiche ne s’est pas ouverte : {detail_page.url}"
        )

    text = history_text(detail_page)
    events = extract_call_events(detail_page, record)

    enriched = dict(record)
    enriched.pop("detail_url", None)

    # Le compteur affiché dans le CRM est la référence pour nombre_nrp.
    enriched["nombre_nrp"] = parse_nrp(text)

    if events:
        latest = max(
            datetime.fromisoformat(event["event_datetime"])
            for event in events
        )
        enriched["dernier_appel"] = latest.isoformat(sep=" ")
    else:
        enriched["dernier_appel"] = parse_last_call(text)

    enriched["raw_detail_text"] = text
    return enriched, events

def sync(
    interactive: bool = False,
    max_leads: int | None = None,
) -> None:
    completed: list[dict[str, Any]] = []
    pending_call_events: list[dict[str, Any]] = []
    processed_ids: set[str] = set()
    ensure_call_events_table()

    with sync_playwright() as playwright:
        context = launch_context(
            playwright, PROFILE_DIR, {"width": 1490, "height": 995}
        )

        list_page = (
            context.pages[0]
            if context.pages
            else context.new_page()
        )

        open_list(list_page)

        if interactive:
            input(
                "Connecte-toi si nécessaire, puis appuie sur Entrée…"
            )
            open_list(list_page)

        set_100_rows(list_page)
        apply_target_statuses(list_page)

        detail_page = context.new_page()
        page_number = 1

        while True:
            records = extract_page_records(list_page)
            print(
                f"Page filtrée {page_number} : "
                f"{len(records)} fiche(s) à traiter."
            )

            for record in records:
                if record["crm_id"] in processed_ids:
                    continue

                processed_ids.add(record["crm_id"])

                try:
                    enriched, call_events = enrich_record(
                        detail_page,
                        record,
                    )
                    completed.append(enriched)
                    pending_call_events.extend(call_events)

                    print(
                        f"[{len(completed)}] "
                        f"{record.get('nom') or record['crm_id']} — "
                        f"{enriched['nombre_nrp']} NRP — "
                        f"dernier appel : "
                        f"{enriched['dernier_appel'] or 'non trouvé'}"
                    )
                except Exception as exc:
                    print(
                        f"Erreur fiche "
                        f"{record.get('nom') or record['crm_id']} : {exc}"
                    )
                    continue

                if len(completed) % 20 == 0:
                    upsert(pd.DataFrame(completed))
                    save_call_events(pending_call_events)
                    pending_call_events.clear()
                    print(
                        f"Sauvegarde intermédiaire : "
                        f"{len(completed)} fiches."
                    )

                if (
                    max_leads is not None
                    and len(completed) >= max_leads
                ):
                    break

            if (
                max_leads is not None
                and len(completed) >= max_leads
            ):
                break

            if not next_page(list_page):
                break

            page_number += 1

        detail_page.close()
        context.close()

    if not completed:
        raise RuntimeError(
            "Aucune fiche filtrée n’a été synchronisée."
        )

    upsert(pd.DataFrame(completed))
    save_call_events(pending_call_events)
    print(
        f"Terminé : {len(completed)} fiches "
        f"PROSPECT À ATTRIBUER / À RELANCER synchronisées."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--max-leads", type=int)
    arguments = parser.parse_args()

    sync(
        interactive=arguments.interactive,
        max_leads=arguments.max_leads,
    )
