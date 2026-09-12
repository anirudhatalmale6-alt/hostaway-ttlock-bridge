"""The core reconcile loop: one reservation -> the right codes on the right doors.

Everything here is written as *converge to desired state*, never as "handle
this event". That distinction is the whole design:

  * a webhook delivered twice does nothing the second time;
  * a webhook that never arrives is picked up by the periodic sweep;
  * two edits arriving out of order both end at the same final state, because
    we re-read the reservation from Hostaway instead of trusting the payload;
  * a crash halfway through leaves a row marked out-of-sync, and the next pass
    finishes the job.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
from dataclasses import dataclass

from .codes import generate_code
from .config import LockConfig, ResolvedUnit, Settings, UnitMap, get_settings, get_unit_map
from .db import PasscodeRecord, session_scope, utcnow
from .hostaway import HostawayClient, HostawayError
from .ttlock import VIA_BLUETOOTH, VIA_GATEWAY, TTLockClient, TTLockError

log = logging.getLogger(__name__)

# Backoff after a failed TTLock write: 30s, 60s, 2m, 4m ... capped at 30m.
# A gateway that is momentarily busy is the common case, so the first retry is
# deliberately quick; a genuinely broken lock backs off out of the way.
BACKOFF_BASE_SECONDS = 30
MAX_BACKOFF_SECONDS = 30 * 60


@dataclass
class DesiredCode:
    lock: LockConfig
    present: bool
    start_ms: int | None
    end_ms: int | None
    strategy: str
    # Set only when the unit shares one code across its doors. None means
    # "any code will do", and the digits are chosen per lock.
    code: str | None = None

    @property
    def hash(self) -> str:
        raw = (
            f"{self.present}|{self.start_ms}|{self.end_ms}|{self.strategy}|{self.code}"
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


@dataclass
class SyncResult:
    reservation_id: str
    actions: list[str]
    errors: list[str]
    codes: dict[int, str]          # lock_id -> code
    skipped_reason: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


# ----------------------------------------------------------------------
# Reservation -> desired state
# ----------------------------------------------------------------------


def _parse_date(value) -> dt.date | None:
    if not value:
        return None
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    # Hostaway sends "2026-09-20" for dates and sometimes "2026-09-20 15:00:00".
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(text[: len(fmt) + 2], fmt).date()
        except ValueError:
            continue
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def _parse_hour(value) -> dt.time | None:
    """Hostaway gives check-in/out as an integer hour, occasionally 'HH:MM'."""
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return dt.time(hour=value % 24)
    text = str(value).strip()
    if text.isdigit():
        return dt.time(hour=int(text) % 24)
    if ":" in text:
        hh, _, mm = text.partition(":")
        try:
            return dt.time(hour=int(hh) % 24, minute=int(mm[:2]))
        except ValueError:
            return None
    return None


def _to_ms(local: dt.datetime, unit: ResolvedUnit) -> int:
    aware = local.replace(tzinfo=unit.tz)
    return int(aware.timestamp() * 1000)


def compute_window(reservation: dict, unit: ResolvedUnit) -> tuple[int, int] | None:
    """The exact millisecond window the code should be live for.

    Times come from the reservation when Hostaway has them, else from the
    unit's configured defaults. Buffers widen the window at both ends.
    """
    arrival = _parse_date(reservation.get("arrivalDate"))
    departure = _parse_date(reservation.get("departureDate"))
    if not arrival or not departure:
        return None

    check_in = _parse_hour(reservation.get("checkInTime")) or unit.check_in_time
    check_out = _parse_hour(reservation.get("checkOutTime")) or unit.check_out_time

    start_local = dt.datetime.combine(arrival, check_in) - dt.timedelta(
        minutes=unit.buffer_before_minutes
    )
    end_local = dt.datetime.combine(departure, check_out) + dt.timedelta(
        minutes=unit.buffer_after_minutes
    )
    if end_local <= start_local:
        # Same-day turnaround with a bad check-out time, or a data error.
        # Give the guest the rest of the arrival day rather than a dead code.
        end_local = start_local + dt.timedelta(hours=1)
    return _to_ms(start_local, unit), _to_ms(end_local, unit)


def reservation_is_active(reservation: dict, unit_map: UnitMap) -> bool:
    status = str(reservation.get("status") or "").strip()
    d = unit_map.defaults
    if status in d.cancelled_statuses:
        return False
    if d.active_statuses and status not in d.active_statuses:
        log.warning(
            "reservation %s has unrecognised status %r -- treating as inactive; "
            "add it to defaults.active_statuses in units.yaml if that is wrong",
            reservation.get("id"),
            status,
        )
        return False
    return True


# ----------------------------------------------------------------------
# Lock capabilities (for strategy: auto)
# ----------------------------------------------------------------------


class LockCapabilities:
    """Caches ``hasGateway`` per lock so ``strategy: auto`` can decide."""

    def __init__(self, ttlock: TTLockClient, ttl_seconds: int = 900):
        self.ttlock = ttlock
        self.ttl = dt.timedelta(seconds=ttl_seconds)
        self._data: dict[int, dict] = {}
        self._fetched_at: dt.datetime | None = None
        self._lock = asyncio.Lock()

    async def refresh(self, force: bool = False) -> dict[int, dict]:
        async with self._lock:
            fresh = (
                self._fetched_at is not None
                and utcnow() - self._fetched_at < self.ttl
            )
            if fresh and not force:
                return self._data
            locks = await self.ttlock.list_locks()
            self._data = {int(l["lockId"]): l for l in locks if l.get("lockId")}
            self._fetched_at = utcnow()
            log.info("ttlock: refreshed %d lock(s)", len(self._data))
            return self._data

    async def has_gateway(self, lock_id: int) -> bool:
        data = await self.refresh()
        info = data.get(int(lock_id))
        if info is None:
            await self.refresh(force=True)
            info = self._data.get(int(lock_id))
        if info is None:
            log.warning(
                "lock %s is in units.yaml but not visible on the TTLock account",
                lock_id,
            )
            return False
        return bool(info.get("hasGateway"))

    async def resolve_strategy(self, unit: ResolvedUnit, lock: LockConfig) -> str:
        declared = unit.lock_strategy(lock)
        if declared != "auto":
            return declared
        return "custom" if await self.has_gateway(lock.lock_id) else "generated"


# ----------------------------------------------------------------------
# The syncer
# ----------------------------------------------------------------------


class Syncer:
    def __init__(
        self,
        hostaway: HostawayClient,
        ttlock: TTLockClient,
        settings: Settings | None = None,
    ):
        self.hostaway = hostaway
        self.ttlock = ttlock
        self.s = settings or get_settings()
        self.caps = LockCapabilities(ttlock)
        self._res_locks: dict[str, asyncio.Lock] = {}

    def _reservation_lock(self, reservation_id: str) -> asyncio.Lock:
        """One in-flight sync per reservation. Without this, a create and an
        immediate edit race and you end up with two codes on the door."""
        return self._res_locks.setdefault(reservation_id, asyncio.Lock())

    # -- public --------------------------------------------------------

    async def sync_reservation(
        self, reservation_id: str | int, reservation: dict | None = None
    ) -> SyncResult:
        rid = str(reservation_id)
        async with self._reservation_lock(rid):
            return await self._sync_locked(rid, reservation)

    async def _sync_locked(self, rid: str, reservation: dict | None) -> SyncResult:
        result = SyncResult(reservation_id=rid, actions=[], errors=[], codes={})
        unit_map = get_unit_map()

        if reservation is None:
            try:
                reservation = await self.hostaway.get_reservation(rid)
            except HostawayError as exc:
                if getattr(exc, "status", None) == 404:
                    # Reservation deleted outright -- revoke everything we hold.
                    await self._revoke_all(rid, result)
                    return result
                result.errors.append(f"hostaway fetch failed: {exc}")
                return result

        listing_id = reservation.get("listingMapId") or reservation.get("listingId")
        if listing_id is None:
            result.skipped_reason = "reservation has no listingMapId"
            log.warning("reservation %s: %s", rid, result.skipped_reason)
            return result

        unit = unit_map.resolve(int(listing_id))
        if unit is None:
            result.skipped_reason = f"listing {listing_id} is not in units.yaml"
            log.info("reservation %s skipped: %s", rid, result.skipped_reason)
            # If it *used* to be mapped (unit reassignment), clean the old door.
            await self._revoke_all(rid, result)
            return result

        active = reservation_is_active(reservation, unit_map)
        window = compute_window(reservation, unit) if active else None
        if active and window is None:
            result.errors.append("could not read arrival/departure dates")
            return result

        desired = await self._desired_codes(unit, active, window)
        await self._apply(rid, int(listing_id), desired, result)

        if result.codes and active:
            await self._write_back_codes(rid, desired, result)
        return result

    # -- desired state -------------------------------------------------

    async def _desired_codes(
        self,
        unit: ResolvedUnit,
        active: bool,
        window: tuple[int, int] | None,
    ) -> list[DesiredCode]:
        out: list[DesiredCode] = []
        now = int(utcnow().replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
        grace_ms = self.s.delete_after_checkout_minutes * 60_000
        horizon_ms = now + self.s.horizon_days * 86_400_000

        for lock in unit.locks:
            strategy = await self.caps.resolve_strategy(unit, lock)
            present = active and window is not None
            start_ms = end_ms = None
            if present and window:
                start_ms, end_ms = window
                if end_ms + grace_ms <= now:
                    # Guest has checked out (plus grace): pull the code.
                    present = False
                    start_ms = end_ms = None
                elif start_ms > horizon_ms:
                    # Too far out to bother the lock with yet.
                    present = False
                    start_ms = end_ms = None
            out.append(
                DesiredCode(
                    lock=lock,
                    present=present,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    strategy=strategy,
                )
            )
        return out

    # -- apply ---------------------------------------------------------

    async def _apply(
        self,
        rid: str,
        listing_map_id: int,
        desired: list[DesiredCode],
        result: SyncResult,
    ) -> None:
        wanted_lock_ids = {d.lock.lock_id for d in desired}

        # Any door this reservation held that is no longer in its unit -- i.e.
        # the booking was moved to another property -- must be revoked.
        with session_scope() as s:
            stale = [
                r
                for r in s.query(PasscodeRecord).filter_by(reservation_id=rid).all()
                if r.lock_id not in wanted_lock_ids
            ]
            stale_ids = [(r.lock_id, r.listing_map_id) for r in stale]
        for lock_id, old_listing in stale_ids:
            desired.append(
                DesiredCode(
                    lock=LockConfig(lock_id=lock_id, label="reassigned"),
                    present=False,
                    start_ms=None,
                    end_ms=None,
                    strategy="custom",
                )
            )

        self._assign_shared_code(rid, listing_map_id, desired)

        for d in desired:
            try:
                code = await self._apply_one(rid, listing_map_id, d, result)
                if code:
                    result.codes[d.lock.lock_id] = code
            except Exception as exc:  # noqa: BLE001 - recorded on the row, not raised
                msg = f"lock {d.lock.lock_id}: {exc}"
                result.errors.append(msg)
                log.exception("reservation %s -- %s", rid, msg)
                self._record_failure(rid, d.lock.lock_id, str(exc))

    async def _apply_one(
        self,
        rid: str,
        listing_map_id: int,
        d: DesiredCode,
        result: SyncResult,
    ) -> str | None:
        with session_scope() as s:
            row = (
                s.query(PasscodeRecord)
                .filter_by(reservation_id=rid, lock_id=d.lock.lock_id)
                .one_or_none()
            )
            if row is None:
                row = PasscodeRecord(
                    reservation_id=rid,
                    lock_id=d.lock.lock_id,
                    listing_map_id=listing_map_id,
                )
                s.add(row)
            row.listing_map_id = listing_map_id
            row.desired_present = d.present
            row.desired_start_ms = d.start_ms
            row.desired_end_ms = d.end_ms
            row.desired_hash = d.hash
            row.strategy = d.strategy
            s.flush()
            snapshot = {
                "keyboard_pwd_id": row.keyboard_pwd_id,
                "code": row.code,
                "in_sync": row.in_sync,
                "attempts": row.attempts,
            }

        if snapshot["in_sync"]:
            return snapshot["code"] if d.present else None

        if self.s.dry_run:
            result.actions.append(
                f"[dry-run] lock {d.lock.lock_id}: would "
                f"{'set' if d.present else 'remove'} code"
            )
            return snapshot["code"]

        via = VIA_GATEWAY if await self.caps.has_gateway(d.lock.lock_id) else VIA_BLUETOOTH

        if not d.present:
            if snapshot["keyboard_pwd_id"]:
                await self.ttlock.delete_passcode(
                    lock_id=d.lock.lock_id,
                    keyboard_pwd_id=int(snapshot["keyboard_pwd_id"]),
                    delete_type=via,
                )
                result.actions.append(
                    f"lock {d.lock.lock_id}: deleted passcode "
                    f"{snapshot['keyboard_pwd_id']}"
                )
            self._mark_synced(rid, d, keyboard_pwd_id=None, code=None)
            return None

        assert d.start_ms is not None and d.end_ms is not None
        name = f"HA-{rid}"

        if snapshot["keyboard_pwd_id"] and d.strategy == "custom":
            # Same digits, new window -- the guest's code does not change when
            # they extend their stay. This is the nicest possible behaviour.
            # The one exception is a door joining a shared code late, where the
            # digits have to be brought into line with the rest of the unit.
            retarget = bool(d.code) and snapshot["code"] != d.code
            await self.ttlock.change_passcode(
                lock_id=d.lock.lock_id,
                keyboard_pwd_id=int(snapshot["keyboard_pwd_id"]),
                name=name,
                new_code=d.code if retarget else None,
                start_ms=d.start_ms,
                end_ms=d.end_ms,
                change_type=via,
            )
            final_code = d.code if retarget else snapshot["code"]
            result.actions.append(
                f"lock {d.lock.lock_id}: updated "
                f"{'digits and window' if retarget else 'window'} on passcode "
                f"{snapshot['keyboard_pwd_id']}"
            )
            self._mark_synced(
                rid, d, int(snapshot["keyboard_pwd_id"]), final_code
            )
            return final_code

        if snapshot["keyboard_pwd_id"]:
            # Generated codes are derived from the window, so a date change
            # means a new code. Remove the old one first so the door does not
            # accumulate dead entries.
            try:
                await self.ttlock.delete_passcode(
                    lock_id=d.lock.lock_id,
                    keyboard_pwd_id=int(snapshot["keyboard_pwd_id"]),
                    delete_type=via,
                )
                result.actions.append(
                    f"lock {d.lock.lock_id}: removed superseded passcode "
                    f"{snapshot['keyboard_pwd_id']}"
                )
            except TTLockError as exc:
                log.warning(
                    "lock %s: could not delete superseded passcode %s (%s); "
                    "continuing to issue the replacement",
                    d.lock.lock_id,
                    snapshot["keyboard_pwd_id"],
                    exc,
                )

        if d.strategy == "custom":
            code = d.code or generate_code(
                length=self._code_length(listing_map_id),
                exclude=self._codes_in_use(d.lock.lock_id, exclude_reservation=rid),
            )
            created = await self.ttlock.add_custom_passcode(
                lock_id=d.lock.lock_id,
                code=code,
                name=name,
                start_ms=d.start_ms,
                end_ms=d.end_ms,
                add_type=via,
            )
        else:
            created = await self.ttlock.generate_period_passcode(
                lock_id=d.lock.lock_id,
                name=name,
                start_ms=d.start_ms,
                end_ms=d.end_ms,
            )
        result.actions.append(
            f"lock {d.lock.lock_id}: created passcode {created['keyboard_pwd_id']} "
            f"({d.strategy})"
        )
        self._mark_synced(rid, d, created["keyboard_pwd_id"], created["code"])
        return created["code"]

    # -- shared codes --------------------------------------------------

    def _assign_shared_code(
        self, rid: str, listing_map_id: int, desired: list[DesiredCode]
    ) -> None:
        """Give every gateway door in this unit the same digits.

        For a building entrance plus the flat behind it, one number is what the
        guest actually wants -- and it is the only arrangement that fits
        Hostaway's single ``doorCode`` field without inventing a convention.

        Offline (``generated``) codes are derived per lock by TTLock, so they
        cannot join the shared code and keep their own.
        """
        unit = get_unit_map().resolve(listing_map_id)
        if unit is None or not unit.shared_code:
            return

        sharers = [d for d in desired if d.present and d.strategy == "custom"]
        if not sharers:
            return

        lock_ids = [d.lock.lock_id for d in sharers]
        code = self._existing_shared_code(rid, lock_ids)
        if code is None:
            code = generate_code(
                length=unit.code_length,
                exclude=self._codes_in_use_on_any(lock_ids, exclude_reservation=rid),
            )
        for d in sharers:
            d.code = code

        offline = [d for d in desired if d.present and d.strategy != "custom"]
        if offline:
            log.info(
                "reservation %s: unit %s shares one code across %d door(s); "
                "%d offline door(s) keep their own derived code",
                rid,
                listing_map_id,
                len(sharers),
                len(offline),
            )

    def _existing_shared_code(self, rid: str, lock_ids: list[int]) -> str | None:
        """Reuse the code this booking already has, so turning shared_code on
        (or adding a door) does not change the digits a guest was given."""
        with session_scope() as s:
            rows = (
                s.query(PasscodeRecord)
                .filter(
                    PasscodeRecord.reservation_id == rid,
                    PasscodeRecord.lock_id.in_(lock_ids),
                    PasscodeRecord.code.isnot(None),
                    PasscodeRecord.strategy == "custom",
                )
                .all()
            )
            by_lock = {r.lock_id: r.code for r in rows if r.code}
        for lock_id in lock_ids:  # config order, so the primary door wins
            if lock_id in by_lock:
                return by_lock[lock_id]
        return None

    def _codes_in_use_on_any(
        self, lock_ids: list[int], exclude_reservation: str
    ) -> set[str]:
        with session_scope() as s:
            rows = (
                s.query(PasscodeRecord)
                .filter(
                    PasscodeRecord.lock_id.in_(lock_ids),
                    PasscodeRecord.reservation_id != exclude_reservation,
                    PasscodeRecord.code.isnot(None),
                )
                .all()
            )
            return {r.code for r in rows if r.code}

    # -- ledger helpers ------------------------------------------------

    def _code_length(self, listing_map_id: int) -> int:
        unit = get_unit_map().resolve(listing_map_id)
        return unit.code_length if unit else 6

    def _codes_in_use(self, lock_id: int, exclude_reservation: str) -> set[str]:
        with session_scope() as s:
            rows = (
                s.query(PasscodeRecord)
                .filter(
                    PasscodeRecord.lock_id == lock_id,
                    PasscodeRecord.reservation_id != exclude_reservation,
                    PasscodeRecord.code.isnot(None),
                )
                .all()
            )
            return {r.code for r in rows if r.code}

    def _mark_synced(
        self,
        rid: str,
        d: DesiredCode,
        keyboard_pwd_id: int | None,
        code: str | None,
    ) -> None:
        with session_scope() as s:
            row = (
                s.query(PasscodeRecord)
                .filter_by(reservation_id=rid, lock_id=d.lock.lock_id)
                .one_or_none()
            )
            if row is None:
                return
            row.keyboard_pwd_id = keyboard_pwd_id
            row.code = code
            row.synced_hash = d.hash
            row.attempts = 0
            row.last_error = None
            row.next_attempt_at = None

    def _record_failure(self, rid: str, lock_id: int, error: str) -> None:
        with session_scope() as s:
            row = (
                s.query(PasscodeRecord)
                .filter_by(reservation_id=rid, lock_id=lock_id)
                .one_or_none()
            )
            if row is None:
                return
            row.attempts = (row.attempts or 0) + 1
            row.last_error = error[:2000]
            backoff = min(
                BACKOFF_BASE_SECONDS * 2 ** min(row.attempts - 1, 8),
                MAX_BACKOFF_SECONDS,
            )
            row.next_attempt_at = utcnow() + dt.timedelta(seconds=backoff)

    async def _revoke_all(self, rid: str, result: SyncResult) -> None:
        """Remove every code this reservation still holds anywhere."""
        with session_scope() as s:
            rows = [
                (r.lock_id, r.keyboard_pwd_id, r.listing_map_id)
                for r in s.query(PasscodeRecord).filter_by(reservation_id=rid).all()
            ]
        for lock_id, pwd_id, listing_map_id in rows:
            d = DesiredCode(
                lock=LockConfig(lock_id=lock_id, label="revoked"),
                present=False,
                start_ms=None,
                end_ms=None,
                strategy="custom",
            )
            try:
                await self._apply_one(rid, listing_map_id, d, result)
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"lock {lock_id}: revoke failed: {exc}")
                self._record_failure(rid, lock_id, str(exc))

    async def _write_back_codes(
        self, rid: str, desired: list[DesiredCode], result: SyncResult
    ) -> None:
        """Put the codes where the guest's check-in instructions can find them.

        Hostaway gives one ``doorCode`` field per reservation, which goes to the
        door marked ``primary: true``. Any other door that needs to reach the
        guest -- a building entrance on its own code -- is written to a
        reservation custom field instead, named per lock in units.yaml.

        Both are best effort. The lock is already correct at this point; a
        failure here means the guest was not *told*, which is bad but is not
        the same as being locked out, and the next sync retries it.
        """
        present = [d for d in desired if d.present]
        if not present:
            return

        if self.s.write_door_code_to_hostaway:
            primary = next((d for d in present if d.lock.primary), None)
            if primary is None:
                primary = present[0]
            code = result.codes.get(primary.lock.lock_id)
            if code:
                try:
                    await self.hostaway.set_door_code(rid, code)
                    result.actions.append(f"hostaway: wrote doorCode {code}")
                except HostawayError as exc:
                    log.warning(
                        "reservation %s: doorCode write-back failed: %s", rid, exc
                    )
                    result.actions.append(
                        f"hostaway: doorCode write-back failed ({exc})"
                    )

        custom = {
            d.lock.custom_field_id: result.codes[d.lock.lock_id]
            for d in present
            if d.lock.custom_field_id and result.codes.get(d.lock.lock_id)
        }
        if not custom:
            return
        try:
            await self.hostaway.set_custom_fields(rid, custom)
            result.actions.append(
                "hostaway: wrote custom field(s) "
                + ", ".join(str(k) for k in sorted(custom))
            )
        except HostawayError as exc:
            log.warning("reservation %s: custom field write-back failed: %s", rid, exc)
            result.actions.append(f"hostaway: custom field write-back failed ({exc})")
