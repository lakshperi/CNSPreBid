"""Administrator operations: close, correct, cancel, reports, sync."""

from __future__ import annotations

import csv
import io
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from prebidding.db import transaction
from prebidding.services.bidding import BidError, apply_due_closures, leading_bid


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_items(conn: sqlite3.Connection, *, published_only: bool = False) -> list[dict]:
    apply_due_closures(conn)
    sql = "SELECT * FROM bid_items"
    if published_only:
        sql += " WHERE published = 1"
    sql += " ORDER BY item_id"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def publish_item(conn: sqlite3.Connection, item_id: str) -> None:
    item = conn.execute(
        "SELECT photograph_ref, initial_bid_amount FROM bid_items WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    if item is None:
        raise BidError("Item not found.")
    if not item["photograph_ref"]:
        raise BidError("Item missing photograph.")
    if item["initial_bid_amount"] is None or float(item["initial_bid_amount"]) <= 0:
        raise BidError("Set an initial bid amount greater than 0 before publishing.")
    conn.execute(
        "UPDATE bid_items SET published = 1, status = CASE WHEN status = 'Upcoming' THEN 'Open' ELSE status END WHERE item_id = ?",
        (item_id,),
    )


def unpublish_item(conn: sqlite3.Connection, item_id: str) -> None:
    item = conn.execute(
        "SELECT item_id, status FROM bid_items WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    if item is None:
        raise BidError("Item not found.")
    if item["status"] == "Closed":
        raise BidError("Closed items cannot be unpublished.")
    bid_count = conn.execute(
        "SELECT COUNT(*) AS c FROM bids WHERE item_id = ?",
        (item_id,),
    ).fetchone()["c"]
    if bid_count:
        raise BidError("Cannot unpublish an item that already has bids.")
    conn.execute(
        "UPDATE bid_items SET published = 0, status = 'Upcoming' WHERE item_id = ?",
        (item_id,),
    )


def update_item_details(
    conn: sqlite3.Connection,
    item_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    initial_bid_amount: float | None = None,
    published: bool | None = None,
) -> None:
    """Admin catalog edits. Initial amount can change only when there are no bids."""
    item = conn.execute(
        "SELECT * FROM bid_items WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    if item is None:
        raise BidError("Item not found.")

    bid_count = conn.execute(
        "SELECT COUNT(*) AS c FROM bids WHERE item_id = ?",
        (item_id,),
    ).fetchone()["c"]

    new_name = name if name is not None else item["name"]
    new_description = description if description is not None else (item["description"] or "")
    new_amount = (
        float(initial_bid_amount)
        if initial_bid_amount is not None
        else float(item["initial_bid_amount"])
    )
    if new_amount < 0:
        raise BidError("Initial bid amount cannot be negative.")
    if initial_bid_amount is not None and bid_count and abs(new_amount - float(item["initial_bid_amount"])) > 1e-9:
        raise BidError("Initial bid amount cannot change after bids exist.")

    new_published = int(item["published"] if published is None else published)
    new_status = item["status"]
    if published is True:
        if not item["photograph_ref"]:
            raise BidError("Item missing photograph.")
        if new_amount <= 0:
            raise BidError("Set an initial bid amount greater than 0 before publishing.")
        new_published = 1
        if new_status == "Upcoming":
            new_status = "Open"
    elif published is False:
        if item["status"] == "Closed":
            raise BidError("Closed items cannot be unpublished.")
        if bid_count:
            raise BidError("Cannot unpublish an item that already has bids.")
        new_published = 0
        new_status = "Upcoming"

    conn.execute(
        """
        UPDATE bid_items
        SET name = ?, description = ?, initial_bid_amount = ?, published = ?, status = ?
        WHERE item_id = ?
        """,
        (new_name.strip() or item["name"], new_description, new_amount, new_published, new_status, item_id),
    )


def close_item(
    conn: sqlite3.Connection,
    item_id: str,
    *,
    administrator_id: str,
    closure_type: str = "Manual",
) -> None:
    with transaction(conn):
        item = conn.execute(
            "SELECT * FROM bid_items WHERE item_id = ?", (item_id,)
        ).fetchone()
        if item is None:
            raise BidError("Item not found.")
        if item["status"] == "Closed":
            return
        now = _now()
        conn.execute(
            "UPDATE bid_items SET status = 'Closed' WHERE item_id = ?",
            (item_id,),
        )
        conn.execute(
            """
            INSERT INTO item_schedules(
                item_id, opening_at, closing_at, closure_type,
                closed_by_user_id, closed_at, timezone
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                closure_type=excluded.closure_type,
                closed_by_user_id=excluded.closed_by_user_id,
                closed_at=excluded.closed_at
            """,
            (
                item_id,
                item["opening_at"],
                item["closing_at"],
                closure_type,
                administrator_id,
                now,
                item["event_timezone"],
            ),
        )


def set_schedule(
    conn: sqlite3.Connection,
    item_id: str,
    *,
    closing_at: str,
    opening_at: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE bid_items
        SET closing_at = ?, opening_at = COALESCE(?, opening_at)
        WHERE item_id = ?
        """,
        (closing_at, opening_at, item_id),
    )
    conn.execute(
        """
        INSERT INTO item_schedules(item_id, opening_at, closing_at, timezone)
        VALUES (?, ?, ?, (SELECT event_timezone FROM bid_items WHERE item_id = ?))
        ON CONFLICT(item_id) DO UPDATE SET
            opening_at=excluded.opening_at,
            closing_at=excluded.closing_at
        """,
        (item_id, opening_at, closing_at, item_id),
    )


def correct_bid(
    conn: sqlite3.Connection,
    *,
    bid_id: str,
    revised_amount: float,
    reason: str,
    administrator_id: str,
) -> None:
    if not reason.strip():
        raise BidError("A reason is mandatory for corrections.")
    with transaction(conn):
        bid = conn.execute("SELECT * FROM bids WHERE bid_id = ?", (bid_id,)).fetchone()
        if bid is None:
            raise BidError("Bid not found.")
        if bid["status"] == "Cancelled":
            raise BidError("Cancelled bids cannot be corrected.")
        original = float(bid["amount"])
        conn.execute(
            "UPDATE bids SET amount = ?, status = 'Corrected' WHERE bid_id = ?",
            (float(revised_amount), bid_id),
        )
        conn.execute(
            """
            INSERT INTO bid_audits(
                audit_id, bid_id, action, original_amount, revised_amount,
                reason, administrator_id, action_at
            ) VALUES (?, ?, 'Correct', ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                bid_id,
                original,
                float(revised_amount),
                reason.strip(),
                administrator_id,
                _now(),
            ),
        )


def cancel_bid(
    conn: sqlite3.Connection,
    *,
    bid_id: str,
    reason: str,
    administrator_id: str,
) -> None:
    if not reason.strip():
        raise BidError("A reason is mandatory for cancellations.")
    with transaction(conn):
        bid = conn.execute("SELECT * FROM bids WHERE bid_id = ?", (bid_id,)).fetchone()
        if bid is None:
            raise BidError("Bid not found.")
        original = float(bid["amount"])
        conn.execute(
            "UPDATE bids SET status = 'Cancelled' WHERE bid_id = ?",
            (bid_id,),
        )
        conn.execute(
            """
            INSERT INTO bid_audits(
                audit_id, bid_id, action, original_amount, revised_amount,
                reason, administrator_id, action_at
            ) VALUES (?, ?, 'Cancel', ?, NULL, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                bid_id,
                original,
                reason.strip(),
                administrator_id,
                _now(),
            ),
        )


def list_audits(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT a.*, u.display_name AS administrator_name, b.item_id
        FROM bid_audits a
        JOIN users u ON u.google_sub = a.administrator_id
        JOIN bids b ON b.bid_id = a.bid_id
        ORDER BY a.action_at DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def winners_report(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    items = conn.execute(
        "SELECT * FROM bid_items WHERE status = 'Closed' AND published = 1"
    ).fetchall()
    report = []
    for item in items:
        leader = leading_bid(conn, item["item_id"])
        if not leader:
            report.append(
                {
                    "item_id": item["item_id"],
                    "name": item["name"],
                    "winner": None,
                    "amount": None,
                }
            )
            continue
        user = conn.execute(
            "SELECT display_name, email FROM users WHERE google_sub = ?",
            (leader["user_id"],),
        ).fetchone()
        report.append(
            {
                "item_id": item["item_id"],
                "name": item["name"],
                "winner": user["display_name"] if user else leader["user_id"],
                "email": user["email"] if user else "",
                "amount": float(leader["amount"]),
                "recorded_sequence": leader["recorded_sequence"],
            }
        )
    return report


def export_winners_csv(conn: sqlite3.Connection) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=["item_id", "name", "winner", "email", "amount", "recorded_sequence"],
    )
    writer.writeheader()
    for row in winners_report(conn):
        writer.writerow(row)
    return buf.getvalue()


def record_sync(
    conn: sqlite3.Connection,
    *,
    source: str,
    status: str,
    imported: int = 0,
    rejected: int = 0,
    error_detail: str | None = None,
) -> str:
    sync_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """
        INSERT INTO item_synchronizations(
            sync_id, source, started_at, finished_at, status,
            error_detail, items_imported, items_rejected
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (sync_id, source, now, now, status, error_detail, imported, rejected),
    )
    return sync_id


def list_syncs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM item_synchronizations ORDER BY started_at DESC LIMIT 20"
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_item(
    conn: sqlite3.Connection,
    *,
    item_id: str,
    name: str,
    description: str,
    photograph_ref: str,
    initial_bid_amount: float,
    status: str = "Open",
    published: bool = True,
    closing_at: str | None = None,
) -> None:
    existing = conn.execute(
        "SELECT item_id FROM bid_items WHERE item_id = ?", (item_id,)
    ).fetchone()
    if existing:
        # Do not mutate history-bearing fields that would rewrite bids; update catalog fields only.
        conn.execute(
            """
            UPDATE bid_items
            SET name = ?, description = ?, photograph_ref = ?,
                initial_bid_amount = CASE
                    WHEN (SELECT COUNT(*) FROM bids WHERE item_id = ?) = 0
                    THEN ? ELSE initial_bid_amount END
            WHERE item_id = ?
            """,
            (name, description, photograph_ref, item_id, float(initial_bid_amount), item_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO bid_items(
                item_id, name, description, photograph_ref, initial_bid_amount,
                status, opening_at, closing_at, published
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                name,
                description,
                photograph_ref,
                float(initial_bid_amount),
                status,
                _now(),
                closing_at,
                1 if published else 0,
            ),
        )