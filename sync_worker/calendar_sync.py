from __future__ import annotations

import argparse
import re
import sqlite3
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import Locator, Page, sync_playwright
from cloud_browser import ensure_crm_login, launch_context

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "browser-profile"
DB_PATH = BASE_DIR / "data" / "leads.sqlite"
CALENDAR_URL = "https://crm.freeenergie.fr/event"

FRENCH_MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11,
    "décembre": 12, "decembre": 12,
}


def clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize(value: str | None) -> str:
    value = unicodedata.normalize("NFD", clean(value))
    return "".join(c for c in value if unicodedata.category(c) != "Mn").upper()


def first_visible(locator: Locator):
    for i in range(locator.count()):
        item = locator.nth(i)
        try:
            if item.is_visible():
                return item
        except Exception:
            pass
    return None


def is_rdv(title: str) -> bool:
    # Strictement R1 ou R2 comme demandé.
    return bool(re.search(r"(?<![A-Za-z0-9])R\s*[12](?![A-Za-z0-9])", clean(title), re.I))


def ensure_tables() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS calendar_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                commercial TEXT NOT NULL,
                event_date TEXT NOT NULL,
                start_time TEXT,
                end_time TEXT,
                title TEXT,
                color_hex TEXT,
                status_kind TEXT,
                week_label TEXT,
                source_url TEXT,
                synced_at TEXT NOT NULL
            )
        """)
        cols = {r[1] for r in con.execute("PRAGMA table_info(calendar_events)").fetchall()}
        for name, sqltype in {
            "event_key": "TEXT", "commercial": "TEXT", "event_date": "TEXT",
            "start_time": "TEXT", "end_time": "TEXT", "title": "TEXT",
            "color_hex": "TEXT", "status_kind": "TEXT",
            "week_label": "TEXT", "source_url": "TEXT", "synced_at": "TEXT"
        }.items():
            if name not in cols:
                con.execute(f'ALTER TABLE calendar_events ADD COLUMN "{name}" {sqltype}')

        con.execute("""
            CREATE TABLE IF NOT EXISTS calendar_daily_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                activity_key TEXT NOT NULL UNIQUE,
                commercial TEXT NOT NULL,
                activity_date TEXT NOT NULL,
                appointment_count INTEGER NOT NULL DEFAULT 0,
                appointment_minutes INTEGER NOT NULL DEFAULT 0,
                first_appointment TEXT,
                last_appointment TEXT,
                synced_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS sync_state (
                sync_name TEXT PRIMARY KEY,
                last_started_at TEXT,
                last_completed_at TEXT,
                status TEXT,
                message TEXT
            )
        """)
        con.commit()


def set_sync_state(status: str, message: str = "", completed: bool = False) -> None:
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    with sqlite3.connect(DB_PATH) as con:
        current = con.execute(
            "SELECT last_started_at, last_completed_at FROM sync_state WHERE sync_name='calendar'"
        ).fetchone()
        started = current[0] if current else now
        completed_at = current[1] if current else None
        if status == "running":
            started = now
        if completed:
            completed_at = now
        con.execute("""
            INSERT INTO sync_state(sync_name,last_started_at,last_completed_at,status,message)
            VALUES('calendar',?,?,?,?)
            ON CONFLICT(sync_name) DO UPDATE SET
              last_started_at=excluded.last_started_at,
              last_completed_at=excluded.last_completed_at,
              status=excluded.status,
              message=excluded.message
        """, (started, completed_at, status, message))
        con.commit()


def save_events(events: list[dict[str, Any]]) -> int:
    """Sauvegarde immédiatement : aucun résultat déjà trouvé n'est perdu."""
    if not events:
        return 0
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    with sqlite3.connect(DB_PATH) as con:
        for event in events:
            con.execute("""
                INSERT INTO calendar_events(
                    event_key, commercial, event_date, start_time, end_time,
                    title, color_hex, status_kind, week_label, source_url, synced_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(event_key) DO UPDATE SET
                    commercial=excluded.commercial,
                    event_date=excluded.event_date,
                    start_time=excluded.start_time,
                    end_time=excluded.end_time,
                    title=excluded.title,
                    color_hex=excluded.color_hex,
                    status_kind=excluded.status_kind,
                    week_label=excluded.week_label,
                    source_url=excluded.source_url,
                    synced_at=excluded.synced_at
            """, (
                event["event_key"], event["commercial"], event["event_date"],
                event.get("start_time"), event.get("end_time"), event.get("title"),
                event.get("color_hex"), event.get("status_kind"),
                event.get("week_label"), event.get("source_url"), now
            ))
        con.commit()
    return len(events)


