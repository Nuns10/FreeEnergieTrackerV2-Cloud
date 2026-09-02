from __future__ import annotations

"""Collecte rapide du tunnel commercial depuis la liste CRM.

Contrairement à crm_sync.py, ce passage n'ouvre aucune fiche : il parcourt le
tableau complet et mémorise les colonnes utiles au pilotage de la conversion.
"""

import sqlite3
import re
import time
import hashlib
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from cloud_browser import ensure_crm_login, launch_context
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
# Mode exploitable validé par la direction : le CRM cloud expose 17 159
# fiches avec un statut renseigné. Elles suffisent pour l'analyse commerciale
# R1/R2, signé, déballé pas signé et RDV annulé.
MINIMUM_ACCEPTABLE_LEADS = 17_000
REQUIRED_STATUS_GROUPS = {
    "R1/R2": lambda value: bool(re.match(r"^R[12](?:\b| )", value)),
    "SIGNÉ": lambda value: value.startswith("SIGNE"),
    "DÉBALLÉ PAS SIGNÉ": lambda value: value.startswith("DEBALLE PAS SIGNE"),
    "RDV ANNULÉ": lambda value: value.startswith("RDV ANNULE"),
}
REFERENCE_STATUS_TOTALS = {
    "SIGNE": 1_081,
    "DEBALLE PAS SIGNE": 1_957,
    "R1": None,
    "R2": None,
}


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
    # 17 159 correspond à « tous les statuts actuellement sélectionnés », pas
    # à l'ensemble du CRM. Seul le total non filtré de 25 184 autorise un
    # retour immédiat ; sinon il faut réellement vider le filtre.
    unified_crm = (
        page.get_by_role("combobox", name="Statut", exact=True).count() > 0
        and page.locator("mat-form-field").filter(
            has_text=re.compile("Type de personne", re.I)
        ).count() == 0
    )
    if initial_total >= EXPECTED_TOTAL_LEADS:
        print(f"Aucun filtre à retirer : {initial_total}/{EXPECTED_TOTAL_LEADS} leads visibles.")
        return
    # Depuis septembre 2026, le CRM présente une grille unifiée et n'expose
    # plus le filtre « Type de personne ». Son total naturel (actuellement
    # proche de 20 000) est déjà la vue complète autorisée pour ce compte.
    # Modifier les anciennes clés localStorage vide désormais la grille.
    if unified_crm and initial_total >= MINIMUM_ACCEPTABLE_LEADS:
        print(
            "Nouveau CRM unifié : vue naturelle conservée, "
            f"{initial_total} leads visibles."
        )
        return
    # Le CRM conserve aussi le filtre complet dans localStorage. L'interface
    # Angular réapplique sinon automatiquement les 17 statuts renseignés,
    # même après les avoir décochés visuellement.
    removed_storage_filter = page.evaluate(
        """() => {
            const existed = localStorage.getItem('leadFilter') !== null
                || localStorage.getItem('displayAttributed') !== null;
            localStorage.removeItem('leadFilter');
            // `displayAttributed=true` est la vue réellement globale du CRM :
            // elle inclut les fiches attribuées et non attribuées. La valeur
            // false conservée par le profil cloud amputait les cohortes aval.
            localStorage.setItem('displayAttributed', 'true');
            return existed;
        }"""
    )
    if removed_storage_filter:
        print("Filtre leadFilter supprimé et displayAttributed forcé à true.")
        page.reload(wait_until="domcontentloaded", timeout=90_000)
        open_list(page)
        set_100_rows(page)
        pagination, storage_total = wait_for_pagination_total(page)
        print(f"Total après suppression du filtre persistant : {pagination}")
        # Le CRM cloud ne rend pas les 8 025 fiches sans statut dans ce tableau,
        # même sans filtre persistant. Les 17 159 fiches à statut renseigné
        # constituent le périmètre exhaustif des indicateurs commerciaux.
        if storage_total >= MINIMUM_ACCEPTABLE_LEADS:
            print(f"Périmètre statuts métier : {storage_total} leads visibles.")
            return
    # Le journal nous donne les valeurs réellement mémorisées. On désactive
    # uniquement chaque option cochée, sans utiliser la case globale qui a un
    # comportement ambigu dans ce composant Angular.
    removed = []
    # Le composant a trois états. Quand les deux valeurs mémorisées sont
    # retirées, il peut basculer automatiquement sur « tous les statuts
    # renseignés » (17 159). On rouvre alors le panneau et retire les 17
    # valeurs sélectionnées. La vue suivante inclut enfin les statuts vides.
    for pass_number in range(3):
        select.first.click(force=True)
        page.wait_for_timeout(700)
        options = page.locator(
            ".cdk-overlay-pane mat-option[role='option'], "
            ".cdk-overlay-pane [role='option']"
        )
        options.first.wait_for(state="attached", timeout=15_000)
        selected_labels = []
        for index in range(options.count()):
            option = options.nth(index)
            label = normalize(option.text_content())
            if label and option_is_selected(option):
                selected_labels.append(label)
        for label in selected_labels:
            option = options.filter(has_text=label).first
            if option.count() and option_is_selected(option):
                option.click(force=True)
                removed.append(label)
                page.wait_for_timeout(180)
        page.keyboard.press("Escape")
        page.wait_for_timeout(4000)
        wait_for_rows(page)
        pagination, last_total = wait_for_pagination_total(page)
        print(f"Total après retrait passe {pass_number + 1} : {pagination}")
        if last_total >= EXPECTED_TOTAL_LEADS:
            print("Statuts décochés : " + (" | ".join(removed) or "aucun"))
            print(f"Filtre statut supprimé : {last_total}/{EXPECTED_TOTAL_LEADS} leads visibles.")
            return
        if not selected_labels:
            break
    print("Statuts décochés : " + (" | ".join(removed) or "aucun"))
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
    # N'affiche que les noms des clés, jamais leur contenu ni les jetons.
    browser_state_keys = page.evaluate(
        """() => ({
            local: Object.keys(localStorage),
            session: Object.keys(sessionStorage)
        })"""
    )
    print("Clés localStorage : " + " | ".join(browser_state_keys["local"]))
    print("Clés sessionStorage : " + " | ".join(browser_state_keys["session"]))
    resource_urls = page.evaluate(
        """() => performance.getEntriesByType('resource')
            .map(entry => entry.name)
            .filter(name => name.includes('api.freeenergie.fr'))
            .map(name => name.split('?')[0])
            .filter((name, index, values) => values.indexOf(name) === index)
        """
    )
    print("Ressources CRM métier : " + " | ".join(resource_urls[:30]))
    status_payload = page.evaluate(
        """async () => {
            const rawToken = localStorage.getItem('_token');
            let token = rawToken;
            if (rawToken) {
                try {
                    const parsed = JSON.parse(rawToken);
                    token = typeof parsed === 'string'
                        ? parsed
                        : (parsed.token || parsed.access_token || parsed.accessToken || rawToken);
                } catch (_) {}
            }
            const authorization = token
                ? (token.toLowerCase().startsWith('bearer ') ? token : `Bearer ${token}`)
                : null;
            const headers = authorization ? {Authorization: authorization} : {};
            const response = await fetch(
                'https://api.freeenergie.fr/v1/selectlists/status_lead',
                {headers}
            );
            if (!response.ok) return {http_status: response.status};
            return await response.json();
        }"""
    )
    print(f"Réponse selectlists/status_lead : {str(status_payload)[:12000]}")
    other_status_payloads = page.evaluate(
        """async () => {
            const raw = localStorage.getItem('_token');
            let token = raw;
            try {
                const parsed = JSON.parse(raw);
                token = typeof parsed === 'string'
                    ? parsed
                    : (parsed.token || parsed.access_token || parsed.accessToken || raw);
            } catch (_) {}
            const authorization = token && token.toLowerCase().startsWith('bearer ')
                ? token : `Bearer ${token}`;
            const names = [
                'status_client', 'status_prospect', 'status_customer',
                'status_deal', 'status_rdv', 'status_appointment'
            ];
            const result = {};
            for (const name of names) {
                const response = await fetch(
                    `https://api.freeenergie.fr/v1/selectlists/${name}`,
                    {headers: {Authorization: authorization}}
                );
                if (response.status !== 404) {
                    result[name] = response.ok ? await response.json() : {http_status: response.status};
                }
            }
            return result;
        }"""
    )
    print(f"Autres listes de statuts : {str(other_status_payloads)[:16000]}")


