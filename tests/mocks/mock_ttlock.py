"""A stand-in for the TTLock Open Platform.

Mirrors the real contract closely enough that a test failure means a real
failure: form-encoded POSTs, ``errcode``/``errmsg`` envelopes (HTTP 200 even on
error, which is how TTLock actually reports problems), MD5 password auth, and
the two distinct ways of putting a code on a door.

It also models the bit that matters operationally: a lock with no gateway
rejects ``keyboardPwd/add`` with addType=2, exactly like the real platform,
which is what forces the bridge onto the generated-code path.
"""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

CLIENT_ID = "ttlock-test-client"
CLIENT_SECRET = "ttlock-test-secret"
USERNAME = "owner@example.com"
PASSWORD_MD5 = hashlib.md5(b"hunter2hunter2").hexdigest()

ERR_INVALID_TOKEN = 10003
ERR_NO_GATEWAY = -3002
ERR_BAD_PARAM = -1


class State:
    def __init__(self) -> None:
        self.locks: dict[int, dict] = {}
        self.passcodes: dict[int, dict] = {}
        self.next_pwd_id = 1000
        self.tokens: set[str] = set()
        self.refresh_tokens: set[str] = set()
        self.calls: list[tuple[str, dict]] = []
        # Positive N: fail the next N write calls. Negative: fail every write
        # until cleared. Zero: behave normally.
        self.fail_next_writes = 0

    def reset(self) -> None:
        self.passcodes.clear()
        self.calls.clear()
        self.fail_next_writes = 0


state = State()
app = FastAPI(title="mock-ttlock")


def seed_locks(locks: list[dict]) -> None:
    state.locks = {int(l["lockId"]): l for l in locks}


def err(code: int, msg: str) -> JSONResponse:
    # TTLock returns HTTP 200 with an errcode body. Getting this wrong in a
    # mock is how you ship a client that treats every failure as success.
    return JSONResponse({"errcode": code, "errmsg": msg, "description": msg})


async def _params(request: Request) -> dict[str, Any]:
    if request.method == "GET":
        return dict(request.query_params)
    form = await request.form()
    return {k: v for k, v in form.items()}


def _check_auth(p: dict) -> JSONResponse | None:
    if p.get("clientId") != CLIENT_ID:
        return err(ERR_BAD_PARAM, "invalid clientId")
    if p.get("accessToken") not in state.tokens:
        return err(ERR_INVALID_TOKEN, "invalid token")
    if not p.get("date"):
        return err(ERR_BAD_PARAM, "date is required")
    return None


# -- oauth --------------------------------------------------------------


@app.post("/oauth2/token")
async def token(request: Request) -> JSONResponse:
    form = await request.form()
    if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
        return err(ERR_BAD_PARAM, "invalid client")
    if form.get("username") != USERNAME or form.get("password") != PASSWORD_MD5:
        return err(ERR_BAD_PARAM, "invalid user credentials")
    access = "tt_" + hashlib.md5(str(len(state.tokens)).encode()).hexdigest()
    refresh = "rt_" + access
    state.tokens.add(access)
    state.refresh_tokens.add(refresh)
    return JSONResponse(
        {
            "access_token": access,
            "refresh_token": refresh,
            "uid": 4242,
            "expires_in": 7776000,
            "scope": "user,key,room",
        }
    )


@app.post("/oauth2/refreshToken")
async def refresh_token(request: Request) -> JSONResponse:
    form = await request.form()
    if form.get("refresh_token") not in state.refresh_tokens:
        return err(ERR_BAD_PARAM, "invalid refresh token")
    access = "tt_r" + hashlib.md5(str(len(state.tokens)).encode()).hexdigest()
    state.tokens.add(access)
    return JSONResponse(
        {
            "access_token": access,
            "refresh_token": form.get("refresh_token"),
            "uid": 4242,
            "expires_in": 7776000,
        }
    )


# -- locks --------------------------------------------------------------


