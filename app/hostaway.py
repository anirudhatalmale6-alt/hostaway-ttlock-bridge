"""Hostaway Public API client.

Auth is OAuth2 client_credentials: account ID as client_id, API key as
client_secret, scope ``general``.  Tokens are long lived (the docs say 24
months) so we cache them in the ledger and only re-mint on 401 or expiry.

Reference: https://api.hostaway.com/documentation
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

import httpx

from .config import Settings
from .db import TokenCache, session_scope, utcnow

log = logging.getLogger(__name__)


class HostawayError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class HostawayClient:
    PROVIDER = "hostaway"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.s = settings
        self.base = settings.hostaway_base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None
        self._token: str | None = None
        self._expires_at: dt.datetime | None = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- auth ----------------------------------------------------------

    async def _get_token(self, force: bool = False) -> str:
        async with self._lock:
            if not force:
                if self._token and not self._expired():
                    return self._token
                cached = self._load_cached()
                if cached:
                    self._token, self._expires_at = cached
                    if not self._expired():
                        return self._token

            data = {
                "grant_type": "client_credentials",
                "client_id": self.s.hostaway_account_id,
                "client_secret": self.s.hostaway_api_key,
                "scope": "general",
            }
            r = await self._client.post(
                f"{self.base}/accessTokens",
                data=data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cache-control": "no-cache",
                },
            )
            if r.status_code >= 400:
                raise HostawayError(
                    f"token request failed: {r.status_code}", r.status_code, r.text
                )
            body = r.json()
            token = body.get("access_token")
            if not token:
                raise HostawayError("token response had no access_token", body=body)
            expires_in = int(body.get("expires_in") or 3600)
            self._token = token
            self._expires_at = utcnow() + dt.timedelta(seconds=expires_in)
            self._store_cached(token, self._expires_at)
            # Documented quirk: a freshly minted token is only valid one second
            # after it is returned. Cheap insurance against a spurious 401.
            await asyncio.sleep(1.1)
            log.info("hostaway: obtained new access token")
            return token

    def _expired(self) -> bool:
        if self._expires_at is None:
            return True
        return utcnow() >= self._expires_at - dt.timedelta(minutes=5)

    def _load_cached(self) -> tuple[str, dt.datetime] | None:
        with session_scope() as s:
            row = s.get(TokenCache, self.PROVIDER)
            if row and row.access_token and row.expires_at:
                return row.access_token, row.expires_at
        return None

    def _store_cached(self, token: str, expires_at: dt.datetime) -> None:
        with session_scope() as s:
            row = s.get(TokenCache, self.PROVIDER)
            if row is None:
                row = TokenCache(provider=self.PROVIDER)
                s.add(row)
            row.access_token = token
            row.expires_at = expires_at

    # -- transport -----------------------------------------------------

    async def _request(
        self, method: str, path: str, *, retry_auth: bool = True, **kw
    ) -> Any:
        token = await self._get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Cache-control": "no-cache",
        }
        headers.update(kw.pop("headers", {}))
        r = await self._client.request(
            method, f"{self.base}{path}", headers=headers, **kw
        )
        if r.status_code == 401 and retry_auth:
            log.warning("hostaway: 401, re-minting token")
            await self._get_token(force=True)
            return await self._request(method, path, retry_auth=False, **kw)
        if r.status_code >= 400:
            raise HostawayError(
                f"{method} {path} -> {r.status_code}", r.status_code, r.text
            )
        body = r.json()
        if isinstance(body, dict) and body.get("status") == "fail":
            raise HostawayError(
                f"{method} {path} returned status=fail: {body.get('message')}",
                r.status_code,
                body,
            )
        return body

    # -- endpoints -----------------------------------------------------

    async def get_reservation(self, reservation_id: str | int) -> dict:
        """Single reservation. This is our source of truth -- we never trust
        the webhook body, because two edits seconds apart can be delivered out
        of order and the older payload would win."""
        body = await self._request("GET", f"/reservations/{reservation_id}")
        result = body.get("result") if isinstance(body, dict) else None
        if not result:
            raise HostawayError(f"reservation {reservation_id} not found", body=body)
        return result

    async def list_reservations(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        sort_order: str = "arrivalDateAsc",
        params: dict | None = None,
    ) -> list[dict]:
        q = {"limit": limit, "offset": offset, "sortOrder": sort_order}
        if params:
            q.update(params)
        body = await self._request("GET", "/reservations", params=q)
        return body.get("result") or []

    async def iter_reservations_in_window(
        self, start: dt.date, end: dt.date, page_size: int = 100
    ) -> list[dict]:
        """Reservations departing on/after ``start`` and arriving on/before
        ``end``.  Used by the reconciler to rebuild desired state from scratch.
        """
        out: list[dict] = []
        offset = 0
        while True:
            page = await self.list_reservations(
                limit=page_size,
                offset=offset,
                params={
                    "dateType": "arrivalDate",
                    "startDate": start.isoformat(),
                    "endDate": end.isoformat(),
                },
            )
            out.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
            if offset > 10_000:  # guard against a pagination bug looping forever
                log.warning("hostaway: stopping pagination at offset %d", offset)
                break
        return out

    async def list_listings(self, page_size: int = 100) -> list[dict]:
        out: list[dict] = []
        offset = 0
        while True:
            body = await self._request(
                "GET", "/listings", params={"limit": page_size, "offset": offset}
            )
            page = body.get("result") or []
            out.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
            if offset > 10_000:  # pragma: no cover - pagination guard
                break
        return out

    async def list_custom_fields(self) -> list[dict]:
        """Custom field *definitions* (id, name, type, object it belongs to).

        The id is what `customFieldValues` entries reference, so this is how you
        turn "Building Door Code" into something the bridge can write to.
        """
        body = await self._request("GET", "/customFields")
        return body.get("result") or []

    async def get_listing(self, listing_id: str | int) -> dict:
        body = await self._request(
            "GET", f"/listings/{listing_id}", params={"includeResources": 1}
        )
        result = body.get("result") if isinstance(body, dict) else None
        if not result:
            raise HostawayError(f"listing {listing_id} not found", body=body)
        return result

    async def get_reservation_with_resources(self, reservation_id: str | int) -> dict:
        """Hidden custom fields (isPublic=0) only come back with this flag."""
        body = await self._request(
            "GET",
            f"/reservations/{reservation_id}",
            params={"includeResources": 1},
        )
        result = body.get("result") if isinstance(body, dict) else None
        if not result:
            raise HostawayError(f"reservation {reservation_id} not found", body=body)
        return result

    async def set_door_code(self, reservation_id: str | int, code: str) -> None:
        """Write the code onto the reservation so Hostaway's guest-messaging
        templates can use {{doorCode}}.  Best effort -- never fatal."""
        await self._request(
            "PUT",
            f"/reservations/{reservation_id}",
            params={"forceOverbooking": 0},
            json={"doorCode": code},
        )
