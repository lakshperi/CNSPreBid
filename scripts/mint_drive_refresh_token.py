#!/usr/bin/env python3
"""Mint a new Google Drive refresh_token for Streamlit Cloud Secrets.

Run on your laptop (browser consent required):

  PYTHONPATH=. .venv/bin/python scripts/mint_drive_refresh_token.py

Then paste the printed refresh_token into Streamlit Cloud → Settings → Secrets → [drive].
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from prebidding.services import drive_oauth

ROOT = Path(__file__).resolve().parents[1]
SECRETS = ROOT / ".streamlit" / "secrets.toml"


def main() -> None:
    if not SECRETS.exists():
        raise SystemExit(
            f"Missing {SECRETS}. Create it with [auth] client_id/client_secret "
            "(use the NEW client_secret), then re-run."
        )
    secrets = tomllib.loads(SECRETS.read_text(encoding="utf-8"))
    print("Ensure Google Cloud OAuth client Authorized redirect URIs include:")
    for uri in drive_oauth.REQUIRED_REDIRECT_URIS:
        print(f"  - {uri}")
    print("\nOpening browser for Drive consent (offline access)...")
    creds = drive_oauth.connect_drive_interactive(secrets)
    if not creds.refresh_token:
        raise SystemExit(
            "Google did not return a refresh_token. Revoke prior app access at "
            "https://myaccount.google.com/permissions then try again with prompt=consent."
        )
    print("\nPaste this into Streamlit Cloud Secrets under [drive]:\n")
    print(f'refresh_token = "{creds.refresh_token}"')
    print(f'client_id = "{creds.client_id}"')
    print(f'client_secret = "{creds.client_secret}"')
    print('token_uri = "https://oauth2.googleapis.com/token"')
    print("\nThen Save Secrets and Reboot the app. Open Administrator → Sync → Sync now.")


if __name__ == "__main__":
    main()