def rebuild_daily_activity() -> None:
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM calendar_daily_activity")
        rows = con.execute("""
            SELECT commercial,event_date,start_time,end_time
            FROM calendar_events
            ORDER BY commercial,event_date,start_time
        """).fetchall()

        grouped = {}
        for commercial, event_date, start_time, end_time in rows:
            grouped.setdefault((commercial, event_date), []).append((start_time, end_time))

        for (commercial, event_date), slots in grouped.items():
            starts, ends, minutes = [], [], 0
            for start, end in slots:
                if start: starts.append(start)
                if end: ends.append(end)
                if start and end:
                    try:
                        a = datetime.strptime(start, "%H:%M")
                        b = datetime.strptime(end, "%H:%M")
                        minutes += max(0, int((b-a).total_seconds()/60))
                    except Exception:
                        pass
            key = f"{normalize(commercial)}|{event_date}"
            con.execute("""
                INSERT INTO calendar_daily_activity(
                    activity_key,commercial,activity_date,appointment_count,
                    appointment_minutes,first_appointment,last_appointment,synced_at
                ) VALUES(?,?,?,?,?,?,?,?)
            """, (
                key, commercial, event_date, len(slots), minutes,
                min(starts) if starts else None,
                max(ends) if ends else None, now
            ))
        con.commit()


