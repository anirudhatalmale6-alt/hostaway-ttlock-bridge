"""End-to-end harness.

Three real HTTP servers on loopback: mock Hostaway, mock TTLock, and the
bridge itself running under uvicorn exactly as it does in production. Nothing
is monkeypatched and no internal function is called directly -- the tests poke
the mock Hostaway, which delivers a genuine webhook over the network, and then
assert on what ended up on the mock lock.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LOCK_WITH_GATEWAY = 5001
LOCK_NO_GATEWAY = 5002
LOCK_OTHER_UNIT = 5003
LOCK_ENTRANCE = 5004
LOCK_FLAT = 5005
LISTING_A = 101
LISTING_B = 102
LISTING_SHARED = 103

UNITS_YAML = f"""
defaults:
  timezone: Europe/London
  check_in_time: "15:00"
  check_out_time: "11:00"
  buffer_before_minutes: 60
  buffer_after_minutes: 60
  code_length: 6
  strategy: auto

units:
  - listing_map_id: {LISTING_A}
    name: "Seaside Cottage"
    locks:
      - lock_id: {LOCK_WITH_GATEWAY}
        label: "Front door"
      - lock_id: {LOCK_NO_GATEWAY}
        label: "Side gate"
        # Deliberately NOT the first lock, and on the other strategy, so the
        # write-back test proves `primary` decides -- not YAML ordering.
        primary: true

  - listing_map_id: {LISTING_B}
    name: "City Loft"
    locks:
      - lock_id: {LOCK_OTHER_UNIT}
        label: "Apartment door"

  # A building entrance plus the flat behind it: the guest gets one number.
  - listing_map_id: {LISTING_SHARED}
    name: "Old Town Apartment"
    shared_code: true
    locks:
      - lock_id: {LOCK_ENTRANCE}
        label: "Building entrance"
      - lock_id: {LOCK_FLAT}
        label: "Flat door"
        primary: true
"""

ADMIN_TOKEN = "test-admin-token"
WEBHOOK_USER = "hostaway"
WEBHOOK_PASSWORD = "webhook-secret"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerThread:
    def __init__(self, app, port: int):
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.port = port

    def start(self, timeout: float = 30.0) -> None:
        self.thread.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError(f"server on port {self.port} did not start")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)


def wait_until(predicate, timeout: float = 25.0, interval: float = 0.25, what: str = ""):
    """Poll until ``predicate`` returns something truthy, else fail loudly."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what or predicate!r}; last={last!r}")


class Harness:
    def __init__(self, hostaway_url: str, ttlock_url: str, bridge_url: str):
        self.hostaway_url = hostaway_url
        self.ttlock_url = ttlock_url
        self.bridge_url = bridge_url
        self.http = httpx.Client(timeout=30.0)

    # -- driving Hostaway ---------------------------------------------

    def upsert_reservation(self, reservation: dict, event: str | None = None) -> dict:
        body = {"reservation": reservation}
        if event:
            body["event"] = event
        r = self.http.post(f"{self.hostaway_url}/__test__/reservations", json=body)
        r.raise_for_status()
        return r.json()

    def delete_reservation(self, rid) -> None:
        self.http.delete(f"{self.hostaway_url}/__test__/reservations/{rid}")

    def door_code_writes(self) -> list:
        return self.http.get(f"{self.hostaway_url}/__test__/door_codes").json()["writes"]

    # -- inspecting TTLock --------------------------------------------

    def passcodes(self, lock_id: int | None = None) -> list[dict]:
        data = self.http.get(f"{self.ttlock_url}/__test__/passcodes").json()["passcodes"]
        if lock_id is None:
            return data
        return [p for p in data if p["lockId"] == lock_id]

    def fail_next_ttlock_writes(self, n: int) -> None:
        self.http.post(f"{self.ttlock_url}/__test__/fail_next_writes/{n}")

    def add_manual_passcode(self, lock_id: int, code: str, name: str) -> int:
        """Add a code the way a human would in the TTLock app."""
        r = self.http.post(
            f"{self.ttlock_url}/__test__/manual_passcode",
            json={"lockId": lock_id, "keyboardPwd": code, "keyboardPwdName": name},
        )
        r.raise_for_status()
        return r.json()["keyboardPwdId"]

    def passcode_by_id(self, pwd_id: int) -> dict | None:
        return next(
            (p for p in self.passcodes() if p["keyboardPwdId"] == pwd_id), None
        )

    # -- the bridge ----------------------------------------------------

    def admin(self, method: str, path: str, **kw) -> httpx.Response:
        return self.http.request(
            method,
            f"{self.bridge_url}{path}",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            **kw,
        )

    def ledger(self, rid) -> list[dict]:
        return self.admin("GET", f"/admin/reservations/{rid}").json()["codes"]

    def post_webhook_raw(self, payload, auth: bool = True) -> httpx.Response:
        return self.http.post(
            f"{self.bridge_url}/webhooks/hostaway",
            json=payload,
            auth=(WEBHOOK_USER, WEBHOOK_PASSWORD) if auth else None,
        )

    # -- assertions ----------------------------------------------------
    #
    # Every passcode the bridge creates is named "HA-<reservationId>", so each
    # test can scope itself to its own booking and the suite needs no global
    # reset between tests.

    def codes_for(self, rid, lock_id: int) -> list[dict]:
        want = f"HA-{rid}"
        return [p for p in self.passcodes(lock_id) if p["keyboardPwdName"] == want]

    def wait_for_codes(
        self, rid, lock_id: int, count: int = 1, timeout: float = 30.0
    ) -> list[dict]:
        def check():
            found = self.codes_for(rid, lock_id)
            return found if len(found) == count else None

        return wait_until(
            check,
            timeout=timeout,
            what=f"{count} passcode(s) named HA-{rid} on lock {lock_id}",
        )

    def wait_for_no_codes(self, rid, lock_id: int, timeout: float = 60.0) -> None:
        wait_until(
            lambda: not self.codes_for(rid, lock_id),
            timeout=timeout,
            what=f"no passcodes named HA-{rid} on lock {lock_id}",
        )


