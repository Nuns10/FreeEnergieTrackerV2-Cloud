"""Contexte Chromium local ou GitHub Actions, sans secret dans le code."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path


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
    if "/sign-in" not in page.url and page.get_by_role("button", name="Connexion").count() == 0:
        return

    email = os.getenv("CRM_LOGIN_EMAIL", "").strip()
    password = os.getenv("CRM_LOGIN_PASSWORD", "")
    if not email or not password:
        raise RuntimeError(
            "Session CRM expirée et secrets CRM_LOGIN_EMAIL/CRM_LOGIN_PASSWORD absents."
        )

    email_input = page.locator("input[placeholder='Email'], input[placeholder='email']").first
    password_input = page.locator("input[type='password']").first
    if email_input.count() == 0 or password_input.count() == 0:
        raise RuntimeError("Formulaire de connexion CRM introuvable.")

    email_input.fill(email)
    password_input.fill(password)
    page.get_by_role("button", name="Connexion").click()
    page.wait_for_url(lambda url: "/sign-in" not in url, timeout=90_000)
    page.goto(target_url, wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(1_500)