def open_calendar(page: Page) -> None:
    page.goto(CALENDAR_URL, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(1800)
    ensure_crm_login(page, CALENDAR_URL)
    if page.get_by_text("Intervenants", exact=False).count() == 0:
        raise RuntimeError("Calendrier inaccessible : session CRM non connectée.")


def click_planning(page: Page) -> None:
    for selector in (
        "button.fc-listWeek-button",
        "button:has-text('Planning')",
        "[role='button']:has-text('Planning')",
    ):
        try:
            loc = page.locator(selector)
            item = first_visible(loc)
            if item:
                item.click(force=True)
                page.wait_for_timeout(900)
                return
        except Exception:
            pass
    raise RuntimeError("Bouton Planning introuvable.")


def get_commercials(page: Page) -> list[str]:
    card = page.locator("mat-card").filter(has_text=re.compile("Intervenants", re.I)).first
    if not card.count():
        raise RuntimeError("Carte Intervenants introuvable.")

    options = card.locator("mat-selection-list mat-list-option")
    if not options.count():
        options = card.locator("[role='option']")

    result = []
    for i in range(options.count()):
        option = options.nth(i)
        try:
            text = clean(option.locator(".mat-list-text").inner_text(timeout=1000))
        except Exception:
            try:
                text = clean(option.inner_text(timeout=1000))
            except Exception:
                text = ""
        if text and text.lower() not in ("tout décocher", "tout cocher"):
            result.append(text)

    # dédoublonnage stable
    seen, unique = set(), []
    for name in result:
        key = normalize(name)
        if key not in seen:
            seen.add(key)
            unique.append(name)
    return unique


def select_only(page: Page, name: str) -> None:
    card = page.locator("mat-card").filter(has_text=re.compile("Intervenants", re.I)).first
    options = card.locator("mat-selection-list mat-list-option")
    if not options.count():
        options = card.locator("[role='option']")

    # Décoche toutes les options sélectionnées.
    for i in range(options.count()):
        opt = options.nth(i)
        try:
            selected = (opt.get_attribute("aria-selected") or "").lower() == "true"
            if selected:
                opt.click(force=True)
                page.wait_for_timeout(120)
        except Exception:
            pass

    # DOM Angular peut avoir changé : on recharge les options.
    card = page.locator("mat-card").filter(has_text=re.compile("Intervenants", re.I)).first
    options = card.locator("mat-selection-list mat-list-option")
    if not options.count():
        options = card.locator("[role='option']")

    found = False
    for i in range(options.count()):
        opt = options.nth(i)
        try:
            text = clean(opt.locator(".mat-list-text").inner_text(timeout=700))
        except Exception:
            text = clean(opt.inner_text(timeout=700))
        if normalize(text) == normalize(name):
            opt.scroll_into_view_if_needed()
            opt.click(force=True)
            found = True
            break

    if not found:
        raise RuntimeError(f"Impossible de sélectionner {name}")

    page.wait_for_timeout(1200)


def parse_date(text: str) -> str | None:
    m = re.search(
        r"\b(\d{1,2})\s+(janvier|février|fevrier|mars|avril|mai|juin|juillet|"
        r"août|aout|septembre|octobre|novembre|décembre|decembre)\s+(\d{4})\b",
        text, re.I
    )
    if not m:
        return None
    return date(int(m.group(3)), FRENCH_MONTHS[m.group(2).lower()], int(m.group(1))).isoformat()


def week_label(page: Page) -> str:
    body = clean(page.locator("body").inner_text())
    m = re.search(
        r"\b\d{1,2}\s*[-–]\s*\d{1,2}\s+"
        r"(?:janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|"
        r"septembre|octobre|novembre|décembre|decembre)\s+\d{4}\b",
        body, re.I
    )
    return m.group(0) if m else ""


def css_to_hex(value: str | None) -> str | None:
    match = re.search(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", value or "")
    if not match:
        return None
    rgb = tuple(int(match.group(i)) for i in range(1, 4))
    if max(rgb) - min(rgb) < 15 and max(rgb) > 225:
        return None
    return "#" + "".join(f"{part:02X}" for part in rgb)


def classify_color(color_hex: str | None, event_date: str) -> str:
    if not color_hex:
        return "scheduled" if event_date >= date.today().isoformat() else "unknown"
    red = int(color_hex[1:3], 16)
    green = int(color_hex[3:5], 16)
    blue = int(color_hex[5:7], 16)
    # Code couleur métier du calendrier CRM :
    # vert = effectué, bleu = non débriefé, rouge = annulé,
    # violet = non effectué.
    if green > red * 1.10 and green > blue * 1.05:
        return "completed"
    if blue > red * 1.08 and blue >= green:
        return "pending_debrief"
    if red > 90 and blue > 90 and green < min(red, blue) * .82:
        return "not_completed"
    if red > green * 1.18 and red > blue * 1.10:
        return "cancelled"
    return "scheduled" if event_date >= date.today().isoformat() else "unknown"


def extract_events(page: Page, commercial: str) -> list[dict[str, Any]]:
    raw = page.evaluate("""
    () => {
      const visible = e => {
        const r=e.getBoundingClientRect(), s=getComputedStyle(e);
        return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';
      };
      const all=[...document.querySelectorAll('div,li,tr,p,span')]
        .filter(visible)
        .map(e => {
          const r=e.getBoundingClientRect();
          let node=e, color='';
          const usefulColor = value => {
            const m=(value||'').match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
            if(!m) return false;
            const rgb=[+m[1],+m[2],+m[3]], hi=Math.max(...rgb), lo=Math.min(...rgb);
            return hi-lo>45 && !(hi>235 && lo>220);
          };
          const colorsOf = element => {
            const s=getComputedStyle(element);
            return [s.backgroundColor,s.borderLeftColor,s.borderTopColor,s.color,s.fill,s.stroke]
              .filter(usefulColor);
          };
          // Le statut est souvent porté par une petite pastille enfant et non
          // par la ligne du rendez-vous elle-même.
          const descendants=[...e.querySelectorAll('*')].filter(visible);
          color=descendants.flatMap(colorsOf)[0] || '';
          for(let depth=0; depth<5 && node; depth++, node=node.parentElement){
            color=colorsOf(node)[0] || color;
            if(color) break;
          }
          return {text:(e.innerText||'').replace(/\\s+/g,' ').trim(),x:r.x,y:r.y,color};
        })
        .filter(x => x.x>390 && x.text);
      const dates=all.filter(x => /\\b\\d{1,2}\\s+(janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|novembre|décembre|decembre)\\s+\\d{4}\\b/i.test(x.text))
                     .sort((a,b)=>a.y-b.y);
      const events=all.filter(x => /^\\d{1,2}:\\d{2}\\s*[-–]\\s*\\d{1,2}:\\d{2}\\s+.+/.test(x.text))
                      .sort((a,b)=>a.y-b.y);
      return events.map(ev => {
        const prev=dates.filter(d=>d.y<=ev.y);
        return {text:ev.text,dateText:prev.length?prev[prev.length-1].text:'',color:ev.color||''};
      });
    }
    """)

    label = week_label(page)
    result, seen = [], set()

    for item in raw:
        m = re.match(r"^(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})\s+(.+)$", clean(item["text"]))
        if not m:
            continue

        title = clean(m.group(3))
        # IMPORTANT : les A RELANCER et autres événements sont ignorés.
        if not is_rdv(title):
            continue

        event_date = parse_date(item.get("dateText", ""))
        if not event_date:
            continue

        start, end = m.group(1), m.group(2)
        color_hex = css_to_hex(item.get("color"))
        key = "|".join([normalize(commercial), event_date, start, end, normalize(title)])
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "event_key": key,
            "commercial": commercial,
            "event_date": event_date,
            "start_time": start,
            "end_time": end,
            "title": title,
            "color_hex": color_hex,
            "status_kind": classify_color(color_hex, event_date),
            "week_label": label,
            "source_url": page.url,
        })
    return result


