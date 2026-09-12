# Runbook

Day-to-day operation. Written for whoever is holding the phone at 7am when a
guest says the code does not work.

---

## First question, always: what does the bridge think?

```bash
export ADMIN=your-admin-token
export HOST=https://your-host

curl -s -H "Authorization: Bearer $ADMIN" $HOST/admin/status | jq
```

```json
{
  "ledger": {"rows": 84, "codes_live": 31, "out_of_sync": 0, "failing": 0,
             "out_of_sync_detail": []},
  "reconciler": {"last_retry_pass": "2026-09-12T07:02:11",
                 "last_full_sweep": "2026-09-12T06:55:03",
                 "last_error": null},
  "queue_depth": 0,
  "dry_run": false
}
```

* `out_of_sync: 0` and `last_error: null` → the bridge believes every door is
  correct.
* `last_full_sweep` older than ~20 minutes → the reconciler is stuck or the
  container restarted. Check logs.
* `dry_run: true` → **nothing is being written to any lock.** This is the single
  most common cause of "it stopped working" after a config change.

---

## "The guest's code doesn't work"

```bash
curl -s -H "Authorization: Bearer $ADMIN" $HOST/admin/reservations/12345678 | jq
```

Read it in this order:

1. **Is there a row at all?**
   No → the listing is not in `units.yaml`, or the reservation's status is not
   in `active_statuses`. Check `docker compose logs bridge | grep 12345678`;
   the bridge logs the reason it skipped.

2. **`in_sync: false` or `last_error` set?**
   The bridge tried and TTLock refused. The error text is the TTLock message
   verbatim. Common ones:
   * *gateway is busy or not connected* — the gateway is offline or out of
     range of that lock. Power-cycle the gateway; the bridge retries on its own.
   * *invalid token* — the TTLock app password changed. Update `.env`, restart.
   * *passcode already exists on this lock* — a code with those digits was added
     by hand in the TTLock app. Delete it there; the bridge reissues.

3. **`in_sync: true` — the code is on the lock.** Then check the window:
   `start_ms`/`end_ms` are UTC milliseconds.
   ```bash
   python3 -c "import datetime;print(datetime.datetime.fromtimestamp(1789567200000/1000))"
   ```
   If the window looks an hour off, the unit's `timezone` in `units.yaml` is
   wrong. If it starts too late, add `buffer_before_minutes`.

4. **Window is right, code is on the lock, keypad still refuses.**
   That is the lock, not the bridge. Check the battery in
   `GET /admin/locks`, and confirm the lock's clock — a TTLock that has drifted
   rejects time-bound codes. Re-sync it from the TTLock app.

Force a re-push at any point:

```bash
curl -XPOST -H "Authorization: Bearer $ADMIN" $HOST/admin/sync/12345678 | jq
```

It returns exactly what it did and any error, synchronously.

---

## "A guest still has access after checking out"

On a door **with a gateway** this should never persist: the code is deleted
within `DELETE_AFTER_CHECKOUT_MINUTES` of the window closing. Check
`/admin/reservations/{id}` — if `desired_present: false` and `in_sync: true`,
the code is gone from the lock.

On a door **without a gateway**, the code expires at its end time but cannot be
actively deleted until the lock is reachable. It will stop working on time;
it simply remains listed in the TTLock app. If you need certainty of removal on
a given door, that door needs a gateway.

---

## Adding a property

```bash
curl -s -H "Authorization: Bearer $ADMIN" $HOST/admin/locks | jq     # 1. get lock_id
$EDITOR units.yaml                                                   # 2. add the block
curl -XPOST -H "Authorization: Bearer $ADMIN" $HOST/admin/reload     # 3. reload
curl -XPOST -H "Authorization: Bearer $ADMIN" $HOST/admin/sweep      # 4. back-fill
```

Then confirm: `GET /admin/status` → `out_of_sync` should return to 0 within a
minute or two.

---

## Changing check-in times or buffers

Edit `units.yaml`, `POST /admin/reload`, `POST /admin/sweep`. The sweep
recomputes every live booking's window and pushes the change. Existing guests
keep their digits on gateway doors; on Bluetooth doors they are reissued, so
avoid doing this mid-stay unless you mean to.

---

## Restart / redeploy

```bash
docker compose up -d --build
```

Safe at any time. In-flight work is re-derived from the ledger on the next
pass; nothing is lost. Keep the `bridge-data` volume — see below.

## Backups

Only one thing matters: `/data/bridge.db`.

```bash
docker compose exec bridge python -c "import sqlite3,shutil;\
  c=sqlite3.connect('/data/bridge.db');b=sqlite3.connect('/data/backup.db');\
  c.backup(b);print('ok')"
docker compose cp bridge:/data/backup.db ./bridge-backup-$(date +%F).db
```

Losing it does not lock guests out — the reconciler rebuilds desired state from
Hostaway — but the `keyboardPwdId` of every code already on a lock is in there,
and without it those codes cannot be deleted programmatically. They would have
to be cleared in the TTLock app.

---

## Rotating credentials

| what | where | after |
|---|---|---|
| Hostaway API key | `.env` → `HOSTAWAY_API_KEY` | restart |
| TTLock app password | `.env` → `TTLOCK_PASSWORD` | restart |
| Webhook basic auth | `.env` **and** Hostaway's webhook screen | restart |
| Admin token | `.env` | restart |

Cached tokens live in the ledger's `tokens` table and are replaced
automatically on the first 401.

---

## Log lines worth knowing

| line | meaning |
|---|---|
| `webhook: event='reservation created' reservation=123 queued` | accepted, work handed to the queue |
| `webhook: ignored: event 'new message received' is not a reservation event` | normal; Hostaway sends message events too |
| `synced reservation 123: actions=[...] errors=[none]` | done |
| `reservation 123 skipped: listing 456 is not in units.yaml` | expected for unmanaged properties |
| `reservation 123 has unrecognised status 'x' -- treating as inactive` | **act on this.** Add the status to `active_statuses` if it should get a code |
| `reconciler: re-syncing reservation 123` | the safety net doing its job |
| `lock 999 is in units.yaml but not visible on the TTLock account` | wrong `lock_id`, or the lock is under a different TTLock account |

---

## Panic switch

To stop all lock writes immediately without losing state:

```bash
# add DRY_RUN=true to .env
docker compose up -d
```

The bridge keeps tracking desired state; it simply writes nothing. Set it back
to `false` and `POST /admin/sweep` to catch up.