@app.get("/v3/lock/list")
async def lock_list(request: Request) -> JSONResponse:
    p = await _params(request)
    if (bad := _check_auth(p)) is not None:
        return bad
    items = list(state.locks.values())
    page_no = int(p.get("pageNo") or 1)
    page_size = int(p.get("pageSize") or 20)
    chunk = items[(page_no - 1) * page_size : page_no * page_size]
    return JSONResponse(
        {
            "list": chunk,
            "pageNo": page_no,
            "pageSize": page_size,
            "pages": max(1, -(-len(items) // page_size)),
            "total": len(items),
        }
    )


@app.get("/v3/lock/listKeyboardPwd")
async def list_keyboard_pwd(request: Request) -> JSONResponse:
    p = await _params(request)
    if (bad := _check_auth(p)) is not None:
        return bad
    lock_id = int(p["lockId"])
    items = [c for c in state.passcodes.values() if c["lockId"] == lock_id]
    return JSONResponse(
        {"list": items, "pageNo": 1, "pageSize": len(items), "pages": 1,
         "total": len(items)}
    )


# -- passcodes ----------------------------------------------------------


def _write_guard() -> JSONResponse | None:
    if state.fail_next_writes != 0:
        if state.fail_next_writes > 0:
            state.fail_next_writes -= 1
        return err(-4001, "injected failure")
    return None


@app.post("/v3/keyboardPwd/add")
async def keyboard_pwd_add(request: Request) -> JSONResponse:
    p = await _params(request)
    state.calls.append(("add", p))
    if (bad := _check_auth(p)) is not None:
        return bad
    if (bad := _write_guard()) is not None:
        return bad
    lock_id = int(p["lockId"])
    lock = state.locks.get(lock_id)
    if lock is None:
        return err(ERR_BAD_PARAM, "lock not found")
    add_type = int(p.get("addType") or 1)
    if add_type == 2 and not lock.get("hasGateway"):
        return err(ERR_NO_GATEWAY, "gateway is busy or not connected")
    code = str(p.get("keyboardPwd") or "")
    if not code.isdigit() or not 4 <= len(code) <= 9:
        return err(ERR_BAD_PARAM, "keyboardPwd must be 4-9 digits")
    clash = [
        c
        for c in state.passcodes.values()
        if c["lockId"] == lock_id and c["keyboardPwd"] == code
    ]
    if clash:
        return err(-3007, "passcode already exists on this lock")
    state.next_pwd_id += 1
    pwd_id = state.next_pwd_id
    state.passcodes[pwd_id] = {
        "keyboardPwdId": pwd_id,
        "lockId": lock_id,
        "keyboardPwd": code,
        "keyboardPwdName": p.get("keyboardPwdName") or "",
        "keyboardPwdType": 3,
        "keyboardPwdVersion": lock.get("keyboardPwdVersion", 4),
        "startDate": int(p["startDate"]),
        "endDate": int(p["endDate"]),
        "isCustom": 1,
        "status": 1,
    }
    return JSONResponse({"keyboardPwdId": pwd_id})


@app.post("/v3/keyboardPwd/get")
async def keyboard_pwd_get(request: Request) -> JSONResponse:
    p = await _params(request)
    state.calls.append(("get", p))
    if (bad := _check_auth(p)) is not None:
        return bad
    if (bad := _write_guard()) is not None:
        return bad
    lock_id = int(p["lockId"])
    lock = state.locks.get(lock_id)
    if lock is None:
        return err(ERR_BAD_PARAM, "lock not found")
    pwd_type = int(p.get("keyboardPwdType") or 0)
    if pwd_type == 3 and not (p.get("startDate") and p.get("endDate")):
        return err(ERR_BAD_PARAM, "period passcode needs startDate and endDate")
    state.next_pwd_id += 1
    pwd_id = state.next_pwd_id
    # The real platform derives the digits from the lock secret and window;
    # any deterministic function of those is a faithful enough stand-in.
    seed = f"{lock_id}:{p.get('startDate')}:{p.get('endDate')}:{pwd_id}"
    digits = str(int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16))[-6:].rjust(
        6, "7"
    )
    state.passcodes[pwd_id] = {
        "keyboardPwdId": pwd_id,
        "lockId": lock_id,
        "keyboardPwd": digits,
        "keyboardPwdName": p.get("keyboardPwdName") or "",
        "keyboardPwdType": pwd_type,
        "keyboardPwdVersion": lock.get("keyboardPwdVersion", 4),
        "startDate": int(p.get("startDate") or 0),
        "endDate": int(p.get("endDate") or 0),
        "isCustom": 0,
        "status": 1,
    }
    return JSONResponse({"keyboardPwd": digits, "keyboardPwdId": pwd_id})


@app.post("/v3/keyboardPwd/change")
async def keyboard_pwd_change(request: Request) -> JSONResponse:
    p = await _params(request)
    state.calls.append(("change", p))
    if (bad := _check_auth(p)) is not None:
        return bad
    if (bad := _write_guard()) is not None:
        return bad
    pwd_id = int(p["keyboardPwdId"])
    record = state.passcodes.get(pwd_id)
    if record is None:
        return err(-3003, "passcode does not exist")
    lock = state.locks.get(record["lockId"], {})
    if int(p.get("changeType") or 1) == 2 and not lock.get("hasGateway"):
        return err(ERR_NO_GATEWAY, "gateway is busy or not connected")
    if p.get("newKeyboardPwd"):
        record["keyboardPwd"] = str(p["newKeyboardPwd"])
    if p.get("keyboardPwdName"):
        record["keyboardPwdName"] = p["keyboardPwdName"]
    if p.get("startDate"):
        record["startDate"] = int(p["startDate"])
    if p.get("endDate"):
        record["endDate"] = int(p["endDate"])
    return JSONResponse({"errcode": 0, "errmsg": "none", "description": "none"})


@app.post("/v3/keyboardPwd/delete")
async def keyboard_pwd_delete(request: Request) -> JSONResponse:
    p = await _params(request)
    state.calls.append(("delete", p))
    if (bad := _check_auth(p)) is not None:
        return bad
    if (bad := _write_guard()) is not None:
        return bad
    pwd_id = int(p["keyboardPwdId"])
    if pwd_id not in state.passcodes:
        return err(-3003, "passcode does not exist")
    del state.passcodes[pwd_id]
    return JSONResponse({"errcode": 0, "errmsg": "none", "description": "none"})


# -- test control -------------------------------------------------------


@app.get("/__test__/passcodes")
async def dump_passcodes() -> dict:
    return {"passcodes": list(state.passcodes.values())}


@app.post("/__test__/reset")
async def reset() -> dict:
    state.reset()
    return {"ok": True}


@app.post("/__test__/fail_next_writes/{n}")
async def fail_next_writes(n: int) -> dict:
    state.fail_next_writes = n
    return {"ok": True, "fail_next_writes": n}
