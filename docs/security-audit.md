# PDC Funding security audit

Scope: this repository as of the 0.1.1 fix set. Method: read the request path, wallet RPC client, scanner, and SQLite writes; run the existing tests (6 passed) before edits; add regression tests for each fix. No live wallet or daemon was contacted.

Status: **READY FOR REVIEW**. Not a production approval. Escrow remains custodial.

## Priority

| ID | Sev | Status | Issue |
| --- | --- | --- | --- |
| F-1 | P0 | Fixed | Two payouts could both observe the same available balance, both broadcast, and both insert. Escrow accounting then exceeded deposits. |
| F-2 | P0 | Fixed | `transfer` ran before the payout row existed. A crash or a lost RPC response after broadcast left funds sent and still spendable in the app. |
| F-3 | P0 | Fixed | Deposits were credited when daemon height was unknown, and when `block_height` was 0 (mempool / unconfirmed). `allow_locked_transactions` was requested and the confirmation check was skipped in those cases. |
| F-4 | P0 | Fixed | The scan watermark advanced to `height - 20` even when a payment was still short of `DEPOSIT_MIN_CONFIRMATIONS`. The next query used that cursor minus 5. With a confirmation setting above that window, a not-yet-final payment was never queried again. |
| F-5 | P0 | Fixed | Non-zero `unlock_time` was compared to chain height. Unix-time locks (values ≥ 500_000_000) are always above any realistic height, so those payments never matured after the timestamp passed. |
| F-6 | P1 | Fixed | Confirmations used `height - block_height + 1`. For Zano `getinfo` (chain size) that is one confirmation early. The scanner now uses `height - block_height`, which never credits sooner than the old formula. |
| F-7 | P1 | Fixed | Incoming `asset_id`, when the RPC sends it, was ignored. A non-PDC asset on the donation payment id could increase PDC escrow. Missing `asset_id` is still credited (see residual risk). |
| F-8 | P1 | Fixed | Two overlapping address assignments could overwrite `payment_id` after the first integrated address was already handed out. Assignment is now a conditional update. |
| F-9 | P1 | Fixed | `user_count() == 0` outside the insert could promote two concurrent first registrations to admin across processes. The check now runs inside `BEGIN IMMEDIATE`. |
| F-10 | P1 | Fixed | `get_user` raised `KeyError` for a stale session id, so every later page returned 500. |
| F-11 | P1 | Fixed | `NaN`, `Infinity`, and amounts that do not fit in a signed 64-bit SQLite integer were not rejected in `parse_amount`. |
| F-12 | P1 | Fixed | A down Trade price URL blocked every page on a 20s timeout, because failures were not cached. Prices are display-only. |
| F-13 | P1 | Fixed | Passwords longer than 72 bytes make bcrypt raise; registration surfaced that as a generic 500. They are rejected up front. |
| F-14 | P1 | Fixed | Binding a non-loopback host with a known placeholder `SECRET_KEY` is refused at startup. Loopback dev still starts. |
| F-15 | P1 | Fixed | A malformed `get_bulk_payments` element aborted the whole scan. Bad elements are skipped. Hex tx ids are normalized so `ABC` and `abc` cannot credit twice. |
| F-16 | P2 | Fixed | Clickjacking of the payout form. Responses send `X-Frame-Options: DENY` and `frame-ancestors 'none'`. |
| F-17 | P2 | Fixed | Proposal body, payout note, and payout address had no upper bound. |
| F-18 | P2 | Not changed | Custodial pooled wallet. Fees and outputs are not segregated per proposal. The operator key spends every donation. |
| F-19 | P2 | Not changed | A reorg that removes a credited tx does not decrease `deposits`. Uniqueness prevents a second credit of the same tx id; a replaced tx id can credit again. |
| F-20 | P2 | Not changed | Duplicate RPC rows for one `(tx_hash, payment_id)` keep the larger amount, not the sum. Summing would double-credit if the node repeats one output. Two real outputs in one tx can be under-counted. |
| F-21 | P2 | Not changed | Status `completed` and `disabled` are not scanned. A donation that confirms only after that transition is invisible until an operator sets the watermark back and returns the proposal to `funding` or `wip`. |
| F-22 | P2 | Partial | TOTP 2FA on login/register. CSRF tokens on payout/status/comment/admin-user POSTs. Still no login rate limit. Cross-site POST cookies withheld by `SameSite=Lax`. |
| F-27 | P0 | Fixed | Payout/status/user-admin required only `is_admin`. A forged or accidental second admin row could pay out. Now requires primary bootstrap username + `is_admin`. |
| F-28 | P1 | Fixed | With `BOOTSTRAP_ADMIN_PASSWORD` set, public registration no longer auto-promotes the first user to admin. |
| F-23 | P2 | Not changed | Session cookie is not `Secure`. Set a TLS terminator and a future `Secure` flag before exposing the app on the public internet. Placeholder secrets on loopback are still accepted for dry-run. |
| F-24 | P2 | Not changed | If `get_bulk_payments` omits `asset_id`, the scanner cannot tell PDC from another asset. |
| F-25 | P2 | Not changed | The first registered user is admin when `users` is empty and no bootstrap password was set. That is the documented bootstrap path; only the double-admin race was fixed. |
| F-26 | P2 | Not changed | `parse_amount` treats `1,5` as `1.5` and `1,000` as `1` (comma → dot). Pre-existing. |

