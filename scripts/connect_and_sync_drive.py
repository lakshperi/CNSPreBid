#!/usr/bin/env python3
"""Connect Google Drive in the browser, then sync photos from the configured folder.

Usage:
  PYTHONPATH=. python scripts/connect_and_sync_drive.py
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from prebidding.db import connect, init_db
from prebidding.services import drive_oauth, drive_sync

ROOT = Path(__file__).resolve().parents[1]
SECRETS = ROOT / ".streamlit" / "secrets.toml"
FOLDER_FALLBACK = "15XIK7dkLBgYNNqRG0Wn7HYUU9Wc1Nes4"


def main() -> None:
    if not SECRETS.exists():
        raise SystemExit(f"Missing {SECRETS}")
    secrets = tomllib.loads(SECRETS.read_text(encoding="utf-8"))

    print("Opening browser for Google Drive consent...")
    print("Ensure OAuth Authorized redirect URIs include:")
    for uri in drive_oauth.REQUIRED_REDIRECT_URIS:
        print(f"  - {uri}")
    drive_oauth.connect_drive_interactive(secrets)
    print("Drive connected.")

    conn = connect()
    init_db(conn)
    folder = (
        drive_sync.get_setting(conn, "drive_folder_id")
        or str(secrets.get("prebidding", {}).get("drive_folder_id") or "")
        or FOLDER_FALLBACK
    )
    folder_id = drive_sync.extract_folder_id(folder)
    drive_sync.set_setting(conn, "drive_folder_id", folder_id)
    token = drive_oauth.get_valid_access_token()
    meta = drive_oauth.probe_folder(token, folder_id)
    print(f"Syncing folder: {meta.get('name')} ({folder_id})")
    result = drive_sync.sync_photos_from_drive(conn, access_token=token, folder_id=folder_id)
    print(
        f"Done. new={result.imported} updated={result.updated} rejected={result.rejected}"
    )
    for err in result.errors:
        print("  -", err)
    conn.close()


if __name__ == "__main__":
    main()
