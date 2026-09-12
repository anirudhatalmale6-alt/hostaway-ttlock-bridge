# Hostaway → TTLock bridge

Reservation events in Hostaway become time-bound passcodes on TTLock smart
locks. A booking is confirmed, a code appears; the dates move, the code follows;
the guest checks out, the code goes away. No one touches anything.

```
Hostaway webhook ──► /webhooks/hostaway ──► queue ──► Syncer ──► TTLock API
                                                        ▲
                     Hostaway API (re-read) ────────────┤
                                                        │
                     Reconciler (every 60s / 15min) ────┘
```

---

## The one thing to decide first: do your locks have a gateway?

TTLock offers two ways to put a code on a door, and which one you can use is a
hardware question, not a software one.

| | **custom** (`keyboardPwd/add`) | **generated** (`keyboardPwd/get`) |
|---|---|---|
| Who picks the digits | We do | TTLock does |
| Needs a WiFi gateway | **Yes** | No |
| Code survives a date change | Yes — same digits, new window | No — new code issued |
| Can be revoked early | Yes, instantly | Only if the lock is reachable |

A lock with a **gateway** (G2/G3, or a WiFi lock model) is online, so we can
push and pull codes at will. A **Bluetooth-only** lock is not reachable from
the internet at all — so TTLock instead derives a code from the lock's own
secret and the time window. The lock recognises that code offline, without
ever having heard of the reservation. That is what makes unattended rentals
work on cheap hardware.

The bridge defaults to `strategy: auto`: it reads `hasGateway` from the TTLock
account and picks per door. You can override per unit or per lock in
`units.yaml`.

**What this means for gateway-less doors:** the code is bounded by the stay
window, so checkout is handled by expiry. But an *early revocation* (cancelled
booking, guest leaves early) cannot reach the lock until someone is next in
Bluetooth range. If a booking can be cancelled at short notice and that matters
to you, that door needs a gateway. This is a TTLock platform limit; no
middleware can work around it.

---

## Setup

### 1. Credentials

```bash
cp .env.example .env
$EDITOR .env
```

* **Hostaway** — dashboard → Settings → Hostaway API → create a key. You need
  the numeric *account id* and the *API key*.
* **TTLock** — `clientId`/`clientSecret` from the Open Platform developer
  console, plus the username and password of the **TTLock app account that owns
  the locks** (not the developer login). The password is MD5'd inside the
  service before it is sent; set `TTLOCK_PASSWORD_MD5` instead if you would
  rather not store the plaintext.
* **WEBHOOK_BASIC_USER / WEBHOOK_BASIC_PASSWORD** — invent these; you will type
  the same values into Hostaway in step 4.
* **ADMIN_TOKEN** — invent this too. It guards `/admin/*`. Leave it empty and
  those endpoints are disabled entirely.

### 2. Map listings to locks

```bash
cp units.example.yaml units.yaml
```

You need two ids per door. Get the lock ids from the service itself:

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/admin/locks
```

```json
{"locks": [
  {"lock_id": 9876543, "alias": "Seaside front door", "has_gateway": true,
   "battery": 88, "mapped_to": null}
]}
```

The Hostaway `listingMapId` is on the listing in Hostaway, or on any
reservation for that property.

```yaml
units:
  - listing_map_id: 123456
    name: "Seaside Cottage"
    locks:
      - lock_id: 9876543
        label: "Front door"
```

### 3. Run it

```bash
docker compose up -d --build
curl -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/readyz
```

`/readyz` is the pre-flight check: it authenticates against both platforms and
tells you if a lock in `units.yaml` is not actually on the TTLock account.

Put TLS in front of it — Caddy, nginx, or a Cloudflare Tunnel. Hostaway will
only deliver to `https://`.

### 4. Point Hostaway at it

Hostaway → Settings → Webhooks → new unified webhook:

* URL: `https://your-host/webhooks/hostaway`
  (or `https://your-host/webhooks/hostaway/<WEBHOOK_PATH_SECRET>` if you set one)
* Login / Password: the `WEBHOOK_BASIC_*` values from `.env`

### 5. Back-fill existing bookings

```bash
curl -XPOST -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/admin/sweep
```

This reads every reservation in the live window and issues the codes that are
missing. Safe to run at any time; it is the same pass that runs every 15
minutes anyway.

**First time, consider `DRY_RUN=true`.** Everything runs — webhooks, Hostaway
reads, the full sweep, the logs — but nothing is written to a lock. It is the
cheapest way to confirm the listing map is right before real doors change.

---

## Adding a property later

1. `GET /admin/locks` → copy the new `lock_id`.
2. Add the block to `units.yaml`.
3. `POST /admin/reload` — re-reads the file, no restart.
4. `POST /admin/sweep` — back-fills codes for bookings already on the calendar.

No code change, no redeploy, no downtime. A unit can have as many locks as it
has doors; all of them get the same window.

---

## Getting the code into the guest's check-in instructions

The bridge writes the code back onto the Hostaway reservation's `doorCode`
field (`WRITE_DOOR_CODE_TO_HOSTAWAY=true`, the default). Hostaway's own message
automations read that field, so the existing check-in-instruction template
picks the code up with no extra integration.

Which door's code? The lock marked `primary: true` in `units.yaml`. Mark
exactly one per unit — the front door, normally. Without it the first
configured lock wins, which makes the guest's message depend on YAML ordering.

