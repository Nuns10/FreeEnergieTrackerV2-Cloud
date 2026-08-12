#!/usr/bin/env python3
"""Exporte la session CRM locale au format attendu par GitHub Secrets."""
from __future__ import annotations

import base64
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path.home() / "Downloads" / "FreeEnergieTrackerV2"
PROFILE = ROOT / "browser-profile"
OUTPUT = ROOT / "crm_storage_state.json"

with sync_playwright() as playwright:
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE), headless=False
    )
    page = context.pages[0] if context.pages else context.new_page()
    page.goto("https://crm.freeenergie.fr/lead")
    input("Vérifiez que le CRM est connecté, puis appuyez sur Entrée…")
    context.storage_state(path=str(OUTPUT))
    context.close()

encoded = base64.b64encode(OUTPUT.read_bytes()).decode("ascii")
print("\nCopiez la ligne suivante dans le secret GitHub CRM_STORAGE_STATE_B64 :\n")
print(encoded)
print("\nNe partagez jamais cette valeur : elle donne accès à la session CRM.")

