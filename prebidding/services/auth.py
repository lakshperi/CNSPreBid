"""Authentication helpers: Google SSO provisioning and optional demo users."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class User:
    google_sub: str
    email: str
    display_name: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "Administrator"


DEMO_USERS = [
    User("google-admin-001", "admin@cns.example", "Alex Admin", "Administrator"),
    User("google-bidder-001", "jamie@cns.example", "Jamie Bidder", "Bidder"),
    User("google-bidder-002", "riley@cns.example", "Riley Bidder", "Bidder"),
    User("google-bidder-003", "casey@cns.example", "Casey Bidder", "Bidder"),
]


def upsert_user(conn: sqlite3.Connection, user: User) -> User:
    conn.execute(
        """
        INSERT INTO users(google_sub, email, display_name, role)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(google_sub) DO UPDATE SET
            email=excluded.email,
            display_name=excluded.display_name,
            role=excluded.role
        """,
        (user.google_sub, user.email, user.display_name, user.role),
    )
    return user


def get_user(conn: sqlite3.Connection, google_sub: str) -> User | None:
    row = conn.execute(
        "SELECT google_sub, email, display_name, role FROM users WHERE google_sub = ?",
        (google_sub,),
    ).fetchone()
    if not row:
        return None
    return User(**dict(row))


def list_demo_users() -> list[User]:
    return list(DEMO_USERS)


def ensure_demo_users(conn: sqlite3.Connection) -> None:
    for user in DEMO_USERS:
        upsert_user(conn, user)


def _normalize_emails(emails: Iterable[str] | None) -> set[str]:
    if not emails:
        return set()
    return {e.strip().lower() for e in emails if e and str(e).strip()}


def resolve_role(email: str, admin_emails: Iterable[str] | None, existing: User | None) -> str:
    admins = _normalize_emails(admin_emails)
    if email.strip().lower() in admins:
        return "Administrator"
    if existing and existing.role == "Administrator":
        # Keep previously granted admin unless explicitly removed from allowlist later.
        return "Administrator"
    return "Bidder"


def user_from_google_identity(
    identity: Any,
    *,
    admin_emails: Iterable[str] | None = None,
    existing: User | None = None,
) -> User:
    """Map a Streamlit/Google OIDC identity token to a domain User."""
    if hasattr(identity, "get"):
        sub = identity.get("sub") or identity.get("user_id")
        email = identity.get("email")
        name = identity.get("name") or identity.get("given_name")
    else:
        sub = getattr(identity, "sub", None)
        email = getattr(identity, "email", None)
        name = getattr(identity, "name", None)

    if not sub:
        raise ValueError("Google identity is missing subject (sub).")
    if not email:
        raise ValueError("Google identity is missing email. Enable the email scope.")

    display_name = name or email.split("@")[0]
    role = resolve_role(email, admin_emails, existing)
    return User(
        google_sub=str(sub),
        email=str(email).strip().lower(),
        display_name=str(display_name),
        role=role,
    )


def provision_google_user(
    conn: sqlite3.Connection,
    identity: Any,
    *,
    admin_emails: Iterable[str] | None = None,
) -> User:
    if hasattr(identity, "get"):
        sub = identity.get("sub")
    else:
        sub = getattr(identity, "sub", None)
    existing = get_user(conn, str(sub)) if sub else None
    user = user_from_google_identity(identity, admin_emails=admin_emails, existing=existing)
    return upsert_user(conn, user)


def google_sso_configured(secrets: Any) -> bool:
    """Return True when Streamlit [auth] secrets look ready for Google OIDC."""
    try:
        auth_cfg = secrets["auth"]
    except Exception:
        return False

    def _get(key: str) -> str:
        try:
            value = auth_cfg[key]
        except Exception:
            return ""
        return str(value).strip()

    required = (
        "redirect_uri",
        "cookie_secret",
        "client_id",
        "client_secret",
        "server_metadata_url",
    )
    for key in required:
        value = _get(key)
        if not value or value.lower() in {"xxx", "changeme", "replace-me"}:
            return False
    return True


def admin_emails_from_secrets(secrets: Any) -> list[str]:
    try:
        cfg = secrets["prebidding"]
        emails = cfg.get("admin_emails", [])
    except Exception:
        return []
    if isinstance(emails, str):
        return [emails]
    return list(emails)


def allow_demo_login(secrets: Any) -> bool:
    try:
        return bool(secrets["prebidding"].get("allow_demo_login", False))
    except Exception:
        return False