## Fixes operators will notice

- Payout UI shows `pending` until the wallet returns a tx id and the row is finalized. A pending row counts toward paid escrow.
- Crediting waits for chain height. A dead daemon pauses credits; it does not post them.
- The first live scan after this upgrade rewinds `deposit_scan_min_height` to `0` once. That can be a large `get_bulk_payments` call. Credits stay idempotent.
- `python -m funding` exits if `BIND_HOST` is not loopback and `SECRET_KEY` is a known placeholder (`dev-only-change-me`, `change-me-to-a-long-random-string`, `changeme`, `secret`, `test`, `test-secret`).

## Reconcile a pending payout

The reservation id is in the admin error text and in `payouts.id`.

Tx never broadcast (wallet log shows no tx):

```sql
DELETE FROM payouts WHERE id = ? AND tx_hash LIKE 'pending:%';
```

Tx is on chain:

```sql
UPDATE payouts
SET tx_hash = '<txid>'
WHERE id = ? AND tx_hash LIKE 'pending:%';
```

Do not delete a row after a broadcast. That makes the same balance spendable again.

Force another full deposit scan (the one-time migration flag will not rewind again):

```sql
UPDATE meta SET value = '0' WHERE key = 'deposit_scan_min_height';
```

## How to verify

From a clean checkout, with Python 3.12:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest -q
```

Expected: all tests pass (25 in this change; the pre-change suite was 6). The suite does not use a live wallet, daemon, or Trade. Price-failure coverage stubs HTTP.

What the new tests lock in:

- Overlapping payouts of the full balance: one broadcast, one `ValueError`, available escrow `0`.
- Wallet reject (`WalletRejected`): reservation removed, balance restored.
- Ambiguous wallet error (`WalletError`, covered with a timeout): reservation kept, second payout refused. A transfer response with no tx id uses that same path and is not covered by its own test.
- Short confirmations, unknown height, mempool (`block_height == 0`), future unix `unlock_time`, and a non-PDC `asset_id` do not credit. A later height or a past timestamp does.
- Tx id case folds to one deposit. Donation address conditional update keeps the first row. Two racing registrations produce one admin.
- Placeholder secret on `0.0.0.0` raises `SystemExit`. Loopback does not.
- Stale session user id renders the home page (HTTP 200) instead of 500.
- Pending payout HTML contains `pending` and does not print the `pending:` token.
- Security headers: `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `frame-ancestors 'none'`.

Dry-run UI (no chain I/O):

```bash
cp .env.example .env
# set BOOTSTRAP_ADMIN_PASSWORD
python -m funding
```

Open `http://127.0.0.1:8088`. Register or use the bootstrap admin, submit a proposal, move it to funding, and confirm the integrated address appears. A normal dry-run payout finalizes immediately (`dryrun_…` tx id). Pending is the ambiguous-RPC path covered by `tests/test_payout_safety.py`.

Before any public bind: set a long random `SECRET_KEY`, set `BOOTSTRAP_ADMIN_PASSWORD`, point `WALLET_RPC_URL` at a thin escrow wallet, and keep surplus cold. Do not deploy on the basis of this review alone.
