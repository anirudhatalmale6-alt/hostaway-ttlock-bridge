"""Unit tests for the parts that are easy to get quietly wrong."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.codes import generate_code, _is_weak  # noqa: E402
from app.config import LockConfig, UnitMap  # noqa: E402
from app.main import extract_reservation_id  # noqa: E402
from app.sync import compute_window, reservation_is_active  # noqa: E402


def unit_map(**defaults) -> UnitMap:
    base = {
        "timezone": "Europe/London",
        "check_in_time": "15:00",
        "check_out_time": "11:00",
        "buffer_before_minutes": 60,
        "buffer_after_minutes": 60,
    }
    base.update(defaults)
    return UnitMap.model_validate(
        {
            "defaults": base,
            "units": [
                {
                    "listing_map_id": 1,
                    "name": "Test",
                    "locks": [{"lock_id": 10, "label": "Front"}],
                }
            ],
        }
    )


def resolved(**defaults):
    return unit_map(**defaults).resolve(1)


# -- windows ------------------------------------------------------------


def test_window_is_the_stay_plus_buffers():
    unit = resolved()
    res = {
        "arrivalDate": "2026-07-10",
        "departureDate": "2026-07-14",
        "checkInTime": 15,
        "checkOutTime": 11,
    }
    start_ms, end_ms = compute_window(res, unit)
    tz = ZoneInfo("Europe/London")
    assert start_ms == int(
        dt.datetime(2026, 7, 10, 14, 0, tzinfo=tz).timestamp() * 1000
    )
    assert end_ms == int(dt.datetime(2026, 7, 14, 12, 0, tzinfo=tz).timestamp() * 1000)


def test_window_crossing_a_dst_boundary_keeps_local_wall_clock_times():
    """The clocks go forward on 29 March 2026 in London.

    A naive UTC-offset conversion gets the check-out an hour wrong, which means
    the guest is locked out (or still has access) for an hour. Converting via
    the unit's zone is the only way this comes out right.
    """
    unit = resolved(buffer_before_minutes=0, buffer_after_minutes=0)
    res = {
        "arrivalDate": "2026-03-27",
        "departureDate": "2026-03-30",
        "checkInTime": 15,
        "checkOutTime": 11,
    }
    start_ms, end_ms = compute_window(res, unit)
    tz = ZoneInfo("Europe/London")
    start = dt.datetime.fromtimestamp(start_ms / 1000, tz)
    end = dt.datetime.fromtimestamp(end_ms / 1000, tz)
    assert (start.hour, start.minute) == (15, 0)
    assert (end.hour, end.minute) == (11, 0)
    assert start.utcoffset() == dt.timedelta(0)          # GMT
    assert end.utcoffset() == dt.timedelta(hours=1)      # BST


def test_check_in_time_falls_back_to_the_unit_default():
    unit = resolved(check_in_time="16:30", buffer_before_minutes=0,
                    buffer_after_minutes=0)
    res = {"arrivalDate": "2026-07-10", "departureDate": "2026-07-11"}
    start_ms, _ = compute_window(res, unit)
    tz = ZoneInfo("Europe/London")
    start = dt.datetime.fromtimestamp(start_ms / 1000, tz)
    assert (start.hour, start.minute) == (16, 30)


def test_string_check_in_times_are_accepted():
    unit = resolved(buffer_before_minutes=0, buffer_after_minutes=0)
    res = {
        "arrivalDate": "2026-07-10",
        "departureDate": "2026-07-11",
        "checkInTime": "16:00",
        "checkOutTime": "10:00",
    }
    start_ms, end_ms = compute_window(res, unit)
    tz = ZoneInfo("Europe/London")
    assert dt.datetime.fromtimestamp(start_ms / 1000, tz).hour == 16
    assert dt.datetime.fromtimestamp(end_ms / 1000, tz).hour == 10


def test_missing_dates_produce_no_window():
    assert compute_window({"arrivalDate": None, "departureDate": None}, resolved()) is None


def test_inverted_dates_do_not_produce_a_dead_code():
    unit = resolved(buffer_before_minutes=0, buffer_after_minutes=0)
    res = {
        "arrivalDate": "2026-07-10",
        "departureDate": "2026-07-10",
        "checkInTime": 15,
        "checkOutTime": 11,
    }
    start_ms, end_ms = compute_window(res, unit)
    assert end_ms > start_ms


# -- statuses -----------------------------------------------------------


@pytest.mark.parametrize("status", ["new", "modified", "confirmed", "ownerStay"])
def test_active_statuses(status):
    assert reservation_is_active({"status": status}, unit_map()) is True


@pytest.mark.parametrize("status", ["cancelled", "declined", "expired", "inquiry"])
def test_inactive_statuses(status):
    assert reservation_is_active({"status": status}, unit_map()) is False


def test_unknown_status_is_treated_as_inactive():
    # Fail closed: an unrecognised status must never leave a code on a door.
    assert reservation_is_active({"status": "somethingNew"}, unit_map()) is False


# -- codes --------------------------------------------------------------


def test_generated_codes_are_the_requested_length():
    for n in range(4, 10):
        assert len(generate_code(n)) == n


def test_generated_codes_avoid_excluded_values():
    taken = {generate_code(4) for _ in range(20)}
    fresh = generate_code(4, exclude=taken)
    assert fresh not in taken


@pytest.mark.parametrize(
    "code", ["123456", "000000", "111111", "654321", "112233", "012345", "1111"]
)
def test_weak_codes_are_rejected(code):
    assert _is_weak(code) is True


def test_invalid_length_is_refused():
    with pytest.raises(ValueError):
        generate_code(3)
    with pytest.raises(ValueError):
        generate_code(10)


# -- webhook payload shapes --------------------------------------------


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"event": "reservation created", "data": {"id": 77}}, "77"),
        ({"event": "reservation.updated", "data": {"reservationId": 78}}, "78"),
        ({"object": "reservation", "body": {"id": 79}}, "79"),
        ({"event": "reservation updated", "reservationId": 80}, "80"),
        ({"event": "reservation created", "result": {"id": 81}}, "81"),
        ({"id": 82}, "82"),
    ],
)
def test_reservation_id_is_found_in_every_payload_shape(payload, expected):
    rid, _ = extract_reservation_id(payload)
    assert rid == expected


@pytest.mark.parametrize("payload", [{}, [], None, "nope", {"data": {}}])
def test_unparseable_payloads_yield_no_id(payload):
    rid, _ = extract_reservation_id(payload)
    assert rid is None


# -- config -------------------------------------------------------------


def test_duplicate_listing_ids_are_rejected():
    with pytest.raises(ValueError):
        UnitMap.model_validate(
            {
                "units": [
                    {"listing_map_id": 5, "locks": []},
                    {"listing_map_id": 5, "locks": []},
                ]
            }
        )


def test_two_primary_locks_are_refused():
    # Exactly one door's code can go into the guest's check-in instructions.
    # Ambiguity here would silently send half the guests the wrong code.
    with pytest.raises(ValueError):
        UnitMap.model_validate(
            {
                "units": [
                    {
                        "listing_map_id": 1,
                        "locks": [
                            {"lock_id": 1, "primary": True},
                            {"lock_id": 2, "primary": True},
                        ],
                    }
                ]
            }
        )


def test_shared_code_defaults_off_and_can_be_set_per_unit():
    m = UnitMap.model_validate(
        {
            "defaults": {"shared_code": False},
            "units": [
                {"listing_map_id": 1, "locks": []},
                {"listing_map_id": 2, "shared_code": True, "locks": []},
            ],
        }
    )
    assert m.resolve(1).shared_code is False
    assert m.resolve(2).shared_code is True


def test_shared_code_can_default_on_for_the_whole_portfolio():
    m = UnitMap.model_validate(
        {
            "defaults": {"shared_code": True},
            "units": [
                {"listing_map_id": 1, "locks": []},
                {"listing_map_id": 2, "shared_code": False, "locks": []},
            ],
        }
    )
    assert m.resolve(1).shared_code is True
    assert m.resolve(2).shared_code is False


def test_no_primary_lock_is_allowed():
    m = UnitMap.model_validate(
        {"units": [{"listing_map_id": 1, "locks": [{"lock_id": 1}, {"lock_id": 2}]}]}
    )
    assert [l.primary for l in m.resolve(1).locks] == [False, False]


def test_per_lock_strategy_overrides_the_unit():
    m = UnitMap.model_validate(
        {
            "defaults": {"strategy": "generated"},
            "units": [
                {
                    "listing_map_id": 1,
                    "locks": [
                        {"lock_id": 1},
                        {"lock_id": 2, "strategy": "custom"},
                    ],
                }
            ],
        }
    )
    unit = m.resolve(1)
    assert unit.lock_strategy(LockConfig(lock_id=1)) == "generated"
    assert unit.lock_strategy(unit.locks[1]) == "custom"


def test_unknown_timezone_is_rejected_at_load_time():
    with pytest.raises(ValueError):
        UnitMap.model_validate(
            {"units": [{"listing_map_id": 1, "timezone": "Mars/Olympus", "locks": []}]}
        )