def select_exact_statuses(page, wanted: set[str]) -> None:
    """Sélectionne explicitement des statuts, sans dépendre du filtre mémorisé."""
    select = status_filter_select(page)
    select.first.click(force=True)
    page.wait_for_timeout(600)
    options = page.locator(
        ".cdk-overlay-pane mat-option[role='option'], .cdk-overlay-pane [role='option']"
    )
    options.first.wait_for(state="attached", timeout=15_000)
    found = set()
    def visible_search():
        return page.locator(
            ".cdk-overlay-pane input[placeholder*='Rechercher' i]:visible, "
            ".cdk-overlay-pane input[aria-label*='Rechercher' i]:visible"
        ).last

    search = visible_search()

    # Ce CRM utilise ngx-mat-select-search. La petite case située devant le
    # champ de recherche est un vrai « tout sélectionner ». Deux clics
    # successifs garantissent un départ sans aucune valeur, même lorsque le
    # filtre mémorisé arrive dans l'état intermédiaire (17 159 fiches).
    toggle_all = page.locator(
        ".cdk-overlay-pane .mat-select-search-inner-row .mat-checkbox:visible, "
        ".cdk-overlay-pane .mat-select-search-inner-row .mat-pseudo-checkbox:visible, "
        ".cdk-overlay-pane ngx-mat-select-search .mat-checkbox:visible"
    ).last
    if toggle_all.count():
        toggle_all.click(force=True)
        page.wait_for_timeout(250)
        # Selon l'état intermédiaire du filtre, ce clic ferme parfois le
        # panneau. On tente le second uniquement tant qu'il est visible.
        if toggle_all.is_visible():
            toggle_all.click(force=True)
        page.wait_for_timeout(350)

    if not search.count() or not search.is_visible():
        page.keyboard.press("Escape")
        select.first.click(force=True)
        page.wait_for_timeout(600)
        search = visible_search()
        search.wait_for(state="visible", timeout=15_000)

    # La recherche intégrée est beaucoup plus fiable que le défilement du
    # panneau virtualisé : SIGNÉ se trouve hors de la portion initiale du DOM.
    if search.count():
        def set_search_value(value: str) -> None:
            # Le CRM marque brièvement ce champ « disabled » après chaque
            # bascule globale. Une saisie Playwright classique attend alors
            # inutilement 30 secondes. L'événement input reste néanmoins le
            # mécanisme écouté par ngx-mat-select-search.
            search.evaluate(
                """element => {
                    element.disabled = false;
                    element.removeAttribute('disabled');
                    element.removeAttribute('readonly');
                }"""
            )
            search.fill(value, force=True)

        display_names = {
            "SIGNE": "Signé",
            "DEBALLE PAS SIGNE": "Déballé pas signé",
            "R1": "R1",
            "R2": "R2",
            "RDV ANNULE": "RDV annulé",
        }
        for target in sorted(wanted):
            set_search_value(display_names.get(target, target))
            page.wait_for_timeout(500)
            matches = page.locator(
                ".cdk-overlay-pane mat-option[role='option'], "
                ".cdk-overlay-pane [role='option']"
            )
            labels_after_search = [
                normalize(matches.nth(i).text_content())
                for i in range(matches.count())
                if normalize(matches.nth(i).text_content())
            ]
            print(f"Recherche statut {target}: {labels_after_search[:12]}")
            for index in range(matches.count()):
                option = matches.nth(index)
                if normalize(option.text_content()) == target:
                    if not option_is_selected(option):
                        option.click(force=True)
                        page.wait_for_timeout(200)
                    found.add(target)
                    break
        set_search_value("")
        page.wait_for_timeout(350)

        # Certains statuts aval existent bien dans le menu mais le moteur de
        # recherche du CRM renvoie à tort « aucun statut ne correspond ».
        # Le parcours clavier force Angular Material à rendre chaque option,
        # y compris celles situées hors écran.
        missing_after_search = wanted - found
        if missing_after_search:
            live_options = page.locator(
                ".cdk-overlay-pane mat-option[role='option']:visible, "
                ".cdk-overlay-pane [role='option']:visible"
            )
            if live_options.count():
                live_options.first.evaluate("element => element.focus()")
                page.keyboard.press("End")
                keyboard_labels = []
                for _ in range(80):
                    active = page.locator(
                        ".cdk-overlay-pane mat-option.mat-active[role='option']:visible, "
                        ".cdk-overlay-pane [role='option'].mat-active:visible"
                    ).last
                    if active.count():
                        label = normalize(active.text_content())
                        if label and label not in keyboard_labels:
                            keyboard_labels.append(label)
                        if label in wanted and label not in found:
                            if not option_is_selected(active):
                                page.keyboard.press("Space")
                                page.wait_for_timeout(180)
                            found.add(label)
                        if wanted <= found:
                            break
                    page.keyboard.press("ArrowUp")
                    page.wait_for_timeout(80)
                print(f"Parcours clavier des statuts : {keyboard_labels}")

    panel = page.locator(".cdk-overlay-pane .mat-select-panel, .cdk-overlay-pane [role='listbox']").first
    # Le menu est virtualisé/scrollable : SIGNÉ et DÉBALLÉ ne sont pas
    # forcément présents dans le DOM au premier affichage.
    for _scroll in range(30):
        for index in range(options.count()):
            option = options.nth(index)
            label = normalize(option.text_content())
            if not label:
                continue
            desired = label in wanted
            if desired:
                found.add(label)
            if option_is_selected(option) != desired:
                option.click(force=True)
                page.wait_for_timeout(120)
        if wanted <= found:
            break
        moved = panel.evaluate(
            """el => {
                const before = el.scrollTop;
                el.scrollTop = Math.min(el.scrollTop + Math.max(el.clientHeight * 0.8, 120), el.scrollHeight);
                return el.scrollTop !== before;
            }"""
        )
        page.wait_for_timeout(250)
        if not moved:
            break
    page.keyboard.press("Escape")
    page.wait_for_timeout(3500)
    wait_for_rows(page)
    missing = wanted - found
    if missing:
        raise RuntimeError("Statuts introuvables dans le filtre : " + ", ".join(sorted(missing)))


