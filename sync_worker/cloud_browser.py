"""Contexte Chromium local ou GitHub Actions, sans secret dans le code."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path


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

