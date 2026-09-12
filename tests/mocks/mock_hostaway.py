"""A stand-in for the Hostaway Public API.

Implements only what the bridge touches, but implements it the way the real
thing behaves: bearer tokens, the ``{"status": "success", "result": ...}``
envelope, date-window filtering with offset pagination -- and it fires the
unified webhook at the bridge whenever a reservation changes, so the tests
exercise the real delivery path rather than calling the syncer directly.
"""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

ACCOUNT_ID = "90210"
API_KEY = "hostaway-test-secret"


class State:
    def __init__(self) -> None:
        self.reservations: dict[str, dict] = {}
        self.tokens: set[str] = set()
        self.webhook_url: str | None = None
        self.webhook_auth: tuple[str, str] | None = None
        self.webhook_calls: list[dict] = []
        self.door_code_writes: list[tuple[str, str]] = []
        self.fail_next_webhook: int = 0


state = State()
app = FastAPI(title="mock-hostaway")


def _envelope(result: Any, count: int | None = None) -> dict:
    body = {"status": "success", "result": result}
    if count is not None:
        body["count"] = count
    return body


def _auth(authorization: str) -> None:
    token = authorization.removeprefix("Bearer ").strip()
    if token not in state.tokens:
        raise HTTPException(401, "invalid token")


# -- auth ---------------------------------------------------------------


@app.post("/v1/accessTokens")
async def access_tokens(request: Request) -> dict:
    form = await request.form()
    if form.get("grant_type") != "client_credentials":
        raise HTTPException(400, "bad grant_type")
    if form.get("client_id") != ACCOUNT_ID or form.get("client_secret") != API_KEY:
        raise HTTPException(403, "bad credentials")
    token = "ha_" + secrets.token_hex(16)
    state.tokens.add(token)
    return {
        "token_type": "Bearer",
        "expires_in": 63072000,
        "access_token": token,
    }


# -- reservations -------------------------------------------------------


@app.get("/v1/reservations/{reservation_id}")
async def get_reservation(
    reservation_id: str, authorization: str = Header(default="")
) -> JSONResponse:
    _auth(authorization)
    res = state.reservations.get(str(reservation_id))
    if res is None:
        return JSONResponse({"status": "fail", "message": "not found"}, status_code=404)
    return JSONResponse(_envelope(res))


@app.get("/v1/reservations")
async def list_reservations(
    request: Request, authorization: str = Header(default="")
) -> dict:
    _auth(authorization)
    q = request.query_params
    items = list(state.reservations.values())

    start = q.get("startDate")
    end = q.get("endDate")
    date_type = q.get("dateType") or "arrivalDate"
    if start and end:
        # The real API filters on the window overlapping the stay; a booking
        # already in progress must still come back, so we test overlap rather
        # than "arrival inside the window".
        s_date = dt.date.fromisoformat(start)
        e_date = dt.date.fromisoformat(end)
        kept = []
        for r in items:
            try:
                arrival = dt.date.fromisoformat(str(r.get("arrivalDate"))[:10])
                departure = dt.date.fromisoformat(str(r.get("departureDate"))[:10])
            except (TypeError, ValueError):
                continue
            if departure >= s_date and arrival <= e_date:
                kept.append(r)
        items = kept

    items.sort(key=lambda r: str(r.get("arrivalDate")))
    total = len(items)
    offset = int(q.get("offset") or 0)
    limit = int(q.get("limit") or 100)
    return _envelope(items[offset : offset + limit], count=total)


@app.put("/v1/reservations/{reservation_id}")
async def update_reservation(
    reservation_id: str, request: Request, authorization: str = Header(default="")
) -> JSONResponse:
    _auth(authorization)
    res = state.reservations.get(str(reservation_id))
    if res is None:
        return JSONResponse({"status": "fail", "message": "not found"}, status_code=404)
    body = await request.json()
    res.update(body)
    if "doorCode" in body:
        state.door_code_writes.append((str(reservation_id), body["doorCode"]))
    return JSONResponse(_envelope(res))


# -- test control -------------------------------------------------------


@app.post("/__test__/config")
async def configure(request: Request) -> dict:
    body = await request.json()
    state.webhook_url = body.get("webhook_url")
    auth = body.get("webhook_auth")
    state.webhook_auth = tuple(auth) if auth else None
    return {"ok": True}


@app.post("/__test__/reservations")
async def upsert_reservation(request: Request) -> dict:
    """Create or edit a reservation, then deliver the webhook like Hostaway."""
    body = await request.json()
    reservation = body["reservation"]
    rid = str(reservation["id"])
    existing = rid in state.reservations
    state.reservations[rid] = {**state.reservations.get(rid, {}), **reservation}
    event = body.get("event") or (
        "reservation updated" if existing else "reservation created"
    )
    delivered = await _deliver_webhook(event, state.reservations[rid])
    return {"ok": True, "event": event, "delivered": delivered}


@app.post("/__test__/reservations_silent")
async def upsert_reservation_silent(request: Request) -> dict:
    """Change a reservation WITHOUT delivering a webhook.

    This is the dropped-webhook case: Hostaway's state moves on and the bridge
    is never told. Only the reconciler can catch it.
    """
    body = await request.json()
    reservation = body["reservation"]
    rid = str(reservation["id"])
    state.reservations[rid] = {**state.reservations.get(rid, {}), **reservation}
    return {"ok": True, "delivered": False}


@app.delete("/__test__/reservations/{reservation_id}")
async def delete_reservation(reservation_id: str) -> dict:
    state.reservations.pop(str(reservation_id), None)
    return {"ok": True}


@app.get("/__test__/door_codes")
async def door_codes() -> dict:
    return {"writes": state.door_code_writes}


@app.post("/__test__/reset")
async def reset() -> dict:
    state.reservations.clear()
    state.webhook_calls.clear()
    state.door_code_writes.clear()
    return {"ok": True}


async def _deliver_webhook(event: str, reservation: dict) -> bool:
    if not state.webhook_url:
        return False
    payload = {
        "id": secrets.randbelow(1_000_000),
        "accountId": int(ACCOUNT_ID),
        "object": "reservation",
        "event": event,
        "data": reservation,
    }
    state.webhook_calls.append(payload)
    auth = httpx.BasicAuth(*state.webhook_auth) if state.webhook_auth else None
    async with httpx.AsyncClient(timeout=20.0, auth=auth) as client:
        r = await client.post(state.webhook_url, json=payload)
        return r.status_code < 300
