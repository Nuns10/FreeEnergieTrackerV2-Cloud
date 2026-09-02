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

    email_input.fill(email)
    password_input.fill(password)
    login_button = page.locator(
        "button:has-text('Connexion'), button:has-text('Se connecter')"
    ).first
    login_button.click()
    page.wait_for_url(lambda url: "/sign-in" not in url, timeout=90_000)
    page.goto(target_url, wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(1_500)
