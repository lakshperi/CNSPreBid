"""Dedicated Google Drive OAuth (offline) separate from Streamlit SSO login.

Uses a local loopback server so Drive consent does not depend on Streamlit's
OIDC callback (which does not reliably grant Drive scopes).
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from prebidding.db import DATA_DIR

DRIVE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive.readonly",
]
TOKEN_PATH = DATA_DIR / "drive_token.json"
# Preferred loopback ports. Add matching Authorized redirect URIs in Google Cloud.
LOOPBACK_PORTS = (8090, 8091, 8092, 8093, 8094)
LOOPBACK_PORT = LOOPBACK_PORTS[0]
LOOPBACK_REDIRECT = f"http://127.0.0.1:{LOOPBACK_PORT}/"
REQUIRED_REDIRECT_URIS = tuple(
    [f"http://127.0.0.1:{p}/" for p in LOOPBACK_PORTS]
    + [f"http://localhost:{p}/" for p in LOOPBACK_PORTS]
    + ["http://localhost:8501/oauth2callback"]
)


def _first_free_port(candidates: tuple[int, ...] = LOOPBACK_PORTS) -> int:
    for port in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise DriveAuthError(
        "No free OAuth loopback port found in 8090-8094. "
        "Close other Connect Google Drive attempts and try again."
    )


class DriveAuthError(Exception):
    pass


def _client_config(secrets: Any) -> dict[str, Any]:
    try:
        auth = secrets["auth"]
        client_id = auth["client_id"]
        client_secret = auth["client_secret"]
    except Exception as exc:
        raise DriveAuthError("Missing Google OAuth client_id/client_secret in secrets.") from exc
    # InstalledAppFlow expects the "installed" shape for loopback auth.
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [
                LOOPBACK_REDIRECT,
                f"http://localhost:{LOOPBACK_PORT}/",
                "http://localhost",
            ],
        }
    }


def connect_drive_interactive(secrets: Any) -> Credentials:
    """Open a browser, complete Google consent, and persist a refresh token."""
    port = _first_free_port()
    flow = InstalledAppFlow.from_client_config(_client_config(secrets), scopes=DRIVE_SCOPES)
    try:
        creds = flow.run_local_server(
            bind_addr="127.0.0.1",
            port=port,
            prompt="consent",
            access_type="offline",
            open_browser=True,
        )
    except OSError as exc:
        raise DriveAuthError(
            f"Could not start local OAuth listener on port {port}. Details: {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        needed = f"http://127.0.0.1:{port}/ and http://localhost:{port}/"
        raise DriveAuthError(
            "Drive authorization failed (often redirect_uri_mismatch). "
            "In Google Cloud → Clients → your Web client → Authorized redirect URIs, add "
            f"{needed} (and ideally 8090-8094). Details: {exc}"
        ) from exc
    if not creds or not creds.token:
        raise DriveAuthError("Google did not return Drive credentials.")
    save_credentials(creds)
    return creds


def save_credentials(creds: Credentials) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or DRIVE_SCOPES),
    }
    TOKEN_PATH.write_text(json.dumps(payload), encoding="utf-8")


def load_credentials() -> Credentials | None:
    if not TOKEN_PATH.exists():
        return None
    data = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    if not data.get("refresh_token") and not data.get("token"):
        return None
    return Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes"),
    )


def clear_credentials() -> None:
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()


def get_valid_access_token() -> str:
    creds = load_credentials()
    if creds is None:
        raise DriveAuthError(
            "Google Drive is not connected. Click Connect Google Drive & Sync first."
        )
    if not creds.valid:
        if creds.refresh_token:
            creds.refresh(Request())
            save_credentials(creds)
        else:
            clear_credentials()
            raise DriveAuthError(
                "Drive token expired and no refresh token is stored. Reconnect Google Drive."
            )
    if not creds.token:
        raise DriveAuthError("Drive access token is empty. Reconnect Google Drive.")
    return creds.token


def drive_connected() -> bool:
    return load_credentials() is not None


def probe_folder(access_token: str, folder_id: str) -> dict[str, Any]:
    url = f"https://www.googleapis.com/drive/v3/files/{folder_id}"
    params = {"fields": "id,name,mimeType", "supportsAllDrives": "true"}
    response = httpx.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        params=params,
        timeout=30.0,
    )
    if response.status_code >= 400:
        raise DriveAuthError(
            f"Cannot access folder ({response.status_code}): {response.text[:250]}"
        )
    return response.json()
