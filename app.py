"""Streamlit UI for the Pre-Bidding Application."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from prebidding.db import connect, init_db
from prebidding.seed import seed
from prebidding.services import admin, auth, bidding
from prebidding.services.auth import User
from prebidding.services.bidding import BidError

st.set_page_config(
    page_title="CNS Pre-Bidding",
    page_icon="🏷️",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_resource
def get_connection():
    seed(force=False)
    conn = connect()
    init_db(conn)
    auth.ensure_demo_users(conn)
    return conn


def _sso_enabled() -> bool:
    return auth.google_sso_configured(st.secrets)


def _demo_enabled() -> bool:
    if not _sso_enabled():
        return True
    return auth.allow_demo_login(st.secrets)


def _set_session_user(user: User) -> None:
    st.session_state["user"] = {
        "google_sub": user.google_sub,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
    }


def sync_google_session(conn) -> User | None:
    """If Streamlit Google OIDC session is active, provision/upsert the domain user."""
    if not _sso_enabled():
        return None
    if not getattr(st.user, "is_logged_in", False):
        return None
    try:
        user = auth.provision_google_user(
            conn,
            st.user,
            admin_emails=auth.admin_emails_from_secrets(st.secrets),
        )
    except ValueError as exc:
        st.error(f"Google sign-in succeeded but profile mapping failed: {exc}")
        st.button("Sign out of Google", on_click=st.logout)
        st.stop()
    _set_session_user(user)
    st.session_state["auth_provider"] = "google"
    return user


def require_login(conn) -> User | None:
    google_user = sync_google_session(conn)
    if google_user is not None:
        return google_user
    user_data = st.session_state.get("user")
    if not user_data:
        return None
    return User(**user_data)


def _demo_login_form(conn) -> None:
    users = auth.list_demo_users()
    labels = {f"{u.display_name} ({u.role}) — {u.email}": u for u in users}
    choice = st.selectbox("Demo identity", list(labels.keys()))
    if st.button("Continue with demo identity", use_container_width=True):
        user = labels[choice]
        auth.upsert_user(conn, user)
        _set_session_user(user)
        st.session_state["auth_provider"] = "demo"
        st.rerun()


def login_page(conn) -> None:
    st.title("CNS Pre-Bidding")
    st.markdown(
        "Centralized pre-bidding platform — transparent amounts, protected identities, "
        "auditable outcomes."
    )

    if _sso_enabled():
        st.subheader("Sign in with Google")
        st.write("Authenticate with your Google account to browse items and place bids.")
        st.button(
            "Log in with Google",
            type="primary",
            use_container_width=True,
            on_click=st.login,
        )
        if _demo_enabled():
            with st.expander("Demo login (local testing)"):
                _demo_login_form(conn)
        return

    st.warning(
        "Google SSO is not configured yet. Add `.streamlit/secrets.toml` from "
        "`secrets.toml.example`, then restart Streamlit."
    )
    with st.expander("Google Cloud setup checklist", expanded=True):
        st.markdown(
            """
