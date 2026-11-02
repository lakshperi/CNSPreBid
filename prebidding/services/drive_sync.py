"""Sync item photographs from a configured Google Drive folder."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from prebidding.db import DATA_DIR
from prebidding.services.admin import record_sync, upsert_item

IMAGE_MIME_PREFIXES = ("image/",)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic"}
DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"


@dataclass
class DriveSyncResult:
    imported: int
    rejected: int
    updated: int
    errors: list[str]
    folder_id: str
    sync_id: str | None = None


class DriveSyncError(Exception):
    """Raised when Drive sync cannot proceed."""


def extract_folder_id(value: str) -> str:
    """Accept a raw folder ID or a Google Drive folder URL."""
    raw = (value or "").strip()
    if not raw:
        raise DriveSyncError("Google Drive folder ID is empty.")

    # https://drive.google.com/drive/folders/<id>
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", raw)
    if match:
        return match.group(1)

    # https://drive.google.com/open?id=<id>
    match = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", raw)
    if match:
        return match.group(1)

    if re.fullmatch(r"[a-zA-Z0-9_-]+", raw):
        return raw

    raise DriveSyncError(
        "Could not parse a Google Drive folder ID. Paste the folder ID or full folder URL."
    )


def item_id_from_filename(name: str) -> str:
    stem = Path(name).stem.strip()
    stem = re.sub(r"\s+", "-", stem)
    stem = re.sub(r"-{2,}", "-", stem)
    stem = re.sub(r"[^A-Za-z0-9._-]", "", stem).strip("-._")
    if not stem:
        raise DriveSyncError(f"Could not derive item id from filename '{name}'.")
    return stem.upper()


def display_name_from_filename(name: str) -> str:
    return Path(name).stem.strip()


def _headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def list_image_files(access_token: str, folder_id: str) -> list[dict[str, Any]]:
    query = (
        f"'{folder_id}' in parents and trashed = false and "
        "("
        "mimeType contains 'image/' or "
        "name contains '.jpg' or name contains '.jpeg' or "
        "name contains '.png' or name contains '.webp' or name contains '.gif'"
        ")"
    )
    params = {
        "q": query,
        "spaces": "drive",
        "fields": "nextPageToken, files(id, name, mimeType, modifiedTime)",
        "pageSize": 1000,
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    files: list[dict[str, Any]] = []
    page_token: str | None = None
    with httpx.Client(timeout=60.0) as client:
        while True:
            page_params = dict(params)
            if page_token:
                page_params["pageToken"] = page_token
            response = client.get(
                DRIVE_FILES_URL,
                headers=_headers(access_token),
                params=page_params,
            )
            if response.status_code == 401:
                raise DriveSyncError(
                    "Google access token is missing Drive permission or expired. "
                    "Sign out, sign back in, and approve Drive access."
                )
            if response.status_code == 403:
                raise DriveSyncError(
                    "Access denied to this Drive folder. Confirm the folder is shared "
                    "with your Google account and Drive API is enabled in the GCP project."
                )
            if response.status_code >= 400:
                raise DriveSyncError(
                    f"Drive list failed ({response.status_code}): {response.text[:300]}"
                )
            payload = response.json()
            for file in payload.get("files", []):
                mime = (file.get("mimeType") or "").lower()
                ext = Path(file.get("name", "")).suffix.lower()
                if mime.startswith(IMAGE_MIME_PREFIXES) or ext in IMAGE_EXTENSIONS:
                    files.append(file)
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
    return files


def download_file(access_token: str, file_id: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    url = f"{DRIVE_FILES_URL}/{quote(file_id)}?alt=media&supportsAllDrives=true"
    with httpx.Client(timeout=120.0) as client:
        with client.stream("GET", url, headers=_headers(access_token)) as response:
            if response.status_code >= 400:
                body = response.read().decode("utf-8", errors="ignore")[:300]
                raise DriveSyncError(
                    f"Download failed for {file_id} ({response.status_code}): {body}"
                )
            with destination.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?", (key,)
    ).fetchone()
    return None if row is None else str(row["value"])


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO app_settings(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def resolve_folder_id(conn: sqlite3.Connection, secrets: Any) -> str:
    stored = get_setting(conn, "drive_folder_id")
    if stored:
        return extract_folder_id(stored)
    try:
        configured = secrets["prebidding"].get("drive_folder_id", "")
    except Exception:
        configured = ""
    if configured:
        return extract_folder_id(str(configured))
    raise DriveSyncError(
        "No Google Drive folder configured. Paste a folder URL/ID in Admin → Sync."
    )


def sync_photos_from_drive(
    conn: sqlite3.Connection,
    *,
    access_token: str,
    folder_id: str,
    default_initial_bid: float = 1.0,
    auto_publish: bool = True,
) -> DriveSyncResult:
    if not access_token:
        raise DriveSyncError("Missing Google access token.")

    folder_id = extract_folder_id(folder_id)
    files = list_image_files(access_token, folder_id)
    photos_dir = DATA_DIR / "photos"
    imported = updated = rejected = 0
    errors: list[str] = []

    for file in files:
        name = file.get("name") or "unnamed"
        try:
            item_id = item_id_from_filename(name)
            ext = Path(name).suffix.lower() or ".jpg"
            destination = photos_dir / f"{item_id}{ext}"
            download_file(access_token, file["id"], destination)

            existing = conn.execute(
                "SELECT item_id, initial_bid_amount, published, status FROM bid_items WHERE item_id = ?",
                (item_id,),
            ).fetchone()
            display_name = display_name_from_filename(name) or item_id
            if existing:
                amount = float(existing["initial_bid_amount"])
                if amount <= 0:
                    amount = float(default_initial_bid)
                published = bool(existing["published"]) or auto_publish
                status = existing["status"]
                if auto_publish and status == "Upcoming":
                    status = "Open"
                upsert_item(
                    conn,
                    item_id=item_id,
                    name=display_name,
                    description="Imported from Google Drive",
                    photograph_ref=str(destination),
                    initial_bid_amount=amount,
                    status=status,
                    published=published,
                )
                if published:
                    conn.execute(
                        "UPDATE bid_items SET published = 1, status = ?, initial_bid_amount = ? WHERE item_id = ?",
                        (status, amount, item_id),
                    )
                updated += 1
            else:
                status = "Open" if auto_publish else "Upcoming"
                upsert_item(
                    conn,
                    item_id=item_id,
                    name=display_name,
                    description="Imported from Google Drive",
                    photograph_ref=str(destination),
                    initial_bid_amount=float(default_initial_bid),
                    status=status,
                    published=auto_publish,
                )
                imported += 1
        except Exception as exc:  # noqa: BLE001
            rejected += 1
            errors.append(f"{name}: {exc}")

    status = "Success" if rejected == 0 else ("Partial" if imported + updated else "Failed")
    sync_id = record_sync(
        conn,
        source=f"GoogleDrive:{folder_id}",
        status=status,
        imported=imported + updated,
        rejected=rejected,
        error_detail="; ".join(errors[:5]) if errors else None,
    )
    return DriveSyncResult(
        imported=imported,
        updated=updated,
        rejected=rejected,
        errors=errors,
        folder_id=folder_id,
        sync_id=sync_id,
    )


def google_access_token_from_user(user_obj: Any) -> str:
    """Fallback: Streamlit SSO access token (often lacks Drive scopes)."""
    tokens = getattr(user_obj, "tokens", None)
    if tokens is None:
        raise DriveSyncError(
            "Google Drive is not connected. Use Connect Google Drive in Admin → Sync."
        )
    token = None
    if hasattr(tokens, "get"):
        token = tokens.get("access")
    if not token:
        token = getattr(tokens, "access", None)
    if not token:
        raise DriveSyncError(
            "No Google access token available. Connect Google Drive in Admin → Sync."
        )
    return str(token)


def resolve_access_token(user_obj: Any | None = None, secrets: Any | None = None) -> str:
    """Prefer Drive secrets/local refresh token; fall back to SSO token."""
    try:
        from prebidding.services.drive_oauth import get_valid_access_token

        return get_valid_access_token(secrets)
    except Exception:
        if user_obj is None:
            raise DriveSyncError(
                "Google Drive is not connected. Add [drive] refresh_token in Secrets "
                "or Connect Google Drive locally."
            )
        return google_access_token_from_user(user_obj)