def previous_week(page: Page) -> None:
    before = week_label(page)
    selectors = [
        "button.fc-prev-button",
        "button[title='Précédent']",
        "button:has(mat-icon:text-is('chevron_left'))",
        "button:has(mat-icon:text-is('navigate_before'))",
    ]
    for sel in selectors:
        try:
            item = first_visible(page.locator(sel))
            if item:
                item.click(force=True)
                for _ in range(25):
                    page.wait_for_timeout(200)
                    if week_label(page) != before:
                        return
                return
        except Exception:
            pass
    # secours par position
    for i in range(page.locator("button").count()):
        b = page.locator("button").nth(i)
        try:
            box = b.bounding_box()
            if box and 380 < box["x"] < 540 and 220 < box["y"] < 340:
                b.click(force=True)
                page.wait_for_timeout(900)
                return
        except Exception:
            pass
    raise RuntimeError("Bouton semaine précédente introuvable.")


def next_week(page: Page) -> None:
    before = week_label(page)
    selectors = [
        "button.fc-next-button",
        "button[title='Suivant']",
        "button:has(mat-icon:text-is('chevron_right'))",
        "button:has(mat-icon:text-is('navigate_next'))",
    ]
    for selector in selectors:
        try:
            item = first_visible(page.locator(selector))
            if item:
                item.click(force=True)
                for _ in range(25):
                    page.wait_for_timeout(200)
                    if week_label(page) != before:
                        return
                return
        except Exception:
            pass
    raise RuntimeError("Bouton semaine suivante introuvable.")


