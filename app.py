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
    conn = connect()
    init_db(conn)
    auth.ensure_demo_users(conn)
    # Always keep a local seed catalog as fallback.
    seed(force=False)
    return conn


@st.cache_resource
def boot_sync_drive_photos() -> dict:
    """Pull Drive folder once per server boot when secrets include a refresh token."""
    from prebidding.services import drive_oauth, drive_sync

    conn = get_connection()
    try:
        if not drive_oauth.drive_connected(st.secrets):
            return {"ok": False, "error": "No [drive] refresh_token in secrets"}
        folder = ""
        try:
            folder = str(st.secrets["prebidding"].get("drive_folder_id", "") or "")
        except Exception:
            folder = ""
        folder = folder or drive_sync.get_setting(conn, "drive_folder_id") or ""
        if not folder:
            return {"ok": False, "error": "drive_folder_id missing in secrets/settings"}
        token = drive_oauth.get_valid_access_token(st.secrets)
        folder_id = drive_sync.extract_folder_id(folder)
        drive_sync.set_setting(conn, "drive_folder_id", folder_id)
        result = drive_sync.sync_photos_from_drive(
            conn,
            access_token=token,
            folder_id=folder_id,
            auto_publish=True,
        )
        return {
            "ok": True,
            "folder_id": folder_id,
            "imported": result.imported,
            "updated": result.updated,
            "rejected": result.rejected,
            "errors": result.errors[:5],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


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


def sign_in_panel(conn, *, heading: str, detail: str, key: str = "sign-in-google") -> None:
    """Prompt Google SSO (or demo login) without blocking catalog browsing."""
    st.subheader(heading)
    st.write(detail)
    if _sso_enabled():
        st.button(
            "Log in with Google",
            type="primary",
            use_container_width=True,
            on_click=st.login,
            key=key,
        )
        if _demo_enabled():
            with st.expander("Demo login (local testing)"):
                _demo_login_form(conn)
        return

    missing = auth.missing_google_sso_keys(st.secrets)
    st.warning(
        "Google SSO is not configured for this deployment. "
        "On Streamlit Cloud: App menu (⋮) → Settings → Secrets → paste TOML → Save → Reboot."
    )
    if missing:
        st.error(f"Missing or placeholder secret keys: {', '.join(missing)}")
    st.subheader("Demo login (until Google SSO is configured)")
    _demo_login_form(conn)


def sidebar(user: User | None) -> str:
    with st.sidebar:
        if user is None:
            st.markdown("**Guest**")
            st.caption("Browse items freely. Sign in only to bid.")
            pages = ["Available Items", "Sign in"]
            page = st.radio("Navigate", pages, key="nav-guest")
            st.divider()
            st.caption("Auth: Google OIDC · Store: SQLite")
            return page

        st.markdown(f"**{user.display_name}**")
        provider = st.session_state.get("auth_provider", "demo")
        st.caption(f"{user.email} · {user.role} · via {provider}")
        pages = ["Available Items", "My Bids"]
        if user.is_admin:
            pages.append("Administrator")
        page = st.radio("Navigate", pages, key="nav-user")
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


def available_items(conn, user: User | None) -> None:
    st.title("Available Items")
    if user is None:
        st.caption("You can browse all items as a guest. Sign in with Google only when you place a bid.")
    bidding.apply_due_closures(conn)
    # Admins also see unpublished Drive imports so photos are visible immediately.
    is_admin = bool(user and user.is_admin)
    items = admin.list_items(conn, published_only=not is_admin)
    if is_admin:
        unpublished = [i for i in items if not i["published"]]
        if unpublished:
            st.info(
                f"{len(unpublished)} unpublished item(s) from Drive are shown only to administrators. "
                "Use Administrator → Items to set Initial $ and check Published, then Save."
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


def item_detail(conn, user: User | None) -> None:
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
        if summary["status"] == "Closed" and summary.get("winner") and user and user.is_admin:
            w = summary["winner"]
            st.success(
                f"Winner: {w['display_name']} ({w['email']}) at {money(w['amount'])}"
            )
        elif summary["status"] == "Closed":
            st.success("Bidding closed. Winner finalized.")

    with right:
        st.subheader("Place a bid")
        if user is None:
            if summary["status"] != "Open":
                st.info(f"Item is {summary['status']}; bidding is disabled.")
            else:
                st.info("Sign in with Google to place a bid on this item.")
                sign_in_panel(
                    conn,
                    heading="Sign in to bid",
                    detail="Your Google account is only required when submitting a bid.",
                    key="sign-in-to-bid",
                )
        elif user.is_admin:
            st.warning("Administrators cannot place bids in this release.")
        elif summary["status"] != "Open":
            st.info(f"Item is {summary['status']}; bidding is disabled.")
        elif summary.get("leader_user_id") == user.google_sub:
            st.success(
                "You hold the highest bid. "
                "If someone outbids you, you can place a higher bid here."
            )
        else:
            if summary.get("leader_user_id"):
                st.caption(
                    "You can place another bid if you were outbid — "
                    "enter an amount at or above the minimum."
                )
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
        if user and user.is_admin:
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
        if not items:
            st.info("No items yet. Sync photos from Google Drive first.")
        else:
            st.info(
                "Double-click **Initial $** (or Published / Name) in the table to edit. "
                "Changes save automatically when you leave the cell. "
                "Initial $ is locked after the first bid."
            )
            show = st.radio(
                "Show",
                ["All", "Unpublished only", "Published only"],
                horizontal=True,
                key="admin-item-filter",
            )
            visible = items
            if show == "Unpublished only":
                visible = [i for i in items if not i["published"]]
            elif show == "Published only":
                visible = [i for i in items if i["published"]]

            rows = []
            for item in visible:
                s = bidding.item_summary(conn, item["item_id"])
                bid_count = int(s["bid_count"] or 0)
                rows.append(
                    {
                        "Item": item["item_id"],
                        "Initial $": float(item["initial_bid_amount"] or 0.0),
                        "Published": bool(item["published"]),
                        "Name": item["name"],
                        "Status": item["status"],
                        "Highest": float(s["highest_bid"]) if s["highest_bid"] is not None else None,
                        "Bids": bid_count,
                        "Amount editable": bid_count == 0,
                    }
                )
            catalog_df = pd.DataFrame(rows)
            editor_rev = int(st.session_state.get("catalog_rev", 0))
            edited_df = st.data_editor(
                catalog_df,
                use_container_width=True,
                hide_index=True,
                num_rows="fixed",
                disabled=["Item", "Status", "Highest", "Bids", "Amount editable"],
                column_config={
                    "Item": st.column_config.TextColumn("Item", width="medium"),
                    "Initial $": st.column_config.NumberColumn(
                        "Initial $",
                        help="Double-click to edit. Locked when Bids > 0.",
                        min_value=0.0,
                        step=1.0,
                        format="%.2f",
                        width="small",
                        required=True,
                    ),
                    "Published": st.column_config.CheckboxColumn(
                        "Published",
                        help="Check to make visible for bidding.",
                        width="small",
                    ),
                    "Name": st.column_config.TextColumn("Name", width="large"),
                    "Status": st.column_config.TextColumn("Status", width="small"),
                    "Highest": st.column_config.NumberColumn("Highest", format="%.2f", width="small"),
                    "Bids": st.column_config.NumberColumn("Bids", width="small"),
                    "Amount editable": st.column_config.CheckboxColumn(
                        "Amount editable",
                        help="Unchecked means Initial $ is locked (bids already exist).",
                        width="small",
                    ),
                },
                key=f"admin-catalog-editor-{show}-{editor_rev}",
            )

            original = {r["Item"]: r for r in rows}
            saved = 0
            errors: list[str] = []
            for _, row in edited_df.iterrows():
                item_id = str(row["Item"])
                before = original.get(item_id)
                if before is None:
                    continue
                new_amount = float(row["Initial $"])
                new_published = bool(row["Published"])
                new_name = str(row["Name"])
                amount_changed = abs(new_amount - float(before["Initial $"])) > 1e-9
                pub_changed = new_published != bool(before["Published"])
                name_changed = new_name != str(before["Name"])
                if not (amount_changed or pub_changed or name_changed):
                    continue
                if amount_changed and not before["Amount editable"]:
                    errors.append(f"{item_id}: Initial $ locked after first bid.")
                    continue
                try:
                    admin.update_item_details(
                        conn,
                        item_id,
                        name=new_name,
                        initial_bid_amount=new_amount,
                        published=new_published,
                    )
                    saved += 1
                except BidError as exc:
                    errors.append(f"{item_id}: {exc}")

            if errors:
                for msg in errors:
                    st.error(msg)
            if saved:
                st.session_state["catalog_rev"] = editor_rev + 1
                st.toast(f"Saved {saved} item(s)")
                st.rerun()

            st.divider()
            with st.expander("Photo preview & close schedule", expanded=False):
                labels = {
                    f"{i['name']}  ·  {i['item_id']}  ·  "
                    f"{'Published' if i['published'] else 'Unpublished'}  ·  "
                    f"${float(i['initial_bid_amount'] or 0):.2f}": i["item_id"]
                    for i in items
                }
                picked = st.selectbox(
                    "Select item",
                    list(labels.keys()),
                    key="admin-item-actions",
                )
                item_id = labels[picked]
                selected = next(i for i in items if i["item_id"] == item_id)

                left, right = st.columns([1, 1], gap="large")
                with left:
                    photo = Path(selected["photograph_ref"]) if selected.get("photograph_ref") else None
                    if photo and photo.exists():
                        st.image(str(photo), use_container_width=True)
                    else:
                        st.info("No local photo preview.")
                    st.caption(selected.get("description") or "No description.")
                with right:
                    st.write(
                        f"**Status:** {selected['status']}  \n"
                        f"**Bids:** {bidding.bid_count(conn, item_id)}  \n"
                        f"**Closing:** {selected.get('closing_at') or 'Not scheduled'}"
                    )
                    if st.button("Close bidding now", type="primary", key="admin-close-now"):
                        admin.close_item(conn, item_id, administrator_id=user.google_sub)
                        st.success(f"Closed {item_id}")
                        st.rerun()
                    hours = st.number_input(
                        "Close in N hours", min_value=1, value=24, key="admin-close-hours"
                    )
                    if st.button("Set scheduled close", key="admin-schedule-close"):
                        closing = datetime.now(timezone.utc) + timedelta(hours=int(hours))
                        admin.set_schedule(conn, item_id, closing_at=closing.isoformat())
                        st.success(f"Scheduled {item_id} to close at {closing.isoformat()}")
                        st.rerun()

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
        status = drive_oauth.drive_secrets_status(st.secrets)
        on_cloud = status["on_cloud"]
        connected = status["connected"]
        boot = boot_sync_drive_photos()
        st.markdown(
            f"**Drive connection:** {'Connected ✅' if connected else 'Not connected ❌'}"
        )
        st.caption(
            f"Secrets [drive] section: {'yes' if status['has_drive_section'] else 'NO'} · "
            f"refresh_token: {'yes' if status['has_refresh_token'] else 'NO'} · "
            f"folder secret: `{status['folder_id'] or 'missing'}`"
        )
        if boot.get("ok"):
            st.success(
                f"Boot sync: folder `{boot.get('folder_id')}` · "
                f"new {boot.get('imported', 0)}, updated {boot.get('updated', 0)}, "
                f"rejected {boot.get('rejected', 0)}"
            )
            for err in boot.get("errors") or []:
                st.warning(err)
        else:
            st.warning(f"Boot sync did not run: {boot.get('error', 'unknown')}")
        if not connected:
            st.error(
                "Add a complete `[drive]` block (refresh_token + client_id + client_secret) "
                "in App ⋮ → Settings → Secrets, Save, then Reboot."
            )
        st.caption("Filename stem becomes the item id (e.g. `ITEM-101.jpg`).")

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
                disabled=on_cloud,
                help="Local only. On Cloud, put [drive] refresh_token in Secrets.",
            )
        with c3:
            sync_only = st.button(
                "Sync now",
                use_container_width=True,
                type="primary" if on_cloud else "secondary",
            )

        if connected and not on_cloud and st.button("Disconnect Drive"):
            drive_oauth.clear_credentials()
            st.rerun()

        def _run_sync(folder_value: str) -> None:
            folder_id = drive_sync.extract_folder_id(folder_value)
            drive_sync.set_setting(conn, "drive_folder_id", folder_id)
            token = drive_oauth.get_valid_access_token(st.secrets)
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

        if connect_and_sync and not on_cloud:
            try:
                with st.spinner("Opening browser for Google Drive consent..."):
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
    # Ensure Drive catalog is pulled once per boot (Cloud + local).
    boot_sync_drive_photos()
    user = require_login(conn)

    if "view" not in st.session_state:
        st.session_state["view"] = "list"

    page = sidebar(user)
    if page == "Available Items":
        if st.session_state.get("view") == "item_detail":
            item_detail(conn, user)
        else:
            available_items(conn, user)
    elif page == "Sign in":
        st.title("CNS Pre-Bidding")
        sign_in_panel(
            conn,
            heading="Sign in with Google",
            detail="Browse items without an account. Sign in to place bids, view My Bids, or administer.",
        )
    elif page == "My Bids":
        if user is None:
            sign_in_panel(
                conn,
                heading="Sign in required",
                detail="Sign in with Google to view your bids.",
            )
        else:
            st.session_state["view"] = "list"
            my_bids_page(conn, user)
    elif page == "Administrator":
        if user is None:
            sign_in_panel(
                conn,
                heading="Sign in required",
                detail="Sign in with a Google admin account to open the Administrator dashboard.",
            )
        elif not user.is_admin:
            st.error("Administrator role required.")
        else:
            st.session_state["view"] = "list"
            admin_page(conn, user)


if __name__ == "__main__":
    main()