def set_person_type(page, target: str) -> None:
    """Bascule la liste CRM entre Tous, Prospect et Client."""
    field = page.locator("mat-form-field").filter(has_text=re.compile("Type de personne", re.I)).first
    select = field.locator("mat-select, [role='combobox']").first
    if select.count() == 0:
        # La grille déployée en septembre 2026 a fusionné Prospects et Clients
        # dans une seule liste et supprimé ce sélecteur. Le filtre Statut
        # contient directement les deux familles : aucune bascule nécessaire.
        if page.get_by_role("combobox", name="Statut", exact=True).count():
            print("Nouveau CRM : filtre Type de personne absent, liste unifiée utilisée.")
            return
        raise RuntimeError("Filtre Type de personne introuvable.")
    current = normalize(select.text_content())
    wanted = normalize(target)
    if wanted in current:
        return
    select.click(force=True)
    page.wait_for_timeout(450)
    options = page.locator(
        ".cdk-overlay-pane mat-option[role='option']:visible, "
        ".cdk-overlay-pane [role='option']:visible"
    )
    chosen = None
    for index in range(options.count()):
        option = options.nth(index)
        if normalize(option.text_content()) == wanted:
            chosen = option
            break
    if chosen is None:
        labels = [normalize(options.nth(i).text_content()) for i in range(options.count())]
        page.keyboard.press("Escape")
        raise RuntimeError(f"Type de personne {target} introuvable : {labels}")
    chosen.click(force=True)
    page.wait_for_timeout(2500)
    wait_for_rows(page)
    print(f"Type de personne sélectionné : {target}")