# The environment has to be in place *before* anything imports app.main --
# Settings is read once and cached, so setting these inside a fixture would
# leave the bridge pointing at the real APIs. conftest is imported before any
# test module, which makes module scope the only correct place for this.
WORKDIR = Path(tempfile.mkdtemp(prefix="ttlock-bridge-test-"))
UNITS_FILE = WORKDIR / "units.yaml"
UNITS_FILE.write_text(UNITS_YAML)

HA_PORT, TT_PORT, BR_PORT = free_port(), free_port(), free_port()
HOSTAWAY_URL = f"http://127.0.0.1:{HA_PORT}"
TTLOCK_URL = f"http://127.0.0.1:{TT_PORT}"
BRIDGE_URL = f"http://127.0.0.1:{BR_PORT}"

from tests.mocks import mock_hostaway, mock_ttlock  # noqa: E402

os.environ.update(
    {
        "HOSTAWAY_BASE_URL": f"{HOSTAWAY_URL}/v1",
        "HOSTAWAY_ACCOUNT_ID": mock_hostaway.ACCOUNT_ID,
        "HOSTAWAY_API_KEY": mock_hostaway.API_KEY,
        "TTLOCK_BASE_URL": TTLOCK_URL,
        "TTLOCK_CLIENT_ID": mock_ttlock.CLIENT_ID,
        "TTLOCK_CLIENT_SECRET": mock_ttlock.CLIENT_SECRET,
        "TTLOCK_USERNAME": mock_ttlock.USERNAME,
        "TTLOCK_PASSWORD": "hunter2hunter2",
        "WEBHOOK_BASIC_USER": WEBHOOK_USER,
        "WEBHOOK_BASIC_PASSWORD": WEBHOOK_PASSWORD,
        "ADMIN_TOKEN": ADMIN_TOKEN,
        "DATABASE_URL": f"sqlite:///{WORKDIR / 'bridge.db'}",
        "UNITS_FILE": str(UNITS_FILE),
        "LOG_LEVEL": "INFO",
        "RECONCILE_INTERVAL_SECONDS": "2",
        "FULL_SWEEP_MINUTES": "1",
        "DELETE_AFTER_CHECKOUT_MINUTES": "1",
        "WRITE_DOOR_CODE_TO_HOSTAWAY": "true",
    }
)


@pytest.fixture(scope="session")
def harness():
    hostaway_url, ttlock_url, bridge_url = HOSTAWAY_URL, TTLOCK_URL, BRIDGE_URL

    mock_ttlock.seed_locks(
        [
            {
                "lockId": LOCK_WITH_GATEWAY,
                "lockAlias": "Seaside front door",
                "hasGateway": 1,
                "keyboardPwdVersion": 4,
                "electricQuantity": 88,
            },
            {
                "lockId": LOCK_NO_GATEWAY,
                "lockAlias": "Seaside side gate",
                "hasGateway": 0,
                "keyboardPwdVersion": 4,
                "electricQuantity": 61,
            },
            {
                "lockId": LOCK_OTHER_UNIT,
                "lockAlias": "City Loft door",
                "hasGateway": 1,
                "keyboardPwdVersion": 4,
                "electricQuantity": 94,
            },
            {
                "lockId": LOCK_ENTRANCE,
                "lockAlias": "Old Town building entrance",
                "hasGateway": 1,
                "keyboardPwdVersion": 4,
                "electricQuantity": 77,
            },
            {
                "lockId": LOCK_FLAT,
                "lockAlias": "Old Town flat door",
                "hasGateway": 1,
                "keyboardPwdVersion": 4,
                "electricQuantity": 82,
            },
        ]
    )

    ha = ServerThread(mock_hostaway.app, HA_PORT)
    tt = ServerThread(mock_ttlock.app, TT_PORT)
    ha.start()
    tt.start()

    from app.main import app as bridge_app

    br = ServerThread(bridge_app, BR_PORT)
    br.start()

    h = Harness(hostaway_url, ttlock_url, bridge_url)
    h.http.post(
        f"{hostaway_url}/__test__/config",
        json={
            "webhook_url": f"{bridge_url}/webhooks/hostaway",
            "webhook_auth": [WEBHOOK_USER, WEBHOOK_PASSWORD],
        },
    )
    wait_until(
        lambda: h.http.get(f"{bridge_url}/healthz").status_code == 200,
        what="bridge healthz",
    )

    yield h

    h.http.close()
    br.stop()
    ha.stop()
    tt.stop()