Two timing notes, both from Hostaway's side:

* Hostaway holds a message carrying a door-code tag for about **15 minutes**
  to give integrations like this one time to fill the field in. We write the
  code the moment the booking is confirmed — usually days ahead — so the field
  is populated long before any message is due.
* If `doorCode` is still empty when that window closes, Hostaway falls back to
  the **listing's default door code**. That is the failure mode to watch for: a
  guest gets a plausible-looking code that does not open the door. If you have
  a listing-level default set, consider clearing it so a miss is loud instead
  of silent.

---

## Configuration reference

`units.yaml` — `defaults:` applies to every unit, and any key can be overridden
per unit; `strategy` can additionally be overridden per lock.

| key | meaning |
|---|---|
| `timezone` | IANA name. Hostaway sends local dates; TTLock wants UTC instants. This is what converts between them, DST included. |
| `check_in_time` / `check_out_time` | Fallback when the reservation carries no time. |
| `buffer_before_minutes` / `buffer_after_minutes` | Widen the window at each end — early arrivals, late departures, cleaners. |
| `code_length` | 4–9 digits. `custom` strategy only. |
| `primary` (per lock) | This door's code is written to Hostaway's `doorCode`. One per unit. |
| `strategy` | `auto` (default) / `custom` / `generated`. |
| `active_statuses` | Hostaway statuses that mean "give access". Anything unrecognised is treated as inactive — it fails closed. |
| `cancelled_statuses` | Statuses that mean "revoke now". |

`.env` — see `.env.example`; every variable is documented there.

---

## Endpoints

| | |
|---|---|
| `POST /webhooks/hostaway` | Hostaway posts here. Always answers 200. |
| `GET /healthz` | Liveness. Touches nothing upstream. |
| `GET /readyz` | Authenticates to both APIs, verifies every configured lock exists. 503 if not. |
| `GET /admin/status` | Ledger summary: codes live, rows out of sync, last error, when the reconciler last ran. |
| `GET /admin/locks` | Every lock on the account, gateway status, battery, which listing it is mapped to. |
| `GET /admin/reservations/{id}` | The codes this booking holds, per door. |
| `POST /admin/sync/{id}` | Force one reservation through now. |
| `POST /admin/sweep` | Force a full reconcile. |
| `POST /admin/reload` | Re-read `units.yaml`. |

`/admin/*` needs `Authorization: Bearer $ADMIN_TOKEN`.

---

## Why it is built this way

**Desired state, not events.** Each `(reservation, lock)` pair is one ledger
row holding what the door *should* look like and what TTLock has *confirmed*.
Sync drives one towards the other. A duplicate webhook is a no-op; a crash
mid-write leaves a row marked out of sync and the next pass finishes it.

**The webhook body is never trusted.** It is used for one thing: the
reservation id. The reservation is then re-read from the Hostaway API. Hostaway
states plainly that events can arrive out of order, and an older payload
arriving second would otherwise overwrite a newer edit.

**Webhooks are assumed to fail.** Hostaway retries three times over about an
hour, then gives up. One bad deploy at the wrong moment and a guest arrives to
a dead keypad. So a reconciler re-reads the whole booking window every 15
minutes regardless. Nothing depends on any single delivery arriving.

**It fails closed.** An unrecognised reservation status is treated as
inactive — no code. A lock that is in `units.yaml` but not on the account makes
`/readyz` go red rather than being silently skipped.

**The 200 is unconditional.** A non-2xx makes Hostaway retry for an hour and
then disable the webhook. Our own retry loop is a better place to handle a
failure than theirs.

---

## Tests

```bash
make test        # everything, ~40s
make test-fast   # skips the two that wait on the reconciler
```

56 tests. The end-to-end suite runs three real HTTP servers on loopback — a
mock Hostaway, a mock TTLock, and the bridge itself under uvicorn. Nothing is
monkeypatched: tests change a booking on the mock Hostaway, which delivers a
genuine webhook over the network, and then assert on what ended up on the mock
lock. The mocks reproduce the real contracts, including TTLock's habit of
reporting failures as `errcode` inside an HTTP 200, and a gateway-less lock
rejecting `addType=2`.

What is proven, beyond the three headline scenarios: codes land on the exact
millisecond boundaries of the stay; a date change keeps the guest's digits on a
gateway door and reissues cleanly on a Bluetooth one; moving a booking between
properties opens the new door *and closes the old one*; duplicate webhooks do
not stack codes; a TTLock outage is retried to success with no human
involvement; a webhook that never arrives is repaired by the sweep; and DST
boundaries do not shift check-out by an hour.

See `docs/RUNBOOK.md` for day-to-day operation and troubleshooting.

---

## Limits worth knowing

* **One replica.** The ledger is SQLite and the reconciler is a singleton. Two
  containers would race onto the same locks. For a portfolio of this size one
  container is ample; if you ever outgrow it, point `DATABASE_URL` at Postgres
  and move the reconciler behind an advisory lock.
* **Early revocation needs a reachable lock.** See the gateway section above.
* **Codes are 4–9 digits**, TTLock's limit.
* **Deleting the ledger volume** does not lock anyone out — the sweep rebuilds
  desired state from Hostaway — but it orphans passcodes already on the locks,
  since the `keyboardPwdId` needed to remove them is gone. Back it up.