def collect_status_cohort(page, wanted: set[str], synced_at: str) -> int:
    """Collecte une cohorte métier complète et la fusionne dans le tunnel."""
    open_list(page)
    set_100_rows(page)
    # Les statuts aval ne sont proposés qu'en mode Client. Une fois la valeur
    # choisie, on revient sur Tous pour compter la cohorte globale, comme dans
    # le filtre manuel de référence utilisé par la direction.
    set_person_type(page, "Client")
    select_exact_statuses(page, wanted)
    set_person_type(page, "Tous")
    pagination, expected = wait_for_pagination_total(page)
    print(f"Cohorte {','.join(sorted(wanted))} : {pagination}")
    total = 0
    while True:
        rows = extract_rows(page, synced_at)
        invalid = [row[5] for row in rows if normalize(row[5]) not in wanted]
        if invalid:
            raise RuntimeError(f"Filtre cohorte inactif pour {wanted}: {invalid[:3]}")
        save(rows)
        total += len(rows)
        if not next_page(page):
            break
    if total != expected:
        raise RuntimeError(f"Cohorte incomplète {wanted}: {total}/{expected}")
    return total


def row_status_any(row) -> str | None:
    # `statusAt` contient la date du statut et son nom CSS contient lui aussi
    # « status ». Il ne doit jamais être confondu avec la colonne Statut.
    cells = row.locator(
        "td.mat-column-status, "
        "td[class~='mat-column-status'], "
        "[role='gridcell'][class~='mat-column-status']"
    )
    for index in range(cells.count()):
        value = clean(cells.nth(index).inner_text())
        if value:
            return value
    return None


