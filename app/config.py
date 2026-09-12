"""Configuration: environment settings plus the YAML unit/lock map.

Two separate concerns on purpose:

* ``Settings``  -- secrets and service tuning, from the environment (.env).
* ``UnitMap``   -- which Hostaway listing drives which TTLock lock(s), from
                   ``units.yaml``.  Adding a property is an edit to that file
                   and a restart (or ``POST /admin/reload``); no code change.
"""

from __future__ import annotations

import logging
from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

Strategy = Literal["auto", "custom", "generated"]


class Settings(BaseSettings):
    """Environment-driven settings. See .env.example for documentation."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Hostaway -----------------------------------------------------
    hostaway_base_url: str = "https://api.hostaway.com/v1"
    hostaway_account_id: str = ""
    hostaway_api_key: str = ""

    # --- TTLock -------------------------------------------------------
    # The Open Platform is served from regional hosts; api.sciener.com is the
    # one named in the official docs and works for EU/global accounts.
    ttlock_base_url: str = "https://api.sciener.com"
    ttlock_client_id: str = ""
    ttlock_client_secret: str = ""
    ttlock_username: str = ""
    # Supply exactly one of these. ttlock_password is MD5'd by us before it
    # leaves the process; ttlock_password_md5 is passed through untouched.
    ttlock_password: str = ""
    ttlock_password_md5: str = ""

    # --- Webhook authentication --------------------------------------
    # Hostaway's unified webhooks authenticate with HTTP Basic, and we also
    # accept an unguessable path segment. Set at least one.
    webhook_basic_user: str = ""
    webhook_basic_password: str = ""
    webhook_path_secret: str = ""
    # Bearer token for the /admin endpoints. Leave blank to disable them.
    admin_token: str = ""

    # --- Service ------------------------------------------------------
    database_url: str = "sqlite:///./data/bridge.db"
    units_file: str = "units.yaml"
    log_level: str = "INFO"
    # How often the safety-net reconciler retries failed/pending ledger rows.
    reconcile_interval_seconds: int = 60
    # How often it re-pulls the whole booking window from Hostaway, to catch
    # events that were never delivered at all.
    full_sweep_minutes: int = 15
    # How far back the full sweep looks, so a stay in progress is still seen.
    sweep_lookback_days: int = 3
    # How far ahead to pre-create codes. Reservations further out than this are
    # held in the ledger and pushed to the lock when they come into range.
    horizon_days: int = 180
    # Grace period after checkout before the passcode is deleted from the lock.
    delete_after_checkout_minutes: int = 30
    # Write the generated code back to the Hostaway reservation's doorCode
    # field, so it appears in guest messaging templates.
    write_door_code_to_hostaway: bool = True
    # Refuse to touch real locks. Everything else runs; TTLock writes are
    # logged and skipped. Useful for a first dry run against live Hostaway.
    dry_run: bool = False

    @property
    def password_md5(self) -> str:
        import hashlib

        if self.ttlock_password_md5:
            return self.ttlock_password_md5.strip().lower()
        if self.ttlock_password:
            return hashlib.md5(self.ttlock_password.encode()).hexdigest()
        return ""


class LockConfig(BaseModel):
    """One physical door."""

    lock_id: int
    label: str = "Lock"
    # Overrides the unit strategy for this door (e.g. one door has a gateway).
    strategy: Strategy | None = None
    # The door whose code gets written back to Hostaway's reservation.doorCode,
    # i.e. the one that ends up in the guest's check-in instructions. Without
    # this the first listed lock wins, which makes the message content depend
    # on YAML ordering -- too subtle to leave to chance.
    primary: bool = False


class UnitConfig(BaseModel):
    """One Hostaway listing and the doors that belong to it."""

    listing_map_id: int
    name: str = ""
    timezone: str | None = None
    check_in_time: str | None = None
    check_out_time: str | None = None
    buffer_before_minutes: int | None = None
    buffer_after_minutes: int | None = None
    code_length: int | None = None
    strategy: Strategy | None = None
    # One code for every door in this unit -- e.g. a building entrance and the
    # flat behind it. The guest memorises one number, and it fits Hostaway's
    # single doorCode field. Only possible on custom-strategy (gateway) doors;
    # a generated code is derived per lock and cannot be shared.
    shared_code: bool | None = None
    locks: list[LockConfig] = Field(default_factory=list)

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as exc:  # pragma: no cover - config error
            raise ValueError(f"unknown timezone {v!r}") from exc
        return v


class Defaults(BaseModel):
    timezone: str = "UTC"
    check_in_time: str = "15:00"
    check_out_time: str = "11:00"
    buffer_before_minutes: int = 0
    buffer_after_minutes: int = 0
    code_length: int = 6
    strategy: Strategy = "auto"
    shared_code: bool = False
    # Hostaway statuses that mean "a guest is coming / is here".
    active_statuses: list[str] = Field(
        default_factory=lambda: ["new", "modified", "confirmed", "ownerStay"]
    )
    # Statuses that mean "revoke access now".
    cancelled_statuses: list[str] = Field(
        default_factory=lambda: [
            "cancelled",
            "declined",
            "expired",
            "inquiry",
            "inquiryNotPossible",
            "inquiryDenied",
            "inquiryTimedout",
            "inquiryNotInterested",
        ]
    )

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        ZoneInfo(v)
        return v


class ResolvedUnit(BaseModel):
    """A UnitConfig with every default filled in. This is what sync/ uses."""

    listing_map_id: int
    name: str
    timezone: str
    check_in_time: time
    check_out_time: time
    buffer_before_minutes: int
    buffer_after_minutes: int
    code_length: int
    locks: list[LockConfig]
    strategy: Strategy
    shared_code: bool

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def lock_strategy(self, lock: LockConfig) -> Strategy:
        return lock.strategy or self.strategy


class UnitMap(BaseModel):
    defaults: Defaults = Field(default_factory=Defaults)
    units: list[UnitConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_units(self) -> "UnitMap":
        seen: set[int] = set()
        for u in self.units:
            if u.listing_map_id in seen:
                raise ValueError(
                    f"listing_map_id {u.listing_map_id} appears twice in units.yaml"
                )
            seen.add(u.listing_map_id)
            primaries = [l for l in u.locks if l.primary]
            if len(primaries) > 1:
                raise ValueError(
                    f"listing_map_id {u.listing_map_id} marks {len(primaries)} locks "
                    "as primary; exactly one door's code goes into the guest's "
                    "check-in instructions"
                )
        return self

    # -- lookups -------------------------------------------------------

    def resolve(self, listing_map_id: int) -> ResolvedUnit | None:
        for u in self.units:
            if u.listing_map_id == listing_map_id:
                return self._resolve(u)
        return None

    def all_resolved(self) -> list[ResolvedUnit]:
        return [self._resolve(u) for u in self.units]

    def _resolve(self, u: UnitConfig) -> ResolvedUnit:
        d = self.defaults
        return ResolvedUnit(
            listing_map_id=u.listing_map_id,
            name=u.name or f"listing {u.listing_map_id}",
            timezone=u.timezone or d.timezone,
            check_in_time=_parse_time(u.check_in_time or d.check_in_time),
            check_out_time=_parse_time(u.check_out_time or d.check_out_time),
            buffer_before_minutes=_first(
                u.buffer_before_minutes, d.buffer_before_minutes
            ),
            buffer_after_minutes=_first(u.buffer_after_minutes, d.buffer_after_minutes),
            code_length=_first(u.code_length, d.code_length),
            locks=u.locks,
            strategy=u.strategy or d.strategy,
            shared_code=_first(u.shared_code, d.shared_code),
        )


def _first(*values):
    for v in values:
        if v is not None:
            return v
    return None


def _parse_time(value: str | int) -> time:
    """Accept '15:00', '15', 15 -- Hostaway sends check-in hours as ints."""
    if isinstance(value, int):
        return time(hour=value)
    value = str(value).strip()
    if ":" in value:
        hh, _, mm = value.partition(":")
        return time(hour=int(hh), minute=int(mm))
    return time(hour=int(value))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


_unit_map: UnitMap | None = None


def load_unit_map(path: str | Path | None = None) -> UnitMap:
    """Read units.yaml. Called at startup and by POST /admin/reload."""
    global _unit_map
    p = Path(path or get_settings().units_file)
    if not p.exists():
        log.warning("units file %s not found -- starting with an empty map", p)
        _unit_map = UnitMap()
        return _unit_map
    raw = yaml.safe_load(p.read_text()) or {}
    _unit_map = UnitMap.model_validate(raw)
    log.info(
        "loaded %d unit(s), %d lock(s) from %s",
        len(_unit_map.units),
        sum(len(u.locks) for u in _unit_map.units),
        p,
    )
    return _unit_map


def get_unit_map() -> UnitMap:
    if _unit_map is None:
        return load_unit_map()
    return _unit_map
