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


def load_credentials_from_file() -> Credentials | None:
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
        scopes=data.get("scopes") or DRIVE_SCOPES,
    )


def load_credentials_from_secrets(secrets: Any) -> Credentials | None:
    """Cloud-friendly Drive auth: store refresh token in Streamlit secrets [drive]."""
    try:
        drive = secrets["drive"]
    except Exception:
        return None

    def _get(key: str, default: str = "") -> str:
        try:
            value = drive[key]
        except Exception:
            value = getattr(drive, key, default)
        return str(value or default).strip()

    refresh_token = _get("refresh_token")
    if not refresh_token:
        return None

    # Fall back to [auth] client credentials when not duplicated under [drive].
    client_id = _get("client_id")
    client_secret = _get("client_secret")
    if not client_id or not client_secret:
        try:
            auth = secrets["auth"]
            client_id = client_id or str(auth["client_id"]).strip()
            client_secret = client_secret or str(auth["client_secret"]).strip()
        except Exception:
            pass
    if not client_id or not client_secret:
        return None

    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=_get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=client_id,
        client_secret=client_secret,
        scopes=DRIVE_SCOPES,
    )


def load_credentials(secrets: Any | None = None) -> Credentials | None:
    if secrets is not None:
        from_secrets = load_credentials_from_secrets(secrets)
        if from_secrets is not None:
            return from_secrets
    return load_credentials_from_file()


def clear_credentials() -> None:
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()


def get_valid_access_token(secrets: Any | None = None) -> str:
    creds = load_credentials(secrets)
    if creds is None:
        raise DriveAuthError(
            "Google Drive is not connected. On Streamlit Cloud, add a [drive] "
            "refresh_token in Secrets. Locally, use Connect Google Drive & Sync."
        )
    if not creds.valid:
        if creds.refresh_token:
            creds.refresh(Request())
            # Persist refreshed access token only for local file-based creds.
            if secrets is None or load_credentials_from_secrets(secrets) is None:
                save_credentials(creds)
        else:
            if secrets is None:
                clear_credentials()
            raise DriveAuthError(
                "Drive token expired and no refresh token is stored. Reconnect Google Drive."
            )
    if not creds.token:
        raise DriveAuthError("Drive access token is empty. Reconnect Google Drive.")
    return creds.token


def drive_connected(secrets: Any | None = None) -> bool:
    return load_credentials(secrets) is not None


def running_on_streamlit_cloud() -> bool:
    import os

    if os.environ.get("STREAMLIT_SHARING_MODE"):
        return True
    if os.environ.get("STREAMLIT_CLOUD") or os.environ.get("STREAMLIT_SHARING"):
        return True
    hostname = os.environ.get("HOSTNAME", "").lower()
    if "streamlit" in hostname:
        return True
    # Community Cloud apps typically run as user appuser.
    if os.environ.get("USER") == "appuser":
        return True
    return False


def drive_secrets_status(secrets: Any) -> dict[str, Any]:
    """Non-sensitive diagnostics for the Sync UI."""
    has_drive_section = False
    has_refresh = False
    try:
        drive = secrets["drive"]
        has_drive_section = True
        try:
            has_refresh = bool(str(drive["refresh_token"]).strip())
        except Exception:
            has_refresh = bool(str(getattr(drive, "refresh_token", "")).strip())
    except Exception:
        pass
    folder = ""
    try:
        folder = str(secrets["prebidding"].get("drive_folder_id", "") or "")
    except Exception:
        folder = ""
    return {
        "has_drive_section": has_drive_section,
        "has_refresh_token": has_refresh,
        "folder_id": folder,
        "connected": drive_connected(secrets),
        "on_cloud": running_on_streamlit_cloud(),
    }


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