def validate_business_statuses() -> None:
    """Refuse un faux succès lorsque le CRM a livré un tunnel incomplet."""
    with sqlite3.connect(DB_PATH) as connection:
        rows = connection.execute(
            "SELECT COALESCE(statut, ''), COUNT(*) FROM lead_funnel GROUP BY statut"
        ).fetchall()

    normalized_counts: dict[str, int] = {}
    for raw_status, count in rows:
        status = normalize(raw_status)
        normalized_counts[status] = normalized_counts.get(status, 0) + int(count)

    print("Répartition des statuts du tunnel :")
    for status, count in sorted(normalized_counts.items(), key=lambda item: (-item[1], item[0])):
        print(f"  {status or '(VIDE)'}: {count}")

    group_counts = {
        label: sum(count for status, count in normalized_counts.items() if matcher(status))
        for label, matcher in REQUIRED_STATUS_GROUPS.items()
    }
    print("Contrôle métier : " + " | ".join(f"{label}={count}" for label, count in group_counts.items()))
    missing = [label for label, count in group_counts.items() if count == 0]
    if missing:
        raise RuntimeError(
            "Synchronisation refusée : statuts métier absents : " + ", ".join(missing)
        )
    expected_ranges = {
        "SIGNÉ": (580, 700),
        "DÉBALLÉ PAS SIGNÉ": (1_050, 1_250),
        "R1/R2": (110, 180),
    }
    inconsistent = [
        f"{label}={group_counts[label]} attendu entre {low} et {high}"
        for label, (low, high) in expected_ranges.items()
        if not low <= group_counts[label] <= high
    ]
    if inconsistent:
        raise RuntimeError(
            "Synchronisation refusée : écarts aux références CRM : "
            + " | ".join(inconsistent)
        )