def sync_calendar(weeks_back: int = 8, weeks_forward: int = 8, interactive: bool = False) -> None:
    ensure_tables()
    set_sync_state("running", "Synchronisation en cours")

    saved_total = 0
    failures = []

    try:
        with sync_playwright() as p:
            context = launch_context(
                p, PROFILE_DIR, {"width": 1600, "height": 1000}
            )
            page = context.pages[0] if context.pages else context.new_page()

            open_calendar(page)
            if interactive:
                input("Connecte-toi si nécessaire puis appuie sur Entrée…")
                open_calendar(page)

            click_planning(page)
            commercials = get_commercials(page)
            if not commercials:
                raise RuntimeError("Aucun commercial détecté.")

            print(f"{len(commercials)} commerciaux détectés.")

            # Chaque commercial est traité indépendamment.
            # S'il plante, les autres continuent et les résultats déjà trouvés restent en base.
            for ci, commercial in enumerate(commercials, 1):
                print(f"\n[{ci}/{len(commercials)}] {commercial}")
                try:
                    select_only(page, commercial)

                    # Retour à aujourd'hui si le bouton est disponible.
                    try:
                        today = first_visible(page.locator("button.fc-today-button, button:has-text(\"Aujourd'hui\")"))
                        if today:
                            today.click(force=True)
                            page.wait_for_timeout(800)
                    except Exception:
                        pass

                    commercial_saved = 0

                    # Semaine courante puis semaines futures.
                    for wi in range(max(0, weeks_forward) + 1):
                        label = week_label(page)
                        events = extract_events(page, commercial)
                        count = save_events(events)  # sauvegarde AU FIL DE L'EAU
                        saved_total += count
                        commercial_saved += count
                        print(
                            f"  Future {wi+1}/{weeks_forward + 1} — {label or 'période'} : "
                            f"{len(events)} RDV R1/R2 enregistrés"
                        )
                        if wi < weeks_forward:
                            next_week(page)

                    # Retour à aujourd'hui, puis historique sans retraiter la semaine courante.
                    try:
                        today = first_visible(page.locator("button.fc-today-button, button:has-text(\"Aujourd'hui\")"))
                        if today:
                            today.click(force=True)
                            page.wait_for_timeout(800)
                    except Exception:
                        pass
                    for wi in range(max(0, weeks_back)):
                        previous_week(page)
                        label = week_label(page)
                        past_events = extract_events(page, commercial)
                        count = save_events(past_events)
                        saved_total += count
                        commercial_saved += count
                        print(
                            f"  Passé {wi+1}/{weeks_back} — {label or 'période'} : "
                            f"{len(past_events)} RDV R1/R2 enregistrés"
                        )

                    print(f"  Total {commercial}: {commercial_saved} RDV R1/R2")
                except Exception as exc:
                    failures.append(f"{commercial}: {exc}")
                    print(f"  ERREUR {commercial}: {exc}")
                    # Recharge le calendrier pour repartir sur un DOM propre.
                    try:
                        open_calendar(page)
                        click_planning(page)
                    except Exception:
                        pass
                    continue

            context.close()

        rebuild_daily_activity()
        message = f"{saved_total} RDV R1/R2 sauvegardés"
        if failures:
            message += f" ; {len(failures)} commercial(aux) en erreur"
        set_sync_state("completed_with_warnings" if failures else "completed", message, completed=True)

        print("\n" + "="*60)
        print(message)
        if failures:
            print("Erreurs non bloquantes :")
            for f in failures:
                print(" -", f)
    except Exception as exc:
        set_sync_state("error", str(exc), completed=False)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--weeks-back", "--weeks", dest="weeks_back", type=int, default=2)
    parser.add_argument("--weeks-forward", type=int, default=2)
    parser.add_argument("--interactive", action="store_true")
    args = parser.parse_args()
    sync_calendar(max(args.weeks_back, 0), max(args.weeks_forward, 0), args.interactive)
