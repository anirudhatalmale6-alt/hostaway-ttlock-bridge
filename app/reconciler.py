"""Safety net.

Webhooks get dropped. Hostaway retries three times over about an hour and then
gives up; a deploy, a restart, or twenty seconds of network trouble at the
wrong moment is enough to lose one permanently. A booking system that only
reacts to events will therefore, eventually, leave a guest at a door with a
dead code.

So the bridge never *only* reacts. Two loops run continuously:

``retry_pass``  -- re-drives ledger rows that are out of sync and due, with
                   exponential backoff. Cheap; runs every minute.
``full_sweep``  -- re-reads every reservation in the live window straight from
                   Hostaway and reconciles it. Catches events that never
                   arrived at all. Runs every 15 minutes by default.

Also the only thing that revokes a code at checkout when no edit ever happens:
the stay simply ends, the sweep notices, the code comes off the door.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from .config import Settings
from .db import PasscodeRecord, session_scope, utcnow
from .sync import SyncResult, Syncer

log = logging.getLogger(__name__)


class Reconciler:
    def __init__(self, syncer: Syncer, settings: Settings):
        self.syncer = syncer
        self.s = settings
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last_retry_pass: dt.datetime | None = None
        self.last_full_sweep: dt.datetime | None = None
        self.last_error: str | None = None

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="reconciler")
            log.info(
                "reconciler started (retry every %ss, full sweep every %smin)",
                self.s.reconcile_interval_seconds,
                self.s.full_sweep_minutes,
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _run(self) -> None:
        # Do not hammer the APIs the instant the container comes up.
        await asyncio.sleep(5)
        next_full = utcnow()
        while not self._stop.is_set():
            try:
                await self.retry_pass()
                if utcnow() >= next_full:
                    await self.full_sweep()
                    next_full = utcnow() + dt.timedelta(
                        minutes=self.s.full_sweep_minutes
                    )
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must not die
                self.last_error = str(exc)
                log.exception("reconciler pass failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.s.reconcile_interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    # -- passes --------------------------------------------------------

    async def retry_pass(self) -> list[SyncResult]:
        """Re-drive rows that are behind: never synced, failed, or expired."""
        now = utcnow()
        now_ms = int(now.replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
        grace_ms = self.s.delete_after_checkout_minutes * 60_000

        with session_scope() as s:
            rows = s.query(PasscodeRecord).all()
            due: set[str] = set()
            for r in rows:
                if r.next_attempt_at and r.next_attempt_at > now:
                    continue
                behind = not r.desired_hash or r.desired_hash != r.synced_hash
                # Checkout has passed (plus grace) but the code is still on the
                # door -- this is what deactivates a code with no edit event.
                expired = (
                    r.desired_present
                    and r.desired_end_ms is not None
                    and r.desired_end_ms + grace_ms <= now_ms
                )
                # Booked far ahead, now inside the horizon -- time to issue it.
                pending = not r.desired_present and r.keyboard_pwd_id is None and behind
                if behind or expired or pending:
                    due.add(r.reservation_id)

        results = []
        for rid in sorted(due):
            log.info("reconciler: re-syncing reservation %s", rid)
            results.append(await self.syncer.sync_reservation(rid))
        if due:
            log.info("reconciler: retry pass handled %d reservation(s)", len(due))
        self.last_retry_pass = utcnow()
        return results

    async def full_sweep(self) -> list[SyncResult]:
        """Re-read the live booking window from Hostaway and reconcile it all."""
        today = dt.date.today()
        start = today - dt.timedelta(days=self.s.sweep_lookback_days)
        end = today + dt.timedelta(days=self.s.horizon_days)
        reservations = await self.syncer.hostaway.iter_reservations_in_window(start, end)
        log.info(
            "reconciler: full sweep over %d reservation(s) %s..%s",
            len(reservations),
            start,
            end,
        )
        results = []
        for res in reservations:
            rid = res.get("id")
            if rid is None:
                continue
            results.append(await self.syncer.sync_reservation(rid, reservation=res))
        self.last_full_sweep = utcnow()
        failed = [r for r in results if r.errors]
        if failed:
            log.warning("reconciler: %d reservation(s) had errors", len(failed))
        return results
