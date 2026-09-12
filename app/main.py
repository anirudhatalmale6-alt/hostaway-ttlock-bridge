"""FastAPI service: Hostaway webhook in, TTLock passcode out."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse

from .config import get_settings, get_unit_map, load_unit_map
from .db import PasscodeRecord, WebhookEvent, init_db, session_scope, utcnow
from .hostaway import HostawayClient
from .reconciler import Reconciler
from .sync import Syncer
from .ttlock import TTLockClient

log = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


class Bridge:
    """Holds the long-lived objects. One instance per process."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.http: httpx.AsyncClient | None = None
        self.hostaway: HostawayClient | None = None
        self.ttlock: TTLockClient | None = None
        self.syncer: Syncer | None = None
        self.reconciler: Reconciler | None = None
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self.started_at = utcnow()

    async def start(self) -> None:
        s = self.settings
        init_db(s.database_url)
        load_unit_map(s.units_file)
        self.http = httpx.AsyncClient(timeout=45.0)
        self.hostaway = HostawayClient(s, self.http)
        self.ttlock = TTLockClient(s, self.http)
        self.syncer = Syncer(self.hostaway, self.ttlock, s)
        self.reconciler = Reconciler(self.syncer, s)
        self._worker = asyncio.create_task(self._drain(), name="sync-worker")
        self.reconciler.start()

    async def stop(self) -> None:
        if self.reconciler:
            await self.reconciler.stop()
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        if self.http:
            await self.http.aclose()

    def enqueue(self, reservation_id: str) -> None:
        self.queue.put_nowait(str(reservation_id))

    async def _drain(self) -> None:
        """Webhook handlers return immediately; the real work happens here.

        Hostaway gives us 20 seconds to acknowledge and treats a slow reply as
        a failure, so nothing that talks to TTLock may run inside the request.
        """
        assert self.syncer is not None
        while True:
            rid = await self.queue.get()
            try:
                result = await self.syncer.sync_reservation(rid)
                level = logging.WARNING if result.errors else logging.INFO
                log.log(
                    level,
                    "synced reservation %s: actions=%s errors=%s",
                    rid,
                    result.actions or ["none"],
                    result.errors or ["none"],
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - worker must survive
                log.exception("sync worker failed on reservation %s", rid)
            finally:
                self.queue.task_done()


bridge = Bridge()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(get_settings().log_level)
    await bridge.start()
    log.info("bridge up")
    try:
        yield
    finally:
        await bridge.stop()
        log.info("bridge down")


app = FastAPI(
    title="Hostaway -> TTLock bridge",
    version="1.0.0",
    lifespan=lifespan,
)


# ----------------------------------------------------------------------
# auth
# ----------------------------------------------------------------------


def _check_basic(request: Request) -> bool:
    s = bridge.settings
    if not s.webhook_basic_user and not s.webhook_basic_password:
        return True
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return False
    import base64

    try:
        raw = base64.b64decode(header.split(" ", 1)[1]).decode()
    except Exception:  # noqa: BLE001
        return False
    user, _, password = raw.partition(":")
    return secrets.compare_digest(user, s.webhook_basic_user) and secrets.compare_digest(
        password, s.webhook_basic_password
    )


def require_admin(authorization: str = Header(default="")) -> None:
    token = bridge.settings.admin_token
    if not token:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "admin endpoints are disabled; set ADMIN_TOKEN to enable them",
        )
    supplied = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(supplied, token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad admin token")


# ----------------------------------------------------------------------
# webhook
# ----------------------------------------------------------------------

_RESERVATION_EVENT_HINTS = ("reservation",)


def extract_reservation_id(payload: Any) -> tuple[str | None, str]:
    """Pull the reservation id and event name out of a webhook body.

    Deliberately forgiving. Hostaway has shipped more than one payload shape
    over the years (``reservation created`` vs ``reservation.created``,
    ``data`` vs ``body``, id at the top level vs nested), and a channel-specific
    variant showing up at 3am should not take the locks offline. Whatever we
    find, we only use the *id* -- the reservation itself is then re-read from
    the API, so a payload we half-understand still produces correct behaviour.
    """
    if not isinstance(payload, dict):
        return None, ""

    event = str(
        payload.get("event")
        or payload.get("eventType")
        or payload.get("type")
        or payload.get("object")
        or ""
    )

    data = payload
    for key in ("data", "body", "result", "reservation", "payload"):
        candidate = payload.get(key)
        if isinstance(candidate, dict):
            data = candidate
            break

    for key in ("reservationId", "id", "reservation_id"):
        value = data.get(key)
        if value not in (None, ""):
            return str(value), event
    for key in ("reservationId", "reservation_id"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value), event
    return None, event


def _looks_like_reservation_event(event: str, payload: Any) -> bool:
    lowered = event.lower()
    if any(h in lowered for h in _RESERVATION_EVENT_HINTS):
        return True
    # Message webhooks carry a reservationId too; those are not our business.
    if "message" in lowered or "conversation" in lowered:
        return False
    return True


async def _handle_webhook(request: Request, secret: str | None = None) -> Response:
    s = bridge.settings
    if s.webhook_path_secret:
        if secret is None or not secrets.compare_digest(secret, s.webhook_path_secret):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if not _check_basic(request):
        return JSONResponse(
            {"status": "unauthorized"},
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": 'Basic realm="hostaway-webhook"'},
        )

    raw = await request.body()
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        # Do not make Hostaway retry a body we will never parse.
        log.warning("webhook: non-JSON body (%d bytes) ignored", len(raw))
        return JSONResponse({"status": "ignored", "reason": "non-JSON body"})

    reservation_id, event = extract_reservation_id(payload)
    outcome = "queued"
    if reservation_id is None:
        outcome = "ignored: no reservation id"
    elif not _looks_like_reservation_event(event, payload):
        outcome = f"ignored: event {event!r} is not a reservation event"

    with session_scope() as db:
        db.add(
            WebhookEvent(
                event=event[:64],
                reservation_id=reservation_id,
                payload=(raw or b"").decode("utf-8", "replace")[:20000],
                outcome=outcome[:32],
            )
        )

    if outcome == "queued":
        assert reservation_id is not None
        bridge.enqueue(reservation_id)
        log.info("webhook: event=%r reservation=%s queued", event, reservation_id)
    else:
        log.info("webhook: %s (event=%r)", outcome, event)

    # Always 200. A non-2xx makes Hostaway retry for an hour and then disable
    # the webhook; our own retry loop is a better place to handle failure.
    return JSONResponse({"status": "ok", "reservation_id": reservation_id})


@app.post("/webhooks/hostaway")
async def webhook(request: Request) -> Response:
    return await _handle_webhook(request)


@app.post("/webhooks/hostaway/{secret}")
async def webhook_with_secret(request: Request, secret: str) -> Response:
    return await _handle_webhook(request, secret)


# ----------------------------------------------------------------------
# health and admin
# ----------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> dict:
    """Liveness. Deliberately does not touch the upstream APIs."""
    return {
        "status": "ok",
        "uptime_seconds": int((utcnow() - bridge.started_at).total_seconds()),
        "queue_depth": bridge.queue.qsize(),
    }


@app.get("/readyz")
async def readyz() -> JSONResponse:
    """Readiness: can we actually reach both platforms and see the locks?"""
    checks: dict[str, Any] = {}
    ok = True

    unit_map = get_unit_map()
    checks["units"] = {
        "count": len(unit_map.units),
        "locks": sum(len(u.locks) for u in unit_map.units),
    }
    if not unit_map.units:
        ok = False
        checks["units"]["error"] = "units.yaml has no units"

    try:
        assert bridge.hostaway is not None
        await bridge.hostaway.list_reservations(limit=1)
        checks["hostaway"] = {"ok": True}
    except Exception as exc:  # noqa: BLE001
        ok = False
        checks["hostaway"] = {"ok": False, "error": str(exc)}

    try:
        assert bridge.ttlock is not None
        locks = await bridge.ttlock.list_locks()
        configured = {l.lock_id for u in unit_map.units for l in u.locks}
        visible = {int(l["lockId"]) for l in locks if l.get("lockId")}
        missing = sorted(configured - visible)
        checks["ttlock"] = {
            "ok": not missing,
            "locks_on_account": len(visible),
            "configured_but_not_found": missing,
        }
        if missing:
            ok = False
    except Exception as exc:  # noqa: BLE001
        ok = False
        checks["ttlock"] = {"ok": False, "error": str(exc)}

    return JSONResponse(
        {"status": "ready" if ok else "not ready", "checks": checks},
        status_code=200 if ok else 503,
    )


@app.get("/admin/status", dependencies=[Depends(require_admin)])
async def admin_status() -> dict:
    with session_scope() as s:
        rows = s.query(PasscodeRecord).all()
        total = len(rows)
        out_of_sync = [r for r in rows if not r.in_sync]
        active = [r for r in rows if r.desired_present and r.keyboard_pwd_id]
        failing = [r for r in rows if r.last_error]
        detail = [
            {
                "reservation_id": r.reservation_id,
                "lock_id": r.lock_id,
                "attempts": r.attempts,
                "last_error": r.last_error,
                "next_attempt_at": r.next_attempt_at.isoformat()
                if r.next_attempt_at
                else None,
            }
            for r in out_of_sync[:50]
        ]
    rec = bridge.reconciler
    return {
        "ledger": {
            "rows": total,
            "codes_live": len(active),
            "out_of_sync": len(out_of_sync),
            "failing": len(failing),
            "out_of_sync_detail": detail,
        },
        "reconciler": {
            "last_retry_pass": rec.last_retry_pass.isoformat()
            if rec and rec.last_retry_pass
            else None,
            "last_full_sweep": rec.last_full_sweep.isoformat()
            if rec and rec.last_full_sweep
            else None,
            "last_error": rec.last_error if rec else None,
        },
        "queue_depth": bridge.queue.qsize(),
        "dry_run": bridge.settings.dry_run,
    }


@app.get("/admin/locks", dependencies=[Depends(require_admin)])
async def admin_locks() -> dict:
    """Every lock on the TTLock account, with the listing it is wired to.

    Run this first when adding a property -- it gives you the lockId to paste
    into units.yaml and tells you whether that door has a gateway.
    """
    assert bridge.ttlock is not None
    locks = await bridge.ttlock.list_locks()
    unit_map = get_unit_map()
    mapping = {
        l.lock_id: (u.name or str(u.listing_map_id), u.listing_map_id)
        for u in unit_map.units
        for l in u.locks
    }
    return {
        "locks": [
            {
                "lock_id": int(l["lockId"]),
                "alias": l.get("lockAlias") or l.get("lockName"),
                "has_gateway": bool(l.get("hasGateway")),
                "battery": l.get("electricQuantity"),
                "passcode_version": l.get("keyboardPwdVersion"),
                "mapped_to": mapping.get(int(l["lockId"]), None),
            }
            for l in locks
            if l.get("lockId")
        ]
    }


@app.get("/admin/reservations/{reservation_id}", dependencies=[Depends(require_admin)])
async def admin_reservation(reservation_id: str) -> dict:
    with session_scope() as s:
        rows = s.query(PasscodeRecord).filter_by(reservation_id=reservation_id).all()
        return {
            "reservation_id": reservation_id,
            "codes": [
                {
                    "lock_id": r.lock_id,
                    "code": r.code,
                    "keyboard_pwd_id": r.keyboard_pwd_id,
                    "strategy": r.strategy,
                    "desired_present": r.desired_present,
                    "start_ms": r.desired_start_ms,
                    "end_ms": r.desired_end_ms,
                    "in_sync": r.in_sync,
                    "last_error": r.last_error,
                }
                for r in rows
            ],
        }


@app.post("/admin/sync/{reservation_id}", dependencies=[Depends(require_admin)])
async def admin_sync(reservation_id: str) -> dict:
    assert bridge.syncer is not None
    result = await bridge.syncer.sync_reservation(reservation_id)
    return {
        "reservation_id": result.reservation_id,
        "ok": result.ok,
        "actions": result.actions,
        "errors": result.errors,
        "codes": {str(k): v for k, v in result.codes.items()},
        "skipped_reason": result.skipped_reason,
    }


@app.post("/admin/sweep", dependencies=[Depends(require_admin)])
async def admin_sweep(background: BackgroundTasks) -> dict:
    assert bridge.reconciler is not None
    background.add_task(bridge.reconciler.full_sweep)
    return {"status": "sweep started"}


@app.post("/admin/reload", dependencies=[Depends(require_admin)])
async def admin_reload() -> dict:
    """Re-read units.yaml without a restart -- how you add a new property."""
    unit_map = load_unit_map(bridge.settings.units_file)
    return {
        "units": len(unit_map.units),
        "locks": sum(len(u.locks) for u in unit_map.units),
    }