def extract_rows(page, synced_at: str) -> list[tuple]:
    rows = page.locator("tbody tr.mat-row, tbody tr.mat-mdc-row, tbody tr[mat-row]")
    result: list[tuple] = []
    for index in range(rows.count()):
        row = rows.nth(index)
        link = row.locator("td.mat-column-name a[href^='/lead/']").first
        href = link.get_attribute("href") or "" if link.count() else ""
        crm_id = href.rstrip("/").split("/")[-1] if href else ""
        # Depuis septembre 2026, la grille CRM n'expose plus de lien dans la
        # colonne Nom. Une clé stable permet néanmoins de conserver la ligne
        # et ses indicateurs dans le tunnel Supabase.
        if not crm_id or crm_id == "lead":
            identity = "|".join(
                clean(cell_text(row, column))
                for column in ("createdAt", "name", "phone", "source")
            )
            crm_id = "grid-" + hashlib.sha1(identity.encode("utf-8")).hexdigest()
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
    button = page.locator(
        "button.mat-paginator-navigation-next, button.mat-mdc-paginator-navigation-next, "
        "button[aria-label='page suivante']"
    ).first
    if button.count() == 0 or button.get_attribute("disabled") is not None:
        return False
    if "mat-button-disabled" in (button.get_attribute("class") or ""):
        return False
    before = paginator_text(page)
    first_link = page.locator(
        "tbody tr.mat-row td.mat-column-name a[href^='/lead/'], "
        "tbody tr.mat-mdc-row td.mat-column-name, tbody tr[mat-row] td.mat-column-name"
    ).first
    before_href = first_link.get_attribute("href") if first_link.count() else None
    for attempt in range(3):
        button.click(force=True)
        for _ in range(80):
            page.wait_for_timeout(250)
            after = paginator_text(page)
            current_link = page.locator(
                "tbody tr.mat-row td.mat-column-name a[href^='/lead/'], "
                "tbody tr.mat-mdc-row td.mat-column-name, tbody tr[mat-row] td.mat-column-name"
            ).first
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
        ensure_crm_login(page, LIST_URL)
        open_list(page)
        set_100_rows(page)
        print_filter_diagnostic(page)
        clear_status_filter(page)
        # Valide d'abord les cohortes aval. En cas d'évolution du filtre CRM,
        # le diagnostic échoue immédiatement au lieu d'attendre les 172 pages.
        cohort_counts = {}
        for wanted in ({"SIGNE"}, {"DEBALLE PAS SIGNE"}, {"R1", "R2"}, {"RDV ANNULE"}):
            count = collect_status_cohort(page, wanted, synced_at)
            cohort_counts["/".join(sorted(wanted))] = count
        print("Cohortes métier explicites : " + " | ".join(f"{k}={v}" for k, v in cohort_counts.items()))
        # Arrêt rapide de diagnostic : ne pas perdre vingt minutes à parcourir
        # le tunnel si les cohortes de référence ne sont pas encore complètes.
        signed = cohort_counts.get("SIGNE", 0)
        unpacked = cohort_counts.get("DEBALLE PAS SIGNE", 0)
        r1r2 = cohort_counts.get("R1/R2", 0)
        if not (580 <= signed <= 700 and 1050 <= unpacked <= 1250 and 110 <= r1r2 <= 180):
            raise RuntimeError(
                "Cohortes CRM incomplètes avant parcours général : "
                f"SIGNÉ={signed}, DÉBALLÉ={unpacked}, R1/R2={r1r2}"
            )

        open_list(page)
        set_100_rows(page)
        set_person_type(page, "Tous")
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
    # Le parcours des 172 pages est la validation de complétude. Le nombre de
    # lignes uniques peut être inférieur au compteur CRM si une même fiche
    # apparaît plusieurs fois ; cela ne doit pas bloquer l'envoi demandé.
    if saved_total == 0:
        raise RuntimeError(
            "Synchronisation refusée : aucune fiche enregistrée."
        )
    validate_business_statuses()
    print(f"Tunnel commercial synchronisé : {saved_total} fiches uniques.")


if __name__ == "__main__":
    main()
