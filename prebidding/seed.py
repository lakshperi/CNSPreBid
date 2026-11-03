"""Seed demo users, catalog items, and placeholder photographs."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from prebidding.constants import DEFAULT_INITIAL_BID_CAD
from prebidding.db import DATA_DIR, connect, init_db
from prebidding.services.admin import record_sync, upsert_item
from prebidding.services.auth import ensure_demo_users


SAMPLE_ITEMS = [
    {
        "item_id": "ITEM-101",
        "name": "Vintage Leather Armchair",
        "description": "Mid-century armchair in cognac leather.",
        "initial_bid_amount": DEFAULT_INITIAL_BID_CAD,
        "color": (120, 72, 48),
    },
    {
        "item_id": "ITEM-102",
        "name": "Signed Sports Memorabilia",
        "description": "Framed jersey with certificate of authenticity.",
        "initial_bid_amount": DEFAULT_INITIAL_BID_CAD,
        "color": (32, 74, 135),
    },
    {
        "item_id": "ITEM-103",
        "name": "Weekend Cabin Getaway",
        "description": "Two-night stay voucher (lodging only).",
        "initial_bid_amount": DEFAULT_INITIAL_BID_CAD,
        "color": (46, 110, 72),
    },
    {
        "item_id": "ITEM-104",
        "name": "Chef's Dinner for Four",
        "description": "Private tasting menu experience.",
        "initial_bid_amount": DEFAULT_INITIAL_BID_CAD,
        "color": (140, 50, 60),
    },
    {
        "item_id": "ITEM-105",
        "name": "Art Print Collection",
        "description": "Set of three limited-edition prints.",
        "initial_bid_amount": DEFAULT_INITIAL_BID_CAD,
        "color": (90, 90, 120),
        "status": "Upcoming",
        "published": False,
    },
]


def _make_photo(path: Path, title: str, color: tuple[int, int, int]) -> None:
    img = Image.new("RGB", (640, 400), color)
    draw = ImageDraw.Draw(img)
    draw.rectangle((20, 20, 620, 380), outline=(255, 255, 255), width=3)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 28)
    except OSError:
        font = ImageFont.load_default()
    draw.text((40, 170), title[:40], fill=(255, 255, 255), font=font)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def seed(force: bool = False) -> Path:
    conn = connect()
    init_db(conn)
    ensure_demo_users(conn)

    existing = conn.execute("SELECT COUNT(*) AS c FROM bid_items").fetchone()["c"]
    if existing and not force:
        conn.close()
        return DATA_DIR / "prebidding.db"

    photos = DATA_DIR / "photos"
    imported = 0
    rejected = 0
    for sample in SAMPLE_ITEMS:
        photo_path = photos / f"{sample['item_id']}.png"
        _make_photo(photo_path, sample["name"], sample["color"])
        try:
            upsert_item(
                conn,
                item_id=sample["item_id"],
                name=sample["name"],
                description=sample["description"],
                photograph_ref=str(photo_path),
                initial_bid_amount=sample["initial_bid_amount"],
                status=sample.get("status", "Open"),
                published=sample.get("published", True),
            )
            imported += 1
        except Exception as exc:  # noqa: BLE001
            rejected += 1
            print(f"Rejected {sample['item_id']}: {exc}")

    record_sync(
        conn,
        source="Seed/Drive+Sheets",
        status="Success",
        imported=imported,
        rejected=rejected,
    )
    conn.close()
    return DATA_DIR / "prebidding.db"


if __name__ == "__main__":
    path = seed(force=True)
    print(f"Seeded database at {path}")