1. Create an OAuth **Web application** client in [Google Auth Platform](https://console.cloud.google.com/auth/clients).
2. Add authorized redirect URI: `http://localhost:8501/oauth2callback`
3. Copy Client ID and Client secret into `.streamlit/secrets.toml`
4. Set a strong random `cookie_secret`
5. Put admin Google emails in `[prebidding].admin_emails`
6. While the OAuth app is in **Testing**, add those emails as test users under Audience
7. Restart: `PYTHONPATH=. streamlit run app.py`
            """
        )
    st.subheader("Demo login (until Google SSO is configured)")
    _demo_login_form(conn)


def sidebar(user: User) -> str:
    with st.sidebar:
        st.markdown(f"**{user.display_name}**")
        provider = st.session_state.get("auth_provider", "demo")
        st.caption(f"{user.email} · {user.role} · via {provider}")
        pages = ["Available Items", "My Bids"]
        if user.is_admin:
            pages.append("Administrator")
        page = st.radio("Navigate", pages)
        if st.button("Sign out"):
            st.session_state.pop("user", None)
            st.session_state.pop("auth_provider", None)
            if provider == "google" and getattr(st.user, "is_logged_in", False):
                st.logout()
            else:
                st.rerun()
        st.divider()
        st.caption("Auth: Google OIDC · Store: SQLite")
    return page


def money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"${value:,.2f}"


def available_items(conn, user: User) -> None:
    st.title("Available Items")
    bidding.apply_due_closures(conn)
    # Admins also see unpublished Drive imports so photos are visible immediately.
    items = admin.list_items(conn, published_only=not user.is_admin)
    if user.is_admin:
        unpublished = [i for i in items if not i["published"]]
        if unpublished:
            st.info(
                f"{len(unpublished)} unpublished item(s) from Drive are shown only to administrators. "
                "Use Administrator → Items → Publish item to open bidding."
            )
    if not items:
        st.warning("No items yet. Admins can sync photos from Google Drive.")
        return

    cols = st.columns(3)
    for idx, item in enumerate(items):
        summary = bidding.item_summary(conn, item["item_id"])
        with cols[idx % 3]:
            photo = Path(item["photograph_ref"])
            if photo.exists():
                st.image(str(photo), use_container_width=True)
            else:
                st.warning("Photo file missing on disk")
            st.subheader(item["name"])
            st.caption(item["item_id"])
            if not item["published"]:
                st.caption("Unpublished")
            st.write(item.get("description") or "")
            st.markdown(
                f"**Status:** {item['status']}  \n"
                f"**Initial:** {money(item['initial_bid_amount'])}  \n"
                f"**Highest:** {money(summary['highest_bid'] or item['initial_bid_amount'])}  \n"
                f"**Bids:** {summary['bid_count']}"
            )
            if st.button("Open", key=f"open-{item['item_id']}", use_container_width=True):
                st.session_state["selected_item"] = item["item_id"]
                st.session_state["view"] = "item_detail"
                st.rerun()


def item_detail(conn, user: User) -> None:
    item_id = st.session_state.get("selected_item")
    if not item_id:
        st.session_state["view"] = "list"
        st.rerun()

    if st.button("← Back to items"):
        st.session_state["view"] = "list"
        st.rerun()

    try:
        summary = bidding.item_summary(conn, item_id)
    except BidError as exc:
        st.error(str(exc))
        return

    left, right = st.columns([1, 1])
    with left:
        photo = Path(summary["photograph_ref"])
        if photo.exists():
            st.image(str(photo), use_container_width=True)
        st.header(summary["name"])
        st.write(summary.get("description") or "")
        st.markdown(
            f"**Status:** {summary['status']}  \n"
            f"**Initial bid:** {money(summary['initial_bid_amount'])}  \n"
            f"**Current highest:** {money(summary['highest_bid'] or summary['initial_bid_amount'])}  \n"
            f"**Minimum acceptable:** {money(summary['minimum_acceptable'])}  \n"
            f"**Bid count:** {summary['bid_count']}"
        )
        if summary["status"] == "Closed" and summary.get("winner") and user.is_admin:
            w = summary["winner"]
            st.success(
                f"Winner: {w['display_name']} ({w['email']}) at {money(w['amount'])}"
            )
        elif summary["status"] == "Closed":
            st.success("Bidding closed. Winner finalized.")

    with right:
        st.subheader("Place a bid")
        if user.is_admin:
            st.warning("Administrators cannot place bids in this release.")
        elif summary["status"] != "Open":
            st.info(f"Item is {summary['status']}; bidding is disabled.")
        else:
            amount = st.number_input(
                "Your bid amount",
                min_value=float(summary["minimum_acceptable"]),
                value=float(summary["minimum_acceptable"]),
                step=5.0,
                format="%.2f",
            )
            if st.button("Submit bid", type="primary"):
                try:
                    result = bidding.submit_bid(
                        conn,
                        item_id=item_id,
                        user_id=user.google_sub,
                        amount=float(amount),
                    )
                    st.success(
                        f"Bid accepted: {money(result['amount'])} "
                        f"(sequence #{result['recorded_sequence']})"
                    )
                    st.rerun()
                except BidError as exc:
                    st.error(str(exc))

        st.subheader("Bid history")
        st.caption("Bidder identities are hidden from non-administrators.")
        if user.is_admin:
            rows = bidding.list_admin_bids(conn, item_id)
            df = pd.DataFrame(rows)
            if not df.empty:
                df = df.rename(
                    columns={
                        "recorded_sequence": "Seq",
                        "amount": "Amount",
                        "recorded_at": "Timestamp",
                        "status": "Status",
                        "display_name": "Bidder",
                        "email": "Email",
                    }
                )
                st.dataframe(
                    df[["Seq", "Amount", "Timestamp", "Status", "Bidder", "Email"]],
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.write("No bids yet.")
        else:
            rows = bidding.list_public_bids(conn, item_id)
            df = pd.DataFrame(rows)
            if not df.empty:
                df = df.rename(
                    columns={
                        "recorded_sequence": "Seq",
                        "amount": "Amount",
                        "recorded_at": "Timestamp",
                        "status": "Status",
                    }
                )
                st.dataframe(df, use_container_width=True, hide_index=True)
            else:
                st.write("No bids yet.")


def my_bids_page(conn, user: User) -> None:
    st.title("My Bids")
    rows = bidding.my_bids(conn, user.google_sub)
    if not rows:
        st.info("You have not placed any bids yet.")
        return
    df = pd.DataFrame(
        [
            {
                "Item": r.item_id,
                "Name": r.item_name,
                "My bid": r.my_bid,
                "Current highest": r.current_highest,
                "My status": r.status_label,
                "Item status": r.item_status,
            }
            for r in rows
        ]
    )
    st.dataframe(df, use_container_width=True, hide_index=True)


def admin_page(conn, user: User) -> None:
    st.title("Administrator Dashboard")
    bidding.apply_due_closures(conn)

    tab_items, tab_bids, tab_audit, tab_sync, tab_report = st.tabs(
        ["Items", "Bid correction", "Audit", "Sync", "Winners"]
    )

    with tab_items:
        items = admin.list_items(conn, published_only=False)
        summaries = []
        for item in items:
            s = bidding.item_summary(conn, item["item_id"])
            summaries.append(
                {
                    "Item": item["item_id"],
                    "Name": item["name"],
                    "Status": item["status"],
                    "Published": bool(item["published"]),
                    "Initial": item["initial_bid_amount"],
                    "Highest": s["highest_bid"],
                    "Bids": s["bid_count"],
                    "Closing at": item["closing_at"],
                }
            )
        st.dataframe(pd.DataFrame(summaries), use_container_width=True, hide_index=True)

        c1, c2, c3 = st.columns(3)
        with c1:
            item_id = st.selectbox("Item", [i["item_id"] for i in items], key="admin-item")
            if st.button("Publish item"):
                try:
                    admin.publish_item(conn, item_id)
                    st.success(f"Published {item_id}")
                    st.rerun()
                except BidError as exc:
                    st.error(str(exc))
            if st.button("Close bidding now", type="primary"):
                admin.close_item(conn, item_id, administrator_id=user.google_sub)
                st.success(f"Closed {item_id}")
                st.rerun()
        with c2:
            hours = st.number_input("Close in N hours", min_value=1, value=24)
            if st.button("Set scheduled close"):
                closing = datetime.now(timezone.utc) + timedelta(hours=int(hours))
                admin.set_schedule(conn, item_id, closing_at=closing.isoformat())
                st.success(f"Scheduled {item_id} to close at {closing.isoformat()}")
                st.rerun()
        with c3:
            st.caption("Open an item detail from Available Items to inspect identities.")

    with tab_bids:
        all_items = admin.list_items(conn, published_only=True)
        item_id = st.selectbox(
            "Select item", [i["item_id"] for i in all_items], key="corr-item"
        )
        bids = bidding.list_admin_bids(conn, item_id)
        st.dataframe(pd.DataFrame(bids), use_container_width=True, hide_index=True)
        if bids:
            bid_labels = {
                f"{b['recorded_sequence']}: {b['display_name']} @ {b['amount']} ({b['status']})": b
                for b in bids
            }
            selected = st.selectbox("Bid", list(bid_labels.keys()))
            bid = bid_labels[selected]
            reason = st.text_input("Mandatory reason")
            new_amount = st.number_input(
                "Corrected amount",
                min_value=0.0,
                value=float(bid["amount"]),
                step=5.0,
            )
            a, b = st.columns(2)
            with a:
                if st.button("Correct bid"):
                    try:
                        admin.correct_bid(
                            conn,
                            bid_id=bid["bid_id"],
                            revised_amount=float(new_amount),
                            reason=reason,
                            administrator_id=user.google_sub,
                        )
                        st.success("Bid corrected.")
                        st.rerun()
                    except BidError as exc:
                        st.error(str(exc))
            with b:
                if st.button("Cancel bid"):
                    try:
                        admin.cancel_bid(
                            conn,
                            bid_id=bid["bid_id"],
                            reason=reason,
                            administrator_id=user.google_sub,
                        )
                        st.success("Bid cancelled.")
                        st.rerun()
                    except BidError as exc:
                        st.error(str(exc))

    with tab_audit:
        audits = admin.list_audits(conn)
        st.dataframe(pd.DataFrame(audits), use_container_width=True, hide_index=True)

    with tab_sync:
        from prebidding.services import drive_oauth, drive_sync

        st.subheader("Google Drive photo folder")
        st.warning(
            "Setting `drive_folder_id` only stores the folder reference. "
            "Photos pull only after **Connect Google Drive & Sync** succeeds."
        )
        st.caption(
            "Name files like `ITEM-101.jpg` (filename stem = item id). "
            "If Google shows redirect_uri_mismatch, add these Authorized redirect URIs "
            "on your OAuth web client: `http://127.0.0.1:8090/` and `http://localhost:8090/`."
        )

        connected = drive_oauth.drive_connected()
        st.markdown(
            f"**Drive connection:** {'Connected ✅' if connected else 'Not connected ❌'}"
        )

        current_folder = drive_sync.get_setting(conn, "drive_folder_id") or ""
        try:
            secret_folder = str(st.secrets["prebidding"].get("drive_folder_id", "") or "")
        except Exception:
            secret_folder = ""
        default_folder = current_folder or secret_folder or "15XIK7dkLBgYNNqRG0Wn7HYUU9Wc1Nes4"
        folder_input = st.text_input(
            "Drive folder ID or URL",
            value=default_folder,
            placeholder="https://drive.google.com/drive/folders/...",
        )

        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("Save folder", use_container_width=True):
                try:
                    folder_id = drive_sync.extract_folder_id(folder_input)
                    drive_sync.set_setting(conn, "drive_folder_id", folder_id)
                    st.success(f"Saved: {folder_id}")
                except drive_sync.DriveSyncError as exc:
                    st.error(str(exc))
        with c2:
            connect_and_sync = st.button(
                "Connect Google Drive & Sync",
                type="primary",
                use_container_width=True,
            )
        with c3:
            sync_only = st.button(
                "Sync now",
                use_container_width=True,
                disabled=not connected,
            )

        if connected and st.button("Disconnect Drive"):
            drive_oauth.clear_credentials()
            st.rerun()

        def _run_sync(folder_value: str) -> None:
            folder_id = drive_sync.extract_folder_id(folder_value)
            drive_sync.set_setting(conn, "drive_folder_id", folder_id)
            token = drive_oauth.get_valid_access_token()
            meta = drive_oauth.probe_folder(token, folder_id)
            with st.spinner(f"Downloading photos from '{meta.get('name', folder_id)}'..."):
                result = drive_sync.sync_photos_from_drive(
                    conn, access_token=token, folder_id=folder_id
                )
            st.success(
                f"Synced folder **{meta.get('name', folder_id)}** — "
                f"new {result.imported}, updated {result.updated}, rejected {result.rejected}."
            )
            for err in result.errors[:10]:
                st.warning(err)
            if result.imported + result.updated == 0 and result.rejected == 0:
                st.info(
                    "No image files found in that folder. "
                    "Upload `.jpg/.png` files named like `ITEM-101.jpg`."
                )

        if connect_and_sync:
            try:
                with st.spinner(
                    "Opening browser for Google Drive consent "
                    "(add redirect URI http://localhost:8090/ if prompted)..."
                ):
                    drive_oauth.connect_drive_interactive(st.secrets)
                _run_sync(folder_input)
                st.rerun()
            except (drive_sync.DriveSyncError, drive_oauth.DriveAuthError) as exc:
                st.error(str(exc))
            except Exception as exc:  # noqa: BLE001
                st.error(f"Unexpected failure: {exc}")

        if sync_only:
            try:
                _run_sync(folder_input)
                st.rerun()
            except (drive_sync.DriveSyncError, drive_oauth.DriveAuthError) as exc:
                st.error(str(exc))
            except Exception as exc:  # noqa: BLE001
                st.error(f"Unexpected sync failure: {exc}")

        st.divider()
        st.caption("Recent synchronization runs")
        st.dataframe(
            pd.DataFrame(admin.list_syncs(conn)),
            use_container_width=True,
            hide_index=True,
        )

    with tab_report:
        report = admin.winners_report(conn)
        st.dataframe(pd.DataFrame(report), use_container_width=True, hide_index=True)
        csv_data = admin.export_winners_csv(conn)
        st.download_button(
            "Export winners CSV",
            data=csv_data,
            file_name="winners.csv",
            mime="text/csv",
        )


def main() -> None:
    conn = get_connection()
    user = require_login(conn)
    if user is None:
        login_page(conn)
        return

    if "view" not in st.session_state:
        st.session_state["view"] = "list"

    page = sidebar(user)
    if page == "Available Items":
        if st.session_state.get("view") == "item_detail":
            item_detail(conn, user)
        else:
            available_items(conn, user)
    elif page == "My Bids":
        st.session_state["view"] = "list"
        my_bids_page(conn, user)
    elif page == "Administrator":
        if not user.is_admin:
            st.error("Administrator role required.")
        else:
            st.session_state["view"] = "list"
            admin_page(conn, user)


if __name__ == "__main__":
    main()
