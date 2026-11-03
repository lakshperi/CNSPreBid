"""Bidding domain service: submit, highest, winner, my-bids."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from prebidding.db import transaction


class BidError(Exception):
    """Raised when a bid is rejected by business rules."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_sequence(conn: sqlite3.Connection) -> int:
    conn.execute(
        "UPDATE sequence_counters SET value = value + 1 WHERE name = 'bid_sequence'"
    )
    row = conn.execute(
        "SELECT value FROM sequence_counters WHERE name = 'bid_sequence'"
    ).fetchone()
    return int(row["value"])


def _valid_bids_sql(table_alias: str = "") -> str:
    col = f"{table_alias}.status" if table_alias else "status"
    return f"{col} IN ('Accepted', 'Corrected')"


def current_highest(conn: sqlite3.Connection, item_id: str) -> float | None:
    row = conn.execute(
        f"""
        SELECT MAX(amount) AS highest
        FROM bids
        WHERE item_id = ? AND {_valid_bids_sql()}
        """,
        (item_id,),
    ).fetchone()
    if row is None or row["highest"] is None:
        return None
    return float(row["highest"])


def minimum_acceptable(conn: sqlite3.Connection, item_id: str) -> float:
    """Lowest amount that can take/keep the lead: initial, or just above current highest."""
    item = conn.execute(
        "SELECT initial_bid_amount FROM bid_items WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    if item is None:
        raise BidError("Item not found.")
    highest = current_highest(conn, item_id)
    if highest is None:
        return float(item["initial_bid_amount"])
    # Must strictly exceed the current highest to overbid.
    return round(float(highest) + 0.01, 2)


def bid_count(conn: sqlite3.Connection, item_id: str) -> int:
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS cnt FROM bids
        WHERE item_id = ? AND {_valid_bids_sql()}
        """,
        (item_id,),
    ).fetchone()
    return int(row["cnt"])


def leading_bid(conn: sqlite3.Connection, item_id: str) -> sqlite3.Row | None:
    return conn.execute(
        f"""
        SELECT * FROM bids
        WHERE item_id = ? AND {_valid_bids_sql()}
        ORDER BY amount DESC, recorded_sequence ASC
        LIMIT 1
        """,
        (item_id,),
    ).fetchone()


def apply_due_closures(conn: sqlite3.Connection) -> int:
    """Close Open items whose closing_at has passed (UTC ISO compare)."""
    now = _now()
    rows = conn.execute(
        """
        SELECT item_id FROM bid_items
        WHERE status = 'Open' AND closing_at IS NOT NULL AND closing_at <= ?
        """,
        (now,),
    ).fetchall()
    closed = 0
    for row in rows:
        conn.execute(
            "UPDATE bid_items SET status = 'Closed' WHERE item_id = ?",
            (row["item_id"],),
        )
        conn.execute(
            """
            INSERT INTO item_schedules(
                item_id, opening_at, closing_at, closure_type,
                closed_by_user_id, closed_at, timezone
            )
            VALUES (
                ?,
                (SELECT opening_at FROM bid_items WHERE item_id = ?),
                (SELECT closing_at FROM bid_items WHERE item_id = ?),
                'Automatic', NULL, ?,
                (SELECT event_timezone FROM bid_items WHERE item_id = ?)
            )
            ON CONFLICT(item_id) DO UPDATE SET
                closure_type='Automatic',
                closed_at=excluded.closed_at,
                closed_by_user_id=NULL
            """,
            (row["item_id"], row["item_id"], row["item_id"], now, row["item_id"]),
        )
        closed += 1
    return closed


def submit_bid(
    conn: sqlite3.Connection,
    *,
    item_id: str,
    user_id: str,
    amount: float,
    allow_admin_bid: bool = False,
) -> dict[str, Any]:
    user = conn.execute(
        "SELECT role FROM users WHERE google_sub = ?", (user_id,)
    ).fetchone()
    if user is None:
        raise BidError("User is not authenticated.")
    if user["role"] == "Administrator" and not allow_admin_bid:
        raise BidError("Administrators cannot place bids.")

    with transaction(conn):
        apply_due_closures(conn)
        item = conn.execute(
            "SELECT * FROM bid_items WHERE item_id = ?", (item_id,)
        ).fetchone()
        if item is None or not item["published"]:
            raise BidError("Item not found.")
        if item["status"] != "Open":
            raise BidError(f"Item is {item['status']}; bidding is not allowed.")

        leader = leading_bid(conn, item_id)
        if leader is not None and leader["user_id"] == user_id:
            raise BidError(
                "You already hold the highest bid. "
                "You can bid again after someone outbids you."
            )

        minimum = minimum_acceptable(conn, item_id)
        if amount < minimum:
            raise BidError(
                f"Bid must be at least {minimum:.2f} (current minimum acceptable)."
            )

        seq = _next_sequence(conn)
        bid_id = str(uuid.uuid4())
        recorded_at = _now()
        conn.execute(
            """
            INSERT INTO bids(
                bid_id, item_id, user_id, amount,
                recorded_sequence, recorded_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, 'Accepted')
            """,
            (bid_id, item_id, user_id, float(amount), seq, recorded_at),
        )
        return {
            "bid_id": bid_id,
            "item_id": item_id,
            "amount": float(amount),
            "recorded_sequence": seq,
            "recorded_at": recorded_at,
            "status": "Accepted",
        }


def list_public_bids(conn: sqlite3.Connection, item_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT recorded_sequence, amount, recorded_at, status
        FROM bids
        WHERE item_id = ?
        ORDER BY recorded_sequence ASC
        """,
        (item_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_admin_bids(conn: sqlite3.Connection, item_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT b.bid_id, b.recorded_sequence, b.amount, b.recorded_at, b.status,
               u.display_name, u.email, b.user_id
        FROM bids b
        JOIN users u ON u.google_sub = b.user_id
        WHERE b.item_id = ?
        ORDER BY b.recorded_sequence ASC
        """,
        (item_id,),
    ).fetchall()
    return [dict(r) for r in rows]


@dataclass
class MyBidRow:
    item_id: str
    item_name: str
    my_bid: float
    current_highest: float
    status_label: str
    item_status: str


def my_bids(conn: sqlite3.Connection, user_id: str) -> list[MyBidRow]:
    """One row per item using the user's highest valid bid."""
    rows = conn.execute(
        f"""
        SELECT b.item_id,
               i.name AS item_name,
               i.status AS item_status,
               MAX(b.amount) AS my_bid,
               MAX(b.recorded_at) AS last_bid_at
        FROM bids b
        JOIN bid_items i ON i.item_id = b.item_id
        WHERE b.user_id = ? AND {_valid_bids_sql('b')}
        GROUP BY b.item_id
        ORDER BY last_bid_at DESC
        """,
        (user_id,),
    ).fetchall()
    result: list[MyBidRow] = []
    for row in rows:
        highest = current_highest(conn, row["item_id"])
        if highest is None:
            highest = 0.0
        leader = leading_bid(conn, row["item_id"])
        if leader and leader["user_id"] == user_id:
            label = "Winning" if row["item_status"] == "Closed" else "Highest"
        else:
            label = "Outbid"
        result.append(
            MyBidRow(
                item_id=row["item_id"],
                item_name=row["item_name"],
                my_bid=float(row["my_bid"]),
                current_highest=float(highest),
                status_label=label,
                item_status=row["item_status"],
            )
        )
    return result


def item_summary(conn: sqlite3.Connection, item_id: str) -> dict[str, Any]:
    item = conn.execute(
        "SELECT * FROM bid_items WHERE item_id = ?", (item_id,)
    ).fetchone()
    if item is None:
        raise BidError("Item not found.")
    highest = current_highest(conn, item_id)
    leader = leading_bid(conn, item_id)
    winner = None
    if item["status"] == "Closed" and leader:
        user = conn.execute(
            "SELECT display_name, email FROM users WHERE google_sub = ?",
            (leader["user_id"],),
        ).fetchone()
        winner = {
            "user_id": leader["user_id"],
            "display_name": user["display_name"] if user else leader["user_id"],
            "email": user["email"] if user else "",
            "amount": float(leader["amount"]),
            "recorded_sequence": leader["recorded_sequence"],
        }
    return {
        **dict(item),
        "highest_bid": highest,
        "bid_count": bid_count(conn, item_id),
        "minimum_acceptable": minimum_acceptable(conn, item_id),
        "winner": winner,
        "leader_user_id": leader["user_id"] if leader else None,
    }