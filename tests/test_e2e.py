"""End-to-end proof of the three acceptance scenarios, plus the failure modes
that decide whether this thing is safe to leave running unattended.

Nothing here calls into the bridge's internals. Each test changes a booking on
the mock Hostaway, that mock delivers a real webhook over the network, and the
assertions read the state of the mock lock. If these pass, the wiring works.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from tests.conftest import (
    LISTING_A,
    LISTING_B,
    LOCK_NO_GATEWAY,
    LOCK_OTHER_UNIT,
    LOCK_WITH_GATEWAY,
    wait_until,
)

TZ = ZoneInfo("Europe/London")
BUFFER_BEFORE = dt.timedelta(minutes=60)
BUFFER_AFTER = dt.timedelta(minutes=60)


def ms(local: dt.datetime) -> int:
    return int(local.replace(tzinfo=TZ).timestamp() * 1000)


def expected_window(arrival: dt.date, departure: dt.date, check_in=15, check_out=11):
    start = dt.datetime.combine(arrival, dt.time(check_in)) - BUFFER_BEFORE
    end = dt.datetime.combine(departure, dt.time(check_out)) + BUFFER_AFTER
    return ms(start), ms(end)


def booking(rid: int, arrival: dt.date, departure: dt.date, **over) -> dict:
    res = {
        "id": rid,
        "listingMapId": LISTING_A,
        "channelId": 2005,
        "status": "new",
        "guestName": "Test Guest",
        "arrivalDate": arrival.isoformat(),
        "departureDate": departure.isoformat(),
        "checkInTime": 15,
        "checkOutTime": 11,
    }
    res.update(over)
    return res


TODAY = dt.date.today()


# ----------------------------------------------------------------------
# Scenario 1 -- a new reservation creates a time-bound code on every door
# ----------------------------------------------------------------------


def test_new_reservation_creates_time_bound_codes(harness):
    rid = 910001
    arrival, departure = TODAY + dt.timedelta(days=10), TODAY + dt.timedelta(days=13)
    harness.upsert_reservation(booking(rid, arrival, departure))

    gw = harness.wait_for_codes(rid, LOCK_WITH_GATEWAY)[0]
    nogw = harness.wait_for_codes(rid, LOCK_NO_GATEWAY)[0]

    start_ms, end_ms = expected_window(arrival, departure)

    # The window is exactly the stay plus the configured buffers -- not a day,
    # not "valid until deleted".
    for code in (gw, nogw):
        assert code["startDate"] == start_ms, code
        assert code["endDate"] == end_ms, code
        assert code["keyboardPwd"].isdigit()

    # The door with a gateway gets a custom code we chose; the door without
    # one gets a TTLock-generated offline code. Both are period codes.
    assert gw["isCustom"] == 1
    assert len(gw["keyboardPwd"]) == 6
    assert nogw["isCustom"] == 0

    # ...and the code is written back onto the reservation, which is what
    # Hostaway's check-in-instruction automations read. The door that supplies
    # it is the one marked `primary: true` -- here the *second* lock in the
    # config, so this fails if selection ever falls back to YAML ordering.
    writes = wait_until(
        lambda: [w for w in harness.door_code_writes() if w[0] == str(rid)] or None,
        what="doorCode write-back",
    )
    assert writes[-1][1] == nogw["keyboardPwd"]
    assert writes[-1][1] != gw["keyboardPwd"]


def test_codes_are_not_guessable(harness):
    rid = 910002
    arrival, departure = TODAY + dt.timedelta(days=20), TODAY + dt.timedelta(days=22)
    harness.upsert_reservation(booking(rid, arrival, departure))
    code = harness.wait_for_codes(rid, LOCK_WITH_GATEWAY)[0]["keyboardPwd"]
    assert code not in {"000000", "111111", "123456", "654321"}
    assert len(set(code)) > 2
    assert not code.startswith("0")


# ----------------------------------------------------------------------
# Scenario 2 -- an edit is reflected on the lock
# ----------------------------------------------------------------------


def test_date_change_updates_the_same_code_in_place(harness):
    rid = 920001
    arrival, departure = TODAY + dt.timedelta(days=30), TODAY + dt.timedelta(days=33)
    harness.upsert_reservation(booking(rid, arrival, departure))
    original = harness.wait_for_codes(rid, LOCK_WITH_GATEWAY)[0]

    new_departure = departure + dt.timedelta(days=2)
    harness.upsert_reservation(
        booking(rid, arrival, new_departure, status="modified"),
        event="reservation updated",
    )

    _, expected_end = expected_window(arrival, new_departure)
    updated = wait_until(
        lambda: (
            harness.codes_for(rid, LOCK_WITH_GATEWAY)[0]
            if harness.codes_for(rid, LOCK_WITH_GATEWAY)
            and harness.codes_for(rid, LOCK_WITH_GATEWAY)[0]["endDate"] == expected_end
            else None
        ),
        what="extended end date on the gateway lock",
    )

    # The guest keeps the digits they were already given -- we changed the
    # window on the existing passcode rather than issuing a second one.
    assert updated["keyboardPwd"] == original["keyboardPwd"]
    assert updated["keyboardPwdId"] == original["keyboardPwdId"]
    assert len(harness.codes_for(rid, LOCK_WITH_GATEWAY)) == 1

    # On the gateway-less door the code is derived from the window, so it must
    # be reissued -- but the old one must not be left behind on the lock.
    assert len(harness.codes_for(rid, LOCK_NO_GATEWAY)) == 1
    assert harness.codes_for(rid, LOCK_NO_GATEWAY)[0]["endDate"] == expected_end


def test_unit_reassignment_moves_access_to_the_new_property(harness):
    rid = 920002
    arrival, departure = TODAY + dt.timedelta(days=40), TODAY + dt.timedelta(days=42)
    harness.upsert_reservation(booking(rid, arrival, departure))
    harness.wait_for_codes(rid, LOCK_WITH_GATEWAY)
    harness.wait_for_codes(rid, LOCK_NO_GATEWAY)

    harness.upsert_reservation(
        booking(rid, arrival, departure, listingMapId=LISTING_B, status="modified"),
        event="reservation updated",
    )

    # New door opens...
    moved = harness.wait_for_codes(rid, LOCK_OTHER_UNIT)[0]
    start_ms, end_ms = expected_window(arrival, departure)
    assert (moved["startDate"], moved["endDate"]) == (start_ms, end_ms)

    # ...and, more importantly, the old doors close.
    harness.wait_for_no_codes(rid, LOCK_WITH_GATEWAY)
    harness.wait_for_no_codes(rid, LOCK_NO_GATEWAY)


# ----------------------------------------------------------------------
# Scenario 3 -- access ends at checkout
# ----------------------------------------------------------------------


def test_checkout_removes_the_code(harness):
    rid = 930001
    arrival = TODAY - dt.timedelta(days=2)
    departure = TODAY + dt.timedelta(days=1)
    harness.upsert_reservation(booking(rid, arrival, departure))
    harness.wait_for_codes(rid, LOCK_WITH_GATEWAY)

    # Guest leaves early; Hostaway shortens the stay to end yesterday.
    harness.upsert_reservation(
        booking(
            rid,
            arrival,
            TODAY - dt.timedelta(days=1),
            status="modified",
        ),
        event="reservation updated",
    )

    harness.wait_for_no_codes(rid, LOCK_WITH_GATEWAY)
    harness.wait_for_no_codes(rid, LOCK_NO_GATEWAY)


def test_cancellation_revokes_immediately(harness):
    rid = 930002
    arrival, departure = TODAY + dt.timedelta(days=5), TODAY + dt.timedelta(days=7)
    harness.upsert_reservation(booking(rid, arrival, departure))
    harness.wait_for_codes(rid, LOCK_WITH_GATEWAY)

    harness.upsert_reservation(
        booking(rid, arrival, departure, status="cancelled"),
        event="reservation updated",
    )
    harness.wait_for_no_codes(rid, LOCK_WITH_GATEWAY)
    harness.wait_for_no_codes(rid, LOCK_NO_GATEWAY)


@pytest.mark.slow
def test_missed_webhook_is_repaired_by_the_reconciler(harness):
    """The case that decides whether this is safe to leave alone.

    Hostaway's state changes and the webhook never arrives. Nothing in the
    event path can save this -- only the periodic sweep.
    """
    rid = 930003
    arrival = TODAY - dt.timedelta(days=2)
    departure = TODAY + dt.timedelta(days=1)
    harness.upsert_reservation(booking(rid, arrival, departure))
    harness.wait_for_codes(rid, LOCK_WITH_GATEWAY)

    # Stay ends yesterday. No webhook is sent at all.
    harness.http.post(
        f"{harness.hostaway_url}/__test__/reservations_silent",
        json={
            "reservation": booking(
                rid, arrival, TODAY - dt.timedelta(days=1), status="modified"
            )
        },
    ).raise_for_status()

    # Full sweep runs every 60s in the test config.
    harness.wait_for_no_codes(rid, LOCK_WITH_GATEWAY, timeout=150)
    harness.wait_for_no_codes(rid, LOCK_NO_GATEWAY, timeout=150)


# ----------------------------------------------------------------------
# Robustness
# ----------------------------------------------------------------------


def test_duplicate_webhooks_do_not_stack_codes(harness):
    rid = 940001
    arrival, departure = TODAY + dt.timedelta(days=50), TODAY + dt.timedelta(days=52)
    res = booking(rid, arrival, departure)
    harness.upsert_reservation(res)
    first = harness.wait_for_codes(rid, LOCK_WITH_GATEWAY)[0]

    payload = {"object": "reservation", "event": "reservation created", "data": res}
    for _ in range(4):
        assert harness.post_webhook_raw(payload).status_code == 200

    wait_until(lambda: harness.admin("GET", "/admin/status").json()["queue_depth"] == 0,
               what="queue to drain")

    assert len(harness.codes_for(rid, LOCK_WITH_GATEWAY)) == 1
    assert harness.codes_for(rid, LOCK_WITH_GATEWAY)[0]["keyboardPwdId"] == (
        first["keyboardPwdId"]
    )
    assert len(harness.codes_for(rid, LOCK_NO_GATEWAY)) == 1


@pytest.mark.slow
def test_ttlock_failure_is_retried_until_it_sticks(harness):
    """A TTLock write that fails must not silently drop the booking.

    Every gateway is offline sometimes. What matters is that the failure is
    recorded against the row and the bridge comes back for it on its own --
    no operator, no replayed webhook.
    """
    rid = 940002
    arrival, departure = TODAY + dt.timedelta(days=60), TODAY + dt.timedelta(days=62)

    harness.fail_next_ttlock_writes(-1)  # every write fails until cleared
    harness.upsert_reservation(booking(rid, arrival, departure))

    wait_until(
        lambda: [c for c in harness.ledger(rid) if c["last_error"]] or None,
        timeout=60,
        interval=0.1,
        what="the failure to be recorded against the ledger row",
    )
    assert harness.codes_for(rid, LOCK_WITH_GATEWAY) == []

    harness.fail_next_ttlock_writes(0)  # "gateway" comes back

    # Nothing else happens: no new webhook, no manual intervention. The
    # bridge's own retry/sweep has to notice and finish the job.
    harness.wait_for_codes(rid, LOCK_WITH_GATEWAY, timeout=150)
    harness.wait_for_codes(rid, LOCK_NO_GATEWAY, timeout=150)
    assert all(c["in_sync"] for c in harness.ledger(rid))
    assert all(c["last_error"] is None for c in harness.ledger(rid))


def test_unmapped_listing_is_ignored(harness):
    rid = 940003
    arrival, departure = TODAY + dt.timedelta(days=70), TODAY + dt.timedelta(days=72)
    harness.upsert_reservation(
        booking(rid, arrival, departure, listingMapId=999999)
    )
    wait_until(lambda: harness.admin("GET", "/admin/status").json()["queue_depth"] == 0,
               what="queue to drain")
    assert harness.ledger(rid) == []
    for lock in (LOCK_WITH_GATEWAY, LOCK_NO_GATEWAY, LOCK_OTHER_UNIT):
        assert harness.codes_for(rid, lock) == []


def test_webhook_rejects_bad_credentials(harness):
    r = harness.post_webhook_raw({"event": "reservation created", "data": {}}, auth=False)
    assert r.status_code == 401


def test_webhook_survives_an_unrecognised_payload(harness):
    # A body we cannot parse must never be answered with a 5xx -- Hostaway
    # retries for an hour and then disables the webhook entirely.
    for payload in ({}, {"event": "new message received", "data": {"id": 1}}, []):
        assert harness.post_webhook_raw(payload).status_code == 200


def test_admin_endpoints_require_a_token(harness):
    r = harness.http.get(f"{harness.bridge_url}/admin/status")
    assert r.status_code == 401


def test_readyz_reports_the_locks_it_can_see(harness):
    body = harness.admin("GET", "/readyz").json()
    assert body["status"] == "ready", body
    assert body["checks"]["ttlock"]["configured_but_not_found"] == []
    assert body["checks"]["hostaway"]["ok"] is True


def test_admin_locks_lists_gateway_status(harness):
    locks = {l["lock_id"]: l for l in harness.admin("GET", "/admin/locks").json()["locks"]}
    assert locks[LOCK_WITH_GATEWAY]["has_gateway"] is True
    assert locks[LOCK_NO_GATEWAY]["has_gateway"] is False
    assert locks[LOCK_OTHER_UNIT]["mapped_to"] == ["City Loft", LISTING_B]
