"""TTLock Open Platform client.

Auth is OAuth2 *resource owner password*: the developer clientId/clientSecret
plus the TTLock **app** account the locks are registered under. The password
must be sent as a lowercase 32-char MD5 hex digest.

Two ways to put a code on a door, and which one you can use depends on the
hardware:

``keyboardPwd/get``   ("generated")
    TTLock's server derives the code from the lock's own secret, so the lock
    recognises it **without ever being contacted**. No gateway needed. This is
    what makes unattended rentals work on Bluetooth-only locks.

``keyboardPwd/add``   ("custom")
    You choose the digits, but the code has to be *delivered* to the lock --
    over Bluetooth (someone standing at the door) or through a WiFi gateway.
    Nicer for guests (memorable, stable across date changes) but it requires a
    gateway for unattended operation.

The bridge picks per lock via ``strategy: auto``, using ``hasGateway`` from
``lock/list``.

Reference: https://euopen.ttlock.com/doc/api/
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

# keyboardPwdType values for keyboardPwd/get
PWD_TYPE_ONCE = 1
PWD_TYPE_PERMANENT = 2
PWD_TYPE_PERIOD = 3

# addType / changeType / deleteType
VIA_BLUETOOTH = 1
VIA_GATEWAY = 2
VIA_NBIOT = 3

# TTLock returns these when the access token is stale.
TOKEN_ERRCODES = {10003}


class TTLockError(RuntimeError):
    def __init__(self, message: str, errcode: int | None = None, body: Any = None):
        super().__init__(message)
        self.errcode = errcode
        self.body = body


def now_ms() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)


class TTLockClient:
    PROVIDER = "ttlock"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.s = settings
        self.base = settings.ttlock_base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=45.0)
        self._owns_client = client is None
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: dt.datetime | None = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- auth ----------------------------------------------------------

    async def _token(self) -> str:
        async with self._lock:
            if self._access_token and not self._expired():
                return self._access_token
            cached = self._load_cached()
            if cached:
                self._access_token, self._refresh_token, self._expires_at = cached
                if self._access_token and not self._expired():
                    return self._access_token
            if self._refresh_token:
                try:
                    return await self._do_refresh()
                except TTLockError as exc:
                    log.warning("ttlock: refresh failed (%s), re-authenticating", exc)
            return await self._do_password_grant()

    def _expired(self) -> bool:
        if self._expires_at is None:
            return True
        return utcnow() >= self._expires_at - dt.timedelta(hours=1)

    async def _do_password_grant(self) -> str:
        pwd = self.s.password_md5
        if not pwd:
            raise TTLockError(
                "no TTLock password configured "
                "(set TTLOCK_PASSWORD or TTLOCK_PASSWORD_MD5)"
            )
        body = await self._post_form(
            "/oauth2/token",
            {
                "client_id": self.s.ttlock_client_id,
                "client_secret": self.s.ttlock_client_secret,
                "username": self.s.ttlock_username,
                "password": pwd,
            },
        )
        return self._store_token_response(body)

    async def _do_refresh(self) -> str:
        body = await self._post_form(
            "/oauth2/refreshToken",
            {
                "client_id": self.s.ttlock_client_id,
                "client_secret": self.s.ttlock_client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token or "",
            },
        )
        return self._store_token_response(body)

    def _store_token_response(self, body: dict) -> str:
        token = body.get("access_token")
        if not token:
            raise TTLockError(
                f"no access_token in TTLock auth response: {body}",
                errcode=body.get("errcode"),
                body=body,
            )
        self._access_token = token
        self._refresh_token = body.get("refresh_token") or self._refresh_token
        expires_in = int(body.get("expires_in") or 7776000)
        self._expires_at = utcnow() + dt.timedelta(seconds=expires_in)
        with session_scope() as s:
            row = s.get(TokenCache, self.PROVIDER)
            if row is None:
                row = TokenCache(provider=self.PROVIDER)
                s.add(row)
            row.access_token = token
            row.refresh_token = self._refresh_token
            row.expires_at = self._expires_at
        log.info("ttlock: obtained access token (expires %s)", self._expires_at)
        return token

    def _load_cached(self) -> tuple[str, str | None, dt.datetime | None] | None:
        with session_scope() as s:
            row = s.get(TokenCache, self.PROVIDER)
            if row and row.access_token:
                return row.access_token, row.refresh_token, row.expires_at
        return None

    def invalidate_token(self) -> None:
        self._access_token = None
        self._expires_at = None

    # -- transport -----------------------------------------------------

    async def _post_form(self, path: str, data: dict) -> dict:
        r = await self._client.post(
            f"{self.base}{path}",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if r.status_code >= 400:
            raise TTLockError(f"POST {path} -> {r.status_code}: {r.text}")
        return r.json()

    async def _call(
        self, path: str, params: dict, *, method: str = "POST", _retried: bool = False
    ) -> dict:
        token = await self._token()
        payload = {
            "clientId": self.s.ttlock_client_id,
            "accessToken": token,
            "date": now_ms(),
            **{k: v for k, v in params.items() if v is not None},
        }
        if method == "GET":
            r = await self._client.get(f"{self.base}{path}", params=payload)
        else:
            r = await self._client.post(
                f"{self.base}{path}",
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if r.status_code >= 400:
            raise TTLockError(f"{method} {path} -> {r.status_code}: {r.text}")
        body = r.json()
        errcode = body.get("errcode")
        if errcode in TOKEN_ERRCODES and not _retried:
            log.warning("ttlock: token rejected (errcode %s), re-authenticating", errcode)
            self.invalidate_token()
            await self._do_password_grant()
            return await self._call(path, params, method=method, _retried=True)
        if errcode not in (None, 0):
            raise TTLockError(
                f"{path} failed: errcode={errcode} errmsg={body.get('errmsg')!r} "
                f"description={body.get('description')!r}",
                errcode=errcode,
                body=body,
            )
        return body

    # -- locks ---------------------------------------------------------

    async def list_locks(self, page_size: int = 100) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            body = await self._call(
                "/v3/lock/list",
                {"pageNo": page, "pageSize": page_size},
                method="GET",
            )
            items = body.get("list") or []
            out.extend(items)
            if len(items) < page_size or page >= int(body.get("pages") or page):
                break
            page += 1
        return out

    async def lock_detail(self, lock_id: int) -> dict:
        return await self._call("/v3/lock/detail", {"lockId": lock_id}, method="GET")

    async def list_passcodes(self, lock_id: int, page_size: int = 100) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            body = await self._call(
                "/v3/lock/listKeyboardPwd",
                {"lockId": lock_id, "pageNo": page, "pageSize": page_size},
                method="GET",
            )
            items = body.get("list") or []
            out.extend(items)
            if len(items) < page_size or page >= int(body.get("pages") or page):
                break
            page += 1
        return out

    # -- passcodes -----------------------------------------------------

    async def add_custom_passcode(
        self,
        *,
        lock_id: int,
        code: str,
        name: str,
        start_ms: int,
        end_ms: int,
        add_type: int = VIA_GATEWAY,
    ) -> dict:
        """keyboardPwd/add -- our digits, delivered to the lock."""
        body = await self._call(
            "/v3/keyboardPwd/add",
            {
                "lockId": lock_id,
                "keyboardPwd": code,
                "keyboardPwdName": name,
                "startDate": start_ms,
                "endDate": end_ms,
                "addType": add_type,
            },
        )
        pwd_id = body.get("keyboardPwdId")
        if not pwd_id:
            raise TTLockError(f"add returned no keyboardPwdId: {body}", body=body)
        return {"keyboard_pwd_id": int(pwd_id), "code": code}

    async def generate_period_passcode(
        self,
        *,
        lock_id: int,
        name: str,
        start_ms: int,
        end_ms: int,
        keyboard_pwd_version: int = 4,
    ) -> dict:
        """keyboardPwd/get -- TTLock derives the digits; no gateway required."""
        body = await self._call(
            "/v3/keyboardPwd/get",
            {
                "lockId": lock_id,
                "keyboardPwdVersion": keyboard_pwd_version,
                "keyboardPwdType": PWD_TYPE_PERIOD,
                "keyboardPwdName": name,
                "startDate": start_ms,
                "endDate": end_ms,
            },
        )
        code = body.get("keyboardPwd")
        pwd_id = body.get("keyboardPwdId")
        if not code or not pwd_id:
            raise TTLockError(f"get returned no passcode: {body}", body=body)
        return {"keyboard_pwd_id": int(pwd_id), "code": str(code)}

    async def change_passcode(
        self,
        *,
        lock_id: int,
        keyboard_pwd_id: int,
        name: str | None = None,
        new_code: str | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        change_type: int = VIA_GATEWAY,
    ) -> None:
        await self._call(
            "/v3/keyboardPwd/change",
            {
                "lockId": lock_id,
                "keyboardPwdId": keyboard_pwd_id,
                "keyboardPwdName": name,
                "newKeyboardPwd": new_code,
                "startDate": start_ms,
                "endDate": end_ms,
                "changeType": change_type,
            },
        )

    async def delete_passcode(
        self,
        *,
        lock_id: int,
        keyboard_pwd_id: int,
        delete_type: int = VIA_GATEWAY,
    ) -> None:
        await self._call(
            "/v3/keyboardPwd/delete",
            {
                "lockId": lock_id,
                "keyboardPwdId": keyboard_pwd_id,
                "deleteType": delete_type,
            },
        )
