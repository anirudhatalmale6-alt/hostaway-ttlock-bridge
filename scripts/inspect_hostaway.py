#!/usr/bin/env python3
"""Show what the Hostaway API actually exposes for custom fields and door codes.

Run this instead of guessing. It answers three questions:

  1. What custom fields exist, what are their ids, and are they attached to
     listings or to reservations?
  2. Does a real listing carry a value for them?
  3. Does a real reservation carry one -- and what is in doorCode?

    python scripts/inspect_hostaway.py
    python scripts/inspect_hostaway.py --listing 123456 --reservation 98765432

Read-only. Nothing is written to Hostaway or to any lock.

Guest names, emails and phone numbers are deliberately not printed -- this
output is meant to be safe to paste into a chat.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db import init_db  # noqa: E402
from app.hostaway import HostawayClient, HostawayError  # noqa: E402

SAFE_RESERVATION_KEYS = [
    "id",
    "listingMapId",
    "channelId",
    "status",
    "arrivalDate",
    "departureDate",
    "checkInTime",
    "checkOutTime",
    "doorCode",
    "doorCodeVendor",
    "doorCodeInstruction",
]


def show(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listing", help="listing id to inspect")
    ap.add_argument("--reservation", help="reservation id to inspect")
    args = ap.parse_args()

    settings = get_settings()
    if not settings.hostaway_api_key:
        print("HOSTAWAY_API_KEY is not set -- fill in .env first.", file=sys.stderr)
        return 2
    init_db(settings.database_url)
    hostaway = HostawayClient(settings)

    try:
        show("Custom field definitions")
        try:
            fields = await hostaway.list_custom_fields()
            if not fields:
                print("(none defined on this account)")
            for f in fields:
                print(
                    f"  id={f.get('id')}  "
                    f"name={f.get('name')!r}  "
                    f"type={f.get('type')}  "
                    f"object={f.get('object') or f.get('objectType')}  "
                    f"isPublic={f.get('isPublic')}"
                )
            print()
            print("  ^ 'object' tells you whether the field hangs off a LISTING")
            print("    (one fixed value per property) or a RESERVATION (a value")
            print("    per booking). Only a reservation field can carry a code")
            print("    that changes for every guest.")
        except HostawayError as exc:
            print(f"could not list custom fields: {exc}")

        listing_id = args.listing
        if not listing_id:
            listings = await hostaway.list_listings()
            if listings:
                listing_id = listings[0].get("id")
            show(f"Listings on the account: {len(listings)}")
            for l in listings[:20]:
                print(f"  {l.get('id')}  {l.get('name') or l.get('internalListingName')}")
            if len(listings) > 20:
                print(f"  ... and {len(listings) - 20} more")

        if listing_id:
            show(f"Listing {listing_id} -- customFieldValues")
            try:
                listing = await hostaway.get_listing(listing_id)
                values = listing.get("customFieldValues") or []
                print(json.dumps(values, indent=2) if values else "(empty)")
            except HostawayError as exc:
                print(f"could not read listing: {exc}")

        reservation_id = args.reservation
        if not reservation_id:
            today = dt.date.today()
            recent = await hostaway.iter_reservations_in_window(
                today - dt.timedelta(days=30), today + dt.timedelta(days=120)
            )
            if recent:
                reservation_id = recent[0].get("id")
            print()
            print(f"(using reservation {reservation_id} out of {len(recent)} found)")

        if reservation_id:
            show(f"Reservation {reservation_id} -- door code and custom fields")
            try:
                res = await hostaway.get_reservation_with_resources(reservation_id)
                for key in SAFE_RESERVATION_KEYS:
                    if key in res:
                        print(f"  {key}: {res[key]!r}")
                values = res.get("customFieldValues") or []
                print("  customFieldValues:")
                print(
                    "\n".join(f"    {line}" for line in json.dumps(values, indent=2).splitlines())
                    if values
                    else "    (empty)"
                )
            except HostawayError as exc:
                print(f"could not read reservation: {exc}")
    finally:
        await hostaway.aclose()

    print()
    print("Nothing was written. This was a read-only inspection.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
