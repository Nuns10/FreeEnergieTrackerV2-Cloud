"""Contexte Chromium local ou GitHub Actions, sans secret dans le code."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from urllib.parse import quote


CRM_BASE_URL = "https://crm.freeenergie.fr"


def launch_context(playwright, profile_dir: Path, viewport: dict):
    if not os.getenv("GITHUB_ACTIONS"):
        return playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir), headless=False, viewport=viewport
        )

    encoded = os.getenv("CRM_STORAGE_STATE_B64", "").strip()
    if not encoded:
        raise RuntimeError("Secret GitHub CRM_STORAGE_STATE_B64 absent")
    state = json.loads(base64.b64decode(encoded).decode("utf-8"))
    browser = playwright.chromium.launch(headless=True)
    return browser.new_context(storage_state=state, viewport=viewport)


def ensure_crm_login(page, target_url: str) -> None:
    """Reconnecte le robot si la session GitHub enregistrée a expiré."""
    authenticated = page.locator(
        "a[href='/dashboard'], button[aria-label='Ouvrir le menu utilisateur']"
    )
    # La nouvelle interface CRM rend le menu après le chargement HTML. Attendre
    # sa présence évite de prendre une page encore en cours d'initialisation
    # pour une session expirée.
    try:
        authenticated.first.wait_for(state="attached", timeout=15_000)
        return
    except Exception:
        pass

    email = os.getenv("CRM_LOGIN_EMAIL", "").strip()
    password = os.getenv("CRM_LOGIN_PASSWORD", "")
    if not email or not password:
        raise RuntimeError(
            "Session CRM expirée et secrets CRM_LOGIN_EMAIL/CRM_LOGIN_PASSWORD absents."
        )

    return_path = "/" + target_url.split("/", 3)[-1]
    page.goto(
        f"{CRM_BASE_URL}/sign-in?returnUrl={quote(return_path, safe='')}",
        wait_until="domcontentloaded",
        timeout=90_000,
    )
    # Une session encore valide peut être redirigée directement vers le CRM.
    try:
        authenticated.first.wait_for(state="attached", timeout=5_000)
        page.goto(target_url, wait_until="domcontentloaded", timeout=90_000)
        return
    except Exception:
        pass

    email_input = page.locator(
        "input[type='email'], input[name='email'], input[autocomplete='username'], "
        "input[placeholder*='mail' i]"
    ).first
    password_input = page.locator(
        "input[type='password'], input[name='password'], input[autocomplete='current-password']"
    ).first
    try:
        email_input.wait_for(state="visible", timeout=20_000)
        password_input.wait_for(state="visible", timeout=20_000)
    except Exception as exc:
        raise RuntimeError(
            f"Formulaire de connexion CRM introuvable (page actuelle : {page.url})."
        ) from exc

    auth_events: list[str] = []

    def record_auth_response(response) -> None:
        if "/auth/" in response.url:
            auth_events.append(f"{response.status} {response.url.split('?')[0]}")

    page.on("response", record_auth_response)
    login_button = page.locator(
        "button:has-text('Connexion'), button:has-text('Se connecter'), "
        "button[type='submit']"
    ).first
    connected = False
    for attempt in range(3):
        email_input.fill(email)
        password_input.fill(password)
        if attempt == 0:
            login_button.click(force=True)
        else:
            password_input.press("Enter")
        # La nouvelle page de connexion change parfois d'URL sans événement de
        # navigation classique. On contrôle donc directement l'URL et le menu
        # authentifié, avec plusieurs tentatives en cas de réponse API lente.
        for _ in range(60):
            page.wait_for_timeout(500)
            if "/sign-in" not in page.url or authenticated.count():
                connected = True
                break
        if connected:
            break
        messages = page.locator("[role='alert'], mat-error, .alert, .error").all_text_contents()
        print(
            f"Connexion CRM sans redirection (tentative {attempt + 1}/3)"
            + (f" : {' | '.join(messages)[:400]}" if messages else ".")
        )
        page.wait_for_timeout(1000)
    if not connected:
        diagnostic_dir = Path(__file__).resolve().parent / "diagnostic_cloud"
        diagnostic_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(diagnostic_dir / "connexion-crm.png"), full_page=True)
        print("Réponses d'authentification CRM : " + (" | ".join(auth_events[-20:]) or "aucune"))
        print(
            "État du formulaire CRM : "
            f"bouton_actif={login_button.is_enabled()}, "
            f"url={page.url.split('?')[0]}"
        )
        raise RuntimeError("La connexion CRM reste sur la page d'identification après 3 tentatives.")
    page.goto(target_url, wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(1_500)
